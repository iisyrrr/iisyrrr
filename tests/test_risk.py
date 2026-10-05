from bot.risk import adaptive_multiplier, compute_lot, daily_loss_exceeded


def test_lot_rounds_down_to_step():
    # 100 € de risque, 1 lot perd 300 € -> 0.333 lot -> 0.33
    assert compute_lot(100, 300, 0.01, 100, 0.01) == 0.33


def test_lot_never_exceeds_risk_when_min_lot_too_big():
    assert compute_lot(1, 300, 0.01, 100, 0.01) == 0.0


def test_lot_capped_at_max():
    assert compute_lot(1_000_000, 10, 0.01, 50, 0.01) == 50


def test_lot_invalid_inputs():
    assert compute_lot(100, 0, 0.01, 100, 0.01) == 0.0


def test_daily_loss():
    assert daily_loss_exceeded(10_000, 9_700, 3.0)
    assert not daily_loss_exceeded(10_000, 9_750, 3.0)
    assert not daily_loss_exceeded(None, 5_000, 3.0)


def test_adaptive_multiplier():
    assert adaptive_multiplier([10, -5], 20, 1.0, 0.5) == 1.0  # pas assez d'historique
    assert adaptive_multiplier([-10] * 15 + [5] * 5, 20, 1.0, 0.5) == 0.5
    assert adaptive_multiplier([10] * 12 + [-10] * 8, 20, 1.0, 0.5) == 1.0
