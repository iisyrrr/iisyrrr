"""Filtre qualité : ne garder que l'information fiable, récente et utile.

Étapes :
1. fraîcheur      -> on jette ce qui est trop vieux ou daté dans le futur ;
2. anti-spam      -> on jette les posts sociaux promotionnels / sans engagement ;
3. pertinence     -> on ne garde que ce qui touche un actif tradé ;
4. dédoublonnage  -> la même info reprise par 5 sites = 1 info, corroborée 5 fois ;
5. score          -> fiabilité de la source x fraîcheur x corroboration x importance.

Une info venant UNIQUEMENT des réseaux sociaux est marquée « rumeur » tant
qu'aucun média fiable ne la confirme.

La fiabilité d'une info rapportée par plusieurs groupes INDÉPENDANTS se combine :
W = 1 - (1 - w1)(1 - w2)… Exemple : FinancialJuice seul 0.75, FinancialJuice +
FXStreet 0.94. Reuters sur Bluesky et le site de Reuters comptent pour un seul groupe.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .assets import detect_assets
from .models import OFFICIAL, SOCIAL, NewsItem

TIER_WEIGHT = {1: 0.9, 2: 0.75, 3: 0.4}
OFFICIAL_WEIGHT = 1.0
SOCIAL_WEIGHT = 0.15  # foule : utile comme signal faible, jamais comme preuve

# Mots qui signalent une info potentiellement importante pour les marchés
IMPACT_WORDS = [
    "rate decision", "rate cut", "rate hike", "cuts rates", "raises rates", "holds rates", "hike", "cut",
    "surprise", "unexpected", "unexpectedly", "emergency", "intervention", "intervene", "default",
    "downgrade", "recession", "inflation", "CPI", "payrolls", "NFP", "GDP", "unemployment",
    "tariff", "sanctions", "war", "ceasefire", "attack", "resigns", "fired", "shutdown",
    "bank failure", "collapse", "crash", "plunge", "plunges", "surge", "surges", "soars", "tumbles",
    "record high", "record low", "month high", "month low", "year high", "year low", "highest since",
    "lowest since", "breaking", "BREAKING", "flash", "rate", "rates", "yields", "jobs", "employment",
    "sell-off", "selloff", "rally", "rallies", "slump", "slumps",
]
_IMPACT_RE = re.compile(r"(?<![\w])(" + "|".join(re.escape(w) for w in IMPACT_WORDS) + r")(?![\w])",
                        re.IGNORECASE)

# Signes typiques de spam / promotion sur les réseaux sociaux
SPAM_PATTERNS = [
    r"\bfree signals?\b", r"\bsignal (group|channel)\b", r"\bjoin (my|our)\b", r"\bdm me\b", r"\bdm for\b",
    r"\bt\.me/", r"\bwhats ?app\b", r"\b100x\b", r"\bguaranteed\b", r"\bpassive income\b",
    r"\bcopy (my )?trades?\b", r"\baccount management\b", r"\bairdrop\b", r"\bgiveaway\b",
    r"\bto the moon\b", r"\bpromo code\b", r"\breferral\b", r"\blink in bio\b",
]
SPAM_PATTERNS += [r"\b(buy|sell) signal\b", r"\btp ?1\b", r"\bsl ?:", r"\bentry zone\b", r"\bvip\b",
                  r"\brcs score\b"]
_SPAM_RE = re.compile("|".join(SPAM_PATTERNS), re.IGNORECASE)
_CASHTAG_RE = re.compile(r"\$[A-Z]{2,6}(?:\.[A-Z])?\b")

# Formulations de rumeur : poids réduit tant qu'une source de premier plan ne confirme pas
_RUMOR_RE = re.compile(r"\b(sources? say|sources said|reportedly|people familiar|said to be|rumou?rs?|unconfirmed|"
                       r"market talk|chatter|desk talk|according to sources)\b", re.IGNORECASE)
# Récapitulatifs : utiles pour le briefing, jamais pour une alerte
_RECAP_RE = re.compile(r"(what are the main events|stock market news for|week ahead|weekly outlook|morning wrap|"
                       r"markets? wrap|news wrap|recap\b|live updates?|live blog|\blive:|economic calendar for|"
                       r"daily open|forex today|market news:|live levels)", re.IGNORECASE)
# Analyses / prévisions / opinions : contexte seulement
_ANALYSIS_RE = re.compile(r"(price prediction|price forecast|\bforecast:|technical analysis|chart of the day|"
                          r"elliott wave|trade idea|FJElite|\bopinion:|"
                          r"currency strength chart|implied volatility|correlation matrix|interest rate probabilities|"
                          r": market analysis|"
                          # note de banque citée en fin de titre : « Gold: vulnerable… – OCBC »
                          r"[–—-]\s*(ING|UOB|OCBC|MUFG|Danske Bank|Commerzbank|Rabobank|SocGen|Soci[ée]t[ée] G[ée]n[ée]rale|"
                          r"BBH|TDS|TD Securities|Scotiabank|Wells Fargo|Goldman Sachs|Morgan Stanley|Citi|HSBC|BofA|"
                          r"Nomura|Standard Chartered|Deutsche Bank|Barclays|ANZ|Westpac|NAB|CBA|RBC|BNY|Natixis|"
                          r"Cr[ée]dit Agricole|Swissquote|Saxo|Pepperstone|Julius Baer|UBS|JPMorgan|BNP Paribas)\s*$)",
                          re.IGNORECASE)
_NUMBER_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?%?")
# Mots qui distinguent deux publications différentes malgré des titres presque identiques
_DISTINCT = {"core", "flash", "final", "prelim", "preliminary", "mom", "yoy", "qoq", "services", "manufacturing",
             "composite", "headline", "annualized", "german", "germany", "france", "french", "eurozone", "euro",
             "uk", "us", "japan", "japanese", "china", "chinese", "italy", "italian", "spain", "spanish",
             "canada", "canadian", "australia", "australian", "swiss", "switzerland", "zealand", "ex"}
# Gabarit des titres de données (« Actual X (Forecast Y, Previous Z) ») : sans valeur pour comparer
_TEMPLATE = {"actual", "forecast", "previous", "prior", "est", "exp", "expected", "consensus", "vs", "revised"}
_STOPWORDS = set(
    "the a an of to in on for and or is are be as at by with from its it this that after over says said "
    "amid vs versus will may could new us de la le les des du et en un une au aux".split()
)


@dataclass
class FilterSettings:
    max_age_hours: float = 24.0
    half_life_hours: float = 3.0  # une info perd la moitié de son score toutes les 3 h
    dedup_similarity: float = 0.55  # similarité des titres au-delà de laquelle c'est la même info
    social_min_engagement: dict = field(default_factory=lambda: {"score": 25, "likes": 5, "followers": 500})
    # un post social sans mot de marché n'est gardé que s'il fait vraiment réagir
    social_strong_engagement: dict = field(default_factory=lambda: {"score": 300, "likes": 100, "comments": 100})
    traded_assets: set[str] = field(default_factory=set)  # vide = tout garder


def normalize_tokens(text: str) -> set[str]:
    text = re.sub(r"\b([myq])/\1?([myq])\b", lambda m: {"m/m": "mom", "y/y": "yoy", "q/q": "qoq"}.get(
        m.group(0).lower(), m.group(0)), text, flags=re.IGNORECASE)
    words = re.findall(r"[a-z0-9%.]+", text.lower())
    return {w.strip(".") for w in words
            if len(w.strip(".")) > 1 and w not in _STOPWORDS and w.strip(".") not in _TEMPLATE}


def similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_spam(item: NewsItem) -> bool:
    text = f"{item.title} {item.summary}"
    if _SPAM_RE.search(text):
        return True
    letters = [c for c in item.title if c.isalpha()]
    if len(letters) > 20 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        return True  # titre en MAJUSCULES
    if item.title.count("🚀") + item.title.count("💰") + item.title.count("🔥") >= 3:
        return True
    if len(set(_CASHTAG_RE.findall(item.title))) >= 4:
        return True  # liste de tickers = post promotionnel
    return False


def has_min_engagement(item: NewsItem, thresholds: dict) -> bool:
    """Un post social doit dépasser AU MOINS UN seuil d'engagement fourni par sa source."""
    checks = [item.engagement[k] >= v for k, v in thresholds.items() if k in item.engagement]
    return any(checks) if checks else False


