from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests
import yaml

try:
    import structlog
except ImportError:  # pragma: no cover
    class _Logger:
        def info(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class _Structlog:
        @staticmethod
        def get_logger(_name: str) -> _Logger:
            return _Logger()

    structlog = _Structlog()  # type: ignore[assignment]

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

try:
    import matplotlib.pyplot as plt
except ImportError:  # pragma: no cover
    plt = None

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    Image = None
    ImageDraw = None

try:
    from _df_common.atomic_io import atomic_append_jsonl, atomic_write_bytes, atomic_write_text
except ImportError:
    def atomic_write_bytes(path: str | Path, data: bytes) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False, dir=target.parent) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_path = Path(handle.name)
        os.replace(tmp_path, target)

    def atomic_write_text(path: str | Path, text: str) -> None:
        atomic_write_bytes(path, text.encode("utf-8"))

    def atomic_append_jsonl(path: str | Path, record: Any) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


TRUSTPILOT_ENV = "DF_HLM_3_REAL_TRUSTPILOT_ENABLED"
BOOKING_ENV = "DF_HLM_3_REAL_BOOKING_PARTNER_ENABLED"
PHRONESIS_ENV = "PHRONESIS_TICKET"

MOCK_NUMBERS: dict[str, dict[str, float | int]] = {
    "heylou-hildesheim": {"stars": 4.2, "position": 2, "price_low": 81, "price_high": 118, "review_count": 280},
    "heylou-frankfurt": {"stars": 4.1, "position": 4, "price_low": 94, "price_high": 146, "review_count": 325},
    "heylou-karlsruhe": {"stars": 4.3, "position": 3, "price_low": 88, "price_high": 129, "review_count": 211},
    "heylou-hannover": {"stars": 4.0, "position": 5, "price_low": 76, "price_high": 111, "review_count": 164},
    "heylou-darmstadt": {"stars": 4.1, "position": 6, "price_low": 89, "price_high": 133, "review_count": 140},
    "heylou-pforzheim": {"stars": 4.0, "position": 4, "price_low": 73, "price_high": 108, "review_count": 129},
    "heylou-mitte": {"stars": 4.2, "position": 3, "price_low": 97, "price_high": 149, "review_count": 302},
}


class TrackerMode(str, Enum):
    FULL = "full"
    DEGRADED_TRUSTPILOT = "degraded_trustpilot_api"
    DEGRADED_BOOKING = "degraded_booking_api"
    WEB_FALLBACK = "web_scraping_fallback"
    CACHE_ONLY = "standalone_cache_only"


class CircuitBreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    timeout_s: float = 30.0
    open_threshold: int = 3
    half_open_test_interval_s: float = 60.0
    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    fail_count: int = 0
    last_open_ts: float | None = None

    def record_success(self) -> None:
        self.fail_count = 0
        self.state = CircuitBreakerState.CLOSED
        self.last_open_ts = None

    def record_failure(self) -> None:
        self.fail_count += 1
        if self.fail_count >= self.open_threshold:
            self.state = CircuitBreakerState.OPEN
            self.last_open_ts = time.time()

    def should_attempt(self) -> bool:
        if self.state == CircuitBreakerState.CLOSED:
            return True
        if self.state == CircuitBreakerState.OPEN:
            if self.last_open_ts is None:
                return False
            if time.time() - self.last_open_ts >= self.half_open_test_interval_s:
                self.state = CircuitBreakerState.HALF_OPEN
                return True
            return False
        return True


@dataclass(frozen=True)
class HotelTarget:
    hotel_id: str
    hotel_name: str
    brand: str
    region: str
    is_heylou: bool


@dataclass
class HotelSnapshot:
    snapshot_date: str
    hotel_id: str
    hotel_name: str
    brand: str
    region: str
    is_heylou: bool
    stars: float
    position: int
    price_low: int
    price_high: int
    review_count: int
    mode: str
    fetched_at_iso: str
    confidence_score: float
    provenance: list[dict[str, Any]] = field(default_factory=list)

    def to_csv_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["provenance"] = json.dumps(self.provenance, ensure_ascii=False)
        return row


