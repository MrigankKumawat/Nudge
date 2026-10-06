import asyncio, logging, os
from contextlib import asynccontextmanager
from typing import Literal
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import Base, engine, get_db, now, SessionLocal
from .models import Lead, PostOpportunity, Draft, Conversation, FollowUp, Activity, AgentRun, DISMISSED
from . import serializers as S
from .agent.pipeline import execute_run
from .agent.github_adapter import GitHubSearchError
from .agent.github_search import search_and_save, DEFAULT_LIMIT

logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")   # uvicorn only configures its own loggers; without this INFO from app.agent is invisible

async def scheduler():
    """Optional: set AGENT_INTERVAL_MINUTES=30 to auto-run. Runs only create drafts; nothing is ever sent."""
    while True:
        await asyncio.sleep(max(30, S.INTERVAL * 60))
        await asyncio.to_thread(start_run, "scheduled")

def start_run(trigger: str, source: str = "linkedin") -> int | None:
    with SessionLocal() as db:
        if db.scalar(select(AgentRun).where(AgentRun.status == "RUNNING")): return None
        run = AgentRun(trigger=trigger); db.add(run); db.commit(); rid = run.id
    execute_run(rid, source)
    return rid

@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:   # a server restart mid-run leaves a stale RUNNING row
        for r in db.scalars(select(AgentRun).where(AgentRun.status == "RUNNING")):
            r.status, r.error, r.finished_at = "FAILED", "server restarted", now()
        db.commit()
    task = asyncio.create_task(scheduler()) if S.INTERVAL else None
    yield
    if task: task.cancel()

app = FastAPI(title="Outreach Agent", lifespan=lifespan)
# Browsers may call this API only from these origins (exact match, no wildcard: this app creates and approves drafts).
# Extra origins, e.g. a Vercel preview URL, go in the CORS_ORIGINS env var on the host, comma-separated, with no code change.
ALLOWED_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173", "https://nudge-five-self.vercel.app"] \
    + [o.strip().rstrip("/") for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["*"], allow_headers=["*"])
from fastapi import APIRouter
r = APIRouter(prefix="/api")

class ContentBody(BaseModel):
    content: str | None = None

class DismissBody(BaseModel):
    reason: Literal["IGNORED", "NOT_RELEVANT"] = "IGNORED"

def get_or_404(db, model, id):
    obj = db.get(model, id)
    if not obj: raise HTTPException(404, f"{model.__name__} {id} not found")
    return obj

# ---------- dashboard / bootstrap ----------
@r.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    return {**S.stats(db), "agent": S.agent_status(db)}

@r.get("/bootstrap")
def bootstrap(db: Session = Depends(get_db)):
    return S.bootstrap(db)

# ---------- leads ----------
@r.get("/leads")
def leads(status: str | None = None, q: str | None = None, include_dismissed: bool = False, db: Session = Depends(get_db)):
    stmt = select(Lead).order_by(Lead.relevance_score.desc())
    if status: stmt = stmt.where(Lead.status == status.upper().replace(" ", "_"))
    if q: stmt = stmt.where(Lead.name.ilike(f"%{q}%") | Lead.company.ilike(f"%{q}%"))
    if not include_dismissed: stmt = stmt.where(Lead.status.notin_(DISMISSED))
    return [S.lead_out(db, l) for l in db.scalars(stmt)]

@r.get("/leads/{id}")
def lead_detail(id: int, db: Session = Depends(get_db)):
    l = get_or_404(db, Lead, id)
    posts = db.scalars(select(PostOpportunity).where(PostOpportunity.lead_id == id).order_by(PostOpportunity.posted_at.desc()))
    drafts = db.scalars(select(Draft).where(Draft.lead_id == id).order_by(Draft.id.desc()))
    acts = db.scalars(select(Activity).where(Activity.meta["lead_id"].as_integer() == id).order_by(Activity.id.desc()))
    return {**S.lead_out(db, l), "posts": [S.post_out(db, p) for p in posts], "drafts": [S.queue_out(db, d) for d in drafts],
            "activity": [S.activity_out(a) for a in acts]}

