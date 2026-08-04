import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPOSITORY_ROOT
    / "plugins"
    / "matt-implement-adapter"
    / "skills"
    / "implement-ticket-batch"
    / "SKILL.md"
)
SESSION_START_PATH = (
    REPOSITORY_ROOT
    / "plugins"
    / "matt-implement-adapter"
    / "scripts"
    / "session_start.ps1"
)
README_PATH = REPOSITORY_ROOT / "README.md"
BRIEF_HELPERS = (
    REPOSITORY_ROOT
    / "plugins"
    / "matt-implement-adapter"
    / "scripts"
    / "discover_worker_brief.ps1",
    REPOSITORY_ROOT
    / "plugins"
    / "matt-implement-adapter"
    / "scripts"
    / "implementation_brief.py",
)


class NoWorkerBriefContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = SKILL_PATH.read_text(encoding="utf-8").casefold()
        cls.session_start = SESSION_START_PATH.read_text(encoding="utf-8").casefold()
        cls.readme = README_PATH.read_text(encoding="utf-8").casefold()

    def test_session_start_does_not_inject_brief_reading(self) -> None:
        self.assertIn(
            "matt-implement-adapter:implement-ticket-batch",
            self.session_start,
        )
        self.assertNotIn("implementation brief", self.session_start)
        self.assertNotIn("implementation-briefs", self.session_start)
        self.assertNotIn("discover_worker_brief", self.session_start)

    def test_batch_skill_does_not_request_brief_reading(self) -> None:
        self.assertIn("run one ticket", self.skill)
        self.assertNotIn("implementation brief", self.skill)
        self.assertNotIn("implementation-briefs", self.skill)
        self.assertNotIn("discover_worker_brief", self.skill)
        self.assertNotIn("@brief-path", self.skill)

    def test_batch_skill_delegates_native_implement_workflow(self) -> None:
        self.assertIn(
            "invoke the installed matt `implement` skill for that ticket only",
            self.skill,
        )
        self.assertIn("do not restate its workflow", self.skill)
        self.assertNotIn("work through matt `tdd`", self.skill)
        self.assertNotIn("run focused tests and type checks regularly", self.skill)
        self.assertNotIn("create a provisional ticket commit", self.skill)
        self.assertNotIn("run the ticket's own two-axis matt `code-review`", self.skill)
        self.assertNotIn("the provisional-commit bridge", self.skill)

    def test_session_start_delegates_worker_workflow_to_native_implement(self) -> None:
        self.assertIn(
            "installed matt implement skill for exactly one ticket",
            self.session_start,
        )
        self.assertIn("installed skill remains authoritative", self.session_start)
        self.assertNotIn(
            "tdd, tests, two-axis code review, fixes, and ticket commit",
            self.session_start,
        )

    def test_readme_does_not_advertise_worker_brief_reading(self) -> None:
        self.assertNotIn("implementation brief", self.readme)
        self.assertNotIn("implementation-briefs", self.readme)
        self.assertNotIn("brief discovery", self.readme)

    def test_worker_brief_helpers_are_removed(self) -> None:
        for helper in BRIEF_HELPERS:
            with self.subTest(helper=helper.name):
                self.assertFalse(helper.exists())

    def test_agent_contract_separates_activation_from_concurrency(self) -> None:
        for name, document in (
            ("session start", self.session_start),
            ("batch skill", self.skill),
            ("readme", self.readme),
        ):
            with self.subTest(document=name):
                self.assertIn("adapter activation does not imply concurrency", document)
                self.assertIn("validated batch plan", document)
                self.assertIn("scheduler", document)
                self.assertIn("frontier", document)

    def test_batch_skill_requires_plan_before_scheduler_frontier_start(self) -> None:
        self.assertIn("before invoking any worker start", self.skill)
        self.assertIn(
            "work only the open, unblocked frontier returned by the scheduler",
            self.skill,
        )
        self.assertIn("start only the tickets it returns", self.skill)

    def test_plugin_metadata_does_not_imply_all_tickets_run_in_parallel(self) -> None:
        metadata = (
            REPOSITORY_ROOT
            / "plugins"
            / "matt-implement-adapter"
            / ".codex-plugin"
            / "plugin.json"
        ).read_text(encoding="utf-8").casefold()
        agent_metadata = (
            REPOSITORY_ROOT
            / "plugins"
            / "matt-implement-adapter"
            / "skills"
            / "implement-ticket-batch"
            / "agents"
            / "openai.yaml"
        ).read_text(encoding="utf-8").casefold()
        for document in (metadata, agent_metadata):
            self.assertIn("validated", document)
            self.assertIn("frontier", document)
            self.assertNotIn("all tickets", document)
            self.assertNotIn("parallelize independent tickets", document)


if __name__ == "__main__":
    unittest.main()
