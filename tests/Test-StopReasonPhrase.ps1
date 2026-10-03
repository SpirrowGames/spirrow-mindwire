# Pin tests for deploy/lib/StopReason.ps1.
#
# What this pins (T-park-alert-says-judgement-when-it-is-a-fault, Bohr msg-1466 §D-4):
#   D-4-1  the header phrase for every known reason comes FROM the SOT map (no wording
#          duplicated into the test — the pin is on the pointer, not the string).
#   D-4-2  no-regression: no known reason produces the OLD fixed-label header shape
#          `— 判断待ち (reason=`. Different reason = different phrase, always.
#   D-4-3  every immutable field survives on every reason: `**<ThreadId>**`, `(<Project>)`,
#          `reason=<raw>`, `rounds=<n>`, `<LastMsgId>`.
#   D-4-4  ONLY the `human` map entry contains the substring "判断待ち"; the four failure /
#          anomaly / config-error reasons do NOT claim to be judgement-pending. This is the
#          core requirement of msg-1465 and is asserted independently of D-4-1 so a future
#          rewrite of any wording still trips this pin if it accidentally re-adds "判断待ち"
#          to a failure phrase.
#   D-4-5  unknown reason (`'wat'` / `''` / `$null`): (a) no throw, (b) yields the loud
#          default phrase, (c) does NOT match any known-reason phrase, (d) does NOT contain
#          "判断待ち", (e) `reason=<raw>` still appears in the header.
#
# D-4-6 closed-world pin (T-stop-reason-map-drift-pin, Bohr msg-4827 U1', endorsed by Einstein).
# History: #172 left this pin out on purpose (msg-1468: 7 enum values, 5 map keys, and a
# hand-written exclusion list would have been a second SOT). The premise changed. Three
# values were added in 37 days, and the hand-written copy in tests/test_conductor_core.py
# went stale (5 values, missing 'self_handoff_to_human'). The block at the bottom of this
# file now reads the Python enum SEMANTICALLY: `uv run python -c` imports StopReason, with
# no regex over source and no export build step. It then asserts
#   enum values == (Get-StopReasonPhraseMap).Keys ∪ $unnotified   and   Keys ∩ $unnotified = ∅.
# $unnotified is a test-only ledger and is never read at runtime. The notification predicate's
# SOT is still the map's key set. Getting the ledger wrong reds this gate and changes no
# notification. D-4-5's runtime loud default stays as the second line of defence.

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$libPath = Join-Path $repoRoot 'deploy/lib/StopReason.ps1'
if (-not (Test-Path -LiteralPath $libPath)) { throw "StopReason.ps1 not found: $libPath" }

# --- PR-gate #172 regression pin: dot-sourcing the lib MUST NOT mutate caller scope ------
# PR-gate flagged an earlier revision that set `$ErrorActionPreference = 'Stop'` at script
# scope inside deploy/lib/StopReason.ps1, which is a side effect of dot-sourcing — it
# overwrites the caller's preference silently. The lib is documented as pure. Pin the
# invariant here by measuring $ErrorActionPreference BEFORE and AFTER the dot-source and
# asserting it is unchanged, with the caller's preference deliberately set to a NON-Stop
# value so a re-added `$ErrorActionPreference = 'Stop'` in the lib would flip it and fail.
$prevErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$eapBefore = $ErrorActionPreference
. $libPath
$eapAfter = $ErrorActionPreference
$ErrorActionPreference = $prevErrorAction   # restore for the rest of the test

# The library is a pure-function file — dot-sourcing it has no side effects (unlike
# run-conductor-scheduled.ps1, which launches a sweep on load).

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) {
        Write-Host ('  PASS  {0}' -f $Name)
    }
    else {
        $script:failures++
        Write-Host ('  FAIL  {0} — expected [{1}], got [{2}]' -f $Name, $Expected, $Actual)
    }
}
function CheckTrue {
    param([string]$Name, [bool]$Actual, [string]$Detail = '')
    if ($Actual) { Write-Host ('  PASS  {0}' -f $Name); return }
    $script:failures++
    Write-Host ('  FAIL  {0}{1}' -f $Name, $(if ($Detail) { " — $Detail" } else { '' }))
}

