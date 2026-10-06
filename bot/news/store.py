"""Mémoire de la veille (SQLite) : infos vues, analyses IA déjà payées, alertes envoyées."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .models import NewsItem


class NewsStore:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY, first_seen TEXT, published TEXT, source TEXT, kind TEXT,
                tier INTEGER, title TEXT, url TEXT, assets TEXT, score REAL,
                corroborations INTEGER, analysis TEXT, alerted INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS items_published ON items(published);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated TEXT);
            """
        )
        self.db.commit()

    def save_items(self, items: list[NewsItem], now: datetime) -> None:
        with self._lock:
            for i in items:
                self.db.execute(
                    """INSERT INTO items (id, first_seen, published, source, kind, tier, title, url, assets,
                                          score, corroborations, analysis)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET score=excluded.score,
                           corroborations=excluded.corroborations, assets=excluded.assets,
                           analysis=COALESCE(excluded.analysis, items.analysis)""",
                    (i.id, now.isoformat(), i.published.isoformat(), i.source, i.kind, i.tier, i.title, i.url,
                     json.dumps(sorted(i.assets)), i.score, i.corroborations,
                     json.dumps(i.analysis) if i.analysis is not None else None),
                )
            self.db.commit()

    def analyses(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        with self._lock:
            rows = self.db.execute(
                f"SELECT id, analysis FROM items WHERE analysis IS NOT NULL AND id IN ({','.join('?' * len(ids))})",
                ids,
            ).fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

    def alerted_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        with self._lock:
            rows = self.db.execute(
                f"SELECT id FROM items WHERE alerted = 1 AND id IN ({','.join('?' * len(ids))})", ids
            ).fetchall()
        return {r[0] for r in rows}

    def mark_alerted(self, ids: list[str]) -> None:
        with self._lock:
            self.db.executemany("UPDATE items SET alerted = 1 WHERE id = ?", [(i,) for i in ids])
            self.db.commit()

    def get(self, key: str) -> tuple[str, str] | None:
        """(valeur, date de mise à jour ISO) ou None."""
        with self._lock:
            row = self.db.execute("SELECT value, updated FROM kv WHERE key = ?", (key,)).fetchone()
        return (row[0], row[1]) if row else None

    def put(self, key: str, value: str, now: datetime) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, value, now.isoformat()))
            self.db.commit()

    def purge(self, before: datetime) -> None:
        with self._lock:
            self.db.execute("DELETE FROM items WHERE published < ?", (before.isoformat(),))
            self.db.commit()
