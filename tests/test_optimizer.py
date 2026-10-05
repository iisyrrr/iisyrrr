import numpy as np

from bot.backtest import BacktestResult
from bot.optimizer import OptimizerSettings, optimize, sample_params, should_adopt
from bot.strategy import EmaCrossRsiAtr

S = OptimizerSettings(min_trades_oos=5, min_profit_factor_oos=1.1, improvement_margin=0.1)


def res(values):
    return BacktestResult(np.asarray(values, dtype=float))


def test_rejects_same_params():
    ok, _ = should_adopt({"a": 1}, {"a": 1}, res([1, -1]), res([2] * 10), S)
    assert not ok


def test_rejects_too_few_trades():
    ok, reason = should_adopt({"a": 1}, {"a": 2}, res([-1, 1]), res([2, -1, 2]), S)
    assert not ok and "trades" in reason


def test_rejects_low_profit_factor():
    ok, _ = should_adopt({"a": 1}, {"a": 2}, res([-1] * 6), res([1, -1] * 5), S)
    assert not ok


def test_adopts_clear_improvement():
    ok, _ = should_adopt({"a": 1}, {"a": 2}, res([1, -1] * 10), res([2, -1, 2, 1, -1] * 4), S)
    assert ok


def test_sampled_params_within_bounds_and_valid():
    s, rng = EmaCrossRsiAtr(), np.random.default_rng(0)
    for _ in range(200):
        p = sample_params(s, rng)
        assert s.is_valid(p)
        for k, spec in s.param_space.items():
            assert spec.low <= p[k] <= spec.high


def test_optimize_runs_end_to_end(random_walk):
    s = EmaCrossRsiAtr()
    rep = optimize("EURUSD", random_walk, s, dict(s.default_params),
                   OptimizerSettings(trials=10, local_steps=5, min_trades_oos=5),
                   spread=0.0001, rng=np.random.default_rng(1))
    assert rep.best_is.trades > 0
    assert "EURUSD" in rep.summary()
    if not rep.adopted:
        assert rep.reason
