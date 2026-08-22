# Regression guard for the sweep's head-skip nomination-predicate WIRING
# (deploy/run-conductor-scheduled.ps1). The two-stage judgment itself lives in
# scripts/head_skip_decide.py and src/spirrow_mindwire/conductor/head_skip.py — pytest reaches
# both. This file targets the seam the PowerShell wrapper adds around them, because a broken
# wire is the failure mode msg-1181 §F-3 measured in production: the predicate was written and
# tested, but the wrapper never called it and nobody noticed for 6 days.
#
# What is covered here (post-msg-1430 §W-1 / §W-2):
#   1. The deleted symbols are actually deleted. `Test-CanSkip`, `Get-HeadRecord`, and the
#      `$headsStatePath` variable must be absent from the sweep script — a rename or accidental
#      re-introduction would revive the "head equality → skip" predicate the module retired.
#   2. Nothing in the checkout still references state/heads.json. That file is deleted with its
#      readers/writers (msg-1432 §W-2 explicit: no conditions, no residual mirror), so a grep
#      hit anywhere is a wiring bug — either a code path was missed or a doc line will teach
#      the next reader that heads.json is still authoritative.
#   3. The new wiring helpers (`Invoke-HeadSkipDecide` / `Invoke-HeadSkipCommitLaunch` /
#      `Get-HeadSkipMode`) exist and parse. If any is renamed, the AST scan below fails
#      loudly — a caller depends on those exact names.
#   4. `Get-ConductorVerdict` still parses `last_msg=None` as an absence, not a head called
#      "None". This one is unrelated to the head-skip rewire but was covered in the previous
#      test file — its regression would still ship a silent bug, so it stays.
#
# What is NOT covered here (deliberately): the two-stage judgment (`stop-token` set, backoff
# math, park→resume detector, HEAD_CACHE_TTL). That is all in tests/test_head_skip.py under
# pytest — one place, one owner, not scattered across a PS harness that lifts functions out of
# the wrapper's AST.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
if (-not (Test-Path -LiteralPath $sweepScript)) { throw "sweep script not found: $sweepScript" }

$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) {
    $parseErrors | ForEach-Object { Write-Host "PARSE ERROR line $($_.Extent.StartLineNumber): $($_.Message)" }
    throw "deploy/run-conductor-scheduled.ps1 does not parse"
}

# Get-JsonState logs through Write-Log on unreadable input; stub it so the lift stays self-contained.
function Write-Log { param([string]$Message) }

$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)

function Get-FunctionAst {
    param([string]$Name)
    return ($functions | Where-Object { $_.Name -eq $Name } | Select-Object -First 1)
}

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) {
        Write-Host ("  PASS  {0}" -f $Name)
    }
    else {
        $script:failures++
        Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual)
    }
}

Write-Host "W-1 — the retired equality predicate is GONE (msg-1428/1430 §W-1)"
# The old head-equality skip was the exact bug the head-skip module was written to end. Keeping
# even a dead-code copy of `Test-CanSkip` would let a future reader wire it back by accident;
# the frozen spec is explicit that revert is via `git revert`, not a flag.
Check "Test-CanSkip is absent" $null (Get-FunctionAst -Name 'Test-CanSkip')
Check "Get-HeadRecord is absent" $null (Get-FunctionAst -Name 'Get-HeadRecord')

