import numpy as np
import pandas as pd

from bot.backtest import run_backtest
from bot.strategy import EmaCrossRsiAtr, Strategy


def test_no_lookahead(random_walk):
    s = EmaCrossRsiAtr()
    p = s.default_params
    full = s.compute(random_walk, p)
    part = s.compute(random_walk.iloc[:3000], p)
    pd.testing.assert_frame_equal(full.iloc[:3000], part)


def test_strategy_generates_both_sides(random_walk):
    out = EmaCrossRsiAtr().compute(random_walk, EmaCrossRsiAtr.default_params)
    assert (out["signal"] == 1).any() and (out["signal"] == -1).any()


def test_last_signal_matches_compute(random_walk):
    s = EmaCrossRsiAtr()
    out = s.compute(random_walk, s.default_params)
    i = int(np.flatnonzero(out["signal"].to_numpy())[-1])
    sig = s.last_signal(random_walk.iloc[: i + 1], s.default_params)
    assert sig is not None and sig.side == out["signal"].iloc[i]


class Scripted(Strategy):
    """Signal imposé à la main pour tester le moteur de backtest."""

    def __init__(self, signals, sl, tp):
        self.signals, self.sl, self.tp = signals, sl, tp

    def compute(self, df, p):
        return pd.DataFrame({"signal": self.signals, "sl_dist": self.sl, "tp_dist": self.tp}, index=df.index)


def frame(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def test_buy_hits_tp():
    df = frame([(1.0, 1.0, 1.0, 1.0), (1.0, 1.01, 0.999, 1.0), (1.0, 1.03, 1.0, 1.02)])
    res = run_backtest(df, Scripted([1, 0, 0], 0.01, 0.02), {})
    assert res.trades == 1 and abs(res.r[0] - 2.0) < 1e-9


def test_same_bar_sl_and_tp_counts_as_loss():
    df = frame([(1.0, 1.0, 1.0, 1.0), (1.0, 1.05, 0.95, 1.0)])
    res = run_backtest(df, Scripted([1, 0], 0.01, 0.02), {})
    assert abs(res.r[0] + 1.0) < 1e-9


def test_sell_gap_through_stop_is_worse_than_minus_one():
    df = frame([(1.0, 1.0, 1.0, 1.0), (1.0, 1.001, 0.999, 1.0), (1.05, 1.06, 1.04, 1.05)])
    res = run_backtest(df, Scripted([-1, 0, 0], 0.01, 0.02), {})
    assert res.r[0] < -1.0


def test_spread_can_prevent_tp():
    # sans spread le TP (1.02) est touché ; avec 0.006 de spread il passe à 1.026
    df = frame([(1.0, 1.0, 1.0, 1.0), (1.0, 1.025, 1.0, 1.02)])
    no_spread = run_backtest(df, Scripted([1, 0], 0.01, 0.02), {}).r[0]
    with_spread = run_backtest(df, Scripted([1, 0], 0.01, 0.02), {}, spread=0.006).r[0]
    assert abs(no_spread - 2.0) < 1e-9 and with_spread < 2.0


def test_one_position_at_a_time():
    df = frame([(1.0, 1.0, 1.0, 1.0)] + [(1.0, 1.001, 0.999, 1.0)] * 5)
    res = run_backtest(df, Scripted([1, 1, 1, 0, 0, 0], 0.01, 0.02), {})
    assert res.trades == 1  # jamais clôturée -> les signaux suivants sont ignorés
