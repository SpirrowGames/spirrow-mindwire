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

# msg-1757 blocker #1 regression: progress MUST clear a stale expiring flag left over from a
# prior Phase-1 mark. If it does not, a later idle sequence jumps straight to Phase 2 promotion
# and bypasses the 1-tick pre-emption window guaranteed by D-6'd (msg-1183).
$lease = @{
    idle_evaluations = 6
    last_progress_at = '2020-01-01T00:00:00Z'
    expiring         = $true
    revoked_at       = '2026-08-25T00:00:00Z'
    revoked_reason   = 'idle'
}
Update-LeaseFromClassification -Lease $lease -Classification 'progress' -Now $now
CheckFalse "progress clears stale expiring flag (msg-1757 blocker #1)" ([bool]$lease['expiring'])
Check "progress clears stale revoked_at" $null $lease['revoked_at']
Check "progress clears stale revoked_reason" $null $lease['revoked_reason']

# Composite scenario: Phase 1 marks expiring; probe next tick sees progress; a THIRD tick with
# expiring conditions again must land in Phase 1, not Phase 2 (promotion).
$farPast = $now.AddHours(-3).ToUniversalTime().ToString("o")
$lease = @{
    holder           = 'p/T-old'
    generation       = 2
    pinned           = $false
    expiring         = $true
    revoked_at       = '2026-08-25T00:00:00Z'
    revoked_reason   = 'idle'
    idle_evaluations = 6
    last_progress_at = $farPast
    queue            = @( @{ key = 'p/T-new'; waiting_since = $now.AddMinutes(-1).ToUniversalTime().ToString("o") } )
}
# Holder resumes progress: probe classifies as 'progress'.
Update-LeaseFromClassification -Lease $lease -Classification 'progress' -Now $now
CheckFalse "post-progress: expiring flag cleared" ([bool]$lease['expiring'])
# Simulate the holder going idle again for two ticks.
$lease['last_progress_at'] = $farPast
$lease['idle_evaluations'] = 6
$phase1Again = Invoke-LeasePromotion -Lease $lease -Now $now -SweepOrderKeys @('p/T-new') -Reason 'idle'
Check "post-progress -> re-idle: next promotion attempt is Phase 1 (marked-expiring), NOT Phase 2" `
    'marked-expiring' $phase1Again.action
Check "post-progress -> re-idle: holder still the old one after Phase 1" 'p/T-old' $lease['holder']

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

# msg-1875 blocker: Remove-IneligibleLeaseWaiters scrubs waiters that became un-launchable
# between enqueue and this tick. If left in queue, they'd get promoted, hold the lease idle
# for the full LeaseIdleTtl (2h) — the "silent parked" failure the mechanism was built to end.
Write-Host "Remove-IneligibleLeaseWaiters — scrub dead waiters before promotion (msg-1875 blocker)"

$lease = @{
    queue = @(
        @{ key = 'p/T-a'; waiting_since = '2026-08-26T05:00:00Z' },
        @{ key = 'p/T-b'; waiting_since = '2026-08-26T05:01:00Z' },
        @{ key = 'p/T-c'; waiting_since = '2026-08-26T05:02:00Z' },
        @{ key = 'p/T-d'; waiting_since = '2026-08-26T05:03:00Z' }
    )
}
# Eligible set: only p/T-a and p/T-c. p/T-b was quarantined; p/T-d dropped requires.
$dropped = Remove-IneligibleLeaseWaiters -Lease $lease -EligibleKeys @('p/T-a', 'p/T-c')
Check "scrub: 2 waiters dropped" 2 $dropped.Count
Check "scrub: dropped includes p/T-b" $true ($dropped -contains 'p/T-b')
Check "scrub: dropped includes p/T-d" $true ($dropped -contains 'p/T-d')
Check "scrub: eligible waiters remain (count)" 2 $lease['queue'].Count
Check "scrub: FIFO order preserved (a still before c)" 'p/T-a' $lease['queue'][0].key
Check "scrub: c is second" 'p/T-c' $lease['queue'][1].key

# Idempotent when everyone is eligible.
$lease = @{ queue = @( @{ key = 'p/T-a'; waiting_since = 'x' }, @{ key = 'p/T-b'; waiting_since = 'y' } ) }
$dropped = Remove-IneligibleLeaseWaiters -Lease $lease -EligibleKeys @('p/T-a', 'p/T-b')
Check "scrub (all eligible): dropped is empty" 0 $dropped.Count
Check "scrub (all eligible): queue untouched" 2 $lease['queue'].Count

# All-ineligible: queue becomes empty.
$lease = @{ queue = @( @{ key = 'p/T-a'; waiting_since = 'x' } ) }
$dropped = Remove-IneligibleLeaseWaiters -Lease $lease -EligibleKeys @()
Check "scrub (none eligible): all dropped" 1 $dropped.Count
Check "scrub (none eligible): queue is empty" 0 $lease['queue'].Count

# Empty queue: no-op, no output.
$lease = @{ queue = @() }
$dropped = Remove-IneligibleLeaseWaiters -Lease $lease -EligibleKeys @('p/T-x')
Check "scrub (empty queue): no dropped" 0 $dropped.Count

# Missing queue key: no-op.
$lease = @{}
$dropped = Remove-IneligibleLeaseWaiters -Lease $lease -EligibleKeys @('p/T-x')
Check "scrub (no queue key): no dropped" 0 $dropped.Count

# After scrub, Get-NextLeaseWaiter picks from remaining eligibles only.
$lease = @{
    queue = @(
        @{ key = 'p/T-dead'; waiting_since = '2026-08-26T04:00:00Z' },   # earliest but ineligible
        @{ key = 'p/T-live'; waiting_since = '2026-08-26T05:00:00Z' }
    )
}
Remove-IneligibleLeaseWaiters -Lease $lease -EligibleKeys @('p/T-live') | Out-Null
$nextAfterScrub = Get-NextLeaseWaiter -Lease $lease -SweepOrderKeys @('p/T-live')
Check "scrub -> next waiter is the surviving live one, NOT the dropped-earliest" 'p/T-live' $nextAfterScrub

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

# msg-1852 blocker: Invoke-LeaseGrantFromEmpty drains a queued waiter into an empty lease
# (the state Grant-Lease.ps1 -Clear leaves when waiters were already registered). Without this,
# any candidate arriving after the clear snipes the free lease past the FIFO queue.
Write-Host "Invoke-LeaseGrantFromEmpty — FIFO drain on empty-holder + non-empty queue (msg-1852 blocker)"

# --- happy path: empty lease with waiters + reclaim_required=true (operator -Clear left this) ---
$lease = @{
    holder            = $null
    generation        = 7
    pinned            = $false
    expiring          = $false
    reclaim_required  = $true
    reclaimed_from    = 'p/T-cleared'
    revoked_at        = '2026-08-25T00:00:00Z'
    revoked_reason    = 'operator-clear: PIE crashed'
    queue = @(
        @{ key = 'p/T-w1'; waiting_since = $now.AddMinutes(-30).ToUniversalTime().ToString("o") },
        @{ key = 'p/T-w2'; waiting_since = $now.AddMinutes(-10).ToUniversalTime().ToString("o") }
    )
}
$grant = Invoke-LeaseGrantFromEmpty -Lease $lease -Now $now -SweepOrderKeys @('p/T-w1', 'p/T-w2')
Check "grant-from-empty: action = granted-from-empty" 'granted-from-empty' $grant.action
Check "grant-from-empty: FIFO head (earliest waiting_since) wins" 'p/T-w1' $grant.new_holder
Check "grant-from-empty: lease.holder set to promoted waiter" 'p/T-w1' $lease['holder']
Check "grant-from-empty: generation bumped" 8 $lease['generation']
CheckTrue "grant-from-empty: reclaim_required preserved on new holder" ([bool]$lease['reclaim_required'])
Check "grant-from-empty: reclaimed_from preserved" 'p/T-cleared' $lease['reclaimed_from']
Check "grant-from-empty: revoked_at cleared (that annotated the INTO-empty transition)" $null $lease['revoked_at']
Check "grant-from-empty: revoked_reason cleared" $null $lease['revoked_reason']
Check "grant-from-empty: dequeued only the promoted waiter" 1 $lease['queue'].Count
Check "grant-from-empty: remaining waiter is w2" 'p/T-w2' $lease['queue'][0].key
Check "grant-from-empty: caller sees reclaim_required=true for notification decision" $true $grant.reclaim_required

# --- edge: empty lease with empty queue -> noop, nothing to grant ---
$lease = @{ holder = $null; generation = 5; queue = @() }
$grant = Invoke-LeaseGrantFromEmpty -Lease $lease -Now $now -SweepOrderKeys @()
Check "grant-from-empty (empty queue): action = noop" 'noop' $grant.action
Check "grant-from-empty (empty queue): holder stays null" $null $lease['holder']
Check "grant-from-empty (empty queue): generation NOT bumped" 5 $lease['generation']

# --- edge: reclaim_required NOT set on the empty record -> new holder does NOT inherit it ---
$lease = @{
    holder = $null; generation = 3; pinned = $false; expiring = $false
    reclaim_required = $false
    queue = @( @{ key = 'p/T-w1'; waiting_since = $now.AddMinutes(-5).ToUniversalTime().ToString("o") } )
}
$grant = Invoke-LeaseGrantFromEmpty -Lease $lease -Now $now -SweepOrderKeys @('p/T-w1')
Check "grant-from-empty (clean release): action = granted-from-empty" 'granted-from-empty' $grant.action
CheckFalse "grant-from-empty (clean release): reclaim_required stays false" ([bool]$lease['reclaim_required'])
Check "grant-from-empty (clean release): caller signal reclaim_required=false" $false $grant.reclaim_required

# --- no-steal invariant: called on a lease with an actual holder -> throw ---
$lease = @{ holder = 'p/T-live'; queue = @( @{ key = 'p/T-w1'; waiting_since = $now.ToUniversalTime().ToString("o") } ) }
$threw = $false
try { Invoke-LeaseGrantFromEmpty -Lease $lease -Now $now -SweepOrderKeys @() }
catch { $threw = $true }
CheckTrue "grant-from-empty on non-empty holder: throws (no accidental steal)" $threw

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
    'ConvertTo-LeasesStateHashtable',
    # msg-1852 blocker fix: probe drains queued waiters into an empty lease before the
    # candidate loop can snipe past FIFO.
    'Invoke-LeaseGrantFromEmpty',
    # msg-1875 blocker fix: probe scrubs the queue of quarantined / off-sweep / dropped-
    # requires waiters before any promotion decision, so no dead waiter is ever promoted.
    'Remove-IneligibleLeaseWaiters'
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

# --- 9. Wrapper AST wiring — commit-launch order (Einstein msg-1738 objection) -------------------
#
# The lease-waiting bail must NOT be reached through Invoke-HeadSkipCommitLaunch. head_skip.py's
# module docstring is explicit: "LAUNCH verdicts the sweep did not act on are left with their
# launch baseline untouched — they stay eligible on the next tick." Calling commit-launch and
# THEN bailing on lease-waiting sets nomination_at_launch to the current nomination, so next
# tick's decide sees progressed=False, applies backoff (BASE=15min up to CAP=60min), and the
# waiter cannot wake for up to CAP AFTER the grant. Placing the lease check BEFORE
# commit-launch keeps the head_skip record untouched for a waiter and matches head_skip's
# two-phase contract. This test scans the wrapper's sweep body and pins the source order.
Write-Host "Wrapper AST wiring — lease-waiting bail is BEFORE Invoke-HeadSkipCommitLaunch (contract with head_skip.py two-phase protocol)"

# Find every CommandAst reference to Invoke-HeadSkipCommitLaunch and every StringConstant
# 'lease-waiting'. The lease-waiting bail must come STRICTLY BEFORE the commit-launch call.
$commitCalls = @($ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.CommandElements[0].Extent.Text -eq 'Invoke-HeadSkipCommitLaunch'
    }, $true))
$leaseWaitingLiterals = @($ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
        $n.Value -eq 'lease-waiting'
    }, $true))

if ($commitCalls.Count -eq 0) {
    $script:failures++
    Write-Host "  FAIL  Invoke-HeadSkipCommitLaunch call not found — wiring gone?"
}
elseif ($leaseWaitingLiterals.Count -eq 0) {
    $script:failures++
    Write-Host "  FAIL  'lease-waiting' string literal not found — bail branch gone?"
}
else {
    # Pick the FIRST call site of commit-launch and the assignment of the 'lease-waiting'
    # disposition (there may be others in comments; we look for the assignment).
    $firstCommitLine = ($commitCalls | ForEach-Object { $_.Extent.StartLineNumber } | Sort-Object)[0]
    $firstWaitingLine = ($leaseWaitingLiterals | ForEach-Object { $_.Extent.StartLineNumber } | Sort-Object)[0]
    if ($firstWaitingLine -lt $firstCommitLine) {
        Write-Host "  PASS  lease-waiting (line $firstWaitingLine) precedes Invoke-HeadSkipCommitLaunch (line $firstCommitLine)"
    }
    else {
        $script:failures++
        Write-Host "  FAIL  Invoke-HeadSkipCommitLaunch (line $firstCommitLine) precedes lease-waiting bail (line $firstWaitingLine) — commit-launch will fire for a waiter, feeding backoff and delaying grant-wake by up to CAP=60min (head_skip.py L119)"
    }
}

# --- 10. Wrapper AST wiring — dirty-acquire notification (msg-1757 blocker #2) -------------------
#
# When the probe's 'released' branch fires (Phase-2 promotion with an empty queue), the lease
# record is left with `reclaim_required=true` and no `holder`. Invoke-LeaseAcquire preserves the
# flag on the next candidate that acquires. Without an explicit notification at THAT acquire,
# the operator has no warning that the incoming session is inheriting reclaim duty. Pin two
# things in the wrapper: the operator-visible notification key exists, and the dirty-detection
# gate happens BEFORE Invoke-LeaseAcquire mutates the record (otherwise we would sample a
# fabricated post-acquire state).
Write-Host "Wrapper AST wiring — dirty-acquire notification (msg-1757 blocker #2)"

# The notification key is built as "__lease_reclaim_acquire__/$($dirty.Resource)" — an expandable
# string, so it lives in the wrapper's source as ExpandableStringExpressionAst rather than
# StringConstantExpressionAst. Scan both, and match on the fixed prefix embedded in either.
$hasDirtyKeyConst = @($stringConsts | Where-Object {
    $_.Value -is [string] -and $_.Value.StartsWith('__lease_reclaim_acquire__')
}).Count -gt 0
$expandableStrings = @($ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.ExpandableStringExpressionAst] }, $true))
$hasDirtyKeyExpandable = @($expandableStrings | Where-Object {
    $_.Value -is [string] -and $_.Value.Contains('__lease_reclaim_acquire__')
}).Count -gt 0
CheckTrue "wrapper emits the '__lease_reclaim_acquire__' notification key" ($hasDirtyKeyConst -or $hasDirtyKeyExpandable)

# The dirty detection needs to see the record BEFORE Invoke-LeaseAcquire mutates `holder`.
# That means somewhere in the wrapper source, a 'reclaim_required' string reference must appear
# BEFORE at least one Invoke-LeaseAcquire call site in the candidate-loop flow. If a future
# refactor moves the sampling to after the acquire, the sampled `reclaim_required` will still
# be true (Invoke-LeaseAcquire preserves it), but the `holder` check that gates the sampling
# will misfire and every self-hold re-acquire would fire the notification — annoying and wrong.
# Pin the source order.
$acquireCalls = @($ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.CommandElements[0].Extent.Text -eq 'Invoke-LeaseAcquire'
    }, $true))
$reclaimRefs = @($stringConsts | Where-Object { $_.Value -eq 'reclaim_required' })
if ($acquireCalls.Count -eq 0) {
    $script:failures++
    Write-Host "  FAIL  Invoke-LeaseAcquire call not found — acquire wiring gone?"
}
elseif ($reclaimRefs.Count -eq 0) {
    $script:failures++
    Write-Host "  FAIL  'reclaim_required' string reference not found — dirty-detection branch gone?"
}
else {
    $firstAcquireLine = ($acquireCalls | ForEach-Object { $_.Extent.StartLineNumber } | Sort-Object)[0]
    $firstReclaimLine = ($reclaimRefs | ForEach-Object { $_.Extent.StartLineNumber } | Sort-Object)[0]
    if ($firstReclaimLine -lt $firstAcquireLine) {
        Write-Host "  PASS  reclaim_required sampled (line $firstReclaimLine) BEFORE first Invoke-LeaseAcquire (line $firstAcquireLine)"
    }
    else {
        $script:failures++
        Write-Host "  FAIL  Invoke-LeaseAcquire (line $firstAcquireLine) precedes reclaim_required sampling (line $firstReclaimLine) — dirty detection will misfire on self-hold re-acquires"
    }
}

# --- 11. Merge-LeasesStateForWrite — operator write mid-sweep survives (msg-1802 blocker #1) ----
#
# leases.json's top-level key is the resource name. The sweep's probe mutates that key at tick
# start (last_progress_at, idle_evaluations, expiring). If an operator runs Grant-Lease.ps1
# mid-sweep and writes a new holder for the same key to disk, the generic Merge-StateForWrite
# (which merges at top-level key granularity and lets memory win on collision) would silently
# destroy the operator's Tier-C override. Merge-LeasesStateForWrite uses per-resource
# `generation` as an optimistic-concurrency token to detect the external write.
Write-Host "Merge-LeasesStateForWrite — operator write mid-sweep survives (msg-1802 blocker #1)"

# Fresh temp fixture (never this checkout's own state dir).
$fixtureDir = Join-Path ([System.IO.Path]::GetTempPath()) ("mindwire-lease-merge-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixtureDir -Force | Out-Null
$fixturePath = Join-Path $fixtureDir 'leases.json'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Save-FixtureLeases {
    param([string]$Path, [hashtable]$State)
    [System.IO.File]::WriteAllText($Path, ($State | ConvertTo-Json -Depth 5), $utf8NoBom)
}

try {
    # --- scenario A: no external write. Sweep memory wins; probe's last_progress_at persists ---
    $diskA = @{
        editor = @{ holder = 'p/T-a'; generation = 5; last_progress_at = '2026-08-25T00:00:00Z'; idle_evaluations = 0; queue = @() }
    }
    Save-FixtureLeases -Path $fixturePath -State $diskA
    $memoryA = ConvertTo-LeasesStateHashtable (Get-JsonState -Path $fixturePath)
    $originalGens = Get-LeaseGenerations -LeasesState $memoryA
    $memoryA['editor']['last_progress_at'] = '2026-08-26T05:00:00Z'  # probe updated the clock
    $mergedA = Merge-LeasesStateForWrite -Memory $memoryA -OriginalGenerations $originalGens -DiskPath $fixturePath
    Check "no-external-write: probe update survives" '2026-08-26T05:00:00Z' $mergedA['editor'].last_progress_at
    Check "no-external-write: generation unchanged" 5 $mergedA['editor'].generation

    # --- scenario B: operator write mid-sweep (bumped generation). Disk wins entirely; memory
    # discarded for that resource. This is THE bug fix.
    $diskB0 = @{
        editor = @{ holder = 'p/T-a'; generation = 5; last_progress_at = '2026-08-25T00:00:00Z'; idle_evaluations = 0; queue = @() }
    }
    Save-FixtureLeases -Path $fixturePath -State $diskB0
    $memoryB = ConvertTo-LeasesStateHashtable (Get-JsonState -Path $fixturePath)
    $originalGens = Get-LeaseGenerations -LeasesState $memoryB
    # Sweep touches memory (last_progress_at bump), does NOT bump generation.
    $memoryB['editor']['last_progress_at'] = '2026-08-26T05:00:00Z'
    # Meanwhile operator (Grant-Lease.ps1) rewrote disk: new holder, generation bumped to 6.
    $diskB1 = @{
        editor = @{ holder = 'p/T-b'; generation = 6; last_progress_at = '2026-08-26T04:59:00Z'; idle_evaluations = 0; queue = @(); revoked_reason = 'operator-grant: emergency' }
    }
    Save-FixtureLeases -Path $fixturePath -State $diskB1
    $mergedB = Merge-LeasesStateForWrite -Memory $memoryB -OriginalGenerations $originalGens -DiskPath $fixturePath
    Check "operator-write: disk holder wins over sweep memory" 'p/T-b' $mergedB['editor'].holder
    Check "operator-write: disk generation wins" 6 $mergedB['editor'].generation
    Check "operator-write: disk revoked_reason preserved" 'operator-grant: emergency' $mergedB['editor'].revoked_reason

    # --- scenario C: sweep did an acquire (bumped generation) with no operator write. Memory wins.
    $diskC0 = @{
        editor = @{ holder = $null; generation = 5; last_progress_at = $null; idle_evaluations = 0; queue = @(); reclaim_required = $true; reclaimed_from = 'p/T-a' }
    }
    Save-FixtureLeases -Path $fixturePath -State $diskC0
    $memoryC = ConvertTo-LeasesStateHashtable (Get-JsonState -Path $fixturePath)
    $originalGens = Get-LeaseGenerations -LeasesState $memoryC
    Invoke-LeaseAcquire -LeasesState $memoryC -Resource 'editor' -CandidateKey 'p/T-b' -Now $now
    # Disk unchanged (nobody else wrote).
    $mergedC = Merge-LeasesStateForWrite -Memory $memoryC -OriginalGenerations $originalGens -DiskPath $fixturePath
    Check "no-collision + sweep-acquire: memory holder wins" 'p/T-b' $mergedC['editor'].holder
    Check "no-collision + sweep-acquire: memory bumped generation wins" 6 $mergedC['editor'].generation

    # --- scenario D: resource on disk that the sweep never read. Preserved from disk.
    $diskD = @{
        editor = @{ holder = 'p/T-a'; generation = 5; queue = @() }
        runner = @{ holder = 'p/T-x'; generation = 3; queue = @() }
    }
    Save-FixtureLeases -Path $fixturePath -State $diskD
    # Simulate a sweep that only knew about `editor` at start (runner added by operator later).
    $memoryD = @{ editor = @{ holder = 'p/T-a'; generation = 5; queue = @() } }
    $originalGensD = @{ editor = 5 }
    $mergedD = Merge-LeasesStateForWrite -Memory $memoryD -OriginalGenerations $originalGensD -DiskPath $fixturePath
    Check "disk-only resource: preserved" 'p/T-x' $mergedD['runner'].holder
    Check "disk-only resource: generation preserved" 3 $mergedD['runner'].generation

    # --- scenario E: Get-LeaseGenerations edge cases ---
    $gens = Get-LeaseGenerations -LeasesState @{}
    Check "Get-LeaseGenerations: empty map -> empty" 0 $gens.Keys.Count
    $gens = Get-LeaseGenerations -LeasesState @{ editor = @{ generation = 7 }; runner = @{ } }
    Check "Get-LeaseGenerations: editor gen extracted" 7 $gens['editor']
    Check "Get-LeaseGenerations: missing generation -> 0" 0 $gens['runner']
}
finally {
    Remove-Item -LiteralPath $fixtureDir -Recurse -Force -ErrorAction SilentlyContinue
}

# --- 12. Wrapper AST wiring — the LEASE merger is used, not the generic one (msg-1802 #1) -------
#
# Regression pin: a future refactor must not silently swap Merge-LeasesStateForWrite back to
# Merge-StateForWrite for leases.json. The specific failure that hides behind that swap is a
# silent operator-grant destruction — no exception, no log, just the wrong value on disk.
Write-Host "Wrapper AST wiring — Merge-LeasesStateForWrite (not Merge-StateForWrite) merges leases.json"

$mergeLeaseCalls = @($ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.CommandElements[0].Extent.Text -eq 'Merge-LeasesStateForWrite'
    }, $true))
CheckTrue "wrapper calls Merge-LeasesStateForWrite" ($mergeLeaseCalls.Count -ge 1)

# Pin that the argument threaded through -Memory is $leasesState (the normalised in-memory map)
# and -OriginalGenerations receives a snapshot variable. The variable name is soft-pinned to
# `$leasesOriginalGenerations` to guarantee the snapshot is taken (not omitted or defaulted).
$hasGenerationsParam = $false
foreach ($c in $mergeLeaseCalls) {
    foreach ($el in $c.CommandElements) {
        if ($el -is [System.Management.Automation.Language.CommandParameterAst] -and $el.ParameterName -eq 'OriginalGenerations') {
            $hasGenerationsParam = $true; break
        }
    }
}
CheckTrue "Merge-LeasesStateForWrite called with -OriginalGenerations (snapshot threaded through)" $hasGenerationsParam

$leaseGensSnapshotCalls = @($ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.CommandElements[0].Extent.Text -eq 'Get-LeaseGenerations'
    }, $true))
CheckTrue "wrapper calls Get-LeaseGenerations (snapshot at sweep start)" ($leaseGensSnapshotCalls.Count -ge 1)

# Pin the order: Get-LeaseGenerations snapshot must precede the first Invoke-LeasePromotion /
# Invoke-LeaseAcquire call (either of which can bump generation). If someone re-orders the
# snapshot to run AFTER a mutation, the OriginalGenerations map will contain the post-mutation
# value, the merger will see disk_gen == snapshot_gen even after an operator write, and the
# concurrency-detection breaks silently.
$firstSnapshotLine = ($leaseGensSnapshotCalls | ForEach-Object { $_.Extent.StartLineNumber } | Sort-Object)[0]
$mutatingCalls = @($ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        ($n.CommandElements[0].Extent.Text -eq 'Invoke-LeasePromotion' -or
         $n.CommandElements[0].Extent.Text -eq 'Invoke-LeaseAcquire')
    }, $true))
if ($mutatingCalls.Count -gt 0) {
    $firstMutationLine = ($mutatingCalls | ForEach-Object { $_.Extent.StartLineNumber } | Sort-Object)[0]
    if ($firstSnapshotLine -lt $firstMutationLine) {
        Write-Host "  PASS  Get-LeaseGenerations snapshot (line $firstSnapshotLine) precedes first Invoke-Lease{Promotion|Acquire} (line $firstMutationLine)"
    }
    else {
        $script:failures++
        Write-Host "  FAIL  snapshot (line $firstSnapshotLine) runs AFTER first Invoke-Lease{Promotion|Acquire} (line $firstMutationLine) — operator-write detection is broken"
    }
}

# Also pin that leases.json is NOT handed to the generic Merge-StateForWrite anymore (the fix).
$genericMergeCalls = @($ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.CommandElements[0].Extent.Text -eq 'Merge-StateForWrite'
    }, $true))
$leasesFedToGeneric = $false
foreach ($c in $genericMergeCalls) {
    foreach ($el in $c.CommandElements) {
        # Look for `-DiskPath $leasesStatePath` on any generic-merge call.
        if ($el -is [System.Management.Automation.Language.VariableExpressionAst] -and $el.VariablePath.UserPath -eq 'leasesStatePath') {
            $leasesFedToGeneric = $true; break
        }
    }
}
CheckFalse "leases.json is NOT passed to the generic Merge-StateForWrite" $leasesFedToGeneric

# --- 13. Wrapper AST wiring — empty-with-queue drain runs in the probe (msg-1852 blocker) --------
#
# Regression pin: the drain must happen in the OUT-OF-BAND probe (before the candidate loop),
# not in the candidate loop itself. If a future refactor moves Invoke-LeaseGrantFromEmpty into
# the candidate loop, the very FIRST candidate evaluated for the resource (whether it was
# queued or not) will still snipe the lease before the drain runs — the FIFO promise (msg-1183
# D-3) is only honoured if the drain precedes ANY candidate-loop lease decision.
Write-Host "Wrapper AST wiring — Invoke-LeaseGrantFromEmpty runs in the probe, BEFORE Test-LeaseAvailableFor (msg-1852 blocker)"

$grantFromEmptyCalls = @($ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.CommandElements[0].Extent.Text -eq 'Invoke-LeaseGrantFromEmpty'
    }, $true))
$availableCalls = @($ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.CommandElements[0].Extent.Text -eq 'Test-LeaseAvailableFor'
    }, $true))

if ($grantFromEmptyCalls.Count -eq 0) {
    $script:failures++
    Write-Host "  FAIL  Invoke-LeaseGrantFromEmpty call not found in wrapper — drain wiring gone?"
}
elseif ($availableCalls.Count -eq 0) {
    $script:failures++
    Write-Host "  FAIL  Test-LeaseAvailableFor call not found — candidate-loop lease gate gone?"
}
else {
    $firstGrantLine = ($grantFromEmptyCalls | ForEach-Object { $_.Extent.StartLineNumber } | Sort-Object)[0]
    $firstAvailableLine = ($availableCalls | ForEach-Object { $_.Extent.StartLineNumber } | Sort-Object)[0]
    if ($firstGrantLine -lt $firstAvailableLine) {
        Write-Host "  PASS  Invoke-LeaseGrantFromEmpty (line $firstGrantLine) precedes Test-LeaseAvailableFor (line $firstAvailableLine) — probe drains queue before candidate loop"
    }
    else {
        $script:failures++
        Write-Host "  FAIL  Test-LeaseAvailableFor (line $firstAvailableLine) precedes Invoke-LeaseGrantFromEmpty (line $firstGrantLine) — first candidate snipes past FIFO before drain runs"
    }
}

# --- 14. Wrapper AST wiring — queue scrub runs BEFORE any promotion (msg-1875 blocker) -----------
#
# Regression pin: Remove-IneligibleLeaseWaiters must precede BOTH Invoke-LeaseGrantFromEmpty
# (empty-holder drain) and Invoke-LeasePromotion (Phase 2 revoke). If a future refactor moves
# the scrub after either promotion path, the dead waiter is promoted first and holds the lease
# idle for up to LeaseIdleTtl (2h). This is a semantic-adjacency invariant that the source
# order encodes; a source-order test is the cheapest way to keep it honest (same lesson as
# rounds 2/4/5 for commit-launch, merger snapshot, and grant-from-empty).
Write-Host "Wrapper AST wiring — Remove-IneligibleLeaseWaiters precedes Invoke-LeaseGrantFromEmpty AND Invoke-LeasePromotion (msg-1875 blocker)"

$scrubCalls = @($ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.CommandElements[0].Extent.Text -eq 'Remove-IneligibleLeaseWaiters'
    }, $true))
$grantFromEmptyCalls = @($ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.CommandElements[0].Extent.Text -eq 'Invoke-LeaseGrantFromEmpty'
    }, $true))
$promotionCalls = @($ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.CommandElements[0].Extent.Text -eq 'Invoke-LeasePromotion'
    }, $true))

if ($scrubCalls.Count -eq 0) {
    $script:failures++
    Write-Host "  FAIL  Remove-IneligibleLeaseWaiters call not found — scrub wiring gone?"
}
else {
    $firstScrubLine = ($scrubCalls | ForEach-Object { $_.Extent.StartLineNumber } | Sort-Object)[0]
    if ($grantFromEmptyCalls.Count -gt 0) {
        $firstGrantLine = ($grantFromEmptyCalls | ForEach-Object { $_.Extent.StartLineNumber } | Sort-Object)[0]
        if ($firstScrubLine -lt $firstGrantLine) {
            Write-Host "  PASS  Remove-IneligibleLeaseWaiters (line $firstScrubLine) precedes Invoke-LeaseGrantFromEmpty (line $firstGrantLine)"
        }
        else {
            $script:failures++
            Write-Host "  FAIL  Invoke-LeaseGrantFromEmpty (line $firstGrantLine) precedes scrub (line $firstScrubLine) — dead waiter would be promoted from empty lease"
        }
    }
    if ($promotionCalls.Count -gt 0) {
        $firstPromotionLine = ($promotionCalls | ForEach-Object { $_.Extent.StartLineNumber } | Sort-Object)[0]
        if ($firstScrubLine -lt $firstPromotionLine) {
            Write-Host "  PASS  Remove-IneligibleLeaseWaiters (line $firstScrubLine) precedes Invoke-LeasePromotion (line $firstPromotionLine)"
        }
        else {
            $script:failures++
            Write-Host "  FAIL  Invoke-LeasePromotion (line $firstPromotionLine) precedes scrub (line $firstScrubLine) — dead waiter would be promoted from revoked holder"
        }
    }
}

# --- 15. Grant-Lease.ps1 preserves reclaim_required on empty-state grant (msg-1876 blocker) -----
#
# End-to-end test: invoke the real Grant-Lease.ps1 script against a temp fixture and check that
# a -Grant on a lease whose prior state was `holder=null, reclaim_required=true` (produced by a
# preceding -Clear) does NOT silently erase the flag. Erasure would fire the new holder blindly
# on top of a physically-lingering session — the exact silent-collision this mechanism exists
# to end. Same fixture pattern as Test-ClearQuarantine.ps1 (temp dir + shell out).
Write-Host "Grant-Lease.ps1 — preserve reclaim_required across empty-state grant (msg-1876 blocker)"

$grantScript = Join-Path $repoRoot "deploy/Grant-Lease.ps1"
if (-not (Test-Path -LiteralPath $grantScript)) { throw "Grant-Lease.ps1 not found: $grantScript" }

# Parse-check up front so a syntax break falls out here rather than through a shell-exit code.
$parseErrors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($grantScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) {
    $parseErrors | ForEach-Object { Write-Host "PARSE ERROR line $($_.Extent.StartLineNumber): $($_.Message)" }
    throw "deploy/Grant-Lease.ps1 does not parse"
}

$fixtureDir = Join-Path ([System.IO.Path]::GetTempPath()) ("mindwire-grant-lease-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $fixtureDir 'state') -Force | Out-Null
$fixtureLeases = Join-Path $fixtureDir 'state\leases.json'
$utf8NoBomLocal = New-Object System.Text.UTF8Encoding($false)

function Save-FixtureLeasesGL {
    param([string]$Path, [hashtable]$State)
    [System.IO.File]::WriteAllText($Path, ($State | ConvertTo-Json -Depth 5), $utf8NoBomLocal)
}
function Load-FixtureLeasesGL {
    param([string]$Path)
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding utf8
    $obj = $raw | ConvertFrom-Json
    $map = @{}
    foreach ($p in $obj.PSObject.Properties) { $map[$p.Name] = $p.Value }
    return $map
}

try {
    # --- scenario A: prior state is empty-with-reclaim from a Clear. Grant must preserve. -------
    Save-FixtureLeasesGL -Path $fixtureLeases -State @{
        editor = @{
            holder = $null; acquired_at = $null; last_progress_at = $null
            idle_evaluations = 0; generation = 7; pinned = $false; expiring = $false
            reclaimed_from = 'p/T-crashed-holder'; reclaim_required = $true
            revoked_at = '2026-08-25T00:00:00Z'; revoked_reason = 'operator-clear: PIE crashed'
            queue = @()
        }
    }
    $env:MINDWIRE_PATHS__DATA_DIR = $fixtureDir
    try {
        $grantOutput = & pwsh -NoProfile -File $grantScript -Resource 'editor' -To 'p/T-new-owner' -Reason 'A/B test' -Confirm:$false *>&1
    }
    finally {
        Remove-Item Env:\MINDWIRE_PATHS__DATA_DIR -ErrorAction SilentlyContinue
    }
    if ($LASTEXITCODE -ne 0) {
        $script:failures++
        Write-Host "  FAIL  Grant-Lease.ps1 exited non-zero: $LASTEXITCODE"
        $grantOutput | ForEach-Object { Write-Host "    $_" }
    }
    $post = Load-FixtureLeasesGL -Path $fixtureLeases
    Check "empty-state grant: new holder recorded" 'p/T-new-owner' "$($post['editor'].holder)"
    CheckTrue "empty-state grant: reclaim_required PRESERVED (msg-1876 blocker)" ([bool]$post['editor'].reclaim_required)
    Check "empty-state grant: reclaimed_from PRESERVED (prior Clear's audit)" 'p/T-crashed-holder' "$($post['editor'].reclaimed_from)"
    Check "empty-state grant: generation bumped from 7" 8 ([int]$post['editor'].generation)
    Check "empty-state grant: revoked_reason updated to operator-grant" $true (("$($post['editor'].revoked_reason)").StartsWith('operator-grant:'))
    # Console output must carry the inherited-reclaim warning so the operator sees it at
    # command time — the operator-inbox notification is fired by the next sweep probe.
    $hasWarning = @($grantOutput | Where-Object { "$_" -match 'reclaim_required=true' }).Count -gt 0
    CheckTrue "empty-state grant: console WARNING surfaces inherited reclaim duty" $hasWarning

    # --- scenario B: prior state is a live different holder. Standard reclaim (unchanged behavior).
    Save-FixtureLeasesGL -Path $fixtureLeases -State @{
        editor = @{
            holder = 'p/T-live'; acquired_at = '2026-08-26T00:00:00Z'; last_progress_at = '2026-08-26T00:00:00Z'
            idle_evaluations = 0; generation = 3; pinned = $false; expiring = $false
            reclaimed_from = $null; reclaim_required = $false
            revoked_at = $null; revoked_reason = $null
            queue = @()
        }
    }
    $env:MINDWIRE_PATHS__DATA_DIR = $fixtureDir
    try {
        & pwsh -NoProfile -File $grantScript -Resource 'editor' -To 'p/T-forced' -Reason 'operator override' -Confirm:$false *>&1 | Out-Null
    }
    finally {
        Remove-Item Env:\MINDWIRE_PATHS__DATA_DIR -ErrorAction SilentlyContinue
    }
    $post = Load-FixtureLeasesGL -Path $fixtureLeases
    Check "live-overwrite grant: new holder recorded" 'p/T-forced' "$($post['editor'].holder)"
    CheckTrue "live-overwrite grant: reclaim_required = true (msg-1183 D-6'e, unchanged)" ([bool]$post['editor'].reclaim_required)
    Check "live-overwrite grant: reclaimed_from records prior live holder" 'p/T-live' "$($post['editor'].reclaimed_from)"

    # --- scenario C: prior state is clean (no reclaim owed). Grant into empty must NOT invent one.
    Save-FixtureLeasesGL -Path $fixtureLeases -State @{
        editor = @{
            holder = $null; acquired_at = $null; last_progress_at = $null
            idle_evaluations = 0; generation = 2; pinned = $false; expiring = $false
            reclaimed_from = $null; reclaim_required = $false
            revoked_at = $null; revoked_reason = $null
            queue = @()
        }
    }
    $env:MINDWIRE_PATHS__DATA_DIR = $fixtureDir
    try {
        & pwsh -NoProfile -File $grantScript -Resource 'editor' -To 'p/T-first' -Reason 'fresh assignment' -Confirm:$false *>&1 | Out-Null
    }
    finally {
        Remove-Item Env:\MINDWIRE_PATHS__DATA_DIR -ErrorAction SilentlyContinue
    }
    $post = Load-FixtureLeasesGL -Path $fixtureLeases
    Check "clean-empty grant: new holder recorded" 'p/T-first' "$($post['editor'].holder)"
    CheckFalse "clean-empty grant: reclaim_required stays false (no prior duty to inherit)" ([bool]$post['editor'].reclaim_required)
}
finally {
    Remove-Item -LiteralPath $fixtureDir -Recurse -Force -ErrorAction SilentlyContinue
}

# --- 16. Wrapper AST wiring — probe-level reclaim notification (msg-1876 blocker) ----------------
#
# The msg-1876 blocker cannot be caught by the candidate-loop dirty-detection (which only fires
# on empty priorHolder). The probe-level fire must exist and must appear in a location that runs
# per-resource inside the probe loop, so an operator-Grant-into-empty-state gets picked up on
# the very next tick even though at candidate-eval time the holder is already set.
Write-Host "Wrapper AST wiring — probe-level reclaim notification for post-operator-Grant (msg-1876 blocker)"

# All Send-NotificationIfChanged call sites that fire __lease_reclaim_acquire__/*.
$allExpandable = @($ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.ExpandableStringExpressionAst] }, $true))
$reclaimAcquireKeys = @($allExpandable | Where-Object {
    $_.Value -is [string] -and $_.Value.Contains('__lease_reclaim_acquire__')
})
CheckTrue "wrapper still emits __lease_reclaim_acquire__ (probe + candidate-loop paths)" ($reclaimAcquireKeys.Count -ge 2)

# Cross-tick dedup requires the signature to include only stable fields (holder + generation),
# not $nowUtc. A `nowUtc.ToString('o')` in the signature of any __lease_reclaim_acquire__ fire
# would silently break dedup — every tick with reclaim_required=true would re-notify. Pin the
# absence of tick-varying signatures on these fires. Scan the wrapper source lines with
# 'lease_reclaim_acquire' for any that also contain 'nowUtc.ToString'.
$wrapperText = Get-Content -LiteralPath $sweepScript -Raw
$reclaimAcquireLineRegions = [regex]::Matches(
    $wrapperText,
    '__lease_reclaim_acquire__[\s\S]{0,600}?ToUniversalTime|Send-NotificationIfChanged[^\n]*__lease_reclaim_acquire__[\s\S]{0,600}?nowUtc\.ToString',
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
# We want NONE — every __lease_reclaim_acquire__ fire's signature should use "$holder@$gen"
# style, no time-varying material.
Check "no __lease_reclaim_acquire__ signature uses tick-varying nowUtc.ToString (cross-tick dedup)" 0 $reclaimAcquireLineRegions.Count

# --- 17. Wrapper AST wiring — pre-spawn lease durability flush (msg-1877 blocker) ---------------
#
# Regression pin: Invoke-LeaseAcquire only mutates $leasesState in memory. Without a durable
# flush BEFORE Invoke-HeadSkipCommitLaunch (and BEFORE the actual spawn), an OS-level kill
# after a physical session started but before end-of-tick would leave leases.json on disk
# without our holder — next tick another candidate finds the lease free and boots a second
# session over the first. This test pins that (a) Merge-LeasesStateForWrite is called at
# LEAST TWICE (mid-candidate-loop pre-spawn + end-of-tick), (b) at least one of those calls
# appears at a source line BEFORE the first Invoke-HeadSkipCommitLaunch call site (the
# pre-spawn one), and (c) the 'lease-lost' disposition string exists so the bail-on-override
# path is present in the source.
Write-Host "Wrapper AST wiring — pre-spawn lease durability flush precedes commit-launch (msg-1877 blocker)"

$mergeLeaseCalls2 = @($ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.CommandElements[0].Extent.Text -eq 'Merge-LeasesStateForWrite'
    }, $true))
CheckTrue "wrapper calls Merge-LeasesStateForWrite at least twice (pre-spawn + end-of-tick)" ($mergeLeaseCalls2.Count -ge 2)

$commitLaunchCalls2 = @($ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.CommandElements[0].Extent.Text -eq 'Invoke-HeadSkipCommitLaunch'
    }, $true))
if ($mergeLeaseCalls2.Count -eq 0 -or $commitLaunchCalls2.Count -eq 0) {
    $script:failures++
    Write-Host "  FAIL  either Merge-LeasesStateForWrite or Invoke-HeadSkipCommitLaunch call missing"
}
else {
    $firstCommitLine2 = ($commitLaunchCalls2 | ForEach-Object { $_.Extent.StartLineNumber } | Sort-Object)[0]
    $mergesBeforeCommit = @($mergeLeaseCalls2 | Where-Object { $_.Extent.StartLineNumber -lt $firstCommitLine2 })
    if ($mergesBeforeCommit.Count -ge 1) {
        Write-Host "  PASS  at least one Merge-LeasesStateForWrite (line $($mergesBeforeCommit[0].Extent.StartLineNumber)) precedes Invoke-HeadSkipCommitLaunch (line $firstCommitLine2) — pre-spawn flush wired"
    }
    else {
        $script:failures++
        Write-Host "  FAIL  no Merge-LeasesStateForWrite call precedes Invoke-HeadSkipCommitLaunch — lease acquire is not durably flushed before spawn"
    }
}

# The lease-lost disposition is the observable trace of an operator-override bail. Pin its
# presence so a future refactor cannot silently drop the bail branch (which would spawn even
# after our acquire was overridden mid-sweep — the exact silent collision).
$leaseLostLiterals = @($stringConsts | Where-Object { $_.Value -eq 'lease-lost' })
CheckTrue "wrapper has the 'lease-lost' disposition string (operator-override bail wired)" ($leaseLostLiterals.Count -ge 1)

# Also require Save-JsonState with -Path referencing $leasesStatePath to appear before the
# first commit-launch. This closes a loophole where someone merges but forgets to save.
$saveJsonCalls = @($ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.CommandElements[0].Extent.Text -eq 'Save-JsonState'
    }, $true))
$preSpawnSaveFound = $false
foreach ($c in $saveJsonCalls) {
    if ($c.Extent.StartLineNumber -ge $firstCommitLine2) { continue }
    foreach ($el in $c.CommandElements) {
        if ($el -is [System.Management.Automation.Language.VariableExpressionAst] -and $el.VariablePath.UserPath -eq 'leasesStatePath') {
            $preSpawnSaveFound = $true; break
        }
    }
    if ($preSpawnSaveFound) { break }
}
CheckTrue "Save-JsonState -Path \$leasesStatePath appears before Invoke-HeadSkipCommitLaunch (durable flush persisted)" $preSpawnSaveFound

Write-Host ""
if ($script:failures -gt 0) { Write-Host "lease gate: $($script:failures) check(s) FAILED"; exit 1 }
Write-Host "lease gate: all checks passed"
exit 0
