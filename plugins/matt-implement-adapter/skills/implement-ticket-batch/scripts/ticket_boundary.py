#!/usr/bin/env python3
"""Create, verify, integrate, and clean up one ticket's Git worktree."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Iterator, Literal, TypedDict, cast


STATE_SCHEMA_VERSION = 2
BATCH_PLAN_SCHEMA_VERSION = 1
BATCH_PLAN_KIND = "matt-implement-batch-plan"
TICKET_STATUSES = frozenset(
    {
        "planned",
        "runnable",
        "started",
        "finished",
        "integrated",
        "cleaned",
        "integration_conflict",
        "failed",
        "orphaned",
    }
)
RUNNABLE_STATUSES = frozenset({"planned", "runnable"})
TicketStatus = Literal[
    "planned",
    "runnable",
    "started",
    "finished",
    "integrated",
    "cleaned",
    "integration_conflict",
    "failed",
    "orphaned",
]


class BatchTicketRecord(TypedDict):
    ticket: str
    dependencies: list[str]
    direct_dependencies: list[str]
    required_checks: list[str]
    status: TicketStatus


class BatchPlanError(RuntimeError):
    """A fail-closed, machine-readable batch plan/state error."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "invalid_batch_plan",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


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


@contextmanager
def batch_state_lock(state_path: Path) -> Iterator[None]:
    """Serialize batch-state readers/writers with an OS-level file lock."""

    lock_path = state_path.with_name(f"{state_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, document: dict[str, object]) -> None:
    """Replace a JSON state atomically, cleaning up failed temporary writes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_batch_state(state_path: Path, state: dict[str, object]) -> None:
    validate_batch_plan(state)
    with batch_state_lock(state_path):
        _atomic_write_json(state_path, state)


def _load_batch_state_unlocked(state_path: Path) -> dict[str, object]:
    if not state_path.exists():
        raise BatchPlanError(
            f"batch state does not exist: {state_path}",
            error_code="batch_state_missing",
        )
    try:
        document = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BatchPlanError(
            f"batch state is corrupted: {state_path}",
            error_code="batch_state_corrupt",
            details={"reason": str(error)},
        ) from error
    if not isinstance(document, dict):
        raise BatchPlanError(
            "batch state root must be a JSON object", error_code="batch_state_corrupt"
        )
    validate_batch_plan(document)
    return document


def load_batch_state(state_arg: str) -> tuple[Path, dict[str, object]]:
    state_path = Path(state_arg).expanduser().resolve()
    if not state_path.exists():
        raise BatchPlanError(
            f"batch state does not exist: {state_path}",
            error_code="batch_state_missing",
        )
    with batch_state_lock(state_path):
        state = _load_batch_state_unlocked(state_path)
    return state_path, state


def _ticket_identity(record: object) -> str:
    if isinstance(record, str):
        identity = record
    elif isinstance(record, dict):
        identity = record.get("ticket", record.get("ticket_id", record.get("id", "")))
    else:
        identity = ""
    if not isinstance(identity, str) or not identity.strip():
        raise BatchPlanError("ticket identity must be a non-empty string")
    return identity.strip()


def _string_list(value: object, field: str, ticket: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise BatchPlanError(
            f"{field} for ticket {ticket!r} must be a list of strings",
            details={"ticket": ticket, "field": field},
        )
    values = [item.strip() for item in value]
    if any(not item for item in values):
        raise BatchPlanError(
            f"{field} for ticket {ticket!r} contains an empty value",
            details={"ticket": ticket, "field": field},
        )
    if len(values) != len(set(values)):
        raise BatchPlanError(
            f"{field} for ticket {ticket!r} contains duplicates",
            details={"ticket": ticket, "field": field},
        )
    return values


def normalize_ticket_records(records: object) -> list[BatchTicketRecord]:
    if isinstance(records, dict) and "tickets" in records:
        ticket_values = records["tickets"]
        dependency_map = records.get("dependencies", records.get("direct_dependencies", {}))
        checks_map = records.get("required_checks", {})
        if not isinstance(dependency_map, dict) or not isinstance(checks_map, dict):
            raise BatchPlanError(
                "batch plan dependency and required-check maps must be JSON objects"
            )
        if isinstance(ticket_values, list):
            expanded: list[object] = []
            for value in ticket_values:
                ticket = _ticket_identity(value)
                if isinstance(value, dict):
                    item = dict(value)
                    item.setdefault("dependencies", dependency_map.get(ticket, []))
                    item.setdefault("required_checks", checks_map.get(ticket, []))
                else:
                    item = {
                        "ticket": ticket,
                        "dependencies": dependency_map.get(ticket, []),
                        "required_checks": checks_map.get(ticket, []),
                    }
                expanded.append(item)
            records = expanded
    if isinstance(records, dict):
        records = [
            {"ticket": ticket, "dependencies": dependencies}
            for ticket, dependencies in records.items()
        ]
    if not isinstance(records, list) or not records:
        raise BatchPlanError("tickets must be a non-empty JSON list")
    normalized: list[BatchTicketRecord] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    for raw in records:
        ticket = _ticket_identity(raw)
        if ticket in seen:
            duplicates.append(ticket)
            continue
        seen.add(ticket)
        if isinstance(raw, dict):
            dependencies = raw.get("dependencies", raw.get("direct_dependencies", []))
            required_checks = raw.get("required_checks", [])
            status = raw.get("status", "planned")
        else:
            dependencies = []
            required_checks = []
            status = "planned"
        if not isinstance(status, str) or status not in TICKET_STATUSES:
            raise BatchPlanError(
                f"unsupported status for ticket {ticket!r}",
                details={"ticket": ticket, "status": status},
            )
        normalized_dependencies = _string_list(dependencies, "dependencies", ticket)
        normalized_checks = _string_list(required_checks, "required_checks", ticket)
        item: BatchTicketRecord = {
            "ticket": ticket,
            "dependencies": normalized_dependencies,
            "direct_dependencies": normalized_dependencies.copy(),
            "required_checks": normalized_checks,
            "status": cast(TicketStatus, status),
        }
        normalized.append(item)
    if duplicates:
        raise BatchPlanError(
            "duplicate ticket identities are not allowed",
            details={"duplicates": sorted(set(duplicates))},
        )
    return normalized


def _validate_dependency_graph(records: list[BatchTicketRecord]) -> None:
    identifiers = [str(record["ticket"]) for record in records]
    known = set(identifiers)
    unknown: dict[str, list[str]] = {}
    self_dependencies: dict[str, list[str]] = {}
    graph: dict[str, list[str]] = {}
    for record in records:
        ticket = str(record["ticket"])
        dependencies = [str(item) for item in record["dependencies"]]
        graph[ticket] = dependencies
        missing = [dependency for dependency in dependencies if dependency not in known]
        if missing:
            unknown[ticket] = missing
        own = [dependency for dependency in dependencies if dependency == ticket]
        if own:
            self_dependencies[ticket] = own
    if unknown:
        raise BatchPlanError(
            "dependency references an unknown ticket",
            details={"unknown_dependencies": unknown},
        )
    if self_dependencies:
        raise BatchPlanError(
            "a ticket cannot depend on itself",
            details={"self_dependencies": self_dependencies},
        )

    visiting: set[str] = set()
    visited: set[str] = set()
    path: list[str] = []

    def visit(ticket: str) -> None:
        if ticket in visiting:
            cycle_start = path.index(ticket)
            cycle = path[cycle_start:] + [ticket]
            raise BatchPlanError(
                "dependency graph contains a cycle", details={"cycle": cycle}
            )
        if ticket in visited:
            return
        visiting.add(ticket)
        path.append(ticket)
        for dependency in graph[ticket]:
            visit(dependency)
        path.pop()
        visiting.remove(ticket)
        visited.add(ticket)

    for ticket in identifiers:
        visit(ticket)


def _require_batch_size(records: list[BatchTicketRecord]) -> None:
    if len(records) < 2:
        raise BatchPlanError(
            "batch plan requires at least two implementation tickets",
            error_code="single_ticket_batch",
        )


def _validate_worktree_slugs(records: list[BatchTicketRecord]) -> None:
    by_slug: dict[str, list[str]] = {}
    for record in records:
        ticket = str(record["ticket"])
        by_slug.setdefault(ticket_slug(ticket), []).append(ticket)
    collisions = {
        slug: sorted(tickets)
        for slug, tickets in by_slug.items()
        if len(tickets) > 1
    }
    if collisions:
        raise BatchPlanError(
            "ticket identities produce colliding worker worktree names",
            details={"worktree_slug_collisions": collisions},
        )


def validate_batch_plan(state: dict[str, object]) -> None:
    if state.get("schema_version") != BATCH_PLAN_SCHEMA_VERSION:
        raise BatchPlanError(
            "batch plan schema version is unsupported",
            error_code="batch_state_unsupported_schema",
            details={"schema_version": state.get("schema_version")},
        )
    if state.get("kind") != BATCH_PLAN_KIND:
        raise BatchPlanError("state is not a batch plan", error_code="batch_state_corrupt")
    for field in (
        "repo",
        "target_branch",
        "starting_sha",
        "batch_id",
        "tickets",
        "required_checks",
    ):
        if field not in state:
            raise BatchPlanError(
                f"batch plan is missing required field: {field}",
                error_code="batch_state_corrupt",
            )
    if not isinstance(state["target_branch"], str) or not state["target_branch"]:
        raise BatchPlanError("target_branch must be a non-empty string")
    if not isinstance(state["starting_sha"], str) or not state["starting_sha"]:
        raise BatchPlanError("starting_sha must be a non-empty string")
    if "frontier_sha" in state and (
        not isinstance(state["frontier_sha"], str) or not state["frontier_sha"]
    ):
        raise BatchPlanError(
            "frontier_sha must be a non-empty string",
            error_code="batch_state_corrupt",
        )
    if not isinstance(state["batch_id"], str) or not state["batch_id"]:
        raise BatchPlanError(
            "batch_id must be a non-empty string",
            error_code="batch_state_corrupt",
        )
    records = normalize_ticket_records(state["tickets"])
    _validate_dependency_graph(records)
    _require_batch_size(records)
    _validate_worktree_slugs(records)
    state["tickets"] = records
    identities = {str(record["ticket"]) for record in records}
    checks = state["required_checks"]
    if not isinstance(checks, dict) or set(checks) != identities:
        raise BatchPlanError(
            "required_checks must map every ticket identity exactly once",
            error_code="batch_state_corrupt",
        )
    for ticket, values in checks.items():
        _string_list(values, "required_checks", str(ticket))
    if "frontier" in state and (
        not isinstance(state["frontier"], list)
        or any(not isinstance(ticket, str) or ticket not in identities for ticket in state["frontier"])
    ):
        raise BatchPlanError(
            "persisted frontier contains an unknown or invalid ticket",
            error_code="batch_state_corrupt",
        )
    ticket_states = state.get("ticket_states")
    if ticket_states is not None:
        if not isinstance(ticket_states, dict):
            raise BatchPlanError(
                "ticket_states must be a JSON object",
                error_code="batch_state_corrupt",
            )
        unknown_states = sorted(str(ticket) for ticket in ticket_states if ticket not in identities)
        if unknown_states:
            raise BatchPlanError(
                "ticket_states contains an unknown ticket",
                error_code="batch_state_corrupt",
                details={"unknown_tickets": unknown_states},
            )
        for ticket, ticket_state in ticket_states.items():
            if not isinstance(ticket_state, dict):
                raise BatchPlanError(
                    f"ticket state for {ticket!r} must be a JSON object",
                    error_code="batch_state_corrupt",
                )
            ticket_state_identity = ticket_state.get("ticket")
            if ticket_state_identity is not None and ticket_state_identity != ticket:
                raise BatchPlanError(
                    f"ticket state identity mismatch for {ticket!r}",
                    error_code="batch_state_corrupt",
                )
            if ticket_state.get("batch_id") != state["batch_id"]:
                raise BatchPlanError(
                    f"ticket state batch identity mismatch for {ticket!r}",
                    error_code="batch_state_corrupt",
                    details={
                        "ticket": ticket,
                        "batch_id": ticket_state.get("batch_id"),
                        "expected_batch_id": state["batch_id"],
                    },
                )
            record = next(
                record for record in records if record["ticket"] == ticket
            )
            record_status = str(record["status"])
            ticket_status = ticket_state.get("status")
            if record_status in RUNNABLE_STATUSES:
                raise BatchPlanError(
                    f"ticket state exists for runnable ticket {ticket!r}",
                    error_code="batch_state_corrupt",
                    details={"ticket": ticket, "status": record_status},
                )
            if ticket_status == "started" and record_status != "started":
                raise BatchPlanError(
                    f"ticket state status does not match batch ticket {ticket!r}",
                    error_code="batch_state_corrupt",
                    details={
                        "ticket": ticket,
                        "ticket_status": ticket_status,
                        "batch_status": record_status,
                    },
                )


def calculate_frontier(state: dict[str, object]) -> dict[str, object]:
    """Return the current runnable frontier without mutating the persisted plan."""

    validate_batch_plan(state)
    return _calculate_frontier(state)


def _calculate_frontier(state: dict[str, object]) -> dict[str, object]:
    """Calculate a frontier for a plan that has already passed validation."""

    tickets = [dict(record) for record in state["tickets"]]
    frontier: list[str] = []
    blocked: dict[str, dict[str, object]] = {}
    for record in tickets:
        ticket = str(record["ticket"])
        status = str(record["status"])
        dependencies = [str(item) for item in record["dependencies"]]
        if status not in RUNNABLE_STATUSES:
            blocked[ticket] = {
                "predecessors": [],
                "gates": [],
                "status": status,
                "reason": "ticket is already in progress or complete",
            }
            continue
        if dependencies:
            blocked[ticket] = {
                "predecessors": dependencies,
                "gates": [],
                "status": status,
                "reason": "ticket has declared predecessors",
            }
        else:
            frontier.append(ticket)
    return {
        "verified": True,
        "schema_version": state["schema_version"],
        "kind": state["kind"],
        "state_path": state.get("state_path"),
        "batch_id": state.get("batch_id"),
        "repo": state.get("repo"),
        "target_branch": state["target_branch"],
        "starting_sha": state["starting_sha"],
        "tickets": tickets,
        "dependencies": {
            str(record["ticket"]): list(record["dependencies"]) for record in tickets
        },
        "required_checks": state.get("required_checks", {}),
        "frontier": frontier,
        "runnable": frontier,
        "blocked": blocked,
    }


def _read_tickets_argument(value: str) -> object:
    candidate = Path(value).expanduser()
    if candidate.exists():
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BatchPlanError(
                f"tickets file is not valid JSON: {candidate}",
                details={"reason": str(error)},
            ) from error
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise BatchPlanError(
            "tickets must be inline JSON or a path to a JSON file",
            details={"reason": str(error)},
        ) from error


def create_batch_plan(
    repo_arg: str,
    state_arg: str,
    *,
    target_branch: str | None = None,
    starting_sha: str | None = None,
    tickets: object,
) -> dict[str, object]:
    main_repo = resolve_repo(repo_arg)
    require_clean(main_repo)
    branch = target_branch or run_git(main_repo, "branch", "--show-current")
    if not branch:
        raise BatchPlanError("detached HEAD is not supported for a batch plan")
    if (
        run_git_process(main_repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode
        != 0
    ):
        raise BatchPlanError(
            f"target branch does not exist: {branch}",
            error_code="batch_target_invalid",
        )
    target_head = run_git(main_repo, "rev-parse", f"refs/heads/{branch}")
    requested_start = starting_sha or target_head
    try:
        resolved_start = run_git(main_repo, "rev-parse", "--verify", f"{requested_start}^{{commit}}")
    except RuntimeError as error:
        raise BatchPlanError(
            f"starting SHA is not a commit: {requested_start}",
            error_code="batch_start_invalid",
        ) from error
    if resolved_start != target_head:
        raise BatchPlanError(
            "starting SHA must equal the target branch HEAD",
            error_code="batch_target_stale",
            details={"target_head": target_head, "starting_sha": resolved_start},
        )

    records = normalize_ticket_records(tickets)
    _validate_dependency_graph(records)
    _require_batch_size(records)
    _validate_worktree_slugs(records)
    state_path = Path(state_arg).expanduser().resolve()
    required_checks = {
        str(record["ticket"]): list(record["required_checks"]) for record in records
    }
    identity = json.dumps(
        {"repo": str(main_repo), "branch": branch, "sha": resolved_start, "tickets": records},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    plan: dict[str, object] = {
        "schema_version": BATCH_PLAN_SCHEMA_VERSION,
        "kind": BATCH_PLAN_KIND,
        "batch_id": hashlib.sha256(identity).hexdigest()[:24],
        "repo": str(main_repo),
        "state_path": str(state_path),
        "target_branch": branch,
        "starting_sha": resolved_start,
        "frontier_sha": resolved_start,
        "tickets": records,
        "dependencies": {
            str(record["ticket"]): list(record["dependencies"]) for record in records
        },
        "required_checks": required_checks,
        "frontier_generation": 0,
    }
    initial_frontier = _calculate_frontier(plan)
    plan["frontier"] = list(initial_frontier["frontier"])
    plan["runnable"] = list(initial_frontier["runnable"])
    with batch_state_lock(state_path):
        if state_path.exists():
            raise BatchPlanError(
                f"batch state already exists: {state_path}", error_code="batch_state_exists"
            )
        _atomic_write_json(state_path, plan)
    return initial_frontier


def query_batch_frontier(state_arg: str) -> dict[str, object]:
    state_path, state = load_batch_state(state_arg)
    state["state_path"] = str(state_path)
    return calculate_frontier(state)


def _find_batch_ticket(state: dict[str, object], ticket: str) -> BatchTicketRecord | None:
    records = state.get("tickets")
    if not isinstance(records, list):
        return None
    for record in records:
        if isinstance(record, dict) and record.get("ticket") == ticket:
            return cast(BatchTicketRecord, record)
    return None


def _start_failure_details(
    state: dict[str, object],
    ticket: str,
    *,
    target_head: str | None = None,
    status: str | None = None,
    predecessors: list[str] | None = None,
    gates: list[str] | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    record = _find_batch_ticket(state, ticket)
    return {
        "ticket": ticket,
        "status": status if status is not None else str(record["status"]) if record else "unknown",
        "unmet_predecessors": predecessors
        if predecessors is not None
        else list(record["dependencies"]) if record else [],
        "gates": gates or [],
        "target_head": target_head,
        "target_branch": state.get("target_branch"),
        "reason": reason or "ticket is not in the current runnable frontier",
    }


def _target_failure_details(
    state: dict[str, object],
    ticket: str | None,
    *,
    target_head: str | None,
    **extra: object,
) -> dict[str, object]:
    details = (
        _start_failure_details(state, ticket, target_head=target_head)
        if ticket is not None
        else {}
    )
    details.update(extra)
    return details


def _require_start_target(
    repo: Path, state: dict[str, object], *, ticket: str | None = None
) -> tuple[str, str]:
    expected_repo = Path(str(state["repo"])).expanduser().resolve()
    if repo != expected_repo:
        raise BatchPlanError(
            "start repository does not match the batch plan",
            error_code="target_repository_mismatch",
            details=_target_failure_details(
                state,
                ticket,
                target_head=None,
                expected_repo=str(expected_repo),
                actual_repo=str(repo),
            ),
        )

    expected_branch = str(state["target_branch"])
    actual_branch = run_git(repo, "branch", "--show-current")
    target_head = run_git(repo, "rev-parse", "HEAD")
    if actual_branch != expected_branch:
        raise BatchPlanError(
            "target branch does not match the batch plan",
            error_code="target_branch_mismatch",
            details=_target_failure_details(
                state,
                ticket,
                target_head=target_head,
                expected_branch=expected_branch,
                actual_branch=actual_branch,
            ),
        )

    status = run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise BatchPlanError(
            "target worktree is not clean",
            error_code="target_worktree_dirty",
            details=_target_failure_details(
                state,
                ticket,
                target_head=target_head,
                target_branch=actual_branch,
            ),
        )

    expected_head = str(state.get("frontier_sha", state["starting_sha"]))
    if target_head != expected_head:
        raise BatchPlanError(
            "target HEAD changed after the batch frontier was frozen",
            error_code="target_head_stale",
            details=_target_failure_details(
                state,
                ticket,
                target_head=target_head,
                expected_head=expected_head,
                target_branch=actual_branch,
            ),
        )
    return actual_branch, target_head


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
    batch_state: str | Path | None = None,
    worktree_root: str | Path | None = None,
    branch_name: str | None = None,
) -> dict[str, object]:
    if batch_state is None or not str(batch_state).strip():
        raise BatchPlanError(
            "multi-ticket start requires a validated batch state; create a batch plan first",
            error_code="batch_state_missing",
            details={
                "guidance": (
                    "Run `ticket_boundary.py plan create` with the complete approved ticket graph, "
                    "or follow migration guidance to move existing lifecycle records into a batch plan, "
                    "then retry start with --batch-state <path>."
                )
            },
        )

    batch_state_path = Path(batch_state).expanduser().resolve()
    if not batch_state_path.exists():
        raise BatchPlanError(
            f"batch state does not exist: {batch_state_path}",
            error_code="batch_state_missing",
            details={
                "state_path": str(batch_state_path),
                "guidance": (
                    "Create a validated batch plan (or follow migration guidance for an existing lifecycle state) "
                    "before starting any worker; "
                    "do not resume a multi-ticket start without batch state."
                ),
            },
        )

    with batch_state_lock(batch_state_path):
        state = _load_batch_state_unlocked(batch_state_path)
        state["state_path"] = str(batch_state_path)
        main_repo = resolve_repo(repo_arg)
        base_branch, start_sha = _require_start_target(main_repo, state, ticket=ticket)

        record = _find_batch_ticket(state, ticket)
        frontier = _calculate_frontier(state)
        runnable = [str(item) for item in frontier["frontier"]]
        if record is None:
            raise BatchPlanError(
                f"ticket is not present in the batch plan: {ticket}",
                error_code="ticket_unknown",
                details=_start_failure_details(
                    state,
                    ticket,
                    target_head=start_sha,
                    predecessors=[],
                    reason="ticket is not declared in the validated batch plan",
                ),
            )
        if ticket not in runnable:
            reason = str(
                frontier["blocked"].get(ticket, {}).get("reason", "ticket is not runnable")
            )
            raise BatchPlanError(
                f"ticket {ticket!r} is not in the current runnable frontier",
                error_code="ticket_not_runnable",
                details=_start_failure_details(
                    state,
                    ticket,
                    target_head=start_sha,
                    status=str(record["status"]),
                    predecessors=list(record["dependencies"])
                    if str(record["status"]) in RUNNABLE_STATUSES
                    else [],
                    reason=reason,
                ),
            )

        token = uuid.uuid4().hex[:12]
        slug = ticket_slug(ticket)
        worker_branch = branch_name or f"codex/matt-ticket/{slug}-{token}"
        try:
            run_git(main_repo, "check-ref-format", "--branch", worker_branch)
        except RuntimeError as error:
            raise BatchPlanError(
                f"worker branch is invalid: {worker_branch}",
                error_code="worker_branch_invalid",
                details={"branch": worker_branch},
            ) from error
        if (
            run_git_process(
                main_repo, "show-ref", "--verify", "--quiet", f"refs/heads/{worker_branch}"
            ).returncode
            == 0
        ):
            raise BatchPlanError(
                f"worker branch already exists: {worker_branch}",
                error_code="worker_branch_exists",
                details={"branch": worker_branch, "ticket": ticket},
            )

        if worktree_root is None:
            worktree_root_path = Path(tempfile.gettempdir()) / "matt-implement-adapter" / "worktrees"
        else:
            worktree_root_path = Path(worktree_root).expanduser().resolve()
        worktree_path = worktree_root_path / slug
        if worktree_path.exists():
            raise BatchPlanError(
                f"worker worktree already exists: {worktree_path}",
                error_code="worker_worktree_exists",
                details={"worktree": str(worktree_path), "ticket": ticket},
            )

        state_dir = Path(tempfile.gettempdir()) / "matt-implement-adapter" / "states"
        state_dir.mkdir(parents=True, exist_ok=True)
        ticket_state_path = state_dir / f"{uuid.uuid4().hex}.json"
        frontier_generation = int(state.get("frontier_generation", 0))
        ticket_state: dict[str, object] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "repo": str(main_repo),
            "worktree": str(worktree_path),
            "ticket": ticket,
            "base_branch": base_branch,
            "branch": worker_branch,
            "start_sha": start_sha,
            "verified_start_sha": start_sha,
            "target_head": start_sha,
            "target_branch": base_branch,
            "state_path": str(ticket_state_path),
            "status": "started",
            "verified": True,
            "batch_state": str(batch_state_path),
            "batch_state_path": str(batch_state_path),
            "batch_id": state.get("batch_id"),
            "frontier_generation": frontier_generation,
            "predecessor_evidence": {},
            "predecessor_integration_evidence": {},
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
            _atomic_write_json(ticket_state_path, ticket_state)

            record["status"] = "started"
            ticket_states = state.setdefault("ticket_states", {})
            if not isinstance(ticket_states, dict):
                raise BatchPlanError(
                    "ticket_states must be a JSON object",
                    error_code="batch_state_corrupt",
                )
            ticket_states[ticket] = ticket_state
            next_frontier = _calculate_frontier(state)
            state["frontier"] = list(next_frontier["frontier"])
            state["runnable"] = list(next_frontier["runnable"])
            _atomic_write_json(batch_state_path, state)
        except (OSError, RuntimeError, BatchPlanError):
            if worktree_path.exists():
                run_git(main_repo, "worktree", "remove", "--force", str(worktree_path), check=False)
            run_git(main_repo, "branch", "-D", worker_branch, check=False)
            ticket_state_path.unlink(missing_ok=True)
            raise

        return ticket_state


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
        description=(
            "Create and query a validated batch plan, or create, verify, integrate, "
            "and clean up one ticket's Git worktree."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser(
        "plan", aliases=("batch-plan",), help="create or query a persisted batch plan"
    )
    plan_subparsers = plan_parser.add_subparsers(dest="plan_command", required=True)

    plan_create_parser = plan_subparsers.add_parser(
        "create", help="validate and persist a batch plan before any worker starts"
    )
    plan_create_parser.add_argument("--repo", required=True)
    plan_create_parser.add_argument("--state", required=True)
    plan_create_parser.add_argument("--target-branch", "--target", dest="target_branch")
    plan_create_parser.add_argument("--starting-sha", "--start-sha", dest="starting_sha")
    plan_create_parser.add_argument(
        "--tickets-json", "--tickets-file", "--tickets", dest="tickets", required=True
    )

    plan_frontier_parser = plan_subparsers.add_parser(
        "frontier", aliases=("query", "show", "load"), help="query the current runnable frontier"
    )
    plan_frontier_parser.add_argument("--state", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--repo", required=True)
    start_parser.add_argument("--ticket", required=True)
    start_parser.add_argument(
        "--batch-state",
        help="path to the validated batch plan state (required for every multi-ticket start)",
    )
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
        if args.command in {"plan", "batch-plan"}:
            if args.plan_command == "create":
                result = create_batch_plan(
                    args.repo,
                    args.state,
                    target_branch=args.target_branch,
                    starting_sha=args.starting_sha,
                    tickets=_read_tickets_argument(args.tickets),
                )
            else:
                result = query_batch_frontier(args.state)
        elif args.command == "start":
            result = start_boundary(
                args.repo,
                args.ticket,
                batch_state=args.batch_state,
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
    except BatchPlanError as error:
        print(
            json.dumps(
                {
                    "verified": False,
                    "error": str(error),
                    "error_code": error.error_code,
                    "details": error.details,
                }
            ),
            file=sys.stderr,
        )
        return 1
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(json.dumps({"verified": False, "error": str(error)}), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
