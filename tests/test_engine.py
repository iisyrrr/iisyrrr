from datetime import datetime, timezone

import pandas as pd
import pytest

from bot.broker import Account, ClosedDeal, OrderResult, Position, SymbolSpec
from bot.config import DEFAULTS, deep_merge
from bot.engine import Bot
from bot.journal import Journal
from bot.state import StateStore
from bot.strategy import Signal, Strategy


class AlwaysBuy(Strategy):
    name = "always_buy"
    default_params = {"x": 1}

    def compute(self, df, p):
        return pd.DataFrame({"signal": 1, "sl_dist": 0.0020, "tp_dist": 0.0040}, index=df.index)


class FakeNotifier:
    def __init__(self):
        self.sent, self.inbox = [], []

    def send(self, text):
        self.sent.append(text)
        return True

    def poll_commands(self):
        cmds, self.inbox = self.inbox, []
        return cmds

    def drain(self):
        self.inbox = []


class FakeBroker:
    def __init__(self):
        self.bar = 0
        self.orders, self.positions_list, self.deals = [], [], []
        self.equity = 10_000.0

    def connect(self): pass
    def ensure_connected(self): pass
    def shutdown(self): pass

    def rates(self, symbol, tf, count):
        idx = pd.date_range("2026-01-01", periods=self.bar + 2, freq="15min", tz="UTC")[-count:]
        return pd.DataFrame({"open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1, "spread": 10}, index=idx)

    def spec(self, symbol):
        return SymbolSpec(symbol, 0.00001, 5, 0.01, 100, 0.01, 0, 10, 1.10000, 1.10010)

    def account(self):
        return Account(self.equity, self.equity, "EUR")

    def positions(self):
        return list(self.positions_list)

    def loss_per_lot(self, symbol, side, entry, sl):
        return abs(entry - sl) * 100_000  # 1 lot = 100 000 unités

    def market_order(self, symbol, side, volume, sl, tp, comment):
        self.orders.append((symbol, side, volume, sl, tp))
        self.positions_list.append(Position(len(self.orders), symbol, side, volume, 1.1001, sl, tp, 0.0))
        return OrderResult(True, ticket=len(self.orders), price=1.1001)

    def close_position(self, pos):
        self.positions_list.remove(pos)
        return OrderResult(True)

    def closed_deals(self, after_ticket):
        return [d for d in self.deals if d.ticket > after_ticket]


@pytest.fixture
def make_bot(tmp_path):
    def _make(mode="live", **risk):
        cfg = deep_merge(DEFAULTS, {"mode": mode, "trading": {"symbols": ["EURUSD"]},
                                    "risk": risk, "optimizer": {"enabled": False}})
        broker, notifier = FakeBroker(), FakeNotifier()
        bot = Bot(cfg, broker, notifier, Journal(tmp_path / "j.db"), StateStore(tmp_path),
                  AlwaysBuy(), clock=lambda: datetime(2026, 1, 5, 12, tzinfo=timezone.utc))
        bot.start()
        return bot, broker, notifier
    return _make


def new_bar(bot, broker):
    broker.bar += 1
    bot.tick()


def test_waits_for_next_bar_after_start(make_bot):
    bot, broker, _ = make_bot()
    bot.tick()  # première lecture : on mémorise la bougie, pas de trade
    assert broker.orders == []


def test_live_order_with_risk_based_lot_and_alert(make_bot):
    bot, broker, notifier = make_bot()
    bot.tick()
    new_bar(bot, broker)
    assert len(broker.orders) == 1
    symbol, side, volume, sl, tp = broker.orders[0]
    # 0.5 % de 10 000 = 50 € ; SL à 20 pips -> 200 €/lot -> 0.25 lot
    assert (symbol, side, volume) == ("EURUSD", 1, 0.25)
    assert sl < 1.1001 < tp
    assert any("POSITION OUVERTE" in m for m in notifier.sent)


def test_alert_only_never_sends_orders(make_bot):
    bot, broker, notifier = make_bot(mode="alert_only")
    bot.tick()
    new_bar(bot, broker)
    assert broker.orders == []
    assert any("SIGNAL" in m and "non exécuté" in m for m in notifier.sent)


def test_no_second_position_on_same_symbol(make_bot):
    bot, broker, _ = make_bot()
    bot.tick()
    new_bar(bot, broker)
    new_bar(bot, broker)
    assert len(broker.orders) == 1


def test_pause_and_resume_commands(make_bot):
    bot, broker, notifier = make_bot()
    bot.tick()
    notifier.inbox = ["/pause"]
    new_bar(bot, broker)
    assert broker.orders == [] and bot.state.paused
    notifier.inbox = ["/resume"]
    new_bar(bot, broker)
    assert len(broker.orders) == 1


def test_daily_loss_pauses_trading(make_bot):
    bot, broker, notifier = make_bot(max_daily_loss_pct=3.0)
    bot.tick()
    broker.equity = 9_600
    new_bar(bot, broker)
    assert broker.orders == [] and bot.state.paused
    assert any("Perte journalière" in m for m in notifier.sent)


def test_closed_trade_is_reported_once(make_bot):
    bot, broker, notifier = make_bot()
    broker.deals.append(ClosedDeal(7, 1, "EURUSD", 0.25, 1.1041, 98.5, "take-profit"))
    bot.tick()
    bot.tick()
    assert sum("Position fermée" in m for m in notifier.sent) == 1
    assert bot.journal.recent_profits(5) == [98.5]


def test_closeall_requires_confirmation(make_bot):
    bot, broker, notifier = make_bot()
    bot.tick()
    new_bar(bot, broker)
    notifier.inbox = ["/closeall"]
    bot.tick()
    assert len(broker.positions_list) == 1
    notifier.inbox = ["/closeall oui"]
    bot.tick()
    assert broker.positions_list == []


# ---------------------------------------------------------------- veille news
from datetime import timedelta, timezone as tz
from zoneinfo import ZoneInfo

from bot.news.models import CalendarEvent, NewsItem, SymbolContext


class FakeNews:
    def __init__(self, sentiment=None, blackout=None, calendar_ok=True, recognized=True):
        self.sentiment, self.blackout = sentiment, blackout
        self.calendar_ok, self.recognized = calendar_ok, recognized
        self.tz = ZoneInfo("Europe/Paris")
        self.brief_requested = False

    def context(self, symbol):
        item = NewsItem("Wire", "news", 1, "Fed signals pause", "u", datetime(2026, 1, 5, 11, tzinfo=timezone.utc))
        return SymbolContext(symbol, self.sentiment, 3 if self.sentiment is not None else 0, [item], None,
                             self.blackout, self.calendar_ok, self.recognized)

    def request_brief(self):
        self.brief_requested = True

    def start(self):
        pass

    def news_text(self):
        return "TOP NEWS"

    def calendar_text(self):
        return "AGENDA"

    def brief_text(self, now):
        return "BRIEF"

    def status_line(self):
        return "Veille news : test"


@pytest.fixture
def news_bot(make_bot):
    def _make(news, **news_cfg):
        bot, broker, notifier = make_bot()
        bot.news = news
        bot.cfg["news"].update(news_cfg)
        bot.tick()
        return bot, broker, notifier
    return _make


def test_no_entry_during_news_blackout(news_bot):
    nfp = CalendarEvent("Non-Farm Employment Change", "USD", datetime(2026, 1, 5, 12, 20, tzinfo=timezone.utc), "High")
    bot, broker, notifier = news_bot(FakeNews(blackout=nfp))
    new_bar(bot, broker)
    assert broker.orders == []
    assert any("Non-Farm" in m and "ignoré" in m for m in notifier.sent)


def test_sentiment_block_mode(news_bot):
    bot, broker, notifier = news_bot(FakeNews(sentiment=-0.8), sentiment_filter="block")
    new_bar(bot, broker)  # AlwaysBuy contre un sentiment très baissier
    assert broker.orders == []
    assert any("contraire au sentiment" in m for m in notifier.sent)


def test_sentiment_warn_mode_trades_with_warning_and_context(news_bot):
    bot, broker, notifier = news_bot(FakeNews(sentiment=-0.8), sentiment_filter="warn")
    new_bar(bot, broker)
    assert len(broker.orders) == 1
    opened = next(m for m in notifier.sent if "POSITION OUVERTE" in m)
    assert "CONTRE le sentiment" in opened and "Fed signals pause" in opened


def test_sentiment_recorded_in_journal(news_bot):
    bot, broker, _ = news_bot(FakeNews(sentiment=0.5))
    new_bar(bot, broker)
    row = bot.journal.db.execute("SELECT news_sentiment FROM signals").fetchone()
    assert row == (0.5,)


def test_auto_mode_learns_to_block_losing_counter_trend_trades(news_bot):
    bot, broker, notifier = news_bot(FakeNews(sentiment=-0.8), sentiment_filter="auto", auto_min_trades=5)
    assert bot.sentiment_mode() == "warn"
    for k in range(6):  # 6 trades perdants pris contre le sentiment, 6 gagnants dans le sens
        bot.journal.record_signal("2026-01-05T10:00:00", "EURUSD", 1, 0.1, 1, 0.9, 1.2, True, 100 + k, "r", {}, -0.8)
        bot.journal.record_close("t", ClosedDeal(1000 + k, 100 + k, "EURUSD", 0.1, 1, -50.0, "stop-loss"))
        bot.journal.record_signal("2026-01-05T10:00:00", "EURUSD", 1, 0.1, 1, 0.9, 1.2, True, 200 + k, "r", {}, 0.8)
        bot.journal.record_close("t", ClosedDeal(2000 + k, 200 + k, "EURUSD", 0.1, 1, 80.0, "take-profit"))
    assert bot.sentiment_mode() == "block"
    assert any("BLOQUE désormais" in m for m in notifier.sent)


def test_news_commands(news_bot):
    news = FakeNews()
    bot, _, notifier = news_bot(news)
    notifier.inbox = ["/news", "/calendar", "/brief"]
    bot.tick()
    assert {"TOP NEWS", "AGENDA"} <= set(notifier.sent)
    assert news.brief_requested  # préparé par la tâche de fond, pas par la boucle de trading


def test_failing_command_does_not_drop_following_ones(news_bot):
    news = FakeNews()
    news.news_text = lambda: 1 / 0
    bot, _, notifier = news_bot(news)
    notifier.inbox = ["/news", "/pause"]
    bot.tick()
    assert bot.state.paused
    assert any("en erreur" in m for m in notifier.sent)


def test_calendar_unavailable_fails_closed(news_bot):
    bot, broker, notifier = news_bot(FakeNews(calendar_ok=False))
    new_bar(bot, broker)
    new_bar(bot, broker)
    assert broker.orders == []
    assert sum("calendrier économique indisponible" in m for m in notifier.sent) == 1  # message limité


def test_calendar_fail_open_when_user_chooses_it(news_bot):
    bot, broker, _ = news_bot(FakeNews(calendar_ok=False))
    bot.cfg["news"]["blackout"]["fail_closed"] = False
    new_bar(bot, broker)
    assert len(broker.orders) == 1


def test_news_service_down_blocks_entries_when_required(news_bot):
    bot, broker, notifier = news_bot(None)
    bot.news_required = True
    new_bar(bot, broker)
    assert broker.orders == [] and any("veille news est en panne" in m for m in notifier.sent)


def test_unrecognized_symbol_is_flagged_in_alert(news_bot):
    bot, broker, notifier = news_bot(FakeNews(recognized=False))
    new_bar(bot, broker)
    assert any("Symbole non reconnu" in m for m in notifier.sent if "POSITION OUVERTE" in m)


def test_sentiment_off_means_ignored(news_bot):
    bot, broker, notifier = news_bot(FakeNews(sentiment=-0.9), sentiment_filter="off")
    new_bar(bot, broker)
    opened = next(m for m in notifier.sent if "POSITION OUVERTE" in m)
    assert "CONTRE" not in opened and "Sentiment news" not in opened


def test_auto_mode_blocks_even_without_aligned_trades(news_bot):
    bot, _, _ = news_bot(FakeNews(sentiment=-0.8), sentiment_filter="auto", auto_min_trades=5)
    for k in range(6):  # que des trades contre les news, tous perdants, aucun dans le sens
        bot.journal.record_signal("2026-01-05T10:00:00", "EURUSD", 1, 0.1, 1, 0.9, 1.2, True, 300 + k, "r", {}, -0.8)
        bot.journal.record_close("t", ClosedDeal(3000 + k, 300 + k, "EURUSD", 0.1, 1, -50.0, "stop-loss"))
    assert bot.sentiment_mode() == "block"


def test_auto_mode_forgets_old_trades(news_bot):
    bot, _, _ = news_bot(FakeNews(sentiment=-0.8), sentiment_filter="auto", auto_min_trades=5)
    for k in range(6):  # trades perdants vieux de plus de 90 jours : ne comptent plus
        bot.journal.record_signal("2025-06-01T10:00:00", "EURUSD", 1, 0.1, 1, 0.9, 1.2, True, 400 + k, "r", {}, -0.8)
        bot.journal.record_close("t", ClosedDeal(4000 + k, 400 + k, "EURUSD", 0.1, 1, -50.0, "stop-loss"))
    assert bot.sentiment_mode() == "warn"


def test_partial_closes_count_as_one_trade(make_bot):
    bot, _, _ = make_bot()
    bot.journal.record_signal("2026-01-05T10:00:00", "EURUSD", 1, 0.2, 1, 0.9, 1.2, True, 500, "r", {}, 0.8)
    bot.journal.record_close("t", ClosedDeal(5001, 500, "EURUSD", 0.1, 1, 30.0, "manuel/robot"))
    bot.journal.record_close("t", ClosedDeal(5002, 500, "EURUSD", 0.1, 1, -10.0, "stop-loss"))
    assert bot.journal.alignment_stats(0.5)["aligned"] == (1, 99.0)


def test_spread_spike_blocks_entry(make_bot):
    bot, broker, notifier = make_bot()
    bot.tick()
    for _ in range(40):
        bot.record_spread("EURUSD", 10)  # spread habituel mesuré en continu : 10 points
    normal_spec = broker.spec
    broker.spec = lambda symbol: SymbolSpec(symbol, 0.00001, 5, 0.01, 100, 0.01, 0, 30, 1.10000, 1.10030)
    new_bar(bot, broker)  # spread 30 pts alors que d'habitude il est de 10
    assert broker.orders == []
    assert any("spread anormal" in m for m in notifier.sent)
    broker.spec = normal_spec
    new_bar(bot, broker)
    assert len(broker.orders) == 1


def test_spread_guard_inactive_until_enough_samples_and_needs_absolute_excess(make_bot):
    bot, broker, _ = make_bot()
    bot.tick()
    for _ in range(40):
        bot.record_spread("EURUSD", 1)  # compte ECN : spread quasi nul
    broker.spec = lambda symbol: SymbolSpec(symbol, 0.00001, 5, 0.01, 100, 0.01, 0, 3, 1.10000, 1.10003)
    new_bar(bot, broker)  # 3 pts = 3 x l'habituel, mais seulement +2 points : pas une anomalie
    assert len(broker.orders) == 1


def test_symbol_errors_are_throttled(make_bot):
    bot, broker, notifier = make_bot()
    broker.rates = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no data"))
    for _ in range(5):
        bot.tick()
    assert sum("no data" in m for m in notifier.sent) == 1


def test_news_retry_restores_trading(news_bot):
    bot, broker, notifier = news_bot(None)
    bot.news_required = True
    bot._news_builder = lambda: FakeNews()
    bot._news_retry_at = 0
    new_bar(bot, broker)
    assert bot.news is not None and not bot.news_required
    assert any("redémarrée" in m for m in notifier.sent)
