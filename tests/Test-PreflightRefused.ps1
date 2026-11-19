# Regression guard for the sweep's preflight-refusal branch (deploy/run-conductor-scheduled.ps1).
#
# T-preflight-failure-counts-toward-stall, design v3 (Bohr msg-9808 v1 / msg-9810 v2 / msg-9812 v3,
# go Einstein msg-9813). When mindwire-loop exits $PreflightRefusedExitCode (the preflight refused the
# run; no role was spawned) the wrapper must:
#   * revert the LAUNCH commit, so refused runs never build launches_same_head up to STALLED
#     (msg-9782: three refusals became "Bohr posted nothing");
#   * leave retry-pending exactly as it is (neither read nor written, the retry not consumed), never
#     quarantine, never count toward K, and NOT skip the other candidates of the repo (Obj-1-2);
#   * notify per candidate (key preflight-refused:<cand.key>, signature check + normalised detail),
#     and end that notice only on THIS candidate's MINDWIRE_PREFLIGHT_OK (Obj-2-1);
#   * leave the preflight-deferred episode untouched (Obj-2-2: no `kind`, an exit-13 run prints no
#     OK row so it ends nothing).
#
# Numbered sections follow the design's test list (v2 §2' items 1, 2, 4, 5; v3 §2'' items 3, 6, 7).
# Functions are lifted out of the sweep script's AST (dot-sourcing would run the sweep); the inline
# loop is pinned structurally on the AST, and the drivers apply exactly the calls those checks pin.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }

$script:RetryEventsCap = 500
function Write-Log { param([string]$Message) }
. (Join-Path $repoRoot 'deploy/lib/StopReason.ps1')

