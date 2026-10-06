"""Non-régression des problèmes trouvés par la revue de code de la veille news."""
import json
from datetime import datetime, timedelta, timezone

import httpx2
import anthropic
import pytest

from bot.news.calendar import BlackoutSettings
from bot.news.llm import ClaudeAnalyzer
from bot.news.models import NEWS, OFFICIAL, SOCIAL, CalendarEvent, NewsItem
from bot.news.service import NewsService, NewsSettings
from bot.news.sources import ForexFactoryCalendar
from bot.news.store import NewsStore

NOW = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)


def item(title, source="Src", kind=NEWS, tier=1, minutes_ago=5, group=""):
    return NewsItem(source, kind, tier, title, f"https://{source.replace(' ', '')}/{abs(hash(title))}",
                    NOW - timedelta(minutes=minutes_ago), group=group)


def verdict(impact="high", cred="high", moves=(("USD", "down"),), same="", rumor=False, relevant=True):
    return {"relevant": relevant, "impact": impact, "credibility": cred, "is_rumor": rumor,
            "asset_moves": [{"asset": a, "direction": d} for a, d in moves], "summary_fr": "Résumé.",
            "same_event_as": same}


class Notifier:
    enabled = True

    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def send(self, text):
        self.sent.append(text)
        return self.ok


class Source:
    def __init__(self, name, items=None, fail=False, poll_seconds=None):
        self.name, self.items, self.fail, self.poll_seconds = name, items or [], fail, poll_seconds
        self.data_time = None

    def fetch(self, session, now):
        if self.fail:
            raise ConnectionError("down")
        self.data_time = now
        return list(self.items)


class Analyzer:
    def __init__(self, verdicts=None, error=None, on_call=None):
        self.verdicts, self.error, self.on_call, self.calls = verdicts or {}, error, on_call, 0

    def analyze(self, items, assets, known=None):
        self.calls += 1
        if self.on_call:
            self.on_call()
        if self.error:
            raise self.error
        return {i.id: {"id": i.id, **self.verdicts[i.title]} for i in items if i.title in self.verdicts}

    def brief(self, *a, **k):
        return None


def make(tmp_path, sources, analyzer=None, events=None, notifier=None, store=None, cal=None, **kw):
    cal = cal or Source("cal", events or [])
    kw.setdefault("morning_brief_time", "")
    return NewsService(NewsSettings(**kw), ["EURUSD", "USDJPY"], notifier or Notifier(),
                       store or NewsStore(tmp_path / "n.db"), sources, cal, analyzer, None, lambda: NOW)


def alerts(svc):
    return [m for m in svc.notifier.sent if "IMPACT FORT" in m or "📰" in m]


# ---------------------------------------------------------------- IA en panne
def test_analyzer_crash_still_publishes_items_events_and_refresh_time(tmp_path):
    nfp = CalendarEvent("Non-Farm Employment Change", "USD", NOW + timedelta(minutes=10), "High")
    svc = make(tmp_path, [Source("s", [item("Fed unexpectedly cuts rates")])],
               Analyzer(error=RuntimeError("boom")), events=[nfp])
    useful = svc.refresh()
    assert useful and svc.last_refresh == NOW
    assert svc.context("EURUSD").blackout_event is nfp


def test_calendar_is_published_before_the_slow_ai_step(tmp_path):
    nfp = CalendarEvent("Non-Farm Employment Change", "USD", NOW + timedelta(minutes=10), "High")
    seen = {}
    svc = None

    def during_analysis():
        seen["blackout"] = svc.context("EURUSD").blackout_event

    an = Analyzer({"Fed unexpectedly cuts rates": verdict()}, on_call=during_analysis)
    svc = make(tmp_path, [Source("s", [item("Fed unexpectedly cuts rates")])], an, events=[nfp])
    svc.refresh()
    assert seen["blackout"] is nfp


def _parse_response(text, stop_reason):
    def handler(request):
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}})
    return anthropic.Anthropic(api_key="x", max_retries=0,
                               http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))


def test_truncated_ai_answer_is_ignored_not_raised():
    client = _parse_response('{"items": [{"id": "abc", "relev', "max_tokens")
    out = ClaudeAnalyzer(client=client).analyze([item("Fed cuts")], ["USD"])
    assert out == {}


