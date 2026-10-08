"""Manual GitHub keyword hunt: user keyword -> GitHub search (pool of ~1,000) -> issue-URL dedupe -> existing analyzer -> COMMENT-worthy -> rank ->
one issue per author -> top N -> saved as a Scan.

Reuses github_adapter (fetching, bot filter), github_analyzer (the ONLY scoring system) and github_commenter (drafts).
Read-only toward GitHub: the single outbound call is the public search API. Nothing is ever posted, opened or reacted to.

Scans. Every hunt is a `Scan` row: RUNNING -> COMPLETED | FAILED. A scan's results are just an ordered list of PostOpportunity ids; the opportunities, leads,
drafts, approvals and activity are permanent and shared by every scan. The CURRENT scan is the newest COMPLETED one, and it is replaced only by the same
transaction that saves a successful new scan, so a running or failed scan never changes what is shown and never deletes anything.

Identity. An issue is its canonical URL (github_adapter.canonical_issue_url, compared case-insensitively). Seeing it again never creates a second
PostOpportunity / Lead / Draft and never changes a status you have already moved (approved, contacted, dismissed, ...).

Only the issues that are SHOWN get written to the database (new Lead / PostOpportunity / Draft rows). The rest of the pool is analysed in memory and
thrown away, so a 1,000-issue search does not flood the tables.
"""
import logging, os
from datetime import timedelta, timezone
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from ..db import now as db_now
from ..models import Lead, PostOpportunity, Draft, Activity, Scan, DISMISSED
from .. import serializers as S
from .github_adapter import GitHubIssueDiscovery, GitHubIssue, GitHubSearchError, SOURCE, to_candidate, issue_key
from .github_analyzer import Analysis, analyze_github_issue
from .github_commenter import github_comment_text

log = logging.getLogger(__name__)
DEFAULT_LIMIT = 30
STALE_SCAN = timedelta(minutes=15)   # a RUNNING scan older than this is treated as dead (crashed server / abandoned request) and no longer blocks a new one

def rank(scored: list[tuple[GitHubIssue, Analysis]]) -> list[tuple[GitHubIssue, Analysis]]:
    """Best first. The analyzer score already folds in pain evidence, retries, CI, repro difficulty, framework, investigation, details and
    recency (+10 within 3 days, +5 within 7). Ties go to the more recently updated issue. COMMENT-class results always outrank IGNORE ones."""
    return sorted(scored, key=lambda t: (t[1].recommended_action == "IGNORE", -t[1].score, -t[0].updated_at.timestamp(), t[0].url))

def _naive_utc(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None)

def _existing_posts(db: Session, urls: list[str]) -> dict[str, PostOpportunity]:
    """Saved posts for these issue URLs, keyed by issue_key() (case-insensitive canonical URL)."""
    keys = [issue_key(u) for u in urls]
    out: dict[str, PostOpportunity] = {}
    for i in range(0, len(keys), 400):   # stay well under SQLite's bound-variable limit
        for p in db.scalars(select(PostOpportunity).where(func.lower(PostOpportunity.post_url).in_(keys[i:i + 400]))):
            out[issue_key(p.post_url)] = p
    return out

def _has_draft(db: Session, post_id: int) -> bool:
    return db.scalar(select(Draft.id).where(Draft.target_type == "post", Draft.target_id == post_id).limit(1)) is not None

def _save(db: Session, issue: GitHubIssue, an: Analysis, cand, existing: PostOpportunity | None) -> PostOpportunity:
    """Create (or complete) the Lead / PostOpportunity / Draft for one COMMENT-worthy issue. Mirrors pipeline.save for GitHub, but reuses a lead
    that already exists for this author (leads.profile_url is unique) instead of colliding with it.
    `existing` is only ever a draft-less post still in status NEW (an earlier low-score pass); search_and_save never passes in one you have worked."""
    lead = db.scalar(select(Lead).where(Lead.profile_url == issue.author_url))
    if lead is None:
        lead = Lead(name=cand.name, profile_url=cand.profile_url, company=cand.company, role="", relevance_score=an.score, reasons=list(an.signals),
                    tags=list(an.signals), analysis=an.why, status="AWAITING_APPROVAL", recommended_action="COMMENT", source=SOURCE,
                    last_activity_at=_naive_utc(issue.updated_at))
        db.add(lead); db.flush()
        db.add(Activity(type="discovered", actor="agent", description=f"Agent discovered {cand.name} via {SOURCE}", meta={"lead_id": lead.id}))
    elif lead.recommended_action == "IGNORE" and lead.status == "NEW":   # an earlier low-score pass: upgrade, never touch leads you already worked
        lead.recommended_action, lead.status, lead.relevance_score, lead.analysis = "COMMENT", "AWAITING_APPROVAL", an.score, an.why
        lead.tags, lead.reasons = list(an.signals), list(an.signals)
    if existing is None:
        post = PostOpportunity(lead_id=lead.id, author=cand.name, author_role="", author_company=cand.company, author_profile_url=cand.profile_url,
                               post_url=issue.url, content=cand.post_text, posted_at=_naive_utc(issue.updated_at), relevance_score=an.score,
                               reasons=list(an.signals), why=an.why, recommended_action="COMMENT", source=SOURCE, status="AWAITING_APPROVAL")
        db.add(post); db.flush()
    else:   # saved earlier as IGNORE (no draft, still NEW): it now qualifies, so complete it
        post = existing
        post.relevance_score, post.reasons, post.why, post.recommended_action, post.status = an.score, list(an.signals), an.why, "COMMENT", "AWAITING_APPROVAL"
    db.add(Activity(type="scored", actor="agent", description=f"Agent scored {cand.name} {an.score}/100", meta={"lead_id": lead.id, "score": an.score}))
    db.add(Draft(lead_id=lead.id, target_type="post", target_id=post.id, action_type="COMMENT", content=github_comment_text(cand), why=an.why, status="AWAITING_APPROVAL"))
    db.add(Activity(type="draft_generated", actor="agent", description=f"Agent generated comment draft for {cand.name}", meta={"lead_id": lead.id}))
    return post

