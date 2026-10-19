"""What exchanges see today: oracle vs mark on HIP-3 perps already listed on Hyperliquid mainnet."""

from __future__ import annotations

import json
import urllib.request

INFO = "https://api.hyperliquid.xyz/info"


def _post(body: dict) -> object:
    req = urllib.request.Request(INFO, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def dexes() -> list[dict]:
    return [d for d in _post({"type": "perpDexs"}) if d]  # type: ignore[union-attr]


def markets(dex: str) -> list[dict]:
    meta, ctxs = _post({"type": "metaAndAssetCtxs", "dex": dex})  # type: ignore[misc]
    rows = []
    for u, c in zip(meta["universe"], ctxs):
        o, m = float(c.get("oraclePx") or 0), float(c.get("markPx") or 0)
        rows.append(
            {
                "coin": u["name"],
                "oracle": o,
                "mark": m,
                "gap_pct": (m / o - 1) * 100 if o else float("nan"),
                "open_interest": float(c.get("openInterest") or 0),
                "day_volume_usd": float(c.get("dayNtlVlm") or 0),
                "funding_1h": float(c.get("funding") or 0),
                "max_leverage": u.get("maxLeverage"),
                "delisted": bool(u.get("isDelisted")),
            }
        )
    return rows
