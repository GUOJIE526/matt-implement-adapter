# Brief templates

Use the smallest template that matches the ticket's hidden-logic risk. Do not
add empty sections just to match the medium or heavy shape.

## Shared frontmatter

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

## Light

Keep the brief compact. Use these sections:

```markdown
## Applicable Instructions
## Confirmed Requirements
## Confirmed Repository Facts
## Outcome
## Scope
## Existing Flow
## Tests
## Verification Checklist
```

Add `## Plain-language Summary`, `## Implementation Choices`, or
`## Open Questions` only when they add useful information. A light brief is not
required when the user did not explicitly ask for one.

## Medium

Use the shared sections above and add:

```markdown
## Plain-language Summary
## Behavior Model
## Decision Table
## Module Responsibilities
## Implementation Choices
## Test Seams
## Known Risks
## Open Questions
```

The decision table must state conditions, outcomes, precedence, and conflict
behavior. Each non-trivial row needs a test or verification seam.

## Heavy

Use the medium sections and add:

```markdown
## Invariants
## State and Side Effects
```

`State and Side Effects` must cover transition ownership, transaction boundaries,
external calls, idempotency or retry behavior where relevant, ordering, and
failure behavior. `Open Questions` must contain unresolved product, domain,
contract, or ownership decisions, or explicitly say that none remain.

## Evidence sections

Keep these four categories separate when they are relevant:

```markdown
## Confirmed Requirements
## Confirmed Repository Facts
## Implementation Choices
## Open Questions
```

Use `references/evidence-policy.md` for the meaning of each category. Do not
turn the brief into a line-by-line source annotation or a second teaching
document.
