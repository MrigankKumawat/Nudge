"""GitHub reply tracking: find the issues YOU commented on, read the comments after yours, and record them as Conversations.

STRICTLY READ-ONLY toward GitHub. Every request goes through GitHubReader.get(), which can only issue GET; there is no method that comments, replies, reacts,
opens a page or logs in. No HTML scraping and no OAuth: just the public REST API with the optional GITHUB_TOKEN from backend/.env.

Config (read at call time): GITHUB_USERNAME (required: your identity), GITHUB_TOKEN (optional but practically needed: 60 requests/hour without it),
GITHUB_TRACK_DAYS (default 30: how far back to look for issues you commented on).

How a check works (check_replies):
  1. Find issues you commented on, wherever the comment was made (Nudge's Open Issue link, GitHub directly, another tab):
       - Search API: `commenter:<you> -author:<you> is:issue updated:>=<cutoff>` (open AND closed issues; up to 300), plus
       - issues Nudge already knows: open GitHub conversations and approved/contacted opportunities (the search index lags by minutes).
  2. For each issue, GET /repos/{owner}/{repo}/issues/{number}/comments (only comments changed since the last check, for issues already tracked).
  3. Keep YOUR comments, and other people's comments written AFTER your first one. Earlier comments are context only and are never stored. Bots are skipped.
  4. Each comment is stored once: (source, external_id = GitHub comment id) is unique. A comment already stored is ignored, so repeat checks create no duplicate
     messages, no duplicate Activity rows and no new unread notifications. Only a genuinely new row can change unread / status.
  5. The issue is linked to its existing opportunity / lead. If Nudge has none (you commented on it outside Nudge), the minimum is created: one Lead (reused by
     GitHub profile URL, never duplicated) and one PostOpportunity. Nothing is ever fabricated: every message is a real GitHub comment.

Conversation states (GitHub conversations): WAITING_FOR_THEM (your comment is the latest), NEW_REPLY (someone replied and you have not read / answered it),
WAITING_FOR_ME (you marked it read), CLOSED (set by you; reopens on any new comment).
Lead / post status only ever moves FORWARD: NEW / AWAITING_APPROVAL / APPROVED -> CONTACTED when your comment is found, CONTACTED -> REPLIED when the lead
(the issue author) replies. IGNORED / NOT_RELEVANT / INTERESTED / NOT_INTERESTED are never touched, and drafts are never touched.
"""
import json, logging, os, re, threading, time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..db import now as db_now
from ..models import Lead, PostOpportunity, Conversation, ConversationMessage, Activity
from .github_adapter import (SOURCE, TOKEN_ENV, USER_AGENT, POST_TEXT_MAX, GitHubIssue, _urllib_get, _rate_limited, _parse_ts, is_bot, parse_issue,
                             canonical_issue_url, issue_key, split_issue_url)

log = logging.getLogger(__name__)

API = "https://api.github.com"
USERNAME_ENV, TRACK_DAYS_ENV = "GITHUB_USERNAME", "GITHUB_TRACK_DAYS"
DEFAULT_TRACK_DAYS = 30
MAX_SEARCH_PAGES = 3          # x100 = at most 300 issues found through search
MAX_COMMENT_PAGES = 5         # x100 = at most 500 comments read per issue
MAX_LOCAL_POSTS = 100         # approved / contacted opportunities re-checked on top of the search results
OVERLAP = timedelta(minutes=5)   # re-read a little before the last check, so a comment created while the previous check ran is never missed
SEARCH_PAGE_GAP = 0.3
FORWARD_FROM = ("NEW", "AWAITING_APPROVAL", "APPROVED")   # statuses that become CONTACTED once your comment is found
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_LOCK = threading.Lock()      # one check at a time (a double click must not run two overlapping polls)

class TrackerError(Exception):
    """A check could not run. `status_code` is the HTTP status the API layer should answer with."""
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message); self.status_code = status_code

class _RateLimited(Exception):
    def __init__(self, wait: int | None):
        super().__init__("rate limited"); self.wait = wait

