"""Masquage des secrets (jeton Telegram, clés API, mots de passe) dans les journaux
et les messages d'erreur : une URL d'API contient souvent la clé en clair."""
from __future__ import annotations

import logging

_SECRETS: set[str] = set()

SECRET_KEYS = ("token", "key", "secret", "password", "bearer")


def register(*values) -> None:
    for v in values:
        if isinstance(v, str) and len(v) >= 6:
            _SECRETS.add(v)


def register_from_config(cfg) -> None:
    """Enregistre toutes les valeurs dont la clé ressemble à un secret, partout dans la config."""
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if isinstance(v, str) and any(word in str(k).lower() for word in SECRET_KEYS):
                register(v)
            else:
                register_from_config(v)
    elif isinstance(cfg, list):
        for v in cfg:
            register_from_config(v)


def mask(text: str) -> str:
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _SECRETS:
            msg = record.getMessage()
            masked = mask(msg)
            if masked != msg:
                record.msg, record.args = masked, ()
            if record.exc_info and record.exc_info[1] is not None:
                record.exc_text = mask(logging.Formatter().formatException(record.exc_info))
        return True
