"""Manual GitHub keyword hunt: user keyword -> GitHub search (pool of ~500) -> existing analyzer -> rank -> save/return the top N.

Reuses github_adapter (fetching, bot filter), github_analyzer (the ONLY scoring system) and github_commenter (drafts).
Read-only toward GitHub: the single outbound call is the public search API. Nothing is ever posted, opened or reacted to.

Only the issues that are SHOWN get written to the database (new Lead / PostOpportunity / Draft rows). The rest of the pool is analysed in
memory and thrown away, so a 500-issue search does not flood the tables. Issue URL is the identity: a repeat search finds the existing
PostOpportunity and returns it instead of creating a duplicate, so previously saved issues still surface (with their current status).
"""
import logging, os
from datetime import timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db import now as db_now
from ..models import Lead, PostOpportunity, Draft, Activity, DISMISSED
from .. import serializers as S
from .github_adapter import GitHubIssueDiscovery, GitHubIssue, SOURCE, to_candidate
from .github_analyzer import Analysis, analyze_github_issue
from .github_commenter import github_comment_text

log = logging.getLogger(__name__)
DEFAULT_LIMIT = 25

def rank(scored: list[tuple[GitHubIssue, Analysis]]) -> list[tuple[GitHubIssue, Analysis]]:
    """Best first. The analyzer score already folds in pain evidence, retries, CI, repro difficulty, framework, investigation, details and
    recency (+10 within 3 days, +5 within 7). Ties go to the more recently updated issue. COMMENT-class results always outrank IGNORE ones."""
    return sorted(scored, key=lambda t: (t[1].recommended_action == "IGNORE", -t[1].score, -t[0].updated_at.timestamp(), t[0].url))

def _naive_utc(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None)

def _existing_posts(db: Session, urls: list[str]) -> dict[str, PostOpportunity]:
    out: dict[str, PostOpportunity] = {}
    for i in range(0, len(urls), 400):   # stay well under SQLite's bound-variable limit
        for p in db.scalars(select(PostOpportunity).where(PostOpportunity.post_url.in_(urls[i:i + 400]))):
            out[p.post_url] = p
    return out

def _has_draft(db: Session, post_id: int) -> bool:
    return db.scalar(select(Draft.id).where(Draft.target_type == "post", Draft.target_id == post_id).limit(1)) is not None

def _save(db: Session, issue: GitHubIssue, an: Analysis, cand, existing: PostOpportunity | None) -> PostOpportunity:
    """Create (or complete) the Lead / PostOpportunity / Draft for one COMMENT-worthy issue. Mirrors pipeline.save for GitHub, but reuses a lead
    that already exists for this author (leads.profile_url is unique) instead of colliding with it."""
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
    else:   # saved earlier as IGNORE (no draft): it now qualifies, so complete it
        post = existing
        post.relevance_score, post.reasons, post.why, post.recommended_action, post.status = an.score, list(an.signals), an.why, "COMMENT", "AWAITING_APPROVAL"
    db.add(Activity(type="scored", actor="agent", description=f"Agent scored {cand.name} {an.score}/100", meta={"lead_id": lead.id, "score": an.score}))
    db.add(Draft(lead_id=lead.id, target_type="post", target_id=post.id, action_type="COMMENT", content=github_comment_text(cand), why=an.why, status="AWAITING_APPROVAL"))
    db.add(Activity(type="draft_generated", actor="agent", description=f"Agent generated comment draft for {cand.name}", meta={"lead_id": lead.id}))
    return post

def search_and_save(db: Session, keyword: str, limit: int = DEFAULT_LIMIT, adapter: GitHubIssueDiscovery | None = None) -> dict:
    """Run one manual keyword hunt. Raises GitHubSearchError (with .status_code) when GitHub cannot be searched; never creates data in that case."""
    adapter = adapter or GitHubIssueDiscovery()
    res = adapter.search_keyword(keyword)                                                   # 1-2. fetch the pool (raises on failure, before any DB write)
    now = adapter.clock()
    scored = [(i, analyze_github_issue(i, now=now)) for i in res.issues]                    # 5-6. existing analyzer scores every issue in the pool
    ranked = rank(scored)                                                                   # 7. rank
    actionable = [t for t in ranked if t[1].recommended_action != "IGNORE"]                 # IGNORE-class issues are never presented as outreach opportunities
    existing = _existing_posts(db, [i.url for i, _ in actionable])
    picked: list[PostOpportunity] = []
    authors: set[str] = set()
    hidden = 0
    try:
        for issue, an in actionable:                                                        # 8. select AFTER analysis + ranking (never the first N raw results)
            if len(picked) >= limit: break
            if issue.author_url in authors: continue                                        # one outreach target per person
            old = existing.get(issue.url)
            if old is not None and old.status in DISMISSED:
                hidden += 1; continue                                                       # you already dismissed this one
            authors.add(issue.author_url)
            if old is not None and _has_draft(db, old.id):
                picked.append(old); continue                                                # already saved with a draft: surface it, no duplicate
            picked.append(_save(db, issue, an, to_candidate(issue, an, now), old))
        db.add(Activity(type="github_search", actor="you", meta={"keyword": res.keyword, "total_count": res.total_count},
                        description=f'GitHub search "{res.keyword}": {res.total_count} matching, {len(scored)} analyzed, showing top {len(picked)}'))
        db.commit()
    except Exception:
        db.rollback(); raise
    log.info("GitHub search %r: total=%d fetched=%d analyzed=%d actionable=%d shown=%d", res.keyword, res.total_count, res.raw, len(scored), len(actionable), len(picked))
    return dict(source="github", keyword=res.keyword, query=res.query, limit=limit, total_count=res.total_count, fetched=res.raw, pages=res.pages,
                skipped_unusable=res.invalid, skipped_bots=res.bots, analyzed=len(scored), actionable=len(actionable), hidden_dismissed=hidden,
                shown=len(picked), incomplete=res.incomplete, warning=res.warning, authenticated=bool((adapter._token if adapter._token is not None else os.getenv("GITHUB_TOKEN", "")).strip()),
                results=[S.post_out(db, p) for p in picked])