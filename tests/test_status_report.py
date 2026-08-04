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


class BatchStatusReportCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Status Report Test")
        self.git("config", "user.email", "status-report@example.test")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-m", "base")
        self.state = self.root / "batch-state.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def git(self, *args: str, check: bool = True) -> str:
        return self.git_at(self.repo, *args, check=check)

    def git_at(self, repo: Path, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
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

    def create(self, tickets: list[dict[str, object]]) -> dict[str, object]:
        result = self.cli(
            "plan",
            "create",
            "--repo",
            str(self.repo),
            "--state",
            str(self.state),
            "--target-branch",
            "main",
            "--tickets-json",
            json.dumps(tickets),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def start(self, ticket: str) -> dict[str, object]:
        result = self.cli(
            "start",
            "--repo",
            str(self.repo),
            "--ticket",
            ticket,
            "--batch-state",
            str(self.state),
            "--worktree-root",
            str(self.root / "worktrees"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_status_reports_frontier_ticket_states_and_blocked_gates(self) -> None:
        self.create(
            [
                {"ticket": "01"},
                {"ticket": "02", "dependencies": ["01"], "required_checks": ["tests"]},
            ]
        )

        result = self.cli("status", "--state", str(self.state))

        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertTrue(status["verified"])
        self.assertEqual(status["frontier"], ["01"])
        self.assertEqual(status["current_frontier"], ["01"])
        records = {record["ticket"]: record for record in status["tickets"]}
        self.assertEqual(records["01"]["status"], "planned")
        self.assertEqual(records["02"]["status"], "planned")
        self.assertEqual(records["02"]["unmet_predecessors"], ["01"])
        self.assertEqual(records["02"]["verification_gates"], [])
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["frontier"], ["01"])

    def test_completion_report_is_dependency_ordered_and_audits_worker_artifacts(self) -> None:
        self.create(
            [
                {"ticket": "02", "dependencies": ["01"]},
                {"ticket": "01"},
            ]
        )
        first = self.start("01")
        first_worktree = Path(str(first["worktree"]))
        (first_worktree / "first.txt").write_text("first\n", encoding="utf-8")
        self.git_at(first_worktree, "add", "first.txt")
        self.git_at(first_worktree, "commit", "-m", "first")
        self.assertEqual(self.cli("finish", "--state", str(first["state_path"])).returncode, 0)
        self.assertEqual(
            self.cli("integrate", "--state", str(first["state_path"])).returncode,
            0,
        )
        self.assertEqual(
            self.cli("verify", "--state", str(first["state_path"]), "--result", "passed").returncode,
            0,
        )
        second = self.start("02")
        second_worktree = Path(str(second["worktree"]))
        (second_worktree / "second.txt").write_text("second\n", encoding="utf-8")
        self.git_at(second_worktree, "add", "second.txt")
        self.git_at(second_worktree, "commit", "-m", "second")
        self.assertEqual(self.cli("finish", "--state", str(second["state_path"])).returncode, 0)
        self.assertEqual(
            self.cli("integrate", "--state", str(second["state_path"])).returncode,
            0,
        )
        self.assertEqual(
            self.cli("verify", "--state", str(second["state_path"]), "--result", "passed").returncode,
            0,
        )
        self.assertEqual(self.cli("cleanup", "--state", str(first["state_path"])).returncode, 0)
        self.assertEqual(self.cli("cleanup", "--state", str(second["state_path"])).returncode, 0)

        result = self.cli("report", "--state", str(self.state))

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["complete"])
        self.assertEqual([record["ticket"] for record in report["tickets"]], ["01", "02"])
        first_record, second_record = report["tickets"]
        self.assertEqual(first_record["status"], "cleaned")
        self.assertEqual(first_record["worker_branch"], first["branch"])
        self.assertEqual(first_record["start_sha"], first["start_sha"])
        self.assertTrue(first_record["integrated_commit"])
        self.assertEqual(first_record["verification_result"], "passed")
        self.assertEqual(second_record["status"], "cleaned")

    def test_status_keeps_independent_frontier_open_after_recorded_integration(self) -> None:
        self.create([{"ticket": "01"}, {"ticket": "02"}])
        started = self.start("01")
        worktree = Path(str(started["worktree"]))
        (worktree / "first.txt").write_text("first\n", encoding="utf-8")
        self.git_at(worktree, "add", "first.txt")
        self.git_at(worktree, "commit", "-m", "first")
        self.assertEqual(self.cli("finish", "--state", str(started["state_path"])).returncode, 0)
        self.assertEqual(self.cli("integrate", "--state", str(started["state_path"])).returncode, 0)
        self.assertEqual(
            self.cli("verify", "--state", str(started["state_path"]), "--result", "passed").returncode,
            0,
        )

        for command in ("status", "report"):
            result = self.cli(command, "--state", str(self.state))

            self.assertEqual(result.returncode, 0, result.stderr)
            projection = json.loads(result.stdout)
            self.assertIsNone(projection["target_drift"])
            self.assertEqual(projection["frontier"], ["02"])
            self.assertEqual(projection["current_frontier"], ["02"])
            self.assertEqual(projection["tickets_by_id"]["02"]["status"], "planned")

    def test_completion_report_does_not_misreport_orphaned_worker(self) -> None:
        self.create([{"ticket": "01"}, {"ticket": "peer"}])
        started = self.start("01")
        self.git("worktree", "remove", "--force", str(started["worktree"]))
        self.git("branch", "-D", str(started["branch"]))

        result = self.cli("report", "--state", str(self.state))

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["complete"])
        record = next(item for item in report["tickets"] if item["ticket"] == "01")
        self.assertEqual(record["status"], "orphaned")
        self.assertTrue(record["orphan_reasons"])

    def test_completion_report_does_not_misreport_integrated_worker_with_missing_branch(self) -> None:
        self.create([{"ticket": "01"}, {"ticket": "peer"}])
        started = self.start("01")
        worktree = Path(str(started["worktree"]))
        (worktree / "integrated.txt").write_text("integrated\n", encoding="utf-8")
        self.git_at(worktree, "add", "integrated.txt")
        self.git_at(worktree, "commit", "-m", "integrated worker")
        self.assertEqual(self.cli("finish", "--state", str(started["state_path"])).returncode, 0)
        self.assertEqual(self.cli("integrate", "--state", str(started["state_path"])).returncode, 0)
        self.assertEqual(
            self.cli("verify", "--state", str(started["state_path"]), "--result", "passed").returncode,
            0,
        )

        self.git("update-ref", "-d", f"refs/heads/{started['branch']}")

        result = self.cli("report", "--state", str(self.state))

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["complete"])
        record = next(item for item in report["tickets"] if item["ticket"] == "01")
        self.assertEqual(record["persisted_status"], "integrated")
        self.assertEqual(record["status"], "orphaned")
        self.assertIn("worker_branch_missing", record["orphan_reasons"])

    def test_status_and_report_fail_closed_when_target_head_drifts(self) -> None:
        (self.repo / "baseline.txt").write_text("baseline\n", encoding="utf-8")
        self.git("add", "baseline.txt")
        self.git("commit", "-m", "baseline before batch")
        self.create([{"ticket": "01"}, {"ticket": "peer"}])
        expected_head = json.loads(self.state.read_text(encoding="utf-8"))["frontier_sha"]
        (self.repo / "out_of_band.txt").write_text("drift\n", encoding="utf-8")
        self.git("add", "out_of_band.txt")
        self.git("commit", "-m", "out of band target change")

        def assert_drifted_projection(expected_actual: str) -> None:
            for command in ("status", "report"):
                result = self.cli(command, "--state", str(self.state))

                self.assertEqual(result.returncode, 0, result.stderr)
                projection = json.loads(result.stdout)
                self.assertFalse(projection["complete"])
                self.assertEqual(projection["target_drift"]["expected_head"], expected_head)
                self.assertEqual(projection["target_drift"]["actual_head"], expected_actual)
                self.assertEqual(projection["frontier"], [])
                self.assertEqual(projection["current_frontier"], [])
                self.assertIn("target:stale", projection["blocked"]["01"]["gates"])
                record = next(item for item in projection["tickets"] if item["ticket"] == "01")
                self.assertIn("target:stale", record["verification_gates"])

        advanced_head = self.git("rev-parse", "main")
        assert_drifted_projection(advanced_head)

        self.git("reset", "--hard", "HEAD~2")
        reset_head = self.git("rev-parse", "main")
        self.assertNotEqual(reset_head, expected_head)
        assert_drifted_projection(reset_head)

    def test_status_and_report_have_no_compatibility_aliases(self) -> None:
        self.create([{"ticket": "01"}, {"ticket": "peer"}])

        top_level = self.cli("batch-status", "--state", str(self.state))
        nested = self.cli("plan", "status", "--state", str(self.state))

        self.assertNotEqual(top_level.returncode, 0)
        self.assertNotEqual(nested.returncode, 0)

if __name__ == "__main__":
    unittest.main()
