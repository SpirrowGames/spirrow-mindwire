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
# PR 3 landed rows #1-9 (see the checklist at the top of tests/Test-Lease.ps1 — that is the
# single source of truth per row #9b). The Lease.ps1 changes this file carries for PR 3 are:
#   - the positive entry validation on both -Resource (Invoke-LeaseAcquire) and -Requires
#     (Test-LeaseAvailableFor), implemented via the shared Assert-LeaseResourceName helper below
#     (row #6, msg-2932 §3 supersedes the row #5-only mirror described in msg-2189);
#   - the .OUTPUTS docstring on Test-LeaseAvailableFor that spells out the asymmetric
#     contract 'available' = advisory / 'waiting' = binding and folds the "free OR already
#     held by this candidate" collapse rationale into it (row #5, msg-2644 §2 supersedes the
#     older "lease check before Invoke-HeadSkipCommitLaunch" phrasing that failed to
#     distinguish predicate from mutation, and msg-2738 §5 correction 1 pins that row #5 is
#     documentation-only — the verdict domain does not change);
#   - the grant seam (Get-NextLeaseWaiter / Invoke-LeasePromotion / Invoke-LeaseGrantFromEmpty
#     / Remove-IneligibleLeaseWaiters) that the sweep tick will call in PR 4.
# Row #9a (msg-2738 §3) explicitly required that the prior "PR 3 open items" tracker prose in
# THIS file be DELETED rather than shrunk to a pointer once PR 3 lands — a "PR 3 open items"
# header sitting above finished code reads as unfinished obligation. It is now gone.
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

