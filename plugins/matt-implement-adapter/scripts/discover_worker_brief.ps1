[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Repo,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string[]]$Ticket
)

$helper = Join-Path -Path $PSScriptRoot -ChildPath "implementation_brief.py"
if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
    Write-Error "找不到 worker brief discovery helper：$helper"
    exit 2
}

$arguments = @($helper, "discover", "--repo", $Repo)
foreach ($ticketReference in $Ticket) {
    $arguments += @("--ticket", $ticketReference)
}

& python @arguments
$exitCode = $LASTEXITCODE
if ($null -eq $exitCode) {
    $exitCode = 1
}

exit $exitCode
