"""Calendrier économique : fenêtres d'interdiction autour des annonces majeures."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .assets import FIAT
from .models import CalendarEvent

IMPACT_RANK = {"Holiday": 0, "Low": 1, "Medium": 2, "High": 3}

# Annonces qui méritent une fenêtre plus large (forte volatilité, spreads qui explosent)
MAJOR_KEYWORDS = ("Non-Farm", "Nonfarm", "NFP", "CPI", "FOMC", "Federal Funds Rate", "Fed Chair",
                  "Rate Decision", "Rate Statement", "Monetary Policy", "Cash Rate", "Official Bank Rate",
                  "Main Refinancing Rate", "Press Conference", "GDP")


@dataclass
class BlackoutSettings:
    enabled: bool = True
    impacts: tuple[str, ...] = ("High",)
    minutes_before: int = 30
    minutes_after: int = 30
    major_minutes_before: int = 45  # NFP, CPI, banques centrales…
    major_minutes_after: int = 60
    extra_currencies: dict = field(default_factory=dict)  # ex : {"XAU": ["USD"]}


def is_major(event: CalendarEvent) -> bool:
    return any(k.lower() in event.title.lower() for k in MAJOR_KEYWORDS)


def window(event: CalendarEvent, s: BlackoutSettings) -> tuple[datetime, datetime]:
    before, after = ((s.major_minutes_before, s.major_minutes_after) if is_major(event)
                     else (s.minutes_before, s.minutes_after))
    if "press conference" in event.title.lower():
        after = max(after, 90)  # une conférence dure ~1 h : on couvre jusqu'à 30 min après la fin
    return event.time - timedelta(minutes=before), event.time + timedelta(minutes=after)


def covers_now(events: list[CalendarEvent], now: datetime) -> bool:
    """Le calendrier couvre-t-il la semaine en cours ? Le fichier hebdomadaire commence au plus
    tard 7 jours avant « maintenant » ; un fichier plus ancien est celui d'une semaine passée."""
    return bool(events) and min(e.time for e in events) >= now - timedelta(days=7, hours=12)


def relevant_currencies(assets: set[str], s: BlackoutSettings) -> set[str]:
    out = {a for a in assets if a in FIAT}
    for a in assets:
        out |= set(s.extra_currencies.get(a, []))
    return out


def blackout_event(events: list[CalendarEvent], assets: set[str], now: datetime,
                   s: BlackoutSettings) -> CalendarEvent | None:
    """Événement à fort impact dont la fenêtre englobe `now` pour une des devises du symbole."""
    if not s.enabled:
        return None
    currencies = relevant_currencies(assets, s)
    for ev in sorted(events, key=lambda e: e.time):
        if ev.currency not in currencies or ev.impact not in s.impacts:
            continue
        start, end = window(ev, s)
        if start <= now <= end:
            return ev
    return None


def upcoming(events: list[CalendarEvent], currencies: set[str], now: datetime, hours: float = 24,
             min_impact: str = "Medium") -> list[CalendarEvent]:
    limit = now + timedelta(hours=hours)
    rank = IMPACT_RANK.get(min_impact, 2)
    return sorted(
        (e for e in events if e.currency in currencies and now <= e.time <= limit
         and IMPACT_RANK.get(e.impact, 0) >= rank),
        key=lambda e: e.time,
    )


def format_event(ev: CalendarEvent, tz) -> str:
    local = ev.time.astimezone(tz)
    icon = {"High": "🔴", "Medium": "🟠"}.get(ev.impact, "⚪")
    extra = ", ".join(x for x in (f"prév. {ev.forecast}" if ev.forecast else "",
                                   f"préc. {ev.previous}" if ev.previous else "",
                                   f"réel {ev.actual}" if ev.actual else "") if x)
    return f"{icon} {local:%H:%M} {ev.currency} {ev.title}" + (f" ({extra})" if extra else "")
