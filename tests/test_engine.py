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
    def __init__(self, sentiment=None, blackout=None):
        self.sentiment, self.blackout = sentiment, blackout
        self.tz = ZoneInfo("Europe/Paris")

    def context(self, symbol):
        item = NewsItem("Wire", "news", 1, "Fed signals pause", "u", datetime(2026, 1, 5, 11, tzinfo=timezone.utc))
        return SymbolContext(symbol, self.sentiment, 3 if self.sentiment is not None else 0, [item], None,
                             self.blackout)

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
        bot.journal.record_signal("t", "EURUSD", 1, 0.1, 1, 0.9, 1.2, True, 100 + k, "r", {}, -0.8)
        bot.journal.record_close("t", ClosedDeal(1000 + k, 100 + k, "EURUSD", 0.1, 1, -50.0, "stop-loss"))
        bot.journal.record_signal("t", "EURUSD", 1, 0.1, 1, 0.9, 1.2, True, 200 + k, "r", {}, 0.8)
        bot.journal.record_close("t", ClosedDeal(2000 + k, 200 + k, "EURUSD", 0.1, 1, 80.0, "take-profit"))
    assert bot.sentiment_mode() == "block"
    assert any("BLOQUE désormais" in m for m in notifier.sent)


def test_news_commands(news_bot):
    bot, _, notifier = news_bot(FakeNews())
    notifier.inbox = ["/news", "/calendar", "/brief"]
    bot.tick()
    assert {"TOP NEWS", "AGENDA", "BRIEF"} <= set(notifier.sent)
