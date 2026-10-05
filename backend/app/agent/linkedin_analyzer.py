"""Deterministic relevance analyzer for a recent public LinkedIn post. Pure function: no I/O, no DB, no LLM, no drafting.

Input is only what a search result gives us (title + snippet), so this scores *evidence of a problem*, not the whole post.

Scoring model (all weights are in SIGNALS / PENALTIES below, in one place to tune):
  1. Each matched signal has a weight in 0..1. Evidence is combined as 1 - prod(1 - w), so several independent signals
     reinforce each other but the score saturates smoothly instead of clipping at 100.
  2. "core" signals are actual problem evidence (flaky tests, passes locally/fails in CI, ordering, shared state, ...).
     "context" signals (pytest, CI, E2E, Python) only add weight on top of a core signal. "ask" (actively asking for help)
     counts only when a core signal is present, so a generic question about tooling never scores as a lead.
  3. Penalties are multipliers for posts that are not an open problem (already solved, advice/how-to, hiring, announcements).
Action: IGNORE below ACT_THRESHOLD, or with no core signal, or for hiring/announcement posts.
        COMMENT when the author is asking for help (an open thread a reply can genuinely help with).
        DM when there is a strong problem (>= DM_THRESHOLD) but no ask: a public reply to a no-ask post reads as a pitch.
"""
import re
from dataclasses import dataclass
from .linkedin_discovery import LinkedInPost

ACT_THRESHOLD = 50   # same cut-off the existing pipeline uses for IGNORE
DM_THRESHOLD = 60    # a cold DM needs stronger evidence than a public reply to an open question

_S = r"[^.!?\n]"   # stay inside one sentence
_CI = r"(?:ci|ci/cd|github actions|gha|jenkins|gitlab(?: ci)?|circleci|buildkite|azure pipelines|travis)"

# (key, regex, label, weight, kind)   kind: core | context | ask
SIGNALS = [
    ("flaky", r"flak(?:y|ey|iness)|\bflakes?\b|\bflaking\b", "Flaky tests", 0.55, "core"),
    ("intermittent", r"intermittent\w*|\bfail\w*\s+(?:randomly|sporadically)|\brandomly\s+fail\w*|\bsporadic\w*\s+fail\w*|non-?determinis\w*",
     "Intermittent failures", 0.50, "core"),
    ("local_vs_ci", r"\b(?:pass(?:es|ed|ing)?|work(?:s|ed)?|green|succe\w+)\b" + _S + r"{0,40}?\b(?:locally|on my (?:machine|laptop))\b"
                    r"|\blocally\b" + _S + r"{0,40}?\b(?:but|yet|while)\b" + _S + r"{0,40}?\bfail\w*",
     "Passes locally, fails in CI", 0.50, "core"),
    ("ci_failure", r"\b" + _CI + r"\b" + _S + r"{0,60}?\b(?:fail\w*|red|broke\w*|flak\w*|time ?outs?|timed out)\b"
                   r"|\b(?:fail\w*|red|broke\w*|flak\w*)\b" + _S + r"{0,40}?\b(?:in|on)\s+(?:\w+\s+){0,3}?" + _CI + r"\b",
     "CI failures", 0.35, "core"),
    ("ordering", r"test[- ]order\w*|order[- ]dependen\w*|\bonly fail\w*\b" + _S + r"{0,15}?\b(?:when|after)\b"
                 r"|\b(?:fails?|failed|failing)\s+(?:only\s+)?(?:when|after)\s+(?:run|running|executed)\b|\bafter test_\w+"
                 r"|random(?:ized|ised)?[- ]order|pytest-randomly|run (?:in|with) (?:a )?different order",
     "Test ordering", 0.45, "core"),
    ("shared_state", r"shared (?:state|fixtures?|db|database|resources?|mutable)|global state|state (?:leak\w*|bleed\w*|pollution)"
                     r"|leak\w* state|test pollution|(?:db|database) fixture",
     "Shared state", 0.40, "core"),
    ("hard_repro", r"\b(?:can't|cannot|can not|unable to|couldn't|could not)\s+(?:\w+\s+){0,2}?(?:reproduc\w*|repro\b)"
                   r"|hard to (?:reproduc\w*|debug|track down|pin down)|bisect\w*|impossible to (?:debug|reproduce)"
                   r"|no idea how to (?:even )?(?:start|narrow)",
     "Hard to reproduce", 0.30, "core"),
    ("quarantine", r"quarantin\w*|re-?run\w*|retry-on-fail|auto-?retr(?:y|ies)|\bretries\b|rerunfailures|\bmuted? tests?\b",
     "Reruns / quarantine", 0.30, "core"),
    ("asking", r"\?|\b(?:anyone|anybody)\s+(?:else|know|have|seen|using)\b|\bhow do (?:you|i|we|people)\b"
               r"|\bany (?:ideas|tips|advice|suggestions|pointers|recommendations)\b"
               r"|\b(?:need|needs|looking for|would love|appreciate)\s+(?:some\s+)?(?:help|advice|ideas|suggestions|input|tips)\b"
               r"|\bhelp (?:me|us|needed|wanted)\b|\bstruggling\b|\bwhat am i missing\b|\bno idea\b",
     "Asking for help", 0.25, "ask"),
    ("pytest", r"pytest|conftest|xdist", "pytest", 0.30, "context"),
    ("e2e", r"\be2e\b|end-to-end|integration (?:tests?|suite)|playwright|selenium|cypress", "Integration/E2E tests", 0.15, "context"),
    ("ci_context", r"\b" + _CI + r"\b", "CI", 0.10, "context"),
    ("python", r"\bpython\b|django|fastapi|flask", "Python", 0.05, "context"),
]

