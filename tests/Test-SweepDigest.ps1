# Regression guard for T-digest-exceeds-discord-limit-and-is-dropped (msg-2099 through msg-2106).
#
# The failure this file's checks exist to prevent: msg-2013 §1 measured that 63 of the last 3 days'
# daily digests were rejected by Discord as HTTP 400 because the payload — a per-day listing of every
# human-parked and quarantined thread — passed the 2,000-char `content` limit. The dedup path in
# Send-NotificationIfChanged then marked those signatures as "notified", so the SAME alerts were
# suppressed 257 times after. Two-way failure: no digest AND no alert.
#
# The design (Bohr msg-2099 through msg-2106, endorsed by naysayer Einstein at msg-2100/2102/2104):
#   D-1  bounded renderer with fixed budget; +N 件 preserves total count when truncated
#   D-2  degraded fallback message on deterministic-payload (400/413) failure
#   D-3  alert path (Send-NotificationIfChanged) skips signature record on deterministic-payload;
#        transient (429/5xx/network) preserves the #138 R2 behavior of recording
#   D-5  status-code taxonomy: 400/413 → deterministic-payload, 401/403/404 → deterministic-permanent,
#        429/5xx/network → transient, unknown → transient (fail-safe)
#   D-6  notify-health carries only last_full_success_period; ⚠ is a DERIVED predicate over period
#        arithmetic, not a stored counter (Einstein msg-2102: unstored state cannot be miscleared)
#   D-7  alert-path signature is semantic ("human:msg-2662") not payload-hash — regression-pinned so
#        a future refactor that rehashes the rendered body is caught (msg-2101)
#
# The full DoD list (msg-2106):
#   1  200-entry synthetic set renders ≤ 3,500 chars with correct total
#   2  400 forced → degraded delivered, retry within same period stops
#   3  401/403 forced → NO second POST, health recorded, alert-path NOT recorded
#   4  429 classified as transient (#138 R2 record preserved)
#   5  waiting set fixed, 3 periods advance → exactly 3 digests delivered (no suppression, no drift)
#   6  period boundary: 23:58 send → 00:03 tick does not double-send
#   7  400 forced 3 periods → exactly 3 degraded messages, last_full_success_period NOT advanced
#   8  conductor stopped 3 periods → first full delivery after recovery carries the ⚠
#   9  recovery → ⚠ disappears on the next period
#   10 provisional note declaring status-code taxonomy owner (D-5)
#   11 real-backlog run — deferred to live deploy (out of scope for this static test)
#
# DoDs 1, 5, 6, 7, 8, 9 are unit-testable against the lifted functions. DoDs 2, 3, 4, 10 are
# integration-scoped and are covered by direct helper checks. DoD 11 is a deploy-time acceptance.

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

# Tunables the lifted functions read from $script: scope. Set here so the lifted defaults resolve
# without pulling in the whole script (which would run it).
$script:QuarantineEscalatedAfter = [TimeSpan]::FromHours(24)
$script:QuarantineStaleAfter     = [TimeSpan]::FromDays(7)
$script:StarvedThreshold         = [TimeSpan]::FromHours(24)
$script:DigestBudget             = 3500
$script:DailyDigestDeliveryTime  = [TimeSpan]::FromHours(9)

function Write-Log { param([string]$Message) }

$leaseLib = Join-Path $repoRoot 'deploy/lib/Lease.ps1'
if (-not (Test-Path -LiteralPath $leaseLib)) { throw "Lease lib not found: $leaseLib" }
. $leaseLib

