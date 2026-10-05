"""discover -> research -> score -> decide action -> generate draft -> save.
The agent only ever creates drafts with status AWAITING_APPROVAL. It never contacts anyone."""
import os, re, time
from datetime import timedelta
from sqlalchemy import select
from ..db import SessionLocal, now
from ..models import Lead, PostOpportunity, Draft, FollowUp, Activity, AgentRun
from .adapters import RawCandidate
from .linkedin_adapter import LinkedInDiscovery
from .linkedin_discovery import MAX_AGE
from .github_adapter import GitHubIssueDiscovery
from .github_commenter import github_comment_text

# Real discovery only (SerpApi -> public LinkedIn posts; GitHub REST issue search). MockDiscovery is deliberately NOT here: a real run never creates fake candidates.
# Without SERPAPI_API_KEY, LinkedInDiscovery raises DiscoveryConfigError and the run is marked FAILED with a clear error.
# GITHUB_TOKEN is optional for GitHubIssueDiscovery (higher rate limit only). Adapter order matters: candidates are flattened in this order before the AGENT_MAX_NEW cut.
ADAPTERS = [LinkedInDiscovery(), GitHubIssueDiscovery()]

# (key, regex, evidence label, short tag, weight)
SIGNALS = [
    ("pytest", r"pytest|conftest|xdist", "Uses pytest", "pytest", 22),
    ("flaky", r"flak(y|iness)|intermittent|randomly|only fails|nondetermin|green locally|passes locally|red on", "Discussed flaky or intermittent failures", "Flaky tests", 28),
    ("ci", r"\bci\b|github actions|jenkins|gitlab ci|circleci|pipeline", "Works with CI", "CI", 14),
    ("e2e", r"e2e|end-to-end|integration test|playwright|selenium", "Writes integration/E2E tests", "E2E", 10),
    ("python", r"python|django|fastapi", "Python stack", "Python", 8),
    ("infra", r"test infra|\bsre\b|sdet|qa automation|platform (team|engineer)", "Role touches test/CI infrastructure", "Test infra", 8),
    ("oss", r"maintainer|open[- ]source", "Open-source maintainer", "OSS maintainer", 6),
    ("rerun", r"rerun|retry|retries|quarantin", "Works around flakes with reruns", "Reruns", 8),
]

def ago_h(h: float) -> str:
    return f"{int(h)}h ago" if h < 48 else f"{int(h // 24)} days ago"

# ---- research: pull signals out of bio + (fresh) post text -------------------
def research(c: RawCandidate) -> dict:
    fresh = bool(c.post_text) and (c.post_age_hours or 0) <= MAX_AGE.total_seconds() / 3600
    post = (c.post_text or "").lower() if fresh else ""
    text = f"{c.bio} {c.role} {post}".lower()
    sigs = [(l, t, w, k) for k, rx, l, t, w in SIGNALS if re.search(rx, text)]
    post_keys = {k for k, rx, *_ in SIGNALS if post and re.search(rx, post)}
    return dict(fresh=fresh, sigs=sigs, post_keys=post_keys, question="?" in post,
                promo=bool(re.search(r"hiring|excited to announce", post)))

# ---- score: problem signals beat titles -------------------------------------
def score(c: RawCandidate, r: dict) -> tuple[int, list[str]]:
    s = sum(w for _, _, w, _ in r["sigs"])
    ev = [l for l, *_ in r["sigs"]]
    if r["fresh"]:
        s += 6 if c.post_age_hours <= 72 else 3
        ev.append(f"Posted {ago_h(c.post_age_hours)}")
        if r["question"]:
            s += 6; ev.append("Asked an open question")
    elif c.post_text:
        ev.append(f"Post is older than {MAX_AGE.days} days, not used")
    if r["promo"]:
        s -= 25; ev.append("Promotional post, not a problem signal")
    if not ev:
        ev.append("No problem signals found")
    return max(0, min(100, s)), ev

# ---- decide ------------------------------------------------------------------
def decide(s: int, r: dict) -> str:
    if s < 50: return "IGNORE"
    if r["fresh"] and "flaky" in r["post_keys"] and not r["promo"]: return "COMMENT"
    return "DM"

