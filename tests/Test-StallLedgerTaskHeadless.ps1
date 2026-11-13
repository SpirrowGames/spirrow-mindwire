# Host integration test: the scheduled action that Register-StallLedgerTask.ps1 registers
# (`pythonw.exe -I deploy\headless_launch.py --limit-seconds N "<pwsh>" -NoProfile -File "<wrapper>"
# ...`) really runs the wrapper, shows no console window, still stops a hung wrapper, and reports
# the wrapper's exit code. Thread: T-loop-host-console-windows-steal-focus (PR #576 gate objection,
# msg-9268).
#
# Windows only, and NOT part of .mindwire-gate: CI runs on ubuntu, which has no Task Scheduler,
# and registering real tasks on every gate run would be too slow and too intrusive for the loop.
# Run it by hand on the loop host after changing the registered action:
#
#   pwsh -NoProfile -File tests/Test-StallLedgerTaskHeadless.ps1
#
#   X — the action comes from the script's own -DryRun output, not retyped here, so the test
#       exercises exactly what the script would register. Only the launcher's --limit-seconds is
#       shortened (to $TestLimitSeconds) so the run takes seconds, not 13.5 minutes. The checkout
#       path contains spaces.
#   R — the wrapper runs: it writes a marker with its PID, and its arguments arrive intact.
#   H — no visible console: the wrapper's console window (GetConsoleWindow) is 0 or not visible.
#   L — a hung wrapper is stopped at the launcher's limit (the property conhost --headless lost:
#       there the task's ExecutionTimeLimit stopped only conhost), and the task reports
#       EXIT_LIMIT_REACHED (121).
#   E — a wrapper that ends on its own: the task reports the wrapper's exit code (7).
#
# Each case registers a uniquely named task and always unregisters it, kills any survivor, and
# deletes its temp directory. Runtime is about one minute.

$ErrorActionPreference = 'Stop'

