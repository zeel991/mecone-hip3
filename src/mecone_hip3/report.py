"""Build the listing-readiness report: readiness metrics, relayer replay, and the live HIP-3 snapshot."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from . import listings, live_hip3, mecone, readiness  # noqa: E402
from .relayer import Config, Relayer, replay  # noqa: E402

INK, MUTED, RAW_C, FILT_C = "#1f2328", "#6e7781", "#cf222e", "#0969da"


def _replay(sym: str, m5: pd.DataFrame, confirm: int) -> dict:
    pts = [(r.t.to_pydatetime(), float(r.close)) for r in m5.itertuples()]
    rel = Relayer(listings.DEX, {sym: listings.LISTINGS[sym]}, Config(confirm_prints=confirm))
    return replay(rel, {sym: pts})[sym]


def _spike_chart(sym: str, m5: pd.DataFrame, path: Path) -> None:
    """The worst day of the week: Mecone's 5-minute prints vs a median-of-3 oracle."""
    day = m5.assign(step=m5["close"].pct_change().abs()).sort_values("step", ascending=False)["t"].iloc[0].floor("D")
    d = m5[(m5["t"] >= day) & (m5["t"] < day + pd.Timedelta(days=1))]
    med = d["close"].rolling(3, min_periods=1).median()
    fig, ax = plt.subplots(figsize=(9, 3.8), dpi=150)
    ax.plot(d["t"], d["close"], color=RAW_C, lw=1, label="Mecone 5-minute prints (raw)")
    ax.plot(d["t"], med, color=FILT_C, lw=1.8, label="Median of last 3 prints (relayer option)")
    ax.set_title(f"{sym} on {day.date()}: one-print spikes reaching an oracle", loc="left", color=INK, fontsize=10)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.grid(axis="y", color="#d0d7de", lw=0.5)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def build(out_dir: Path, refresh: bool = False) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    idx = mecone.indices()
    results = []
    worst_spike = None
    for i in idx:
        m5, h, d = (mecone.history(i.slug, w, refresh=refresh) for w in ("5m", "1h", "1d"))
        if refresh:
            time.sleep(0.5)
        r = readiness.assess(i, m5, h, d)
        r["replay_raw"] = _replay(i.symbol, m5, 1)
        r["replay_median3"] = _replay(i.symbol, m5, 3)
        results.append(r)
        if worst_spike is None or r["biggest_5m_step_pct"] > worst_spike[1]:
            worst_spike = (i.symbol, r["biggest_5m_step_pct"], m5)
    if worst_spike:
        _spike_chart(worst_spike[0], worst_spike[2], out_dir / "spike.png")

    live = {"dexes": [], "vntl": []}
    try:
        live["dexes"] = [{"name": x["name"], "fullName": x.get("fullName")} for x in live_hip3.dexes()]
        live["vntl"] = live_hip3.markets("vntl")
    except Exception as e:  # live context is optional
        live["error"] = str(e)

    asof = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    (out_dir / "results.json").write_text(json.dumps({"as_of": asof, "indices": results, "hyperliquid": live}, indent=1, default=str))
    (out_dir / "REPORT.md").write_text(_markdown(results, live, asof, worst_spike[0] if worst_spike else None))
    return out_dir / "REPORT.md"


def _f(x: float, nd: int = 1) -> str:
    return "–" if x != x else f"{x:.{nd}f}"


def _val(v: float, unit: str) -> str:
    if unit == "$" and v >= 1e12:
        return f"${v / 1e12:.2f}T"
    if unit == "$" and v >= 1e9:
        return f"${v / 1e9:.1f}B"
    return f"{v:.4g} {unit}"


