# Regression guard for the sweep's retry-once-before-quarantine path (T-retry-once-before-quarantine;
# decided msg-5424, design Bohr msg-5434 + msg-5441 + msg-5449, approved Einstein msg-5452).
#
# The state transitions and the digest rendering are lifted out of the sweep script's AST and run
# directly (same technique as Test-SweepQuarantine: dot-sourcing would run the sweep). The parts
# that live inline in the sweep body — which branch calls what, and what does NOT — are pinned
# structurally on the AST, because those are exactly the edits a later refactor could make
# silently: an exit-2 branch that starts consuming the retry, a K-budget hit that promotes pending
# threads to quarantine, a --retry-of passed on every launch.

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

$script:QuarantineEscalatedAfter = [TimeSpan]::FromHours(24)
$script:QuarantineStaleAfter     = [TimeSpan]::FromDays(7)
$script:StarvedThreshold         = [TimeSpan]::FromHours(24)
$script:RetryEventsCap           = 500

function Write-Log { param([string]$Message) }

$leaseLib = Join-Path $repoRoot 'deploy/lib/Lease.ps1'
if (-not (Test-Path -LiteralPath $leaseLib)) { throw "Lease lib not found: $leaseLib" }
. $leaseLib

$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'New-QuarantineRecord', 'ConvertTo-RetryPendingState', 'New-RetryPendingRecord',
                  'ConvertTo-RetryIso', 'Get-RetryOfArgument', 'Add-RetryEvent',
                  'Register-CandidateFailure', 'Register-CandidateSuccess', 'Get-RetryFirstAttempt',
                  'Get-DerivedQuarantineState', 'Get-FingerprintHint', 'Get-QuarantineReproHint',
                  'Format-DurationDigest', 'New-DailyDigest', 'Save-JsonState', 'ConvertTo-UtcInstant') {
    $fn = $functions | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}

# The cap the sweep declares must be the one this file exercises.
$capAssign = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $n.Left.Extent.Text -eq '$RetryEventsCap' }, $true) | Select-Object -First 1
if (-not $capAssign) { throw '$RetryEventsCap assignment not found in sweep script' }

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual) }
}

$t0 = [datetime]::Parse('2026-10-01T10:00:00Z').ToUniversalTime()
$t1 = $t0.AddMinutes(10)
$key = 'spirrow-mindwire/T-foo'

function New-Pending {
    param([string]$At, [string]$Head = 'msg-1', [string]$ErrorCode = 'adapter.turn_timeout', [int]$Exit = 1)
    return New-RetryPendingRecord -FirstFailureAt $At -ExitCode $Exit -StopReason 'adapter_error' `
        -ErrorCode $ErrorCode -FailureClass 'unknown' -FailureHead $Head -FailureControl 'running' `
        -SessionLogPath 'C:\logs\conductor.log'
}

# ------------------------------------------------------------------------------------------------
Write-Host "Register-CandidateFailure — the first failure is NOT quarantined"
$state = ConvertTo-RetryPendingState -Raw @{}
$r = Register-CandidateFailure -RetryState $state -Key $key -Record (New-Pending -At $t0.ToString('o')) -Now $t0
Check "first failure -> retry-scheduled" 'retry-scheduled' $r.action
Check "first failure leaves a pending record" $true $state.pending.ContainsKey($key)
Check "first failure records a retry-scheduled event" 'retry-scheduled' $state.events[-1].kind

Write-Host "Register-CandidateFailure — the failed retry is quarantined, first failure carried"
$r2 = Register-CandidateFailure -RetryState $state -Key $key -Record (New-Pending -At $t1.ToString('o') -Head 'msg-2') -Now $t1
Check "second failure -> quarantine" 'quarantine' $r2.action
Check "pending removed once quarantined" $false $state.pending.ContainsKey($key)
Check "the first failure is handed back" $t0.ToString('o') (ConvertTo-RetryIso $r2.first.first_failure_at)
Check "event retry-failed-quarantined" 'retry-failed-quarantined' $state.events[-1].kind
# Head moved between the two failures (msg-1 -> msg-2): still quarantined — the count resets
# only on exit 0 (D-3), or a thread that posts and then dies in SDK teardown would never park.
Check "head change does not reset the count" 'quarantine' $r2.action

