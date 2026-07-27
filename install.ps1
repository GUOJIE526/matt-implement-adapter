[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$marketplaceFile = Join-Path $repositoryRoot ".agents\plugins\marketplace.json"
$marketplaceName = "matt-adapter"
$pluginName = "matt-implement-adapter"

if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    throw "找不到 codex CLI。請先安裝或更新 Codex。"
}

if (-not (Test-Path -LiteralPath $marketplaceFile -PathType Leaf)) {
    throw "找不到 marketplace.json：$marketplaceFile"
}

$userProfile = [Environment]::GetFolderPath("UserProfile")
$skillRoots = @(
    (Join-Path $userProfile ".agents\skills"),
    (Join-Path $userProfile ".codex\skills")
)
$requiredSkills = @("implement", "tdd", "code-review")
$missingSkills = @(
    foreach ($skill in $requiredSkills) {
        $found = $false
        foreach ($root in $skillRoots) {
            if (Test-Path -LiteralPath (Join-Path $root "$skill\SKILL.md") -PathType Leaf) {
                $found = $true
                break
            }
        }
        if (-not $found) {
            $skill
        }
    }
)

if ($missingSkills.Count -gt 0) {
    throw "缺少必要的 Matt skills：$($missingSkills -join ', ')。請先安裝官方 skills。"
}

$marketplaces = (& codex plugin marketplace list --json | ConvertFrom-Json).marketplaces
$configured = $marketplaces | Where-Object { $_.name -eq $marketplaceName }

if (-not $configured) {
    & codex plugin marketplace add $repositoryRoot
    if ($LASTEXITCODE -ne 0) {
        throw "加入 marketplace 失敗。"
    }
}

& codex plugin add "$pluginName@$marketplaceName"
if ($LASTEXITCODE -ne 0) {
    throw "安裝 plugin 失敗。"
}

Write-Host ""
Write-Host "已安裝 $pluginName@$marketplaceName。"
Write-Host "請在 Codex 中信任它的 SessionStart hook，然後開啟新的 task。"

