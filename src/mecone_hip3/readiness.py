"""Listing readiness of a Mecone index as a HIP-3 perp, measured from Mecone's own history.

Each number answers a question an exchange (or HIP-3 deployer) asks before listing:
  * How often does it move 50%+ inside a UTC day?  HIP-3 validators review those moves for
    slashing, and assets doing it more than once a month cannot use cross margin.
  * How big are the 5-minute steps, and how long does a 1%-per-update mark take to catch up?
  * Does the value go flat for long stretches (no new information reaching the oracle)?
  * How large are the tail moves, and what leverage survives them?
  * How deep is the source market the index is built from?

Leverage rule (stated, not standard): at max leverage L an isolated position is liquidated after
an adverse move of about 1/(2L) (maintenance margin is half the initial margin). We require the
99.9th-percentile 24-hour move to fit inside that with a 1.5x safety factor, then round down to
a common tier.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

TIERS = (1, 2, 3, 5, 10, 20)
SAFETY = 1.5
THIN_SOURCE_USD = 100_000


def daily_extremes(h: pd.DataFrame) -> pd.Series:
    """Largest |price / start-of-day - 1| per UTC day, using hourly highs and lows."""
    if h.empty:
        return pd.Series(dtype=float)
    d = h.assign(day=h["t"].dt.floor("D"))
    out = {}
    for day, g in d.groupby("day"):
        sod = g["open"].iloc[0]
        if sod <= 0:
            continue
        out[day] = max(abs(g["high"].max() / sod - 1), abs(g["low"].min() / sod - 1))
    return pd.Series(out)


def daily_bar_extremes(d: pd.DataFrame) -> pd.Series:
    """Largest |price / day open - 1| per daily bar, from the daily highs and lows."""
    if d.empty:
        return pd.Series(dtype=float)
    o = d["open"].where(d["open"] > 0)
    ext = np.maximum((d["high"] / o - 1).abs(), (d["low"] / o - 1).abs())
    return pd.Series(ext.to_numpy(), index=d["t"]).dropna()


def one_print_spikes(m5: pd.DataFrame, min_move: float = 0.05, reversal: float = 0.7) -> list[tuple[str, float, float]]:
    """5-minute prints that jump at least `min_move` and give back `reversal` of it on the next print."""
    c = m5["close"].to_numpy(float)
    out = []
    for i in range(1, len(c) - 1):
        up, back = c[i] / c[i - 1] - 1, c[i + 1] / c[i] - 1
        if abs(up) >= min_move and up * back < 0 and abs(c[i + 1] - c[i]) >= reversal * abs(c[i] - c[i - 1]):
            out.append((str(m5["t"].iloc[i]), up * 100, back * 100))
    return out


def data_gaps(m5: pd.DataFrame, min_gap_min: float = 15.0) -> tuple[int, float]:
    """Gaps between consecutive 5-minute points longer than `min_gap_min`: (count, longest in minutes)."""
    if len(m5) < 2:
        return 0, 0.0
    g = m5["t"].diff().dt.total_seconds().dropna() / 60
    big = g[g > min_gap_min]
    return int(len(big)), float(big.max()) if len(big) else 0.0


def flat_stretches(m5: pd.DataFrame, min_points: int = 12) -> list[tuple[pd.Timestamp, pd.Timestamp, int]]:
    """Runs of identical 5-minute closes at least `min_points` long (an hour by default)."""
    if m5.empty:
        return []
    c = m5["close"].to_numpy()
    runs, start = [], 0
    for i in range(1, len(c) + 1):
        if i == len(c) or c[i] != c[start]:
            if i - start >= min_points:
                runs.append((m5["t"].iloc[start], m5["t"].iloc[i - 1], i - start))
            start = i
    return runs


def catch_up_seconds(step: float, interval_s: float = 3.0, limit: float = 0.01) -> float:
    """Seconds a mark clamped to `limit` per update needs to absorb a jump of `step`."""
    if step <= 0:
        return 0.0
    return math.ceil(math.log(1 + step) / math.log(1 + limit)) * interval_s


def liquidation_rate(h: pd.DataFrame, leverage: float, horizon_h: int = 168) -> float:
    """Share of hourly entries (long and short) liquidated within `horizon_h` hours, isolated margin."""
    if len(h) < 2:
        return float("nan")
    cl, hi, lo = (h[k].to_numpy(float) for k in ("close", "high", "low"))
    thr = 1 / (2 * leverage)
    hits = n = 0
    for i in range(len(cl) - 1):
        j = min(len(cl), i + 1 + horizon_h)
        e = cl[i]
        if e <= 0:
            continue
        n += 2
        hits += int(lo[i + 1 : j].min() <= e * (1 - thr))
        hits += int(hi[i + 1 : j].max() >= e * (1 + thr))
    return hits / n if n else float("nan")


def max_leverage(q999_24h: float) -> int:
    if not np.isfinite(q999_24h) or q999_24h <= 0:
        return TIERS[-1]
    raw = 1 / (2 * SAFETY * q999_24h)
    return max([t for t in TIERS if t <= raw] or [1])


def assess(index, m5: pd.DataFrame, h: pd.DataFrame, d: pd.DataFrame) -> dict:
    de = daily_extremes(h)  # hourly bars, last month
    dd = daily_bar_extremes(d)  # daily bars, full history: the larger sample for the 50% rule
    days = len(dd)
    big = int((dd >= 0.5).sum())
    spikes = one_print_spikes(m5)
    r5 = m5["close"].pct_change().abs().dropna() if len(m5) > 1 else pd.Series(dtype=float)
    rh = np.log(h["close"]).diff().dropna() if len(h) > 1 else pd.Series(dtype=float)
    r24 = (h["close"].pct_change(24).abs().dropna()) if len(h) > 24 else pd.Series(dtype=float)
    q999_24h = float(r24.quantile(0.999)) if len(r24) else float("nan")
    flats = flat_stretches(m5)
    biggest5 = float(r5.max()) if len(r5) else 0.0
    top_steps = m5.assign(step=m5["close"].pct_change()).dropna().reindex(r5.sort_values(ascending=False).index[:3]) if len(r5) else m5.head(0)
    lev = max_leverage(q999_24h)
    per_month = big / days * 30 if days else float("nan")
    thin = index.source_volume_usd < THIN_SOURCE_USD
    cross_ok = days >= 30 and per_month <= 1 and not thin
    return {
        "symbol": index.symbol,
        "name": index.name,
        "category": index.category,
        "unit": index.unit,
        "value": index.value,
        "history_start": str(d["t"].iloc[0].date()) if len(d) else None,
        "days_daily": len(d),
        "days_hourly": days,
        "points_5m": len(m5),
        "max_intraday_move_pct": float(dd.max() * 100) if days else float("nan"),
        "max_intraday_move_last_month_pct": float(de.max() * 100) if len(de) else float("nan"),
        "days_over_25pct": int((dd >= 0.25).sum()),
        "days_over_50pct": big,
        "one_print_spikes": len(spikes),
        "spike_examples": sorted(spikes, key=lambda x: -abs(x[1]))[:3],
        "over_50pct_per_month": per_month,
        "biggest_5m_step_pct": biggest5 * 100,
        "p99_5m_step_pct": float(r5.quantile(0.99) * 100) if len(r5) else float("nan"),
        "worst_catch_up_s": catch_up_seconds(biggest5),
        "top_5m_steps": [(str(r.t), float(r.step) * 100) for r in top_steps.itertuples()],
        "gaps_over_15m": data_gaps(m5)[0],
        "longest_gap_min": data_gaps(m5)[1],
        "flat_stretches": len(flats),
        "longest_flat_h": max((n for *_, n in flats), default=0) * 5 / 60,
        "ann_vol_pct": float(rh.std() * math.sqrt(8760) * 100) if len(rh) > 10 else float("nan"),
        "q999_24h_move_pct": q999_24h * 100,
        "suggested_max_leverage": lev,
        "liq_rate_7d": {L: liquidation_rate(h, L) for L in (2, 3, 5, 10, 20)},
        "source_venues": list(index.source_venues),
        "source_volume_usd": index.source_volume_usd,
        "thin_source": thin,
        "suggested_margin_mode": "normal (cross allowed)" if cross_ok else "noCross (isolated only)",
        "cross_margin_reason": (
            f"{per_month:.1f} days a month with 50%+ moves over {days} days, and source markets above $100K lifetime volume"
            if cross_ok
            else "; ".join(
                r
                for r in (
                    f"only {days} days of history" if days < 30 else "",
                    f"{per_month:.1f} days a month with 50%+ moves" if days and per_month > 1 else "",
                    f"source markets total ${index.source_volume_usd:,.0f} lifetime volume" if thin else "",
                )
                if r
            )
        ),
        "oracle_eligible": index.oracle_eligible,
        "freshness_state": index.freshness_state,
    }
