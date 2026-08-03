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

    def test_integrated_predecessor_without_verification_stays_blocked(self) -> None:
        self.create_plan(
            [
                {"ticket": "01"},
                {"ticket": "02", "dependencies": ["01"]},
            ]
        )
        started = json.loads(self.start("01").stdout)
        worker = Path(str(started["worktree"]))
        (worker / "feature.txt").write_text("01\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(worker), "add", "feature.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(worker), "commit", "-m", "ticket 01"],
            check=True,
            capture_output=True,
            text=True,
        )
        finished = self.cli("finish", "--state", str(started["state_path"]))
        self.assertEqual(finished.returncode, 0, finished.stderr)
        integrated = self.cli("integrate", "--state", str(started["state_path"]))
        self.assertEqual(integrated.returncode, 0, integrated.stderr)

        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        predecessor = next(record for record in persisted["tickets"] if record["ticket"] == "01")
        self.assertEqual(predecessor["status"], "integrated")
        self.assertEqual(predecessor["integration"]["target_branch"], "main")
        self.assertEqual(predecessor["integration"]["strategy"], "cherry-pick")
        self.assertTrue(predecessor["integration"]["commit"])
        self.assertNotIn("verification", predecessor)

        blocked = self.start("02")
        self.assertNotEqual(blocked.returncode, 0)
        error = json.loads(blocked.stderr)
        self.assertEqual(error["error_code"], "ticket_not_runnable")
        self.assertEqual(error["details"]["unmet_predecessors"], [])
        self.assertTrue(any(gate.startswith("01:") for gate in error["details"]["gates"]))
        self.assertFalse((self.worktrees / "02").exists())
        self.assertEqual(self.git("branch", "--list", "codex/matt-ticket/02-*"), "")

    def test_passed_verification_unlocks_dependent_from_verified_target_head(self) -> None:
        self.create_plan(
            [
                {"ticket": "01", "required_checks": []},
                {"ticket": "02", "dependencies": ["01"]},
            ]
        )
        started = json.loads(self.start("01").stdout)
        worker = Path(str(started["worktree"]))
        (worker / "feature.txt").write_text("01\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worker), "add", "feature.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(worker), "commit", "-m", "ticket 01"], check=True
        )
        self.assertEqual(
            self.cli("finish", "--state", str(started["state_path"])).returncode, 0
        )
        integrated = json.loads(
            self.cli("integrate", "--state", str(started["state_path"])).stdout
        )
        integration_commit = str(integrated["integration_sha"])

        verified = self.cli(
            "verify", "--batch-state", str(self.state), "--ticket", "01", "--result", "passed"
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        verification = json.loads(verified.stdout)
        self.assertEqual(verification["verification_result"], "passed")
        self.assertEqual(verification["verification"]["required_checks"], [])

        dependent = self.start("02")
        self.assertEqual(dependent.returncode, 0, dependent.stderr)
        dependent_state = json.loads(dependent.stdout)
        target_head = self.git("rev-parse", "HEAD")
        self.assertEqual(dependent_state["verified_start_sha"], target_head)
        self.assertEqual(
            self.git("merge-base", "--is-ancestor", integration_commit, target_head, check=False),
            "",
        )
        self.assertTrue(dependent_state["predecessor_evidence"]["01"]["ancestor"])

        self.cli("cleanup", "--state", str(started["state_path"]))

    def test_verified_predecessor_unlocks_fork_frontier(self) -> None:
        self.create_plan(
            [
                {"ticket": "01"},
                {"ticket": "02", "dependencies": ["01"]},
                {"ticket": "03", "dependencies": ["01"]},
            ]
        )
        started = json.loads(self.start("01").stdout)
        worker = Path(str(started["worktree"]))
        (worker / "one.txt").write_text("01\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worker), "add", "one.txt"], check=True)
        subprocess.run(["git", "-C", str(worker), "commit", "-m", "01"], check=True)
        self.assertEqual(self.cli("finish", "--state", str(started["state_path"])).returncode, 0)
        self.assertEqual(self.cli("integrate", "--state", str(started["state_path"])).returncode, 0)
        self.assertEqual(
            self.cli("verify", "--state", str(started["state_path"]), "--result", "passed").returncode,
            0,
        )
        frontier = json.loads(self.cli("plan", "frontier", "--state", str(self.state)).stdout)
        self.assertEqual(frontier["frontier"], ["02", "03"])
        cleaned = self.cli("cleanup", "--state", str(started["state_path"]))
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        second = self.start("02")
        third = self.start("03")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(third.returncode, 0, third.stderr)

    def test_join_frontier_waits_for_every_verified_predecessor(self) -> None:
        self.create_plan(
            [
                {"ticket": "01"},
                {"ticket": "02"},
                {"ticket": "03", "dependencies": ["01", "02"]},
            ]
        )
        first = json.loads(self.start("01").stdout)
        second = json.loads(self.start("02").stdout)
        for state, filename in ((first, "one.txt"), (second, "two.txt")):
            worker = Path(str(state["worktree"]))
            (worker / filename).write_text(filename, encoding="utf-8")
            subprocess.run(["git", "-C", str(worker), "add", filename], check=True)
            subprocess.run(["git", "-C", str(worker), "commit", "-m", str(state["ticket"])], check=True)
            self.assertEqual(self.cli("finish", "--state", str(state["state_path"])).returncode, 0)

        self.assertEqual(self.cli("integrate", "--state", str(first["state_path"])).returncode, 0)
        self.assertEqual(
            self.cli("verify", "--state", str(first["state_path"]), "--result", "passed").returncode,
            0,
        )
        blocked = self.start("03")
        self.assertNotEqual(blocked.returncode, 0)
        blocked_error = json.loads(blocked.stderr)
        self.assertEqual(blocked_error["details"]["unmet_predecessors"], ["02"])

        self.assertEqual(self.cli("integrate", "--state", str(second["state_path"])).returncode, 0)
        self.assertEqual(
            self.cli("verify", "--state", str(second["state_path"]), "--result", "passed").returncode,
            0,
        )
        ready = self.start("03")
        self.assertEqual(ready.returncode, 0, ready.stderr)

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

    def test_integration_does_not_rebase_unstarted_frontier_ticket(self) -> None:
        plan = self.create_plan([{"ticket": "01"}, {"ticket": "02"}])
        first = json.loads(self.start("01").stdout)
        worker = Path(str(first["worktree"]))
        (worker / "one.txt").write_text("01\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worker), "add", "one.txt"], check=True)
        subprocess.run(["git", "-C", str(worker), "commit", "-m", "01"], check=True)
        self.assertEqual(
            self.cli("finish", "--state", str(first["state_path"])).returncode, 0
        )
        self.assertEqual(
            self.cli("integrate", "--state", str(first["state_path"])).returncode, 0
        )

        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(persisted["frontier_sha"], plan["starting_sha"])
        self.assertEqual(persisted["frontier_generation"], 0)

        second = self.start("02")
        self.assertEqual(second.returncode, 0, second.stderr)
        second_state = json.loads(second.stdout)
        self.assertEqual(second_state["verified_start_sha"], plan["starting_sha"])

        self.assertEqual(
            self.cli(
                "verify",
                "--state",
                str(first["state_path"]),
                "--result",
                "passed",
            ).returncode,
            0,
        )
        self.cli("cleanup", "--state", str(first["state_path"]))

    def test_fork_sibling_keeps_verified_frontier_after_peer_integration(self) -> None:
        plan = self.create_plan(
            [
                {"ticket": "01"},
                {"ticket": "02", "dependencies": ["01"]},
                {"ticket": "03", "dependencies": ["01"]},
            ]
        )
        first = json.loads(self.start("01").stdout)
        worker = Path(str(first["worktree"]))
        (worker / "one.txt").write_text("01\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worker), "add", "one.txt"], check=True)
        subprocess.run(["git", "-C", str(worker), "commit", "-m", "01"], check=True)
        self.assertEqual(
            self.cli("finish", "--state", str(first["state_path"])).returncode, 0
        )
        self.assertEqual(
            self.cli("integrate", "--state", str(first["state_path"])).returncode, 0
        )
        self.assertEqual(
            self.cli(
                "verify", "--state", str(first["state_path"]), "--result", "passed"
            ).returncode,
            0,
        )
        persisted_after_first = json.loads(self.state.read_text(encoding="utf-8"))
        frozen_frontier_sha = persisted_after_first["frontier_sha"]
        self.assertNotEqual(frozen_frontier_sha, plan["starting_sha"])

        second = json.loads(self.start("02").stdout)
        second_worker = Path(str(second["worktree"]))
        (second_worker / "two.txt").write_text("02\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(second_worker), "add", "two.txt"], check=True)
        subprocess.run(["git", "-C", str(second_worker), "commit", "-m", "02"], check=True)
        self.assertEqual(
            self.cli("finish", "--state", str(second["state_path"])).returncode, 0
        )
        self.assertEqual(
            self.cli("integrate", "--state", str(second["state_path"])).returncode, 0
        )

        sibling = self.start("03")
        self.assertEqual(sibling.returncode, 0, sibling.stderr)
        sibling_state = json.loads(sibling.stdout)
        self.assertEqual(sibling_state["verified_start_sha"], frozen_frontier_sha)
        self.assertEqual(sibling_state["verified_start_sha"], persisted_after_first["frontier_sha"])
        self.assertTrue(sibling_state["predecessor_evidence"]["01"]["ancestor"])

    def test_start_rejects_reset_to_historical_integration_head(self) -> None:
        self.create_plan(
            [
                {"ticket": "01"},
                {"ticket": "02", "dependencies": ["01"]},
                {"ticket": "03"},
            ]
        )

        first = json.loads(self.start("01").stdout)
        first_worker = Path(str(first["worktree"]))
        (first_worker / "one.txt").write_text("01\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(first_worker), "add", "one.txt"], check=True)
        subprocess.run(["git", "-C", str(first_worker), "commit", "-m", "01"], check=True)
        self.assertEqual(self.cli("finish", "--state", str(first["state_path"])).returncode, 0)
        self.assertEqual(self.cli("integrate", "--state", str(first["state_path"])).returncode, 0)
        self.assertEqual(
            self.cli("verify", "--state", str(first["state_path"]), "--result", "passed").returncode,
            0,
        )

        third = json.loads(self.start("03").stdout)
        third_worker = Path(str(third["worktree"]))
        (third_worker / "three.txt").write_text("03\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(third_worker), "add", "three.txt"], check=True)
        subprocess.run(["git", "-C", str(third_worker), "commit", "-m", "03"], check=True)
        self.assertEqual(self.cli("finish", "--state", str(third["state_path"])).returncode, 0)
        self.assertEqual(self.cli("integrate", "--state", str(third["state_path"])).returncode, 0)
        self.assertEqual(
            self.cli("verify", "--state", str(third["state_path"]), "--result", "passed").returncode,
            0,
        )

        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        first_record = next(record for record in persisted["tickets"] if record["ticket"] == "01")
        historical_head = first_record["integration"]["commit"]
        expected_head = persisted["frontier_sha"]
        self.assertNotEqual(historical_head, expected_head)
        self.git("reset", "--hard", historical_head)

        stale = self.start("02")
        self.assertNotEqual(stale.returncode, 0)
        error = json.loads(stale.stderr)
        self.assertEqual(error["error_code"], "target_head_stale")
        self.assertEqual(error["details"]["target_head"], historical_head)
        self.assertEqual(error["details"]["expected_head"], expected_head)
        self.assertFalse((self.worktrees / "02").exists())

    def test_failed_verification_keeps_frontier_generation_frozen(self) -> None:
        plan = self.create_plan([{"ticket": "01"}, {"ticket": "02"}])

        first = json.loads(self.start("01").stdout)
        first_worker = Path(str(first["worktree"]))
        (first_worker / "one.txt").write_text("01\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(first_worker), "add", "one.txt"], check=True)
        subprocess.run(["git", "-C", str(first_worker), "commit", "-m", "01"], check=True)
        self.assertEqual(self.cli("finish", "--state", str(first["state_path"])).returncode, 0)
        self.assertEqual(self.cli("integrate", "--state", str(first["state_path"])).returncode, 0)
        self.assertEqual(
            self.cli("verify", "--state", str(first["state_path"]), "--result", "failed").returncode,
            0,
        )

        second = json.loads(self.start("02").stdout)
        second_worker = Path(str(second["worktree"]))
        (second_worker / "two.txt").write_text("02\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(second_worker), "add", "two.txt"], check=True)
        subprocess.run(["git", "-C", str(second_worker), "commit", "-m", "02"], check=True)
        self.assertEqual(self.cli("finish", "--state", str(second["state_path"])).returncode, 0)
        self.assertEqual(self.cli("integrate", "--state", str(second["state_path"])).returncode, 0)
        self.assertEqual(
            self.cli("verify", "--state", str(second["state_path"]), "--result", "passed").returncode,
            0,
        )

        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(persisted["frontier_sha"], plan["starting_sha"])
        self.assertEqual(persisted["frontier_generation"], 0)
        self.assertEqual(persisted["frontier_tickets"], ["01", "02"])

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

    def test_runtime_failure_reports_structured_lifecycle_details(self) -> None:
        self.create_plan([{"ticket": "01"}, {"ticket": "02"}])
        started = json.loads(self.start("01").stdout)
        failed = self.cli("finish", "--state", str(started["state_path"]))

        self.assertNotEqual(failed.returncode, 0)
        payload = json.loads(failed.stderr)
        self.assertEqual(payload["error_code"], "lifecycle_error")
        self.assertEqual(payload["details"]["ticket"], "01")
        self.assertEqual(payload["details"]["status"], "started")
        self.assertEqual(payload["details"]["target_head"], self.git("rev-parse", "HEAD"))
        self.assertEqual(payload["details"]["unmet_predecessors"], [])
        self.assertEqual(payload["details"]["gates"], [])

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
