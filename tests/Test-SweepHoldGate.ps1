# Regression guard for the sweep's HOLD short-circuit (deploy/run-conductor-scheduled.ps1).
#
# The rule this protects: a held project's launch may be optimised away ONLY after the loop has
# acknowledged the hold. `desired_state` is what the operator asked for; `observed_state` is the
# loop's acknowledgement, and only a launched conductor writes it. Skipping on `desired` alone
# starves that write-back, so the dashboard shows the hold as pending forever and an operator cannot
# tell "stopping" from "stopped".
#
# It is not hypothetical. On 2026-08-06 both projects sat at `hold` and `spirrow-voxelworld` had
# `observed_state: null` — never acknowledged, because never launched. `spirrow-mindwire` had
# `observed: hold` only because its control probe FAILED that tick, so the sweep failed open,
# launched, and the conductor landed the write-back. A transient outage was the sole reason the
# feedback loop ever closed. Found by the Tier B naysayer reviewing PR #126.
#
# Functions are lifted from the script's AST rather than dot-sourced, because dot-sourcing would run
# the sweep: probe the chatroom, rewrite mindwire.toml and launch the conductor.

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

$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
$fn = $functions | Where-Object { $_.Name -eq 'Test-HoldObserved' } | Select-Object -First 1
if (-not $fn) { throw "function not found in sweep script: Test-HoldObserved" }
Invoke-Expression $fn.Extent.Text

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual) }
}

# Shaped like Invoke-ControlProbe's return value (ConvertFrom-Json of loop_control.py's output).
function New-Control {
    param([string]$Desired, $Observed)
    return ('{"project":"p","desired_state":"' + $Desired + '","observed_state":' +
        $(if ($null -eq $Observed) { 'null' } else { '"' + $Observed + '"' }) +
        ',"configured":true}' | ConvertFrom-Json)
}

Write-Host "Test-HoldObserved — a hold is only optimised away after the loop acknowledges it"
Check "hold acknowledged -> may skip the launch" $true `
    (Test-HoldObserved -Control (New-Control -Desired 'hold' -Observed 'hold'))

# The regression. Every one of these must still launch, or the acknowledgement never lands.
Check "hold never acknowledged (null) -> MUST launch" $false `
    (Test-HoldObserved -Control (New-Control -Desired 'hold' -Observed $null))
Check "hold not yet acknowledged (still run) -> MUST launch" $false `
    (Test-HoldObserved -Control (New-Control -Desired 'hold' -Observed 'run'))
Check "hold not yet acknowledged (still supervised) -> MUST launch" $false `
    (Test-HoldObserved -Control (New-Control -Desired 'hold' -Observed 'supervised'))

Write-Host "Test-HoldObserved — never withholds a launch the operator did not ask to withhold"
Check "desired run -> launch" $false `
    (Test-HoldObserved -Control (New-Control -Desired 'run' -Observed 'run'))
Check "desired supervised -> launch" $false `
    (Test-HoldObserved -Control (New-Control -Desired 'supervised' -Observed 'supervised'))
# A stale `observed: hold` under a live `desired: run` is exactly a release from hold; skipping there
# would reproduce the bug PR #127 fixed on the other side of the same state machine.
Check "released to run, observed still hold -> launch" $false `
    (Test-HoldObserved -Control (New-Control -Desired 'run' -Observed 'hold'))

Write-Host "Test-HoldObserved — an unreadable probe fails OPEN"
Check "null control -> launch" $false (Test-HoldObserved -Control $null)

Write-Host ""
if ($script:failures -gt 0) { Write-Host "sweep hold gate: $($script:failures) check(s) FAILED"; exit 1 }
Write-Host "sweep hold gate: all checks passed"
exit 0
