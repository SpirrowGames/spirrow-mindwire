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
# leases.json shape (one entry per resource name; v1 ships with `editor` only, but the code is
# resource-agnostic — the sweep candidate's `requires` string, wired in a later PR, is what
# decides which lease a candidate needs, and the runner never hard-codes a name):
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
#                                      #   e.g. "human-clear: PIE crashed", "human-grant:
#                                      #   emergency", or "idle" for the automatic TTL path).
#                                      #   PAIRED WITH `reclaimed_from`. Set on every write that
#                                      #   changes ownership (promotion/release/human-grant/
#                                      #   human-clear); NEVER touched by transient state-machine
#                                      #   transitions (progress-clear-of-expiring, empty-drain,
#                                      #   idempotent self-acquire). The digest reads this.
#                                      #   The `human-*` prefixes name the `human` role from the
#                                      #   role registry (ADR-2026-05-29-10) rather than the
#                                      #   invented `operator` lane the earlier rounds used —
#                                      #   msg-2072 correction.
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
#
# --- what THIS PR ships (T-exclusive-resource-lease-queue PR 2 / 4) -----------------------------
# THIS FILE'S state machine IS STILL INERT — the sweep wrapper does not yet consult
# Test-LeaseAvailableFor before launching a candidate. That wiring lands in PR 4. What ships in
# PR 2 on top of PR 1's persistence contract is:
#   - the holder-classification predicate (Get-LeaseHolderClassification, msg-1187 §4(ii): the
#     ONE predicate the whole design's correctness turns on) and its state-machine update
#     (Update-LeaseFromClassification), plus the dual expiry predicate (Test-LeaseExpiring);
#   - the candidate-loop gate (Test-LeaseAvailableFor + Invoke-LeaseAcquire), including the
#     "self-hold is available" branch and the "no accidental steal" throw that keep acquire
#     from having to know the queue state;
#   - the enqueue primitives (Add-LeaseWaiter / Remove-LeaseWaiter / Register-LeaseWaiter):
#     idempotent append with waiting_since preservation, and the create-record-then-append
#     for a never-leased resource.
# Promotion / grant-from-empty / scrub still live in PR 3, and the wrapper AST wiring + the
# probe activation land in PR 4.
#
# THE READER COLLAPSE (msg-2151 measurement + msg-2172 Tier-C, 2026-08-28). Get-JsonState is now
# the CANONICAL state-file reader for the whole runner. The wrapper's previous inline
# Get-JsonState is gone; the wrapper dot-sources this file and calls THIS function for every
# state-file read (notify.json, pending-decisions.json, quarantine.json, evaluated.json,
# digest.json, head_skip.json, and leases.json when PR 4 wires it). The collapse eliminates the
# duplicate-reader-drift risk called out in R2/R3 PR-gate reviews AND lets the shape guard added
# here protect every state file the sweep reads. Rationale in Bohr msg-1916 §3: the header
# previously claimed "behaviourally identical to the wrapper's inline reader"; keeping two
# readers just to make that claim true was bureaucratic negligence — collapsing to one makes the
# claim moot.
#
# THE SHAPE GUARD (msg-2151 measurement, Bohr msg-1916 §1 / §2, Einstein msg-1915 endorse). Root
# JSON arrays (`[{"editor":"x"},{"foo":"y"}]`) and root scalars (`"just a string"`) round-trip
# through the old reader as OBJECTS with array/string metadata keys (Count, IsFixedSize, Length,
# LongLength, Rank, ...). Those keys landed on disk on the next flush and were then read back on
# the NEXT tick as "resources" — a one-way corruption vector one operator typo could open
# permanently. The 2026-08-28 measurement confirmed permanent landing for multi-element arrays,
# scalar arrays, and root strings (cases B/D/E of `.git/mindwire-scratch/array-shape-probe-v2.ps1`,
# archived alongside this PR). The guard now returns empty for any non-PSCustomObject root, so
# no metadata leaks into the memory map. The `.bad-<utc>` backup (Save-CorruptedStateBackup) is
# the paired side effect: the flush caller renames the offending file before overwriting it,
# preserving the operator's forensic trail rather than converting a typo into a silent deletion.
# The Read-JsonStateWithShape helper exposes the shape verdict to that caller without a second
# file read; Get-JsonState is a thin wrapper that discards the verdict for backward-compat.
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
            # `reclaimed_*` are PERMANENT audit (msg-1900 split) and MUST NOT be cleared here —
            # progress is a transient state-machine transition, not an ownership change.
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
        reclamation happened ("human-clear: PIE crashed", "human-grant: emergency", or
        "idle" for the automatic TTL path). The `human-*` prefixes name the `human` role from
        the role registry (ADR-2026-05-29-10) — the actor that invokes Grant-Lease.ps1 — rather
        than an invented `operator` category (msg-2072 correction).
        Read by the digest (Get-LeaseSummaryLines). Separate from `revoked_reason` on purpose
        (see file-header split rationale, msg-1900): this field is PERMANENT audit;
        `revoked_reason` is TRANSIENT Phase-1 intent that gets cleared on progress. Default $null.

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
        Append a waiter to a lease's queue in append order. Idempotent: a waiter that is
        already in the queue is left untouched — the record is not duplicated, and its
        original `waiting_since` is PRESERVED (NOT refreshed).

    .DESCRIPTION
        Why waiting_since is preserved on a re-enqueue (this IS the contract):
          The sweep tick runs every ~5 min. If a candidate declares `requires: editor` and the
          lease is held, the wrapper calls Register-LeaseWaiter → Add-LeaseWaiter on it every
          tick until the lease frees. Refreshing `waiting_since` on those repeat calls would
          reset the wait clock every 5 min, so the waiter that arrived first would never age
          past a fresher one — grant-time FIFO (msg-1183 D-3) collapses to "the last one to
          re-attempt wins". The idempotent-with-preserve behaviour is what makes "waiting_since
          FIFO" a real ordering rather than sweep-tick noise. The regression test §8
          ("first enqueue's waiting_since is NOT refreshed") pins this literally.

        NOTE: msg-1183 D-3 says grant is waiting-time FIFO, sweep order is the tiebreak. The
        tiebreak resolves at grant time (Get-NextLeaseWaiter, PR 3), not here — this function
        only preserves append order and the `waiting_since` of pre-existing entries.

    .PARAMETER Lease
        The per-resource lease hashtable. Mutated in place; a missing `queue` key is
        initialised to an empty array. Callers that need the resource RECORD to exist first
        (rather than just the queue slot) should use Register-LeaseWaiter, which wraps this
        function.

    .PARAMETER WaiterKey
        The waiter's "$project/$thread_id" key.

    .PARAMETER Now
        The tick's UTC timestamp; used ONLY when this is a fresh enqueue. Ignored on the
        idempotent-repeat branch — see the discussion above.

    .NOTES
        Fixed in PR-gate R1 (msg-2181): the prior SYNOPSIS falsely claimed "or refresh its
        `waiting_since` if it is already there", which directly contradicted the code and
        the test. Docstrings on this file are normative (they carry design invariants that
        callers rely on), so a wrong SYNOPSIS is a real contract break — a later refactor
        reading the docstring as truth could add a `waiting_since` refresh and silently
        break FIFO. The naysayer was right to block on this.
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
        # Already queued: return WITHOUT touching `waiting_since` — this is the FIFO-preserving
        # branch the SYNOPSIS above documents. Refreshing would reset the wait clock every ~5-min
        # sweep tick and collapse FIFO to "last re-attempt wins" (msg-2181 blocker fix).
        if ($k -eq $WaiterKey) { return }
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

