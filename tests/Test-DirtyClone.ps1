# Regression guard for the sweep's dirty-shared-clone branch (deploy/run-conductor-scheduled.ps1).
#
# T-timed-out-implementer-turn-leaves-dirty-shared-clone, design v4 D-2 (Bohr msg-5781, endorsed by
# Einstein msg-5782). When mindwire-loop exits $DirtyCloneExitCode the wrapper must:
#   * NOT quarantine and NOT put the thread on retry-pending (the clone is at fault, not the thread);
#   * skip the rest of this tick's candidates on the SAME repo_dir (case / separator insensitive);
#   * keep launching candidates on OTHER repos (Einstein msg-5778 #1);
#   * notify on the per-repo key __dirty_clone__/<repo>, even when the payload row is unreadable,
#     and never on a GitHub credential key (Einstein msg-5776 #2).
#
# Functions are lifted out of the sweep script's AST (dot-sourcing would run the sweep), and the
# loop's ORDERING — which a lifted function cannot show — is checked on the AST of the loop body.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }

$script:logLines = @()
function Write-Log { param([string]$Message) $script:logLines += $Message }
$script:sent = @()
function Send-Notification { param([string]$Message) $script:sent += $Message; return @{ status = 'sent' } }

$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'ConvertTo-DirtyCloneRepoKey', 'Test-DirtyCloneSkip', 'Get-DirtyCloneNotice',
                  'Test-NotificationSuppressed', 'Send-NotificationIfChanged') {
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
        $n.Left.Extent.Text -eq '$DirtyCloneExitCode' }, $true) | Select-Object -First 1
Check 'DirtyCloneExitCode is assigned at script level' $true ($null -ne $assign)
$DirtyCloneExitCode = [int]$assign.Right.Extent.Text
Check 'DirtyCloneExitCode is 8 (not 5/6/7: wall-clock budget codes)' 8 $DirtyCloneExitCode

# --- repo key normalisation -------------------------------------------------------------------------
$base = Join-Path ([System.IO.Path]::GetTempPath()) 'MindWire-Clone'
$k1 = ConvertTo-DirtyCloneRepoKey -RepoDir $base
$k2 = ConvertTo-DirtyCloneRepoKey -RepoDir ($base.ToUpperInvariant() + [System.IO.Path]::DirectorySeparatorChar)
$k3 = ConvertTo-DirtyCloneRepoKey -RepoDir ($base.Replace('\', '/'))
Check 'repo key ignores case and a trailing separator' $k1 $k2
Check 'repo key ignores / vs \ separators' $k1 $k3

# --- notice: payload parsed ---------------------------------------------------------------------
$okOutput = @(
    'some log line',
    'MINDWIRE_DIRTY_CLONE_PAYLOAD {"repo_dir": "x", "reason": "wrong_head", "head": "feature/a", "porcelain": [], "detail": "HEAD is on feature/a"}'
)
$n = Get-DirtyCloneNotice -RepoDir $base -CandidateKey 'p::T-a' -Output $okOutput -ExitCode 8
Check 'notice key is per repo' "__dirty_clone__/$k1" $n.key
Check 'signature from reason + head' 'dirty-clone:wrong_head:feature/a' $n.signature
Check 'body names the reason' $true ($n.message -like '*reason=wrong_head*')

# --- notice: payload unreadable ------------------------------------------------------------------
$badOutput = @('MINDWIRE_DIRTY_CLONE_PAYLOAD {not json')
$nb = Get-DirtyCloneNotice -RepoDir $base -CandidateKey 'p::T-a' -Output $badOutput -ExitCode 8
Check 'unreadable payload: key still per repo' "__dirty_clone__/$k1" $nb.key
Check 'unreadable payload: parse-failed signature' 'dirty-clone:parse-failed' $nb.signature
$nn = Get-DirtyCloneNotice -RepoDir $base -CandidateKey 'p::T-a' -Output @() -ExitCode 8
Check 'missing payload: parse-failed signature' 'dirty-clone:parse-failed' $nn.signature
Check 'never a GitHub key' $false ($nb.key -like '*github*')

# --- simulated tick: two repos, A dirty, B clean ----------------------------------------------------
$repoA = Join-Path ([System.IO.Path]::GetTempPath()) 'clone-A'
$repoB = Join-Path ([System.IO.Path]::GetTempPath()) 'clone-B'
$candidates = @(
    @{ key = 'p::T-a1'; repo_dir = $repoA },
    @{ key = 'q::T-b1'; repo_dir = $repoB },
    @{ key = 'p::T-a2'; repo_dir = $repoA.ToUpperInvariant() },
    @{ key = 'q::T-b2'; repo_dir = $repoB }
)
$exitByRepo = @{ (ConvertTo-DirtyCloneRepoKey $repoA) = 8; (ConvertTo-DirtyCloneRepoKey $repoB) = 0 }
$dirtyRepoDirs = @{}
$dispositions = @{}
$launchedKeys = @()
$notifyState = @{}
$script:sent = @()
foreach ($cand in $candidates) {
    if (Test-DirtyCloneSkip -RepoDir $cand.repo_dir -DirtyRepoDirs $dirtyRepoDirs) {
        $dispositions[$cand.key] = 'dirty-clone-skip'
        continue
    }
    $launchedKeys += $cand.key
    $code = $exitByRepo[(ConvertTo-DirtyCloneRepoKey $cand.repo_dir)]
    if ($code -eq $DirtyCloneExitCode) {
        $dispositions[$cand.key] = 'dirty-clone'
        $notice = Get-DirtyCloneNotice -RepoDir $cand.repo_dir -CandidateKey $cand.key -Output @() -ExitCode $code
        $dirtyRepoDirs[$notice.repo_key] = $true
        Send-NotificationIfChanged -State $notifyState -Key $notice.key -Signature $notice.signature -Message $notice.message
        continue
    }
    $dispositions[$cand.key] = 'worked'
}
Check 'A first candidate launched and refused' 'dirty-clone' $dispositions['p::T-a1']
Check 'A second candidate (different case) skipped' 'dirty-clone-skip' $dispositions['p::T-a2']
Check 'B candidates still launched' 'worked,worked' (($dispositions['q::T-b1'], $dispositions['q::T-b2']) -join ',')
Check 'A launched once only' 3 $launchedKeys.Count
Check 'one notification' 1 $script:sent.Count

# --- the real loop: ordering and shape (AST) --------------------------------------------------------
$loop = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.ForEachStatementAst] -and
        $n.Variable.Extent.Text -eq '$cand' -and $n.Body.Extent.Text -match 'Invoke-HeadSkipCommitLaunch' }, $true) |
    Select-Object -First 1
