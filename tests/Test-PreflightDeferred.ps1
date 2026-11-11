# Regression guard for the sweep's preflight-deferral branch (deploy/run-conductor-scheduled.ps1).
#
# T-preflight-transient-gh-failure-quarantines-threads, design v3.2 (Bohr msg-9200 / msg-9202 /
# msg-9204 / msg-9206 / msg-9208, go Einstein). When mindwire-loop exits $PreflightDeferredExitCode
# (preflight P1 could not confirm protection because GitHub was transiently unreachable) the
# wrapper must:
#   * NOT quarantine, NOT write retry-pending, NOT count toward K, revert the LAUNCH commit, skip
#     the launch-end retreat (exits 8/9 do the same);
#   * D-5a: leave an EXISTING retry-pending record exactly as it is — the retry is not consumed —
#     and skip the rest of this tick's candidates on the same repo_dir without touching theirs;
#   * D-5b: keep one deferral episode per repo — alert on open, alert once more after 2 h, stay
#     quiet otherwise — and end it (record AND notification key) when a later run prints
#     MINDWIRE_PREFLIGHT_OK, whatever that run's exit code.
#
# Functions are lifted out of the sweep script's AST (dot-sourcing would run the sweep). The
# inline loop is pinned structurally on the AST; the state flows are driven through a small
# driver that applies, per exit code, exactly the calls the AST checks show the loop making.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }

$script:RetryEventsCap = 500
function Write-Log { param([string]$Message) }

$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'ConvertTo-DirtyCloneRepoKey', 'Test-DirtyCloneSkip', 'Test-NotificationSuppressed',
                  'ConvertTo-RetryIso', 'ConvertTo-UtcInstant', 'Add-RetryEvent', 'New-RetryPendingRecord',
                  'ConvertTo-RetryPendingState', 'Register-CandidateFailure', 'Register-CandidateSuccess',
                  'Remove-RetryPendingNotLive', 'Invoke-LaunchEndRetreat',
                  'Get-PreflightDeferredPayload', 'Get-PreflightOkRepos', 'Get-PreflightDeferredRepoKey',
                  'Get-PreflightDeferredNoticeKey', 'ConvertTo-PreflightDeferredEpisodes',
                  'Update-PreflightDeferredEpisode', 'Clear-PreflightDeferredEpisodes',
                  'Get-PreflightDeferredNotice') {
    $fn = $functions | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual) }
}

function Get-ScriptValue {
    param([string]$Var)
    $a = $ast.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $n.Left.Extent.Text -eq $Var }, $true) | Select-Object -First 1
    if (-not $a) { throw "$Var is not assigned in the sweep script" }
    return $a.Right.Extent.Text
}

# --- constants ---------------------------------------------------------------------------------------
$PreflightDeferredExitCode = [int](Get-ScriptValue '$PreflightDeferredExitCode')
Check 'PreflightDeferredExitCode is 11 (not 1-10: taken)' 11 $PreflightDeferredExitCode
$PreflightDeferredEscalateAfter = Invoke-Expression (Get-ScriptValue '$PreflightDeferredEscalateAfter')
Check 'escalation after 2 h' ([TimeSpan]::FromHours(2)) $PreflightDeferredEscalateAfter
$DirtyCloneExitCode = 8
$GitSyncDeferredExitCode = 9