function Test-LeaseAvailableFor {
    <#
    .SYNOPSIS
        Given a candidate that requires a resource, decide whether it may launch this tick with
        respect to leases. Returns one of:
          - 'available' : the required resource is free OR already held by this candidate; the
                          candidate may launch. The caller must then call Invoke-LeaseAcquire on
                          the free one BEFORE launch, so the record shows the acquisition
                          before the session starts (a killed acquire+launch survives as a
                          committed lease, not a phantom launch — same reasoning as head_skip's
                          "session-start-before-write" contract).
          - 'waiting'   : the required resource is held by someone else and NOT this candidate.
                          The candidate must NOT launch; disposition `lease-waiting`. The caller
                          SHOULD call Register-LeaseWaiter to queue.

        v1 is single-resource per candidate (msg-2038 correction: sweep.json's `requires` is a
        single string, not an array). Multi-resource coordination + rollback is v2 work that
        needs proper deadlock avoidance — bolting it onto a single-resource state machine is what
        rounds 11-12 tried and had to be reverted.

    .PARAMETER LeasesState
        The full leases.json map: resource-name -> lease hashtable.

    .PARAMETER CandidateKey
        The "$project/$thread_id" key of the candidate.

    .PARAMETER Requires
        Array of resource names the candidate declares in sweep.json's `requires`. v1 will only
        ever pass zero or one element; the array-typed parameter is kept for the internal loop's
        clarity (0-element = 'available' trivially).

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
        when a candidate declares `requires: foo` for a resource nobody has ever held).
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

    .NOTES
        Cast safety. An operator hand-editing leases.json may leave `generation` as a string
        (`"generation": ""`, `"generation": "pending"`) — structurally valid JSON that
        ConvertFrom-Json accepts, but a hard `[int]$value` cast would raise a RuntimeException
        and bubble out of Merge-LeasesStateForWrite, aborting the flush. That directly violates
        the "corrupt state does not fail closed" invariant Get-JsonState's header commits to.
        We use `-as [int]` instead, which returns $null on any un-parseable value; the fallback
        is 0, treated the same as "generation not recorded", which lets the merger fall through
        to its normal collision detection instead of collapsing the sweep.
    #>
    param([hashtable]$LeasesState)
    $out = @{}
    if ($null -eq $LeasesState) { return $out }
    foreach ($k in @($LeasesState.Keys)) {
        $lease = $LeasesState[$k]
        if ($null -eq $lease) { continue }
        $gen = 0
        $rawGen = $null
        if ($lease -is [hashtable]) {
            if ($lease.ContainsKey('generation')) { $rawGen = $lease['generation'] }
        }
        else {
            $prop = $lease.PSObject.Properties['generation']
            if ($prop) { $rawGen = $prop.Value }
        }
        if ($null -ne $rawGen) {
            $parsed = $rawGen -as [int]
            if ($null -ne $parsed) { $gen = $parsed }
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
          - If disk's generation is DIFFERENT, an external writer landed a change against fresher
            state than ours. Their write is a Tier-C human decision. Disk wins for that resource:
            leave whatever is (or is not) in $out, discard memory. Two shapes of "different" both
            resolve this way:
              (a) mutation — the key is still on disk with a bumped generation (e.g. operator ran
                  `Grant-Lease.ps1 -To`). The disk value is already in $out from the copy-through.
              (b) DELETION — the operator emptied the lease AND cleared the queue, so per the
                  schema (file header) the key is absent. $currentGens does not have the resource,
                  so its diskGen is -1 while priorGen is still the pre-sweep value — still a
                  mismatch. $out has no entry to leave alone; the key correctly stays absent,
                  matching the operator's intent.
            The read `if ($current.ContainsKey($resource) -and $diskGen -ne $priorGen)` that this
            file used to carry silently missed case (b) and resurrected the deleted lease from
            stale memory. The current condition tests generation mismatch alone.
          - A resource present on disk but not in memory (rare — sweep never adds keys the
            wrapper did not read at start) is preserved from disk.
          - A resource the sweep held at tick start (present in $OriginalGenerations) but has
            REMOVED from memory this tick is the mirror of case (b): the sweep freed the lease
            and drained its queue, and the schema commits to no empty stub entries. A second
            pass, run after the Memory-keys loop, honours the deletion — .Remove()s the key from
            $out — unless the disk generation has advanced (an external writer landed
            concurrently), in which case the external write wins on the same generation tie-
            break rule as the mutation path.

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
        if ($diskGen -ne $priorGen) {
            # External writer landed on this resource during the sweep. Two shapes both surface as
            # a generation mismatch:
            #   (a) mutation — disk still has the key, with a bumped generation (operator ran
            #       Grant-Lease.ps1 -To / -Clear-that-keeps-a-record). The disk value is already in
            #       $out from the copy-through above.
            #   (b) DELETION — disk no longer has the key at all. The schema (file header) makes
            #       an absent key equivalent to a freed lease with an empty queue, so an operator
            #       Clear that empties both may legitimately remove the key. In that case
            #       $currentGens does not have the resource, so $diskGen is -1 while $priorGen is
            #       the pre-sweep value (e.g. 5) — still a mismatch, still "external writer wins",
            #       but $out has no entry to leave alone. `continue`ing here correctly leaves the
            #       key absent from $out — which is the operator's intent.
            # Without dropping the earlier `$current.ContainsKey($resource)` guard, case (b) would
            # fall through and silently resurrect the deleted lease from stale memory.
            continue
        }
        # No collision (or the resource is new-in-memory: priorGen == diskGen == -1) — write
        # memory's version through. If memory added a new resource this tick, this is where it
        # first lands on disk.
        $out[$resource] = $Memory[$resource]
    }

    # Sweep-side deletion (msg-2131 blocker). The schema commits: "ABSENT resource key = no
    # holder, empty queue. Do NOT create empty stub entries." When the sweep frees a lease and
    # drains its queue (TTL expiry that promotes no waiter, operator-Clear followed by empty
    # queue, etc.), the caller is required to REMOVE the key from its in-memory map — not to
    # leave a stub. That deletion has to persist through the merger.
    #
    # The Memory-keys loop above cannot detect it: a deleted key is absent from Memory.Keys and
    # so is never iterated. Without this second pass, $out silently keeps the old disk state and
    # the sweep's deletion is dropped — the mirror of the R1 (external-deletion) bug.
    #
    # Rule: for every key the sweep saw at tick start (present in $OriginalGenerations) that is
    # now absent from Memory,
    #   - if disk's generation is unchanged (no external write), honour the sweep's deletion —
    #     .Remove() from $out.
    #   - if disk's generation has advanced (an external writer landed), external wins — leave
    #     whatever the disk copy-through put in $out. Same tie-break rule as the mutation path.
    #   - if the resource is also absent from disk now (external deleter also dropped it), $out
    #     never had it, and .Remove() on an absent key is a no-op — the operator and the sweep
    #     agree, and the key stays absent.
    foreach ($resource in @($OriginalGenerations.Keys)) {
        if ($Memory.ContainsKey($resource)) { continue }   # handled by the Memory-keys loop
        $priorGen = [int]$OriginalGenerations[$resource]
        $diskGen  = if ($currentGens.ContainsKey($resource)) { [int]$currentGens[$resource] } else { -1 }
        if ($diskGen -eq $priorGen) {
            # No external write during the sweep — the sweep's deletion is authoritative.
            $out.Remove($resource) | Out-Null
        }
        # else: external writer landed; disk value is already in $out (or absent from $out if
        # they also deleted). Leave whichever is there.
    }

    return $out
}

