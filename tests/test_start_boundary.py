from __future__ import annotations

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


class BatchStartCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Batch Start Test")
        self.git("config", "user.email", "batch-start@example.test")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-m", "base")
        self.state = self.root / "batch-state.json"
        self.worktrees = self.root / "worktrees"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def git(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=check,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout.strip()

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def create_plan(self, tickets: list[dict[str, object]]) -> dict[str, object]:
        completed = self.cli(
            "plan",
            "create",
            "--repo",
            str(self.repo),
            "--state",
            str(self.state),
            "--target-branch",
            "main",
            "--starting-sha",
            self.git("rev-parse", "HEAD"),
            "--tickets-json",
            json.dumps(tickets),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def start(self, ticket: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.cli(
            "start",
            "--repo",
            str(self.repo),
            "--ticket",
            ticket,
            "--batch-state",
            str(self.state),
            "--worktree-root",
            str(self.worktrees),
            *extra,
        )

    def test_start_without_batch_state_fails_closed_with_migration_guidance(self) -> None:
        completed = self.cli(
            "start", "--repo", str(self.repo), "--ticket", "01"
        )

        self.assertNotEqual(completed.returncode, 0)
        error = json.loads(completed.stderr)
        self.assertFalse(error["verified"])
        self.assertEqual(error["error_code"], "batch_state_missing")
        self.assertIn("migration", error["details"]["guidance"].lower())
        self.assertEqual(self.git("branch", "--list"), "* main")
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)

    def test_blocked_dependent_start_has_no_git_or_started_artifacts(self) -> None:
        self.create_plan(
            [
                {"ticket": "01"},
                {"ticket": "02", "dependencies": ["01"]},
            ]
        )
        completed = self.start("02")

        self.assertNotEqual(completed.returncode, 0)
        error = json.loads(completed.stderr)
        self.assertFalse(error["verified"])
        self.assertEqual(error["error_code"], "ticket_not_runnable")
        details = error["details"]
        self.assertEqual(details["ticket"], "02")
        self.assertEqual(details["status"], "planned")
        self.assertEqual(details["unmet_predecessors"], ["01"])
        self.assertEqual(details["gates"], [])
        self.assertEqual(details["target_head"], self.git("rev-parse", "HEAD"))
        self.assertFalse((self.worktrees / "02").exists())
        self.assertEqual(self.git("branch", "--list", "codex/matt-ticket/02-*"), "")
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertNotIn("ticket_states", persisted)
        self.assertEqual([record["status"] for record in persisted["tickets"]], ["planned", "planned"])

    def test_frontier_start_claims_batch_linked_state_and_updates_frontier(self) -> None:
        plan = self.create_plan(
            [
                {"ticket": "01", "required_checks": ["tests"]},
                {"ticket": "02", "dependencies": ["01"]},
            ]
        )
        completed = self.start("01")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        started = json.loads(completed.stdout)
        self.assertTrue(started["verified"])
        self.assertEqual(started["status"], "started")
        self.assertEqual(started["ticket"], "01")
        self.assertEqual(started["batch_id"], plan["batch_id"])
        self.assertEqual(started["batch_state"], str(self.state.resolve()))
        self.assertEqual(started["frontier_generation"], 0)
        self.assertEqual(started["verified_start_sha"], plan["starting_sha"])
        self.assertEqual(self.git("rev-parse", "HEAD"), plan["starting_sha"])
        self.assertTrue((self.worktrees / "01").exists())

        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        records = {record["ticket"]: record for record in persisted["tickets"]}
        self.assertEqual(records["01"]["status"], "started")
        self.assertEqual(persisted["frontier"], [])
        self.assertEqual(persisted["runnable"], [])
        ticket_state = persisted["ticket_states"]["01"]
        self.assertEqual(ticket_state["batch_id"], plan["batch_id"])
        self.assertEqual(ticket_state["verified_start_sha"], plan["starting_sha"])
        self.assertEqual(ticket_state["predecessor_evidence"], {})

    def test_independent_frontier_starts_share_frozen_sha(self) -> None:
        plan = self.create_plan([{"ticket": "01"}, {"ticket": "02"}])
        first = self.start("01")
        second = self.start("02")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        first_state = json.loads(first.stdout)
        second_state = json.loads(second.stdout)
        self.assertEqual(first_state["verified_start_sha"], plan["starting_sha"])
        self.assertEqual(second_state["verified_start_sha"], plan["starting_sha"])
        self.assertNotEqual(first_state["branch"], second_state["branch"])
        self.assertNotEqual(first_state["worktree"], second_state["worktree"])
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            {record["status"] for record in persisted["tickets"]}, {"started"}
        )

    def test_target_validation_fails_before_artifacts(self) -> None:
        plan = self.create_plan([{"ticket": "01"}, {"ticket": "02"}])
        (self.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        dirty = self.start("01")
        self.assertNotEqual(dirty.returncode, 0)
        dirty_error = json.loads(dirty.stderr)
        self.assertEqual(dirty_error["error_code"], "target_worktree_dirty")
        self.assertEqual(dirty_error["details"]["target_head"], plan["starting_sha"])
        self.assertFalse((self.worktrees / "01").exists())

        (self.repo / "dirty.txt").unlink()
        self.git("checkout", "-b", "wrong-target")
        wrong_branch = self.start("01")
        self.assertNotEqual(wrong_branch.returncode, 0)
        branch_error = json.loads(wrong_branch.stderr)
        self.assertEqual(branch_error["error_code"], "target_branch_mismatch")
        self.assertEqual(branch_error["details"]["expected_branch"], "main")
        self.assertEqual(branch_error["details"]["actual_branch"], "wrong-target")
        self.git("checkout", "main")

        (self.repo / "stale.txt").write_text("stale\n", encoding="utf-8")
        self.git("add", "stale.txt")
        self.git("commit", "-m", "stale target")
        stale = self.start("01")
        self.assertNotEqual(stale.returncode, 0)
        stale_error = json.loads(stale.stderr)
        self.assertEqual(stale_error["error_code"], "target_head_stale")
        self.assertEqual(stale_error["details"]["expected_head"], plan["starting_sha"])
        self.assertEqual(stale_error["details"]["target_head"], self.git("rev-parse", "HEAD"))
        self.assertFalse((self.worktrees / "01").exists())

    def test_concurrent_start_claims_ticket_once(self) -> None:
        self.create_plan([{"ticket": "01"}, {"ticket": "02"}])
        command = [
            sys.executable,
            str(SCRIPT_PATH),
            "start",
            "--repo",
            str(self.repo),
            "--ticket",
            "01",
            "--batch-state",
            str(self.state),
            "--worktree-root",
            str(self.worktrees),
        ]
        first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        first_output, first_error = first.communicate(timeout=30)
        second_output, second_error = second.communicate(timeout=30)

        self.assertEqual(
            sorted((first.returncode, second.returncode)), [0, 1],
            f"first={first_output!r} {first_error!r}; second={second_output!r} {second_error!r}",
        )
        failure = second_error if second.returncode != 0 else first_error
        error = json.loads(failure)
        self.assertIn(error["error_code"], {"ticket_not_runnable", "ticket_already_started"})
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            [record["status"] for record in persisted["tickets"] if record["ticket"] == "01"],
            ["started"],
        )
        self.assertEqual(len(list(self.worktrees.glob("01"))), 1)
        self.assertEqual(len(self.git("branch", "--list", "codex/matt-ticket/01-*").splitlines()), 1)

    def test_git_boundary_failure_rolls_back_claim_and_branch(self) -> None:
        self.create_plan([{"ticket": "01"}, {"ticket": "02"}])
        invalid_root = self.root / "not-a-directory"
        invalid_root.write_text("file\n", encoding="utf-8")
        completed = self.cli(
            "start",
            "--repo",
            str(self.repo),
            "--ticket",
            "01",
            "--batch-state",
            str(self.state),
            "--worktree-root",
            str(invalid_root),
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(json.loads(completed.stderr)["verified"])
        self.assertEqual(self.git("branch", "--list", "codex/matt-ticket/01-*"), "")
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            [record["status"] for record in persisted["tickets"] if record["ticket"] == "01"],
            ["planned"],
        )
        self.assertNotIn("ticket_states", persisted)

    def test_inconsistent_ticket_state_fails_closed_before_artifacts(self) -> None:
        self.create_plan([{"ticket": "01"}, {"ticket": "02"}])
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        persisted["ticket_states"] = {"01": {"ticket": "01", "status": "started"}}
        self.state.write_text(json.dumps(persisted), encoding="utf-8")

        completed = self.start("01")

        self.assertNotEqual(completed.returncode, 0)
        error = json.loads(completed.stderr)
        self.assertEqual(error["error_code"], "batch_state_corrupt")
        self.assertFalse((self.worktrees / "01").exists())
        self.assertEqual(self.git("branch", "--list", "codex/matt-ticket/01-*"), "")

    def test_missing_batch_identity_fails_closed_before_artifacts(self) -> None:
        self.create_plan([{"ticket": "01"}, {"ticket": "02"}])
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        del persisted["batch_id"]
        self.state.write_text(json.dumps(persisted), encoding="utf-8")

        completed = self.start("01")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stderr)["error_code"], "batch_state_corrupt")
        self.assertFalse((self.worktrees / "01").exists())
        self.assertEqual(self.git("branch", "--list", "codex/matt-ticket/01-*"), "")


if __name__ == "__main__":
    unittest.main()