@r.post("/leads/{id}/dismiss")
def dismiss_lead(id: int, body: DismissBody = DismissBody(), db: Session = Depends(get_db)):
    l = get_or_404(db, Lead, id)
    l.status = body.reason
    for d in db.scalars(select(Draft).where(Draft.lead_id == id, Draft.status == "AWAITING_APPROVAL")):
        d.status = "REJECTED"
    db.add(Activity(type="lead_dismissed", actor="you", description=f"You marked {l.name} as {S.label(body.reason).lower()}", meta={"lead_id": id}))
    db.commit()
    return S.lead_out(db, l)

# ---------- posts ----------
@r.get("/posts")
def posts(include_dismissed: bool = False, db: Session = Depends(get_db)):
    stmt = select(PostOpportunity).order_by(PostOpportunity.relevance_score.desc())
    if not include_dismissed: stmt = stmt.where(PostOpportunity.status.notin_(DISMISSED))
    return [S.post_out(db, p) for p in db.scalars(stmt)]

@r.get("/posts/{id}")
def post_detail(id: int, db: Session = Depends(get_db)):
    p = get_or_404(db, PostOpportunity, id)
    lead = db.get(Lead, p.lead_id) if p.lead_id else None
    return {**S.post_out(db, p), "lead": S.lead_out(db, lead) if lead else None}

@r.post("/posts/{id}/dismiss")
def dismiss_post(id: int, body: DismissBody = DismissBody(), db: Session = Depends(get_db)):
    p = get_or_404(db, PostOpportunity, id)
    p.status = body.reason
    for d in db.scalars(select(Draft).where(Draft.target_type == "post", Draft.target_id == id, Draft.status == "AWAITING_APPROVAL")):
        d.status = "REJECTED"
    db.add(Activity(type="post_dismissed", actor="you", description=f"You marked {p.author}'s post as {S.label(body.reason).lower()}", meta={"lead_id": p.lead_id}))
    db.commit()
    return S.post_out(db, p)

# ---------- approval queue ----------
@r.get("/queue")
def queue(kind: Literal["DM", "COMMENT", "FOLLOW_UP"] | None = None, db: Session = Depends(get_db)):
    return S.queue(db, kind)

def pending_draft(db, id: int, body: ContentBody | None = None) -> Draft:
    d = get_or_404(db, Draft, id)
    if d.status != "AWAITING_APPROVAL": raise HTTPException(409, f"Draft is already {d.status}")
    if body and body.content is not None and body.content.strip() and body.content != d.content:
        d.content, d.edited_at = body.content, now()
    return d

def resolve(db, d: Draft, status: str, verb: str):
    """Approve/reject only changes OUR state. Nothing external happens here; sent_at stays empty until an integration exists."""
    d.status = status
    lead = db.get(Lead, d.lead_id)
    kind = S.ACT[d.action_type].lower()
    if status == "APPROVED":
        d.approved_at = now()
        desc = f"You approved {kind} to {lead.name} (queued, not sent: no integration connected)"
    else:
        desc = f"You {verb} {kind} draft for {lead.name}"
    if d.target_type == "post":
        p = db.get(PostOpportunity, d.target_id)
        p.status = {"APPROVED": "APPROVED", "REJECTED": "REJECTED", "NOT_RELEVANT": "NOT_RELEVANT"}[status]
    elif d.target_type == "followup":
        db.get(FollowUp, d.target_id).status = "APPROVED" if status == "APPROVED" else "DISMISSED"
    if status == "APPROVED" and d.target_type != "conversation":
        lead.status = "APPROVED"
    elif status == "NOT_RELEVANT":
        lead.status = "NOT_RELEVANT"
    elif status == "REJECTED" and lead.status == "AWAITING_APPROVAL":
        lead.status = "NEW"
    db.add(Activity(type=f"draft_{status.lower()}", actor="you", description=desc, meta={"lead_id": lead.id, "draft_id": d.id}))
    db.commit()
    return S.queue_out(db, d)

