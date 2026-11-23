param(
    [string]$Python = "",
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$SkillRoot = Split-Path -Parent $PSScriptRoot
$VendorRoot = Join-Path $SkillRoot "vendor"
$VendorCore = Join-Path $VendorRoot "memory_core"
$Migrations = Join-Path $VendorRoot "migrations"
$DbSkill = Join-Path $PSScriptRoot "db_skill.py"

if (-not (Test-Path -LiteralPath $VendorCore) -or -not (Test-Path -LiteralPath $Migrations)) {
    throw "Packaged vendor/memory_core or vendor/migrations is missing."
}
if ((Get-ChildItem -LiteralPath $Migrations -File -Filter '*.sql').Count -eq 0) {
    throw "Packaged vendor/migrations contains no SQL migrations."
}
if ([string]::IsNullOrWhiteSpace($Python)) {
    if (-not [string]::IsNullOrWhiteSpace($env:MEMORY_DB_PYTHON)) {
        $Python = $env:MEMORY_DB_PYTHON
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $Python = "py"
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $Python = (Get-Command python).Source
    } else {
        throw "Python 3.11-3.13 is required. Run scripts/bootstrap_runtime.ps1 first."
    }
}

$oldPythonPath = $env:PYTHONPATH
$oldMigrationPath = $env:MEMORY_CORE_MIGRATIONS_DIR
$oldCoreSrc = $env:MEMORY_CORE_SRC
$env:PYTHONPATH = $VendorRoot + ";" + $PSScriptRoot + $(if ([string]::IsNullOrWhiteSpace($oldPythonPath)) { "" } else { ";" + $oldPythonPath })
$env:MEMORY_CORE_MIGRATIONS_DIR = $Migrations
$env:MEMORY_CORE_SRC = ""
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONUTF8 = "1"
try {
    if ($Python -eq "py") {
        & py -3.13 (Join-Path $PSScriptRoot "db_skill.py") self-test --json
        if ($LASTEXITCODE -ne 0) { & py -3.12 (Join-Path $PSScriptRoot "db_skill.py") self-test --json }
    } else {
        & $Python $DbSkill self-test --json
    }
    $exitCode = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $oldPythonPath
    $env:MEMORY_CORE_MIGRATIONS_DIR = $oldMigrationPath
    $env:MEMORY_CORE_SRC = $oldCoreSrc
}
exit $exitCode
