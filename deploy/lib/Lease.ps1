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
#     "reclaimed_at":      null,       # WHEN the current reclamation happened  (permanent audit)
#     "reclaimed_reason":  null,       # WHY  the current reclamation happened  (permanent audit,
#                                      #   e.g. "operator-clear: PIE crashed", "operator-grant:
#                                      #   emergency", or "idle" for the automatic TTL path).
#                                      #   PAIRED WITH `reclaimed_from`. Set on every write that
#                                      #   changes ownership (promotion/release/operator-grant/
#                                      #   operator-clear); NEVER touched by transient state-machine
#                                      #   transitions (progress-clear-of-expiring, empty-drain,
#                                      #   idempotent self-acquire). The digest reads this.
#     "reclaim_required":  false,      # new holder MUST restart the resource before use
#     "revoked_at":        null,       # transient Phase-1 intent (mark-expiring) — see below
#     "revoked_reason":    null,       # transient Phase-1 intent — CLEARED on progress, on Phase-2
#                                      #   promotion/release, on operator grant/clear, on grant-
#                                      #   from-empty. Do NOT rely on this for audit; use
#                                      #   reclaimed_reason.
#     "queue": [
#       {"key": "spirrow-mindwire/T-x", "waiting_since": "2026-08-26T04:30:00.0000000Z"}
#     ]
#   }
# }
#
# ABSENT resource key = no holder, empty queue. Do NOT create empty stub entries — a resource that
# has never been leased leaves no trace in the file. This keeps the file readable by eye when
# every lease is free (the common case).
#
# --- revoked_* vs reclaimed_* separation (msg-1900 blocker fix) ---------------------------------
# BEFORE this split, `revoked_at` / `revoked_reason` served two conflicting purposes:
#   (a) transient Phase-1 revocation intent (`Update-LeaseFromClassification 'progress'` cleared
#       them so a holder that pre-empted its own revocation reset cleanly);
#   (b) permanent historical audit paired with `reclaimed_from` (the digest renders it as
#       "reclaimed: <from> → <to> (reason=...)" so the operator's intent survives on the display).
# The two collide directly. If an operator ran `Grant-Lease.ps1 -Clear -Reason "PIE crashed"`, the
# reason was written into `revoked_reason` under (b). On the NEXT tick, once someone drained the
# queue or the record entered progress, (a) fired and cleared it — but `reclaimed_from` remained
# populated forever, so the digest fell back to the hardcoded 'idle' and the operator's Tier-C
# audit trail was silently overwritten with a false automated-timeout narrative.
#
# The fix: split into two independent fields.
#   - `revoked_at` / `revoked_reason` = TRANSIENT Phase-1 intent only. Cleared aggressively on
#     progress, on Phase-2 promotion/release (its purpose was served), on empty-drain (any prior
#     intent no longer applies to the new holder), on operator grant/clear (a fresh write
#     supersedes any pending intent).
#   - `reclaimed_at` / `reclaimed_reason` = PERMANENT audit paired with `reclaimed_from`. Set on
#     every write that changes ownership. NEVER cleared by transient state-machine transitions.
#     The digest reads THIS.
# Every write that changes `reclaimed_from` (Invoke-LeasePromotion Phase-2, Invoke-LeaseAcquire
# on new holder, Grant-Lease.ps1 -Grant/-Clear) also writes `reclaimed_reason`. Writes that
# preserve `reclaimed_from` (Invoke-LeaseGrantFromEmpty, operator empty-state re-grant) also
# preserve `reclaimed_reason` — the two travel together.

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
            # Cancel any pending Phase-1 revocation intent. A holder that resumed progress this
            # tick pre-empts the mark-expiring done on a previous tick — the two-phase revoke
            # contract (msg-1183 D-6'd) is that Phase 1 gives the holder ONE tick to come back;
            # if we do not clear `expiring` here, that flag becomes permanent the moment it is
            # first set, and a later idle sequence would find Test-LeaseExpiring true AND
            # `expiring=true`, so Invoke-LeasePromotion jumps straight to Phase 2 on the first
            # eligible tick — the exact 1-tick pre-emption window the design guarantees is
            # bypassed. Reported as msg-1757 blocker #1 (2026-08-26 PR-gate). `revoked_at` and
            # `revoked_reason` are stale audit at that point (they described the CANCELLED
            # revocation, not the new one Phase 1 will record next time), so clear them too.
            $Lease['expiring']       = $false
            $Lease['revoked_at']     = $null
            $Lease['revoked_reason'] = $null
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

    .PARAMETER ReclaimedReason
        Free-text audit paired with `ReclaimedFrom`. Callers set this to describe WHY the
        reclamation happened ("operator-clear: PIE crashed", "operator-grant: emergency", or
        "idle" for the automatic TTL path). Read by the digest (Get-LeaseSummaryLines).
        Separate from `revoked_reason` on purpose (see file-header split rationale, msg-1900):
        this field is PERMANENT audit; `revoked_reason` is TRANSIENT Phase-1 intent that gets
        cleared on progress. Default $null.

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
        [string]$ReclaimedReason = $null,
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
        reclaimed_at      = if ($ReclaimedFrom) { $nowIso } else { $null }
        reclaimed_reason  = $ReclaimedReason
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
        Free-text audit stored in `reclaimed_reason` (permanent, paired with `reclaimed_from`)
        on Phase-2 promotion or release, and in `revoked_reason` (transient Phase-1 intent) on
        Phase 1 mark-expiring. See the file-header revoked_* vs reclaimed_* rationale for why
        the two fields are separate (msg-1900). Default 'idle' — the automatic TTL path.

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

    $nowIso = $Now.ToUniversalTime().ToString("o")

    if (-not $nextKey) {
        # No waiter — release the lease entirely so the next arriving candidate can acquire.
        # Phase-2 write: `reclaimed_*` (PERMANENT audit paired with `reclaimed_from`) captures
        # the operator-visible narrative; `revoked_*` (TRANSIENT Phase-1 intent) is now cleared
        # because Phase 1's intent has been fulfilled (msg-1900 split rationale in file header).
        $Lease.Remove('holder')
        $Lease['acquired_at']       = $null
        $Lease['last_progress_at']  = $null
        $Lease['idle_evaluations']  = 0
        $Lease['generation']        = $priorGen + 1
        $Lease['pinned']            = $false
        $Lease['expiring']          = $false
        $Lease['reclaimed_from']    = $oldHolder
        $Lease['reclaimed_at']      = $nowIso
        $Lease['reclaimed_reason']  = $Reason
        $Lease['reclaim_required']  = $true
        $Lease['revoked_at']        = $null
        $Lease['revoked_reason']    = $null
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
    $Lease['acquired_at']       = $nowIso
    $Lease['last_progress_at']  = $nowIso
    $Lease['idle_evaluations']  = 0
    $Lease['generation']        = $priorGen + 1
    $Lease['pinned']            = $false
    $Lease['expiring']          = $false
    $Lease['reclaimed_from']    = $oldHolder
    $Lease['reclaimed_at']      = $nowIso
    $Lease['reclaimed_reason']  = $Reason
    $Lease['reclaim_required']  = $true
    $Lease['revoked_at']        = $null
    $Lease['revoked_reason']    = $null
    $Lease['queue']             = $remaining
    $result.action = 'promoted'
    $result.new_holder = $nextKey
    return $result
}

