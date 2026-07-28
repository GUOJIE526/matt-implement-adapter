from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "matt-implement-adapter"
    / "skills"
    / "implement-ticket-batch"
    / "scripts"
    / "ticket_boundary.py"
)
SPEC = importlib.util.spec_from_file_location("ticket_boundary", SCRIPT_PATH)
assert SPEC and SPEC.loader
ticket_boundary = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ticket_boundary
SPEC.loader.exec_module(ticket_boundary)


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check:
        return result.stdout.strip()
    return f"{result.stdout}\n{result.stderr}".strip()


class TicketBoundaryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Ticket Boundary Test")
        git(self.repo, "config", "user.email", "ticket-boundary@example.test")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "base")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def start_ticket(self, ticket: str) -> dict[str, object]:
        return ticket_boundary.start_boundary(
            str(self.repo),
            ticket,
            worktree_root=self.root / "worktrees",
        )

    def commit_worker_change(self, state: dict[str, object], content: str) -> None:
        worktree = Path(str(state["worktree"]))
        (worktree / "feature.txt").write_text(content, encoding="utf-8")
        git(worktree, "add", "feature.txt")
        git(worktree, "commit", "-m", str(state["ticket"]))

    def test_cherry_pick_ticket_then_cleanup(self) -> None:
        state = self.start_ticket("ticket-001")
        self.commit_worker_change(state, "ticket 001\n")

        finished = ticket_boundary.finish_boundary(str(state["state_path"]))
        self.assertTrue(finished["verified"])

        integrated = ticket_boundary.integrate_boundary(
            str(state["state_path"]), strategy="cherry-pick"
        )
        self.assertEqual(integrated["status"], "integrated")
        self.assertEqual(
            (self.repo / "feature.txt").read_text(encoding="utf-8"), "ticket 001\n"
        )

        cleaned = ticket_boundary.cleanup_boundary(str(state["state_path"]))
        self.assertEqual(cleaned["status"], "cleaned")
        self.assertFalse(Path(str(state["worktree"])).exists())
        self.assertEqual(git(self.repo, "branch", "--list", str(state["branch"])), "")
        self.assertEqual(git(self.repo, "status", "--porcelain=v1"), "")

    def test_merge_ticket_then_cleanup(self) -> None:
        state = self.start_ticket("ticket-merge")
        self.commit_worker_change(state, "merge ticket\n")
        ticket_boundary.finish_boundary(str(state["state_path"]))

        integrated = ticket_boundary.integrate_boundary(
            str(state["state_path"]), strategy="merge"
        )
        self.assertEqual(integrated["status"], "integrated")
        self.assertIn("Merge", git(self.repo, "log", "-1", "--format=%s"))

        ticket_boundary.cleanup_boundary(str(state["state_path"]))
        self.assertEqual(git(self.repo, "branch", "--list", str(state["branch"])), "")

    def test_conflicting_integration_is_recorded_for_main_agent(self) -> None:
        first = self.start_ticket("ticket-first")
        second = self.start_ticket("ticket-second")

        for state, content in ((first, "first\n"), (second, "second\n")):
            worktree = Path(str(state["worktree"]))
            (worktree / "README.md").write_text(content, encoding="utf-8")
            git(worktree, "add", "README.md")
            git(worktree, "commit", "-m", str(state["ticket"]))
            ticket_boundary.finish_boundary(str(state["state_path"]))

        ticket_boundary.integrate_boundary(str(first["state_path"]), strategy="cherry-pick")
        ticket_boundary.cleanup_boundary(str(first["state_path"]))
        with self.assertRaisesRegex(RuntimeError, "integration conflict"):
            ticket_boundary.integrate_boundary(
                str(second["state_path"]), strategy="cherry-pick"
            )

        self.assertEqual(
            json_state(str(second["state_path"]))["status"], "integration_conflict"
        )
        self.assertTrue(Path(str(second["worktree"])).exists())
        self.assertNotEqual(git(self.repo, "branch", "--list", str(second["branch"])), "")
        (self.repo / "README.md").write_text("first\nsecond\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "core.editor=true",
                "cherry-pick",
                "--continue",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        continued = ticket_boundary.integrate_boundary(
            str(second["state_path"]),
            strategy="cherry-pick",
            continue_integration=True,
        )
        self.assertEqual(continued["status"], "integrated")
        ticket_boundary.cleanup_boundary(str(second["state_path"]))
        self.assertEqual(git(self.repo, "status", "--porcelain=v1"), "")


def json_state(path: str) -> dict[str, object]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
