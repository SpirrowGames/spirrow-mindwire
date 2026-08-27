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
# --- what THIS PR ships (T-exclusive-resource-lease-queue PR 1 / 4) -----------------------------
# THIS FILE, IN THIS PR, IS INERT. The lease block is not called from the sweep wrapper yet — the
# wiring lands in a later PR. What ships here is the persistence contract only:
#   - the record shape (New-LeaseRecord),
#   - the on-disk read (Get-JsonState) and the digest render (Get-LeaseSummaryLines),
#   - the merge rule that lets an operator's mid-sweep write beat the sweep's in-memory hold
#     (Merge-LeasesStateForWrite, with per-resource `generation` as an optimistic-concurrency
#     token — the msg-1802 fix rebased onto a fresh, split PR).
# The state-machine functions the header comments reference (Invoke-LeaseAcquire,
# Invoke-LeasePromotion, Grant-Lease.ps1, etc.) are named here so the fields' contracts read as
# a coherent whole; they land in the next PRs and activate the mechanism in PR 4.
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
