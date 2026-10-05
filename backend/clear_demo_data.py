"""Removes the DEMO data (seed.py records and mock-candidate leftovers) and keeps everything the real agent found.
Dry run by default: prints what it WOULD delete. Add --yes to apply. A backup copy of the SQLite file is made first.
Usage (from backend/):   python clear_demo_data.py          # preview
                         python clear_demo_data.py --yes    # delete
Real records (source "linkedin:search") are never touched. Stop uvicorn first if a run is in progress."""
import shutil, sys
from datetime import datetime
from sqlalchemy import select, delete
from app.db import engine, SessionLocal
from app.models import Lead, PostOpportunity, Draft, Conversation, FollowUp, Activity, AgentRun

REAL_SOURCE = "linkedin:search"
SEED_FEED = {   # the fixed activity-feed lines written by seed.py
    "You approved DM to Tomás Herrera (queued, not sent: no integration connected)", "Agent classified reply from Tomás as HIGH INTENT",
    "Agent generated comment draft for Sarah Chen", "Agent scored Daniel Okafor 94/100", "Agent discovered Daniel Okafor from a public post",
    "Agent decided to ignore Alex Whitford: not enough evidence", "You rejected a comment draft (too salesy)", "DM sent to Priya Raghavan"}
SEED_RUN_COUNTS = {(31, 58, 9, 5), (42, 73, 12, 7)}   # the two fake "scheduled" runs seed.py inserts

apply = "--yes" in sys.argv
with SessionLocal() as db:
    if db.scalar(select(AgentRun).where(AgentRun.status == "RUNNING")):
        raise SystemExit("An agent run is in progress. Wait for it to finish, then run this again.")
    demo_leads = [l for l in db.scalars(select(Lead)) if l.source == "seed" or (l.source or "").startswith("mock:")]
    ids = [l.id for l in demo_leads]
    posts = [p for p in db.scalars(select(PostOpportunity)) if p.lead_id in ids or p.source == "seed" or (p.source or "").startswith("mock:")]
    drafts = [d for d in db.scalars(select(Draft)) if d.lead_id in ids]
    convos = [c for c in db.scalars(select(Conversation)) if c.lead_id in ids]
    fups = [f for f in db.scalars(select(FollowUp)) if f.lead_id in ids]
    runs = [r for r in db.scalars(select(AgentRun)) if r.trigger == "seed" or (r.trigger == "scheduled" and
            (r.people_scanned, r.posts_analyzed, r.opportunities_found, r.drafts_generated) in SEED_RUN_COUNTS)]
    run_prefixes = tuple(f"Agent run #{r.id} " for r in runs)
    acts = [a for a in db.scalars(select(Activity)) if (a.meta or {}).get("lead_id") in ids or a.description in SEED_FEED
            or (run_prefixes and a.description.startswith(run_prefixes))]

    keep = db.scalars(select(Lead).where(Lead.source == REAL_SOURCE)).all()
    print(f"{'DELETING' if apply else 'WOULD DELETE'}: {len(demo_leads)} demo leads, {len(posts)} posts, {len(drafts)} drafts, "
          f"{len(convos)} conversations, {len(fups)} follow-ups, {len(acts)} activity lines, {len(runs)} fake agent runs")
    print(f"KEEPING: {len(keep)} real leads from the LinkedIn search" + "".join(f"\n   - {l.name} ({l.recommended_action}, {l.relevance_score}/100)" for l in keep[:15]))
    if not apply:
        print("\nNothing changed. Re-run with --yes to apply."); raise SystemExit(0)

    path = engine.url.database if engine.url.get_backend_name() == "sqlite" else None
    if path:
        bak = f"{path}.bak-{datetime.now():%Y%m%d-%H%M%S}"; shutil.copyfile(path, bak); print(f"\nBackup written: {bak}")
    for rows in (acts, drafts, fups, convos, posts, demo_leads, runs):   # dependents first
        for row in rows: db.delete(row)
    db.commit()
    print("Done. Restart the frontend page to see the cleaned data.")