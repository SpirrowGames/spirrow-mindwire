# Regression guard for the sweep's quarantine machinery (deploy/run-conductor-scheduled.ps1).
#
# Why this file exists at all: the failure-isolation design (spec/msg-814) is entirely about turning
# a class of silent failure into a loud one. Every check here is one where being wrong is invisible
# in the log — a mis-derived state stays `quarantined` forever with no escalation notification, a
# mis-computed fingerprint hint claims coverage the runner does not have, a starvation report that
# skips quarantined threads recreates the "quiet dark area" the whole design refuses.
#
# Functions are lifted out of the sweep script's AST rather than dot-sourced, for the same reason
# Test-SweepHeadCache does it: dot-sourcing would run the sweep — probe the chatroom, rewrite
# mindwire.toml, launch the conductor.

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

# The functions read $script:QuarantineEscalatedAfter etc. via default parameters; set them here so
# the lifted defaults resolve without pulling in the whole script (which would run it).
$script:QuarantineEscalatedAfter = [TimeSpan]::FromHours(24)
$script:QuarantineStaleAfter     = [TimeSpan]::FromDays(7)
$script:StarvedThreshold         = [TimeSpan]::FromHours(24)

# Get-JsonState / Write-Log stubs so the lifts stay self-contained.
function Write-Log { param([string]$Message) }

# Get-JsonState now lives in deploy/lib/Lease.ps1 (msg-2172 reader collapse) — dot-source it here
# so the lifted wrapper functions that call Get-JsonState (e.g. Merge-StateForWrite) resolve. Lease
# lib is pure functions with no top-level side effects, same as StopReason.ps1's discipline.
$leaseLib = Join-Path $repoRoot 'deploy/lib/Lease.ps1'
if (-not (Test-Path -LiteralPath $leaseLib)) { throw "Lease lib not found: $leaseLib" }
. $leaseLib

$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'New-QuarantineRecord', 'Get-FailureClass',
                  'Get-DerivedQuarantineState', 'Get-FingerprintHint',
                  'Get-QuarantineReproHint',
                  'Format-DurationDigest', 'Get-StarvedKeys', 'New-DailyDigest',
                  'Get-SystemicAlertSignature', 'Merge-StateForWrite',
                  'Save-JsonState', 'Update-EvaluatedTimestamp', 'ConvertTo-UtcInstant',
                  # T-digest-exceeds-discord-limit-and-is-dropped (msg-2099..2106): Test-DigestClockAdvances
                  # is retired in favour of the two-question split (delivered vs. full-success), so
                  # cadence and health advance separately. See the two Check blocks below and the
                  # dedicated Test-SweepDigest.ps1 for the full DoD suite.
                  'Test-DigestDelivered', 'Test-DigestFullSuccess',
                  'Get-DigestPeriod', 'Test-DigestDeliveryDue',
                  'Get-DigestPeriodsMissed', 'Get-DigestHealthWarning',
                  'New-DegradedDigestMessage', 'Get-NotificationFailureClass') {
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

Write-Host "Get-DerivedQuarantineState — age drives quarantined -> escalated -> stale"
$now = [datetime]::Parse('2026-08-11T12:00:00Z').ToUniversalTime()
Check "fresh (0h) -> quarantined" 'quarantined' `
    (Get-DerivedQuarantineState -FirstFailureAt $now.AddHours(-1) -Now $now)
Check "just under threshold (23h) -> quarantined" 'quarantined' `
    (Get-DerivedQuarantineState -FirstFailureAt $now.AddHours(-23) -Now $now)
# 24h EXACTLY must escalate — the spec value is 24h, off-by-one boundaries silently delay
# escalation for another whole day (288 ticks) if the comparison is >.
Check "24h exactly -> escalated" 'escalated' `
    (Get-DerivedQuarantineState -FirstFailureAt $now.AddHours(-24) -Now $now)
Check "3d -> escalated" 'escalated' `
    (Get-DerivedQuarantineState -FirstFailureAt $now.AddDays(-3) -Now $now)
Check "7d exactly -> stale" 'stale' `
    (Get-DerivedQuarantineState -FirstFailureAt $now.AddDays(-7) -Now $now)
Check "10d -> stale" 'stale' `
    (Get-DerivedQuarantineState -FirstFailureAt $now.AddDays(-10) -Now $now)

Write-Host "Get-FingerprintHint — survives a JSON round-trip (PSCustomObject, not hashtable)"
# THE regression this section exists to prevent: the moment quarantine.json is written and read
# back, ConvertFrom-Json parses the nested `failure_fingerprint` object into a PSCustomObject, NOT
# a hashtable. A `[hashtable]$Fingerprint` parameter throws "Cannot process argument transformation"
# on that value, taking the daily digest — and the whole sweep — down with it. Untyped $Fingerprint
# duck-types on both shapes; this test locks that in.
$fpPSObj = [pscustomobject]@{ head = 'msg-1'; control = 'run' }
Check "PSCustomObject fingerprint: head change reported" '新規メッセージあり (head 変化)' `
    (Get-FingerprintHint -Fingerprint $fpPSObj -CurrentHead 'msg-2' -CurrentControl 'run')
Check "PSCustomObject fingerprint: nothing changed -> no hint" $null `
    (Get-FingerprintHint -Fingerprint $fpPSObj -CurrentHead 'msg-1' -CurrentControl 'run')
# Actually round-trip a whole quarantine record through ConvertTo-Json / ConvertFrom-Json to prove
# the shape a real live sweep sees on any tick after the failure tick. This is what the previous
# tests missed: they built @{} native hashtables directly and bypassed the serialization cycle.
$fake = @{
    state = 'quarantined'
    failure_fingerprint = @{ head = 'msg-100'; control = 'run' }
}
$roundTripped = ($fake | ConvertTo-Json -Depth 5) | ConvertFrom-Json
Check "round-tripped: hint still returned on head change" '新規メッセージあり (head 変化)' `
    (Get-FingerprintHint -Fingerprint $roundTripped.failure_fingerprint `
                         -CurrentHead 'msg-101' -CurrentControl 'run')