function Remove-IneligibleLeaseWaiters {
    <#
    .SYNOPSIS
        Scrub a lease's queue of waiters that are no longer eligible for the resource. Called
        by the probe at the top of each per-resource iteration BEFORE any promotion decision,
        so a promoted waiter is guaranteed to be a currently-launchable candidate.

    .DESCRIPTION
        WHY THIS EXISTS (msg-1875 blocker). A waiter enters a queue when it declares `requires:
        [R]` in sweep.json and R is held by someone else. Between that enqueue and eventual
        promotion, the waiter can become un-launchable in three ways:
          1. Quarantined (non-zero exit on a launch) — the candidate loop will skip it until
             an operator runs Clear-Quarantine.
          2. Removed from sweep.json entirely (thread folded, config change).
          3. `requires:` list edited to drop R (config change).
        If the queue is not scrubbed, `Get-NextLeaseWaiter` will hand any of these back as the
        promotion target. The probe classifies the promoted holder as `parked` (quarantined or
        off-sweep is `parked` per Get-LeaseHolderClassification) and starts incrementing
        `idle_evaluations` — but the wall-clock half of the dual predicate is measured from the
        promotion time. That means the resource stays held by a dead candidate for the full
        `LeaseIdleTtl` (2h) before revocation, silently starving every valid waiter behind it.
        This IS the "silent parked" failure the mechanism exists to end. Scrubbing before
        promotion is the fix — a candidate that cannot launch this tick has no business being
        the next promotion target.

        Held is NOT scrubbed. Held is a transient operator-imposed pause that lifts on the
        operator's own timeline; keeping a held waiter in queue mirrors the probe's treatment
        of a held holder as `progress` (D-6'c) and preserves the "operator's intent survives
        the tick" property throughout the mechanism.

    .PARAMETER Lease
        The per-resource lease hashtable. Its `queue` is mutated in place.

    .PARAMETER EligibleKeys
        The set of "$project/$thread_id" keys still eligible for THIS resource this tick.
        Waiters whose key is not in this set are removed. The caller computes the set (the
        wrapper joins sweep-order keys, quarantine-not-membership, and per-candidate `requires`
        containing this resource).

    .OUTPUTS
        The array of keys that were removed. An empty array means the queue was already clean.
        Callers can iterate this to log a per-waiter audit line.
    #>
    param(
        [hashtable]$Lease,
        [string[]]$EligibleKeys
    )
    if ($null -eq $Lease) { return @() }
    if (-not $Lease.ContainsKey('queue') -or $null -eq $Lease['queue']) { return @() }
    if ($null -eq $EligibleKeys) { $EligibleKeys = @() }

    $droppedKeys = @()
    $newQueue = @()
    foreach ($item in @($Lease['queue'])) {
        $k = if ($item -is [hashtable]) { $item['key'] } else { $item.key }
        if (-not $k) { continue }   # malformed entry — drop silently
        if ($EligibleKeys -contains $k) {
            $newQueue += $item
        }
        else {
            $droppedKeys += $k
        }
    }
    $Lease['queue'] = $newQueue
    return @($droppedKeys)
}

function Invoke-LeaseGrantFromEmpty {
    <#
    .SYNOPSIS
        Promote the FIFO waiter into an already-empty lease. Fills the gap where a lease has no
        holder but a non-empty queue — a state produced by `Grant-Lease.ps1 -Clear` when waiters
        were already registered, or (in theory) by any future path that frees a lease without
        draining its queue.

    .DESCRIPTION
        WHY THIS FUNCTION EXISTS (msg-1852 blocker). `Invoke-LeasePromotion` handles the flow
        "revoke a live holder → promote from queue" (two-phase: mark expiring, then promote).
        `Invoke-LeaseAcquire` handles the flow "candidate arrives, lease is free, take it." What
        was MISSING was the flow "lease is already free, but waiters are queued, promote them
        so the next candidate that arrives does not snipe past FIFO." The probe was skipping
        every empty-holder resource with `continue`, so the queue never drained, and the first
        candidate that arrived acquired the lease directly via Invoke-LeaseAcquire — silently
        cutting in front of anyone who had been waiting for hours.

        The fix is to run this from the out-of-band probe BEFORE its empty-holder skip. If a
        resource has no holder but a non-empty queue, drain the head of the queue into the
        holder slot; the next candidate loop will see the promoted holder and either self-hold
        (if it is the promoted waiter) or lease-wait (if it is not).

    .PARAMETER Lease
        The per-resource lease hashtable. Precondition: `$Lease['holder']` is null or empty.
        Contract violation (non-empty holder) is treated as a wiring bug — the caller must
        gate this on `[string]::IsNullOrEmpty($holderKey)` before calling. Same "no accidental
        steal" invariant as Invoke-LeaseAcquire.

    .PARAMETER Now
        UTC tick timestamp.

    .PARAMETER SweepOrderKeys
        Sweep-order key list for the FIFO tiebreak, as with Invoke-LeasePromotion.

    .OUTPUTS
        A hashtable with:
          - action              : 'granted-from-empty' | 'noop'
          - new_holder          : the promoted waiter's key (only on 'granted-from-empty')
          - reclaim_required    : $true if the previous empty state carried reclaim duty
                                  (Grant-Lease.ps1 -Clear sets this); the caller decides whether
                                  to fire the dirty-acquire notification based on this bit
          - reclaimed_from      : as preserved from the empty record (may be $null)
          - reclaimed_reason    : as preserved from the empty record (may be $null) — the
                                  operator's Clear-reason (or Phase-2 release's 'idle') survives
                                  to the new holder's audit trail (msg-1900 blocker)
    #>
    param(
        [hashtable]$Lease,
        [datetime]$Now,
        [string[]]$SweepOrderKeys = @()
    )
    $result = @{ action = 'noop'; new_holder = $null; reclaim_required = $false; reclaimed_from = $null; reclaimed_reason = $null }
    if ($null -eq $Lease) { return $result }
    $holder = "$($Lease['holder'])"
    if (-not [string]::IsNullOrEmpty($holder)) {
        throw "Invoke-LeaseGrantFromEmpty called on a lease with holder '$holder' — caller must gate on empty holder"
    }
    $nextKey = Get-NextLeaseWaiter -Lease $Lease -SweepOrderKeys $SweepOrderKeys
    if (-not $nextKey) { return $result }   # nothing queued; nothing to do

    # Preserve reclaim duty across the transition. If the empty state was produced by
    # Grant-Lease.ps1 -Clear (which sets reclaim_required=true because the cleared session may
    # still physically hold the resource) or by any other empty-with-reclaim path, the new
    # holder inherits the duty. Reading these before we mutate the record so the caller can
    # decide whether to fire the __lease_reclaim_acquire__ notification.
    $priorReclaim = $Lease.ContainsKey('reclaim_required') -and [bool]$Lease['reclaim_required']
    $priorReclaimedFrom = if ($Lease.ContainsKey('reclaimed_from')) { "$($Lease['reclaimed_from'])" } else { '' }
    # msg-1900 fix: `reclaimed_reason` is the PERMANENT audit paired with `reclaimed_from`. It
    # carries the operator's Clear-reason text ("PIE crashed"), or the Phase-2 release's 'idle',
    # or a prior Grant's reason. Preserve it verbatim across the drain — the digest reads this
    # to render the "reclaimed: <from> → <to> (reason=...)" line, and losing it here would
    # overwrite the operator's Tier-C audit with the hardcoded 'idle' fallback.
    $priorReclaimedReason = if ($Lease.ContainsKey('reclaimed_reason')) { "$($Lease['reclaimed_reason'])" } else { '' }
    $priorGen = 1
    if ($Lease.ContainsKey('generation') -and $null -ne $Lease['generation']) {
        $priorGen = [int]$Lease['generation']
    }

    $remaining = @($Lease['queue'] | Where-Object {
        $k = if ($_ -is [hashtable]) { $_['key'] } else { $_.key }
        $k -ne $nextKey
    })
    $nowIso = $Now.ToUniversalTime().ToString("o")
    $Lease['holder']            = $nextKey
    $Lease['acquired_at']       = $nowIso
    $Lease['last_progress_at']  = $nowIso
    $Lease['idle_evaluations']  = 0
    $Lease['generation']        = $priorGen + 1
    $Lease['pinned']            = $false
    $Lease['expiring']          = $false
    # Preserve reclaim_required, reclaimed_from, and reclaimed_reason from the empty state.
    # The new holder must bounce the resource before use if the previous empty state carried
    # reclaim_required=true; the digest surfaces reclaimed_reason so the operator's audit
    # trail travels alongside the reclaim duty (msg-1900).
    $Lease['reclaim_required']  = $priorReclaim
    if ($priorReclaimedFrom) { $Lease['reclaimed_from'] = $priorReclaimedFrom }
    if ($priorReclaimedReason) { $Lease['reclaimed_reason'] = $priorReclaimedReason }
    # Clear the TRANSIENT Phase-1 revocation annotations — they no longer describe anything.
    # The PERMANENT reclaimed_* fields above carry whatever audit the operator wrote; the
    # Phase-1 revoked_* fields were about a different (now-fulfilled) intent and would be
    # stale noise if left over on the new holder's record. (msg-1900 split rationale.)
    $Lease['revoked_at']        = $null
    $Lease['revoked_reason']    = $null
    $Lease['queue']             = $remaining
    $result.action = 'granted-from-empty'
    $result.new_holder = $nextKey
    $result.reclaim_required = $priorReclaim
    $result.reclaimed_from = $priorReclaimedFrom
    $result.reclaimed_reason = $priorReclaimedReason
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

function Invoke-LeaseReleaseHold {
    <#
    .SYNOPSIS
        Release a lease we successfully acquired but decided NOT to launch on. Used to roll back
        a partial acquire when a multi-resource candidate lost the race on one required resource
        (msg-1955 blocker: hold-and-wait deadlock avoidance).

    .DESCRIPTION
        WHY THIS FUNCTION EXISTS (msg-1955 blocker). `requires` is an array in the sweep.json
        schema (msg-1180 D-1). A candidate that requires more than one resource acquires each
        in memory sequentially, then the wrapper does a pre-spawn Merge+Save (round 8, msg-1877).
        If a concurrent operator Grant stole ONE required resource but not the others, the
        merger lets disk win for the stolen one — but our acquire on the OTHER resources is
        durably written to disk. Without rollback, we'd hold those resources indefinitely
        without launching (we bail via `lease-lost` disposition), causing:
          - Hold-and-wait deadlock: next tick we sit in `lease-waiting` for the stolen one
            while STILL holding the won ones, blocking every other candidate that wants any
            of them for the full LeaseIdleTtl (2h) before revocation kicks in.
          - Violates the mechanism's "all-or-nothing" acquire contract (Test-LeaseAvailableFor
            returns 'available' only if EVERY required resource is free).
        The fix is to release ANY resource we managed to acquire before bailing.

    .PARAMETER Lease
        The per-resource lease hashtable. Idempotent w.r.t. operator override — if we're not
        the current holder (the operator's grant won the merge race), this function is a
        no-op. This makes the caller safe to invoke on every $cand.requires resource without
        pre-filtering, matching how the round-8 durability re-check enumerates.

    .PARAMETER CandidateKey
        The candidate whose acquire is being rolled back. Only released if the current holder
        matches.

    .PARAMETER Now
        UTC tick timestamp — used for the generation bump so downstream mergers see a semantic
        state change (round 4 optimistic-concurrency contract).

    .OUTPUTS
        A hashtable with:
          - action  : 'released-hold' | 'noop-not-holder'
    #>
    param(
        [hashtable]$Lease,
        [string]$CandidateKey,
        [datetime]$Now
    )
    if ($null -eq $Lease) { return @{ action = 'noop-not-holder' } }
    $currentHolder = "$($Lease['holder'])"
    if ($currentHolder -ne $CandidateKey) {
        # Either the operator's grant won the merge race for this resource (so we never held
        # it durably), or the caller invoked us on a resource we didn't acquire. Either way,
        # nothing to release.
        return @{ action = 'noop-not-holder' }
    }
    $priorGen = 1
    if ($Lease.ContainsKey('generation') -and $null -ne $Lease['generation']) {
        $priorGen = [int]$Lease['generation']
    }
    # Release: back to empty-holder state, preserving all PERMANENT audit fields (reclaimed_*,
    # reclaim_required, queue) that describe the record's history before OUR acquire. Bump
    # generation so the end-of-tick merger sees this as a semantic write (in case a concurrent
    # operator write arrives after our roll-back — they'd still compete via the generation
    # comparison in Merge-LeasesStateForWrite).
    $Lease.Remove('holder') | Out-Null
    $Lease['acquired_at']       = $null
    $Lease['last_progress_at']  = $null
    $Lease['idle_evaluations']  = 0
    $Lease['generation']        = $priorGen + 1
    $Lease['expiring']          = $false
    # Preserve reclaimed_from / reclaimed_at / reclaimed_reason / reclaim_required verbatim.
    # These describe the LAST transition of ownership before ours (the ones we inherited on
    # acquire — from a prior release or operator -Clear) — unrelated to our roll-back, so the
    # digest still shows the true reclaim narrative.
    # Clear TRANSIENT Phase-1 markers if any leaked in.
    $Lease['revoked_at']        = $null
    $Lease['revoked_reason']    = $null
    # Queue stays as-is.
    return @{ action = 'released-hold' }
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
    # Precondition: callers normalise the state at the trust boundary. The wrapper runs
    # ConvertTo-LeasesStateHashtable at load; Grant-Lease.ps1 does the same after reading disk;
    # tests build hashtables directly. Re-normalising here (msg-1802 blocker #2) is dead code
    # that also fails to normalise `queue` items, so its "defense in depth" is a lie — drop it
    # and trust the boundary. Same reason the candidate loop's redundant ConvertTo-* call was
    # stripped.
    $lease = $LeasesState[$Resource]
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
    # `reclaim_required`, `reclaimed_from`, and `reclaimed_reason` are preserved from the
    # prior release — they are the PERMANENT audit trail of what happened last (the release's
    # 'idle' or an operator's Clear-reason). Clearing `revoked_*` is safe because those are
    # TRANSIENT Phase-1 revocation intent that no longer applies to a fresh acquire (msg-1900
    # split rationale in file header).
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
            reclaimed_at      = $null
            reclaimed_reason  = $null
            reclaim_required  = $false
            revoked_at        = $null
            revoked_reason    = $null
            queue             = @()
        }
    }
    # See Invoke-LeaseAcquire above — normalisation is the caller's boundary responsibility
    # (msg-1802 blocker #2). Do NOT re-invent it inline here.
    Add-LeaseWaiter -Lease $LeasesState[$Resource] -WaiterKey $WaiterKey -Now $Now
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

