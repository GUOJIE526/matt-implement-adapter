import json
import os
import subprocess
import tempfile
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
WRAPPER_PATH = (
    REPOSITORY_ROOT
    / "plugins"
    / "matt-implement-adapter"
    / "scripts"
    / "discover_worker_brief.ps1"
)
README_PATH = REPOSITORY_ROOT / "README.md"


class WorkerOwnedBriefContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = SKILL_PATH.read_text(encoding="utf-8")
        cls.session_start = SESSION_START_PATH.read_text(encoding="utf-8")
        cls.readme = README_PATH.read_text(encoding="utf-8")

    def test_skill_assigns_brief_discovery_to_worker_worktree(self) -> None:
        self.assertIn("worker worktree", self.skill)
        self.assertIn("discover_worker_brief.ps1", self.skill)
        self.assertIn("$env:PLUGIN_ROOT", self.skill)
        self.assertIn("--ticket \"<ticket-reference>\"", self.skill)
        self.assertIn("parent prompt", self.skill)
        self.assertIn("brief body", self.skill)
        self.assertIn("IsNullOrWhiteSpace($env:PLUGIN_ROOT)", self.skill)
        self.assertIn("Test-Path -LiteralPath $wrapper", self.skill)

        self.assertNotIn('python "<this-skill-directory>\\..\\..\\scripts\\implementation_brief.py" discover', self.skill)
        self.assertNotIn("Before creating worker worktrees, look for optional implementation briefs", self.skill)
        self.assertNotIn("The parent should retain the matched brief path", self.skill)
        self.assertNotIn("If the parent discovered an optional implementation brief", self.skill)

    def test_session_start_declares_parent_prompt_brief_boundary(self) -> None:
        self.assertIn("worker worktree", self.session_start)
        self.assertIn("parent", self.session_start)
        self.assertIn("brief body", self.session_start)
        self.assertIn("$env:PLUGIN_ROOT", self.session_start)
        self.assertIn("discover_worker_brief.ps1", self.session_start)
        self.assertNotIn("pass only matching briefs to their", self.session_start)

    def test_readme_describes_worker_owned_brief_discovery(self) -> None:
        self.assertIn("worker worktree", self.readme)
        self.assertIn("brief body", self.readme)
        self.assertIn("discover_worker_brief.ps1", self.readme)
        self.assertIn("PLUGIN_ROOT", self.readme)
        self.assertNotIn("在建立 worker 前會搜尋", self.readme)
        self.assertNotIn("匹配的 brief path 傳給對應 worker", self.readme)

    def test_wrapper_uses_its_own_location_without_plugin_absolute_path(self) -> None:
        wrapper = WRAPPER_PATH.read_text(encoding="utf-8")

        self.assertIn("$PSScriptRoot", wrapper)
        self.assertIn("implementation_brief.py", wrapper)
        self.assertNotIn("C:\\Users\\", wrapper)
        self.assertNotIn("D:\\", wrapper)

    def test_wrapper_discovers_brief_without_plugin_root_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            repository_root = Path(temporary_root)
            brief_path = (
                repository_root
                / ".scratch"
                / "feature"
                / "implementation-briefs"
                / "01-login.md"
            )
            brief_path.parent.mkdir(parents=True)
            brief_path.write_text(
                "---\nticket: 01\nstatus: ready\n---\n\n# Login\n",
                encoding="utf-8",
            )

            environment = os.environ.copy()
            environment.pop("PLUGIN_ROOT", None)
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(WRAPPER_PATH),
                    "-Repo",
                    str(repository_root),
                    "-Ticket",
                    "01-login",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["found"])
        self.assertEqual(
            Path(payload["matched"]["1"]["path"]).resolve(),
            brief_path.resolve(),
        )


if __name__ == "__main__":
    unittest.main()
