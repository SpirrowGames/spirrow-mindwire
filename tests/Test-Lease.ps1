# Regression guard for the exclusive-resource lease + queue (T-exclusive-resource-lease-queue,
# msg-1180 / 1183 / 1185 / 1187 design; msg-1188 Tier-C approval).
#
# What is covered:
#   1. Get-LeaseHolderClassification — the four cases (progress / parked / neutral) it must
#      produce for the current wrapper's decide verdicts + held/quarantined/absent inputs.
#      This is the ONE predicate the whole design's correctness turns on (msg-1187 §4 (ii):
#      the reading of the sweep wrapper's semantics was Bohr + Einstein agreeing on the same
#      thing, and takahito msg-1188 explicitly asked the implementer to pin it in code).
#   2. Update-LeaseFromClassification — 'progress' resets, 'parked' increments,
#      'neutral' does neither.
#   3. Test-LeaseExpiring — the DUAL predicate (msg-1183 D-6'b): idle_evaluations AND
#      wall-clock, both required; pin makes the lease TTL-immune (D-7).
#   4. Add-LeaseWaiter / Remove-LeaseWaiter — idempotent enqueue, no duplication.
#   5. Get-NextLeaseWaiter — FIFO on waiting_since, sweep-order tiebreak (msg-1183 D-3).
#   6. Invoke-LeasePromotion — two-phase revoke (msg-1183 D-6'd: first mark expiring, then
#      next call actually promotes with reclaim_required=true).
#   7. Test-LeaseAvailableFor + Invoke-LeaseAcquire — the candidate-loop gate.
#
# Also does the same style of AST-wiring check as Test-SweepHeadCache.ps1 to confirm the
# lease-waiting disposition and the probe are actually referenced in the wrapper (msg-1181
# F-3 "wrote it, tested it, never wired it" is the failure mode the AST scan targets).

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$leaseLib = Join-Path $repoRoot "deploy/lib/Lease.ps1"
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"

if (-not (Test-Path -LiteralPath $leaseLib))     { throw "Lease lib not found: $leaseLib" }
if (-not (Test-Path -LiteralPath $sweepScript)) { throw "sweep script not found: $sweepScript" }

. $leaseLib

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else {
        $script:failures++
        Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual)
    }
}
function CheckTrue {
    param([string]$Name, [bool]$Actual)
    Check -Name $Name -Expected $true -Actual $Actual
}
function CheckFalse {
    param([string]$Name, [bool]$Actual)
    Check -Name $Name -Expected $false -Actual $Actual
}

function New-Verdict {
    param([string]$Decision)
    return [pscustomobject]@{ decision = $Decision }
}

# --- 1. Get-LeaseHolderClassification -----------------------------------------------------------
Write-Host "Get-LeaseHolderClassification — progress / parked / neutral trichotomy"

# Held pauses the TTL clock (msg-1183 D-6'c).
Check "held holder -> progress (keeps lease alive)" 'progress' `
    (Get-LeaseHolderClassification -HolderKey 'p/T-a' -IsHeld $true -IsQuarantined $false -IsOnSweep $true -Verdict $null)

# Even a LAUNCH verdict is ignored when held — held wins first.
Check "held + LAUNCH verdict -> still progress" 'progress' `
    (Get-LeaseHolderClassification -HolderKey 'p/T-a' -IsHeld $true -IsQuarantined $false -IsOnSweep $true -Verdict (New-Verdict 'launch'))

# Removed from sweep list = definitely won't run.
Check "not on sweep -> parked" 'parked' `
    (Get-LeaseHolderClassification -HolderKey 'p/T-a' -IsHeld $false -IsQuarantined $false -IsOnSweep $false -Verdict $null)

# Quarantined = wrapper-side stopped.
Check "quarantined -> parked" 'parked' `
    (Get-LeaseHolderClassification -HolderKey 'p/T-a' -IsHeld $false -IsQuarantined $true -IsOnSweep $true -Verdict $null)

# The core decide-verdict mapping (msg-1185 §2-c).
Check "SKIP verdict -> parked (stop-token: NEXT: none/human)" 'parked' `
    (Get-LeaseHolderClassification -HolderKey 'p/T-a' -IsHeld $false -IsQuarantined $false -IsOnSweep $true -Verdict (New-Verdict 'skip'))
Check "LAUNCH verdict -> progress (about to run)" 'progress' `
    (Get-LeaseHolderClassification -HolderKey 'p/T-a' -IsHeld $false -IsQuarantined $false -IsOnSweep $true -Verdict (New-Verdict 'launch'))
