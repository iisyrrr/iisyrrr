"""Point d'entrée du robot.

    python run_bot.py                  # lance le robot 24h/24
    python run_bot.py --test-telegram  # vérifie que les alertes arrivent
    python run_bot.py --backtest       # teste les réglages actuels sur l'historique
    python run_bot.py --optimize       # lance l'auto-amélioration tout de suite
    python run_bot.py --news           # teste la veille news (sans MT5) et envoie le briefing
"""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot import secrets
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
        h.addFilter(secrets.SecretFilter())  # aucun jeton / clé API en clair dans les journaux
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console])


def main() -> None:
    ap = argparse.ArgumentParser(description="Robot de trading MT5")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--news", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    secrets.register_from_config(cfg)
    setup_logging(cfg["data_dir"])
    notifier = TelegramNotifier(cfg["telegram"]["token"], cfg["telegram"]["chat_id"])

    if args.test_telegram:
        ok = notifier.send("✅ Test : les alertes du robot fonctionnent.")
        print("Message envoyé !" if ok else "Échec : vérifie telegram.token et telegram.chat_id")
        return

    if args.news:
        from bot.news.factory import build_news_service

        cfg["news"]["enabled"] = True
        svc = build_news_service(cfg, notifier)
        svc.refresh()
        print(svc.status_line())
        print(svc.dump_status())
        print(svc.calendar_text(), "\n")
        print(svc.news_text(15), "\n")
        brief = svc.brief_text(svc.clock())
        print(brief)
        notifier.send(brief)
        return

    from bot.broker import MT5Broker

    broker = MT5Broker(cfg["mt5"], cfg["trading"]["magic"], cfg["trading"]["deviation_points"])
    one_shot = args.backtest or args.optimize
    bot = build_bot(cfg, broker, notifier, get_strategy(cfg["strategy"]), with_news=not one_shot)

    if one_shot:
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