class GitHubReader:
    """GET-only GitHub REST client. The token is sent only in the Authorization header and never logged or put in an error."""
    def __init__(self, token: str = "", http_get=_urllib_get, sleep=time.sleep, timeout: float = 30.0):
        self.token, self.http_get, self.sleep, self.timeout = token, http_get, sleep, timeout
        self.headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": USER_AGENT}
        if token: self.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str, params: dict | None = None) -> tuple[int, dict, object]:
        url = API + path + (("?" + urlencode(params)) if params else "")
        try:
            status, rh, body = self.http_get(url, self.headers, self.timeout)
        except OSError:
            raise TrackerError("Could not reach GitHub. Check your network connection and try again.", 502) from None
        if _rate_limited(status, rh, body):
            reset = rh.get("x-ratelimit-reset", "")
            raise _RateLimited(max(1, int(reset) - int(time.time())) if reset.isdigit() else None)
        if status == 401 and self.token:
            raise TrackerError("GITHUB_TOKEN was rejected by GitHub (HTTP 401); fix or remove it.", 400)
        try: data = json.loads(body) if body else None
        except ValueError: data = None
        return status, rh, data

@dataclass
class Target:
    url: str                       # canonical issue URL
    owner: str
    repo: str
    number: int
    issue: GitHubIssue | None = None   # known when it came from search; fetched on demand otherwise
    @property
    def ref(self) -> str: return f"{self.owner}/{self.repo}#{self.number}"

@dataclass
class Ctx:
    username: str
    mention: re.Pattern
    days: int
    new_mine: int = 0
    new_replies: int = 0
    new_mentions: int = 0
    created: int = 0
    touched: set = field(default_factory=set)
    replies: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    rate_limited: bool = False
    incomplete: bool = False

def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)

def _excerpt(text: str, n: int = 200) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"

def _config() -> tuple[str, str, int]:
    username = os.getenv(USERNAME_ENV, "").strip().lstrip("@")
    if not username:
        raise TrackerError(f"Set {USERNAME_ENV} in backend/.env (your GitHub username) so Nudge knows which comments are yours.", 400)
    if not _LOGIN.match(username):
        raise TrackerError(f"{USERNAME_ENV} is not a valid GitHub username.", 400)
    try: days = max(1, min(int(os.getenv(TRACK_DAYS_ENV, DEFAULT_TRACK_DAYS)), 365))
    except ValueError: days = DEFAULT_TRACK_DAYS
    return username, os.getenv(TOKEN_ENV, "").strip(), days

# ---- 1. which issues to look at ------------------------------------------------------------------------------------------------------------
def _gather(db: Session, reader: GitHubReader, ctx: Ctx) -> dict[str, Target]:
    targets: dict[str, Target] = {}
    cutoff = db_now() - timedelta(days=ctx.days)
    q = f"commenter:{ctx.username} -author:{ctx.username} is:issue updated:>={cutoff:%Y-%m-%d}"
    try:
        total = 0
        for page in range(1, MAX_SEARCH_PAGES + 1):
            status, rh, data = reader.get("/search/issues", {"q": q, "sort": "updated", "order": "desc", "per_page": 100, "page": page})
            items = data.get("items") if isinstance(data, dict) else None
            if status != 200 or not isinstance(items, list):
                ctx.warnings.append(f"Searching GitHub for your comments failed (HTTP {status}); checked only issues Nudge already knows."); break
            if page == 1: total = int(data.get("total_count") or 0)
            for item in items:
                issue = parse_issue(item, q)
                sp = split_issue_url(issue.url) if issue else None
                if issue and sp: targets[issue_key(issue.url)] = Target(issue.url, sp[0], sp[1], sp[2], issue)
            if len(items) < 100 or page * 100 >= total: break
            reader.sleep(SEARCH_PAGE_GAP)
        if total > MAX_SEARCH_PAGES * 100:
            ctx.incomplete = True
            ctx.warnings.append(f"You commented on {total} recently updated issues; only the {MAX_SEARCH_PAGES * 100} most recently updated were checked.")
    except _RateLimited as e:
        ctx.rate_limited = True
        ctx.warnings.append("GitHub search rate limit reached" + (f"; try again in about {e.wait}s" if e.wait else "") + "; checked only issues Nudge already knows.")
    closed: set[str] = set()
    for c in db.scalars(select(Conversation).where(Conversation.source == SOURCE, Conversation.issue_url.is_not(None))):
        key = issue_key(c.issue_url)
        if c.status == "CLOSED": closed.add(key); continue
        sp = split_issue_url(c.issue_url)
        if sp and key not in targets and (c.last_message_at or c.updated_at) >= cutoff:
            targets[key] = Target(canonical_issue_url(c.issue_url), *sp)
    for p in db.scalars(select(PostOpportunity).where(PostOpportunity.source == SOURCE, PostOpportunity.status.in_(("APPROVED", "CONTACTED"))).order_by(PostOpportunity.id.desc()).limit(MAX_LOCAL_POSTS)):
        key = issue_key(p.post_url)
        sp = split_issue_url(p.post_url)
        if sp and key not in targets and key not in closed: targets[key] = Target(canonical_issue_url(p.post_url), *sp)
    return targets

