"""Resets the DB and fills it with realistic Flaky-Repro outreach data, then runs the agent once on top.
Usage: python seed.py"""
import os
os.environ["AGENT_STEP_DELAY"] = "0"
from datetime import timedelta
from app.db import Base, engine, SessionLocal, now
from app.models import *
from app.agent.pipeline import execute_run

def ago(**k): return now() - timedelta(**k)

Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
db = SessionLocal()
L = {}
def lead(key, name, role, co, score, status, act, tags, ev, why, url, hours, **kw):
    L[key] = Lead(name=name, role=role, company=co, relevance_score=score, status=status, recommended_action=act, tags=tags, reasons=ev,
                  analysis=why, profile_url=url, source="seed", last_activity_at=ago(hours=hours), created_at=ago(hours=hours + 6), **kw)
    db.add(L[key]); db.flush()

lead("daniel", "Daniel Okafor", "Staff Engineer", "Tessellate", 94, "AWAITING_APPROVAL", "DM", ["Python", "pytest", "CI infra", "Flaky tests"],
     ["Posted about intermittent failures 2 days ago", "Maintains pytest plugins at Tessellate", "Active in pytest-dev discussions"],
     "Wrote that a pytest integration suite fails about 1 in 20 runs on GitHub Actions and he cannot reproduce it locally. Maintains the team’s CI tooling.", "https://github.com/dokafor", 2)
lead("sarah", "Sarah Chen", "Software Engineer", "Loomfield", 91, "AWAITING_APPROVAL", "COMMENT", ["pytest", "GitHub Actions", "Flaky tests"],
     ["Open question about flaky pytest in CI", "Replies to others within hours", "Python-only stack"],
     "Asked publicly whether anyone else sees flaky pytest integration tests in GitHub Actions. Direct match for the problem Flaky-Repro solves.", "https://github.com/schen-loom", 5)
lead("marcus", "Marcus Lindqvist", "Test Infrastructure Lead", "Northbeam", 86, "AWAITING_APPROVAL", "DM", ["E2E", "CI infra", "Python"],
     ["Conference talk on flaky E2E tests", "Owns the CI platform team", "Uses pytest-xdist"],
     "Gave a talk on quarantining flaky E2E tests. Quarantine is a workaround; he may want root-cause tooling.", "https://github.com/mlindqvist", 24)
lead("priya", "Priya Raghavan", "Maintainer, pytest-retry-extras", "Open source", 82, "CONTACTED", "FOLLOW_UP", ["pytest", "OSS maintainer"],
     ["Maintains a pytest plugin", "Issue tracker has 14 flaky-test reports"],
     "Maintains a retry plugin, so she sees flaky tests daily. Contacted 6 days ago, no reply yet.", "https://github.com/praghavan", 144, last_contacted_at=ago(days=6))
lead("tomas", "Tomás Herrera", "Backend Engineer", "Quayside", 71, "INTERESTED", "DM", ["Python", "CI"],
     ["Asked a specific product question"], "Replied asking how Flaky-Repro differs from rerunning with pytest-rerunfailures.", "https://github.com/therrera", 3, last_contacted_at=ago(days=1))
lead("alex", "Alex Whitford", "Senior Python Engineer", "Brightlane", 34, "NEW", "IGNORE", ["Python"], ["No problem signals found"],
     "Not enough evidence — ignore. Job title matches, but no posts about tests or CI.", "https://github.com/awhitford", 96)
lead("mei", "Mei Tanaka", "Senior QA Engineer", "Kestrel", 79, "CONTACTED", "FOLLOW_UP", ["pytest", "E2E"], ["Replied politely to first note", "Runs pytest E2E suite"],
     "Said she would look this weekend; one gentle nudge is reasonable if she goes quiet.", "https://github.com/mtanaka-qa", 30, last_contacted_at=ago(days=2))
lead("jonas", "Jonas Weber", "Engineering Manager", "Ridgeline", 58, "NOT_INTERESTED", "IGNORE", ["CI"], ["Already uses a commercial tool"],
     "Replied that they already use a commercial tool. Do not follow up.", "https://github.com/jweber-eng", 96, last_contacted_at=ago(days=5))

def post(key, lead_key, who, role, co, url, text, days, score, sig, why, act):
    p = PostOpportunity(lead_id=L[lead_key].id, author=who, author_role=role, author_company=co, author_profile_url=L[lead_key].profile_url,
                        post_url=url, content=text, posted_at=ago(days=days), relevance_score=score, reasons=sig, why=why, recommended_action=act,
                        status="AWAITING_APPROVAL" if act == "COMMENT" else "NEW", source="seed")
    db.add(p); db.flush(); L[key] = p
post("p1", "sarah", "Sarah Chen", "Software Engineer", "Loomfield", "https://github.com/loomfield/ci/discussions/412",
     "Anyone else dealing with flaky pytest integration tests in GitHub Actions? Passes locally every time, fails roughly one run in twenty on CI.", 2, 94,
     ["pytest", "CI", "flaky tests", "active thread"], "Directly matches the problem Flaky-Repro investigates, and the question is still open.", "COMMENT")
post("p2", "daniel", "Daniel Okafor", "Staff Engineer", "Tessellate", "https://mastodon.social/@dokafor/113904",
     "Spent the day bisecting a test that only fails when run after test_billing. Shared state again. Why is this still so manual?", 3, 91,
     ["test ordering", "shared state", "pytest"], "Order-dependent failure, a core Flaky-Repro scenario. He also asked why it is still manual.", "COMMENT")
