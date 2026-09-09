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

# Test-HoldForCandidate is the resource-axis addition (T-loop-control-keyed-by-project-resource-
# is-the-repo §5a v2). It is a PURE function so it MUST be extractable via the same AST idiom.
# If someone inlines it into the main loop the test loses coverage silently — Einstein's E-47 in
# the specifying thread is what mandates the named-function shape.
$fnResource = $functions | Where-Object { $_.Name -eq 'Test-HoldForCandidate' } | Select-Object -First 1
if (-not $fnResource) { throw "function not found in sweep script: Test-HoldForCandidate" }
Invoke-Expression $fnResource.Extent.Text

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
Write-Host "Test-HoldForCandidate — resource-axis addition (T-loop-control-keyed-by-project-resource-is-the-repo)"

# Small builders — kept local to this file so a change to the shape stays confined here.
function New-Cand {
    param([string]$Project, [string]$ThreadId, [string]$RepoDir)
    return [pscustomobject]@{
        project   = $Project
        thread_id = $ThreadId
        repo_dir  = $RepoDir
        key       = "$Project/$ThreadId"
    }
}
function New-PredictedMap {
    # Builds @{ <repo_dir> = @{ predicted_resource; reason } }.
    param([hashtable]$Rows)
    $out = @{}
    foreach ($k in $Rows.Keys) {
        $v = $Rows[$k]
        $out[$k] = @{
            predicted_resource = if ($null -eq $v.predicted_resource) { '' } else { [string]$v.predicted_resource }
            reason             = if ($null -eq $v.reason)             { '' } else { [string]$v.reason }
        }
    }
    return $out
}
function New-ControlMap {
    # Builds @{ <project> = <ConvertFrom-Json control object> }.
    param([hashtable]$States)
    $out = @{}
    foreach ($k in $States.Keys) {
        $v = $States[$k]  # @{ desired; observed }
        $out[$k] = (New-Control -Desired $v.desired -Observed $v.observed)
    }
    return $out
}

# The core positive case, which is the whole point of the change: a spirrow-magickit candidate
# whose repo_dir points at mindwire-impl must be HELD when spirrow-mindwire is HELD. This is the
# 2026-09-09 measurement from §2 of the specifying thread, encoded as a regression.
$cand_magickit_writing_mindwire = New-Cand -Project 'spirrow-magickit' -ThreadId 'T-cross-repo' -RepoDir 'C:/workspace/sandbox/mindwire-impl'
$predicted_ok = New-PredictedMap @{
    'C:/workspace/sandbox/mindwire-impl' = @{ predicted_resource = 'github.com/spirrowgames/spirrow-mindwire' }
}
$ownerMap = @{ 'github.com/spirrowgames/spirrow-mindwire' = 'spirrow-mindwire' }
$controls_mindwire_held = New-ControlMap @{
    'spirrow-mindwire' = @{ desired = 'hold'; observed = 'hold' }
    'spirrow-magickit' = @{ desired = 'run';  observed = 'run'  }
}
Check "cross-repo: mindwire HELD -> hold this magickit candidate that writes to mindwire-impl" $true `
    (Test-HoldForCandidate -Candidate $cand_magickit_writing_mindwire `
        -PredictedResourceByRepoDir $predicted_ok -OwnerMap $ownerMap -ControlByProject $controls_mindwire_held)

# The complementary negative case — same candidate, same map, but the owning project is RUN.
$controls_mindwire_run = New-ControlMap @{
    'spirrow-mindwire' = @{ desired = 'run'; observed = 'run' }
    'spirrow-magickit' = @{ desired = 'run'; observed = 'run' }
}
Check "cross-repo: mindwire RUN -> do NOT hold the magickit candidate (project-only decides)" $false `
    (Test-HoldForCandidate -Candidate $cand_magickit_writing_mindwire `
        -PredictedResourceByRepoDir $predicted_ok -OwnerMap $ownerMap -ControlByProject $controls_mindwire_run)

# Hold DESIRED but not yet ACKNOWLEDGED: mirror the Test-HoldObserved rule. The wrapper must
# launch until the loop lands the acknowledgement — even via the resource axis, otherwise the
# ack write-back starves the same way msg-2841 §3 documents ("the second hole") for the project
# axis. This test is the reason Test-HoldForCandidate composes Test-HoldObserved instead of
# reading `desired_state` directly.
$controls_mindwire_hold_unack = New-ControlMap @{
    'spirrow-mindwire' = @{ desired = 'hold'; observed = 'run' }
    'spirrow-magickit' = @{ desired = 'run';  observed = 'run' }
}
Check "cross-repo: mindwire HOLD not yet acknowledged -> MUST launch (ack lands via launch)" $false `
    (Test-HoldForCandidate -Candidate $cand_magickit_writing_mindwire `
        -PredictedResourceByRepoDir $predicted_ok -OwnerMap $ownerMap -ControlByProject $controls_mindwire_hold_unack)