# --- payload / OK row parsing ------------------------------------------------------------------------
$base = Join-Path ([System.IO.Path]::GetTempPath()) 'MindWire-PfClone'
$baseKey = ConvertTo-DirtyCloneRepoKey -RepoDir $base
$out11 = @('log line', 'MINDWIRE_PREFLIGHT_DEFERRED_PAYLOAD {"owner": "SpirrowGames", "repo": "r1", "endpoint": "repos/SpirrowGames/r1", "attempts": 4, "last_stderr": "schannel: failed to receive handshake", "reason": "transient", "checked": [], "unchecked": ["repos/SpirrowGames/r1"]}')
$p = Get-PreflightDeferredPayload -Output $out11
Check 'payload parsed' 'r1' "$($p.repo)"
Check 'repo key from payload' 'SpirrowGames/r1' (Get-PreflightDeferredRepoKey -Payload $p -RepoDir $base)
$pBad = Get-PreflightDeferredPayload -Output @('MINDWIRE_PREFLIGHT_DEFERRED_PAYLOAD {not json')
Check 'unreadable payload -> $null' $true ($null -eq $pBad)
Check 'unreadable payload -> repo_dir key' "dir:$baseKey" (Get-PreflightDeferredRepoKey -Payload $pBad -RepoDir $base)
$ok = Get-PreflightOkRepos -Output @('x', 'MINDWIRE_PREFLIGHT_OK {"repos": ["SpirrowGames/r1"]}', 'conductor stopped: reason=none')
Check 'OK row seen' $true $ok.seen
Check 'OK row repos' 'SpirrowGames/r1' ($ok.repos -join ',')
Check 'no OK row -> not seen' $false (Get-PreflightOkRepos -Output @('conductor stopped: reason=adapter_error')).seen
Check 'unreadable OK row still counts as seen' $true (Get-PreflightOkRepos -Output @('MINDWIRE_PREFLIGHT_OK {nope')).seen

# --- D-5b: the episode lifecycle, clock pinned ---------------------------------------------------------
# Driver: what the loop does with one run's (exit code, output) for the episode state. The AST checks
# at the bottom pin that the real loop makes exactly these calls, in this order.
$script:sent = @()
function Invoke-RunForEpisodes {
    param([hashtable]$Episodes, [hashtable]$Notify, [int]$Code, $Output, [datetime]$Now, [string]$RepoDir = $base)
    $okRow = Get-PreflightOkRepos -Output $Output
    if ($okRow.seen -and $Episodes.Count -gt 0) {
        $null = Clear-PreflightDeferredEpisodes -Episodes $Episodes -NotifyState $Notify -Repos $okRow.repos -RepoDir $RepoDir
    }
    if ($Code -eq $PreflightDeferredExitCode) {
        $pl = Get-PreflightDeferredPayload -Output $Output
        $rk = Get-PreflightDeferredRepoKey -Payload $pl -RepoDir $RepoDir
        $t = Update-PreflightDeferredEpisode -Episodes $Episodes -RepoKey $rk -RepoDir $RepoDir -NowUtc $Now
        if ($t.action -ne 'quiet') {
            $n = Get-PreflightDeferredNotice -RepoKey $rk -Transition $t -CandidateKey 'p::T-a' -RepoDir $RepoDir -Payload $pl -ExitCode $Code
            if (-not (Test-NotificationSuppressed -State $Notify -Key $n.key -Signature $n.signature)) {
                $script:sent += $n.signature
                $Notify[$n.key] = $n.signature
            }
        }
        return $t
    }
    return $null
}

$t0 = [datetime]::Parse('2026-11-11T00:04:00Z').ToUniversalTime()
$eps = @{}
$notify = @{}
$okOut1 = @('MINDWIRE_PREFLIGHT_OK {"repos": ["SpirrowGames/r1"]}', 'conductor stopped: reason=adapter_error rounds=1')
$noOk1 = @('preflight P1 failed: ... (HTTP 404)')

