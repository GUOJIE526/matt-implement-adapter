---
name: implementation-brief
description: Prepare implementation briefs that expose hidden logic, risks, side effects, and test seams before coding.
disable-model-invocation: true
---

# Implementation Brief

Use this skill after ticketing and before implementation when one approved ticket
may hide logic that an ordinary task description does not make visible. Produce
implementation guidance; do not implement the ticket.

Keep the work scoped to one ticket. The ticket, parent spec, user decisions,
repository instructions, and installed implementation skills remain authoritative.
The brief is guidance, not proof that the proposed design is correct.

## 1. Establish the boundary

Read in this order:

1. User decisions, the approved ticket, parent spec, and acceptance criteria.
2. Repository `AGENTS.md` files and applicable project skills.
3. The existing code path affected by the ticket.

For project instructions, follow this routing:

- Read skills explicitly referenced by applicable `AGENTS.md` files.
- Read a skill whose description clearly covers this ticket's language, framework,
  or affected responsibility.
- Do not scan or apply every skill in the repository. If applicability is unclear,
  record it as a candidate or open question instead of silently applying it.

Record the instruction paths actually read. If no applicable project skill is
found, say so. Do not claim to have applied a rule that was not read.

Discover the repository before proposing a design. If codebase-memory MCP is
available, index the repository when required, then use architecture, graph search,
call tracing, and targeted snippets for code discovery. Use `rg` or equivalent for
ticket files, Markdown, scripts, and configuration that the graph does not model.
Record relevant existing paths and symbols so another developer can verify the
reasoning.

## 2. Classify the ticket

Classify by hidden-logic risk, not by file count or line count. If uncertain, use
the higher level.

### Light

Use for routine CRUD or a single-module change that follows an existing path and
has no new state transition, rule precedence, shared contract, external side
effect, transaction, retry, or concurrency concern.

If a brief was not explicitly requested, report `light: direct implementation
recommended` and do not create a brief. If a brief is requested, use the compact
light shape in `references/brief-template.md`.

### Medium

Use when the ticket crosses established layers, adds meaningful validation or
branching, introduces a state transition, or coordinates modules, persistence,
permissions, transactions, events, or caches.

Read `references/brief-template.md` and `references/evidence-policy.md`, then
write the medium brief shape.

### Heavy

Use when the ticket contains a state machine, conflicting rules, idempotency,
retry, concurrency, payment or security behavior, distributed or external
workflow, migration, backward compatibility, or several interacting domain
concepts.

Read both references, then write the heavy brief shape with explicit decision
precedence, state transitions, side-effect ordering, failure behavior, and
unresolved questions. Do not mark it ready while a product, domain, contract, or
ownership decision is unresolved.

## 3. Expose the hidden logic

For medium and heavy work, model the implementation before naming classes or
files. Cover the applicable items below:

- Behavior model: input, normalization, validation, decision, state change,
  persistence, external effects, and output.
- Decision table: conditions, outcomes, precedence, and conflict behavior.
- Invariants: facts that must remain true on successful and retry paths.
- Module responsibilities: what each boundary owns and explicitly does not own.
- State and side effects: transitions, transaction boundaries, idempotency,
  retries, events, cache invalidation, external calls, and failure behavior.
- Test seams: behavior cases and boundaries that prove the rules without coupling
  tests to private implementation details.
- Known risks: nested conditional stacks, duplicated rules, leaked infrastructure
  concerns, or an oversized orchestrator.

Prefer a small number of deep, named responsibilities over many shallow wrappers.
Keep orchestration linear where possible; make decisions explicit; keep pure rules
free of I/O; and do not hide side effects in predicates or mapping helpers. Do not
introduce a pattern, layer, or abstraction unless the ticket needs its independent
responsibility or the repository already uses it at the same seam.

Separate these categories in the brief:

- Confirmed requirements: from the ticket, spec, or user decisions.
- Confirmed repository facts: from actual files, symbols, call paths, or tests.
- Implementation choices: proposals made by the agent, not business rules.
- Open questions: unresolved product, domain, contract, ownership, or evidence gaps.

Do not invent business rules. A reasonable implementation choice must not be
written as a confirmed requirement.

## 4. Write and validate the brief

Store one brief per ticket at:

```text
.scratch/<feature-slug>/implementation-briefs/<ticket>-<ticket-slug>.md
```

Use a numeric ticket prefix when the ticket has one. Use this frontmatter:

```yaml
---
type: implementation-brief
version: 1
feature: <feature-slug>
ticket: "01"
source_ticket: <ticket reference or path>
complexity: light|medium|heavy
status: draft|ready-for-implement|blocked|superseded
---
```

Use the minimum shape appropriate to the classification. Read
`references/brief-template.md` for the exact section set. Do not pad a light
brief with empty medium/heavy sections. Do not create a second human-version
brief under `implementation-briefs/`; if the user needs teaching, pass this brief
to the separate `teach` skill.

If a brief already exists for the ticket, update it in place. Never leave two
usable briefs for the same ticket. When replacing an old brief, mark the old one
`superseded` before creating its replacement.

Before marking a medium or heavy brief `ready-for-implement`, verify:

- Every acceptance criterion maps to a behavior or verification item.
- Every non-trivial branch has an explicit outcome and test seam.
- Rule precedence and invariants are unambiguous.
- State changes and side effects have an owner and ordering.
- No unresolved decision is hidden as an implementation detail.
- The proposed shape does not add unrelated refactoring.
- The brief is sufficient to prevent one opaque method or scattered duplicate
  conditions.

Run the bundled `scripts/validate_brief.py` against the brief. When checking a
repository, also pass the repository root with `--scan-root` so duplicate active
briefs can be detected. Treat validator errors as blockers. Warnings about
external references do not by themselves block the brief.

The optional Matt adapter discovers only briefs under
`.scratch/**/implementation-briefs/*.md`. Missing, draft, blocked, stale-looking,
malformed, or ambiguous briefs must never be treated as an implementation blocker.

Report the classification, brief path (if created), instruction paths actually
read, key decisions, unresolved questions, validator result, and whether direct
implementation or the brief-guided workflow is recommended. Do not commit or push
unless requested.
