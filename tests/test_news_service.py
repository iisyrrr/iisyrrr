from datetime import datetime, timedelta, timezone

import pytest

from bot.news.calendar import BlackoutSettings
from bot.news.models import NEWS, OFFICIAL, SOCIAL, CalendarEvent, NewsItem
from bot.news.service import NewsService, NewsSettings, symbol_sentiment
from bot.news.store import NewsStore

NOW = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)  # 08:00 à Paris


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


class FakeSource:
    def __init__(self, name, items, fail=False):
        self.name, self.items, self.fail = name, items, fail

    def fetch(self, session, now):
        if self.fail:
            raise ConnectionError("down")
        return list(self.items)


class FakeAnalyzer:
    def __init__(self, verdicts):
        self.verdicts = verdicts  # titre -> analyse
        self.calls = 0
        self.briefs = 0

    def analyze(self, items, assets):
        self.calls += 1
        out = {}
        for i in items:
            v = self.verdicts.get(i.title)
            if v:
                out[i.id] = {"id": i.id, **v}
        return out

    def brief(self, *a, **k):
        self.briefs += 1
        return None


def news(title, source="Wire", kind=NEWS, tier=1, minutes_ago=5, engagement=None):
    return NewsItem(source, kind, tier, title, f"https://{source}/{abs(hash(title))}",
                    NOW - timedelta(minutes=minutes_ago), engagement=engagement or {})


def verdict(impact="high", cred="high", moves=(("USD", "down"),), relevant=True, rumor=False):
    return {"relevant": relevant, "impact": impact, "credibility": cred, "is_rumor": rumor,
            "asset_moves": [{"asset": a, "direction": d} for a, d in moves], "summary_fr": "Résumé.",
            "same_event_as": ""}


def make(tmp_path, sources, analyzer=None, events=None, clock=None, **settings):
    cal = FakeSource("cal", events or [])
    svc = NewsService(NewsSettings(**settings), ["EURUSD", "XAUUSD"], FakeNotifier(),
                      NewsStore(tmp_path / "news.db"), sources, cal, analyzer, None, clock or Clock(NOW))
    return svc


def test_alert_sent_once_for_high_impact(tmp_path):
    item = news("Fed cuts rates by 50bp in surprise move")
    an = FakeAnalyzer({item.title: verdict()})
    svc = make(tmp_path, [FakeSource("s", [item])], an, morning_brief_time="")
    svc.refresh()
    svc.refresh()
    alerts = [m for m in svc.notifier.sent if "IMPACT FORT" in m]
    assert len(alerts) == 1 and "USD⬇" in alerts[0]


def test_analysis_is_cached_between_cycles(tmp_path):
    item = news("ECB's Lagarde hints at December cut")
    an = FakeAnalyzer({item.title: verdict("medium", "high", (("EUR", "down"),))})
    svc = make(tmp_path, [FakeSource("s", [item])], an)
    svc.refresh()
    svc.refresh()
    assert an.calls == 1  # pas de double facturation de l'IA


def test_irrelevant_items_hidden_and_rumors_not_alerted(tmp_path):
    noise = news("Markets wrap: stocks mixed in quiet session")
    rumor = news("Unconfirmed: BoJ to intervene tonight", "X @anon", SOCIAL, 3, engagement={"likes": 5000})
    an = FakeAnalyzer({noise.title: verdict("none", relevant=False),
                       rumor.title: verdict("high", "low", (("JPY", "up"),), rumor=True)})
    svc = make(tmp_path, [FakeSource("s", [noise, rumor])], an)
    useful = svc.refresh()
    assert noise.title not in [i.title for i in useful]
    assert not any("IMPACT FORT" in m for m in svc.notifier.sent)


def test_no_alert_for_old_news_after_restart(tmp_path):
    old = news("Fed cuts rates by 50bp in surprise move", minutes_ago=300)
    svc = make(tmp_path, [FakeSource("s", [old])], FakeAnalyzer({old.title: verdict()}))
    svc.refresh()
    assert not any("IMPACT FORT" in m for m in svc.notifier.sent)


def test_without_ai_only_tier1_with_impact_words_alert(tmp_path):
    a = news("Federal Reserve Board announces approval of application by Some Bank", "Fed", OFFICIAL, 1)
    b = news("Fed unexpectedly cuts rates", "Fed", OFFICIAL, 1)
    svc = make(tmp_path, [FakeSource("s", [a, b])], None)
    svc.refresh()
    text = "\n".join(svc.notifier.sent)
    assert "unexpectedly cuts" in text and "approval of application" not in text


