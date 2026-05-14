from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest


def load_module():
    root = Path(__file__).resolve().parents[1]
    module_path = root / "src" / "konkurrenz_tracker.py"
    spec = importlib.util.spec_from_file_location("df_hlm_3_tracker", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, timeout=30):
        self.calls.append((url, timeout))
        return self.handler(url, timeout)


def build_config(tmp_path):
    return {
        "k16_concurrent_spawn_mutex": {"lock_dir": str(tmp_path / "lock"), "engine_pgrep_check": True},
        "lose_coupling": {"LC3_circuit_breaker": {"timeout_s": 30, "open_threshold": 3, "rate_limit_backoff_s": 60}},
        "operations": {
            "audit_log_path": "audit/df-hlm-3-audit.jsonl",
            "dlq_dir": "runs/dlq",
            "daily_output_dir": "outputs/daily",
            "heatmap_dir": "outputs/heatmaps",
            "slack_alert_dir": "outputs/slack",
            "health_file_path": str(tmp_path / "health.json"),
            "allowed_domains": ["api.trustpilot.com", "distribution-xml.booking.com", "booking.com", "www.booking.com", "trustpilot.com"],
        },
        "source_catalog": {
            "hotels": [
                {"hotel_id": "heylou-hildesheim", "hotel_name": "HeyLou Hildesheim", "brand": "HeyLou", "region": "Hildesheim", "is_heylou": True},
                {"hotel_id": "motel-one-hildesheim", "hotel_name": "Motel One Hildesheim", "brand": "Motel One", "region": "Hildesheim", "is_heylou": False},
            ]
        },
    }


def make_tracker(tmp_path, monkeypatch, session=None, sleep_fn=lambda _s: None):
    module = load_module()
    now = lambda: datetime(2026, 5, 14, 4, 0, tzinfo=timezone.utc)
    tracker = module.KonkurrenzTracker(build_config(tmp_path), output_root=tmp_path, session=session, sleep_fn=sleep_fn, now_fn=now)
    return module, tracker


def enable_real_mode(monkeypatch):
    monkeypatch.setenv("DF_HLM_3_REAL_TRUSTPILOT_ENABLED", "true")
    monkeypatch.setenv("DF_HLM_3_REAL_BOOKING_PARTNER_ENABLED", "true")
    monkeypatch.setenv("PHRONESIS_TICKET", "PT-2026-05-001")


def snapshot(module, hotel_id, is_heylou, stars, position):
    return module.HotelSnapshot(
        snapshot_date="2026-05-14",
        hotel_id=hotel_id,
        hotel_name=hotel_id,
        brand="HeyLou" if is_heylou else "Comp",
        region="Hildesheim",
        is_heylou=is_heylou,
        stars=stars,
        position=position,
        price_low=80,
        price_high=120,
        review_count=10,
        mode="full",
        fetched_at_iso="2026-05-14T04:00:00+00:00",
        confidence_score=0.9,
        provenance=[{"source": "trustpilot_api", "timestamp": "2026-05-14T04:00:00+00:00", "confidence_score": 0.9}],
    )


def test_default_mock_mode_no_api_call(tmp_path, monkeypatch):
    session = FakeSession(lambda _url, _timeout: (_ for _ in ()).throw(AssertionError("should not be called")))
    _, tracker = make_tracker(tmp_path, monkeypatch, session=session)
    result = tracker.run("2026-05-14")
    assert result["mode"] == "standalone_cache_only"
    assert session.calls == []


def test_env_var_true_real_mode(tmp_path, monkeypatch):
    enable_real_mode(monkeypatch)
    module, tracker = make_tracker(tmp_path, monkeypatch, session=FakeSession(lambda url, _timeout: FakeResponse(payload={"stars": 4.6, "review_count": 100}) if "trustpilot" in url else FakeResponse(payload={"stars": 4.4, "position": 1, "price_low": 89, "price_high": 129})))
    assert tracker.real_mode_enabled() is True
    assert tracker.run("2026-05-14")["mode"] == module.TrackerMode.FULL.value


def test_concurrent_spawn_protection(tmp_path, monkeypatch):
    _, tracker_a = make_tracker(tmp_path, monkeypatch)
    _, tracker_b = make_tracker(tmp_path, monkeypatch)
    assert tracker_a.acquire_mutex() is True
    assert tracker_b.acquire_mutex() is False
    tracker_a.release_mutex()


def test_cascade_containment(tmp_path, monkeypatch):
    enable_real_mode(monkeypatch)
    def handler(url, _timeout):
        if "trustpilot" in url:
            raise RuntimeError("tp down")
        return FakeResponse(payload={"stars": 4.3, "position": 2, "price_low": 90, "price_high": 120})
    _, tracker = make_tracker(tmp_path, monkeypatch, session=FakeSession(handler))
    result = tracker.run("2026-05-14")
    assert result["mode"] == "degraded_trustpilot_api"
    assert result["snapshots"][0].position == 2


def test_external_anchor_two_sources(tmp_path, monkeypatch):
    enable_real_mode(monkeypatch)
    _, tracker = make_tracker(tmp_path, monkeypatch, session=FakeSession(lambda url, _timeout: FakeResponse(payload={"stars": 4.7, "review_count": 50}) if "trustpilot" in url else FakeResponse(payload={"stars": 4.5, "position": 1, "price_low": 88, "price_high": 140})))
    result = tracker.run("2026-05-14")
    assert {item["source"] for item in result["snapshots"][0].provenance} == {"trustpilot_api", "booking_partner_api"}


