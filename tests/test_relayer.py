from datetime import datetime, timedelta, timezone

import pytest
from eth_account import Account

from mecone_hip3 import hip3
from mecone_hip3.relayer import Config, Relayer, Sample, replay

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
LISTINGS = {
    "MOAI": hip3.Listing("MOAI", "OPENAI", price_scale=1e9, sz_decimals=2),
    "MCPI": hip3.Listing("MCPI", "USCPI", sz_decimals=1),
}


def relayer(**cfg):
    return Relayer("mcn", dict(LISTINGS), Config(**cfg))


def oracle_of(action, coin):
    return dict(map(tuple, action["setOracle"]["oraclePxs"]))[coin]


def test_price_rounding_matches_sdk_rule():
    assert hip3.round_px(1089.810603746, 2) == 1089.8  # 5 significant figures
    assert hip3.round_px(3.70305116, 1) == 3.7031
    assert hip3.round_px(0.000123456, 0) == 0.000123
    assert hip3.px_str(1089.8, 2) == "1089.8"


def test_set_oracle_action_shape_and_sorting():
    a = hip3.set_oracle_action("mcn", LISTINGS, {"MOAI": 1089.8, "MCPI": 3.703}, {"MOAI": 1089.8, "MCPI": 3.703}, {"MOAI": 1089.8, "MCPI": 3.703})
    so = a["setOracle"]
    assert so["dex"] == "mcn"
    assert so["oraclePxs"] == [["mcn:OPENAI", "1089.8"], ["mcn:USCPI", "3.703"]]
    assert len(so["markPxs"]) == 1 and so["markPxs"][0] == so["oraclePxs"]
    with pytest.raises(ValueError):
        hip3.set_oracle_action("mcn", LISTINGS, {"MOAI": 1.0}, {"MOAI": 1.0}, {})


def test_first_tick_publishes_scaled_price():
    r = relayer()
    actions, _ = r.tick(T0, {"MOAI": Sample(1_089_810_603_746.98, T0), "MCPI": Sample(3.703, T0)})
    assert len(actions) == 1
    assert oracle_of(actions[0], "mcn:OPENAI") == "1089.8"


def test_min_interval_is_enforced():
    r = relayer()
    s = {"MCPI": Sample(3.7, T0)}
    assert r.tick(T0, s)[0]
    assert r.tick(T0 + timedelta(seconds=2), s)[0] == []
    assert r.tick(T0 + timedelta(seconds=3), s)[0]


def test_mark_steps_at_most_one_percent_per_update():
    r = relayer()
    r.tick(T0, {"MCPI": Sample(3.7, T0)})
    t = T0
    marks = []
    for i in range(1, 8):
        t += timedelta(seconds=3)
        r.tick(t, {"MCPI": Sample(3.9, t)})  # +5.4% jump in the index
        marks.append(r.state.mark["MCPI"])
    assert r.oracle["MCPI"] == 3.9
    prev = 3.7
    for m in marks:
        assert abs(m / prev - 1) <= 0.01 + 1e-12
        prev = m
    assert marks[-1] == 3.9  # caught up within 7 updates (~21 s)


def test_external_is_ema_of_mark():
    r = relayer(external_half_life_s=3.0)
    r.tick(T0, {"MCPI": Sample(3.7, T0)})
    r.tick(T0 + timedelta(seconds=3), {"MCPI": Sample(3.7, T0)})
    assert r.state.external["MCPI"] == pytest.approx(3.7)