# ---------------------------------------------------------------------------------------
# PR-gate #172 regression pin (measured above): the dot-source did not mutate the
# caller's $ErrorActionPreference. Reported here so a failure is a clearly-named check.
# ---------------------------------------------------------------------------------------
Write-Host 'PR-gate #172 regression — dot-sourcing deploy/lib/StopReason.ps1 does not mutate $ErrorActionPreference'
Check 'caller $ErrorActionPreference is unchanged after dot-source' $eapBefore $eapAfter

# ---------------------------------------------------------------------------------------
# Preconditions on the map itself — the tests below trust these.
# ---------------------------------------------------------------------------------------
Write-Host 'Get-StopReasonPhraseMap — returns the expected KEY SET (the notification predicate)'
$map = Get-StopReasonPhraseMap
# NB: the keys are intentionally hard-coded HERE (and only here) because this is the
# one place where "which reasons need a notification" is a fact we want the test to fail on
# if silently narrowed. Wording is NOT hard-coded — only the enum-like key set.
#
# 2026-09-14 (design §6.1): grew from five to six with 'self_handoff_to_human'. The count
# assertion is here to catch a silent NARROWING; a widening is a deliberate act and updating
# this line is the act. A new conductor StopReason that needs a notification belongs in the
# map AND in this list.
#
# 2026-09-30 (T42): grew from six to seven with 'stalled_to_human' (the stall watchdog exits 0
# after posting STALLED, so this notification is the operator's signal).
# 2026-10-03 (T-next-operator-is-silent D3): grew to eight with 'operator_work_to_human' — a valid
# `NEXT: operator` is work a person must do, so it is heard; its phrase is not 判断待ち.
$expectedKeys = @('human', 'no_handoff_to_human', 'no_progress_to_human', 'self_handoff_to_human', 'stalled_to_human', 'operator_work_to_human', 'round_cap', 'empty_thread')
foreach ($k in $expectedKeys) {
    CheckTrue "map has key '$k'" ($map.ContainsKey($k))
}
Check 'map has exactly 8 keys (narrowing = notification loss, §4 §W-4)' 8 $map.Count

Write-Host 'Get-StopReasonPhraseMap — returns a fresh hashtable each call (no shared state)'
$m1 = Get-StopReasonPhraseMap
$m2 = Get-StopReasonPhraseMap
$m1['human'] = 'MUTATED'
Check 'mutation in one caller does not leak into another' 'あなたの判断待ちで停止しました' $m2['human']

# ---------------------------------------------------------------------------------------
# D-4-4: ONLY `human` claims to be judgement-pending. Failure / anomaly / config-error
# reasons must NOT contain "判断待ち" in their phrase. This is the core requirement of
# msg-1465 and is asserted here on the MAP directly so it holds regardless of how the
# header is built.
# ---------------------------------------------------------------------------------------
Write-Host ''
Write-Host 'D-4-4: only `human` phrase contains "判断待ち"; failure/anomaly reasons do NOT'
$map = Get-StopReasonPhraseMap
foreach ($k in $map.Keys) {
    $hasJudgement = $map[$k].Contains('判断待ち')
    if ($k -eq 'human') {
        CheckTrue "human phrase contains '判断待ち' (this is the actual judgement case)" $hasJudgement $map[$k]
    }
    else {
        CheckTrue "$k phrase does NOT contain '判断待ち' (failure/anomaly must not masquerade as judgement)" (-not $hasJudgement) $map[$k]
    }
}

# ---------------------------------------------------------------------------------------
# Get-StopReasonPhrase: known reason returns the map entry; unknown reason falls open.
# ---------------------------------------------------------------------------------------
Write-Host ''
Write-Host 'Get-StopReasonPhrase — every known key returns its map value verbatim'
$map = Get-StopReasonPhraseMap
foreach ($k in $map.Keys) {
    Check "Get-StopReasonPhrase '$k' == map[`$k`]" $map[$k] (Get-StopReasonPhrase -StopReason $k)
}