function Get-LeaseGenerations {
    <#
    .SYNOPSIS
        Snapshot the per-resource `generation` counter of a leases-state map. Used at sweep
        start so Merge-LeasesStateForWrite can detect an external write (operator running
        Grant-Lease.ps1 mid-sweep) at flush time.

    .PARAMETER LeasesState
        A leases.json map, ideally already normalised via ConvertTo-LeasesStateHashtable.
        Un-normalised entries are tolerated (both hashtable and PSCustomObject shapes) so this
        can be called on the raw disk read too.

    .OUTPUTS
        A hashtable: resource-name -> [int] generation.
    #>
    param([hashtable]$LeasesState)
    $out = @{}
    if ($null -eq $LeasesState) { return $out }
    foreach ($k in @($LeasesState.Keys)) {
        $lease = $LeasesState[$k]
        if ($null -eq $lease) { continue }
        $gen = 0
        if ($lease -is [hashtable]) {
            if ($lease.ContainsKey('generation') -and $null -ne $lease['generation']) { $gen = [int]$lease['generation'] }
        }
        else {
            $prop = $lease.PSObject.Properties['generation']
            if ($prop -and $null -ne $prop.Value) { $gen = [int]$prop.Value }
        }
        $out[$k] = $gen
    }
    return $out
}

