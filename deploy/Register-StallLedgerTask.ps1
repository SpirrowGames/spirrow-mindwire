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
    # pythonw.exe of a base (non-venv) interpreter, to run deploy\headless_launch.py. Default: the
    # interpreter `uv python find --system` names, with pythonw.exe beside it.
    [string]$Pythonw = '',
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
# SilentlyContinue (not Stop) so a missing pwsh reaches the explicit refusal below rather than a
# generic CommandNotFoundException (PR #451 gate advisory).
$pwshPath = (Get-Command pwsh -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $pwshPath -or -not [System.IO.Path]::IsPathRooted($pwshPath)) {
    throw "could not resolve an absolute path for pwsh (got '$pwshPath'); not registering"
}

# The task runs pwsh through deploy\headless_launch.py under pythonw. Launched directly, pwsh opens a
# console window on every tick of an Interactive-logon task, and that window takes keyboard focus
# from whatever the operator is typing into (2026-11-13). `conhost.exe --headless` hid the window but
# made the ExecutionTimeLimit stop only conhost, leaving a hung pwsh running (PR #576 gate,
# msg-9268; measured by tests/Test-StallLedgerTaskHeadless.ps1). The launcher has no window, starts
# pwsh with CREATE_NO_WINDOW, and terminates pwsh itself at $launcherLimitSeconds, which is a little
# below the task limit so the task limit stays an outer backstop. The full reasoning is in the
# launcher's docstring.
if (-not $Pythonw) {
    $basePython = (& uv python find --system 2>$null | Select-Object -First 1)
    if ($basePython) { $Pythonw = Join-Path (Split-Path -Parent "$basePython".Trim()) 'pythonw.exe' }
}
if (-not $Pythonw -or -not [System.IO.Path]::IsPathRooted($Pythonw) -or
    -not (Test-Path -LiteralPath $Pythonw -PathType Leaf)) {
    throw "could not resolve pythonw.exe (got '$Pythonw'); pass -Pythonw; not registering"
}
# A venv's pythonw.exe is a launcher that starts the real interpreter as a second process; use the
# base interpreter so the task's process is the launcher itself.
if ($Pythonw -match '\\(\.venv|Scripts)\\') {
    throw "pythonw '$Pythonw' is inside a venv; pass a base interpreter's pythonw.exe; not registering"
}
$launcher = Join-Path $Checkout 'deploy\headless_launch.py'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw "launcher not found: $launcher" }
$LauncherMarginSeconds = 30

$interval = [System.Xml.XmlConvert]::ToTimeSpan($HeartbeatInterval)
$limit = [System.Xml.XmlConvert]::ToTimeSpan($ExecutionTimeLimit)
$launcherLimitSeconds = [int]$limit.TotalSeconds - $LauncherMarginSeconds

$argList = @('-I', "`"$launcher`"", '--limit-seconds', "$launcherLimitSeconds",
    "`"$pwshPath`"", '-NoProfile', '-File', "`"$wrapper`"")
if ($Repo.Count -gt 0) { $argList += @('-Repo', ($Repo -join ',')) }
if ($Project.Count -gt 0) { $argList += @('-Project', ($Project -join ',')) }
$argString = $argList -join ' '

if ($DryRun) {
    [pscustomobject]@{
        TaskName           = $TaskName
        Execute            = $Pythonw
        Arguments          = $argString
        RepetitionInterval = $HeartbeatInterval
        ExecutionTimeLimit = $ExecutionTimeLimit
        MultipleInstances  = 'IgnoreNew'
    } | Format-List
    return
}

$action = New-ScheduledTaskAction -Execute $Pythonw -Argument $argString -WorkingDirectory $Checkout
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval $interval
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit $limit `
    -StartWhenAvailable
# Success is decided by reading the task back, never by whether Register-ScheduledTask threw
# (T-stalled-pr-has-no-detector msg-8361 Obj-1-3). Register-ScheduledTask is a CDXML function: the
# script's $ErrorActionPreference does not reach inside it, and without -Force an existing task made
# it fail ("already exists") while this script still printed "registered" (msg-7374). -Force makes
# this script's definition replace whatever is there; -ErrorAction Stop surfaces what it can; and the
# read-back below is the only thing allowed to print success.
$registerError = $null
try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description 'MindWire stall ledger: one log-only heartbeat (D-16c)' -Force -ErrorAction Stop |
        Out-Null
} catch {
    $registerError = $_
    Write-Warning "Register-ScheduledTask failed: $($_.Exception.Message); checking the existing task"
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    throw "task $TaskName does not exist after registration; not registered (register error: $registerError)"
}
# The two durations come back as ISO 8601 strings, and the scheduler may store them in a different
# but equal spelling (PT60M vs PT1H). Compare them as TimeSpan, never as text (PR #537 gate). A value
# that does not parse stays as its raw text and so mismatches instead of throwing a parse error.
function ConvertTo-Duration([string]$Iso) {
    try { return [System.Xml.XmlConvert]::ToTimeSpan($Iso) } catch { return "unparseable '$Iso'" }
}
$actual = [ordered]@{
    Execute            = [string]$task.Actions[0].Execute
    Arguments          = [string]$task.Actions[0].Arguments
    RepetitionInterval = ConvertTo-Duration ([string]$task.Triggers[0].Repetition.Interval)
    ExecutionTimeLimit = ConvertTo-Duration ([string]$task.Settings.ExecutionTimeLimit)
}
$expected = [ordered]@{
    Execute            = $Pythonw
    Arguments          = $argString
    RepetitionInterval = $interval
    ExecutionTimeLimit = $limit
}
$mismatch = @($expected.Keys | Where-Object { $actual[$_] -ne $expected[$_] } |
        ForEach-Object { "$_ expected '$($expected[$_])' got '$($actual[$_])'" })
if ($mismatch.Count -gt 0) {
    throw "task $TaskName does not match this script's definition: $($mismatch -join '; ')"
}
$note = if ($registerError) { ' (registration failed, but the existing definition already matches; nothing replaced)' } else { '' }
Write-Output "verified $TaskName ($Pythonw headless_launch.py $pwshPath, every $HeartbeatInterval, killed after $ExecutionTimeLimit)$note"
