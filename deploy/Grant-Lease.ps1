#!/usr/bin/env pwsh
# deploy/Grant-Lease.ps1 — operator override for the exclusive-resource lease + queue
# (T-exclusive-resource-lease-queue, msg-1183 D-7).
#
# WHY THIS SCRIPT EXISTS. The sweep grants leases automatically (FIFO from the queue, msg-1183
# D-3) and reclaims them from a starved holder after $LeaseIdleTtl (msg-1183 D-6', msg-1187
# §5-4). Those two paths cover the common case. What they don't cover:
#
#   1. A HUMAN OVERRIDE. Takahito can decide "I want the editor on Thread X right now, regardless
#      of the queue" — msg-923 requirements: "Tier-C の割当を上書きできる（人が明示的に「今はこ
#      のスレ」と言えば機構がそれに従う）". Pre-mechanism this was a hand-written line in a Tier-C
#      decision message; this script is the machine equivalent.
#
#      TERMINOLOGY NOTE (unresolved, msg-1876 + msg-1877 non-blocking pointers). This file
#      names the invocation lane "operator" (revoked_reason prefixes `operator-grant:` /
#      `operator-clear:`). Two ADRs are relevant but I have not read either body (index/title
#      only, per OBL-DECLARE-UNREADABLE + OBL-ADR-CITATION-WEIGHT):
#        - ADR-2026-05-29-10 (role registry).
#        - ADR-2026-05-27-09 (4-layer identity model: identity_name / independence_class /
#          role / embodiment as orthogonal layers).
#      The msg-1877 naysayer correctly flagged that my previous rewrite of this note conflated
#      identity and role by calling "human" "the identity" — treating them as one layer when
#      the ADR-27-09 title explicitly separates them. The correct cross-check therefore has
#      TWO independent questions: (a) does the role layer forbid "operator" as a distinct role
#      value; (b) does the identity_name layer forbid it as a distinct identity value. Without
#      the ADR bodies I cannot answer either, so I record both as open follow-ups rather than
#      guess — an incomplete report is better than a confident wrong one.
#
#   2. A PIN. A grant that MUST NOT be auto-reclaimed even after idle TTL — a legitimately long
#      PIE turn where the holder is deliberately paused. msg-1183 D-7. The lease's `pinned=true`
#      field is what Test-LeaseExpiring reads to keep the lease immune.
#
#   3. AN EXPLICIT CLEAR. Free a lease with no promotion — the queue advances on the next sweep.
#      Complement to Clear-Quarantine (a different container, same operational shape: a human
#      act with a mandatory reason).
#
# NOT A PLACE TO WATCH FOR DEADLOCKS. v1 has one resource (`editor`); a "held-by-A-waiting-on-B
# and vice versa" cycle is not possible. Adding a second resource is where this analysis becomes
# non-trivial — that is called out in msg-1180 §3 as a v2 concern and NOT solved here.
#
# MERGE-ON-WRITE (msg-1187 §2, msg-1802 blocker #1 correction). This script writes leases.json
# directly. The sweep uses Merge-LeasesStateForWrite (NOT the generic Merge-StateForWrite —
# that would silently destroy our write, since sweep memory would win on collision at the
# `editor` top-level key). Merge-LeasesStateForWrite compares per-resource `generation` before
# and after; because this script bumps `generation` on every Grant / Clear, the sweep sees the
# generation moved and lets disk (this script's write) win. The reverse race remains: if THIS
# script re-reads leases.json AFTER the sweep flushes, we operate on stale state — but that
# read-modify-write window is sub-second, well below the tick length, and closing it fully
# would require a real file lock (see Merge-StateForWrite W:255-257 for why that trade is
# refused).

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium', DefaultParameterSetName = 'Grant')]
param(
    # The resource name. v1 in-tree resource: `editor`. No enum validation — resource names are
    # coordination tokens (see sweep.json.example).
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Resource,

    # State key of the new holder: "project/thread_id". Required for -Grant, ignored for -Clear.
    [Parameter(Mandatory = $false, ParameterSetName = 'Grant')]
    [ValidatePattern('^[^/]+/[^/]+$')]
    [string]$To,

    # Pin the grant — TTL-immune (msg-1183 D-7). Only meaningful with -Grant.
    [Parameter(Mandatory = $false, ParameterSetName = 'Grant')]
    [switch]$Pin,

    # Clear the lease entirely: no holder, empty queue-mutation. Different container than
    # -Grant. The next arriving candidate will acquire.
    [Parameter(Mandatory = $true, ParameterSetName = 'Clear')]
    [switch]$Clear,

    # Free-text explanation. REQUIRED for both -Grant and -Clear — the whole reason we record
    # this in leases.json (revoked_reason / grant reason) is so the operator's intent survives
    # in the audit trail. Same discipline as Clear-Quarantine.
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Reason,

    # Data root; defaults to the same location the sweep respects.
    [string]$DataDir = $(if ($env:MINDWIRE_PATHS__DATA_DIR) { $env:MINDWIRE_PATHS__DATA_DIR } else { Join-Path $HOME "spirrow-mindwire-data" })
)

