"""Correspondance entre les infos et les actifs tradés.

Chaque actif (devise, or, pétrole, indices) a une liste de mots-clés. Un mot
écrit entièrement en MAJUSCULES est recherché en respectant la casse (« US »
ne doit pas matcher « us »), les autres sans tenir compte de la casse.
"""
from __future__ import annotations

import re
from functools import lru_cache

ASSET_KEYWORDS: dict[str, list[str]] = {
    "USD": ["Fed", "FOMC", "Federal Reserve", "Powell", "Jefferson", "Waller", "Williams", "Bowman",
            "Goolsbee", "Logan", "Kashkari", "nonfarm", "non-farm", "NFP", "payrolls", "jobless claims",
            "US CPI", "PCE", "ISM", "US GDP", "US economy", "U.S. economy", "Treasury", "Treasuries",
            "US yields", "dollar", "greenback", "DXY", "US", "U.S.", "United States", "White House",
            "Bessent", "Trump", "tariff", "tariffs", "Wall Street", "BLS", "BEA"],
    "EUR": ["ECB", "European Central Bank", "Lagarde", "Schnabel", "Lane", "Nagel", "Villeroy", "eurozone",
            "euro zone", "euro area", "euro", "EUR", "Eurostat", "Bund", "Bunds", "Germany", "German",
            "France", "French", "Italy", "Italian", "Ifo", "ZEW", "HCOB", "EU"],
    "GBP": ["BoE", "BOE", "Bank of England", "Bailey", "MPC", "sterling", "pound", "GBP", "UK", "U.K.",
            "Britain", "British", "gilt", "gilts", "ONS", "Reeves"],
    "JPY": ["BoJ", "BOJ", "Bank of Japan", "Ueda", "yen", "JPY", "Japan", "Japanese", "JGB", "JGBs",
            "Ministry of Finance", "intervention", "Tokyo CPI"],
    "CHF": ["SNB", "Swiss National Bank", "Schlegel", "Swiss franc", "franc", "CHF", "Switzerland", "Swiss"],
    "CAD": ["BoC", "BOC", "Bank of Canada", "Macklem", "loonie", "CAD", "Canada", "Canadian"],
    "AUD": ["RBA", "Reserve Bank of Australia", "Bullock", "Aussie", "AUD", "Australia", "Australian",
            "iron ore"],
    "NZD": ["RBNZ", "Reserve Bank of New Zealand", "kiwi", "NZD", "New Zealand"],
    "CNY": ["PBOC", "PBoC", "People's Bank of China", "yuan", "renminbi", "CNY", "CNH", "China", "Chinese"],
    "XAU": ["gold", "bullion", "XAU", "XAUUSD", "precious metals", "safe haven", "safe-haven"],
    "XAG": ["silver", "XAG", "XAGUSD"],
    "OIL": ["oil", "crude", "Brent", "WTI", "OPEC", "OPEC+", "EIA", "barrel", "barrels"],
    "US_INDICES": ["S&P 500", "S&P", "SPX", "Nasdaq", "Dow", "Dow Jones", "Wall Street", "US stocks",
                   "equities", "stocks", "Nvidia", "Apple", "Microsoft", "earnings", "VIX"],
    "EU_INDICES": ["DAX", "Euro Stoxx", "STOXX", "CAC 40", "CAC", "European stocks", "European shares"],
    "UK_INDICES": ["FTSE"],
    "JP_INDICES": ["Nikkei", "Topix", "TOPIX"],
    "CRYPTO": ["bitcoin", "Bitcoin", "BTC", "ethereum", "ETH", "crypto"],
}

# Préfixes de noms de symboles courants chez les brokers MT5 (insensible à la casse)
INDEX_ALIASES: dict[str, list[str]] = {
    "US_INDICES": ["US30", "US500", "US100", "NAS100", "USTEC", "SPX500", "SP500", "DJ30", "DOW", "NDX",
                   "USA30", "USA500", "USTECH", "WS30"],
    "EU_INDICES": ["GER40", "GER30", "DE40", "DE30", "DAX", "EU50", "EUSTX50", "FRA40", "FR40", "CAC40"],
    "UK_INDICES": ["UK100", "FTSE100"],
    "JP_INDICES": ["JP225", "JPN225", "NIKKEI"],
    "OIL": ["USOIL", "UKOIL", "WTI", "BRENT", "XTIUSD", "XBRUSD", "CL"],
    "CRYPTO": ["BTCUSD", "ETHUSD"],
}
INDEX_CURRENCY = {"US_INDICES": "USD", "EU_INDICES": "EUR", "UK_INDICES": "GBP", "JP_INDICES": "JPY",
                  "OIL": "USD", "CRYPTO": "USD"}


def symbol_assets(symbol: str, overrides: dict | None = None) -> set[str]:
    """Actifs concernés par un symbole MT5 : EURUSD -> {EUR, USD}, XAUUSD -> {XAU, USD},
    GER40 -> {EU_INDICES, EUR}. Les suffixes de broker (EURUSD.r, EURUSDm) sont ignorés."""
    if overrides and symbol in overrides:
        return set(overrides[symbol])
    clean = re.sub(r"[^A-Z0-9]", "", symbol.upper())
    for asset, aliases in INDEX_ALIASES.items():
        if any(clean.startswith(a) for a in aliases):
            return {asset, INDEX_CURRENCY[asset]}
    base, quote = clean[:3], clean[3:6]
    out = {c for c in (base, quote) if c in ASSET_KEYWORDS}
    return out


def symbol_direction_sign(symbol: str, asset: str, overrides: dict | None = None) -> int:
    """+1 si une hausse de `asset` fait monter le symbole, -1 si elle le fait baisser.
    EURUSD : EUR fort -> +1, USD fort -> -1."""
    assets = symbol_assets(symbol, overrides)
    if asset not in assets:
        return 0
    clean = re.sub(r"[^A-Z0-9]", "", symbol.upper())
    if len(assets) == 2 and clean[3:6] == asset and clean[:3] in assets:
        return -1  # devise de cotation
    if asset in INDEX_CURRENCY.values() and any(a in assets for a in INDEX_CURRENCY):
        return 0  # devise d'un indice : effet ambigu
    return 1


@lru_cache(maxsize=None)
def _patterns(asset: str) -> tuple[re.Pattern, ...]:
    pats = []
    for kw in ASSET_KEYWORDS[asset]:
        flags = 0 if (kw.isupper() and len(kw) <= 6) else re.IGNORECASE
        pats.append(re.compile(r"(?<![\w])" + re.escape(kw) + r"(?![\w])", flags))
    return tuple(pats)


def detect_assets(text: str) -> set[str]:
    return {asset for asset in ASSET_KEYWORDS if any(p.search(text) for p in _patterns(asset))}
