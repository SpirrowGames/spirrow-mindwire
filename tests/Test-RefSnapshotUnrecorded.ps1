# Regression guard for the sweep's ref-snapshot-unrecorded branch (deploy/run-conductor-scheduled.ps1).
#
# T-ref-snapshot-unreadable-file-blocks-thread-forever, design v2 D-2 / D-3 (Bohr msg-9433, go from
# Einstein after it). When mindwire-loop exits $RefSnapshotUnrecordedExitCode (the implementer
# launch's ref snapshot file could not be read, recovered or written; the implementer never
# started) the wrapper must:
#   * NOT park the repo — exit 8 used to, so one thread's state file stopped every other candidate
#     on the same clone every tick (Bohr msg-9431 判断 1). Other candidates on the repo launch in
#     the same sweep;
#   * revert the LAUNCH commit (no backoff, no launches_same_head / STALLED count);
#   * NOT quarantine and NOT put the thread on retry-pending;
#   * notify on a per-CANDIDATE key `__ref_snapshot__/<repo>/<thread>`, naming the file path, the
#     failure kind, the OSError and the remedy, even when the payload row is unreadable;
#   * drop that key once a later launch of the candidate gets past the snapshot.
#
# Functions are lifted out of the sweep script's AST (dot-sourcing would run the sweep); the loop's
# ORDERING and the branch's shape are checked on the AST of the loop body.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }

$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'ConvertTo-DirtyCloneRepoKey', 'Test-DirtyCloneSkip', 'Get-RefSnapshotNoticeKey',
                  'Get-RefSnapshotNotice', 'Clear-RefSnapshotNotice', 'Test-RefSnapshotEpisodeOver',
                  'Test-NotificationSuppressed') {
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

function Get-ScriptInt([string]$Var) {
    $a = $ast.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $n.Left.Extent.Text -eq $Var }, $true) | Select-Object -First 1
    if ($null -eq $a) { return $null }
    return [int]$a.Right.Extent.Text
}

# --- exit code constants ---------------------------------------------------------------------------
$RefSnapshotUnrecordedExitCode = Get-ScriptInt '$RefSnapshotUnrecordedExitCode'
$DirtyCloneExitCode = Get-ScriptInt '$DirtyCloneExitCode'
$GitSyncDeferredExitCode = Get-ScriptInt '$GitSyncDeferredExitCode'
$PreflightDeferredExitCode = Get-ScriptInt '$PreflightDeferredExitCode'
Check 'RefSnapshotUnrecordedExitCode is 12' 12 $RefSnapshotUnrecordedExitCode
Check 'not the dirty-clone code 8' $true ($RefSnapshotUnrecordedExitCode -ne $DirtyCloneExitCode)

# --- notice: payload parsed ---------------------------------------------------------------------
$base = Join-Path ([System.IO.Path]::GetTempPath()) 'MindWire-Clone'
$k = ConvertTo-DirtyCloneRepoKey -RepoDir $base
$okOutput = @(
    'some log line',
    'MINDWIRE_REF_SNAPSHOT_UNRECORDED_PAYLOAD {"reason": "ref_snapshot_unrecorded", "kind": "write", "path": "C:\\state\\turn_ref_snapshots\\p\\T-a.json", "project": "p", "thread_id": "T-a", "error": "[Errno 28] No space left on device", "remedy": "state ディレクトリの空き容量と権限を確認してください。"}'
)
$n = Get-RefSnapshotNotice -RepoDir $base -CandidateKey 'p::T-a' -Output $okOutput -ExitCode 12
Check 'notice key is per candidate (repo + thread)' "__ref_snapshot__/$k/p::T-a" $n.key
Check 'signature names the failure kind' 'ref-snapshot-unrecorded:write' $n.signature
Check 'body names the reason and kind' $true ($n.message -like '*reason=ref_snapshot_unrecorded kind=write*')
Check 'body names the file path' $true ($n.message -like '*file: C:\state\turn_ref_snapshots\p\T-a.json*')
Check 'body carries the OSError' $true ($n.message -like '*No space left on device*')
Check 'body carries the remedy' $true ($n.message -like '*空き容量と権限*')
Check 'body says the repo is not parked' $true ($n.message -like '*駐機せず*')

# --- notice: payload unreadable -------------------------------------------------------------------
$nb = Get-RefSnapshotNotice -RepoDir $base -CandidateKey 'p::T-a' -Output @('MINDWIRE_REF_SNAPSHOT_UNRECORDED_PAYLOAD {not json') -ExitCode 12
Check 'unreadable payload: key still per candidate' "__ref_snapshot__/$k/p::T-a" $nb.key
Check 'unreadable payload: parse-failed signature' 'ref-snapshot-unrecorded:parse-failed' $nb.signature
Check 'unreadable payload: remedy still shown' $true ($nb.message -like '*空き容量と権限*')
Check 'never a dirty-clone or GitHub key' $false ($nb.key -like '*dirty_clone*' -or $nb.key -like '*github*')

# --- two threads on one repo: separate keys, debounce per thread -----------------------------------
$outB = @('MINDWIRE_REF_SNAPSHOT_UNRECORDED_PAYLOAD {"reason": "ref_snapshot_unrecorded", "kind": "read", "path": "x", "error": "e"}')
$nB = Get-RefSnapshotNotice -RepoDir $base -CandidateKey 'p::T-b' -Output $outB -ExitCode 12
Check 'two threads on one repo get different keys' $true ($n.key -ne $nB.key)
$state = @{}
$sent = 0
foreach ($tick in 1..3) {
    foreach ($x in $n, $nB) {
        if (-not (Test-NotificationSuppressed -State $state -Key $x.key -Signature $x.signature)) {
            $sent++; $state[$x.key] = $x.signature
        }
    }
}
Check 'repeated failures over 3 ticks alert once per thread' 2 $sent

