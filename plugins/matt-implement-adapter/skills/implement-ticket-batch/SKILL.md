---
name: implement-ticket-batch
description: Adapt Matt's implement workflow to Codex when one request contains more than one approved implementation ticket. Run one fresh subagent per ticket in an isolated Git worktree and branch, run independent unblocked tickets in parallel, integrate completed branches in dependency order, handle conflicts and shared test resources in the parent, require each ticket to complete its own TDD, tests, code review, fixes, and commit, and clean up integrated worktrees and branches. Apply implicitly from the task shape; do not require a trigger phrase. Do not use for a single ticket or for unresolved Wayfinder decision tickets.
---

# Matt Implement Ticket Batch

Act as the parent Codex orchestrator. Preserve the installed Matt skills unchanged. The parent owns
parallel scheduling, Git integration, shared-resource coordination, integration testing, and cleanup;
each ticket worker owns the complete Matt workflow for exactly one ticket.

## Qualify the batch

- Require more than one approved implementation ticket.
- Do not treat Wayfinder decision tickets as implementation tickets. Finish the Wayfinder → to-spec →
  to-tickets handoff first.
- Use the configured tracker to read the parent spec, ticket bodies, statuses, blockers, and linked decisions.
- Honor explicit handoff or stop boundaries.
- Use the ordinary Matt `implement` workflow in the current context when there is only one ticket.

## Worker-owned optional implementation briefs

The parent does not discover, read, attach, inline, or mention implementation brief paths when it
constructs a worker prompt. The parent prompt contains only the ticket, worker worktree, parent
spec, repository instructions, and boundary information required to perform the ticket.

After the worker worktree is created, the worker resolves `<this-skill-directory>` from the directory
containing this loaded `SKILL.md`, then discovers optional briefs from inside that worktree:

```powershell
$skillDirectory = "<this-skill-directory>"
$wrapper = Join-Path $skillDirectory "..\..\scripts\discover_worker_brief.ps1"
if (-not (Test-Path -LiteralPath $wrapper -PathType Leaf)) {
  Write-Warning "Optional worker brief wrapper not found; continuing without a brief: $wrapper"
} else {
  & $wrapper `
    -Repo "<worker-worktree>" `
    -Ticket "<ticket-reference>"
}
```

Treat only `matched` entries as available guidance. The worker reads a matched brief from its own
worktree and keeps the brief body in the worker context. Missing, draft, stale-looking, malformed,
ambiguous, or failed discovery is optional and must not stop implementation. Do not use `@brief-path`,
file attachments, or parent-generated brief summaries.

The wrapper resolves `implementation_brief.py` from its own `$PSScriptRoot`. Do not depend on
`$env:PLUGIN_ROOT` or put a host-specific absolute path in repository files or prompt templates.

Briefs are implementation guidance, not a replacement for the parent spec, ticket, repository
instructions, or installed Matt skills. Do not read or pass one ticket's brief to another worker.

## Establish isolated ticket worktrees

1. Resolve the main repository root and inspect the live Git status.
2. Refuse to auto-stash, reset, discard, or absorb unrelated changes. Ask for resolution when the main
   worktree is not clean.
3. Freeze the current target branch and starting SHA before opening the frontier. Do not integrate into
   that branch while the initial frontier worktrees are being created.
4. Order tickets by their blocking graph and work only the open, unblocked frontier. Independent tickets
   in the same frontier may run in parallel; a dependent ticket waits until its predecessors are integrated
   and the required checks pass.
5. Create one worktree and worker branch per frontier ticket with:

   ```powershell
   python "<this-skill-directory>\scripts\ticket_boundary.py" start `
     --repo "<main-repo-root>" `
     --ticket "<ticket-reference>"
   ```

   Resolve `<this-skill-directory>` from the loaded `SKILL.md` path. Retain every returned `state_path`,
   `worktree`, `branch`, and `start_sha`. Never give two workers the same worktree or branch.

6. Keep at most one worker per ticket, but allow one fresh worker for every ticket in the current
   unblocked frontier to be active at the same time.

The worktree directory name is exactly the bounded `ticket_slug(ticket)`. Do not append UUIDs,
branch names, or other suffixes to the worktree path; the worker branch retains its unique token so
branch names remain collision-resistant.

## Run one ticket

Spawn exactly one fresh subagent with no inherited conversation turns for each frontier ticket. Give it:

- the exact worker worktree path and worker branch;
- the main repository root as read-only integration context;
- exactly one ticket reference and its full body;
- the parent spec and relevant linked decisions;
- repository instructions and user constraints;
- the ticket-start SHA and boundary-state path;
- any shared test-resource lock, isolated database/schema, port, temporary directory, or generated-output
  directory it must use.

