"""Journal des trades (SQLite) : historique complet consultable à tout moment."""
from __future__ import annotations

import json
import math
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
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(signals)")}
        if "news_sentiment" not in cols:  # migration des journaux créés avant la veille news
            self.db.execute("ALTER TABLE signals ADD COLUMN news_sentiment REAL")
        self.db.commit()

    def record_signal(self, time: str, symbol: str, side: int, volume: float, entry: float,
                      sl: float, tp: float, executed: bool, ticket: int, reason: str, params: dict,
                      news_sentiment: float | None = None) -> None:
        self.db.execute(
            "INSERT INTO signals (time, symbol, side, volume, entry, sl, tp, executed, ticket, reason, params,"
            " news_sentiment) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (time, symbol, side, volume, entry, sl, tp, int(executed), ticket, reason, json.dumps(params),
             news_sentiment),
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

    def alignment_stats(self, threshold: float, since: str | None = None) -> dict[str, tuple[int, float]]:
        """Résultats des trades réels selon qu'ils allaient dans le sens du sentiment
        des news, contre lui, ou sans sentiment clair : {clé: (nombre, profit factor)}.
        Les clôtures partielles d'une même position sont additionnées (1 trade).
        Profit factor plafonné à 99 (aucune perte) ; 0 si aucun trade."""
        rows = self.db.execute(
            "SELECT s.side, s.news_sentiment, SUM(c.profit) FROM signals s"
            " JOIN closed c ON c.position_id = s.ticket WHERE s.executed = 1 AND s.ticket > 0"
            " AND s.time >= ? GROUP BY s.id",
            (since or "",),
        ).fetchall()
        groups: dict[str, list[float]] = {"aligned": [], "against": [], "neutral": []}
        for side, sentiment, profit in rows:
            if sentiment is None or abs(sentiment) < threshold:
                groups["neutral"].append(profit)
            elif side * sentiment > 0:
                groups["aligned"].append(profit)
            else:
                groups["against"].append(profit)

        def pf(values: list[float]) -> float:
            gains = sum(v for v in values if v > 0)
            losses = -sum(v for v in values if v < 0)
            return min(99.0, gains / losses) if losses > 0 else (99.0 if gains > 0 else 0.0)

        return {k: (len(v), pf(v)) for k, v in groups.items()}
