[CmdletBinding()]
param(
    [string]$CodexHome = "",
    [switch]$Force,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($CodexHome)) {
    if (-not [string]::IsNullOrWhiteSpace($env:CODEX_HOME)) { $CodexHome = $env:CODEX_HOME }
    elseif (-not [string]::IsNullOrWhiteSpace($HOME)) { $CodexHome = Join-Path $HOME ".codex" }
    else { throw "Cannot determine CODEX_HOME; pass -CodexHome explicitly." }
}
$SourceRoot = Split-Path -Parent $PSScriptRoot
$SkillsRoot = Join-Path $CodexHome "skills"
$TargetRoot = Join-Path $SkillsRoot "database-installer-core"
$StageRoot = Join-Path $SkillsRoot (".database-installer-core.stage-" + [guid]::NewGuid().ToString("N"))
$BackupRoot = Join-Path $SkillsRoot ("database-installer-core.backup-" + (Get-Date -Format "yyyyMMddTHHmmss"))
$RuntimeItems = @("SKILL.md", "agents", "scripts", "references", "assets", "vendor", "pyproject.toml", ".gitignore", "README.md")
$ExcludedItems = @("work", "tests", ".runtime", "workspace", "data", "backups", "config", ".venv", ".idea", "uv.lock", ".milvus")

New-Item -ItemType Directory -Force $SkillsRoot | Out-Null
if (Test-Path $TargetRoot) {
    if (-not $Force) {
        $result = [ordered]@{
            status = "blocked"
            error = "target already exists; default install never overwrites"
            target = $TargetRoot
            next_step = "Remove or rename the existing Skill explicitly, or pass -Force after reviewing the target."
        }
        if ($Json) { $result | ConvertTo-Json -Depth 6 } else { $result | Format-List }
        exit 2
    }
    Move-Item -LiteralPath $TargetRoot -Destination $BackupRoot
}
try {
    New-Item -ItemType Directory -Force $StageRoot | Out-Null
    $copied = @()
    foreach ($item in $RuntimeItems) {
        $source = Join-Path $SourceRoot $item
        if (-not (Test-Path -LiteralPath $source)) { continue }
        $destination = Join-Path $StageRoot $item
        Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force
        $copied += $item
    }
    $metadata = [ordered]@{
        installed_at = (Get-Date).ToUniversalTime().ToString("o")
        source_root = $SourceRoot
        runtime_items = $copied
        excluded_items = $ExcludedItems
        note = "Runtime-only install; no databases, secrets, tests, work reports, or .env files were copied."
    }
    $metadata | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 (Join-Path $StageRoot "installed-manifest.json")
    Move-Item -LiteralPath $StageRoot -Destination $TargetRoot
    $result = [ordered]@{
        status = "installed"
        target = $TargetRoot
        copied = $copied
        excluded = $ExcludedItems
        backup_of_previous = if (Test-Path $BackupRoot) { $BackupRoot } else { $null }
    }
    if ($Json) { $result | ConvertTo-Json -Depth 6 } else { $result | Format-List }
    exit 0
} catch {
    if (Test-Path $StageRoot) { Remove-Item -LiteralPath $StageRoot -Recurse -Force }
    if (Test-Path $BackupRoot -and -not (Test-Path $TargetRoot)) { Move-Item -LiteralPath $BackupRoot -Destination $TargetRoot }
    $result = [ordered]@{ status = "error"; error = $_.Exception.Message; target = $TargetRoot }
    if ($Json) { $result | ConvertTo-Json -Depth 6 } else { $result | Format-List }
    exit 5
}