Write-Host "Get-FingerprintHint — names ONLY what the runner observes, never 'input changed'"
# Only head moved.
Check "head changed, control same -> head-only hint" '新規メッセージあり (head 変化)' `
    (Get-FingerprintHint -Fingerprint @{ head = 'msg-1'; control = 'run' } `
                         -CurrentHead 'msg-2' -CurrentControl 'run')
# Only control moved.
Check "head same, control changed -> control-only hint" 'control 変更あり' `
    (Get-FingerprintHint -Fingerprint @{ head = 'msg-1'; control = 'run' } `
                         -CurrentHead 'msg-1' -CurrentControl 'hold')
# Both moved.
Check "both changed -> both hints" '新規メッセージあり (head 変化) / control 変更あり' `
    (Get-FingerprintHint -Fingerprint @{ head = 'msg-1'; control = 'run' } `
                         -CurrentHead 'msg-2' -CurrentControl 'hold')
# Nothing moved. THE KEY CHECK: absence of hint must NOT be labeled as "not fixed." It means "no
# head movement, no control change." The label caller relies on $null to omit the hint entirely.
Check "nothing changed -> no hint (null, not 'not fixed')" $null `
    (Get-FingerprintHint -Fingerprint @{ head = 'msg-1'; control = 'run' } `
                         -CurrentHead 'msg-1' -CurrentControl 'run')
# Probe gap — unknown current values must not be misread as a change (silent false positive).
Check "current head unknown -> no head hint" $null `
    (Get-FingerprintHint -Fingerprint @{ head = 'msg-1'; control = 'run' } `
                         -CurrentHead $null -CurrentControl 'run')
Check "current control unknown -> no control hint" $null `
    (Get-FingerprintHint -Fingerprint @{ head = 'msg-1'; control = 'run' } `
                         -CurrentHead 'msg-1' -CurrentControl $null)
# Legacy record with a bare head-only fingerprint — must not throw, must not claim control change.
Check "stored control unknown -> no control hint" '新規メッセージあり (head 変化)' `
    (Get-FingerprintHint -Fingerprint @{ head = 'msg-1'; control = $null } `
                         -CurrentHead 'msg-2' -CurrentControl 'run')

Write-Host "Get-QuarantineReproHint — composes a hint from record fields only, never a new probe"
# T-sdk-is-error-loses-the-reason D-6 / S-8: the runner already has (head, control, log_path)
# in-hand at record-write time; the hint just draws them into something a human can paste. It
# must NOT accept a new external input, must NOT overclaim determinism ("repro_hint", not
# "repro"), and must fail-safe to $null when a piece is missing.
$hint = Get-QuarantineReproHint `
    -Fingerprint @{ head = 'msg-100'; control = 'run' } `
    -SessionLogPath 'C:/logs/conductor-2026-08-11.log' `
    -Key 'spirrow-mindwire/T-foo'
Check "hint names head" $true ($hint -like '*head=msg-100*')
Check "hint names control" $true ($hint -like '*control=run*')
Check "hint names the log path" $true ($hint -like '*C:/logs/conductor-2026-08-11.log*')
Check "hint uses the key so grep finds it" $true ($hint -like '*spirrow-mindwire/T-foo*')
# D-6 explicitly forbids the ``repro`` name — the H2 (systematic) judgement means "same failure
# twice", not "deterministic". A rendered ``repro:`` line would silently overclaim.
Check "hint uses the 'repro-hint' label (not 'repro')" $true ($hint -like 'repro-hint:*')

# Fail-safes — a missing piece drops the hint entirely rather than showing "head=None".
Check "no fingerprint -> null" $null `
    (Get-QuarantineReproHint -Fingerprint $null -SessionLogPath 'x' -Key 'k')
Check "missing head -> null" $null `
    (Get-QuarantineReproHint -Fingerprint @{ head = ''; control = 'run' } -SessionLogPath 'x' -Key 'k')
Check "missing control -> null" $null `
    (Get-QuarantineReproHint -Fingerprint @{ head = 'msg-1'; control = '' } -SessionLogPath 'x' -Key 'k')

# The JSON round-trip regression — every record older than the current tick reads as PSCustomObject,
# never hashtable. If the function ever declared ``[hashtable]$Fingerprint`` it would blow up on the
# very first quarantine older than one tick. Get-FingerprintHint's own tests cover the same shape;
# repeat here to pin this function under the same regularity.
$fpPSObj = [pscustomobject]@{ head = 'msg-99'; control = 'run' }
$hintPS = Get-QuarantineReproHint -Fingerprint $fpPSObj -SessionLogPath 'x' -Key 'k'
Check "PSCustomObject fingerprint survives" $true ($hintPS -like '*head=msg-99*')