$r = Invoke-RunForEpisodes -Episodes $eps -Notify $notify -Code 11 -Output $out11 -Now $t0
Check 'first deferral opens the episode' 'open' $r.action
Check 'first deferral alerts' 1 $script:sent.Count
Check 'episode first_at is the first deferral' ($t0.ToString('o')) $eps['SpirrowGames/r1'].first_at
$r = Invoke-RunForEpisodes -Episodes $eps -Notify $notify -Code 11 -Output $out11 -Now $t0.AddMinutes(119)
Check '1h59m: quiet' 'quiet' $r.action
Check '1h59m: no alert' 1 $script:sent.Count
$r = Invoke-RunForEpisodes -Episodes $eps -Notify $notify -Code 11 -Output $out11 -Now $t0.AddHours(2)
Check '2h: escalates' 'escalate' $r.action
Check '2h: alerts once more' 2 $script:sent.Count
Check '2h: escalated signature' 'preflight-deferred:SpirrowGames/r1:escalated' $script:sent[-1]
$esc = Get-PreflightDeferredNotice -RepoKey 'SpirrowGames/r1' -Transition $r -CandidateKey 'p::T-a' -RepoDir $base -Payload $p -ExitCode 11
Check 'escalation body names first_at and count' $true ($esc.message -like "*first_at=$($t0.ToString('o'))*3 回*")
$r = Invoke-RunForEpisodes -Episodes $eps -Notify $notify -Code 11 -Output $out11 -Now $t0.AddHours(3)
Check '3h: quiet again' 'quiet' $r.action
Check '3h: no alert' 2 $script:sent.Count
Check 'count tracks every deferral' 4 $eps['SpirrowGames/r1'].count
# exit 1 WITHOUT the OK row (a terminal preflight failure): the episode stays.
$null = Invoke-RunForEpisodes -Episodes $eps -Notify $notify -Code 1 -Output $noOk1 -Now $t0.AddHours(4)
Check 'exit 1 without OK row keeps the episode' $true $eps.ContainsKey('SpirrowGames/r1')
Check 'exit 1 without OK row keeps the notice key' $true $notify.ContainsKey('__preflight_deferred__/SpirrowGames/r1')
# exit 1 WITH the OK row (the role failed after a passing preflight): the episode ends.
$null = Invoke-RunForEpisodes -Episodes $eps -Notify $notify -Code 1 -Output $okOut1 -Now $t0.AddHours(5)
Check 'OK row on exit 1 ends the episode' $false $eps.ContainsKey('SpirrowGames/r1')
Check 'OK row drops the notice key too' $false $notify.ContainsKey('__preflight_deferred__/SpirrowGames/r1')
$r = Invoke-RunForEpisodes -Episodes $eps -Notify $notify -Code 11 -Output $out11 -Now $t0.AddHours(6)
Check 'next outage opens a new episode' 'open' $r.action
Check 'next outage alerts again' 3 $script:sent.Count
Check 'new episode restarts first_at' ($t0.AddHours(6).ToString('o')) $eps['SpirrowGames/r1'].first_at

# Unreadable payload: keyed by repo_dir, still alerts, and the same repo_dir's OK row ends it.
$eps2 = @{}; $notify2 = @{}
$r = Invoke-RunForEpisodes -Episodes $eps2 -Notify $notify2 -Code 11 -Output @('MINDWIRE_PREFLIGHT_DEFERRED_PAYLOAD {bad') -Now $t0
Check 'unreadable payload still opens (alerts)' 'open' $r.action
Check 'unreadable payload: dir key' $true $eps2.ContainsKey("dir:$baseKey")
$null = Invoke-RunForEpisodes -Episodes $eps2 -Notify $notify2 -Code 0 -Output @('MINDWIRE_PREFLIGHT_OK {"repos": ["SpirrowGames/r1"]}') -Now $t0.AddMinutes(30)
Check 'OK row from the same repo_dir ends a dir-keyed episode' 0 $eps2.Count
# An OK row from ANOTHER repo leaves this repo's episode alone.
$eps3 = @{}; $notify3 = @{}
$null = Invoke-RunForEpisodes -Episodes $eps3 -Notify $notify3 -Code 11 -Output $out11 -Now $t0
$null = Invoke-RunForEpisodes -Episodes $eps3 -Notify $notify3 -Code 0 -Output @('MINDWIRE_PREFLIGHT_OK {"repos": ["SpirrowGames/other"]}') `
    -Now $t0.AddMinutes(5) -RepoDir (Join-Path ([System.IO.Path]::GetTempPath()) 'MindWire-OtherClone')
Check 'another repo passing does not end this episode' $true $eps3.ContainsKey('SpirrowGames/r1')

# Round trip through the state file shape (JSON -> PSCustomObject -> hashtable).
$json = ($eps | ConvertTo-Json -Depth 5) | ConvertFrom-Json
$back = ConvertTo-PreflightDeferredEpisodes -State $json
Check 'state round-trips: first_at' $eps['SpirrowGames/r1'].first_at $back['SpirrowGames/r1'].first_at
Check 'state round-trips: escalated' $false $back['SpirrowGames/r1'].escalated
Check 'state round-trips: count' 1 $back['SpirrowGames/r1'].count

# --- D-5a: an existing retry-pending record survives a deferral -----------------------------------------
# Driver for retry-pending: the generic non-zero branch calls Register-CandidateFailure, exit 0 calls
# Register-CandidateSuccess, and exit 11 / the same-repo skip call NEITHER (pinned on the AST below).
function Invoke-RunForRetry {
    param([hashtable]$RetryState, [string]$Key, [int]$Code, [datetime]$Now)
    if ($Code -eq $PreflightDeferredExitCode) { return 'deferred' }
    if ($Code -ne 0) {
        $rec = New-RetryPendingRecord -FirstFailureAt $Now.ToString('o') -ExitCode $Code -StopReason 'adapter_error' `
            -ErrorCode 'adapter.x' -FailureHead 'msg-1' -FailureControl 'running' -SessionLogPath 'C:\log'
        return (Register-CandidateFailure -RetryState $RetryState -Key $Key -Record $rec -Now $Now).action
    }
    if (Register-CandidateSuccess -RetryState $RetryState -Key $Key -Now $Now) { return 'retry-recovered' }
    return 'ok'
}
$key = 'spirrow-mindwire/T-a'
$tA = $t0; $tB = $t0.AddMinutes(16); $tC = $t0.AddMinutes(32)

