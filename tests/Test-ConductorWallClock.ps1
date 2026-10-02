# Regression guard for the conductor's hard wall-clock budget (W-2) and the sweep's exit-7 stop
# (W-3). Thread: T-agmsg-transport-lessons-readiness-session-claim-board (Bohr msg-5496 / msg-5498,
# endorsed by Einstein msg-5499 / msg-5501).
#
#   B  — Get-ConductorHardBudgetSeconds: env first, then [conductor].run_hard_budget_s, then the
#        default; a value that is not a positive number throws, and so does a duplicate key.
#   P  — ps output parsing: header skipped; a one-column output is empty (cannot confirm); the
#        ps call uses one keyword per -o, which BSD (macOS) ps accepts.
#   S  — the output files' directory is private (0700 off Windows) and must be named by the
#        caller; there is no system-temp default.
#   N  — Invoke-ConductorBounded, normal exit: > 64 KB of output does not block the child (files,
#        not pipes); every line comes back, stderr then stdout; the exit code passes through;
#        an argument with a space arrives as one argument; the temp files are gone.
#   K  — a hung child with a grandchild: exit 6, the run_killed line is appended, root AND
#        grandchild are dead afterwards; the temp files are gone.
#   U  — a kill that cannot be confirmed: exit 7 and the run_kill_unconfirmed line.
#   W  — structure: run-conductor.ps1 no longer calls uv directly and exits with the bounded
#        run's code; it reads the hard budget from config/mindwire.toml with a '/' path; the
#        sweep's dispatch loop stops on exit 7 with a break; the output files go under the data
#        dir.
#
# The Python soft budget and the literals shared between the languages are in
# tests/test_run_budget.py.

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$lib = Join-Path $repoRoot 'deploy/lib/ConductorBudget.ps1'
if (-not (Test-Path -LiteralPath $lib)) { throw "lib not found: $lib" }
. $lib

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ("$Expected" -ceq "$Actual") { Write-Host "PASS  $Name" }
    else {
        Write-Host "FAIL  $Name`n      expected: $Expected`n      actual:   $Actual"
        $script:failures++
    }
}

$pwshPath = (Get-Process -Id $PID).Path
$scratch = Join-Path ([System.IO.Path]::GetTempPath()) ('mindwire-wallclock-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $scratch | Out-Null
$outDir = Join-Path $scratch 'out'
New-Item -ItemType Directory -Path $outDir | Out-Null

function Write-ChildScript {
    param([string]$Name, [string]$Body)
    $p = Join-Path $scratch $Name
    Set-Content -LiteralPath $p -Value $Body -Encoding utf8
    return $p
}

function Test-PidAlive {
    param([int]$Id)
    try { $p = Get-Process -Id $Id -ErrorAction Stop; return -not $p.HasExited } catch { return $false }
}

try {
    # ---------------------------------------------------------------- B
    Check 'B1 default when neither env nor TOML sets it' 4840 (Get-ConductorHardBudgetSeconds -ConfigPath (Join-Path $scratch 'absent.toml') -EnvValue '')
    $toml = Join-Path $scratch 'mindwire.toml'
    Set-Content -LiteralPath $toml -Encoding utf8 -Value @(
        '[loop]', 'run_hard_budget_s = 1', '',
        '[conductor]', 'task_thread_id = "T-x"', 'run_hard_budget_s = 1_800  # thirty minutes', '',
        '[other]', 'run_hard_budget_s = 2')
    Check 'B2 TOML [conductor] key wins over the default, other sections ignored' 1800 (Get-ConductorHardBudgetSeconds -ConfigPath $toml -EnvValue '')
    Check 'B3 env wins over TOML' 900 (Get-ConductorHardBudgetSeconds -ConfigPath $toml -EnvValue '900')
    $threw = $false
    try { Get-ConductorHardBudgetSeconds -ConfigPath $toml -EnvValue 'soon' | Out-Null } catch { $threw = $true }
    Check 'B4 a non-number throws instead of falling back' $true $threw
    $threw = $false
    try { Get-ConductorHardBudgetSeconds -ConfigPath $toml -EnvValue '0' | Out-Null } catch { $threw = $true }
    Check 'B5 zero throws' $true $threw
    $dup = Join-Path $scratch 'dup.toml'
    Set-Content -LiteralPath $dup -Encoding utf8 -Value @(
        '[conductor]', 'run_hard_budget_s = 1800', 'run_hard_budget_s = 3600')
    $threw = $false
    try { Get-ConductorHardBudgetSeconds -ConfigPath $dup -EnvValue '' | Out-Null } catch { $threw = $true }
    Check 'B6 a duplicate [conductor] key throws, as tomllib does' $true $threw

    # ---------------------------------------------------------------- P
    # ps output parsing (PR #407 gate @ 42a6b24): the header is skipped, and a one-column output
    # (BSD ps rejecting a keyword list) yields an empty table, never a partial one.
    $parsed = ConvertFrom-PsPidPpidRows -Rows @('  PID  PPID', '    1     0', '  420     1', '', ' 421   420')
    Check 'P1 header skipped, three rows parsed' 3 $parsed.Count
    Check 'P2 ppid of 421 is 420' 420 $parsed[421]
    Check 'P3 one-column output parses to an empty table' 0 (ConvertFrom-PsPidPpidRows -Rows @('  PID', '    1', '  420')).Count
    $libAst = [System.Management.Automation.Language.Parser]::ParseFile($lib, [ref]$null, [ref]$null)
    $psCalls = @($libAst.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'ps' }, $true))
    Check 'P4 the lib calls ps once' 1 $psCalls.Count
    if ($psCalls.Count -eq 1) {
        Check 'P5 ps uses one keyword per -o (BSD-compatible)' $true $psCalls[0].Extent.Text.StartsWith('& ps -A -o pid -o ppid')
    }

    # ---------------------------------------------------------------- S
    # The output files live in a private directory, never the shared temp dir (PR #407 gate,
    # REQUEST_CHANGES @ 42a6b24).
    $private = Join-Path $scratch 'private/conductor'
    Initialize-ConductorPrivateDirectory -Path $private
    Check 'S1 the private directory is created' $true (Test-Path -LiteralPath $private -PathType Container)
    if (-not $IsWindows) {
        # Compare the numeric mode (0o700 = 448), not the enum's string form: [UnixFileMode]
        # renders combined flags in bit order ("UserExecute, UserWrite, UserRead"), so a string
        # compare against the order the flags were written in never matches.
        Check 'S2 the private directory is 0700' 448 ([int][System.IO.File]::GetUnixFileMode($private))
    }
    $tempParam = (Get-Command Invoke-ConductorBounded).Parameters['TempDirectory']
    Check 'S3 -TempDirectory is mandatory (no system-temp default)' $true (@($tempParam.Attributes | Where-Object { $_ -is [System.Management.Automation.ParameterAttribute] -and $_.Mandatory }).Count -ge 1)

    # ---------------------------------------------------------------- N
    $chatty = Write-ChildScript 'chatty.ps1' @'