Write-Host "New-QuarantineRecord — the failure fingerprint is exactly (head, control), nothing more"
$rec = New-QuarantineRecord -FirstFailureAt '2026-08-11T00:00:00Z' -ExitCode 2 `
    -StopReason 'no_handoff_to_human' -FailureHead 'msg-100' -FailureControl 'run' `
    -SessionLogPath 'C:/logs/conductor-2026-08-11.log' -SessionLogTail @('line1', 'line2')
Check "initial state is 'quarantined'" 'quarantined' $rec.state
Check "consecutive_failures starts at 1" 1 $rec.consecutive_failures
Check "exit_code preserved" 2 $rec.exit_code
Check "stop_reason preserved" 'no_handoff_to_human' $rec.stop_reason
Check "fingerprint has head" 'msg-100' $rec.failure_fingerprint.head
Check "fingerprint has control" 'run' $rec.failure_fingerprint.control
# The design regression the whole record exists to prevent: no external observation on the
# fingerprint. Anything the runner did not already have in its own hand does not go in here.
$hasGitRev = ($rec.failure_fingerprint.PSObject.Properties.Name -contains 'git_rev') -or
             ($rec.failure_fingerprint.Contains('git_rev'))
$hasConfigHash = ($rec.failure_fingerprint.PSObject.Properties.Name -contains 'config_hash') -or
                 ($rec.failure_fingerprint.Contains('config_hash'))
Check "fingerprint does NOT carry git_rev (§1)" $false $hasGitRev
Check "fingerprint does NOT carry config_hash (§1)" $false $hasConfigHash
Check "tail preserved" 2 $rec.session_log_tail.Count

Write-Host "Get-StarvedKeys — pivots on LIVE keys, not on the state file's accumulated keys"
$eval = @{
    'p/T-fresh'        = @{ last_evaluated_at = $now.AddHours(-1).ToString('o') }
    'p/T-old-worked'   = @{ last_evaluated_at = $now.AddHours(-25).ToString('o') }
    'p/T-quarantined'  = @{ last_evaluated_at = $now.AddHours(-30).ToString('o') }
    'p/T-never-live'   = @{ last_evaluated_at = $now.AddDays(-30).ToString('o') }
    'p/T-just-seen'    = @{ first_seen_at    = $now.AddHours(-1).ToString('o') }
    'p/T-never-launched-old' = @{ first_seen_at = $now.AddHours(-25).ToString('o') }
}
$live = @('p/T-fresh', 'p/T-old-worked', 'p/T-quarantined', 'p/T-brand-new',
          'p/T-just-seen', 'p/T-never-launched-old')
$starved = Get-StarvedKeys -EvaluatedState $eval -Now $now -LiveKeys $live
Check "starved list includes 25h-idle thread" $true ($starved -contains 'p/T-old-worked')
# The whole point of the Q4 honesty rule: a quarantined thread's evaluation timestamp is never
# refreshed, so it appears starved once its last real evaluation ages out. If this check ever fails,
# the metric has silently developed a blind spot exactly where the design refuses to have one.
Check "starved list includes an old quarantined thread" $true ($starved -contains 'p/T-quarantined')
Check "starved list excludes a fresh thread" $false ($starved -contains 'p/T-fresh')
# False-negative fix: a live candidate that has never actually launched (only first_seen_at is set)
# must count once its first_seen_at ages past the threshold. Missing this reproduces the exact "dark
# area" failure mode Q4 forbids — see the PR-gate review on #138.
Check "starved list includes a never-launched live thread once its first_seen_at ages out" $true `
    ($starved -contains 'p/T-never-launched-old')
Check "starved list excludes a just-first-seen live thread (age < 24h)" $false `
    ($starved -contains 'p/T-just-seen')
# The other side of the same coin: a live candidate not in EvaluatedState at all must not be starved
# on the very first tick — its 24h clock starts now, not at epoch zero. This complements the
# runner-side first_seen_at write (which is what promotes 'p/T-brand-new' to a normal entry on the
# next tick).
Check "starved list excludes a live candidate absent from the state file" $false `
    ($starved -contains 'p/T-brand-new')
# False-positive fix: a state key that is no longer in the live sweep list must not appear. Without
# pruning to $LiveKeys, folded threads sit in the file and their timestamps eventually age past the
# threshold, spamming the digest with entries the operator can no longer act on. This check IS the
# regression guard for the "removed keys spam forever" bug the PR-gate review named.
Check "starved list excludes an ex-live thread (removed from the sweep list)" $false `
    ($starved -contains 'p/T-never-live')

Write-Host "Format-DurationDigest — coarse, readable, no seconds"
Check "3d 4h" '3d 4h' (Format-DurationDigest -Span ([TimeSpan]::FromHours(76)))
Check "18h"   '18h'   (Format-DurationDigest -Span ([TimeSpan]::FromHours(18)))
Check "45m"   '45m'   (Format-DurationDigest -Span ([TimeSpan]::FromMinutes(45)))
Check "2d (exact)" '2d' (Format-DurationDigest -Span ([TimeSpan]::FromDays(2)))
Check "<1m" '<1m' (Format-DurationDigest -Span ([TimeSpan]::FromSeconds(15)))

