"""État persistant du robot (survit aux redémarrages du VPS)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class BotState:
    paused: bool = False
    pause_reason: str = ""
    params: dict = field(default_factory=dict)  # réglages appris, par symbole
    last_optimization: str | None = None
    day: str | None = None
    day_start_equity: float | None = None
    last_deal_ticket: int | None = None


class StateStore:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        self.history_path = self.dir / "params_history.jsonl"
        self.state = self._load()

    def _load(self) -> BotState:
        if not self.path.exists():
            return BotState()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(BotState)}
        return BotState(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)  # écriture atomique

    def log_params_change(self, symbol: str, old: dict, new: dict, reason: str) -> None:
        """Historique de chaque changement de réglages, pour pouvoir revenir en arrière."""
        entry = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": symbol,
            "old": old,
            "new": new,
            "reason": reason,
        }
        with self.history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