def freshness(item: NewsItem, now: datetime, half_life_hours: float) -> float:
    age_h = max(0.0, (now - item.published).total_seconds() / 3600)
    return 0.5 ** (age_h / half_life_hours)


def impact_hits(item: NewsItem) -> int:
    return len(set(m.lower() for m in _IMPACT_RE.findall(f"{item.title} {item.summary}")))


def member_weight(item: NewsItem) -> float:
    if item.kind == SOCIAL:
        return SOCIAL_WEIGHT
    if item.kind == OFFICIAL and item.tier == 1:
        return OFFICIAL_WEIGHT
    return TIER_WEIGHT.get(item.tier, 0.3)


def same_story(a: tuple[set[str], list[str]], b: tuple[set[str], list[str]], threshold: float) -> bool:
    (tok_a, num_a), (tok_b, num_b) = a, b
    if num_a and num_b and num_a[0] != num_b[0]:
        return False  # premier chiffre (souvent le « réel ») différent : « -10,6 % » ≠ « -5 % »
    if (tok_a ^ tok_b) & _DISTINCT:
        return False  # CPI ≠ Core CPI, m/m ≠ a/a, Allemagne ≠ zone euro
    return similarity(tok_a, tok_b) >= threshold


def deduplicate(items: list[NewsItem], threshold: float) -> list[NewsItem]:
    """Regroupe les infos identiques. On garde la version de la source la plus
    fiable (puis la plus ancienne = la primeur). La corroboration se compte par
    groupe propriétaire indépendant, et les fiabilités se combinent (« ou » bruité)."""
    ordered = sorted(items, key=lambda i: (-member_weight(i), i.published))
    clusters: list[dict] = []
    by_url: dict[str, int] = {}
    for item in ordered:
        key = (normalize_tokens(item.title), _NUMBER_RE.findall(item.title))
        idx = by_url.get(item.url) if item.url else None
        if idx is None:
            for k, c in enumerate(clusters):
                if same_story(key, c["key"], threshold):
                    idx = k
                    break
        if idx is None:
            clusters.append({"kept": item, "key": key, "groups": {}, "owners": set(), "best_tier": item.tier})
            idx = len(clusters) - 1
            if item.url:
                by_url[item.url] = idx
        c = clusters[idx]
        c.setdefault("ids", set()).add(item.id)
        c["owners"].add(item.owner)
        # tous les posts sociaux ne comptent que pour UN groupe : 10 posts ne valent pas une agence
        group = "__social__" if item.kind == SOCIAL else item.owner
        c["groups"][group] = max(c["groups"].get(group, 0.0), member_weight(item))
        if item.kind != SOCIAL:
            c["best_tier"] = min(c["best_tier"], item.tier)
        if item is not c["kept"]:
            c["kept"].assets |= item.assets
    out = []
    for c in clusters:
        kept = c["kept"]
        kept.corroborations = len(c["owners"])
        kept.corroborated_by_tier = c["best_tier"] if any(
            w > SOCIAL_WEIGHT for w in c["groups"].values()) else 3
        kept.groups = dict(c["groups"])
        kept.weight = combined_weight(kept.groups)
        kept.member_ids = set(c["ids"])
        out.append(kept)
    return out