Write-Host "New-DailyDigest — empty day is a heartbeat, not a no-op (spec §5, §7)"
$digest = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $now -LiveKeys @()
Check "empty digest still names the section '隔離中: 0 件'" $true ($digest -match '隔離中: 0 件')
Check "empty digest still names the section '飢餓 .* 0 件'" $true ($digest -match '飢餓.*0 件')
# The one thing an empty digest CANNOT do is be silent — that reproduces the failure mode this
# section exists to end. So the digest must always have content to send.
Check "empty digest is non-empty text" $true ($digest.Length -gt 0)
# Explicitly names that a silent day is intentional — otherwise a future editor may 'optimise'
# away the empty send and reproduce the ambiguity between 'nothing wrong' and 'channel dead.'
Check "empty digest mentions the heartbeat purpose" $true ($digest -match '通知チャネル')

Write-Host "New-DailyDigest — populated day shows escalation tier and fingerprint hint"
$q = @{
    'p/T-fresh'      = @{ state='quarantined'; first_failure_at = $now.AddHours(-1).ToString('o');
                          failure_fingerprint = @{ head='msg-1'; control='run' } }
    'p/T-escalated'  = @{ state='escalated';   first_failure_at = $now.AddDays(-2).ToString('o');
                          failure_fingerprint = @{ head='msg-2'; control='run' } }
    'p/T-stale-fp'   = @{ state='stale';       first_failure_at = $now.AddDays(-8).ToString('o');
                          failure_fingerprint = @{ head='msg-3'; control='run' } }
}
$heads = @{ p = @{ 'T-fresh' = 'msg-1'; 'T-escalated' = 'msg-2b'; 'T-stale-fp' = 'msg-3' } }
$ctrls = @{ p = [pscustomobject]@{ desired_state = 'run' } }
$digest = New-DailyDigest -QuarantineState $q -EvaluatedState @{} `
    -HeadsByProject $heads -ControlByProject $ctrls -Now $now `
    -LiveKeys @('p/T-fresh', 'p/T-escalated', 'p/T-stale-fp')
Check "digest mentions the escalated section" $true ($digest -match '\[escalated\]')
Check "digest mentions the stale section" $true ($digest -match '\[stale\]')
Check "digest carries the stale wording change" $true ($digest -match '直すか、スレッドを畳むか決めよ')
# T-escalated has a head change; T-fresh and T-stale-fp do not. Only T-escalated should carry the
# hint label, and it must be the specific head-change wording (never the deleted "input changed").
Check "escalated entry carries the head-change hint" $true ($digest -match 'T-escalated.*新規メッセージあり')
Check "fresh entry has NO hint (nothing changed)" $true ($digest -notmatch 'T-fresh.*新規メッセージあり')
Check "digest never uses the deleted 'input changed' wording (§4)" $true ($digest -notmatch '入力変化')

Write-Host "New-DailyDigest — repro_hint appears on a continuation line, not on the entry line"
# T-sdk-is-error-loses-the-reason S-8 (PR #181 round 4): the repro-hint is
# rendered on its own indented continuation line. Earlier the comment above
# the render site CLAIMED "one-line-per-entry" while the code emitted a hard
# newline — reconciled in favour of the newline (the hint content is 100+
# chars including the log path, so on-line wrapping is unreadable). Pin the
# actual behaviour so a future edit to either the comment or the code brings
# them back in sync intentionally, not by accident.
$qHint = @{
    'p/T-repro' = @{
        state = 'quarantined'
        first_failure_at = $now.AddHours(-2).ToString('o')
        failure_fingerprint = @{ head = 'msg-99'; control = 'run' }
        session_log_path = 'C:/logs/conductor-2026-08-11.log'
    }
}
$digest = New-DailyDigest -QuarantineState $qHint -EvaluatedState @{} `
    -HeadsByProject @{ p = @{ 'T-repro' = 'msg-99' } } `
    -ControlByProject @{ p = [pscustomobject]@{ desired_state = 'run' } } `
    -Now $now -LiveKeys @('p/T-repro')
Check "digest contains a repro-hint line" $true ($digest -match 'repro-hint:.*p/T-repro.*head=msg-99')
Check "repro-hint is on its OWN line (preceded by a newline + indent)" $true `
    ($digest -match "`n    repro-hint:")
# And the entry itself remains recognisable — the newline is BETWEEN the entry line and the hint,
# not IN the middle of the entry line.
Check "entry line for the key is intact above the repro-hint" $true `
    ($digest -match '  p/T-repro   \d')

Write-Host "New-DailyDigest — survives a real quarantine.json round-trip"
# The failure path the previous test suite did not exercise: build a quarantine map the way the
# sweep sees it on any tick AFTER the failure tick, i.e. with PSCustomObject values. Without the
# Get-FingerprintHint type fix, this throws before New-DailyDigest even returns; with the fix, the
# digest is rendered normally.
$live_rt = @('p/T-rt-fresh', 'p/T-rt-old')
$q_rt = @{}
foreach ($pair in @(
    @{ key = 'p/T-rt-fresh'; ageH = 1;  head = 'msg-a'; ctrl = 'run' }
    @{ key = 'p/T-rt-old';   ageH = 30; head = 'msg-b'; ctrl = 'run' }
)) {
    $native = @{
        state = 'quarantined'
        first_failure_at = $now.AddHours(-$pair.ageH).ToString('o')
        failure_fingerprint = @{ head = $pair.head; control = $pair.ctrl }
    }
    # ConvertFrom-Json returns PSCustomObject values, EXACTLY like Get-JsonState's output on read.
    $q_rt[$pair.key] = ($native | ConvertTo-Json -Depth 5) | ConvertFrom-Json
}
$digest_rt = New-DailyDigest -QuarantineState $q_rt -EvaluatedState @{} `
    -HeadsByProject @{ p = @{ 'T-rt-fresh' = 'msg-a'; 'T-rt-old' = 'msg-b2' } } `
    -ControlByProject @{ p = [pscustomobject]@{ desired_state = 'run' } } `
    -Now $now -LiveKeys $live_rt
