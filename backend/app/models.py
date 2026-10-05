from datetime import datetime
from sqlalchemy import String, Text, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, now

# Status values are plain strings (kept simple on purpose).
# Lead: NEW AWAITING_APPROVAL APPROVED CONTACTED REPLIED INTERESTED NOT_INTERESTED IGNORED NOT_RELEVANT
# Post: NEW AWAITING_APPROVAL APPROVED REJECTED IGNORED NOT_RELEVANT
# Draft: AWAITING_APPROVAL APPROVED REJECTED NOT_RELEVANT   (SENT is reserved for future integrations)
ACTIONS = ("DM", "COMMENT", "FOLLOW_UP", "IGNORE")
DISMISSED = ("IGNORED", "NOT_RELEVANT")

class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    profile_url: Mapped[str | None] = mapped_column(String(300), unique=True)
    company: Mapped[str | None] = mapped_column(String(120))
    role: Mapped[str | None] = mapped_column(String(160))
    relevance_score: Mapped[int] = mapped_column(default=0)
    reasons: Mapped[list] = mapped_column(JSON, default=list)      # evidence checklist
    tags: Mapped[list] = mapped_column(JSON, default=list)         # short signal pills
    analysis: Mapped[str] = mapped_column(Text, default="")        # why this lead / why this action
    status: Mapped[str] = mapped_column(String(30), default="NEW")
    recommended_action: Mapped[str] = mapped_column(String(20), default="IGNORE")
    source: Mapped[str] = mapped_column(String(60), default="manual")
    last_activity_at: Mapped[datetime | None] = mapped_column()
    last_contacted_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

class PostOpportunity(Base):
    __tablename__ = "post_opportunities"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    author: Mapped[str] = mapped_column(String(120))
    author_role: Mapped[str | None] = mapped_column(String(160))
    author_company: Mapped[str | None] = mapped_column(String(120))
    author_profile_url: Mapped[str | None] = mapped_column(String(300))
    post_url: Mapped[str] = mapped_column(String(400), unique=True)
    content: Mapped[str] = mapped_column(Text)
    posted_at: Mapped[datetime] = mapped_column()
    relevance_score: Mapped[int] = mapped_column(default=0)
    reasons: Mapped[list] = mapped_column(JSON, default=list)      # signals shown as pills
    why: Mapped[str] = mapped_column(Text, default="")
    recommended_action: Mapped[str] = mapped_column(String(20), default="IGNORE")
    status: Mapped[str] = mapped_column(String(30), default="NEW")
    source: Mapped[str] = mapped_column(String(60), default="manual")
    created_at: Mapped[datetime] = mapped_column(default=now)

class Draft(Base):
    __tablename__ = "drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    target_type: Mapped[str] = mapped_column(String(20))           # lead | post | followup | conversation
    target_id: Mapped[int] = mapped_column()
    action_type: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    why: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="AWAITING_APPROVAL")
    created_at: Mapped[datetime] = mapped_column(default=now)
    edited_at: Mapped[datetime | None] = mapped_column()
    approved_at: Mapped[datetime | None] = mapped_column()
    sent_at: Mapped[datetime | None] = mapped_column()             # only an external integration may set this

class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    status: Mapped[str] = mapped_column(String(30), default="NEW")  # NEW ACTIVE WAITING INTERESTED NOT_INTERESTED CONVERTED
    intent: Mapped[str] = mapped_column(String(40), default="Neutral")
    messages: Mapped[list] = mapped_column(JSON, default=list)      # [{"from": "me"|"them", "text": str, "at": iso}]
    ai_interpretation: Mapped[str] = mapped_column(Text, default="")
    next_action: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

class FollowUp(Base):
    __tablename__ = "followups"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    last_contacted_at: Mapped[datetime] = mapped_column()
    due_at: Mapped[datetime] = mapped_column()
    status: Mapped[str] = mapped_column(String(20), default="DUE")  # DUE | APPROVED | DISMISSED
    created_at: Mapped[datetime] = mapped_column(default=now)

class Activity(Base):
    __tablename__ = "activities"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(10), default="agent")  # agent | you | sent
    description: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(default=now)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")  # RUNNING COMPLETED FAILED
    trigger: Mapped[str] = mapped_column(String(20), default="manual")
    started_at: Mapped[datetime] = mapped_column(default=now)
    finished_at: Mapped[datetime | None] = mapped_column()
    people_scanned: Mapped[int] = mapped_column(default=0)
    posts_analyzed: Mapped[int] = mapped_column(default=0)
    opportunities_found: Mapped[int] = mapped_column(default=0)
    drafts_generated: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