def combined_weight(groups: dict) -> float:
    return round(1 - math.prod(1 - w for w in groups.values()), 4)


def categorize(item: NewsItem) -> str:
    text = item.title
    if _RECAP_RE.search(text):
        return "recap"
    if _ANALYSIS_RE.search(text):
        return "analysis"
    return "news"


def score_item(item: NewsItem, now: datetime, s: FilterSettings) -> float:
    """Score de 0 à 1 : fiabilité combinée x fraîcheur x importance x nature de l'info."""
    importance = 0.7 + 0.1 * min(impact_hits(item), 3)
    nature = {"news": 1.0, "analysis": 0.35, "recap": 0.3}[item.category]
    if _RUMOR_RE.search(f"{item.title} {item.summary}") and item.corroborated_by_tier > 1:
        nature *= 0.6
    return round(min(1.0, item.weight * freshness(item, now, s.half_life_hours) * importance * nature), 4)


def run_filters(items: list[NewsItem], now: datetime, s: FilterSettings) -> list[NewsItem]:
    kept = []
    oldest = now - timedelta(hours=s.max_age_hours)
    for item in items:
        if not item.title.strip():
            continue
        if item.published < oldest or item.published > now + timedelta(minutes=10):
            continue
        if item.kind == SOCIAL:
            if is_spam(item) or not has_min_engagement(item, s.social_min_engagement):
                continue
            if impact_hits(item) == 0 and not has_min_engagement(item, s.social_strong_engagement):
                continue  # bavardage sans information de marché
        item.assets = item.assets or detect_assets(f"{item.title} {item.summary}")
        if s.traded_assets and not (item.assets & s.traded_assets):
            continue
        kept.append(item)

    unique = deduplicate(kept, s.dedup_similarity)
    for item in unique:
        item.category = categorize(item)
        item.score = score_item(item, now, s)
        item.flags = []
        if item.social_only:
            item.flags.append("rumeur non confirmée")
        if item.category != "news":
            item.flags.append({"analysis": "analyse/opinion", "recap": "récapitulatif"}[item.category])
        if item.corroborations >= 3:
            item.flags.append(f"confirmée par {item.corroborations} sources")
    return sorted(unique, key=lambda i: i.score, reverse=True)


def clamp_sentiment(x: float) -> float:
    return math.tanh(x)
