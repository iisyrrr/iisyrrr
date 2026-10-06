"""Chargement et validation de config.yaml."""
from __future__ import annotations

import copy
import re
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
    "news": {
        "enabled": True,
        "poll_minutes": 5,
        "timezone": "Europe/Paris",
        "morning_brief_time": "07:30",
        "brief_weekends": False,
        "alert_min_score": 0.6,
        "alert_max_age_minutes": 90,
        "max_alerts_per_hour": 6,
        "event_reminder_minutes": 15,
        "sentiment_filter": "warn",  # off | warn | block | auto
        "sentiment_threshold": 0.5,
        "auto_min_trades": 30,
        "sentiment_half_life_hours": 8,
        "symbol_assets": {},
        "calendar": {
            "enabled": True,
            "refresh_minutes": 60,
            "urls": ["https://nfs.faireconomy.media/ff_calendar_thisweek.json"],
        },
        "blackout": {
            "enabled": True,
            "impacts": ["High"],
            "minutes_before": 30,
            "minutes_after": 30,
            "major_minutes_before": 45,
            "major_minutes_after": 60,
        },
        "filters": {
            "max_age_hours": 24,
            "half_life_hours": 6,
            "dedup_similarity": 0.55,
            "social_min_engagement": {"score": 25, "likes": 5, "followers": 500},
        },
        "ai": {
            "enabled": True,
            "api_key": "",
            "model": "claude-opus-5-5",
            "effort": "low",
            "brief_effort": "medium",
            "max_items_per_cycle": 25,
            "min_score": 0.25,
        },
        "sources": [],  # vide = DEFAULT_NEWS_SOURCES
        "extra_sources": [],  # ajoutées aux sources par défaut
    },
}

# Sources activées par défaut : gratuites, sans clé, vérifiées le 06/10/2026.
# tier 1 = officiel ou média de premier plan, 2 = média spécialisé, 3 = réseaux sociaux / agrégateurs.
_NOISE = ["application", "Orders on Banking", "enforcement action", "termination of"]
DEFAULT_NEWS_SOURCES: list[dict] = [
    # --- officiel (banques centrales)
    {"type": "rss", "name": "Fed", "url": "https://www.federalreserve.gov/feeds/press_all.xml",
     "kind": "official", "tier": 1, "assets": ["USD"], "exclude": _NOISE},
    {"type": "rss", "name": "BCE", "url": "https://www.ecb.europa.eu/rss/press.html",
     "kind": "official", "tier": 1, "assets": ["EUR"]},
    {"type": "rss", "name": "Bank of England", "url": "https://www.bankofengland.co.uk/rss/news",
     "kind": "official", "tier": 1, "assets": ["GBP"],
     "exclude": ["Appointment of", "Enforcement", "Prudential Regulation", "consultation"]},
    # --- grands médias (tier 1)
    {"type": "sitemap", "name": "Reuters",
     "url": "https://www.reuters.com/arc/outboundfeeds/news-sitemap/?outputType=xml", "tier": 1,
     "url_include": [r"reuters\.com/(markets|business|world)/"],
     "url_exclude": [r"reuters\.com/(fr|de|es|it|ja|pt|ar|ru|zh)/"]},
    {"type": "rss", "name": "Bloomberg Markets", "url": "https://feeds.bloomberg.com/markets/news.rss", "tier": 1},
    {"type": "rss", "name": "Bloomberg Economics", "url": "https://feeds.bloomberg.com/economics/news.rss", "tier": 1},
    {"type": "rss", "name": "WSJ Markets", "url": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain", "tier": 1},
    {"type": "rss", "name": "WSJ World", "url": "https://feeds.content.dowjones.io/public/rss/RSSWorldNews", "tier": 1},
    {"type": "rss", "name": "CNBC",
     "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362", "tier": 1},
    {"type": "rss", "name": "Financial Times", "url": "https://www.ft.com/markets?format=rss", "tier": 1},
    # --- médias spécialisés forex (tier 2, souvent les plus rapides)
    {"type": "rss", "name": "FinancialJuice", "url": "https://www.financialjuice.com/feed.ashx?xy=rss",
     "tier": 2, "strip_prefix": "FinancialJuice:"},
    {"type": "rss", "name": "FXStreet", "url": "https://xml.fxstreet.com/news/forex-news/index.xml", "tier": 2},
    {"type": "rss", "name": "investingLive", "url": "https://investinglive.com/feed/news/", "tier": 2},
    {"type": "rss", "name": "investingLive (banques centrales)", "url": "https://investinglive.com/feed/centralbank/",
     "tier": 2},
    {"type": "rss", "name": "Investing.com Forex", "url": "https://www.investing.com/rss/news_1.rss", "tier": 2},
    {"type": "rss", "name": "Investing.com Indicateurs", "url": "https://www.investing.com/rss/news_95.rss", "tier": 2},
    {"type": "rss", "name": "Investing.com Matières premières", "url": "https://www.investing.com/rss/news_11.rss",
     "tier": 2},
    # --- réseaux sociaux (tier 3, traités comme des rumeurs tant qu'un média ne confirme pas)
    {"type": "stocktwits", "name": "StockTwits",
     "symbols": {"SPY": ["US_INDICES"], "QQQ": ["US_INDICES"], "GLD": ["XAU"], "UUP": ["USD"]}},
]


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
    n = cfg["news"]
    if n["sentiment_filter"] not in ("off", "warn", "block", "auto"):
        errors.append("news.sentiment_filter doit être off, warn, block ou auto")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(n["timezone"])
    except Exception:
        errors.append(f"news.timezone inconnu : {n['timezone']} (ex : Europe/Paris ; sous Windows : pip install tzdata)")
    if n["morning_brief_time"] and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(n["morning_brief_time"])):
        errors.append("news.morning_brief_time doit être au format HH:MM (ou vide pour désactiver)")
    for sc in n["sources"]:
        if not isinstance(sc, dict) or "type" not in sc:
            errors.append(f"source news invalide (champ 'type' manquant) : {sc}")
    if errors:
        raise ValueError("Erreurs dans la configuration :\n- " + "\n- ".join(errors))


def load_config(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} introuvable : copie config.example.yaml en config.yaml et remplis-le.")
    with path.open(encoding="utf-8") as f:
        user = yaml.safe_load(f) or {}
    cfg = deep_merge(DEFAULTS, user)
    if not (user.get("news") or {}).get("sources"):
        cfg["news"]["sources"] = copy.deepcopy(DEFAULT_NEWS_SOURCES)
    cfg["news"]["sources"] = cfg["news"]["sources"] + list(cfg["news"].get("extra_sources") or [])
    validate(cfg)
    return cfg