$ErrorActionPreference = "Stop"

# Same lib the sweep uses. Dot-sourced here so we share exactly one set of helpers — a divergent
# copy would be the exact failure this design is trying to end.
. (Join-Path $PSScriptRoot 'lib/Lease.ps1')

$leasesStatePath = Join-Path $DataDir "state\leases.json"
$stateDir = Split-Path -Parent $leasesStatePath
if (-not (Test-Path -LiteralPath $stateDir)) { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null }

# Read leases.json directly (do NOT dot-source the sweep — dot-sourcing runs it).
$state = @{}
if (Test-Path -LiteralPath $leasesStatePath) {
    $raw = Get-Content -LiteralPath $leasesStatePath -Raw -Encoding utf8
    if ($raw.Trim()) {
        $obj = $raw | ConvertFrom-Json
        foreach ($p in $obj.PSObject.Properties) { $state[$p.Name] = $p.Value }
    }
}
$state = ConvertTo-LeasesStateHashtable $state

$nowUtc = [DateTime]::UtcNow

switch ($PSCmdlet.ParameterSetName) {
    'Grant' {
        if (-not $To) { throw "-To is required for a grant" }
        $lease = if ($state.ContainsKey($Resource)) { $state[$Resource] } else { $null }
        $priorHolder = if ($lease) { "$($lease['holder'])" } else { $null }

        if (-not $PSCmdlet.ShouldProcess($Resource, "grant to '$To'$(if ($Pin) { ' (pinned)' })  — reason: $Reason")) { return }

        if (-not $lease) {
            $state[$Resource] = New-LeaseRecord -Holder $To -Now $nowUtc -Generation 1 `
                                                -ReclaimedFrom $priorHolder -ReclaimRequired:$false -Pinned:$Pin.IsPresent
        }
        else {
            $priorGen = 1
            if ($lease.ContainsKey('generation') -and $null -ne $lease['generation']) {
                $priorGen = [int]$lease['generation']
            }
            # Queue is mutated in place by Remove-LeaseWaiter below; there is no need to pull it
            # out into a local (msg-1802 blocker #3 — that local was a dead assignment).
            $nowIso = $nowUtc.ToUniversalTime().ToString("o")
            $lease['holder']            = $To
            $lease['acquired_at']       = $nowIso
            $lease['last_progress_at']  = $nowIso
            $lease['idle_evaluations']  = 0
            $lease['generation']        = $priorGen + 1
            $lease['pinned']            = [bool]$Pin.IsPresent
            $lease['expiring']          = $false
            # Reclaim-duty policy on a grant (msg-1876 blocker correction):
            #   - Overwriting a LIVE different holder → new holder inherits reclaim duty
            #     (msg-1183 D-6'e). This mirrors the automated revoke path so there is one
            #     contract for "you just took a lease from someone else." Write BOTH
            #     `reclaimed_from` and `reclaimed_reason` (permanent audit, msg-1900 split).
            #   - Empty-holder grant (previous -Clear or Phase-2 'released' left the record
            #     with holder=null AND reclaim_required=true) → PRESERVE the prior flag +
            #     `reclaimed_from` + `reclaimed_reason`. Erasing them would fire a new holder
            #     blindly on top of the physically-lingering session AND lose the operator's
            #     original Clear-reason narrative in the digest (msg-1876 + msg-1900).
            #   - Same-holder re-grant (idempotent) → preserve existing state.
            $inheritedDirty = $false
            $inheritedFrom = $null
            if ($priorHolder -and $priorHolder -ne $To) {
                $lease['reclaimed_from']    = $priorHolder
                $lease['reclaimed_at']      = $nowIso
                $lease['reclaimed_reason']  = "operator-grant: $Reason"
                $lease['reclaim_required']  = $true
                $inheritedDirty = $true
                $inheritedFrom = $priorHolder
            }
            else {
                # Do NOT touch reclaim_required / reclaimed_from / reclaimed_reason — preserve
                # whatever was there (true from a prior -Clear, or false from a clean state).
                # Fall back to false only if the record was hand-edited to omit the field.
                if (-not $lease.ContainsKey('reclaim_required')) { $lease['reclaim_required'] = $false }
                if ($lease.ContainsKey('reclaim_required') -and [bool]$lease['reclaim_required']) {
                    $inheritedDirty = $true
                    $inheritedFrom = if ($lease.ContainsKey('reclaimed_from')) { "$($lease['reclaimed_from'])" } else { '' }
                }
            }
            # `revoked_*` are TRANSIENT Phase-1 intent (msg-1900 split). A fresh operator grant
            # supersedes any pending Phase-1 intent from an earlier probe cycle, so clear them.
            # The audit trail lives in `reclaimed_*` above, not here.
            $lease['revoked_at']        = $null
            $lease['revoked_reason']    = $null
            # Dequeue the new holder (if they were waiting).
            Remove-LeaseWaiter -Lease $lease -WaiterKey $To
            $state[$Resource] = $lease
        }

        $mode = if ($Pin) { 'pinned' } else { 'unpinned' }
        Write-Host "granted:  $Resource -> $To ($mode)"
        if ($inheritedDirty) {
            # Immediate console warning at operator time — the wrapper's next-tick probe will
            # also fire an operator-inbox notification (dedup key __lease_reclaim_acquire__/
            # <resource>, signature "$holder@$generation"), but the operator running this
            # script sees the console line first. Two paths, one narrative: "reclaim duty
            # inherited from <someone>." (msg-1876 blocker: preserve + surface.)
            $fromLabel = if ($inheritedFrom) { $inheritedFrom } else { '<unknown — no reclaimed_from on record>' }
            Write-Warning ("WARNING: lease $Resource carried reclaim_required=true into this grant " +
                          "(inherited from $fromLabel). $To MUST restart the physical resource " +
                          "(e.g. the editor / bridge session) before use — v1 is scheduling only, " +
                          "so a lingering old session is not killed by this script.")
        }
    }
    'Clear' {
        if (-not $state.ContainsKey($Resource)) {
            throw "no lease record for resource '$Resource' — nothing to clear (state keys: $(($state.Keys | Sort-Object) -join ', '))"
        }
        $lease = $state[$Resource]
        $priorHolder = "$($lease['holder'])"
        if (-not $PSCmdlet.ShouldProcess($Resource, "clear lease (prior holder: $priorHolder) — reason: $Reason")) { return }

        $priorGen = 1
        if ($lease.ContainsKey('generation') -and $null -ne $lease['generation']) {
            $priorGen = [int]$lease['generation']
        }
        $nowIso = $nowUtc.ToUniversalTime().ToString("o")
        $lease.Remove('holder')
        $lease['acquired_at']       = $null
        $lease['last_progress_at']  = $null
        $lease['idle_evaluations']  = 0
        $lease['generation']        = $priorGen + 1
        $lease['pinned']            = $false
        $lease['expiring']          = $false
        # PERMANENT audit paired with `reclaimed_from`. `reclaimed_reason` is what the digest
        # reads — its narrative ("operator-clear: <op-provided text>") survives any subsequent
        # state-machine transition (progress, drain-from-empty, self-hold re-acquire) that
        # would otherwise clear a transient field. See lib/Lease.ps1 file-header split
        # rationale (msg-1900).
        $lease['reclaimed_from']    = $priorHolder
        $lease['reclaimed_at']      = $nowIso
        $lease['reclaimed_reason']  = "operator-clear: $Reason"
        $lease['reclaim_required']  = $true
        # `revoked_*` are TRANSIENT Phase-1 intent (msg-1900 split). An operator Clear
        # supersedes any pending Phase-1 intent from an earlier probe cycle, so clear them.
        $lease['revoked_at']        = $null
        $lease['revoked_reason']    = $null
        $state[$Resource] = $lease

        Write-Host "cleared:  $Resource (prior holder: $priorHolder)"
    }
}

# Save. UTF-8 no BOM, ConvertTo-Json Depth 5 — same shape the wrapper writes with, so a mid-tick
# race against the sweep produces byte-compatible files.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($leasesStatePath, ($state | ConvertTo-Json -Depth 5), $utf8NoBom)

Write-Host "reason:   $Reason"
Write-Host "leases:   $leasesStatePath"