# ---------------------------------------------------------------- calendrier : échec « fermé »
def test_calendar_not_ok_before_first_load_and_loaded_by_start(tmp_path):
    ev = CalendarEvent("CPI m/m", "USD", NOW + timedelta(hours=3), "High")
    svc = make(tmp_path, [], None, events=[ev])
    assert not svc.calendar_ok()
    svc.start()
    svc.stop()
    assert svc.calendar_ok() and svc.context("EURUSD").next_event == ev


def test_calendar_not_ok_when_source_always_fails(tmp_path):
    svc = make(tmp_path, [], None, cal=Source("cal", fail=True))
    svc.refresh()
    assert not svc.context("EURUSD").calendar_ok


def test_calendar_ok_when_blackout_disabled(tmp_path):
    svc = make(tmp_path, [], None, cal=Source("cal", fail=True), blackout=BlackoutSettings(enabled=False))
    assert svc.calendar_ok()


class Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code, self.headers = text, status, {}
        self.content = text.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class Session:
    def __init__(self, *texts):
        self.texts = list(texts)

    def get(self, url, **kw):
        return Resp(self.texts.pop(0))


# comme le vrai fichier : la semaine commence le dimanche (04/10) et finit le vendredi (09/10)
FF = json.dumps([{"title": "OPEC-JMMC Meetings", "country": "All", "date": "2026-10-04T05:15:00-04:00",
                  "impact": "Medium", "forecast": "", "previous": ""},
                 {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-10-09T08:30:00-04:00",
                  "impact": "High", "forecast": "50K", "previous": "29K"}])
DENIED = "<html><body>Request Denied. You've exceeded the limit for Calendar Export requests.</body></html>"


def test_ff_rate_limit_page_uses_recent_cache_then_refuses_stale_cache(tmp_path):
    store = NewsStore(tmp_path / "c.db")
    cal = ForexFactoryCalendar(store, min_interval_minutes=60, max_stale_hours=26)
    assert len(cal.fetch(Session(FF), NOW)) == 2 and cal.data_time == NOW
    later = NOW + timedelta(hours=5)
    assert len(cal.fetch(Session(DENIED), later)) == 2 and cal.data_time == NOW  # copie de 5 h : acceptée
    with pytest.raises(RuntimeError):
        cal.fetch(Session(DENIED), NOW + timedelta(hours=30))  # copie de 30 h : refusée


def test_ff_calendar_of_a_past_week_is_refused(tmp_path):
    saturday = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    assert ForexFactoryCalendar(NewsStore(tmp_path / "a.db")).fetch(Session(FF), saturday)
    monday = datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)  # fichier de la semaine passée
    with pytest.raises(RuntimeError):
        ForexFactoryCalendar(NewsStore(tmp_path / "b.db")).fetch(Session(FF), monday)


def test_calendar_ok_requires_current_week_coverage(tmp_path):
    old_week = [CalendarEvent("CPI m/m", "USD", NOW - timedelta(days=9), "High"),
                CalendarEvent("NFP", "USD", NOW - timedelta(days=4), "High")]
    svc = make(tmp_path, [], None, events=old_week)
    svc.refresh()
    assert not svc.calendar_ok()  # téléchargé à l'instant, mais c'est la semaine d'avant


# ---------------------------------------------------------------- alertes
def test_no_second_alert_when_more_reliable_copy_arrives_later(tmp_path):
    fj = item("Japan intervenes in FX market to support the yen", "FinancialJuice", tier=2, group="FJ")
    fx = item("Japan intervenes in FX market to support the yen", "FXStreet", tier=2, group="FX")
    src = Source("s", [fj, fx])
    an = Analyzer({fj.title: verdict("high", "high", (("JPY", "up"),))})
    svc = make(tmp_path, [src], an)
    svc.refresh()
    assert len(alerts(svc)) == 1
    reuters = item("Japan intervenes in FX market to support the yen", "Reuters", tier=1, group="Reuters")
    src.items.append(reuters)
    svc._last_fetch.clear()
    svc.refresh()
    assert len(alerts(svc)) == 1  # même événement : pas de deuxième alerte


def test_merged_items_are_remembered_no_duplicate_alert_or_ai_call(tmp_path):
    a = item("Fed cuts rates by 50 basis points", "Reuters", group="Reuters")
    b = item("Federal Reserve slashes policy rate in surprise move", "CNBC", group="CNBC")
    an = Analyzer({a.title: verdict(), b.title: verdict(same=a.id)})
    svc = make(tmp_path, [Source("s", [a, b])], an)
    svc.refresh()
    svc._last_fetch.clear()
    svc.refresh()
    assert an.calls == 1 and len(alerts(svc)) == 1
    assert len(svc._items) == 1 and svc._items[0].corroborations == 2