def explain(c, r, s, action) -> str:
    first = c.name.split()[0]
    tags = [t.lower() for _, t, _, _ in r["sigs"]]
    if action == "IGNORE":
        got = ", ".join(tags) if tags else "nothing relevant"
        return f"Not enough evidence — ignore. Matched only: {got}. No flaky-test or CI instability signal."
    if action == "COMMENT":
        return f"{first} posted {ago_h(c.post_age_hours)} about intermittent test failures. A specific technical reply adds more than a cold DM and keeps the conversation public."
    top = ", ".join(tags[:3])
    why = f"Profile shows {top}, but no fresh public thread to reply to. A direct note is the best route."
    if r["fresh"]: why = f"Posted about CI/testing but not directly about flakiness. {why}"
    return why + ("" if s >= 65 else " Confidence is moderate.")

# ---- draft generation (no generic compliments, always a concrete technical point)
def comment_text(c) -> str:
    t = (c.post_text or "").lower()
    if re.search(r"order|shared|fixture|xdist|state", t):
        b = "Order-dependent failures usually come down to shared state between tests. Re-running the failing test with a fixed seed and shuffled order tells you whether it's the ordering or one specific fixture."
    elif re.search(r"timeout|playwright|selenium|e2e|end-to-end", t):
        b = "With E2E flakes the useful question is what differed between the passing and the failing run: timing, data or environment. Capturing those side by side beats rerunning blindly."
    elif re.search(r"jenkins|actions|\bci\b|pipeline|midnight|time", t):
        b = "When it only fails on CI, the difference is usually environment, parallelism or the clock rather than the test body. Diffing env vars, worker count, test order and timestamps between a green and a red run narrows it quickly."
    else:
        b = "Intermittent failures are easiest to debug when you can compare a passing and a failing run under controlled conditions. Rerunning with varied order, seed and environment shows which one flips the result."
    return b + " I'm building an open-source pytest tool, Flaky-Repro, that automates that comparison. Happy to run it on one of your failing tests if that helps."

def dm_text(c, r) -> str:
    first = c.name.split()[0]
    if r["fresh"]:
        hook = f"saw your post: “{c.post_text[:80].rstrip()}…”"
    else:
        hook = f"your profile mentions {', '.join(t.lower() for _, t, _, _ in r['sigs'][:2])}"
    return (f"Hey {first}, {hook} I'm building Flaky-Repro, an open-source pytest CLI that reruns a failing test under different orderings "
            "and environments and reports which condition changed the result. Would you be open to trying it on one flaky test? Happy to share what it finds either way.")

def followup_text(name: str) -> str:
    return (f"Hey {name.split()[0]}, following up on my note last week. No pressure at all. If a flaky test is ever eating your time, "
            "Flaky-Repro might help find the cause. Happy to share a quick example.")

# ---- save --------------------------------------------------------------------
def log(db, type_, desc, actor="agent", **meta):
    db.add(Activity(type=type_, actor=actor, description=desc, meta=meta))

def save(db, c, r, s, ev, action, why) -> bool:
    tags = [t for _, t, _, _ in r["sigs"]]
    if c.source == "github:issue":
        draft_text = github_comment_text(c) if action == "COMMENT" else None
    else:
        draft_text = comment_text(c) if action == "COMMENT" else dm_text(c, r) if action == "DM" else None
    lead = Lead(name=c.name, profile_url=c.profile_url, company=c.company, role=c.role, relevance_score=s, reasons=ev, tags=tags,
                analysis=why, status="AWAITING_APPROVAL" if draft_text else "NEW", recommended_action=action, source=c.source,
                last_activity_at=now() - timedelta(hours=c.post_age_hours or 0))
    db.add(lead); db.flush()
    log(db, "discovered", f"Agent discovered {c.name} via {c.source}", lead_id=lead.id)
    post = None
    if r["fresh"]:
        post = PostOpportunity(lead_id=lead.id, author=c.name, author_role=c.role, author_company=c.company, author_profile_url=c.profile_url,
                               post_url=c.post_url, content=c.post_text, posted_at=now() - timedelta(hours=c.post_age_hours), relevance_score=s,
                               reasons=tags, why=why, recommended_action=action, source=c.source,
                               status="AWAITING_APPROVAL" if draft_text else "NEW")
        db.add(post); db.flush()
    log(db, "scored", f"Agent scored {c.name} {s}/100", lead_id=lead.id, score=s)
    if draft_text:
        tt, tid = ("post", post.id) if action == "COMMENT" else ("lead", lead.id)
        db.add(Draft(lead_id=lead.id, target_type=tt, target_id=tid, action_type=action, content=draft_text, why=why, status="AWAITING_APPROVAL"))
        log(db, "draft_generated", f"Agent generated {'comment' if action == 'COMMENT' else 'DM'} draft for {c.name}", lead_id=lead.id)
        return True
    log(db, "ignored", f"Agent decided to ignore {c.name}: not enough evidence", lead_id=lead.id)
    return False

