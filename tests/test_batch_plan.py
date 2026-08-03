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


class BatchPlanCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Batch Plan Test")
        self.git("config", "user.email", "batch-plan@example.test")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-m", "base")
        self.state = self.root / "batch-state.json"

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

    def create(self, tickets: list[dict[str, object]]) -> subprocess.CompletedProcess[str]:
        return self.cli(
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

    def test_create_reload_and_query_linear_frontier(self) -> None:
        created = self.create(
            [
                {"ticket": "01", "dependencies": [], "required_checks": ["tests"]},
                {"ticket": "02", "dependencies": ["01"], "required_checks": []},
            ]
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        result = json.loads(created.stdout)
        self.assertTrue(result["verified"])
        self.assertEqual(result["target_branch"], "main")
        self.assertEqual(result["starting_sha"], self.git("rev-parse", "HEAD"))
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["frontier"], ["01"])
        self.assertTrue(self.state.exists())

        queried = self.cli("plan", "frontier", "--state", str(self.state))
        self.assertEqual(queried.returncode, 0, queried.stderr)
        reloaded = json.loads(queried.stdout)
        self.assertEqual(reloaded["frontier"], ["01"])
        self.assertEqual(reloaded["tickets"], result["tickets"])

    def test_create_reports_fork_and_join_frontiers(self) -> None:
        fork_state = self.root / "fork.json"
        fork = self.cli(
            "plan",
            "create",
            "--repo",
            str(self.repo),
            "--state",
            str(fork_state),
            "--target-branch",
            "main",
            "--tickets-json",
            json.dumps(
                [
                    {"ticket": "01"},
                    {"ticket": "02", "dependencies": ["01"]},
                    {"ticket": "03", "dependencies": ["01"]},
                ]
            ),
        )
        self.assertEqual(fork.returncode, 0, fork.stderr)
        self.assertEqual(json.loads(fork.stdout)["frontier"], ["01"])

        join_state = self.root / "join.json"
        join = self.cli(
            "plan",
            "create",
            "--repo",
            str(self.repo),
            "--state",
            str(join_state),
            "--target-branch",
            "main",
            "--tickets-json",
            json.dumps(
                [
                    {"ticket": "01"},
                    {"ticket": "02"},
                    {"ticket": "03", "dependencies": ["01", "02"]},
                ]
            ),
        )
        self.assertEqual(join.returncode, 0, join.stderr)
        self.assertEqual(json.loads(join.stdout)["frontier"], ["01", "02"])

    def test_create_accepts_explicit_ticket_set_and_dependency_maps(self) -> None:
        completed = self.cli(
            "plan",
            "create",
            "--repo",
            str(self.repo),
            "--state",
            str(self.state),
            "--target-branch",
            "main",
            "--tickets-json",
            json.dumps(
                {
                    "tickets": ["01", "02"],
                    "dependencies": {"01": [], "02": ["01"]},
                    "required_checks": {"01": ["tests"], "02": []},
                }
            ),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        plan = json.loads(completed.stdout)
        self.assertEqual(plan["frontier"], ["01"])
        self.assertEqual(plan["tickets"][1]["direct_dependencies"], ["01"])

    def test_invalid_graph_fails_without_state_or_git_artifacts(self) -> None:
        cases = {
            "duplicate": [{"ticket": "01"}, {"ticket": "01"}],
            "unknown": [{"ticket": "01", "dependencies": ["missing"]}],
            "self": [{"ticket": "01", "dependencies": ["01"]}],
            "two-node-cycle": [
                {"ticket": "01", "dependencies": ["02"]},
                {"ticket": "02", "dependencies": ["01"]},
            ],
            "multi-node-cycle": [
                {"ticket": "01", "dependencies": ["03"]},
                {"ticket": "02", "dependencies": ["01"]},
                {"ticket": "03", "dependencies": ["02"]},
            ],
        }
        for name, tickets in cases.items():
            state = self.root / f"{name}.json"
            completed = self.cli(
                "plan",
                "create",
                "--repo",
                str(self.repo),
                "--state",
                str(state),
                "--target-branch",
                "main",
                "--tickets-json",
                json.dumps(tickets),
            )
            self.assertNotEqual(completed.returncode, 0, name)
            error = json.loads(completed.stderr)
            self.assertFalse(error["verified"])
            self.assertEqual(error["error_code"], "invalid_batch_plan")
            self.assertFalse(state.exists(), name)
            self.assertEqual(self.git("branch", "--list"), "* main", name)
            self.assertEqual(
                self.git("worktree", "list", "--porcelain").count("worktree "), 1, name
            )

    def test_single_ticket_plan_is_rejected_by_batch_qualification(self) -> None:
        completed = self.create([{"ticket": "01"}])
        self.assertNotEqual(completed.returncode, 0)
        error = json.loads(completed.stderr)
        self.assertEqual(error["error_code"], "single_ticket_batch")
        self.assertFalse(self.state.exists())

    def test_missing_corrupt_and_unsupported_state_fail_closed(self) -> None:
        missing = self.cli("plan", "frontier", "--state", str(self.root / "missing.json"))
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(json.loads(missing.stderr)["error_code"], "batch_state_missing")

        self.state.write_text("{broken", encoding="utf-8")
        corrupt = self.cli("plan", "frontier", "--state", str(self.state))
        self.assertNotEqual(corrupt.returncode, 0)
        self.assertEqual(json.loads(corrupt.stderr)["error_code"], "batch_state_corrupt")

        self.state.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")
        unsupported = self.cli("plan", "frontier", "--state", str(self.state))
        self.assertNotEqual(unsupported.returncode, 0)
        self.assertEqual(
            json.loads(unsupported.stderr)["error_code"], "batch_state_unsupported_schema"
        )

    def test_concurrent_plan_writes_are_serialized_and_atomic(self) -> None:
        tickets = json.dumps([{"ticket": "01"}, {"ticket": "02", "dependencies": ["01"]}])
        command = [
            sys.executable,
            str(SCRIPT_PATH),
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
            tickets,
        ]
        first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        first_output, first_error = first.communicate(timeout=30)
        second_output, second_error = second.communicate(timeout=30)
        self.assertEqual(
            sorted((first.returncode, second.returncode)), [0, 1],
            f"first={first_output!r} {first_error!r}; second={second_output!r} {second_error!r}",
        )
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema_version"], 1)
        self.assertEqual(persisted["tickets"][0]["ticket"], "01")
        self.assertEqual(list(self.state.parent.glob(f".{self.state.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
