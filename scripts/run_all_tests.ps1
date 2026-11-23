$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$env:PYTHONUTF8 = "1"
New-Item -ItemType Directory -Force (Join-Path $Root "work") | Out-Null
$TestRoot = Join-Path (Split-Path -Parent $Root) "test-temp"
New-Item -ItemType Directory -Force $TestRoot | Out-Null
$MemoryTemp = Join-Path $TestRoot ("pytest-memory-" + [guid]::NewGuid().ToString("N"))
$DbTemp = Join-Path $TestRoot ("pytest-db-" + [guid]::NewGuid().ToString("N"))
Push-Location (Join-Path $Root "memory_core_v1")
try { $env:PYTHONPATH = "src"; & $Python -m pytest -q -o addopts="" --basetemp $MemoryTemp tests; if ($LASTEXITCODE -ne 0) { throw "Memory Core tests failed" } } finally { Pop-Location }
Push-Location (Join-Path $Root "skills/conversation-context-cleaner")
try { $env:PYTHONPATH = (Join-Path $Root "memory_core_v1/src"); & $Python -m unittest discover -s tests -p "test_*.py" -q; if ($LASTEXITCODE -ne 0) { throw "Cleaner tests failed" } } finally { Pop-Location }
Push-Location (Join-Path $Root "skills/database-installer-core")
try { $env:PYTHONPATH = (Join-Path $PWD "scripts") + [IO.Path]::PathSeparator + (Join-Path $PWD "vendor"); $env:MEMORY_CORE_SRC = ""; & $Python -m pytest -q -o addopts="" --basetemp $DbTemp tests; if ($LASTEXITCODE -ne 0) { throw "Database Skill tests failed" } } finally { Pop-Location }
& $Python (Join-Path $PSScriptRoot "validate_skills.py") $Root
if ($LASTEXITCODE -ne 0) { throw "Skill validation failed" }
Write-Host "ALL_TESTS_PASS"