$rs = ConvertTo-RetryPendingState -Raw @{}
Check '1. role failure -> retry-scheduled' 'retry-scheduled' (Invoke-RunForRetry -RetryState $rs -Key $key -Code 1 -Now $tA)
$before = $rs.pending[$key] | ConvertTo-Json -Depth 5
Check '2. exit 11 on the retry launch' 'deferred' (Invoke-RunForRetry -RetryState $rs -Key $key -Code 11 -Now $tB)
Check '2. pending record unchanged' $before ($rs.pending[$key] | ConvertTo-Json -Depth 5)
Check '3. next tick exit 0 -> retry-recovered' 'retry-recovered' (Invoke-RunForRetry -RetryState $rs -Key $key -Code 0 -Now $tC)

$rs = ConvertTo-RetryPendingState -Raw @{}
$null = Invoke-RunForRetry -RetryState $rs -Key $key -Code 1 -Now $tA
$null = Invoke-RunForRetry -RetryState $rs -Key $key -Code 11 -Now $tB
$t = Register-CandidateFailure -RetryState $rs -Key $key -Now $tC -Record (New-RetryPendingRecord -FirstFailureAt $tC.ToString('o') -ExitCode 1 `
        -StopReason 'adapter_error' -ErrorCode 'adapter.x' -FailureHead 'msg-1' -FailureControl 'running' -SessionLogPath 'C:\log')
Check '4. next tick exit 1 -> quarantine (the one retry was still unused)' 'quarantine' $t.action
Check '4. carried first_failure_at is step 1' ($tA.ToString('o')) (ConvertTo-RetryIso $t.first.first_failure_at)

$rs = ConvertTo-RetryPendingState -Raw @{}
$null = Invoke-RunForRetry -RetryState $rs -Key $key -Code 1 -Now $tA
$before = $rs.pending[$key] | ConvertTo-Json -Depth 5
$skipDirs = @{ (ConvertTo-DirtyCloneRepoKey -RepoDir $base) = $true }
Check '5. same-repo candidate is skipped' $true (Test-DirtyCloneSkip -RepoDir $base -DirtyRepoDirs $skipDirs)
$removedNotLive = Remove-RetryPendingNotLive -RetryState $rs -LiveKeys @($key)
Check '5. a skipped candidate is still live: pending not swept' 0 @($removedNotLive).Count
Check '5. skipped candidate pending unchanged' $before ($rs.pending[$key] | ConvertTo-Json -Depth 5)

# --- the retreat is skipped on exit 11 ------------------------------------------------------------------
$retreatCalled = $false
$ro = Invoke-LaunchEndRetreat -RepoDir $base -ThreadId 'T-a' -CandidateKey $key -HeadMsg 'msg-1' -StopReason '' -ExitCode 11 `
    -StateDir $base -Retreat { $script:retreatCalled = $true }
Check 'launch-end retreat skipped on exit 11' 'skipped' $ro

# --- the real loop: ordering and shape (AST) ------------------------------------------------------------
$loop = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.ForEachStatementAst] -and
        $n.Variable.Extent.Text -eq '$cand' -and $n.Body.Extent.Text -match 'Invoke-HeadSkipCommitLaunch' }, $true) |
    Select-Object -First 1
