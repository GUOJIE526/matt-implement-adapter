#!/usr/bin/env python3
"""Validate the mechanical contract of an implementation brief.

This validator intentionally does not judge business semantics or design quality.
It checks frontmatter, required sections, status consistency, local source-ticket
paths, and duplicate active briefs when a repository root is supplied.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable


ALLOWED_COMPLEXITIES = {"light", "medium", "heavy"}
ALLOWED_STATUSES = {"draft", "ready-for-implement", "blocked", "superseded"}
REQUIRED_FRONTMATTER = {
    "type",
    "version",
    "feature",
    "ticket",
    "source_ticket",
    "complexity",
    "status",
}

COMMON_SECTIONS = (
    ("Applicable Instructions",),
    ("Confirmed Requirements",),
    ("Confirmed Repository Facts",),
    ("Outcome",),
    ("Scope",),
    ("Verification Checklist",),
)
LIGHT_SECTIONS = (
    ("Existing Flow", "Existing Seam"),
    ("Tests", "Test Seams"),
)
MEDIUM_SECTIONS = (
    ("Plain-language Summary",),
    ("Behavior Model",),
    ("Decision Table",),
    ("Module Responsibilities",),
    ("Implementation Choices", "Implementation Shape"),
    ("Test Seams", "Tests"),
    ("Known Risks",),
    ("Open Questions",),
)
HEAVY_SECTIONS = (
    ("Invariants",),
    ("State and Side Effects",),
)

EMPTY_MARKERS = {
    "none",
    "none identified",
    "no open questions",
    "no unresolved questions",
    "n/a",
    "na",
    "無",
    "無。",
    "無未決問題",
    "無待決問題",
}


class BriefError(ValueError):
    """Raised when a brief cannot be parsed."""


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_brief(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise BriefError(f"cannot read file: {exc}") from exc

    if not lines or lines[0].strip() != "---":
        raise BriefError("frontmatter must start with '---'")

    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        raise BriefError("frontmatter is not closed with '---'")

    frontmatter: dict[str, str] = {}
    for line_number, line in enumerate(lines[1:end], start=2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise BriefError(f"frontmatter line {line_number} is not key: value")
        key, value = stripped.split(":", 1)
        key = key.strip()
        if not key:
            raise BriefError(f"frontmatter line {line_number} has an empty key")
        frontmatter[key] = unquote(value)

    body_sections: dict[str, list[str]] = {}
    current: str | None = None
    heading_pattern = re.compile(r"^##\s+(.+?)\s*$")
    for line in lines[end + 1 :]:
        match = heading_pattern.match(line.strip())
        if match:
            current = match.group(1).strip()
            body_sections.setdefault(current, [])
        elif current is not None:
            body_sections[current].append(line)

    return frontmatter, {
        name: "\n".join(content).strip()
        for name, content in body_sections.items()
    }


def has_content(value: str | None) -> bool:
    if value is None:
        return False
    return any(line.strip() and not line.strip().startswith("<!--") for line in value.splitlines())


def has_open_questions(value: str | None) -> bool:
    if not has_content(value):
        return False
    for line in value.splitlines():
        normalized = re.sub(r"^[-*]\s*", "", line.strip()).strip().lower()
        if not normalized or normalized.startswith("<!--"):
            continue
        if normalized not in EMPTY_MARKERS:
            return True
    return False


def find_section(sections: dict[str, str], candidates: Iterable[str]) -> str | None:
    for candidate in candidates:
        if candidate in sections:
            return candidate
    return None


def validate_brief(path: Path, repo_root: Path | None = None) -> tuple[list[str], list[str], dict[str, str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        frontmatter, sections = parse_brief(path)
    except BriefError as exc:
        return [f"{path}: {exc}"], warnings, {}

    missing = sorted(REQUIRED_FRONTMATTER - frontmatter.keys())
    if missing:
        errors.append(f"{path}: missing frontmatter: {', '.join(missing)}")

    if frontmatter.get("type") != "implementation-brief":
        errors.append(f"{path}: frontmatter type must be implementation-brief")
    if frontmatter.get("version") != "1":
        errors.append(f"{path}: frontmatter version must be 1")

    complexity = frontmatter.get("complexity", "")
    if complexity not in ALLOWED_COMPLEXITIES:
        errors.append(f"{path}: complexity must be one of {sorted(ALLOWED_COMPLEXITIES)}")

    status = frontmatter.get("status", "")
    if status not in ALLOWED_STATUSES:
        errors.append(f"{path}: status must be one of {sorted(ALLOWED_STATUSES)}")

    for key in ("feature", "ticket", "source_ticket"):
        if not frontmatter.get(key, "").strip():
            errors.append(f"{path}: frontmatter {key} must not be empty")

    required_groups = list(COMMON_SECTIONS)
    if complexity in {"light", "medium", "heavy"}:
        required_groups.extend(LIGHT_SECTIONS)
    if complexity in {"medium", "heavy"}:
        required_groups.extend(MEDIUM_SECTIONS)
    if complexity == "heavy":
        required_groups.extend(HEAVY_SECTIONS)

    for group in required_groups:
        section_name = find_section(sections, group)
        if section_name is None:
            errors.append(f"{path}: missing section: {' or '.join(group)}")
        elif not has_content(sections[section_name]):
            errors.append(f"{path}: section is empty: {section_name}")

    open_questions_name = find_section(sections, ("Open Questions",))
    open_questions = sections.get(open_questions_name) if open_questions_name else None
    if status == "blocked" and not has_open_questions(open_questions):
        errors.append(f"{path}: blocked briefs must contain an unresolved Open Questions entry")
    if status == "ready-for-implement" and has_open_questions(open_questions):
        errors.append(f"{path}: ready-for-implement briefs cannot contain unresolved Open Questions")

    source_ticket = frontmatter.get("source_ticket", "").strip()
    if repo_root and source_ticket:
        looks_local = (
            "/" in source_ticket
            or "\\" in source_ticket
            or source_ticket.startswith(".")
            or source_ticket.lower().endswith((".md", ".yaml", ".yml", ".json"))
        )
        if looks_local:
            candidate = Path(source_ticket)
            if not candidate.is_absolute():
                candidate = repo_root / candidate
            if not candidate.exists():
                errors.append(f"{path}: source_ticket path does not exist: {source_ticket}")
        else:
            warnings.append(f"{path}: source_ticket is not a local path; existence was not checked")

    return errors, warnings, frontmatter


def scan_active_duplicates(root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    by_ticket: dict[tuple[str, str], list[Path]] = {}
    pattern = ".scratch/**/implementation-briefs/*.md"
    for path in root.glob(pattern):
        try:
            frontmatter, _ = parse_brief(path)
        except BriefError as exc:
            warnings.append(f"{path}: skipped during duplicate scan: {exc}")
            continue
        if frontmatter.get("status") == "superseded":
            continue
        feature = frontmatter.get("feature", "").strip()
        ticket = frontmatter.get("ticket", "").strip()
        if feature and ticket:
            by_ticket.setdefault((feature, ticket), []).append(path)

    for (feature, ticket), paths in sorted(by_ticket.items()):
        if len(paths) > 1:
            joined = ", ".join(str(path) for path in paths)
            errors.append(f"duplicate active brief for {feature}/{ticket}: {joined}")
    return errors, warnings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate an implementation brief")
    parser.add_argument("brief", type=Path, help="path to one implementation brief")
    parser.add_argument(
        "--scan-root",
        type=Path,
        help="repository root used to check duplicate active briefs and local source_ticket paths",
    )
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    brief = args.brief.resolve()
    repo_root = args.scan_root.resolve() if args.scan_root else None

    if not brief.is_file():
        print(f"ERROR: brief does not exist: {brief}")
        return 1
    if repo_root and not repo_root.is_dir():
        print(f"ERROR: scan root is not a directory: {repo_root}")
        return 1

    errors, warnings, _ = validate_brief(brief, repo_root)
    if repo_root:
        duplicate_errors, duplicate_warnings = scan_active_duplicates(repo_root)
        errors.extend(duplicate_errors)
        warnings.extend(duplicate_warnings)

    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")

    if errors:
        print(f"INVALID: {brief}")
        return 1
    print(f"VALID: {brief}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
