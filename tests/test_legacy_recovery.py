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


class LegacyRecoveryCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Legacy Recovery Test")
        self.git("config", "user.email", "legacy-recovery@example.test")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-m", "base")
        self.batch_state = self.root / "batch-state.json"
        self.legacy_state = self.root / "legacy-state.json"
        self.legacy_worktree = self.root / "legacy-worktree"

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

    def create_integrated_legacy_state(self) -> tuple[str, str]:
        start_sha = self.git("rev-parse", "HEAD")
        worker_branch = "codex/matt-ticket/legacy-001-old"
        self.git(
            "worktree",
            "add",
            "-b",
            worker_branch,
            str(self.legacy_worktree),
            start_sha,
        )
        (self.legacy_worktree / "legacy.txt").write_text("legacy\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.legacy_worktree), "add", "legacy.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.legacy_worktree), "commit", "-m", "legacy-001"],
            check=True,
            capture_output=True,
            text=True,
        )
        final_sha = subprocess.run(
            ["git", "-C", str(self.legacy_worktree), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        self.git("cherry-pick", final_sha)
        integration_sha = self.git("rev-parse", "HEAD")
        self.legacy_state.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "repo": str(self.repo.resolve()),
                    "worktree": str(self.legacy_worktree.resolve()),
                    "ticket": "legacy-001",
                    "base_branch": "main",
                    "branch": worker_branch,
                    "start_sha": start_sha,
                    "final_sha": final_sha,
                    "commit_count": 1,
                    "changed_files": ["legacy.txt"],
                    "worktree_clean": True,
                    "verified": True,
                    "status": "integrated",
                    "integration_start_sha": start_sha,
                    "integration_strategy": "cherry-pick",
                    "integrated_into": "main",
                    "integration_sha": integration_sha,
                    "state_path": str(self.legacy_state.resolve()),
                }
            ),
            encoding="utf-8",
        )
        return final_sha, integration_sha

    def create_started_legacy_state(self, *, commit_worker: bool = True) -> dict[str, object]:
        start_sha = self.git("rev-parse", "HEAD")
        worker_branch = "codex/matt-ticket/legacy-lifecycle-old"
        self.git(
            "worktree",
            "add",
            "-b",
            worker_branch,
            str(self.legacy_worktree),
            start_sha,
        )
        state = {
            "schema_version": 2,
            "repo": str(self.repo.resolve()),
            "worktree": str(self.legacy_worktree.resolve()),
            "ticket": "legacy-lifecycle",
            "base_branch": "main",
            "branch": worker_branch,
            "start_sha": start_sha,
            "state_path": str(self.legacy_state.resolve()),
            "status": "started",
        }
        self.legacy_state.write_text(json.dumps(state), encoding="utf-8")
        (self.legacy_worktree / "lifecycle.txt").write_text("legacy lifecycle\n", encoding="utf-8")
        if commit_worker:
            subprocess.run(
                ["git", "-C", str(self.legacy_worktree), "add", "lifecycle.txt"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(self.legacy_worktree), "commit", "-m", "legacy-lifecycle"],
                check=True,
                capture_output=True,
                text=True,
            )
        return state

    def create_plan_for_legacy(
        self, starting_sha: str, *, legacy_ticket: str = "legacy-001"
    ) -> subprocess.CompletedProcess[str]:
        return self.cli(
            "plan",
            "create",
            "--repo",
            str(self.repo),
            "--state",
            str(self.batch_state),
            "--target-branch",
            "main",
            "--starting-sha",
            starting_sha,
            "--tickets-json",
            json.dumps(
                [
                    {"ticket": legacy_ticket},
                    {"ticket": "dependent-002", "dependencies": [legacy_ticket]},
                ]
            ),
        )

    def test_pre_batch_started_finished_and_integrated_states_remain_recoverable(self) -> None:
        self.create_started_legacy_state()

        finished = self.cli("finish", "--state", str(self.legacy_state))
        self.assertEqual(finished.returncode, 0, finished.stderr)
        finished_state = json.loads(finished.stdout)
        self.assertEqual(finished_state["status"], "finished")
        self.assertTrue(finished_state["final_sha"])

        integrated = self.cli("integrate", "--state", str(self.legacy_state))
        self.assertEqual(integrated.returncode, 0, integrated.stderr)
        integrated_state = json.loads(integrated.stdout)
        self.assertEqual(integrated_state["status"], "integrated")
        self.assertTrue(integrated_state["integration_sha"])

        cleaned = self.cli("cleanup", "--state", str(self.legacy_state))
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assertEqual(json.loads(cleaned.stdout)["status"], "cleaned")
        self.assertFalse(self.legacy_worktree.exists())
        self.assertEqual(self.git("branch", "--list", "codex/matt-ticket/legacy-lifecycle-old"), "")

    def test_invalid_legacy_import_fails_closed_without_mutating_either_state(self) -> None:
        _, integration_sha = self.create_integrated_legacy_state()
        plan = self.create_plan_for_legacy(integration_sha)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        original_batch = self.batch_state.read_bytes()
        original_legacy = self.legacy_state.read_bytes()

        cases = [
            ("corrupt", b"{broken", "legacy_state_corrupt"),
            (
                "unsupported",
                json.dumps({"schema_version": 999}).encode(),
                "legacy_state_unsupported_schema",
            ),
        ]
        for name, payload, expected_code in cases:
            self.legacy_state.write_bytes(payload)
            failed = self.cli(
                "legacy",
                "import",
                "--repo",
                str(self.repo),
                "--batch-state",
                str(self.batch_state),
                "--state",
                str(self.legacy_state),
            )
            self.assertNotEqual(failed.returncode, 0, name)
            self.assertEqual(json.loads(failed.stderr)["error_code"], expected_code, name)
            self.assertEqual(self.batch_state.read_bytes(), original_batch, name)
            self.assertEqual(self.legacy_state.read_bytes(), payload, name)

        self.legacy_state.write_bytes(original_legacy)
        legacy = json.loads(original_legacy)
        legacy["integration_sha"] = legacy["start_sha"]
        self.legacy_state.write_text(json.dumps(legacy), encoding="utf-8")
        forged_legacy = self.legacy_state.read_bytes()
        failed = self.cli(
            "legacy",
            "import",
            "--repo",
            str(self.repo),
            "--batch-state",
            str(self.batch_state),
            "--state",
            str(self.legacy_state),
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn(
            json.loads(failed.stderr)["error_code"],
            {"legacy_ancestry_mismatch", "legacy_evidence_invalid"},
        )
        self.assertEqual(self.batch_state.read_bytes(), original_batch)
        self.assertEqual(self.legacy_state.read_bytes(), forged_legacy)

        self.legacy_state.write_bytes(original_legacy)
        cleaned = self.cli("cleanup", "--state", str(self.legacy_state))
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)

    def test_import_rejects_unrelated_target_commit_as_legacy_integration(self) -> None:
        _, integration_sha = self.create_integrated_legacy_state()
        plan = self.create_plan_for_legacy(integration_sha)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        original_legacy = self.legacy_state.read_bytes()
        (self.repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        self.git("add", "unrelated.txt")
        self.git("commit", "-m", "unrelated target commit")
        unrelated_sha = self.git("rev-parse", "HEAD")
        forged = json.loads(original_legacy)
        forged["integration_sha"] = unrelated_sha
        self.legacy_state.write_text(json.dumps(forged), encoding="utf-8")

        failed = self.cli(
            "legacy",
            "import",
            "--repo",
            str(self.repo),
            "--batch-state",
            str(self.batch_state),
            "--state",
            str(self.legacy_state),
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(json.loads(failed.stderr)["error_code"], "legacy_ancestry_mismatch")

    def test_damaged_legacy_import_evidence_fails_closed_on_resume(self) -> None:
        _, integration_sha = self.create_integrated_legacy_state()
        plan = self.create_plan_for_legacy(integration_sha)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        imported = self.cli(
            "legacy",
            "import",
            "--repo",
            str(self.repo),
            "--batch-state",
            str(self.batch_state),
            "--state",
            str(self.legacy_state),
            "--result",
            "passed",
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        persisted = json.loads(self.batch_state.read_text(encoding="utf-8"))
        persisted["legacy_imports"]["legacy-001"]["ticket"] = "forged-ticket"
        self.batch_state.write_text(json.dumps(persisted), encoding="utf-8")

        resumed = self.cli("plan", "frontier", "--state", str(self.batch_state))
        self.assertNotEqual(resumed.returncode, 0)
        self.assertEqual(json.loads(resumed.stderr)["error_code"], "batch_state_corrupt")

        persisted = json.loads(self.batch_state.read_text(encoding="utf-8"))
        persisted["legacy_imports"]["legacy-001"]["ticket"] = "legacy-001"
        persisted["legacy_imports"]["legacy-001"]["worker_branch"] = None
        self.batch_state.write_text(json.dumps(persisted), encoding="utf-8")
        resumed = self.cli("plan", "frontier", "--state", str(self.batch_state))
        self.assertNotEqual(resumed.returncode, 0)
        self.assertEqual(json.loads(resumed.stderr)["error_code"], "batch_state_corrupt")

    def test_import_validates_repository_target_branch_and_ticket_identity(self) -> None:
        _, integration_sha = self.create_integrated_legacy_state()
        plan = self.create_plan_for_legacy(integration_sha)
        self.assertEqual(plan.returncode, 0, plan.stderr)

        wrong_ticket = self.cli(
            "legacy",
            "import",
            "--repo",
            str(self.repo),
            "--batch-state",
            str(self.batch_state),
            "--state",
            str(self.legacy_state),
            "--ticket",
            "not-legacy-001",
        )
        self.assertNotEqual(wrong_ticket.returncode, 0)
        self.assertEqual(json.loads(wrong_ticket.stderr)["error_code"], "legacy_ticket_mismatch")

        wrong_repo = self.cli(
            "legacy",
            "import",
            "--repo",
            str(self.legacy_worktree),
            "--batch-state",
            str(self.batch_state),
            "--state",
            str(self.legacy_state),
        )
        self.assertNotEqual(wrong_repo.returncode, 0)
        self.assertEqual(json.loads(wrong_repo.stderr)["error_code"], "legacy_repository_mismatch")

        wrong_branch = json.loads(self.legacy_state.read_text(encoding="utf-8"))
        wrong_branch["base_branch"] = "other-target"
        self.legacy_state.write_text(json.dumps(wrong_branch), encoding="utf-8")
        branch_error = self.cli(
            "legacy",
            "import",
            "--repo",
            str(self.repo),
            "--batch-state",
            str(self.batch_state),
            "--state",
            str(self.legacy_state),
        )
        self.assertNotEqual(branch_error.returncode, 0)
        self.assertEqual(json.loads(branch_error.stderr)["error_code"], "legacy_target_mismatch")

    def test_imported_started_state_resumes_through_batch_lifecycle(self) -> None:
        started = self.create_started_legacy_state(commit_worker=False)
        starting_sha = str(started["start_sha"])
        plan = self.create_plan_for_legacy(starting_sha, legacy_ticket="legacy-lifecycle")
        self.assertEqual(plan.returncode, 0, plan.stderr)

        imported = self.cli(
            "legacy",
            "import",
            "--repo",
            str(self.repo),
            "--batch-state",
            str(self.batch_state),
            "--state",
            str(self.legacy_state),
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertEqual(json.loads(imported.stdout)["status"], "started")

        persisted = json.loads(self.batch_state.read_text(encoding="utf-8"))
        record = next(item for item in persisted["tickets"] if item["ticket"] == "legacy-lifecycle")
        self.assertEqual(record["status"], "started")
        self.assertEqual(persisted["frontier"], [])

        subprocess.run(
            ["git", "-C", str(self.legacy_worktree), "add", "lifecycle.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.legacy_worktree), "commit", "-m", "legacy-lifecycle"],
            check=True,
            capture_output=True,
            text=True,
        )

        finished = self.cli("finish", "--state", str(self.legacy_state))
        self.assertEqual(finished.returncode, 0, finished.stderr)
        persisted = json.loads(self.batch_state.read_text(encoding="utf-8"))
        record = next(item for item in persisted["tickets"] if item["ticket"] == "legacy-lifecycle")
        self.assertEqual(record["status"], "finished")

        integrated = self.cli("integrate", "--state", str(self.legacy_state))
        self.assertEqual(integrated.returncode, 0, integrated.stderr)
        persisted = json.loads(self.batch_state.read_text(encoding="utf-8"))
        record = next(item for item in persisted["tickets"] if item["ticket"] == "legacy-lifecycle")
        self.assertEqual(record["status"], "integrated")

        verified = self.cli("verify", "--state", str(self.legacy_state), "--result", "passed")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        frontier = json.loads(
            self.cli("plan", "frontier", "--state", str(self.batch_state)).stdout
        )
        self.assertEqual(frontier["frontier"], ["dependent-002"])

        cleaned = self.cli("cleanup", "--state", str(self.legacy_state))
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assertEqual(json.loads(cleaned.stdout)["status"], "cleaned")

    def test_imported_legacy_integration_requires_explicit_verification_before_unlock(self) -> None:
        _, integration_sha = self.create_integrated_legacy_state()
        plan = self.cli(
            "plan",
            "create",
            "--repo",
            str(self.repo),
            "--state",
            str(self.batch_state),
            "--target-branch",
            "main",
            "--starting-sha",
            integration_sha,
            "--tickets-json",
            json.dumps(
                [
                    {"ticket": "legacy-001"},
                    {"ticket": "dependent-002", "dependencies": ["legacy-001"]},
                ]
            ),
        )
        self.assertEqual(plan.returncode, 0, plan.stderr)

        imported = self.cli(
            "legacy",
            "import",
            "--repo",
            str(self.repo),
            "--batch-state",
            str(self.batch_state),
            "--state",
            str(self.legacy_state),
            "--result",
            "passed",
        )

        self.assertEqual(imported.returncode, 0, imported.stderr)
        result = json.loads(imported.stdout)
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "integrated")
        self.assertEqual(result["verification"]["result"], "passed")

        frontier = json.loads(
            self.cli("plan", "frontier", "--state", str(self.batch_state)).stdout
        )
        self.assertEqual(frontier["frontier"], ["dependent-002"])
        self.assertEqual(frontier["frontier_sha"], integration_sha)

        started = self.cli(
            "start",
            "--repo",
            str(self.repo),
            "--ticket",
            "dependent-002",
            "--batch-state",
            str(self.batch_state),
            "--worktree-root",
            str(self.root / "new-worktrees"),
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(json.loads(started.stdout)["verified_start_sha"], integration_sha)

    def test_import_without_verification_keeps_dependent_blocked_until_recorded(self) -> None:
        _, integration_sha = self.create_integrated_legacy_state()
        plan = self.cli(
            "plan",
            "create",
            "--repo",
            str(self.repo),
            "--state",
            str(self.batch_state),
            "--target-branch",
            "main",
            "--starting-sha",
            integration_sha,
            "--tickets-json",
            json.dumps(
                [
                    {"ticket": "legacy-001"},
                    {"ticket": "dependent-002", "dependencies": ["legacy-001"]},
                ]
            ),
        )
        self.assertEqual(plan.returncode, 0, plan.stderr)

        imported = self.cli(
            "import",
            "--repo",
            str(self.repo),
            "--batch-state",
            str(self.batch_state),
            "--legacy-state",
            str(self.legacy_state),
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertIsNone(json.loads(imported.stdout)["verification"])

        blocked = json.loads(
            self.cli("plan", "frontier", "--state", str(self.batch_state)).stdout
        )
        self.assertEqual(blocked["frontier"], [])
        self.assertIn(
            "legacy-001:verification",
            blocked["blocked"]["dependent-002"]["gates"],
        )

        verified = self.cli(
            "verify",
            "--state",
            str(self.legacy_state),
            "--result",
            "passed",
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(json.loads(verified.stdout)["verification_result"], "passed")

        unlocked = json.loads(
            self.cli("plan", "frontier", "--state", str(self.batch_state)).stdout
        )
        self.assertEqual(unlocked["frontier"], ["dependent-002"])

    def test_imported_integrated_legacy_state_can_cleanup_before_verification(self) -> None:
        _, integration_sha = self.create_integrated_legacy_state()
        plan = self.create_plan_for_legacy(integration_sha)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        imported = self.cli(
            "legacy",
            "import",
            "--repo",
            str(self.repo),
            "--batch-state",
            str(self.batch_state),
            "--state",
            str(self.legacy_state),
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)

        cleaned = self.cli("cleanup", "--state", str(self.legacy_state))
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assertEqual(json.loads(cleaned.stdout)["status"], "cleaned")
        blocked = json.loads(
            self.cli("plan", "frontier", "--state", str(self.batch_state)).stdout
        )
        self.assertEqual(blocked["frontier"], [])


if __name__ == "__main__":
    unittest.main()