Check 'sweep candidate loop found' $true ($null -ne $loop)
$body = $loop.Body.Extent.Text
$posInner = $body.IndexOf('& $inner')
$posOk = $body.IndexOf('Get-PreflightOkRepos -Output $output')
$posBranch = $body.IndexOf('if ($code -eq $PreflightDeferredExitCode)')
$posNonZero = $body.IndexOf('if ($code -ne 0) {')
$posSkip = $body.IndexOf('-DirtyRepoDirs $preflightDeferredRepoDirs')
$posCommit = $body.IndexOf('Invoke-HeadSkipCommitLaunch')
Check 'same-repo skip comes before the LAUNCH commit' $true ($posSkip -ge 0 -and $posSkip -lt $posCommit)
Check 'OK-row clear follows the launch' $true ($posOk -gt $posInner)
Check 'OK-row clear precedes every exit-code branch' $true ($posOk -lt $posBranch -and $posOk -lt $posNonZero)
Check 'deferral branch precedes the generic non-zero (quarantine) branch' $true ($posBranch -gt $posInner -and $posBranch -lt $posNonZero)

$branch = $loop.Body.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.IfStatementAst] -and
        $n.Clauses[0].Item1.Extent.Text -eq '$code -eq $PreflightDeferredExitCode' }, $true) | Select-Object -First 1
Check 'deferral branch found' $true ($null -ne $branch)
$bt = $branch.Clauses[0].Item2.Extent.Text
Check 'branch ends in continue' $true ($bt -match '(?m)^\s*continue\s*$')
Check 'branch reverts the LAUNCH commit' $true ($bt -match 'Invoke-HeadSkipRevertLaunch')
Check 'branch marks the repo_dir for the per-tick skip' $true ($bt -match '\$preflightDeferredRepoDirs\[')
Check 'branch updates the episode' $true ($bt -match 'Update-PreflightDeferredEpisode')
Check 'branch notifies through Send-NotificationIfChanged' $true ($bt -match 'Send-NotificationIfChanged')
Check 'branch never quarantines' $false ($bt -match 'quarantineState|New-QuarantineRecord')
Check 'branch never touches retry state transitions' $false ($bt -match 'Register-Candidate|New-RetryPendingRecord|pending\.Remove|pending\[\$cand\.key\]\s*=')
Check 'branch never counts toward K' $false ($bt -match 'sweepFailures')
Check 'branch gives back the retry-launched count' $true ($bt -match '\$retryLaunched--')

$skipIf = $loop.Body.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.IfStatementAst] -and
        $n.Clauses[0].Item1.Extent.Text -match 'preflightDeferredRepoDirs' }, $true) | Select-Object -First 1
$st = $skipIf.Clauses[0].Item2.Extent.Text
Check 'skip disposition' $true ($st -match "'preflight-deferred-skipped'")
Check 'skip never touches retry state' $false ($st -match 'retryState|Register-Candidate')
Check 'skip ends in continue' $true ($st -match '(?m)^\s*continue\s*$')

if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) FAILED"; exit 1 }
Write-Host "all checks passed"
exit 0
