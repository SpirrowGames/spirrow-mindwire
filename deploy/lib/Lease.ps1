# deploy/lib/Lease.ps1 — exclusive-resource lease + queue for the scheduled sweep.
#
# Why this file exists (T-exclusive-resource-lease-queue, msg-923 request; msg-1180/1183/1185/1187
# design). Some resources are indivisible — the editor/PIE session, bridge port 55557, later a
# repo checkout, later a runner — and the tick before this design landed, the only mechanism
# saying "only Thread X holds the editor" was Takahito writing it into a Tier-C decision as a
# sentence. When that sentence was forgotten, two threads were placed on top of the same editor
# and only one being crashed at the time kept the collision from happening. `NEXT: human` waits
# went unnoticed for days because no machine was tracking them. The design is: give the sweep
# a lease and a queue so the fifo waking is a mechanism, not a note in a chat log.
#
# THIS FILE IS THE MECHANISM. It never talks to Discord, never launches conductors, never touches
# `evaluated.json`. It reads and writes exactly ONE state file — `<data_dir>/state/leases.json` —
# and returns pure verdicts to the runner. Every side effect on any other file is the runner's,
# because that is where operator concurrency + the sweep's flush-on-tick discipline already live
# (`Merge-StateForWrite` in the runner). Adding a second writer here would collapse the "one file,
# one owner" property Bohr msg-1183 §1-b required for review to stay tractable.
#
# NO TOP-LEVEL SIDE EFFECTS — dot-sourced by both the runner and Test-Lease.ps1, so any script-
# scope assignment would leak into the caller. Same discipline as deploy/lib/StopReason.ps1.

# --- schema notes -------------------------------------------------------------------------------
# leases.json shape (one entry per resource name; v1 v ships with `editor` only, but the code is
# resource-agnostic — the sweep candidate's `requires: [...]` array is what decides which lease
# a candidate needs, and the runner never hard-codes a name):
#
# {
#   "editor": {
#     "holder": "spirrow-voxelworld/T-materializechunk-zone-relocation-crash",
#     "acquired_at":       "2026-08-26T04:00:00.0000000Z",
#     "last_progress_at":  "2026-08-26T05:00:00.0000000Z",
#     "idle_evaluations":  0,
#     "generation":        3,
#     "pinned":            false,
#     "expiring":          false,      # tick T: marked; tick T+1: promoted (two-phase revoke)
#     "reclaimed_from":    null,       # who the current holder took the lease from (audit)
#     "reclaim_required":  false,      # new holder MUST restart the resource before use
#     "revoked_at":        null,
#     "revoked_reason":    null,
#     "queue": [
#       {"key": "spirrow-mindwire/T-x", "waiting_since": "2026-08-26T04:30:00.0000000Z"}
#     ]
#   }
# }
#
# ABSENT resource key = no holder, empty queue. Do NOT create empty stub entries — a resource that
# has never been leased leaves no trace in the file. This keeps the file readable by eye when
# every lease is free (the common case).

# --- probe classifications ----------------------------------------------------------------------
# `progress` — holder made progress this tick, OR is currently running (held), OR its verdict is
#              LAUNCH (about to run) — reset `idle_evaluations`, update `last_progress_at`.
# `parked`   — holder definitely will not run this tick regardless of any lease action: verdict is
#              SKIP (Stage 1 stop-token: NEXT: none / NEXT: human), OR holder is quarantined, OR
#              holder was removed from sweep.json entirely. Increment `idle_evaluations`.
# `neutral`  — holder is DEFERred (backoff, will launch soon) — do NOT increment, do NOT reset.
#              This is Bohr msg-1183 §2-c's "wrapper-side starvation" case for the current
#              wrapper: the decision to skip is time-based, not lease-based, and the holder will
#              reach LAUNCH on its own without any lease intervention. Treating DEFER as parked
#              would revive the split-brain Einstein msg-1182 raised.
#
# The parked / progress / neutral trichotomy is exhaustive under the current wrapper's decide
# verdicts + held/quarantined filters. If a fourth disposition is ever added, THIS FUNCTION
# is the one place that must be updated — leaving a case unhandled here reproduces the exact
# "silent parked" failure mode the design exists to end.

