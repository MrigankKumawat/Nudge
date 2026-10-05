"""LinkedIn public-discovery adapter: search provider -> URL/recency filter -> relevance analyzer -> RawCandidate.

Only reads public web search results. It never contacts LinkedIn, logs in, scrapes, posts, or messages anyone.
Without BRAVE_SEARCH_API_KEY it quietly returns [] so the rest of the agent keeps working.
"""
import logging, os, re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse
from .adapters import DiscoveryAdapter, RawCandidate
from .search import SearchProvider, BraveSearchProvider
from .linkedin_discovery import LinkedInPost, discover_recent_posts
from .linkedin_analyzer import Analysis, analyze_post

log = logging.getLogger(__name__)

SOURCE = "linkedin:search"
DEFAULT_QUERIES = (
    '"flaky pytest" site:linkedin.com/posts',
    '"pytest" "GitHub Actions" "fails" site:linkedin.com/posts',
    '"flaky integration tests" site:linkedin.com/posts',
    '"test only fails in CI" site:linkedin.com/posts',
)

@dataclass
class LinkedInCandidate(RawCandidate):
    """A RawCandidate that also carries the analyzer's verdict, so the pipeline does not re-score it with its own rules."""
    analysis: Analysis | None = None

# ---- author attribution (all from the public URL / search title; nothing is fetched) -------------------------
_TITLE_PREFIX = re.compile(r"^(.{2,80}?)\s+on LinkedIn:\s*", re.I)

def author_slug(post_url: str) -> str | None:
    """Vanity name from /posts/<vanity>_<post-title-slug>-activity-<id>-<suffix>. /feed/update/... URLs carry no author."""
    m = re.match(r"^/posts/([^/_]+)_", urlparse(post_url).path)
    return m.group(1).lower() if m else None

def profile_url_for(slug: str) -> str:
    """Derived, unverified. Company-page authors would live under /company/, but a post URL cannot tell us which."""
    return f"https://www.linkedin.com/in/{slug}"

def humanize_slug(slug: str) -> str:
    parts = [p for p in slug.split("-") if p]
    if len(parts) > 1 and re.fullmatch(r"(?=.*\d)[0-9a-z]{6,10}", parts[-1]):   # LinkedIn's trailing id token, e.g. "1a2b3c4d"
        parts = parts[:-1]
    return " ".join(p.capitalize() for p in parts) or slug

def author_name(post: LinkedInPost, slug: str) -> str:
    m = _TITLE_PREFIX.match(post.title.strip())
    return m.group(1).strip() if m else humanize_slug(slug)

def post_text(post: LinkedInPost) -> str:
    """Title (minus the 'Name on LinkedIn:' prefix) and snippet joined without repeating each other."""
    body, snip = _TITLE_PREFIX.sub("", post.title.strip(), count=1).strip(), post.snippet.strip()
    stem = body.rstrip("\u2026. ").strip()
    if not snip: return body
    if not body or stem in snip: return snip   # the snippet already contains the (truncated) title text
    if snip in body: return body
    return f"{body} {snip}"

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def _default_freshness() -> str | None:
    # Brave's date filter ("pw" = past week) keeps the 10 result slots from filling with old posts. Set to "" to disable.
    return os.getenv("LINKEDIN_SEARCH_FRESHNESS", "pw") or None

class LinkedInDiscovery(DiscoveryAdapter):
    name = "linkedin"

    def __init__(self, provider: SearchProvider | None = None, queries=DEFAULT_QUERIES, results_per_query: int = 10,
                 freshness: str | None = ..., clock: Callable[[], datetime] = _utcnow):
        self.provider, self.queries, self.results_per_query, self.clock = provider, tuple(queries), results_per_query, clock
        self.freshness = _default_freshness() if freshness is ... else freshness

    def discover(self) -> list[RawCandidate]:
        try:
            return self._discover()
        except Exception:   # a broken source must never fail the whole agent run
            log.exception("LinkedIn discovery failed")
            return []

    def _discover(self) -> list[RawCandidate]:
        provider, now = self.provider or BraveSearchProvider(), self.clock()
        results = []
        for q in self.queries:
            resp = provider.search(q, count=self.results_per_query, freshness=self.freshness)
            if resp.error:
                if resp.error.code == "not_configured":
                    log.info("LinkedIn discovery skipped: %s", resp.error.message)
                    return []
                log.warning("LinkedIn search failed (%s): %s", resp.error.code, resp.error.message)
                continue
            results.extend(resp.results)

        best: dict[str, LinkedInCandidate] = {}   # one candidate per author: the pipeline keys leads by profile_url (unique)
        for post in discover_recent_posts(results, now=now).accepted:
            slug = author_slug(post.url)
            analysis = analyze_post(post)
            if slug is None or analysis.recommended_action == "IGNORE":
                continue
            cand = LinkedInCandidate(
                name=author_name(post, slug), profile_url=profile_url_for(slug), role="", company="", bio="", source=SOURCE,
                post_url=post.url, post_text=post_text(post), post_age_hours=max(0.0, (now - post.published_at).total_seconds() / 3600),
                analysis=analysis)
            cur = best.get(cand.profile_url)
            if cur is None or (analysis.score, -cand.post_age_hours) > (cur.analysis.score, -cur.post_age_hours):
                best[cand.profile_url] = cand
        return sorted(best.values(), key=lambda c: (-c.analysis.score, c.post_age_hours, c.post_url))