"""Turns DB rows into the exact shapes the existing React UI consumes."""
import os, re
from datetime import timedelta
from sqlalchemy import select, func
from .db import now
from .models import Lead, PostOpportunity, Draft, Conversation, ConversationMessage, FollowUp, Activity, AgentRun, Scan, DISMISSED

ACT = {"DM": "DM", "COMMENT": "Comment", "FOLLOW_UP": "Follow-up", "IGNORE": "Ignore"}
INTERVAL = int(os.getenv("AGENT_INTERVAL_MINUTES", "0"))   # 0 = manual runs only
QUEUE_TARGETS = ("lead", "post", "followup")

def label(s: str) -> str:
    return s.replace("_", " ").capitalize()

def iso(dt) -> str | None:
    """Timestamps are stored as naive UTC. New fields are sent with a trailing Z so the browser reads them as UTC, not local time."""
    return dt.isoformat() + "Z" if dt else None

def ago(dt) -> str:
    if not dt: return "never"
    s = (now() - dt).total_seconds()
    if s < 3600: return f"{max(1, int(s // 60))} min ago"
    if s < 86400: return f"{int(s // 3600)}h ago"
    d = int(s // 86400)
    return f"{d} day{'s' if d != 1 else ''} ago"

def lead_out(db, l: Lead) -> dict:
    via = db.scalar(select(PostOpportunity.id).where(PostOpportunity.lead_id == l.id).order_by(PostOpportunity.relevance_score.desc()).limit(1))
    return dict(id=l.id, name=l.name, role=l.role, co=l.company, score=l.relevance_score, status=label(l.status), status_code=l.status,
                act=ACT[l.recommended_action], last=ago(l.last_activity_at or l.created_at), tags=l.tags, why=l.analysis, ev=l.reasons,
                via=via, profile_url=l.profile_url, source=l.source, created_at=l.created_at.isoformat())

def draft_text_for(db, target_type, target_id) -> str:
    return db.scalar(select(Draft.content).where(Draft.target_type == target_type, Draft.target_id == target_id,
                                                 Draft.status == "AWAITING_APPROVAL").order_by(Draft.id.desc()).limit(1)) or ""

def post_draft(db, p: PostOpportunity) -> str:
    """The pending draft for a post: a comment is attached to the post; a DM recommended from a post is attached to its lead."""
    text = draft_text_for(db, "post", p.id)
    if not text and p.recommended_action == "DM" and p.lead_id:
        text = draft_text_for(db, "lead", p.lead_id)
    return text

GH_SOURCE = "github:issue"
_GH_NUM = re.compile(r"/issues/(\d+)")
# Display-only detection of the test framework / CI named in the issue text. Scoring is untouched (github_analyzer.py does that).
_GH_FRAMEWORKS = (("pytest", r"\bpytest\b|\bconftest\b"), ("pytest-xdist", r"\bxdist\b"), ("Playwright", r"\bplaywright\b"), ("Cypress", r"\bcypress\b"),
                  ("Jest", r"\bjest\b"), ("Vitest", r"\bvitest\b"), ("Selenium", r"\bselenium\b"), ("Mocha", r"\bmocha\b"), ("JUnit", r"\bjunit\b"),
                  ("unittest", r"\bunittest\b"), ("RSpec", r"\brspec\b"), ("WebdriverIO", r"\bwebdriverio\b|\bwdio\b"))
_GH_CI = (("GitHub Actions", r"github actions|\bgha\b"), ("Jenkins", r"\bjenkins\b"), ("GitLab CI", r"gitlab[- ]ci"), ("CircleCI", r"\bcircleci\b"),
          ("Azure Pipelines", r"azure pipelines?"), ("Buildkite", r"\bbuildkite\b"), ("Travis CI", r"\btravis\b"))

def github_info(p: PostOpportunity) -> dict:
    """Structured view of a GitHub issue opportunity, derived only from what the pipeline already stored (no GitHub call)."""
    title, _, body = (p.content or "").partition("\n\n")
    low = (p.content or "").lower()
    m = _GH_NUM.search(p.post_url or "")
    return dict(repository=p.author_company or "", number=int(m.group(1)) if m else None, title=title.strip(), excerpt=body.strip()[:400],
                author=p.author, author_url=p.author_profile_url, issue_url=p.post_url,
                frameworks=[n for n, rx in _GH_FRAMEWORKS if re.search(rx, low)], ci=[n for n, rx in _GH_CI if re.search(rx, low)])

def latest_draft(db, target_type: str, target_id: int) -> Draft | None:
    return db.scalar(select(Draft).where(Draft.target_type == target_type, Draft.target_id == target_id).order_by(Draft.id.desc()).limit(1))

def post_out(db, p: PostOpportunity) -> dict:
    out = dict(id=p.id, lead_id=p.lead_id, url=re.sub(r"^https?://", "", p.post_url), post_url=p.post_url, who=p.author,
                role=" · ".join(x for x in (p.author_role, p.author_company) if x), score=p.relevance_score, when=ago(p.posted_at),
                posted_at=p.posted_at.isoformat(), txt=p.content, why=p.why, sig=p.reasons, act=ACT[p.recommended_action],
                status=label(p.status), draft=post_draft(db, p), source=p.source, github=None, draft_status=None, draft_id=None)
    if p.source == GH_SOURCE:   # additive: a GitHub draft stays visible (and copyable) after approve/reject, with its state
        d = latest_draft(db, "post", p.id)
        out.update(github=github_info(p), draft=d.content if d else "", draft_status=d.status if d else None, draft_id=d.id if d else None)
    return out

def queue_out(db, d: Draft) -> dict:
    l = db.get(Lead, d.lead_id)
    ctx, score, post_id, source, issue_url = "", l.relevance_score, None, l.source, None
    if d.target_type == "post":
        p = db.get(PostOpportunity, d.target_id)
        ctx, score, post_id, source = f"“{p.content[:90]}{'…' if len(p.content) > 90 else ''}”", p.relevance_score, p.id, p.source
        if p.source == GH_SOURCE: ctx, issue_url = f"“{github_info(p)['title'][:90]}”", p.post_url
    elif d.target_type == "followup":
        f = db.get(FollowUp, d.target_id)
        ctx = f"Last contacted {ago(f.last_contacted_at)}, no reply"
    return dict(id=d.id, kind=ACT[d.action_type], who=l.name, co=l.company or "", ctx=ctx, why=d.why, text=d.content, score=score,
                lead_id=l.id, post_id=post_id, target_type=d.target_type, status=d.status, created_at=d.created_at.isoformat(), source=source, issue_url=issue_url)

def queue(db, kind: str | None = None) -> list[dict]:
    q = select(Draft).where(Draft.status == "AWAITING_APPROVAL", Draft.target_type.in_(QUEUE_TARGETS))
    if kind: q = q.where(Draft.action_type == kind)
    items = [queue_out(db, d) for d in db.scalars(q)]
    return sorted(items, key=lambda x: (-x["score"], x["id"]))

def _issue_ref(c: Conversation, p: PostOpportunity | None) -> tuple[str, int | None]:
    """(\"owner/repo\", number) of a GitHub conversation. The post is the source of truth; the issue URL is the fallback."""
    m = _GH_NUM.search(c.issue_url or "")
    number = int(m.group(1)) if m else None
    repo = (p.author_company if p and p.author_company else "") or "/".join((c.issue_url or "").split("/")[3:5])
    return repo, number

def github_convo_out(db, c: Conversation, l: Lead, d: Draft | None) -> dict:
    """A GitHub issue conversation: real comments from ConversationMessage, oldest first. `msgs` keeps the legacy [[\"me\"|\"them\", text]] shape so the
    existing conversation pane renders it unchanged; `messages` is the richer timeline the new UI uses."""
    p = db.get(PostOpportunity, c.post_id) if c.post_id else None
    rows = db.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == c.id)
                      .order_by(ConversationMessage.created_at, ConversationMessage.id)).all()
    repo, number = _issue_ref(c, p)
    title = github_info(p)["title"] if p else ""
    messages = [dict(id=m.id, external_id=m.external_id, author=m.author_username, is_from_me=m.is_from_me, mentions_me=m.mentions_me, text=m.body,
                     created_at=iso(m.created_at), when=ago(m.created_at), url=m.source_url) for m in rows]
    last = rows[-1] if rows else None
    stamp = c.last_message_at or c.updated_at
    return dict(id=c.id, lead_id=l.id, who=l.name, intent=c.intent, status=label(c.status), status_code=c.status, when=ago(stamp), last=last.body if last else "",
                read=c.ai_interpretation, next=c.next_action, msgs=[["me" if m.is_from_me else "them", m.body] for m in rows],
                draft=d.content if d else "", draft_id=d.id if d else None,
                source=c.source, unread=bool(c.unread), issue_url=c.issue_url, post_id=c.post_id, repository=repo, number=number, title=title,
                last_message_at=iso(c.last_message_at), last_message_author=c.last_message_author, last_message_from_me=c.last_message_from_me,
                last_checked_at=iso(c.last_checked_at), message_count=len(rows), reply_count=sum(1 for m in rows if not m.is_from_me),
                mentions_me=any(m.mentions_me for m in rows), messages=messages)

