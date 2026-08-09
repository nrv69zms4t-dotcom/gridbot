# build.ps1 — build GridBot.exe (PyInstaller onefile) and GridBot-Setup.exe (Inno Setup).
#
# Usage (from any cwd, Windows PowerShell 5.1+ or pwsh):
#   powershell -ExecutionPolicy Bypass -File gridbot/packaging/build.ps1
#   powershell -ExecutionPolicy Bypass -File gridbot/packaging/build.ps1 -SkipInstaller
#
# Exits with a non-zero code on any failed step.

[CmdletBinding()]
param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- paths ----
$PackagingDir = $PSScriptRoot                              # <repo>/gridbot/packaging
$RepoRoot     = Split-Path -Parent (Split-Path -Parent $PackagingDir)
$DistDir      = Join-Path $PackagingDir 'dist'
$WorkDir      = Join-Path $PackagingDir 'build'
$IconPath     = Join-Path $PackagingDir 'gridbot.ico'
$SpecPath     = Join-Path $PackagingDir 'gridbot_app.spec'
$IssPath      = Join-Path $PackagingDir 'installer.iss'
$ExePath      = Join-Path $DistDir 'GridBot.exe'
$SetupPath    = Join-Path $DistDir 'GridBot-Setup.exe'

function Fail([string]$Message) {
    Write-Host "BUILD FAILED: $Message" -ForegroundColor Red
    exit 1
}

Write-Host "Repo root : $RepoRoot"
Write-Host "Packaging : $PackagingDir"

# ------------------------------------------------- step 1: icon (if missing) ----
if (-not (Test-Path $IconPath)) {
    Write-Host "[1/4] gridbot.ico not found - generating with make_icon.py ..."
    & python (Join-Path $PackagingDir 'make_icon.py')
    if ($LASTEXITCODE -ne 0) { Fail "make_icon.py exited with code $LASTEXITCODE" }
    if (-not (Test-Path $IconPath)) { Fail "make_icon.py finished but $IconPath does not exist" }
} else {
    Write-Host "[1/4] Icon already present: $IconPath"
}

# ------------------------------------------------- step 2: PyInstaller ----
Write-Host "[2/4] Running PyInstaller (onefile, windowed) ..."
Push-Location $RepoRoot
try {
    & python -m PyInstaller 'gridbot/packaging/gridbot_app.spec' --noconfirm `
        --distpath 'gridbot/packaging/dist' --workpath 'gridbot/packaging/build'
    $pyiExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($pyiExit -ne 0) { Fail "PyInstaller exited with code $pyiExit" }

# ------------------------------------------------- step 3: verify exe ----
if (-not (Test-Path $ExePath)) { Fail "expected output not found: $ExePath" }
$exeSizeMB = [math]::Round((Get-Item $ExePath).Length / 1MB, 1)
Write-Host "[3/4] OK: $ExePath ($exeSizeMB MB)" -ForegroundColor Green

# ------------------------------------------------- step 4: installer ----
if ($SkipInstaller) {
    Write-Host "[4/4] -SkipInstaller set - skipping Inno Setup." -ForegroundColor Yellow
    Write-Host "DONE. Portable exe: $ExePath" -ForegroundColor Green
    exit 0
}

# Locate ISCC.exe: known install path first, then common locations, then PATH.
$IsccCandidates = @('C:\Users\kiric\AppData\Local\Programs\Inno Setup 6\ISCC.exe')
if ($env:LOCALAPPDATA)        { $IsccCandidates += (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe') }
if (${env:ProgramFiles(x86)}) { $IsccCandidates += (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe') }
if ($env:ProgramFiles)        { $IsccCandidates += (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe') }
$Iscc = $null
foreach ($cand in $IsccCandidates) {
    if ($cand -and (Test-Path $cand)) { $Iscc = $cand; break }
}
if (-not $Iscc) {
    $cmd = Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue
    if ($cmd) { $Iscc = $cmd.Source }
}

if (-not $Iscc) {
    # Installer was explicitly requested (no -SkipInstaller): a stale
    # GridBot-Setup.exe must not survive to masquerade as a fresh build.
    if (Test-Path $SetupPath) { Remove-Item $SetupPath -Force }
    Fail "ISCC.exe (Inno Setup 6) not found - install Inno Setup 6 or re-run with -SkipInstaller"
}

Write-Host "[4/4] Compiling installer with: $Iscc"
& $Iscc $IssPath
if ($LASTEXITCODE -ne 0) { Fail "ISCC exited with code $LASTEXITCODE" }
if (-not (Test-Path $SetupPath)) { Fail "expected installer not found: $SetupPath" }
$setupSizeMB = [math]::Round((Get-Item $SetupPath).Length / 1MB, 1)

Write-Host "DONE." -ForegroundColor Green
Write-Host "  Portable exe : $ExePath ($exeSizeMB MB)"
Write-Host "  Installer    : $SetupPath ($setupSizeMB MB)"
exit 0
