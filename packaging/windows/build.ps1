# Build PyBrowser for Windows: a --onedir PyInstaller bundle, then (if Inno
# Setup's iscc.exe is on PATH) a proper installer .exe.
#
# Run from a Windows machine, in a fresh venv, from the repository root:
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
#
# This script has NOT been run on a real Windows machine as part of this
# change - it was written from inspecting the app's dependencies and entry
# point, not verified end-to-end. Treat a first real run as a test of the
# script itself; see packaging/windows/README.md for what to check.

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

Write-Host "== Installing build dependencies ==" -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install "pyinstaller>=6.0" "pyinstaller-hooks-contrib>=2024.0"

Write-Host "== Cleaning previous build output ==" -ForegroundColor Cyan
Remove-Item -Recurse -Force "build", "dist" -ErrorAction SilentlyContinue

Write-Host "== Running PyInstaller ==" -ForegroundColor Cyan
pyinstaller packaging/windows/pybrowser.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "== dist\PyBrowser built ==" -ForegroundColor Green
Get-ChildItem "dist\PyBrowser\PyBrowser.exe" | Format-List

$Version = (python -c "from app import __version__; print(__version__)").Trim()
Write-Host "== Building version $Version ==" -ForegroundColor Cyan

$iscc = Get-Command "iscc.exe" -ErrorAction SilentlyContinue
if ($iscc) {
    Write-Host "== Building installer with Inno Setup ==" -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path "packaging\windows\output" | Out-Null
    & $iscc.Path "/DMyAppVersion=$Version" "packaging\windows\installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup compile failed" }
    Write-Host "== Installer written to packaging\windows\output\ ==" -ForegroundColor Green
} else {
    Write-Host "iscc.exe (Inno Setup) not found on PATH - skipping installer." -ForegroundColor Yellow
    Write-Host "Install Inno Setup 6 (https://jrsoftware.org/isinfo.php) to build PyBrowserSetup-<version>.exe." -ForegroundColor Yellow
    Write-Host "The --onedir build in dist\PyBrowser\ is still a runnable app on its own." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Next: work through packaging/windows/README.md's first-run checklist" -ForegroundColor Cyan
Write-Host "against dist\PyBrowser\PyBrowser.exe (or the installed copy) before" -ForegroundColor Cyan
Write-Host "calling this build real." -ForegroundColor Cyan