def convo_out(db, c: Conversation) -> dict:
    l = db.get(Lead, c.lead_id)
    d = db.scalar(select(Draft).where(Draft.target_type == "conversation", Draft.target_id == c.id, Draft.status == "AWAITING_APPROVAL").limit(1))
    if c.source == GH_SOURCE: return github_convo_out(db, c, l, d)
    msgs = c.messages or []
    return dict(id=c.id, lead_id=l.id, who=l.name, intent=c.intent, status=label(c.status), status_code=c.status, when=ago(c.updated_at),
                last=msgs[-1]["text"] if msgs else "", read=c.ai_interpretation, next=c.next_action, msgs=[[m["from"], m["text"]] for m in msgs],
                draft=d.content if d else "", draft_id=d.id if d else None,
                source=c.source, unread=False, issue_url=None, post_id=None, repository="", number=None, title="", last_message_at=None,
                last_message_author=None, last_message_from_me=None, last_checked_at=None, message_count=len(msgs), reply_count=0, mentions_me=False, messages=[])

def convos(db, unread: bool | None = None, status: str | None = None) -> list[dict]:
    """Newest activity first, unread GitHub conversations on top. `unread=True` is the Unread tab, None is All."""
    q = select(Conversation)
    if unread is True: q = q.where(Conversation.unread.is_(True))
    if unread is False: q = q.where(Conversation.unread.is_not(True))
    if status: q = q.where(Conversation.status == status.upper().replace(" ", "_"))
    q = q.order_by(Conversation.unread.desc(), func.coalesce(Conversation.last_message_at, Conversation.updated_at).desc(), Conversation.id.desc())
    return [convo_out(db, c) for c in db.scalars(q)]