def test_merge_never_drops_reliable_item_into_rumor(tmp_path):
    reuters = item("Fed cuts rates by 50 basis points", "Reuters", group="Reuters")
    post = item("FED CUT 50!!! told you", "X @anon", SOCIAL, 3, group="X @anon")
    an = Analyzer({reuters.title: verdict(same=post.id)})
    svc = make(tmp_path, [Source("s", [reuters])], an)
    svc._raw["x"] = [post]
    svc.refresh()
    assert any(i.source == "Reuters" for i in svc._items)


def test_merge_keeps_important_item_when_target_judged_irrelevant(tmp_path):
    recap = item("Stocks and dollar after the Fed", "Reuters", group="Reuters", minutes_ago=6)
    news = item("Fed unexpectedly cuts by 50bp", "FXStreet", tier=2, group="FX")
    an = Analyzer({recap.title: verdict("none", relevant=False), news.title: verdict(same=recap.id)})
    svc = make(tmp_path, [Source("s", [recap, news])], an)
    useful = svc.refresh()
    assert [i.title for i in useful] == [news.title] and len(alerts(svc)) == 1


def test_ai_rumor_is_not_alerted(tmp_path):
    a = item("BoJ reportedly preparing to intervene, sources say", "Bloomberg", group="Bloomberg")
    svc = make(tmp_path, [Source("s", [a])], Analyzer({a.title: verdict("high", "medium", rumor=True)}))
    svc.refresh()
    assert alerts(svc) == []
    assert svc.context("USDJPY").sentiment is None  # une rumeur ne pèse pas sur le sentiment


def test_alert_retried_when_telegram_fails(tmp_path):
    a = item("Fed unexpectedly cuts rates")
    notifier = Notifier(ok=False)
    svc = make(tmp_path, [Source("s", [a])], None, notifier=notifier)
    svc.refresh()
    notifier.ok = True
    svc.refresh()
    assert len(alerts(svc)) == 2  # premier envoi raté, deuxième réussi
    svc.refresh()
    assert len(alerts(svc)) == 2


# ---------------------------------------------------------------- rappels et briefing
def test_reminders_survive_restart_and_mention_blackout_only_if_enabled(tmp_path):
    cpi = CalendarEvent("CPI m/m", "USD", NOW + timedelta(minutes=10), "High")
    store = NewsStore(tmp_path / "n.db")
    svc = make(tmp_path, [], None, events=[cpi], store=store, blackout=BlackoutSettings(enabled=False))
    svc.refresh()
    svc.send_event_reminders(NOW)
    assert len(svc.notifier.sent) == 1 and "Pas de nouvelle entrée" not in svc.notifier.sent[0]
    restarted = make(tmp_path, [], None, events=[cpi], store=store)
    restarted.refresh()
    restarted.send_event_reminders(NOW)
    assert restarted.notifier.sent == []


def test_brief_built_once_and_retried_until_delivered(tmp_path):
    notifier = Notifier(ok=False)
    svc = make(tmp_path, [Source("s", [item("Fed unexpectedly cuts rates")])], None, notifier=notifier,
               morning_brief_time="07:30")
    calls = {"n": 0}
    original = svc.brief_text

    def counting(now):
        calls["n"] += 1
        return original(now)

    svc.brief_text = counting
    svc.refresh()
    svc.maybe_send_brief(NOW)  # 08:00 Paris, échec d'envoi
    notifier.ok = True
    svc.maybe_send_brief(NOW)
    svc.maybe_send_brief(NOW)
    assert calls["n"] == 1
    assert sum("BRIEFING" in m for m in notifier.sent) == 2  # 1 raté + 1 réussi, puis plus rien


def test_brief_request_served_by_background_tick(tmp_path):
    svc = make(tmp_path, [], None)
    svc.request_brief()
    svc.tick()
    assert any("BRIEFING" in m for m in svc.notifier.sent)


def test_unrecognized_symbols_are_reported(tmp_path):
    svc = NewsService(NewsSettings(morning_brief_time=""), ["EURUSD", "WEIRD123"], Notifier(),
                      NewsStore(tmp_path / "n.db"), [], None, None, None, lambda: NOW)
    assert svc.unrecognized == ["WEIRD123"]
    assert "WEIRD123" in svc.status_line() and not svc.context("WEIRD123").recognized