class SecretVault:
    @staticmethod
    def resolve_env(name: str) -> str | None:
        value = os.environ.get(name)
        return value.strip() if value else None


class KonkurrenzTracker:
    DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        output_root: str | Path | None = None,
        session: requests.Session | Any | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime] | None = None,
        skip_mutex_for_tests: bool = False,
    ) -> None:
        self.config = config or self.load_config(self.DEFAULT_CONFIG)
        self.output_root = Path(output_root or Path(__file__).resolve().parents[1])
        self.session = session or requests.Session()
        self.sleep_fn = sleep_fn
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.skip_mutex_for_tests = skip_mutex_for_tests
        ops = self.config["operations"]
        lc3 = self.config["lose_coupling"]["LC3_circuit_breaker"]
        self.allowed_domains = set(ops["allowed_domains"])
        self.timeout_s = float(lc3["timeout_s"])
        self.rate_limit_backoff_s = float(lc3["rate_limit_backoff_s"])
        self.lock_dir = Path(self.config["k16_concurrent_spawn_mutex"]["lock_dir"])
        self.audit_log_path = self.output_root / ops["audit_log_path"]
        self.dlq_dir = self.output_root / ops["dlq_dir"]
        self.daily_dir = self.output_root / ops["daily_output_dir"]
        self.heatmap_dir = self.output_root / ops["heatmap_dir"]
        self.slack_dir = self.output_root / ops["slack_alert_dir"]
        self.health_file = Path(ops["health_file_path"])
        self.logger = structlog.get_logger("df_hlm_3")
        self.circuit_breakers = {
            "trustpilot": CircuitBreaker(timeout_s=self.timeout_s, open_threshold=int(lc3["open_threshold"])),
            "booking": CircuitBreaker(timeout_s=self.timeout_s, open_threshold=int(lc3["open_threshold"])),
            "web_scraping": CircuitBreaker(timeout_s=self.timeout_s, open_threshold=int(lc3["open_threshold"])),
        }
        self.targets = [HotelTarget(**item) for item in self.config["source_catalog"]["hotels"]]

    @staticmethod
    def load_config(path: str | Path) -> dict[str, Any]:
        with Path(path).open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    def real_mode_enabled(self) -> bool:
        ticket = SecretVault.resolve_env(PHRONESIS_ENV)
        return (
            SecretVault.resolve_env(TRUSTPILOT_ENV) == "true"
            and SecretVault.resolve_env(BOOKING_ENV) == "true"
            and bool(ticket)
            and ticket.startswith("PT-2026-")
        )

    def health_check(self) -> dict[str, Any]:
        payload = {"dependencies": [], "score": 1.0, "healthy": True, "mode": self.current_mode(False, False, False).value}
        atomic_write_text(self.health_file, json.dumps(payload, indent=2) + "\n")
        return payload

    def validate_allowed_domain(self, url: str) -> str:
        host = urlparse(url).netloc.lower()
        if not any(host == allowed or host.endswith(f".{allowed}") for allowed in self.allowed_domains):
            raise ValueError(f"K13 domain check failed: {host}")
        return host

    def acquire_mutex(self) -> bool:
        if self.skip_mutex_for_tests:
            return True
        try:
            self.lock_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            return False
        atomic_write_text(self.lock_dir / "pid", str(os.getpid()))
        return True

    def release_mutex(self) -> None:
        if self.lock_dir.exists():
            for child in self.lock_dir.iterdir():
                child.unlink()
            self.lock_dir.rmdir()

    def current_mode(self, trustpilot_ok: bool, booking_ok: bool, scraped_ok: bool) -> TrackerMode:
        if not self.real_mode_enabled():
            return TrackerMode.CACHE_ONLY
        if trustpilot_ok and booking_ok:
            return TrackerMode.FULL
        if trustpilot_ok:
            return TrackerMode.DEGRADED_BOOKING
        if booking_ok:
            return TrackerMode.DEGRADED_TRUSTPILOT
        if scraped_ok:
            return TrackerMode.WEB_FALLBACK
        return TrackerMode.CACHE_ONLY

    def _request(self, source: str, url: str) -> requests.Response | Any:
        breaker = self.circuit_breakers[source]
        if not breaker.should_attempt():
            raise RuntimeError(f"{source} circuit breaker open")
        self.validate_allowed_domain(url)
        last_error: Exception | None = None
        for _ in range(3):
            try:
                response = self.session.get(url, timeout=self.timeout_s)
                if getattr(response, "status_code", 200) == 429:
                    breaker.record_failure()
                    self.sleep_fn(self.rate_limit_backoff_s)
                    continue
                if getattr(response, "status_code", 200) >= 400:
                    raise requests.HTTPError(f"{source} status={response.status_code}")
                breaker.record_success()
                return response
            except (requests.RequestException, RuntimeError) as exc:
                last_error = exc
                breaker.record_failure()
        raise RuntimeError(f"{source} request failed: {last_error}")

    def _mock_metrics(self, target: HotelTarget) -> dict[str, Any]:
        data = MOCK_NUMBERS.get(target.hotel_id, {"stars": 4.0, "position": 7, "price_low": 79, "price_high": 119, "review_count": 100})
        return {**data, "source": "mock", "confidence_score": 0.55}

    def _parse_json(self, response: Any) -> dict[str, Any]:
        return response.json() if hasattr(response, "json") else json.loads(response.text)

    def _scrape_metrics(self, target: HotelTarget) -> dict[str, Any]:
        response = self._request("web_scraping", f"https://www.booking.com/hotel/{target.hotel_id}")
        html = response.text
        if BeautifulSoup is not None:
            soup = BeautifulSoup(html, "html.parser")
            rating_tag = soup.select_one("[data-rating]")
            position_tag = soup.select_one("[data-position]")
            price_tag = soup.select_one("[data-price-range]")
            if rating_tag and position_tag and price_tag:
                low, high = [int(x) for x in price_tag["data-price-range"].split("-")]
                return {
                    "stars": float(rating_tag["data-rating"]),
                    "position": int(position_tag["data-position"]),
                    "price_low": low,
                    "price_high": high,
                    "review_count": 0,
                    "source": "web_scraping",
                    "confidence_score": 0.7,
                }
        match = re.search(r"rating=(?P<rating>\d+\.\d+);position=(?P<position>\d+);price=(?P<low>\d+)-(?P<high>\d+)", html)
        if not match:
            raise ValueError(f"scrape parse failed for {target.hotel_id}")
        return {
            "stars": float(match.group("rating")),
            "position": int(match.group("position")),
            "price_low": int(match.group("low")),
            "price_high": int(match.group("high")),
            "review_count": 0,
            "source": "web_scraping",
            "confidence_score": 0.7,
        }

    def _fetch_trustpilot(self, target: HotelTarget) -> dict[str, Any]:
        payload = self._parse_json(self._request("trustpilot", f"https://api.trustpilot.com/v1/business-units/{target.hotel_id}"))
        return {
            "stars": float(payload["stars"]),
            "review_count": int(payload.get("review_count", 0)),
            "source": "trustpilot_api",
            "confidence_score": 0.95,
        }

    def _fetch_booking(self, target: HotelTarget) -> dict[str, Any]:
        payload = self._parse_json(self._request("booking", f"https://distribution-xml.booking.com/json/bookings/{target.hotel_id}"))
        return {
            "stars": float(payload["stars"]),
            "position": int(payload["position"]),
            "price_low": int(payload["price_low"]),
            "price_high": int(payload["price_high"]),
            "source": "booking_partner_api",
            "confidence_score": 0.95,
        }

    def _append_dlq(self, target: HotelTarget, source: str, error: Exception) -> None:
        atomic_append_jsonl(self.dlq_dir / f"{target.hotel_id}.jsonl", {"hotel_id": target.hotel_id, "source": source, "error": str(error), "ts": self.now_fn().isoformat()})

    def _build_snapshot(self, target: HotelTarget, snapshot_date: str, mode: TrackerMode, trustpilot: dict[str, Any] | None, booking: dict[str, Any] | None, scraped: dict[str, Any] | None) -> HotelSnapshot:
        fallback = self._mock_metrics(target)
        provenance: list[dict[str, Any]] = []
        merged: dict[str, Any] = {}
        for item in (trustpilot, booking, scraped):
            if item:
                merged.update(item)
                provenance.append({"source": item["source"], "timestamp": self.now_fn().isoformat(), "confidence_score": item["confidence_score"]})
        if not merged:
            merged = fallback
            provenance = [{"source": "mock_seed", "timestamp": self.now_fn().isoformat(), "confidence_score": 0.55}]
        stars = float(merged.get("stars", fallback["stars"]))
        return HotelSnapshot(
            snapshot_date=snapshot_date,
            hotel_id=target.hotel_id,
            hotel_name=target.hotel_name,
            brand=target.brand,
            region=target.region,
            is_heylou=target.is_heylou,
            stars=stars,
            position=int(merged.get("position", fallback["position"])),
            price_low=int(merged.get("price_low", fallback["price_low"])),
            price_high=int(merged.get("price_high", fallback["price_high"])),
            review_count=int(merged.get("review_count", fallback["review_count"])),
            mode=mode.value,
            fetched_at_iso=self.now_fn().isoformat(),
            confidence_score=float(merged.get("confidence_score", fallback["confidence_score"])),
            provenance=provenance,
        )

    def _latest_baseline(self, snapshot_date: str) -> dict[str, HotelSnapshot]:
        rows: dict[str, HotelSnapshot] = {}
        if not self.daily_dir.exists():
            return rows
        candidates = sorted(p for p in self.daily_dir.glob("*.csv") if p.stem < snapshot_date)
        if not candidates:
            return rows
        with candidates[-1].open("r", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                rows[row["hotel_id"]] = HotelSnapshot(
                    snapshot_date=row["snapshot_date"],
                    hotel_id=row["hotel_id"],
                    hotel_name=row["hotel_name"],
                    brand=row["brand"],
                    region=row["region"],
                    is_heylou=row["is_heylou"] == "True",
                    stars=float(row["stars"]),
                    position=int(row["position"]),
                    price_low=int(row["price_low"]),
                    price_high=int(row["price_high"]),
                    review_count=int(row["review_count"]),
                    mode=row["mode"],
                    fetched_at_iso=row["fetched_at_iso"],
                    confidence_score=float(row["confidence_score"]),
                    provenance=json.loads(row["provenance"]),
                )
        return rows

    def build_alerts(self, snapshots: list[HotelSnapshot], baseline: dict[str, HotelSnapshot]) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        for snapshot in snapshots:
            prior = baseline.get(snapshot.hotel_id)
            if prior is None:
                continue
            rating_delta = round(snapshot.stars - prior.stars, 2)
            position_delta = snapshot.position - prior.position
            if snapshot.is_heylou and rating_delta <= -0.3:
                alerts.append({"alert_type": "heylou_rating_drop", "hotel_id": snapshot.hotel_id, "delta": rating_delta, "severity": "critical"})
            if not snapshot.is_heylou and rating_delta >= 0.5:
                alerts.append({"alert_type": "competitor_rating_rise", "hotel_id": snapshot.hotel_id, "delta": rating_delta, "severity": "critical"})
            if snapshot.is_heylou and position_delta > 0:
                alerts.append({"alert_type": "market_position_drop", "hotel_id": snapshot.hotel_id, "delta": position_delta, "severity": "critical"})
        return alerts

    def _write_csv(self, snapshots: list[HotelSnapshot], snapshot_date: str) -> Path:
        path = self.daily_dir / f"{snapshot_date}.csv"
        fieldnames = list(snapshots[0].to_csv_row().keys())
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        for snapshot in snapshots:
            writer.writerow(snapshot.to_csv_row())
        atomic_write_text(path, buf.getvalue())
        return path

    def _write_slack_json(self, snapshot_date: str, mode: TrackerMode, alerts: list[dict[str, Any]]) -> Path:
        path = self.slack_dir / f"{snapshot_date}-alerts.json"
        payload = {"snapshot_date": snapshot_date, "mode": mode.value, "generated_at_iso": self.now_fn().isoformat(), "alerts": alerts}
        atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return path

    def _write_heatmap(self, snapshots: list[HotelSnapshot], snapshot_date: str) -> tuple[Path, dict[str, Any]]:
        regions = sorted({snapshot.region for snapshot in snapshots})
        rows = sorted(snapshots, key=lambda item: (item.region, item.hotel_name))
        matrix = [[snapshot.stars] for snapshot in rows]
        path = self.heatmap_dir / f"{snapshot_date}.png"
        if plt is not None:
            fig, ax = plt.subplots(figsize=(6, max(3, len(rows) * 0.35)))
            ax.imshow(matrix, cmap="YlOrRd", aspect="auto", vmin=2.5, vmax=5.0)
            ax.set_yticks(range(len(rows)), [f"{item.hotel_name} ({item.region})" for item in rows])
            ax.set_xticks([0], ["stars"])
            ax.set_title(f"DF-HLM-3 Heatmap {snapshot_date}")
            fig.tight_layout()
            buf = io.BytesIO()
            fig.savefig(buf, format="png")
            plt.close(fig)
            atomic_write_bytes(path, buf.getvalue())
        elif Image is not None and ImageDraw is not None:  # pragma: no cover
            image = Image.new("RGB", (900, max(200, len(rows) * 28)), "white")
            draw = ImageDraw.Draw(image)
            for index, snapshot in enumerate(rows):
                draw.text((10, 10 + index * 22), f"{snapshot.hotel_name}: {snapshot.stars}", fill="black")
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            atomic_write_bytes(path, buf.getvalue())
        else:  # pragma: no cover
            atomic_write_bytes(path, b"PNG")
        return path, {"regions": regions, "shape": [len(rows), 1], "mode": snapshots[0].mode}

    def run(self, snapshot_date: str | None = None) -> dict[str, Any]:
        snap_date = snapshot_date or self.now_fn().date().isoformat()
        snapshots: list[HotelSnapshot] = []
        trustpilot_ok = False
        booking_ok = False
        scraped_ok = False
        for target in self.targets:
            tp: dict[str, Any] | None = None
            bk: dict[str, Any] | None = None
            sc: dict[str, Any] | None = None
            if self.real_mode_enabled():
                try:
                    tp = self._fetch_trustpilot(target)
                    trustpilot_ok = True
                except Exception as exc:
                    self._append_dlq(target, "trustpilot", exc)
                try:
                    bk = self._fetch_booking(target)
                    booking_ok = True
                except Exception as exc:
                    self._append_dlq(target, "booking", exc)
                if tp is None and bk is None:
                    try:
                        sc = self._scrape_metrics(target)
                        scraped_ok = True
                    except Exception as exc:
                        self._append_dlq(target, "web_scraping", exc)
            mode = self.current_mode(trustpilot_ok, booking_ok, scraped_ok)
            snapshots.append(self._build_snapshot(target, snap_date, mode, tp, bk, sc))
        baseline = self._latest_baseline(snap_date)
        alerts = self.build_alerts(snapshots, baseline)
        csv_path = self._write_csv(snapshots, snap_date)
        heatmap_path, heatmap_dict = self._write_heatmap(snapshots, snap_date)
        slack_path = self._write_slack_json(snap_date, self.current_mode(trustpilot_ok, booking_ok, scraped_ok), alerts)
        audit = {"event": "tracker_run", "snapshot_date": snap_date, "mode": self.current_mode(trustpilot_ok, booking_ok, scraped_ok).value, "alerts": len(alerts), "count": len(snapshots), "ts": self.now_fn().isoformat()}
        self.logger.info("tracker_run", **audit)
        atomic_append_jsonl(self.audit_log_path, audit)
        return {
            "mode": self.current_mode(trustpilot_ok, booking_ok, scraped_ok).value,
            "csv_path": str(csv_path),
            "heatmap_path": str(heatmap_path),
            "slack_alert_path": str(slack_path),
            "alerts": alerts,
            "heatmap": heatmap_dict,
            "snapshots": snapshots,
        }


def main() -> int:
    tracker = KonkurrenzTracker()
    result = tracker.run()
    print(json.dumps({"mode": result["mode"], "csv_path": result["csv_path"], "heatmap_path": result["heatmap_path"], "slack_alert_path": result["slack_alert_path"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
