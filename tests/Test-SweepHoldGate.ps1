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

# Get-SweepOwnerMap reads sweep.json's optional owner_map. The shape-check regression is
# PR #252 naysayer objection 4: a mistakenly-stringified or arrayified owner_map used to silently
# walk .NET reflection properties (e.g., an array's `Length`) and populate the map with garbage.
$fnOwnerMap = $functions | Where-Object { $_.Name -eq 'Get-SweepOwnerMap' } | Select-Object -First 1
if (-not $fnOwnerMap) { throw "function not found in sweep script: Get-SweepOwnerMap" }
Invoke-Expression $fnOwnerMap.Extent.Text

# ConvertFrom-ControlTimestamp is F4b's parse helper. It is used from the control probe log's
# HOLD-ack freshness check today, and per Bohr msg-2789 §3 F2-d it will be reused by
# Test-HoldObserved's freshness gate when F2 lands. The fail-mode is load-bearing (F2-c):
# an unparseable timestamp MUST return $null (fall-through → launch), never raise (would flip
# the sweep's fail direction from launch-anyway to abort-tick). Extracted via the same AST
# idiom so a rename or inlining fails loudly here.
$fnTs = $functions | Where-Object { $_.Name -eq 'ConvertFrom-ControlTimestamp' } | Select-Object -First 1
if (-not $fnTs) { throw "function not found in sweep script: ConvertFrom-ControlTimestamp" }
Invoke-Expression $fnTs.Extent.Text

# Test-HoldAckStale is the HOLD-freshness verdict function extracted so PR #279's two blocking
# correctness edges can be pinned as regressions rather than as a comment. Inlining it back into
# the probe loop would silently defeat the tests below.
$fnAck = $functions | Where-Object { $_.Name -eq 'Test-HoldAckStale' } | Select-Object -First 1
if (-not $fnAck) { throw "function not found in sweep script: Test-HoldAckStale" }
Invoke-Expression $fnAck.Extent.Text

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
Write-Host "Get-SweepOwnerMap — malformed owner_map shapes fail LOUDLY (PR #252 objection 4)"

# Helper: write a temporary sweep.json with a specific `owner_map` value and run the reader.
function Invoke-OwnerMapRead {
    param([string]$OwnerMapJson)
    $tmp = New-TemporaryFile
    try {
        $body = @"
{
  "owner_map": $OwnerMapJson,
  "candidates": [
    { "project": "p", "thread_id": "t", "repo_dir": "C:/x" }
  ]
}
"@
        [System.IO.File]::WriteAllText($tmp.FullName, $body)
        return Get-SweepOwnerMap -Path $tmp.FullName
    }
    finally {
        Remove-Item -LiteralPath $tmp.FullName -Force -ErrorAction SilentlyContinue
    }
}

# Positive case — a valid JSON object populates the map.
$goodMap = Invoke-OwnerMapRead -OwnerMapJson '{"github.com/foo/bar":"proj"}'
Check "well-formed owner_map -> 1 entry loaded" 1 $goodMap.Count
Check "well-formed owner_map -> correct value" "proj" $goodMap['github.com/foo/bar']

# Negative cases — a string or array must THROW loudly, not silently populate with .NET
# reflection properties (`Length`, `Chars`, etc). Verified by catching the exception and
# asserting we saw one.
$didThrowString = $false
try { Invoke-OwnerMapRead -OwnerMapJson '"not-an-object"' | Out-Null }
catch { $didThrowString = $true }
Check "owner_map = string throws" $true $didThrowString

$didThrowArray = $false
try { Invoke-OwnerMapRead -OwnerMapJson '["a","b"]' | Out-Null }
catch { $didThrowArray = $true }
Check "owner_map = array throws" $true $didThrowArray

$didThrowNumber = $false
try { Invoke-OwnerMapRead -OwnerMapJson '42' | Out-Null }
catch { $didThrowNumber = $true }
Check "owner_map = number throws" $true $didThrowNumber

# Backward-compat: an ABSENT owner_map (or explicit null) means "gate OFF", not an error.
$tmpNoField = New-TemporaryFile
try {
    [System.IO.File]::WriteAllText($tmpNoField.FullName, '{"candidates":[{"project":"p","thread_id":"t","repo_dir":"C:/x"}]}')
    $absentMap = Get-SweepOwnerMap -Path $tmpNoField.FullName
    Check "owner_map absent -> empty map (gate OFF, backward compat)" 0 $absentMap.Count
}
finally { Remove-Item -LiteralPath $tmpNoField.FullName -Force -ErrorAction SilentlyContinue }

Write-Host ""
Write-Host "ConvertFrom-ControlTimestamp — F4b parse helper (fall-through on unparseable, F2-c)"

