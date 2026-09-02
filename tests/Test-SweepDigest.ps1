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
#   D-6  notify-health carries only period-typed fields (last_full_success_period plus, since E-4,
#        first_attempt_period); ⚠ is a DERIVED predicate over period arithmetic, not a stored
#        counter (Einstein msg-2102: unstored state cannot be miscleared)
#   D-7  alert-path signature is semantic ("human:msg-2662") not payload-hash — regression-pinned so
#        a future refactor that rehashes the rendered body is caught (msg-2101)
#
# The full DoD list (msg-2106):
#   1  200-entry synthetic set renders within the shipped budget with correct total
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
$script:DailyDigestDeliveryTime  = [TimeSpan]::FromHours(9)

# $DigestBudget is READ FROM the script under test, never restated here (Einstein msg-2396 E-2,
# Bohr msg-2401 §3-1). The regression that motivates the parse is not "the number was wrong" but
# "this harness kept its own copy of the number": this line said 3500 and so did
# deploy/run-conductor-scheduled.ps1:102, so when production's value was wrong the suite could only
# prove the wrong value was self-consistent with itself. 1663 checks passed green on 2026-09-01 and
# 09-02 while every real digest was rejected 400. A literal here can drift from production; a parse
# cannot. Every budget assertion below compares against THIS value, and the one assertion that
# validates the value itself lives in the transport block further down.
$budgetAssignments = $ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $n.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
    $n.Left.VariablePath.UserPath -eq 'DigestBudget' }, $true)