$rec = New-QuarantineRecord -FirstFailureAt (ConvertTo-RetryIso $r2.first.first_failure_at) -ExitCode 1 `
    -StopReason 'adapter_error' -FailureHead 'msg-2' -FailureControl 'running' `
    -SessionLogPath 'C:\logs\conductor.log' -SessionLogTail @('x') -FailureClass 'unknown' `
    -LastFailureAt $t1.ToString('o') -ConsecutiveFailures 2 -FirstAttempt (Get-RetryFirstAttempt -Pending $r2.first)
Check "quarantine consecutive_failures = 2" 2 $rec.consecutive_failures
Check "quarantine first_failure_at = the first failure (24h escalation counts from it)" $t0.ToString('o') $rec.first_failure_at
Check "quarantine last_failure_at = the retry" $t1.ToString('o') $rec.last_failure_at
Check "quarantine failure_fingerprint is the latest failure's" 'msg-2' $rec.failure_fingerprint.head
Check "first_attempt keeps the first fingerprint" 'msg-1' $rec.first_attempt.failure_fingerprint.head
Check "first_attempt keeps the first error_code" 'adapter.turn_timeout' $rec.first_attempt.error_code

$legacy = New-QuarantineRecord -FirstFailureAt $t0.ToString('o') -ExitCode 1 -StopReason 'x' `
    -FailureHead 'h' -FailureControl 'c' -SessionLogPath 'p' -SessionLogTail @()
Check "old call shape: consecutive_failures still 1" 1 $legacy.consecutive_failures
Check "old call shape: no first_attempt" $false $legacy.ContainsKey('first_attempt')
Check "old call shape: last = first" $legacy.first_failure_at $legacy.last_failure_at

Write-Host "Register-CandidateSuccess — exit 0 clears pending and records the recovery"
$state = ConvertTo-RetryPendingState -Raw @{}
[void](Register-CandidateFailure -RetryState $state -Key $key -Record (New-Pending -At $t0.ToString('o')) -Now $t0)
Check "recovery reported" $true (Register-CandidateSuccess -RetryState $state -Key $key -Now $t1)
Check "pending cleared on recovery" $false $state.pending.ContainsKey($key)
Check "event retry-recovered" 'retry-recovered' $state.events[-1].kind
Check "success on a thread with nothing pending is a no-op" $false (Register-CandidateSuccess -RetryState $state -Key 'p/other' -Now $t1)
$r3 = Register-CandidateFailure -RetryState $state -Key $key -Record (New-Pending -At $t1.ToString('o')) -Now $t1
Check "after a recovery, the next failure is a FIRST failure again" 'retry-scheduled' $r3.action

Write-Host "State file round-trip — records read back as PSCustomObject still drive the transitions"
# Fresh temp fixture (never this checkout's own state dir), same convention as Test-Lease.
$file = Join-Path ([System.IO.Path]::GetTempPath()) ("mindwire-retry-pending-{0}.json" -f [guid]::NewGuid().ToString('N'))
$state = ConvertTo-RetryPendingState -Raw @{}
[void](Register-CandidateFailure -RetryState $state -Key $key -Record (New-Pending -At $t0.ToString('o')) -Now $t0)
Save-JsonState -Path $file -State $state
$back = ConvertTo-RetryPendingState -Raw (Get-JsonState -Path $file)
Remove-Variable state
Check "pending survives the round-trip" $true $back.pending.ContainsKey($key)
Check "events survive the round-trip" 1 @($back.events).Count
Check "--retry-of after round-trip (DateTime-parsed time renders as the same instant)" $true `
    ((Get-RetryOfArgument -Record $back.pending[$key]) -like 'adapter.turn_timeout@2026-10-01T10:00:00*')
$rb = Register-CandidateFailure -RetryState $back -Key $key -Record (New-Pending -At $t1.ToString('o')) -Now $t1
Check "round-tripped pending -> quarantine on the retry failure" 'quarantine' $rb.action
Check "round-tripped first_attempt fingerprint readable" 'msg-1' (Get-RetryFirstAttempt -Pending $rb.first).failure_fingerprint.head

Write-Host "Get-RetryOfArgument — names the failure, falls back to the exit code"
Check "error_code@first_failure_at" "adapter.shutdown_failed@$($t0.ToString('o'))" `
    (Get-RetryOfArgument -Record (New-Pending -At $t0.ToString('o') -ErrorCode 'adapter.shutdown_failed'))
