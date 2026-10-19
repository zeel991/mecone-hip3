"""Client for Mecone's public index API (the one mecone.trade/benchmarks reads).

Requests are cached on disk and kept light: one catalog call, plus history and
latest-snapshot calls per index. Mecone recalculates roughly every 5 minutes,
so polling faster than that only repeats the same value.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE = "https://api.mecone.trade/api/v1"
CACHE = Path(__file__).resolve().parents[2] / "data"
UA = {"User-Agent": "mecone-hip3-listing-kit/0.1 (research; contact via GitHub)", "Accept": "application/json"}


def _get(path: str, retries: int = 4) -> dict:
    req = urllib.request.Request(BASE + path, headers=UA)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                body = e.read().decode(errors="replace")[:300]
                raise RuntimeError(f"Mecone API {e.code} for {path}: {body}") from e
            if attempt == retries - 1:
                raise
            time.sleep(2**attempt)
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class Index:
    symbol: str  # e.g. MOAI
    slug: str
    name: str
    category: str
    unit: str
    value: float  # latest snapshot
    computed_at: datetime
    oracle_eligible: bool
    publication: str  # "active" or "paused"
    freshness_state: str | None  # "fresh", "stale", "expired"... None when the index has no anchor guard
    source_venues: tuple[tuple[str, float], ...]  # (venue, lifetime USD volume of the source contracts)
    raw: dict

    @property
    def source_volume_usd(self) -> float:
        return sum(v for _, v in self.source_venues)


def _parse(x: dict) -> Index:
    latest = x["catalog_metadata"]["release"].get("latest") or {}
    guard = latest.get("freshness_guard") or {}
    return Index(
        symbol=x["symbol"],
        slug=x["slug"],
        name=x["name"],
        category=x.get("category") or "",
        unit=x.get("unit") or "",
        value=float(x["latest_snapshot"]["value"]["number"]),
        computed_at=pd.Timestamp(x["latest_snapshot"]["computed_at"]).to_pydatetime(),
        oracle_eligible=bool(x["status"]["oracle_eligible"]),
        publication=x["status"]["publication"],
        freshness_state=guard.get("state"),
        source_venues=tuple((s["name"], float(s["value"])) for s in x["catalog_metadata"].get("source_volume_totals", [])),
        raw=x,
    )


def indices() -> list[Index]:
    return [_parse(x) for x in _get("/indices")["data"]]


def snapshot(slug: str) -> dict:
    """Latest computed value: {value, computed_at, oracle_eligible, status}."""
    d = _get(f"/indices/{slug}/snapshots/latest")["data"]
    return {
        "value": float(d["value"]["number"]),
        "computed_at": pd.Timestamp(d["clocks"]["computed_at"]).to_pydatetime(),
        "oracle_eligible": bool(d["eligibility"]["oracle_eligible"]),
        "reason": d["eligibility"].get("reason"),
        "status": d.get("status"),
        "round_id": d.get("round_id"),
    }


# range -> resolution combinations the API accepts (finer ones are rejected)
WINDOWS = {"5m": ("1w", "5m"), "1h": ("1m", "1h"), "1d": ("1y", "1d")}


def history(slug: str, window: str, refresh: bool = False) -> pd.DataFrame:
    """OHLC series for one index. window is "5m" (last week), "1h" (last month) or "1d" (since launch)."""
    rng, res = WINDOWS[window]
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{slug}_{window}.json"
    if path.exists() and not refresh:
        payload = json.loads(path.read_text())
    else:
        payload = _get(f"/indices/{slug}/history?range={rng}&resolution={res}")
        path.write_text(json.dumps(payload))
    df = pd.DataFrame(payload["data"]["series"])
    if df.empty:
        return pd.DataFrame(columns=["t", "open", "high", "low", "close"])
    df["t"] = pd.to_datetime(df["t"], utc=True)
    return df[["t", "open", "high", "low", "close"]].sort_values("t").reset_index(drop=True)
