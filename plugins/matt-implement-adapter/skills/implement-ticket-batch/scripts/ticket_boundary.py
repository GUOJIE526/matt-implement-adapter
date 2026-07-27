#!/usr/bin/env python3
"""Create and verify a clean one-ticket/one-commit Git boundary."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise RuntimeError(message)
    return result.stdout.strip()


def resolve_repo(path: str) -> Path:
    candidate = Path(path).expanduser().resolve()
    root = run_git(candidate, "rev-parse", "--show-toplevel")
    return Path(root).resolve()


def require_clean(repo: Path) -> None:
    status = run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise RuntimeError(
            "worktree is not clean; preserve or resolve existing changes before the ticket batch"
        )


def start_boundary(repo_arg: str, ticket: str) -> dict[str, object]:
    repo = resolve_repo(repo_arg)
    require_clean(repo)
    start_sha = run_git(repo, "rev-parse", "HEAD")
    branch = run_git(repo, "branch", "--show-current")
    if not branch:
        raise RuntimeError("detached HEAD is not supported for a ticket batch")

    state_dir = Path(tempfile.gettempdir()) / "matt-implement-adapter"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / f"{uuid.uuid4().hex}.json"
    state = {
        "schema_version": 1,
        "repo": str(repo),
        "ticket": ticket,
        "branch": branch,
        "start_sha": start_sha,
        "state_path": str(state_path),
    }
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def finish_boundary(state_arg: str) -> dict[str, object]:
    state_path = Path(state_arg).expanduser().resolve()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    repo = resolve_repo(str(state["repo"]))
    require_clean(repo)

    start_sha = str(state["start_sha"])
    branch = run_git(repo, "branch", "--show-current")
    if branch != state["branch"]:
        raise RuntimeError(
            f"branch changed during ticket: expected {state['branch']!r}, got {branch!r}"
        )

    head_sha = run_git(repo, "rev-parse", "HEAD")
    if head_sha == start_sha:
        raise RuntimeError("ticket produced no commit")

    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", start_sha, head_sha],
        capture_output=True,
    )
    if ancestor.returncode != 0:
        raise RuntimeError("ticket start SHA is not an ancestor of the final HEAD")

    commit_count = int(run_git(repo, "rev-list", "--count", f"{start_sha}..{head_sha}"))
    if commit_count != 1:
        raise RuntimeError(
            f"ticket must add exactly one final commit; observed {commit_count}"
        )

    changed_files = [
        line
        for line in run_git(repo, "diff", "--name-only", f"{start_sha}..{head_sha}").splitlines()
        if line
    ]
    if not changed_files:
        raise RuntimeError("ticket commit contains no changed files")

    return {
        **state,
        "final_sha": head_sha,
        "commit_count": commit_count,
        "changed_files": changed_files,
        "worktree_clean": True,
        "verified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or verify a clean one-ticket/one-commit Git boundary."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--repo", required=True)
    start_parser.add_argument("--ticket", required=True)

    finish_parser = subparsers.add_parser("finish")
    finish_parser.add_argument("--state", required=True)

    args = parser.parse_args()
    try:
        if args.command == "start":
            result = start_boundary(args.repo, args.ticket)
        else:
            result = finish_boundary(args.state)
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(json.dumps({"verified": False, "error": str(error)}), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