# ---- 2. read one issue's comments ----------------------------------------------------------------------------------------------------------
def _fetch_comments(reader: GitHubReader, tgt: Target, since: datetime | None, ctx: Ctx) -> list[dict] | None:
    out: list[dict] = []
    for page in range(1, MAX_COMMENT_PAGES + 1):
        params = {"per_page": 100, "page": page, **({"since": since.strftime("%Y-%m-%dT%H:%M:%SZ")} if since else {})}
        status, rh, data = reader.get(f"/repos/{tgt.owner}/{tgt.repo}/issues/{tgt.number}/comments", params)
        if status != 200 or not isinstance(data, list):
            ctx.skipped.append(dict(issue=tgt.ref, url=tgt.url, reason=f"GitHub returned HTTP {status} (deleted, transferred or private?)")); return None
        out += data
        if len(data) < 100: return out
    ctx.incomplete = True
    ctx.warnings.append(f"{tgt.ref} has more than {MAX_COMMENT_PAGES * 100} comments; only the first {MAX_COMMENT_PAGES * 100} were read.")
    return out

def _fetch_issue(reader: GitHubReader, tgt: Target) -> GitHubIssue | None:
    status, rh, data = reader.get(f"/repos/{tgt.owner}/{tgt.repo}/issues/{tgt.number}")
    return parse_issue(data, "reply tracking") if status == 200 and isinstance(data, dict) else None

# ---- 3. link to / create the lead + opportunity ---------------------------------------------------------------------------------------------
def _lead_login(lead: Lead) -> str:
    return (lead.profile_url or "").rstrip("/").rsplit("/", 1)[-1].lower()

def _find_convo(db: Session, url: str) -> Conversation | None:
    return db.scalar(select(Conversation).where(func.lower(Conversation.issue_url) == issue_key(url)))

def _lead_for(db: Session, name: str, profile_url: str, repo: str, at: datetime) -> Lead:
    """One person = one lead, keyed by GitHub profile URL (leads.profile_url is unique)."""
    lead = db.scalar(select(Lead).where(Lead.profile_url == profile_url))
    if lead is None:
        lead = Lead(name=name[:120], profile_url=profile_url, company=repo[:120], role="", relevance_score=0, reasons=[], tags=[], status="NEW", recommended_action="COMMENT",
                    analysis="Tracked from your own GitHub comment; not scored by a Nudge scan.", source=SOURCE, last_activity_at=at)
        db.add(lead); db.flush()
    return lead

def _post_and_lead(db: Session, reader: GitHubReader, tgt: Target) -> tuple[PostOpportunity, Lead] | None:
    post = db.scalar(select(PostOpportunity).where(func.lower(PostOpportunity.post_url) == issue_key(tgt.url)))
    if post is None:
        issue = tgt.issue or _fetch_issue(reader, tgt)
        if issue is None:
            return None
        lead = _lead_for(db, issue.author_login, issue.author_url, issue.repository, _naive(issue.updated_at))
        post = PostOpportunity(lead_id=lead.id, author=issue.author_login[:120], author_role="", author_company=issue.repository[:120], author_profile_url=issue.author_url,
                               post_url=issue.url, content=f"{issue.title}\n\n{issue.body}".strip()[:POST_TEXT_MAX], posted_at=_naive(issue.updated_at), relevance_score=0,
                               reasons=[], why="You commented on this issue directly on GitHub (found by reply tracking).", recommended_action="COMMENT", status="NEW", source=SOURCE)
        db.add(post); db.flush()
        return post, lead
    lead = db.get(Lead, post.lead_id) if post.lead_id else None
    if lead is None:
        lead = _lead_for(db, post.author, post.author_profile_url or f"https://github.com/{post.author}", post.author_company or "", post.posted_at)
        post.lead_id = lead.id
    return post, lead