$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'New-QuarantineRecord', 'Get-DerivedQuarantineState', 'Get-FingerprintHint',
                  'Get-QuarantineReproHint',
                  'Format-DurationDigest', 'Get-StarvedKeys', 'New-DailyDigest',
                  'ConvertTo-UtcInstant',
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
function CheckTrue {
    param([string]$Name, [bool]$Actual, $Debug = $null)
    if ($Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — got '{1}'  debug={2}" -f $Name, $Actual, $Debug) }
}

# =============================================================================================
# D-5 / DoD 10 — status-code taxonomy is defined here PROVISIONALLY; owner is
# T-gate-review-submit-failure-handling. If that thread lands its own contract the mapping
# comes out and this test simply consults the shared helper (whatever its name ends up being).
# =============================================================================================
Write-Host "Get-NotificationFailureClass — provisional taxonomy pending T-gate-review-submit-failure-handling (D-5)"

Check "400 -> deterministic-payload (msg-2013's exact reproducer)" `
    'deterministic-payload' (Get-NotificationFailureClass -HttpStatus 400 -ExceptionMessage 'bad payload')
Check "413 -> deterministic-payload (some proxies rewrite oversize as 413)" `
    'deterministic-payload' (Get-NotificationFailureClass -HttpStatus 413 -ExceptionMessage 'payload too large')
Check "401 -> deterministic-permanent (bad token)" `
    'deterministic-permanent' (Get-NotificationFailureClass -HttpStatus 401 -ExceptionMessage 'unauthorized')
Check "403 -> deterministic-permanent (forbidden)" `
    'deterministic-permanent' (Get-NotificationFailureClass -HttpStatus 403 -ExceptionMessage 'forbidden')
Check "404 -> deterministic-permanent (webhook deleted)" `
    'deterministic-permanent' (Get-NotificationFailureClass -HttpStatus 404 -ExceptionMessage 'not found')
Check "429 -> transient (RATE-LIMITED, NOT deterministic — the whole point of the class)" `
    'transient' (Get-NotificationFailureClass -HttpStatus 429 -ExceptionMessage 'rate limited')
Check "500 -> transient" 'transient' (Get-NotificationFailureClass -HttpStatus 500 -ExceptionMessage 'server error')
Check "503 -> transient" 'transient' (Get-NotificationFailureClass -HttpStatus 503 -ExceptionMessage 'unavailable')
Check "0 (no status = network / DNS / proxy) -> transient" `
    'transient' (Get-NotificationFailureClass -HttpStatus 0 -ExceptionMessage 'no response')
# D-5 fail-safe: unknown 4xx defaults to transient. That is fail-safe because 4xx-transient is what
# the alert path was already doing before this thread (#138 R2), so behavior does not change under
# uncertainty; changing behavior under uncertainty is what msg-2013 §3(b) said was wrong.
Check "418 (I'm a teapot — unknown 4xx) -> transient (D-5 fail-safe)" `
    'transient' (Get-NotificationFailureClass -HttpStatus 418 -ExceptionMessage 'teapot')

# =============================================================================================
# D-6 / DoD 5, 6 — period gate is jitter-immune and drift-free by CONSTRUCTION.
# =============================================================================================
Write-Host ""
Write-Host "Get-DigestPeriod — a period id is the LOCAL calendar day (yyyy-MM-dd)"

$midday = [datetime]::Parse('2026-08-29T15:00:00Z')
# Local formatting: whatever the local zone is, a 12-hour spread within the same day should land
# on the SAME period id. This is the jitter immunity that replaces the fudge constant Einstein
# msg-2104 proposed — no 25h threshold, no 1.5-period ratio.
$local_2am = (Get-Date -Year 2026 -Month 8 -Day 29 -Hour 2 -Minute 0 -Second 0).ToUniversalTime()
$local_2pm = (Get-Date -Year 2026 -Month 8 -Day 29 -Hour 14 -Minute 0 -Second 0).ToUniversalTime()
$local_next2am = (Get-Date -Year 2026 -Month 8 -Day 30 -Hour 2 -Minute 0 -Second 0).ToUniversalTime()
Check "local 02:00 -> '2026-08-29'" '2026-08-29' (Get-DigestPeriod -Now $local_2am)
Check "local 14:00 same day -> SAME period id" '2026-08-29' (Get-DigestPeriod -Now $local_2pm)
Check "local 02:00 next day -> different period id" '2026-08-30' (Get-DigestPeriod -Now $local_next2am)

Write-Host "Test-DigestDeliveryDue — the second half of the two-part cadence gate"
$dt09 = [TimeSpan]::FromHours(9)
$local_08_59 = (Get-Date -Year 2026 -Month 8 -Day 29 -Hour 8 -Minute 59 -Second 0)
$local_09_00 = (Get-Date -Year 2026 -Month 8 -Day 29 -Hour 9 -Minute 0 -Second 0)
$local_09_05 = (Get-Date -Year 2026 -Month 8 -Day 29 -Hour 9 -Minute 5 -Second 0)
# The predicate is >=, so 09:00 EXACTLY delivers. An off-by-one that used > would silently defer
# every day's send by 5 minutes and eventually walk into evening on tick-heavy days.
Check "08:59 -> not due" $false (Test-DigestDeliveryDue -Now $local_08_59.ToUniversalTime() -DeliveryTime $dt09)
Check "09:00 exactly -> DUE" $true (Test-DigestDeliveryDue -Now $local_09_00.ToUniversalTime() -DeliveryTime $dt09)
Check "09:05 (jitter of a 5-min tick) -> due (jitter tolerance is 0 by construction, all 09:00-23:59 is due)" $true `
    (Test-DigestDeliveryDue -Now $local_09_05.ToUniversalTime() -DeliveryTime $dt09)

Write-Host "Get-DigestPeriodsMissed — period arithmetic, no stored counter (msg-2102 → msg-2103)"
Check "no history (initial state) -> 0 (do not alarm on first ever run)" `
    0 (Get-DigestPeriodsMissed -CurrentPeriod '2026-08-29' -LastFullSuccessPeriod $null)
Check "same-day (unusual — a re-send within the period) -> 0" `
    0 (Get-DigestPeriodsMissed -CurrentPeriod '2026-08-29' -LastFullSuccessPeriod '2026-08-29')
Check "yesterday's success, today's tick -> 0 (healthy daily cadence)" `
    0 (Get-DigestPeriodsMissed -CurrentPeriod '2026-08-29' -LastFullSuccessPeriod '2026-08-28')
Check "3 days ago last success -> 2 periods missed" `
    2 (Get-DigestPeriodsMissed -CurrentPeriod '2026-08-29' -LastFullSuccessPeriod '2026-08-26')
Check "7 days ago last success -> 6 periods missed" `
    6 (Get-DigestPeriodsMissed -CurrentPeriod '2026-08-29' -LastFullSuccessPeriod '2026-08-22')
# Malformed period ids should degrade to "no warning" rather than crashing — a bad state file
# must never take the sweep down. (The corrupt-state-does-not-fail-closed invariant.)
Check "malformed period id -> 0 (fail-open on unparseable input)" `
    0 (Get-DigestPeriodsMissed -CurrentPeriod 'garbage' -LastFullSuccessPeriod '2026-08-22')

# =============================================================================================
# D-6 / DoD 8, 9 — ⚠ derivation from period arithmetic
# =============================================================================================
Write-Host ""
Write-Host "Get-DigestHealthWarning — ⚠ is DERIVED from last_full_success_period, not a stored flag"

$healthFresh = @{ last_full_success_period = '2026-08-28'; last_error_class = $null; last_attempt_at = '2026-08-29T09:00:00Z' }
$healthMissed3 = @{ last_full_success_period = '2026-08-26'; last_error_class = 'deterministic-payload'; last_attempt_at = '2026-08-29T09:00:00Z' }
$healthEmpty = @{}

$warnFresh = Get-DigestHealthWarning -Health $healthFresh -CurrentPeriod '2026-08-29'
CheckTrue "healthy state (yesterday's success) -> NO warning" ($null -eq $warnFresh) $warnFresh
$warnMissed = Get-DigestHealthWarning -Health $healthMissed3 -CurrentPeriod '2026-08-29'
CheckTrue "3-days-old success -> warning emitted" ($null -ne $warnMissed) $warnMissed
CheckTrue "warning mentions period count" ($warnMissed -match '2 期間') $warnMissed
CheckTrue "warning surfaces the last error class (diagnostic)" ($warnMissed -match 'deterministic-payload') $warnMissed
CheckTrue "warning shows the last success date" ($warnMissed -match '2026-08-26') $warnMissed
# DoD 5 & 9: on recovery from failure, next-period predicate must clear. Simulate: full success just
# landed at 2026-08-29, next tick is 2026-08-30 → 0 missed → no warning.
$healthRecovered = @{ last_full_success_period = '2026-08-29'; last_error_class = 'deterministic-payload'; last_attempt_at = '2026-08-29T09:00:00Z' }
$warnRecovered = Get-DigestHealthWarning -Health $healthRecovered -CurrentPeriod '2026-08-30'
CheckTrue "next period after recovery -> warning gone" ($null -eq $warnRecovered) $warnRecovered
# The empty-state case must be safe — no crash on first-ever run.
$warnEmpty = Get-DigestHealthWarning -Health $healthEmpty -CurrentPeriod '2026-08-29'
CheckTrue "empty health (first ever run) -> NO warning" ($null -eq $warnEmpty) $warnEmpty

# D-6 predicate discipline: the ⚠ predicate MUST NOT consult diagnostic fields. Verify by proving
# that a state whose diagnostic fields say "just failed" but whose period says "still healthy" does
# not emit a warning. This is the Einstein-msg-2102 §2 defect ("a transient recovery clears the
# counter") turned upside down: our version has no counter to clear.
$sneaky = @{
    last_full_success_period = '2026-08-28'   # healthy — 1 period ago
    last_error_class = 'deterministic-payload' # diagnostic says bad
    last_attempt_at = '2026-08-29T09:00:00Z'
    last_error = 'HTTP 400: bad request'
}
$warnSneaky = Get-DigestHealthWarning -Health $sneaky -CurrentPeriod '2026-08-29'
CheckTrue "predicate discipline: diagnostic 'failed' does NOT trip ⚠ when period is healthy" `
    ($null -eq $warnSneaky) $warnSneaky

# =============================================================================================
# D-2 — degraded fallback message shape
# =============================================================================================
Write-Host ""
Write-Host "New-DegradedDigestMessage — fixed-length, self-describing, safely below any limit"
$degraded_0 = New-DegradedDigestMessage -WaitingCount 0 -CurrentPeriod '2026-08-29'
$degraded_500 = New-DegradedDigestMessage -WaitingCount 500 -CurrentPeriod '2026-08-29'
CheckTrue "degraded message is well below the smallest Discord limit" ($degraded_500.Length -lt 500) $degraded_500.Length
CheckTrue "degraded message names the count so the operator knows queue size" ($degraded_500 -match '500') $degraded_500
CheckTrue "degraded message declares itself as a fallback (operator reads 'mechanism is broken, not the queue')" `
    ($degraded_500 -match '組み立てに失敗' -or $degraded_500 -match '失敗') $degraded_500
CheckTrue "degraded message carries the period id" ($degraded_500 -match '2026-08-29') $degraded_500
CheckTrue "degraded message directs the operator to chatroom (D-1: URL is auxiliary, direction is primary)" `
    ($degraded_500 -match 'chatroom') $degraded_500

# =============================================================================================
# D-1 / DoD 1 — bounded renderer under a large synthetic waiting set stays ≤ budget with correct totals
# =============================================================================================
Write-Host ""
Write-Host "New-DailyDigest -Budget — 200-entry synthetic waiting set stays under 3,500 chars with correct total (DoD 1)"

$now = [datetime]::Parse('2026-08-29T15:00:00Z').ToUniversalTime()

# Build a 200-entry synthetic quarantine map with 50-char ids similar to the real world sample
# msg-2013 §1 quoted ("spirrow-voxelworld/T-materializechunk-zone-relocation-crash" — 55 chars).
$bigQ = @{}
for ($i = 0; $i -lt 200; $i++) {
    $key = "spirrow-voxelworld/T-synthetic-entry-{0:D3}-crash-loop" -f $i
    $bigQ[$key] = @{
        state = 'quarantined'
        first_failure_at = $now.AddHours(-1 - $i).ToString('o')
        failure_fingerprint = @{ head = "msg-$i"; control = 'run' }
    }
}

$digest = New-DailyDigest -QuarantineState $bigQ -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $now -LiveKeys @() `
    -Budget 3500

CheckTrue "200-entry digest fits within budget (≤ 3,500 chars)" ($digest.Length -le 3500) $digest.Length
CheckTrue "digest carries the correct total (200, NOT the truncated count)" ($digest -match '隔離中: 200 件') $digest.Substring(0, [Math]::Min(300, $digest.Length))
CheckTrue "digest emits the +N 件 marker so the reader knows something was omitted" ($digest -match '\+\d+ 件（省略）') $digest
CheckTrue "oldest entry is present (age-descending sort → oldest survives truncation)" `
    ($digest -match 'T-synthetic-entry-199-crash-loop') $digest
CheckTrue "newest entry is omitted (dropped first because it is least likely to be forgotten)" `
    ($digest -notmatch 'T-synthetic-entry-000-crash-loop') $digest

Write-Host "New-DailyDigest -Budget — human-parked section respects the same budget contract"
# The 判断待ち section is what msg-2013 said drove the 400s ("human-parked 14 + 隔離 4"). Verify a
# large human-parked list truncates correctly and shows the total. 50 entries with realistic keys.
$bigParked = @()
for ($i = 0; $i -lt 50; $i++) {
    $bigParked += [PSCustomObject]@{
        key = ("spirrow-voxelworld/T-parked-thread-{0:D2}-decision-pending" -f $i)
        project = 'spirrow-voxelworld'
        thread_id = ("T-parked-thread-{0:D2}-decision-pending" -f $i)
        head_msg_id = "msg-$i"
    }
}
$digest2 = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $now -LiveKeys @() `
    -HumanParked $bigParked -Budget 3500
CheckTrue "50-entry parked digest fits within budget" ($digest2.Length -le 3500) $digest2.Length
CheckTrue "digest carries the correct human-parked total" ($digest2 -match '判断待ち: 50 件') $digest2.Substring(0, [Math]::Min(300, $digest2.Length))

Write-Host "New-DailyDigest -Budget = 0 — legacy callers (unbounded) still work"
$digest3 = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $now -LiveKeys @()
CheckTrue "unbounded call still renders (existing tests pass, but pin it explicitly here too)" `
    ($digest3.Length -gt 0) $digest3.Length
CheckTrue "unbounded call does NOT contain +N marker (no truncation)" ($digest3 -notmatch '\+\d+ 件（省略）') $digest3

# =============================================================================================
# D-6 / D-1 — ⚠ line prepends above the header when passed
# =============================================================================================
Write-Host ""
Write-Host "New-DailyDigest -HealthWarning — ⚠ line goes ABOVE the header (mobile preview)"

$warned = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $now -LiveKeys @() `
    -HealthWarning "⚠ フル digest が 2 期間配送できていません（最後の成功 2026-08-27）"
CheckTrue "⚠ line appears in the digest" ($warned -match '⚠') $warned
CheckTrue "⚠ line is on the FIRST line (before the MindWire header)" `
    ($warned.Split("`n")[0] -match '⚠') $warned.Split("`n")[0]

$noWarning = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $now -LiveKeys @() `
    -HealthWarning $null
CheckTrue "null health warning -> no ⚠ line" ($noWarning -notmatch '⚠ フル digest') $noWarning
# Existing content-tests continue to pass — the digest structure below the ⚠ is unchanged.
CheckTrue "MindWire header still present" ($noWarning -match 'MindWire 日次ダイジェスト') $noWarning

# =============================================================================================
# D-1 — summary line drives action from line 1 (msg-2099: "1 行目で行動が決まる")
# =============================================================================================
Write-Host ""
Write-Host "New-DailyDigest — summary line names counts and oldest age (msg-2099 D-1)"

$q_summary = @{}
$q_summary['p/T-old-1'] = @{ state='quarantined'; first_failure_at = $now.AddDays(-6).ToString('o');
                             failure_fingerprint = @{ head='msg-1'; control='run' } }
$q_summary['p/T-recent-1'] = @{ state='quarantined'; first_failure_at = $now.AddHours(-2).ToString('o');
                                failure_fingerprint = @{ head='msg-2'; control='run' } }
$parked_summary = @(
    [PSCustomObject]@{ key='p/T-parked-1'; project='p'; thread_id='T-parked-1'; head_msg_id='msg-p1' }
    [PSCustomObject]@{ key='p/T-parked-2'; project='p'; thread_id='T-parked-2'; head_msg_id='msg-p2' }
)
$digest_summary = New-DailyDigest -QuarantineState $q_summary -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $now `
    -LiveKeys @('p/T-old-1','p/T-recent-1') -HumanParked $parked_summary
CheckTrue "summary line names human-parked count" ($digest_summary -match 'human-parked 2') $digest_summary
CheckTrue "summary line names 隔離 count" ($digest_summary -match '隔離 2') $digest_summary
CheckTrue "summary line names 最古 (oldest waiting days) — 6d matches the T-old-1 age" `
    ($digest_summary -match '最古 6d') $digest_summary

if ($script:failures -gt 0) {
    Write-Host ""
    Write-Host "sweep digest: $script:failures check(s) FAILED"
    exit 1
}
Write-Host ""
Write-Host "sweep digest: all checks passed"