Check "bare exit 1 -> exit-1@..." "exit-1@$($t0.ToString('o'))" `
    (Get-RetryOfArgument -Record (New-Pending -At $t0.ToString('o') -ErrorCode ''))

Write-Host "Add-RetryEvent — events[] is bounded"
$state = ConvertTo-RetryPendingState -Raw @{}
for ($i = 0; $i -lt ($script:RetryEventsCap + 7); $i++) {
    Add-RetryEvent -RetryState $state -At "t$i" -Key "k$i" -Kind 'retry-scheduled'
}
Check "capped at RetryEventsCap" $script:RetryEventsCap @($state.events).Count
Check "newest kept" "k$($script:RetryEventsCap + 6)" $state.events[-1].key

# ------------------------------------------------------------------------------------------------
Write-Host "New-DailyDigest — summary counts and the [retry-pending] section"
$now = [datetime]::Parse('2026-10-02T12:00:00Z').ToUniversalTime()
$digestState = ConvertTo-RetryPendingState -Raw @{}
$digestState.pending['p/T-fresh'] = New-Pending -At $now.AddHours(-2).ToString('o')
$digestState.pending['p/T-old']   = New-Pending -At $now.AddHours(-30).ToString('o') -ErrorCode ''
$digestState.events = @(
    @{ at = 'a'; key = 'p/T-fresh'; kind = 'retry-scheduled' },
    @{ at = 'b'; key = 'p/T-old'; kind = 'retry-scheduled' },
    @{ at = 'c'; key = 'p/T-x'; kind = 'retry-scheduled' },
    @{ at = 'd'; key = 'p/T-x'; kind = 'retry-recovered' },
    @{ at = 'e'; key = 'p/T-y'; kind = 'retry-failed-quarantined' }
)
$d = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} -HeadsByProject @{} -ControlByProject @{} `
    -Now $now -LiveKeys @() -RetryState $digestState -Budget 1950
Check "summary carries 再試行 / 回復 / 再試行後隔離" $true ($d -match '再試行 3 / 回復 1 / 再試行後隔離 1')
Check "[retry-pending] section header with the true count" $true ($d -match '再試行待ち \[retry-pending\]: 2 件')
$lines = $d -split "`n"
$oldIdx = [array]::FindIndex($lines, [Predicate[string]]{ param($l) $l -like '*p/T-old*' })
$freshIdx = [array]::FindIndex($lines, [Predicate[string]]{ param($l) $l -like '*p/T-fresh*' })
Check "24h+ entry is marked" $true ($lines[$oldIdx] -like '  24h+ p/T-old*')
Check "fresh entry is not marked" $false ($lines[$freshIdx] -like '*24h+*')
Check "24h+ entry is listed first" $true ($oldIdx -ge 0 -and $oldIdx -lt $freshIdx)
Check "entry names its error_code" $true ($lines[$freshIdx] -like '*(adapter.turn_timeout)')
Check "entry without error_code names the exit code" $true ($lines[$oldIdx] -like '*(exit=1)')
Check "retry section sits between 隔離中 and 判断待ち" $true `
    ($d.IndexOf('隔離中') -lt $d.IndexOf('再試行待ち') -and $d.IndexOf('再試行待ち') -lt $d.IndexOf('判断待ち'))

$empty = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} -HeadsByProject @{} -ControlByProject @{} `
    -Now $now -LiveKeys @()
Check "no RetryState: summary shows zeros" $true ($empty -match '再試行 0 / 回復 0 / 再試行後隔離 0')
Check "no RetryState: no [retry-pending] section" $false ($empty -match 'retry-pending')