function Read-JsonStateWithShape {
    <#
    .SYNOPSIS
        Canonical state-file reader — returns both the parsed map AND a shape verdict, so the
        flush caller can decide whether to back up a bad file before overwriting it. Used by
        Get-JsonState (which discards the verdict for backward-compat) and by any caller that
        wants to know WHY the map came back empty.

    .DESCRIPTION
        WHY THE SHAPE VERDICT EXISTS. `ConvertFrom-Json` accepts three root kinds — objects,
        arrays, and scalars. The old reader walked `.PSObject.Properties` on any of them, which
        for an array root yielded array metadata (Count, IsFixedSize, IsReadOnly, IsSynchronized,
        Length, LongLength, Rank, SyncRoot) as if they were resource names, and for a string root
        yielded `Length`. The 2026-08-28 measurement (msg-2151 §1, archived at
        `.git/mindwire-scratch/array-shape-probe-v2.ps1`) confirmed these metadata keys land on
        disk after a flush and are then read back as resources on the NEXT tick — a one-way
        corruption vector triggered by a single operator typo (e.g. wrapping the file in a JSON
        array). Reviewer's phrasing (msg-1912 weakest-point): "fails open safely enough to not
        block the pipeline"; Bohr msg-1916 §1's correction: "tick 単位では fail-open、tick を跨ぐと
        one-way corruption". The shape guard rejects array / scalar roots up front — empty state,
        no metadata leak, no permanent poison — and the flush caller uses the shape verdict to
        decide whether to rename the offending file to `.bad-<utc>` before its own write
        overwrites the evidence.

        THE SIDE-EFFECT BOUNDARY (PR-gate R2/R3 endorse: pure verdicts). This function reads
        only; the rename is Save-CorruptedStateBackup, invoked by the flush path (Bohr msg-1916
        §1 design: "reader は「shape/parse で reject した」という事実を戻り値で伝えるだけにし、
        rename は呼び出し側（flush path）が行う"; Einstein msg-1917 endorse). Wiring the backup
        into Merge-LeasesStateForWrite would collapse the "one file, one owner" property that
        R2/R3 explicitly endorsed.

    .OUTPUTS
        A hashtable @{ state = @{...}; shape = 'missing'|'empty'|'object'|'array'|'scalar'|'parse-error'; error = $null|<msg> }.
        Shape values:
          - 'missing'      : the path does not exist. state = @{}.
          - 'empty'        : the file exists but is blank / whitespace, OR ConvertFrom-Json
                             returned $null (empty JSON array `[]`). state = @{}.
          - 'object'       : root is a JSON object → normalised to hashtable. state = the map.
          - 'array'        : root is a JSON array (multi-element or scalar). state = @{}.
                             THE CORRUPTION CASE — caller MUST back up.
          - 'scalar'       : root is a JSON string / number / boolean. state = @{}.
                             THE CORRUPTION CASE — caller MUST back up.
          - 'parse-error'  : ConvertFrom-Json threw. state = @{}, error = exception message.
                             AMBIGUOUS: could be a partial write in flight, could be operator
                             mid-edit, could be genuine corruption. The flush caller's policy
                             (msg-1916 §2) is to back up here too, on the same "do not convert a
                             weird file into a deleted file" grounds.
    #>
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return @{ state = @{}; shape = 'missing'; error = $null }
    }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw -Encoding utf8
        if (-not $raw.Trim()) {
            return @{ state = @{}; shape = 'empty'; error = $null }
        }
        $obj = $raw | ConvertFrom-Json
        if ($null -eq $obj) {
            # `[]` parses successfully to $null in PowerShell 7. Treat as empty rather than array
            # — there is no metadata to leak, and no forensic value in preserving `[]` alone.
            return @{ state = @{}; shape = 'empty'; error = $null }
        }
        # Type discrimination MUST NOT go through `-is [PSCustomObject]` here. ConvertFrom-Json
        # wraps every scalar return in a PSObject shell for pipeline semantics, and PowerShell's
        # `-is [PSCustomObject]` matches that shell — so `("str" | ConvertFrom-Json) -is
        # [PSCustomObject]` is $true and a root JSON string `"foo"` would slip through the object
        # branch, whose Property-walk then picks up System.String.Length as a "resource" named
        # `Length`. The 2026-08-28 measurement caught this on case E. Use `.GetType()` on the
        # unwrapped object instead — a JSON `{}` root returns a PSCustomObject VALUE (that IS
        # the unwrapped type) while a JSON `"foo"` root returns a String VALUE.
        $actualType = $obj.GetType()
        if ($actualType -eq [System.Management.Automation.PSCustomObject]) {
            $map = @{}
            foreach ($p in $obj.PSObject.Properties) { $map[$p.Name] = $p.Value }
            return @{ state = $map; shape = 'object'; error = $null }
        }
        # PowerShell's ConvertFrom-Json unwraps single-element arrays to their inner element on
        # 7.x, so a lone `[{"editor":"x"}]` reaches the PSCustomObject branch above rather than
        # this one. Multi-element arrays and scalar-arrays land here (System.Object[]).
        if ($actualType.IsArray -or $obj -is [System.Collections.IList]) {
            return @{ state = @{}; shape = 'array'; error = $null }
        }
        # Root strings / numbers / booleans — anything left is a scalar.
        return @{ state = @{}; shape = 'scalar'; error = $null }
    }
    catch {
        return @{ state = @{}; shape = 'parse-error'; error = $_.Exception.Message }
    }
}