def unread_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Conversation).where(Conversation.source == GH_SOURCE, Conversation.unread.is_(True))) or 0

def due_label(f: FollowUp) -> str:
    days = (f.due_at - now()).total_seconds() / 86400
    return "Overdue" if days < 0 else "Due today" if days < 1 else f"Due in {int(days) + 1} day{'s' if int(days) else ''}"

def followups(db) -> list[list]:
    out = []
    for f in db.scalars(select(FollowUp).where(FollowUp.status == "DUE").order_by(FollowUp.due_at)):
        l = db.get(Lead, f.lead_id)
        d = db.scalar(select(Draft).where(Draft.target_type == "followup", Draft.target_id == f.id, Draft.status == "AWAITING_APPROVAL").limit(1))
        out.append([l.name, ago(f.last_contacted_at), due_label(f), d.content if d else "", d.id if d else 0])
    return out

def activity_out(a: Activity) -> list:
    """Feed row. Positions 0-2 are what the UI already reads; type and ISO time are appended for the new Activity page."""
    return [a.description, ago(a.timestamp), a.actor, a.type, iso(a.timestamp)]

def activity_dict(a: Activity) -> dict:
    return dict(id=a.id, type=a.type, actor=a.actor, description=a.description, timestamp=iso(a.timestamp), when=ago(a.timestamp), meta=a.meta or {})

def scan_out(db, scan: Scan, results: bool = True) -> dict:
    """One GitHub scan. The old search response keys (total_count, fetched, analyzed, actionable, shown, hidden_dismissed, warning, authenticated, results)
    are kept as they were, so the Hunter page keeps working; the rest is new. `results` are the scan's opportunities in rank order, read live from
    PostOpportunity, so an issue you approved, contacted or dismissed after the scan shows its CURRENT state (dismissed ones drop out of the list)."""
    ids = list(scan.result_post_ids or [])
    posts: list[dict] = []
    if results and ids:
        by_id = {p.id: p for p in db.scalars(select(PostOpportunity).where(PostOpportunity.id.in_(ids)))}
        posts = [post_out(db, by_id[i]) for i in ids if i in by_id and by_id[i].status not in DISMISSED]
    return dict(id=scan.id, source=scan.source, keyword=scan.keyword, query=scan.query, status=scan.status.lower(), status_code=scan.status,
                started_at=iso(scan.started_at), completed_at=iso(scan.completed_at), when=ago(scan.completed_at or scan.started_at), limit=scan.result_limit,
                total_count=scan.matching_count, fetched=scan.fetched_count, unique=scan.unique_count, analyzed=scan.analyzed_count, actionable=scan.comment_count,
                shown=scan.displayed_count, hidden_dismissed=scan.hidden_dismissed, pages=scan.pages, skipped_unusable=scan.skipped_unusable, skipped_bots=scan.skipped_bots,
                incomplete=bool(scan.incomplete), authenticated=bool(scan.authenticated), warning=scan.warning, error=scan.error, result_ids=ids, results=posts)

