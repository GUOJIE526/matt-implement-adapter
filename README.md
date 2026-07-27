# Matt Implement Adapter Marketplace

這是一個可由 Codex CLI 安裝的 Git marketplace。它不修改或重新發佈 Matt
官方 skills，只替 Codex 補上多工單 implement 的外層協調：

- 一次只執行一張 approved implementation ticket。
- 每張 ticket 使用一個全新、串行的 subagent context。
- 每張 ticket 自己完成 TDD、測試、雙軸 code review、修正與單一 commit。
- 前一張 ticket 通過 Git boundary 驗證後，才開始下一張。
- 單張 ticket 與尚未完成 Wayfinder 決策的 ticket 不套用 adapter。

## 需求

- Windows PowerShell。
- 支援 plugins、hooks 與 subagents 的新版 Codex CLI。
- Matt 的 `implement`、`tdd`、`code-review` skills 已安裝於
  `~/.agents/skills` 或 `~/.codex/skills`。
- 目標 repository 使用 Git，開始多工單執行前必須是 clean worktree。

## 從 GitHub 安裝

將 `<owner>/<repo>` 換成上傳後的 GitHub repository，例如
`my-account/matt-implement-adapter-marketplace`：

```powershell
codex plugin marketplace add <owner>/<repo> --ref main
codex plugin add matt-implement-adapter@matt-adapter
```

也可以使用完整 HTTPS 或 SSH Git URL：

```powershell
codex plugin marketplace add https://github.com/<owner>/<repo>.git --ref main
codex plugin marketplace add git@github.com:<owner>/<repo>.git --ref main
```

私人 repository 必須先讓公司電腦上的 Git 能存取該 repository。

安裝後：

1. 在 Codex 中檢查並信任 `matt-implement-adapter` 的 SessionStart hook。
2. 開啟一個全新的 Codex task。
3. 不需要輸入 plugin 名稱；一個請求含多張 approved implementation tickets
   時會依 task 結構自動套用。

確認安裝狀態：

```powershell
codex plugin list | Select-String "matt-implement-adapter"
```

## 從 clone 的本機目錄安裝

```powershell
git clone https://github.com/<owner>/<repo>.git
Set-Location <repo>
.\install.ps1
```

`install.ps1` 會先檢查 Codex 與必要的 Matt skills，再加入本機 marketplace
並安裝 plugin。

## 更新

上傳新版本後，在使用端執行：

```powershell
codex plugin marketplace upgrade matt-adapter
codex plugin add matt-implement-adapter@matt-adapter
```

接著開啟新的 Codex task。

## 公司環境注意事項

公司管理政策若啟用 `allow_managed_hooks_only`，Codex 會略過 plugin 內附的
SessionStart hook；此時即使 plugin 顯示 installed，adapter 也不會自動注入。
需要由公司 Codex 管理者允許 plugin hooks。

