"""Alertes et commandes Telegram."""
from __future__ import annotations

import logging
import threading

import requests

from .secrets import mask

log = logging.getLogger(__name__)


class TelegramNotifier:
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None):
        self.token = token or ""
        self.chat_id = str(chat_id or "")
        self.session = session or requests.Session()
        self.offset: int | None = None
        self._lock = threading.Lock()  # le robot et la veille news envoient en parallèle

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, **kwargs) -> dict:
        url = self.API.format(token=self.token, method=method)
        with self._lock:
            r = self.session.post(url, json=kwargs, timeout=15)
        r.raise_for_status()
        return r.json()

    def send(self, text: str) -> bool:
        log.info("ALERTE : %s", text.replace("\n", " | "))
        if not self.enabled:
            return False
        ok = True
        for i in range(0, len(text), 4000):  # limite Telegram : 4096 caractères
            try:
                self._call("sendMessage", chat_id=self.chat_id, text=text[i : i + 4000],
                           disable_web_page_preview=True)
            except requests.RequestException as e:
                log.warning("Envoi Telegram échoué : %s", mask(str(e)).replace(self.token, "***"))
                ok = False
        return ok

    def poll_commands(self) -> list[str]:
        """Commandes reçues (uniquement depuis TON chat_id, les autres sont ignorés)."""
        if not self.enabled:
            return []
        params = {"timeout": 0}
        if self.offset is not None:
            params["offset"] = self.offset
        try:
            updates = self._call("getUpdates", **params).get("result", [])
        except requests.RequestException as e:
            log.warning("Lecture Telegram échouée : %s", mask(str(e)).replace(self.token, "***"))
            return []
        commands = []
        for upd in updates:
            self.offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            text = (msg.get("text") or "").strip()
            if str(msg.get("chat", {}).get("id")) == self.chat_id and text.startswith("/"):
                commands.append(text)
        return commands

    def drain(self) -> None:
        """Ignore les commandes envoyées pendant que le robot était éteint
        (évite qu'un vieux /closeall s'exécute au redémarrage)."""
        self.poll_commands()