The worker prompt must not contain a brief body, brief attachment, or `@brief-path` reference. Tell the
worker to run the worker-local discovery command above before coding.

Tell the worker to invoke the installed Matt `implement` skill for that ticket only. It owns the complete
per-ticket workflow:

1. Work through Matt `tdd` at the agreed seams.
2. Run focused tests and type checks regularly, then the full project test suite once at the ticket end
   as Matt `implement` requires. If a suite uses a non-isolated shared resource, the parent must serialize
   that resource-dependent phase or provide an isolated resource; the worker must not silently skip it.
3. Keep all edits inside its worker worktree and ticket scope. Do not edit the main worktree or another
   worker's branch.
4. Create a provisional ticket commit so the installed Matt `code-review` can inspect
   `<ticket-start>...HEAD`.
5. Run the ticket's own two-axis Matt `code-review`; do not defer it to the parent and do not replace it
   with an aggregate batch review.
6. Fix every accepted finding, revalidate, and amend the same ticket commit.
7. Run the boundary finish check and return the final commit SHA, changed files, test results, review
   results, worktree path, branch, and unresolved risks.

If the worker discovers an optional implementation brief for this ticket, require it to read the brief
from the worker worktree before coding. Treat the brief as design guidance only; if it conflicts with
the ticket, parent spec, repository instructions, or installed skills, follow the authoritative source
and report the conflict.

The provisional-commit bridge is required because the installed Matt `code-review` reads committed `HEAD`
history while `implement` otherwise asks for review before the final commit.

## Integrate in dependency order

After a worker finishes, run its finish check. Do not integrate a worker until its finish check succeeds.
The main agent then integrates completed branches into the frozen target branch in dependency order. Use
`cherry-pick` by default for a linear target history, or `merge` when preserving branch topology is useful:

```powershell
python "<this-skill-directory>\scripts\ticket_boundary.py" integrate `
  --state "<state-path>" `
  --strategy cherry-pick `
  --target-branch "<target-branch>"
```

The integration command records the integration state. It must run on the target branch with a clean main
worktree. Integrate independent tickets in a deterministic tracker order, and never integrate a dependent
ticket before all of its predecessors are integrated.

### Handle merge conflicts

If `merge` or `cherry-pick` reports a conflict:

1. Keep the worker worktree and branch. Do not clean them up.
2. Inspect the conflict against the ticket body, parent spec, and the already integrated changes.
3. Resolve only the conflict required to preserve both approved ticket contracts; ask for direction when
   the conflict exposes a product or domain decision.
4. Run focused tests for the integrated area.
5. Complete the Git operation, then record the resolved integration:

   ```powershell
   git add <resolved-files>
   git cherry-pick --continue
   python "<this-skill-directory>\scripts\ticket_boundary.py" integrate `
     --state "<state-path>" `
     --strategy cherry-pick `
     --target-branch "<target-branch>" `
     --continue
   ```

   Use `git merge --continue` and `--strategy merge` for a merge conflict.

Do not delete a conflicted ticket's branch or start its dependents until the integration is recorded and
the required checks pass.

## Coordinate shared resources and integration tests

- Treat databases, Docker containers, ports, singleton services, generated directories, fixture files,
  and external test accounts as shared resources unless isolation is explicit.
- Prefer per-worktree resource names. When isolation is not possible, the parent owns a lock and schedules
  the conflicting test phases serially while allowing unrelated implementation work to continue.
- After each integration, run the narrowest focused checks that can detect a conflict or contract break.
- After the full dependency frontier is integrated, run the requested broad project tests, runtime checks,
  and E2E/integration tests on the target branch before advancing dependent tickets when those tests are
  part of the ticket or parent contract.
- If integration tests fail, keep the relevant worker branch and state for diagnosis; do not hide the
  failure by deleting branches or resetting the target branch.

## Verify and clean up

For every worker, run:

```powershell
python "<this-skill-directory>\scripts\ticket_boundary.py" finish --state "<state-path>"
```

Only continue when the worker boundary succeeds, exactly one commit was added, its worktree is clean, and
the worker completed its own review and validation. After successful integration and required checks, run:

```powershell
python "<this-skill-directory>\scripts\ticket_boundary.py" cleanup --state "<state-path>"
```

Cleanup removes the worker worktree and its now-unnecessary branch. It refuses to operate before integration
is recorded, so conflicted or failed tickets remain recoverable.

At batch completion, verify the ordered ticket-to-worker-branch-to-integrated-commit mapping, no orphaned
worktrees or `codex/matt-ticket/*` branches remain, required broad runtime/E2E evidence is present, the
tracker statuses are updated, and the target worktree is clean. Do not run an aggregate Matt `code-review`;
each ticket already completed the official workflow in its own context. Report any blocked ticket without
bypassing its blockers.
