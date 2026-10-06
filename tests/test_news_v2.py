"""Tests des règles de qualité v2 : groupes propriétaires, fiabilité combinée,
nature de l'info, promotion interdite des rumeurs, collecte rapide, sécurité XML."""
from datetime import datetime, timedelta, timezone

import pytest

from bot.news import service as service_mod
from bot.news.calendar import BlackoutSettings, blackout_event
from bot.news.filters import FilterSettings, deduplicate, run_filters
from bot.news.models import NEWS, OFFICIAL, SOCIAL, CalendarEvent, NewsItem
from bot.news.service import NewsService, NewsSettings
from bot.news.sources import RssSource, SitemapNewsSource, clean_url, valid_time
from bot.news.store import NewsStore

NOW = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)


def item(title, source="Src", kind=NEWS, tier=2, minutes_ago=5, group="", engagement=None, url=None):
    return NewsItem(source, kind, tier, title, url or f"https://{source.replace(' ', '')}/{abs(hash(title))}",
                    NOW - timedelta(minutes=minutes_ago), engagement=engagement or {}, group=group)


def test_same_owner_does_not_corroborate_itself():
    a = item("Fed holds rates steady at 4.25%", "Reuters", tier=1, group="Reuters")
    b = item("Fed holds rates steady at 4.25%", "Reuters (Bluesky)", tier=1, group="Reuters")
    out = deduplicate([a, b], 0.55)
    assert len(out) == 1 and out[0].corroborations == 1 and out[0].weight == pytest.approx(0.9)


def test_noisy_or_weight_of_independent_sources():
    a = item("German factory orders plunge 10.6% in August", "FinancialJuice", group="FinancialJuice")
    b = item("German factory orders plunge 10.6% in August", "FXStreet", group="FXStreet")
    out = deduplicate([a, b], 0.55)
    assert out[0].corroborations == 2 and out[0].weight == pytest.approx(1 - 0.25 * 0.25)


def test_different_numbers_are_different_stories():
    a = item("German factory orders fall 10.6% in August", "A")
    b = item("German factory orders fall 5.2% in August", "B")
    assert len(deduplicate([a, b], 0.55)) == 2


def test_social_never_counts_as_confirmation_of_social():
    a = item("BoJ to intervene in yen tonight", "X @a", SOCIAL, 3, group="X @a", engagement={"likes": 900})
    b = item("BoJ to intervene in yen tonight", "X @b", SOCIAL, 3, group="X @b", engagement={"likes": 900})
    out = run_filters([a, b], NOW, FilterSettings(traded_assets={"JPY"}))
    assert out[0].social_only and out[0].corroborations == 2


def test_categories_reduce_score():
    s = FilterSettings(traded_assets={"EUR", "USD"})
    news = item("ECB raises rates unexpectedly", "Wire", OFFICIAL, 1)
    analysis = item("EUR/USD Price Forecast: bears target 1.05 after ECB", "Wire2", NEWS, 1)
    recap = item("What are the main events for today? ECB and Fed speakers", "Wire3", NEWS, 1)
    out = {i.title: i for i in run_filters([news, analysis, recap], NOW, s)}
    assert out[news.title].category == "news"
    assert out[analysis.title].category == "analysis"
    assert out[recap.title].category == "recap"
    assert out[news.title].score > 2 * out[analysis.title].score


def test_rumor_wording_lowers_score_until_tier1_confirms():
    s = FilterSettings(traded_assets={"JPY"})
    plain = run_filters([item("Japan intervenes to support the yen", "FJ", tier=2)], NOW, s)[0]
    rumor = run_filters([item("Japan reportedly intervenes to support the yen", "FJ", tier=2)], NOW, s)[0]
    assert rumor.score < plain.score


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


class FakeSource:
    def __init__(self, name, items, poll_seconds=None):
        self.name, self.items, self.poll_seconds, self.calls = name, items, poll_seconds, 0

    def fetch(self, session, now):
        self.calls += 1
        return list(self.items)


class FakeAnalyzer:
    def __init__(self, verdicts):
        self.verdicts = verdicts

    def analyze(self, items, assets, known=None):
        return {i.id: {"id": i.id, **self.verdicts[i.title]} for i in items if i.title in self.verdicts}


def verdict(impact="high", cred="high", moves=(("USD", "down"),)):
    return {"relevant": True, "impact": impact, "credibility": cred, "is_rumor": False,
            "asset_moves": [{"asset": a, "direction": d} for a, d in moves], "summary_fr": "Résumé.",
            "same_event_as": ""}


def make(tmp_path, sources, analyzer=None, **kw):
    return NewsService(NewsSettings(morning_brief_time="", **kw), ["EURUSD", "USDJPY"], FakeNotifier(),
                       NewsStore(tmp_path / "n.db"), sources, None, analyzer, None, lambda: NOW)


