$context = @'
Codex host adapter for the installed Matt skills:
- Infer this behavior from task structure; do not require the user to name an adapter or use a trigger phrase.
- When one request contains more than one approved implementation ticket for Matt's implement workflow, use the installed matt-implement-adapter:implement-ticket-batch skill as the outer orchestrator.
- Adapter activation does not imply concurrency. Multiple approved tickets activate the adapter, but only tickets in the same validated, unblocked scheduler frontier may run in parallel.
- Before any worker start, create and persist a validated batch plan containing the complete dependency graph, target branch, starting SHA, and required checks. Start only tickets returned by the scheduler frontier; never infer an unblocked ticket from prompt order or ticket text.
- Create one isolated Git worktree and branch per ticket. Run one fresh subagent per ticket; independent tickets in the same scheduler-approved frontier may run in parallel.
- Each worker must use the installed Matt implement skill for exactly one ticket; the installed skill remains authoritative for the worker workflow.
- The parent agent owns dependency-ordered merge/cherry-pick integration, merge-conflict resolution, shared test-resource coordination, integration tests, and cleanup of integrated worktrees and branches.
- Do not defer ticket reviews to the parent and do not replace them with an aggregate batch review.
- Use batch status and completion report output to show every ticket's state, blocked predecessors or gates, worker branch, start SHA, integrated commit, and verification result. Never report a batch complete when a ticket is unfinished, failed, conflicted, or orphaned.
- Keep a single-ticket implement request in the current context.
- Do not apply the implementation adapter to unresolved Wayfinder decision tickets.
- Existing pre-batch lifecycle states remain recoverable through the batch-state migration/import path; do not bypass that recovery gate for new dependent starts.
'@

Write-Output $context