function Get-LeaseHolderClassification {
    <#
    .SYNOPSIS
        Classify the current holder's activity this tick as progress / parked / neutral.

    .PARAMETER HolderKey
        The "$project/$thread_id" state key of the current lease holder.

    .PARAMETER IsHeld
        $true when the holder's project is currently under an acknowledged operator hold. Held
        pauses the TTL (msg-1183 D-6'c: a legitimately long PIE turn is head-unchanged but held,
        and must not be reclaimed for wall-clock starvation).

    .PARAMETER IsQuarantined
        $true when the holder is in quarantine.json. A quarantined holder is parked (definitely
        will not run) — matches SKIP in the "counts toward idle" rule.

    .PARAMETER IsOnSweep
        $true when the holder is still present in the current sweep.json list. A holder that has
        been removed from sweep.json entirely is parked (will never be evaluated again).

    .PARAMETER Verdict
        The head-skip decide verdict object for the holder this tick, or $null when there is no
        verdict (holder is held or quarantined or absent). Only the `.decision` field is read here.

    .OUTPUTS
        A string: 'progress', 'parked', or 'neutral'.
    #>
    param(
        [string]$HolderKey,
        [bool]$IsHeld,
        [bool]$IsQuarantined,
        [bool]$IsOnSweep,
        $Verdict
    )

    # Held keeps the lease alive without moving anything — msg-1183 D-6'c. A held holder is not
    # parked (the operator has deliberately paused it) and not progressing (nothing is running),
    # but for lease TTL purposes it must not be reclaimed. Treated as progress: the TTL clock is
    # reset every tick the holder is held.
    if ($IsHeld) { return 'progress' }

    # Removed from the sweep list entirely: the holder cannot make progress and will never be
    # evaluated again. Same category as quarantined.
    if (-not $IsOnSweep) { return 'parked' }

    # Quarantined holder: wrapper-side stopped, cannot progress until Clear-Quarantine.
    if ($IsQuarantined) { return 'parked' }

    # No verdict for a candidate that is on the sweep, not held, not quarantined is a wiring bug
    # in the caller (every eligible candidate gets a decide verdict per the wrapper's W-2c
    # contract). Return neutral to keep the lease alive rather than incorrectly reclaiming.
    if ($null -eq $Verdict) { return 'neutral' }

    $decision = "$($Verdict.decision)".ToLowerInvariant()
    if ($decision -eq 'skip')   { return 'parked' }
    if ($decision -eq 'launch') { return 'progress' }
    if ($decision -eq 'defer')  { return 'neutral' }

    # Unknown decision — a contract violation is the caller's concern, not the lease's.
    return 'neutral'
}

function Update-LeaseFromClassification {
    <#
    .SYNOPSIS
        Mutate a lease record in place based on a probe classification. Pure w.r.t. leases.json;
        does not touch evaluated.json (msg-1185 §2-b: two clocks, never crossed).

    .PARAMETER Lease
        The per-resource lease hashtable (see schema notes at file head). Mutated in place.

    .PARAMETER Classification
        One of 'progress' / 'parked' / 'neutral' from Get-LeaseHolderClassification.

    .PARAMETER Now
        The tick's UTC timestamp. Same value used for every mutation this tick.
    #>
    param(
        [hashtable]$Lease,
        [string]$Classification,
        [datetime]$Now
    )
    if ($null -eq $Lease) { return }
    $nowIso = $Now.ToUniversalTime().ToString("o")
    switch ($Classification) {
        'progress' {
            $Lease['idle_evaluations'] = 0
            $Lease['last_progress_at'] = $nowIso
        }
        'parked' {
            $prior = 0
            if ($Lease.ContainsKey('idle_evaluations') -and $null -ne $Lease['idle_evaluations']) {
                $prior = [int]$Lease['idle_evaluations']
            }
            $Lease['idle_evaluations'] = $prior + 1
        }
        # 'neutral' — deliberately do nothing. Backoff isn't parking.
    }
}

