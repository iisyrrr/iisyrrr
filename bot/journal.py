"""Journal des trades (SQLite) : historique complet consultable à tout moment."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class Journal:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT, symbol TEXT, side INTEGER, volume REAL, entry REAL,
                sl REAL, tp REAL, executed INTEGER, ticket INTEGER,
                reason TEXT, params TEXT
            );
            CREATE TABLE IF NOT EXISTS closed (
                deal_ticket INTEGER PRIMARY KEY,
                position_id INTEGER, time TEXT, symbol TEXT, volume REAL,
                price REAL, profit REAL, reason TEXT
            );
            """
        )
        self.db.commit()

    def record_signal(self, time: str, symbol: str, side: int, volume: float, entry: float,
                      sl: float, tp: float, executed: bool, ticket: int, reason: str, params: dict) -> None:
        self.db.execute(
            "INSERT INTO signals (time, symbol, side, volume, entry, sl, tp, executed, ticket, reason, params)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time, symbol, side, volume, entry, sl, tp, int(executed), ticket, reason, json.dumps(params)),
        )
        self.db.commit()

    def record_close(self, time: str, deal) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO closed VALUES (?,?,?,?,?,?,?,?)",
            (deal.ticket, deal.position_id, time, deal.symbol, deal.volume, deal.price, deal.profit, deal.reason),
        )
        self.db.commit()

    def recent_profits(self, n: int) -> list[float]:
        rows = self.db.execute("SELECT profit FROM closed ORDER BY deal_ticket DESC LIMIT ?", (n,)).fetchall()
        return [r[0] for r in reversed(rows)]

    def day_summary(self, day: str) -> tuple[int, int, float]:
        """(nombre de trades, gagnants, profit total) pour une date AAAA-MM-JJ."""
        n, wins, total = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(profit > 0), 0), COALESCE(SUM(profit), 0)"
            " FROM closed WHERE time LIKE ?",
            (f"{day}%",),
        ).fetchone()
        return int(n), int(wins), float(total)
