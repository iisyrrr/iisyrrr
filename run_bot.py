"""Point d'entrée du robot.

    python run_bot.py                  # lance le robot 24h/24
    python run_bot.py --test-telegram  # vérifie que les alertes arrivent
    python run_bot.py --backtest       # teste les réglages actuels sur l'historique
    python run_bot.py --optimize       # lance l'auto-amélioration tout de suite
"""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot.config import load_config
from bot.engine import build_bot
from bot.notifier import TelegramNotifier
from bot.strategy import get_strategy


def setup_logging(data_dir: str) -> None:
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(Path(data_dir) / "bot.log", maxBytes=5_000_000,
                                       backupCount=5, encoding="utf-8")
    console = logging.StreamHandler(sys.stdout)
    for h in (file_handler, console):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console])


def main() -> None:
    ap = argparse.ArgumentParser(description="Robot de trading MT5")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--optimize", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg["data_dir"])
    notifier = TelegramNotifier(cfg["telegram"]["token"], cfg["telegram"]["chat_id"])

    if args.test_telegram:
        ok = notifier.send("✅ Test : les alertes du robot fonctionnent.")
        print("Message envoyé !" if ok else "Échec : vérifie telegram.token et telegram.chat_id")
        return

    from bot.broker import MT5Broker

    broker = MT5Broker(cfg["mt5"], cfg["trading"]["magic"], cfg["trading"]["deviation_points"])
    bot = build_bot(cfg, broker, notifier, get_strategy(cfg["strategy"]))

    if args.backtest or args.optimize:
        broker.connect()
        try:
            if args.backtest:
                print(bot.backtest_report())
            if args.optimize:
                for rep in bot.run_optimization():
                    print(rep.summary(), "\n")
        finally:
            broker.shutdown()
        return

    bot.run_forever()


if __name__ == "__main__":
    main()
