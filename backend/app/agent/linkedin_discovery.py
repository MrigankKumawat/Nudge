"""Turns normalized SearchResults into recent public LinkedIn post candidates. Pure functions: no I/O, no DB, no scoring.

A post is accepted only with positive evidence that it was published within the last 7 days. We never guess a date:
unknown or implausible dates are rejected with an explicit reason, not defaulted to "now".
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from .search import SearchResult, is_linkedin_post_url

MAX_AGE = timedelta(days=7)          # inclusive: a post exactly 7 days old is still accepted
FUTURE_TOLERANCE = timedelta(hours=1)  # clock/timezone skew; anything later than this is treated as bad data
_ID_RE = re.compile(r"(activity|share|ugcpost)[-:](\d{15,})", re.I)
_EARLIEST = datetime(2003, 1, 1)     # LinkedIn launched in 2003; earlier decoded ids are garbage

@dataclass
class LinkedInPost:
    url: str                    # canonical form (see normalize_linkedin_url)
    original_url: str
    post_id: str | None         # e.g. "activity:7123..." when the URL carries one
    title: str
    snippet: str
    published_at: datetime      # naive UTC, always backed by evidence
    date_source: str            # "search" (provider's date) | "linkedin_id" (decoded from the activity id in the URL)
    provider: str

@dataclass
class Rejection:
    url: str
    reason: str                 # not_linkedin_post | duplicate | unknown_date | future_date | too_old
    published_at: datetime | None = None

@dataclass
class DiscoveryReport:
    accepted: list[LinkedInPost]
    rejected: list[Rejection]

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt

# ---- URL handling -------------------------------------------------------------
def normalize_linkedin_url(url: str) -> str | None:
    """Canonical form of a public LinkedIn post URL, or None if it isn't one.
    https, www.linkedin.com (country subdomains collapsed), no query string/fragment/trailing slash."""
    if not is_linkedin_post_url(url):
        return None
    return "https://www.linkedin.com" + urlparse(url.strip()).path.rstrip("/")

def extract_post_id(url: str) -> str | None:
    """'activity:<id>' / 'ugcpost:<id>' / 'share:<id>' from a post URL. The same post can appear as a /posts/<slug> URL
    and a /feed/update/urn:li:activity:<id> URL; the id is what identifies it."""
    m = _ID_RE.search(urlparse(url).path)
    return f"{m.group(1).lower()}:{m.group(2)}" if m else None

def date_from_activity_id(post_id: str | None, now: datetime) -> datetime | None:
    """LinkedIn activity ids encode the creation time: the top bits are milliseconds since the Unix epoch (id >> 22).
    That is evidence from the URL itself, not a guess. Returns None for other id kinds or implausible values."""
    if not post_id or not post_id.startswith("activity:"):
        return None
    try:
        dt = datetime(1970, 1, 1) + timedelta(milliseconds=int(post_id.split(":", 1)[1]) >> 22)
    except (ValueError, OverflowError):
        return None
    return dt if _EARLIEST <= dt <= now + FUTURE_TOLERANCE else None

# ---- filtering ----------------------------------------------------------------
def discover_recent_posts(results: list[SearchResult], now: datetime | None = None, max_age: timedelta = MAX_AGE) -> DiscoveryReport:
    """Order-preserving and deterministic for a given `now`. Every input result ends up in accepted or rejected."""
    now = _naive_utc(now) if now else _utcnow()
    rejected: list[Rejection] = []
    groups: dict[str, list[SearchResult]] = {}   # dedupe key -> results for the same post (dicts keep first-seen order)
    for r in results:
        norm = normalize_linkedin_url(r.url)
        if norm is None:
            rejected.append(Rejection(r.url, "not_linkedin_post"))
            continue
        key = extract_post_id(norm) or norm.lower()
        if key in groups:
            rejected.append(Rejection(r.url, "duplicate"))
        groups.setdefault(key, []).append(r)

    accepted: list[LinkedInPost] = []
    for key, group in groups.items():
        first = group[0]
        norm = normalize_linkedin_url(first.url)
        post_id = extract_post_id(norm)
        # All evidence for this post. When sources disagree we take the EARLIEST date: a search engine's date can be a
        # re-crawl date (later than the real post), but no source can be earlier than the post actually is.
        evidence = [(_naive_utc(r.published_at), "search") for r in group if r.published_at]
        if (d := date_from_activity_id(post_id, now)):
            evidence.append((d, "linkedin_id"))
        if not evidence:
            rejected.append(Rejection(first.url, "unknown_date"))
            continue
        published, source = min(evidence, key=lambda e: (e[0], e[1] != "linkedin_id"))
        if published > now + FUTURE_TOLERANCE:
            rejected.append(Rejection(first.url, "future_date", published))
        elif now - published > max_age:
            rejected.append(Rejection(first.url, "too_old", published))
        else:
            accepted.append(LinkedInPost(norm, first.url, post_id, first.title, first.snippet, published, source, first.provider))
    return DiscoveryReport(accepted, rejected)