def test_circuit_breaker_open(tmp_path, monkeypatch):
    enable_real_mode(monkeypatch)
    _, tracker = make_tracker(tmp_path, monkeypatch, session=FakeSession(lambda _url, _timeout: (_ for _ in ()).throw(RuntimeError("boom"))))
    with pytest.raises(RuntimeError):
        tracker._request("trustpilot", "https://api.trustpilot.com/v1/business-units/heylou-hildesheim")
    assert tracker.circuit_breakers["trustpilot"].state.value == "open"


def test_web_scraping_fallback(tmp_path, monkeypatch):
    enable_real_mode(monkeypatch)
    def handler(url, _timeout):
        if "booking.com/hotel/" in url:
            return FakeResponse(text='<div data-rating="4.4" data-position="3" data-price-range="90-130"></div>')
        raise RuntimeError("api down")
    _, tracker = make_tracker(tmp_path, monkeypatch, session=FakeSession(handler))
    result = tracker.run("2026-05-14")
    assert result["mode"] == "web_scraping_fallback"
    assert result["snapshots"][0].confidence_score == 0.7


def test_idempotent_daily_snapshot(tmp_path, monkeypatch):
    _, tracker = make_tracker(tmp_path, monkeypatch)
    first = tracker.run("2026-05-14")
    second = tracker.run("2026-05-14")
    assert Path(first["csv_path"]).read_text(encoding="utf-8") == Path(second["csv_path"]).read_text(encoding="utf-8")


def test_health_check_no_deps(tmp_path, monkeypatch):
    _, tracker = make_tracker(tmp_path, monkeypatch)
    health = tracker.health_check()
    assert health["dependencies"] == []


def test_rating_diff_critical_alert(tmp_path, monkeypatch):
    module, tracker = make_tracker(tmp_path, monkeypatch)
    current = [snapshot(module, "heylou-hildesheim", True, 4.0, 2)]
    baseline = {"heylou-hildesheim": snapshot(module, "heylou-hildesheim", True, 4.3, 2)}
    alerts = tracker.build_alerts(current, baseline)
    assert alerts[0]["alert_type"] == "heylou_rating_drop"


def test_competitor_rise_alert(tmp_path, monkeypatch):
    module, tracker = make_tracker(tmp_path, monkeypatch)
    current = [snapshot(module, "motel-one-hildesheim", False, 4.8, 1)]
    baseline = {"motel-one-hildesheim": snapshot(module, "motel-one-hildesheim", False, 4.2, 1)}
    alerts = tracker.build_alerts(current, baseline)
    assert alerts[0]["alert_type"] == "competitor_rating_rise"


def test_heatmap_generation_matplotlib(tmp_path, monkeypatch):
    _, tracker = make_tracker(tmp_path, monkeypatch)
    result = tracker.run("2026-05-14")
    assert Path(result["heatmap_path"]).exists()
    assert result["heatmap"]["shape"][1] == 1


def test_csv_output_format(tmp_path, monkeypatch):
    _, tracker = make_tracker(tmp_path, monkeypatch)
    result = tracker.run("2026-05-14")
    header = Path(result["csv_path"]).read_text(encoding="utf-8").splitlines()[0]
    assert "snapshot_date" in header and "provenance" in header


def test_slack_alert_json_schema(tmp_path, monkeypatch):
    _, tracker = make_tracker(tmp_path, monkeypatch)
    tracker.daily_dir.mkdir(parents=True, exist_ok=True)
    (tracker.daily_dir / "2026-05-13.csv").write_text("snapshot_date,hotel_id,hotel_name,brand,region,is_heylou,stars,position,price_low,price_high,review_count,mode,fetched_at_iso,confidence_score,provenance\n2026-05-13,heylou-hildesheim,HeyLou Hildesheim,HeyLou,Hildesheim,True,4.5,1,80,120,10,full,2026-05-13T04:00:00+00:00,0.9,[]\n", encoding="utf-8")
    result = tracker.run("2026-05-14")
    payload = json.loads(Path(result["slack_alert_path"]).read_text(encoding="utf-8"))
    assert sorted(payload.keys()) == ["alerts", "generated_at_iso", "mode", "snapshot_date"]


def test_rate_limit_backoff_60s(tmp_path, monkeypatch):
    enable_real_mode(monkeypatch)
    sleeps = []
    calls = {"n": 0}
    def handler(url, _timeout):
        if "trustpilot" in url:
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResponse(status_code=429)
            return FakeResponse(payload={"stars": 4.5, "review_count": 12})
        return FakeResponse(payload={"stars": 4.4, "position": 1, "price_low": 90, "price_high": 130})
    _, tracker = make_tracker(tmp_path, monkeypatch, session=FakeSession(handler), sleep_fn=lambda s: sleeps.append(s))
    tracker.run("2026-05-14")
    assert 60 in sleeps


def test_provenance_in_output(tmp_path, monkeypatch):
    _, tracker = make_tracker(tmp_path, monkeypatch)
    result = tracker.run("2026-05-14")
    assert "mock_seed" in Path(result["csv_path"]).read_text(encoding="utf-8")


def test_pre_action_domain_check(tmp_path, monkeypatch):
    _, tracker = make_tracker(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        tracker.validate_allowed_domain("https://evil.example.com/data")


def test_audit_log_appended_per_run(tmp_path, monkeypatch):
    _, tracker = make_tracker(tmp_path, monkeypatch)
    tracker.run("2026-05-14")
    tracker.run("2026-05-15")
    assert len(Path(tracker.audit_log_path).read_text(encoding="utf-8").strip().splitlines()) == 2