# Fail-open arms (D-KEY-4c(2)). Each of these MUST return $false — the resource axis exists to
# HOLD MORE, never fewer. Silently withholding a launch because a hashtable was missing would be
# the exact silent-stall failure the specifying thread's §1 rejected.
Write-Host ""
Write-Host "Test-HoldForCandidate — every unresolved arm fails OPEN (launch)"
Check "null predicted map -> launch" $false `
    (Test-HoldForCandidate -Candidate $cand_magickit_writing_mindwire `
        -PredictedResourceByRepoDir $null -OwnerMap $ownerMap -ControlByProject $controls_mindwire_held)
Check "null owner map -> launch" $false `
    (Test-HoldForCandidate -Candidate $cand_magickit_writing_mindwire `
        -PredictedResourceByRepoDir $predicted_ok -OwnerMap $null -ControlByProject $controls_mindwire_held)
Check "null control map -> launch" $false `
    (Test-HoldForCandidate -Candidate $cand_magickit_writing_mindwire `
        -PredictedResourceByRepoDir $predicted_ok -OwnerMap $ownerMap -ControlByProject $null)
Check "null candidate -> launch" $false `
    (Test-HoldForCandidate -Candidate $null `
        -PredictedResourceByRepoDir $predicted_ok -OwnerMap $ownerMap -ControlByProject $controls_mindwire_held)

# repo_dir not in the predicted map at all -> launch. Reproduces the case where the resolver
# probe as a whole succeeded, but a specific repo_dir was not in the batch we asked about.
$predicted_missing = New-PredictedMap @{
    'C:/workspace/sandbox/some-other-checkout' = @{ predicted_resource = 'github.com/spirrowgames/lexora' }
}
Check "repo_dir absent from predicted map -> launch" $false `
    (Test-HoldForCandidate -Candidate $cand_magickit_writing_mindwire `
        -PredictedResourceByRepoDir $predicted_missing -OwnerMap $ownerMap -ControlByProject $controls_mindwire_held)

# repo_dir was probed but resolution failed (predicted_resource empty, reason populated).
# Same as "absent" — the resource is unknown, so the resource axis must fail open.
$predicted_failed = New-PredictedMap @{
    'C:/workspace/sandbox/mindwire-impl' = @{ predicted_resource = ''; reason = 'git exit 128: not a git repo' }
}
Check "predicted_resource UNRESOLVED for this repo_dir -> launch" $false `
    (Test-HoldForCandidate -Candidate $cand_magickit_writing_mindwire `
        -PredictedResourceByRepoDir $predicted_failed -OwnerMap $ownerMap -ControlByProject $controls_mindwire_held)

# Owner map does not include this resource (operator has not opted the repo in). Same fail-open
# behaviour as "no owner_map at all" — the gate silently reverts to the pre-change behaviour on
# repos the operator has not declared.
$ownerMap_empty = @{}
Check "owner_map missing this resource -> launch (operator did not opt this repo in)" $false `
    (Test-HoldForCandidate -Candidate $cand_magickit_writing_mindwire `
        -PredictedResourceByRepoDir $predicted_ok -OwnerMap $ownerMap_empty -ControlByProject $controls_mindwire_held)

# Owner map names a project the control map has no entry for (typo, or a project retired).
# Test-HoldObserved with $null control returns $false — fail-open. Compose that through, don't
# raise, don't withhold.
$ownerMap_bad = @{ 'github.com/spirrowgames/spirrow-mindwire' = 'spirrow-not-a-real-project' }
Check "owner_map points at project not in control map -> launch" $false `
    (Test-HoldForCandidate -Candidate $cand_magickit_writing_mindwire `
        -PredictedResourceByRepoDir $predicted_ok -OwnerMap $ownerMap_bad -ControlByProject $controls_mindwire_held)

Write-Host ""
if ($script:failures -gt 0) { Write-Host "sweep hold gate: $($script:failures) check(s) FAILED"; exit 1 }
Write-Host "sweep hold gate: all checks passed"
exit 0