# A valid ISO-8601 with microseconds parses to the same UTC instant.
$tsMicro = ConvertFrom-ControlTimestamp '2026-09-08T20:32:19.106309Z'
Check "microsecond-precision ISO -> parsed" ([datetimeoffset]) $tsMicro.GetType()
Check "microsecond-precision ISO -> UTC offset" ([TimeSpan]::Zero) $tsMicro.Offset

# A second-precision timestamp parses too — the whole point of the helper is not to rely on the
# raw strings for ordering (F2-a: microsecond-vs-second precision flips lexical ordering).
$tsSec = ConvertFrom-ControlTimestamp '2026-09-08T20:32:19Z'
Check "second-precision ISO -> parsed" ([datetimeoffset]) $tsSec.GetType()

# The regression F2-a exists to prevent: microsecond value is LATER than the same-second value.
# Naive string comparison would put the microsecond value FIRST ('.' < 'Z'), which would read as
# stale. The parsed comparison must respect real time order.
Check "F2-a: microsecond after same-second (parsed order)" $true ($tsMicro -gt $tsSec)

# A missing microsecond field with a fractional-second observed_at (same second): observed newer.
$dAt = ConvertFrom-ControlTimestamp '2026-09-08T20:32:19Z'
$oAt = ConvertFrom-ControlTimestamp '2026-09-08T20:32:19.500000Z'
Check "F2-a: fractional-second observed after bare-second desired" $true ($oAt -gt $dAt)

# Fail-through cases — every one MUST return $null (F2-c: parse failure = "no ack"; never throw).
Check "null -> null (never throws)"    $null (ConvertFrom-ControlTimestamp $null)
Check "empty string -> null"           $null (ConvertFrom-ControlTimestamp '')
Check "whitespace -> null"             $null (ConvertFrom-ControlTimestamp '   ')
Check "garbage string -> null"         $null (ConvertFrom-ControlTimestamp 'not-a-date')
Check "half-parsed date -> null"       $null (ConvertFrom-ControlTimestamp '2026-99-99T99:99:99Z')

# A no-suffix timestamp is treated as UTC (AssumeUniversal + AdjustToUniversal), not as local
# machine time. This is what keeps the "N minutes stale" metric in the HOLD NOT ACKNOWLEDGED
# warning honest across daemon hosts in any timezone.
$tsNoZ = ConvertFrom-ControlTimestamp '2026-09-08T20:32:19'
Check "bare (no Z) treated as UTC -> zero offset" ([TimeSpan]::Zero) $tsNoZ.Offset

Write-Host ""
Write-Host "Test-HoldAckStale — HOLD-ack freshness verdict (PR #279 review, blocking objections 1 & 2)"

# Control builder that includes the F4b timestamp fields. observed_at = $null models older
# magickit servers that predate the timestamp fields (PR #279 review objection 1).
function New-ControlWithTs {
    param(
        [string]$Desired,
        $Observed,
        [string]$DesiredAt,
        [string]$ObservedAt
    )
    $obsField = if ($null -eq $Observed) { 'null' } else { '"' + $Observed + '"' }
    $desAtField = if ([string]::IsNullOrEmpty($DesiredAt)) { 'null' } else { '"' + $DesiredAt + '"' }
    $obsAtField = if ([string]::IsNullOrEmpty($ObservedAt)) { 'null' } else { '"' + $ObservedAt + '"' }
    return ('{"project":"p","desired_state":"' + $Desired + '","desired_at":' + $desAtField +
        ',"observed_state":' + $obsField + ',"observed_at":' + $obsAtField +
        ',"configured":true}' | ConvertFrom-Json)
}

# Trivial-return arms (not the interesting bug fixes, but the function must still fall out fast
# for callers other than the probe loop, in case Test-HoldAckStale ever gets composed elsewhere).
Check "null control -> null (nothing to warn about)" $null `
    (Test-HoldAckStale -Control $null)
Check "desired != hold -> null (nothing to warn about)" $null `
    (Test-HoldAckStale -Control (New-ControlWithTs -Desired 'run' -Observed 'run' `
        -DesiredAt '2026-09-08T20:00:00Z' -ObservedAt '2026-09-08T20:01:00Z'))

# The two ack-not-landed arms — reason string is what the outer log line quotes.
$verdictNever = Test-HoldAckStale -Control (New-ControlWithTs -Desired 'hold' -Observed 'run' `
    -DesiredAt '2026-09-08T20:00:00Z' -ObservedAt '')
Check "hold desired, never observed (observed=run, observed_at=null) -> stale, 'never acknowledged'" `
    'never acknowledged' $verdictNever.Reason

# ============================================================================================
# PR #279 review, BLOCKING objection 1 (false-positive on older magickit).
# ============================================================================================
# Older magickit servers omit the timestamp fields. If the loop is holding, observed_state comes
# back as 'hold' but observed_at is $null. The pre-fix code unconditionally emitted "never
# acknowledged" in that case, blasting a false HOLD NOT ACKNOWLEDGED warning to the logs every
# tick against those hosts. The correct verdict is "fresh" — silent — because observed_state
# itself is the acknowledgement; observed_at is only there to measure freshness.
Check "PR#279 obj#1: old magickit (observed='hold', observed_at=null) -> null (SILENT, hold IS acked)" `
    $null `
    (Test-HoldAckStale -Control (New-ControlWithTs -Desired 'hold' -Observed 'hold' `
        -DesiredAt '2026-09-08T20:00:00Z' -ObservedAt ''))
