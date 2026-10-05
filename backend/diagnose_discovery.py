"""Dry run of the REAL discovery path, stage by stage. Writes nothing to the DB and never contacts LinkedIn.
SerpApi -> Google results -> public-post URL check -> recency/date evidence -> relevance analyzer -> candidates.
Usage (from backend/, with SERPAPI_API_KEY in backend/.env):
  python diagnose_discovery.py           # the real queries (1 SerpApi credit per query)
  python diagnose_discovery.py --probe [pw|pm]   # compare 8 candidate queries by real yield (8 credits)
The API key is never printed."""
import json, sys
from collections import Counter
from urllib.parse import urlparse
from app.agent.search import SerpApiSearchProvider, _urllib_get, is_linkedin_post_url
from app.agent.linkedin_adapter import LinkedInDiscovery, DEFAULT_QUERIES, author_slug
from app.agent.linkedin_discovery import discover_recent_posts
from app.agent.linkedin_analyzer import analyze_post
from app.agent.adapters import DiscoveryError

class Recorder(SerpApiSearchProvider):
    """Same provider, but remembers each query's results, the raw `date` SerpApi sent per link, and Google's own view of the query."""
    def __init__(self):
        super().__init__(http_get=self._capture); self.calls, self.raw_dates, self.last = [], {}, {}
    def _capture(self, url, headers, timeout):
        status, body = _urllib_get(url, headers, timeout)
        try: self.last = json.loads(body)
        except ValueError: self.last = {}
        return status, body
    def search(self, query, count=10, freshness=None):
        resp = super().search(query, count, freshness); self.calls.append((query, resp, self.last)); return resp
    def _normalize(self, item):
        if isinstance(item, dict): self.raw_dates[item.get("link")] = item.get("date")
        return super()._normalize(item)

def google_view(body: dict) -> str:
    """What Google actually searched: the displayed query and result state show when it silently relaxed or dropped `site:`."""
    info, meta = body.get("search_information") or {}, body.get("search_metadata") or {}
    return (f"displayed={info.get('query_displayed')!r} state={info.get('organic_results_state')!r} total={info.get('total_results')} "
            f"google_url={meta.get('google_url')}")

rec = Recorder()
PROBE_QUERIES = [   # candidate queries to compare by real yield; edit freely
    'flaky tests site:linkedin.com/posts',
    '"flaky tests" site:linkedin.com/posts',
    '"flaky test" CI site:linkedin.com/posts',
    'intermittent test failures CI site:linkedin.com/posts',
    'tests pass locally fail in CI site:linkedin.com/posts',
    'tests randomly failing CI site:linkedin.com/posts',
    'pytest failing CI site:linkedin.com/posts',
    'flaky playwright OR cypress OR selenium tests site:linkedin.com/posts',
]
if "--probe" in sys.argv:
    fresh = next((a for a in sys.argv[1:] if a in ("pd", "pw", "pm", "py")), "pw")
    print(f"yield of {len(PROBE_QUERIES)} candidate queries, date filter {fresh!r} (the local recency window, LINKEDIN_MAX_AGE_DAYS, is still applied)\n")
    for query in PROBE_QUERIES:
        resp = rec.search(query, 10, fresh)
        if resp.error: print(f"ERROR {resp.error.code}: {query}\n"); continue
        acc = discover_recent_posts(resp.results).accepted
        verdicts = [(analyze_post(p), p) for p in acc]
        acts = Counter(a.recommended_action for a, _ in verdicts)
        print(f"{query}\n  {len(resp.results)} results | {sum(is_linkedin_post_url(r.url) for r in resp.results)} LinkedIn posts | {len(acc)} within window"
              f" | COMMENT {acts['COMMENT']}  DM {acts['DM']}  IGNORE {acts['IGNORE']}   (google total={(rec.last.get('search_information') or {}).get('total_results')})")
        for a, p in sorted((v for v in verdicts if v[0].recommended_action != "IGNORE"), key=lambda v: -v[0].score)[:3]:
            print(f"    {a.recommended_action} {a.score}/100 {urlparse(p.url).path[:60]}  [{', '.join(a.signals[:3])}]")
        print()
    raise SystemExit(0)

disc = LinkedInDiscovery(provider=rec)
print(f"freshness filter: {disc.freshness!r}   queries: {len(DEFAULT_QUERIES)}\n")
try:
    cands = disc.discover()
except DiscoveryError as e:
    print("DISCOVERY FAILED:", e); raise SystemExit(1)

allres = []
for q, resp, body in rec.calls:
    print(f"[{'ERROR ' + resp.error.code + ': ' + resp.error.message if resp.error else str(len(resp.results)) + ' results'}] {q}\n    {google_view(body)}")
    allres += resp.results
by_url = {r.url: r for r in allres}
rep = discover_recent_posts(allres)
reasons = {x.url: x for x in rep.rejected}
print("\n--- every result ---")
for url, r in by_url.items():
    raw = rec.raw_dates.get(url)
    if url in reasons: verdict = "REJECTED " + reasons[url].reason
    else:
        post = next((p for p in rep.accepted if p.original_url == url), None)
        if post is None: verdict = "REJECTED duplicate"
        else:
            a = analyze_post(post)
            verdict = f"accepted ({post.date_source} date {post.published_at:%Y-%m-%d}) -> {a.recommended_action} {a.score}/100 author={author_slug(post.url)}"
    print(f"{urlparse(url).netloc}{urlparse(url).path[:70]}\n    serpapi date={raw!r} parsed={r.published_at}\n    {verdict}")
print("\n" + disc.summary())
print(f"\nfinal candidates ({len(cands)}):")
for c in cands: print(f"  {c.analysis.recommended_action} {c.analysis.score}/100  {c.name}  {c.post_url}")