# Budget: a long pending list still renders within the budget and keeps 判断待ち's floor.
$big = ConvertTo-RetryPendingState -Raw @{}
for ($i = 0; $i -lt 80; $i++) { $big.pending["proj/T-retry-pending-long-name-$i"] = New-Pending -At $now.AddHours(-$i).ToString('o') }
$parked = @([PSCustomObject]@{ key = 'proj/T-parked'; project = 'proj'; thread_id = 'T-parked'; head_msg_id = 'msg-9' })
$bd = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} -HeadsByProject @{} -ControlByProject @{} `
    -Now $now -LiveKeys @() -RetryState $big -HumanParked $parked -Budget 1950
Check "budget respected with 80 pending" $true ($bd.Length -le 1950)
Check "pending overflow is counted, not silent" $true ($bd -match '\+\d+ 件（省略）')
Check "判断待ち keeps its floor row behind a long retry list" $true ($bd -match 'proj/T-parked')

# ------------------------------------------------------------------------------------------------
Write-Host "Sweep body — structure the retry path depends on"
$ifs = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.IfStatementAst] }, $true)
function Find-IfByCondition {
    param([string]$Condition)
    foreach ($i in $ifs) {
        foreach ($clause in $i.Clauses) {
            if ($clause.Item1.Extent.Text -eq $Condition) { return $clause.Item2.Extent.Text }
        }
    }
    return $null
}
$exit2 = Find-IfByCondition '$code -eq 2'
Check "exit-2 branch exists" $true ($null -ne $exit2)
Check "exit 2 does not consume or clear the retry" $false ($exit2 -match 'Register-Candidate|retryState')
Check "exit 2 still continues before any retry handling" $true ($exit2 -match '\bcontinue\b')

$kBlock = Find-IfByCondition '$sweepFailures -ge $QuarantineFailureBudget'
Check "K-budget is counted on failures (first failures included)" $true ($null -ne $kBlock)
Check "K hit does not promote pending to quarantine" $false ($kBlock -match 'quarantineState\[|Register-CandidateFailure|pending\.Remove')

$retryArg = Find-IfByCondition '$retryState.pending.ContainsKey($cand.key)'
Check "--retry-of is passed only under the pending check" $true ($retryArg -match "--retry-of")
$sweepText = $ast.Extent.Text
Check "--retry-of appears nowhere else" 1 ([regex]::Matches($sweepText, "'--retry-of'")).Count

$defer = Find-IfByCondition "`$decision -eq 'defer'"
Check "DEFER carries pending over (does not touch retry state)" $false ($defer -match 'retryState|Register-Candidate')
$report = Find-IfByCondition "`$headSkipMode -eq 'report'"
Check "report-mode branch does not touch retry state" $false ($report -match 'retryState|Register-Candidate')
$saves = [regex]::Matches($sweepText, 'Save-JsonState -Path \$retryPendingStatePath')
$guardedSaves = $ifs | Where-Object { $_.Clauses[0].Item1.Extent.Text -like "`$headSkipMode -ne 'report'*" } |
    ForEach-Object { [regex]::Matches($_.Clauses[0].Item2.Extent.Text, 'Save-JsonState -Path \$retryPendingStatePath').Count } |
    Measure-Object -Sum
Check "every retry-pending write is guarded by report mode" $saves.Count ([int]$guardedSaves.Sum)

foreach ($prefix in 'retry-scheduled ', 'retry-recovered ', 'retry-failed→quarantined ') {
    Check "log prefix '$($prefix.Trim())' is emitted" $true ($sweepText -match [regex]::Escape("Write-Log `"$prefix"))
}

$decideExclusion = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.ForEachStatementAst] -and
        $n.Condition.Extent.Text -eq '$projCands' }, $true) | Select-Object -First 1
Check "decide batch found" $true ($null -ne $decideExclusion)
Check "a retry-pending thread is NOT excluded from decide" $false ($decideExclusion.Body.Extent.Text -match 'retryState')

if ($script:failures -gt 0) {
    Write-Host "sweep retry: $script:failures check(s) FAILED"
    exit 1
}
Write-Host "sweep retry: all checks passed"
