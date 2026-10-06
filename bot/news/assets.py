"""Correspondance entre les infos et les actifs tradés.

Chaque actif (devise, or, pétrole, indices) a une liste de mots-clés. Un mot
écrit entièrement en MAJUSCULES est recherché en respectant la casse (« US »
ne doit pas matcher « us »), les autres sans tenir compte de la casse.
"""
from __future__ import annotations

import re
from functools import lru_cache

ASSET_KEYWORDS: dict[str, list[str]] = {
    "USD": ["Fed", "FOMC", "Federal Reserve", "Warsh", "Powell", "Jefferson", "Miran", "Waller", "Williams", "Bowman",
            "Goolsbee", "Logan", "Kashkari", "nonfarm", "non-farm", "NFP", "payrolls", "jobless claims",
            "US CPI", "PCE", "ISM", "US GDP", "US economy", "U.S. economy", "Treasury", "Treasuries",
            "US yields", "dollar", "greenback", "DXY", "US", "U.S.", "United States", "White House",
            "Bessent", "Trump", "tariff", "tariffs", "Wall Street", "BLS", "BEA"],
    "EUR": ["ECB", "European Central Bank", "Lagarde", "Schnabel", "Lane", "Nagel", "Villeroy", "eurozone",
            "euro zone", "euro area", "euro", "EUR", "Eurostat", "Bund", "Bunds", "Germany", "German",
            "France", "French", "Italy", "Italian", "Ifo", "ZEW", "HCOB", "EU", "Destatis",
            "Inflationsrate", "Verbraucherpreise", "Auftragseingang", "Bruttoinlandsprodukt", "Arbeitsmarkt",
            "Exporte", "Produktion im Produzierenden Gewerbe"],
    "GBP": ["BoE", "BOE", "Bank of England", "Bailey", "MPC", "sterling", "pound", "GBP", "UK", "U.K.",
            "Britain", "British", "gilt", "gilts", "ONS", "Reeves"],
    "JPY": ["BoJ", "BOJ", "Bank of Japan", "Ueda", "yen", "JPY", "Japan", "Japanese", "JGB", "JGBs",
            "Ministry of Finance", "MoF", "Katayama", "rate check", "intervention", "Tokyo CPI"],
    "CHF": ["SNB", "Swiss National Bank", "Schlegel", "Swiss franc", "franc", "CHF", "Switzerland", "Swiss"],
    "CAD": ["BoC", "BOC", "Bank of Canada", "Macklem", "loonie", "CAD", "Canada", "Canadian"],
    "AUD": ["RBA", "Reserve Bank of Australia", "Bullock", "Aussie", "AUD", "Australia", "Australian",
            "iron ore"],
    "NZD": ["RBNZ", "Reserve Bank of New Zealand", "kiwi", "NZD", "New Zealand"],
    "CNY": ["PBOC", "PBoC", "People's Bank of China", "yuan", "renminbi", "CNY", "CNH", "China", "Chinese"],
    "XAU": ["gold", "bullion", "XAU", "XAUUSD", "precious metals", "safe haven", "safe-haven", "SPDR",
            "central bank buying", "World Gold Council"],
    "XAG": ["silver", "XAG", "XAGUSD"],
    "OIL": ["oil", "crude", "Brent", "WTI", "OPEC", "OPEC+", "EIA", "barrel", "barrels"],
    "US_INDICES": ["S&P 500", "S&P", "SPX", "Nasdaq", "Dow", "Dow Jones", "Wall Street", "US stocks",
                   "equities", "stocks", "Nvidia", "Apple", "Microsoft", "earnings", "VIX"],
    "EU_INDICES": ["DAX", "Euro Stoxx", "STOXX", "CAC 40", "CAC", "European stocks", "European shares",
                   "SAP", "Siemens"],
    "UK_INDICES": ["FTSE"],
    "JP_INDICES": ["Nikkei", "Topix", "TOPIX"],
    "CRYPTO": ["bitcoin", "Bitcoin", "BTC", "ethereum", "ETH", "crypto"],
}

