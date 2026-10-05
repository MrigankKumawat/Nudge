"""GitHub COMMENT draft generator.

Produces short (2-4 sentences), technically grounded comment drafts for GitHub issues
classified as COMMENT (score >= 60).

Guidelines:
- Grounded strictly in issue evidence (CI failure, retries, parallel/xdist, reproduction difficulty, flaky behavior).
- Useful first, promotional second.
- Mentions Flaky-Repro naturally without claiming it solves or will reproduce the issue.
- Does not invent root causes.
- No spam phrases ("Great post!", "Check out my product!", etc.).
- Never creates DM drafts; strictly for public comments awaiting manual user approval.
"""
import re
from typing import Any

def github_comment_text(candidate: Any) -> str:
    """Generate a 2-4 sentence technical GitHub comment draft tailored to candidate evidence."""
    iss = getattr(candidate, "issue", None)
    an = getattr(candidate, "analysis", None)
    
    author = getattr(candidate, "name", "") or (getattr(iss, "author_login", "") if iss else "")
    author_prefix = f"Hey @{author}, " if author and re.match(r"^[a-zA-Z0-9-]+$", author) else "Hey, "
    
    title = (getattr(iss, "title", "") if iss else "") or ""
    body = (getattr(iss, "body", "") if iss else "") or (getattr(candidate, "post_text", "") or "")
    text = f"{title}\n{body}".lower()
    
    signals = set(getattr(an, "signals", []) if an else [])
    
    has_parallel = (
        "Passes alone / fails in parallel" in signals
        or bool(re.search(r"\b(?:parallel|xdist|pytest-xdist|concurrency|concurrent execution|run in parallel|fails? in parallel)\b|\b(?:passes?|works?)\s+(?:alone|individually|in isolation)\b", text))
    )
    has_repro_diff = (
        "Reproduction difficulty" in signals
        or bool(re.search(r"\b(?:can't|cannot|can not|unable to|hard to|difficult to|not)\s+(?:reproduce|pin down)\b", text))
    )
    has_ci = (
        "CI failure" in signals
        or bool(re.search(r"\b(?:ci|github actions|jenkins|pipeline|runner|azure pipelines)\b", text))
    )
    has_retry = (
        "Retry/rerun evidence" in signals
        or bool(re.search(r"\b(?:retry|retries|rerun|reruns|passes on retry|passed on retry)\b", text))
    )
    is_pytest = bool(re.search(r"\b(?:pytest|xdist)\b", text))
    is_playwright = bool(re.search(r"\bplaywright\b", text))
    
    # Sentence 1: Technical observation / pain point acknowledgment
    if has_parallel:
        s1 = (
            f"{author_prefix}I came across this while looking into intermittent test failures. "
            "Tests that pass individually or sequentially but fail under parallel execution or worker concurrency usually point to shared state or resource contention between workers."
        )
    elif has_ci and has_repro_diff and has_retry:
        s1 = (
            f"{author_prefix}I came across this while looking into flaky test reproduction. "
            "The combination of failing intermittently on the CI runner, passing on retry, and being difficult to reproduce locally is one of the trickiest patterns to isolate."
        )
    elif has_repro_diff:
        s1 = (
            f"{author_prefix}I came across this while looking into intermittent test reproduction. "
            "Failures that only appear intermittently and are hard to recreate in a local environment can burn a lot of triage time."
        )
    elif has_ci and has_retry:
        s1 = (
            f"{author_prefix}I came across this while researching CI test stability. "
            "Dealing with tests that fail on the initial run and pass on retry masks whether the problem is in the test body, timing, or the CI runner environment."
        )
    elif has_ci:
        s1 = (
            f"{author_prefix}I came across this while looking into CI test failures. "
            "When tests run green locally but fail intermittently on CI, the difference is often subtle timing, worker load, or environment differences."
        )
    elif has_retry:
        s1 = (
            f"{author_prefix}I came across this while looking into flaky test triage. "
            "Having to rely on retries to pass builds burns CI minutes and usually indicates an intermittent race condition or timing variance."
        )
    else:
        s1 = (
            f"{author_prefix}I came across this while researching flaky test reproduction. "
            "Intermittent test failures like this are often tough to pin down when they don't reproduce consistently in normal runs."
        )
        
    # Sentence 2: Natural, humble mention of Flaky-Repro
    if is_pytest:
        s2 = "I'm building an open-source pytest tool called Flaky-Repro that tries to reproduce intermittent failures by systematically varying execution conditions like ordering, timing, and environment."
    elif is_playwright:
        s2 = "I'm building an open-source tool called Flaky-Repro that studies flaky test reproduction by varying execution conditions like timing and runner environment."
    else:
        s2 = "I'm building an open-source tool called Flaky-Repro that focuses on reproducing intermittent test failures by systematically varying execution conditions."
        
    # Sentence 3: Constructive, non-pushy closing
    if has_parallel:
        s3 = "If you're still actively debugging this, I'd be curious whether stress-testing under varied concurrency and ordering conditions helps isolate the trigger."
    elif has_repro_diff:
        s3 = "If you're still investigating, I'd be curious whether varying those execution conditions helps trigger the failure more reliably."
    else:
        s3 = "If you're still working through this, happy to share how we approach reproducing these or see if varying execution conditions helps recreate it."
        
    return f"{s1} {s2} {s3}"