@r.patch("/drafts/{id}")
def edit_draft(id: int, body: ContentBody, db: Session = Depends(get_db)):
    if not body.content or not body.content.strip(): raise HTTPException(422, "content is required")
    d = pending_draft(db, id, body); db.commit()
    return S.queue_out(db, d)

@r.post("/drafts/{id}/approve")
def approve(id: int, body: ContentBody = ContentBody(), db: Session = Depends(get_db)):
    return resolve(db, pending_draft(db, id, body), "APPROVED", "approved")

@r.post("/drafts/{id}/reject")
def reject(id: int, db: Session = Depends(get_db)):
    return resolve(db, pending_draft(db, id), "REJECTED", "rejected")

@r.post("/drafts/{id}/not-relevant")
def not_relevant(id: int, db: Session = Depends(get_db)):
    return resolve(db, pending_draft(db, id), "NOT_RELEVANT", "marked not relevant:")

# ---------- conversations / follow-ups / activity ----------
@r.get("/conversations")
def conversations(status: str | None = None, db: Session = Depends(get_db)):
    stmt = select(Conversation).order_by(Conversation.updated_at.desc())
    if status: stmt = stmt.where(Conversation.status == status.upper().replace(" ", "_"))
    return [S.convo_out(db, c) for c in db.scalars(stmt)]

@r.get("/conversations/{id}")
def conversation(id: int, db: Session = Depends(get_db)):
    return S.convo_out(db, get_or_404(db, Conversation, id))

@r.get("/followups")
def followups(db: Session = Depends(get_db)):
    return [dict(lead=n, last_contacted=w, urgency=u, draft=t, draft_id=i) for n, w, u, t, i in S.followups(db)]

@r.get("/activity")
def activity(limit: int = Query(50, le=200), db: Session = Depends(get_db)):
    return [dict(id=a.id, type=a.type, actor=a.actor, description=a.description, timestamp=a.timestamp.isoformat(), when=S.ago(a.timestamp), meta=a.meta)
            for a in db.scalars(select(Activity).order_by(Activity.id.desc()).limit(limit))]

# ---------- agent ----------
@r.get("/agent/status")
def agent_status(db: Session = Depends(get_db)):
    return S.agent_status(db)

@r.get("/agent/runs")
def agent_runs(db: Session = Depends(get_db)):
    return [dict(id=x.id, status=x.status, trigger=x.trigger, started_at=x.started_at.isoformat(), finished_at=x.finished_at.isoformat() if x.finished_at else None,
                 people_scanned=x.people_scanned, posts_analyzed=x.posts_analyzed, opportunities_found=x.opportunities_found,
                 drafts_generated=x.drafts_generated, error=x.error) for x in db.scalars(select(AgentRun).order_by(AgentRun.id.desc()).limit(20))]

@r.post("/agent/run", status_code=202)
def trigger_run(bg: BackgroundTasks, source: Literal["linkedin", "github", "all"] = "linkedin", db: Session = Depends(get_db)):
    """Runs ONE source. Default is linkedin; the GitHub keyword hunt is POST /github/search instead."""
    if db.scalar(select(AgentRun).where(AgentRun.status == "RUNNING")):
        raise HTTPException(409, "An agent run is already in progress")
    run = AgentRun(trigger="manual"); db.add(run); db.commit()
    bg.add_task(execute_run, run.id, source)
    return dict(run_id=run.id, status="RUNNING", source=source)

# ---------- GitHub keyword hunt (manual, read-only toward GitHub) ----------
class GithubSearchBody(BaseModel):
    keyword: str = Field(min_length=1, max_length=300)
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=50)

@r.post("/github/search")
def github_search(body: GithubSearchBody, db: Session = Depends(get_db)):
    """keyword -> GitHub search (~500 issue pool) -> existing analyzer -> rank -> save/return the top `limit`. Synchronous: takes a few seconds."""
    try:
        return search_and_save(db, body.keyword, body.limit)
    except GitHubSearchError as e:
        raise HTTPException(e.status_code, str(e))

app.include_router(r)

@app.get("/")
def root():
    return {"app": "Outreach Agent", "docs": "/docs"}