[CmdletBinding()]
param([string]$CodexHome = "", [switch]$Json)
$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($CodexHome)) {
    if ($env:CODEX_HOME) { $CodexHome = $env:CODEX_HOME }
    elseif ($HOME) { $CodexHome = Join-Path $HOME ".codex" }
    else { throw "Pass -CodexHome explicitly." }
}
$SourceRoot = Split-Path -Parent $PSScriptRoot
$SkillsRoot = Join-Path $CodexHome "skills"
$TargetRoot = Join-Path $SkillsRoot "conversation-context-cleaner"
$StageRoot = Join-Path $SkillsRoot (".conversation-context-cleaner.stage-" + [guid]::NewGuid().ToString("N"))
$RuntimeItems = @("SKILL.md", "agents", "scripts", "references", "assets", "pyproject.toml", ".gitignore", "README.md")
New-Item -ItemType Directory -Force $SkillsRoot | Out-Null
if (Test-Path -LiteralPath $TargetRoot) {
    @{ status = "blocked"; error = "target already exists; remove or rename it explicitly"; target = $TargetRoot } | ConvertTo-Json -Depth 4
    exit 2
}
New-Item -ItemType Directory -Force $StageRoot | Out-Null
foreach ($item in $RuntimeItems) {
    $source = Join-Path $SourceRoot $item
    if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination (Join-Path $StageRoot $item) -Recurse -Force }
}
Move-Item -LiteralPath $StageRoot -Destination $TargetRoot
@{ status = "installed"; target = $TargetRoot; runtime_items = $RuntimeItems } | ConvertTo-Json -Depth 4
