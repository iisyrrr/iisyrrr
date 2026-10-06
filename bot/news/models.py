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
    group: str = ""  # propriétaire de la source : Reuters sur Bluesky et Reuters en direct = même groupe
    # remplis par le filtre
    assets: set[str] = field(default_factory=set)
    corroborations: int = 1  # nombre de sources différentes qui rapportent la même info
    corroborated_by_tier: int = 3  # meilleur tier parmi les sources qui la rapportent
    weight: float = 0.0  # fiabilité combinée des groupes indépendants qui la rapportent (0…1)
    groups: dict = field(default_factory=dict)  # groupe propriétaire -> poids, pour la combinaison
    member_ids: set = field(default_factory=set)  # ids de toutes les copies de la même info
    category: str = "news"  # news | analysis (analyse/prévision) | recap (récapitulatif)
    score: float = 0.0
    flags: list[str] = field(default_factory=list)
    # remplis par l'IA (optionnelle)
    analysis: dict | None = None

    @property
    def id(self) -> str:
        key = self.url or f"{self.source}|{self.title}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    @property
    def owner(self) -> str:
        return self.group or self.source

    @property
    def social_only(self) -> bool:
        """Rapportée uniquement par les réseaux sociaux : jamais d'alerte ni d'effet sur le trading,
        même si l'IA la juge crédible (l'IA peut déclasser, jamais promouvoir)."""
        return self.kind == SOCIAL and self.corroborated_by_tier > 2

    @property
    def is_rumor(self) -> bool:
        if self.social_only:
            return True
        if self.analysis is not None:
            return bool(self.analysis.get("is_rumor"))
        return False


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
    calendar_ok: bool = True  # False = calendrier absent ou périmé : la protection ne peut pas être garantie
    recognized: bool = True  # False = symbole non reconnu : aucune devise associée
