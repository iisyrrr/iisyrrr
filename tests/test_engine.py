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
