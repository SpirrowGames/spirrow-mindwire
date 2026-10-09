# Regression guard for the sweep's git-sync deferral branch (deploy/run-conductor-scheduled.ps1).
#
# ADR-2026-10-09-23 unit 1, design v1.2 B-2 (Bohr msg-6766, go Einstein msg-6765). When
# mindwire-loop exits $GitSyncDeferredExitCode (the GitHub API stayed transiently unavailable while
# naming the thread's branch) the wrapper must:
#   * NOT quarantine, NOT put the thread on retry-pending, NOT park the repo;
#   * revert the LAUNCH commit, so the deferral counts toward neither backoff nor STALLED;
#   * notify on a per-repo key on the same Send-NotificationIfChanged channel as the dirty clone,
#     even when the payload row is unreadable, and never on a GitHub credential key;
#   * keep going with the rest of the sweep.
#
# Functions are lifted out of the sweep script's AST (dot-sourcing would run the sweep); the
# loop's ORDERING is checked on the AST of the loop body.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }

$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'ConvertTo-DirtyCloneRepoKey', 'Get-GitSyncDeferredNotice') {
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

# --- exit code constant ---------------------------------------------------------------------------
$assign = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $n.Left.Extent.Text -eq '$GitSyncDeferredExitCode' }, $true) | Select-Object -First 1
Check 'GitSyncDeferredExitCode is assigned at script level' $true ($null -ne $assign)
$GitSyncDeferredExitCode = [int]$assign.Right.Extent.Text
Check 'GitSyncDeferredExitCode is 9 (not 1-8: taken)' 9 $GitSyncDeferredExitCode

# --- notice: payload parsed ---------------------------------------------------------------------
$base = Join-Path ([System.IO.Path]::GetTempPath()) 'MindWire-Clone'
$k = ConvertTo-DirtyCloneRepoKey -RepoDir $base
$okOutput = @(
    'some log line',
    'MINDWIRE_GIT_SYNC_DEFERRED_PAYLOAD {"thread_id": "T-a", "candidate": "loop/t-a", "detail": "HTTP 502"}'
)
$n = Get-GitSyncDeferredNotice -RepoDir $base -CandidateKey 'p::T-a' -Output $okOutput -ExitCode 9
Check 'notice key is per repo' "__git_sync_deferred__/$k" $n.key
Check 'signature from the candidate branch' 'git-sync-deferred:loop/t-a' $n.signature
Check 'body names the thread and the branch' $true ($n.message -like '*thread=T-a branch=loop/t-a*')

# --- notice: payload unreadable ------------------------------------------------------------------
$nb = Get-GitSyncDeferredNotice -RepoDir $base -CandidateKey 'p::T-a' -Output @('MINDWIRE_GIT_SYNC_DEFERRED_PAYLOAD {not json') -ExitCode 9
Check 'unreadable payload: key still per repo' "__git_sync_deferred__/$k" $nb.key
Check 'unreadable payload: parse-failed signature' 'git-sync-deferred:parse-failed' $nb.signature
$nn = Get-GitSyncDeferredNotice -RepoDir $base -CandidateKey 'p::T-a' -Output @() -ExitCode 9
Check 'missing payload: parse-failed signature' 'git-sync-deferred:parse-failed' $nn.signature
Check 'never a GitHub key' $false ($nb.key -like '*github*')

# --- the real loop: ordering and shape (AST) --------------------------------------------------------
$loop = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.ForEachStatementAst] -and
        $n.Variable.Extent.Text -eq '$cand' -and $n.Body.Extent.Text -match 'Invoke-HeadSkipCommitLaunch' }, $true) |
    Select-Object -First 1
Check 'sweep candidate loop found' $true ($null -ne $loop)
$body = $loop.Body.Extent.Text
$posInner = $body.IndexOf('& $inner')
$posBranch = $body.IndexOf('if ($code -eq $GitSyncDeferredExitCode)')
$posNonZero = $body.IndexOf('if ($code -ne 0) {')
Check 'deferral branch follows the launch' $true ($posBranch -gt $posInner)
Check 'deferral branch precedes the generic non-zero (quarantine) branch' $true ($posBranch -ge 0 -and $posBranch -lt $posNonZero)

$branch = $loop.Body.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.IfStatementAst] -and
        $n.Clauses[0].Item1.Extent.Text -eq '$code -eq $GitSyncDeferredExitCode' }, $true) | Select-Object -First 1
Check 'deferral branch found' $true ($null -ne $branch)
$branchText = $branch.Clauses[0].Item2.Extent.Text
Check 'branch ends in continue' $true ($branchText -match '(?m)^\s*continue\s*$')
Check 'branch reverts the LAUNCH commit' $true ($branchText -match 'Invoke-HeadSkipRevertLaunch')
Check 'branch notifies through Send-NotificationIfChanged' $true ($branchText -match 'Send-NotificationIfChanged')
Check 'branch never quarantines' $false ($branchText -match 'quarantineState|New-QuarantineRecord')
Check 'branch never parks the repo' $false ($branchText -match 'Set-DirtyCloneParked|dirtyRepoDirs')
Check 'branch never puts the thread on retry-pending' $false ($branchText -match 'retryPending|Set-RetryPending')

if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) FAILED"; exit 1 }
Write-Host "all checks passed"
exit 0