Check "round-tripped digest renders without throwing" $true ($digest_rt.Length -gt 0)
Check "round-tripped digest surfaces the escalated entry" $true ($digest_rt -match 'T-rt-old')
Check "round-tripped digest picks up the head-change hint from a PSCustomObject" $true `
    ($digest_rt -match 'T-rt-old.*新規メッセージあり')

Write-Host "Get-SystemicAlertSignature — day-bucketed, so an ongoing wave alerts ONCE per day"
# The failure this dedup key exists to prevent: during a real systemic outage, tick T fills K=2 and
# stops; tick T+5min hits K=2 again on the next 2 candidates; and so on. A tick-timestamp signature
# defeats dedup entirely and floods Discord every 5 minutes. Bucketing on the UTC day keeps the
# alert at once per day of the wave, then silent — with re-arm on the next day.
$day1_t1 = [datetime]::Parse('2026-08-11T00:05:00Z').ToUniversalTime()
$day1_t2 = [datetime]::Parse('2026-08-11T23:55:00Z').ToUniversalTime()
$day2    = [datetime]::Parse('2026-08-12T00:05:00Z').ToUniversalTime()
Check "same UTC day, different ticks -> SAME signature (dedup wins)" $true `
    ((Get-SystemicAlertSignature -Now $day1_t1 -Count 2) -eq (Get-SystemicAlertSignature -Now $day1_t2 -Count 2))
Check "next UTC day -> DIFFERENT signature (alert re-arms)" $false `
    ((Get-SystemicAlertSignature -Now $day1_t1 -Count 2) -eq (Get-SystemicAlertSignature -Now $day2 -Count 2))
# The count is included so if a future edit changes K the operator gets one fresh alert on the day
# the new budget first bites. Not strictly required by the fix, but cheap insurance.
Check "signature does NOT carry the wall-clock time (no HH:mm)" $false `
    ((Get-SystemicAlertSignature -Now $day1_t1 -Count 2) -match ':\d\d:\d\d')

Write-Host "New-DailyDigest — starvation section is pivoted on LIVE keys"
# Same scenario as the Get-StarvedKeys checks: three live candidates, one launched-old, one
# never-launched-old, one just-seen-fresh — plus a lingering non-live key that must NOT appear.
$eval2 = @{
    'p/T-launched-old' = @{ last_evaluated_at = $now.AddHours(-30).ToString('o') }
    'p/T-never-old'    = @{ first_seen_at    = $now.AddHours(-30).ToString('o') }
    'p/T-just-seen'    = @{ first_seen_at    = $now.AddHours(-1).ToString('o') }
    'p/T-ex-live'      = @{ last_evaluated_at = $now.AddDays(-30).ToString('o') }
}
$live2 = @('p/T-launched-old', 'p/T-never-old', 'p/T-just-seen', 'p/T-brand-new')
$digest2 = New-DailyDigest -QuarantineState @{} -EvaluatedState $eval2 `
    -HeadsByProject @{} -ControlByProject @{} -Now $now -LiveKeys $live2
Check "starvation section lists a launched-then-idle live thread" $true `
    ($digest2 -match 'p/T-launched-old')
Check "starvation section lists a never-launched live thread with old first_seen_at" $true `
    ($digest2 -match 'p/T-never-old')
Check "the never-launched entry is labelled (未評価)" $true `
    ($digest2 -match 'p/T-never-old.*未評価')
Check "starvation section excludes a just-seen live thread" $true `
    ($digest2 -notmatch 'p/T-just-seen')
Check "starvation section excludes a brand-new live thread absent from state" $true `
    ($digest2 -notmatch 'p/T-brand-new')
# The critical false-positive guard: a state key that is not on the live sweep list must not appear
# in the digest, no matter how stale its timestamp. Without this, a folded thread would spam every
# day forever. This check IS the regression guard for the failure mode the PR-gate review named.
Check "starvation section excludes an ex-live thread (state file only)" $true `
    ($digest2 -notmatch 'p/T-ex-live')

Write-Host "Test-DigestDelivered / Test-DigestFullSuccess — the two questions after msg-2106"
# THE regression this exists to prevent: previously a webhook-less run had Send-Notification
# return $false on the "not configured" branch, the digest gate held its clock, digestDue stayed
# true forever, and every 5-min tick spammed 2 log lines.
#
# msg-2106 split the single "should the clock advance?" question into two, because degraded
# delivery satisfies cadence (mark the period sent) but NOT health (⚠ must stay up until a
# FULL delivery lands). Locking BOTH tables into helpers means any future edit that flips
# a case is caught here.
#
# The predicate is "should cadence advance?" — i.e., "is any further retry THIS period guaranteed
# not to help?". YES for delivery, YES for non-retryable failure (webhook dead — a 2nd POST 5 min
# later fails the same way, and holding the clock spams the log and Discord). NO only for
# transient failure, where the next tick has a real chance. (PR-gate naysayer 2026-08-30 caught
# that an earlier version of this predicate held cadence on 404, spamming for the rest of the day.)
Check "sent(ok) delivered? -> yes" $true (Test-DigestDelivered -Result @{ status='sent'; class='ok' })
Check "sent(ok) full success? -> yes" $true (Test-DigestFullSuccess -Result @{ status='sent'; class='ok' })

Check "degraded delivered? -> yes (cadence satisfied)" $true (Test-DigestDelivered -Result @{ status='degraded'; class='ok' })
Check "degraded full success? -> NO (⚠ still lit)" $false (Test-DigestFullSuccess -Result @{ status='degraded'; class='ok' })

Check "skipped(no-webhook) delivered? -> yes (no retry loop on missing webhook)" $true `
    (Test-DigestDelivered -Result @{ status='skipped'; class='no-webhook' })
