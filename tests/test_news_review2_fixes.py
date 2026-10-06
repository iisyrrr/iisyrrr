"""Non-régression des défauts trouvés par la deuxième revue (corrections de la v2)."""
from datetime import datetime, timedelta, timezone

from bot.news.assets import symbol_direction_sign, symbol_profile
from bot.news.filters import FilterSettings, categorize, deduplicate, run_filters
from bot.news.models import NEWS, SOCIAL, NewsItem
from bot.news.sources import RssSource
from tests.test_news_review_fixes import NOW, Analyzer, Notifier, Source, alerts, item, make, verdict
from tests.test_news_v2 import FakeResponse, FakeSession


def test_confirmation_survives_merge_into_earlier_rumor(tmp_path):
    rumor = item("Japan said to intervene in yen market, sources say", "Bloomberg", group="Bloomberg", minutes_ago=6)
    confirmed = item("Japan intervenes in currency market, finance ministry confirms", "Reuters", group="Reuters",
                     minutes_ago=3)
    an = Analyzer({rumor.title: verdict("high", "medium", (("JPY", "up"),), rumor=True),
                   confirmed.title: verdict("high", "high", (("JPY", "up"),), same=rumor.id)})
    svc = make(tmp_path, [Source("s", [rumor, confirmed])], an)
    useful = svc.refresh()
    assert [i.source for i in useful] == ["Reuters"] and not useful[0].is_rumor
    assert len(alerts(svc)) == 1
    assert svc.context("USDJPY").blackout_event is not None  # 15 min sans entrée après l'intervention


def test_same_event_recognised_across_ai_batches(tmp_path):
    a = item("Fed cuts rates by 50 basis points", "Reuters", group="Reuters", minutes_ago=4)
    src = Source("s", [a])
    an = Analyzer({a.title: verdict()})
    svc = make(tmp_path, [src], an)
    svc.refresh()
    b = item("Federal Reserve slashes policy rate in surprise move", "CNBC", group="CNBC", minutes_ago=1)
    an.verdicts[b.title] = verdict(same=a.id)
    seen_known = {}
    original = an.analyze

    def spy(items, assets, known=None):
        seen_known["ids"] = [k.id for k in known or []]
        return original(items, assets, known)

    an.analyze = spy
    src.items.append(b)
    svc._last_fetch.clear()
    useful = svc.refresh()
    assert a.id in seen_known["ids"]  # l'IA voit l'info déjà connue
    assert len(useful) == 1 and len(alerts(svc)) == 1


def test_startup_floor_applies_to_every_cycle(tmp_path):
    old = item("Fed unexpectedly cuts rates", minutes_ago=40)
    svc = make(tmp_path, [Source("s", [old])], None)
    svc.refresh()
    svc._last_fetch.clear()
    svc.refresh()
    assert alerts(svc) == []


def _keys(titles):
    return deduplicate([item(t, f"S{k}", group=f"S{k}") for k, t in enumerate(titles)], 0.55)


def test_different_releases_with_same_number_are_not_merged():
    assert len(_keys(["US CPI MoM Actual 0.3% (Forecast 0.3%, Previous 0.4%)",
                      "US Core CPI MoM Actual 0.3% (Forecast 0.2%, Previous 0.3%)"])) == 2
    assert len(_keys(["US CPI m/m Actual 0.3%", "US CPI y/y Actual 0.3%"])) == 2
    assert len(_keys(["Germany manufacturing PMI Actual 49.1", "Eurozone manufacturing PMI Actual 49.1"])) == 2
    assert len(_keys(["US CPI MoM Actual 0.3% (Forecast 0.3%)", "US CPI MoM 0.3% vs 0.3% expected"])) == 1


def test_social_copies_cannot_lift_single_source_over_alert_bar():
    fj = item("Japan intervenes in FX market, yen surges", "FJ", tier=2, group="FJ")
    posts = [NewsItem(f"X @{k}", SOCIAL, 3, "Japan intervenes in FX market, yen surges", f"https://x/{k}",
                      NOW - timedelta(minutes=2), engagement={"likes": 900}, group=f"X @{k}") for k in range(6)]
    out = deduplicate([fj] + posts, 0.55)
    assert len(out) == 1 and out[0].weight < 0.85 and out[0].corroborations == 7


def test_bank_notes_and_daily_recaps_are_categorised():
    assert categorize(item("Gold: Vulnerable as yields stay elevated – OCBC")) == "analysis"
    assert categorize(item("EUR/USD: Further downside likely - ING")) == "analysis"
    assert categorize(item("Forex Today: Dollar firm ahead of FOMC minutes")) == "recap"
    assert categorize(item("Fed's Waller: outlook for inflation remains uncertain")) == "news"


def test_symbol_profile_overrides_and_metal_quotes():
    assert symbol_profile("MYPAIR", {"MYPAIR": ["EUR", "USD"]}) == {"EUR": 1, "USD": -1}
    assert symbol_profile("GOLDEUR") == {"XAU": 1, "EUR": -1}
    assert symbol_direction_sign("GOLD.r", "USD") == -1


def test_conditional_get_refilters_future_dated_entries_later():
    future = (NOW + timedelta(minutes=20)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    rss = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Scheduled statement</title><link>https://x.com/a</link><pubDate>{future}</pubDate></item>
</channel></rss>""".encode()
    session = FakeSession([FakeResponse(200, rss, {"ETag": '"v1"'}), FakeResponse(304)])
    src = RssSource("t", "https://x.com/feed")
    assert src.fetch(session, NOW) == []
    assert [i.title for i in src.fetch(session, NOW + timedelta(minutes=30))] == ["Scheduled statement"]


def test_status_says_calendar_loading_before_first_attempt(tmp_path):
    svc = make(tmp_path, [], None)
    assert "en cours de chargement" in svc.status_line()


def test_ai_failures_are_reported_once(tmp_path):
    a = item("Fed unexpectedly cuts rates")
    an = Analyzer(error=RuntimeError("boom"))
    svc = make(tmp_path, [Source("s", [a])], an)
    for _ in range(5):
        svc.store.db.execute("DELETE FROM items")
        svc._last_fetch.clear()
        svc.refresh()
    warnings = [m for m in svc.notifier.sent if "Analyse IA en échec" in m]
    assert len(warnings) == 1 and "IA en échec" in svc.status_line()
