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
# D-4-6 (Bohr) — pin `set(map.keys()) == set(StopReason values)` — is DELIBERATELY NOT
# implemented here. The Python enum StopReason has SEVEN values, of which the map covers
# FIVE by policy (SETTLED and HOLD are silent by design; see the comment above $needsHuman
# in deploy/run-conductor-scheduled.ps1). Encoding the exclusion list into this test would
# create a second SOT for that policy, which is exactly what Bohr's D-4-6 fallback covers:
# "列挙可能でなければこの pin は作らない — テストに 5 種を直書きすると 2 つ目の SOT になるため。
# その場合の drift 検出は 5 の未知既定（実行時の警報）に委ねる。"
# D-4-5 IS that drift signal: a new StopReason value in conductor/core.py that reaches the
# notification path without a phrase entry here will land in the unknown-reason branch and
# produce the loud "未知の停止理由で停止しました" header in production — noisy on purpose.

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
# NB: the five keys are intentionally hard-coded HERE (and only here) because this is the
# one place where "which reasons need a notification" is a fact we want the test to fail on
# if silently narrowed. Wording is NOT hard-coded — only the enum-like key set.
$expectedKeys = @('human', 'no_handoff_to_human', 'no_progress_to_human', 'round_cap', 'empty_thread')
foreach ($k in $expectedKeys) {
    CheckTrue "map has key '$k'" ($map.ContainsKey($k))
}
Check 'map has exactly 5 keys (narrowing = notification loss, §4 §W-4)' 5 $map.Count

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

Write-Host ''
if ($script:failures -gt 0) {
    Write-Host "StopReason phrase pins: $($script:failures) check(s) FAILED"
    exit 1
}
Write-Host 'StopReason phrase pins: all checks passed'
exit 0
