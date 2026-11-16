# Regression guard for the sweep's turn-isolation stop branch (deploy/run-conductor-scheduled.ps1).
#
# ADR-2026-10-09-23 unit 2a wiring (Bohr msg-9439 W1-W3 / msg-9441, go Einstein). When
# mindwire-loop exits $TurnIsolationStopExitCode (10: the host's turn isolation refused the launch
# before any turn started) the wrapper must:
#   * NOT quarantine, NOT park the repo, and NOT consume an existing retry-pending;
#   * revert the LAUNCH commit, so the stop counts toward neither backoff nor STALLED;
#   * notify once per host on a host-wide key, with the daemon's stop reason as the signature;
#   * stop launching for the rest of the tick (break), since every candidate would hit it too.
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
foreach ($name in 'Get-TurnIsolationStopNotice', 'Test-NotificationSuppressed') {
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
        $n.Left.Extent.Text -eq '$TurnIsolationStopExitCode' }, $true) | Select-Object -First 1
Check 'TurnIsolationStopExitCode is assigned at script level' $true ($null -ne $assign)
Check 'TurnIsolationStopExitCode is 10' 10 ([int]$assign.Right.Extent.Text)

# --- notice ---------------------------------------------------------------------------------------
$out = @('INFO something', 'turn-isolation: STOP turn-isolation-check timed out', 'tail')
$n = Get-TurnIsolationStopNotice -CandidateKey 'p::T-a' -Output $out -ExitCode 10
Check 'notice key is host-wide' '__turn_isolation__' $n.key
Check 'notice signature carries the reason' 'turn-isolation:turn-isolation-check timed out' $n.signature
Check 'notice reason parsed' 'turn-isolation-check timed out' $n.reason
Check 'message names the candidate' $true ($n.message -match 'p::T-a')
$n2 = Get-TurnIsolationStopNotice -CandidateKey 'p::T-b' -Output $out -ExitCode 10
Check 'same reason from another candidate has the same key and signature' $true (($n2.key -eq $n.key) -and ($n2.signature -eq $n.signature))
$nm = Get-TurnIsolationStopNotice -CandidateKey 'p::T-a' -Output @('no stop line') -ExitCode 10
Check 'missing stop line still notifies with a fixed signature' 'turn-isolation:stop-line-missing' $nm.signature

# --- the real loop: ordering and shape (AST) --------------------------------------------------------
$loop = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.ForEachStatementAst] -and
        $n.Variable.Extent.Text -eq '$cand' -and $n.Body.Extent.Text -match 'Invoke-HeadSkipCommitLaunch' }, $true) |
    Select-Object -First 1
Check 'sweep candidate loop found' $true ($null -ne $loop)
$body = $loop.Body.Extent.Text
$posInner = $body.IndexOf('& $inner')
$posBranch = $body.IndexOf('if ($code -eq $TurnIsolationStopExitCode)')
$posNonZero = $body.IndexOf('if ($code -ne 0) {')
Check 'stop branch follows the launch' $true ($posBranch -gt $posInner)
Check 'stop branch precedes the generic non-zero (quarantine) branch' $true ($posBranch -ge 0 -and $posBranch -lt $posNonZero)

$branch = $loop.Body.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.IfStatementAst] -and
        $n.Clauses[0].Item1.Extent.Text -eq '$code -eq $TurnIsolationStopExitCode' }, $true) | Select-Object -First 1
Check 'stop branch found' $true ($null -ne $branch)
$branchText = $branch.Clauses[0].Item2.Extent.Text
Check 'branch ends the tick (break)' $true ($branchText -match '(?m)^\s*break\s*$')
Check 'branch reverts the LAUNCH commit' $true ($branchText -match 'Invoke-HeadSkipRevertLaunch')
Check 'branch notifies through Send-NotificationIfChanged' $true ($branchText -match 'Send-NotificationIfChanged')
Check 'branch never quarantines' $false ($branchText -match 'quarantineState|New-QuarantineRecord')
Check 'branch never parks the repo' $false ($branchText -match 'Set-DirtyCloneParked|dirtyRepoDirs')
Check 'branch never writes retry-pending' $false ($branchText -match 'Set-RetryPending|pending\.Remove|pending\[')

if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) FAILED"; exit 1 }
Write-Host "all checks passed"
exit 0
