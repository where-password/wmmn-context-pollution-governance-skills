[CmdletBinding()]
param(
    [string]$WorkspaceRoot = "",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

$ErrorActionPreference = "Stop"
$SkillRoot = Split-Path -Parent $PSScriptRoot
$DbSkill = Join-Path $PSScriptRoot "db_skill.py"
$RuntimePython = if ([string]::IsNullOrWhiteSpace($WorkspaceRoot)) { "" } else { Join-Path $WorkspaceRoot ".runtime\venv\Scripts\python.exe" }

if (-not [string]::IsNullOrWhiteSpace($WorkspaceRoot) -and (Test-Path $RuntimePython)) {
    $Python = $RuntimePython
} elseif (-not [string]::IsNullOrWhiteSpace($env:MEMORY_DB_PYTHON)) {
    $Python = $env:MEMORY_DB_PYTHON
} else {
    $Command = Get-Command python -ErrorAction SilentlyContinue
    if (-not $Command) {
        & (Join-Path $PSScriptRoot "bootstrap_runtime.ps1") -WorkspaceRoot $WorkspaceRoot
        exit 2
    }
    $Python = $Command.Source
}

& $Python $DbSkill @CliArgs
exit $LASTEXITCODE
