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
        self.batch_state = self.root / "batch-state.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def start_ticket(self, ticket: str) -> dict[str, object]:
        if not self.batch_state.exists():
            ticket_boundary.create_batch_plan(
                str(self.repo),
                str(self.batch_state),
                target_branch="main",
                starting_sha=git(self.repo, "rev-parse", "HEAD"),
                tickets=[
                    {"ticket": ticket},
                    {"ticket": f"peer-{ticket}"},
                ],
            )
        return ticket_boundary.start_boundary(
            str(self.repo),
            ticket,
            batch_state=self.batch_state,
            worktree_root=self.root / "worktrees",
        )

    def create_batch_plan(self, tickets: list[str]) -> None:
        ticket_boundary.create_batch_plan(
            str(self.repo),
            str(self.batch_state),
            target_branch="main",
            starting_sha=git(self.repo, "rev-parse", "HEAD"),
            tickets=[{"ticket": ticket} for ticket in tickets],
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

        ticket_boundary.record_verification(str(state["state_path"]), result="passed")
        cleaned = ticket_boundary.cleanup_boundary(str(state["state_path"]))
        self.assertEqual(cleaned["status"], "cleaned")
        self.assertFalse(Path(str(state["worktree"])).exists())
        self.assertEqual(git(self.repo, "branch", "--list", str(state["branch"])), "")
        self.assertEqual(git(self.repo, "status", "--porcelain=v1"), "")

    def test_cleanup_requires_explicit_passed_verification(self) -> None:
        state = self.start_ticket("ticket-verification")
        self.commit_worker_change(state, "verification\n")
        ticket_boundary.finish_boundary(str(state["state_path"]))
        ticket_boundary.integrate_boundary(str(state["state_path"]), strategy="cherry-pick")

        with self.assertRaisesRegex(ticket_boundary.BatchPlanError, "verification"):
            ticket_boundary.cleanup_boundary(str(state["state_path"]))

        failed = ticket_boundary.record_verification(
            str(state["state_path"]), result="failed"
        )
        self.assertEqual(failed["verification_result"], "failed")
        with self.assertRaisesRegex(ticket_boundary.BatchPlanError, "verification"):
            ticket_boundary.cleanup_boundary(str(state["state_path"]))

        ticket_boundary.record_verification(str(state["state_path"]), result="passed")
        cleaned = ticket_boundary.cleanup_boundary(str(state["state_path"]))
        self.assertEqual(cleaned["status"], "cleaned")

    def test_cleanup_rejects_reset_target_without_integrated_ancestry(self) -> None:
        state = self.start_ticket("ticket-reset-target")
        self.commit_worker_change(state, "reset target\n")
        ticket_boundary.finish_boundary(str(state["state_path"]))
        ticket_boundary.integrate_boundary(str(state["state_path"]), strategy="cherry-pick")
        ticket_boundary.record_verification(str(state["state_path"]), result="passed")

        batch = json.loads(self.batch_state.read_text(encoding="utf-8"))
        git(self.repo, "reset", "--hard", str(batch["starting_sha"]))

        with self.assertRaisesRegex(ticket_boundary.BatchPlanError, "not an ancestor"):
            ticket_boundary.cleanup_boundary(str(state["state_path"]))
        self.assertTrue(Path(str(state["worktree"])).exists())
        self.assertNotEqual(git(self.repo, "branch", "--list", str(state["branch"])), "")

    def test_passed_verification_rejects_failed_or_missing_required_check(self) -> None:
        ticket_boundary.create_batch_plan(
            str(self.repo),
            str(self.batch_state),
            target_branch="main",
            starting_sha=git(self.repo, "rev-parse", "HEAD"),
            tickets=[
                {"ticket": "ticket-check", "required_checks": ["tests"]},
                {"ticket": "ticket-peer"},
            ],
        )
        state = ticket_boundary.start_boundary(
            str(self.repo),
            "ticket-check",
            batch_state=self.batch_state,
            worktree_root=self.root / "worktrees",
        )
        self.commit_worker_change(state, "check\n")
        ticket_boundary.finish_boundary(str(state["state_path"]))
        ticket_boundary.integrate_boundary(str(state["state_path"]), strategy="cherry-pick")

        with self.assertRaisesRegex(ticket_boundary.BatchPlanError, "failed required checks"):
            ticket_boundary.record_verification(
                str(state["state_path"]),
                result="passed",
                checks={"tests": "failed"},
            )
        with self.assertRaisesRegex(ticket_boundary.BatchPlanError, "every required check"):
            ticket_boundary.record_verification(
                str(state["state_path"]), result="passed"
            )

        failed = ticket_boundary.record_verification(
            str(state["state_path"]),
            result="failed",
            checks={"tests": "failed"},
        )
        self.assertEqual(failed["verification_result"], "failed")

    def test_damaged_verification_evidence_fails_closed(self) -> None:
        state = self.start_ticket("ticket-damaged")
        self.commit_worker_change(state, "damaged\n")
        ticket_boundary.finish_boundary(str(state["state_path"]))
        ticket_boundary.integrate_boundary(str(state["state_path"]), strategy="cherry-pick")
        persisted = json.loads(self.batch_state.read_text(encoding="utf-8"))
        record = next(item for item in persisted["tickets"] if item["ticket"] == "ticket-damaged")
        record["verification"] = {"result": "passed"}
        self.batch_state.write_text(json.dumps(persisted), encoding="utf-8")

        with self.assertRaisesRegex(ticket_boundary.BatchPlanError, "verification"):
            ticket_boundary.load_batch_state(str(self.batch_state))

    def test_damaged_integration_map_evidence_fails_closed(self) -> None:
        state = self.start_ticket("ticket-damaged-integration")
        self.commit_worker_change(state, "damaged integration\n")
        ticket_boundary.finish_boundary(str(state["state_path"]))
        ticket_boundary.integrate_boundary(str(state["state_path"]), strategy="cherry-pick")

        persisted = json.loads(self.batch_state.read_text(encoding="utf-8"))
        persisted["integrations"]["ticket-damaged-integration"] = {
            "commit": git(self.repo, "rev-parse", "HEAD"),
        }
        self.batch_state.write_text(json.dumps(persisted), encoding="utf-8")

        with self.assertRaisesRegex(ticket_boundary.BatchPlanError, "integration evidence"):
            ticket_boundary.load_batch_state(str(self.batch_state))

    def test_passed_verification_evidence_requires_all_required_checks(self) -> None:
        ticket_boundary.create_batch_plan(
            str(self.repo),
            str(self.batch_state),
            target_branch="main",
            starting_sha=git(self.repo, "rev-parse", "HEAD"),
            tickets=[
                {"ticket": "ticket-damaged-checks", "required_checks": ["tests"]},
                {"ticket": "ticket-peer"},
            ],
        )
        state = ticket_boundary.start_boundary(
            str(self.repo),
            "ticket-damaged-checks",
            batch_state=self.batch_state,
            worktree_root=self.root / "worktrees",
        )
        self.commit_worker_change(state, "damaged checks\n")
        ticket_boundary.finish_boundary(str(state["state_path"]))
        ticket_boundary.integrate_boundary(str(state["state_path"]), strategy="cherry-pick")
        ticket_boundary.record_verification(
            str(state["state_path"]), result="passed", checks={"tests": "passed"}
        )

        persisted = json.loads(self.batch_state.read_text(encoding="utf-8"))
        persisted["verifications"]["ticket-damaged-checks"]["checks"] = {}
        self.batch_state.write_text(json.dumps(persisted), encoding="utf-8")

        with self.assertRaisesRegex(ticket_boundary.BatchPlanError, "missing required checks"):
            ticket_boundary.load_batch_state(str(self.batch_state))

    def test_verification_target_evidence_must_be_on_target_history(self) -> None:
        ticket_boundary.create_batch_plan(
            str(self.repo),
            str(self.batch_state),
            target_branch="main",
            starting_sha=git(self.repo, "rev-parse", "HEAD"),
            tickets=[
                {"ticket": "ticket-verification-target"},
                {
                    "ticket": "ticket-dependent",
                    "dependencies": ["ticket-verification-target"],
                },
            ],
        )
        state = ticket_boundary.start_boundary(
            str(self.repo),
            "ticket-verification-target",
            batch_state=self.batch_state,
            worktree_root=self.root / "worktrees",
        )
        self.commit_worker_change(state, "verification target\n")
        ticket_boundary.finish_boundary(str(state["state_path"]))
        ticket_boundary.integrate_boundary(str(state["state_path"]), strategy="cherry-pick")
        ticket_boundary.record_verification(str(state["state_path"]), result="passed")

        persisted = json.loads(self.batch_state.read_text(encoding="utf-8"))
        record = next(
            item for item in persisted["tickets"] if item["ticket"] == "ticket-verification-target"
        )
        record["verification"]["target_branch"] = "wrong-branch"
        persisted["verifications"]["ticket-verification-target"]["target_branch"] = "wrong-branch"
        persisted["ticket_states"]["ticket-verification-target"]["verification"]["target_branch"] = "wrong-branch"
        self.batch_state.write_text(json.dumps(persisted), encoding="utf-8")
        blocked = ticket_boundary.query_batch_frontier(str(self.batch_state))
        self.assertNotIn("ticket-dependent", blocked["frontier"])
        self.assertIn("ticket-verification-target:verification", blocked["blocked"]["ticket-dependent"]["gates"])

        persisted = json.loads(self.batch_state.read_text(encoding="utf-8"))
        record = next(
            item for item in persisted["tickets"] if item["ticket"] == "ticket-verification-target"
        )
        record["verification"]["target_branch"] = "main"
        record["verification"]["target_head"] = "not-a-commit"
        persisted["verifications"]["ticket-verification-target"].update(
            {"target_branch": "main", "target_head": "not-a-commit"}
        )
        persisted["ticket_states"]["ticket-verification-target"]["verification"].update(
            {"target_branch": "main", "target_head": "not-a-commit"}
        )
        self.batch_state.write_text(json.dumps(persisted), encoding="utf-8")
        blocked = ticket_boundary.query_batch_frontier(str(self.batch_state))
        self.assertNotIn("ticket-dependent", blocked["frontier"])
        self.assertIn("ticket-verification-target:verification", blocked["blocked"]["ticket-dependent"]["gates"])

    def test_mirrored_lifecycle_evidence_must_match(self) -> None:
        ticket_boundary.create_batch_plan(
            str(self.repo),
            str(self.batch_state),
            target_branch="main",
            starting_sha=git(self.repo, "rev-parse", "HEAD"),
            tickets=[
                {"ticket": "ticket-mirrored"},
                {"ticket": "ticket-dependent", "dependencies": ["ticket-mirrored"]},
            ],
        )
        state = ticket_boundary.start_boundary(
            str(self.repo),
            "ticket-mirrored",
            batch_state=self.batch_state,
            worktree_root=self.root / "worktrees",
        )
        self.commit_worker_change(state, "mirrored evidence\n")
        ticket_boundary.finish_boundary(str(state["state_path"]))
        ticket_boundary.integrate_boundary(str(state["state_path"]), strategy="cherry-pick")
        ticket_boundary.record_verification(str(state["state_path"]), result="passed")

        persisted = json.loads(self.batch_state.read_text(encoding="utf-8"))
        record = next(item for item in persisted["tickets"] if item["ticket"] == "ticket-mirrored")
        mismatched_integration = dict(persisted["integrations"]["ticket-mirrored"])
        mismatched_integration["commit"] = persisted["starting_sha"]
        persisted["integrations"]["ticket-mirrored"] = mismatched_integration
        self.batch_state.write_text(json.dumps(persisted), encoding="utf-8")
        with self.assertRaisesRegex(ticket_boundary.BatchPlanError, "differs"):
            ticket_boundary.query_batch_frontier(str(self.batch_state))

        persisted = json.loads(self.batch_state.read_text(encoding="utf-8"))
        record = next(item for item in persisted["tickets"] if item["ticket"] == "ticket-mirrored")
        persisted["integrations"]["ticket-mirrored"] = dict(record["integration"])
        mismatched_verification = dict(persisted["verifications"]["ticket-mirrored"])
        mismatched_verification["result"] = "failed"
        mismatched_verification["status"] = "failed"
        persisted["verifications"]["ticket-mirrored"] = mismatched_verification
        self.batch_state.write_text(json.dumps(persisted), encoding="utf-8")
        with self.assertRaisesRegex(ticket_boundary.BatchPlanError, "differs"):
            ticket_boundary.query_batch_frontier(str(self.batch_state))

        persisted = json.loads(self.batch_state.read_text(encoding="utf-8"))
        record = next(item for item in persisted["tickets"] if item["ticket"] == "ticket-mirrored")
        persisted["verifications"]["ticket-mirrored"] = dict(record["verification"])
        persisted["ticket_states"]["ticket-mirrored"]["verification"] = {
            **record["verification"],
            "result": "failed",
            "status": "failed",
        }
        self.batch_state.write_text(json.dumps(persisted), encoding="utf-8")
        with self.assertRaisesRegex(ticket_boundary.BatchPlanError, "ticket state"):
            ticket_boundary.query_batch_frontier(str(self.batch_state))

    def test_worktree_name_uses_only_bounded_ticket_slug(self) -> None:
        ticket = "ticket/with-a-very-long-name-" + ("x" * 100)
        state = self.start_ticket(ticket)

        worktree_name = Path(str(state["worktree"])).name
        self.assertEqual(worktree_name, ticket_boundary.ticket_slug(ticket))
        self.assertLessEqual(len(worktree_name), 48)
        self.assertTrue(str(state["branch"]).startswith(f"codex/matt-ticket/{worktree_name}-"))

        self.commit_worker_change(state, "long ticket\n")
        ticket_boundary.finish_boundary(str(state["state_path"]))
        ticket_boundary.integrate_boundary(str(state["state_path"]), strategy="cherry-pick")
        ticket_boundary.record_verification(str(state["state_path"]), result="passed")
        ticket_boundary.cleanup_boundary(str(state["state_path"]))

    def test_merge_ticket_then_cleanup(self) -> None:
        state = self.start_ticket("ticket-merge")
        self.commit_worker_change(state, "merge ticket\n")
        ticket_boundary.finish_boundary(str(state["state_path"]))

        integrated = ticket_boundary.integrate_boundary(
            str(state["state_path"]), strategy="merge"
        )
        self.assertEqual(integrated["status"], "integrated")
        self.assertIn("Merge", git(self.repo, "log", "-1", "--format=%s"))

        ticket_boundary.record_verification(str(state["state_path"]), result="passed")
        ticket_boundary.cleanup_boundary(str(state["state_path"]))
        self.assertEqual(git(self.repo, "branch", "--list", str(state["branch"])), "")

    def test_conflicting_integration_is_recorded_for_main_agent(self) -> None:
        self.create_batch_plan(["ticket-first", "ticket-second"])
        first = self.start_ticket("ticket-first")
        second = self.start_ticket("ticket-second")

        for state, content in ((first, "first\n"), (second, "second\n")):
            worktree = Path(str(state["worktree"]))
            (worktree / "README.md").write_text(content, encoding="utf-8")
            git(worktree, "add", "README.md")
            git(worktree, "commit", "-m", str(state["ticket"]))
            ticket_boundary.finish_boundary(str(state["state_path"]))

        ticket_boundary.integrate_boundary(str(first["state_path"]), strategy="cherry-pick")
        ticket_boundary.record_verification(str(first["state_path"]), result="passed")
        ticket_boundary.cleanup_boundary(str(first["state_path"]))
        with self.assertRaisesRegex(RuntimeError, "integration conflict"):
            ticket_boundary.integrate_boundary(
                str(second["state_path"]), strategy="cherry-pick"
            )

        self.assertEqual(
            json_state(str(second["state_path"]))["status"], "integration_conflict"
        )
        persisted = json.loads(self.batch_state.read_text(encoding="utf-8"))
        second_record = next(record for record in persisted["tickets"] if record["ticket"] == "ticket-second")
        self.assertEqual(second_record["status"], "integration_conflict")
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
        ticket_boundary.record_verification(str(second["state_path"]), result="passed")
        ticket_boundary.cleanup_boundary(str(second["state_path"]))
        self.assertEqual(git(self.repo, "status", "--porcelain=v1"), "")

    def test_aborted_cherry_pick_cannot_be_recorded_as_integration(self) -> None:
        self.create_batch_plan(["ticket-first", "ticket-second"])
        first = self.start_ticket("ticket-first")
        second = self.start_ticket("ticket-second")
        for state, content in ((first, "first\n"), (second, "second\n")):
            worktree = Path(str(state["worktree"]))
            (worktree / "README.md").write_text(content, encoding="utf-8")
            git(worktree, "add", "README.md")
            git(worktree, "commit", "-m", str(state["ticket"]))
            ticket_boundary.finish_boundary(str(state["state_path"]))

        ticket_boundary.integrate_boundary(str(first["state_path"]), strategy="cherry-pick")
        ticket_boundary.record_verification(str(first["state_path"]), result="passed")
        ticket_boundary.cleanup_boundary(str(first["state_path"]))
        with self.assertRaisesRegex(RuntimeError, "integration conflict"):
            ticket_boundary.integrate_boundary(str(second["state_path"]), strategy="cherry-pick")

        subprocess.run(
            ["git", "-C", str(self.repo), "cherry-pick", "--abort"],
            check=True,
            capture_output=True,
            text=True,
        )
        (self.repo / "README.md").write_text("forged\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "ticket-second")
        with self.assertRaisesRegex(RuntimeError, "does not contain the cherry-picked"):
            ticket_boundary.integrate_boundary(
                str(second["state_path"]),
                strategy="cherry-pick",
                continue_integration=True,
            )
        persisted = json.loads(self.batch_state.read_text(encoding="utf-8"))
        second_record = next(item for item in persisted["tickets"] if item["ticket"] == "ticket-second")
        self.assertEqual(second_record["status"], "integration_conflict")


def json_state(path: str) -> dict[str, object]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
