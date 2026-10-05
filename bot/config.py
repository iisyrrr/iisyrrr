"""Chargement et validation de config.yaml."""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

TIMEFRAMES = {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}

DEFAULTS: dict = {
    "mode": "alert_only",
    "strategy": "ema_cross_rsi_atr",
    "data_dir": "data",
    "mt5": {"login": None, "password": "", "server": "", "path": ""},
    "telegram": {"token": "", "chat_id": ""},
    "trading": {
        "symbols": ["EURUSD"],
        "timeframe": "M15",
        "history_bars": 1000,
        "magic": 20261005,
        "deviation_points": 20,
        "max_spread_points": 30,
        "poll_seconds": 10,
    },
    "risk": {
        "risk_per_trade_pct": 0.5,
        "max_open_positions": 3,
        "max_daily_loss_pct": 3.0,
        "adaptive": {
            "enabled": True,
            "lookback_trades": 20,
            "min_profit_factor": 1.0,
            "reduced_multiplier": 0.5,
        },
    },
    "optimizer": {
        "enabled": True,
        "every_days": 7,
        "history_bars": 20000,
        "trials": 80,
        "local_steps": 30,
        "in_sample_ratio": 0.7,
        "min_trades_oos": 30,
        "min_profit_factor_oos": 1.15,
        "improvement_margin": 0.10,
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def validate(cfg: dict) -> None:
    errors = []
    if cfg["mode"] not in ("alert_only", "live"):
        errors.append("mode doit être 'alert_only' ou 'live'")
    t = cfg["trading"]
    if not t["symbols"]:
        errors.append("trading.symbols est vide")
    if t["timeframe"] not in TIMEFRAMES:
        errors.append(f"trading.timeframe doit être parmi {sorted(TIMEFRAMES)}")
    r = cfg["risk"]
    if not 0 < r["risk_per_trade_pct"] <= 5:
        errors.append("risk.risk_per_trade_pct doit être entre 0 et 5 (%)")
    if r["max_open_positions"] < 1:
        errors.append("risk.max_open_positions doit être >= 1")
    o = cfg["optimizer"]
    if not 0.5 <= o["in_sample_ratio"] <= 0.9:
        errors.append("optimizer.in_sample_ratio doit être entre 0.5 et 0.9")
    if errors:
        raise ValueError("Erreurs dans la configuration :\n- " + "\n- ".join(errors))


def load_config(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} introuvable : copie config.example.yaml en config.yaml et remplis-le.")
    with path.open(encoding="utf-8") as f:
        cfg = deep_merge(DEFAULTS, yaml.safe_load(f) or {})
    validate(cfg)
    return cfg