def current_scan(db, results: bool = True) -> dict | None:
    """The scan the Hunter page shows: the newest COMPLETED one. A running or failed scan never replaces it."""
    scan = db.scalar(select(Scan).where(Scan.status == "COMPLETED").order_by(Scan.id.desc()).limit(1))
    return scan_out(db, scan, results) if scan else None

def scan_status(db) -> dict | None:
    """The newest scan of any status (so the page can say 'a scan is running' or 'the last scan failed' above the still-shown previous results)."""
    scan = db.scalar(select(Scan).order_by(Scan.id.desc()).limit(1))
    return scan_out(db, scan, results=False) if scan else None

def agent_status(db) -> dict:
    last = db.scalar(select(AgentRun).order_by(AgentRun.id.desc()).limit(1))
    running = bool(last and last.status == "RUNNING")
    done = db.scalar(select(AgentRun).where(AgentRun.status != "RUNNING").order_by(AgentRun.id.desc()).limit(1))
    today0 = now().replace(hour=0, minute=0, second=0, microsecond=0)
    t = db.execute(select(func.coalesce(func.sum(AgentRun.people_scanned), 0), func.coalesce(func.sum(AgentRun.posts_analyzed), 0),
                          func.coalesce(func.sum(AgentRun.opportunities_found), 0), func.coalesce(func.sum(AgentRun.drafts_generated), 0))
                   .where(AgentRun.started_at >= today0)).one()
    next_at = (done.finished_at + timedelta(minutes=INTERVAL)) if (INTERVAL and done and done.finished_at) else None
    mins = max(0, int((next_at - now()).total_seconds() // 60)) if next_at else None
    recent = [[a.description, ago(a.timestamp)] for a in db.scalars(select(Activity).where(Activity.actor == "agent").order_by(Activity.id.desc()).limit(5))]
    if running: recent.insert(0, ["Scanning for new leads", "now"])
    return dict(status="running" if running else "idle", running=running, last_run_id=done.id if done else None,
                last_run_at=done.finished_at.isoformat() if done and done.finished_at else None, last=ago(done.finished_at) if done else "never",
                next_run_at=next_at.isoformat() if next_at else None, next=f"in {mins} min" if mins is not None else "not scheduled",
                interval_minutes=INTERVAL or None, last_status=done.status.lower() if done else None, last_error=done.error if done else None, people=int(t[0]), posts=int(t[1]), opps=int(t[2]), drafts=int(t[3]), recent=recent)

def stats(db) -> dict:
    live = lambda col: col.notin_(DISMISSED)
    week = now() - timedelta(days=7)
    open_fu = db.scalars(select(FollowUp).where(FollowUp.status == "DUE")).all()
    return dict(
        new_leads=db.scalar(select(func.count()).select_from(Lead).where(Lead.status.in_(("NEW", "AWAITING_APPROVAL")), Lead.recommended_action != "IGNORE")) or 0,
        new_leads_24h=db.scalar(select(func.count()).select_from(Lead).where(Lead.created_at >= now() - timedelta(days=1), live(Lead.status))) or 0,
        post_opportunities=db.scalar(select(func.count()).select_from(PostOpportunity).where(PostOpportunity.posted_at >= week, live(PostOpportunity.status))) or 0,
        awaiting_approval=len(queue(db)), followups=len(open_fu), overdue=sum(1 for f in open_fu if f.due_at < now()),
        conversations=db.scalar(select(func.count()).select_from(Conversation)) or 0, unread_conversations=unread_count(db))

def bootstrap(db) -> dict:
    leads = [lead_out(db, l) for l in db.scalars(select(Lead).where(Lead.status.notin_(DISMISSED)).order_by(Lead.relevance_score.desc()))]
    posts = [post_out(db, p) for p in db.scalars(select(PostOpportunity).where(PostOpportunity.status.notin_(DISMISSED)).order_by(PostOpportunity.relevance_score.desc()))]
    convs = convos(db)
    feed = [activity_out(a) for a in db.scalars(select(Activity).order_by(Activity.id.desc()).limit(40))]
    return dict(leads=leads, posts=posts, queue=queue(db), convos=convs, feed=feed, followups=followups(db), stats=stats(db), agent=agent_status(db),
                unread=unread_count(db), scan=current_scan(db), scan_status=scan_status(db))