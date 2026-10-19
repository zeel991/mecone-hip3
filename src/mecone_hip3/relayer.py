"""Mecone index -> HIP-3 oracle relayer.

`Relayer.tick(now, samples)` is pure: given the latest Mecone value per index it returns the
actions to send now (setOracle every ~3 s, haltTrading on/off) and the events behind them.
`run()` wraps it with polling, local signing and a JSONL log. Nothing is posted: this is a
dry-run relayer by design.

Rules, in order, for each listed index:
  1. Withhold when Mecone says so: not oracle-eligible, publication not active, an anchored
     index whose freshness guard is not "fresh", or no new snapshot for `max_staleness_s`.
     A withheld asset is halted (haltTrading) and its last good prices are frozen, because
     setOracle must still carry every asset's externalPerpPx.
  2. Halt when the new value would cross the 10x start-of-day clamp, or move more than
     `circuit_breaker` from the start of day (the protocol reviews >50% moves for slashing).
  3. Otherwise: oracle = index value / price_scale (optionally the median of the last
     `confirm_prints` Mecone prints, which drops one-print spikes at the cost of lag); mark steps toward the oracle by at most
     1% per update; external = EMA of the mark.
  4. Never send setOracle less than 2.5 s after the previous one.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import hip3


@dataclass(frozen=True)
class Sample:
    value: float
    computed_at: datetime
    oracle_eligible: bool = True
    publication: str = "active"
    freshness_state: str | None = None  # Mecone's anchor guard; None when the index has none


@dataclass
class Config:
    max_staleness_s: float = 15 * 60  # three missed 5-minute rounds
    circuit_breaker: float = 0.40  # halt before the protocol's 50% start-of-day review threshold
    external_half_life_s: float = 120.0
    confirm_prints: int = 1  # >1: use the median of the last N distinct Mecone prints (filters one-print spikes)
    interval_s: float = hip3.TARGET_INTERVAL_S


@dataclass
class Event:
    t: datetime
    symbol: str
    kind: str  # "halt" | "resume" | "start_of_day" | "clamped"
    detail: str


@dataclass
class Relayer:
    dex: str
    listings: dict[str, hip3.Listing]  # by Mecone symbol
    config: Config = field(default_factory=Config)
    state: hip3.DexState = field(init=False)
    oracle: dict[str, float] = field(default_factory=dict)
    last_sent: datetime | None = None
    day: object = None
    recent: dict[str, list[tuple[datetime, float]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.state = hip3.DexState(self.dex)

    def _withhold_reason(self, now: datetime, s: Sample | None) -> str | None:
        if s is None:
            return "no Mecone value yet"
        if not s.oracle_eligible:
            return "Mecone marks the index not oracle-eligible"
        if s.publication != "active":
            return f"Mecone publication is {s.publication}"
        if s.freshness_state not in (None, "fresh"):
            return f"Mecone anchor freshness guard is {s.freshness_state}"
        age = (now - s.computed_at).total_seconds()
        if age > self.config.max_staleness_s:
            return f"no new Mecone value for {age / 60:.0f} min"
        return None

    def tick(self, now: datetime, samples: dict[str, Sample]) -> tuple[list[dict], list[Event]]:
        if self.last_sent is not None and (now - self.last_sent).total_seconds() < hip3.MIN_INTERVAL_S:
            return [], []
        actions: list[dict] = []
        events: list[Event] = []
        st = self.state

        if self.day != now.date():
            self.day = now.date()
            for sym, px in self.oracle.items():
                st.start_of_day[sym] = px
                events.append(Event(now, sym, "start_of_day", f"{px:g}"))

        dt = (now - self.last_sent).total_seconds() if self.last_sent else self.config.interval_s
        alpha = 1 - 0.5 ** (dt / self.config.external_half_life_s)

        for sym, lst in self.listings.items():
            s = samples.get(sym)
            reason = self._withhold_reason(now, s)
            target = None
            if s is not None and s.value > 0:
                prints = self.recent.setdefault(sym, [])
                if not prints or prints[-1][0] != s.computed_at:
                    prints.append((s.computed_at, s.value))
                    del prints[: -self.config.confirm_prints]
                vals = sorted(v for _, v in prints)
                target = lst.perp_price(vals[len(vals) // 2])
            sod = st.start_of_day.get(sym)
            if reason is None and target is not None and sod:
                if not sod / hip3.SOD_CLAMP_MULT <= target <= sod * hip3.SOD_CLAMP_MULT:
                    reason = f"value {target:g} is outside the protocol's 10x start-of-day clamp ({sod:g})"
                elif abs(target / sod - 1) > self.config.circuit_breaker:
                    reason = f"moved {abs(target / sod - 1):.0%} from start of day (breaker {self.config.circuit_breaker:.0%})"

            halted = st.halted.get(sym, False)
            if reason is not None:
                if not halted and sym in self.oracle:
                    st.halted[sym] = True
                    actions.append(hip3.halt_action(self.dex, lst.coin, True))
                    events.append(Event(now, sym, "halt", reason))
                continue  # freeze last good prices
            if halted:
                st.halted[sym] = False
                actions.append(hip3.halt_action(self.dex, lst.coin, False))
                events.append(Event(now, sym, "resume", "Mecone value is fresh and within limits"))

            assert target is not None
            px = hip3.round_px(target, lst.sz_decimals)
            self.oracle[sym] = px
            st.start_of_day.setdefault(sym, px)
            prev_mark = st.mark.get(sym)
            st.mark[sym] = px if prev_mark is None else hip3.step_toward(prev_mark, px, hip3.MARK_STEP_LIMIT, lst.sz_decimals)
            if prev_mark is not None and st.mark[sym] != px:
                events.append(Event(now, sym, "clamped", f"mark {st.mark[sym]:g} stepping toward {px:g}"))
            prev_ext = st.external.get(sym)
            st.external[sym] = st.mark[sym] if prev_ext is None else prev_ext + alpha * (st.mark[sym] - prev_ext)

        if self.oracle:
            listed = {s: self.listings[s] for s in self.oracle}
            actions.append(
                hip3.set_oracle_action(
                    self.dex,
                    listed,
                    dict(self.oracle),
                    {s: st.mark[s] for s in self.oracle},
                    {s: st.external[s] for s in self.oracle},
                )
            )
        self.last_sent = now
        return actions, events


def run(relayer: Relayer, minutes: float, poll_s: float = 60.0, log_path: Path | None = None, key_hex: str | None = None, is_mainnet: bool = False) -> dict:
    """Poll Mecone, tick every ~3 s, sign each action locally (dry run), append to a JSONL log."""
    from . import mecone

    catalog = {i.symbol: i for i in mecone.indices()}
    missing = [s for s in relayer.listings if s not in catalog]
    if missing:
        raise KeyError(f"not in Mecone's catalog: {missing}")
    samples: dict[str, Sample] = {}
    lock = threading.Lock()
    stop = threading.Event()
    counts = {"setOracle": 0, "haltTrading": 0, "events": 0, "polls": 0, "poll_errors": 0, "max_interval_s": 0.0}

    def poll() -> None:
        # Polling runs off the tick loop so a slow Mecone response never delays a setOracle.
        while not stop.is_set():
            for sym in relayer.listings:
                idx = catalog[sym]
                try:
                    snap = mecone.snapshot(idx.slug)
                except Exception:
                    counts["poll_errors"] += 1  # the staleness rule halts the asset if this persists
                    continue
                with lock:
                    samples[sym] = Sample(snap["value"], snap["computed_at"], snap["oracle_eligible"], idx.publication, idx.freshness_state)
            counts["polls"] += 1
            stop.wait(poll_s)

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    while not samples and poller.is_alive() and not stop.wait(0.2):
        pass  # first values
    log = open(log_path, "a") if log_path else None
    deadline = time.time() + minutes * 60
    last_tick = None
    try:
        while time.time() < deadline:
            now = datetime.now(timezone.utc)
            with lock:
                current = dict(samples)
            actions, events = relayer.tick(now, current)
            if actions and last_tick is not None:
                counts["max_interval_s"] = max(counts["max_interval_s"], (now - last_tick).total_seconds())
            if actions:
                last_tick = now
            for a in actions:
                kind = "setOracle" if "setOracle" in a else "haltTrading"
                counts[kind] += 1
                rec = hip3.sign(a, key_hex, is_mainnet, int(now.timestamp() * 1000)) if key_hex else {"action": a}
                rec.update({"t": now.isoformat(timespec="seconds"), "posted": False})
                if log:
                    log.write(json.dumps(rec) + "\n")
            for e in events:
                counts["events"] += 1
                print(json.dumps({"t": e.t.isoformat(timespec="seconds"), "symbol": e.symbol, "event": e.kind, "detail": e.detail}))
                if log:
                    log.write(json.dumps({"t": e.t.isoformat(timespec="seconds"), "symbol": e.symbol, "event": e.kind, "detail": e.detail}) + "\n")
            time.sleep(max(0.0, relayer.config.interval_s - (datetime.now(timezone.utc) - now).total_seconds()))
    finally:
        stop.set()
        if log:
            log.close()
    counts["max_interval_s"] = round(counts["max_interval_s"], 2)
    return counts


def replay(relayer: Relayer, series: dict[str, list[tuple[datetime, float]]], tick_s: float = 3.0) -> dict:
    """Run the relayer over recorded 5-minute Mecone history, ticking every `tick_s` seconds.

    Returns per-symbol stats: updates sent, time the mark spent away from the oracle, the longest
    catch-up after a jump, and halts.
    """
    times = sorted({t for pts in series.values() for t, _ in pts})
    if not times:
        return {}
    cursor = {s: 0 for s in series}
    latest: dict[str, Sample] = {}
    stats = {s: {"ticks": 0, "lagging_ticks": 0, "max_catch_up_s": 0.0, "halts": 0, "max_gap_pct": 0.0} for s in series}
    catching_since: dict[str, datetime | None] = {s: None for s in series}
    now, end = times[0], times[-1] + timedelta(minutes=5)
    while now <= end:
        for s, pts in series.items():
            while cursor[s] < len(pts) and pts[cursor[s]][0] <= now:
                t, v = pts[cursor[s]]
                latest[s] = Sample(v, t)
                cursor[s] += 1
        _, events = relayer.tick(now, latest)
        for e in events:
            if e.kind == "halt":
                stats[e.symbol]["halts"] += 1
        for s in series:
            if s not in relayer.oracle:
                continue
            o, m = relayer.oracle[s], relayer.state.mark[s]
            st = stats[s]
            st["ticks"] += 1
            gap = abs(m / o - 1)
            st["max_gap_pct"] = max(st["max_gap_pct"], gap * 100)
            if gap > 1e-9:
                st["lagging_ticks"] += 1
                catching_since[s] = catching_since[s] or now
            elif catching_since[s] is not None:
                st["max_catch_up_s"] = max(st["max_catch_up_s"], (now - catching_since[s]).total_seconds())
                catching_since[s] = None
        now += timedelta(seconds=tick_s)
    return stats
