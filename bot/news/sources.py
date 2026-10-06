"""Lecteurs de sources : flux RSS, calendrier économique, réseaux sociaux, API de news.

Chaque source expose `name` et `fetch(session, now) -> list[NewsItem]`
(ou `list[CalendarEvent]` pour le calendrier). Une source en panne ne bloque
jamais les autres : le service attrape ses erreurs et continue.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

from .models import NEWS, OFFICIAL, SOCIAL, CalendarEvent, NewsItem

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TradingBot-News/1.0"
TIMEOUT = 20

# Fiabilité des médias quand la source est un agrégateur (GDELT, Finnhub…)
DOMAIN_TIER = {
    "reuters.com": 1, "bloomberg.com": 1, "ft.com": 1, "wsj.com": 1, "apnews.com": 1, "afp.com": 1,
    "federalreserve.gov": 1, "ecb.europa.eu": 1, "bankofengland.co.uk": 1, "boj.or.jp": 1, "snb.ch": 1,
    "bankofcanada.ca": 1, "rba.gov.au": 1, "rbnz.govt.nz": 1, "bls.gov": 1, "bea.gov": 1, "treasury.gov": 1,
    "cnbc.com": 2, "marketwatch.com": 2, "economist.com": 2, "nikkei.com": 2, "asia.nikkei.com": 2,
    "barrons.com": 2, "forexlive.com": 2, "investinglive.com": 2, "fxstreet.com": 2, "investing.com": 2,
    "finance.yahoo.com": 2, "kitco.com": 2, "lesechos.fr": 2, "boursorama.com": 2, "handelsblatt.com": 2,
    "nytimes.com": 2, "theguardian.com": 2, "bbc.co.uk": 2, "bbc.com": 2, "cnn.com": 2, "politico.com": 2,
    "axios.com": 2, "businessinsider.com": 3, "zerohedge.com": 3, "seekingalpha.com": 3, "benzinga.com": 3,
}


def domain_tier(url_or_domain: str, default: int = 3) -> int:
    host = urlparse(url_or_domain).netloc or url_or_domain
    host = host.lower().removeprefix("www.")
    while host:
        if host in DOMAIN_TIER:
            return DOMAIN_TIER[host]
        host = host.partition(".")[2]
    return default


def clean_text(text: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def http_get(session, url: str, **kwargs):
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    r = session.get(url, headers=headers, timeout=kwargs.pop("timeout", TIMEOUT), **kwargs)
    r.raise_for_status()
    return r


def _compile(patterns) -> re.Pattern | None:
    patterns = [p for p in (patterns or []) if p]
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE) if patterns else None


# ======================================================================== RSS / Atom
class RssSource:
    def __init__(self, name: str, url: str, kind: str = NEWS, tier: int = 2, exclude=None, include=None,
                 max_items: int = 50, assets=None, strip_prefix: str = ""):
        self.name, self.url, self.kind, self.tier = name, url, kind, tier
        self.strip_prefix = strip_prefix
        self.exclude, self.include = _compile(exclude), _compile(include)
        self.max_items = max_items
        self.assets = set(assets or [])  # actifs imposés (ex : flux « or » -> XAU)

    def fetch(self, session, now: datetime) -> list[NewsItem]:
        import feedparser

        feed = feedparser.parse(http_get(session, self.url).content)
        if feed.bozo and not feed.entries:
            raise ValueError(f"flux illisible : {feed.bozo_exception}")
        items = []
        for e in feed.entries[: self.max_items]:
            stamp = e.get("published_parsed") or e.get("updated_parsed")
            if not stamp:
                continue  # sans date on ne peut pas juger la fraîcheur
            title = clean_text(e.get("title"))
            if self.strip_prefix and title.startswith(self.strip_prefix):
                title = title[len(self.strip_prefix):].strip()
            summary = clean_text(e.get("summary") or e.get("description"))
            if len(summary) < 40 or summary.lower().startswith("read more"):
                # certains flux mettent le vrai résumé dans un champ à eux (ex : fxstnewsns:summary)
                extra = next((v for k, v in e.items() if k.endswith("_summary") and isinstance(v, str)), "")
                summary = clean_text(extra) or ("" if summary.lower().startswith("read more") else summary)
            summary = summary[:1000]
            categories = " ".join(t.get("term", "") for t in e.get("tags", []) if isinstance(t, dict))
            haystack = f"{title} {summary} {categories}"
            if self.exclude and self.exclude.search(haystack):
                continue
            if self.include and not self.include.search(haystack):
                continue
            items.append(NewsItem(
                source=self.name, kind=self.kind, tier=self.tier, title=title,
                url=e.get("link", ""), published=datetime(*stamp[:6], tzinfo=timezone.utc),
                summary=summary, assets=set(self.assets),
            ))
        return items


class SitemapNewsSource:
    """Plan de site « Google News » (ex : Reuters, qui n'a plus de flux RSS depuis 2020).
    Contient les titres de la dernière heure, sans résumé."""

    def __init__(self, name: str, url: str, tier: int = 1, url_include=None, url_exclude=None,
                 kind: str = NEWS):
        self.name, self.url, self.tier, self.kind = name, url, tier, kind
        self.url_include, self.url_exclude = _compile(url_include), _compile(url_exclude)

    def fetch(self, session, now: datetime) -> list[NewsItem]:
        import xml.etree.ElementTree as ET

        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9",
              "n": "http://www.google.com/schemas/sitemap-news/0.9"}
        root = ET.fromstring(http_get(session, self.url).content)
        items = []
        for u in root.findall("s:url", ns):
            loc = (u.findtext("s:loc", "", ns) or "").strip()
            if self.url_include and not self.url_include.search(loc):
                continue
            if self.url_exclude and self.url_exclude.search(loc):
                continue
            news = u.find("n:news", ns)
            if news is None:
                continue
            title = clean_text(news.findtext("n:title", "", ns))
            stamp = news.findtext("n:publication_date", "", ns) or u.findtext("s:lastmod", "", ns)
            try:
                when = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                continue
            if title:
                items.append(NewsItem(source=self.name, kind=self.kind, tier=self.tier, title=title, url=loc,
                                      published=when))
        return items


# ======================================================================== calendrier
class ForexFactoryCalendar:
    """Export hebdomadaire public de ForexFactory (faireconomy). Le site limite le
    nombre de téléchargements : on garde une copie locale et on ne la rafraîchit
    qu'une fois par `min_interval_minutes`. En cas d'échec, l'ancienne copie sert."""

    name = "ForexFactory (calendrier)"
    URLS = ("https://nfs.faireconomy.media/ff_calendar_thisweek.json",)

    def __init__(self, store, min_interval_minutes: int = 60, urls=None):
        self.store = store
        self.min_interval = timedelta(minutes=min_interval_minutes)
        self.urls = tuple(urls or self.URLS)

    def _payload(self, session, url: str, now: datetime) -> str:
        key = f"calendar:{url}"
        cached = self.store.get(key)
        if cached and now - datetime.fromisoformat(cached[1]) < self.min_interval:
            return cached[0]
        try:
            text = http_get(session, url).text
            json.loads(text)  # vérifie que c'est bien du JSON (pas une page de blocage)
        except Exception:
            if cached:
                log.warning("Calendrier %s indisponible, utilisation de la copie locale", url)
                return cached[0]
            raise
        self.store.put(key, text, now)
        return text

    @staticmethod
    def parse(payload: str) -> list[CalendarEvent]:
        events = []
        for e in json.loads(payload):
            try:
                when = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
            except (KeyError, ValueError):
                continue
            events.append(CalendarEvent(
                title=e.get("title", "").strip(), currency=(e.get("country") or "").upper(), time=when,
                impact=e.get("impact", ""), forecast=e.get("forecast") or "", previous=e.get("previous") or "",
                actual=e.get("actual") or "",
            ))
        return events

    def fetch(self, session, now: datetime) -> list[CalendarEvent]:
        events: dict[str, CalendarEvent] = {}
        errors = []
        for url in self.urls:
            try:
                for ev in self.parse(self._payload(session, url, now)):
                    events[ev.id] = ev
            except Exception as e:
                errors.append(e)
        if errors and not events:
            raise errors[0]
        return sorted(events.values(), key=lambda e: e.time)


# ======================================================================== réseaux sociaux
class StockTwitsSource:
    """Flux public StockTwits par symbole (sans clé). Bruyant : uniquement des
    posts d'utilisateurs suivis, et toujours traités comme des rumeurs à confirmer."""

    URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"

    def __init__(self, symbols: dict[str, list[str]], name: str = "StockTwits"):
        self.symbols = symbols  # symbole StockTwits -> actifs (ex : {"SPY": ["US_INDICES"]})
        self.name = name

    def fetch(self, session, now: datetime) -> list[NewsItem]:
        items = []
        for symbol, assets in self.symbols.items():
            data = http_get(session, self.URL.format(symbol=quote(symbol))).json()
            for m in data.get("messages", []):
                user = m.get("user") or {}
                sentiment = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
                items.append(NewsItem(
                    source=f"{self.name} @{user.get('username', '?')}", kind=SOCIAL,
                    tier=3, title=clean_text(m.get("body"))[:280],
                    url=f"https://stocktwits.com/{user.get('username', '')}/message/{m.get('id')}",
                    published=datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")),
                    engagement={"likes": int((m.get("likes") or {}).get("total", 0)),
                                "followers": int(user.get("followers", 0))},
                    author=user.get("username", ""), assets=set(assets),
                    summary=f"Sentiment déclaré : {sentiment}" if sentiment else "",
                ))
        return items


class RedditSource:
    """Reddit. Avec une appli « script » (client_id/secret gratuits sur
    reddit.com/prefs/apps) on passe par l'API officielle OAuth ; sinon on tente
    les flux JSON publics, souvent bloqués."""

    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

    def __init__(self, subreddits: list[str], client_id: str = "", client_secret: str = "",
                 user_agent: str = "windows:tradingbot-news:1.0 (by /u/unknown)", listing: str = "hot",
                 limit: int = 25, name: str = "Reddit"):
        self.subreddits, self.listing, self.limit, self.name = subreddits, listing, limit, name
        self.client_id, self.client_secret, self.user_agent = client_id, client_secret, user_agent
        self._token: tuple[str, float] | None = None

    def _auth_header(self, session) -> dict:
        if not (self.client_id and self.client_secret):
            return {}
        if not self._token or self._token[1] < time.time() + 60:
            r = session.post(self.TOKEN_URL, data={"grant_type": "client_credentials"},
                             auth=(self.client_id, self.client_secret),
                             headers={"User-Agent": self.user_agent}, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()
            self._token = (d["access_token"], time.time() + float(d.get("expires_in", 3600)))
        return {"Authorization": f"bearer {self._token[0]}"}

    def fetch(self, session, now: datetime) -> list[NewsItem]:
        auth = self._auth_header(session)
        base = "https://oauth.reddit.com" if auth else "https://www.reddit.com"
        items = []
        for sub in self.subreddits:
            url = f"{base}/r/{sub}/{self.listing}" + ("" if auth else ".json") + f"?limit={self.limit}&raw_json=1"
            data = http_get(session, url, headers={**auth, "User-Agent": self.user_agent}).json()
            for child in data.get("data", {}).get("children", []):
                p = child.get("data", {})
                if p.get("stickied") or p.get("over_18"):
                    continue
                link = p.get("url_overridden_by_dest") or ""
                items.append(NewsItem(
                    source=f"Reddit r/{sub}", kind=SOCIAL, tier=3, title=clean_text(p.get("title")),
                    url="https://www.reddit.com" + p.get("permalink", ""),
                    published=datetime.fromtimestamp(float(p.get("created_utc", 0)), timezone.utc),
                    summary=(clean_text(p.get("selftext"))[:600] + (f" [lien : {link}]" if link else "")).strip(),
                    engagement={"score": int(p.get("score", 0)), "comments": int(p.get("num_comments", 0))},
                    author=p.get("author", ""),
                ))
        return items


class XSource:
    """X (Twitter) – API payante : recherche récente sur une liste de comptes."""

    URL = "https://api.x.com/2/tweets/search/recent"

    def __init__(self, bearer_token: str, accounts: list[str], official_accounts=(), name: str = "X"):
        self.token, self.accounts, self.name = bearer_token, accounts, name
        self.official = {a.lower() for a in official_accounts}

    def fetch(self, session, now: datetime) -> list[NewsItem]:
        items = []
        # la requête est limitée en longueur : on découpe la liste de comptes
        for k in range(0, len(self.accounts), 15):
            chunk = self.accounts[k: k + 15]
            query = "(" + " OR ".join(f"from:{a}" for a in chunk) + ") -is:retweet -is:reply"
            data = http_get(session, self.URL, headers={"Authorization": f"Bearer {self.token}"}, params={
                "query": query, "max_results": 50, "tweet.fields": "created_at,public_metrics,author_id",
                "expansions": "author_id", "user.fields": "username,public_metrics,verified",
            }).json()
            users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
            for t in data.get("data", []):
                u = users.get(t.get("author_id"), {})
                name = u.get("username", "?")
                official = name.lower() in self.official
                metrics = t.get("public_metrics", {})
                items.append(NewsItem(
                    source=f"X @{name}", kind=OFFICIAL if official else SOCIAL, tier=1 if official else 3,
                    title=clean_text(t.get("text"))[:280], url=f"https://x.com/{name}/status/{t['id']}",
                    published=datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")),
                    engagement={"likes": int(metrics.get("like_count", 0)),
                                "followers": int(u.get("public_metrics", {}).get("followers_count", 0))},
                    author=name,
                ))
        return items


# ======================================================================== API de news
class GdeltSource:
    """GDELT DOC 2.0 : moteur mondial gratuit, sans clé. La fiabilité de chaque
    article est déduite de son domaine (Reuters, Bloomberg… = tier 1)."""

    URL = "https://api.gdeltproject.org/api/v2/doc/doc"

    def __init__(self, query: str, timespan: str = "2h", max_records: int = 75, min_tier: int = 2,
                 name: str = "GDELT"):
        self.query, self.timespan, self.max_records, self.min_tier, self.name = query, timespan, max_records, min_tier, name

    def fetch(self, session, now: datetime) -> list[NewsItem]:
        r = http_get(session, self.URL, params={
            "query": self.query, "mode": "artlist", "format": "json", "timespan": self.timespan,
            "maxrecords": self.max_records, "sort": "datedesc",
        })
        try:
            data = r.json()
        except ValueError as e:  # GDELT répond en texte brut quand la requête est invalide
            raise ValueError(f"réponse GDELT invalide : {r.text[:200]}") from e
        items = []
        for a in data.get("articles", []):
            tier = domain_tier(a.get("domain") or a.get("url", ""))
            if tier > self.min_tier:
                continue
            try:
                when = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            items.append(NewsItem(source=a.get("domain", self.name), kind=NEWS, tier=tier,
                                  title=clean_text(a.get("title")), url=a.get("url", ""), published=when))
        return items


class FinnhubNewsSource:
    """Finnhub (clé gratuite sur finnhub.io) : flux de news forex et marchés."""

    URL = "https://finnhub.io/api/v1/news"

    def __init__(self, api_key: str, categories=("forex", "general"), name: str = "Finnhub"):
        self.api_key, self.categories, self.name = api_key, categories, name

    def fetch(self, session, now: datetime) -> list[NewsItem]:
        items = []
        for cat in self.categories:
            for a in http_get(session, self.URL, params={"category": cat, "token": self.api_key}).json():
                src = a.get("source") or self.name
                items.append(NewsItem(
                    source=src, kind=NEWS, tier=domain_tier(a.get("url", ""), default=domain_tier(src.lower(), 2)),
                    title=clean_text(a.get("headline")), url=a.get("url", ""),
                    published=datetime.fromtimestamp(int(a.get("datetime", 0)), timezone.utc),
                    summary=clean_text(a.get("summary"))[:1000],
                ))
        return items
