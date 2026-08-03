#!/usr/bin/env python3
"""Create, verify, integrate, and clean up one ticket's Git worktree."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
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
SATISFIED_PREDECESSOR_STATUSES = frozenset({"integrated", "cleaned"})
VERIFICATION_RESULTS = frozenset({"passed", "failed"})
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


class IntegrationEvidence(TypedDict, total=False):
    target_branch: str
    strategy: str
    commit: str
    integrated_commit: str
    integration_sha: str


class VerificationEvidence(TypedDict, total=False):
    result: str
    status: str
    required_checks: list[str]
    checks: dict[str, object]
    target_branch: str
    target_head: str


class BatchTicketLifecycle(TypedDict, total=False):
    integration: IntegrationEvidence
    verification: VerificationEvidence
    integration_error: str
    integration_target_branch: str
    integration_strategy: str
    integrated_commit: str


class BatchTicketRecord(BatchTicketLifecycle):
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
        # Keep lifecycle evidence on the ticket record when reloading a plan. The
        # original batch-plan fields remain normalized, while integration and
        # verification metadata is intentionally persisted for frontier checks.
        if isinstance(raw, dict):
            if "integration" in raw:
                item["integration"] = cast(IntegrationEvidence, raw["integration"])
            if "verification" in raw:
                item["verification"] = cast(VerificationEvidence, raw["verification"])
            if "integration_error" in raw:
                item["integration_error"] = cast(str, raw["integration_error"])
            if "integration_target_branch" in raw:
                item["integration_target_branch"] = cast(
                    str, raw["integration_target_branch"]
                )
            if "integration_strategy" in raw:
                item["integration_strategy"] = cast(str, raw["integration_strategy"])
            if "integrated_commit" in raw:
                item["integrated_commit"] = cast(str, raw["integrated_commit"])
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


def _validate_evidence_maps(
    state: dict[str, object], identities: set[str]
) -> None:
    """Validate optional persisted integration/verification evidence maps."""

    for field in ("integrations", "verifications"):
        value = state.get(field)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise BatchPlanError(
                f"{field} must be a JSON object",
                error_code="batch_state_corrupt",
            )
        unknown = sorted(str(ticket) for ticket in value if ticket not in identities)
        if unknown:
            raise BatchPlanError(
                f"{field} contains an unknown ticket",
                error_code="batch_state_corrupt",
                details={"unknown_tickets": unknown},
            )
        for ticket, evidence in value.items():
            if not isinstance(evidence, dict):
                raise BatchPlanError(
                    f"{field} for ticket {ticket!r} must be a JSON object",
                    error_code="batch_state_corrupt",
                )


def _validate_legacy_imports(state: dict[str, object], identities: set[str]) -> None:
    value = state.get("legacy_imports")
    if value is None:
        return
    if not isinstance(value, dict):
        raise BatchPlanError("legacy_imports must be a JSON object", error_code="batch_state_corrupt")
    unknown = sorted(str(ticket) for ticket in value if ticket not in identities)
    if unknown:
        raise BatchPlanError(
            "legacy_imports contains an unknown ticket",
            error_code="batch_state_corrupt",
            details={"unknown_tickets": unknown},
        )
    for ticket, evidence in value.items():
        if not isinstance(evidence, dict):
            raise BatchPlanError(
                f"legacy import evidence for ticket {ticket!r} must be a JSON object",
                error_code="batch_state_corrupt",
            )
        for field in (
            "state_path",
            "schema_version",
            "status",
            "repo",
            "target_branch",
            "ticket",
            "worker_branch",
            "start_sha",
        ):
            if field not in evidence:
                raise BatchPlanError(
                    f"legacy import evidence for ticket {ticket!r} is incomplete",
                    error_code="batch_state_corrupt",
                    details={"ticket": ticket, "missing_field": field},
                )
        if evidence.get("ticket") != ticket or evidence.get("schema_version") != STATE_SCHEMA_VERSION:
            raise BatchPlanError(
                f"legacy import evidence identity/schema is invalid for ticket {ticket!r}",
                error_code="batch_state_corrupt",
                details={"ticket": ticket},
            )
        for field in ("state_path", "repo", "target_branch", "ticket", "worker_branch", "start_sha"):
            if not isinstance(evidence.get(field), str) or not str(evidence[field]).strip():
                raise BatchPlanError(
                    f"legacy import evidence field {field!r} is invalid for ticket {ticket!r}",
                    error_code="batch_state_corrupt",
                    details={"ticket": ticket, "field": field},
                )
        if evidence.get("status") not in TICKET_STATUSES:
            raise BatchPlanError(
                f"legacy import evidence status is unsupported for ticket {ticket!r}",
                error_code="batch_state_corrupt",
                details={"ticket": ticket, "status": evidence.get("status")},
            )
        status = str(evidence["status"])
        if status in {"finished", "integrated", "cleaned"} and (
            not isinstance(evidence.get("final_sha"), str)
            or not str(evidence.get("final_sha", "")).strip()
        ):
            raise BatchPlanError(
                f"legacy import evidence is missing final commit for ticket {ticket!r}",
                error_code="batch_state_corrupt",
                details={"ticket": ticket},
            )
        if status in {"integrated", "cleaned"} and not isinstance(
            evidence.get("integration"), dict
        ):
            raise BatchPlanError(
                f"legacy import evidence is missing integration for ticket {ticket!r}",
                error_code="batch_state_corrupt",
                details={"ticket": ticket},
            )


def _validate_mirrored_evidence(
    state: dict[str, object], records: list[BatchTicketRecord]
) -> None:
    """Reject divergent lifecycle evidence copied to record and top-level maps."""

    for record in records:
        ticket = str(record["ticket"])
        for record_field, map_field, record_evidence in (
            ("integration", "integrations", record.get("integration")),
            ("verification", "verifications", record.get("verification")),
        ):
            values = state.get(map_field)
            map_evidence = (
                values.get(ticket)
                if isinstance(values, dict) and ticket in values
                else None
            )
            if record_evidence is not None and map_evidence is not None and record_evidence != map_evidence:
                raise BatchPlanError(
                    f"{record_field} evidence differs between ticket record and {map_field} map",
                    error_code="batch_state_corrupt",
                    details={"ticket": ticket, "record_field": record_field, "map_field": map_field},
                )
            ticket_states = state.get("ticket_states")
            ticket_state = (
                ticket_states.get(ticket)
                if isinstance(ticket_states, dict) and ticket in ticket_states
                else None
            )
            ticket_state_evidence = (
                ticket_state.get(record_field)
                if isinstance(ticket_state, dict)
                else None
            )
            canonical_evidence = record_evidence if record_evidence is not None else map_evidence
            if (
                ticket_state_evidence is not None
                and canonical_evidence is not None
                and ticket_state_evidence != canonical_evidence
            ):
                raise BatchPlanError(
                    f"{record_field} evidence differs in ticket state for {ticket!r}",
                    error_code="batch_state_corrupt",
                    details={"ticket": ticket, "field": record_field},
                )


def _integration_commit(evidence: object) -> str | None:
    if not isinstance(evidence, dict):
        return None
    values: list[object] = [
        evidence[field]
        for field in ("commit", "integrated_commit", "integration_sha")
        if field in evidence
    ]
    if not values or any(not isinstance(value, str) or not value for value in values):
        return None
    if len(set(values)) != 1:
        return None
    return str(values[0])


def _is_passed_check(value: object) -> bool:
    return value is True or (
        isinstance(value, str)
        and value.strip().lower()
        in {"passed", "pass", "success", "succeeded", "ok", "true"}
    )


def _validate_ticket_evidence(
    record: BatchTicketRecord,
    required_checks: list[str] | None = None,
) -> None:
    integration = record.get("integration")
    if integration is not None:
        if not isinstance(integration, dict):
            raise BatchPlanError(
                f"integration evidence for ticket {record['ticket']!r} must be a JSON object",
                error_code="batch_state_corrupt",
            )
        integration_aliases = [
            integration[field]
            for field in ("commit", "integrated_commit", "integration_sha")
            if field in integration
        ]
        if (
            len(integration_aliases) > 1
            and all(isinstance(value, str) for value in integration_aliases)
            and len(set(integration_aliases)) != 1
        ):
            raise BatchPlanError(
                f"integration evidence aliases differ; evidence differs for ticket {record['ticket']!r}",
                error_code="batch_state_corrupt",
                details={"ticket": record["ticket"]},
            )
        commit = _integration_commit(integration)
        for field, value in (
            ("target_branch", integration.get("target_branch")),
            ("strategy", integration.get("strategy")),
            ("commit", commit),
        ):
            if not isinstance(value, str) or not value:
                raise BatchPlanError(
                    f"integration evidence for ticket {record['ticket']!r} is incomplete",
                    error_code="batch_state_corrupt",
                    details={"ticket": record["ticket"], "missing_field": field},
                )
        if integration.get("strategy") not in {"merge", "cherry-pick"}:
            raise BatchPlanError(
                f"integration strategy for ticket {record['ticket']!r} is unsupported",
                error_code="batch_state_corrupt",
                details={"ticket": record["ticket"], "strategy": integration.get("strategy")},
            )
    verification = record.get("verification")
    if verification is not None:
        if not isinstance(verification, dict):
            raise BatchPlanError(
                f"verification evidence for ticket {record['ticket']!r} must be a JSON object",
                error_code="batch_state_corrupt",
            )
        result_value = verification.get("result")
        status_value = verification.get("status")
        if result_value is not None and status_value is not None and result_value != status_value:
            raise BatchPlanError(
                f"verification result/status disagree for ticket {record['ticket']!r}",
                error_code="batch_state_corrupt",
                details={"ticket": record["ticket"]},
            )
        result = result_value if result_value is not None else status_value
        if result not in VERIFICATION_RESULTS:
            raise BatchPlanError(
                f"verification result for ticket {record['ticket']!r} is unsupported",
                error_code="batch_state_corrupt",
                details={"ticket": record["ticket"], "result": result},
            )
        if required_checks is not None:
            evidence_checks = verification.get("required_checks")
            if evidence_checks != required_checks:
                raise BatchPlanError(
                    f"verification required-check declaration for ticket {record['ticket']!r} is invalid",
                    error_code="batch_state_corrupt",
                    details={
                        "ticket": record["ticket"],
                        "required_checks": required_checks,
                        "evidence_checks": evidence_checks,
                    },
                )
            check_results = verification.get("checks")
            if not isinstance(check_results, dict):
                raise BatchPlanError(
                    f"verification checks for ticket {record['ticket']!r} must be a JSON object",
                    error_code="batch_state_corrupt",
                )
            unknown_checks = sorted(
                str(check) for check in check_results if check not in required_checks
            )
            if unknown_checks:
                raise BatchPlanError(
                    f"verification checks for ticket {record['ticket']!r} contain unknown checks",
                    error_code="batch_state_corrupt",
                    details={"ticket": record["ticket"], "unknown_checks": unknown_checks},
                )
            if result == "passed":
                missing_checks = [
                    check for check in required_checks if check not in check_results
                ]
                if missing_checks:
                    raise BatchPlanError(
                        f"passed verification for ticket {record['ticket']!r} is missing required checks",
                        error_code="batch_state_corrupt",
                        details={"ticket": record["ticket"], "missing_checks": missing_checks},
                    )
                failed_checks = [
                    check
                    for check in required_checks
                    if not _is_passed_check(check_results[check])
                ]
                if failed_checks:
                    raise BatchPlanError(
                        f"passed verification for ticket {record['ticket']!r} contains failed checks",
                        error_code="batch_state_corrupt",
                        details={"ticket": record["ticket"], "failed_checks": failed_checks},
                    )
            for field in ("target_branch", "target_head"):
                if not isinstance(verification.get(field), str) or not verification[field]:
                    raise BatchPlanError(
                        f"verification evidence for ticket {record['ticket']!r} is incomplete",
                        error_code="batch_state_corrupt",
                        details={"ticket": record["ticket"], "missing_field": field},
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
    _validate_evidence_maps(state, identities)
    _validate_legacy_imports(state, identities)
    for record in records:
        _validate_ticket_evidence(record, list(checks[str(record["ticket"])]))
    verifications = state.get("verifications")
    if isinstance(verifications, dict):
        for ticket, evidence in verifications.items():
            synthetic: BatchTicketRecord = {
                "ticket": str(ticket),
                "dependencies": [],
                "direct_dependencies": [],
                "required_checks": list(checks[str(ticket)]),
                "status": "integrated",
                "verification": cast(VerificationEvidence, evidence),
            }
            _validate_ticket_evidence(synthetic, list(checks[str(ticket)]))
    integrations = state.get("integrations")
    if isinstance(integrations, dict):
        for ticket, evidence in integrations.items():
            synthetic: BatchTicketRecord = {
                "ticket": str(ticket),
                "dependencies": [],
                "direct_dependencies": [],
                "required_checks": list(checks[str(ticket)]),
                "status": "integrated",
                "integration": cast(IntegrationEvidence, evidence),
            }
            _validate_ticket_evidence(synthetic)
    _validate_mirrored_evidence(state, records)
    if "frontier" in state and (
        not isinstance(state["frontier"], list)
        or any(not isinstance(ticket, str) or ticket not in identities for ticket in state["frontier"])
    ):
        raise BatchPlanError(
            "persisted frontier contains an unknown or invalid ticket",
            error_code="batch_state_corrupt",
        )
    if "frontier_tickets" in state and (
        not isinstance(state["frontier_tickets"], list)
        or any(
            not isinstance(ticket, str) or ticket not in identities
            for ticket in state["frontier_tickets"]
        )
    ):
        raise BatchPlanError(
            "persisted frontier_tickets contains an unknown or invalid ticket",
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
            if ticket_status in {"finished", "integrated", "cleaned"} and ticket_status != record_status:
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
            unmet_predecessors: list[str] = []
            gates: list[str] = []
            repository = Path(str(state.get("repo", ""))).expanduser().resolve()
            target_head = str(state.get("frontier_sha", state.get("starting_sha")))
            for dependency in dependencies:
                gate, _ = _evaluate_predecessor(
                    repository,
                    state,
                    dependency,
                    target_head,
                )
                if gate == "predecessor":
                    unmet_predecessors.append(dependency)
                elif gate is not None:
                    gates.append(f"{dependency}:{gate}")

            if not unmet_predecessors and not gates:
                frontier.append(ticket)
            else:
                blocked[ticket] = {
                    "predecessors": unmet_predecessors,
                    "gates": gates,
                    "status": status,
                    "reason": "ticket predecessors or integration verification are not satisfied",
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
        "frontier_sha": state.get("frontier_sha", state["starting_sha"]),
        "tickets": tickets,
        "dependencies": {
            str(record["ticket"]): list(record["dependencies"]) for record in tickets
        },
        "required_checks": state.get("required_checks", {}),
        "frontier": frontier,
        "runnable": frontier,
        "blocked": blocked,
    }


def _frontier_generation_tickets(state: dict[str, object]) -> list[str]:
    """Return the tickets captured when the current frozen frontier opened."""

    value = state.get("frontier_tickets")
    if isinstance(value, list):
        return [str(ticket) for ticket in value]
    # Plans written before the frozen-frontier snapshot was introduced can be
    # read safely by treating their persisted projection as the first snapshot.
    frontier = state.get("frontier")
    if isinstance(frontier, list):
        return [str(ticket) for ticket in frontier]
    return []


def _frontier_generation_is_complete(
    state: dict[str, object], tickets: list[str]
) -> bool:
    if not tickets:
        return False
    records = {
        str(record["ticket"]): record
        for record in state.get("tickets", [])
        if isinstance(record, dict)
    }
    return all(
        str(records[ticket]["status"]) in SATISFIED_PREDECESSOR_STATUSES
        and (
            (verification := _ticket_verification(records[ticket], state)) is not None
            and verification.get("result", verification.get("status")) == "passed"
        )
        for ticket in tickets
        if ticket in records
    ) and all(ticket in records for ticket in tickets)


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
        "frontier_tickets": [],
        "tickets": records,
        "dependencies": {
            str(record["ticket"]): list(record["dependencies"]) for record in records
        },
        "required_checks": required_checks,
        "integrations": {},
        "verifications": {},
        "frontier_generation": 0,
    }
    initial_frontier = _calculate_frontier(plan)
    plan["frontier"] = list(initial_frontier["frontier"])
    plan["runnable"] = list(initial_frontier["runnable"])
    plan["frontier_tickets"] = list(initial_frontier["frontier"])
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


def _dependency_order(state: dict[str, object]) -> list[str]:
    """Return ticket identities in deterministic dependency order."""

    records = [
        cast(BatchTicketRecord, record)
        for record in state.get("tickets", [])
        if isinstance(record, dict)
    ]
    identities = [str(record["ticket"]) for record in records]
    position = {ticket: index for index, ticket in enumerate(identities)}
    dependencies = {
        str(record["ticket"]): [str(item) for item in record["dependencies"]]
        for record in records
    }
    remaining = set(identities)
    ordered: list[str] = []
    while remaining:
        ready = [
            ticket
            for ticket in identities
            if ticket in remaining
            and all(dependency not in remaining for dependency in dependencies[ticket])
        ]
        if not ready:
            # ``validate_batch_plan`` already rejects cycles. Keep this helper
            # fail-closed if it is called with an unvalidated in-memory state.
            raise BatchPlanError(
                "dependency graph contains a cycle",
                details={"remaining": sorted(remaining)},
            )
        for ticket in sorted(ready, key=position.__getitem__):
            ordered.append(ticket)
            remaining.remove(ticket)
    return ordered


def _ticket_state_record(
    state: dict[str, object], ticket: str
) -> dict[str, object] | None:
    ticket_states = state.get("ticket_states")
    if not isinstance(ticket_states, dict):
        return None
    value = ticket_states.get(ticket)
    return value if isinstance(value, dict) else None


def _git_branch_exists(repo: Path, branch: str | None) -> bool | None:
    if not isinstance(branch, str) or not branch:
        return None
    return (
        run_git_process(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
        ).returncode
        == 0
    )


def _registered_worktree_paths(repo: Path) -> set[Path]:
    result = run_git_process(repo, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return set()
    paths: set[Path] = set()
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            paths.add(Path(line[len("worktree ") :]).expanduser().resolve())
    return paths


def _worktree_registered(repo: Path, worktree: str | None) -> bool | None:
    if not isinstance(worktree, str) or not worktree:
        return None
    return Path(worktree).expanduser().resolve() in _registered_worktree_paths(repo)


def _verification_result(
    record: BatchTicketRecord, state: dict[str, object]
) -> str | None:
    verification = _ticket_verification(record, state)
    if verification is None:
        return None
    value = verification.get("result", verification.get("status"))
    return str(value) if value is not None else None


def _audit_ticket(
    state: dict[str, object],
    frontier: dict[str, object],
    record: BatchTicketRecord,
    *,
    target_drift: dict[str, object] | None = None,
) -> dict[str, object]:
    """Project persisted lifecycle evidence and live Git artifacts for reports."""

    ticket = str(record["ticket"])
    lifecycle_status = str(record["status"])
    ticket_state = _ticket_state_record(state, ticket)
    integration = _ticket_integration(record, state)
    verification = _ticket_verification(record, state)
    worker_branch = (
        ticket_state.get("branch") if ticket_state is not None else None
    )
    worktree = ticket_state.get("worktree") if ticket_state is not None else None
    start_sha = (
        ticket_state.get("start_sha", ticket_state.get("verified_start_sha"))
        if ticket_state is not None
        else None
    )
    final_sha = ticket_state.get("final_sha") if ticket_state is not None else None
    integrated_commit = _integration_commit(integration)
    verification_result = _verification_result(record, state)

    branch_exists: bool | None = None
    worktree_registered: bool | None = None
    worktree_exists: bool | None = None
    orphan_reasons: list[str] = []
    repo_value = state.get("repo")
    repository = Path(str(repo_value)).expanduser().resolve() if repo_value else None
    if repository is not None and worker_branch is not None:
        branch_exists = _git_branch_exists(repository, str(worker_branch))
    if repository is not None and worktree is not None:
        worktree_registered = _worktree_registered(repository, str(worktree))
        worktree_exists = Path(str(worktree)).expanduser().resolve().exists()

    # A started/finished/integrating/integration-conflict worker owns both
    # artifacts until cleanup. Missing either side means the evidence can no
    # longer be resumed safely. A cleaned worker owns neither artifact.
    expected_active = lifecycle_status in {
        "started",
        "finished",
        "integrating",
        "integration_conflict",
        "failed",
        "integrated",
    }
    if expected_active:
        if not isinstance(worker_branch, str) or not worker_branch:
            orphan_reasons.append("worker_branch_missing")
        elif branch_exists is False:
            orphan_reasons.append("worker_branch_missing")
        if not isinstance(worktree, str) or not worktree:
            orphan_reasons.append("worktree_missing")
        elif worktree_registered is False or worktree_exists is False:
            orphan_reasons.append("worktree_missing")
    elif lifecycle_status == "cleaned":
        if branch_exists is True:
            orphan_reasons.append("worker_branch_present_after_cleanup")
        if worktree_registered is True or worktree_exists is True:
            orphan_reasons.append("worktree_present_after_cleanup")

    blocked = frontier.get("blocked", {})
    blocked_details = blocked.get(ticket, {}) if isinstance(blocked, dict) else {}
    if not isinstance(blocked_details, dict):
        blocked_details = {}
    unmet_predecessors = list(blocked_details.get("predecessors", []))
    verification_gates = list(blocked_details.get("gates", []))
    if lifecycle_status in {"failed", "integration_conflict"}:
        verification_gates.append(f"status:{lifecycle_status}")
    if lifecycle_status in {"integrated", "cleaned"}:
        if verification_result is None:
            verification_gates.append("verification:missing")
        elif verification_result != "passed":
            verification_gates.append(f"verification:{verification_result}")
    if target_drift is not None:
        verification_gates.append("target:stale")
    # Preserve order while removing duplicate gate labels produced by the
    # dependency projection and lifecycle checks.
    verification_gates = list(dict.fromkeys(str(gate) for gate in verification_gates))

    observed_status = "orphaned" if orphan_reasons else lifecycle_status
    return {
        "ticket": ticket,
        "dependencies": list(record["dependencies"]),
        "required_checks": list(record["required_checks"]),
        "status": observed_status,
        "persisted_status": lifecycle_status,
        "unmet_predecessors": [str(item) for item in unmet_predecessors],
        "verification_gates": verification_gates,
        "gates": verification_gates,
        "worker_branch": worker_branch,
        "start_sha": start_sha,
        "final_sha": final_sha,
        "integrated_commit": integrated_commit,
        "verification_result": verification_result,
        "verification": verification,
        "worktree": worktree,
        "branch_exists": branch_exists,
        "worktree_registered": worktree_registered,
        "worktree_exists": worktree_exists,
        "orphan_reasons": orphan_reasons,
        "target_drift": target_drift,
        "reason": blocked_details.get("reason"),
    }


def _current_target_head(state: dict[str, object]) -> str | None:
    repo_value = state.get("repo")
    target_branch = state.get("target_branch")
    if not isinstance(repo_value, str) or not isinstance(target_branch, str):
        return None
    try:
        return run_git(Path(repo_value).expanduser().resolve(), "rev-parse", target_branch)
    except (OSError, RuntimeError):
        return None


def _report_completion(
    entries: list[dict[str, object]],
    *,
    target_drift: dict[str, object] | None = None,
) -> tuple[bool, list[str]]:
    incomplete: list[str] = []
    for entry in entries:
        status = str(entry["status"])
        ticket = str(entry["ticket"])
        if status not in SATISFIED_PREDECESSOR_STATUSES:
            incomplete.append(ticket)
            continue
        if entry.get("verification_result") != "passed":
            incomplete.append(ticket)
            continue
        if entry.get("orphan_reasons"):
            incomplete.append(ticket)
    if target_drift is not None:
        incomplete.extend(
            str(entry["ticket"])
            for entry in entries
            if str(entry["ticket"]) not in incomplete
        )
    return not incomplete, incomplete


def _target_drift(
    state: dict[str, object], live_target_head: str | None
) -> dict[str, object] | None:
    expected = str(state.get("frontier_sha", state.get("starting_sha", "")))
    if live_target_head is None:
        return {
            "reason": "target branch HEAD could not be read",
            "expected_head": expected,
            "actual_head": None,
        }
    if live_target_head != expected:
        repo_value = state.get("repo")
        repository = Path(str(repo_value)).expanduser().resolve() if repo_value else None
        recorded_integration = _is_recorded_integration_head(state, live_target_head)
        try:
            expected_is_ancestor = (
                repository is not None
                and recorded_integration
                and is_ancestor(repository, expected, live_target_head)
            )
        except (OSError, RuntimeError):
            expected_is_ancestor = False
        if expected_is_ancestor:
            # Integrating one ticket advances the target branch while the
            # current frozen frontier still uses its original start SHA. This
            # is legal only when the new HEAD is a recorded integration commit
            # descended from that frozen base; unrelated target changes remain
            # stale and block the scheduler.
            return None
        return {
            "reason": "target branch HEAD differs from the persisted frontier SHA",
            "expected_head": expected,
            "actual_head": live_target_head,
        }
    return None


def _build_audit_report(
    state_arg: str,
    *,
    report_type: str,
    dependency_ordered: bool,
) -> dict[str, object]:
    state_path, state = load_batch_state(state_arg)
    state["state_path"] = str(state_path)
    frontier = calculate_frontier(state)
    live_target_head = _current_target_head(state)
    target_drift = _target_drift(state, live_target_head)
    if target_drift is not None:
        blocked = frontier.get("blocked", {})
        if not isinstance(blocked, dict):
            blocked = {}
        for record in state["tickets"]:
            if not isinstance(record, dict):
                continue
            if str(record["status"]) not in RUNNABLE_STATUSES:
                continue
            ticket = str(record["ticket"])
            details = dict(blocked.get(ticket, {}))
            gates = list(details.get("gates", []))
            gates.append("target:stale")
            details["gates"] = list(dict.fromkeys(str(gate) for gate in gates))
            details["status"] = str(record["status"])
            details["reason"] = target_drift["reason"]
            blocked[ticket] = details
        frontier["frontier"] = []
        frontier["runnable"] = []
        frontier["blocked"] = blocked
    if dependency_ordered:
        records_by_ticket = {
            str(record["ticket"]): cast(BatchTicketRecord, record)
            for record in state["tickets"]
        }
        records = [records_by_ticket[ticket] for ticket in _dependency_order(state)]
    else:
        records = [
            cast(BatchTicketRecord, record) for record in state["tickets"]
        ]
    entries = [
        _audit_ticket(state, frontier, record, target_drift=target_drift)
        for record in records
    ]
    complete, incomplete = _report_completion(entries, target_drift=target_drift)
    tickets_by_id = {str(entry["ticket"]): entry for entry in entries}
    blocked_reasons = {
        str(entry["ticket"]): {
            "unmet_predecessors": list(entry["unmet_predecessors"]),
            "verification_gates": list(entry["verification_gates"]),
            "reason": entry.get("reason"),
        }
        for entry in entries
        if entry["unmet_predecessors"] or entry["verification_gates"] or entry["reason"]
    }
    return {
        "verified": True,
        "report_type": report_type,
        "state_path": str(state_path),
        "batch_id": state.get("batch_id"),
        "repo": state.get("repo"),
        "target_branch": state.get("target_branch"),
        "target_head": live_target_head,
        "target_drift": target_drift,
        "starting_sha": state.get("starting_sha"),
        "frontier_sha": frontier.get("frontier_sha"),
        "frontier": list(frontier.get("frontier", [])),
        "current_frontier": list(frontier.get("frontier", [])),
        "runnable": list(frontier.get("runnable", [])),
        "blocked": frontier.get("blocked", {}),
        "tickets": entries,
        "tickets_by_id": tickets_by_id,
        "ticket_statuses": {
            str(entry["ticket"]): entry["status"] for entry in entries
        },
        "dependency_order": [str(entry["ticket"]) for entry in entries],
        "blocked_reasons": blocked_reasons,
        "complete": complete,
        "completion_status": "complete" if complete else "incomplete",
        "incomplete_tickets": incomplete,
    }


def batch_status(state_arg: str) -> dict[str, object]:
    """Return an auditable live projection of the current batch scheduler state."""

    return _build_audit_report(
        state_arg,
        report_type="batch-status",
        dependency_ordered=False,
    )


def completion_report(state_arg: str) -> dict[str, object]:
    """Return dependency-ordered completion evidence without mutating state."""

    return _build_audit_report(
        state_arg,
        report_type="completion-report",
        dependency_ordered=True,
    )


def _find_batch_ticket(state: dict[str, object], ticket: str) -> BatchTicketRecord | None:
    records = state.get("tickets")
    if not isinstance(records, list):
        return None
    for record in records:
        if isinstance(record, dict) and record.get("ticket") == ticket:
            return cast(BatchTicketRecord, record)
    return None


def _ticket_batch_state_path(ticket_state: dict[str, object]) -> Path | None:
    value = ticket_state.get("batch_state_path", ticket_state.get("batch_state"))
    if value is None or not str(value).strip():
        return None
    return Path(str(value)).expanduser().resolve()


def _ticket_verification(
    record: BatchTicketRecord, state: dict[str, object]
) -> VerificationEvidence | None:
    evidence = record.get("verification")
    if isinstance(evidence, dict):
        return evidence
    values = state.get("verifications")
    if isinstance(values, dict):
        evidence = values.get(str(record["ticket"]))
        if isinstance(evidence, dict):
            return cast(VerificationEvidence, evidence)
    return None


def _ticket_integration(
    record: BatchTicketRecord, state: dict[str, object]
) -> IntegrationEvidence | None:
    evidence = record.get("integration")
    if isinstance(evidence, dict):
        return evidence
    legacy_commit = record.get("integrated_commit")
    if isinstance(legacy_commit, str) and legacy_commit:
        return {
            "target_branch": record.get("integration_target_branch", ""),
            "strategy": record.get("integration_strategy", ""),
            "commit": legacy_commit,
        }
    values = state.get("integrations")
    if isinstance(values, dict):
        evidence = values.get(str(record["ticket"]))
        if isinstance(evidence, dict):
            return cast(IntegrationEvidence, evidence)
    return None


def _evaluate_predecessor(
    repo: Path,
    state: dict[str, object],
    dependency: str,
    target_head: str,
) -> tuple[str | None, dict[str, object] | None]:
    """Return the first unmet predecessor gate and verified evidence, if any."""

    predecessor = _find_batch_ticket(state, dependency)
    if predecessor is None or str(predecessor["status"]) not in SATISFIED_PREDECESSOR_STATUSES:
        return "predecessor", None

    integration = _ticket_integration(predecessor, state)
    if integration is None:
        return "integration", None
    if integration.get("target_branch") != state.get("target_branch"):
        return "integration", None
    commit = _integration_commit(integration)
    if commit is None:
        return "integration", None
    try:
        if not is_ancestor(repo, commit, target_head):
            return "ancestry", None
    except (OSError, RuntimeError):
        return "ancestry", None

    verification = _ticket_verification(predecessor, state)
    verification_result = (
        verification.get("result", verification.get("status"))
        if verification is not None
        else None
    )
    if verification_result != "passed":
        return "verification", None
    if verification.get("target_branch") != state.get("target_branch"):
        return "verification", None
    verification_head = verification.get("target_head")
    if not isinstance(verification_head, str) or not verification_head:
        return "verification", None
    try:
        if not is_ancestor(repo, commit, verification_head):
            return "verification", None
        if not is_ancestor(repo, verification_head, target_head):
            return "verification", None
    except (OSError, RuntimeError):
        return "verification", None
    return None, {
        "integration_sha": commit,
        "target_branch": integration.get("target_branch"),
        "strategy": integration.get("strategy"),
        "verification_result": "passed",
        "verification_target_head": verification_head,
        "ancestor": True,
    }


def _persist_batch_ticket_state(
    ticket_state: dict[str, object],
    *,
    status: str,
    integration: IntegrationEvidence | None = None,
    verification: VerificationEvidence | None = None,
    integration_error: str | None = None,
) -> dict[str, object] | None:
    """Mirror a ticket lifecycle transition into its validated batch plan."""

    batch_path = _ticket_batch_state_path(ticket_state)
    if batch_path is None:
        return None

    ticket = str(ticket_state.get("ticket", ""))
    with batch_state_lock(batch_path):
        batch = _load_batch_state_unlocked(batch_path)
        batch["state_path"] = str(batch_path)
        record = _find_batch_ticket(batch, ticket)
        if record is None:
            raise BatchPlanError(
                f"ticket is not present in the batch plan: {ticket}",
                error_code="ticket_unknown",
            )

        record["status"] = cast(TicketStatus, status)
        if integration is not None:
            record["integration"] = integration
            record["integration_target_branch"] = integration.get("target_branch", "")
            record["integration_strategy"] = integration.get("strategy", "")
            record["integrated_commit"] = _integration_commit(integration) or ""
            integrations = batch.setdefault("integrations", {})
            if not isinstance(integrations, dict):
                raise BatchPlanError("integrations must be a JSON object", error_code="batch_state_corrupt")
            integrations[ticket] = integration
        if verification is not None:
            record["verification"] = verification
            verifications = batch.setdefault("verifications", {})
            if not isinstance(verifications, dict):
                raise BatchPlanError("verifications must be a JSON object", error_code="batch_state_corrupt")
            verifications[ticket] = verification
        if integration_error is not None:
            record["integration_error"] = integration_error
        elif "integration_error" in record:
            record.pop("integration_error", None)

        ticket_states = batch.setdefault("ticket_states", {})
        if not isinstance(ticket_states, dict):
            raise BatchPlanError("ticket_states must be a JSON object", error_code="batch_state_corrupt")
        mirrored = dict(ticket_state)
        mirrored["status"] = status
        if integration is not None:
            mirrored["integration"] = integration
        if verification is not None:
            mirrored["verification"] = verification
        if integration_error is not None:
            mirrored["integration_error"] = integration_error
        ticket_states[ticket] = mirrored

        next_frontier = _calculate_frontier(batch)
        batch["frontier"] = list(next_frontier["frontier"])
        batch["runnable"] = list(next_frontier["runnable"])
        _atomic_write_json(batch_path, batch)
    return batch


def _predecessor_evidence(
    repo: Path,
    state: dict[str, object],
    ticket: str,
    target_head: str,
) -> tuple[list[str], list[str], dict[str, object]]:
    record = _find_batch_ticket(state, ticket)
    if record is None:
        return [], [], {}
    unmet: list[str] = []
    gates: list[str] = []
    evidence: dict[str, object] = {}
    for dependency in record["dependencies"]:
        gate, predecessor_evidence = _evaluate_predecessor(
            repo, state, dependency, target_head
        )
        if gate == "predecessor":
            unmet.append(dependency)
        elif gate is not None:
            gates.append(f"{dependency}:{gate}")
        elif predecessor_evidence is not None:
            evidence[dependency] = predecessor_evidence
    return unmet, gates, evidence


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


def _is_recorded_integration_head(
    state: dict[str, object], target_head: str
) -> bool:
    """Allow a frozen frontier to coexist with a recorded integration commit."""

    records = state.get("tickets")
    if not isinstance(records, list):
        return False
    for raw_record in records:
        if not isinstance(raw_record, dict):
            continue
        if str(raw_record.get("status")) not in SATISFIED_PREDECESSOR_STATUSES:
            continue
        record = cast(BatchTicketRecord, raw_record)
        integration = _ticket_integration(record, state)
        if (
            isinstance(integration, dict)
            and integration.get("target_branch") == state.get("target_branch")
            and _integration_commit(integration) == target_head
        ):
            return True
    return False


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
        recorded_integration = _is_recorded_integration_head(state, target_head)
        try:
            expected_is_ancestor = is_ancestor(repo, expected_head, target_head)
        except (OSError, RuntimeError):
            expected_is_ancestor = False
        if not recorded_integration or not expected_is_ancestor:
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

    start_sha = expected_head

    if ticket is not None:
        unmet, gates, _ = _predecessor_evidence(repo, state, ticket, target_head)
        if gates and any(gate.endswith(":ancestry") for gate in gates):
            raise BatchPlanError(
                f"predecessor integration is not an ancestor of target HEAD for {ticket!r}",
                error_code="predecessor_not_ancestor",
                details=_start_failure_details(
                    state,
                    ticket,
                    target_head=start_sha,
                    predecessors=unmet,
                    gates=gates,
                    reason="target HEAD does not contain every predecessor integrated commit",
                ),
            )
    return actual_branch, start_sha


def load_state(state_arg: str) -> tuple[Path, dict[str, object]]:
    state_path = Path(state_arg).expanduser().resolve()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    return state_path, state


def _load_legacy_state(state_arg: str) -> tuple[Path, dict[str, object]]:
    """Load a pre-batch lifecycle state without mutating it."""

    state_path = Path(state_arg).expanduser().resolve()
    if not state_path.exists():
        raise BatchPlanError(
            f"legacy state does not exist: {state_path}",
            error_code="legacy_state_missing",
            details={"state_path": str(state_path)},
        )
    try:
        document = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BatchPlanError(
            f"legacy state is corrupted: {state_path}",
            error_code="legacy_state_corrupt",
            details={"state_path": str(state_path), "reason": str(error)},
        ) from error
    if not isinstance(document, dict):
        raise BatchPlanError(
            "legacy state root must be a JSON object",
            error_code="legacy_state_corrupt",
            details={"state_path": str(state_path)},
        )
    if document.get("schema_version") != STATE_SCHEMA_VERSION:
        raise BatchPlanError(
            "legacy state schema version is unsupported",
            error_code="legacy_state_unsupported_schema",
            details={"schema_version": document.get("schema_version")},
        )
    for field in ("repo", "ticket", "base_branch", "branch", "start_sha", "status"):
        if not isinstance(document.get(field), str) or not str(document[field]).strip():
            raise BatchPlanError(
                f"legacy state is missing required field: {field}",
                error_code="legacy_state_corrupt",
                details={"state_path": str(state_path), "field": field},
            )
    status = str(document["status"])
    if status not in {"started", "finished", "integrated"}:
        raise BatchPlanError(
            f"legacy state status cannot be imported: {status}",
            error_code="legacy_state_corrupt",
            details={"state_path": str(state_path), "status": status},
        )
    document["state_path"] = str(state_path)
    return state_path, cast(dict[str, object], document)


def _legacy_commit(repo: Path, value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BatchPlanError(
            f"legacy state is missing {field} commit evidence",
            error_code="legacy_evidence_invalid",
            details={"field": field},
        )
    try:
        return run_git(repo, "rev-parse", "--verify", f"{value}^{{commit}}")
    except RuntimeError as error:
        raise BatchPlanError(
            f"legacy {field} is not a valid commit: {value}",
            error_code="legacy_evidence_invalid",
            details={"field": field, "value": value},
        ) from error


def _commit_patch_id(repo: Path, commit: str) -> str | None:
    shown = subprocess.run(
        ["git", "-C", str(repo), "show", "--format=", "--no-ext-diff", commit],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if shown.returncode != 0:
        return None
    patch = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=shown.stdout,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if patch.returncode != 0 or not patch.stdout.strip():
        return None
    return patch.stdout.split()[0]


def _git_common_dir(path: Path) -> Path:
    value = Path(run_git(path, "rev-parse", "--git-common-dir"))
    return (value if value.is_absolute() else path / value).resolve()


def _legacy_worker_evidence(
    repo: Path, state: dict[str, object]
) -> tuple[str, str | None]:
    """Validate the legacy worker ref and final commit evidence."""

    branch = str(state["branch"])
    try:
        run_git(repo, "check-ref-format", "--branch", branch)
        worker_head = _legacy_commit(
            repo,
            run_git(repo, "rev-parse", f"refs/heads/{branch}"),
            "worker",
        )
    except BatchPlanError:
        raise
    except RuntimeError as error:
        raise BatchPlanError(
            f"legacy worker branch is invalid or missing: {branch}",
            error_code="legacy_evidence_invalid",
            details={"branch": branch},
        ) from error
    start_sha = _legacy_commit(repo, state.get("start_sha"), "start")
    status = str(state["status"])
    worktree_value = state.get("worktree")
    if not isinstance(worktree_value, str) or not worktree_value.strip():
        raise BatchPlanError(
            "legacy state is missing worker worktree evidence",
            error_code="legacy_evidence_invalid",
        )
    worktree = Path(worktree_value).expanduser().resolve()
    if not worktree.exists():
        raise BatchPlanError(
            f"legacy worker worktree does not exist: {worktree}",
            error_code="legacy_evidence_invalid",
            details={"worktree": str(worktree)},
        )
    try:
        if _git_common_dir(worktree) != _git_common_dir(repo) or run_git(worktree, "branch", "--show-current") != branch:
            raise BatchPlanError(
                "legacy worker worktree does not match its repository or branch",
                error_code="legacy_evidence_invalid",
                details={"worktree": str(worktree), "branch": branch},
            )
    except BatchPlanError:
        raise
    except RuntimeError as error:
        raise BatchPlanError(
            "legacy worker worktree is not a valid Git worktree",
            error_code="legacy_evidence_invalid",
            details={"worktree": str(worktree)},
        ) from error
    if status in {"finished", "integrated"}:
        try:
            require_clean(worktree)
        except RuntimeError as error:
            raise BatchPlanError(
                "legacy finished worker worktree is not clean",
                error_code="legacy_evidence_invalid",
                details={"worktree": str(worktree)},
            ) from error
        final_sha = _legacy_commit(repo, state.get("final_sha"), "final")
        if worker_head != final_sha:
            raise BatchPlanError(
                "legacy worker branch does not point at final commit",
                error_code="legacy_ancestry_mismatch",
                details={"worker_head": worker_head, "final_sha": final_sha},
            )
        if not is_ancestor(repo, start_sha, final_sha):
            raise BatchPlanError(
                "legacy start commit is not an ancestor of final commit",
                error_code="legacy_ancestry_mismatch",
                details={"start_sha": start_sha, "final_sha": final_sha},
            )
        commit_count = int(run_git(repo, "rev-list", "--count", f"{start_sha}..{final_sha}"))
        if commit_count != 1:
            raise BatchPlanError(
                "legacy worker does not contain exactly one final commit",
                error_code="legacy_evidence_invalid",
                details={"commit_count": commit_count},
            )
        changed_files = [
            line
            for line in run_git(repo, "diff", "--name-only", f"{start_sha}..{final_sha}").splitlines()
            if line
        ]
        if not changed_files:
            raise BatchPlanError(
                "legacy final commit contains no changed files",
                error_code="legacy_evidence_invalid",
            )
        return start_sha, final_sha
    if worker_head != start_sha:
        raise BatchPlanError(
            "legacy started worker branch moved beyond its start commit",
            error_code="legacy_ancestry_mismatch",
            details={"worker_head": worker_head, "start_sha": start_sha},
        )
    return start_sha, None


def _legacy_integration_evidence(
    repo: Path,
    state: dict[str, object],
    *,
    target_branch: str,
    start_sha: str,
    final_sha: str,
) -> tuple[IntegrationEvidence, str]:
    """Validate the old integration commit and target ancestry."""

    integration_value = state.get("integration")
    integration = integration_value if isinstance(integration_value, dict) else {}
    strategy = state.get("integration_strategy", integration.get("strategy"))
    integrated_branch = state.get("integrated_into", integration.get("target_branch"))
    commit_value = state.get(
        "integration_sha",
        state.get("integrated_commit", integration.get("commit")),
    )
    if integrated_branch != target_branch:
        raise BatchPlanError(
            "legacy integration target branch does not match the batch plan",
            error_code="legacy_target_mismatch",
            details={"expected_branch": target_branch, "actual_branch": integrated_branch},
        )
    if strategy not in {"merge", "cherry-pick"}:
        raise BatchPlanError(
            "legacy integration strategy is unsupported",
            error_code="legacy_evidence_invalid",
            details={"strategy": strategy},
        )
    integration_sha = _legacy_commit(repo, commit_value, "integration")
    integration_start = _legacy_commit(
        repo,
        state.get("integration_start_sha", start_sha),
        "integration start",
    )
    target_head = _legacy_commit(
        repo,
        run_git(repo, "rev-parse", f"refs/heads/{target_branch}"),
        "target",
    )
    if not is_ancestor(repo, integration_start, integration_sha):
        raise BatchPlanError(
            "legacy integration commit is not based on its recorded target start",
            error_code="legacy_ancestry_mismatch",
            details={"integration_start_sha": integration_start, "integration_sha": integration_sha},
        )
    if not is_ancestor(repo, integration_sha, target_head):
        raise BatchPlanError(
            "legacy integration commit is not an ancestor of the target branch",
            error_code="legacy_ancestry_mismatch",
            details={"integration_sha": integration_sha, "target_head": target_head},
        )
    if strategy == "merge" and not is_ancestor(repo, final_sha, integration_sha):
        raise BatchPlanError(
            "legacy merge integration does not contain the worker final commit",
            error_code="legacy_ancestry_mismatch",
            details={"final_sha": final_sha, "integration_sha": integration_sha},
        )
    if strategy == "cherry-pick" and integration_sha == integration_start:
        raise BatchPlanError(
            "legacy cherry-pick integration does not contain a new integration commit",
            error_code="legacy_ancestry_mismatch",
            details={"integration_start_sha": integration_start, "integration_sha": integration_sha},
        )
    if strategy == "cherry-pick":
        worker_patch = _commit_patch_id(repo, final_sha)
        integrated_patch = _commit_patch_id(repo, integration_sha)
        if worker_patch is None or integrated_patch is None or worker_patch != integrated_patch:
            raise BatchPlanError(
                "legacy cherry-pick integration does not match the worker final commit",
                error_code="legacy_ancestry_mismatch",
                details={"final_sha": final_sha, "integration_sha": integration_sha},
            )
    evidence: IntegrationEvidence = {
        "target_branch": target_branch,
        "strategy": str(strategy),
        "commit": integration_sha,
        "integrated_commit": integration_sha,
        "integration_sha": integration_sha,
    }
    return evidence, target_head


def import_legacy_state(
    repo_arg: str,
    batch_state_arg: str,
    legacy_state_arg: str,
    *,
    ticket: str | None = None,
    result: str | None = None,
    checks: object = None,
) -> dict[str, object]:
    """Import one pre-batch lifecycle state into an authoritative batch plan."""

    legacy_path, legacy_state = _load_legacy_state(legacy_state_arg)
    main_repo = resolve_repo(repo_arg)
    expected_repo = Path(str(legacy_state["repo"])).expanduser().resolve()
    if main_repo != expected_repo:
        raise BatchPlanError(
            "legacy state repository does not match the requested repository",
            error_code="legacy_repository_mismatch",
            details={"expected_repo": str(expected_repo), "actual_repo": str(main_repo)},
        )
    require_clean(main_repo)
    actual_ticket = str(legacy_state["ticket"])
    if ticket is not None and ticket != actual_ticket:
        raise BatchPlanError(
            "legacy state ticket identity does not match the requested ticket",
            error_code="legacy_ticket_mismatch",
            details={"state_ticket": actual_ticket, "requested_ticket": ticket},
        )
    start_sha, final_sha = _legacy_worker_evidence(main_repo, legacy_state)

    batch_path = Path(batch_state_arg).expanduser().resolve()
    with batch_state_lock(batch_path):
        batch = _load_batch_state_unlocked(batch_path)
        batch["state_path"] = str(batch_path)
        if Path(str(batch["repo"])).expanduser().resolve() != main_repo:
            raise BatchPlanError(
                "batch plan repository does not match the requested repository",
                error_code="target_repository_mismatch",
                details={"batch_repo": batch.get("repo"), "actual_repo": str(main_repo)},
            )
        expected_branch = str(batch["target_branch"])
        if str(legacy_state["base_branch"]) != expected_branch:
            raise BatchPlanError(
                "legacy state target branch does not match the batch plan",
                error_code="legacy_target_mismatch",
                details={"expected_branch": expected_branch, "actual_branch": legacy_state["base_branch"]},
            )
        batch_start_sha = _legacy_commit(main_repo, batch.get("starting_sha"), "batch start")
        if not is_ancestor(main_repo, start_sha, batch_start_sha):
            raise BatchPlanError(
                "legacy worker start commit is not an ancestor of the batch starting SHA",
                error_code="legacy_ancestry_mismatch",
                details={"legacy_start_sha": start_sha, "batch_starting_sha": batch_start_sha},
            )
        record = _find_batch_ticket(batch, actual_ticket)
        if record is None:
            raise BatchPlanError(
                f"ticket is not present in the batch plan: {actual_ticket}",
                error_code="ticket_unknown",
            )
        existing_imports = batch.get("legacy_imports")
        already_imported = (
            isinstance(existing_imports, dict)
            and actual_ticket in existing_imports
            and str(record["status"]) == str(legacy_state["status"])
        )
        if str(record["status"]) not in RUNNABLE_STATUSES and not already_imported:
            raise BatchPlanError(
                f"ticket is already claimed in the batch plan: {actual_ticket}",
                error_code="ticket_already_started",
                details={"ticket": actual_ticket, "status": record["status"]},
            )
        unmet, gates, _ = _predecessor_evidence(main_repo, batch, actual_ticket, start_sha)
        if unmet or gates:
            raise BatchPlanError(
                "legacy worker start does not contain validated predecessor evidence",
                error_code="legacy_ancestry_mismatch",
                details={
                    "ticket": actual_ticket,
                    "unmet_predecessors": unmet,
                    "gates": gates,
                    "start_sha": start_sha,
                },
            )

        integration: IntegrationEvidence | None = None
        if (
            run_git_process(
                main_repo,
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{expected_branch}",
            ).returncode
            != 0
        ):
            raise BatchPlanError(
                f"batch target branch does not exist: {expected_branch}",
                error_code="legacy_target_mismatch",
                details={"target_branch": expected_branch},
            )
        target_head = run_git(main_repo, "rev-parse", f"refs/heads/{expected_branch}")
        if str(legacy_state["status"]) == "integrated":
            if final_sha is None:
                raise BatchPlanError(
                    "legacy integrated state is missing final commit evidence",
                    error_code="legacy_evidence_invalid",
                )
            integration, target_head = _legacy_integration_evidence(
                main_repo,
                legacy_state,
                target_branch=expected_branch,
                start_sha=start_sha,
                final_sha=final_sha,
            )

        verification: VerificationEvidence | None = None
        required_checks_map = batch.get("required_checks")
        if not isinstance(required_checks_map, dict):
            raise BatchPlanError(
                "required_checks must map every ticket identity exactly once",
                error_code="batch_state_corrupt",
            )
        required_checks = list(required_checks_map.get(actual_ticket, []))
        if result is not None:
            if str(legacy_state["status"]) != "integrated" or integration is None:
                raise BatchPlanError(
                    "legacy verification requires a successfully integrated state",
                    error_code="verification_not_ready",
                    details={"ticket": actual_ticket, "status": legacy_state["status"]},
                )
            if result not in VERIFICATION_RESULTS:
                raise BatchPlanError(
                    "legacy verification result must be 'passed' or 'failed'",
                    error_code="verification_invalid",
                    details={"result": result},
                )
            check_results = _parse_check_results(checks, required_checks, result=result)
            verification = {
                "result": result,
                "status": result,
                "required_checks": required_checks,
                "checks": check_results,
                "target_branch": expected_branch,
                "target_head": target_head,
            }

        original_batch = deepcopy(batch)
        original_legacy = deepcopy(legacy_state)
        status = str(legacy_state["status"])
        record["status"] = cast(TicketStatus, status)
        if integration is not None:
            record["integration"] = integration
            record["integration_target_branch"] = expected_branch
            record["integration_strategy"] = integration["strategy"]
            record["integrated_commit"] = integration["commit"]
            integrations = batch.setdefault("integrations", {})
            if not isinstance(integrations, dict):
                raise BatchPlanError("integrations must be a JSON object", error_code="batch_state_corrupt")
            integrations[actual_ticket] = integration
        if verification is not None:
            record["verification"] = verification
            verifications = batch.setdefault("verifications", {})
            if not isinstance(verifications, dict):
                raise BatchPlanError("verifications must be a JSON object", error_code="batch_state_corrupt")
            verifications[actual_ticket] = verification

        legacy_imports = batch.setdefault("legacy_imports", {})
        if not isinstance(legacy_imports, dict):
            raise BatchPlanError("legacy_imports must be a JSON object", error_code="batch_state_corrupt")
        legacy_imports[actual_ticket] = {
            "state_path": str(legacy_path),
            "schema_version": legacy_state["schema_version"],
            "status": status,
            "repo": str(main_repo),
            "target_branch": expected_branch,
            "ticket": actual_ticket,
            "worker_branch": legacy_state["branch"],
            "start_sha": start_sha,
            "final_sha": final_sha,
            "integration": integration,
            "verification": verification,
        }

        ticket_states = batch.setdefault("ticket_states", {})
        if not isinstance(ticket_states, dict):
            raise BatchPlanError("ticket_states must be a JSON object", error_code="batch_state_corrupt")
        mirrored = dict(legacy_state)
        mirrored.update(
            {
                "batch_state": str(batch_path),
                "batch_state_path": str(batch_path),
                "batch_id": batch["batch_id"],
                "legacy_imported": True,
                "status": status,
            }
        )
        if integration is not None:
            mirrored["integration"] = integration
        if verification is not None:
            mirrored["verification"] = verification
            mirrored["verification_result"] = result
            mirrored["verification_target_head"] = target_head
        ticket_states[actual_ticket] = mirrored

        if (
            status in SATISFIED_PREDECESSOR_STATUSES
            and verification is not None
            and result == "passed"
        ):
            generation_tickets = _frontier_generation_tickets(batch)
            if (
                actual_ticket in generation_tickets
                and _frontier_generation_is_complete(batch, generation_tickets)
            ):
                batch["frontier_sha"] = target_head
                batch["frontier_generation"] = int(batch.get("frontier_generation", 0)) + 1
                next_frontier = _calculate_frontier(batch)
                batch["frontier_tickets"] = list(next_frontier["frontier"])
        next_frontier = _calculate_frontier(batch)
        batch["frontier"] = list(next_frontier["frontier"])
        batch["runnable"] = list(next_frontier["runnable"])
        validate_batch_plan(batch)
        _atomic_write_json(batch_path, batch)

        try:
            legacy_state.update(
                {
                    "batch_state": str(batch_path),
                    "batch_state_path": str(batch_path),
                    "batch_id": batch["batch_id"],
                    "legacy_imported": True,
                }
            )
            if integration is not None:
                legacy_state["integration"] = integration
            if verification is not None:
                legacy_state.update(
                    {
                        "verification": verification,
                        "verification_result": result,
                        "verification_target_head": target_head,
                    }
                )
            write_state(legacy_path, legacy_state)
        except (OSError, TypeError, ValueError):
            _atomic_write_json(batch_path, original_batch)
            legacy_state.clear()
            legacy_state.update(original_legacy)
            raise

    return {
        **next_frontier,
        "verified": True,
        "imported": True,
        "legacy": True,
        "ticket": actual_ticket,
        "status": status,
        "state_path": str(legacy_path),
        "legacy_state_path": str(legacy_path),
        "batch_state": str(batch_path),
        "batch_state_path": str(batch_path),
        "batch_id": batch["batch_id"],
        "verification": verification,
        "integration": integration,
        "verification_result": result,
        "integration_sha": integration.get("commit") if integration is not None else None,
    }


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
            blocked_details = frontier["blocked"].get(ticket, {})
            reason = str(blocked_details.get("reason", "ticket is not runnable"))
            raise BatchPlanError(
                f"ticket {ticket!r} is not in the current runnable frontier",
                error_code="ticket_not_runnable",
                details=_start_failure_details(
                    state,
                    ticket,
                    target_head=start_sha,
                    status=str(record["status"]),
                    predecessors=list(blocked_details.get("predecessors", [])),
                    gates=list(blocked_details.get("gates", [])),
                    reason=reason,
                ),
            )

        unmet, gates, predecessor_evidence = _predecessor_evidence(
            main_repo, state, ticket, start_sha
        )
        if unmet or gates:
            raise BatchPlanError(
                f"ticket {ticket!r} is not in the current runnable frontier",
                error_code="ticket_not_runnable",
                details=_start_failure_details(
                    state,
                    ticket,
                    target_head=start_sha,
                    status=str(record["status"]),
                    predecessors=unmet,
                    gates=gates,
                    reason="ticket predecessors or integration verification are not satisfied",
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
            "predecessor_evidence": predecessor_evidence,
            "predecessor_integration_evidence": predecessor_evidence,
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
    _persist_batch_ticket_state(state, status="finished")
    return state


def integration_error(result: subprocess.CompletedProcess) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or "git integration failed"
    return f"integration conflict or failure: {detail}"


def _require_cherry_pick_recovery_evidence(
    repo: Path,
    state: dict[str, object],
    head_sha: str,
) -> None:
    """Reject ``--continue`` after an aborted pick plus an unrelated commit."""

    final_sha = str(state["final_sha"])
    reflog_entries = run_git(
        repo,
        "reflog",
        "--format=%H%x09%gs",
        str(state["target_branch"]),
    ).splitlines()
    head_reflog = [entry for entry in reflog_entries if entry.startswith(f"{head_sha}\t")]
    source_subject = run_git(repo, "show", "-s", "--format=%s", final_sha)
    head_subject = run_git(repo, "show", "-s", "--format=%s", head_sha)
    if source_subject != head_subject:
        raise RuntimeError("integrated HEAD does not contain the cherry-picked ticket commit")

    source_files = {
        path
        for path in run_git(repo, "diff", "--name-only", f"{final_sha}^", final_sha).splitlines()
        if path
    }
    target_files = {
        path
        for path in run_git(
            repo,
            "diff",
            "--name-only",
            f"{state['integration_start_sha']}..{head_sha}",
        ).splitlines()
        if path
    }
    if not source_files or not source_files.issubset(target_files):
        raise RuntimeError("integrated HEAD does not contain the cherry-picked ticket commit")

    if not any(
        "cherry-pick" in entry.lower() and "abort" not in entry.lower()
        for entry in head_reflog
    ):
        raise RuntimeError("integrated HEAD does not contain the cherry-picked ticket commit")


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
    elif state.get("status") == "integration_conflict":
        _require_cherry_pick_recovery_evidence(repo, state, head_sha)

    integration: IntegrationEvidence = {
        "target_branch": target_branch,
        "strategy": strategy,
        "commit": head_sha,
        "integrated_commit": head_sha,
        "integration_sha": head_sha,
    }
    state.update(
        {
            "status": "integrated",
            "integration_strategy": strategy,
            "integrated_into": target_branch,
            "integration_sha": head_sha,
            "integration": integration,
        }
    )
    state.pop("integration_error", None)
    write_state(state_path, state)
    _persist_batch_ticket_state(
        state,
        status="integrated",
        integration=integration,
    )
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
    batch_path = _ticket_batch_state_path(state)
    if batch_path is not None:
        _, batch_plan = load_batch_state(str(batch_path))
        expected_target = str(batch_plan["target_branch"])
        if target != expected_target:
            raise BatchPlanError(
                "integration target branch does not match the batch plan",
                error_code="target_branch_mismatch",
                details={"expected_branch": expected_target, "actual_branch": target},
            )
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
        _persist_batch_ticket_state(
            state,
            status="integration_conflict",
            integration_error=str(state["integration_error"]),
        )
        raise RuntimeError(str(state["integration_error"]))

    return mark_integrated(
        repo=main_repo,
        state_path=state_path,
        state=state,
        strategy=strategy,
        target_branch=target,
    )


def _parse_check_results(
    value: object,
    required_checks: list[str],
    *,
    result: str,
) -> dict[str, object]:
    if value is None:
        value = {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise BatchPlanError(
                "verification checks must be valid JSON",
                error_code="verification_invalid",
                details={"reason": str(error)},
            ) from error
    if not isinstance(value, dict):
        raise BatchPlanError(
            "verification checks must be a JSON object",
            error_code="verification_invalid",
        )
    unknown = sorted(str(check) for check in value if check not in required_checks)
    if unknown:
        raise BatchPlanError(
            "verification contains an unknown required check",
            error_code="verification_invalid",
            details={"unknown_checks": unknown},
        )
    check_results = {str(check): check_result for check, check_result in value.items()}
    if result == "passed":
        missing = [check for check in required_checks if check not in check_results]
        if missing:
            raise BatchPlanError(
                "a passed verification must include every required check",
                error_code="verification_invalid",
                details={"missing_checks": missing},
            )
        failed = [
            check
            for check, check_result in check_results.items()
            if not _is_passed_check(check_result)
        ]
        if failed:
            raise BatchPlanError(
                "a passed verification cannot contain failed required checks",
                error_code="verification_invalid",
                details={"failed_checks": failed},
            )
    return check_results


def record_verification(
    state_arg: str,
    *,
    result: str,
    checks: object = None,
) -> dict[str, object]:
    """Record the parent's explicit required-check result for an integrated ticket."""

    if result not in VERIFICATION_RESULTS:
        raise BatchPlanError(
            "verification result must be 'passed' or 'failed'",
            error_code="verification_invalid",
            details={"result": result},
        )

    state_path, ticket_state = load_state(state_arg)
    if ticket_state.get("status") != "integrated":
        raise BatchPlanError(
            "required-check verification requires a successfully integrated ticket",
            error_code="verification_not_ready",
            details={"ticket": ticket_state.get("ticket"), "status": ticket_state.get("status")},
        )
    batch_path = _ticket_batch_state_path(ticket_state)
    if batch_path is None:
        raise BatchPlanError(
            "required-check verification requires a batch state",
            error_code="batch_state_missing",
            details={"guidance": "record verification against the validated batch plan"},
        )

    ticket = str(ticket_state.get("ticket", ""))
    repo = resolve_repo(str(ticket_state["repo"]))
    with batch_state_lock(batch_path):
        batch = _load_batch_state_unlocked(batch_path)
        record = _find_batch_ticket(batch, ticket)
        if record is None:
            raise BatchPlanError(
                f"ticket is not present in the batch plan: {ticket}",
                error_code="ticket_unknown",
            )
        if str(record["status"]) != "integrated":
            raise BatchPlanError(
                "required-check verification requires a successfully integrated ticket",
                error_code="verification_not_ready",
                details={"ticket": ticket, "status": record["status"]},
            )
        integration = _ticket_integration(record, batch)
        if integration is None:
            raise BatchPlanError(
                "integrated ticket is missing integration evidence",
                error_code="batch_state_corrupt",
                details={"ticket": ticket},
            )
        target_branch = str(integration["target_branch"])
        assert_current_branch(repo, target_branch)
        require_clean(repo)
        target_head = run_git(repo, "rev-parse", "HEAD")
        integration_commit = _integration_commit(integration)
        if integration_commit is None:
            raise BatchPlanError(
                "integrated ticket is missing an integration commit",
                error_code="batch_state_corrupt",
                details={"ticket": ticket},
            )
        if not is_ancestor(repo, integration_commit, target_head):
            raise BatchPlanError(
                "integration commit is not an ancestor of the verification target HEAD",
                error_code="predecessor_not_ancestor",
                details={
                    "ticket": ticket,
                    "integration_commit": integration_commit,
                    "target_head": target_head,
                },
            )

        required_checks_map = batch.get("required_checks")
        if not isinstance(required_checks_map, dict):
            raise BatchPlanError(
                "required_checks must map every ticket identity exactly once",
                error_code="batch_state_corrupt",
            )
        required_checks = list(required_checks_map.get(ticket, []))
        check_results = _parse_check_results(
            checks,
            required_checks,
            result=result,
        )
        verification: VerificationEvidence = {
            "result": result,
            "status": result,
            "required_checks": required_checks,
            "checks": check_results,
            "target_branch": target_branch,
            "target_head": target_head,
        }
        record["verification"] = verification
        verifications = batch.setdefault("verifications", {})
        if not isinstance(verifications, dict):
            raise BatchPlanError("verifications must be a JSON object", error_code="batch_state_corrupt")
        verifications[ticket] = verification
        ticket_states = batch.setdefault("ticket_states", {})
        if not isinstance(ticket_states, dict):
            raise BatchPlanError("ticket_states must be a JSON object", error_code="batch_state_corrupt")
        mirrored = dict(ticket_state)
        mirrored["verification"] = verification
        mirrored["verification_result"] = result
        mirrored["verification_target_head"] = target_head
        ticket_states[ticket] = mirrored
        if result == "passed":
            generation_tickets = _frontier_generation_tickets(batch)
            if (
                ticket in generation_tickets
                and _frontier_generation_is_complete(batch, generation_tickets)
            ):
                # Keep every ticket in one frozen frontier on the same base
                # until that generation has been integrated and verified in
                # full. Only then can the next frontier open at this target.
                batch["frontier_sha"] = target_head
                batch["frontier_generation"] = int(
                    batch.get("frontier_generation", 0)
                ) + 1
                next_frontier = _calculate_frontier(batch)
                batch["frontier_tickets"] = list(next_frontier["frontier"])
        next_frontier = _calculate_frontier(batch)
        batch["frontier"] = list(next_frontier["frontier"])
        batch["runnable"] = list(next_frontier["runnable"])
        _atomic_write_json(batch_path, batch)

    ticket_state.update(
        {
            "verification": verification,
            "verification_result": result,
            "verification_target_head": target_head,
        }
    )
    write_state(state_path, ticket_state)
    return ticket_state


