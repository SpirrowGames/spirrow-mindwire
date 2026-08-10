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

$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'New-QuarantineRecord', 'Get-DerivedQuarantineState', 'Get-FingerprintHint',
                  'Format-DurationDigest', 'Get-StarvedKeys', 'New-DailyDigest') {
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

Write-Host ""
if ($script:failures -gt 0) {
    Write-Host "sweep quarantine: $($script:failures) check(s) FAILED"
    exit 1
}
Write-Host "sweep quarantine: all checks passed"
exit 0
