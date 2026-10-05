"""Turns DB rows into the exact shapes the existing React UI consumes."""
import os, re
from datetime import timedelta
from sqlalchemy import select, func
from .db import now
from .models import Lead, PostOpportunity, Draft, Conversation, FollowUp, Activity, AgentRun, DISMISSED

ACT = {"DM": "DM", "COMMENT": "Comment", "FOLLOW_UP": "Follow-up", "IGNORE": "Ignore"}
INTERVAL = int(os.getenv("AGENT_INTERVAL_MINUTES", "0"))   # 0 = manual runs only
QUEUE_TARGETS = ("lead", "post", "followup")

def label(s: str) -> str:
    return s.replace("_", " ").capitalize()

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

def post_out(db, p: PostOpportunity) -> dict:
    return dict(id=p.id, lead_id=p.lead_id, url=re.sub(r"^https?://", "", p.post_url), post_url=p.post_url, who=p.author,
                role=" · ".join(x for x in (p.author_role, p.author_company) if x), score=p.relevance_score, when=ago(p.posted_at),
                posted_at=p.posted_at.isoformat(), txt=p.content, why=p.why, sig=p.reasons, act=ACT[p.recommended_action],
                status=label(p.status), draft=post_draft(db, p), source=p.source)

def queue_out(db, d: Draft) -> dict:
    l = db.get(Lead, d.lead_id)
    ctx, score, post_id = "", l.relevance_score, None
    if d.target_type == "post":
        p = db.get(PostOpportunity, d.target_id)
        ctx, score, post_id = f"“{p.content[:90]}{'…' if len(p.content) > 90 else ''}”", p.relevance_score, p.id
    elif d.target_type == "followup":
        f = db.get(FollowUp, d.target_id)
        ctx = f"Last contacted {ago(f.last_contacted_at)}, no reply"
    return dict(id=d.id, kind=ACT[d.action_type], who=l.name, co=l.company or "", ctx=ctx, why=d.why, text=d.content, score=score,
                lead_id=l.id, post_id=post_id, target_type=d.target_type, status=d.status, created_at=d.created_at.isoformat())

def queue(db, kind: str | None = None) -> list[dict]:
    q = select(Draft).where(Draft.status == "AWAITING_APPROVAL", Draft.target_type.in_(QUEUE_TARGETS))
    if kind: q = q.where(Draft.action_type == kind)
    items = [queue_out(db, d) for d in db.scalars(q)]
    return sorted(items, key=lambda x: (-x["score"], x["id"]))

def convo_out(db, c: Conversation) -> dict:
    l = db.get(Lead, c.lead_id)
    d = db.scalar(select(Draft).where(Draft.target_type == "conversation", Draft.target_id == c.id, Draft.status == "AWAITING_APPROVAL").limit(1))
    msgs = c.messages or []
    return dict(id=c.id, lead_id=l.id, who=l.name, intent=c.intent, status=label(c.status), status_code=c.status, when=ago(c.updated_at),
                last=msgs[-1]["text"] if msgs else "", read=c.ai_interpretation, next=c.next_action, msgs=[[m["from"], m["text"]] for m in msgs],
                draft=d.content if d else "", draft_id=d.id if d else None)

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
    return [a.description, ago(a.timestamp), a.actor]

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
                interval_minutes=INTERVAL or None, people=int(t[0]), posts=int(t[1]), opps=int(t[2]), drafts=int(t[3]), recent=recent)

def stats(db) -> dict:
    live = lambda col: col.notin_(DISMISSED)
    week = now() - timedelta(days=7)
    open_fu = db.scalars(select(FollowUp).where(FollowUp.status == "DUE")).all()
    return dict(
        new_leads=db.scalar(select(func.count()).select_from(Lead).where(Lead.status.in_(("NEW", "AWAITING_APPROVAL")), Lead.recommended_action != "IGNORE")) or 0,
        new_leads_24h=db.scalar(select(func.count()).select_from(Lead).where(Lead.created_at >= now() - timedelta(days=1), live(Lead.status))) or 0,
        post_opportunities=db.scalar(select(func.count()).select_from(PostOpportunity).where(PostOpportunity.posted_at >= week, live(PostOpportunity.status))) or 0,
        awaiting_approval=len(queue(db)), followups=len(open_fu), overdue=sum(1 for f in open_fu if f.due_at < now()),
        conversations=db.scalar(select(func.count()).select_from(Conversation)) or 0)

def bootstrap(db) -> dict:
    leads = [lead_out(db, l) for l in db.scalars(select(Lead).where(Lead.status.notin_(DISMISSED)).order_by(Lead.relevance_score.desc()))]
    posts = [post_out(db, p) for p in db.scalars(select(PostOpportunity).where(PostOpportunity.status.notin_(DISMISSED)).order_by(PostOpportunity.relevance_score.desc()))]
    convos = [convo_out(db, c) for c in db.scalars(select(Conversation).order_by(Conversation.updated_at.desc()))]
    feed = [activity_out(a) for a in db.scalars(select(Activity).order_by(Activity.id.desc()).limit(40))]
    return dict(leads=leads, posts=posts, queue=queue(db), convos=convos, feed=feed, followups=followups(db), stats=stats(db), agent=agent_status(db))