function Test-LeaseExpiring {
    <#
    .SYNOPSIS
        Whether a lease has reached the expiry predicate (msg-1183 D-6' dual predicate:
        `idle_evaluations >= LeaseIdleEvaluations` AND wall-clock since `last_progress_at` >
        `LeaseIdleTtl`). A pinned lease NEVER expires (msg-1183 D-7).

    .PARAMETER Lease
        The per-resource lease hashtable.

    .PARAMETER IdleEvaluationsMin
        Threshold count of consecutive parked probes.

    .PARAMETER IdleTtl
        Wall-clock TimeSpan since last_progress_at that must be exceeded.

    .PARAMETER Now
        The tick's UTC timestamp.

    .OUTPUTS
        $true when the lease is eligible for revocation this tick.
    #>
    param(
        [hashtable]$Lease,
        [int]$IdleEvaluationsMin,
        [TimeSpan]$IdleTtl,
        [datetime]$Now
    )
    if ($null -eq $Lease) { return $false }
    if (-not $Lease.ContainsKey('holder') -or [string]::IsNullOrEmpty("$($Lease['holder'])")) { return $false }
    if ($Lease['pinned']) { return $false }

    $ideCount = 0
    if ($Lease.ContainsKey('idle_evaluations') -and $null -ne $Lease['idle_evaluations']) {
        $ideCount = [int]$Lease['idle_evaluations']
    }
    if ($ideCount -lt $IdleEvaluationsMin) { return $false }

    $lastProgress = $null
    if ($Lease.ContainsKey('last_progress_at') -and $Lease['last_progress_at']) {
        try { $lastProgress = [datetime]::Parse("$($Lease['last_progress_at'])").ToUniversalTime() } catch { $lastProgress = $null }
    }
    # No last_progress_at recorded (freshly-acquired lease with no probe yet): not expiring.
    if ($null -eq $lastProgress) { return $false }
    return (($Now - $lastProgress) -gt $IdleTtl)
}

