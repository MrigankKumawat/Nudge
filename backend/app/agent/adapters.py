"""Discovery adapter interface. The real pipeline (pipeline.py) uses LinkedInDiscovery only; MockDiscovery is a dev/test fixture."""
from dataclasses import dataclass

@dataclass
class RawCandidate:
    name: str
    profile_url: str
    role: str
    company: str
    bio: str
    source: str
    post_url: str | None = None
    post_text: str | None = None
    post_age_hours: float | None = None

class DiscoveryError(RuntimeError):
    """A discovery source failed. Propagates so the AgentRun is marked FAILED instead of looking like an empty success."""

class DiscoveryConfigError(DiscoveryError):
    """A discovery source is not configured (e.g. missing SERPAPI_API_KEY)."""

class DiscoveryAdapter:
    name = "base"
    def discover(self) -> list[RawCandidate]:
        raise NotImplementedError

def C(*a, **k):
    return RawCandidate(*a, **k)

class MockDiscovery(DiscoveryAdapter):
    """Dev/test fixture only. NOT used by the real pipeline: never add it to the pipeline's adapter list."""
    name = "mock"
    def discover(self):
        return [
            C("Ines Moreau", "https://github.com/imoreau", "Platform Engineer", "Fieldstack", "Platform team, Python services, pytest-xdist in CI.", "mock:github",
              "https://github.com/fieldstack/platform/discussions/61", "Our pytest-xdist run is green locally and red on Jenkins about once a day. Is it conftest ordering? No idea how to even start narrowing this down.", 20),
            C("Ravi Patel", "https://github.com/rpatel-qa", "QA Automation Lead", "Cobalt Health", "QA automation lead. Python, Playwright, pytest.", "mock:reddit",
              "https://www.reddit.com/r/QualityAssurance/comments/1fx2k9/", "Our end-to-end suite has 6 tests that randomly fail with timeouts in CI. Reruns hide it but nobody knows the cause. How do you triage this?", 52),
            C("Hannah Berg", "https://github.com/hberg", "Maintainer, httpx-mock-lite", "Open source", "Open-source maintainer. Python testing utilities.", "mock:github",
              "https://github.com/hberg/httpx-mock-lite/issues/142", "test_retry_backoff fails intermittently on the Windows CI runner only. Passes 40 times in a row locally.", 30),
            C("Kofi Mensah", "https://github.com/kmensah", "Backend Engineer", "Ledgerly", "Backend engineer. Django and Python.", "mock:hn",
              "https://news.ycombinator.com/item?id=41900213", "Finally tracked a flaky Django integration test down to a shared DB fixture. Took two days of bisecting by hand.", 70),
            C("Lucía Fernández", "https://github.com/lfernandez-sre", "SRE", "Parcelway", "SRE. CI pipelines, GitHub Actions, Python tooling.", "mock:reddit",
              "https://www.reddit.com/r/devops/comments/1fy7p3/", "CI minutes are up 30% because we keep rerunning failing pytest jobs. Is there anything better than retry-on-fail?", 10),
            C("Owen Gallagher", "https://github.com/ogallagher", "Engineering Manager", "Basil", "Engineering manager. Python shop.", "mock:linkedin-export",
              "https://example.com/posts/ogallagher/hiring", "Excited to announce we're hiring Python engineers! Come join a fast-growing team.", 26),
            C("Mika Suzuki", "https://github.com/msuzuki-data", "Data Engineer", "Tidewater", "Data engineer. Airflow, pytest for DAG tests.", "mock:github",
              "https://github.com/tidewater/pipelines/discussions/18", "DAG tests that depend on the current time keep failing randomly in CI, mostly around midnight UTC. pytest freezegun didn't fully fix it.", 95),
            C("Grace Oladipo", "https://github.com/goladipo", "Senior Software Engineer", "Nimbus", "Senior software engineer. Python, Django.", "mock:github"),
            C("Tariq Aziz", "https://github.com/taziz-devops", "DevOps Engineer", "Halyard", "DevOps engineer. GitHub Actions, Terraform.", "mock:reddit",
              "https://www.reddit.com/r/devops/comments/1fz1a8/", "Anyone have a good pattern for caching Docker layers in GitHub Actions?", 40),
            C("Dimitri Volkov", "https://github.com/dvolkov", "SDET", "Orbital Labs", "SDET running the flaky test triage rotation. Python, pytest, test infrastructure.", "mock:github"),
        ]