$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'ConvertTo-DirtyCloneRepoKey', 'Test-NotificationSuppressed',
                  'ConvertTo-RetryIso', 'ConvertTo-UtcInstant', 'Add-RetryEvent', 'New-RetryPendingRecord',
                  'ConvertTo-RetryPendingState', 'Register-CandidateFailure', 'Register-CandidateSuccess',
                  'Invoke-LaunchEndRetreat', 'Test-RefSnapshotEpisodeOver',
                  'Get-PreflightDeferredPayload', 'Get-PreflightOkRepos', 'Get-PreflightDeferredRepoKey',
                  'Get-PreflightDeferredNoticeKey', 'Update-PreflightDeferredEpisode', 'Clear-PreflightDeferredEpisodes',
                  'Get-PreflightRefusedPayload', 'Get-PreflightRefusedNoticeKey',
                  'ConvertTo-PreflightRefusedSignatureDetail', 'Get-PreflightRefusedNotice',
                  'Clear-PreflightRefusedNotice') {
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
$PreflightRefusedExitCode = [int](Get-ScriptValue '$PreflightRefusedExitCode')
Check 'PreflightRefusedExitCode is 13 (1-12 taken)' 13 $PreflightRefusedExitCode
$PreflightDeferredExitCode = [int](Get-ScriptValue '$PreflightDeferredExitCode')
$PreflightDeferredEscalateAfter = [TimeSpan]::FromHours(2)
$DirtyCloneExitCode = 8
$GitSyncDeferredExitCode = 9
$RefSnapshotUnrecordedExitCode = 12

# --- payload, key, signature, body -------------------------------------------------------------------
$cloneA = Join-Path ([System.IO.Path]::GetTempPath()) 'MindWire-PrCloneA'
$cloneB = Join-Path ([System.IO.Path]::GetTempPath()) 'MindWire-PrCloneB'
$keyA = 'spirrow-mindwire/T-a'
$keyB = 'spirrow-mindwire/T-b'
function New-RefusedOutput {
    param([string]$Check = 'P1', [string]$Detail = 'preflight P1 failed: could not fetch rules for SpirrowGames/spirrow-mindwire@main: gh: Not Found (HTTP 404). Fail-closed (msg-1267 §4).')
    $row = @{ check = $Check; detail = $Detail } | ConvertTo-Json -Compress
    return @('preflight refused exit=13', "MINDWIRE_PREFLIGHT_REFUSED_PAYLOAD $row")
}
$out13 = New-RefusedOutput
$pl = Get-PreflightRefusedPayload -Output $out13
Check 'payload parsed: check' 'P1' "$($pl.check)"
Check 'unreadable payload -> $null' $true ($null -eq (Get-PreflightRefusedPayload -Output @('MINDWIRE_PREFLIGHT_REFUSED_PAYLOAD {bad')))
Check 'notice key is per candidate' "preflight-refused:$keyA" (Get-PreflightRefusedNoticeKey -CandidateKey $keyA)
$n = Get-PreflightRefusedNotice -CandidateKey $keyA -RepoDir $cloneA -Output $out13 -ExitCode 13
Check 'notice key' "preflight-refused:$keyA" $n.key
Check 'notice check' 'P1' $n.check
Check 'signature = check + normalised detail' $true ($n.signature -like 'preflight-refused:P1:preflight P1 failed: could not fetch rules*')
Check 'body says the role was not started' $true ($n.message -like '*role は起動していません*')
Check 'body names the check' $true ($n.message -like '*preflight の P1 が*')
Check 'body carries the detail' $true ($n.message -like '*HTTP 404*')
Check 'body names reason=preflight_refused' $true ($n.message -like '*reason=preflight_refused*')
Check 'body is not the STALLED phrase' $false ($n.message -like '*stall watchdog*')
Check 'body does not say the role posted nothing' $false ($n.message -like '*何も投稿*')
$nLayout = Get-PreflightRefusedNotice -CandidateKey $keyA -RepoDir $cloneA -ExitCode 13 `
    -Output (New-RefusedOutput -Detail "preflight P1 failed: could not fetch rules for SpirrowGames/spirrow-mindwire@main:`n   gh: Not Found (HTTP 404). Fail-closed (msg-1267 §4).")
Check 'whitespace-only change keeps the signature' $n.signature $nLayout.signature
$nOther = Get-PreflightRefusedNotice -CandidateKey $keyA -RepoDir $cloneA -ExitCode 13 -Output (New-RefusedOutput -Check 'P3' -Detail 'preflight P3 failed: no core.hooksPath')
Check 'a different refusal changes the signature' $true ($nOther.signature -ne $n.signature)
$nBad = Get-PreflightRefusedNotice -CandidateKey $keyA -RepoDir $cloneA -ExitCode 13 -Output @('MINDWIRE_PREFLIGHT_REFUSED_PAYLOAD {bad')
Check 'unreadable payload still notifies (check unknown)' 'unknown' $nBad.check
Check 'unreadable payload body still says the role was not started' $true ($nBad.message -like '*role は起動していません*')

# --- driver: what the loop does with one run of one candidate --------------------------------------
# Order and calls pinned on the AST at the bottom: OK row -> Clear-PreflightDeferredEpisodes and
# Clear-PreflightRefusedNotice (this candidate only); exit 13 -> Send-NotificationIfChanged with the
# candidate's notice; exit 11 -> the deferred episode; nothing else touches either state.
$script:sent = @()
function Invoke-Run {
    param([hashtable]$Notify, [hashtable]$Episodes, [string]$CandKey, [string]$RepoDir, [int]$Code, $Output, [datetime]$Now)
    $okRow = Get-PreflightOkRepos -Output $Output
    if ($okRow.seen) { $null = Clear-PreflightRefusedNotice -State $Notify -CandidateKey $CandKey }
    if ($okRow.seen -and $Episodes.Count -gt 0) {
        $null = Clear-PreflightDeferredEpisodes -Episodes $Episodes -NotifyState $Notify -Repos $okRow.repos -RepoDir $RepoDir
    }
    if ($Code -eq $PreflightDeferredExitCode) {
        $p = Get-PreflightDeferredPayload -Output $Output
        $rk = Get-PreflightDeferredRepoKey -Payload $p -RepoDir $RepoDir
        $t = Update-PreflightDeferredEpisode -Episodes $Episodes -RepoKey $rk -RepoDir $RepoDir -NowUtc $Now
        if ($t.action -ne 'quiet') {
            $k = Get-PreflightDeferredNoticeKey -RepoKey $rk
            if (-not (Test-NotificationSuppressed -State $Notify -Key $k -Signature $t.signature)) { $script:sent += $t.signature; $Notify[$k] = $t.signature }
        }
        return 'deferred'
    }
    if ($Code -eq $PreflightRefusedExitCode) {
        $nn = Get-PreflightRefusedNotice -CandidateKey $CandKey -RepoDir $RepoDir -Output $Output -ExitCode $Code
        if (-not (Test-NotificationSuppressed -State $Notify -Key $nn.key -Signature $nn.signature)) {
            $script:sent += "$CandKey|$($nn.signature)"
            $Notify[$nn.key] = $nn.signature
        }
        return 'refused'
    }
    return 'ran'
}
$t0 = [datetime]::Parse('2026-11-11T00:04:00Z').ToUniversalTime()
$okOut = @('MINDWIRE_PREFLIGHT_OK {"repos": ["SpirrowGames/spirrow-mindwire"]}', 'conductor stopped: reason=none rounds=1')

# --- 3. per candidate: refused -> A's OK -> the same refusal alerts again -------------------------------
Write-Host '3. refusal -> OK -> same refusal (candidate A)'
$notify = @{}; $eps = @{}; $script:sent = @()
$null = Invoke-Run -Notify $notify -Episodes $eps -CandKey $keyA -RepoDir $cloneA -Code 13 -Output $out13 -Now $t0
Check '3. first refusal alerts' 1 $script:sent.Count
$null = Invoke-Run -Notify $notify -Episodes $eps -CandKey $keyA -RepoDir $cloneA -Code 13 -Output $out13 -Now $t0.AddMinutes(16)
Check '3. same refusal again: suppressed' 1 $script:sent.Count
$null = Invoke-Run -Notify $notify -Episodes $eps -CandKey $keyA -RepoDir $cloneA -Code 13 -Output (New-RefusedOutput -Check 'P3' -Detail 'preflight P3 failed: no core.hooksPath') -Now $t0.AddMinutes(32)
Check '3. a changed refusal alerts' 2 $script:sent.Count
$null = Invoke-Run -Notify $notify -Episodes $eps -CandKey $keyA -RepoDir $cloneA -Code 0 -Output $okOut -Now $t0.AddMinutes(48)
Check '3. A OK clears A notice key (nothing of A left behind)' $false $notify.ContainsKey("preflight-refused:$keyA")
$null = Invoke-Run -Notify $notify -Episodes $eps -CandKey $keyA -RepoDir $cloneA -Code 13 -Output (New-RefusedOutput -Check 'P3' -Detail 'preflight P3 failed: no core.hooksPath') -Now $t0.AddMinutes(64)
Check '3. the same refusal after the OK alerts again' 3 $script:sent.Count
# An OK row on a run that then failed (exit 1) still ends the refusal: the preflight passed.
$null = Invoke-Run -Notify $notify -Episodes $eps -CandKey $keyA -RepoDir $cloneA -Code 1 -Output @($okOut[0], 'conductor stopped: reason=adapter_error') -Now $t0.AddMinutes(80)
Check '3. OK row on exit 1 also clears' $false $notify.ContainsKey("preflight-refused:$keyA")
# An exit 1 WITHOUT the OK row ends nothing.
$null = Invoke-Run -Notify $notify -Episodes $eps -CandKey $keyA -RepoDir $cloneA -Code 13 -Output $out13 -Now $t0.AddMinutes(96)
$null = Invoke-Run -Notify $notify -Episodes $eps -CandKey $keyA -RepoDir $cloneA -Code 1 -Output @('boom') -Now $t0.AddMinutes(112)
Check '3. exit 1 without OK row keeps the notice' $true $notify.ContainsKey("preflight-refused:$keyA")

# --- 6. two clones of the same owner/repo: B's OK never clears A's refusal ------------------------------
Write-Host '6. A refused, B (other clone, same repo) OK, A refused again, A OK'
$notify = @{}; $eps = @{}; $script:sent = @()
$null = Invoke-Run -Notify $notify -Episodes $eps -CandKey $keyA -RepoDir $cloneA -Code 13 -Output $out13 -Now $t0
Check '6. A refused: A notified' 1 $script:sent.Count
$sigA = $notify["preflight-refused:$keyA"]
$null = Invoke-Run -Notify $notify -Episodes $eps -CandKey $keyB -RepoDir $cloneB -Code 0 -Output $okOut -Now $t0.AddMinutes(1)
Check '6. B OK: A signature remains' $sigA $notify["preflight-refused:$keyA"]
$null = Invoke-Run -Notify $notify -Episodes $eps -CandKey $keyA -RepoDir $cloneA -Code 13 -Output $out13 -Now $t0.AddMinutes(16)
Check '6. A refused again for the same reason: no second alert' 1 $script:sent.Count
$null = Invoke-Run -Notify $notify -Episodes $eps -CandKey $keyA -RepoDir $cloneA -Code 0 -Output $okOut -Now $t0.AddMinutes(32)
Check '6. A OK: only now is A cleared' $false $notify.ContainsKey("preflight-refused:$keyA")

# --- 7. an open deferred episode is untouched by exit 13; the next OK ends both ------------------------
Write-Host '7. deferred episode open, exit 13, then OK'
$notify = @{}; $eps = @{}; $script:sent = @()
$out11 = @('MINDWIRE_PREFLIGHT_DEFERRED_PAYLOAD {"owner": "SpirrowGames", "repo": "spirrow-mindwire", "endpoint": "repos/SpirrowGames/spirrow-mindwire", "attempts": 4, "last_stderr": "x", "reason": "unclassified", "checked": [], "unchecked": []}')
$null = Invoke-Run -Notify $notify -Episodes $eps -CandKey $keyA -RepoDir $cloneA -Code 11 -Output $out11 -Now $t0
$epBefore = $eps['SpirrowGames/spirrow-mindwire'] | ConvertTo-Json -Compress
$dKey = Get-PreflightDeferredNoticeKey -RepoKey 'SpirrowGames/spirrow-mindwire'
$dSig = $notify[$dKey]
$null = Invoke-Run -Notify $notify -Episodes $eps -CandKey $keyA -RepoDir $cloneA -Code 13 -Output $out13 -Now $t0.AddMinutes(16)
Check '7. exit 13: deferred episode record unchanged (first_at, count)' $epBefore ($eps['SpirrowGames/spirrow-mindwire'] | ConvertTo-Json -Compress)
Check '7. exit 13: deferred notice state unchanged' $dSig $notify[$dKey]
Check '7. exit 13: refusal alerted on its own key' $true $notify.ContainsKey("preflight-refused:$keyA")
$null = Invoke-Run -Notify $notify -Episodes $eps -CandKey $keyA -RepoDir $cloneA -Code 0 -Output $okOut -Now $t0.AddMinutes(32)
Check '7. OK ends the deferred episode' $false $eps.ContainsKey('SpirrowGames/spirrow-mindwire')
Check '7. OK ends the deferred notice key' $false $notify.ContainsKey($dKey)
Check '7. OK ends the candidate refusal' $false $notify.ContainsKey("preflight-refused:$keyA")

# --- 4. an existing retry-pending record survives exit 13 -----------------------------------------------
Write-Host '4. retry-pending kept through exit 13'
function Invoke-RunForRetry {
    param([hashtable]$RetryState, [string]$Key, [int]$Code, [datetime]$Now)
    if ($Code -eq $PreflightRefusedExitCode) { return 'refused' }
    if ($Code -ne 0) {
        $rec = New-RetryPendingRecord -FirstFailureAt $Now.ToString('o') -ExitCode $Code -StopReason 'adapter_error' `
            -ErrorCode 'adapter.x' -FailureHead 'msg-1' -FailureControl 'running' -SessionLogPath 'C:\log'
        return (Register-CandidateFailure -RetryState $RetryState -Key $Key -Record $rec -Now $Now).action
    }
    if (Register-CandidateSuccess -RetryState $RetryState -Key $Key -Now $Now) { return 'retry-recovered' }
    return 'ok'
}
$tA = $t0; $tB = $t0.AddMinutes(16); $tC = $t0.AddMinutes(32)
$rs = ConvertTo-RetryPendingState -Raw @{}
Check '4. role failure -> retry-scheduled (R)' 'retry-scheduled' (Invoke-RunForRetry -RetryState $rs -Key $keyA -Code 1 -Now $tA)
$before = $rs.pending[$keyA] | ConvertTo-Json -Depth 5
Check '4. exit 13 on the retry launch' 'refused' (Invoke-RunForRetry -RetryState $rs -Key $keyA -Code 13 -Now $tB)
Check '4. R unchanged (first_failure_at included)' $before ($rs.pending[$keyA] | ConvertTo-Json -Depth 5)
Check '4. next run exit 0 -> retry-recovered' 'retry-recovered' (Invoke-RunForRetry -RetryState $rs -Key $keyA -Code 0 -Now $tC)
Check '4. R gone after recovery' $false $rs.pending.ContainsKey($keyA)
$rs = ConvertTo-RetryPendingState -Raw @{}
$null = Invoke-RunForRetry -RetryState $rs -Key $keyA -Code 1 -Now $tA
$null = Invoke-RunForRetry -RetryState $rs -Key $keyA -Code 13 -Now $tB
$t = Register-CandidateFailure -RetryState $rs -Key $keyA -Now $tC -Record (New-RetryPendingRecord -FirstFailureAt $tC.ToString('o') -ExitCode 1 `
        -StopReason 'adapter_error' -ErrorCode 'adapter.x' -FailureHead 'msg-1' -FailureControl 'running' -SessionLogPath 'C:\log')
Check '4. next run exit 1 -> quarantine (R carried, its one retry still unused)' 'quarantine' $t.action
Check '4. carried first_failure_at is R' ($tA.ToString('o')) (ConvertTo-RetryIso $t.first.first_failure_at)

# --- launch-end bookkeeping skipped on exit 13 -----------------------------------------------------------
$script:retreatCalled = $false
$ro = Invoke-LaunchEndRetreat -RepoDir $cloneA -ThreadId 'T-a' -CandidateKey $keyA -HeadMsg 'msg-1' -StopReason '' -ExitCode 13 `
    -StateDir $cloneA -Retreat { $script:retreatCalled = $true }
Check 'launch-end retreat skipped on exit 13' 'skipped' $ro
Check 'retreat block not run on exit 13' $false $script:retreatCalled
Check 'exit 13 does not end a ref-snapshot episode' $false (Test-RefSnapshotEpisodeOver -ExitCode 13)

# --- 1 / 2. launches_same_head through the REAL head_skip commit/revert (uv) -------------------------
Write-Host ''
Write-Host '1/2. launches_same_head — real execution through uv'
$uvOk = $false
try { $null = & uv --version 2>$null; if ($LASTEXITCODE -eq 0) { $uvOk = $true } } catch { $uvOk = $false }
if (-not $uvOk) {
    $script:failures++
    Write-Host '  FAIL  uv not on PATH — the real commit/revert execution could not run'
}
else {
    foreach ($name in 'Get-BoundedProbeCommandLine', 'Invoke-BoundedUvProbe', 'Get-ProbeOutputLines',
                      'New-ProbeInputFile', 'Remove-ProbeInputFile',
                      'Invoke-HeadSkipCommitLaunch', 'Invoke-HeadSkipRevertLaunch') {
        $fn = $functions | Where-Object { $_.Name -eq $name } | Select-Object -First 1
        if (-not $fn) { throw "function not found in sweep script: $name" }
        Invoke-Expression $fn.Extent.Text
    }
    $HeadSkipProbeTimeoutSeconds = 120
    $ProbeKillGraceMs = 5000
    $ProbeInputFilePrefix = 'mindwire-probe-'
    $ProbeInputDirectory = [System.IO.Path]::GetTempPath()
    $hsState = Join-Path ([System.IO.Path]::GetTempPath()) ('head-skip-refused-test-{0}.json' -f [guid]::NewGuid().ToString('N'))
    try {
        # Initial state: no LAUNCH committed for head msg-9184, launches_same_head unrecorded.
        $hsPayload = [ordered]@{ thread_id = 'T-refused'; head_msg_id = 'msg-9184'; token = 'bohr'; token_raw = 'Bohr'
                                 attempts_after = 1; control_state = 'run'; head_fetched = $true; thread_status = '' }
        $initial = if (Test-Path -LiteralPath $hsState) { [System.IO.File]::ReadAllText($hsState) } else { '' }
        $maxSeen = 0
        $allReverted = $true
        for ($i = 1; $i -le 3; $i++) {
            $c = Invoke-HeadSkipCommitLaunch -Payload $hsPayload -StateFilePath $hsState
            Check "1. refused run ${i}: commit counts 1" 1 $c.launches_same_head
            if ($c.launches_same_head -gt $maxSeen) { $maxSeen = $c.launches_same_head }
            $rv = Invoke-HeadSkipRevertLaunch -CommitOutput $c.commit_output -StateFilePath $hsState
            if (-not $rv.ok) { $allReverted = $false; Write-Host "    revert error: $($rv.error)" }
            $now = if (Test-Path -LiteralPath $hsState) { [System.IO.File]::ReadAllText($hsState) } else { '' }
            $hasRecord = $now -match '"T-refused"'
            if ($hasRecord -and $now -match '"launches_same_head"\s*:\s*[1-9]') { $allReverted = $false }
        }
        Check '1. every revert succeeded and left no counted launch' $true $allReverted
        Check '1. three refused runs: max launches_same_head is 1 (never 3, no STALLED)' 1 $maxSeen
        $c4 = Invoke-HeadSkipCommitLaunch -Payload $hsPayload -StateFilePath $hsState
        Check '2. next real launch counts 1' 1 $c4.launches_same_head
    }
    finally { if (Test-Path -LiteralPath $hsState) { Remove-Item -LiteralPath $hsState -Force } }
}

# --- the real loop: ordering and shape (AST) ------------------------------------------------------------
$loop = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.ForEachStatementAst] -and
        $n.Variable.Extent.Text -eq '$cand' -and $n.Body.Extent.Text -match 'Invoke-HeadSkipCommitLaunch' }, $true) |
    Select-Object -First 1
Check 'sweep candidate loop found' $true ($null -ne $loop)
$body = $loop.Body.Extent.Text
$posInner = $body.IndexOf('& $inner')
$posOk = $body.IndexOf('Get-PreflightOkRepos -Output $output')
$posClear = $body.IndexOf('Clear-PreflightRefusedNotice -State $notifyState -CandidateKey $cand.key')
$posBranch = $body.IndexOf('if ($code -eq $PreflightRefusedExitCode)')
$posNonZero = $body.IndexOf('if ($code -ne 0) {')
Check 'refusal clear is keyed by THIS candidate and follows the OK row' $true ($posClear -gt $posOk -and $posOk -gt $posInner)
Check 'refusal clear needs the OK row' $true ($body -match 'if \(\$preflightOk\.seen -and \(Clear-PreflightRefusedNotice')
Check 'refusal clear precedes the exit-13 branch' $true ($posClear -lt $posBranch)
Check 'exit-13 branch precedes the generic non-zero (quarantine) branch' $true ($posBranch -gt $posInner -and $posBranch -lt $posNonZero)
Check '5. no per-repo skip exists for refusals (other candidates of the repo still launch)' $false ($body -match 'preflightRefusedRepoDirs|preflight-refused-skipped')

$branch = $loop.Body.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.IfStatementAst] -and
        $n.Clauses[0].Item1.Extent.Text -eq '$code -eq $PreflightRefusedExitCode' }, $true) | Select-Object -First 1
Check 'exit-13 branch found' $true ($null -ne $branch)
$bt = $branch.Clauses[0].Item2.Extent.Text
Check 'branch ends in continue (not break: the tick goes on)' $true ($bt -match '(?m)^\s*continue\s*$')
Check 'branch never breaks the tick' $false ($bt -match '(?m)^\s*break\s*$')
Check 'branch reverts the LAUNCH commit' $true ($bt -match 'Invoke-HeadSkipRevertLaunch')
Check 'branch notifies through Send-NotificationIfChanged' $true ($bt -match 'Send-NotificationIfChanged')
Check 'branch uses the per-candidate notice' $true ($bt -match 'Get-PreflightRefusedNotice -CandidateKey \$cand\.key')
Check 'branch never quarantines' $false ($bt -match 'quarantineState|New-QuarantineRecord')
Check 'branch never touches retry state transitions' $false ($bt -match 'Register-Candidate|New-RetryPendingRecord|pending\.Remove|pending\[\$cand\.key\]\s*=')
Check 'branch never counts toward K' $false ($bt -match 'sweepFailures')
Check 'branch gives back the retry-launched count' $true ($bt -match '\$retryLaunched--')
Check '5. branch marks no repo for skipping' $false ($bt -match 'RepoDirs\[')
Check '7. branch never touches the deferred episode' $false ($bt -match 'PreflightDeferred')
Check '3. branch pushes no decision material (no park, so no card to leave behind)' $false ($bt -match 'Send-HumanParkAlert|Push-DecisionMaterial')
Check 'exit 13 does not end a git-sync deferral episode' $true ($body -match '\$code -ne \$PreflightRefusedExitCode -and \$code -ne \$RefSnapshotUnrecordedExitCode')

if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) FAILED"; exit 1 }
Write-Host "all checks passed"
exit 0