# ---------------------------------------------------------------------------------------
# D-4-5: unknown reason must not throw, must degrade loudly, must NOT contain "判断待ち".
# ---------------------------------------------------------------------------------------
Write-Host ''
Write-Host 'D-4-5: unknown reason falls open loudly and does NOT re-introduce "判断待ち"'
$unknownInputs = @('wat', '', $null)
$defaultPhrase = "未知の停止理由で停止しました（通知側がこの reason を知りません）"
foreach ($u in $unknownInputs) {
    $label = if ($null -eq $u) { '$null' } elseif ($u -eq '') { "''" } else { "'$u'" }
    $threw = $false
    $result = $null
    try { $result = Get-StopReasonPhrase -StopReason $u } catch { $threw = $true }
    CheckTrue "unknown reason $label does not throw" (-not $threw)
    Check "unknown reason $label returns the loud default phrase" $defaultPhrase $result
    if ($null -ne $result) {
        CheckTrue "unknown reason $label result does NOT contain '判断待ち'" (-not $result.Contains('判断待ち')) $result
        # Sanity: unknown result must not accidentally equal any known reason's phrase.
        $mapNow = Get-StopReasonPhraseMap
        foreach ($k in $mapNow.Keys) {
            CheckTrue "unknown reason $label does NOT match known phrase for '$k'" ($result -ne $mapNow[$k])
        }
    }
}

# ---------------------------------------------------------------------------------------
# New-NotificationHeader — the shape the operator reads in Discord.
# ---------------------------------------------------------------------------------------
Write-Host ''
Write-Host 'New-NotificationHeader — shape is preserved byte-for-byte; only the label field changes'

# D-4-1 / D-4-2 / D-4-3: iterate every known reason with the same non-label fields; assert
# on the map POINTER (not the wording) and on the immutable fields.
$fixed = @{
    ThreadId  = 'T-thread-fix'
    Project   = 'proj-fix'
    Rounds    = 7
    LastMsgId = 'msg-1234'
}
$map = Get-StopReasonPhraseMap
foreach ($k in $map.Keys) {
    $header = New-NotificationHeader -ThreadId $fixed.ThreadId -Project $fixed.Project `
        -StopReason $k -Rounds $fixed.Rounds -LastMsgId $fixed.LastMsgId

    # D-4-1: the phrase for $k is present verbatim.
    CheckTrue "reason '$k': header contains the SOT phrase for '$k'" ($header.Contains($map[$k])) $header

    # D-4-2: no known reason regresses to the old fixed-label header shape.
    CheckTrue "reason '$k': header does NOT match the old fixed-label form '— 判断待ち (reason='" `
        (-not $header.Contains('— 判断待ち (reason=')) $header

    # D-4-3: every immutable field survives on every reason.
    CheckTrue "reason '$k': header carries **ThreadId**" ($header.Contains("**$($fixed.ThreadId)**")) $header
    CheckTrue "reason '$k': header carries (Project)" ($header.Contains("($($fixed.Project))")) $header
    CheckTrue "reason '$k': header carries reason=<raw>" ($header.Contains("reason=$k")) $header
    CheckTrue "reason '$k': header carries rounds=<n>" ($header.Contains("rounds=$($fixed.Rounds)")) $header
    CheckTrue "reason '$k': header carries LastMsgId" ($header.Contains($fixed.LastMsgId)) $header

    # Field ORDER pin: the daily digest / composed-message pipeline reads this line by eye,
    # and reasonable regexes elsewhere may key on "reason=…, rounds=…". Keep the order that
    # was in the pre-change literal.
    $reasonPos = $header.IndexOf('reason=')
    $roundsPos = $header.IndexOf('rounds=')
    $msgPos    = $header.IndexOf($fixed.LastMsgId)
    CheckTrue "reason '$k': field order reason= < rounds= < LastMsgId" (($reasonPos -lt $roundsPos) -and ($roundsPos -lt $msgPos)) $header

    # Header prefix pin: `MindWire: ` starts every header.
    CheckTrue "reason '$k': header begins with 'MindWire: '" ($header.StartsWith('MindWire: ')) $header
}

