---
name: implement-ticket-batch
description: Adapt Matt's implement workflow to Codex when one request contains more than one approved implementation ticket. Orchestrate one fresh sequential subagent per ticket, respect blockers, require that ticket to complete its own TDD, tests, code review, fixes, and commit, and verify the Git boundary before starting the next ticket. Apply implicitly from the task shape; do not require a trigger phrase. Do not use for a single ticket or for unresolved Wayfinder decision tickets.
---

# Matt Implement Ticket Batch

Act only as the outer Codex orchestrator. Preserve the installed Matt skills unchanged.

## Qualify the batch

- Require more than one approved implementation ticket.
- Do not treat Wayfinder decision tickets as implementation tickets. Finish the Wayfinder → to-spec → to-tickets handoff first.
- Use the configured tracker to read the parent spec, ticket bodies, statuses, blockers, and linked decisions.
- Honor explicit handoff or stop boundaries.
- Use the ordinary Matt `implement` workflow in the current context when there is only one ticket.

## Establish the boundary

1. Resolve the repository root and inspect the live Git status.
2. Refuse to auto-stash, reset, discard, or absorb unrelated changes. Ask for resolution when the worktree is not clean.
3. Order tickets by their blocking graph. Work only the open, unblocked frontier.
4. Keep at most one implementation-ticket subagent active.

Before spawning a ticket worker, run:

```powershell
python "<this-skill-directory>\scripts\ticket_boundary.py" start --repo "<repo-root>" --ticket "<ticket-reference>"
```

Resolve `<this-skill-directory>` from the loaded `SKILL.md` path. Retain the returned `state_path`.

## Run one ticket

Spawn exactly one fresh subagent with no inherited conversation turns. Give it:

- the exact repository root;
- exactly one ticket reference and its full body;
- the parent spec and relevant linked decisions;
- repository instructions and user constraints;
- the ticket-start SHA and boundary-state path.

Tell the worker to invoke the installed Matt `implement` skill for that ticket only. It owns the complete per-ticket workflow:

1. Work through Matt `tdd` at the agreed seams.
2. Run focused tests and type checks regularly, then the full project test suite once at the ticket end as Matt `implement` requires.
3. Keep all edits within the ticket scope.
4. Create a provisional ticket commit so the installed Matt `code-review` can inspect `<ticket-start>...HEAD`.
5. Run the ticket's own two-axis Matt `code-review`; do not defer it to the parent and do not replace it with an aggregate batch review.
6. Fix every accepted finding, revalidate, and amend the same ticket commit.
7. Run the boundary finish check and return the final commit SHA, changed files, test results, review results, and unresolved risks.

The provisional-commit bridge is required because the installed Matt `code-review` reads committed `HEAD` history while `implement` otherwise asks for review before the final commit.

## Verify before continuing

After the worker returns, run:

```powershell
python "<this-skill-directory>\scripts\ticket_boundary.py" finish --state "<state_path>"
```

Do not start another ticket unless all are true:

- the boundary command succeeds;
- exactly one commit was added since the ticket start;
- the final worktree is clean;
- the worker completed that ticket's review and validation;
- the tracker status is updated without creating unrelated uncommitted files.

Then recompute the frontier from live tracker state and repeat with a new fresh subagent.

## Finish

Do not run an aggregate Matt `code-review`; each ticket already completed the official workflow in its own context. At batch completion, verify the ordered ticket-to-commit mapping, required broad runtime/E2E evidence explicitly requested by the tickets, and a clean worktree. Report any blocked ticket without bypassing its blockers.
