"""Stratégies de trading.

Une stratégie calcule, pour chaque bougie clôturée, un signal (1 = achat,
-1 = vente, 0 = rien) ainsi que la distance du stop-loss et du take-profit.

La MÊME fonction `compute` sert au trading en direct, au backtest et à
l'optimisation : ce que le robot teste est exactement ce qu'il trade.

>>> Pour brancher ta propre stratégie : crée une classe qui hérite de
>>> `Strategy`, remplis `default_params`, `param_space` et `compute`, puis
>>> ajoute-la dans `STRATEGIES` en bas du fichier.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind


@dataclass(frozen=True)
class Signal:
    side: int  # 1 = achat, -1 = vente
    sl_dist: float  # distance du stop-loss en prix
    tp_dist: float  # distance du take-profit en prix
    reason: str


@dataclass(frozen=True)
class Param:
    """Bornes d'un paramètre : l'optimiseur ne sortira jamais de ces bornes."""

    low: float
    high: float
    integer: bool = False


class Strategy:
    name = "base"
    default_params: dict = {}
    param_space: dict[str, Param] = {}

    def compute(self, df: pd.DataFrame, p: dict) -> pd.DataFrame:
        """Doit renvoyer un DataFrame indexé comme `df` avec au minimum les
        colonnes `signal`, `sl_dist`, `tp_dist`. Le signal de la ligne i ne
        doit dépendre que des lignes <= i (pas de regard vers le futur)."""
        raise NotImplementedError

    def is_valid(self, p: dict) -> bool:
        return True

    def describe(self, row: pd.Series, p: dict) -> str:
        return self.name

    def last_signal(self, df: pd.DataFrame, p: dict) -> Signal | None:
        """Signal sur la dernière bougie clôturée de `df`."""
        if df.empty:
            return None
        out = self.compute(df, p)
        last = out.iloc[-1]
        side = int(last["signal"])
        sl_dist, tp_dist = float(last["sl_dist"]), float(last["tp_dist"])
        if side == 0 or not (np.isfinite(sl_dist) and sl_dist > 0):
            return None
        if not (np.isfinite(tp_dist) and tp_dist > 0):
            return None
        return Signal(side, sl_dist, tp_dist, self.describe(last, p))


class EmaCrossRsiAtr(Strategy):
    """Stratégie de démonstration (à remplacer par la tienne).

    - Achat : l'EMA rapide croise au-dessus de l'EMA lente et RSI < rsi_buy_max
    - Vente : l'EMA rapide croise en dessous de l'EMA lente et RSI > rsi_sell_min
    - Stop-loss = sl_atr x ATR, take-profit = tp_atr x ATR
    """

    name = "ema_cross_rsi_atr"
    default_params = {
        "ema_fast": 20,
        "ema_slow": 50,
        "rsi_period": 14,
        "rsi_buy_max": 70,
        "rsi_sell_min": 30,
        "atr_period": 14,
        "sl_atr": 1.5,
        "tp_atr": 3.0,
    }
    param_space = {
        "ema_fast": Param(5, 40, integer=True),
        "ema_slow": Param(20, 200, integer=True),
        "rsi_period": Param(7, 21, integer=True),
        "rsi_buy_max": Param(55, 80, integer=True),
        "rsi_sell_min": Param(20, 45, integer=True),
        "atr_period": Param(7, 28, integer=True),
        "sl_atr": Param(1.0, 3.0),
        "tp_atr": Param(1.0, 5.0),
    }

    def is_valid(self, p: dict) -> bool:
        return p["ema_fast"] < p["ema_slow"] and p["rsi_sell_min"] < p["rsi_buy_max"]

    def compute(self, df: pd.DataFrame, p: dict) -> pd.DataFrame:
        close = df["close"]
        fast = ind.ema(close, int(p["ema_fast"]))
        slow = ind.ema(close, int(p["ema_slow"]))
        rsi = ind.rsi(close, int(p["rsi_period"]))
        atr = ind.atr(df, int(p["atr_period"]))

        above = (fast > slow).to_numpy()
        prev_above = np.concatenate([[False], above[:-1]])
        cross_up = above & ~prev_above
        cross_down = ~above & prev_above
        r = rsi.to_numpy()

        signal = np.where(cross_up & (r < p["rsi_buy_max"]), 1, 0)
        signal = np.where(cross_down & (r > p["rsi_sell_min"]), -1, signal)
        warmup = 2 * int(max(p["ema_slow"], p["rsi_period"], p["atr_period"]))
        signal[:warmup] = 0

        return pd.DataFrame(
            {
                "signal": signal,
                "sl_dist": atr.to_numpy() * p["sl_atr"],
                "tp_dist": atr.to_numpy() * p["tp_atr"],
                "rsi": r,
                "atr": atr.to_numpy(),
            },
            index=df.index,
        )

    def describe(self, row: pd.Series, p: dict) -> str:
        sens = "haussier" if row["signal"] > 0 else "baissier"
        return (
            f"Croisement {sens} EMA{int(p['ema_fast'])}/EMA{int(p['ema_slow'])}, "
            f"RSI={row['rsi']:.1f}"
        )


STRATEGIES: dict[str, type[Strategy]] = {
    EmaCrossRsiAtr.name: EmaCrossRsiAtr,
}


def get_strategy(name: str) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"Stratégie inconnue '{name}'. Disponibles : {list(STRATEGIES)}")
    return STRATEGIES[name]()
