<#
.SYNOPSIS
  Installs the Among Us Trading Desk on the Windows VPS that runs UniTrader.
.DESCRIPTION
  Creates .venv, installs the desk with the Windows GUI backend, and copies the example config if you
  do not have one yet. It never asks for, or stores, your UniTrader credentials.
  Run from the project folder:  powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
#>
[CmdletBinding()]
param(
    [string]$Python = "py"
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root

Write-Host "== Among Us Trading Desk: install ==" -ForegroundColor Cyan
& $Python -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.11 or newer is required (install it from python.org, tick 'Add to PATH')." }

if (-not (Test-Path ".venv")) {
    & $Python -3 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[windows]"

if (-not (Test-Path "config\desk.yaml")) {
    Copy-Item "config\desk.example.yaml" "config\desk.yaml"
    Write-Host "Created config\desk.yaml from the example (mode: paper)." -ForegroundColor Yellow
}
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example; fill in the keys you use." -ForegroundColor Yellow
}

# .env holds API keys; data\ holds the dashboard token, the database and screenshots of your terminal.
# Make both readable by your Windows account only (other accounts on a shared VPS could read them).
$account = "$env:USERDOMAIN\$env:USERNAME"
New-Item -ItemType Directory -Force -Path "data" | Out-Null
foreach ($item in @(@{ Path = ".env"; Grant = "${account}:F" }, @{ Path = "data"; Grant = "${account}:(OI)(CI)F" })) {
    & icacls $item.Path /inheritance:r /grant:r $item.Grant | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning "could not restrict $($item.Path) to $account; other accounts may read it" }
}

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Open UniTrader and log in to your DEMO account yourself (the desk never handles your password)."
Write-Host "  2. Copy each instrument's contract size / lot step / min lot from UniTrader's symbol specification"
Write-Host "     into config\desk.yaml and set specs_verified: true."
Write-Host "  3. .\.venv\Scripts\desk.exe calibrate -c config\desk.yaml"
Write-Host "  4. Set mode: demo, execution.venue: unitrader and feed.kind: twelvedata (TWELVEDATA_API_KEY in .env),"
Write-Host "     then: .\.venv\Scripts\desk.exe preflight -c config\desk.yaml"
Write-Host "  5. First day: execution.dry_run: true. Then:"
Write-Host "     powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\run-desk.ps1"
