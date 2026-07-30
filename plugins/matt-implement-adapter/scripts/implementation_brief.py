#!/usr/bin/env python3
"""Discover optional implementation briefs for approved tickets."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


READY_STATUSES = frozenset(
    {
        "approved",
        "ready",
        "ready-for-agent",
        "ready-for-implement",
        "ready_for_implement",
    }
)
IGNORED_STATUSES = frozenset(
    {
        "archived",
        "blocked",
        "draft",
        "in-progress",
        "in_progress",
        "rejected",
        "superseded",
    }
)


@dataclass(frozen=True)
class BriefReference:
    """A usable brief matched to one ticket."""

    ticket_id: str
    path: Path
    status: str
    feature: str | None = None
    source_ticket: str | None = None


@dataclass(frozen=True)
class IgnoredBrief:
    """A brief that was discovered but cannot be used for the requested tickets."""

    path: Path
    ticket_id: str | None
    reason: str


@dataclass(frozen=True)
class BriefCatalog:
    """Optional brief discovery results; missing briefs never block a caller."""

    matched: dict[str, BriefReference]
    missing: tuple[str, ...]
    ignored: tuple[IgnoredBrief, ...]

    @property
    def found(self) -> bool:
        return bool(self.matched)


def normalize_ticket_id(value: str | int | None) -> str | None:
    """Normalize numeric ticket references while preserving non-numeric references."""

    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    numeric_match = re.search(r"(?<!\d)(\d+)(?!\d)", text)
    if numeric_match:
        return str(int(numeric_match.group(1)))

    return text.casefold()


def _normalize_ticket_path(value: str | None, repo_root: Path) -> str | None:
    if value is None:
        return None

    text = value.strip().replace("\\", "/")
    if not text or "/" not in text:
        return None

    path = Path(text)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(repo_root)
        except ValueError:
            path = path.resolve()

    return os.path.normcase(path.as_posix().removeprefix("./")).replace("\\", "/")


def parse_front_matter(text: str) -> dict[str, str]:
    """Read the small YAML-like metadata block used by implementation briefs."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    metadata: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$", line)
        if not match:
            continue
        key, value = match.groups()
        metadata[key.casefold()] = value.strip("\"'")

    return metadata


def _ticket_id_from_path(path: Path) -> str | None:
    match = re.match(r"^(\d+)(?:[-_.]|$)", path.stem)
    return normalize_ticket_id(match.group(1)) if match else None


def _brief_paths(repo_root: Path, brief_root: str | Path | None) -> list[Path]:
    if brief_root is not None:
        root = Path(brief_root)
        if not root.is_absolute():
            root = repo_root / root
        if not root.exists():
            return []
        if root.is_file() and root.suffix.casefold() == ".md":
            return [root.resolve()]
        return sorted(path.resolve() for path in root.rglob("*.md") if path.is_file())

    scratch_root = repo_root / ".scratch"
    if not scratch_root.is_dir():
        return []

    paths: list[Path] = []
    for directory in scratch_root.rglob("implementation-briefs"):
        if directory.is_dir():
            paths.extend(path.resolve() for path in directory.glob("*.md") if path.is_file())
    return sorted(set(paths))


def _read_record(path: Path) -> tuple[BriefReference | None, IgnoredBrief | None]:
    try:
        metadata = parse_front_matter(path.read_text(encoding="utf-8"))
    except OSError as error:
        return None, IgnoredBrief(path, None, f"read failed: {error}")

    ticket_id = normalize_ticket_id(metadata.get("ticket")) or _ticket_id_from_path(path)
    if ticket_id is None:
        return None, IgnoredBrief(path, None, "missing ticket metadata and ticket-like filename")

    status = metadata.get("status", "unspecified").casefold()
    if status in IGNORED_STATUSES:
        return None, IgnoredBrief(path, ticket_id, f"status={status}")
    if status not in READY_STATUSES and status != "unspecified":
        return None, IgnoredBrief(path, ticket_id, f"unsupported status={status}")

    feature = metadata.get("feature") or path.parent.parent.name
    source_ticket = metadata.get("source_ticket")
    return (
        BriefReference(
            ticket_id=ticket_id,
            path=path,
            status=status,
            feature=feature,
            source_ticket=source_ticket,
        ),
        None,
    )


def discover_briefs(
    repo_root: str | Path,
    tickets: Sequence[str],
    *,
    brief_root: str | Path | None = None,
) -> BriefCatalog:
    """Find optional briefs matching the requested tickets without blocking on misses."""

    root = Path(repo_root).expanduser().resolve()
    requested_tickets: list[tuple[str, str | None]] = []
    for ticket in tickets:
        ticket_id = normalize_ticket_id(ticket)
        if ticket_id is None:
            continue
        ticket_path = _normalize_ticket_path(ticket, root)
        requested_ticket = (ticket_id, ticket_path)
        if requested_ticket not in requested_tickets:
            requested_tickets.append(requested_ticket)

    candidates: dict[str, list[BriefReference]] = {}
    ignored: list[IgnoredBrief] = []
    for path in _brief_paths(root, brief_root):
        record, ignored_record = _read_record(path)
        if ignored_record is not None:
            ignored.append(ignored_record)
            continue
        assert record is not None
        candidates.setdefault(record.ticket_id, []).append(record)

    matched: dict[str, BriefReference] = {}
    missing: list[str] = []
    for ticket_id, ticket_path in requested_tickets:
        records = candidates.get(ticket_id, [])
        if ticket_path is not None:
            records = [
                record
                for record in records
                if _normalize_ticket_path(record.source_ticket, root) == ticket_path
            ]
        if len(records) == 1:
            matched[ticket_id] = records[0]
        elif len(records) > 1:
            ignored.append(
                IgnoredBrief(
                    path=records[0].path,
                    ticket_id=ticket_id,
                    reason=f"ambiguous: {len(records)} briefs match this ticket",
                )
            )
            missing.append(ticket_id)
        else:
            missing.append(ticket_id)

    return BriefCatalog(
        matched=matched,
        missing=tuple(missing),
        ignored=tuple(ignored),
    )


def catalog_payload(catalog: BriefCatalog) -> dict[str, object]:
    """Serialize discovery results for the parent agent and worker prompts."""

    return {
        "found": catalog.found,
        "matched": {
            ticket_id: {
                "ticket": record.ticket_id,
                "path": str(record.path),
                "status": record.status,
                "feature": record.feature,
                "source_ticket": record.source_ticket,
            }
            for ticket_id, record in catalog.matched.items()
        },
        "missing": list(catalog.missing),
        "ignored": [
            {
                "path": str(record.path),
                "ticket": record.ticket_id,
                "reason": record.reason,
            }
            for record in catalog.ignored
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover optional implementation briefs for tickets."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser("discover")
    discover_parser.add_argument("--repo", required=True)
    discover_parser.add_argument("--ticket", action="append", required=True)
    discover_parser.add_argument("--brief-root")

    args = parser.parse_args()
    if args.command == "discover":
        catalog = discover_briefs(
            args.repo,
            args.ticket,
            brief_root=args.brief_root,
        )
        print(json.dumps(catalog_payload(catalog), indent=2, ensure_ascii=False))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
