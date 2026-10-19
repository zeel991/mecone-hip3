# mecone-hip3: Mecone indices as Hyperliquid HIP-3 perps

Mecone builds continuous benchmarks from prediction markets so that exchanges can list perps on things with no live price: OpenAI's valuation, US CPI, GPU rental rates, emerging-market volatility. This kit covers the step between a Mecone index and a live perp on Hyperliquid's builder-deployed markets (HIP-3):

1. **[Listing-readiness report](reports/REPORT.md)** for all 12 Mecone indices, measured from Mecone's own history against HIP-3's oracle rules.
2. **An oracle relayer** that turns Mecone's ~5-minute values into the 3-second `setOracle` updates HIP-3 requires, within the protocol's limits. It's **dry-run only**: actions are built and signed locally, never posted.

## What the report found (data pulled 2026-10-19)

- **The four emerging-market volatility indices are not ready for leverage.**
  - Their daily bars show 50%+ intraday moves on 5 to 101 days out of about a year: Mexico 101, Argentina 97, Brazil 29, China 5.
  - HIP-3 validators review every 50% move from the start of day.
- **They also print one-print spikes,** where a 5-minute value jumps 5%+ and reverts on the next print: 32 to 89 a week per index. On 2026-10-19 the Mexico index printed 63.7 between 47.7 and 47.2. A median of the last 3 prints removes most of this (see the chart in the report).
- **Seven indices sit on thin source markets:** GPU rentals, US GDP, CPI, unemployment and Mexico volatility all rest on prediction-market contracts with under $100K of lifetime volume. That argues for small open-interest caps and isolated margin.
- **Nine of the 12 indices have 5-minute data gaps;** the three GPU indices have none. The AI-company indices each have 7 gaps of 15+ minutes in the last week. The relayer halts those assets rather than let the mark drift on an old value.
- **The AI-company, GPU and US macro indices are calm.**
  - Largest intraday moves: 1.6% to 16.2%.
  - Suggested max leverage: 2–10x.
- **The only pre-IPO perps on HIP-3 so far are delisted.** Ventuals (`vntl`) has OpenAI, Anthropic, SpaceX and sector baskets, all delisted with zero open interest. There's no live comparison yet.

All of these are early measurements: the histories are 1 week (5-minute), 1 month (hourly) and 2 to 12 months (daily), and Mecone labels them indicative.

## The relayer

Each tick (every 3 s, never less than 2.5 s apart), for each listed index:

1. **Withhold:** halts the asset (`haltTrading`) when Mecone says the index isn't oracle-eligible, is paused, has an expired anchor, or hasn't produced a new value in 15 minutes. The asset's last good prices stay in `setOracle`, because the action must carry every asset.
2. **Circuit breaker:** halts on a 40%+ move from the start of day, before the protocol's 50% review, or when the price would cross the 10x start-of-day clamp.
3. **Publish:**
   - `oraclePx` is the index divided by its price scale (valuations are quoted per $1B), optionally the median of the last 3 Mecone prints.
   - `markPx` steps toward the oracle by at most 1% per update.
   - `externalPerpPx` is an EMA of the mark.

These rules come from Hyperliquid's HIP-3 deployer docs: the `setOracle` cadence and the 10-second fallback, the 10x and 1% clamps, the 50% review, and the requirement that every asset is included.

## What has been checked

- **15 unit tests** cover:
  - Price rounding (the SDK's 5-significant-figure rule).
  - The sorted wire format.
  - The 2.5-second minimum interval and the 1% mark step.
  - The EMA.
  - Halting and resuming on stale data, and every one of Mecone's withhold signals.
  - The circuit breaker and local signing.
  - Replay on recorded data.
  - Spike detection, and the median-of-3 filter on the actual Mexico spike.
- **Replay:** the relayer was run over the last week of real 5-minute data for all 12 indices (results in the report).
- **Live dry run:** 2.5 minutes against Mecone's live API for all 12 indices produced 50 signed `setOracle` actions. The longest gap between updates was 3.01 s, after moving Mecone polling off the tick loop; it had been 8 s. Nothing was posted.

## Not verified

- **No live market.** No `setOracle` has been sent to a real HIP-3 dex. That needs a deployed dex (testnet or mainnet) whose oracle updater is the signing key.
- **Order-book depth.** Mecone's API exposes only lifetime volume for the source contracts, not order-book depth, so manipulation cost is approximated by volume.
- **The thresholds are proposals, not Hyperliquid's or Mecone's rules:** the leverage suggestion (1/(2L) must cover the 99.9th-percentile 24-hour move with a 1.5x margin), the cross-margin screen, and the per-$1B price scale.

## Run it

```bash
uv sync
uv run mecone-hip3 report --refresh            # reports/REPORT.md, results.json, spike.png
uv run mecone-hip3 relay --minutes 5           # dry run, all 12 indices; unsigned unless HIP3_ORACLE_KEY is set
HIP3_ORACLE_KEY=0x... uv run mecone-hip3 relay --symbols MOAI MCPI --minutes 10 --confirm 3
uv run pytest
```

Mecone's API is polled lightly: one snapshot per index per minute while relaying, and history is cached under `data/`.