# (key, regex, label, multiplier, is_promo)
PENALTIES = [
    ("hiring", r"\bhiring\b|#hiring|\bwe(?:'re| are) looking for\b|\bopen (?:roles?|positions?)\b|\bjoin (?:our|the|my) team\b"
               r"|\bapply (?:now|here|today)\b|\bjob opening\b", "Hiring post", 0.25, True),
    ("announcement", r"\b(?:excited|thrilled|proud|pleased|delighted|happy) to (?:announce|introduce|launch)\b|\bannouncing\b"
                     r"|\bwe(?:'ve| have| just)\s+(?:just\s+)?(?:launched|released|shipped|published)\b|\bnow (?:available|live)\b"
                     r"|\bwebinar\b|\bregister (?:now|today|here)\b|\bjoin us (?:for|at|on)\b|\bnew role\b|\bcongratulat\w+",
     "Announcement", 0.25, True),
    ("advice", r"\b(?:top|best) \d+\b|\b\d+ (?:tips|ways|reasons|lessons|mistakes)\b|\bhere(?:'s| is) how (?:to|we)\b"
               r"|\bhow we (?:fixed|solved|reduced|cut|eliminated)\b|\b(?:a|the) (?:guide|tutorial|checklist)\b|\bblog post\b|\bread (?:more|our)\b",
     "Advice / how-to content", 0.60, False),
    ("solved", r"\bfinally (?:tracked|fixed|figured|found|solved)\b|\b(?:we|i) (?:fixed|solved|resolved)\b|\broot cause (?:was|is)\b"
               r"|\bturned out\b|\bno more flak\w*|\bzero flak\w*|\bgot rid of\b",
     "Already solved", 0.75, False),
]
_GENERIC = re.compile(r"\b(?:testing|test automation|qa)\s+(?:best practices|tips|trends|strategy|strategies|pyramid|tools|automation)\b"
                      r"|\bimportance of (?:testing|qa|quality)\b|\bshift[- ]left\b|\b(?:manual|exploratory|automated) testing\b|\btest automation\b")
_SIGNALS = [(k, re.compile(rx), label, w, kind) for k, rx, label, w, kind in SIGNALS]
_PENALTIES = [(k, re.compile(rx), label, m, promo) for k, rx, label, m, promo in PENALTIES]

@dataclass
class Analysis:
    score: int                  # 0-100
    signals: list[str]          # short labels, strongest first, then penalties; empty if nothing matched
    why: str
    recommended_action: str     # COMMENT | DM | IGNORE  (same strings as app.models.ACTIONS)

def _normalize(text: str) -> str:
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", text).strip().lower()

def analyze_post(post: LinkedInPost) -> Analysis:
    return analyze_text(f"{post.title}\n{post.snippet}")

def analyze_text(raw: str) -> Analysis:
    text = _normalize(raw or "")
    hit = [(k, label, w, kind) for k, rx, label, w, kind in _SIGNALS if rx.search(text)]
    has_core = any(kind == "core" for *_, kind in hit)
    used = [(k, label, w) for k, label, w, kind in hit if kind != "ask" or has_core]   # an ask only counts next to a real problem
    pen = [(k, label, m, promo) for k, rx, label, m, promo in _PENALTIES if rx.search(text)]

    miss = 1.0
    for _, _, w in used:
        miss *= 1 - w
    value = 1 - miss
    for _, _, m, _ in pen:
        value *= m
    score = int(value * 100 + 0.5)

    labels = [label for _, label, _ in sorted(used, key=lambda u: -u[2])]   # stable: ties keep declaration order
    generic = not has_core and bool(_GENERIC.search(text))
    if generic:
        labels.append("Generic testing content")
    labels += [label for _, label, _, _ in pen]

    keys = {k for k, _, _ in used} | {k for k, _, _, _ in pen}
    asking = "asking" in keys
    core_labels = [label for k, label, w, kind in sorted(hit, key=lambda h: -h[2]) if kind == "core"]
    top = ", ".join(l[0].lower() + l[1:] for l in core_labels[:2])   # keep acronyms: "fails in CI"
    promo = next((label for _, label, _, p in pen if p), None)

    if promo:
        return Analysis(score, labels, f"Promotional post ({promo.lower()}), not a problem signal.", "IGNORE")
    if not has_core:
        why = "Generic testing/QA content with no identifiable problem." if generic else "No identifiable flaky-test or CI problem in the post."
        return Analysis(score, labels, why, "IGNORE")
    if score < ACT_THRESHOLD:
        closed = " Reads as advice or an already-solved problem, not an open one." if keys & {"advice", "solved"} else ""
        return Analysis(score, labels, f"Weak evidence ({score}/100): {top}.{closed}", "IGNORE")
    if asking:
        return Analysis(score, labels, f"Open question about {top}. A specific technical reply adds value in the thread.", "COMMENT")
    if score >= DM_THRESHOLD:
        why = f"Describes {top} but does not ask for help; a public reply could read as a pitch, so a direct note is the lighter touch."
        if "solved" in keys:
            why += " It reads as already resolved, so keep it light."
        return Analysis(score, labels, why, "DM")
    return Analysis(score, labels, f"Relevant ({top}) but no open question, and the evidence is not strong enough for a cold DM.", "IGNORE")