def test_alert_rate_limit(tmp_path):
    titles = ["Fed emergency cut", "ECB surprise hike", "BoJ intervenes on yen", "SNB scraps floor",
              "US CPI shock", "Gold record high", "OPEC slashes output", "UK gilt crash"]
    items = [news(t) for t in titles]
    an = FakeAnalyzer({i.title: verdict() for i in items})
    svc = make(tmp_path, [FakeSource("s", items)], an, max_alerts_per_hour=3)
    svc.refresh()
    assert sum(m.count("IMPACT FORT") for m in svc.notifier.sent) == 3


def test_failing_source_does_not_break_cycle(tmp_path):
    ok = news("Fed unexpectedly cuts rates")
    svc = make(tmp_path, [FakeSource("down", [], fail=True), FakeSource("up", [ok])], None)
    useful = svc.refresh()
    assert useful and svc.source_status["down"].startswith("erreur")


def test_symbol_sentiment_directions():
    usd_down = news("Fed cuts")
    usd_down.analysis = verdict("high", "high", (("USD", "down"),))
    assert symbol_sentiment([usd_down], "EURUSD", NOW, 8)[0] > 0.5   # USD faible -> EURUSD monte
    assert symbol_sentiment([usd_down], "USDJPY", NOW, 8)[0] < -0.5  # USD faible -> USDJPY baisse
    assert symbol_sentiment([usd_down], "GER40", NOW, 8) == (None, 0)


def test_context_blackout_and_next_event(tmp_path):
    nfp = CalendarEvent("Non-Farm Employment Change", "USD", NOW + timedelta(minutes=20), "High")
    svc = make(tmp_path, [], None, events=[nfp])
    svc.refresh()
    ctx = svc.context("EURUSD")
    assert ctx.blackout_event is nfp and ctx.next_event is nfp
    assert svc.context("EURUSD", NOW - timedelta(hours=3)).blackout_event is None


def test_event_reminder_sent_once(tmp_path):
    cpi = CalendarEvent("CPI m/m", "USD", NOW + timedelta(minutes=10), "High")
    svc = make(tmp_path, [], None, events=[cpi], morning_brief_time="")
    svc.tick()
    svc.tick()
    assert sum("⏰" in m for m in svc.notifier.sent) == 1


def test_morning_brief_once_per_day_and_not_before_time(tmp_path):
    clock = Clock(NOW - timedelta(hours=1))  # 07:00 Paris
    svc = make(tmp_path, [FakeSource("s", [news("Fed unexpectedly cuts rates")])], None, clock=clock,
               morning_brief_time="07:30")
    svc.tick()
    assert not any("BRIEFING" in m for m in svc.notifier.sent)
    clock.t = NOW  # 08:00 Paris
    svc.tick()
    clock.t = NOW + timedelta(minutes=30)
    svc.tick()
    assert sum("BRIEFING" in m for m in svc.notifier.sent) == 1


def test_calendar_kept_when_source_temporarily_down(tmp_path):
    ev = CalendarEvent("CPI m/m", "USD", NOW + timedelta(hours=5), "High")
    svc = make(tmp_path, [], None, events=[ev])
    svc.refresh()
    svc.calendar_source.fail = True
    svc.refresh()
    assert svc.context("EURUSD").next_event == ev


def test_ai_merges_same_event_written_differently(tmp_path):
    a = news("German Factory Orders Tumbled in August", "WSJ", minutes_ago=10)
    b = news("German industrial orders fall sharply over large contracts", "Reuters", minutes_ago=12)
    v = verdict("medium", "high", (("EUR", "down"),))
    an = FakeAnalyzer({a.title: {**v, "same_event_as": b.id}, b.title: {**v, "same_event_as": ""}})
    svc = make(tmp_path, [FakeSource("s", [a, b])], an)
    useful = svc.refresh()
    assert [i.title for i in useful] == [b.title]
    assert useful[0].corroborations == 2


def test_same_event_cycle_does_not_loop(tmp_path):
    a = news("Fed holds rates", "A")
    b = news("Federal Reserve leaves policy unchanged", "B")
    v = verdict()
    an = FakeAnalyzer({a.title: {**v, "same_event_as": b.id}, b.title: {**v, "same_event_as": a.id}})
    svc = make(tmp_path, [FakeSource("s", [a, b])], an)
    useful = svc.refresh()
    assert len(useful) >= 1