if (-not $IsWindows) {
    Write-Host 'SKIP  Windows only (Task Scheduler)'
    exit 0
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$register = Join-Path $repoRoot 'deploy\Register-StallLedgerTask.ps1'
$TestLimitSeconds = 20

$script:failures = 0
function Check {
    param([string]$Name, [bool]$Ok, [string]$Detail = '')
    if ($Ok) { Write-Host "PASS  $Name" }
    else { Write-Host "FAIL  $Name  $Detail"; $script:failures++ }
}

Add-Type -Namespace MwTest -Name User32 -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool IsWindowVisible(System.IntPtr hWnd);
'@

function Invoke-Case {
    param([string]$Label, [int]$WrapperSleepSeconds, [int]$WrapperExitCode, [switch]$CheckAction)
    $guid = [guid]::NewGuid().ToString('N').Substring(0, 12)
    $taskName = "mindwire-test-headless-$guid"
    $root = Join-Path ([System.IO.Path]::GetTempPath()) "mw headless test $guid"
    $checkout = Join-Path $root 'fake checkout'
    $marker = Join-Path $root 'marker.json'
    $wrapperPid = $null
    try {
        New-Item -ItemType Directory -Path (Join-Path $checkout 'deploy') -Force | Out-Null
        # The launcher is the one under test; the wrapper is a stand-in for the tick.
        Copy-Item -LiteralPath (Join-Path $repoRoot 'deploy\headless_launch.py') `
            -Destination (Join-Path $checkout 'deploy\headless_launch.py')
        $wrapperBody = @"
param([string]`$Repo, [string]`$Project)
Add-Type -Namespace MwTestW -Name K32 -MemberDefinition '[DllImport("kernel32.dll")] public static extern System.IntPtr GetConsoleWindow();'
[ordered]@{
    pid     = `$PID
    repo    = `$Repo
    project = `$Project
    hwnd    = [int64][MwTestW.K32]::GetConsoleWindow()
} | ConvertTo-Json | Set-Content -LiteralPath '$marker' -Encoding utf8
Start-Sleep -Seconds $WrapperSleepSeconds
exit $WrapperExitCode
"@
        Set-Content -LiteralPath (Join-Path $checkout 'deploy\run-stall-ledger-tick.ps1') -Value $wrapperBody -Encoding utf8

        # X: the action from the script's own -DryRun.
        $dry = & $register -Checkout $checkout -Repo 'o/r' -Project 'p' -TaskName $taskName -DryRun |
            Out-String -Width 8192
        $execute = ([regex]::Match($dry, '(?m)^Execute\s*:\s*(.+?)\s*$')).Groups[1].Value
        $arguments = ([regex]::Match($dry, '(?m)^Arguments\s*:\s*(.+?)\s*$')).Groups[1].Value
        if ($CheckAction) {
            Check 'X  DryRun names pythonw.exe as Execute' ($execute -like '*\pythonw.exe') "got '$execute'"
            Check 'X  DryRun runs the launcher isolated' ($arguments -match '^-I "[^"]+\\deploy\\headless_launch\.py" --limit-seconds \d+ "') "got '$arguments'"
            Check 'X  launcher limit is below the task limit' (
                [int]([regex]::Match($arguments, '--limit-seconds (\d+)').Groups[1].Value) -lt 14 * 60) "got '$arguments'"
            Check 'X  checkout path contains spaces' ($arguments.Contains('fake checkout')) "got '$arguments'"
        }
        $arguments = $arguments -replace '--limit-seconds \d+', "--limit-seconds $TestLimitSeconds"

        $action = New-ScheduledTaskAction -Execute $execute -Argument $arguments -WorkingDirectory $checkout
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddYears(1)
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
            -Description 'mindwire host test (Test-StallLedgerTaskHeadless.ps1); safe to delete' -Force |
            Out-Null
        $started = Get-Date
        Start-ScheduledTask -TaskName $taskName

        $deadline = (Get-Date).AddSeconds(30)
        while (-not (Test-Path -LiteralPath $marker) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
        Check "R  [$Label] wrapper ran (marker written)" (Test-Path -LiteralPath $marker)
        if (-not (Test-Path -LiteralPath $marker)) { return }
        Start-Sleep -Milliseconds 500
        $m = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
        $wrapperPid = [int]$m.pid
        Check "R  [$Label] -Repo arrived" ($m.repo -eq 'o/r') "got '$($m.repo)'"
        Check "R  [$Label] -Project arrived" ($m.project -eq 'p') "got '$($m.project)'"
        $hwnd = [IntPtr][int64]$m.hwnd
        $visible = ($hwnd -ne [IntPtr]::Zero) -and [MwTest.User32]::IsWindowVisible($hwnd)
        Check "H  [$Label] wrapper console window is not visible" (-not $visible) "hwnd=$($m.hwnd)"

        # Wait for the task to finish (the wrapper's own end, or the launcher's limit).
        $deadline = $started.AddSeconds($TestLimitSeconds + 60)
        while ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running' -and (Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 1
        }
        $elapsed = [int]((Get-Date) - $started).TotalSeconds
        $alive = [bool](Get-Process -Id $wrapperPid -ErrorAction SilentlyContinue)
        $result = (Get-ScheduledTaskInfo -TaskName $taskName).LastTaskResult
        Check "$($Label.Substring(0,1))  [$Label] wrapper process is gone" (-not $alive) "pid $wrapperPid alive after ${elapsed}s"
        $expected = if ($WrapperSleepSeconds -gt $TestLimitSeconds) { 121 } else { $WrapperExitCode }
        Check "$($Label.Substring(0,1))  [$Label] LastTaskResult is $expected" ($result -eq $expected) "got $result after ${elapsed}s"
    } finally {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        if ($wrapperPid) { Stop-Process -Id $wrapperPid -Force -ErrorAction SilentlyContinue }
        Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Invoke-Case -Label 'L hung wrapper' -WrapperSleepSeconds 600 -WrapperExitCode 0 -CheckAction
Invoke-Case -Label 'E wrapper ends' -WrapperSleepSeconds 2 -WrapperExitCode 7

if ($script:failures -gt 0) { Write-Host "$($script:failures) failure(s)"; exit 1 }
Write-Host 'all checks passed'
