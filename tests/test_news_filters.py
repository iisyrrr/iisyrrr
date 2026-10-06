from datetime import datetime, timedelta, timezone

from bot.news.assets import detect_assets, symbol_assets, symbol_direction_sign
from bot.news.calendar import BlackoutSettings, blackout_event, upcoming
from bot.news.filters import FilterSettings, deduplicate, is_spam, run_filters
from bot.news.models import NEWS, OFFICIAL, SOCIAL, CalendarEvent, NewsItem

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def item(title, source="Src", kind=NEWS, tier=2, minutes_ago=10, url=None, summary="", engagement=None):
    return NewsItem(source, kind, tier, title, url or f"https://{source}/{abs(hash(title))}",
                    NOW - timedelta(minutes=minutes_ago), summary, engagement or {})


def test_symbol_assets():
    assert symbol_assets("EURUSD") == {"EUR", "USD"}
    assert symbol_assets("EURUSD.r") == {"EUR", "USD"}
    assert symbol_assets("XAUUSDm") == {"XAU", "USD"}
    assert symbol_assets("GER40") == {"EU_INDICES", "EUR"}
    assert symbol_assets("NAS100") == {"US_INDICES", "USD"}
    assert symbol_assets("FOO", {"FOO": ["XAU"]}) == {"XAU"}


def test_direction_sign():
    assert symbol_direction_sign("EURUSD", "EUR") == 1
    assert symbol_direction_sign("EURUSD", "USD") == -1
    assert symbol_direction_sign("USDJPY", "JPY") == -1
    assert symbol_direction_sign("XAUUSD", "XAU") == 1
    assert symbol_direction_sign("XAUUSD", "USD") == -1
    assert symbol_direction_sign("GER40", "EU_INDICES") == 1
    assert symbol_direction_sign("GER40", "EUR") == 0
    assert symbol_direction_sign("EURUSD", "XAU") == 0


def test_detect_assets_case_rules():
    assert {"USD"} <= detect_assets("Fed's Powell signals patience")
    assert "USD" not in detect_assets("tell us what you think")  # « us » minuscule ≠ US
    assert {"XAU"} <= detect_assets("Gold hits record high")
    assert {"EUR"} <= detect_assets("ECB's Lagarde: inflation risks balanced")


def test_dedup_counts_corroborations_and_keeps_best_tier():
    a = item("Fed cuts rates by 50 basis points in surprise move", "Blog", tier=3, minutes_ago=5)
    b = item("Fed cuts rates by 50 basis points in a surprise move", "Wire", tier=1, minutes_ago=4)
    c = item("ECB holds rates steady", "Wire", tier=1)
    out = deduplicate([a, b, c], 0.55)
    assert len(out) == 2
    fed = next(i for i in out if "Fed" in i.title)
    assert fed.source == "Wire" and fed.corroborations == 2


def test_spam_detection():
    assert is_spam(item("Join my free signals group t.me/xyz 100x gains", kind=SOCIAL, tier=3))
    assert is_spam(item("EURUSD IS GOING TO EXPLODE TODAY BUY NOW EVERYONE", kind=SOCIAL, tier=3))
    assert not is_spam(item("EURUSD testing 1.10 support ahead of CPI", kind=SOCIAL, tier=3))


def test_run_filters_quality_rules():
    s = FilterSettings(traded_assets={"EUR", "USD"})
    items = [
        item("Fed's Powell says rate cuts are not imminent", "Fed", OFFICIAL, 1),
        item("Old news about the ECB", minutes_ago=60 * 30),  # trop vieux
        item("Bitcoin pumps again", kind=NEWS),  # hors actifs tradés
        item("Dollar slides as US yields drop", kind=SOCIAL, tier=3, engagement={"likes": 1}),  # pas d'engagement
        item("Dollar slides after soft US CPI print", kind=SOCIAL, tier=3, engagement={"likes": 40}),
    ]
    out = run_filters(items, NOW, s)
    titles = [i.title for i in out]
    assert titles[0].startswith("Fed's Powell")  # source officielle en tête
    assert "Old news about the ECB" not in titles
    assert "Bitcoin pumps again" not in titles
    assert "Dollar slides as US yields drop" not in titles
    social = next(i for i in out if i.kind == SOCIAL)
    assert social.is_rumor and "rumeur non confirmée" in social.flags


def test_social_rumor_confirmed_by_media_is_not_rumor():
    s = FilterSettings(traded_assets={"JPY"})
    out = run_filters([
        item("Japan intervenes in FX market to support the yen", "X", SOCIAL, 3, 6, engagement={"likes": 900}),
        item("Japan intervenes in FX market to support yen", "Wire", NEWS, 1, 3),
    ], NOW, s)
    assert len(out) == 1 and not out[0].is_rumor and out[0].corroborations == 2


def ev(title, cur="USD", minutes=20, impact="High"):
    return CalendarEvent(title, cur, NOW + timedelta(minutes=minutes), impact)


def test_blackout_window():
    s = BlackoutSettings(minutes_before=30, minutes_after=30, major_minutes_before=45, major_minutes_after=60)
    assert blackout_event([ev("Retail Sales", minutes=20)], {"EUR", "USD"}, NOW, s) is not None
    assert blackout_event([ev("Retail Sales", minutes=40)], {"EUR", "USD"}, NOW, s) is None
    assert blackout_event([ev("Non-Farm Employment Change", minutes=40)], {"EUR", "USD"}, NOW, s) is not None
    assert blackout_event([ev("CPI y/y", minutes=-50)], {"USD"}, NOW, s) is not None  # 60 min après
    assert blackout_event([ev("Retail Sales", "GBP", 10)], {"EUR", "USD"}, NOW, s) is None  # autre devise
    assert blackout_event([ev("Retail Sales", impact="Medium")], {"USD"}, NOW, s) is None
    assert blackout_event([ev("Retail Sales")], {"USD"}, NOW, BlackoutSettings(enabled=False)) is None


def test_upcoming_sorted_and_filtered():
    evs = [ev("B", minutes=120), ev("A", minutes=60), ev("Low one", impact="Low"), ev("Past", minutes=-5)]
    assert [e.title for e in upcoming(evs, {"USD"}, NOW, hours=24)] == ["A", "B"]


def test_direction_sign_unknown_base_and_named_metals():
    assert symbol_direction_sign("ZARJPY", "JPY") == -1  # JPY fort -> ZARJPY baisse
    assert symbol_direction_sign("GOLD", "USD") == -1
    assert symbol_direction_sign("GOLD", "XAU") == 1
    assert symbol_direction_sign("SILVER.r", "XAG") == 1
    assert symbol_assets("US2000") == {"US_INDICES", "USD"}
    assert symbol_direction_sign("MYGOLD", "USD", {"MYGOLD": ["XAU", "USD"]}) == -1
    assert symbol_direction_sign("MYIDX", "USD", {"MYIDX": ["US_INDICES", "USD"]}) == 0
    assert symbol_direction_sign("X", "USD", {"X": {"XAU": 1, "USD": -1}}) == -1


def test_metals_and_oil_are_not_currencies():
    from bot.news.calendar import relevant_currencies
    assert relevant_currencies({"XAU", "USD"}, BlackoutSettings()) == {"USD"}
    assert relevant_currencies({"OIL"}, BlackoutSettings()) == set()