def verify_boundary(
    state_arg: str,
    *,
    result: str,
    checks: object = None,
) -> dict[str, object]:
    """Compatibility alias for callers that name the operation ``verify``."""

    return record_verification(state_arg, result=result, checks=checks)


def _require_cleanup_verification(ticket_state: dict[str, object]) -> None:
    batch_path = _ticket_batch_state_path(ticket_state)
    if batch_path is None:
        return
    _, batch = load_batch_state(str(batch_path))
    ticket = str(ticket_state.get("ticket", ""))
    record = _find_batch_ticket(batch, ticket)
    verification = _ticket_verification(record, batch) if record is not None else None
    result = (
        verification.get("result", verification.get("status"))
        if verification is not None
        else None
    )
    if ticket_state.get("legacy_imported") and result != "passed":
        # A pre-batch ticket keeps its original finish/integrate/cleanup
        # contract.  Missing or failed imported verification blocks new
        # dependents, but must not strand the legacy worker resources.
        return
    if record is None or str(record["status"]) != "integrated" or result != "passed":
        raise BatchPlanError(
            "cleanup requires integration and an explicit passed verification result",
            error_code="verification_required",
            details={"ticket": ticket, "verification_result": result},
        )

    integration = _ticket_integration(record, batch)
    integration_commit = _integration_commit(integration)
    target_branch = str(integration.get("target_branch", "")) if integration else ""
    verification_head = (
        verification.get("target_head") if verification is not None else None
    )
    if (
        integration_commit is None
        or not target_branch
        or not isinstance(verification_head, str)
        or not verification_head
    ):
        raise BatchPlanError(
            "cleanup requires complete integration and verification evidence",
            error_code="batch_state_corrupt",
            details={"ticket": ticket},
        )

    expected_target = str(batch.get("target_branch", ""))
    if (
        target_branch != expected_target
        or verification.get("target_branch") != target_branch
    ):
        raise BatchPlanError(
            "cleanup evidence target branch does not match the batch plan",
            error_code="target_branch_mismatch",
            details={
                "ticket": ticket,
                "expected_branch": expected_target,
                "integration_branch": target_branch,
                "verification_branch": verification.get("target_branch"),
            },
        )

    repo = resolve_repo(str(ticket_state["repo"]))
    target_head = run_git(repo, "rev-parse", target_branch)
    if not is_ancestor(repo, integration_commit, target_head):
        raise BatchPlanError(
            "integrated commit is not an ancestor of the cleanup target HEAD",
            error_code="integration_not_ancestor",
            details={
                "ticket": ticket,
                "integration_commit": integration_commit,
                "target_branch": target_branch,
                "target_head": target_head,
            },
        )
    if not is_ancestor(repo, verification_head, target_head):
        raise BatchPlanError(
            "verification target HEAD is not an ancestor of the cleanup target HEAD",
            error_code="verification_target_not_ancestor",
            details={
                "ticket": ticket,
                "verification_target_head": verification_head,
                "target_branch": target_branch,
                "target_head": target_head,
            },
        )


