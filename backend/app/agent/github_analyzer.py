"""GitHub pain qualification and scoring layer. Pure function: no I/O, no DB, no LLM, no drafting.

Scores evidence actually present in a GitHub issue (title, body, labels, comments, author, updated_at).
Positive signals:
  +25 concrete flaky/intermittent failure
  +15 retry/rerun evidence
  +15 CI failure
  +15 reproduction difficulty
  +10 passes alone / fails in parallel
  +10 relevant test framework
  +10 updated within 3 days
  +5  updated within 7 days
  +5  multiple occurrences
  +5  active investigation
  +5  technical failure details

Negative signals:
  -30 bot/automation author
  -20 clearly solved
  -15 generic/meta discussion
  -15 educational/non-problem content

Score clamped to 0..100.
Classification:
  80-100 = COMMENT
  60-79  = COMMENT
  40-59  = IGNORE
  0-39   = IGNORE
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Regex rules for positive signals: (key, weight, label, list_of_regex_strings)
POSITIVE_RULES = [
    ("flaky_concrete", 25, "Concrete flaky/intermittent failure", [
        r"\b(?:flak(?:y|ey|iness)|intermittent(?:ly)?|sporadic(?:ally)?|nondeterministic|non-deterministic|unstable)\s+(?:tests?|failures?|specs?|runs?|jobs?|behavior|behaviour|builds?|cases?|suites?|step)\b",
        r"\b(?:tests?|specs?|runs?|jobs?|builds?|cases?|suites?)\s+(?:are|is|were|was)?\s*(?:flak(?:y|ey|iness)|intermittent(?:ly)?|sporadic(?:ally)?|nondeterministic|unstable)\b",
        r"\b(?:fails?|failing|failed)\s+(?:intermittent(?:ly)?|randomly|sporadically|sometimes|occasionally|unpredictably)\b",
        r"\b(?:random|intermittent|sporadic)\s+(?:failures?|fails?)\b",
        r"\bfails?\s+\d+\s+(?:in|out of)\s+\d+\b",
        r"\bflak(?:ing|es)\b",
    ]),
    ("retry_evidence", 15, "Retry/rerun evidence", [
        r"\bpass(?:es|ed|ing)?\s+on\s+(?:re-?try|re-?run|attempt)\b",
        r"\bfail(?:s|ed|ing)?\s+(?:on|first)\s+(?:re-?try|attempt|run)\b",
        r"\b(?:re-?run(?:s|ning|ned)?|re-?tr(?:y|ies|ying|ied))\b",
        r"\b(?:retries:\s*\d+|--repeat-each|gh run rerun|rerunfailures|retry-on-fail(?:ure)?|auto-?retr(?:y|ies))\b",
        r"\bpasses?\s+on\s+second\s+(?:try|run|attempt)\b",
    ]),
    ("ci_failure", 15, "CI failure", [
        r"\b(?:pass(?:es|ed|ing)?|green|works?|work(?:s|ed)?)\s+(?:fine\s+)?locally\b[^.\n]{0,60}?\b(?:fails?|failed|failing|red|breaks?|broken)\s+(?:in|on)\s+(?:ci|github actions|gha|jenkins|gitlab|circleci|buildkite|runner)\b",
        r"\b(?:fails?|failed|failing|broken|red)\s+(?:in|on|under)\s+(?:ci|github actions|gha|jenkins|gitlab(?:-ci)?|circleci|buildkite|travis|azure pipelines?|pipeline|actions runner)\b",
        r"\b(?:ci|github actions|gha|jenkins|gitlab|circleci)\s+(?:only|build|run|job|runner|workflow|pipeline)\b[^.\n]{0,40}?\b(?:fails?|failed|failing|red|breaks?)\b",
        r"\bfails? in CI\b",
        r"\bCI failure\b|\bCI failures\b",
        r"\bCI only\b|\bseen only on CI\b",
        r"\bfail CI\b|\bfails CI\b",
        r"\bCI leg(?:s)?\b|\bloaded CI agent\b",
    ]),
    ("reproduction_difficulty", 15, "Reproduction difficulty", [
        r"\b(?:can't|cannot|can not|unable to|couldn't|could not|failed to)\s+(?:\w+\s+){0,3}?(?:reproduc\w*|repro\b)",
        r"\bnot\s+(?:reproducible|able to reproduce)\b",
        r"\bhard\s+to\s+(?:reproduc\w*|repro\b|debug|pin down|isolate|trigger)\b",
        r"\b(?:difficult|impossible)\s+to\s+(?:reproduc\w*|repro\b)\b",
        r"\b(?:has not|hasn't)\s+been\s+reproduced\s+locally\b",
        r"\bnot\s+proven\s+at\s+runtime\b",
        r"\b(?:no|little)\s+repro\b",
        r"\bno idea how to\s+(?:reproduce|narrow|debug)\b",
    ]),
    ("parallel_isolation", 10, "Passes alone / fails in parallel", [
        r"\bpass(?:es|ed|ing)?\s+(?:alone|individually|in isolation|by itself|single-threaded)\b",
        r"\bfail(?:s|ed|ing)?\s+(?:in parallel|with xdist|under xdist|with pytest-xdist|when run in parallel|concurrently|with multiple workers)\b",
        r"\bpytest-xdist\b|\bxdist\b",
        r"\b(?:order[- ]dependen\w*|order dependent|test ordering|state pollution|state leak\w*)\b",
        r"\bfail(?:s|ed|ing)?\s+(?:only\s+)?when run (?:together|with other tests|in a suite)\b",
        r"\bpasses in isolation\b",
        r"\bdeterministic in both a full local parallel run and CI\b",
    ]),
    ("test_framework", 10, "Relevant test framework", [
        r"\b(?:pytest|playwright|cypress|jest|vitest|selenium|webdriverio|wdio|unittest|junit|testng|mocha|karma|ava|rspec)\b",
    ]),
    ("multiple_occurrences", 5, "Multiple occurrences", [
        r"\b(?:multiple|several|frequent|repeated|recurrent)\s+(?:times|runs|failures|prs|builds|occurrences|attempts)\b",
        r"\b(?:failed|fails|flaked|occurred|happened)\s+(?:multiple times|several times|repeatedly|frequently|again and again|more than once|\d+\s+times)\b",
        r"\b\d+\s+(?:out of|in)\s+\d+\s+(?:runs|times|builds|attempts)\b",
        r"\b(?:seen|observed)\s+(?:on|in|across)\s+(?:several|multiple|various|unrelated)\s+(?:prs|builds|runs|sessions)\b",
        r"\b(?:every few|almost every)\s+(?:runs?|prs?|builds?|days?)\b",
        r"\bseen across recent sessions\b",
        r"\bseen on (?:several|multiple|many|unrelated)\b",
    ]),
    ("active_investigation", 5, "Active investigation", [
        r"\b(?:investigating|investigation|looking into|actively looking|trying to figure|triag(?:ing|ed)|bisecting|root cause|diagnosing)\b",
    ]),
    ("technical_details", 5, "Technical failure details", [
        r"Traceback \(most recent call last\):",
        r"\b(?:AssertionError|TimeoutError|NullPointerException|TypeError|ValueError|ReferenceError|IndexError|KeyError|ConnectionRefusedError|ConnectionResetError|ImageAssert|NoSuchElementException)\b",
        r"\b(?:exit code [1-9]\d*|SIGSEGV|SIGKILL|SIGABRT|timed out after \d+|timeout of \d+ms exceeded|differ by \d+ pixel|assert .* == .*)\b",
        r"```[\s\S]*?(?:error|exception|failure|fail|exit code|timed out|timeout|Assertion|ReferenceError)[\s\S]*?```",
    ]),
]

# Negative rules
NEGATIVE_BOT = ("bot_author", -30, "Bot/automation author", [
    r"\b(?:automated report|filed by scripts/|this issue was automatically (?:created|opened|generated))\b",
])

NEGATIVE_SOLVED = ("clearly_solved", -20, "Clearly solved", [
    r"\b(?:fixed in #?\d+|resolved in #?\d+|fixed by #?\d+|resolved by #?\d+|closed via #?\d+|closing this as (?:fixed|resolved)|already fixed|no longer reproduc(?:ing|ible)|solution found|root cause found and fixed)\b",
])

NEGATIVE_META = ("generic_meta", -15, "Generic/meta discussion", [
    r"\b(?:RFC|proposal|roadmap|umbrella|tracking issue|meta[- ]issue|epic|discussion|decision log|doc compression)\b",
])

NEGATIVE_EDU = ("educational_content", -15, "Educational/non-problem content", [
    r"\b(?:tutorial|how[- ]to guide|example repo|course exercise|homework|learn(?:ing)? pytest|textbook|demo repository|sample project)\b",
])

# Compiled regex patterns
_POSITIVE_COMPILED = [
    (key, weight, label, [re.compile(p, re.IGNORECASE) for p in patterns])
    for key, weight, label, patterns in POSITIVE_RULES
]
_BOT_COMPILED = [re.compile(p, re.IGNORECASE) for p in NEGATIVE_BOT[3]]
_SOLVED_COMPILED = [re.compile(p, re.IGNORECASE) for p in NEGATIVE_SOLVED[3]]
_META_COMPILED = [re.compile(p, re.IGNORECASE) for p in NEGATIVE_META[3]]
_EDU_COMPILED = [re.compile(p, re.IGNORECASE) for p in NEGATIVE_EDU[3]]

KNOWN_BOT_LOGINS = {"dependabot", "renovate", "github-actions", "kibanamachine", "ghost"}

@dataclass
class Analysis:
    """Matches the interface expected by pipeline.py: assess(c, r)."""
    score: int                  # 0..100
    signals: list[str]          # labels of matched signals
    why: str                    # human-readable explanation
    recommended_action: str     # COMMENT | IGNORE

def analyze_github_issue(issue: Any, now: datetime | None = None) -> Analysis:
    """Deterministic pain scorer for a GitHubIssue.
    
    Scores positive and negative signals based strictly on evidence present in the issue.
    Returns an Analysis object with clamped score (0-100) and recommendation (COMMENT or IGNORE).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    title = (getattr(issue, "title", "") or "").strip()
    body = getattr(issue, "body", "") or ""
    labels = getattr(issue, "labels", []) or []
    labels_text = " ".join(labels).lower()
    comments = int(getattr(issue, "comments", 0) or 0)
    author = getattr(issue, "author_login", "") or ""
    state = getattr(issue, "state", "open") or "open"
    updated_at = getattr(issue, "updated_at", None)

    text = f"{title}\n\n{body}"

    raw_score = 0
    breakdown: list[str] = []
    tags: list[str] = []

    # 1. Positive signals
    for key, weight, label, regexes in _POSITIVE_COMPILED:
        matched = False
        if key == "flaky_concrete":
            # Check title first, or labels, or patterns in text
            if re.search(r"\bflak(?:y|iness)\b|\bintermittent\b", title, re.IGNORECASE):
                matched = True
            elif any(l in labels_text for l in ("flaky", "flaky-test", "flaky test", "area/flaky")):
                matched = True
            elif any(rx.search(text) for rx in regexes):
                matched = True
        elif key == "active_investigation":
            # Investigation is present if comments exist, or triage/investigation tags or terms
            if comments > 0 or any(l in labels_text for l in ("triage", "investigation", "needs-triage")) or any(rx.search(text) for rx in regexes):
                matched = True
        else:
            if any(rx.search(text) for rx in regexes):
                matched = True

        if matched:
            raw_score += weight
            breakdown.append(f"{label} (+{weight})")
            tags.append(label)

    # 2. Recency / updated within 3 days (+10) or within 7 days (+5)
    if updated_at:
        age_hours = max(0.0, (now - updated_at).total_seconds() / 3600.0)
        if age_hours <= 72.0:
            raw_score += 10
            breakdown.append("Updated within 3 days (+10)")
            tags.append("Updated within 3 days")
        elif age_hours <= 168.0:
            raw_score += 5
            breakdown.append("Updated within 7 days (+5)")
            tags.append("Updated within 7 days")

    # 3. Negative signals
    # Bot author (-30)
    bot_login = author.lower()
    is_bot = (
        bot_login.endswith("[bot]")
        or any(b in bot_login for b in KNOWN_BOT_LOGINS)
        or ("bot" in bot_login and not bot_login.startswith("robotics"))
        or any(rx.search(text) for rx in _BOT_COMPILED)
    )
    if is_bot:
        raw_score -= 30
        breakdown.append("Bot/automation author (-30)")
        tags.append("Bot author")

    # Clearly solved (-20)
    is_solved = (
        state.lower() == "closed"
        or any(l in labels_text for l in ("resolved", "fixed", "closed"))
        or any(rx.search(text) for rx in _SOLVED_COMPILED)
    )
    if is_solved:
        raw_score -= 20
        breakdown.append("Clearly solved (-20)")
        tags.append("Clearly solved")

    # Generic / meta discussion (-15)
    is_meta = (
        any(l in labels_text for l in ("documentation", "meta", "rfc", "roadmap", "discussion", "epic"))
        or any(rx.search(title) for rx in _META_COMPILED)
        or any(rx.search(labels_text) for rx in _META_COMPILED)
    )
    if is_meta:
        raw_score -= 15
        breakdown.append("Generic/meta discussion (-15)")
        tags.append("Generic/meta discussion")

    # Educational / non-problem content (-15)
    is_edu = any(rx.search(title) for rx in _EDU_COMPILED) or any(rx.search(text) for rx in _EDU_COMPILED)
    if is_edu:
        raw_score -= 15
        breakdown.append("Educational/non-problem content (-15)")
        tags.append("Educational content")

    # 4. Clamping & classification
    clamped_score = max(0, min(100, raw_score))
    action = "COMMENT" if clamped_score >= 60 else "IGNORE"

    summary_bd = ", ".join(breakdown) if breakdown else "No qualifying signals found"
    why = f"Score {clamped_score}/100 ({action}): {summary_bd}"

    return Analysis(score=clamped_score, signals=tags, why=why, recommended_action=action)