def followup_pass(db, run) -> None:
    cutoff = now() - timedelta(days=5)
    open_ids = set(db.scalars(select(FollowUp.lead_id).where(FollowUp.status == "DUE")))
    for l in db.scalars(select(Lead).where(Lead.status == "CONTACTED", Lead.last_contacted_at <= cutoff)):
        if l.id in open_ids: continue
        fu = FollowUp(lead_id=l.id, last_contacted_at=l.last_contacted_at, due_at=now())
        db.add(fu); db.flush()
        db.add(Draft(lead_id=l.id, target_type="followup", target_id=fu.id, action_type="FOLLOW_UP", content=followup_text(l.name),
                     why=f"Contacted {(now() - l.last_contacted_at).days} days ago with no reply. One gentle nudge is reasonable."))
        log(db, "followup_due", f"Agent drafted a follow-up for {l.name}", lead_id=l.id)
        run.drafts_generated += 1

def assess(c, r) -> tuple[int, list[str], str, str]:
    """(score, evidence, action, why). A candidate carrying an `analysis` (LinkedIn) is already scored; the rest use the rules above."""
    a = getattr(c, "analysis", None)
    if a is None:
        s, ev = score(c, r)
        action = decide(s, r)
        return s, ev, action, explain(c, r, s, action)
    r["sigs"] = [(label, label, 0, label) for label in a.signals]   # analyzer signals become the lead's tags
    return a.score, list(a.signals), a.recommended_action, a.why

def select_candidates(cands: list, limit: int) -> list:
    """Best first, then cut to AGENT_MAX_NEW. Uses the score the adapter already computed during discovery (`analysis`, both LinkedIn and GitHub),
    so the 287th GitHub issue is never researched just because it came first in list order.
    Actionable (COMMENT/DM) candidates rank ahead of IGNORE ones, so an IGNORE can never take a slot an opportunity could have had; it only fills
    leftover slots. Stable sort: ties keep discovery order (GitHub: newest update first; LinkedIn: already score-sorted, so its order is unchanged).
    A candidate with no analysis (dev fixtures only) ranks after scored actionable ones and before IGNOREs."""
    def rank(c):
        a = getattr(c, "analysis", None)
        return (a.recommended_action == "IGNORE", -a.score) if a is not None else (False, 0)
    return sorted(cands, key=rank)[:limit]

def execute_run(run_id: int) -> None:
    delay = float(os.getenv("AGENT_STEP_DELAY", "0.4"))
    max_new = int(os.getenv("AGENT_MAX_NEW", "4"))
    db = SessionLocal()
    try:
        run = db.get(AgentRun, run_id)
        seen = set(db.scalars(select(Lead.profile_url)))
        seen_posts = set(db.scalars(select(PostOpportunity.post_url)))
        found = [(a, a.discover()) for a in ADAPTERS]                                                        # 1. discover
        for a, _ in found:
            if getattr(a, "summary", None) and a.summary(): log(db, "agent_run", a.summary())                # funnel counts, visible in the feed
        fresh = [c for _, cs in found for c in cs if c.profile_url not in seen and c.post_url not in seen_posts]
        cands = select_candidates(fresh, max_new)                                                            # best-scored first, then cut to AGENT_MAX_NEW
        if len(fresh) > len(cands): log(db, "agent_run", f"Selected the {len(cands)} best-scored of {len(fresh)} new candidates (AGENT_MAX_NEW={max_new})")
        for c in cands:
            time.sleep(delay)
            r = research(c)                                                                                  # 2. research
            s, ev, action, why = assess(c, r)                                                                # 3-4. score + decide
            made = save(db, c, r, s, ev, action, why)                                                        # 5-6. draft + save
            run.people_scanned += 1
            run.posts_analyzed += 1 if c.post_text else 0
            run.opportunities_found += 1 if action != "IGNORE" else 0
            run.drafts_generated += 1 if made else 0
            db.commit()
        followup_pass(db, run)
        run.status = "COMPLETED"
    except Exception as e:  # keep the failure visible in the UI instead of crashing the server
        db.rollback()
        run = db.get(AgentRun, run_id); run.status = "FAILED"; run.error = str(e)
    finally:
        run.finished_at = now()
        log(db, "agent_run", f"Agent run #{run.id} {run.status.lower()}: {run.people_scanned} people, {run.drafts_generated} drafts" + (f" ({run.error})" if run.error else ""))
        db.commit(); db.close()