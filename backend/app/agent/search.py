"""Public web search provider layer. Isolated on purpose: no DB, no pipeline, no scoring.

Returns raw, normalized results only. Never raises: failures come back as SearchResponse.error.
Config: BRAVE_SEARCH_API_KEY (read at call time, never hardcoded, never included in errors).
"""
import html, json, os, re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

API_KEY_ENV = "BRAVE_SEARCH_API_KEY"
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

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

def _urllib_get(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as resp:
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

def _parse_date(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt

class BraveSearchProvider(SearchProvider):
    name = "brave"

    def __init__(self, api_key: str | None = None, http_get: HttpGet = _urllib_get, timeout: float = 8.0):
        self._api_key, self._http_get, self._timeout = api_key, http_get, timeout

    def search(self, query: str, count: int = 10, freshness: str | None = None) -> SearchResponse:
        """freshness: Brave values pd | pw | pm | py (past day/week/month/year), or None."""
        key = self._api_key or os.getenv(API_KEY_ENV)
        if not key:
            return SearchResponse(error=SearchError("not_configured", f"{API_KEY_ENV} is not set; web search is disabled."))
        if not query or not query.strip():
            return SearchResponse(error=SearchError("invalid_query", "Query is empty."))
        params = {"q": query, "count": max(1, min(count, 20))}
        if freshness:
            params["freshness"] = freshness
        try:
            status, body = self._http_get(f"{BRAVE_ENDPOINT}?{urlencode(params)}",
                                          {"Accept": "application/json", "X-Subscription-Token": key}, self._timeout)
        except Exception as e:
            return SearchResponse(error=SearchError("network_error", f"Search request failed: {type(e).__name__}"))
        if status != 200:
            return SearchResponse(error=SearchError("http_error", f"Search API returned HTTP {status}", status))
        try:
            items = json.loads(body)["web"]["results"]
            if not isinstance(items, list):
                raise TypeError
        except KeyError:
            items = []   # valid response with no web results (Brave omits "web" when nothing matches)
        except (ValueError, TypeError):
            return SearchResponse(error=SearchError("invalid_response", "Search API returned an unexpected response."))
        return SearchResponse(results=[r for r in map(self._normalize, items) if r])

    def _normalize(self, item) -> SearchResult | None:
        """Skips (returns None for) any result without a usable title and http(s) url."""
        if not isinstance(item, dict):
            return None
        url, title = item.get("url"), _clean(item.get("title"))
        if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https") or not title:
            return None
        return SearchResult(title, url, _clean(item.get("description")), _parse_date(item.get("page_age")), self.name)

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
    return bool(_POST_PATH.match(u.path))t