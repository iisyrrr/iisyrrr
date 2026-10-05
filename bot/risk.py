"""Gestion du risque : taille de position, perte journalière max, risque adaptatif."""
from __future__ import annotations

import math


def compute_lot(
    risk_money: float,
    loss_per_lot: float,
    vol_min: float,
    vol_max: float,
    vol_step: float,
) -> float:
    """Lot tel que la perte au stop-loss <= risk_money.

    `loss_per_lot` = perte (en devise du compte) pour 1 lot si le SL est touché.
    On arrondit TOUJOURS vers le bas. Si le lot minimum du broker ferait
    dépasser le risque, on renvoie 0 (trade refusé) plutôt que de sur-risquer.
    """
    if risk_money <= 0 or loss_per_lot <= 0 or vol_step <= 0:
        return 0.0
    raw = risk_money / loss_per_lot
    steps = math.floor(raw / vol_step + 1e-9)
    lot = min(steps * vol_step, vol_max)
    if lot < vol_min - 1e-12:
        return 0.0
    decimals = max(0, -int(math.floor(math.log10(vol_step)))) if vol_step < 1 else 0
    return round(lot, decimals)


def daily_loss_exceeded(day_start_equity: float | None, equity: float, max_loss_pct: float) -> bool:
    if not day_start_equity or max_loss_pct <= 0:
        return False
    return (day_start_equity - equity) / day_start_equity * 100 >= max_loss_pct


def adaptive_multiplier(
    recent_profits: list[float],
    lookback: int,
    min_profit_factor: float,
    reduced_multiplier: float,
) -> float:
    """Réduit automatiquement le risque quand les derniers trades réels sont
    mauvais (profit factor < seuil). Revient à 1.0 quand ça repart."""
    if len(recent_profits) < lookback:
        return 1.0
    window = recent_profits[-lookback:]
    gains = sum(p for p in window if p > 0)
    losses = -sum(p for p in window if p < 0)
    pf = gains / losses if losses > 0 else math.inf
    return reduced_multiplier if pf < min_profit_factor else 1.0
