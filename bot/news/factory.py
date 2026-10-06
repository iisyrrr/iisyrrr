"""Construit le service de veille à partir de la section `news` de config.yaml."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import requests

from .calendar import BlackoutSettings
from .filters import FilterSettings
from .service import NewsService, NewsSettings
from .sources import (FinnhubNewsSource, ForexFactoryCalendar, GdeltSource, RedditSource, RssSource,
                      SitemapNewsSource, StockTwitsSource, XSource)
from .store import NewsStore

log = logging.getLogger(__name__)


def make_source(sc: dict):
    t = sc["type"]
    name = sc.get("name") or t
    if t == "rss":
        return RssSource(name, sc["url"], sc.get("kind", "news"), int(sc.get("tier", 2)),
                         sc.get("exclude"), sc.get("include"), assets=sc.get("assets"),
                         strip_prefix=sc.get("strip_prefix", ""))
    if t == "sitemap":
        return SitemapNewsSource(name, sc["url"], int(sc.get("tier", 1)), sc.get("url_include"),
                                 sc.get("url_exclude"), sc.get("kind", "news"))
    if t == "stocktwits":
        return StockTwitsSource(sc["symbols"], name=name)
    if t == "reddit":
        return RedditSource(sc["subreddits"], sc.get("client_id", ""), sc.get("client_secret", ""),
                            sc.get("user_agent") or "windows:tradingbot-news:1.0", name=name)
    if t == "x":
        if not sc.get("bearer_token"):
            raise ValueError("bearer_token manquant (API X payante)")
        return XSource(sc["bearer_token"], sc["accounts"], sc.get("official_accounts", []), name=name)
    if t == "gdelt":
        return GdeltSource(sc["query"], sc.get("timespan", "2h"), int(sc.get("max_records", 75)),
                           int(sc.get("min_tier", 2)), name=name)
    if t == "finnhub":
        if not sc.get("api_key"):
            raise ValueError("api_key manquante (clé gratuite sur finnhub.io)")
        return FinnhubNewsSource(sc["api_key"], tuple(sc.get("categories", ("forex", "general"))), name=name)
    raise ValueError(f"type de source inconnu : {t}")


def build_analyzer(ai: dict):
    if not ai["enabled"]:
        return None
    key = ai.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        log.warning("Analyse IA désactivée : renseigne news.ai.api_key ou la variable ANTHROPIC_API_KEY")
        return None
    try:
        from .llm import ClaudeAnalyzer

        return ClaudeAnalyzer(api_key=key, model=ai["model"], effort=ai["effort"], brief_effort=ai["brief_effort"])
    except ImportError:
        log.warning("Analyse IA désactivée : `pip install anthropic`")
        return None


def build_news_service(cfg: dict, notifier) -> NewsService | None:
    n = cfg["news"]
    if not n["enabled"]:
        return None
    store = NewsStore(Path(cfg["data_dir"]) / "news.db")
    sources = []
    for sc in n["sources"]:
        if not sc.get("enabled", True):
            continue
        try:
            sources.append(make_source(sc))
        except (KeyError, ValueError) as e:
            log.warning("Source %s ignorée : %s", sc.get("name") or sc.get("type"), e)
    cal = n["calendar"]
    calendar = ForexFactoryCalendar(store, cal["refresh_minutes"], cal["urls"]) if cal["enabled"] else None
    f, b = n["filters"], n["blackout"]
    settings = NewsSettings(
        poll_minutes=n["poll_minutes"], timezone=n["timezone"], morning_brief_time=n["morning_brief_time"],
        brief_weekends=n["brief_weekends"], alert_min_score=n["alert_min_score"],
        alert_max_age_minutes=n["alert_max_age_minutes"], max_alerts_per_hour=n["max_alerts_per_hour"],
        event_reminder_minutes=n["event_reminder_minutes"], llm_max_items=n["ai"]["max_items_per_cycle"],
        llm_min_score=n["ai"]["min_score"], sentiment_half_life_hours=n["sentiment_half_life_hours"],
        filters=FilterSettings(max_age_hours=f["max_age_hours"], half_life_hours=f["half_life_hours"],
                               dedup_similarity=f["dedup_similarity"],
                               social_min_engagement=dict(f["social_min_engagement"])),
        blackout=BlackoutSettings(enabled=b["enabled"], impacts=tuple(b["impacts"]),
                                  minutes_before=b["minutes_before"], minutes_after=b["minutes_after"],
                                  major_minutes_before=b["major_minutes_before"],
                                  major_minutes_after=b["major_minutes_after"]),
        symbol_assets=n["symbol_assets"],
    )
    session = requests.Session()
    log.info("Veille news : %d source(s), calendrier %s, IA %s", len(sources),
             "actif" if calendar else "désactivé", "active" if n["ai"]["enabled"] else "désactivée")
    return NewsService(settings, cfg["trading"]["symbols"], notifier, store, sources, calendar,
                       build_analyzer(n["ai"]), session)