# Préfixes de noms de symboles courants chez les brokers MT5 (insensible à la casse)
INDEX_ALIASES: dict[str, list[str]] = {
    "US_INDICES": ["US30", "US500", "US100", "US2000", "NAS100", "USTEC", "SPX500", "SP500", "DJ30", "DOW", "NDX",
                   "USA30", "USA500", "USTECH", "WS30", "RUSSELL"],
    "EU_INDICES": ["GER40", "GER30", "DE40", "DE30", "DAX", "EU50", "EUSTX50", "FRA40", "FR40", "CAC40"],
    "UK_INDICES": ["UK100", "FTSE100"],
    "JP_INDICES": ["JP225", "JPN225", "NIKKEI"],
    "OIL": ["USOIL", "UKOIL", "WTI", "BRENT", "XTIUSD", "XBRUSD", "SPOTCRUDE", "CRUDE", "OIL"],
    "CRYPTO": ["BTCUSD", "ETHUSD", "BITCOIN"],
}
INDEX_CURRENCY = {"US_INDICES": "USD", "EU_INDICES": "EUR", "UK_INDICES": "GBP", "JP_INDICES": "JPY",
                  "OIL": "USD", "CRYPTO": "USD"}
# Métaux nommés en toutes lettres par certains brokers : cotés en dollars
METAL_ALIASES = {"GOLD": "XAU", "SILVER": "XAG"}
# Actifs « cotés » dans une devise : la devise de cotation joue en sens inverse
PRICED_IN_CURRENCY = {"XAU", "XAG"}


def symbol_profile(symbol: str, overrides: dict | None = None) -> dict[str, int]:
    """Actifs concernés par un symbole et sens de leur effet :
    +1 = une hausse de l'actif fait monter le symbole, -1 = le fait baisser, 0 = concerné mais effet ambigu.
    EURUSD -> {EUR: +1, USD: -1} ; XAUUSD et GOLD -> {XAU: +1, USD: -1} ; GER40 -> {EU_INDICES: +1, EUR: 0}.
    Les suffixes de broker (EURUSD.r, EURUSDm) sont ignorés.

    Surcharge possible dans config.yaml (news.symbol_assets) :
      liste : {MONSYMBOLE: [XAU, USD]}       -> signes déduits (métal coté en USD : USD = -1)
      dict  : {MONSYMBOLE: {XAU: 1, USD: -1}} -> signes explicites"""
    if overrides and symbol in overrides:
        ov = overrides[symbol]
        if isinstance(ov, dict):
            return {str(k): int(v) for k, v in ov.items()}
        assets = [str(a) for a in ov]
        priced = [a for a in assets if a in PRICED_IN_CURRENCY]
        indices = [a for a in assets if a in INDEX_CURRENCY]
        profile = {}
        for a in assets:
            if len(a) == 3 and a not in PRICED_IN_CURRENCY and (priced or indices):
                profile[a] = -1 if priced else 0
            else:
                profile[a] = 1
        return profile
    clean = re.sub(r"[^A-Z0-9]", "", symbol.upper())
    for name, metal in METAL_ALIASES.items():
        if clean.startswith(name):
            return {metal: 1, "USD": -1}
    for asset, aliases in INDEX_ALIASES.items():
        if any(clean.startswith(a) for a in aliases):
            return {asset: 1, INDEX_CURRENCY[asset]: 0}
    base, quote = clean[:3], clean[3:6]
    profile = {}
    if base in ASSET_KEYWORDS:
        profile[base] = 1
    if quote in ASSET_KEYWORDS and quote != base:
        profile[quote] = -1  # devise de cotation : même si la devise de base est inconnue (ZARJPY)
    return profile


def symbol_assets(symbol: str, overrides: dict | None = None) -> set[str]:
    return set(symbol_profile(symbol, overrides))


def symbol_direction_sign(symbol: str, asset: str, overrides: dict | None = None) -> int:
    """+1 si une hausse de `asset` fait monter le symbole, -1 si elle le fait baisser, 0 sinon."""
    return symbol_profile(symbol, overrides).get(asset, 0)


@lru_cache(maxsize=None)
def _patterns(asset: str) -> tuple[re.Pattern, ...]:
    pats = []
    for kw in ASSET_KEYWORDS[asset]:
        flags = 0 if (kw.isupper() and len(kw) <= 6) else re.IGNORECASE
        pats.append(re.compile(r"(?<![\w])" + re.escape(kw) + r"(?![\w])", flags))
    return tuple(pats)


def detect_assets(text: str) -> set[str]:
    return {asset for asset in ASSET_KEYWORDS if any(p.search(text) for p in _patterns(asset))}