post("p3", "marcus", "Marcus Lindqvist", "Test Infrastructure Lead", "Northbeam", "https://northbeam.dev/blog/quarantine-debt",
     "We quarantined 40 flaky tests this quarter. Nobody wants to own fixing them.", 5, 77, ["quarantine", "E2E"],
     "Real pain, but no ask. A comment may read as a pitch, so the agent recommends a DM later.", "IGNORE")

def draft(lead_key, ttype, tid, action, text, why):
    d = Draft(lead_id=L[lead_key].id, target_type=ttype, target_id=tid, action_type=action, content=text, why=why); db.add(d); db.flush(); return d
draft("daniel", "lead", L["daniel"].id, "DM", "Hey Daniel, saw your post about the test that only fails after test_billing. I’m building Flaky-Repro, an open-source pytest CLI that reruns a failing test under different orderings and environments and reports what changed. It might save you the manual bisecting. Want me to point it at that test?", "Uses pytest and posted about order-dependent CI failures this week.")
draft("sarah", "post", L["p1"].id, "COMMENT", "I’ve run into this pattern too. Comparing the conditions of a passing run against a failing one (ordering, env, timing) usually narrows it faster than rerunning. I’m building an open-source pytest tool around that idea. Happy to try it on one of your failing tests if useful.", "Open question about the exact problem space, 2 days old.")
draft("marcus", "lead", L["marcus"].id, "DM", "Hi Marcus, your talk on quarantining flaky E2E tests resonated. I’m working on an open-source pytest tool that tries to explain why a test flakes instead of hiding it. Would you be open to trying it on one quarantined test?", "Talk on quarantining flaky E2E tests; likely wants root causes.")
draft("daniel", "post", L["p2"].id, "COMMENT", "Order-dependent failures are brutal to bisect by hand. Does it also change when you shuffle with a fixed seed? That tells you whether it is the ordering or the shared fixture.", "Order-dependent failure thread, still active.")
for k, days, due, text, why in [("priya", 6, timedelta(hours=-12), "Hey Priya, following up on my note last week. No pressure. If retries ever hide a failure you want to understand, Flaky-Repro might be a useful complement. Happy to share a quick example.", "Maintains a retry plugin; one gentle nudge is reasonable."),
                                ("mei", 2, timedelta(days=1, hours=2), "Hope the weekend test run went well. Happy to look at any output that looks odd.", "She said she would try it over the weekend; check in once if quiet.")]:
    f = FollowUp(lead_id=L[k].id, last_contacted_at=ago(days=days), due_at=now() + due); db.add(f); db.flush()
    draft(k, "followup", f.id, "FOLLOW_UP", text, why)

def convo(key, status, intent, msgs, read, nxt, hours):
    c = Conversation(lead_id=L[key].id, status=status, intent=intent, ai_interpretation=read, next_action=nxt, updated_at=ago(hours=hours),
                     messages=[{"from": w, "text": t, "at": ago(hours=hours).isoformat()} for w, t in msgs]); db.add(c); db.flush(); return c
c1 = convo("tomas", "INTERESTED", "High interest", [("me", "Hey Tomás, saw your note about CI flakiness. I’m building Flaky-Repro, an open-source pytest tool for investigating them."), ("them", "Interesting. How is this different from pytest-rerunfailures?")],
           "High interest. Asked how Flaky-Repro works.", "Explain the investigation workflow and offer to test it on one of his flaky tests.", 3)
draft("tomas", "conversation", c1.id, "DM", "rerunfailures hides a flake by retrying. Flaky-Repro reruns the test under varied conditions (order, seed, env) and tells you which one flips the result. If you have a test that flakes, send me the name and I’ll show you the output.", "He asked a direct product question.")
convo("mei", "WAITING", "Neutral", [("me", "Here is the repo link if you want to try it."), ("them", "Thanks, I’ll take a look this weekend.")], "Polite, no commitment yet.", "Wait until Monday, then send one follow-up.", 26)
convo("jonas", "NOT_INTERESTED", "Low interest", [("them", "We use a commercial tool already.")], "Not a fit right now.", "Do not follow up.", 96)

for typ, actor, text, h in [("draft_approved", "you", "You approved DM to Tomás Herrera (queued, not sent: no integration connected)", 28), ("reply_classified", "agent", "Agent classified reply from Tomás as HIGH INTENT", 3),
                            ("draft_generated", "agent", "Agent generated comment draft for Sarah Chen", 5), ("scored", "agent", "Agent scored Daniel Okafor 94/100", 6),
                            ("discovered", "agent", "Agent discovered Daniel Okafor from a public post", 6), ("ignored", "agent", "Agent decided to ignore Alex Whitford: not enough evidence", 24),
                            ("draft_rejected", "you", "You rejected a comment draft (too salesy)", 26), ("sent", "sent", "DM sent to Priya Raghavan", 144)]:
    db.add(Activity(type=typ, actor=actor, description=text, timestamp=ago(hours=h)))

db.add(AgentRun(status="COMPLETED", trigger="scheduled", started_at=ago(days=1, minutes=30), finished_at=ago(days=1, minutes=26), people_scanned=31, posts_analyzed=58, opportunities_found=9, drafts_generated=5))
db.add(AgentRun(status="COMPLETED", trigger="scheduled", started_at=ago(minutes=12), finished_at=ago(minutes=8), people_scanned=42, posts_analyzed=73, opportunities_found=12, drafts_generated=7))
run = AgentRun(trigger="seed"); db.add(run); db.commit(); rid = run.id; db.close()
execute_run(rid)   # one real pipeline pass over the mock adapter
print("Seeded. Pipeline run", rid, "added more leads/drafts on top.")