Check "DEFER verdict -> neutral (backoff, not parking)" 'neutral' `
    (Get-LeaseHolderClassification -HolderKey 'p/T-a' -IsHeld $false -IsQuarantined $false -IsOnSweep $true -Verdict (New-Verdict 'defer'))

# Missing verdict when on sweep and not held/quarantined = wiring bug in caller; be conservative.
Check "no verdict + on sweep + not held/qtn -> neutral (fail-safe)" 'neutral' `
    (Get-LeaseHolderClassification -HolderKey 'p/T-a' -IsHeld $false -IsQuarantined $false -IsOnSweep $true -Verdict $null)

# --- 2. Update-LeaseFromClassification -----------------------------------------------------------
Write-Host "Update-LeaseFromClassification — parked increments, progress resets, neutral no-op"

$now = [DateTime]::UtcNow

$lease = @{ idle_evaluations = 3; last_progress_at = '2020-01-01T00:00:00Z' }
Update-LeaseFromClassification -Lease $lease -Classification 'progress' -Now $now
Check "progress resets idle_evaluations to 0" 0 $lease['idle_evaluations']
Check "progress updates last_progress_at" $true ([bool]($lease['last_progress_at'] -match '^20\d\d'))

$lease = @{ idle_evaluations = 3; last_progress_at = '2020-01-01T00:00:00Z' }
Update-LeaseFromClassification -Lease $lease -Classification 'parked' -Now $now
Check "parked increments idle_evaluations" 4 $lease['idle_evaluations']
Check "parked does NOT update last_progress_at" '2020-01-01T00:00:00Z' $lease['last_progress_at']

$lease = @{ idle_evaluations = 3; last_progress_at = '2020-01-01T00:00:00Z' }
Update-LeaseFromClassification -Lease $lease -Classification 'neutral' -Now $now
Check "neutral does not change idle_evaluations" 3 $lease['idle_evaluations']
Check "neutral does not change last_progress_at" '2020-01-01T00:00:00Z' $lease['last_progress_at']

# --- 3. Test-LeaseExpiring — dual predicate (msg-1183 D-6'b) -------------------------------------
Write-Host "Test-LeaseExpiring — BOTH idle_evaluations AND wall-clock, pin immune"

$farPast = $now.AddHours(-3).ToUniversalTime().ToString("o")
$recent  = $now.AddMinutes(-10).ToUniversalTime().ToString("o")

# Both gates open, not pinned -> expiring.
$lease = @{ holder = 'p/T-a'; idle_evaluations = 6; last_progress_at = $farPast; pinned = $false }
CheckTrue "6 idle + 3h wall + not-pinned -> expiring" `
    (Test-LeaseExpiring -Lease $lease -IdleEvaluationsMin 6 -IdleTtl ([TimeSpan]::FromHours(2)) -Now $now)

# Idle count below threshold -> NOT expiring (Einstein's split-brain guard: not both gates).
$lease = @{ holder = 'p/T-a'; idle_evaluations = 5; last_progress_at = $farPast; pinned = $false }
CheckFalse "5 idle + 3h wall (idle below min) -> NOT expiring" `
    (Test-LeaseExpiring -Lease $lease -IdleEvaluationsMin 6 -IdleTtl ([TimeSpan]::FromHours(2)) -Now $now)

# Wall-clock too recent -> NOT expiring (dual predicate again).
$lease = @{ holder = 'p/T-a'; idle_evaluations = 10; last_progress_at = $recent; pinned = $false }
CheckFalse "10 idle + 10m wall (wall too short) -> NOT expiring" `
    (Test-LeaseExpiring -Lease $lease -IdleEvaluationsMin 6 -IdleTtl ([TimeSpan]::FromHours(2)) -Now $now)

# Pinned -> immune regardless (msg-1183 D-7).
$lease = @{ holder = 'p/T-a'; idle_evaluations = 100; last_progress_at = $farPast; pinned = $true }
CheckFalse "pinned lease is TTL-immune even at high idle + long wall" `
    (Test-LeaseExpiring -Lease $lease -IdleEvaluationsMin 6 -IdleTtl ([TimeSpan]::FromHours(2)) -Now $now)