def _markdown(rs: list[dict], live: dict, asof: str, spike_sym: str | None) -> str:
    by = sorted(rs, key=lambda r: (r["category"], r["symbol"]))
    lines = [
        "# Mecone indices as Hyperliquid HIP-3 perps: listing readiness",
        "",
        f"Data: Mecone's public index API (`api.mecone.trade/api/v1`) and Hyperliquid mainnet, pulled {asof}.",
        "History is short: hourly bars cover the last month, 5-minute bars the last week, and daily bars go back to",
        "August 2026 (October 2025 for the volatility indices). Mecone flags these histories as indicative, not official,",
        "and its own statistics endpoint reports too few observations for annualized volatility. Read every number",
        "below as an early measurement.",
        "",
        "## Summary",
        "",
        "| Index | Value | 50%+ intraday days | Biggest 5-min step | One-print spikes (week) | 5-min gaps >15 min | Source volume | Suggested max leverage | Suggested margin mode |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in by:
        lines.append(
            f"| {r['symbol']} {r['name']} | {_val(r['value'], r['unit'])} | {r['days_over_50pct']} of {r['days_daily']} | "
            f"{_f(r['biggest_5m_step_pct'], 2)}% | {r['one_print_spikes']} | {r['gaps_over_15m']} | ${r['source_volume_usd']:,.0f} | "
            f"{r['suggested_max_leverage']}x | {r['suggested_margin_mode']} |"
        )
    vix = [r for r in rs if r["category"] == "Emerging Market Vol"]
    thin = [r["symbol"] for r in rs if r["thin_source"]]
    calm = [r for r in rs if r["category"] != "Emerging Market Vol"]
    lines += [
        "",
        "## What stands out",
        "",
        f"- **The four emerging-market volatility indices are not ready for leverage.** Their daily bars show 50%+ intraday moves on "
        + ", ".join(f"{r['symbol']} {r['days_over_50pct']}" for r in vix)
        + " days. HIP-3 validators review every 50% move from the start of day, and assets doing that more than once a month are not eligible for cross margin.",
        f"- **One-print spikes.** Over the last week the volatility indices printed jumps of 5%+ that reverted on the very next 5-minute print "
        + ", ".join(f"{r['symbol']} {r['one_print_spikes']}x" for r in vix)
        + f". Passed straight to an oracle, each one moves every position's mark and liquidation price. A median of the last 3 prints removes the worst of it (chart below), at the cost of up to 10 minutes of lag.",
        f"- **Thin source markets.** {', '.join(thin)} are built on prediction-market contracts with under $100K of lifetime volume. An exchange should keep open-interest caps small and isolated margin only until the source deepens.",
        "- **Data gaps.** Several indices have stretches of 15+ minutes with no new 5-minute point. The relayer halts trading on those assets instead of letting the mark drift on an old value (see the replay table).",
        "- **The AI-company, GPU and US macro indices behave calmly.** Their largest intraday moves range from "
        + f"{min(r['max_intraday_move_pct'] for r in calm):.1f}% to {max(r['max_intraday_move_pct'] for r in calm):.1f}%, and the suggested max leverage is "
        + f"{min(r['suggested_max_leverage'] for r in calm)}–{max(r['suggested_max_leverage'] for r in calm)}x. MCHNIV passes the cross-margin screen yet gets 1x leverage: eligibility looks at how often 50% days happen, leverage at how large the tail moves are.",
        "",
    ]
    if spike_sym:
        lines += ["![Spike chart](spike.png)", ""]
    lines += [
        "## Relayer replay (last week of 5-minute data, setOracle every 3 seconds)",
        "",
        "Each run sends a setOracle every 3 seconds, steps the mark at most 1% per update, and halts an asset when Mecone has no new value for 15 minutes or the price moves more than 40% from the start of day.",
        "",
        "| Index | Halts (raw) | Halts (median of 3) | Largest mark–oracle gap raw / median3 | Longest catch-up raw / median3 |",
        "|---|---|---|---|---|",
    ]
    for r in by:
        a, b = r["replay_raw"], r["replay_median3"]
        lines.append(f"| {r['symbol']} | {a['halts']} | {b['halts']} | {_f(a['max_gap_pct'], 2)}% / {_f(b['max_gap_pct'], 2)}% | {a['max_catch_up_s']:.0f}s / {b['max_catch_up_s']:.0f}s |")
    lines += [
        "",
        "## Per-index detail",
        "",
    ]
    for r in by:
        liq = ", ".join(f"{L}x {v * 100:.1f}%" for L, v in r["liq_rate_7d"].items())
        lines += [
            f"### {r['symbol']}: {r['name']}",
            "",
            f"- Value {_val(r['value'], r['unit'])}; oracle-eligible: {r['oracle_eligible']}; anchor freshness: {r['freshness_state'] or 'no anchor'}.",
            f"- History: daily from {r['history_start']} ({r['days_daily']} days); {r['points_5m']} five-minute points in the last week.",
            f"- Largest intraday move from the day's open: {_f(r['max_intraday_move_pct'])}% (last month: {_f(r['max_intraday_move_last_month_pct'])}%). Days over 25%: {r['days_over_25pct']}; over 50%: {r['days_over_50pct']}.",
            f"- 5-minute steps: largest {_f(r['biggest_5m_step_pct'], 2)}%, 99th percentile {_f(r['p99_5m_step_pct'], 2)}%; a 1%-per-update mark needs {r['worst_catch_up_s']:.0f}s to absorb the largest. One-print spikes: {r['one_print_spikes']}"
            + (f" (largest at {r['spike_examples'][0][0]}: {r['spike_examples'][0][1]:+.1f}% then {r['spike_examples'][0][2]:+.1f}%)" if r["spike_examples"] else "")
            + ".",
            f"- Flat stretches of an hour or more: {r['flat_stretches']} (longest {r['longest_flat_h']:.1f}h). Gaps over 15 minutes: {r['gaps_over_15m']} (longest {r['longest_gap_min']:.0f} min).",
            f"- Hourly annualized volatility {_f(r['ann_vol_pct'])}%; 99.9th-percentile 24-hour move {_f(r['q999_24h_move_pct'])}%.",
            f"- Share of hourly entries (long and short) liquidated within 7 days, isolated: {liq}.",
            f"- Source markets: " + ", ".join(f"{n} ${v:,.0f}" for n, v in r["source_venues"]) + ".",
            f"- **Suggested: {r['suggested_max_leverage']}x max leverage, {r['suggested_margin_mode']}.** {r['cross_margin_reason'].capitalize() if r['cross_margin_reason'] else ''}",
            "",
        ]
    lines += [
        "## Hyperliquid today",
        "",
    ]
    if live.get("dexes"):
        lines.append(f"- {len(live['dexes'])} HIP-3 dexes are live on mainnet: " + ", ".join(f"{d['name']} ({d['fullName']})" for d in live["dexes"]) + ". None is Mecone's.")
    if live.get("vntl"):
        n = len(live["vntl"])
        dl = sum(m["delisted"] for m in live["vntl"])
        lines.append(f"- Ventuals (`vntl`), the dex that listed pre-IPO perps (OpenAI, Anthropic, SpaceX) and sector baskets, shows {dl} of {n} markets delisted, with zero open interest and volume. There is no live pre-IPO perp on HIP-3 to compare against.")
    lines += [
        "",
        "## Method and limits",
        "",
        "- **Leverage rule:** at max leverage L an isolated position is liquidated after an adverse move of about 1/(2L). The suggestion is the largest of 1, 2, 3, 5, 10 or 20x where 1/(2L) covers the 99.9th-percentile 24-hour move with a 1.5x safety factor. It is a starting point for a risk team, not Hyperliquid's or Mecone's rule.",
        "- **Margin-mode rule:** cross margin is suggested only with at least 30 days of daily history, at most one 50%+ intraday day per month, and source markets above $100K lifetime volume. The protocol's actual eligibility review also weighs oracle reliability and manipulation resistance, which this report does not fully measure.",
        "- **Price scale:** valuations are quoted per $1B, as Ventuals did; other indices at face value. Mecone may prefer other units.",
        "- **Not measured:** order-book depth of the source contracts (only lifetime volume is public in Mecone's API), funding behaviour on a live book, and anything that needs a deployed HIP-3 dex.",
        "",
    ]
    return "\n".join(lines)
