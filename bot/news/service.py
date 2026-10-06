"""Service de veille : tourne en tâche de fond à côté du robot.

- collecte les sources en parallèle, chacune à son rythme (FinancialJuice chaque
  minute, la plupart toutes les `poll_minutes`) ;
- filtre, dédoublonne, note, puis fait analyser les meilleures infos par Claude ;
- envoie les alertes importantes (une seule fois par événement) et les rappels
  avant les annonces majeures ;
- envoie le briefing du matin ;
- fournit au moteur de trading le contexte de chaque symbole (fenêtre
  d'annonce à éviter, sentiment des news, état du calendrier).

Principe de sécurité : une panne (source, IA, calendrier) ne doit jamais faire
croire au robot qu'il n'y a « rien » : le calendrier absent ou périmé est signalé
et le moteur peut alors refuser les entrées.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .assets import FIAT, symbol_assets, symbol_direction_sign
from .calendar import BlackoutSettings, blackout_event, covers_now, format_event, relevant_currencies, upcoming
from .filters import FilterSettings, combined_weight, freshness, impact_hits, member_weight, run_filters, score_item
from .llm import events_for_prompt, format_brief
from .models import SOCIAL, CalendarEvent, NewsItem, SymbolContext

log = logging.getLogger(__name__)

IMPACT_WEIGHT = {"high": 3.0, "medium": 1.5, "low": 0.5, "none": 0.0}
IMPACT_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
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
    startup_alert_max_age_minutes: int = 15  # au démarrage : pas de rafale d'alertes sur le passé
    max_alerts_per_hour: int = 6
    event_reminder_minutes: int = 15  # rappel avant une annonce à fort impact (0 = off)
    llm_max_items: int = 25  # infos envoyées à Claude par cycle
    llm_min_score: float = 0.25
    sentiment_half_life_hours: float = 8.0
    breaking_blackout_minutes: int = 15  # pas d'entrée juste après une info urgente confirmée (0 = off)
    calendar_max_age_hours: float = 26  # au-delà, la protection calendrier n'est plus garantie
    filters: FilterSettings = field(default_factory=FilterSettings)
    blackout: BlackoutSettings = field(default_factory=BlackoutSettings)
    symbol_assets: dict = field(default_factory=dict)  # surcharge symbole -> actifs


def counts_for_sentiment(item: NewsItem) -> bool:
    a = item.analysis
    if not a or not a.get("relevant") or a.get("impact") == "none":
        return False
    # ni rumeur (sociale ou « selon des sources »), ni opinion, ni récapitulatif
    return not item.is_rumor and item.category == "news"


def symbol_sentiment(items: list[NewsItem], symbol: str, now: datetime, half_life_hours: float,
                     overrides: dict | None = None) -> tuple[float | None, int]:
    """Sentiment agrégé des infos analysées par l'IA pour un symbole, entre -1 et +1."""
    total, count = 0.0, 0
    for item in items:
        if not counts_for_sentiment(item):
            continue
        a = item.analysis
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
        # symboles dont on ne connaît aucune devise : pas de protection calendrier possible
        self.unrecognized = [s for s, a in self.assets_by_symbol.items()
                             if not relevant_currencies(a, settings.blackout)]

        self._lock = threading.Lock()
        self._raw: dict[str, list[NewsItem]] = {}  # dernier résultat valide de chaque source
        self._last_fetch: dict[str, float] = {}
        self._items: list[NewsItem] = []
        self._events: list[CalendarEvent] = []
        self.calendar_data_time: datetime | None = None
        self._alert_times: list[datetime] = []
        # au démarrage, on n'alerte pas sur ce qui a été publié avant (démarrage - 15 min) : pas de rafale
        self._alert_floor = clock() - timedelta(minutes=settings.startup_alert_max_age_minutes)
        self._ai_failures = 0
        self._ai_warned_at: datetime | None = None
        self._pending_brief: tuple[str, str] | None = None  # (date, texte) prêt mais pas encore envoyé
        self._brief_requested = threading.Event()
        jobs = list(sources) + ([calendar_source] if calendar_source else [])
        self.source_status: dict[str, str] = {src.name: "en attente" for src in jobs}
        self.last_refresh: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ================================================================ tâche de fond
    def start(self) -> None:
        # Le calendrier est chargé AVANT de rendre la main : après un redémarrage, la
        # protection autour des annonces est active dès la première bougie.
        if self.calendar_source:
            events = self._fetch_one(self.calendar_source, self.clock())
            self._last_fetch[self.calendar_source.name] = time.monotonic()
            if events is not None:
                self._publish_events(events)
        self._thread = threading.Thread(target=self._run, name="news", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(30)

    def tick(self) -> None:
        """Chaque étape est isolée : une panne de l'une n'empêche pas les autres."""
        now = self.clock()
        steps = [self.send_event_reminders, self.maybe_send_brief, self._serve_brief_request]
        if self.last_refresh is None or self._due_sources(time.monotonic()):
            steps.insert(0, lambda _now: self.refresh())
        for step in steps:
            try:
                step(now)
            except Exception:
                log.exception("Erreur dans la veille news")

    def _interval(self, source) -> float:
        return getattr(source, "poll_seconds", None) or self.s.poll_minutes * 60

    def _due_sources(self, mono: float) -> list:
        jobs = list(self.sources) + ([self.calendar_source] if self.calendar_source else [])
        return [src for src in jobs
                if mono - self._last_fetch.get(src.name, -1e18) >= self._interval(src)]

    # ================================================================ collecte
    def _set_status(self, name: str, text: str) -> None:
        with self._lock:
            self.source_status[name] = text

    def _fetch_one(self, source, now):
        """Résultat de la source, ou None si elle est en panne (≠ liste vide)."""
        try:
            data = source.fetch(self.session, now)
            self._set_status(source.name, f"ok ({len(data)})")
            return data
        except Exception as e:
            self._set_status(source.name, f"erreur : {e.__class__.__name__}")
            log.warning("Source %s indisponible : %s", source.name, e)
            return None

    def _publish_events(self, events: list[CalendarEvent]) -> None:
        data_time = getattr(self.calendar_source, "data_time", None) or self.clock()
        with self._lock:
            self._events = events
            self.calendar_data_time = data_time

    def collect(self, now: datetime) -> list[NewsItem]:
        """Interroge les sources dont c'est le tour et renvoie une COPIE de tout ce qui est connu.
        Le calendrier est publié immédiatement (avant l'analyse IA, qui peut être lente)."""
        mono = time.monotonic()
        due = self._due_sources(mono)
        if due:
            with ThreadPoolExecutor(max_workers=min(8, len(due))) as pool:
                results = list(pool.map(lambda src: self._fetch_one(src, now), due))
            for src, res in zip(due, results):
                self._last_fetch[src.name] = mono
                if res is None:
                    continue  # en panne : on garde le dernier résultat valide
                if src is self.calendar_source:
                    self._publish_events(res)
                else:
                    self._raw[src.name] = res
        # copies : le filtre modifie les objets, il ne doit pas toucher au cache des sources
        return [dataclasses.replace(i, assets=set(i.assets), engagement=dict(i.engagement), flags=[],
                                    analysis=None, groups={}, member_ids=set())
                for res in list(self._raw.values()) for i in res]

    def refresh(self) -> list[NewsItem]:
        now = self.clock()
        try:
            items = run_filters(self.collect(now), now, self.s.filters)
            cached = self.store.analyses([i.id for i in items])
            for i in items:
                i.analysis = cached.get(i.id)
            if self.analyzer:
                todo = [i for i in items if i.analysis is None and i.score >= self.s.llm_min_score]
                todo = todo[: self.s.llm_max_items]
                if todo:
                    # infos récentes déjà analysées : l'IA peut reconnaître qu'une nouvelle info
                    # rapporte le même événement (dédoublonnage d'un lot à l'autre)
                    known = [i for i in items if i.analysis is not None and i.analysis.get("relevant")
                             and now - i.published <= timedelta(hours=6)][:30]
                    try:
                        results = self.analyzer.analyze(todo, sorted(self.traded_assets), known)
                    except Exception:
                        log.exception("Analyse IA impossible pour ce cycle")
                        results = None
                    self._track_ai_health(results, now)
                    for i in todo:
                        i.analysis = (results or {}).get(i.id)
            # sauvegarde AVANT la fusion : les analyses des infos fusionnées restent en mémoire
            self.store.save_items(items, now)
            items = self.merge_same_events(items, now)
            # l'IA a jugé l'info sans intérêt -> on la sort des listes (elle reste en mémoire)
            useful = [i for i in items
                      if not (i.analysis and (not i.analysis.get("relevant") or i.analysis.get("impact") == "none"))]
            self.store.purge(now - timedelta(days=14))
            with self._lock:
                self._items = useful
        finally:
            self.last_refresh = now  # même en cas d'échec : pas de nouvel essai toutes les 30 s
        self.send_alerts(useful, now)
        return useful

    def _track_ai_health(self, results, now: datetime) -> None:
        """Prévient (au plus toutes les 6 h) si l'IA échoue à répétition : clé révoquée, crédit épuisé…"""
        if results:
            self._ai_failures = 0
            return
        if results is None or getattr(self.analyzer, "last_error", None):
            self._ai_failures += 1
        if self._ai_failures >= 3 and (self._ai_warned_at is None or now - self._ai_warned_at >= timedelta(hours=6)):
            self._ai_warned_at = now
            err = getattr(self.analyzer, "last_error", "") or "erreur inconnue"
            self.notifier.send(f"⚠️ Analyse IA en échec depuis {self._ai_failures} cycles ({err}). "
                               "La veille continue avec le filtre automatique. Vérifie ta clé / ton crédit Anthropic.")

    @staticmethod
    def _trust(item: NewsItem) -> tuple:
        a = item.analysis or {}
        return (not item.is_rumor, item.category == "news", bool(a.get("relevant", True)),
                IMPACT_RANK.get(a.get("impact", "none"), 0), member_weight(item), item.weight, -item.tier)

    def merge_same_events(self, items: list[NewsItem], now: datetime) -> list[NewsItem]:
        """Fusionne les infos que l'IA a reconnues comme le même événement écrit autrement.
        Quel que soit le sens indiqué par l'IA, on garde TOUJOURS la version la plus digne de
        confiance (fait confirmé > rumeur, info > analyse/récap, impact, fiabilité) et on y
        fusionne l'autre : une confirmation ne disparaît jamais dans une rumeur."""
        by_id = {i.id: i for i in items}
        # une info déjà fusionnée dans une autre est représentée par celle-ci
        owner_of = {mid: i for i in items for mid in i.member_ids}
        merged: set[str] = set()
        for item in items:
            if item.id in merged:
                continue
            ref = (item.analysis or {}).get("same_event_as") or ""
            other = by_id.get(ref) or owner_of.get(ref)
            seen = {item.id}
            while other is not None and other.id in merged and other.id not in seen:
                seen.add(other.id)  # cible déjà fusionnée : on suit vers son représentant
                other = next((i for i in items if other.id in i.member_ids and i.id not in merged), None)
            if other is None or other is item or other.id in merged:
                continue
            keep, drop = (item, other) if self._trust(item) > self._trust(other) else (other, item)
            for group, weight in drop.groups.items():
                keep.groups[group] = max(keep.groups.get(group, 0.0), weight)
            keep.weight = combined_weight(keep.groups)
            keep.corroborations = max(keep.corroborations, len(keep.groups))
            keep.corroborated_by_tier = min(keep.corroborated_by_tier, drop.corroborated_by_tier)
            keep.assets |= drop.assets
            keep.member_ids |= drop.member_ids | {drop.id}
            keep.score = score_item(keep, now, self.s.filters)
            merged.add(drop.id)
        return [i for i in items if i.id not in merged]

    # ================================================================ alertes
    def _delivered(self, ok: bool) -> bool:
        # sans Telegram configuré, on considère le message « livré » (sinon on réessaierait sans fin)
        return ok or not getattr(self.notifier, "enabled", True)

    def is_alert_worthy(self, item: NewsItem, now: datetime) -> bool:
        if now - item.published > timedelta(minutes=self.s.alert_max_age_minutes) or item.published < self._alert_floor:
            return False
        if item.social_only or item.category != "news":
            return False  # rumeur sociale, analyse ou récapitulatif : jamais d'alerte
        a = item.analysis
        if a is not None:
            if a.get("is_rumor"):
                return False
            if a["impact"] == "high" and a["credibility"] != "low":
                return True
            return a["impact"] == "medium" and a["credibility"] == "high" and item.score >= self.s.alert_min_score
        # sans IA : fiabilité combinée >= 0.85 (officiel, agence majeure, ou 2 groupes indépendants)
        return item.score >= self.s.alert_min_score and item.weight >= 0.85 and impact_hits(item) >= 1

    def send_alerts(self, items: list[NewsItem], now: datetime) -> None:
        candidates = [i for i in items if self.is_alert_worthy(i, now)]
        if not candidates:
            return
        # une info est « déjà alertée » si N'IMPORTE QUELLE de ses copies l'a été
        ids = {i.id: (i.member_ids | {i.id}) for i in candidates}
        already = self.store.alerted_ids(sorted(set().union(*ids.values())))
        fresh = [i for i in candidates if not (ids[i.id] & already)]
        self._alert_times = [t for t in self._alert_times if now - t < timedelta(hours=1)]
        room = max(0, self.s.max_alerts_per_hour - len(self._alert_times))
        to_send = fresh[:room]
        if len(fresh) > room:
            log.info("%d alertes news non envoyées (limite horaire atteinte)", len(fresh) - room)
        if not to_send:
            return
        ok = self.notifier.send("\n\n".join(self.format_item(i, now, detailed=True) for i in to_send))
        if self._delivered(ok):
            self.store.mark_alerted(sorted(set().union(*(ids[i.id] for i in to_send))), now)
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
            cred = {"high": "fiabilité haute", "medium": "fiabilité moyenne", "low": "fiabilité faible"}.get(
                a.get("credibility"), "")
            moves = " ".join(f"{m['asset']}{ARROW.get(m['direction'], '')}" for m in a.get("asset_moves", []))
            lines = [f"{head} ({cred})" + (" – ⚠️ RUMEUR" if item.is_rumor else ""), text]
            if moves:
                lines.append(f"Effet probable : {moves}")
        else:
            lines = [("⚠️ RUMEUR – " if item.is_rumor else "📰 ") + item.title]
        lines.append(f"Source : {item.source} ({KIND_FR.get(item.kind, item.kind)}){sources} – {age_txt}")
        if detailed and item.url:
            lines.append(item.url)
        return "\n".join(lines)

    def _reminded(self) -> dict[str, str]:
        raw = self.store.get("reminded_events")
        try:
            return json.loads(raw[0]) if raw else {}
        except ValueError:
            return {}

    def send_event_reminders(self, now: datetime) -> None:
        if self.s.event_reminder_minutes <= 0:
            return
        with self._lock:
            events = list(self._events)
        soon = upcoming(events, self.currencies, now, hours=self.s.event_reminder_minutes / 60, min_impact="High")
        reminded = self._reminded()  # persisté : pas de doublon après un redémarrage
        new = [e for e in soon if e.id not in reminded]
        if not new:
            return
        b = self.s.blackout
        protected = b.enabled and any(e.impact in b.impacts for e in new)
        text = (f"⏰ Annonce(s) dans moins de {self.s.event_reminder_minutes} min :\n"
                + "\n".join(format_event(e, self.tz) for e in new)
                + ("\nPas de nouvelle entrée sur les actifs concernés pendant la fenêtre de sécurité."
                   if protected else ""))
        if self._delivered(self.notifier.send(text)):
            reminded.update({e.id: e.time.isoformat() for e in new})
            limit = (now - timedelta(days=8)).isoformat()
            reminded = {k: v for k, v in reminded.items() if v >= limit}
            self.store.put("reminded_events", json.dumps(reminded), now)

    # ================================================================ briefing
    def request_brief(self) -> None:
        """Demande un briefing immédiat (commande /brief) : il est préparé par la tâche de
        fond, jamais par la boucle de trading, car l'IA peut prendre du temps."""
        self._brief_requested.set()

    def _serve_brief_request(self, now: datetime) -> None:
        if self._brief_requested.is_set():
            self._brief_requested.clear()
            self.notifier.send(self.brief_text(now))

    def maybe_send_brief(self, now: datetime) -> None:
        if not self.s.morning_brief_time:
            return
        local = now.astimezone(self.tz)
        if not self.s.brief_weekends and local.weekday() >= 5:
            return
        hh, mm = (int(x) for x in self.s.morning_brief_time.split(":"))
        if (local.hour, local.minute) < (hh, mm):
            return
        today = local.date().isoformat()
        last = self.store.get("last_brief_date")
        if last and last[0] == today:
            return
        if self.last_refresh is None:
            return  # attendre une première collecte
        if not self._pending_brief or self._pending_brief[0] != today:
            self._pending_brief = (today, self.brief_text(now))  # préparé une seule fois (coût IA)
        if self._delivered(self.notifier.send(self._pending_brief[1])):
            self.store.put("last_brief_date", today, now)
            self._pending_brief = None

    def brief_text(self, now: datetime) -> str:
        local = now.astimezone(self.tz)
        with self._lock:
            items = list(self._items)
            events = list(self._events)
        day_end = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), self.tz)
        hours_left = max(1.0, (day_end - now).total_seconds() / 3600)
        today = upcoming(events, self.currencies, now, hours=hours_left, min_impact="Medium")
        # même calcul que celui utilisé par le moteur de trading
        sentiments = {s: self.context(s, now).sentiment for s in self.symbols}
        reliable, _ = self.split_reliable(items)

        parts = [f"☀️ BRIEFING DU {local:%d/%m/%Y}"]
        if not self.calendar_ok(now):
            parts.append("⚠️ Calendrier économique indisponible : la protection autour des annonces n'est pas garantie.")
        brief = None
        if self.analyzer and reliable:
            try:
                brief = self.analyzer.brief(reliable[:20], events_for_prompt(today, self.tz), self.symbols,
                                            sentiments, f"{local:%A %d %B %Y %H:%M}")
            except Exception:
                log.exception("Briefing IA impossible, version simple envoyée")
        if brief is not None:
            parts.append(format_brief(brief))
        else:
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
    def calendar_ok(self, now: datetime | None = None) -> bool:
        """La protection calendrier est-elle garantie ? (calendrier chargé et récent)"""
        if not self.s.blackout.enabled or self.calendar_source is None:
            return True
        now = now or self.clock()
        with self._lock:
            t = self.calendar_data_time
            events = list(self._events)
        return (t is not None and now - t <= timedelta(hours=self.s.calendar_max_age_hours)
                and covers_now(events, now))

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
            symbol=symbol, sentiment=sentiment, sentiment_items=n,
            top_items=[i for i in items if not i.is_rumor and i.category == "news"][:3],
            next_event=nxt[0] if nxt else None,
            blackout_event=blackout_event(events, assets, now, self.s.blackout) or self.breaking_event(items, now),
            calendar_ok=self.calendar_ok(now),
            recognized=bool(currencies),
        )

    def breaking_event(self, items: list[NewsItem], now: datetime) -> CalendarEvent | None:
        """Info urgente imprévue (intervention, décision d'urgence, choc géopolitique) confirmée
        par une source de premier plan : pas de nouvelle entrée pendant quelques minutes."""
        minutes = self.s.breaking_blackout_minutes
        if minutes <= 0:
            return None
        for item in items:
            a = item.analysis or {}
            if (a.get("impact") == "high" and a.get("credibility") == "high" and not item.is_rumor
                    and item.corroborated_by_tier == 1 and item.category == "news"
                    and timedelta(0) <= now - item.published <= timedelta(minutes=minutes)):
                currency = next((x for x in sorted(item.assets) if x in FIAT), "")
                return CalendarEvent(title=f"info urgente : {a.get('summary_fr') or item.title}"[:160],
                                     currency=currency, time=item.published, impact="High")
        return None

    @staticmethod
    def split_reliable(items: list[NewsItem]) -> tuple[list[NewsItem], list[NewsItem]]:
        """(infos fiables, rumeurs/réseaux sociaux à confirmer)."""
        reliable = [i for i in items if not i.is_rumor and not (
            i.kind == SOCIAL and i.analysis is None and i.corroborated_by_tier > 2)]
        ids = {i.id for i in reliable}
        return reliable, [i for i in items if i.id not in ids]

    def _status_snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self.source_status)

    def news_text(self, limit: int = 8, social_limit: int = 3) -> str:
        now = self.clock()
        with self._lock:
            items = list(self._items)
        reliable, unconfirmed = self.split_reliable(items)
        status = self._status_snapshot()
        ok = sum(1 for v in status.values() if v.startswith("ok"))
        parts = [f"📰 Infos fiables (sources actives : {ok}/{len(status)})"]
        parts += [self.format_item(i, now) for i in reliable[:limit]] or ["Rien de marquant pour tes actifs."]
        if unconfirmed[:social_limit]:
            parts.append("💬 Réseaux sociaux – NON CONFIRMÉ")
            parts += [self.format_item(i, now) for i in unconfirmed[:social_limit]]
        return "\n\n".join(parts)

    def calendar_text(self, hours: float = 36) -> str:
        now = self.clock()
        with self._lock:
            events = list(self._events)
        warn = "" if self.calendar_ok(now) else "⚠️ Calendrier indisponible ou périmé : liste peut-être incomplète.\n"
        evs = upcoming(events, self.currencies, now, hours=hours, min_impact="Medium")
        if not evs:
            return warn + "🗓 Aucune annonce moyenne/forte à venir pour tes devises."
        lines, day = [], None
        for e in evs:
            d = e.time.astimezone(self.tz).date()
            if d != day:
                lines.append(f"— {d:%A %d/%m} —")
                day = d
            lines.append(format_event(e, self.tz))
        return warn + "🗓 Agenda économique\n" + "\n".join(lines)

    def status_line(self) -> str:
        status = self._status_snapshot()
        ok = sum(1 for v in status.values() if v.startswith("ok"))
        last = self.last_refresh.astimezone(self.tz).strftime("%H:%M") if self.last_refresh else "jamais"
        ia = "IA active" if self.analyzer else "IA désactivée"
        line = f"Veille news : {ok}/{len(status)} sources OK, dernière mise à jour {last}, {ia}"
        if self.calendar_source and status.get(self.calendar_source.name) == "en attente":
            line += "\n⏳ Calendrier économique en cours de chargement"
        elif not self.calendar_ok():
            line += "\n⚠️ Calendrier économique indisponible ou périmé : aucune nouvelle entrée"
        if self.analyzer and self._ai_failures >= 3:
            line += f"\n⚠️ IA en échec depuis {self._ai_failures} cycles ({getattr(self.analyzer, 'last_error', '')})"
        if self.unrecognized:
            line += (f"\n⚠️ Symbole(s) non reconnu(s) : {', '.join(self.unrecognized)} – aucune protection news. "
                     "Renseigne news.symbol_assets dans config.yaml.")
        return line

    def dump_status(self) -> str:
        return json.dumps(self._status_snapshot(), ensure_ascii=False, indent=1)
