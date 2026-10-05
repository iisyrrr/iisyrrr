"""Auto-amélioration du robot par optimisation « walk-forward ».

Principe :
1. On prend l'historique récent et on le coupe en deux :
   - in-sample (le passé, ~70 %) : on y cherche les meilleurs paramètres ;
   - out-of-sample (la période la plus récente, ~30 %) : données que
     l'optimiseur n'a JAMAIS vues, qui servent d'examen.
2. Les nouveaux paramètres ne remplacent les actuels QUE s'ils font
   nettement mieux sur l'out-of-sample, avec assez de trades et un profit
   factor minimum. Sinon le robot garde ses réglages.

Ces garde-fous évitent le piège n°1 des robots « qui apprennent seuls » :
le sur-apprentissage (des réglages parfaits sur le passé, perdants ensuite).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .backtest import BacktestResult, run_backtest
from .strategy import Strategy


@dataclass
class OptimizerSettings:
    trials: int = 80
    local_steps: int = 30
    in_sample_ratio: float = 0.7
    min_trades_oos: int = 30
    min_profit_factor_oos: float = 1.15
    improvement_margin: float = 0.10


@dataclass
class OptimizationReport:
    symbol: str
    current_params: dict
    best_params: dict
    current_oos: BacktestResult
    best_is: BacktestResult
    best_oos: BacktestResult
    adopted: bool
    reason: str

    def summary(self) -> str:
        head = "✅ NOUVEAUX RÉGLAGES ADOPTÉS" if self.adopted else "⏸ Réglages conservés"
        lines = [
            f"{self.symbol} — {head}",
            f"Raison : {self.reason}",
            f"Actuels (test) : {self.current_oos.summary()}",
            f"Candidat (test) : {self.best_oos.summary()}",
        ]
        if self.adopted:
            changes = [
                f"{k}: {self.current_params.get(k)} → {v}"
                for k, v in self.best_params.items()
                if self.current_params.get(k) != v
            ]
            lines.append("Changements : " + ", ".join(changes))
        return "\n".join(lines)


def _round_param(spec, value: float):
    value = min(max(value, spec.low), spec.high)
    return int(round(value)) if spec.integer else round(float(value), 2)


def sample_params(strategy: Strategy, rng: np.random.Generator) -> dict | None:
    for _ in range(1000):
        p = {k: _round_param(s, rng.uniform(s.low, s.high)) for k, s in strategy.param_space.items()}
        if strategy.is_valid(p):
            return p
    return None


def perturb_params(strategy: Strategy, params: dict, rng: np.random.Generator, scale: float = 0.1) -> dict | None:
    for _ in range(100):
        p = dict(params)
        for k, s in strategy.param_space.items():
            p[k] = _round_param(s, params[k] + rng.normal(0, (s.high - s.low) * scale))
        if strategy.is_valid(p):
            return p
    return None


def score(res: BacktestResult, min_trades: int) -> float:
    return res.sqn if res.trades >= min_trades else -np.inf


def should_adopt(
    current_params: dict,
    candidate_params: dict,
    current_oos: BacktestResult,
    candidate_oos: BacktestResult,
    s: OptimizerSettings,
) -> tuple[bool, str]:
    if candidate_params == current_params:
        return False, "les réglages actuels restent les meilleurs"
    if candidate_oos.trades < s.min_trades_oos:
        return False, f"pas assez de trades sur la période test ({candidate_oos.trades} < {s.min_trades_oos})"
    if candidate_oos.profit_factor < s.min_profit_factor_oos:
        return False, (
            f"profit factor test trop faible ({candidate_oos.profit_factor:.2f} "
            f"< {s.min_profit_factor_oos})"
        )
    cur = current_oos.sqn
    required = cur + max(s.improvement_margin * abs(cur), s.improvement_margin)
    if candidate_oos.sqn < required:
        return False, f"amélioration insuffisante (SQN {candidate_oos.sqn:.2f} < {required:.2f} requis)"
    return True, f"meilleur sur données jamais vues (SQN {cur:.2f} → {candidate_oos.sqn:.2f})"


def optimize(
    symbol: str,
    df: pd.DataFrame,
    strategy: Strategy,
    current_params: dict,
    settings: OptimizerSettings,
    spread: float = 0.0,
    rng: np.random.Generator | None = None,
) -> OptimizationReport:
    rng = rng or np.random.default_rng()
    split = int(len(df) * settings.in_sample_ratio)
    is_df, oos_df = df.iloc[:split], df.iloc[split:]
    ratio = settings.in_sample_ratio / max(1e-9, 1 - settings.in_sample_ratio)
    min_trades_is = max(10, int(settings.min_trades_oos * ratio))

    def evaluate(p: dict) -> tuple[float, BacktestResult]:
        res = run_backtest(is_df, strategy, p, spread)
        return score(res, min_trades_is), res

    best_p = dict(current_params)
    best_score, best_is = evaluate(best_p)

    # 1) recherche aléatoire dans tout l'espace autorisé
    for _ in range(settings.trials):
        p = sample_params(strategy, rng)
        if p is None:
            break
        sc, res = evaluate(p)
        if sc > best_score:
            best_p, best_score, best_is = p, sc, res

    # 2) affinage local autour du meilleur candidat
    for _ in range(settings.local_steps):
        p = perturb_params(strategy, best_p, rng)
        if p is None:
            break
        sc, res = evaluate(p)
        if sc > best_score:
            best_p, best_score, best_is = p, sc, res

    current_oos = run_backtest(oos_df, strategy, current_params, spread)
    best_oos = run_backtest(oos_df, strategy, best_p, spread)
    adopted, reason = should_adopt(current_params, best_p, current_oos, best_oos, settings)
    return OptimizationReport(
        symbol=symbol,
        current_params=dict(current_params),
        best_params=best_p,
        current_oos=current_oos,
        best_is=best_is,
        best_oos=best_oos,
        adopted=adopted,
        reason=reason,
    )