Check "skipped(no-webhook) full success? -> NO (no channel means no informed operator)" $false `
    (Test-DigestFullSuccess -Result @{ status='skipped'; class='no-webhook' })

Check "failed(transient) delivered? -> NO (retry next tick)" $false `
    (Test-DigestDelivered -Result @{ status='failed'; class='transient' })
Check "failed(transient) full success? -> NO" $false `
    (Test-DigestFullSuccess -Result @{ status='failed'; class='transient' })

# CRITICAL: non-retryable failures MUST advance cadence. Held would mean the digest cadence spams
# a POST every 5 minutes for the rest of the day (404 → 404 → 404 → …). This is the exact spam
# loop the PR-gate naysayer flagged in the first revision of this file. Full-success stays false
# so ⚠ lights up on tomorrow's digest.
Check "failed(deterministic-permanent, e.g. 404) delivered? -> YES (webhook dead; do not spam)" $true `
    (Test-DigestDelivered -Result @{ status='failed'; class='deterministic-permanent' })
Check "failed(deterministic-permanent) full success? -> NO (channel not informed)" $false `
    (Test-DigestFullSuccess -Result @{ status='failed'; class='deterministic-permanent' })

# deterministic-payload only reaches this predicate when the FULL digest 400s AND degraded ALSO
# 400s (defensive — the degraded is fixed and small; this branch is theoretically unreachable but
# the predicate must still be safe if it is). Retry cannot help; advance cadence.
Check "failed(deterministic-payload, degraded also failed) delivered? -> YES (do not spam 400s)" $true `
    (Test-DigestDelivered -Result @{ status='failed'; class='deterministic-payload' })
Check "failed(deterministic-payload) full success? -> NO" $false `
    (Test-DigestFullSuccess -Result @{ status='failed'; class='deterministic-payload' })

# Legacy string-form still handled — Send-NotificationIfChanged historically dropped the return
# via $null=, but a future refactor may pass the string on. Under the string form there is no
# class, so 'failed' has to be held (fail-safe = retry, in case it was transient — this is the
# ONLY safe default when class is unknown).
Check "string 'sent' delivered? -> yes" $true (Test-DigestDelivered -Result 'sent')
Check "string 'failed' delivered? -> no (unknown class; fail-safe hold for retry)" $false `
    (Test-DigestDelivered -Result 'failed')

Write-Host "ConvertTo-UtcInstant — handles both string and [DateTime] inputs uniformly"
# Naive [datetime]::Parse on a DateTime happens to survive via implicit ToString + Parse-Local +
# ToUniversalTime cancellation. Under a non-invariant culture or a DateTimeKind change it silently
# produces off-by-hours ages. The helper takes both shapes and returns the same UTC instant.
# $expected is built the same way the helper does its final coercion, so the test is truly
# comparing "does the helper return the same UTC instant regardless of input shape?" rather than
# accidentally testing timezone-conversion arithmetic.
$expected = [datetime]::Parse('2026-08-11T12:00:00Z').ToUniversalTime()
Check "string ISO in -> UTC instant out" $expected (ConvertTo-UtcInstant '2026-08-11T12:00:00Z')
Check "[DateTime] Local in -> same UTC instant" $expected `
    (ConvertTo-UtcInstant ([datetime]::Parse('2026-08-11T12:00:00Z')))
Check "null in -> null out" $null (ConvertTo-UtcInstant $null)
# The value shape produced by an actual JSON round-trip — this is the case the runner sees on
# every tick after the first save.
$rt = (@{ ts = '2026-08-11T12:00:00Z' } | ConvertTo-Json | ConvertFrom-Json).ts
Check "round-tripped JSON value -> same UTC instant" $expected (ConvertTo-UtcInstant $rt)

Write-Host "Update-EvaluatedTimestamp — refreshes last_evaluated_at, preserves first_seen_at"
# The whole point of the extraction: every disposition that reached the candidate uses the same
# pattern. Test that behaviour once here so the per-callsite change is trivial. Compare by parsed
# UTC instant rather than by literal string, so the round-trip through ConvertTo-UtcInstant + ISO
# formatting is not conflated with textual precision (7-digit fractional seconds are canonical for
# ToString("o") — that IS the shape the runner writes to disk).
$state = @{
    'p/T-both'  = @{ first_seen_at = '2026-08-01T00:00:00Z'; last_evaluated_at = '2026-08-05T00:00:00Z' }
    'p/T-first' = @{ first_seen_at = '2026-08-01T00:00:00Z' }   # never launched yet
}
$fixed = [datetime]::Parse('2026-08-11T12:00:00Z').ToUniversalTime()
$aug1 = [datetime]::Parse('2026-08-01T00:00:00Z').ToUniversalTime()
Update-EvaluatedTimestamp -State $state -Key 'p/T-both' -Now $fixed
Check "refresh: last_evaluated_at moved to now" $fixed `
    ([datetime]::Parse($state['p/T-both'].last_evaluated_at).ToUniversalTime())