function Merge-LeasesStateForWrite {
    <#
    .SYNOPSIS
        Lease-specific merge-on-write. Same operational purpose as the wrapper's
        Merge-StateForWrite (narrow the race between the sweep's tick-long in-memory hold and an
        operator running Grant-Lease.ps1 mid-sweep), but with the correct collision rule for
        leases.json.

    .DESCRIPTION
        WHY THIS EXISTS AND WHY THE GENERIC MERGER IS WRONG FOR THIS FILE. The generic
        Merge-StateForWrite merges at the top-level key boundary and lets in-memory win on any
        key present at sweep start. That is correct for quarantine.json — its top-level keys are
        thread IDs, and an operator running Clear-Quarantine touches a different thread than the
        one the sweep is quarantining, so the collision is structural rather than semantic.

        leases.json is different. Its top-level keys are RESOURCE names — "editor" is one key.
        The sweep's probe mutates that key at tick start (last_progress_at, idle_evaluations,
        expiring). An operator running `Grant-Lease.ps1 -Resource editor -To B` mid-sweep writes
        a new holder to that SAME key on disk. Under the generic merger, sweep memory wins on
        collision — the operator's Tier-C override is silently destroyed. (msg-1802 blocker #1.)

        The fix uses the per-lease `generation` counter as an optimistic-concurrency token.
        Every writer that changes a lease semantically bumps `generation` — Invoke-LeaseAcquire,
        Invoke-LeasePromotion (promoted/released), and Grant-Lease.ps1 (grant/clear) all bump.
        The sweep's probe DOES NOT bump (it only touches the lease clock). So at flush time:

          - If disk's generation for a resource EQUALS the snapshot we took at sweep start, no
            external write has landed against that resource this tick — memory wins (write
            through the sweep's clock updates and any acquire/promote it did).
          - If disk's generation is DIFFERENT (higher — it never decreases), an external writer
            landed a change against fresher state than ours. Their write is a Tier-C human
            decision. Disk wins for that resource: leave the disk value in $out, discard memory.
          - A resource present on disk but not in memory (rare — sweep never adds keys the
            wrapper did not read at start) is preserved from disk.

        THE WINDOW WE DO NOT CLOSE. This narrows the race from "tick duration" (minutes) to
        "the gap between our re-read and our write" (sub-millisecond) — same operational trade
        as Merge-StateForWrite's header describes for quarantine. A full file lock is not the
        right answer for a multi-minute sweep (see Merge-StateForWrite W:255-257).

    .PARAMETER Memory
        The sweep's in-memory leases-state map.

    .PARAMETER OriginalGenerations
        The `generation` snapshot taken at sweep start via Get-LeaseGenerations.

    .PARAMETER DiskPath
        Path to leases.json. Re-read RIGHT BEFORE the write so operator changes during the
        sweep are visible.
    #>
    param(
        [hashtable]$Memory,
        [hashtable]$OriginalGenerations,
        [string]$DiskPath
    )
    if ($null -eq $Memory)              { $Memory = @{} }
    if ($null -eq $OriginalGenerations) { $OriginalGenerations = @{} }

    # Re-read disk. Use the same primitive the wrapper uses so the shape and error behaviour
    # match Merge-StateForWrite exactly (a corrupt state file falls back to empty rather than
    # aborting the flush — the sweep must not fail closed for a JSON syntax hiccup).
    $current = Get-JsonState -Path $DiskPath
    $currentGens = Get-LeaseGenerations -LeasesState $current

    $out = @{}
    foreach ($k in @($current.Keys)) { $out[$k] = $current[$k] }

    foreach ($resource in @($Memory.Keys)) {
        $priorGen = if ($OriginalGenerations.ContainsKey($resource)) { [int]$OriginalGenerations[$resource] } else { -1 }
        $diskGen  = if ($currentGens.ContainsKey($resource))         { [int]$currentGens[$resource]         } else { -1 }
        if ($current.ContainsKey($resource) -and $diskGen -ne $priorGen) {
            # External writer landed on this resource during the sweep — keep disk (already in $out).
            continue
        }
        # No collision (or the resource is new-in-memory) — write memory's version through.
        $out[$resource] = $Memory[$resource]
    }
    return $out
}

