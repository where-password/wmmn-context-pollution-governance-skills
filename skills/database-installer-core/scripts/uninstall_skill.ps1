[CmdletBinding()]
param(
    [string]$CodexHome = "",
    [switch]$Json
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($CodexHome)) {
    if (-not [string]::IsNullOrWhiteSpace($env:CODEX_HOME)) { $CodexHome = $env:CODEX_HOME }
    elseif (-not [string]::IsNullOrWhiteSpace($HOME)) { $CodexHome = Join-Path $HOME ".codex" }
    else { throw "Cannot determine CODEX_HOME; pass -CodexHome explicitly." }
}
$SkillsRoot = Join-Path $CodexHome "skills"
$TargetRoot = Join-Path $SkillsRoot "database-installer-core"
$RuntimeItems = @("SKILL.md", "agents", "scripts", "references", "assets", "pyproject.toml", ".gitignore", "README.md", "vendor", "installed-manifest.json")
if (-not (Test-Path -LiteralPath $TargetRoot)) {
    $result = [ordered]@{ status = "not_installed"; target = $TargetRoot; preserved = @() }
    if ($Json) { $result | ConvertTo-Json -Depth 6 } else { $result | Format-List }
    exit 0
}
$resolvedSkills = (Resolve-Path -LiteralPath $SkillsRoot).Path
$resolvedTarget = (Resolve-Path -LiteralPath $TargetRoot).Path
if (-not $resolvedTarget.StartsWith($resolvedSkills, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to uninstall a path outside Codex skills root."
}
foreach ($item in $RuntimeItems) {
    $path = Join-Path $TargetRoot $item
    if (Test-Path -LiteralPath $path) {
        $resolved = (Resolve-Path -LiteralPath $path).Path
        if (-not $resolved.StartsWith($resolvedTarget, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove a path outside the installed Skill."
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
$remaining = @(Get-ChildItem -LiteralPath $TargetRoot -Force -ErrorAction SilentlyContinue)
if ($remaining.Count -eq 0) {
    Remove-Item -LiteralPath $TargetRoot -Force
}
$result = [ordered]@{
    status = "uninstalled"
    target = $TargetRoot
    preserved = @($remaining | ForEach-Object Name)
    note = "Default uninstall preserves workspaces, runtime data, databases, backups, config, and unknown user files."
}
if ($Json) { $result | ConvertTo-Json -Depth 6 } else { $result | Format-List }
exit 0
