#!/usr/bin/env pwsh
# deploy/Register-StallLedgerTask.ps1 — register the scheduled stall-ledger tick (D-16c C1).
#
# Spec: T-stalled-pr-has-no-detector msg-5744 §3. Run BY THE OPERATOR on the loop host, once, from
# the daemon checkout (docs/deploy.md → "Stall ledger tick"). Nothing in the loop calls this.
#
# The two durations below must equal HEARTBEAT_INTERVAL and T_LOCK_STALE in
# src/spirrow_mindwire/stall_ledger/timing.py. tests/test_stall_ledger_d16c.py reads them
# from this file and fails the build if they drift, or if the order
# T_TICK_MAX < T_LOCK_STALE < HEARTBEAT_INTERVAL stops holding.
#
#   RepetitionInterval = H            how often a tick fires
#   ExecutionTimeLimit = T_LOCK_STALE the scheduler kills a tick still running after this; the OS
#                                     then releases the ledger lock. Shorter than H, so a hung
#                                     tick is gone before the next one fires.

param(
    # The daemon checkout (never a working one — docs/deploy.md).
    [Parameter(Mandatory = $true)][string]$Checkout,
    [string[]]$Repo = @(),
    [string[]]$Project = @(),
    [string]$TaskName = 'mindwire-stall-ledger',
    # Print what would be registered and stop.
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$HeartbeatInterval = 'PT15M'
$ExecutionTimeLimit = 'PT14M'

$wrapper = Join-Path $Checkout 'deploy\run-stall-ledger-tick.ps1'
if (-not (Test-Path -LiteralPath $wrapper)) { throw "wrapper not found: $wrapper" }

# Task Scheduler does not resolve a bare `pwsh` the way a shell does: on a host with the Store build
# (C:\Program Files\WindowsApps\...\pwsh.exe) a bare name failed every launch with 0x80070002 and the
# tick wrote nothing at all (T-stalled-pr-has-no-detector msg-6307). Resolve the absolute path NOW,
# and refuse to register if there is none.
$pwshPath = (Get-Command pwsh -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
if (-not $pwshPath -or -not [System.IO.Path]::IsPathRooted($pwshPath)) {
    throw "could not resolve an absolute path for pwsh (got '$pwshPath'); not registering"
}

$argList = @('-NoProfile', '-File', "`"$wrapper`"")
if ($Repo.Count -gt 0) { $argList += @('-Repo', ($Repo -join ',')) }
if ($Project.Count -gt 0) { $argList += @('-Project', ($Project -join ',')) }
$argString = $argList -join ' '

$interval = [System.Xml.XmlConvert]::ToTimeSpan($HeartbeatInterval)
$limit = [System.Xml.XmlConvert]::ToTimeSpan($ExecutionTimeLimit)

if ($DryRun) {
    [pscustomobject]@{
        TaskName           = $TaskName
        Execute            = $pwshPath
        Arguments          = $argString
        RepetitionInterval = $HeartbeatInterval
        ExecutionTimeLimit = $ExecutionTimeLimit
        MultipleInstances  = 'IgnoreNew'
    } | Format-List
    return
}

$action = New-ScheduledTaskAction -Execute $pwshPath -Argument $argString -WorkingDirectory $Checkout
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval $interval
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit $limit `
    -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description 'MindWire stall ledger: one log-only heartbeat (D-16c)' | Out-Null
Write-Output "registered $TaskName ($pwshPath, every $HeartbeatInterval, killed after $ExecutionTimeLimit)"