function Get-JsonState {
    # Fallback shim: if the caller has NOT dot-sourced the wrapper (e.g. Test-Lease.ps1), we need
    # a Get-JsonState of our own so Merge-LeasesStateForWrite is testable in isolation. When the
    # wrapper IS dot-sourced first, its Get-JsonState wins by shadowing (both are just plain
    # functions — the later definition takes over the name). Keep the shape and error behaviour
    # identical to the wrapper's implementation at run-conductor-scheduled.ps1:200.
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return @{} }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw -Encoding utf8
        if (-not $raw.Trim()) { return @{} }
        $obj = $raw | ConvertFrom-Json
        $map = @{}
        foreach ($p in $obj.PSObject.Properties) { $map[$p.Name] = $p.Value }
        return $map
    }
    catch { return @{} }
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
            # msg-1900 fix: read the PERMANENT `reclaimed_reason` field first — that's the
            # audit trail paired with `reclaimed_from` and preserved across state-machine
            # transitions. Fall back to `revoked_reason` only for backward-compat with
            # records written before the split, then to 'idle' as the final default.
            $reason = if ($lease.ContainsKey('reclaimed_reason') -and $lease['reclaimed_reason']) {
                "$($lease['reclaimed_reason'])"
            }
            elseif ($lease.ContainsKey('revoked_reason') -and $lease['revoked_reason']) {
                "$($lease['revoked_reason'])"
            }
            else { 'idle' }
            $lines += "    reclaimed: $($lease['reclaimed_from']) → ${holder} (reason=${reason})"
        }
    }
    return $lines
}
