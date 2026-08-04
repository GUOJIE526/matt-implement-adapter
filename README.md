# Matt Implement Adapter Marketplace

## 專案由來與需求原因

Matt 官方的 `implement` workflow 以單張 ticket 為執行邊界，要求該 ticket 在自己的 context
中完成 TDD、測試、雙軸 code review、修正與 commit。這種方式能讓每張 ticket 的需求、變更範圍
與交付結果保持清楚。

然而，在 Codex 中一次處理多張 approved implementation tickets 時，如果所有工作共用同一個
context，容易造成 ticket 之間的需求與修改互相混淆，也不容易確保每張 ticket 都有獨立的 review
與 commit 邊界。依賴關係、阻塞狀態與前一張 ticket 的驗證結果，也需要由外層協調後才能安全地進入
下一張 ticket。

因此，本專案提供一個 Codex host adapter，專門補上多工單情境的外層協調：每張 ticket 使用全新、
隔離的 worktree 與 branch，由獨立 subagent 完成自己的 Matt implement workflow；彼此獨立的 ticket
只有在 validated batch plan 的同一個 scheduler-approved、未阻塞 frontier 中才可以並行執行，再由主 agent
依 dependency order 整合回主線。它不修改或重新發佈 Matt 官方 skills；
單張 ticket 與尚未完成 Wayfinder 決策的 ticket 不套用此 adapter。

這是一個可由 Codex CLI 安裝的 Git marketplace，提供以下多工單 implement 協調行為：

- 每張 ticket 使用一個全新、隔離 worktree 與 branch 的 subagent context。
- 只有 validated batch plan 所回報的同一個 scheduler-approved、未阻塞 dependency frontier 中，彼此獨立的 ticket 才可以並行執行。
- 每張 ticket 自己完成 TDD、測試、雙軸 code review、修正與單一 commit。
- 主 agent 依 ticket dependency order 將完成的 branch merge 或 cherry-pick 回主線。
- 主 agent 處理 merge conflict、共用測試資源與整合測試，最後移除已整合的 worktree 與 branch。
- 單張 ticket 與尚未完成 Wayfinder 決策的 ticket 不套用 adapter。

### Batch activation, scheduling, and audit contract

Adapter activation does not imply concurrency. A request containing more than one approved implementation
ticket activates this adapter even when the dependency graph is a single linear chain. Before any worker
starts, the parent must create and persist a validated batch plan. The scheduler's current unblocked frontier
is the only legal start set; independent tickets may run in parallel only when they are in that same frontier.
Single-ticket requests stay in the current context, and unresolved Wayfinder decision tickets stay outside the
adapter.

Use the boundary CLI to inspect durable progress after every lifecycle transition:

```powershell
python "<plugin-root>\skills\implement-ticket-batch\scripts\ticket_boundary.py" status --state "<batch-state>"
python "<plugin-root>\skills\implement-ticket-batch\scripts\ticket_boundary.py" report --state "<batch-state>"
```

`status` reports the current frontier, every ticket state, unmet predecessors, and verification gates.
`report` emits dependency-ordered ticket, worker branch, start SHA, integrated commit, verification result,
and live Git worktree/branch evidence. A report never marks a batch complete when a ticket is unfinished,
failed, conflicted, or orphaned.

## 安裝需求

- Windows PowerShell。
- 支援 plugins、hooks 與 subagents 的新版 Codex CLI。
- Matt 的 `implement`、`tdd`、`code-review` skills 已安裝於
  `~/.agents/skills` 或 `~/.codex/skills`。
- 目標 repository 使用 Git，開始多工單執行前必須是 clean worktree。

## 安裝教學

### 從 GitHub 安裝

這個 plugin 使用 `GUOJIE526/matt-implement-adapter` repository，以下指令可直接貼上，
不需要替換任何文字：

```powershell
codex plugin marketplace add GUOJIE526/matt-implement-adapter --ref main
codex plugin add matt-implement-adapter@matt-adapter
```

以上安裝方式不需要另外安裝 `gh`，只要 Codex CLI 能透過 Git 存取 repository 即可。

也可以使用完整 HTTPS 或 SSH Git URL：

```powershell
codex plugin marketplace add https://github.com/GUOJIE526/matt-implement-adapter.git --ref main
codex plugin marketplace add git@github.com:GUOJIE526/matt-implement-adapter.git --ref main
```

私人 repository 必須先讓電腦上的 Git 能存取該 repository。

安裝後：

1. 在 Codex 中檢查並信任 `matt-implement-adapter` 的 SessionStart hook。
2. 開啟一個全新的 Codex task。
3. 不需要輸入 plugin 名稱；一個請求含多張 approved implementation tickets
   時會依 task 結構自動套用，但必須先建立 validated batch plan，再依 scheduler frontier 啟動 workers。

確認安裝狀態：

```powershell
codex plugin list | Select-String "matt-implement-adapter"
```

## 更新

上傳新版本後，在使用端執行：

```powershell
codex plugin marketplace upgrade matt-adapter
codex plugin add matt-implement-adapter@matt-adapter
```

接著開啟新的 Codex task。

升級時，batch state 是 scheduler 的 durable source of truth；resume 或 context compaction 後應重新載入
state 並查詢 `status`/`frontier`，不要從 conversation memory 重建進度。既有 pre-batch worker state 可用
`plan import`/`legacy import` 安全恢復；恢復時會驗證 repository、target branch、ticket identity、Git
ancestry 與明確 verification，完成前不得解鎖新的 dependent。此 ticket 不執行 release 或 marketplace upgrade。