# ---- 4. ingest one issue -------------------------------------------------------------------------------------------------------------------
def _process(db: Session, reader: GitHubReader, tgt: Target, ctx: Ctx) -> None:
    convo = _find_convo(db, tgt.url)
    started = db_now()
    comments = _fetch_comments(reader, tgt, (convo.last_checked_at - OVERLAP) if convo and convo.last_checked_at else None, ctx)
    if comments is None: return
    rows = db.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == convo.id)).all() if convo else []
    rows.sort(key=lambda m: (m.created_at, int(m.external_id)))
    have = {m.external_id for m in rows}
    parsed = []
    for c in comments:
        user, created = c.get("user") or {}, _parse_ts(c.get("created_at"))
        login, cid = user.get("login") or "ghost", c.get("id")
        if not isinstance(cid, int) or created is None: continue
        mine = login.lower() == ctx.username.lower()
        if not mine and is_bot(user): continue                                  # automation noise is never a reply
        body = c.get("body") or ""
        parsed.append(dict(id=cid, login=login, mine=mine, created=_naive(created), body=body, url=c.get("html_url"), mention=(not mine) and bool(ctx.mention.search(body))))
    mine_keys = [(m.created_at, int(m.external_id)) for m in rows if m.is_from_me] + [(p["created"], p["id"]) for p in parsed if p["mine"]]
    if not mine_keys:                                                           # you have not commented here: nothing to track
        if convo: convo.last_checked_at = started; db.commit()
        return
    first_mine = min(mine_keys)
    fresh = sorted((p for p in parsed if str(p["id"]) not in have and (p["mine"] or (p["created"], p["id"]) > first_mine)), key=lambda p: (p["created"], p["id"]))
    if not fresh:
        if convo: convo.last_checked_at = started; db.commit()
        return
    pl = _post_and_lead(db, reader, tgt)
    if pl is None:
        ctx.skipped.append(dict(issue=tgt.ref, url=tgt.url, reason="could not read the issue itself (pull request, deleted or private?)")); return
    post, lead = pl
    if convo is None:
        convo = Conversation(lead_id=lead.id, status="WAITING_FOR_THEM", source=SOURCE, issue_url=canonical_issue_url(post.post_url), post_id=post.id)
        db.add(convo); db.flush(); ctx.created += 1
    prev = rows[-1] if rows else None
    prev_from_me, prev_login = (prev.is_from_me, prev.author_username) if prev else (None, "")
    new_other = new_mine = 0
    for p in fresh:
        db.add(ConversationMessage(conversation_id=convo.id, source=SOURCE, external_id=str(p["id"]), author_username=p["login"], is_from_me=p["mine"], mentions_me=p["mention"],
                                   body=p["body"], created_at=p["created"], source_url=p["url"]))
        meta = dict(lead_id=lead.id, conversation_id=convo.id, post_id=post.id, comment_id=p["id"], issue_url=convo.issue_url)
        if p["mine"]:
            new_mine += 1
            if prev_from_me is False:
                db.add(Activity(type="followed_up", actor="you", timestamp=p["created"], meta=meta, description=f"You replied to {prev_login} on {tgt.ref}"))
            else:
                db.add(Activity(type="comment_detected", actor="you", timestamp=p["created"], meta=meta, description=f"You commented on {tgt.ref}"))
            if lead.status in FORWARD_FROM: lead.status = "CONTACTED"            # forward only; dismissed / interested / replied are never touched
            if post.status in FORWARD_FROM: post.status = "CONTACTED"
            lead.last_contacted_at = max(lead.last_contacted_at or p["created"], p["created"])
        else:
            new_other += 1
            kind = "mention_detected" if p["mention"] else "reply_detected"
            db.add(Activity(type=kind, actor="github", timestamp=p["created"], meta={**meta, "author": p["login"]},
                            description=f"{p['login']} mentioned you on {tgt.ref}" if p["mention"] else f"{p['login']} replied to your GitHub comment on {tgt.ref}"))
            if lead.status == "CONTACTED" and _lead_login(lead) == p["login"].lower(): lead.status = "REPLIED"
            ctx.replies.append(dict(conversation_id=convo.id, author=p["login"], body=_excerpt(p["body"]), mentions_me=p["mention"], at=p["created"].isoformat(),
                                    comment_url=p["url"], issue_url=convo.issue_url, repository=tgt.owner + "/" + tgt.repo, number=tgt.number,
                                    title=(post.content or "").partition("\n\n")[0].strip()))
            if p["mention"]: ctx.new_mentions += 1
        lead.last_activity_at = max(lead.last_activity_at or p["created"], p["created"])
        prev_from_me, prev_login = p["mine"], p["login"]
    db.flush()                                                                  # a duplicate comment id would fail here (UNIQUE), before anything is committed
    last = db.scalar(select(ConversationMessage).where(ConversationMessage.conversation_id == convo.id).order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc()).limit(1))
    convo.last_message_at, convo.last_message_author, convo.last_message_from_me = last.created_at, last.author_username, last.is_from_me
    if last.is_from_me:                                                         # you have the last word: nothing to read or answer
        convo.status, convo.unread = "WAITING_FOR_THEM", False
    elif new_other:                                                             # someone replied and you have not answered
        convo.status, convo.unread = "NEW_REPLY", True
    convo.last_checked_at = started
    db.commit()
    ctx.new_mine += new_mine; ctx.new_replies += new_other; ctx.touched.add(convo.id)

