from datetime import datetime
from sqlalchemy import String, Text, ForeignKey, JSON, Boolean, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, now

# Status values are plain strings (kept simple on purpose).
# Lead: NEW AWAITING_APPROVAL APPROVED CONTACTED REPLIED INTERESTED NOT_INTERESTED IGNORED NOT_RELEVANT
# Post: NEW AWAITING_APPROVAL APPROVED CONTACTED REJECTED IGNORED NOT_RELEVANT   (CONTACTED: reply tracking found a comment by you on that issue)
# Draft: AWAITING_APPROVAL APPROVED REJECTED NOT_RELEVANT   (SENT is reserved for future integrations)
# Conversation (LinkedIn/legacy): NEW ACTIVE WAITING INTERESTED NOT_INTERESTED CONVERTED
# Conversation (GitHub, source == "github:issue"): NEW_REPLY WAITING_FOR_ME WAITING_FOR_THEM CLOSED
# Scan: RUNNING COMPLETED FAILED
ACTIONS = ("DM", "COMMENT", "FOLLOW_UP", "IGNORE")
DISMISSED = ("IGNORED", "NOT_RELEVANT")
GH_CONVO_STATES = ("NEW_REPLY", "WAITING_FOR_ME", "WAITING_FOR_THEM", "CLOSED")

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
    """One conversation per lead thread. LinkedIn/legacy rows keep their JSON `messages`. GitHub rows (source == "github:issue") are one per issue:
    their messages live in ConversationMessage (unique per GitHub comment id), and the columns below are filled by github_tracker.py.
    Every column after `updated_at` is NEW and nullable / defaulted, so db.migrate() can add it to an existing outreach.db without touching old rows."""
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_issue_url", "issue_url", unique=True),)   # NULLs (legacy rows) do not collide in SQLite
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    status: Mapped[str] = mapped_column(String(30), default="NEW")  # see the status notes at the top of this file
    intent: Mapped[str] = mapped_column(String(40), default="Neutral")
    messages: Mapped[list] = mapped_column(JSON, default=list)      # legacy: [{"from": "me"|"them", "text": str, "at": iso}]
    ai_interpretation: Mapped[str] = mapped_column(Text, default="")
    next_action: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)
    # ---- GitHub reply tracking (new) ----
    source: Mapped[str | None] = mapped_column(String(60))                          # "github:issue" for tracked issues, NULL for legacy rows
    issue_url: Mapped[str | None] = mapped_column(String(400))                       # canonical issue URL: the identity of a GitHub conversation
    post_id: Mapped[int | None] = mapped_column(ForeignKey("post_opportunities.id"))  # repo / number / title are read from this post, not duplicated
    last_message_at: Mapped[datetime | None] = mapped_column()                       # GitHub's timestamp of the newest stored comment (not our poll time)
    last_message_author: Mapped[str | None] = mapped_column(String(120))
    last_message_from_me: Mapped[bool | None] = mapped_column(Boolean)
    unread: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    last_checked_at: Mapped[datetime | None] = mapped_column()                       # when the tracker last polled this issue (drives the `since` filter)

class ConversationMessage(Base):
    """One row per GitHub comment that Nudge has actually seen: either by you, or by someone else after your first comment on that issue.
    (source, external_id) is unique, so polling the same comment twice can never create a second row or a second notification."""
    __tablename__ = "conversation_messages"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_convmsg_source_external"),
                      Index("ix_convmsg_conversation_created", "conversation_id", "created_at"))
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    source: Mapped[str] = mapped_column(String(60), default="github:issue")
    external_id: Mapped[str] = mapped_column(String(60))             # GitHub comment id, stored as text
    author_username: Mapped[str] = mapped_column(String(120))
    is_from_me: Mapped[bool] = mapped_column(Boolean, default=False)
    mentions_me: Mapped[bool] = mapped_column(Boolean, default=False)
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column()                   # when the comment was written on GitHub (naive UTC)
    source_url: Mapped[str | None] = mapped_column(String(500))      # html_url of the comment, for "open on GitHub"
    seen_at: Mapped[datetime] = mapped_column(default=now)           # when Nudge first detected it

class Scan(Base):
    """One GitHub keyword scan. The CURRENT scan is simply the newest row with status COMPLETED; older rows stay as a tiny history.
    A scan never owns opportunities: result_post_ids only points (in display order) at PostOpportunity rows, which live on after the scan is replaced."""
    __tablename__ = "scans"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(60), default="github:issue")
    keyword: Mapped[str] = mapped_column(String(300))
    query: Mapped[str | None] = mapped_column(String(400))           # full query sent to GitHub (never contains the token)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")   # RUNNING COMPLETED FAILED
    started_at: Mapped[datetime] = mapped_column(default=now)
    completed_at: Mapped[datetime | None] = mapped_column()
    result_limit: Mapped[int] = mapped_column(default=30)
    matching_count: Mapped[int] = mapped_column(default=0)           # GitHub's total_count
    fetched_count: Mapped[int] = mapped_column(default=0)            # raw items received
    unique_count: Mapped[int] = mapped_column(default=0)             # usable issues after URL dedupe / bot / PR filtering
    analyzed_count: Mapped[int] = mapped_column(default=0)
    comment_count: Mapped[int] = mapped_column(default=0)            # COMMENT-worthy
    displayed_count: Mapped[int] = mapped_column(default=0)          # after author dedupe + top-N cut
    hidden_dismissed: Mapped[int] = mapped_column(default=0)
    pages: Mapped[int] = mapped_column(default=0)
    skipped_unusable: Mapped[int] = mapped_column(default=0)
    skipped_bots: Mapped[int] = mapped_column(default=0)
    incomplete: Mapped[bool] = mapped_column(Boolean, default=False)
    authenticated: Mapped[bool] = mapped_column(Boolean, default=False)
    warning: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    result_post_ids: Mapped[list] = mapped_column(JSON, default=list)   # PostOpportunity ids, best first

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
    actor: Mapped[str] = mapped_column(String(10), default="agent")  # agent | you | sent | github (a reply from someone else)
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