if ($budgetAssignments.Count -ne 1) {
    throw "expected exactly one `$DigestBudget assignment in the sweep script, found $($budgetAssignments.Count)"
}
$script:DigestBudget = [int]$budgetAssignments[0].Right.Extent.Text

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
                  'New-DegradedDigestMessage', 'Get-NotificationFailureClass',
                  # For the PR-gate regression block below (alert-path spam-loop pin,
                  # degraded-fallback class-propagation pin).
                  'Test-NotificationSuppressed', 'Send-NotificationIfChanged',
                  'Resolve-DigestSendResult') {
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

# E-4 (Einstein msg-2396, accepted whole by Bohr msg-2401 §5) — "never succeeded" is NOT "first run".
#
# The measured failure: live state/notify-health.json carried last_error / last_error_class /
# last_attempt_at and no last_full_success_period at all, because that key is written only on a full
# success and there had not been one since #203 landed. The rule "no success record -> return 0"
# then read the total absence of success as a healthy first run, so the ⚠ stayed dark for exactly
# the state it exists to announce. A success record is an EDGE; "has never succeeded" is a LEVEL,
# and a level cannot be derived from an edge record alone. first_attempt_period supplies the
# missing lower bound: it says when the failing started, without any time-typed field entering the
# predicate (D-6 predicate discipline is preserved — every input here is a period id).
Check "never succeeded, first attempt is TODAY -> 0 (the attempt is still in flight)" `
    0 (Get-DigestPeriodsMissed -CurrentPeriod '2026-08-29' -LastFullSuccessPeriod $null -FirstAttemptPeriod '2026-08-29')
Check "never succeeded, first attempt YESTERDAY -> 1 (yesterday's attempt failed and nobody was told)" `
    1 (Get-DigestPeriodsMissed -CurrentPeriod '2026-08-29' -LastFullSuccessPeriod $null -FirstAttemptPeriod '2026-08-28')
Check "never succeeded since 2026-08-26 -> 3 (the live #203 regression shape)" `
    3 (Get-DigestPeriodsMissed -CurrentPeriod '2026-08-29' -LastFullSuccessPeriod $null -FirstAttemptPeriod '2026-08-26')
# The two branches differ by one for the same delta, and that is the point: on the success branch
# the boundary period DELIVERED, on this branch it FAILED. Pinned side by side so a future
# "simplification" that collapses them has to argue with this line.
Check "same delta, success branch -> 0 while never-succeeded branch -> 1" `
    0 (Get-DigestPeriodsMissed -CurrentPeriod '2026-08-29' -LastFullSuccessPeriod '2026-08-28' -FirstAttemptPeriod '2026-08-28')
Check "a recorded success WINS over first_attempt_period (the stronger evidence)" `
    0 (Get-DigestPeriodsMissed -CurrentPeriod '2026-08-29' -LastFullSuccessPeriod '2026-08-28' -FirstAttemptPeriod '2026-01-01')
Check "neither field -> 0 (genuinely nothing has been attempted yet)" `
    0 (Get-DigestPeriodsMissed -CurrentPeriod '2026-08-29' -LastFullSuccessPeriod $null -FirstAttemptPeriod $null)
Check "malformed first_attempt_period -> 0 (corrupt state must not take the sweep down)" `
    0 (Get-DigestPeriodsMissed -CurrentPeriod '2026-08-29' -LastFullSuccessPeriod $null -FirstAttemptPeriod 'garbage')

# =============================================================================================
# D-6 / DoD 8, 9 — ⚠ derivation from period arithmetic
# =============================================================================================
Write-Host ""
Write-Host "Get-DigestHealthWarning — ⚠ is DERIVED from the period fields, not a stored flag"

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

# E-4 at the warning level. $healthNeverSucceeded is the LIVE shape measured on
# state/notify-health.json during the #203 regression, plus the first_attempt_period this change
# adds: three days of 400s and not one full delivery.
$healthNeverSucceeded = @{
    first_attempt_period = '2026-08-26'
    last_error_class = 'deterministic-payload'
    last_error = 'Response status code does not indicate success: 400 (Bad Request).'
    last_attempt_at = '2026-08-29T00:00:15.4356674Z'
}
$warnNever = Get-DigestHealthWarning -Health $healthNeverSucceeded -CurrentPeriod '2026-08-29'
CheckTrue "never-succeeded state -> warning emitted (the fail-open E-4 reported)" ($null -ne $warnNever) $warnNever
CheckTrue "never-succeeded warning says 一度も, not a fabricated last-success date" `
    ($warnNever -match '一度も') $warnNever
CheckTrue "never-succeeded warning counts the periods (3)" ($warnNever -match '3 期間') $warnNever
CheckTrue "never-succeeded warning names when the failing started" ($warnNever -match '2026-08-26') $warnNever
# The old wording interpolated an absent last-success into "（最後の成功 ）". Whatever the wording
# becomes, it must never render an empty date as though it were one.
CheckTrue "never-succeeded warning does NOT render an empty 最後の成功 field" `
    ($warnNever -notmatch '最後の成功\s*[）)]') $warnNever
# And the state that motivated the whole pin: first_attempt_period absent AND no success recorded
# is still treated as a first run, because in that state we genuinely cannot tell.
$warnUnknown = Get-DigestHealthWarning -Health @{ last_error_class = 'deterministic-payload' } -CurrentPeriod '2026-08-29'
CheckTrue "no period history at all -> still NO warning (nothing has been attempted)" ($null -eq $warnUnknown) $warnUnknown

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
# E-2 (Einstein msg-2396 / Bohr msg-2401 §3) — the budget must be legal for the transport the
# digest actually ships on. This block is the one place that judges the VALUE of $DigestBudget;
# every other budget check in this file only asks "does the render fit the value we ship".
#
# What went wrong without it: the constant was chosen at 3,500 for `embed.description` (4,096),
# the transport move to embeds never happened, and the sender kept posting `content` (2,000). No
# check anywhere compared the budget to the limit of the transport in use, so the arithmetic error
# survived in a comment ("3,500 is well under the `content` 2,000 hard limit") and in the shipped
# constant, through a full PR-gate review, for four days of daily 400s.
# =============================================================================================
Write-Host ""
Write-Host "E-2 — shipped digest budget is legal for the transport the sender actually uses"

$sweepText = Get-Content -Raw -LiteralPath $sweepScript

# Discord's hard limit for a single message's `content` field.
$DiscordContentHardLimit = 2000

CheckTrue "the sender posts the digest as Discord ``content`` (the field whose limit is $DiscordContentHardLimit)" `
    ($sweepText -match '@\{\s*content\s*=\s*\$Message\s*\}') 'Send-Notification payload shape'
CheckTrue "shipped `$DigestBudget ($($script:DigestBudget)) is within the ``content`` hard limit ($DiscordContentHardLimit)" `
    ($script:DigestBudget -le $DiscordContentHardLimit) $script:DigestBudget
# Both Discord budgets in the wrapper answer to the same limit. Pinned together so raising one
# without the other is visible: they were 3500 and 1950 for the same 2,000-char field.
$decisionBudgetAssignments = $ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $n.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
    $n.Left.VariablePath.UserPath -eq 'DecisionMessageDiscordBudget' }, $true)
CheckTrue "`$DecisionMessageDiscordBudget is also within the ``content`` hard limit" `
    ($decisionBudgetAssignments.Count -eq 1 -and [int]$decisionBudgetAssignments[0].Right.Extent.Text -le $DiscordContentHardLimit) `
    $(if ($decisionBudgetAssignments.Count -eq 1) { $decisionBudgetAssignments[0].Right.Extent.Text } else { "assignments=$($decisionBudgetAssignments.Count)" })

# E-4's field needs a WRITER, not just a reader. This thread has now shipped three fields whose
# consumer existed while nothing supplied a value (`next_participant`, `last_full_success_period`,
# and the quarantine error class — Bohr msg-2401 §5 E-6 counts them). A reader-only field reads as
# "healthy" forever, which is the precise shape of the fail-open E-4 reports.
CheckTrue "``first_attempt_period`` has exactly one writer in the sweep script" `
    (([regex]::Matches($sweepText, "\`$notifyHealth\['first_attempt_period'\]\s*=")).Count -eq 1) `
    ([regex]::Matches($sweepText, "\`$notifyHealth\['first_attempt_period'\]\s*=")).Count

# =============================================================================================
# D-1 / DoD 1 — bounded renderer under a large synthetic waiting set stays ≤ budget with correct totals
# =============================================================================================
Write-Host ""
Write-Host "New-DailyDigest -Budget — 200-entry synthetic waiting set stays under the shipped budget with correct total (DoD 1)"

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
    -Budget $script:DigestBudget

CheckTrue "200-entry digest fits within the shipped budget (≤ $($script:DigestBudget) chars)" ($digest.Length -le $script:DigestBudget) $digest.Length
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
    -HumanParked $bigParked -Budget $script:DigestBudget
CheckTrue "50-entry parked digest fits within budget" ($digest2.Length -le $script:DigestBudget) $digest2.Length
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

# =============================================================================================
# PR-GATE REGRESSION BLOCK — 2026-08-30 naysayer review of PR #203 found two spam loops in the
# first revision. Both are pinned here so a future edit that reintroduces them fails loudly.
# =============================================================================================
Write-Host ""
Write-Host "PR-gate regression (2026-08-30): the two spam loops must stay closed"

# --- Spam loop #1: alert-path deterministic-payload retry loop ---------------------------------
# Reproducer: a systemic alert with signature S fires; Send-Notification returns 400. If
# Send-NotificationIfChanged SKIPS recording the signature (as the first PR revision did), the
# next 5-minute tick generates the same alert with the same S, dedup check passes (nothing
# recorded), sends again, 400 again — forever. THE fix is: record on every outcome. The dedup map
# is keyed by $Key (thread id), NOT by $Signature: a DIFFERENT signature for the same key
# already bypasses the check because `$State[$Key] -eq $Signature` is false when signatures
# differ, so recording the failed signature never blocks a new-signature alert.
$state = @{}
$key = 'p/T-spam-loop'
$sig1 = 'human:msg-A'
$script:sentMessages = @()
# Stub Send-Notification to return the 400/payload class the first PR revision was skipping on.
function global:Send-Notification { param([string]$Message) $script:sentMessages += $Message; return @{ status='failed'; class='deterministic-payload'; http_status=400; error='bad' } }
Send-NotificationIfChanged -State $state -Key $key -Signature $sig1 -Message 'alert body #1'
$countAfterFirstFailure = $script:sentMessages.Count
Send-NotificationIfChanged -State $state -Key $key -Signature $sig1 -Message 'alert body #2 (identical sig)'
$countAfterSecondCall = $script:sentMessages.Count
Check "spam-loop #1: SAME signature after 400 -> suppressed on 2nd call (no 2nd POST)" `
    $countAfterFirstFailure $countAfterSecondCall
CheckTrue "spam-loop #1: state was recorded so Test-NotificationSuppressed returns true" `
    (Test-NotificationSuppressed -State $state -Key $key -Signature $sig1)
# DIFFERENT signature for the SAME key still tries — this is what recording the failed signature
# preserves (contra the first PR revision's rationale that skipping was needed to unblock new sigs).
$sig2 = 'human:msg-B'
Send-NotificationIfChanged -State $state -Key $key -Signature $sig2 -Message 'alert body #3 (new sig)'
Check "spam-loop #1: DIFFERENT signature for same key still POSTs (new-sig delivery preserved)" `
    ($countAfterSecondCall + 1) $script:sentMessages.Count
Remove-Item Function:\Send-Notification -ErrorAction SilentlyContinue

# --- Spam loop #2: digest-path deterministic-permanent 404 loop --------------------------------
# Reproducer: the webhook was deleted from Discord side. Send-Notification returns
# {status=failed, class=deterministic-permanent, http_status=404}. If Test-DigestDelivered
# inspects ONLY $status (as the first PR revision did), it returns false → last_sent_period is
# NOT advanced → next 5-min tick sees "digest due", POSTs again, 404 again — forever, for the
# rest of the day. THE fix: Test-DigestDelivered inspects $class too, and returns true for any
# non-retryable failure class (permanent OR payload-after-degraded-also-failed).
Check "spam-loop #2: failed(deterministic-permanent) advances cadence (no 404 spam)" $true `
    (Test-DigestDelivered -Result @{ status='failed'; class='deterministic-permanent'; http_status=404 })
Check "spam-loop #2: failed(deterministic-payload, both attempts failed) advances cadence" $true `
    (Test-DigestDelivered -Result @{ status='failed'; class='deterministic-payload'; http_status=400 })
# And the necessary complement: transient IS held (retry next tick), or the cadence gate loses
# its whole reason to exist.
Check "spam-loop #2 complement: failed(transient) HOLDS cadence (retry next tick)" $false `
    (Test-DigestDelivered -Result @{ status='failed'; class='transient'; http_status=503 })

# =============================================================================================
# PR-GATE REGRESSION BLOCK (round 3) — 2026-08-30 review of PR #203 found two more issues.
# =============================================================================================
Write-Host ""
Write-Host "PR-gate regression (2026-08-30, round 3): degraded-fallback class propagation"

# --- Round 3, item 1: degraded-fallback class propagation --------------------------------------
# Reproducer: the FULL digest 400s (deterministic-payload); the degraded fallback also fails but
# TRANSIENTLY (503 / network flake). The earlier revision kept `$result` pointed at the
# ORIGINAL deterministic-payload failure — which Test-DigestDelivered treats as non-retryable —
# silently advancing cadence and abandoning what was actually a retryable delivery. THE fix:
# Resolve-DigestSendResult replaces the caller's $result with the degraded's ACTUAL result on
# any non-success, so its class drives the cadence decision.
$fullPayloadFail = @{ status='failed'; class='deterministic-payload'; http_status=400; error='content too long' }
$degradedTransient = @{ status='failed'; class='transient'; http_status=503; error='service unavailable' }
$degradedSent = @{ status='sent'; class='ok'; http_status=200; error=$null }
$degradedPermanent = @{ status='failed'; class='deterministic-permanent'; http_status=404; error='webhook gone' }
$degradedPayload = @{ status='failed'; class='deterministic-payload'; http_status=400; error='even degraded 400d' }

# Case A: no degraded attempt (fallback not triggered) → pass-through.
$rA = Resolve-DigestSendResult -FullResult $fullPayloadFail -DegradedResult $null
Check "resolver: no degraded -> pass-through class" 'deterministic-payload' $rA['class']

# Case B: degraded landed → rename status to 'degraded', class 'ok'; Test-DigestFullSuccess stays false.
$rB = Resolve-DigestSendResult -FullResult $fullPayloadFail -DegradedResult $degradedSent
Check "resolver: degraded sent -> status renamed to 'degraded'" 'degraded' $rB['status']
Check "resolver: degraded sent -> class ok" 'ok' $rB['class']
Check "resolver: degraded sent -> delivered? YES" $true (Test-DigestDelivered -Result $rB)
Check "resolver: degraded sent -> full success? NO (queue not shown)" $false (Test-DigestFullSuccess -Result $rB)

# Case C (THE fix): degraded failed transiently → preserve TRANSIENT class → cadence HOLDS.
$rC = Resolve-DigestSendResult -FullResult $fullPayloadFail -DegradedResult $degradedTransient
Check "resolver: degraded transient -> class preserved (transient, NOT the original payload)" `
    'transient' $rC['class']
Check "resolver: degraded transient -> delivered? NO (cadence must HOLD for retry — the fix)" `
    $false (Test-DigestDelivered -Result $rC)

# Case D: degraded failed permanently → preserve permanent → cadence advances (webhook dead).
$rD = Resolve-DigestSendResult -FullResult $fullPayloadFail -DegradedResult $degradedPermanent
Check "resolver: degraded permanent -> class preserved (deterministic-permanent)" `
    'deterministic-permanent' $rD['class']
Check "resolver: degraded permanent -> delivered? YES (webhook dead, no spam)" `
    $true (Test-DigestDelivered -Result $rD)

# Case E: degraded also 400d (theoretically impossible for a fixed short message; defensive).
$rE = Resolve-DigestSendResult -FullResult $fullPayloadFail -DegradedResult $degradedPayload
Check "resolver: degraded also 400 -> class preserved (deterministic-payload)" `
    'deterministic-payload' $rE['class']
Check "resolver: degraded also 400 -> delivered? YES (defensive spam-avoidance)" `
    $true (Test-DigestDelivered -Result $rE)

# --- Round 3, item 2: $ParkedPollErrors must respect the budget --------------------------------
# Reproducer: a candidate-server outage generates a spike of fetch errors (20+). The earlier
# revision appended each row unconditionally, blowing past $Budget and reintroducing the exact
# Discord 400 this PR was fixing. THE fix: route the row list through _AddSectionEntries so the
# same budget discipline governs it.
Write-Host ""
Write-Host "PR-gate regression (2026-08-30, round 3): ParkedPollErrors respects budget"

$errorSpike = @()
for ($i = 0; $i -lt 100; $i++) {
    $errorSpike += [PSCustomObject]@{
        thread_id = ("spirrow-voxelworld/T-parked-{0:D3}-fetch-error-with-long-detail" -f $i)
        reason = 'HTTP 503 upstream server temporarily unavailable — retrying next tick'
    }
}
$digest_errspike = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $now -LiveKeys @() `
    -HumanParked @() -ParkedPollErrors $errorSpike `
    -Budget $script:DigestBudget
CheckTrue "100-entry ParkedPollErrors spike still fits within budget" ($digest_errspike.Length -le $script:DigestBudget) $digest_errspike.Length
CheckTrue "count line still names the true total (100)" ($digest_errspike -match '取得失敗: 100 件') $digest_errspike.Substring(0, [Math]::Min(400, $digest_errspike.Length))
CheckTrue "budget-forced truncation surfaces via `+N 件（省略）` under the fetch-error section" `
    ($digest_errspike -match '\+\d+ 件（省略）') $digest_errspike

if ($script:failures -gt 0) {
    Write-Host ""
    Write-Host "sweep digest: $script:failures check(s) FAILED"
    exit 1
}
Write-Host ""
Write-Host "sweep digest: all checks passed"