# Missing last_progress_at -> not expiring (fresh acquire with no probe yet).
$lease = @{ holder = 'p/T-a'; idle_evaluations = 100; pinned = $false }
CheckFalse "no last_progress_at -> not expiring (freshly acquired)" `
    (Test-LeaseExpiring -Lease $lease -IdleEvaluationsMin 6 -IdleTtl ([TimeSpan]::FromHours(2)) -Now $now)

# --- 4. Add/Remove waiter — idempotent enqueue ---------------------------------------------------
Write-Host "Add/Remove-LeaseWaiter — idempotent enqueue, no duplication"

$lease = @{ queue = @() }
Add-LeaseWaiter -Lease $lease -WaiterKey 'p/T-w1' -Now $now
Add-LeaseWaiter -Lease $lease -WaiterKey 'p/T-w2' -Now $now.AddSeconds(1)
Add-LeaseWaiter -Lease $lease -WaiterKey 'p/T-w1' -Now $now.AddSeconds(2)   # duplicate — must NOT add again
Check "duplicate enqueue is idempotent" 2 $lease['queue'].Count

# Wrap in @() — a Where-Object that returns exactly one item collapses to the item itself, and
# indexing into a lone hashtable with [0] yields $null.
$firstWaitingSince = (@($lease['queue'] | Where-Object { $_.key -eq 'p/T-w1' })[0]).waiting_since
Check "first enqueue's waiting_since is NOT refreshed by a re-enqueue" $now.ToUniversalTime().ToString("o") $firstWaitingSince

Remove-LeaseWaiter -Lease $lease -WaiterKey 'p/T-w1'
Check "remove drops the entry" 1 $lease['queue'].Count
Check "remaining entry is w2" 'p/T-w2' $lease['queue'][0].key
Remove-LeaseWaiter -Lease $lease -WaiterKey 'p/T-not-there'   # no-op on absent
Check "remove of absent key is a no-op" 1 $lease['queue'].Count

# --- 5. Get-NextLeaseWaiter — FIFO + sweep tiebreak ---------------------------------------------
Write-Host "Get-NextLeaseWaiter — FIFO on waiting_since, sweep order as tiebreak"

$lease = @{
    queue = @(
        @{ key = 'p/T-b'; waiting_since = $now.AddMinutes(-10).ToUniversalTime().ToString("o") },
        @{ key = 'p/T-a'; waiting_since = $now.AddMinutes(-20).ToUniversalTime().ToString("o") }   # earlier -> should win
    )
}
Check "FIFO: earlier waiting_since wins" 'p/T-a' `
    (Get-NextLeaseWaiter -Lease $lease -SweepOrderKeys @('p/T-b', 'p/T-a'))

# Ties on waiting_since: sweep-list index breaks the tie (msg-1183 D-3).
$sameSince = $now.AddMinutes(-10).ToUniversalTime().ToString("o")
$lease = @{
    queue = @(
        @{ key = 'p/T-b'; waiting_since = $sameSince },
        @{ key = 'p/T-a'; waiting_since = $sameSince }
    )
}
Check "tie -> sweep-order first (T-b before T-a in sweep)" 'p/T-b' `
    (Get-NextLeaseWaiter -Lease $lease -SweepOrderKeys @('p/T-b', 'p/T-a'))
Check "tie -> sweep-order first (T-a before T-b in sweep)" 'p/T-a' `
    (Get-NextLeaseWaiter -Lease $lease -SweepOrderKeys @('p/T-a', 'p/T-b'))

Check "empty queue -> null" $null (Get-NextLeaseWaiter -Lease @{ queue = @() } -SweepOrderKeys @())

# --- 6. Invoke-LeasePromotion — two-phase revoke (msg-1183 D-6'd) --------------------------------
Write-Host "Invoke-LeasePromotion — two-phase (mark expiring, then promote)"

$lease = @{
    holder = 'p/T-old'
    generation = 2
    pinned = $false
    expiring = $false
    queue = @( @{ key = 'p/T-new'; waiting_since = $now.AddMinutes(-1).ToUniversalTime().ToString("o") } )
}
$r1 = Invoke-LeasePromotion -Lease $lease -Now $now -SweepOrderKeys @('p/T-new') -Reason 'idle'
Check "phase 1: action is marked-expiring" 'marked-expiring' $r1.action
CheckTrue "phase 1: expiring flag is set" ([bool]$lease['expiring'])
Check "phase 1: holder still the old one" 'p/T-old' $lease['holder']
Check "phase 1: queue untouched" 1 $lease['queue'].Count

$r2 = Invoke-LeasePromotion -Lease $lease -Now $now -SweepOrderKeys @('p/T-new') -Reason 'idle'
Check "phase 2: action is promoted" 'promoted' $r2.action
Check "phase 2: new holder is the waiter" 'p/T-new' $lease['holder']
Check "phase 2: reclaimed_from records the old holder" 'p/T-old' $lease['reclaimed_from']
CheckTrue "phase 2: reclaim_required is true (msg-1183 D-6'e)" ([bool]$lease['reclaim_required'])
Check "phase 2: generation bumped" 3 $lease['generation']
CheckFalse "phase 2: expiring flag cleared" ([bool]$lease['expiring'])
Check "phase 2: queue empty (promoted waiter dequeued)" 0 $lease['queue'].Count