Check "refresh: first_seen_at preserved (same instant)" $aug1 `
    ([datetime]::Parse($state['p/T-both'].first_seen_at).ToUniversalTime())
Update-EvaluatedTimestamp -State $state -Key 'p/T-first' -Now $fixed
Check "first launch: last_evaluated_at now set" $fixed `
    ([datetime]::Parse($state['p/T-first'].last_evaluated_at).ToUniversalTime())
Check "first launch: first_seen_at kept" $aug1 `
    ([datetime]::Parse($state['p/T-first'].first_seen_at).ToUniversalTime())
# Brand-new key: no prior row, no first_seen_at anywhere. Just write last_evaluated_at.
Update-EvaluatedTimestamp -State $state -Key 'p/T-brand' -Now $fixed
Check "brand new key: last_evaluated_at set" $fixed `
    ([datetime]::Parse($state['p/T-brand'].last_evaluated_at).ToUniversalTime())
Check "brand new key: no first_seen_at (nothing to preserve)" $false ($state['p/T-brand'].ContainsKey('first_seen_at'))
# Round-tripped PSCustomObject — same duck typing concern as Get-FingerprintHint. Here it goes
# further: ConvertFrom-Json auto-parses ISO 8601 strings into DateTime OBJECTS, so a state entry
# read from disk has a [DateTime] where the writer put a [string]. Update-EvaluatedTimestamp must
# tolerate both and canonicalise the stored value back to an ISO string. If this ever regresses,
# a saved evaluated.json entry would crash the helper on the very next tick, or would silently
# accumulate mixed shapes that confuse Get-StarvedKeys.
$rt = ((@{ first_seen_at = '2026-08-01T00:00:00Z' } | ConvertTo-Json) | ConvertFrom-Json)
$state2 = @{ 'p/T-rt' = $rt }
Update-EvaluatedTimestamp -State $state2 -Key 'p/T-rt' -Now $fixed
$storedRt = $state2['p/T-rt'].first_seen_at
Check "PSCustomObject entry: first_seen_at is a string after canonicalisation" $true `
    ($storedRt -is [string])
Check "PSCustomObject entry: first_seen_at is parseable back to the original instant" `
    ([datetime]::Parse('2026-08-01T00:00:00Z').ToUniversalTime()) `
    ([datetime]::Parse($storedRt).ToUniversalTime())

Write-Host "Starvation semantics — a head-skipped thread must NOT flag as starved"
# THE regression the PR-gate flagged: a weekend-idle thread that head-skips correctly every tick
# used to still show as starved because head-skip did not refresh last_evaluated_at. If the fix
# ever regresses, this check turns red and the operator's Monday-morning digest fills with
# healthy threads. The fix (calling Update-EvaluatedTimestamp on head-skip) is exercised by the
# runner; here we lock the invariant it must satisfy at the metric layer.
$eval_hs = @{
    'p/T-weekend-idle' = @{
        first_seen_at    = $now.AddDays(-30).ToString('o')
        last_evaluated_at = $now.AddMinutes(-5).ToString('o')  # last tick's head-skip refreshed it
    }
}
$starved_hs = Get-StarvedKeys -EvaluatedState $eval_hs -Now $now -LiveKeys @('p/T-weekend-idle')
Check "head-skipped-every-tick thread is NOT starved (weekend idle scenario)" $false `
    ($starved_hs -contains 'p/T-weekend-idle')

Write-Host "Merge-StateForWrite — a mid-sweep operator clear must NOT be resurrected"
# The TOCTOU concern the PR-gate flagged: sweep reads state at T=0, holds it for minutes, operator
# clears an entry at T=1min (real disk write), sweep flushes at T=3min. Without merge-on-write,
# the operator's clear is silently overwritten because the sweep writes its stale in-memory map
# unconditionally. The merge helper re-reads disk at flush time and respects operator removals.
function New-MergeFixture {
    # Real temp file, because Merge-StateForWrite calls Get-JsonState which reads from disk.
    $path = Join-Path ([System.IO.Path]::GetTempPath()) ("mindwire-merge-" + [guid]::NewGuid().ToString('N') + ".json")
    return $path
}

# Case A: sweep reads {A,B}; operator clears A mid-sweep (disk now {B}); sweep tries to write
# {A,B}. Merge must produce {B} — A stays gone.
$path = New-MergeFixture
try {
    Save-JsonState -Path $path -State @{ A = @{ x=1 }; B = @{ x=2 } }
    $originalKeys = @('A', 'B')
    # Operator clears A on disk mid-sweep.
    Save-JsonState -Path $path -State @{ B = @{ x=2 } }
    # Sweep's in-memory state still has both.
    $memory = @{ A = @{ x=1 }; B = @{ x=2 } }
    $merged = Merge-StateForWrite -Memory $memory -OriginalKeys $originalKeys -DiskPath $path
    Check "operator clear survives: A dropped" $false ($merged.ContainsKey('A'))
    Check "operator clear survives: B kept" $true ($merged.ContainsKey('B'))
}
finally { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force } }

