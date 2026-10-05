"""GitHub issue discovery adapter: GitHub REST search API -> duplicate/bot filter -> RawCandidate.

Read-only. The only request it makes is GET https://api.github.com/search/issues (public data): no HTML scraping, no browser automation,
no GitHub login, and it never comments, messages, reacts to or otherwise acts on GitHub.
GITHUB_TOKEN is optional (it only raises the API rate limit). It is sent solely in the Authorization header and is never logged, never put
in an error message and never included in summary().
No scoring and no drafting here: the pipeline's existing research/score/decide steps handle the candidates.
"""
import json, logging, os, ssl, time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import certifi
from dotenv import load_dotenv
from .adapters import DiscoveryAdapter, DiscoveryConfigError, DiscoveryError, RawCandidate
from .github_analyzer import Analysis, analyze_github_issue

load_dotenv()   # picks up backend/.env so GITHUB_TOKEN works without exporting it in the shell
log = logging.getLogger(__name__)

SOURCE = "github:issue"
ENDPOINT = "https://api.github.com/search/issues"
TOKEN_ENV = "GITHUB_TOKEN"   # the NAME of the env var; the token itself is read at call time
USER_AGENT = "Nudge-Discovery/0.1 (+https://github.com/MrigankKumawat/Nudge)"
WINDOW_DAYS = 21             # only issues updated within this many days
PER_PAGE = 30                # one page per query
POST_TEXT_MAX = 2000         # title + body is cut to this many characters on the RawCandidate (the full body stays on GitHubIssue)

# Search API limits: 10 requests/min unauthenticated, 30/min with a token. Pace requests so a full run stays under them.
MIN_INTERVAL_ANON, MIN_INTERVAL_TOKEN = 6.5, 2.2

# Each entry is the free-text part of the search; is:issue / is:open / updated:>= are appended per request.
QUERIES = (
    '"flaky test"',
    '"flaky tests"',
    '"intermittent test"',
    '"intermittently fails"',
    '"passes on retry"',
    '"passed on rerun"',
    '"fails in parallel"',
    '"passes individually"',
    '"pytest-xdist"',
    '"fails in CI"',
    '"CI only" test',
    '"random failure" test',
    '"cannot reproduce" test',
    'pytest flaky',
    'pytest intermittent',
    'Playwright flaky',
    'Cypress flaky',
    'Jest flaky',
)

BOT_MARKERS = ("dependabot", "renovate", "github-actions", "[bot]")   # substring match on the lower-cased login
DELETED_ACCOUNT = "ghost"                                             # GitHub's placeholder for deleted users: nobody to reach

# (url, headers, timeout) -> (status, lower-cased response headers, body). Injectable so tests never touch the network.
HttpGet = Callable[[str, dict, float], tuple[int, dict, bytes]]

# Verified TLS using certifi's CA bundle (same reason as search.py: some Windows Python installs ship a stale system CA store).
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

def _urllib_get(url: str, headers: dict, timeout: float) -> tuple[int, dict, bytes]:
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout, context=_SSL_CONTEXT) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except HTTPError as e:   # non-2xx still carries a status and headers; let the caller classify it
        return e.code, {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}, e.read()

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

@dataclass
class GitHubIssue:
    url: str                 # html_url of the issue
    repository: str          # "owner/repo"
    number: int
    title: str
    body: str                # full body, "" when empty
    author_login: str
    author_url: str          # https://github.com/<login>
    created_at: datetime     # timezone-aware UTC
    updated_at: datetime     # timezone-aware UTC
    state: str
    labels: list[str]
    comments: int
    search_query: str        # the query term that first found this issue

@dataclass
class GitHubCandidate(RawCandidate):
    """A RawCandidate that also carries the full issue record and analyzer verdict."""
    issue: GitHubIssue | None = None
    analysis: Analysis | None = None

# ---- parsing -------------------------------------------------------------------------------------------------
def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str): return None
    try: d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError: return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

def _repository(item: dict) -> str:
    """repository_url looks like https://api.github.com/repos/<owner>/<repo>."""
    url = item.get("repository_url") or ""
    return url.split("/repos/", 1)[1] if "/repos/" in url else ""

def is_bot(user: dict) -> bool:
    login = (user.get("login") or "").lower()
    return user.get("type") == "Bot" or any(m in login for m in BOT_MARKERS)

def parse_issue(item: dict, query: str) -> GitHubIssue | None:
    """None when the item is unusable (missing author/URL/dates) or is a pull request."""
    if not isinstance(item, dict) or "pull_request" in item: return None
    user = item.get("user") or {}
    login, url, repo, number = user.get("login"), item.get("html_url"), _repository(item), item.get("number")
    created, updated = _parse_ts(item.get("created_at")), _parse_ts(item.get("updated_at"))
    if not (login and url and repo and created and updated and isinstance(number, int)): return None
    return GitHubIssue(
        url=url, repository=repo, number=number, title=(item.get("title") or "").strip(), body=item.get("body") or "",
        author_login=login, author_url=user.get("html_url") or f"https://github.com/{login}", created_at=created, updated_at=updated,
        state=item.get("state") or "open", labels=[l["name"] for l in item.get("labels") or [] if isinstance(l, dict) and l.get("name")],
        comments=int(item.get("comments") or 0), search_query=query)

def _rate_limited(status: int, headers: dict, body: bytes) -> bool:
    if status == 429: return True
    return status == 403 and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers or b"rate limit" in body[:1000].lower())

def _message(body: bytes) -> str:
    try: return str(json.loads(body).get("message", ""))[:200]
    except (ValueError, AttributeError): return ""