def test_stale_mecone_value_halts_then_resumes():
    r = relayer(max_staleness_s=600)
    r.tick(T0, {"MCPI": Sample(3.7, T0)})
    later = T0 + timedelta(minutes=11)
    actions, events = r.tick(later, {"MCPI": Sample(3.7, T0)})
    assert {"type": "perpDeploy", "haltTrading": {"coin": "mcn:USCPI", "isHalted": True}} in actions
    assert events[0].kind == "halt" and "11 min" in events[0].detail
    # The halted asset's last good price is still carried, as setOracle must include every asset.
    assert any("setOracle" in a and oracle_of(a, "mcn:USCPI") == "3.7" for a in actions)
    t2 = later + timedelta(seconds=3)
    actions, events = r.tick(t2, {"MCPI": Sample(3.71, t2)})
    assert {"type": "perpDeploy", "haltTrading": {"coin": "mcn:USCPI", "isHalted": False}} in actions
    assert events[0].kind == "resume"


@pytest.mark.parametrize(
    "sample,why",
    [
        (Sample(3.7, T0, oracle_eligible=False), "not oracle-eligible"),
        (Sample(3.7, T0, publication="paused"), "publication is paused"),
        (Sample(3.7, T0, freshness_state="expired"), "freshness guard is expired"),
    ],
)
def test_withholds_when_mecone_says_so(sample, why):
    r = relayer()
    r.tick(T0, {"MCPI": Sample(3.7, T0)})
    _, events = r.tick(T0 + timedelta(seconds=3), {"MCPI": sample})
    assert events and events[0].kind == "halt" and why in events[0].detail


def test_circuit_breaker_before_protocol_review():
    r = relayer(circuit_breaker=0.40)
    r.tick(T0, {"MCPI": Sample(4.0, T0)})
    t = T0 + timedelta(seconds=3)
    _, events = r.tick(t, {"MCPI": Sample(6.0, t)})  # +50% from start of day
    assert events[0].kind == "halt" and "50%" in events[0].detail


def test_signing_is_local_and_complete():
    r = relayer()
    actions, _ = r.tick(T0, {"MCPI": Sample(3.7, T0)})
    rec = hip3.sign(actions[0], Account.create().key.hex(), False, 1_700_000_000_000)
    assert set(rec["signature"]) == {"r", "s", "v"} and rec["action"] is actions[0]


def test_replay_reports_catch_up_after_jump():
    pts = [(T0 + timedelta(minutes=5 * i), 3.7 if i < 3 else 3.9) for i in range(6)]
    stats = replay(relayer(), {"MCPI": pts})["MCPI"]
    assert stats["halts"] == 0
    assert 15 <= stats["max_catch_up_s"] <= 24
    # Measured after the first update has already stepped the mark 1% toward the new value.
    assert stats["max_gap_pct"] == pytest.approx((1 - 3.7 * 1.01 / 3.9) * 100, rel=0.01)


def test_median_of_three_filters_a_one_print_spike():
    r = relayer(confirm_prints=3)
    vals = [47.6, 47.7, 63.7, 47.2, 47.9]  # the MMXIV spike shape, 2026-10-19 00:50 UTC
    lst = {"MMXIV": hip3.Listing("MMXIV", "MXVIX", sz_decimals=1)}
    r.listings = lst
    seen = []
    for k, v in enumerate(vals):
        t = T0 + timedelta(minutes=5 * k)
        r.tick(t, {"MMXIV": Sample(v, t)})
        seen.append(r.oracle["MMXIV"])
    assert max(seen) < 48.0  # the 63.7 print never reaches the oracle


def test_spike_detector_and_daily_bar_extremes():
    import pandas as pd

    from mecone_hip3 import readiness

    t = pd.date_range("2026-10-19", periods=5, freq="5min", tz="UTC")
    m5 = pd.DataFrame({"t": t, "open": 0.0, "high": 0.0, "low": 0.0, "close": [47.6, 47.7, 63.7, 47.2, 47.9]})
    spikes = readiness.one_print_spikes(m5)
    assert len(spikes) == 1 and round(spikes[0][1]) == 34
    d = pd.DataFrame({"t": t[:1], "open": [59.08], "high": [64.18], "low": [13.33], "close": [21.44]})
    assert readiness.daily_bar_extremes(d).iloc[0] == pytest.approx(1 - 13.33 / 59.08)
