"""Structures de données de la veille."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

# Types de source, du plus fiable au moins fiable
OFFICIAL = "official"  # banques centrales, instituts statistiques
NEWS = "news"  # médias financiers
SOCIAL = "social"  # réseaux sociaux


@dataclass
class NewsItem:
    source: str
    kind: str  # OFFICIAL | NEWS | SOCIAL
    tier: int  # 1 = source primaire / agence de premier plan, 2 = média spécialisé, 3 = social/agrégateur
    title: str
    url: str
    published: datetime  # toujours en UTC
    summary: str = ""
    engagement: dict = field(default_factory=dict)  # likes, score, commentaires, abonnés…
    author: str = ""
    # remplis par le filtre
    assets: set[str] = field(default_factory=set)
    corroborations: int = 1  # nombre de sources différentes qui rapportent la même info
    corroborated_by_tier: int = 3  # meilleur tier parmi les sources qui la rapportent
    score: float = 0.0
    flags: list[str] = field(default_factory=list)
    # remplis par l'IA (optionnelle)
    analysis: dict | None = None

    @property
    def id(self) -> str:
        key = self.url or f"{self.source}|{self.title}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    @property
    def is_rumor(self) -> bool:
        if self.analysis is not None:
            return bool(self.analysis.get("is_rumor"))
        return self.kind == SOCIAL and self.corroborated_by_tier > 2


@dataclass
class CalendarEvent:
    title: str
    currency: str  # USD, EUR, …
    time: datetime  # UTC
    impact: str  # High | Medium | Low | Holiday
    forecast: str = ""
    previous: str = ""
    actual: str = ""

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.currency}|{self.title}|{self.time.isoformat()}".encode()).hexdigest()[:16]


@dataclass
class SymbolContext:
    """Ce que le moteur de trading sait des news pour un symbole."""

    symbol: str
    sentiment: float | None  # -1 (très baissier) … +1 (très haussier), None si inconnu
    sentiment_items: int  # nombre d'infos analysées qui fondent le sentiment
    top_items: list[NewsItem]
    next_event: CalendarEvent | None
    blackout_event: CalendarEvent | None  # événement majeur trop proche -> pas d'entrée