function Get-JsonState {
    <#
    .SYNOPSIS
        Canonical state-file reader for the scheduled sweep. Returns a hashtable map of the
        top-level JSON object at `-Path`, or an empty map on any read failure (missing / empty /
        parse error / bad shape).

    .DESCRIPTION
        HISTORY. Before 2026-08-28 this function was a fallback shim that Test-Lease.ps1 needed
        because the wrapper's inline reader (`run-conductor-scheduled.ps1:200`) was the live one.
        The reader collapse ordered by msg-2172 replaces that wrapper reader with a dot-source of
        this file, so THIS function is now the canonical reader for every state file the sweep
        reads (notify.json, pending-decisions.json, quarantine.json, evaluated.json, digest.json,
        head_skip.json, and — once PR 4 wires it — leases.json). The parity comment the earlier
        rounds carried is retired: there is only one reader now.

        WHAT CHANGED WITH THE COLLAPSE. Two behaviours were folded in:
          - Shape guard (Read-JsonStateWithShape docstring for the full rationale): array and
            scalar JSON roots now return empty rather than leaking metadata keys.
          - Wrapper-side logging: when a call to Write-Log resolves in the caller's scope, the
            function emits a one-line log entry on parse errors / bad shapes so operators still
            see the "state file unreadable — treating as empty" signal the wrapper had. Test-
            Lease.ps1 has no Write-Log; the Get-Command probe stays silent in that case.

    .PARAMETER Path
        Absolute or repo-relative path to a UTF-8 JSON file.

    .OUTPUTS
        A hashtable. Empty on any read failure or bad shape.

    .NOTES
        The `.bad-<utc>` file rename (Save-CorruptedStateBackup) is a SEPARATE side effect,
        invoked by the flush caller after inspecting the shape via Read-JsonStateWithShape.
        This function stays pure so it is safe to call from anywhere (probes, digests, tests).
    #>
    param([string]$Path)
    $r = Read-JsonStateWithShape -Path $Path
    if ($r.shape -in @('array', 'scalar', 'parse-error')) {
        # Opportunistic log-through: when the caller's scope has a Write-Log function (the
        # wrapper does; Test-Lease.ps1 does not), surface the reason we returned empty.
        # Otherwise silent — the caller opted out of logging by not defining it.
        if (Get-Command Write-Log -ErrorAction SilentlyContinue) {
            $reason = if ($r.shape -eq 'parse-error') {
                "parse error: $($r.error)"
            } else {
                "root is a JSON $($r.shape), not an object"
            }
            Write-Log "state file unreadable ($Path): $reason — treating as empty"
        }
    }
    return $r.state
}

