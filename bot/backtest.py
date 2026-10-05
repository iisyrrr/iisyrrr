"""Backtest bougie par bougie, résultats exprimés en R (multiples du risque).

Hypothèses (volontairement prudentes) :
- entrée à l'ouverture de la bougie qui suit le signal ;
- spread payé à l'entrée (achat) ou à la sortie (vente) ;
- si le SL et le TP sont touchés dans la même bougie, on compte le SL ;
- un gap au-delà du SL est exécuté au prix d'ouverture (pire que le SL) ;
- une seule position à la fois par symbole (comme en direct).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .strategy import Strategy


@dataclass
class BacktestResult:
    r: np.ndarray  # résultat de chaque trade en R

    @property
    def trades(self) -> int:
        return int(self.r.size)

    @property
    def win_rate(self) -> float:
        return float((self.r > 0).mean()) if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        gains = self.r[self.r > 0].sum()
        losses = -self.r[self.r < 0].sum()
        if losses > 0:
            return float(gains / losses)
        return float("inf") if gains > 0 else 0.0

    @property
    def expectancy(self) -> float:
        return float(self.r.mean()) if self.trades else 0.0

    @property
    def total_r(self) -> float:
        return float(self.r.sum())

    @property
    def max_drawdown(self) -> float:
        if not self.trades:
            return 0.0
        equity = np.concatenate([[0.0], np.cumsum(self.r)])
        return float((np.maximum.accumulate(equity) - equity).max())

    @property
    def sqn(self) -> float:
        """System Quality Number (Van Tharp) : espérance / écart-type x racine(n).
        Récompense à la fois la rentabilité et la régularité."""
        if self.trades < 2:
            return 0.0
        std = self.r.std(ddof=1)
        if std == 0:
            return 0.0
        return float(self.r.mean() / std * np.sqrt(min(self.trades, 100)))

    def summary(self) -> str:
        pf = self.profit_factor
        pf_txt = "∞" if pf == float("inf") else f"{pf:.2f}"
        return (
            f"{self.trades} trades | gagnants {self.win_rate:.0%} | PF {pf_txt} | "
            f"espérance {self.expectancy:+.2f}R | total {self.total_r:+.1f}R | "
            f"DD max {self.max_drawdown:.1f}R | SQN {self.sqn:.2f}"
        )


def _first_true(mask: np.ndarray) -> int:
    if mask.size == 0:
        return 0
    idx = int(np.argmax(mask))
    return idx if mask[idx] else mask.size


def run_backtest(
    df: pd.DataFrame,
    strategy: Strategy,
    params: dict,
    spread: float = 0.0,
    max_hold_bars: int | None = None,
) -> BacktestResult:
    out = strategy.compute(df, params)
    sig = out["signal"].to_numpy()
    sl_d = out["sl_dist"].to_numpy(dtype=float)
    tp_d = out["tp_dist"].to_numpy(dtype=float)
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    n = len(df)

    results: list[float] = []
    next_free = 0
    for i in np.flatnonzero(sig):
        j = i + 1
        if j >= n:
            break
        if j < next_free:
            continue  # déjà en position
        sd, td, side = sl_d[i], tp_d[i], int(sig[i])
        if not (sd > 0 and td > 0):
            continue

        end = n if max_hold_bars is None else min(n, j + max_hold_bars)
        hh, ll, oo = h[j:end], l[j:end], o[j:end]
        if side == 1:
            entry = o[j] + spread
            sl, tp = entry - sd, entry + td
            sl_hit, tp_hit = ll <= sl, hh >= tp
        else:
            entry = o[j]
            sl, tp = entry + sd, entry - td
            sl_hit, tp_hit = hh + spread >= sl, ll + spread <= tp

        k_sl, k_tp, m = _first_true(sl_hit), _first_true(tp_hit), hh.size
        if k_sl == m and k_tp == m:
            k = m - 1
            exit_price = c[j + k] + (spread if side == -1 else 0.0)
        elif k_sl <= k_tp:
            k = k_sl
            exit_price = min(sl, oo[k]) if side == 1 else max(sl, oo[k] + spread)
        else:
            k = k_tp
            exit_price = tp

        results.append(side * (exit_price - entry) / sd)
        next_free = j + k + 1

    return BacktestResult(np.asarray(results, dtype=float))