# Promotion with no waiter -> release (holder gone, generation still bumps, reclaim_required set).
$lease = @{
    holder = 'p/T-old'
    generation = 5
    pinned = $false
    expiring = $false
    queue = @()
}
Invoke-LeasePromotion -Lease $lease -Now $now -SweepOrderKeys @() -Reason 'idle' | Out-Null
$r2 = Invoke-LeasePromotion -Lease $lease -Now $now -SweepOrderKeys @() -Reason 'idle'
Check "released: action is released" 'released' $r2.action
Check "released: holder is gone" $false ($lease.ContainsKey('holder'))
Check "released: generation bumped" 6 $lease['generation']

# --- 7. Test-LeaseAvailableFor + Invoke-LeaseAcquire ---------------------------------------------
Write-Host "Test-LeaseAvailableFor + Invoke-LeaseAcquire — the candidate-loop gate"

$state = @{}
$check = Test-LeaseAvailableFor -LeasesState $state -CandidateKey 'p/T-a' -Requires @('editor')
Check "empty state: available" 'available' $check.status

# Acquire on empty state creates the record.
Invoke-LeaseAcquire -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-a' -Now $now
Check "acquire creates the record" 'p/T-a' $state['editor']['holder']
Check "acquire generation is 1" 1 $state['editor']['generation']

# Second candidate needs the same lease -> waiting.
$check = Test-LeaseAvailableFor -LeasesState $state -CandidateKey 'p/T-b' -Requires @('editor')
Check "second candidate: waiting" 'waiting' $check.status
Check "waiting: waitOn names the resource" 'editor' $check.waitOn[0]

# Same candidate re-checking its own lease -> available (self-hold).
$check = Test-LeaseAvailableFor -LeasesState $state -CandidateKey 'p/T-a' -Requires @('editor')
Check "self-hold: available" 'available' $check.status

# Invoke-LeaseAcquire on a foreign lease throws (no-steal invariant).
$threw = $false
try { Invoke-LeaseAcquire -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-b' -Now $now }
catch { $threw = $true }
CheckTrue "acquire on foreign lease refuses (throws)" $threw

# Register-LeaseWaiter creates the resource record if absent (rare — never-leased resource).
$state = @{}
Register-LeaseWaiter -LeasesState $state -Resource 'runner' -WaiterKey 'p/T-x' -Now $now
Check "Register-LeaseWaiter creates the resource record" 1 $state['runner']['queue'].Count
Check "Register-LeaseWaiter leaves holder empty" $null $state['runner']['holder']

# --- 8. Wrapper AST wiring — the lease block is actually called (msg-1181 F-3 defense) ----------
Write-Host "Wrapper AST wiring — the lease block is actually reachable from the sweep body"

$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) {
    $parseErrors | ForEach-Object { Write-Host "PARSE ERROR line $($_.Extent.StartLineNumber): $($_.Message)" }
    throw "sweep script does not parse"
}

$commandNames = @($ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) |
    ForEach-Object { $_.CommandElements[0].Extent.Text })

# The five lease-flavoured helpers the wrapper MUST call — a rename in Lease.ps1 without updating
# the wrapper would silently reintroduce the "wrote it, never wired it" failure. Command lookup
# rather than string-grep because a comment mentioning the name would fool a plain grep.
$mustCall = @(
    'Get-LeaseHolderClassification',
    'Update-LeaseFromClassification',
    'Test-LeaseExpiring',
    'Test-LeaseAvailableFor',
    'ConvertTo-LeasesStateHashtable'
)
foreach ($fn in $mustCall) {
    $found = $commandNames -contains $fn
    if ($found) { Write-Host "  PASS  wrapper calls $fn" }
    else { $script:failures++; Write-Host "  FAIL  wrapper never calls $fn — Lease.ps1 rename likely orphaned" }
}

# The lease-waiting disposition literal must appear somewhere — that is the visible symptom of
# the whole mechanism from the operator's side.
$stringConsts = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true)
$hasLeaseWaiting = @($stringConsts | Where-Object { $_.Value -eq 'lease-waiting' }).Count -gt 0
CheckTrue "wrapper has the 'lease-waiting' disposition string" $hasLeaseWaiting

# The digest is passed the LeasesState — required for the section to render.
$digestCallHasLeases = $false
$commandAsts = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)
foreach ($c in $commandAsts) {
    if ($c.CommandElements[0].Extent.Text -ne 'New-DailyDigest') { continue }
    foreach ($el in $c.CommandElements) {
        if ($el -is [System.Management.Automation.Language.CommandParameterAst] -and $el.ParameterName -eq 'LeasesState') {
            $digestCallHasLeases = $true; break
        }
    }
}
CheckTrue "New-DailyDigest is called with -LeasesState" $digestCallHasLeases

Write-Host ""
if ($script:failures -gt 0) { Write-Host "lease gate: $($script:failures) check(s) FAILED"; exit 1 }
Write-Host "lease gate: all checks passed"
exit 0