def cleanup_boundary(state_arg: str) -> dict[str, object]:
    state_path, state = load_state(state_arg)
    if state.get("status") != "integrated":
        raise RuntimeError("only an integrated ticket can be cleaned up")
    _require_cleanup_verification(state)

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
    _persist_batch_ticket_state(state, status="cleaned")
    return state


def _cli_failure_details(args: argparse.Namespace) -> dict[str, object]:
    """Provide a stable lifecycle failure shape even for unexpected errors."""

    details: dict[str, object] = {
        "ticket": getattr(args, "ticket", None),
        "status": None,
        "unmet_predecessors": [],
        "gates": [],
        "target_head": None,
    }

    state_arg = getattr(args, "state", None) or getattr(args, "legacy_state", None)
    if state_arg:
        try:
            _, ticket_state = load_state(str(state_arg))
            details["ticket"] = ticket_state.get("ticket", details["ticket"])
            details["status"] = ticket_state.get("status")
            repo_value = ticket_state.get("repo")
            if repo_value:
                repo = resolve_repo(str(repo_value))
                branch = ticket_state.get(
                    "target_branch", ticket_state.get("integrated_into", ticket_state.get("base_branch"))
                )
                details["target_head"] = run_git(
                    repo, "rev-parse", str(branch) if branch else "HEAD"
                )
        except (OSError, RuntimeError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            pass

    batch_arg = getattr(args, "batch_state", None)
    if batch_arg:
        try:
            _, batch = load_batch_state(str(batch_arg))
            ticket = details["ticket"]
            if ticket:
                record = _find_batch_ticket(batch, str(ticket))
                if record is not None:
                    details["status"] = record["status"]
                    details["unmet_predecessors"] = list(record["dependencies"])
            repo_value = batch.get("repo")
            if repo_value:
                details["target_head"] = run_git(Path(str(repo_value)), "rev-parse", "HEAD")
        except (OSError, RuntimeError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            pass

    return details


def _add_legacy_import_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True)
    parser.add_argument("--batch-state", required=True)
    parser.add_argument("--state", "--legacy-state", dest="legacy_state", required=True)
    parser.add_argument("--ticket")
    parser.add_argument(
        "--result",
        "--verification-result",
        dest="verification_result",
        choices=tuple(sorted(VERIFICATION_RESULTS)),
    )
    parser.add_argument(
        "--checks-json",
        "--checks",
        dest="checks",
        help="optional JSON object mapping required check names to their observed result",
    )


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

    plan_import_parser = plan_subparsers.add_parser(
        "import", aliases=("legacy-import", "recover", "migrate"),
        help="validate and import a pre-batch lifecycle state",
    )
    _add_legacy_import_arguments(plan_import_parser)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--repo", required=True)
    start_parser.add_argument("--ticket", required=True)
    start_parser.add_argument(
        "--batch-state",
        help="path to the validated batch plan state (required for every multi-ticket start)",
    )
    start_parser.add_argument("--worktree-root")
    start_parser.add_argument("--branch")

    status_parser = subparsers.add_parser(
        "status", help="report live batch status and blocked gates"
    )
    status_parser.add_argument("--state", required=True)

    report_parser = subparsers.add_parser(
        "report", help="report dependency-ordered completion evidence"
    )
    report_parser.add_argument("--state", required=True)

    finish_parser = subparsers.add_parser("finish")
    finish_parser.add_argument("--state", required=True)

    integrate_parser = subparsers.add_parser("integrate")
    integrate_parser.add_argument("--state", required=True)
    integrate_parser.add_argument(
        "--strategy", choices=("merge", "cherry-pick"), default="cherry-pick"
    )
    integrate_parser.add_argument("--target-branch")
    integrate_parser.add_argument("--continue", dest="continue_integration", action="store_true")

    verify_parser = subparsers.add_parser(
        "verify",
        aliases=("record-verification", "verification"),
        help="record the parent's explicit required-check result",
    )
    verify_parser.add_argument("--state", help="ticket lifecycle state path")
    verify_parser.add_argument("--batch-state", help="validated batch state path")
    verify_parser.add_argument("--ticket", help="ticket identity when using --batch-state")
    verify_parser.add_argument(
        "--result",
        "--status",
        "--verification-result",
        dest="verification_result",
        choices=tuple(sorted(VERIFICATION_RESULTS)),
        required=True,
    )
    verify_parser.add_argument(
        "--checks-json",
        "--checks",
        dest="checks",
        help="optional JSON object mapping required check names to their observed result",
    )

    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--state", required=True)

    legacy_parser = subparsers.add_parser(
        "legacy", help="import one pre-batch lifecycle state into a validated batch plan"
    )
    legacy_subparsers = legacy_parser.add_subparsers(dest="legacy_command", required=True)
    legacy_import_parser = legacy_subparsers.add_parser(
        "import", aliases=("recover", "migrate"), help="validate and import a legacy state"
    )
    _add_legacy_import_arguments(legacy_import_parser)

    direct_import_parser = subparsers.add_parser(
        "import", aliases=("legacy-import", "recover", "migrate"),
        help="validate and import a pre-batch state (alias for legacy import)",
    )
    _add_legacy_import_arguments(direct_import_parser)

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
            elif args.plan_command in {"import", "legacy-import", "recover", "migrate"}:
                result = import_legacy_state(
                    args.repo,
                    args.batch_state,
                    args.legacy_state,
                    ticket=args.ticket,
                    result=args.verification_result,
                    checks=args.checks,
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
        elif args.command == "status":
            result = batch_status(args.state)
        elif args.command == "report":
            result = completion_report(args.state)
        elif args.command == "finish":
            result = finish_boundary(args.state)
        elif args.command == "integrate":
            result = integrate_boundary(
                args.state,
                strategy=args.strategy,
                target_branch=args.target_branch,
                continue_integration=args.continue_integration,
            )
        elif args.command in {"verify", "record-verification", "verification"}:
            verification_state = args.state
            if verification_state is None:
                if not args.batch_state or not args.ticket:
                    raise BatchPlanError(
                        "verification requires --state or --batch-state with --ticket",
                        error_code="verification_invalid",
                    )
                _, batch = load_batch_state(args.batch_state)
                ticket_states = batch.get("ticket_states")
                if not isinstance(ticket_states, dict) or args.ticket not in ticket_states:
                    raise BatchPlanError(
                        f"ticket state does not exist in batch state: {args.ticket}",
                        error_code="ticket_unknown",
                    )
                ticket_state = ticket_states[args.ticket]
                if not isinstance(ticket_state, dict) or not ticket_state.get("state_path"):
                    raise BatchPlanError(
                        f"ticket state path is missing for batch ticket: {args.ticket}",
                        error_code="batch_state_corrupt",
                    )
                verification_state = str(ticket_state["state_path"])
            if verification_state is None:
                raise BatchPlanError(
                    "verification state path is missing",
                    error_code="verification_invalid",
                )
            result = record_verification(
                verification_state,
                result=args.verification_result,
                checks=args.checks,
            )
        elif args.command == "legacy":
            result = import_legacy_state(
                args.repo,
                args.batch_state,
                args.legacy_state,
                ticket=args.ticket,
                result=args.verification_result,
                checks=args.checks,
            )
        elif args.command in {"import", "legacy-import", "recover", "migrate"}:
            result = import_legacy_state(
                args.repo,
                args.batch_state,
                args.legacy_state,
                ticket=args.ticket,
                result=args.verification_result,
                checks=args.checks,
            )
        else:
            result = cleanup_boundary(args.state)
    except BatchPlanError as error:
        details = _cli_failure_details(args)
        details.update(error.details)
        print(
            json.dumps(
                {
                    "verified": False,
                    "error": str(error),
                    "error_code": error.error_code,
                    "details": details,
                }
            ),
            file=sys.stderr,
        )
        return 1
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {
                    "verified": False,
                    "error": str(error),
                    "error_code": "lifecycle_error",
                    "details": _cli_failure_details(args),
                }
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