# D-4-2 is on the map itself too: the map's `human` entry contains "判断待ち" but that
# doesn't mean any WHOLE header string produces the old form. Assert directly.
$humanHeader = New-NotificationHeader -ThreadId 'T-a' -Project 'p' `
    -StopReason 'human' -Rounds 1 -LastMsgId 'msg-1'
CheckTrue "human reason still contains '判断待ち' (the phrase, not the old label)" ($humanHeader.Contains('判断待ち')) $humanHeader
CheckTrue "human reason does NOT match the old fixed-label form" (-not $humanHeader.Contains('— 判断待ち (reason=')) $humanHeader

# D-4-5 completeness: unknown reason produces a valid-shape header with the loud default.
$unknownHeader = New-NotificationHeader -ThreadId 'T-b' -Project 'q' `
    -StopReason 'wat' -Rounds 3 -LastMsgId 'msg-999'
CheckTrue 'unknown reason header contains the loud default phrase' `
    ($unknownHeader.Contains('未知の停止理由で停止しました（通知側がこの reason を知りません）')) $unknownHeader
CheckTrue 'unknown reason header carries reason=<raw> even for the drift case' ($unknownHeader.Contains('reason=wat')) $unknownHeader
CheckTrue 'unknown reason header does NOT contain "判断待ち"' (-not $unknownHeader.Contains('判断待ち')) $unknownHeader

# Exact-shape golden: one full-string equality check on a canonical input so a stray edit
# that adds/removes whitespace, em-dash, or field separators trips a single obvious pin.
# The wording INSIDE this golden is intentionally the same one the SOT map returns — the
# assertion is on the ORDER and PUNCTUATION around it, not on the wording itself.
Write-Host ''
Write-Host 'New-NotificationHeader — exact-shape golden for the canonical `human` reason'
$golden = "MindWire: **T-x** (proj-x) — $(Get-StopReasonPhrase -StopReason 'human') (reason=human, rounds=2, msg-42)"
$actual = New-NotificationHeader -ThreadId 'T-x' -Project 'proj-x' `
    -StopReason 'human' -Rounds 2 -LastMsgId 'msg-42'
Check 'canonical human header matches golden byte-for-byte' $golden $actual

# ---------------------------------------------------------------------------------------
# D-4-6 closed-world pin: every Python StopReason value is classified exactly once,
# either as NOTIFYING (a key of Get-StopReasonPhraseMap) or as UNNOTIFIED (the ledger below).
# (T-stop-reason-map-drift-pin, Bohr msg-4827 U1' §2.1.)
#
# The enum is read by importing it through `uv run`, the same toolchain .mindwire-gate has
# already synced. `--project $repoRoot` makes the call independent of the CWD (msg-2601 §1-2).
# FAIL-CLOSED: a non-zero exit, unparseable JSON, or an empty array is RED, never a skip.
# A missing Python is a breakage of this gate, the same way a missing pwsh is (.mindwire-gate).
# ---------------------------------------------------------------------------------------
Write-Host ''
Write-Host 'D-4-6: Python StopReason == phrase-map keys UNION $unnotified ledger (disjoint)'

# UNNOTIFIED LEDGER: test-only, never consulted at runtime. Each line names the path that
# covers the reason instead of a Discord notification. Adding a value here is a deliberate
# act; the alternative is adding it to Get-StopReasonPhraseMap (see StopReason.ps1 note 1).
$unnotified = @(
    'none'           # thread settled: the normal end, the sweep just moves on
    'hold'           # the operator asked for the stop, so telling them is not news
    'ci_wait'        # pre-gate CI-wait DEFER (design v0.3.1 §5.2A); past the cap it becomes 'human'
    'merge_wait'     # PR-gate APPROVE on a human-merged PR: the merge-wait PR list carries it (msg-4361)
    'resume_retry'   # gate resume could not read GitHub / head moved: silent retry, bounded by the stall watchdog
    'slice_end'      # gate-lane run handed its relay / ci-route head to the role lane (msg-6313 §1′)
    'adapter_error'  # adapter raised; exit!=0 -> quarantine, where the K alert sounds
)

