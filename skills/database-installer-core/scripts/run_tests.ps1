param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$StartTest = Join-Path $PSScriptRoot "Start-SQLite-Test.ps1"
if (-not (Test-Path -LiteralPath $StartTest)) {
    throw "Start-SQLite-Test.ps1 is missing from the package."
}
& $StartTest -Python $Python
exit $LASTEXITCODE
