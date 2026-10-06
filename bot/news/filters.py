"""Filtre qualité : ne garder que l'information fiable, récente et utile.

Étapes :
1. fraîcheur      -> on jette ce qui est trop vieux ou daté dans le futur ;
2. anti-spam      -> on jette les posts sociaux promotionnels / sans engagement ;
3. pertinence     -> on ne garde que ce qui touche un actif tradé ;
4. dédoublonnage  -> la même info reprise par 5 sites = 1 info, corroborée 5 fois ;
5. score          -> fiabilité de la source x fraîcheur x corroboration x importance.

Une info venant UNIQUEMENT des réseaux sociaux est marquée « rumeur » tant
qu'aucun média fiable ne la confirme.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .assets import detect_assets
from .models import SOCIAL, NewsItem

TIER_WEIGHT = {1: 1.0, 2: 0.75, 3: 0.4}

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
_SPAM_RE = re.compile("|".join(SPAM_PATTERNS), re.IGNORECASE)
_STOPWORDS = set(
    "the a an of to in on for and or is are be as at by with from its it this that after over says said "
    "amid vs versus will may could new us de la le les des du et en un une au aux".split()
)


@dataclass
class FilterSettings:
    max_age_hours: float = 24.0
    half_life_hours: float = 6.0  # une info perd la moitié de son score toutes les 6 h
    dedup_similarity: float = 0.55  # similarité des titres au-delà de laquelle c'est la même info
    social_min_engagement: dict = field(default_factory=lambda: {"score": 25, "likes": 5, "followers": 500})
    # un post social sans mot de marché n'est gardé que s'il fait vraiment réagir
    social_strong_engagement: dict = field(default_factory=lambda: {"score": 300, "likes": 100, "comments": 100})
    traded_assets: set[str] = field(default_factory=set)  # vide = tout garder


def normalize_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9%.]+", text.lower())
    return {w.strip(".") for w in words if len(w.strip(".")) > 1 and w not in _STOPWORDS}


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


def deduplicate(items: list[NewsItem], threshold: float) -> list[NewsItem]:
    """Regroupe les infos identiques. On garde la version de la source la plus
    fiable (puis la plus ancienne = la primeur) et on compte les corroborations."""
    ordered = sorted(items, key=lambda i: (i.tier, i.published))
    clusters: list[tuple[NewsItem, set[str], set[str], int]] = []  # (gardée, tokens, sources, meilleur tier)
    by_url: dict[str, int] = {}
    for item in ordered:
        tokens = normalize_tokens(item.title)
        idx = by_url.get(item.url) if item.url else None
        if idx is None:
            for k, (_, toks, _, _) in enumerate(clusters):
                if similarity(tokens, toks) >= threshold:
                    idx = k
                    break
        if idx is None:
            clusters.append((item, tokens, {item.source}, item.tier))
            if item.url:
                by_url[item.url] = len(clusters) - 1
        else:
            kept, toks, sources, best = clusters[idx]
            sources.add(item.source)
            clusters[idx] = (kept, toks | tokens if len(toks) < 4 else toks, sources, min(best, item.tier))
            kept.assets |= item.assets
    out = []
    for kept, _, sources, best in clusters:
        kept.corroborations = len(sources)
        kept.corroborated_by_tier = best
        out.append(kept)
    return out


def score_item(item: NewsItem, now: datetime, s: FilterSettings) -> float:
    """Score de 0 à ~1 : fiabilité x fraîcheur x corroboration x importance."""
    base = TIER_WEIGHT.get(item.tier, 0.3)
    if item.kind == SOCIAL and item.corroborated_by_tier <= 2:
        base = max(base, TIER_WEIGHT[item.corroborated_by_tier] * 0.9)  # rumeur confirmée par un média
    corroboration = 1 + 0.15 * min(item.corroborations - 1, 4)
    importance = 1 + 0.2 * min(impact_hits(item), 3)
    return round(min(1.0, base * freshness(item, now, s.half_life_hours) * corroboration * importance / 1.6), 4)


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
        item.score = score_item(item, now, s)
        item.flags = []
        if item.is_rumor:
            item.flags.append("rumeur non confirmée")
        if item.corroborations >= 3:
            item.flags.append(f"confirmée par {item.corroborations} sources")
    return sorted(unique, key=lambda i: i.score, reverse=True)


def clamp_sentiment(x: float) -> float:
    return math.tanh(x)
