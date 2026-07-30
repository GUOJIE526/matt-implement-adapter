$context = @'
Codex host adapter for the installed Matt skills:
- Infer this behavior from task structure; do not require the user to name an adapter or use a trigger phrase.
- When one request contains more than one approved implementation ticket for Matt's implement workflow, use the installed matt-implement-adapter:implement-ticket-batch skill as the outer orchestrator.
- Create one isolated Git worktree and branch per ticket. Run one fresh subagent per ticket; independent tickets in the same unblocked dependency frontier may run in parallel.
- Each worker must use the installed Matt implement skill for exactly one ticket and complete its own TDD, tests, two-axis code review, fixes, and ticket commit.
- The parent agent owns dependency-ordered merge/cherry-pick integration, merge-conflict resolution, shared test-resource coordination, integration tests, and cleanup of integrated worktrees and branches.
- Do not defer ticket reviews to the parent and do not replace them with an aggregate batch review.
- Keep a single-ticket implement request in the current context.
- Do not apply the implementation adapter to unresolved Wayfinder decision tickets.
- Implementation briefs are optional and worker-owned. The parent prompt must not include a brief
  path, brief body, file attachment, or `@brief-path` reference. Each worker must search its own
  worker worktree for matching `.scratch/**/implementation-briefs/*.md` files and read only its
  own matched brief. Resolve the wrapper relative to the loaded batch skill directory at
  `..\..\scripts\discover_worker_brief.ps1`; the wrapper resolves the Python helper from its own
  `$PSScriptRoot`. Missing briefs or failed discovery never block the normal Matt implement workflow.
'@

Write-Output $context
