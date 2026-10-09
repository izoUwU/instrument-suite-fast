# Build Windows GUI bundles (no Python required on target PCs).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1            # both
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -Target suite
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -Target fluke
#
# Outputs (one folder per app -- distribute the whole folder or its zip):
#   dist\InstrumentSuite\InstrumentSuite.exe         (full multi-instrument suite)
#   dist\InstrumentSuiteFast\InstrumentSuiteFast.exe (fast multi-instrument, no mpl)
#   dist\FlukeFast\FlukeFast.exe                     (lean Fluke 8846A only)

param(
    [ValidateSet('all','suite','fast','fluke')]
    [string]$Target = 'all'
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Build-Bundle {
    param(
        [string]$Spec,
        [string]$OutDir,
        [string]$ExeName
    )
    Write-Host ""
    Write-Host "=== Building $OutDir from $Spec ===" -ForegroundColor Cyan
    python -m PyInstaller --noconfirm $Spec
    $exe = Join-Path $PSScriptRoot "dist\$OutDir\$ExeName"
    if (-not (Test-Path $exe)) {
        Write-Error "Build failed: $exe not found."
        exit 1
    }
    $folderSize = (Get-ChildItem (Split-Path $exe) -Recurse |
                   Measure-Object Length -Sum).Sum / 1MB
    Write-Host ("OK: {0}" -f $exe)
    Write-Host ("Bundle folder size: {0:N1} MB" -f $folderSize)
    $internal = Join-Path (Split-Path $exe) "_internal"
    if (-not (Test-Path (Join-Path $internal "python3.dll"))) {
        Write-Warning "Missing python3.dll in $internal -- exe may show 'Failed to load Python DLL'."
    }
    if (-not (Get-ChildItem $internal -Filter "python3*.dll" -File -ErrorAction SilentlyContinue |
              Where-Object { $_.Name -match '^python3\d+\.dll$' })) {
        Write-Warning "Missing python3X.dll in $internal -- exe may show 'Failed to load Python DLL'."
    }
    Write-Host "Ship the whole folder (exe + _internal), not the .exe alone."

    $zip = Join-Path $PSScriptRoot "dist\$OutDir.zip"
    Remove-Item $zip -ErrorAction SilentlyContinue
    Compress-Archive -Path "$PSScriptRoot\dist\$OutDir\*" -DestinationPath $zip
    Write-Host ("Created zip: {0}" -f $zip)
}

Write-Host "Installing build + runtime dependencies..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

Write-Host "Cleaning previous build artifacts..."
Remove-Item -Recurse -Force .\build, .\dist -ErrorAction SilentlyContinue

if ($Target -eq 'all' -or $Target -eq 'suite') {
    Build-Bundle -Spec 'instrument_suite.spec' `
                 -OutDir 'InstrumentSuite' `
                 -ExeName 'InstrumentSuite.exe'
}

if ($Target -eq 'all' -or $Target -eq 'fast') {
    Build-Bundle -Spec 'instrument_suite_fast.spec' `
                 -OutDir 'InstrumentSuiteFast' `
                 -ExeName 'InstrumentSuiteFast.exe'
}

if ($Target -eq 'all' -or $Target -eq 'fluke') {
    Build-Bundle -Spec 'fluke_fast.spec' `
                 -OutDir 'FlukeFast' `
                 -ExeName 'FlukeFast.exe'
}
