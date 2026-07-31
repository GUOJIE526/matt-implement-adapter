# Evidence policy

An implementation brief is guidance for an implementer, not proof that the
design is correct. Separate what is known from what is proposed.

## Categories

- `Confirmed Requirements`: directly supported by the ticket, parent spec,
  acceptance criteria, or an explicit user decision.
- `Confirmed Repository Facts`: supported by an actual file, qualified symbol,
  call path, configuration entry, database constraint, or existing test.
- `Implementation Choices`: a proposed shape chosen by the agent. It is not a
  business rule and must not be written as a confirmed requirement.
- `Open Questions`: an unresolved product, domain, contract, ownership, or
  evidence question. Do not hide one inside an implementation choice.

## Source format

For important repository facts, give a repo-relative path and the most precise
stable locator available:

```markdown
- `src/Orders/OrderHandler.cs` — `Orders.OrderHandler.Handle`: calls the existing
  persistence seam before publishing the event.
- `tests/Orders/OrderHandlerTests.cs` — duplicate-order case: proves the current
  rejection behavior.
```

Use codebase-memory graph symbols and traces when the repository provides them.
Line numbers are optional because they drift; a path plus qualified symbol is
usually more durable. If a claim has no source, classify it as an
`Implementation Choice` or `Open Question`.

## Confidence rules

- Do not infer a business rule from a method name alone.
- Do not treat an existing implementation as proof that the behavior is desired;
  compare it with the ticket and acceptance criteria.
- Do not treat a missing test as proof that a behavior is unsupported.
- If two sources conflict, record the conflict and stop at `blocked` until the
  authoritative decision is known.
- A brief may be `ready-for-implement` only when the required behavior and
  ownership decisions are explicit, even if the proposed code shape remains a
  choice for the implementer.