# The probe ALWAYS writes one stderr line before the JSON. That forces the stderr-merge path
# below to run on every gate run, not only when uv happens to print a warning or progress line.
# No double quotes inside the probe: when a native argument contains `"`, the quoting gets
# mangled on the way to python.exe.
$pyProbe = 'import json, sys; from spirrow_mindwire.conductor.core import StopReason; print(''probe: stderr line (intentional)'', file=sys.stderr, flush=True); print(json.dumps(sorted(r.value for r in StopReason)))'
$enumValues = $null
$probeError = $null
# PR-gate #369 round 1: with `2>&1`, each native stderr line arrives as an ErrorRecord. Under
# this file's $ErrorActionPreference = 'Stop', some hosts (Windows PowerShell 5.1) turn the
# first such record into a terminating error. The try would then jump to catch before
# $LASTEXITCODE is read, and every run would fail for the wrong reason. pwsh 7.6 does not
# throw here, but the check must not depend on the host version. So the preference is
# lowered to 'Continue' for the native call ONLY and restored in finally. Fail-closed still
# holds: exit != 0, no JSON line, and an empty list are each checked explicitly below.
$savedErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $probeOut = & uv run --project $repoRoot python -c $pyProbe 2>&1
    $probeExit = $LASTEXITCODE
    if ($probeExit -ne 0) {
        $probeError = "uv run exited $probeExit`: $($probeOut -join ' | ')"
    }
    else {
        # uv may write progress to stderr (merged by 2>&1); the JSON is the last stdout line.
        $jsonLine = @($probeOut | Where-Object { $_ -is [string] -and $_.TrimStart().StartsWith('[') }) | Select-Object -Last 1
        if (-not $jsonLine) {
            $probeError = "no JSON array in probe output: $($probeOut -join ' | ')"
        }
        else {
            $enumValues = @($jsonLine | ConvertFrom-Json)
        }
    }
}
catch {
    $probeError = "probe failed: $($_.Exception.Message)"
}
finally {
    $ErrorActionPreference = $savedErrorAction
}
if ($null -eq $probeError -and ($null -eq $enumValues -or $enumValues.Count -eq 0)) {
    $probeError = 'probe returned an empty StopReason value list'
}
Check 'caller $ErrorActionPreference restored to Stop after the uv probe' 'Stop' $ErrorActionPreference
CheckTrue 'StopReason enum is readable via uv run (fail-closed: exit!=0 / bad JSON / empty = RED)' `
    ($null -eq $probeError) $probeError

if ($null -eq $probeError) {
    $notifying = @((Get-StopReasonPhraseMap).Keys)
    $classified = @($notifying) + @($unnotified)

    $unclassified = @($enumValues | Where-Object { $classified -notcontains $_ } | Sort-Object)
    $notInEnum = @($classified | Where-Object { $enumValues -notcontains $_ } | Sort-Object)
    $overlap = @($notifying | Where-Object { $unnotified -contains $_ } | Sort-Object)

    $detail = 'unclassified enum values (add to Get-StopReasonPhraseMap or to $unnotified): [{0}]; classified but not in enum (stale map key or ledger row): [{1}]' -f `
        ($unclassified -join ', '), ($notInEnum -join ', ')
    CheckTrue 'set(StopReason) == phrase-map keys UNION $unnotified' `
        (($unclassified.Count -eq 0) -and ($notInEnum.Count -eq 0)) $detail
    CheckTrue 'phrase-map keys INTERSECT $unnotified is empty (a reason is notifying XOR silent)' `
        ($overlap.Count -eq 0) ("in both: [{0}]" -f ($overlap -join ', '))
}

Write-Host ''
if ($script:failures -gt 0) {
    Write-Host "StopReason phrase pins: $($script:failures) check(s) FAILED"
    exit 1
}
Write-Host 'StopReason phrase pins: all checks passed'
exit 0
