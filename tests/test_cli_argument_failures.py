from __future__ import annotations

import json
import subprocess
import sys
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


class CliArgumentFailureTests(unittest.TestCase):
    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def assert_structured_argument_failure(
        self, completed: subprocess.CompletedProcess[str]
    ) -> dict[str, object]:
        self.assertEqual(completed.returncode, 2, completed.stdout)
        self.assertEqual(completed.stdout, "")
        self.assertNotIn("usage:", completed.stderr.lower())
        payload = json.loads(completed.stderr)
        self.assertFalse(payload["verified"])
        self.assertEqual(payload["error_code"], "cli_argument_error")
        self.assertTrue(payload["error"])
        details = payload["details"]
        for field in ("ticket", "status", "unmet_predecessors", "gates", "target_head"):
            self.assertIn(field, details)
        self.assertIsInstance(details["unmet_predecessors"], list)
        self.assertIsInstance(details["gates"], list)
        return payload

    def test_missing_required_argument_is_structured(self) -> None:
        payload = self.assert_structured_argument_failure(
            self.cli("start", "--repo", "missing-repository")
        )
        self.assertIn("--ticket", payload["error"])
        self.assertEqual(payload["details"]["ticket"], None)

    def test_invalid_option_is_structured_and_preserves_known_ticket(self) -> None:
        payload = self.assert_structured_argument_failure(
            self.cli(
                "start",
                "--repo",
                "missing-repository",
                "--ticket",
                "01",
                "--not-an-option",
            )
        )
        self.assertIn("--not-an-option", payload["error"])
        self.assertEqual(payload["details"]["ticket"], "01")

    def test_unknown_command_uses_the_same_failure_envelope(self) -> None:
        payload = self.assert_structured_argument_failure(self.cli("not-a-command"))
        self.assertIn("invalid choice", payload["error"])

    def test_missing_nested_command_is_structured(self) -> None:
        payload = self.assert_structured_argument_failure(self.cli("plan"))
        self.assertIn("plan_command", payload["error"])

    def test_invalid_choice_is_structured(self) -> None:
        payload = self.assert_structured_argument_failure(
            self.cli("verify", "--result", "not-a-result")
        )
        self.assertIn("invalid choice", payload["error"])

    def test_help_remains_human_readable_and_successful(self) -> None:
        for command in ((), ("start",), ("plan", "create")):
            with self.subTest(command=command):
                completed = self.cli(*command, "--help")
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stderr, "")
                self.assertIn("usage:", completed.stdout.lower())
                self.assertNotIn('"verified"', completed.stdout)


if __name__ == "__main__":
    unittest.main()