function Save-CorruptedStateBackup {
    <#
    .SYNOPSIS
        Rename a corrupt state file to `<Path>.bad-<utc>` so the next flush cannot overwrite it.
        Called by the flush path AFTER Read-JsonStateWithShape reports a shape in
        {array, scalar, parse-error} — the three cases where the operator's file is preserved
        as forensic evidence rather than silently destroyed.

    .DESCRIPTION
        WHY THIS EXISTS. The shape guard alone prevents metadata keys from leaking into the
        merged state map (the corruption vector). But the sweep flushes memory to the same path
        on the next write, and memory is empty for that resource, so the operator's original
        typo — the evidence — is silently overwritten with a clean `{}`. That converts a
        recoverable operator typo into an irrecoverable silent deletion, and produces the same
        "we ate your Tier-C edit" failure class R1/R4 were written to end. The rename gives the
        operator a name they can grep for (`leases.json.bad-2026-08-28T04-15-32Z`) and gives the
        sweep a clean disk to start from.

        WHY THIS ISN'T IN THE READER (Bohr msg-1916 §1 design boundary; Einstein msg-1917
        endorse). Read-JsonStateWithShape reports the verdict; the rename is a side effect on a
        different file (and creates a new one). Doing the rename inside the reader would violate
        the "pure mechanism, one owner" boundary R2/R3 explicitly endorsed. The flush caller (in
        PR 4, when the wrapper is wired) inspects the shape and invokes this before its own
        write.

    .PARAMETER Path
        The state file to rename.

    .OUTPUTS
        The renamed path on success (a String), or $null when the file was already gone (nothing
        to preserve — a race with an external delete, harmless to no-op).
    #>
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    # Filename-safe UTC stamp — colons are invalid in Windows filenames, and file managers show
    # T/Z/- fine. The stamp resolves to the second, which is enough for forensic pairing (the
    # sweep runs on a 5-minute cadence — sub-second collisions cannot happen).
    $stamp = [datetime]::UtcNow.ToString("yyyy-MM-ddTHH-mm-ssZ")
    $badPath = "$Path.bad-$stamp"
    Move-Item -LiteralPath $Path -Destination $badPath
    return $badPath
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
    # Cast safety (msg-2114 blocker follow-on to the [int] fix in Get-LeaseGenerations). A plain
    # `[bool]$value` promotes any non-empty string — including the literal `"false"` — to $true,
    # because PowerShell's [bool] cast is length-based on strings. If an operator hand-edited
    # leases.json with `"pinned": "false"`, the digest would render `[pinned]` for a lease that
    # is not pinned; worse, `"pinned": "true"` and `"pinned": "false"` become indistinguishable
    # on the display. The fix accepts only real booleans as true and interprets recognised
    # string values ("true"/"yes"/"1", "false"/"no"/"0", case-insensitive). Anything else falls
    # back to $false — the safe default (a stray flag stays off; the operator sees the wrong
    # cell being blank rather than being lied to about its state).
    $coerceBool = {
        param($v)
        if ($null -eq $v) { return $false }
        if ($v -is [bool]) { return [bool]$v }
        if ($v -is [string]) {
            $s = $v.Trim().ToLowerInvariant()
            if ($s -in @('true','yes','1'))  { return $true }
            if ($s -in @('false','no','0','')) { return $false }
            return $false
        }
        # Numbers: 0 -> false, non-zero -> true. Anything else (arrays, hashtables) -> false.
        $n = $v -as [int]
        if ($null -ne $n) { return ($n -ne 0) }
        return $false
    }
    foreach ($name in $names) {
        $lease = ConvertTo-LeaseHashtable -Lease $LeasesState[$name]
        # Null-lease guard (msg-2124 blocker follow-on). An operator hand-editing leases.json can
        # leave `"editor": null` as a placeholder. Get-JsonState round-trips that as $null;
        # ConvertTo-LeaseHashtable propagates the $null upward, and PowerShell will raise a
        # RuntimeException on the very next `.ContainsKey()` call — aborting the whole digest
        # render. Same failure class as the R2 [int]/[bool] casts: a corrupt scalar takes the
        # sweep down with it. Skip the entry (it renders nothing) rather than fail closed. The
        # merger already treats a null lease as "no data" via the null-guard in
        # Get-LeaseGenerations, so the two behaviours are consistent.
        if ($null -eq $lease) { continue }
        $holder = if ($lease.ContainsKey('holder')) { "$($lease['holder'])" } else { '' }
        $pinned = $lease.ContainsKey('pinned') -and (& $coerceBool $lease['pinned'])
        $expiring = $lease.ContainsKey('expiring') -and (& $coerceBool $lease['expiring'])
        $reclaimRequired = $lease.ContainsKey('reclaim_required') -and (& $coerceBool $lease['reclaim_required'])
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
