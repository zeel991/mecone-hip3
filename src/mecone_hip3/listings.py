"""Proposed HIP-3 listing settings for each Mecone index.

price_scale turns a value into a tradeable perp price: valuations are quoted per $1B (the
convention Ventuals uses for its pre-IPO perps), everything else at face value. These are
proposals for discussion with Mecone, not their choices.
"""

from .hip3 import Listing

DEX = "mcn"  # placeholder HIP-3 dex name

LISTINGS = {
    "MOAI": Listing("MOAI", "OPENAI", price_scale=1e9, sz_decimals=2),
    "MANTH": Listing("MANTH", "ANTHROPIC", price_scale=1e9, sz_decimals=2),
    "MH100": Listing("MH100", "H100", sz_decimals=1),
    "MH200": Listing("MH200", "H200", sz_decimals=1),
    "MB200": Listing("MB200", "B200", sz_decimals=1),
    "MGDP": Listing("MGDP", "USGDP", sz_decimals=1),
    "MCPI": Listing("MCPI", "USCPI", sz_decimals=1),
    "MUNEMP": Listing("MUNEMP", "USUNEMP", sz_decimals=1),
    "MBRIV": Listing("MBRIV", "BRVIX", sz_decimals=1),
    "MMXIV": Listing("MMXIV", "MXVIX", sz_decimals=1),
    "MARGIV": Listing("MARGIV", "ARVIX", sz_decimals=1),
    "MCHNIV": Listing("MCHNIV", "CNVIX", sz_decimals=1),
}
