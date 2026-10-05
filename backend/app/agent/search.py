"""Public web search provider layer. Isolated on purpose: no DB, no pipeline, no scoring.

Returns raw, normalized results only. Never raises: failures come back as SearchResponse.error.
Config: SERPAPI_API_KEY (read at call time, never hardcoded, never included in errors).
"""
import html, json, os, re, ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
import certifi
from dotenv import load_dotenv

load_dotenv()   # picks up backend/.env so SERPAPI_API_KEY works without exporting it in the shell

API_KEY_ENV = "SERPAPI_API_KEY"   # the NAME of the env var; the key itself is read at call time in search()
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"

# Freshness codes used by callers (pd | pw | pm | py) -> Google "tbs" date filter.
_FRESHNESS_TO_TBS = {"pd": "qdr:d", "pw": "qdr:w", "pm": "qdr:m", "py": "qdr:y"}

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    published_at: datetime | None   # naive UTC (matches app.db.now()); None when the provider gives no date
    provider: str

@dataclass
class SearchError:
    code: str      # not_configured | invalid_query | http_error | network_error | invalid_response
    message: str
    status: int | None = None

@dataclass
class SearchResponse:
    results: list[SearchResult] = field(default_factory=list)
    error: SearchError | None = None
    @property
    def ok(self) -> bool:
        return self.error is None

# (url, headers, timeout) -> (status, body). Injectable so tests never touch the network.
HttpGet = Callable[[str, dict, float], tuple[int, bytes]]

# Verified TLS using certifi's CA bundle: some Python installs (notably on Windows) ship a stale/incomplete system CA store.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

def _urllib_get(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout, context=_SSL_CONTEXT) as resp:
            return resp.status, resp.read()
    except HTTPError as e:   # non-2xx still carries a status; let the caller classify it
        return e.code, e.read()

class SearchProvider:
    name = "base"
    def search(self, query: str, count: int = 10, freshness: str | None = None) -> SearchResponse:
        raise NotImplementedError

_TAGS = re.compile(r"<[^>]+>")
def _clean(text) -> str:
    return html.unescape(_TAGS.sub("", text)).strip() if isinstance(text, str) else ""

_ABS_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y")
_REL = re.compile(r"^(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago$", re.I)
_REL_SECONDS = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000}

def _parse_date(value) -> datetime | None:
    """Understands ISO strings, 'Mar 3, 2026' and relative forms like '2 days ago'. Returns naive UTC or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    m = _REL.match(value)
    if m:
        seconds = int(m.group(1)) * _REL_SECONDS[m.group(2).lower()]
        return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        pass
    for fmt in _ABS_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None

class SerpApiSearchProvider(SearchProvider):
    name = "serpapi"

    def __init__(self, api_key: str | None = None, http_get: HttpGet = _urllib_get, timeout: float = 30.0):
        self._api_key, self._http_get, self._timeout = api_key, http_get, timeout

    def search(self, query: str, count: int = 10, freshness: str | None = None) -> SearchResponse:
        """freshness: pd | pw | pm | py (past day/week/month/year), or None. Mapped to Google's tbs=qdr:* filter."""
        key = self._api_key or os.getenv(API_KEY_ENV)
        if not key:
            return SearchResponse(error=SearchError("not_configured", f"{API_KEY_ENV} is not set; web search is disabled."))
        if not query or not query.strip():
            return SearchResponse(error=SearchError("invalid_query", "Query is empty."))
        params = {"engine": "google", "q": query, "num": max(1, min(count, 20)), "api_key": key}
        tbs = _FRESHNESS_TO_TBS.get(freshness or "")
        if tbs:
            params["tbs"] = tbs
        url = f"{SERPAPI_ENDPOINT}?{urlencode(params)}"
        for attempt in (1, 2):   # an uncached Google query can be slow: retry once on a network error/timeout before giving up
            try:
                status, body = self._http_get(url, {"Accept": "application/json"}, self._timeout)
                break
            except Exception as e:   # never include str(e): urllib errors can echo the URL, which contains the API key
                reason = getattr(e, "reason", None)   # URLError wraps the real cause (timeout, SSL, DNS); its TYPE is safe to show
                what = type(e).__name__ + (f"({type(reason).__name__})" if reason is not None else "")
                if attempt == 2:
                    return SearchResponse(error=SearchError("network_error", f"Search request failed: {what}"))
        if status != 200:
            return SearchResponse(error=SearchError("http_error", f"Search API returned HTTP {status}", status))
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise TypeError
        except (ValueError, TypeError):
            return SearchResponse(error=SearchError("invalid_response", "Search API returned an unexpected response."))
        if data.get("error"):
            # SerpAPI reports "no results" as a 200 with an error string; anything else is a real failure.
            if "hasn't returned any results" in str(data["error"]).lower().replace("\u2019", "'"):
                return SearchResponse(results=[])
            return SearchResponse(error=SearchError("http_error", "Search API reported an error."))
        items = data.get("organic_results", [])   # key is omitted when nothing matches
        if not isinstance(items, list):
            return SearchResponse(error=SearchError("invalid_response", "Search API returned an unexpected response."))
        return SearchResponse(results=[r for r in map(self._normalize, items) if r])

    def _normalize(self, item) -> SearchResult | None:
        """Skips (returns None for) any result without a usable title and http(s) url."""
        if not isinstance(item, dict):
            return None
        url, title = item.get("link"), _clean(item.get("title"))
        if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https") or not title:
            return None
        return SearchResult(title, url, _clean(item.get("snippet")), _parse_date(item.get("date")), self.name)

# ---- URL helper ---------------------------------------------------------------
_POST_PATH = re.compile(r"^/(posts/[^/]+|feed/update/urn:li:(activity|share|ugcPost):\d+)/?$", re.I)

def is_linkedin_post_url(url: str) -> bool:
    """True if the URL looks like a public LinkedIn post. Pure string check; never fetches anything."""
    try:
        u = urlparse(url.strip())
    except (AttributeError, ValueError):
        return False
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return False
    return bool(_POST_PATH.match(u.path))