function Assert-LeaseResourceName {
    <#
    .SYNOPSIS
        POSITIVE-ALLOWLIST entry validation for a resource-name parameter. Throws on ANY input
        that is not a plain, non-null, non-empty, non-whitespace [string]. Returns the unwrapped
        scalar string on success.

    .DESCRIPTION
        WHY THIS EXISTS (row #6 revised body, msg-2932 §3 supersedes the msg-2189-only mirror
        described in the old row #6 wording). PowerShell's parameter binder silently coerces
        a `[string]` annotation into misleading shapes at TWO different layers:

          layer 1 (msg-2189, still true): `[string]$Requires` on Test-LeaseAvailableFor let the
            binder join `@('editor','runner')` into the space-separated phantom name
            'editor runner' BEFORE the body ran. A `[string]` annotation is not the hard-runtime
            rejection it looks like — the type check happens after coercion, so a multi-element
            array becomes a nonsense string that no lease matches, and the caller reads that as
            'available'. Two candidates then race on the same real resource, and mutual exclusion
            has silently collapsed (msg-2189 R3 phantom lock).

          layer 2 (msg-2746 blocking objection). The natural fix to layer 1 — strip `[string]`
            so the binder cannot join arrays — WIDENS the accepted type from [string] to [object].
            $null, $true, $false, $42, `[pscustomobject]@{...}` all now flow through untouched.
            A blocklist reading of the msg-1961 wording ("multi-element array / hashtable /
            integer MUST throw") would let $null / $true / $false / whitespace-only strings /
            arbitrary objects slip past — [bool] most dangerously, because $true becomes the
            hashtable key "True" and locks a phantom resource EXACTLY as 'editor runner' did.

        The fix (msg-2932 §3 #6a): a POSITIVE allowlist. The parameter is ACCEPTED if and only
        if, after unwrapping any PSObject shell, it is an instance of [string] AND not $null
        AND not empty AND not whitespace-only. EVERYTHING ELSE THROWS. No exceptions, no
        coercion, no defaulting. The set of types PowerShell can hand you is open — a blocklist
        would silently accept the next unlisted type as soon as it existed.

        The PSObject unwrap step is required because pipeline / splat callsites can wrap a real
        string in a PSObject shell (`ConvertFrom-Json` does this to every scalar, for instance).
        `.BaseObject` gives the unwrapped value; the `-is [string]` check then answers the real
        question. Without the unwrap, a legitimate string arriving from `ConvertFrom-Json`
        would fail the `-is [string]` test even though the underlying value IS a string.

    .PARAMETER Value
        The raw parameter value handed by the binder.

    .PARAMETER ParamName
        The PARAMETER NAME to include in the exception message (e.g. 'Resource', 'Requires').

    .PARAMETER FunctionName
        The FUNCTION NAME to include in the exception message (e.g. 'Invoke-LeaseAcquire').

    .OUTPUTS
        The unwrapped scalar [string] on success. Throws on any invalid input; the exception
        message includes the actual type PowerShell handed the function and the ParamName /
        FunctionName so callers can grep the failure back to their site.

    .NOTES
        Row #6a mandates that a call path reaching Invoke-LeaseAcquire WITHOUT first calling
        Test-LeaseAvailableFor MUST hit the same validation (msg-1961: authorisation lives in
        the mutation, not the predicate — msg-1959). Sharing the validator between the two
        functions is what makes that mandate hold structurally: neither function can ever
        drift into a weaker check without editing the shared helper, which is impossible to
        do accidentally.
    #>
    param(
        $Value,
        [string]$ParamName,
        [string]$FunctionName
    )
    # Unwrap PSObject shell so a string carried through a pipeline / splat is judged on its
    # underlying type, not the shell's. `ConvertFrom-Json` wraps every scalar this way, so a
    # value legitimately arriving from disk state could look like [PSObject] at first glance.
    #
    # SUBTLE UNWRAP HAZARD (layer 3, msg-2932 §3 #6a implementation note). An earlier iteration
    # of this helper wrote `$unwrapped = if (…) { $Value.PSObject.BaseObject } else { $Value }`.
    # That LOOKED correct but the `if` was used as an EXPRESSION on the right-hand side of the
    # assignment. PowerShell evaluates that shape by running the chosen branch through the
    # PIPELINE, which UNROLLS a single-element array into its scalar element before assignment.
    # ∴ `@('editor')` arrived as [object[]] with Count=1, went through the else branch, and
    # `$unwrapped` was assigned the STRING 'editor' — the single-element array was silently
    # coerced to a plain string BEFORE the -is [string] rejection could see it, and the
    # subsequent validation passed. This is the layer-3 hole that repeats msg-2189 layer-1 in
    # a different dialect: an implicit unwrap happens outside the function body and defeats the
    # validation. The row #6a rejected-set pin for the single-element array case caught this.
    #
    # Fix: use STATEMENT-form if/else with direct assignments inside each branch. Direct
    # assignment does NOT go through the pipeline, so the array shape survives untouched.
    $unwrapped = $Value
    if ($null -ne $Value -and $Value -is [System.Management.Automation.PSObject]) {
        $unwrapped = $Value.PSObject.BaseObject
    }
    if ($null -eq $unwrapped) {
        throw "${FunctionName}: -${ParamName} must be a non-empty [string] (msg-2932 §3 #6a positive-allowlist entry validation); got `$null"
    }
    if (-not ($unwrapped -is [string])) {
        throw "${FunctionName}: -${ParamName} must be a non-empty [string] (msg-2932 §3 #6a positive-allowlist entry validation); got [$($unwrapped.GetType().FullName)]"
    }
    if ([string]::IsNullOrWhiteSpace($unwrapped)) {
        # Whitespace-only strings ('', '   ', "`t`n") are strings by type but not by intent —
        # a hashtable lookup on '' or '   ' would collide with the "no resource declared"
        # semantics the caller opted OUT of by calling this function. Reject them explicitly
        # so a stray ' ' in a caller's config becomes a hard error, not a silent hit.
        throw "${FunctionName}: -${ParamName} must be a non-empty [string] (msg-2932 §3 #6a positive-allowlist entry validation); got an empty or whitespace-only string"
    }
    return [string]$unwrapped
}

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
        respect to leases. This is an ADVISORY predicate. The BINDING mutation is
        Invoke-LeaseAcquire — see the ORDERING REQUIREMENT below.

    .DESCRIPTION
        VERDICTS. Returns one of:
          - 'available' : the required resource is FREE, OR is already HELD BY THIS candidate;
                          the candidate is authorised to attempt an acquire.
          - 'waiting'   : the required resource is held by someone else and NOT this candidate.
                          The candidate must NOT attempt an acquire; disposition `lease-waiting`.
                          The caller SHOULD call Register-LeaseWaiter to queue.

        THE 'available' COLLAPSE — intentional (msg-2738 §5 correction 1 pins this as
        documentation-only, not a verdict change). 'available' folds two distinct situations
        together: "the resource has no holder" AND "the current holder IS this candidate".
        Both situations end with the same caller action — call Invoke-LeaseAcquire, which is
        idempotent on self-hold (see its 'currentHolder -eq CandidateKey' branch below) — so
        splitting the verdict into 'free' vs 'held-by-self' would ADD a caller branch that
        every call site would immediately collapse again. The `.SYNOPSIS` still reads "free
        OR already held by this candidate" for exactly this reason. If a future caller needs
        the distinction, that caller's scope is when to split — verdict-domain expansion is
        PR 4 attack-surface at the earliest and out of scope for PR 3 (row #5 documentation-
        only, msg-1958 §4 single-seam rule).

        ASYMMETRIC CONTRACT (msg-1961; msg-2644 §2). The two verdicts do NOT carry equal
        weight against a subsequent acquire:

          - 'available' is ADVISORY. Between this call and Invoke-LeaseAcquire another candidate
            may acquire the lease, and OUR subsequent acquire will be refused by the "no
            accidental steal" throw. Callers MUST treat 'available' as "try to acquire", NOT
            as "you have the lease". An acquire failure after an 'available' verdict is an
            EXPECTED outcome (a TOCTOU race), not an error condition.
          - 'waiting' is BINDING. If this call returns 'waiting', the caller MUST NOT attempt
            an acquire and MUST NOT launch (msg-1959: the mutation is the authorisation, so
            the caller has no path to authorisation without the acquire succeeding).

        ORDERING REQUIREMENT (msg-2644 §2 blocker fix; supersedes the earlier phrasing
        "lease check before Invoke-HeadSkipCommitLaunch" in msg-1958 / msg-1960, which failed
        to distinguish the advisory predicate from the binding mutation).

            Invoke-LeaseAcquire MUST SUCCEED BEFORE any un-rollbackable side effect is committed.

          Un-rollbackable side effects include, but are not limited to:
            - Invoke-HeadSkipCommitLaunch (commits the head-skip in conductor state)
            - any process launch (editor / PIE / runner)
            - any write to sweep or loop-control state that a later abort cannot undo

          The required sequence is therefore:

              Test-LeaseAvailableFor        (advisory; MAY be skipped — it is an optimisation)
              Invoke-LeaseAcquire           (binding; MUST succeed)
              Invoke-HeadSkipCommitLaunch   (un-rollbackable; ONLY after a successful acquire)
              <launch>

          If Invoke-LeaseAcquire fails, the caller MUST NOT commit the head-skip, MUST NOT
          launch, and MUST call Register-LeaseWaiter. Acquire failure after an 'available'
          verdict is an EXPECTED outcome (TOCTOU), not an error. The candidate has NOT consumed
          its turn — nothing un-rollbackable was committed, so it is still eligible when the
          mechanism next wakes it.

        The test file's row #7 ordering pin (tests/Test-Lease.ps1 §14) enforces this sequence
        MECHANICALLY: it injects a spy for the known un-rollbackable command names, drives the
        candidate loop into the acquire-failure branch, and asserts each spy was called ZERO
        times. Docstring text alone is not enough — msg-923 explicitly forbids relying on
        "the implementer reads it" as a control-flow mechanism (msg-2644 §3).

        SINGLE-RESOURCE, FAIL-CLOSED, POSITIVE ALLOWLIST (msg-2185 → msg-2189 → msg-2746 →
        msg-2932 §3 #6a). The `-Requires` validator has passed through THREE spec revisions:

          - msg-2185: signature was collapsed from `[string[]]` to `[string]`. Intended as a
            hard single-resource signal, but PowerShell's binder silently joined
            `@('editor','runner')` into 'editor runner' BEFORE the body ran (layer-1 coercion).
          - msg-2189: dropped the `[string]` annotation so the binder cannot join arrays. Layer-1
            fixed, but the accepted type widened from [string] to [object], letting $null /
            [bool] / [pscustomobject] flow through untouched (layer-2 hole raised by msg-2746).
          - msg-2932 §3 #6a: POSITIVE allowlist — accept ONLY plain non-empty [string]. Every
            other input throws through the shared Assert-LeaseResourceName helper. This closes
            layer 2 without re-opening layer 1: no annotation, so the binder cannot coerce; the
            body validates the actual runtime type. See Assert-LeaseResourceName's docstring
            for the layered coercion story.

        Entry validation is OWNED by row #6a via Assert-LeaseResourceName. This function does
        NOT re-implement the validation inline — sharing the validator is what structurally
        prevents drift between Test-LeaseAvailableFor's rejection set and Invoke-LeaseAcquire's
        rejection set (msg-2932 §3 #6c: the same rejection MUST hold on the direct-acquire
        path). This `.OUTPUTS` section describes the verdict domain and its contract; it does
        NOT re-state the accepted-input shape (that lives on the shared helper).

    .PARAMETER LeasesState
        The full leases.json map: resource-name -> lease hashtable.

    .PARAMETER CandidateKey
        The "$project/$thread_id" key of the candidate.

    .PARAMETER Requires
        The single resource name the candidate declares in sweep.json's `requires`. Must be a
        plain non-empty [string] — see Assert-LeaseResourceName for the rejection set. Callers
        that KNOW a candidate has no `requires` declared MUST skip this call entirely; the
        function does NOT accept `$null` / empty / whitespace / arrays / hashtables / booleans
        as "trivially available" any more (msg-2932 §3 #6a; supersedes the msg-2189-era
        behaviour where `$null` / `''` / `@()` collapsed to 'available').

    .OUTPUTS
        A hashtable @{
          status   = 'available' | 'waiting'
          holders  = @{ resource -> current-holder-key } for resources that are held (any holder)
          waitOn   = [string[]] resources this candidate is waiting on (0 or 1 element in v1).
                     Kept as an array so the caller can uniformly `.Count`-test rather than
                     null-check a scalar; the array wraps a single-resource verdict, it does
                     NOT signal multi-resource support.
        }

        A malformed `-Requires` argument throws through Assert-LeaseResourceName — the function
        does NOT return a verdict in that case. Callers must not swallow the exception silently;
        the whole point of failing closed is to make the misuse visible to the caller.
    #>
    param(
        [hashtable]$LeasesState,
        [string]$CandidateKey,
        # UNTYPED on purpose (msg-2189 layer-1 coercion). A `[string]` annotation would let
        # pwsh silently join a multi-element array into a nonsense string before the
        # Assert-LeaseResourceName call below could see it. The shared validator is the real
        # single-resource enforcement.
        $Requires
    )

    # ENTRY VALIDATION owned by the shared helper (msg-2932 §3 #6a positive allowlist).
    # Assert-LeaseResourceName throws on ANY input that is not a plain non-empty [string] —
    # $null, whitespace-only, booleans, hashtables, integers, [pscustomobject], multi- or
    # single-element arrays. The unwrapping / rejection story lives on the helper's docstring;
    # this call is the ENTIRE validation for this function.
    $requiresStr = Assert-LeaseResourceName -Value $Requires -ParamName 'Requires' -FunctionName 'Test-LeaseAvailableFor'

    # Assert-LeaseResourceName guarantees $requiresStr is a non-empty, non-whitespace [string].
    # No conditional block is needed here — the msg-2189-era wrapper (`if (-not
    # [string]::IsNullOrEmpty($requiresStr)) { ... }`) existed to handle the empty-Requires =
    # trivially-available branch, which msg-2932 §3 #6a explicitly deleted. Empty / null /
    # whitespace inputs throw at entry now; the body only runs with a real resource name.
    $holders = @{}
    $waitOn = @()
    $resource = $requiresStr
    if ($LeasesState.ContainsKey($resource)) {
        $lease = $LeasesState[$resource]
        if ($null -ne $lease) {
            $h = if ($lease -is [hashtable]) { $lease['holder'] } else { $lease.holder }
            if (-not [string]::IsNullOrEmpty("$h")) {
                $holders[$resource] = "$h"
                if ("$h" -ne $CandidateKey) { $waitOn += $resource }
            }
        }
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

    .DESCRIPTION
        THE BINDING MUTATION (msg-1959: the mutation is the authorisation, not the predicate).
        Test-LeaseAvailableFor is advisory; the call to THIS function is the one that decides
        whether the candidate holds the lease. See Test-LeaseAvailableFor's ORDERING REQUIREMENT
        block for the sequence contract: acquire MUST succeed BEFORE any un-rollbackable side
        effect (Invoke-HeadSkipCommitLaunch / process launch / conductor-state write). Acquire
        failure — including the TOCTOU race where another candidate acquired between the
        available verdict and this call — is an EXPECTED outcome, not an error: the caller MUST
        NOT launch, MUST NOT commit head-skip state, and MUST call Register-LeaseWaiter.

        ENTRY VALIDATION is owned by the shared Assert-LeaseResourceName helper — SAME positive
        allowlist as Test-LeaseAvailableFor's -Requires (msg-2932 §3 #6a / #6c). A direct-acquire
        path that does not first call Test-LeaseAvailableFor gets the SAME rejection set, because
        the authorisation lives here and the guard must hold here. Sharing the validator is what
        structurally prevents the two functions from drifting.

    .PARAMETER LeasesState
        The full leases.json map. Mutated in place — a resource that had no entry gets one.

    .PARAMETER Resource
        Resource name. UNTYPED on purpose (msg-2189 layer-1 coercion; msg-2932 §3 #6a). Must
        pass Assert-LeaseResourceName — a plain non-empty [string]. Any other shape ($null /
        empty / whitespace-only / booleans / hashtables / integers / [pscustomobject] / multi-
        or single-element arrays) throws before the state map is touched.

    .PARAMETER CandidateKey
        The "$project/$thread_id" acquiring the lease.

    .PARAMETER Now
        UTC tick timestamp.
    #>
    param(
        [hashtable]$LeasesState,
        # UNTYPED on purpose (msg-2189 layer-1 coercion, msg-2932 §3 #6a positive allowlist).
        # A `[string]` annotation would let pwsh silently join a multi-element array into a
        # nonsense resource name that then creates a phantom lease record, LOCKING a resource
        # whose name matches nothing real ("editor runner" instead of "editor"). The shared
        # Assert-LeaseResourceName below is the single source of truth for accepted shapes.
        $Resource,
        [string]$CandidateKey,
        [datetime]$Now
    )
    # ENTRY VALIDATION — SAME shared helper Test-LeaseAvailableFor uses. Direct-acquire callers
    # (row #6c) hit exactly this rejection set. Reassign the untyped $Resource to the unwrapped
    # scalar so every subsequent hashtable lookup uses a real string, not a PSObject shell.
    $Resource = Assert-LeaseResourceName -Value $Resource -ParamName 'Resource' -FunctionName 'Invoke-LeaseAcquire'

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

function Get-NextLeaseWaiter {
    <#
    .SYNOPSIS
        Pick the next waiter to promote from a lease's queue. FIFO on `waiting_since`, with
        sweep-list order as the tiebreak (msg-1183 D-3). Returns $null when the queue is empty
        or every entry is ineligible after the sweep filter.

    .DESCRIPTION
        WHY FIFO AT GRANT TIME AND NOT ENQUEUE TIME (msg-1958 §5 row #1 pin). Add-LeaseWaiter
        preserves append order and each waiter's original `waiting_since` (msg-2181 blocker fix
        — the SYNOPSIS on Add-LeaseWaiter carries the full rationale). The FIFO ordering is
        established here, at the moment the lease grants, so a promotion decision that has to
        cross a re-enqueue storm still respects the first arrival's wait.

        SWEEP-ORDER TIEBREAK. Two waiters with the exact-same `waiting_since` (same tick, same
        Register-LeaseWaiter call ordering) fall back to sweep.json enumeration order. This is
        deterministic (`SweepOrder` is a materialised list) and avoids the "sort-order is a
        hash of the string" surprise that would appear if we sorted by key alphabetically.

        ELIGIBILITY FILTER. When `-EligibleKeys` is provided, waiters not in that set are
        skipped without being removed from the queue — that removal is Remove-IneligibleLease-
        Waiters' job (a separate function so a caller that only wants to PEEK the next waiter
        does not accidentally mutate the queue). When `-EligibleKeys` is $null, no filtering
        happens (this is the "peek without knowledge of the current sweep" mode; tests use it).

    .PARAMETER Lease
        The per-resource lease hashtable.

    .PARAMETER SweepOrder
        The current sweep enumeration order (usually the sorted list of `sweep.json` project /
        thread keys). Used ONLY as the tiebreak when two waiters share `waiting_since`. Empty /
        $null means "no known sweep order" — ties fall back to queue-append order.

    .PARAMETER EligibleKeys
        Optional set of waiter keys currently eligible to be granted (usually the sweep keys
        minus quarantined / off-sweep). When $null, no filtering happens.

    .OUTPUTS
        The chosen waiter hashtable (with `key` and `waiting_since`), or $null.
    #>
    param(
        [hashtable]$Lease,
        [string[]]$SweepOrder = @(),
        [string[]]$EligibleKeys = $null
    )
    if ($null -eq $Lease) { return $null }
    if (-not $Lease.ContainsKey('queue') -or $null -eq $Lease['queue']) { return $null }
    $queue = @($Lease['queue'])
    if ($queue.Count -eq 0) { return $null }

    # Materialise the eligibility set (fast lookup vs. per-item linear scan).
    $eligibleSet = $null
    if ($null -ne $EligibleKeys) {
        $eligibleSet = @{}
        foreach ($k in $EligibleKeys) { $eligibleSet["$k"] = $true }
    }

    # Attach sweep-order index for the tiebreak, then sort by (waiting_since, sweep index,
    # queue index). Queue index is the final tiebreaker so callers that pass neither SweepOrder
    # nor EligibleKeys still get a deterministic answer (append order).
    $annotated = @()
    for ($i = 0; $i -lt $queue.Count; $i++) {
        $w = $queue[$i]
        $k = if ($w -is [hashtable]) { $w['key'] } else { $w.key }
        if ($null -ne $eligibleSet -and -not $eligibleSet.ContainsKey("$k")) { continue }
        $wsRaw = if ($w -is [hashtable]) { $w['waiting_since'] } else { $w.waiting_since }
        $ws = [datetime]::MaxValue
        try { if ($wsRaw) { $ws = [datetime]::Parse("$wsRaw").ToUniversalTime() } } catch { }
        $sweepIdx = if ($null -ne $SweepOrder) { [array]::IndexOf($SweepOrder, "$k") } else { -1 }
        if ($sweepIdx -lt 0) { $sweepIdx = [int]::MaxValue }
        $annotated += [pscustomobject]@{
            _waiter    = $w
            _key       = "$k"
            _wsInstant = $ws
            _sweepIdx  = $sweepIdx
            _queueIdx  = $i
        }
    }
    if ($annotated.Count -eq 0) { return $null }
    $sorted = $annotated | Sort-Object -Property _wsInstant, _sweepIdx, _queueIdx
    return @($sorted)[0]._waiter
}

function Invoke-LeasePromotion {
    <#
    .SYNOPSIS
        Advance the two-phase expiry state machine for a single lease (msg-1183 D-6' /
        D-6'd). Phase 1: mark expiring, record TRANSIENT revoke intent, DO NOT change the
        holder — the current holder gets ONE tick to make progress and pre-empt the revoke.
        Phase 2 (called on the NEXT eligible tick, once Test-LeaseExpiring still holds AND
        expiring=$true): reclaim from the current holder, either promote the next FIFO waiter
        or empty the record (which the caller may then delete from LeasesState per the schema).

    .DESCRIPTION
        RETURN VALUE. A short string describing what happened this call, so the caller can log
        and route without re-inspecting the record:
          'phase-1'            — first tick: expiring flag set, holder unchanged.
          'phase-2-promoted'   — waiter promoted, record now holds the new holder.
          'phase-2-released'   — no eligible waiter; holder cleared, record left as an empty
                                 stub (the caller MUST remove the key per the schema:
                                 msg-2131 blocker, "ABSENT resource key = no holder, empty
                                 queue"). We do NOT .Remove() here because $Lease is a
                                 hashtable reference and the caller owns the parent map.
          'noop'               — $null lease, or somehow already-clean state.

        NO-STEAL INVARIANT PRESERVED (msg-1958 §5 row #2). Even in the grant path, the new
        holder MUST come from the FIFO queue — a raw acquire on an occupied lease still throws
        via Invoke-LeaseAcquire's no-steal branch (msg-1958 §5). This function does not bypass
        that: promotion goes through the record's own `holder` field write with `reclaimed_from`
        set to the PRIOR holder, which is a legitimate reclamation, not a steal. Callers that
        pull a waiter out of the queue and try to `Invoke-LeaseAcquire` it on an occupied lease
        would still be refused — the acquire path is a strict single writer, the grant path is
        the reclamation.

        AUDIT PAIRING (msg-1900 split). `reclaimed_from` / `reclaimed_at` / `reclaimed_reason`
        are the PERMANENT audit fields; `revoked_at` / `revoked_reason` are the TRANSIENT
        Phase-1 intent. Phase 2 CLEARS the transient fields (its purpose was served) and WRITES
        the permanent trio in the same tick. Phase 1 sets ONLY the transient trio; the
        permanent fields are untouched until the tick that actually reclaims.

    .PARAMETER Lease
        The per-resource lease hashtable. Mutated in place.

    .PARAMETER Reason
        Free-text audit paired with `reclaimed_from`. Default 'idle' (the automatic TTL path).
        Callers passing an operator-initiated reason should use one of the `human-*` prefixes
        (ADR-2026-05-29-10 role registry).

    .PARAMETER Now
        The tick's UTC timestamp.

    .PARAMETER SweepOrder
        The current sweep enumeration order (tiebreak on `waiting_since` collisions).

    .PARAMETER EligibleKeys
        Optional set of waiter keys currently eligible for promotion. Passed straight through
        to Get-NextLeaseWaiter.
    #>
    param(
        [hashtable]$Lease,
        [string]$Reason = 'idle',
        [datetime]$Now,
        [string[]]$SweepOrder = @(),
        [string[]]$EligibleKeys = $null
    )
    if ($null -eq $Lease) { return 'noop' }

    $isExpiring = $Lease.ContainsKey('expiring') -and [bool]$Lease['expiring']
    if (-not $isExpiring) {
        # Phase 1 — msg-1183 D-6'd 1-tick pre-emption window. Mark, record intent, DO NOT touch
        # the holder or the queue. The permanent audit fields (reclaimed_*) are NOT written yet;
        # nothing has been reclaimed. Only the transient revoked_* fields carry the intent.
        $Lease['expiring']       = $true
        $Lease['revoked_at']     = $Now.ToUniversalTime().ToString("o")
        $Lease['revoked_reason'] = "$Reason"
        return 'phase-1'
    }

    # Phase 2 — actually reclaim.
    $priorHolder = if ($Lease.ContainsKey('holder')) { "$($Lease['holder'])" } else { '' }
    $priorGen = 0
    if ($Lease.ContainsKey('generation') -and $null -ne $Lease['generation']) {
        $priorGen = [int]$Lease['generation']
    }
    $nowIso = $Now.ToUniversalTime().ToString("o")
    $next = Get-NextLeaseWaiter -Lease $Lease -SweepOrder $SweepOrder -EligibleKeys $EligibleKeys

    if ($null -ne $next) {
        $newHolder = if ($next -is [hashtable]) { $next['key'] } else { $next.key }
        $Lease['holder']           = "$newHolder"
        $Lease['acquired_at']      = $nowIso
        $Lease['last_progress_at'] = $nowIso
        $Lease['idle_evaluations'] = 0
        $Lease['generation']       = $priorGen + 1
        $Lease['pinned']           = $false
        $Lease['expiring']         = $false
        $Lease['reclaimed_from']   = $priorHolder
        $Lease['reclaimed_at']     = $nowIso
        $Lease['reclaimed_reason'] = "$Reason"
        # The new holder MUST restart the resource before use (msg-1183 D-6'e) — the previous
        # holder still physically had the editor / PIE / runner at Phase 1, and Phase 2 is the
        # promotion tick, not a graceful release.
        $Lease['reclaim_required'] = $true
        # TRANSIENT Phase-1 intent is now consumed — clear.
        $Lease['revoked_at']       = $null
        $Lease['revoked_reason']   = $null
        # Dequeue the promoted waiter.
        Remove-LeaseWaiter -Lease $Lease -WaiterKey "$newHolder"
        return 'phase-2-promoted'
    }

    # No eligible waiter — release. The record stays in memory as a "recently-reclaimed"
    # stub so the digest can render the reclamation. The caller decides whether to remove the
    # key from LeasesState (the schema commits to no empty stubs — msg-2131 — but the
    # Merge-LeasesStateForWrite pass is where that decision lives, not here).
    $Lease['holder']           = $null
    $Lease['acquired_at']      = $null
    $Lease['last_progress_at'] = $null
    $Lease['idle_evaluations'] = 0
    $Lease['generation']       = $priorGen + 1
    $Lease['pinned']           = $false
    $Lease['expiring']         = $false
    $Lease['reclaimed_from']   = $priorHolder
    $Lease['reclaimed_at']     = $nowIso
    $Lease['reclaimed_reason'] = "$Reason"
    # SET reclaim_required = $true even though there is no immediate successor (msg-2946
    # blocking objection). `reclaim_required` is a property of the RESOURCE — "the physical
    # editor/PIE/runner was NOT gracefully returned; the next holder MUST restart it before
    # use" — NOT a property of the successor candidate. A forceful eviction (Phase 2) leaves
    # the physical resource dirty by definition, and Invoke-LeaseAcquire's post-release branch
    # preserves reclaim_required verbatim into the next holder's record. If we clear the flag
    # here, whichever candidate later arrives on this empty record inherits `$false` and
    # skips the restart — the exact "next holder walks into a dirty editor" failure the flag
    # exists to prevent. The empty-record window between here and the next acquire does not
    # execute code that CLEANS the resource; the dirty-state signal has to survive the gap.
    #
    # Contrast: Invoke-LeaseAcquire on a FRESH cold-start lease (New-LeaseRecord path,
    # generation = 1) leaves `reclaim_required = $false` by default — that path represents
    # "resource never held", not "resource forcefully evicted". The two writes are the
    # correct pair; clearing here would collapse them into one lossy write.
    $Lease['reclaim_required'] = $true
    $Lease['revoked_at']       = $null
    $Lease['revoked_reason']   = $null
    return 'phase-2-released'
}

function Remove-IneligibleLeaseWaiters {
    <#
    .SYNOPSIS
        Scrub a lease's queue of waiters that are no longer eligible: not on the current sweep
        list, or currently quarantined. Preserves order of the remaining eligible entries.

    .DESCRIPTION
        WHY THIS IS SEPARATE FROM Get-NextLeaseWaiter. Get-NextLeaseWaiter is a peek — it MUST
        NOT mutate the queue, so a caller that inspects "who's next" without granting doesn't
        leave the queue in a different state than it found it (the sweep's classification /
        promotion cycles walk this record multiple times per tick). Remove-IneligibleLeaseWaiters
        is the sanction — call it explicitly, once per tick, from the classification pass, to
        clean up waiters that have gone away (removed from sweep.json entirely, or quarantined).

        WHY NOT DELETE MID-GRANT. Deletion inside promotion would collapse two responsibilities
        into one: "who is next" (a pure predicate) and "who is no longer eligible" (a policy
        decision that requires the current sweep enumeration). Keeping them separate is what
        made Get-LeaseHolderClassification a clean predicate — same split.

    .PARAMETER Lease
        The per-resource lease hashtable. Mutated in place.

    .PARAMETER SweepKeys
        The set of keys currently present in sweep.json. Waiters NOT in this set are removed.

    .PARAMETER QuarantinedKeys
        The set of keys currently quarantined. Waiters in this set are removed.

    .OUTPUTS
        The number of waiters removed this call.
    #>
    param(
        [hashtable]$Lease,
        [string[]]$SweepKeys = @(),
        [string[]]$QuarantinedKeys = @()
    )
    if ($null -eq $Lease) { return 0 }
    if (-not $Lease.ContainsKey('queue') -or $null -eq $Lease['queue']) { return 0 }
    $sweepSet = @{}
    foreach ($k in $SweepKeys)       { $sweepSet["$k"]       = $true }
    $qSet = @{}
    foreach ($k in $QuarantinedKeys) { $qSet["$k"] = $true }
    $before = @($Lease['queue']).Count
    $Lease['queue'] = @($Lease['queue'] | Where-Object {
        $k = if ($_ -is [hashtable]) { $_['key'] } else { $_.key }
        $onSweep = $sweepSet.ContainsKey("$k")
        $quarantined = $qSet.ContainsKey("$k")
        $onSweep -and (-not $quarantined)
    })
    return ($before - @($Lease['queue']).Count)
}

function Invoke-LeaseGrantFromEmpty {
    <#
    .SYNOPSIS
        When a lease has no holder but has a waiter, promote the FIFO waiter into the empty
        record. Returns the promoted holder key, or $null when nothing was promoted (no queue,
        no eligible waiters, or the lease was already held).

    .DESCRIPTION
        WHY THIS FUNCTION EXISTS SEPARATELY FROM Invoke-LeasePromotion. Empty-state promotion
        does NOT go through Phase 1 (there is nothing to expire — the previous holder is
        already gone). This is the "someone released, now grant to the next waiter" path,
        called by the classification pass after it processes releases. Bolting it onto
        Invoke-LeasePromotion would force a fake `expiring=$true` intermediate, which would
        write a false Phase-1 audit record. Keep them separate.

        AUDIT. The permanent audit trio (`reclaimed_from` / `reclaimed_at` / `reclaimed_reason`)
        already carries the release rationale (set at the point the lease was emptied). This
        function DOES NOT overwrite them — the new holder inherits the release's audit exactly
        as `Invoke-LeaseAcquire` does on the post-release path.

    .PARAMETER Lease
        The per-resource lease hashtable. Mutated in place.

    .PARAMETER Now
        UTC tick timestamp.

    .PARAMETER SweepOrder
        The current sweep enumeration order (tiebreak on `waiting_since` collisions).

    .PARAMETER EligibleKeys
        Optional set of waiter keys currently eligible for promotion.

    .OUTPUTS
        The promoted holder key (string) on success, $null otherwise.
    #>
    param(
        [hashtable]$Lease,
        [datetime]$Now,
        [string[]]$SweepOrder = @(),
        [string[]]$EligibleKeys = $null
    )
    if ($null -eq $Lease) { return $null }
    $currentHolder = if ($Lease.ContainsKey('holder')) { "$($Lease['holder'])" } else { '' }
    # Already held — nothing to grant here. Callers that want to force a reclamation should
    # go through Invoke-LeasePromotion, which respects the two-phase expiry contract.
    if (-not [string]::IsNullOrEmpty($currentHolder)) { return $null }
    $next = Get-NextLeaseWaiter -Lease $Lease -SweepOrder $SweepOrder -EligibleKeys $EligibleKeys
    if ($null -eq $next) { return $null }
    $newHolder = if ($next -is [hashtable]) { $next['key'] } else { $next.key }
    $priorGen = 0
    if ($Lease.ContainsKey('generation') -and $null -ne $Lease['generation']) {
        $priorGen = [int]$Lease['generation']
    }
    $nowIso = $Now.ToUniversalTime().ToString("o")
    $Lease['holder']           = "$newHolder"
    $Lease['acquired_at']      = $nowIso
    $Lease['last_progress_at'] = $nowIso
    $Lease['idle_evaluations'] = 0
    $Lease['generation']       = $priorGen + 1
    $Lease['pinned']           = $false
    $Lease['expiring']         = $false
    # PERMANENT audit trio is preserved from the release — the digest reads reclaimed_reason
    # to render the operator's Tier-C intent, and the new holder inherits reclaim_required if
    # the release set it. Same discipline as Invoke-LeaseAcquire's post-release branch.
    $Lease['revoked_at']       = $null
    $Lease['revoked_reason']   = $null
    Remove-LeaseWaiter -Lease $Lease -WaiterKey "$newHolder"
    return "$newHolder"
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