Check 'sweep candidate loop found' $true ($null -ne $loop)
$body = $loop.Body.Extent.Text
$posSkip = $body.IndexOf('Test-DirtyCloneSkip')
$posCommit = $body.IndexOf('Invoke-HeadSkipCommitLaunch')
$posInner = $body.IndexOf('& $inner')
$posDirtyBranch = $body.IndexOf('if ($code -eq $DirtyCloneExitCode)')
$posNonZero = $body.IndexOf('if ($code -ne 0) {')
Check 'skip check precedes commit-launch (a skipped candidate is not counted as launched)' $true ($posSkip -ge 0 -and $posSkip -lt $posCommit)
Check 'skip check precedes the launch' $true ($posSkip -lt $posInner)
Check 'dirty-clone branch follows the launch' $true ($posDirtyBranch -gt $posInner)
Check 'dirty-clone branch precedes the generic non-zero (quarantine) branch' $true ($posDirtyBranch -ge 0 -and $posDirtyBranch -lt $posNonZero)

$branch = $loop.Body.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.IfStatementAst] -and
        $n.Clauses[0].Item1.Extent.Text -eq '$code -eq $DirtyCloneExitCode' }, $true) | Select-Object -First 1
$branchText = $branch.Clauses[0].Item2.Extent.Text
Check 'branch ends in continue' $true ($branchText -match '(?m)^\s*continue\s*$')
Check 'branch never quarantines' $false ($branchText -match 'quarantineState|New-QuarantineRecord')
Check 'branch never schedules a retry' $false ($branchText -match 'Register-CandidateFailure|New-RetryPendingRecord')
Check 'branch records the repo as dirty' $true ($branchText -match '\$dirtyRepoDirs\[')
Check 'branch does not stop the tick' $false ($branchText -match '(?m)^\s*break\s*$')

# --- exit 2 regression: the env-terminal branch is untouched by the new one ----------------------
$env2 = $loop.Body.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.IfStatementAst] -and
        $n.Clauses[0].Item1.Extent.Text -eq '$code -eq 2' }, $true) | Select-Object -First 1
$env2Text = $env2.Clauses[0].Item2.Extent.Text
Check 'exit 2 branch still present' $true ($null -ne $env2)
Check 'exit 2 still alerts on the GitHub credential fallback key' $true ($env2Text -match '__github_credential__')
Check 'exit 2 still does not quarantine' $false ($env2Text -match 'quarantineState|New-QuarantineRecord|Register-CandidateFailure')
Check 'exit 2 still continues the sweep' $true ($env2Text -match '(?m)^\s*continue\s*$')
Check 'exit 2 branch does not mark the repo dirty' $false ($env2Text -match 'dirtyRepoDirs')
$posEnv2 = $body.IndexOf('if ($code -eq 2)')
Check 'exit 2 branch still precedes the generic non-zero branch' $true ($posEnv2 -ge 0 -and $posEnv2 -lt $posNonZero)

if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) FAILED"; exit 1 }
Write-Host "all dirty-clone checks passed"
