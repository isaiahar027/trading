<#
.SYNOPSIS
  Registers a Task Scheduler task that starts the desk when YOU log on to Windows.
.DESCRIPTION
  The task runs in your interactive session (LogonType Interactive). That is required: a task that runs
  "whether the user is logged on or not" has no desktop, so it could not see or click UniTrader.
  Unregister with:  Unregister-ScheduledTask -TaskName AmongUsDesk -Confirm:$false
#>
[CmdletBinding()]
param(
    [string]$TaskName = "AmongUsDesk",
    [string]$Config = "config\desk.yaml"
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$script = Join-Path $root "scripts\windows\run-desk.ps1"
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Config `"$Config`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Force | Out-Null
Write-Host "Registered '$TaskName': starts the desk at your logon. You still log in to UniTrader yourself."