# Even without desired_at on the old server, observed_state='hold' is enough to declare fresh.
Check "PR#279 obj#1: old magickit (both timestamps null, observed='hold') -> null (SILENT)" `
    $null `
    (Test-HoldAckStale -Control (New-ControlWithTs -Desired 'hold' -Observed 'hold' `
        -DesiredAt '' -ObservedAt ''))
# Contrast case: same server (both timestamps null) but observed_state is NOT 'hold' — that
# genuinely IS "never acknowledged" (there is no observation of any kind), so it MUST warn.
$verdictOldRun = Test-HoldAckStale -Control (New-ControlWithTs -Desired 'hold' -Observed 'run' `
    -DesiredAt '' -ObservedAt '')
Check "PR#279 obj#1: old magickit (observed='run', both null) -> stale 'never acknowledged'" `
    'never acknowledged' $verdictOldRun.Reason

# ============================================================================================
# PR #279 review, BLOCKING objection 2 (lag measured from desired_at, not observed_at).
# ============================================================================================
# Scenario: operator requested a HOLD 10 seconds ago (desired_at = Now - 10s). The daemon's last
# successful observation happened during the previous run, 5 days ago (observed_at = Now - 5d).
# The pre-fix code computed lag = UtcNow - observed_at = ~7200 minutes, an absurd figure that
# implies the operator has been waiting 5 days. The correct lag = UtcNow - desired_at ≈ 0.2 min,
# because the operator's HOLD request is what has been pending, not the daemon's observation.
$now = [datetimeoffset]::Parse('2026-09-17T04:00:00Z',
    [System.Globalization.CultureInfo]::InvariantCulture,
    [System.Globalization.DateTimeStyles]::AssumeUniversal -bor [System.Globalization.DateTimeStyles]::AdjustToUniversal)
$verdictLag = Test-HoldAckStale -Now $now -Control (New-ControlWithTs -Desired 'hold' -Observed 'run' `
    -DesiredAt '2026-09-17T03:59:50Z' -ObservedAt '2026-09-12T04:00:00Z')
# desired_at is 10 seconds before Now → ~0.2 minutes stale (rounded, 1 dp).
Check "PR#279 obj#2: lag measured from desired_at (not observed_at); expected '0.2 minutes stale'" `
    '0.2 minutes stale' $verdictLag.Reason
# Explicit counter-claim: the WRONG value under the pre-fix logic would be ~7200 minutes.
# Regression pin — if this ever comes back to matching the observed_at subtraction, the fix
# has been reverted.
Check "PR#279 obj#2: lag NOT '7200 minutes stale' (would be the pre-fix answer)" `
    $true ($verdictLag.Reason -ne '7200 minutes stale')

# Fresh-ack arm — observed_at >= desired_at means the ack landed at or after the request. This
# is the settled steady state that MUST stay silent (no warning every tick).
Check "fresh ack (observed_at > desired_at) -> null (SILENT)" $null `
    (Test-HoldAckStale -Control (New-ControlWithTs -Desired 'hold' -Observed 'hold' `
        -DesiredAt '2026-09-08T20:00:00Z' -ObservedAt '2026-09-08T20:00:05Z'))
Check "fresh ack (observed_at == desired_at exactly) -> null (SILENT)" $null `
    (Test-HoldAckStale -Control (New-ControlWithTs -Desired 'hold' -Observed 'hold' `
        -DesiredAt '2026-09-08T20:00:00Z' -ObservedAt '2026-09-08T20:00:00Z'))

# desired_at unparseable BUT observed_at present: cannot measure freshness in either direction,
# so the safe verdict is "fresh" (silent). Warning here would fire on every tick against a
# hypothetical mixed-schema server that returns only observed_at.
Check "desired_at null, observed_at present -> null (can't compare freshness; assume fresh)" `
    $null `
    (Test-HoldAckStale -Control (New-ControlWithTs -Desired 'hold' -Observed 'hold' `
        -DesiredAt '' -ObservedAt '2026-09-08T20:00:00Z'))

Write-Host ""
if ($script:failures -gt 0) { Write-Host "sweep hold gate: $($script:failures) check(s) FAILED"; exit 1 }
Write-Host "sweep hold gate: all checks passed"
exit 0
