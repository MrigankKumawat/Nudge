"""LinkedIn public-discovery adapter: search provider -> URL/recency filter -> relevance analyzer -> RawCandidate.

Only reads public web search results. It never contacts LinkedIn, logs in, scrapes, posts, or messages anyone.
A missing SERPAPI_API_KEY raises DiscoveryConfigError (the run fails visibly); a successful search with no usable posts returns [].
"""
import logging, os, re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse
from .adapters import DiscoveryAdapter, DiscoveryConfigError, DiscoveryError, RawCandidate
from .search import SearchProvider, SerpApiSearchProvider
from .linkedin_discovery import MAX_AGE, LinkedInPost, discover_recent_posts
from .linkedin_analyzer import Analysis, analyze_post

log = logging.getLogger(__name__)

SOURCE = "linkedin:search"
# Chosen from live yield probing (diagnose_discovery.py --probe pm). Rare exact phrases such as "flaky pytest" or "pytest failing CI" return
# almost no LinkedIn posts (Google falls back to pypi/blog pages); these four returned 4-10 public posts each, with COMMENT/DM-worthy ones.
# Relevance is still decided by linkedin_analyzer, not by Google.
DEFAULT_QUERIES = (
    '"flaky test" CI site:linkedin.com/posts',
    'flaky playwright OR cypress OR selenium tests site:linkedin.com/posts',
    'flaky tests site:linkedin.com/posts',
    'tests randomly failing CI site:linkedin.com/posts',
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
    # Google's date filter via SerpApi ("pw" = past week) keeps the 10 result slots from filling with old posts. Set to "" to disable.
    days = MAX_AGE.days   # Google's own filter must be at least as wide as our window, or it hides posts we would accept
    return os.getenv("LINKEDIN_SEARCH_FRESHNESS", "pw" if days <= 7 else "pm" if days <= 31 else "py") or None

class LinkedInDiscovery(DiscoveryAdapter):
    name = "linkedin"

    def __init__(self, provider: SearchProvider | None = None, queries=DEFAULT_QUERIES, results_per_query: int = 10,
                 freshness: str | None = ..., clock: Callable[[], datetime] = _utcnow):
        self.provider, self.queries, self.results_per_query, self.clock = provider, tuple(queries), results_per_query, clock
        self.freshness = _default_freshness() if freshness is ... else freshness
        self.last_report: dict = {}   # funnel counts of the most recent discover(); never contains the API key

    def summary(self) -> str:
        r = self.last_report
        if not r: return ""
        rej = ", ".join(f"{k} {v}" for k, v in sorted(r["rejected"].items())) or "none"
        return (f"LinkedIn discovery: {r['queries']} queries ({r['failed']} failed), {r['raw']} results ({r['raw'] - r['linkedin_urls']} not public LinkedIn posts), "
                f"{r['accepted']} dated within {MAX_AGE.days} days (rejected: {rej}), {r['analyzer_ignored']} not relevant, {r['no_author']} without author, {r['candidates']} candidates")

    def discover(self) -> list[RawCandidate]:
        try:
            return self._discover()
        except DiscoveryError:   # missing configuration / every search failed: surface as a FAILED run, not as "0 results"
            raise
        except Exception as e:   # an unexpected bug in the real source must not look like an empty successful run either
            log.exception("LinkedIn discovery crashed")
            raise DiscoveryError(f"LinkedIn discovery crashed ({type(e).__name__}); see the server log.") from None

    def _discover(self) -> list[RawCandidate]:
        provider, now = self.provider or SerpApiSearchProvider(), self.clock()
        results, failed = [], []
        self.last_report = rep = dict(queries=len(self.queries), failed=0, raw=0, linkedin_urls=0, accepted=0, rejected={}, analyzer_ignored=0, no_author=0, candidates=0)
        for q in self.queries:
            resp = provider.search(q, count=self.results_per_query, freshness=self.freshness)
            if resp.error:
                if resp.error.code == "not_configured":
                    raise DiscoveryConfigError("SERPAPI_API_KEY is not configured; LinkedIn discovery cannot run.")
                log.warning("LinkedIn query failed (%s): %s | %s", resp.error.code, resp.error.message, q)
                failed.append(resp.error.code)
                continue
            log.info("LinkedIn query returned %d results: %s", len(resp.results), q)
            results.extend(resp.results)
        rep["failed"], rep["raw"] = len(failed), len(results)
        if self.queries and len(failed) == len(self.queries):   # every query errored: that is a failure, not "found nothing"
            raise DiscoveryError(f"SerpApi search failed for all {len(failed)} queries ({', '.join(sorted(set(failed)))}); check network, SERPAPI_API_KEY and credits.")

        best: dict[str, LinkedInCandidate] = {}   # one candidate per author: the pipeline keys leads by profile_url (unique)
        report = discover_recent_posts(results, now=now)
        rejected = Counter(x.reason for x in report.rejected)
        rep.update(linkedin_urls=len(results) - rejected["not_linkedin_post"], accepted=len(report.accepted),
                   rejected={k: v for k, v in rejected.items() if k != "not_linkedin_post"})
        for post in report.accepted:
            slug = author_slug(post.url)
            analysis = analyze_post(post)
            if slug is None:
                rep["no_author"] += 1; log.info("LinkedIn post has no author slug in its URL: %s", post.url); continue
            if analysis.recommended_action == "IGNORE":
                rep["analyzer_ignored"] += 1; log.info("LinkedIn post ignored by analyzer (%s/100, %s): %s", analysis.score, analysis.why, post.url); continue
            cand = LinkedInCandidate(
                name=author_name(post, slug), profile_url=profile_url_for(slug), role="", company="", bio="", source=SOURCE,
                post_url=post.url, post_text=post_text(post), post_age_hours=max(0.0, (now - post.published_at).total_seconds() / 3600),
                analysis=analysis)
            cur = best.get(cand.profile_url)
            if cur is None or (analysis.score, -cand.post_age_hours) > (cur.analysis.score, -cur.post_age_hours):
                best[cand.profile_url] = cand
        rep["candidates"] = len(best)
        log.info(self.summary())
        return sorted(best.values(), key=lambda c: (-c.analysis.score, c.post_age_hours, c.post_url))