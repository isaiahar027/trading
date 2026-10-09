<#
.SYNOPSIS
  Runs the desk in the current (interactive) Windows session and restarts it after a crash.
.DESCRIPTION
  Loads environment variables from .env, then runs `desk run`. A clean exit (Ctrl+C, `desk kill` is not an
  exit) stops the loop. After a crash it waits and restarts, at most -MaxRestartsPerHour times; on restart
  the desk reconciles: any order that may have been clicked but not confirmed halts trading until you
  resolve it on the dashboard. GUI automation needs this to run in YOUR logged-in desktop session, not as
  a service.
#>
[CmdletBinding()]
param(
    [string]$Config = "config\desk.yaml",
    [int]$MaxRestartsPerHour = 5
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root

function Import-DotEnv([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    foreach ($line in Get-Content $Path) {
        $trimmed = $line.Trim()
        if ($trimmed -eq "" -or $trimmed.StartsWith("#")) { continue }
        $eq = $trimmed.IndexOf("=")
        if ($eq -lt 1) { continue }
        $name = $trimmed.Substring(0, $eq).Trim()
        $value = $trimmed.Substring($eq + 1).Trim().Trim('"')
        if ($value -ne "") { [Environment]::SetEnvironmentVariable($name, $value, "Process") }
    }
}

Import-DotEnv (Join-Path $root ".env")
$desk = Join-Path $root ".venv\Scripts\desk.exe"
if (-not (Test-Path $desk)) { throw "desk.exe not found; run scripts\windows\install.ps1 first." }

$restarts = New-Object System.Collections.Generic.List[datetime]
while ($true) {
    Write-Host "$(Get-Date -Format s) starting desk ($Config)" -ForegroundColor Cyan
    & $desk run -c $Config
    $code = $LASTEXITCODE
    if ($code -eq 0) { Write-Host "desk exited cleanly"; break }
    if ($code -eq 3) { Write-Host "another desk instance is already running; not restarting" -ForegroundColor Red; break }
    if ($code -eq 2) { Write-Host "configuration error; fix config and start again" -ForegroundColor Red; break }
    $now = Get-Date
    $restarts.Add($now)
    $recent = @($restarts | Where-Object { $_ -gt $now.AddHours(-1) })
    if ($recent.Count -gt $MaxRestartsPerHour) {
        Write-Host "desk crashed $($recent.Count) times in an hour; giving up (check data\logs\desk.log)" -ForegroundColor Red
        break
    }
    $wait = [Math]::Min(300, 15 * [Math]::Pow(2, $recent.Count - 1))
    Write-Host "desk exited with code $code; restarting in $wait s" -ForegroundColor Yellow
    Start-Sleep -Seconds $wait
}