function New-LeaseRecord {
    <#
    .SYNOPSIS
        Build a fresh lease-record hashtable for a new holder. Used both on the very first
        acquire (queue was empty, no prior holder) and on promotion from the queue.

    .PARAMETER Holder
        The "$project/$thread_id" key of the new holder.

    .PARAMETER Now
        UTC timestamp of this tick.

    .PARAMETER Generation
        The new lease generation. Callers pass (prior generation + 1) on a promotion; use 1 on
        first acquire.

    .PARAMETER ReclaimedFrom
        The previous holder's key (audit), or $null for a fresh acquire on a free lease.

    .PARAMETER ReclaimRequired
        $true when the promotion happened while the previous holder still physically had the
        resource — the new holder must restart the resource before use (msg-1183 D-6'e).

    .PARAMETER Pinned
        $true for an operator-pinned grant (TTL-immune). Default $false.

    .PARAMETER Queue
        The current queue array to preserve (or $null for empty).
    #>
    param(
        [string]$Holder,
        [datetime]$Now,
        [int]$Generation = 1,
        [string]$ReclaimedFrom = $null,
        [bool]$ReclaimRequired = $false,
        [bool]$Pinned = $false,
        [array]$Queue = $null
    )
    $nowIso = $Now.ToUniversalTime().ToString("o")
    $record = @{
        holder            = $Holder
        acquired_at       = $nowIso
        last_progress_at  = $nowIso
        idle_evaluations  = 0
        generation        = $Generation
        pinned            = $Pinned
        expiring          = $false
        reclaimed_from    = $ReclaimedFrom
        reclaim_required  = $ReclaimRequired
        revoked_at        = $null
        revoked_reason    = $null
        queue             = @()
    }
    if ($null -ne $Queue) { $record['queue'] = @($Queue) }
    return $record
}

function Add-LeaseWaiter {
    <#
    .SYNOPSIS
        Append a waiter to a lease's queue in FIFO order, or refresh its `waiting_since` if it
        is already there (an idempotent enqueue — a waiter that shows up every 5 min must not
        multiply in the queue).

        NOTE: msg-1183 D-3 says grant is waiting-time FIFO, sweep order is the tiebreak. The
        tiebreak resolves at grant time (Get-NextLeaseWaiter), not here — this function only
        preserves append order.

    .PARAMETER Lease
        The per-resource lease hashtable. Mutated in place; created with an empty holder if
        called on a resource whose lease record does not exist yet — the caller decides whether
        that is legal (see Register-LeaseWaiter which wraps the check).

    .PARAMETER WaiterKey
        The waiter's "$project/$thread_id" key.

    .PARAMETER Now
        The tick's UTC timestamp; used only when this is a fresh enqueue.
    #>
    param(
        [hashtable]$Lease,
        [string]$WaiterKey,
        [datetime]$Now
    )
    if (-not $Lease.ContainsKey('queue') -or $null -eq $Lease['queue']) { $Lease['queue'] = @() }
    $existing = @($Lease['queue'])
    foreach ($w in $existing) {
        $k = if ($w -is [hashtable]) { $w['key'] } else { $w.key }
        if ($k -eq $WaiterKey) { return }   # already queued; do not refresh `waiting_since`
    }
    $Lease['queue'] = @($existing + @(@{
        key           = $WaiterKey
        waiting_since = $Now.ToUniversalTime().ToString("o")
    }))
}

function Remove-LeaseWaiter {
    <#
    .SYNOPSIS
        Remove a waiter from a lease's queue by key. No-op if absent.
    #>
    param(
        [hashtable]$Lease,
        [string]$WaiterKey
    )
    if (-not $Lease.ContainsKey('queue') -or $null -eq $Lease['queue']) { return }
    $Lease['queue'] = @($Lease['queue'] | Where-Object {
        $k = if ($_ -is [hashtable]) { $_['key'] } else { $_.key }
        $k -ne $WaiterKey
    })
}

function Get-NextLeaseWaiter {
    <#
    .SYNOPSIS
        Return the FIFO-earliest waiter's key, or $null on an empty queue. Ties on
        `waiting_since` are broken by sweep-order — the caller passes the ordered list of live
        sweep keys, and the first waiter that also appears earliest in that list wins the tie.

    .PARAMETER Lease
        The per-resource lease hashtable.

    .PARAMETER SweepOrderKeys
        The ordered array of "$project/$thread_id" keys as they appear in sweep.json (from
        Get-SweepCandidates). Used only for tiebreak — if two waiters have identical
        `waiting_since`, the earlier sweep-list position wins.
    #>
    param(
        [hashtable]$Lease,
        [string[]]$SweepOrderKeys = @()
    )
    if (-not $Lease.ContainsKey('queue') -or $null -eq $Lease['queue']) { return $null }
    $items = @($Lease['queue'])
    if ($items.Count -eq 0) { return $null }

    $rows = @()
    foreach ($w in $items) {
        $k = if ($w -is [hashtable]) { $w['key'] } else { $w.key }
        $wsRaw = if ($w -is [hashtable]) { $w['waiting_since'] } else { $w.waiting_since }
        $ws = [datetime]::MaxValue
        try { if ($wsRaw) { $ws = [datetime]::Parse("$wsRaw").ToUniversalTime() } } catch { }
        $sweepIx = [Array]::IndexOf($SweepOrderKeys, $k)
        if ($sweepIx -lt 0) { $sweepIx = [int]::MaxValue }
        $rows += [pscustomobject]@{ Key = $k; Since = $ws; SweepIx = $sweepIx }
    }
    $sorted = $rows | Sort-Object -Property Since, SweepIx
    return $sorted[0].Key
}

function Invoke-LeasePromotion {
    <#
    .SYNOPSIS
        Promote the FIFO waiter to holder on an expiring lease. Two-phase revoke (msg-1183
        D-6'd, loop_control-style): the first tick that satisfies Test-LeaseExpiring only marks
        the lease `expiring=true` and records `revoked_at`/`revoked_reason` on the OLD holder;
        the SECOND tick (still expiring, still expired, queue non-empty) is when the promotion
        actually lands and the new record is written.

        This mirrors how loop_control's desired/observed split works — the observed side changes
        on a distinct tick from the desired side so a mistaken revocation has a full tick to be
        pre-empted by the operator (a pin, a manual grant) before the physical state changes.

        On promotion the new record is built with:
          - reclaimed_from   = old holder key (audit)
          - reclaim_required = $true (msg-1183 D-6'e: new holder MUST restart the resource)
          - generation       = prior generation + 1 (audit; NOT used by head_skip's skip key in
                               the current wrapper — see msg-1187 §2, but recorded so a future
                               enforcement layer can key on it).

    .PARAMETER Lease
        The per-resource lease hashtable.

    .PARAMETER Now
        UTC tick timestamp.

    .PARAMETER SweepOrderKeys
        As Get-NextLeaseWaiter.

    .PARAMETER Reason
        Free-text stored in `revoked_reason` on the old holder's terminal snapshot.

    .OUTPUTS
        A hashtable with:
          - action           : 'marked-expiring' | 'promoted' | 'released'   ('released' when no waiter)
          - previous_holder  : old holder key (or $null)
          - new_holder       : new holder key (only for 'promoted')
          - lease            : the (mutated) lease hashtable
    #>
    param(
        [hashtable]$Lease,
        [datetime]$Now,
        [string[]]$SweepOrderKeys = @(),
        [string]$Reason = 'idle'
    )
    $result = @{ action = 'noop'; previous_holder = $null; new_holder = $null; lease = $Lease }
    if ($null -eq $Lease) { return $result }
    if (-not $Lease.ContainsKey('holder') -or [string]::IsNullOrEmpty("$($Lease['holder'])")) { return $result }
    $oldHolder = "$($Lease['holder'])"
    $result.previous_holder = $oldHolder

    $expiringNow = [bool]$Lease['expiring']

    if (-not $expiringNow) {
        # Phase 1: mark expiring, record the revocation intent on the old holder, keep the lease.
        $Lease['expiring']       = $true
        $Lease['revoked_at']     = $Now.ToUniversalTime().ToString("o")
        $Lease['revoked_reason'] = $Reason
        $result.action = 'marked-expiring'
        return $result
    }

    # Phase 2 (this tick): actually promote. Decide who takes the lease.
    $nextKey = Get-NextLeaseWaiter -Lease $Lease -SweepOrderKeys $SweepOrderKeys
    $priorGen = 1
    if ($Lease.ContainsKey('generation') -and $null -ne $Lease['generation']) {
        $priorGen = [int]$Lease['generation']
    }

    if (-not $nextKey) {
        # No waiter — release the lease entirely so the next arriving candidate can acquire.
        $Lease.Remove('holder')
        $Lease['acquired_at']       = $null
        $Lease['last_progress_at']  = $null
        $Lease['idle_evaluations']  = 0
        $Lease['generation']        = $priorGen + 1
        $Lease['pinned']            = $false
        $Lease['expiring']          = $false
        $Lease['reclaimed_from']    = $oldHolder
        $Lease['reclaim_required']  = $true
        $Lease['revoked_at']        = $Now.ToUniversalTime().ToString("o")
        $Lease['revoked_reason']    = $Reason
        # Queue stays as-is (empty by definition here).
        $result.action = 'released'
        return $result
    }

    # Promote $nextKey; dequeue it; carry the rest of the queue forward.
    $remaining = @($Lease['queue'] | Where-Object {
        $k = if ($_ -is [hashtable]) { $_['key'] } else { $_.key }
        $k -ne $nextKey
    })
    $Lease['holder']            = $nextKey
    $Lease['acquired_at']       = $Now.ToUniversalTime().ToString("o")
    $Lease['last_progress_at']  = $Now.ToUniversalTime().ToString("o")
    $Lease['idle_evaluations']  = 0
    $Lease['generation']        = $priorGen + 1
    $Lease['pinned']            = $false
    $Lease['expiring']          = $false
    $Lease['reclaimed_from']    = $oldHolder
    $Lease['reclaim_required']  = $true
    $Lease['revoked_at']        = $Now.ToUniversalTime().ToString("o")
    $Lease['revoked_reason']    = $Reason
    $Lease['queue']             = $remaining
    $result.action = 'promoted'
    $result.new_holder = $nextKey
    return $result
}

function Test-LeaseAvailableFor {
    <#
    .SYNOPSIS
        Given a candidate that requires a set of resources, decide whether it may launch this
        tick with respect to leases. Returns one of:
          - 'available' : every required resource is free OR already held by this candidate; the
                          candidate may launch. The caller must then call Invoke-LeaseAcquire on
                          each free one BEFORE launch, so the record shows the acquisition
                          before the session starts (a killed acquire+launch survives as a
                          committed lease, not a phantom launch — same reasoning as head_skip's
                          "session-start-before-write" contract).
          - 'waiting'   : at least one required resource is held by someone else and NOT pinned
                          to this candidate. The candidate must NOT launch; disposition
                          `lease-waiting`. The caller SHOULD call Register-LeaseWaiter to queue.

    .PARAMETER LeasesState
        The full leases.json map: resource-name -> lease hashtable.

    .PARAMETER CandidateKey
        The "$project/$thread_id" key of the candidate.

    .PARAMETER Requires
        Array of resource names the candidate declares in sweep.json's `requires`.

    .OUTPUTS
        A hashtable @{
          status   = 'available' | 'waiting'
          holders  = @{ resource -> current-holder-key } for resources that are held (any holder)
          waitOn   = [string[]] resources this candidate is waiting on
        }
    #>
    param(
        [hashtable]$LeasesState,
        [string]$CandidateKey,
        [string[]]$Requires
    )
    $holders = @{}
    $waitOn = @()
    foreach ($resource in $Requires) {
        if (-not $LeasesState.ContainsKey($resource)) { continue }
        $lease = $LeasesState[$resource]
        if ($null -eq $lease) { continue }
        $h = if ($lease -is [hashtable]) { $lease['holder'] } else { $lease.holder }
        if ([string]::IsNullOrEmpty("$h")) { continue }
        $holders[$resource] = "$h"
        if ("$h" -ne $CandidateKey) { $waitOn += $resource }
    }
    $status = if ($waitOn.Count -eq 0) { 'available' } else { 'waiting' }
    return @{ status = $status; holders = $holders; waitOn = $waitOn }
}

function Invoke-LeaseAcquire {
    <#
    .SYNOPSIS
        Acquire a free lease for a candidate. Precondition: the caller has verified via
        Test-LeaseAvailableFor that the lease is free (no holder) or already held by this
        candidate. Mutates LeasesState in place.

    .PARAMETER LeasesState
        The full leases.json map. Mutated in place — a resource that had no entry gets one.

    .PARAMETER Resource
        Resource name.

    .PARAMETER CandidateKey
        The "$project/$thread_id" acquiring the lease.

    .PARAMETER Now
        UTC tick timestamp.
    #>
    param(
        [hashtable]$LeasesState,
        [string]$Resource,
        [string]$CandidateKey,
        [datetime]$Now
    )
    if (-not $LeasesState.ContainsKey($Resource) -or $null -eq $LeasesState[$Resource]) {
        # Cold start — no record ever existed. Generation begins at 1.
        $LeasesState[$Resource] = New-LeaseRecord -Holder $CandidateKey -Now $Now -Generation 1
        return
    }
    $lease = $LeasesState[$Resource]
    # Normalise PSCustomObject → hashtable so mutation works uniformly. Round-tripped JSON comes
    # back as PSCustomObject, but our probe / mutation code expects hashtable semantics.
    if ($lease -isnot [hashtable]) {
        $normalised = @{}
        foreach ($p in $lease.PSObject.Properties) { $normalised[$p.Name] = $p.Value }
        $lease = $normalised
        $LeasesState[$Resource] = $lease
    }
    $currentHolder = "$($lease['holder'])"
    if ($currentHolder -eq $CandidateKey) {
        # Already held by this candidate — keep the record; the probe will refresh the clocks.
        # This branch is what makes acquire safe to call on every tick the holder is running.
        return
    }
    if (-not [string]::IsNullOrEmpty($currentHolder)) {
        # Called with a non-free lease. Caller violated the precondition — treat as a wiring
        # bug rather than silently overwriting. This is the "no accidental steal" guarantee.
        throw "Invoke-LeaseAcquire on '$Resource' held by '$currentHolder' — refusing to overwrite (call Test-LeaseAvailableFor first)"
    }
    # Reuse the record — preserve `queue` and bump generation. On a fresh acquire after a
    # release, `reclaim_required` from the release survives (the new holder inherits the duty
    # to restart the resource).
    $priorGen = 1
    if ($lease.ContainsKey('generation') -and $null -ne $lease['generation']) {
        $priorGen = [int]$lease['generation']
    }
    $nowIso = $Now.ToUniversalTime().ToString("o")
    $lease['holder']            = $CandidateKey
    $lease['acquired_at']       = $nowIso
    $lease['last_progress_at']  = $nowIso
    $lease['idle_evaluations']  = 0
    $lease['generation']        = $priorGen + 1
    $lease['pinned']            = $false
    $lease['expiring']          = $false
    # `reclaim_required` and `reclaimed_from` are preserved from the prior release.
    $lease['revoked_at']        = $null
    $lease['revoked_reason']    = $null
    # Dequeue this key if present (self-waiters can occur if a candidate was queued then the
    # lease freed up on the same tick).
    Remove-LeaseWaiter -Lease $lease -WaiterKey $CandidateKey
}

function Register-LeaseWaiter {
    <#
    .SYNOPSIS
        Enqueue a waiter for a resource. Creates the resource record if absent (rare — happens
        when a candidate declares `requires: [foo]` for a resource nobody has ever held).
        Idempotent per Add-LeaseWaiter's contract.
    #>
    param(
        [hashtable]$LeasesState,
        [string]$Resource,
        [string]$WaiterKey,
        [datetime]$Now
    )
    if (-not $LeasesState.ContainsKey($Resource) -or $null -eq $LeasesState[$Resource]) {
        # A wait on a resource that has never been leased: create a bare record with no holder
        # so the queue has a place to live. The next candidate to acquire will populate it.
        $LeasesState[$Resource] = @{
            holder            = $null
            acquired_at       = $null
            last_progress_at  = $null
            idle_evaluations  = 0
            generation        = 0
            pinned            = $false
            expiring          = $false
            reclaimed_from    = $null
            reclaim_required  = $false
            revoked_at        = $null
            revoked_reason    = $null
            queue             = @()
        }
    }
    $lease = $LeasesState[$Resource]
    if ($lease -isnot [hashtable]) {
        $normalised = @{}
        foreach ($p in $lease.PSObject.Properties) { $normalised[$p.Name] = $p.Value }
        $lease = $normalised
        $LeasesState[$Resource] = $lease
    }
    Add-LeaseWaiter -Lease $lease -WaiterKey $WaiterKey -Now $Now
}

function ConvertTo-LeaseHashtable {
    <#
    .SYNOPSIS
        Normalise a lease record (possibly a PSCustomObject from JSON round-trip) into a
        hashtable. Idempotent. Used by the runner right after Get-JsonState so the rest of the
        code only ever sees hashtables.
    #>
    param($Lease)
    if ($null -eq $Lease) { return $null }
    if ($Lease -is [hashtable]) { return $Lease }
    $ht = @{}
    foreach ($p in $Lease.PSObject.Properties) { $ht[$p.Name] = $p.Value }
    # Normalise queue entries too.
    if ($ht.ContainsKey('queue') -and $null -ne $ht['queue']) {
        $normQueue = @()
        foreach ($w in @($ht['queue'])) {
            if ($w -is [hashtable]) { $normQueue += $w; continue }
            $wht = @{}
            foreach ($p in $w.PSObject.Properties) { $wht[$p.Name] = $p.Value }
            $normQueue += $wht
        }
        $ht['queue'] = $normQueue
    }
    return $ht
}

function ConvertTo-LeasesStateHashtable {
    <#
    .SYNOPSIS
        Normalise the whole leases.json map to hashtables-of-hashtables. Runs Convert-LeaseHashtable
        on each resource entry.
    #>
    param([hashtable]$LeasesState)
    if ($null -eq $LeasesState) { return @{} }
    foreach ($k in @($LeasesState.Keys)) {
        $LeasesState[$k] = ConvertTo-LeaseHashtable -Lease $LeasesState[$k]
    }
    return $LeasesState
}

function Get-LeaseSummaryLines {
    <#
    .SYNOPSIS
        Render the daily-digest lease section — a snapshot of who holds what, who is waiting,
        and any expiring / recently-reclaimed leases. Emitted even when everything is free
        (silent-day-is-the-point, msg-814 §5 / New-DailyDigest header). Purely read-only —
        writes nothing.

    .PARAMETER LeasesState
        The full leases.json map (normalised to hashtables).

    .PARAMETER Now
        UTC tick timestamp; used for hold-age formatting.

    .PARAMETER FormatDuration
        A scriptblock that takes a [TimeSpan] and returns a display string. Injected because
        Format-DurationDigest lives in the runner and is not dot-sourced here (the runner
        cannot be dot-sourced by tests — same reason StopReason.ps1 exists).
    #>
    param(
        [hashtable]$LeasesState,
        [datetime]$Now,
        [scriptblock]$FormatDuration
    )
    $lines = @()
    $names = @($LeasesState.Keys | Sort-Object)
    if ($names.Count -eq 0) {
        $lines += "  (該当なし)"
        return $lines
    }
    foreach ($name in $names) {
        $lease = ConvertTo-LeaseHashtable -Lease $LeasesState[$name]
        $holder = if ($lease.ContainsKey('holder')) { "$($lease['holder'])" } else { '' }
        $pinned = $lease.ContainsKey('pinned') -and [bool]$lease['pinned']
        $expiring = $lease.ContainsKey('expiring') -and [bool]$lease['expiring']
        $reclaimRequired = $lease.ContainsKey('reclaim_required') -and [bool]$lease['reclaim_required']
        $queue = if ($lease.ContainsKey('queue')) { @($lease['queue']) } else { @() }
        $queueKeys = @()
        foreach ($w in $queue) {
            $k = if ($w -is [hashtable]) { $w['key'] } else { $w.key }
            if ($k) { $queueKeys += $k }
        }
        $ageStr = ''
        if ($lease.ContainsKey('acquired_at') -and $lease['acquired_at']) {
            try {
                $acq = [datetime]::Parse("$($lease['acquired_at'])").ToUniversalTime()
                if ($FormatDuration) { $ageStr = " (保持 $(& $FormatDuration ($Now - $acq)))" }
            } catch { }
        }
        $flags = @()
        if ($pinned)          { $flags += 'pinned' }
        if ($expiring)        { $flags += 'expiring' }
        if ($reclaimRequired) { $flags += 'reclaim-required' }
        $flagStr = if ($flags.Count -gt 0) { "  [" + ($flags -join ',') + "]" } else { '' }
        if ([string]::IsNullOrEmpty($holder)) {
            $lines += "  ${name}: (free)${flagStr}"
        }
        else {
            $lines += "  ${name}: ${holder}${ageStr}${flagStr}"
        }
        if ($queueKeys.Count -gt 0) {
            $lines += "    queue: " + ($queueKeys -join ', ')
        }
        # If a reclaim just happened, surface it: msg-1185 §3-1 "reclaimed: <old> → <new> (reason=...)"
        if ($lease.ContainsKey('reclaimed_from') -and $lease['reclaimed_from']) {
            $reason = if ($lease.ContainsKey('revoked_reason') -and $lease['revoked_reason']) {
                "$($lease['revoked_reason'])"
            } else { 'idle' }
            $lines += "    reclaimed: $($lease['reclaimed_from']) → ${holder} (reason=${reason})"
        }
    }
    return $lines
}