# ---- scan lifecycle -------------------------------------------------------------------------------------------------------------------------
def _claim_scan(db: Session, keyword: str, limit: int) -> int:
    """Start a scan: one at a time. The RUNNING row is committed up front so it survives a failure later in the request."""
    for s in db.scalars(select(Scan).where(Scan.status == "RUNNING")).all():
        if db_now() - s.started_at > STALE_SCAN:
            s.status, s.error, s.completed_at = "FAILED", "abandoned: no result after 15 minutes", db_now()
        else:
            db.commit()
            raise GitHubSearchError("A GitHub scan is already running. Wait for it to finish.", 409)
    scan = Scan(source=SOURCE, keyword=keyword.strip()[:300], result_limit=limit)
    db.add(scan); db.commit()
    return scan.id

def _fail_scan(db: Session, scan_id: int, keyword: str, err: Exception) -> None:
    """Mark the scan FAILED (the previous COMPLETED scan is untouched and stays current). Runs after the caller has rolled back."""
    msg = str(err)[:500]
    try:
        scan = db.get(Scan, scan_id)
        if scan is not None: scan.status, scan.error, scan.completed_at = "FAILED", msg, db_now()
        if getattr(err, "status_code", 500) != 422:   # an empty / unsearchable keyword is a typo, not worth an Activity row
            db.add(Activity(type="github_search_failed", actor="you", description=f'GitHub search "{keyword.strip()[:80]}" failed: {msg[:200]}', meta={"scan_id": scan_id, "keyword": keyword.strip()[:80]}))
        db.commit()
    except Exception:
        db.rollback(); log.exception("Could not record failed scan %s", scan_id)

def search_and_save(db: Session, keyword: str, limit: int = DEFAULT_LIMIT, adapter: GitHubIssueDiscovery | None = None) -> dict:
    """Run one manual keyword hunt and save it as the new current scan. Raises GitHubSearchError (with .status_code) when GitHub cannot be searched or
    another scan is running. On any failure the scan is recorded as FAILED, no opportunity data is written, and the previous scan stays current."""
    adapter = adapter or GitHubIssueDiscovery()
    scan_id = _claim_scan(db, keyword, limit)
    try:
        res = adapter.search_keyword(keyword)                                               # 1-2. fetch the pool (raises on failure, before any opportunity is written)
        now = adapter.clock()
        scored = [(i, analyze_github_issue(i, now=now)) for i in res.issues]                # 5-6. existing analyzer scores every issue in the pool (issues are already URL-deduped)
        ranked = rank(scored)                                                               # 7. rank
        actionable = [t for t in ranked if t[1].recommended_action != "IGNORE"]             # IGNORE-class issues are never presented as outreach opportunities
        existing = _existing_posts(db, [i.url for i, _ in actionable])
        picked: list[PostOpportunity] = []
        authors: set[str] = set()
        hidden = 0
        for issue, an in actionable:                                                        # 8. select AFTER analysis + ranking (never the first N raw results)
            if len(picked) >= limit: break
            if issue.author_url in authors: continue                                        # one outreach target per person
            old = existing.get(issue_key(issue.url))
            if old is not None and old.status in DISMISSED:
                hidden += 1; continue                                                       # you already dismissed this one
            authors.add(issue.author_url)
            if old is not None and (old.status != "NEW" or _has_draft(db, old.id)):
                picked.append(old); continue                                                # already worked or drafted: surface it exactly as it is, no duplicate, no reset
            picked.append(_save(db, issue, an, to_candidate(issue, an, now), old))
        authed = bool((adapter._token if adapter._token is not None else os.getenv("GITHUB_TOKEN", "")).strip())
        scan = db.get(Scan, scan_id)                                                        # fill the scan and flip it to COMPLETED in the SAME transaction as the saves above
        scan.keyword, scan.query = res.keyword[:300], res.query[:400]
        scan.status, scan.completed_at = "COMPLETED", db_now()
        scan.matching_count, scan.fetched_count, scan.unique_count, scan.analyzed_count = res.total_count, res.raw, len(res.issues), len(scored)
        scan.comment_count, scan.displayed_count, scan.hidden_dismissed = len(actionable), len(picked), hidden
        scan.pages, scan.skipped_unusable, scan.skipped_bots = res.pages, res.invalid, res.bots
        scan.incomplete, scan.authenticated, scan.warning = bool(res.incomplete), authed, res.warning or None
        scan.result_post_ids = [p.id for p in picked]
        db.add(Activity(type="github_search", actor="you", meta={"scan_id": scan_id, "keyword": res.keyword, "total_count": res.total_count, "fetched": res.raw, "shown": len(picked)},
                        description=f'GitHub search "{res.keyword}" completed: {res.raw:,} fetched · {len(picked)} opportunities'))
        db.commit()
    except Exception as e:
        db.rollback()
        _fail_scan(db, scan_id, keyword, e)
        raise
    log.info("GitHub scan #%d %r: total=%d fetched=%d unique=%d analyzed=%d actionable=%d shown=%d", scan_id, res.keyword, res.total_count, res.raw, len(res.issues), len(scored), len(actionable), len(picked))
    return S.scan_out(db, scan)