# Case B: sweep reads {A}; operator does NOT clear; sweep updates A's state and adds new C. Merge
# must produce {A(updated), C} — no losses, no resurrections.
$path = New-MergeFixture
try {
    Save-JsonState -Path $path -State @{ A = @{ state='quarantined' } }
    $originalKeys = @('A')
    # No operator action. Disk unchanged.
    $memory = @{ A = @{ state='escalated' }; C = @{ state='quarantined' } }
    $merged = Merge-StateForWrite -Memory $memory -OriginalKeys $originalKeys -DiskPath $path
    Check "sweep update to A applied (quarantined -> escalated)" 'escalated' $merged['A'].state
    Check "sweep add of C written through" $true ($merged.ContainsKey('C'))
    Check "no ghost keys" 2 $merged.Keys.Count
}
finally { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force } }

# Case C: sweep reads {A,B}; operator clears A AND sweep adds C this tick. Result: {B,C}.
# The single test that proves the merge respects the operator AND still writes new adds.
$path = New-MergeFixture
try {
    Save-JsonState -Path $path -State @{ A = @{ x=1 }; B = @{ x=2 } }
    $originalKeys = @('A', 'B')
    # Operator clears A.
    Save-JsonState -Path $path -State @{ B = @{ x=2 } }
    # Sweep adds C in memory (and still thinks it has A).
    $memory = @{ A = @{ x=1 }; B = @{ x=2 }; C = @{ x=3 } }
    $merged = Merge-StateForWrite -Memory $memory -OriginalKeys $originalKeys -DiskPath $path
    Check "combined: operator's A drop survives" $false ($merged.ContainsKey('A'))
    Check "combined: B still present" $true ($merged.ContainsKey('B'))
    Check "combined: sweep's new C written through" $true ($merged.ContainsKey('C'))
}
finally { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force } }

# Case D: empty starting disk, sweep adds A. Merge must produce {A}. Straight-through add path.
$path = New-MergeFixture
try {
    # File does not exist yet. Get-JsonState returns @{}.
    $originalKeys = @()
    $memory = @{ A = @{ x=1 } }
    $merged = Merge-StateForWrite -Memory $memory -OriginalKeys $originalKeys -DiskPath $path
    Check "fresh add on empty disk written through" $true ($merged.ContainsKey('A'))
}
finally { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force } }

# Case E: operator ADDS a key mid-sweep (not a real path for either file today, but the merge
# should still handle it gracefully — keep the operator's add). Cheap insurance against a future
# writer being added without noticing this contract.
$path = New-MergeFixture
try {
    Save-JsonState -Path $path -State @{ A = @{ x=1 } }
    $originalKeys = @('A')
    Save-JsonState -Path $path -State @{ A = @{ x=1 }; OP_ADDED = @{ y=99 } }
    $memory = @{ A = @{ x=1 } }
    $merged = Merge-StateForWrite -Memory $memory -OriginalKeys $originalKeys -DiskPath $path
    Check "operator-added key survives the sweep flush" $true ($merged.ContainsKey('OP_ADDED'))
}
finally { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force } }

Write-Host ""
# T-stalled-pr-has-no-detector Deliverable 6 — the failure_class field must survive
# round trips and default to 'unknown' when not supplied. Both branches matter:
#   * default: existing callers (nothing named the parameter) still write the field
#     so downstream group-by never sees a missing key.
#   * populated: a caller that classified the tail can pass the resolved label and
#     it lands on the record unchanged (byte-for-byte, no PSCustomObject rewrite).
Write-Host "New-QuarantineRecord — failure_class field (T-stalled-pr-has-no-detector D-6)"
$rec1 = New-QuarantineRecord -FirstFailureAt '2026-09-03T10:00:00Z' -ExitCode 1 `
    -StopReason 'reason=eod' -FailureHead 'msg-1' -FailureControl 'run' `
    -SessionLogPath 'C:\logs\a.log' -SessionLogTail @('one','two')
Check "default failure_class is 'unknown'" 'unknown' $rec1['failure_class']

$rec2 = New-QuarantineRecord -FirstFailureAt '2026-09-03T10:00:00Z' -ExitCode 1 `
    -StopReason 'reason=eod' -FailureHead 'msg-1' -FailureControl 'run' `
    -SessionLogPath 'C:\logs\a.log' -SessionLogTail @('one','two') `
    -FailureClass 'sdk-error-during-execution'
Check "explicit failure_class is preserved" 'sdk-error-during-execution' $rec2['failure_class']

# Round-trip test — the field must survive the same JSON path failure_fingerprint does.
$rt = ($rec2 | ConvertTo-Json -Depth 5) | ConvertFrom-Json
Check "failure_class survives JSON round-trip" 'sdk-error-during-execution' $rt.failure_class

# Get-FailureClass — no tail means unknown, no subprocess spawned. This is the branch
# the sweep hits when the failure produced no captured output (msg-2354 §1 M-2's
# "last_msg 空" case), so it MUST return quickly and MUST NOT surface an exception.
Write-Host "Get-FailureClass — empty tail returns 'unknown' without spawning python"
Check "empty tail -> unknown" 'unknown' (Get-FailureClass -SessionLogTail @())
Check "null tail -> unknown" 'unknown' (Get-FailureClass -SessionLogTail $null)

if ($script:failures -gt 0) {
    Write-Host "sweep quarantine: $($script:failures) check(s) FAILED"
    exit 1
}
Write-Host "sweep quarantine: all checks passed"
exit 0
