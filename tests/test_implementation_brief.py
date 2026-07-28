from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "matt-implement-adapter"
    / "scripts"
    / "implementation_brief.py"
)
SPEC = importlib.util.spec_from_file_location("implementation_brief", SCRIPT_PATH)
assert SPEC and SPEC.loader
implementation_brief = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = implementation_brief
SPEC.loader.exec_module(implementation_brief)


def write_brief(root: Path, relative_path: str, content: str) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class ImplementationBriefDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_missing_brief_directory_is_optional(self) -> None:
        catalog = implementation_brief.discover_briefs(self.root, ["01-login"])

        self.assertFalse(catalog.found)
        self.assertEqual(catalog.matched, {})
        self.assertEqual(catalog.missing, ("1",))

    def test_matches_brief_by_ticket_metadata(self) -> None:
        path = write_brief(
            self.root,
            ".scratch/add-login/implementation-briefs/01-login.md",
            """---
ticket: 01
feature: add-login
status: ready-for-implement
---

# Login implementation brief
""",
        )

        catalog = implementation_brief.discover_briefs(self.root, ["01-login"])

        self.assertTrue(catalog.found)
        self.assertEqual(catalog.matched["1"].path, path.resolve())
        self.assertEqual(catalog.matched["1"].status, "ready-for-implement")
        self.assertEqual(catalog.missing, ())

    def test_matches_ticket_path_and_normalizes_numeric_id(self) -> None:
        path = write_brief(
            self.root,
            ".scratch/add-login/implementation-briefs/01-login.md",
            """---
ticket: 01
status: approved
---
""",
        )

        catalog = implementation_brief.discover_briefs(
            self.root,
            [".scratch/add-login/issues/01-login.md"],
        )

        self.assertEqual(catalog.matched["1"].path, path.resolve())

    def test_partial_briefs_are_returned_without_blocking(self) -> None:
        path = write_brief(
            self.root,
            ".scratch/add-login/implementation-briefs/01-login.md",
            "---\nticket: 01\nstatus: ready\n---\n",
        )

        catalog = implementation_brief.discover_briefs(
            self.root,
            ["01-login", "02-session"],
        )

        self.assertTrue(catalog.found)
        self.assertEqual(catalog.matched["1"].path, path.resolve())
        self.assertEqual(catalog.missing, ("2",))

    def test_non_ready_brief_is_ignored(self) -> None:
        path = write_brief(
            self.root,
            ".scratch/add-login/implementation-briefs/01-login.md",
            "---\nticket: 01\nstatus: draft\n---\n",
        )

        catalog = implementation_brief.discover_briefs(self.root, ["01-login"])

        self.assertFalse(catalog.found)
        self.assertEqual(catalog.matched, {})
        self.assertEqual(catalog.missing, ("1",))
        self.assertEqual(len(catalog.ignored), 1)
        self.assertEqual(catalog.ignored[0].path, path.resolve())
        self.assertIn("status", catalog.ignored[0].reason)

    def test_brief_without_ticket_metadata_needs_numeric_filename(self) -> None:
        path = write_brief(
            self.root,
            ".scratch/add-login/implementation-briefs/login.md",
            "# Login implementation brief\n",
        )

        catalog = implementation_brief.discover_briefs(self.root, ["01-login"])

        self.assertFalse(catalog.found)
        self.assertEqual(catalog.missing, ("1",))
        self.assertEqual(len(catalog.ignored), 1)
        self.assertEqual(catalog.ignored[0].path, path.resolve())
        self.assertIn("missing ticket", catalog.ignored[0].reason)

    def test_ambiguous_briefs_are_ignored(self) -> None:
        write_brief(
            self.root,
            ".scratch/first/implementation-briefs/01-login.md",
            "---\nticket: 01\nstatus: approved\n---\n",
        )
        write_brief(
            self.root,
            ".scratch/second/implementation-briefs/01-login.md",
            "---\nticket: 01\nstatus: approved\n---\n",
        )

        catalog = implementation_brief.discover_briefs(self.root, ["01-login"])

        self.assertFalse(catalog.found)
        self.assertEqual(catalog.matched, {})
        self.assertEqual(catalog.missing, ("1",))
        self.assertEqual(len(catalog.ignored), 1)
        self.assertIn("ambiguous", catalog.ignored[0].reason)


if __name__ == "__main__":
    unittest.main()