def test_ai_cannot_promote_social_rumor_to_alert_or_sentiment(tmp_path):
    post = item("Fed emergency cut tonight", "X @anon", SOCIAL, 3, group="X @anon", engagement={"likes": 5000})
    svc = make(tmp_path, [FakeSource("s", [post])], FakeAnalyzer({post.title: verdict()}))
    svc.refresh()
    assert not any("IMPACT FORT" in m for m in svc.notifier.sent)
    assert svc.context("EURUSD").sentiment is None


def test_analysis_items_never_alert(tmp_path):
    a = item("EUR/USD Price Forecast: Fed cut fuels rally", "Wire", tier=1)
    svc = make(tmp_path, [FakeSource("s", [a])], FakeAnalyzer({a.title: verdict()}))
    svc.refresh()
    assert not any("IMPACT FORT" in m for m in svc.notifier.sent)


def test_single_tier2_source_does_not_alert_without_ai(tmp_path):
    a = item("Japan intervenes in FX market, yen surges", "FJ", tier=2, group="FJ")
    svc = make(tmp_path, [FakeSource("s", [a])])
    svc.refresh()
    assert svc.notifier.sent == []
    b = item("Japan intervenes in FX market, yen surges", "FXStreet", tier=2, group="FXStreet")
    svc2 = make(tmp_path / "2", [FakeSource("s", [a, b])])
    svc2.refresh()
    assert svc2.notifier.sent  # deux groupes indépendants -> fiabilité 0.94


def test_breaking_news_blocks_entries_for_a_few_minutes(tmp_path):
    a = item("Japan intervenes in FX market to support the yen", "Reuters", tier=1, minutes_ago=3)
    an = FakeAnalyzer({a.title: verdict("high", "high", (("JPY", "up"),))})
    svc = make(tmp_path, [FakeSource("s", [a])], an)
    svc.refresh()
    ev = svc.context("USDJPY").blackout_event
    assert ev is not None and "urgente" in ev.title
    assert svc.context("USDJPY", NOW + timedelta(minutes=20)).blackout_event is None


def test_fast_sources_polled_more_often(tmp_path, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(service_mod.time, "monotonic", lambda: clock["t"])
    fast = FakeSource("fast", [], poll_seconds=60)
    slow = FakeSource("slow", [], poll_seconds=None)  # poll_minutes = 5
    svc = make(tmp_path, [fast, slow])
    svc.tick()
    for _ in range(4):
        clock["t"] += 61
        svc.tick()
    assert fast.calls == 5 and slow.calls == 1


def test_cached_source_items_are_not_mutated_by_filters(tmp_path):
    a = item("Fed unexpectedly cuts rates", "Fed", OFFICIAL, 1)
    src = FakeSource("s", [a])
    svc = make(tmp_path, [src])
    svc.refresh()
    svc.refresh()
    assert a.score == 0.0 and a.flags == []


def test_press_conference_window_covers_the_whole_conference():
    s = BlackoutSettings()
    pc = CalendarEvent("FOMC Press Conference", "USD", NOW, "High")
    assert blackout_event([pc], {"USD"}, NOW + timedelta(minutes=85), s) is pc


def test_clean_url_and_valid_time():
    assert clean_url("http://www.ecb.europa.eu//press/pr/x.html?utm_source=rss&id=3") == \
        "https://www.ecb.europa.eu/press/pr/x.html?id=3"
    assert not valid_time(datetime(1899, 12, 30, tzinfo=timezone.utc), NOW)
    assert not valid_time(NOW + timedelta(days=30), NOW)
    assert valid_time(NOW - timedelta(hours=1), NOW)


class FakeResponse:
    def __init__(self, status, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}
        self.text = content.decode("utf-8", "ignore")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def get(self, url, headers=None, timeout=None, **kw):
        self.requests.append(headers or {})
        return self.responses.pop(0)


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Fed cuts rates</title><link>http://x.com/a?utm_medium=rss</link>
<pubDate>Tue, 06 Oct 2026 05:50:00 GMT</pubDate></item>
<item><title>Old testimony</title><link>http://x.com/b</link><pubDate>Sat, 30 Dec 1899 00:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_rss_conditional_get_and_date_sanity():
    session = FakeSession([FakeResponse(200, RSS, {"ETag": '"v1"'}), FakeResponse(304)])
    src = RssSource("t", "https://x.com/feed")
    first = src.fetch(session, NOW)
    assert [i.title for i in first] == ["Fed cuts rates"] and first[0].url == "https://x.com/a"
    second = src.fetch(session, NOW)
    assert session.requests[1].get("If-None-Match") == '"v1"'
    assert [i.title for i in second] == ["Fed cuts rates"]


def test_sitemap_rejects_entity_declarations():
    bomb = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><urlset></urlset>'
    with pytest.raises(ValueError):
        SitemapNewsSource("r", "https://x").fetch(FakeSession([FakeResponse(200, bomb)]), NOW)
