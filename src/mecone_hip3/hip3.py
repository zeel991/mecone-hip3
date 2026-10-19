"""Hyperliquid HIP-3 deployer actions for an index-referenced perp, with the protocol's oracle rules.

From Hyperliquid's HIP-3 deployer docs (perpDeploy / setOracle):
  * setOracle can be called repeatedly, at least 2.5 s apart; deployers are expected to call it
    every ~3 s even when nothing changed. After 10 s without an update the mark falls back to
    the local book price.
  * All prices are clamped to 10x the start-of-day value.
  * markPx moves are clamped to 1% from the previous markPx.
  * externalPerpPxs must include every asset; they guard against sudden mark deviations and can
    fall back to an EMA of recent mark prices.
  * Validators review a deployer whenever an asset's externalPerpPx moves more than 50% from the
    start-of-day price.
Prices on the wire are strings; this module rounds them the way the official SDK rounds perp
prices (5 significant figures, at most 6 - szDecimals decimals).
"""

from __future__ import annotations

from dataclasses import dataclass, field

MIN_INTERVAL_S = 2.5
TARGET_INTERVAL_S = 3.0
MARK_STEP_LIMIT = 0.01
SOD_CLAMP_MULT = 10.0
SOD_REVIEW_MOVE = 0.50


@dataclass(frozen=True)
class Listing:
    """How one Mecone index is listed as a perp on a HIP-3 dex."""

    symbol: str  # Mecone symbol, e.g. MOAI
    coin: str  # perp name on the dex, e.g. OPENAI
    price_scale: float = 1.0  # perp price = index value / price_scale (e.g. 1e9 for "$ per $1B of valuation")
    sz_decimals: int = 2

    def perp_price(self, index_value: float) -> float:
        return index_value / self.price_scale


def round_px(px: float, sz_decimals: int) -> float:
    """Hyperliquid perp price rule: 5 significant figures, at most 6 - szDecimals decimals."""
    if px <= 0:
        raise ValueError("price must be positive")
    return round(float(f"{px:.5g}"), 6 - sz_decimals)


def px_str(px: float, sz_decimals: int) -> str:
    out = f"{round_px(px, sz_decimals):.{max(0, 6 - sz_decimals)}f}".rstrip("0").rstrip(".")
    return out or "0"


def step_toward(prev: float, target: float, limit: float = MARK_STEP_LIMIT, sz_decimals: int = 2) -> float:
    """Move at most `limit` (fractional) from prev toward target, staying inside the limit after rounding."""
    if prev <= 0:
        return round_px(target, sz_decimals)
    lo, hi = prev * (1 - limit), prev * (1 + limit)
    nxt = min(max(target, lo), hi)
    r = round_px(nxt, sz_decimals)
    # Rounding can push a full step just past the limit; pull back one tick of precision if so.
    if r > hi or r < lo:
        r = round_px(prev + (nxt - prev) * 0.98, sz_decimals)
    return r


@dataclass
class DexState:
    dex: str
    start_of_day: dict[str, float] = field(default_factory=dict)
    mark: dict[str, float] = field(default_factory=dict)
    external: dict[str, float] = field(default_factory=dict)
    halted: dict[str, bool] = field(default_factory=dict)


def set_oracle_action(dex: str, listings: dict[str, Listing], oracle: dict[str, float], mark: dict[str, float], external: dict[str, float]) -> dict:
    """The perpDeploy/setOracle action, keys sorted as the protocol requires. Keys are '<dex>:<COIN>'."""

    def wire(prices: dict[str, float]) -> list[list[str]]:
        return sorted([f"{dex}:{listings[c].coin}", px_str(p, listings[c].sz_decimals)] for c, p in prices.items())

    if set(external) != set(oracle):
        raise ValueError("externalPerpPxs must include every asset")
    return {
        "type": "perpDeploy",
        "setOracle": {"dex": dex, "oraclePxs": wire(oracle), "markPxs": [wire(mark)], "externalPerpPxs": wire(external)},
    }


def halt_action(dex: str, coin: str, halted: bool) -> dict:
    return {"type": "perpDeploy", "haltTrading": {"coin": f"{dex}:{coin}", "isHalted": halted}}


def sign(action: dict, key_hex: str, is_mainnet: bool, nonce_ms: int) -> dict:
    """Sign an L1 action locally with the official SDK; nothing is sent."""
    from eth_account import Account
    from hyperliquid.utils.signing import sign_l1_action

    wallet = Account.from_key(key_hex)
    return {"action": action, "nonce": nonce_ms, "signature": sign_l1_action(wallet, action, None, nonce_ms, None, is_mainnet), "signer": wallet.address}
