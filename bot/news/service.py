"""Service de veille : tourne en tâche de fond à côté du robot.

- collecte toutes les sources en parallèle toutes les `poll_minutes` ;
- filtre, dédoublonne, note, puis fait analyser les meilleures infos par Claude ;
- envoie les alertes importantes (une seule fois par info) et les rappels
  avant les annonces majeures ;
- envoie le briefing du matin ;
- fournit au moteur de trading le contexte de chaque symbole (fenêtre
  d'annonce à éviter, sentiment des news).
"""
from __future__ import annotations

import json
import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .assets import symbol_assets, symbol_direction_sign
from .calendar import BlackoutSettings, blackout_event, format_event, relevant_currencies, upcoming
from .filters import FilterSettings, freshness, impact_hits, run_filters
from .llm import events_for_prompt, format_brief
from .models import SOCIAL, CalendarEvent, NewsItem, SymbolContext

log = logging.getLogger(__name__)

IMPACT_WEIGHT = {"high": 3.0, "medium": 1.5, "low": 0.5, "none": 0.0}
CREDIBILITY_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.2}
ARROW = {"up": "⬆", "down": "⬇", "unclear": "↔"}
KIND_FR = {"official": "officiel", "news": "média", "social": "réseau social"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class NewsSettings:
    poll_minutes: float = 5
    timezone: str = "Europe/Paris"
    morning_brief_time: str = "07:30"  # heure locale, "" pour désactiver
    brief_weekends: bool = False
    alert_min_score: float = 0.6
    alert_max_age_minutes: int = 90  # on n'alerte pas sur une info plus ancienne
    max_alerts_per_hour: int = 6
    event_reminder_minutes: int = 15  # rappel avant une annonce à fort impact (0 = off)
    llm_max_items: int = 25  # infos envoyées à Claude par cycle
    llm_min_score: float = 0.25
    sentiment_half_life_hours: float = 8.0
    filters: FilterSettings = field(default_factory=FilterSettings)
    blackout: BlackoutSettings = field(default_factory=BlackoutSettings)
    symbol_assets: dict = field(default_factory=dict)  # surcharge symbole -> actifs


def symbol_sentiment(items: list[NewsItem], symbol: str, now: datetime, half_life_hours: float,
                     overrides: dict | None = None) -> tuple[float | None, int]:
    """Sentiment agrégé des infos analysées par l'IA pour un symbole, entre -1 et +1."""
    total, count = 0.0, 0
    for item in items:
        a = item.analysis
        if not a or not a.get("relevant") or a.get("impact") == "none":
            continue
        net = 0
        for move in a.get("asset_moves", []):
            sign = symbol_direction_sign(symbol, move["asset"], overrides)
            if sign and move["direction"] in ("up", "down"):
                net += sign * (1 if move["direction"] == "up" else -1)
        if net == 0:
            continue
        weight = (IMPACT_WEIGHT.get(a["impact"], 0) * CREDIBILITY_WEIGHT.get(a["credibility"], 0.2)
                  * freshness(item, now, half_life_hours))
        total += (1 if net > 0 else -1) * weight
        count += 1
    if count == 0:
        return None, 0
    return math.tanh(total / 3), count


class NewsService:
    def __init__(self, settings: NewsSettings, symbols: list[str], notifier, store, sources: list,
                 calendar_source=None, analyzer=None, session=None, clock=utcnow):
        self.s = settings
        self.symbols = symbols
        self.notifier = notifier
        self.store = store
        self.sources = sources
        self.calendar_source = calendar_source
        self.analyzer = analyzer
        self.session = session
        self.clock = clock
        self.tz = ZoneInfo(settings.timezone)
        self.assets_by_symbol = {s: symbol_assets(s, settings.symbol_assets) for s in symbols}
        self.traded_assets = set().union(*self.assets_by_symbol.values()) if symbols else set()
        self.currencies = relevant_currencies(self.traded_assets, settings.blackout)
        self.s.filters.traded_assets = self.traded_assets

        self._lock = threading.Lock()
        self._items: list[NewsItem] = []
        self._events: list[CalendarEvent] = []
        self._alert_times: list[datetime] = []
        self._reminded: set[str] = set()
        self.source_status: dict[str, str] = {}
        self.last_refresh: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ================================================================ tâche de fond
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="news", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Erreur dans la veille news")
            self._stop.wait(30)

    def tick(self) -> None:
        now = self.clock()
        if self.last_refresh is None or now - self.last_refresh >= timedelta(minutes=self.s.poll_minutes):
            self.refresh()
        self.send_event_reminders(now)
        self.maybe_send_brief(now)

    # ================================================================ collecte
    def _fetch_one(self, source, now):
        try:
            data = source.fetch(self.session, now)
            self.source_status[source.name] = f"ok ({len(data)})"
            return data
        except Exception as e:
            self.source_status[source.name] = f"erreur : {e.__class__.__name__}"
            log.warning("Source %s indisponible : %s", source.name, e)
            return []

    def collect(self, now: datetime) -> tuple[list[NewsItem], list[CalendarEvent] | None]:
        jobs = list(self.sources) + ([self.calendar_source] if self.calendar_source else [])
        if not jobs:
            return [], None
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            results = list(pool.map(lambda src: self._fetch_one(src, now), jobs))
        items = [i for r in results[: len(self.sources)] for i in r]
        events = results[-1] if self.calendar_source else None
        if self.calendar_source and not events and self._events:
            events = None  # calendrier momentanément indisponible : on garde l'ancien
        return items, events

    def refresh(self) -> list[NewsItem]:
        now = self.clock()
        raw, events = self.collect(now)
        items = run_filters(raw, now, self.s.filters)

        cached = self.store.analyses([i.id for i in items])
        for i in items:
            i.analysis = cached.get(i.id)
        if self.analyzer:
            todo = [i for i in items if i.analysis is None and i.score >= self.s.llm_min_score]
            todo = todo[: self.s.llm_max_items]
            if todo:
                results = self.analyzer.analyze(todo, sorted(self.traded_assets))
                for i in todo:
                    i.analysis = results.get(i.id)
        items = self.merge_same_events(items)
        # l'IA a jugé l'info sans intérêt -> on la sort des listes (elle reste en mémoire)
        useful = [i for i in items
                  if not (i.analysis and (not i.analysis.get("relevant") or i.analysis.get("impact") == "none"))]
        self.store.save_items(items, now)
        self.store.purge(now - timedelta(days=14))

        with self._lock:
            self._items = useful
            if events is not None:
                self._events = events
            self.last_refresh = now
        self.send_alerts(useful, now)
        return useful

    @staticmethod
    def merge_same_events(items: list[NewsItem]) -> list[NewsItem]:
        """Fusionne les infos que l'IA a reconnues comme le même événement écrit autrement."""
        by_id = {i.id: i for i in items}
        merged: set[str] = set()
        for item in items:
            target_id = (item.analysis or {}).get("same_event_as") or ""
            seen = {item.id}
            # suit la chaîne a -> b -> c jusqu'à l'info de référence (sans boucle)
            while target_id in by_id and target_id not in seen and (by_id[target_id].analysis or {}).get("same_event_as"):
                seen.add(target_id)
                target_id = by_id[target_id].analysis["same_event_as"]
            target = by_id.get(target_id)
            if target is None or target is item or target_id in merged and target_id == item.id:
                continue
            target.corroborations += item.corroborations
            target.corroborated_by_tier = min(target.corroborated_by_tier, item.corroborated_by_tier, item.tier)
            target.assets |= item.assets
            merged.add(item.id)
        return [i for i in items if i.id not in merged]

    # ================================================================ alertes
    def is_alert_worthy(self, item: NewsItem, now: datetime) -> bool:
        if now - item.published > timedelta(minutes=self.s.alert_max_age_minutes):
            return False
        a = item.analysis
        if a is not None:
            if a["impact"] == "high" and a["credibility"] != "low":
                return True
            return a["impact"] == "medium" and a["credibility"] == "high" and item.score >= self.s.alert_min_score
        # sans IA : seulement les sources fiables ou recoupées, avec des mots d'impact, jamais les rumeurs
        return (item.score >= self.s.alert_min_score and not item.is_rumor and impact_hits(item) >= 1
                and (item.tier == 1 or item.corroborations >= 2))

    def send_alerts(self, items: list[NewsItem], now: datetime) -> None:
        candidates = [i for i in items if self.is_alert_worthy(i, now)]
        if not candidates:
            return
        already = self.store.alerted_ids([i.id for i in candidates])
        fresh = [i for i in candidates if i.id not in already]
        self._alert_times = [t for t in self._alert_times if now - t < timedelta(hours=1)]
        room = max(0, self.s.max_alerts_per_hour - len(self._alert_times))
        to_send = fresh[:room]
        if len(fresh) > room:
            log.info("%d alertes news non envoyées (limite horaire atteinte)", len(fresh) - room)
        if not to_send:
            return
        self.notifier.send("\n\n".join(self.format_item(i, now, detailed=True) for i in to_send))
        self.store.mark_alerted([i.id for i in to_send])
        self._alert_times += [now] * len(to_send)

    def format_item(self, item: NewsItem, now: datetime, detailed: bool = False) -> str:
        a = item.analysis
        age = int((now - item.published).total_seconds() // 60)
        age_txt = f"il y a {age} min" if age < 120 else f"il y a {age // 60} h"
        sources = f" +{item.corroborations - 1} source(s)" if item.corroborations > 1 else ""
        if a:
            head = {"high": "🚨 IMPACT FORT", "medium": "📰 Impact moyen", "low": "📰 Impact faible"}.get(
                a["impact"], "📰")
            text = a.get("summary_fr") or item.title
            cred = {"high": "fiabilité haute", "medium": "fiabilité moyenne", "low": "fiabilité faible"}[a["credibility"]]
            moves = " ".join(f"{m['asset']}{ARROW.get(m['direction'], '')}" for m in a.get("asset_moves", []))
            lines = [f"{head} ({cred})" + (" – ⚠️ RUMEUR" if a.get("is_rumor") else ""), text]
            if moves:
                lines.append(f"Effet probable : {moves}")
        else:
            lines = [("⚠️ RUMEUR – " if item.is_rumor else "📰 ") + item.title]
        lines.append(f"Source : {item.source} ({KIND_FR.get(item.kind, item.kind)}){sources} – {age_txt}")
        if detailed and item.url:
            lines.append(item.url)
        return "\n".join(lines)

    def send_event_reminders(self, now: datetime) -> None:
        if self.s.event_reminder_minutes <= 0:
            return
        with self._lock:
            events = list(self._events)
        soon = upcoming(events, self.currencies, now, hours=self.s.event_reminder_minutes / 60, min_impact="High")
        new = [e for e in soon if e.id not in self._reminded]
        if not new:
            return
        self._reminded |= {e.id for e in new}
        lines = [format_event(e, self.tz) for e in new]
        self.notifier.send(f"⏰ Annonce(s) dans moins de {self.s.event_reminder_minutes} min :\n" + "\n".join(lines)
                           + "\nPas de nouvelle entrée sur les actifs concernés pendant la fenêtre de sécurité.")

    # ================================================================ briefing
    def maybe_send_brief(self, now: datetime) -> None:
        if not self.s.morning_brief_time:
            return
        local = now.astimezone(self.tz)
        if not self.s.brief_weekends and local.weekday() >= 5:
            return
        hh, mm = (int(x) for x in self.s.morning_brief_time.split(":"))
        if (local.hour, local.minute) < (hh, mm):
            return
        last = self.store.get("last_brief_date")
        if last and last[0] == local.date().isoformat():
            return
        if self.last_refresh is None:
            return  # attendre une première collecte
        self.store.put("last_brief_date", local.date().isoformat(), now)
        self.notifier.send(self.brief_text(now))

    def brief_text(self, now: datetime) -> str:
        local = now.astimezone(self.tz)
        with self._lock:
            items = list(self._items)
            events = list(self._events)
        day_end = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), self.tz)
        hours_left = max(1.0, (day_end - now).total_seconds() / 3600)
        today = upcoming(events, self.currencies, now, hours=hours_left, min_impact="Medium")
        sentiments = {s: symbol_sentiment(items, s, now, self.s.sentiment_half_life_hours,
                                          self.s.symbol_assets)[0] for s in self.symbols}

        parts = [f"☀️ BRIEFING DU {local:%d/%m/%Y}"]
        brief = None
        if self.analyzer and items:
            brief = self.analyzer.brief(items[:20], events_for_prompt(today, self.tz), self.symbols,
                                        sentiments, f"{local:%A %d %B %Y %H:%M}")
        if brief is not None:
            parts.append(format_brief(brief))
        else:
            reliable, _ = self.split_reliable(items)
            if reliable:
                parts.append("Infos clés :\n" + "\n".join(f"• {i.analysis.get('summary_fr') if i.analysis else i.title}"
                                                           f" ({i.source})" for i in reliable[:6]))
            sent = [f"{s} {v:+.2f}" for s, v in sentiments.items() if v is not None]
            if sent:
                parts.append("Sentiment news : " + ", ".join(sent))
        parts.append("🗓 Agenda du jour :\n" + ("\n".join(format_event(e, self.tz) for e in today)
                                                if today else "Aucune annonce majeure."))
        return "\n\n".join(parts)

    # ================================================================ lecture (moteur, Telegram)
    def context(self, symbol: str, now: datetime | None = None) -> SymbolContext:
        now = now or self.clock()
        assets = self.assets_by_symbol.get(symbol) or symbol_assets(symbol, self.s.symbol_assets)
        with self._lock:
            items = [i for i in self._items if i.assets & assets]
            events = list(self._events)
        sentiment, n = symbol_sentiment(items, symbol, now, self.s.sentiment_half_life_hours, self.s.symbol_assets)
        currencies = relevant_currencies(assets, self.s.blackout)
        nxt = upcoming(events, currencies, now, hours=24, min_impact="High")
        return SymbolContext(
            symbol=symbol, sentiment=sentiment, sentiment_items=n, top_items=items[:3],
            next_event=nxt[0] if nxt else None,
            blackout_event=blackout_event(events, assets, now, self.s.blackout),
        )

    @staticmethod
    def split_reliable(items: list[NewsItem]) -> tuple[list[NewsItem], list[NewsItem]]:
        """(infos fiables, rumeurs/réseaux sociaux à confirmer)."""
        reliable = [i for i in items if not i.is_rumor and not (
            i.kind == SOCIAL and i.analysis is None and i.corroborated_by_tier > 2)]
        ids = {i.id for i in reliable}
        return reliable, [i for i in items if i.id not in ids]

    def news_text(self, limit: int = 8, social_limit: int = 3) -> str:
        now = self.clock()
        with self._lock:
            items = list(self._items)
        reliable, unconfirmed = self.split_reliable(items)
        ok = sum(1 for v in self.source_status.values() if v.startswith("ok"))
        parts = [f"📰 Infos fiables (sources actives : {ok}/{len(self.source_status)})"]
        parts += [self.format_item(i, now) for i in reliable[:limit]] or ["Rien de marquant pour tes actifs."]
        if unconfirmed[:social_limit]:
            parts.append("💬 Réseaux sociaux – NON CONFIRMÉ")
            parts += [self.format_item(i, now) for i in unconfirmed[:social_limit]]
        return "\n\n".join(parts)

    def calendar_text(self, hours: float = 36) -> str:
        now = self.clock()
        with self._lock:
            events = list(self._events)
        evs = upcoming(events, self.currencies, now, hours=hours, min_impact="Medium")
        if not evs:
            return "🗓 Aucune annonce moyenne/forte à venir pour tes devises."
        lines, day = [], None
        for e in evs:
            d = e.time.astimezone(self.tz).date()
            if d != day:
                lines.append(f"— {d:%A %d/%m} —")
                day = d
            lines.append(format_event(e, self.tz))
        return "🗓 Agenda économique\n" + "\n".join(lines)

    def status_line(self) -> str:
        ok = sum(1 for v in self.source_status.values() if v.startswith("ok"))
        last = self.last_refresh.astimezone(self.tz).strftime("%H:%M") if self.last_refresh else "jamais"
        ia = "IA active" if self.analyzer else "IA désactivée"
        return f"Veille news : {ok}/{len(self.source_status)} sources OK, dernière mise à jour {last}, {ia}"

    def dump_status(self) -> str:
        return json.dumps(self.source_status, ensure_ascii=False, indent=1)