# ---- public API ---------------------------------------------------------------------------------------------------------------------------
def unread_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Conversation).where(Conversation.source == SOURCE, Conversation.unread.is_(True))) or 0

def check_replies(db: Session, reader: GitHubReader | None = None) -> dict:
    """Manual 'Check for replies'. Persists everything it finds (one commit per issue, so a rate limit midway keeps the progress) and returns what was NEW."""
    username, token, days = _config()
    if not _LOCK.acquire(blocking=False):
        raise TrackerError("A reply check is already running.", 409)
    try:
        reader = reader or GitHubReader(token)
        ctx = Ctx(username=username, days=days, mention=re.compile(rf"(?<![\w-])@{re.escape(username)}(?![\w-])", re.I))
        if token:                                                               # sanity check only: warn if the token belongs to someone else
            try:
                status, _, me = reader.get("/user")
                if status == 200 and isinstance(me, dict) and (me.get("login") or "").lower() != username.lower():
                    ctx.warnings.append(f"GITHUB_TOKEN belongs to @{me.get('login')}, but {USERNAME_ENV} is @{username}. Comments are matched on {USERNAME_ENV}.")
            except _RateLimited: pass
        targets = _gather(db, reader, ctx)
        checked = 0
        for tgt in targets.values():
            try:
                _process(db, reader, tgt, ctx); checked += 1
            except _RateLimited as e:
                db.rollback(); ctx.rate_limited = True; ctx.incomplete = True
                ctx.warnings.append("GitHub rate limit reached" + (f"; try again in about {e.wait}s" if e.wait else "") + f". Checked {checked} of {len(targets)} issues; the rest wait for the next check.")
                break
            except IntegrityError:
                db.rollback(); ctx.warnings.append(f"{tgt.ref}: a comment was already recorded by another check; skipped.")
        log.info("Reply check for @%s: %d issues, %d new comments by you, %d new replies, %d new conversations", username, checked, ctx.new_mine, ctx.new_replies, ctx.created)
        return dict(username=username, authenticated=bool(token), window_days=days, issues_checked=checked, issues_found=len(targets), new_comments=ctx.new_mine, new_replies=ctx.new_replies,
                    new_mentions=ctx.new_mentions, conversations_created=ctx.created, conversations_updated=len(ctx.touched), replies=ctx.replies, skipped=ctx.skipped,
                    warnings=ctx.warnings, rate_limited=ctx.rate_limited, incomplete=ctx.incomplete, unread_total=unread_count(db))
    finally:
        _LOCK.release()

def mark_read(db: Session, convo: Conversation) -> Conversation:
    """You have read it: clears unread; a NEW_REPLY becomes WAITING_FOR_ME. Idempotent."""
    convo.unread = False
    if convo.status == "NEW_REPLY": convo.status = "WAITING_FOR_ME"
    db.commit()
    return convo

def set_status(db: Session, convo: Conversation, status: str) -> Conversation:
    """Manual state change. CLOSED (done with it) and the two waiting states are allowed; NEW_REPLY only comes from a real new comment."""
    if status not in ("CLOSED", "WAITING_FOR_ME", "WAITING_FOR_THEM"):
        raise TrackerError("Status must be CLOSED, WAITING_FOR_ME or WAITING_FOR_THEM.", 422)
    convo.status = status
    if status == "CLOSED": convo.unread = False
    db.commit()
    return convo