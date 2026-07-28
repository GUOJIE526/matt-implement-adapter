#!/usr/bin/env python3
"""Create, verify, integrate, and clean up one ticket's Git worktree."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


STATE_SCHEMA_VERSION = 2


def run_git_process(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    result = run_git_process(repo, *args)
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise RuntimeError(message)
    return result.stdout.strip()


def resolve_repo(path: str | Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    root = run_git(candidate, "rev-parse", "--show-toplevel")
    return Path(root).resolve()


def require_clean(repo: Path) -> None:
    status = run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise RuntimeError(
            "worktree is not clean; preserve or resolve existing changes before the ticket batch"
        )


def write_state(state_path: Path, state: dict[str, object]) -> None:
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_state(state_arg: str) -> tuple[Path, dict[str, object]]:
    state_path = Path(state_arg).expanduser().resolve()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    return state_path, state


def ticket_slug(ticket: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", ticket).strip("-.")
    return slug[:48] or "ticket"


def assert_current_branch(repo: Path, expected: str) -> None:
    branch = run_git(repo, "branch", "--show-current")
    if branch != expected:
        raise RuntimeError(f"branch changed during ticket: expected {expected!r}, got {branch!r}")


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return run_git_process(
        repo, "merge-base", "--is-ancestor", ancestor, descendant
    ).returncode == 0


def start_boundary(
    repo_arg: str,
    ticket: str,
    *,
    worktree_root: str | Path | None = None,
    branch_name: str | None = None,
) -> dict[str, object]:
    main_repo = resolve_repo(repo_arg)
    require_clean(main_repo)
    base_branch = run_git(main_repo, "branch", "--show-current")
    if not base_branch:
        raise RuntimeError("detached HEAD is not supported for a ticket batch")

    start_sha = run_git(main_repo, "rev-parse", "HEAD")
    token = uuid.uuid4().hex[:12]
    slug = ticket_slug(ticket)
    worker_branch = branch_name or f"codex/matt-ticket/{slug}-{token}"
    run_git(main_repo, "check-ref-format", "--branch", worker_branch)
    if (
        run_git_process(
            main_repo, "show-ref", "--verify", "--quiet", f"refs/heads/{worker_branch}"
        ).returncode
        == 0
    ):
        raise RuntimeError(f"worker branch already exists: {worker_branch}")

    if worktree_root is None:
        worktree_root_path = Path(tempfile.gettempdir()) / "matt-implement-adapter" / "worktrees"
    else:
        worktree_root_path = Path(worktree_root).expanduser().resolve()
    worktree_root_path.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_root_path / slug
    if worktree_path.exists():
        raise RuntimeError(f"worker worktree already exists: {worktree_path}")

    state_dir = Path(tempfile.gettempdir()) / "matt-implement-adapter" / "states"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / f"{uuid.uuid4().hex}.json"
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "repo": str(main_repo),
        "worktree": str(worktree_path),
        "ticket": ticket,
        "base_branch": base_branch,
        "branch": worker_branch,
        "start_sha": start_sha,
        "state_path": str(state_path),
        "status": "started",
    }

    try:
        run_git(
            main_repo,
            "worktree",
            "add",
            "-b",
            worker_branch,
            str(worktree_path),
            start_sha,
        )
        write_state(state_path, state)
    except (OSError, RuntimeError):
        if worktree_path.exists():
            run_git(main_repo, "worktree", "remove", "--force", str(worktree_path), check=False)
        run_git(main_repo, "branch", "-D", worker_branch, check=False)
        state_path.unlink(missing_ok=True)
        raise

    return state


def finish_boundary(state_arg: str) -> dict[str, object]:
    state_path, state = load_state(state_arg)
    worktree = resolve_repo(str(state.get("worktree", state["repo"])))
    require_clean(worktree)

    assert_current_branch(worktree, str(state["branch"]))
    start_sha = str(state["start_sha"])
    head_sha = run_git(worktree, "rev-parse", "HEAD")
    if head_sha == start_sha:
        raise RuntimeError("ticket produced no commit")
    if not is_ancestor(worktree, start_sha, head_sha):
        raise RuntimeError("ticket start SHA is not an ancestor of the final HEAD")

    commit_count = int(
        run_git(worktree, "rev-list", "--count", f"{start_sha}..{head_sha}")
    )
    if commit_count != 1:
        raise RuntimeError(f"ticket must add exactly one final commit; observed {commit_count}")

    changed_files = [
        line
        for line in run_git(
            worktree, "diff", "--name-only", f"{start_sha}..{head_sha}"
        ).splitlines()
        if line
    ]
    if not changed_files:
        raise RuntimeError("ticket commit contains no changed files")

    state.update(
        {
            "final_sha": head_sha,
            "commit_count": commit_count,
            "changed_files": changed_files,
            "worktree_clean": True,
            "verified": True,
            "status": "finished",
        }
    )
    write_state(state_path, state)
    return state


def integration_error(result: subprocess.CompletedProcess) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or "git integration failed"
    return f"integration conflict or failure: {detail}"


def mark_integrated(
    state_path: Path,
    state: dict[str, object],
    repo: Path,
    strategy: str,
    target_branch: str,
) -> dict[str, object]:
    head_sha = run_git(repo, "rev-parse", "HEAD")
    start_sha = str(state["integration_start_sha"])
    if head_sha == start_sha:
        raise RuntimeError("integration produced no commit")

    final_sha = str(state["final_sha"])
    if strategy == "merge":
        if not is_ancestor(repo, final_sha, head_sha):
            raise RuntimeError("integrated HEAD does not contain the ticket commit")

    state.update(
        {
            "status": "integrated",
            "integration_strategy": strategy,
            "integrated_into": target_branch,
            "integration_sha": head_sha,
        }
    )
    state.pop("integration_error", None)
    write_state(state_path, state)
    return state


def integrate_boundary(
    state_arg: str,
    *,
    strategy: str = "cherry-pick",
    target_branch: str | None = None,
    continue_integration: bool = False,
) -> dict[str, object]:
    if strategy not in {"merge", "cherry-pick"}:
        raise ValueError("integration strategy must be 'merge' or 'cherry-pick'")

    state_path, state = load_state(state_arg)
    main_repo = resolve_repo(str(state["repo"]))
    target = target_branch or str(state.get("base_branch", ""))
    if not target:
        raise ValueError("state does not contain a target branch")
    assert_current_branch(main_repo, target)

    if continue_integration:
        if state.get("status") != "integration_conflict":
            raise RuntimeError("there is no recorded integration conflict to continue")
        if state.get("integration_strategy") != strategy:
            raise RuntimeError("integration strategy does not match the recorded conflict")
        if run_git_process(main_repo, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
            raise RuntimeError("finish the merge with git merge --continue before recording integration")
        if run_git_process(main_repo, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD").returncode == 0:
            raise RuntimeError("finish the cherry-pick with git cherry-pick --continue before recording integration")
        require_clean(main_repo)
        return mark_integrated(
            repo=main_repo,
            state_path=state_path,
            state=state,
            strategy=strategy,
            target_branch=target,
        )

    if state.get("status") != "finished":
        raise RuntimeError("ticket must pass finish boundary before integration")
    require_clean(main_repo)
    integration_start_sha = run_git(main_repo, "rev-parse", "HEAD")
    state.update(
        {
            "status": "integrating",
            "integration_strategy": strategy,
            "integrated_into": target,
            "integration_start_sha": integration_start_sha,
        }
    )
    write_state(state_path, state)

    if strategy == "merge":
        result = run_git_process(
            main_repo, "merge", "--no-ff", "--no-edit", str(state["branch"])
        )
    else:
        result = run_git_process(main_repo, "cherry-pick", str(state["final_sha"]))

    if result.returncode != 0:
        state["status"] = "integration_conflict"
        state["integration_error"] = integration_error(result)
        write_state(state_path, state)
        raise RuntimeError(str(state["integration_error"]))

    return mark_integrated(
        repo=main_repo,
        state_path=state_path,
        state=state,
        strategy=strategy,
        target_branch=target,
    )


def cleanup_boundary(state_arg: str) -> dict[str, object]:
    state_path, state = load_state(state_arg)
    if state.get("status") != "integrated":
        raise RuntimeError("only an integrated ticket can be cleaned up")

    main_repo = resolve_repo(str(state["repo"]))
    target_branch = str(state["integrated_into"])
    assert_current_branch(main_repo, target_branch)
    require_clean(main_repo)

    worktree_path = Path(str(state["worktree"])).expanduser().resolve()
    if worktree_path.exists():
        worktree = resolve_repo(worktree_path)
        require_clean(worktree)
        run_git(main_repo, "worktree", "remove", str(worktree_path))

    branch = str(state["branch"])
    strategy = str(state["integration_strategy"])
    delete_flag = "-d" if strategy == "merge" else "-D"
    run_git(main_repo, "branch", delete_flag, branch)

    state.update({"status": "cleaned", "worktree_removed": True, "branch_removed": True})
    write_state(state_path, state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create, verify, integrate, and clean up one ticket's Git worktree."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--repo", required=True)
    start_parser.add_argument("--ticket", required=True)
    start_parser.add_argument("--worktree-root")
    start_parser.add_argument("--branch")

    finish_parser = subparsers.add_parser("finish")
    finish_parser.add_argument("--state", required=True)

    integrate_parser = subparsers.add_parser("integrate")
    integrate_parser.add_argument("--state", required=True)
    integrate_parser.add_argument(
        "--strategy", choices=("merge", "cherry-pick"), default="cherry-pick"
    )
    integrate_parser.add_argument("--target-branch")
    integrate_parser.add_argument("--continue", dest="continue_integration", action="store_true")

    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--state", required=True)

    args = parser.parse_args()
    try:
        if args.command == "start":
            result = start_boundary(
                args.repo,
                args.ticket,
                worktree_root=args.worktree_root,
                branch_name=args.branch,
            )
        elif args.command == "finish":
            result = finish_boundary(args.state)
        elif args.command == "integrate":
            result = integrate_boundary(
                args.state,
                strategy=args.strategy,
                target_branch=args.target_branch,
                continue_integration=args.continue_integration,
            )
        else:
            result = cleanup_boundary(args.state)
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(json.dumps({"verified": False, "error": str(error)}), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
