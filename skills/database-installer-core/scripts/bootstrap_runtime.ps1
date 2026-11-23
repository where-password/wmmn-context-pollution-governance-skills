[CmdletBinding()]
param(
    [string]$WorkspaceRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) "workspace"),
    [string]$PythonExe = "",
    [string]$MemoryCoreSource = "",
    [switch]$InstallPython,
    [switch]$Apply,
    [switch]$Json,
    [string]$PythonPackageId = "Python.Python.3.13"
)

$ErrorActionPreference = "Stop"
$SkillRoot = Split-Path -Parent $PSScriptRoot
$LockFile = Join-Path $SkillRoot "assets\runtime\requirements-runtime.lock"
$RuntimeRoot = Join-Path $WorkspaceRoot ".runtime"
$VenvRoot = Join-Path $RuntimeRoot "venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"

function Get-PythonVersion([string]$Executable) {
    if ([string]::IsNullOrWhiteSpace($Executable)) { return $null }
    try {
        $value = & $Executable -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return [version]($value.Trim())
    } catch {
        return $null
    }
}

function Find-SupportedPython {
    param([string]$Preferred)
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($Preferred)) { $candidates += $Preferred }
    if (-not [string]::IsNullOrWhiteSpace($env:MEMORY_DB_PYTHON)) { $candidates += $env:MEMORY_DB_PYTHON }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($version in @("3.13", "3.12", "3.11")) {
            $candidates += @{ Launcher = $true; Version = $version }
        }
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { $candidates += $python.Source }
    $known = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:ProgramFiles "Python313\python.exe"),
        (Join-Path $env:ProgramFiles "Python312\python.exe"),
        (Join-Path $env:ProgramFiles "Python311\python.exe")
    )
    $candidates += $known
    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        if ($candidate -is [hashtable]) {
            try {
                $exe = & py "-$($candidate.Version)" -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0) { $candidate = $exe.Trim() } else { continue }
            } catch { continue }
        }
        $version = Get-PythonVersion $candidate
        if ($null -ne $version -and $version -ge [version]"3.11" -and $version -lt [version]"3.14") {
            return @{ Path = $candidate; Version = $version.ToString(2) }
        }
    }
    return $null
}

function Test-VenvReady([string]$VenvPython, [string]$MemoryCoreSource) {
    if (-not (Test-Path $VenvPython)) { return $false }
    try {
        $code = "import pydantic, memory_core.skill_api"
        & $VenvPython -c $code 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

$selected = Find-SupportedPython $PythonExe
$plan = [ordered]@{
    status = "repair_required"
    workspace_root = [System.IO.Path]::GetFullPath($WorkspaceRoot)
    venv_root = [System.IO.Path]::GetFullPath($VenvRoot)
    requirements_lock = [System.IO.Path]::GetFullPath($LockFile)
    python = if ($selected) { $selected } else { $null }
    python_install_requested = [bool]$InstallPython
    actions = @()
    next_steps = @()
    notes = @(
        "Without -Apply this script only returns a plan and changes nothing.",
        "V1 never installs Docker, connects to Milvus, or changes production aliases.",
        "UAC, security software, BIOS, and restart steps require a separate user action."
    )
}

if (-not $selected) {
    $installCommand = @("winget", "install", "--id", $PythonPackageId, "-e", "--scope", "user", "--accept-source-agreements", "--accept-package-agreements")
    $plan.actions += [ordered]@{
        action_id = "install_supported_python"
        required = $true
        requires_user_confirmation = $true
        summary = "Install Python 3.11-3.13 only after explicit -InstallPython confirmation."
        command = $installCommand
        manual_url = "https://www.python.org/downloads/windows/"
    }
    if ($InstallPython -and $Apply) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if (-not $winget) {
            $plan.status = "blocked"
            $plan.next_steps += "winget is unavailable. Install Python from the official URL or provide a controlled portable Python path with -PythonExe."
            $plan | ConvertTo-Json -Depth 8
            exit 2
        }
        & $winget.Source @installCommand | Out-Host
        if ($LASTEXITCODE -ne 0) {
            $plan.status = "blocked"
            $plan.next_steps += "Python installer returned a non-zero exit code. Complete the official installer manually, then rerun bootstrap without -InstallPython."
            $plan | ConvertTo-Json -Depth 8
            exit 5
        }
        $selected = Find-SupportedPython $PythonExe
        $plan.python = if ($selected) { $selected } else { $null }
        if (-not $selected) {
            $plan.status = "blocked"
            $plan.next_steps += "Python was not discoverable after installation. Open a new terminal or add the installed Python directory to PATH; no PATH change was made by this script."
            $plan | ConvertTo-Json -Depth 8
            exit 2
        }
    } else {
        $plan | ConvertTo-Json -Depth 8
        exit 2
    }
}

if ($selected) {
    $venvReady = Test-VenvReady $VenvPython $MemoryCoreSource
    if (-not $venvReady) {
        $plan.actions += [ordered]@{
            action_id = "create_isolated_runtime"
            required = $true
            requires_user_confirmation = $true
            summary = "Create a Skill-owned virtual environment."
            command = @($selected.Path, "-m", "venv", $VenvRoot)
        }
        $plan.actions += [ordered]@{
            action_id = "install_locked_dependencies"
            required = $true
            requires_user_confirmation = $true
            summary = "Install exact versions from the runtime lock into the isolated venv."
            command = @($VenvPython, "-m", "pip", "install", "--requirement", $LockFile)
        }
    }
    if ([string]::IsNullOrWhiteSpace($MemoryCoreSource)) {
        $plan.notes += "Pass -MemoryCoreSource pointing to a wheel or editable memory_core source before zero-basis installation."
    } elseif (-not $venvReady) {
        $plan.actions += [ordered]@{
            action_id = "install_memory_core"
            required = $true
            requires_user_confirmation = $true
            summary = "Install memory_core into the isolated venv without changing system Python."
            command = @($VenvPython, "-m", "pip", "install", "--no-deps", "-e", $MemoryCoreSource)
        }
    }
    if ($venvReady -and -not [string]::IsNullOrWhiteSpace($MemoryCoreSource)) {
        $plan.status = "ready"
    }
}

if (-not $Apply) {
    $plan | ConvertTo-Json -Depth 8
    exit 0
}

if ($plan.status -eq "ready") {
    $plan | ConvertTo-Json -Depth 8
    exit 0
}

try {
    New-Item -ItemType Directory -Force $RuntimeRoot | Out-Null
    if (-not (Test-Path $VenvPython)) {
        & $selected.Path -m venv $VenvRoot
        if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
    }
    & $VenvPython -m pip install --disable-pip-version-check --requirement $LockFile
    if ($LASTEXITCODE -ne 0) { throw "locked dependency installation failed" }
    if (-not [string]::IsNullOrWhiteSpace($MemoryCoreSource)) {
        & $VenvPython -m pip install --disable-pip-version-check --no-deps -e $MemoryCoreSource
        if ($LASTEXITCODE -ne 0) { throw "memory_core installation failed" }
    }
    $plan.status = "ready"
    $plan.python = $selected
    $plan | ConvertTo-Json -Depth 8
    exit 0
} catch {
    $plan.status = "blocked"
    $plan | Add-Member -NotePropertyName error -NotePropertyValue $_.Exception.Message -Force
    $plan.next_steps += "Review the failed step and retry only after resolving the specific installer or dependency error."
    $plan | ConvertTo-Json -Depth 8
    exit 5
}