# --- the episode ends once the candidate gets past the snapshot --------------------------------------
Check 'exit 0 ends the episode' $true (Test-RefSnapshotEpisodeOver -ExitCode 0)
Check 'exit 1 ends the episode (the snapshot step was passed)' $true (Test-RefSnapshotEpisodeOver -ExitCode 1)
Check 'git-sync deferral (after the snapshot) ends the episode' $true (Test-RefSnapshotEpisodeOver -ExitCode $GitSyncDeferredExitCode)
Check 'exit 12 does not end it' $false (Test-RefSnapshotEpisodeOver -ExitCode $RefSnapshotUnrecordedExitCode)
Check 'dirty-clone (before the snapshot) does not end it' $false (Test-RefSnapshotEpisodeOver -ExitCode $DirtyCloneExitCode)
Check 'preflight deferral (before the snapshot) does not end it' $false (Test-RefSnapshotEpisodeOver -ExitCode $PreflightDeferredExitCode)
Check 'clear removes the candidate key' $true (Clear-RefSnapshotNotice -State $state -RepoDir $base -CandidateKey 'p::T-a')
Check 'clear leaves the other thread alone' $true ($state.ContainsKey($nB.key))
Check 'clear of an absent key reports false' $false (Clear-RefSnapshotNotice -State $state -RepoDir $base -CandidateKey 'p::T-a')
Check 'next failure of the cleared thread alerts again' $false (Test-NotificationSuppressed -State $state -Key $n.key -Signature $n.signature)

# --- the real loop: ordering and shape (AST) --------------------------------------------------------
$loop = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.ForEachStatementAst] -and
        $n.Variable.Extent.Text -eq '$cand' -and $n.Body.Extent.Text -match 'Invoke-HeadSkipCommitLaunch' }, $true) |
    Select-Object -First 1
Check 'sweep candidate loop found' $true ($null -ne $loop)
$body = $loop.Body.Extent.Text
$posInner = $body.IndexOf('& $inner')
$posBranch = $body.IndexOf('if ($code -eq $RefSnapshotUnrecordedExitCode)')
$posNonZero = $body.IndexOf('if ($code -ne 0) {')
Check 'branch follows the launch' $true ($posBranch -gt $posInner)
Check 'branch precedes the generic non-zero (retry / quarantine) branch' $true ($posBranch -ge 0 -and $posBranch -lt $posNonZero)

$branch = $loop.Body.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.IfStatementAst] -and
        $n.Clauses[0].Item1.Extent.Text -eq '$code -eq $RefSnapshotUnrecordedExitCode' }, $true) | Select-Object -First 1
Check 'branch found' $true ($null -ne $branch)
$branchText = $branch.Clauses[0].Item2.Extent.Text
Check 'branch ends in continue (the sweep goes on)' $true ($branchText -match '(?m)^\s*continue\s*$')
Check 'branch reverts the LAUNCH commit (no backoff / STALLED count)' $true ($branchText -match 'Invoke-HeadSkipRevertLaunch')
Check 'branch notifies on the per-candidate notice' $true ($branchText -match 'Get-RefSnapshotNotice' -and $branchText -match 'Send-NotificationIfChanged')
Check 'branch never quarantines' $false ($branchText -match 'quarantineState|New-QuarantineRecord|quarantineReason')
Check 'branch never parks the repo' $false ($branchText -match 'Set-DirtyCloneParked|dirtyRepoDirs|dirtyClonesParked|preflightDeferredRepoDirs')
Check 'branch never puts the thread on retry-pending' $false ($branchText -match 'retryPending|Set-RetryPending|retryState')
Check 'branch never uses the dirty-clone notice' $false ($branchText -match 'Get-DirtyCloneNotice')

# Behavioural: a repo is skipped only through $dirtyRepoDirs, which this branch never writes, so the
# next candidate on the same repo_dir is not skipped this sweep.
$dirtyRepoDirs = @{}
Check 'same-repo sibling is not skipped after an exit 12' $false (Test-DirtyCloneSkip -RepoDir $base -DirtyRepoDirs $dirtyRepoDirs)

$loopText = $loop.Body.Extent.Text
$posClear = $loopText.IndexOf('Clear-RefSnapshotNotice')
Check 'loop clears the key after the launch' $true ($posClear -gt $posInner)
Check 'the clear is gated on Test-RefSnapshotEpisodeOver' $true ($loopText -match '\(Test-RefSnapshotEpisodeOver -ExitCode \$code\) -and\s+\(Clear-RefSnapshotNotice')
Check 'git-sync clear is skipped on exit 12 (snapshot precedes git-sync)' $true ($loopText -match '\$code -ne \$RefSnapshotUnrecordedExitCode -and\s+\$code -ne \$PreflightDeferredExitCode -and \$code -ne \$GitSyncDeferredExitCode -and\s+\(Clear-GitSyncDeferredNotice')

# The launch-end clone retreat is skipped on exit 12 (the clone was not touched).
$retreat = $functions | Where-Object { $_.Name -eq 'Invoke-LaunchEndRetreat' } | Select-Object -First 1
Check 'launch-end retreat skips exit 12' $true ($retreat.Extent.Text -match '\$ExitCode -eq \$RefSnapshotUnrecordedExitCode\) \{ return ''skipped'' \}')

if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) FAILED"; exit 1 }
Write-Host "all checks passed"
exit 0
