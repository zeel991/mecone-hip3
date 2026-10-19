"""mecone-hip3: Mecone indices -> Hyperliquid HIP-3 perps (readiness report and dry-run oracle relayer)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mecone-hip3", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("report", help="listing-readiness report for every Mecone index")
    r.add_argument("--out", type=Path, default=Path("reports"))
    r.add_argument("--refresh", action="store_true", help="re-download Mecone history instead of using the cache")

    rl = sub.add_parser("relay", help="run the oracle relayer against live Mecone values (dry run: nothing is posted)")
    rl.add_argument("--symbols", nargs="*", help="Mecone symbols to list (default: all 12)")
    rl.add_argument("--minutes", type=float, default=5)
    rl.add_argument("--poll", type=float, default=60, help="seconds between Mecone polls")
    rl.add_argument("--confirm", type=int, default=3, help="median of the last N Mecone prints (1 = raw)")
    rl.add_argument("--log", type=Path, default=Path("logs/relay.jsonl"))
    rl.add_argument("--network", choices=["testnet", "mainnet"], default="testnet", help="signing domain for the dry-run signatures")

    a = p.parse_args(argv)
    if a.cmd == "report":
        from . import report

        print(report.build(a.out, refresh=a.refresh))
        return 0

    from . import listings
    from .relayer import Config, Relayer, run

    syms = a.symbols or list(listings.LISTINGS)
    rel = Relayer(listings.DEX, {s: listings.LISTINGS[s] for s in syms}, Config(confirm_prints=a.confirm))
    # Signs with HIP3_ORACLE_KEY when set (the dex's oracleUpdater key); otherwise logs unsigned actions.
    a.log.parent.mkdir(parents=True, exist_ok=True)
    counts = run(rel, a.minutes, a.poll, a.log, os.environ.get("HIP3_ORACLE_KEY"), a.network == "mainnet")
    print(json.dumps({**counts, "posted": 0, "log": str(a.log)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