param([string]$Spaced)
[Console]::Error.WriteLine("err-first")
for ($i = 0; $i -lt 2000; $i++) { [Console]::Out.WriteLine(("x" * 60) + " line $i") }
[Console]::Out.WriteLine("arg=[$Spaced]")
[Console]::Out.WriteLine("marker-last")
exit 3
'@
    $r = Invoke-ConductorBounded -FilePath $pwshPath -Arguments @('-NoProfile', '-File', $chatty, 'two words') `
        -HardBudgetSeconds 60 -TempDirectory $outDir
    Check 'N1 exit code passes through' 3 $r.code
    Check 'N2 not killed' $false $r.killed
    Check 'N3 every line returned (1 stderr + 2000 + 2)' 2003 @($r.lines).Count
    Check 'N4 stderr first' 'err-first' @($r.lines)[0]
    Check 'N5 stdout last (the exit-time marker stays last)' 'marker-last' @($r.lines)[-1]
    Check 'N6 an argument with a space arrives as one argument' $true (@($r.lines) -contains 'arg=[two words]')
    Check 'N7 temp files removed' 0 @(Get-ChildItem -LiteralPath $outDir).Count
    $bytes = (@($r.lines) | Measure-Object -Property Length -Sum).Sum
    Check 'N8 output exceeded 64 KB' $true ($bytes -gt 65536)

    # ---------------------------------------------------------------- K
    $pidFile = Join-Path $scratch 'grandchild.pid'
    $sleeper = Write-ChildScript 'sleeper.ps1' 'Start-Sleep -Seconds 120'
    $hung = Write-ChildScript 'hung.ps1' @"
`$g = Start-Process -FilePath '$pwshPath' -ArgumentList '-NoProfile','-File','$sleeper' -PassThru -NoNewWindow
Set-Content -LiteralPath '$pidFile' -Value `$g.Id
[Console]::Out.WriteLine('child started')
[Console]::Out.Flush()
Start-Sleep -Seconds 120
"@
    $r = Invoke-ConductorBounded -FilePath $pwshPath -Arguments @('-NoProfile', '-File', $hung) `
        -HardBudgetSeconds 8 -TempDirectory $outDir
    Check 'K1 killed' $true $r.killed
    Check 'K2 kill confirmed' $true $r.confirmed
    Check 'K3 exit 6' 6 $r.code
    Check 'K4 run_killed line appended last' "conductor.run_killed budget_s=8 pid=$($r.pid)" @($r.lines)[-1]
    Check 'K5 output written before the kill survives' $true (@($r.lines) -contains 'child started')
    Check 'K6 root is dead' $false (Test-PidAlive -Id $r.pid)
    $grandchild = if (Test-Path -LiteralPath $pidFile) { [int](Get-Content -LiteralPath $pidFile -Raw).Trim() } else { 0 }
    Check 'K7 grandchild pid was recorded' $true ($grandchild -gt 0)
    if ($grandchild -gt 0) { Check 'K8 grandchild is dead (no descendant left)' $false (Test-PidAlive -Id $grandchild) }
    Check 'K9 temp files removed' 0 @(Get-ChildItem -LiteralPath $outDir).Count

    # ---------------------------------------------------------------- U
    $realStop = ${function:Stop-ConductorProcessTree}
    function Stop-ConductorProcessTree {
        param([System.Diagnostics.Process]$Process, [int]$GraceMs)
        try { $Process.Kill($true) } catch { }
        return @{ confirmed = $false; survivors = @(424242); listed = $true }
    }
    try {
        $r = Invoke-ConductorBounded -FilePath $pwshPath -Arguments @('-NoProfile', '-File', $sleeper) `
            -HardBudgetSeconds 2 -TempDirectory $outDir
    }
    finally { Set-Item -Path function:Stop-ConductorProcessTree -Value $realStop }
    Check 'U1 exit 7 when the kill is not confirmed' 7 $r.code
    Check 'U2 run_killed line still written' $true ((@($r.lines) | Where-Object { $_ -like 'conductor.run_killed budget_s=2 *' }).Count -eq 1)
    Check 'U3 run_kill_unconfirmed line names the survivors' $true ((@($r.lines) | Where-Object { $_ -like 'conductor.run_kill_unconfirmed *survivors=424242*' }).Count -eq 1)

    # ---------------------------------------------------------------- W
    $runner = Join-Path $repoRoot 'deploy/run-conductor.ps1'
    $rast = [System.Management.Automation.Language.Parser]::ParseFile($runner, [ref]$null, [ref]$null)
    $cmds = $rast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)
    Check 'W1 run-conductor.ps1 no longer runs uv directly (unbounded)' 0 @($cmds | Where-Object { $_.GetCommandName() -eq 'uv' }).Count
    Check 'W2 run-conductor.ps1 calls Invoke-ConductorBounded once' 1 @($cmds | Where-Object { $_.GetCommandName() -eq 'Invoke-ConductorBounded' }).Count
    $last = $rast.EndBlock.Statements[-1]
    Check 'W3 run-conductor.ps1 ends with exit $run.code' 'exit $run.code' $last.Extent.Text
    # The hard-budget config path must use '/' : on Linux/macOS a '\' is a filename character, so
    # 'config\mindwire.toml' would miss the file and silently fall back to the default hard budget
    # while Python reads the real one (PR #407 gate, REQUEST_CHANGES @ 9d7208d).
    $budgetCall = @($cmds | Where-Object { $_.GetCommandName() -eq 'Get-ConductorHardBudgetSeconds' })
    Check 'W8 run-conductor.ps1 reads the hard budget once' 1 $budgetCall.Count
    if ($budgetCall.Count -eq 1) {
        $t = $budgetCall[0].Extent.Text
        Check 'W9 the hard-budget config path is config/mindwire.toml (no backslash)' $true ($t.Contains('"config/mindwire.toml"') -and -not $t.Contains('\'))
    }
    $bounded = @($cmds | Where-Object { $_.GetCommandName() -eq 'Invoke-ConductorBounded' })
    if ($bounded.Count -eq 1) {
        Check 'W10 the output files go under the data dir (tmp/conductor), not system temp' $true $bounded[0].Extent.Text.Contains('-TempDirectory (Join-Path $dataDir "tmp/conductor")')
    }

    $sweep = Join-Path $repoRoot 'deploy/run-conductor-scheduled.ps1'
    $sast = [System.Management.Automation.Language.Parser]::ParseFile($sweep, [ref]$null, [ref]$null)
    $loops = @($sast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.ForEachStatementAst] -and
        $n.Condition.Extent.Text -eq '$candidates' -and
        $n.Body.Extent.Text.Contains('& $inner') }, $true))
    Check 'W4 exactly one dispatch loop' 1 $loops.Count
    $ifs = @($loops[0].FindAll({ param($n)
        $n -is [System.Management.Automation.Language.IfStatementAst] -and
        $n.Clauses.Count -eq 1 -and
        $n.Clauses[0].Item1.Extent.Text -eq '$code -eq $ConductorKillUnconfirmedExitCode' }, $true))
    Check 'W5 the dispatch loop branches on exit 7 once' 1 $ifs.Count
    if ($ifs.Count -eq 1) {
        $body = $ifs[0].Clauses[0].Item2
        $breaks = @($body.FindAll({ param($n) $n -is [System.Management.Automation.Language.BreakStatementAst] }, $true))
        Check 'W6 the exit-7 branch breaks the sweep' 1 $breaks.Count
        Check 'W7 the exit-7 branch records why' $true $body.Extent.Text.Contains("`$breakReason = 'kill-unconfirmed'")
    }
}
finally {
    Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
}

if ($script:failures -gt 0) {
    Write-Host "`n$($script:failures) check(s) FAILED"
    exit 1
}
Write-Host "`nall checks passed"
exit 0