Write-Host "W-2 — the wrapper's AST contains no LIVE references to heads.json / headsState (msg-1432 §W-2)"
# Msg-1432's requirement: readers / writers / merge-on-write / path def / load are ALL removed as
# one atomic change. Comments that document the removal are welcome — they teach the next reader
# not to re-add the state file. What we forbid is executable code that still refers to the deleted
# names: an unused variable reference, a Join-Path expression building the old path, a call site
# that survived the edit. Those are the wiring bugs the AST scan pins.
$forbiddenVars = @('headsState', 'headsStatePath', 'headsOriginalKeys', 'mergedHeads')
$varRefs = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)
$badVars = @()
foreach ($v in $varRefs) {
    if ($forbiddenVars -contains $v.VariablePath.UserPath) { $badVars += $v.VariablePath.UserPath }
}
if ($badVars.Count -gt 0) {
    $script:failures++
    Write-Host "  FAIL  sweep AST still uses forbidden variables: $($badVars -join ', ')"
}
else {
    Write-Host "  PASS  no live sweep-AST references to $($forbiddenVars -join ' / ')"
}
# The old path literal, in either separator style. Belongs only to comments now; a StringConstant
# in the AST would mean the path is still being computed somewhere.
$stringConsts = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true)
$badPaths = @($stringConsts | Where-Object { $_.Value -match 'heads\.json$' })
if ($badPaths.Count -gt 0) {
    $script:failures++
    Write-Host "  FAIL  sweep AST still contains the literal path 'heads.json': $($badPaths | ForEach-Object { $_.Value }) -join ', ')"
}
else {
    Write-Host "  PASS  no live 'heads.json' path literals in sweep AST"
}

Write-Host "W-2 — the CLI wiring helpers exist under their contract names"
# The three PS helpers callers depend on. Renaming any of them without updating its call site
# is a silent contract break; keep the names pinned here.
Check "Invoke-HeadSkipDecide exists" $true ($null -ne (Get-FunctionAst -Name 'Invoke-HeadSkipDecide'))
Check "Invoke-HeadSkipCommitLaunch exists" $true ($null -ne (Get-FunctionAst -Name 'Invoke-HeadSkipCommitLaunch'))
Check "Get-HeadSkipMode exists" $true ($null -ne (Get-FunctionAst -Name 'Get-HeadSkipMode'))

Write-Host "Get-HeadSkipMode — env var routes to CLI mode, unknowns fall back to 'decide'"
$fn = Get-FunctionAst -Name 'Get-HeadSkipMode'
Invoke-Expression $fn.Extent.Text
# Save + restore the caller's env in case the harness itself runs with the var set.
$origMode = $env:MINDWIRE_HEADSKIP_MODE
try {
    $env:MINDWIRE_HEADSKIP_MODE = $null
    Check "unset -> decide" 'decide' (Get-HeadSkipMode)
    $env:MINDWIRE_HEADSKIP_MODE = 'decide'
    Check "explicit decide -> decide" 'decide' (Get-HeadSkipMode)
    $env:MINDWIRE_HEADSKIP_MODE = 'report'
    Check "report -> report (dry-run)" 'report' (Get-HeadSkipMode)
    $env:MINDWIRE_HEADSKIP_MODE = 'garbage'
    Check "unknown value falls back to decide (not to a silent no-op)" 'decide' (Get-HeadSkipMode)
}
finally {
    if ($null -eq $origMode) { Remove-Item Env:\MINDWIRE_HEADSKIP_MODE -ErrorAction SilentlyContinue }
    else { $env:MINDWIRE_HEADSKIP_MODE = $origMode }
}

Write-Host "Get-ConductorVerdict — 'last_msg=None' is absence, not a head called 'None'"
# Unchanged carry-over from the prior test suite. The wrapper's own verdict parsing still owns
# this rule regardless of what happens on the head-skip side.
foreach ($name in 'Get-ConductorVerdict', 'Get-JsonState', 'Save-JsonState') {
    $fn = Get-FunctionAst -Name $name
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}
$v = Get-ConductorVerdict -Output @('conductor stopped: reason=hold rounds=0 forced_naysayer=0 last_msg=None')
Check "None becomes null" $null $v.last_msg
Check "reason still parsed" 'hold' $v.reason
$v = Get-ConductorVerdict -Output @('conductor stopped: reason=human rounds=17 forced_naysayer=0 last_msg=msg-2167')
Check "real id parsed" 'msg-2167' $v.last_msg
Check "rounds parsed" 17 $v.rounds

Write-Host ""
if ($script:failures -gt 0) {
    Write-Host "sweep head-cache: $($script:failures) check(s) FAILED"
    exit 1
}
Write-Host "sweep head-cache: all checks passed"
exit 0