# ---- adapter -------------------------------------------------------------------------------------------------
class GitHubIssueDiscovery(DiscoveryAdapter):
    name = "github"

    def __init__(self, token: str | None = None, queries=QUERIES, window_days: int = WINDOW_DAYS, per_page: int = PER_PAGE,
                 http_get: HttpGet = _urllib_get, clock: Callable[[], datetime] = _utcnow, sleep: Callable[[float], None] = time.sleep,
                 timeout: float = 30.0):
        self._token = token   # None -> GITHUB_TOKEN is read when discover() runs
        self.queries, self.window_days, self.per_page = tuple(queries), window_days, per_page
        self.http_get, self.clock, self.sleep, self.timeout = http_get, clock, sleep, timeout
        self.last_report: dict = {}   # funnel counts of the most recent run; never contains the token

    def summary(self) -> str:
        r = self.last_report
        if not r: return ""
        return (f"GitHub discovery: {r['queries']} queries ({r['failed']} failed{', rate limited' if r['rate_limited'] else ''}, "
                f"{'with' if r['authenticated'] else 'without'} token), {r['raw']} results, {r['invalid']} unusable, {r['bots']} bot/deleted authors skipped, "
                f"{r['unique']} unique issues, {r['candidates']} candidates (one per author)")

    def fetch_issues(self) -> list[GitHubIssue]:
        """Run every query, drop unusable items, bots and duplicate issue URLs. Newest update first."""
        token = (self._token if self._token is not None else os.getenv(TOKEN_ENV, "")).strip()
        cutoff = (self.clock() - timedelta(days=self.window_days)).strftime("%Y-%m-%d")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": USER_AGENT}
        if token: headers["Authorization"] = f"Bearer {token}"
        gap = MIN_INTERVAL_TOKEN if token else MIN_INTERVAL_ANON
        self.last_report = rep = dict(queries=len(self.queries), failed=0, rate_limited=False, authenticated=bool(token),
                                      raw=0, invalid=0, bots=0, unique=0, candidates=0)
        seen_urls: set[str] = set()
        kept: list[GitHubIssue] = []
        ok = 0
        for i, term in enumerate(self.queries):
            if i: self.sleep(gap)
            q = f"{term} is:issue is:open updated:>={cutoff}"
            url = f"{ENDPOINT}?{urlencode({'q': q, 'sort': 'updated', 'order': 'desc', 'per_page': self.per_page})}"
            try:
                status, resp_headers, body = self.http_get(url, headers, self.timeout)
            except OSError as e:   # URLError/timeout/SSL; URLError wraps the real cause, and its TYPE is safe to show
                log.warning("GitHub query failed (network: %s): %s", type(getattr(e, "reason", None) or e).__name__, term)
                continue
            if _rate_limited(status, resp_headers, body):
                rep["rate_limited"] = True
                log.warning("GitHub search rate limit reached after %d of %d queries; stopping this run", i, len(self.queries))
                break
            if status == 401 and token:
                raise DiscoveryConfigError("GITHUB_TOKEN was rejected by GitHub (HTTP 401); fix or remove it.")
            if status != 200:
                log.warning("GitHub query failed (HTTP %s %s): %s", status, _message(body), term)
                continue
            try: items = json.loads(body).get("items")
            except (ValueError, AttributeError): items = None
            if not isinstance(items, list):
                log.warning("GitHub query returned an unexpected payload: %s", term)
                continue
            ok += 1
            log.info("GitHub query returned %d results: %s", len(items), term)
            for item in items:
                rep["raw"] += 1
                issue = parse_issue(item, term)
                if issue is None:
                    rep["invalid"] += 1; continue
                if issue.url in seen_urls: continue            # dedupe by issue URL; the first query that found it is kept as search_query
                seen_urls.add(issue.url)
                user = item.get("user") or {}
                if is_bot(user) or issue.author_login.lower() == DELETED_ACCOUNT:
                    rep["bots"] += 1; continue
                kept.append(issue)
        rep["failed"], rep["unique"] = len(self.queries) - ok, len(kept)
        if self.queries and ok == 0:   # nothing succeeded: that is a failure, not "found nothing"
            raise DiscoveryError(f"GitHub search failed for all {len(self.queries)} queries" + (" (rate limited; set GITHUB_TOKEN for a higher limit)" if rep["rate_limited"] else "")
                                 + "; check network and GITHUB_TOKEN.")
        return sorted(kept, key=lambda x: (-x.updated_at.timestamp(), x.url))

    def discover(self) -> list[RawCandidate]:
        try:
            issues = self.fetch_issues()
            now = self.clock()
            
            best: dict[str, GitHubCandidate] = {}   # one candidate per author: the pipeline keys leads by profile_url (unique)
            for issue in issues:                    # newest update first, so the first issue seen per author is that author's most recent
                if issue.author_url in best: continue
                text = f"{issue.title}\n\n{issue.body}".strip()[:POST_TEXT_MAX]
                analysis = analyze_github_issue(issue, now=now)
                best[issue.author_url] = GitHubCandidate(
                    name=issue.author_login[:120], profile_url=issue.author_url, role="", company=issue.repository[:120], bio="", source=SOURCE,
                    post_url=issue.url, post_text=text, post_age_hours=max(0.0, (now - issue.updated_at).total_seconds() / 3600), issue=issue,
                    analysis=analysis)
            self.last_report["candidates"] = len(best)
            log.info(self.summary())
            return list(best.values())
        except DiscoveryError:   # missing/invalid configuration or every query failed: surface as a FAILED run, not as "0 results"
            raise
        except Exception as e:   # an unexpected bug in the real source must not look like an empty successful run either
            log.exception("GitHub discovery crashed")
            raise DiscoveryError(f"GitHub discovery crashed ({type(e).__name__}); see the server log.") from None