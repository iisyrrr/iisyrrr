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
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from .assets import symbol_assets
from .calendar import covers_now
from .models import NEWS, OFFICIAL, SOCIAL, CalendarEvent, NewsItem

log = logging.getLogger(__name__)

# Ne jamais utiliser un « Mozilla/5.0 » nu : BLS et investingLive le bloquent.
# Un robot identifié (avec un contact) est accepté partout.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TradingBot-News/1.0"
TIMEOUT = 20
MAX_BYTES = 5_000_000  # on refuse les réponses anormalement grosses
OLDEST_VALID = datetime(2000, 1, 1, tzinfo=timezone.utc)  # certains flux contiennent des dates en 1899


def set_contact(contact: str) -> None:
    """Ajoute un contact au User-Agent (demandé par certains sites officiels comme le BLS)."""
    global USER_AGENT
    base = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TradingBot-News/1.0"
    USER_AGENT = f"{base} (+{contact})" if contact else base

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


def clean_text(text: str | None, limit: int = 5000) -> str:
    # on tronque AVANT le regex (un texte géant ne doit pas bloquer le robot) ;
    # « [^<>]* » évite tout retour arrière coûteux sur un « < » orphelin
    text = re.sub(r"<[^<>]*>", " ", (text or "")[:limit])
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def clean_url(url: str) -> str:
    """URL canonique : pas de paramètres de pistage, pas de double « / », https."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlparse(url)
    path = re.sub(r"/{2,}", "/", parts.path)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.lower().startswith(("utm_", "xy"))])
    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme
    return urlunparse((scheme, parts.netloc.lower(), path, parts.params, query, ""))


def http_get(session, url: str, **kwargs):
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    r = session.get(url, headers=headers, timeout=kwargs.pop("timeout", TIMEOUT), **kwargs)
    r.raise_for_status()
    if len(r.content) > MAX_BYTES:
        raise ValueError(f"réponse trop volumineuse ({len(r.content)} octets)")
    return r


def valid_time(when: datetime, now: datetime) -> bool:
    return OLDEST_VALID <= when <= now + timedelta(minutes=5)


class Source:
    """Réglages communs : nom, groupe propriétaire, fréquence de collecte."""

    name = "source"
    group = ""
    poll_seconds: float | None = None  # None = fréquence par défaut du service


def _compile(patterns) -> re.Pattern | None:
    patterns = [p for p in (patterns or []) if p]
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE) if patterns else None


# ======================================================================== RSS / Atom
class RssSource(Source):
    def __init__(self, name: str, url: str, kind: str = NEWS, tier: int = 2, exclude=None, include=None,
                 max_items: int = 50, assets=None, strip_prefix: str = "", strip_title=None, group: str = "",
                 poll_seconds: float | None = None, headers: dict | None = None):
        self.name, self.url, self.kind, self.tier = name, url, kind, tier
        self.group = group or name
        self.poll_seconds = poll_seconds
        self.headers = headers or {}
        patterns = list(strip_title or []) + ([re.escape(strip_prefix)] if strip_prefix else [])
        self.strip_title = re.compile(r"^\s*(?:" + "|".join(patterns) + r")\s*", re.IGNORECASE) if patterns else None
        self.exclude, self.include = _compile(exclude), _compile(include)
        self.max_items = max_items
        self.assets = set(assets or [])  # actifs imposés (ex : flux « or » -> XAU)
        self._etag = self._modified = None
        self._last: list[NewsItem] = []

    def fetch(self, session, now: datetime) -> list[NewsItem]:
        import feedparser

        headers = dict(self.headers)
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._modified:
            headers["If-Modified-Since"] = self._modified
        r = http_get(session, self.url, headers=headers)
        if r.status_code == 304:  # rien de neuf : on refiltre la dernière version avec l'heure actuelle
            return [i for i in self._last if valid_time(i.published, now)]
        feed = feedparser.parse(r.content)
        if feed.bozo and not feed.entries:
            raise ValueError(f"flux illisible : {feed.bozo_exception}")
        items = []
        for e in feed.entries[: self.max_items]:
            stamp = e.get("published_parsed") or e.get("updated_parsed")
            if not stamp:
                continue  # sans date on ne peut pas juger la fraîcheur
            try:
                published = datetime(*stamp[:6], tzinfo=timezone.utc)
            except (ValueError, OverflowError, TypeError):
                continue  # date sentinelle (an 0 ou 10000) : on ignore juste cette entrée
            if published < OLDEST_VALID:
                continue  # date absurde (1899)
            title = clean_text(e.get("title"))
            if self.strip_title:
                title = self.strip_title.sub("", title).strip()
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
            assets = set(self.assets)
            pair = next((v for k, v in e.items() if k.endswith("_pair") and isinstance(v, str) and v.strip()), "")
            if pair:  # ex : FXStreet indique directement la paire concernée
                assets |= symbol_assets(pair.strip())
            items.append(NewsItem(
                source=self.name, kind=self.kind, tier=self.tier, title=title, url=clean_url(e.get("link", "")),
                published=published, summary=summary, assets=assets, group=self.group,
            ))
        self._etag = r.headers.get("ETag")
        self._modified = r.headers.get("Last-Modified")
        self._last = items
        # les entrées datées dans le futur (agendas) sont écartées maintenant, mais restent en cache
        return [i for i in items if valid_time(i.published, now)]


class SitemapNewsSource(Source):
    """Plan de site « Google News » (ex : Reuters, qui n'a plus de flux RSS depuis 2020).
    Contient les titres de la dernière heure, sans résumé."""

    def __init__(self, name: str, url: str, tier: int = 1, url_include=None, url_exclude=None,
                 kind: str = NEWS, group: str = "", poll_seconds: float | None = None):
        self.name, self.url, self.tier, self.kind = name, url, tier, kind
        self.group = group or name
        self.poll_seconds = poll_seconds
        self.url_include, self.url_exclude = _compile(url_include), _compile(url_exclude)

    def fetch(self, session, now: datetime) -> list[NewsItem]:
        import xml.etree.ElementTree as ET

        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9",
              "n": "http://www.google.com/schemas/sitemap-news/0.9"}
        content = http_get(session, self.url).content
        if b"<!DOCTYPE" in content[:2000] or b"<!ENTITY" in content[:5000]:
            raise ValueError("plan de site refusé : déclaration DTD/entités")  # protection « billion laughs »
        root = ET.fromstring(content)
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
            if title and valid_time(when, now):
                items.append(NewsItem(source=self.name, kind=self.kind, tier=self.tier, title=title,
                                      url=clean_url(loc), published=when, group=self.group))
        return items


# ======================================================================== calendrier
class ForexFactoryCalendar(Source):
    """Export hebdomadaire public de ForexFactory (faireconomy). Le site limite à
    2 téléchargements par 5 minutes et renvoie sinon une page HTML « Request Denied » :
    on garde une copie locale, rafraîchie au plus une fois par `min_interval_minutes`.
    Une copie de plus de `max_stale_hours` n'est JAMAIS utilisée : mieux vaut signaler
    l'absence de calendrier que croire qu'il n'y a aucune annonce."""

    name = "ForexFactory (calendrier)"
    URLS = ("https://nfs.faireconomy.media/ff_calendar_thisweek.json",)

    def __init__(self, store, min_interval_minutes: int = 60, urls=None, max_stale_hours: float = 26):
        self.store = store
        self.group = self.name
        self.min_interval = timedelta(minutes=min_interval_minutes)
        self.max_stale = timedelta(hours=max_stale_hours)
        self.urls = tuple(urls or self.URLS)
        self.data_time: datetime | None = None  # date de téléchargement des données servies

    def _payload(self, session, url: str, now: datetime) -> tuple[str, datetime]:
        key = f"calendar:{url}"
        cached = self.store.get(key)
        cached_at = datetime.fromisoformat(cached[1]) if cached else None
        if cached and now - cached_at < self.min_interval:
            return cached[0], cached_at
        try:
            text = http_get(session, url).text
            if not text.lstrip().startswith("["):
                raise ValueError("réponse non JSON (limite de téléchargement atteinte ?)")
            json.loads(text)
        except Exception as e:
            if cached and now - cached_at <= self.max_stale:
                log.warning("Calendrier %s indisponible (%s), copie locale de %s utilisée", url, e,
                            cached_at.strftime("%d/%m %H:%M"))
                return cached[0], cached_at
            raise RuntimeError("calendrier indisponible" + (" et copie locale périmée" if cached else "")) from e
        self.store.put(key, text, now)
        return text, now

    @staticmethod
    def parse(payload: str) -> list[CalendarEvent]:
        events = []
        for e in json.loads(payload):
            try:
                when = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
            except (KeyError, ValueError, TypeError):
                continue
            events.append(CalendarEvent(
                title=clean_text(e.get("title"), 200), currency=clean_text(e.get("country"), 8).upper(), time=when,
                impact=clean_text(e.get("impact"), 20), forecast=clean_text(e.get("forecast"), 40),
                previous=clean_text(e.get("previous"), 40), actual=clean_text(e.get("actual"), 40),
            ))
        return events

    def fetch(self, session, now: datetime) -> list[CalendarEvent]:
        events: dict[str, CalendarEvent] = {}
        errors, oldest = [], None
        for url in self.urls:
            try:
                payload, stamp = self._payload(session, url, now)
                for ev in self.parse(payload):
                    events[ev.id] = ev
                oldest = stamp if oldest is None else min(oldest, stamp)
            except Exception as e:
                errors.append(e)
        if errors:
            raise errors[0]  # une adresse configurée en panne sans copie utilisable = calendrier incomplet
        if not covers_now(list(events.values()), now):
            raise RuntimeError("calendrier périmé : il ne couvre pas la semaine en cours")
        self.data_time = oldest
        return sorted(events.values(), key=lambda e: e.time)


# ======================================================================== réseaux sociaux
class StockTwitsSource(Source):
    """Flux public StockTwits par symbole (sans clé). Bruyant : uniquement des
    posts d'utilisateurs suivis, et toujours traités comme des rumeurs à confirmer."""

    URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"

    def __init__(self, symbols: dict[str, list[str]], name: str = "StockTwits", poll_seconds: float | None = 900):
        self.symbols = symbols  # symbole StockTwits -> actifs (ex : {"SPY": ["US_INDICES"]})
        self.name = self.group = name
        self.poll_seconds = poll_seconds  # limite : 200 requêtes / heure / IP

    def fetch(self, session, now: datetime) -> list[NewsItem]:
        items = []
        for symbol, assets in self.symbols.items():
            data = http_get(session, self.URL.format(symbol=quote(symbol))).json()
            for m in data.get("messages", []):
                try:
                    created = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00"))
                except (KeyError, ValueError, AttributeError):
                    continue
                user = m.get("user") or {}
                sentiment = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
                items.append(NewsItem(
                    source=f"{self.name} @{user.get('username', '?')}", kind=SOCIAL,
                    tier=3, title=clean_text(m.get("body"))[:280],
                    url=f"https://stocktwits.com/{user.get('username', '')}/message/{m.get('id')}",
                    published=created,
                    engagement={"likes": int((m.get("likes") or {}).get("total", 0)),
                                "followers": int(user.get("followers", 0))},
                    author=user.get("username", ""), assets=set(assets), group=f"{self.name} @{user.get('username', '?')}",
                    summary=f"Sentiment déclaré : {sentiment}" if sentiment else "",
                ))
        return items


class RedditSource(Source):
    """Reddit. Avec une appli « script » (client_id/secret gratuits sur
    reddit.com/prefs/apps) on passe par l'API officielle OAuth ; sinon on tente
    les flux JSON publics, souvent bloqués."""

    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

    def __init__(self, subreddits: list[str], client_id: str = "", client_secret: str = "",
                 user_agent: str = "windows:tradingbot-news:1.0 (by /u/unknown)", listing: str = "hot",
                 limit: int = 25, name: str = "Reddit"):
        self.subreddits, self.listing, self.limit, self.name = subreddits, listing, limit, name
        self.group = name
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


class XSource(Source):
    """X (Twitter) – API payante : recherche récente sur une liste de comptes."""

    URL = "https://api.x.com/2/tweets/search/recent"

    def __init__(self, bearer_token: str, accounts: list[str], official_accounts=(), name: str = "X"):
        self.token, self.accounts, self.name = bearer_token, accounts, name
        self.group = name
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
                try:
                    created = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
                except (KeyError, ValueError, AttributeError):
                    continue
                u = users.get(t.get("author_id"), {})
                name = u.get("username", "?")
                official = name.lower() in self.official
                metrics = t.get("public_metrics", {})
                items.append(NewsItem(
                    source=f"X @{name}", kind=OFFICIAL if official else SOCIAL, tier=1 if official else 3,
                    title=clean_text(t.get("text"))[:280], url=f"https://x.com/{name}/status/{t['id']}",
                    published=created,
                    engagement={"likes": int(metrics.get("like_count", 0)),
                                "followers": int(u.get("public_metrics", {}).get("followers_count", 0))},
                    author=name, group=f"X @{name}",
                ))
        return items


class BlueskyAuthorSource(Source):
    """Comptes Bluesky vérifiés par leur nom de domaine (reuters.com, apnews.com,
    federalreserve.gov…) : impossibles à usurper, gratuits, sans clé."""

    URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"

    def __init__(self, accounts: list[dict], name: str = "Bluesky", poll_seconds: float | None = None):
        # accounts : [{"handle": "apnews.com", "name": "AP", "tier": 1, "kind": "news", "group": "AP"}]
        self.accounts, self.name, self.group, self.poll_seconds = accounts, name, name, poll_seconds

    def fetch(self, session, now: datetime) -> list[NewsItem]:
        items, errors = [], []
        for acc in self.accounts:
            try:
                data = http_get(session, self.URL, params={"actor": acc["handle"], "limit": 30,
                                                           "filter": "posts_no_replies"}).json()
            except Exception as e:
                errors.append(e)
                continue
            for entry in data.get("feed", []):
                if entry.get("reason"):  # repost d'un autre compte
                    continue
                post = entry.get("post") or {}
                record = post.get("record") or {}
                try:
                    when = datetime.fromisoformat(record["createdAt"].replace("Z", "+00:00")).astimezone(timezone.utc)
                except (KeyError, ValueError):
                    continue
                if not valid_time(when, now):
                    continue
                ext = (post.get("embed") or {}).get("external") or {}
                text = clean_text(record.get("text"))
                rkey = post.get("uri", "").rsplit("/", 1)[-1]
                label = acc.get("name") or acc["handle"]
                items.append(NewsItem(
                    source=f"{label} (Bluesky)", kind=acc.get("kind", NEWS), tier=int(acc.get("tier", 1)),
                    title=clean_text(ext.get("title")) or text[:280],
                    summary=clean_text(ext.get("description")) or (text if ext.get("title") else ""),
                    url=f"https://bsky.app/profile/{acc['handle']}/post/{rkey}", published=when,
                    engagement={"likes": int(post.get("likeCount", 0)), "reposts": int(post.get("repostCount", 0))},
                    author=acc["handle"], group=acc.get("group") or label, assets=set(acc.get("assets", [])),
                ))
        if errors and not items:
            raise errors[0]
        return items


# ======================================================================== API de news
class GdeltSource(Source):
    """GDELT DOC 2.0 : moteur mondial gratuit, sans clé. La fiabilité de chaque
    article est déduite de son domaine (Reuters, Bloomberg… = tier 1)."""

    URL = "https://api.gdeltproject.org/api/v2/doc/doc"

    def __init__(self, query: str, timespan: str = "2h", max_records: int = 75, min_tier: int = 2,
                 name: str = "GDELT"):
        self.query, self.timespan, self.max_records, self.min_tier, self.name = query, timespan, max_records, min_tier, name
        self.group = name

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


class FinnhubNewsSource(Source):
    """Finnhub (clé gratuite sur finnhub.io) : flux de news forex et marchés."""

    URL = "https://finnhub.io/api/v1/news"

    def __init__(self, api_key: str, categories=("forex", "general"), name: str = "Finnhub"):
        self.api_key, self.categories, self.name = api_key, categories, name
        self.group = name

    def fetch(self, session, now: datetime) -> list[NewsItem]:
        items = []
        for cat in self.categories:
            # clé dans un en-tête et non dans l'URL : elle n'apparaît pas dans les messages d'erreur
            for a in http_get(session, self.URL, params={"category": cat},
                              headers={"X-Finnhub-Token": self.api_key}).json():
                src = a.get("source") or self.name
                items.append(NewsItem(
                    source=src, kind=NEWS, tier=domain_tier(a.get("url", ""), default=domain_tier(src.lower(), 2)),
                    title=clean_text(a.get("headline")), url=a.get("url", ""),
                    published=datetime.fromtimestamp(int(a.get("datetime", 0)), timezone.utc),
                    summary=clean_text(a.get("summary"))[:1000],
                ))
        return items
