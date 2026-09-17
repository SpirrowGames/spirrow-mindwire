# Regression guard for the exclusive-resource lease + queue (T-exclusive-resource-lease-queue,
# msg-1180 / 1183 / 1185 / 1187 design; msg-1188 Tier-C approval).
#
# What THIS PR covers (T-exclusive-resource-lease-queue PR 2 / 4 — acquire / classification /
# reader collapse; PR 1 landed state & persistence):
#   1. Merge-LeasesStateForWrite — the operator-write-mid-sweep survival semantics (per-resource
#      `generation` as an optimistic-concurrency token). This is the msg-1802 blocker fix.
#   2. Get-LeaseHolderClassification — the four cases (progress / parked / neutral) it must
#      produce for the current wrapper's decide verdicts + held/quarantined/absent inputs.
#      This is the ONE predicate the whole design's correctness turns on (msg-1187 §4 (ii)).
#   3. Update-LeaseFromClassification — 'progress' resets + clears TRANSIENT Phase-1 fields but
#      preserves PERMANENT audit; 'parked' increments; 'neutral' no-op.
#   4. Test-LeaseExpiring — DUAL predicate (idle_evaluations AND wall-clock); pin is TTL-immune.
#   5. Add-LeaseWaiter / Remove-LeaseWaiter — idempotent enqueue, no duplication.
#   6. Test-LeaseAvailableFor + Invoke-LeaseAcquire — the candidate-loop gate, self-hold is
#      available, no accidental steal throws.
#   7. Register-LeaseWaiter — creates the resource record if absent, enqueues.
#   8. Read-JsonStateWithShape / Get-JsonState shape guard / Save-CorruptedStateBackup — the
#      reader collapse (msg-2172 Tier-C, 2026-08-28). ROOT array / scalar JSON now returns empty
#      rather than leaking Count/Length/... metadata as fake resource keys. The 2026-08-28
#      measurement (archived at .git/mindwire-scratch/array-shape-probe-v2.ps1) confirmed the
#      pre-fix vector was a permanent one-way corruption; these tests pin the fixes.
#
# Queue sections (Get-NextLeaseWaiter, Invoke-LeasePromotion, Remove-IneligibleLeaseWaiters,
# Invoke-LeaseGrantFromEmpty) landed with PR 3 — see §13 (grant seam), §15 (Remove-Ineligible-
# LeaseWaiters), §16 (Invoke-LeaseGrantFromEmpty), and the merged Test-LeaseAvailableFor /
# Invoke-LeaseAcquire section §9 (positive-allowlist entry validation, row #6 shared helper).
# Wrapper AST checks land in PR 4 with the state-machine wiring.
#
# ---------------------------------------------------------------------------------------------
# PR 3 PIN CHECKLIST — this list is the durable materialisation of the msg-2644 §4 pin table
# (Bohr, T-exclusive-resource-lease-queue) plus Einstein's msg-2645 addition, promoted by the
# human's msg-2653 Tier-C ruling out of the chat log and into the test file so PR 3 cannot
# silently drop any item. Row bodies were subsequently revised by msg-2738 §3 (row #9 added
# for the dual-management deletion), msg-2738 §5 correction 1 (row #5 is documentation-only,
# msg-1959 asymmetric-contract wording), msg-2738 §5 correction 2 (row #6 mirror parameter
# name / untyped requirement), and msg-2932 §3 (row #6 replaced with the #6a-#6d positive
# allowlist body). Any row that PR 3 decides NOT to implement MUST be justified in the PR
# body and this comment updated in the same PR — silent deletion is the msg-923 failure
# this whole feature exists to prevent.
#
#   #1  end-to-end grant order — real grant FIFO across candidates (msg-1958 §5).
#   #2  no-steal invariant preserved in the grant path (msg-1958 §5).
#   #3  expiry -> reclaim -> next-waiter grant ordering (msg-1958 §5).
#   #4  TOCTOU pin — 'available' verdict followed by a competing acquire, our acquire is
#       refused by the no-steal throw (msg-1960 §5). Proves the "acquire MAY fail after
#       'available'" clause in Test-LeaseAvailableFor's docstring is load-bearing.
#   #5  DOCUMENTATION-ONLY (msg-2738 §5 correction 1). Test-LeaseAvailableFor's .OUTPUTS /
#       .DESCRIPTION docstring must spell out THREE things and the verdict domain MUST NOT
#       change (msg-1958 §4 single-seam rule):
#         a) ASYMMETRIC CONTRACT — 'available' is advisory (acquire MAY fail via TOCTOU);
#            'waiting' is binding.
#         b) THE 'available' COLLAPSE RATIONALE — 'available' intentionally folds "free" and
#            "held-by-self" together because both call sites take the same action (idempotent
#            acquire). PR 4 may split later if a real caller needs the distinction; PR 3
#            does not.
#         c) ORDERING REQUIREMENT (msg-2644 §2, verbatim in the docstring) — acquire MUST
#            succeed BEFORE any un-rollbackable side effect. The docstring cites the row #7
#            mechanical pin as the enforcement mechanism, per msg-2644 §3.
#       Cross-reference: entry validation is OWNED by row #6a (Assert-LeaseResourceName);
#       this row does not restate the accepted-input shape.
#   #6  POSITIVE-ALLOWLIST entry validation (msg-2932 §3, supersedes the msg-1961 mirror
#       wording). Split into four sub-items — all four are load-bearing:
#         #6a Both Invoke-LeaseAcquire's -Resource and Test-LeaseAvailableFor's -Requires
#             are UNTYPED (no [string]) and validated by the SHARED helper
#             Assert-LeaseResourceName. Accept iff (after PSObject unwrap) the value is
#             [string] AND not $null AND not empty AND not whitespace-only. Everything else
#             throws. A blocklist reading is FORBIDDEN — the type set is open.
#         #6b The rejected-set / accepted-set table is DRIVEN FROM A SINGLE fixture that
#             runs against BOTH functions (two hand-written copies WILL drift). Rejected:
#             multi-element array, single-element array (do NOT unwrap silently), hashtable,
#             integer, $true / $false, $null, '', '   ', arbitrary [pscustomobject]. Accepted:
#             'editor', a [string] arriving wrapped in PSObject (splat / pipeline path).
#         #6c DIRECT-ACQUIRE PIN — a call path reaching Invoke-LeaseAcquire WITHOUT ever
#             calling Test-LeaseAvailableFor MUST hit the same rejection set. Authorisation
#             lives in the mutation (msg-1959).
#         #6d PR BODY — one line per rejected input describing what the OLD [string]
#             annotation did with that value and what the NEW positive validation does, so
#             the change is demonstrably a net gain at the boundary rather than trading one
#             hole for three (msg-2746 blocking objection).
#   #7  ordering pin — inject a spy/stub for the un-rollbackable side-effect commands and
#       assert `called times = 0` on the acquire-failure path (msg-2644 §3). The pin is
#       LOAD-BEARING only if reversing the sequence in the caller under test actually breaks
#       it — see the revert-and-rerun receipt in §14 of THIS file.
#   #8  spy/stub coverage documentation — row #7's assertion is a NEGATIVE check bound to
#       SPECIFIC command names. In the test file itself, immediately next to the spy/stub
#       pin, list EVERY command name the pin actually guards (msg-2645 advisory, promoted
#       by msg-2653 Tier-C). At time of writing the known name is Invoke-HeadSkipCommitLaunch;
#       PR 3 MUST enumerate any additional un-rollbackable commands reachable from the
#       candidate loop (Update-LoopControlState, process-launch shims, sweep-state writers,
#       ...) or explicitly note "no other un-rollbackable commands reachable at PR 3 time"
#       so a PR 4 implementer who adds one immediately sees they have widened the safety
#       net's blind spot. Silent green under a renamed or newly-added un-rollbackable is
#       the failure mode this item exists to catch.
#   #9  DUAL-MANAGEMENT DELETION (msg-2738 §3, disposition of the msg-2735 advisory). PR 3
#       MUST delete (not shrink to a pointer) the "PR 3 open items that land in THIS file"
#       comment block that used to sit above Assert-LeaseResourceName in deploy/lib/Lease.ps1
#       — a "future work" heading over finished code reads as unfinished obligation. Row
#       #9a is verified in tree (the block is gone). Row #9b installs a single-source rule:
#       until PR 3 lands, if any PR-3 requirement wording is revised, THIS file's checklist
#       is canonical and the Lease.ps1-side prose (if any survives) is a pointer only —
#       never fork.
#
# ROW-9b ROUTING RULE for row bodies that live outside this checklist:
#   - #5's contract text is materialised in Test-LeaseAvailableFor's .DESCRIPTION / .OUTPUTS
#     docstring in deploy/lib/Lease.ps1. THIS file is canonical for the row's INTENT (what
#     must be documented); Lease.ps1 is where the docstring lives (WHERE the documentation
#     is rendered).
#   - #6's shared entry-validation helper is Assert-LeaseResourceName in deploy/lib/Lease.ps1
#     (msg-2932 §3 #6a). Both Test-LeaseAvailableFor and Invoke-LeaseAcquire call it; no
#     inline copy of the validation exists on either function (msg-2932 §3 #6c mandates
#     structural anti-drift, and a shared helper is that mechanism).
# The rest are pins that PR 3 adds to THIS file.
#
# READ-BACK BASIS (msg-2738 §5, obligation OBL-READBACK-ENTRY / OBL-READBACK-EXIT). The
# specifying messages for PR 3 are: msg-1958 §5, msg-1960 §3 / §5 / §6, msg-1961, msg-2644
# §2 / §3 / §4, msg-2645, msg-2653, msg-2738 §3 / §5 (rows #9 + row #6 correction + row #5
# scope), and msg-2932 §3 (rows #6a-#6d final wording). msg-2644 §4's table taken alone is
# a SUPERSEDED snapshot; the checklist above is the current basis, and each row cites its
# source msg-id(s). The OBL-READBACK-EXIT table in the PR body is 1:1 with rows #1-#9.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$leaseLib = Join-Path $repoRoot "deploy/lib/Lease.ps1"

if (-not (Test-Path -LiteralPath $leaseLib)) { throw "Lease lib not found: $leaseLib" }

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
function CheckFalse { param([string]$Name, $Actual) Check -Name $Name -Expected $false -Actual ([bool]$Actual) }
function CheckTrue  { param([string]$Name, $Actual) Check -Name $Name -Expected $true  -Actual ([bool]$Actual) }

# --- 1. New-LeaseRecord — the record shape callers rely on --------------------------------------
Write-Host "New-LeaseRecord — field defaults, reclaimed_at pairing, queue preservation"

$now = [datetime]::Parse('2026-08-27T00:00:00Z').ToUniversalTime()

# Fresh acquire on a free lease (no prior holder).
$rec = New-LeaseRecord -Holder 'p/T-a' -Now $now
Check "fresh: holder set"                'p/T-a' $rec.holder
Check "fresh: generation defaults to 1"  1       $rec.generation
Check "fresh: idle_evaluations=0"        0       $rec.idle_evaluations
Check "fresh: expiring=false"            $false  $rec.expiring
Check "fresh: pinned=false"              $false  $rec.pinned
Check "fresh: reclaim_required=false"    $false  $rec.reclaim_required
# reclaimed_from is a [string] param, so $null-default coerces to '' on binding — that is the
# on-disk shape too (empty string round-trips as empty). What matters for the digest is that
# it's falsy (no prior holder recorded); the digest treats '' the same as $null via `if`.
Check "fresh: reclaimed_from empty"      ''      $rec.reclaimed_from
Check "fresh: reclaimed_at=null (no reclamation happened)" $null $rec.reclaimed_at
Check "fresh: reclaimed_reason empty"    ''      $rec.reclaimed_reason
Check "fresh: revoked_at=null"           $null   $rec.revoked_at
Check "fresh: revoked_reason=null"       $null   $rec.revoked_reason
Check "fresh: queue empty"               0       $rec.queue.Count

# Promotion from queue (reclaimed_from set → reclaimed_at MUST be paired).
$rec2 = New-LeaseRecord -Holder 'p/T-b' -Now $now -Generation 6 `
    -ReclaimedFrom 'p/T-a' -ReclaimRequired $true `
    -ReclaimedReason 'human-clear: PIE crashed'
Check "promoted: holder set"                        'p/T-b' $rec2.holder
Check "promoted: generation as passed"              6       $rec2.generation
Check "promoted: reclaimed_from paired"             'p/T-a' $rec2.reclaimed_from
Check "promoted: reclaimed_at paired with reclaimed_from" $rec2.acquired_at $rec2.reclaimed_at
Check "promoted: reclaimed_reason preserved"        'human-clear: PIE crashed' $rec2.reclaimed_reason
Check "promoted: reclaim_required=true"             $true   $rec2.reclaim_required

# Queue preservation.
$q = @(@{ key = 'p/T-c'; waiting_since = '2026-08-26T00:00:00Z' })
$rec3 = New-LeaseRecord -Holder 'p/T-b' -Now $now -Queue $q
Check "queue preserved on new record" 1 $rec3.queue.Count
Check "queue element key preserved"   'p/T-c' $rec3.queue[0].key

# Pinned grant (operator TTL-immune).
$rec4 = New-LeaseRecord -Holder 'p/T-x' -Now $now -Pinned $true
Check "pinned: pinned=true" $true $rec4.pinned


# --- 2. ConvertTo-LeaseHashtable / ConvertTo-LeasesStateHashtable — JSON round-trip normalisation
Write-Host "ConvertTo-Lease*Hashtable — PSCustomObject from disk is normalised to hashtables"

# Simulate what Get-JsonState returns for a lease record with a queue (PSCustomObject through
# ConvertFrom-Json; the queue is an array of PSCustomObjects too).
$raw = @'
{
  "editor": {
    "holder": "p/T-a",
    "generation": 4,
    "queue": [
      {"key": "p/T-b", "waiting_since": "2026-08-26T00:00:00Z"}
    ]
  }
}
'@
$obj = $raw | ConvertFrom-Json
$map = @{}
foreach ($p in $obj.PSObject.Properties) { $map[$p.Name] = $p.Value }
$norm = ConvertTo-LeasesStateHashtable $map
Check "top-level entry normalised to hashtable" $true ($norm['editor'] -is [hashtable])
Check "queue entries normalised to hashtable"   $true ($norm['editor']['queue'][0] -is [hashtable])
Check "queue key preserved through normalisation" 'p/T-b' $norm['editor']['queue'][0]['key']

# Idempotence: passing an already-hashtable through returns the same shape.
$again = ConvertTo-LeaseHashtable -Lease $norm['editor']
Check "ConvertTo-LeaseHashtable is idempotent" $true ($again -is [hashtable])
Check "ConvertTo-LeaseHashtable idempotent: holder preserved" 'p/T-a' $again['holder']


# --- 3. Merge-LeasesStateForWrite — operator write mid-sweep survives (msg-1802 blocker #1) -----
#
# leases.json's top-level key is the resource name. When wiring lands (PR 4), the sweep's probe
# will mutate that key at tick start (last_progress_at, idle_evaluations, expiring). If an
# operator runs Grant-Lease.ps1 mid-sweep and writes a new holder for the same key to disk, the
# generic Merge-StateForWrite (which merges at top-level key granularity and lets memory win on
# collision) would silently destroy the operator's Tier-C override. Merge-LeasesStateForWrite
# uses per-resource `generation` as an optimistic-concurrency token to detect the external write.
#
# Scenario C ("sweep did an acquire and wins") requires Invoke-LeaseAcquire, which lands in PR 2;
# that scenario is added to this section when PR 2 lands. A/B/D/E/F/G/H cover the merger's own
# logic in isolation and are complete without any other function.
#   - F: msg-2103 external deletion mid-sweep (the merger must not resurrect the deleted key).
#   - G: msg-2114 corrupt-scalar fall-back (a hand-edit typo must not abort the flush).
#   - H: msg-2131 sweep-side deletion (mirror of F — a sweep-freed lease with an empty queue
#     must not be silently resurrected from stale disk state).
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
        editor = @{ holder = 'p/T-b'; generation = 6; last_progress_at = '2026-08-26T04:59:00Z'; idle_evaluations = 0; queue = @(); reclaimed_reason = 'human-grant: emergency' }
    }
    Save-FixtureLeases -Path $fixturePath -State $diskB1
    $mergedB = Merge-LeasesStateForWrite -Memory $memoryB -OriginalGenerations $originalGens -DiskPath $fixturePath
    Check "operator-write: disk holder wins over sweep memory" 'p/T-b' $mergedB['editor'].holder
    Check "operator-write: disk generation wins" 6 $mergedB['editor'].generation
    Check "operator-write: disk reclaimed_reason preserved (msg-1900 audit split)" 'human-grant: emergency' $mergedB['editor'].reclaimed_reason

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

    # --- scenario F: operator DELETION mid-sweep (msg-2103 blocker). The schema treats an
    # absent key as "no holder, empty queue" — an operator that empties both may legitimately
    # remove the key entirely. The merger must NOT resurrect it from stale memory. The bug the
    # earlier `$current.ContainsKey($resource) -and $diskGen -ne $priorGen` condition hid was
    # exactly this: no key on disk meant the mismatch check was skipped and memory silently
    # won, reversing the operator's -Clear. Fix: mismatch alone decides; deletion produces
    # diskGen=-1 vs a live priorGen, which is a mismatch, and the key correctly stays absent.
    $diskF0 = @{
        editor = @{ holder = 'p/T-a'; generation = 5; last_progress_at = '2026-08-25T00:00:00Z'; idle_evaluations = 0; queue = @() }
    }
    Save-FixtureLeases -Path $fixturePath -State $diskF0
    $memoryF = ConvertTo-LeasesStateHashtable (Get-JsonState -Path $fixturePath)
    $originalGensF = Get-LeaseGenerations -LeasesState $memoryF
    # Sweep touches memory (probe clock bump).
    $memoryF['editor']['last_progress_at'] = '2026-08-26T05:00:00Z'
    # Operator ran Grant-Lease.ps1 -Clear, emptied the queue, and removed the key entirely.
    Save-FixtureLeases -Path $fixturePath -State @{}
    $mergedF = Merge-LeasesStateForWrite -Memory $memoryF -OriginalGenerations $originalGensF -DiskPath $fixturePath
    Check "operator-deletion: resurrected key must be ABSENT (schema: absent = free)" $false $mergedF.ContainsKey('editor')
    Check "operator-deletion: merged state has no other resurrected keys either"      0      $mergedF.Keys.Count

    # --- scenario F': deletion + concurrent memory-added new resource. The deletion is honoured
    # (editor stays absent) AND a truly-new-in-memory key (runner) writes through. This proves
    # the "new-in-memory" case (priorGen=-1, diskGen=-1) is not accidentally routed through the
    # deletion branch.
    $diskFp0 = @{
        editor = @{ holder = 'p/T-a'; generation = 5; queue = @() }
    }
    Save-FixtureLeases -Path $fixturePath -State $diskFp0
    $memoryFp = ConvertTo-LeasesStateHashtable (Get-JsonState -Path $fixturePath)
    $originalGensFp = Get-LeaseGenerations -LeasesState $memoryFp
    # Sweep this tick: acquired the fresh 'runner' resource for the first time.
    $memoryFp['runner'] = @{ holder = 'p/T-x'; generation = 1; queue = @() }
    # Operator deleted 'editor' mid-sweep.
    Save-FixtureLeases -Path $fixturePath -State @{}
    $mergedFp = Merge-LeasesStateForWrite -Memory $memoryFp -OriginalGenerations $originalGensFp -DiskPath $fixturePath
    Check "deletion + new-in-memory: deleted key stays absent" $false $mergedFp.ContainsKey('editor')
    Check "deletion + new-in-memory: fresh key writes through" 'p/T-x' $mergedFp['runner'].holder
    Check "deletion + new-in-memory: fresh key generation preserved" 1 $mergedFp['runner'].generation

    # --- scenario H: SWEEP-side deletion (msg-2131 blocker). The mirror of scenario F. The
    # schema commits: "ABSENT resource key = no holder, empty queue. Do NOT create empty stub
    # entries." When the sweep frees a lease and drains its queue, the caller REMOVES the key
    # from its in-memory map. Without a second pass in the merger, that deletion is invisible
    # (Memory.Keys no longer contains it, and $out was seeded from disk which still has it) —
    # so the flush silently resurrects the old lease from stale disk state.
    $diskH0 = @{
        editor = @{ holder = 'p/T-a'; generation = 5; last_progress_at = '2026-08-25T00:00:00Z'; idle_evaluations = 0; queue = @() }
    }
    Save-FixtureLeases -Path $fixturePath -State $diskH0
    $memoryH = ConvertTo-LeasesStateHashtable (Get-JsonState -Path $fixturePath)
    $originalGensH = Get-LeaseGenerations -LeasesState $memoryH
    # Sweep freed the lease (TTL expiry, no waiter to promote) and dropped the stub, per schema.
    $memoryH.Remove('editor') | Out-Null
    # Disk unchanged (no external write).
    $mergedH = Merge-LeasesStateForWrite -Memory $memoryH -OriginalGenerations $originalGensH -DiskPath $fixturePath
    Check "sweep-deletion (uncontested): key must be ABSENT from merged" $false $mergedH.ContainsKey('editor')
    Check "sweep-deletion (uncontested): merged has no other stray keys"  0 $mergedH.Keys.Count

    # --- scenario H': sweep-side deletion COLLIDES with concurrent external write. External
    # write wins on the same generation tie-break as the mutation path — the sweep's deletion
    # is dropped in favour of the operator's Tier-C decision.
    $diskHp0 = @{
        editor = @{ holder = 'p/T-a'; generation = 5; queue = @() }
    }
    Save-FixtureLeases -Path $fixturePath -State $diskHp0
    $memoryHp = ConvertTo-LeasesStateHashtable (Get-JsonState -Path $fixturePath)
    $originalGensHp = Get-LeaseGenerations -LeasesState $memoryHp
    # Sweep decided to free 'editor' and dropped the stub.
    $memoryHp.Remove('editor') | Out-Null
    # Meanwhile operator ran Grant-Lease.ps1 -To p/T-c and bumped disk to gen 6.
    $diskHp1 = @{
        editor = @{ holder = 'p/T-c'; generation = 6; queue = @(); reclaimed_reason = 'human-grant: urgent' }
    }
    Save-FixtureLeases -Path $fixturePath -State $diskHp1
    $mergedHp = Merge-LeasesStateForWrite -Memory $memoryHp -OriginalGenerations $originalGensHp -DiskPath $fixturePath
    Check "sweep-deletion vs operator-write: operator wins" 'p/T-c' $mergedHp['editor'].holder
    Check "sweep-deletion vs operator-write: operator generation wins" 6 $mergedHp['editor'].generation

    # --- scenario H'': sweep-side deletion AND external deletion of the same key. Both parties
    # agree; the key must stay absent. Also confirms that .Remove() on an absent $out entry is
    # harmless (no throw).
    $diskHpp0 = @{
        editor = @{ holder = 'p/T-a'; generation = 5; queue = @() }
    }
    Save-FixtureLeases -Path $fixturePath -State $diskHpp0
    $memoryHpp = ConvertTo-LeasesStateHashtable (Get-JsonState -Path $fixturePath)
    $originalGensHpp = Get-LeaseGenerations -LeasesState $memoryHpp
    $memoryHpp.Remove('editor') | Out-Null      # sweep also freed
    Save-FixtureLeases -Path $fixturePath -State @{}  # operator also cleared
    $threwHpp = $false
    $mergedHpp = $null
    try {
        $mergedHpp = Merge-LeasesStateForWrite -Memory $memoryHpp -OriginalGenerations $originalGensHpp -DiskPath $fixturePath
    }
    catch { $threwHpp = $true }
    CheckFalse "sweep+external deletion agreement: does NOT throw" $threwHpp
    if (-not $threwHpp) {
        Check "sweep+external deletion agreement: key absent" $false $mergedHpp.ContainsKey('editor')
    }

    # --- scenario E: Get-LeaseGenerations edge cases ---
    $gens = Get-LeaseGenerations -LeasesState @{}
    Check "Get-LeaseGenerations: empty map -> empty" 0 $gens.Keys.Count
    $gens = Get-LeaseGenerations -LeasesState @{ editor = @{ generation = 7 }; runner = @{ } }
    Check "Get-LeaseGenerations: editor gen extracted" 7 $gens['editor']
    Check "Get-LeaseGenerations: missing generation -> 0" 0 $gens['runner']

    # --- scenario G: corrupt generation values (msg-2114 blocker). The Get-JsonState header
    # commits to "a corrupt state file falls back to empty rather than aborting the flush — the
    # sweep must not fail closed for a JSON syntax hiccup". ConvertFrom-Json accepts values that
    # are structurally valid JSON but semantically wrong for the schema — an operator hand-edit
    # can leave `"generation": ""` or `"generation": "pending"`. A hard `[int]` cast raises a
    # RuntimeException, which bubbles out of Merge-LeasesStateForWrite and aborts the flush.
    # Fix: -as [int] returns $null instead of throwing, and the fallback of 0 lets the merger
    # continue.
    $corruptCases = @(
        @{ label = "empty string"; value = '' }
        @{ label = "non-numeric string"; value = 'pending' }
        @{ label = "hashtable"; value = @{ nested = 'bad' } }
        @{ label = "array"; value = @(1, 2) }
    )
    foreach ($case in $corruptCases) {
        $gensC = $null
        $threw = $false
        try {
            $gensC = Get-LeaseGenerations -LeasesState @{ editor = @{ generation = $case.value } }
        }
        catch { $threw = $true }
        CheckFalse "Get-LeaseGenerations: corrupt generation ($($case.label)) does NOT throw" $threw
        if (-not $threw) {
            Check "Get-LeaseGenerations: corrupt generation ($($case.label)) falls back to 0" 0 $gensC['editor']
        }
    }

    # And the end-to-end path: corrupt disk value must not abort the flush.
    $diskG = @{
        editor = @{ holder = 'p/T-a'; generation = 'pending'; queue = @() }
    }
    Save-FixtureLeases -Path $fixturePath -State $diskG
    $memoryG = ConvertTo-LeasesStateHashtable (Get-JsonState -Path $fixturePath)
    $originalGensG = Get-LeaseGenerations -LeasesState $memoryG
    $memoryG['editor']['last_progress_at'] = '2026-08-27T00:00:00Z'
    $mergedG = $null
    $threwG = $false
    try {
        $mergedG = Merge-LeasesStateForWrite -Memory $memoryG -OriginalGenerations $originalGensG -DiskPath $fixturePath
    }
    catch { $threwG = $true }
    CheckFalse "Merge-LeasesStateForWrite: corrupt disk generation does NOT abort flush" $threwG
}
finally {
    Remove-Item -LiteralPath $fixtureDir -Recurse -Force -ErrorAction SilentlyContinue
}


# --- 4. Get-LeaseSummaryLines — digest render (msg-1185 §3-1 + msg-1900 audit split) ------------
Write-Host "Get-LeaseSummaryLines — digest render for the daily-digest lease section"

$format = { param($ts) "{0:0}min" -f $ts.TotalMinutes }
$digestNow = [datetime]::Parse('2026-08-27T00:00:00Z').ToUniversalTime()

# Empty state — the digest still speaks (silent-day-is-the-point, msg-814 §5). The wrapper
# writes non-ASCII into that line, so we assert the SHAPE (exactly one line, indented) rather
# than the literal bytes — the shape is what the daily-digest layout depends on, and it lets
# this file stay ASCII across Windows console codepages.
$lines = @(Get-LeaseSummaryLines -LeasesState @{} -Now $digestNow -FormatDuration $format)
Check "empty state produces exactly one summary line" 1 $lines.Count
Check "empty state line is indented (digest layout)"  $true ($lines[0].StartsWith('  '))

# One free lease + one held-and-queued.
$state = @{
    editor = @{
        holder = 'p/T-b'
        acquired_at = '2026-08-26T23:00:00Z'
        pinned = $false
        expiring = $false
        reclaim_required = $true
        reclaimed_from = 'p/T-a'
        reclaimed_reason = 'human-clear: PIE crashed'
        queue = @(@{ key = 'p/T-c'; waiting_since = '2026-08-26T23:30:00Z' })
    }
    runner = @{
        holder = ''
        queue = @()
    }
}
$lines = Get-LeaseSummaryLines -LeasesState $state -Now $digestNow -FormatDuration $format
$joined = ($lines -join "`n")

Check "held lease renders holder + age (60min)"        $true ($joined -match 'editor: p/T-b .*60min')
Check "reclaim-required flag surfaces"                 $true ($joined -match '\[reclaim-required\]')
Check "queue line lists waiter key"                    $true ($joined -match 'queue: p/T-c')
Check "reclaimed line uses reclaimed_reason (msg-1900)" $true ($joined -match 'reclaimed: p/T-a .* p/T-b \(reason=human-clear: PIE crashed\)')
Check "free lease renders (free)"                      $true ($joined -match 'runner: \(free\)')

# msg-1900 audit split: when reclaimed_reason is present, it is preferred over revoked_reason
# (which is transient Phase-1 intent and may have been overwritten).
$state2 = @{
    editor = @{
        holder = 'p/T-b'
        reclaimed_from = 'p/T-a'
        reclaimed_reason = 'human-clear: crash'
        revoked_reason = 'idle'
        queue = @()
    }
}
$lines = Get-LeaseSummaryLines -LeasesState $state2 -Now $digestNow -FormatDuration $format
Check "reclaimed_reason preferred over revoked_reason" $true (($lines -join "`n") -match 'reason=human-clear: crash')

# msg-2114 blocker follow-on: bool coercion must not fall for the [bool]"false" -> $true trap.
# PowerShell's [bool] cast on strings is length-based ("false" is non-empty ∴ truthy), so an
# operator hand-editing leases.json with `"pinned": "false"` used to render `[pinned]` on the
# digest. The safe helper accepts only real booleans and recognised string values; anything
# else falls back to $false so a corrupt cell stays blank rather than lying.
$stateBool = @{
    editor = @{ holder = 'p/T-b'; pinned = 'false'; expiring = 'false'; reclaim_required = 'false'; queue = @() }
    runner = @{ holder = 'p/T-c'; pinned = 'true';  expiring = 'true';  reclaim_required = 'true';  queue = @() }
    third  = @{ holder = 'p/T-d'; pinned = 'garbage'; expiring = 'nope'; reclaim_required = ''; queue = @() }
}
$linesBool = Get-LeaseSummaryLines -LeasesState $stateBool -Now $digestNow -FormatDuration $format
$joinedBool = ($linesBool -join "`n")
# The string literals `"true"` / `"false"` in JSON round-trip to strings, not booleans. Ensure
# per-lease flag rendering respects the strings' meaning, not their length.
CheckFalse "string 'false' does NOT render as [pinned] (was the [bool] cast bug)" ($joinedBool -match 'editor:.*\[.*pinned.*\]')
CheckFalse "string 'false' does NOT render as [expiring]"                          ($joinedBool -match 'editor:.*\[.*expiring.*\]')
CheckFalse "string 'false' does NOT render as [reclaim-required]"                  ($joinedBool -match 'editor:.*\[.*reclaim-required.*\]')
CheckTrue  "string 'true'  DOES render as [pinned]"           ($joinedBool -match 'runner:.*\[.*pinned.*\]')
CheckTrue  "string 'true'  DOES render as [expiring]"         ($joinedBool -match 'runner:.*\[.*expiring.*\]')
CheckTrue  "string 'true'  DOES render as [reclaim-required]" ($joinedBool -match 'runner:.*\[.*reclaim-required.*\]')
# Un-parseable strings fall back to $false (the safe default: silent-blank is better than a
# false-positive flag that misleads the operator).
CheckFalse "string 'garbage' falls back to false (pinned stays off)"   ($joinedBool -match 'third:.*\[.*pinned.*\]')
CheckFalse "string 'nope' falls back to false (expiring stays off)"    ($joinedBool -match 'third:.*\[.*expiring.*\]')
CheckFalse "empty string falls back to false (reclaim-required off)"   ($joinedBool -match 'third:.*\[.*reclaim-required.*\]')
# Real booleans still round-trip correctly (regression pin for the happy path).
$stateBoolReal = @{ editor = @{ holder = 'p/T-b'; pinned = $true; queue = @() } }
$linesBoolReal = Get-LeaseSummaryLines -LeasesState $stateBoolReal -Now $digestNow -FormatDuration $format
CheckTrue "real $true still renders as [pinned]" (($linesBoolReal -join "`n") -match '\[pinned\]')

# msg-2124 blocker: a null lease record (operator hand-edit leaves `"editor": null` as a
# placeholder) round-trips through ConvertTo-LeaseHashtable as $null. The old code called
# $lease.ContainsKey('holder') on $null, raising a RuntimeException and aborting the whole
# digest render. Fix: null-guard skips the entry entirely — corrupt cell renders nothing
# rather than taking the whole digest down (same failure class as the R2 [int]/[bool] casts,
# same failure-open response).
$stateNullLease = @{
    editor = $null
    runner = @{ holder = 'p/T-x'; queue = @() }
}
$threwNull = $false
$linesNull = $null
try {
    $linesNull = Get-LeaseSummaryLines -LeasesState $stateNullLease -Now $digestNow -FormatDuration $format
}
catch { $threwNull = $true }
CheckFalse "null lease record does NOT abort Get-LeaseSummaryLines" $threwNull
if (-not $threwNull) {
    $joinedNull = ($linesNull -join "`n")
    # The healthy lease still renders — one bad entry must not silence the rest of the digest.
    CheckTrue  "sibling healthy lease still renders around a null entry" ($joinedNull -match 'runner: p/T-x')
    # The null-valued key is skipped rather than rendered as garbage.
    CheckFalse "null-valued key does not appear in the digest at all"     ($joinedNull -match 'editor:')
}

# Also the fully-null state (someone truncated the file to `{}` with only null values, or
# passed an all-null map from a corrupt disk read): the empty-state fallback should still run
# because no live entries render.
$stateAllNull = @{ editor = $null; runner = $null }
$threwAllNull = $false
$linesAllNull = $null
try {
    $linesAllNull = @(Get-LeaseSummaryLines -LeasesState $stateAllNull -Now $digestNow -FormatDuration $format)
}
catch { $threwAllNull = $true }
CheckFalse "all-null state does NOT abort Get-LeaseSummaryLines" $threwAllNull
if (-not $threwAllNull) {
    # Two null entries produce zero live lines. The current design does NOT fall back to the
    # "(該当なし)" empty-state line in this case — that line is only rendered when the map is
    # keyless. Two null entries render as two skipped iterations, so the return is empty.
    # Test that assertion literally: no lines are emitted rather than crashing.
    Check "all-null state produces no rendered lines" 0 $linesAllNull.Count
}

# --- 5. Get-LeaseHolderClassification -----------------------------------------------------------
Write-Host ""
Write-Host "Get-LeaseHolderClassification — progress / parked / neutral trichotomy"

function New-Verdict { param([string]$Decision) return [pscustomobject]@{ decision = $Decision } }

# Held pauses the TTL clock (msg-1183 D-6'c).
Check "held holder -> progress (keeps lease alive)" 'progress' `
    (Get-LeaseHolderClassification -HolderKey 'p/T-a' -IsHeld $true -IsQuarantined $false -IsOnSweep $true -Verdict $null)
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

# Unknown decision — degrades to neutral (do no harm) rather than throwing.
Check "unknown verdict -> neutral (do no harm)" 'neutral' `
    (Get-LeaseHolderClassification -HolderKey 'p/T-a' -IsHeld $false -IsQuarantined $false -IsOnSweep $true -Verdict (New-Verdict 'something-unexpected'))

# --- 6. Update-LeaseFromClassification -----------------------------------------------------------
Write-Host ""
Write-Host "Update-LeaseFromClassification — parked increments, progress resets, neutral no-op"

$now2 = [DateTime]::Parse('2026-08-28T00:00:00Z').ToUniversalTime()

$lease = @{ idle_evaluations = 3; last_progress_at = '2020-01-01T00:00:00Z' }
Update-LeaseFromClassification -Lease $lease -Classification 'progress' -Now $now2
Check "progress resets idle_evaluations to 0" 0 $lease['idle_evaluations']
CheckTrue "progress updates last_progress_at" ([bool]("$($lease['last_progress_at'])" -match '^20\d\d'))

$lease = @{ idle_evaluations = 3; last_progress_at = '2020-01-01T00:00:00Z' }
Update-LeaseFromClassification -Lease $lease -Classification 'parked' -Now $now2
Check "parked increments idle_evaluations" 4 $lease['idle_evaluations']
Check "parked does NOT update last_progress_at" '2020-01-01T00:00:00Z' $lease['last_progress_at']

$lease = @{ idle_evaluations = 3; last_progress_at = '2020-01-01T00:00:00Z' }
Update-LeaseFromClassification -Lease $lease -Classification 'neutral' -Now $now2
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
    # msg-1900 blocker: PERMANENT audit fields must survive progress. Set them here on the
    # fixture so we can verify the split.
    reclaimed_from   = 'p/T-prior'
    reclaimed_at     = '2026-08-24T00:00:00Z'
    reclaimed_reason = 'human-clear: PIE crashed'
}
Update-LeaseFromClassification -Lease $lease -Classification 'progress' -Now $now2
CheckFalse "progress clears stale expiring flag (msg-1757 blocker #1)" ([bool]$lease['expiring'])
Check "progress clears stale revoked_at (TRANSIENT Phase-1 intent)" $null $lease['revoked_at']
Check "progress clears stale revoked_reason (TRANSIENT Phase-1 intent)" $null $lease['revoked_reason']
# msg-1900 blocker: PERMANENT audit fields must NOT be cleared by progress. If they were, the
# digest would render 'idle' instead of the operator's Tier-C Clear-reason.
Check "progress PRESERVES reclaimed_from (msg-1900: permanent audit)" 'p/T-prior' $lease['reclaimed_from']
Check "progress PRESERVES reclaimed_at (msg-1900: permanent audit)" '2026-08-24T00:00:00Z' $lease['reclaimed_at']
Check "progress PRESERVES reclaimed_reason (msg-1900: permanent audit)" 'human-clear: PIE crashed' $lease['reclaimed_reason']

# Null lease is a no-op (defensive against upstream normalisation bugs).
$prior = @{ idle_evaluations = 5 }
Update-LeaseFromClassification -Lease $null -Classification 'parked' -Now $now2
Check "null lease: no-op, does not throw" 5 $prior['idle_evaluations']

# --- 7. Test-LeaseExpiring — dual predicate (msg-1183 D-6'b) -------------------------------------
Write-Host ""
Write-Host "Test-LeaseExpiring — BOTH idle_evaluations AND wall-clock, pin immune"

$farPast = $now2.AddHours(-3).ToUniversalTime().ToString("o")
$recent  = $now2.AddMinutes(-10).ToUniversalTime().ToString("o")

# Both gates open, not pinned -> expiring.
$lease = @{ holder = 'p/T-a'; idle_evaluations = 6; last_progress_at = $farPast; pinned = $false }
CheckTrue "6 idle + 3h wall + not-pinned -> expiring" `
    (Test-LeaseExpiring -Lease $lease -IdleEvaluationsMin 6 -IdleTtl ([TimeSpan]::FromHours(2)) -Now $now2)

# Idle count below threshold -> NOT expiring (split-brain guard: both gates required).
$lease = @{ holder = 'p/T-a'; idle_evaluations = 5; last_progress_at = $farPast; pinned = $false }
CheckFalse "5 idle + 3h wall (idle below min) -> NOT expiring" `
    (Test-LeaseExpiring -Lease $lease -IdleEvaluationsMin 6 -IdleTtl ([TimeSpan]::FromHours(2)) -Now $now2)

# Wall-clock too recent -> NOT expiring (dual predicate again).
$lease = @{ holder = 'p/T-a'; idle_evaluations = 10; last_progress_at = $recent; pinned = $false }
CheckFalse "10 idle + 10m wall (wall too short) -> NOT expiring" `
    (Test-LeaseExpiring -Lease $lease -IdleEvaluationsMin 6 -IdleTtl ([TimeSpan]::FromHours(2)) -Now $now2)

# Pinned -> immune regardless (msg-1183 D-7).
$lease = @{ holder = 'p/T-a'; idle_evaluations = 100; last_progress_at = $farPast; pinned = $true }
CheckFalse "pinned lease is TTL-immune even at high idle + long wall" `
    (Test-LeaseExpiring -Lease $lease -IdleEvaluationsMin 6 -IdleTtl ([TimeSpan]::FromHours(2)) -Now $now2)

# Missing last_progress_at -> not expiring (fresh acquire with no probe yet).
$lease = @{ holder = 'p/T-a'; idle_evaluations = 100; pinned = $false }
CheckFalse "no last_progress_at -> not expiring (freshly acquired)" `
    (Test-LeaseExpiring -Lease $lease -IdleEvaluationsMin 6 -IdleTtl ([TimeSpan]::FromHours(2)) -Now $now2)

# No holder -> not expiring (empty leases don't expire; they wait for grant-from-empty in PR 3).
$lease = @{ holder = $null; idle_evaluations = 100; last_progress_at = $farPast; pinned = $false }
CheckFalse "no holder -> not expiring (free lease has nothing to expire)" `
    (Test-LeaseExpiring -Lease $lease -IdleEvaluationsMin 6 -IdleTtl ([TimeSpan]::FromHours(2)) -Now $now2)

# Null lease -> not expiring (defensive).
CheckFalse "null lease -> not expiring (defensive)" `
    (Test-LeaseExpiring -Lease $null -IdleEvaluationsMin 6 -IdleTtl ([TimeSpan]::FromHours(2)) -Now $now2)

# --- 8. Add/Remove-LeaseWaiter — idempotent enqueue ---------------------------------------------
Write-Host ""
Write-Host "Add/Remove-LeaseWaiter — idempotent enqueue, no duplication"

$lease = @{ queue = @() }
Add-LeaseWaiter -Lease $lease -WaiterKey 'p/T-w1' -Now $now2
Add-LeaseWaiter -Lease $lease -WaiterKey 'p/T-w2' -Now $now2.AddSeconds(1)
Add-LeaseWaiter -Lease $lease -WaiterKey 'p/T-w1' -Now $now2.AddSeconds(2)   # duplicate — must NOT add again
Check "duplicate enqueue is idempotent" 2 $lease['queue'].Count

# Wrap in @() — a Where-Object that returns exactly one item collapses to the item itself, and
# indexing into a lone hashtable with [0] yields $null.
$firstWaitingSince = (@($lease['queue'] | Where-Object { $_.key -eq 'p/T-w1' })[0]).waiting_since
Check "first enqueue's waiting_since is NOT refreshed by a re-enqueue" $now2.ToUniversalTime().ToString("o") $firstWaitingSince

Remove-LeaseWaiter -Lease $lease -WaiterKey 'p/T-w1'
Check "remove drops the entry" 1 $lease['queue'].Count
Check "remaining entry is w2" 'p/T-w2' $lease['queue'][0].key
Remove-LeaseWaiter -Lease $lease -WaiterKey 'p/T-not-there'   # no-op on absent
Check "remove of absent key is a no-op" 1 $lease['queue'].Count

# Missing queue key on the lease: Add creates it, Remove is a no-op.
$lease = @{}
Add-LeaseWaiter -Lease $lease -WaiterKey 'p/T-w1' -Now $now2
Check "Add on missing queue: creates the queue" 1 $lease['queue'].Count
$lease = @{}
Remove-LeaseWaiter -Lease $lease -WaiterKey 'p/T-w1'
CheckFalse "Remove on missing queue: no-op (does not add a queue key)" ($lease.ContainsKey('queue'))

# msg-2181 pin: the FIFO consequence of the docstring's "waiting_since preserved on re-enqueue"
# promise. Enqueue A first, B second; then re-enqueue A many times with progressively fresher
# timestamps (this simulates the ~5-min sweep tick spamming Register-LeaseWaiter → Add-LeaseWaiter
# on a candidate that keeps declaring `requires: editor` while the lease is held). If a future
# refactor "helpfully" refreshed `waiting_since` on the idempotent branch, A's timestamp would
# leapfrog B's every 5 min and grant-time FIFO (msg-1183 D-3) would collapse to "last re-attempt
# wins" — the exact failure mode the naysayer's R1 objection warned about. Pin the invariant here
# where Get-NextLeaseWaiter is not yet available (that lands in PR 3): sort keys on waiting_since
# and confirm A remains ordered before B despite the re-enqueue storm.
$lease = @{ queue = @() }
$tsA = $now2
$tsB = $now2.AddSeconds(30)
Add-LeaseWaiter -Lease $lease -WaiterKey 'p/T-A' -Now $tsA
Add-LeaseWaiter -Lease $lease -WaiterKey 'p/T-B' -Now $tsB
# Spam re-enqueues of A with fresher and fresher timestamps.
foreach ($minsLater in 5, 10, 15, 20, 25) {
    Add-LeaseWaiter -Lease $lease -WaiterKey 'p/T-A' -Now $tsA.AddMinutes($minsLater)
}
Check "FIFO pin: 2 waiters after re-enqueue storm (no duplicates)" 2 $lease['queue'].Count
$aSince = (@($lease['queue'] | Where-Object { $_.key -eq 'p/T-A' })[0]).waiting_since
$bSince = (@($lease['queue'] | Where-Object { $_.key -eq 'p/T-B' })[0]).waiting_since
Check "FIFO pin: A's waiting_since is EXACTLY the original (msg-2181 blocker)" `
    $tsA.ToUniversalTime().ToString("o") $aSince
Check "FIFO pin: B's waiting_since is EXACTLY the original" `
    $tsB.ToUniversalTime().ToString("o") $bSince
# The load-bearing relation: A remains ordered before B on waiting_since. If the docstring
# claim were false (refresh on re-enqueue), A's timestamp would be $tsA.AddMinutes(25) — LATER
# than B's — and this check would fail. This is the check that catches a hypothetical
# regression the naysayer's R1 flagged.
$aInstant = [datetime]::Parse($aSince).ToUniversalTime()
$bInstant = [datetime]::Parse($bSince).ToUniversalTime()
CheckTrue "FIFO pin: A remains ordered BEFORE B on waiting_since (collapse-guard)" ($aInstant -lt $bInstant)

# --- 9. Test-LeaseAvailableFor + Invoke-LeaseAcquire ---------------------------------------------
Write-Host ""
Write-Host "Test-LeaseAvailableFor + Invoke-LeaseAcquire — the candidate-loop gate"

$state = @{}
# NOTE (msg-2185): Requires is now a SINGLE STRING, not an array. PowerShell's parameter binding
# used to accept `@('editor')` and silently coerce it, which read as "supports multi-resource" —
# a false contract the state machine cannot honour. The type is now `[string]$Requires`, so
# tests pass 'editor' directly.
$check = Test-LeaseAvailableFor -LeasesState $state -CandidateKey 'p/T-a' -Requires 'editor'
Check "empty state: available" 'available' $check.status

# Acquire on empty state creates the record.
Invoke-LeaseAcquire -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-a' -Now $now2
Check "acquire creates the record" 'p/T-a' $state['editor']['holder']
Check "acquire generation is 1" 1 $state['editor']['generation']

# Second candidate needs the same lease -> waiting.
$check = Test-LeaseAvailableFor -LeasesState $state -CandidateKey 'p/T-b' -Requires 'editor'
Check "second candidate: waiting" 'waiting' $check.status
Check "waiting: waitOn names the resource" 'editor' $check.waitOn[0]
Check "waiting: holders reports the current one" 'p/T-a' $check.holders['editor']
Check "waiting: waitOn has exactly one element (single-resource contract)" 1 $check.waitOn.Count

# Same candidate re-checking its own lease -> available (self-hold).
$check = Test-LeaseAvailableFor -LeasesState $state -CandidateKey 'p/T-a' -Requires 'editor'
Check "self-hold: available" 'available' $check.status
Check "self-hold: waitOn is empty" 0 $check.waitOn.Count

# Idempotent re-acquire on self-hold: bumps generation? no — record kept, clocks refresh separately.
$genBefore = $state['editor']['generation']
Invoke-LeaseAcquire -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-a' -Now $now2.AddMinutes(5)
Check "acquire on self-hold: generation unchanged (idempotent)" $genBefore $state['editor']['generation']

# Invoke-LeaseAcquire on a foreign lease throws (no-steal invariant).
$threw = $false
try { Invoke-LeaseAcquire -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-b' -Now $now2 }
catch { $threw = $true }
CheckTrue "acquire on foreign lease refuses (throws)" $threw

# --- ROW #6 POSITIVE-ALLOWLIST ENTRY VALIDATION (msg-2932 §3 #6a-#6d) ---------------------------
# Rewrite of the msg-2189 mirror pins. The prior wording admitted null / empty / single-element
# array as "trivially available"; msg-2746 raised the layer-2 hole (untyped $Resource + blocklist
# validation would let $null / $true / [pscustomobject] slip through), and msg-2932 §3 replaced
# the row with a POSITIVE allowlist: accept iff the value is [string] AND not null AND not
# empty/whitespace. Everything else throws.
#
# Row #6b mandates a SINGLE fixture driving both functions so a rejection cannot exist on one
# function and not the other. Row #6c mandates that a DIRECT-ACQUIRE path (Invoke-LeaseAcquire
# called without Test-LeaseAvailableFor first) hits the same rejection set. Row #6a mandates
# that this validation is implemented by a SHARED helper — the row-6b fixture below is the
# executable enforcement of that shared implementation.
#
# The rejected-set table is the msg-2932 §3 #6b table verbatim. Do NOT duplicate the entries
# below across two hand-written loops — the loop drives both functions from ONE table.

$leaseRejectedSet = @(
    @{ label = "multi-element array (2, R3 case)"; value = @('editor','runner') }
    @{ label = "multi-element array (3)";          value = @('editor','runner','gpu') }
    @{ label = "single-element array (do NOT unwrap silently)"; value = @('editor') }
    @{ label = "empty array";                      value = @() }
    @{ label = "hashtable";                        value = @{ name = 'editor' } }
    @{ label = "integer";                          value = 42 }
    @{ label = "bool `$true (msg-2746: locks phantom key 'True')"; value = $true }
    @{ label = "bool `$false";                     value = $false }
    @{ label = "`$null";                            value = $null }
    @{ label = "empty string ''";                  value = '' }
    @{ label = "whitespace-only '   '";            value = '   ' }
    @{ label = "arbitrary [pscustomobject]";       value = [pscustomobject]@{ n = 1 } }
)

function Assert-ThrowsForResource {
    param([scriptblock]$Call, [string]$Label, [string]$Function)
    $threw = $false
    try { & $Call | Out-Null } catch { $threw = $true }
    CheckTrue ("msg-2932 §3 #6b [$Function]: $Label MUST throw") $threw
}

# Row #6b — same table drives both functions. Row #6c is implicitly satisfied because
# Invoke-LeaseAcquire's own call throws before any state-map lookup, whether Test-LeaseAvailableFor
# was consulted or not; the explicit direct-acquire pin lives just below the loop.
foreach ($case in $leaseRejectedSet) {
    Assert-ThrowsForResource -Function 'Test-LeaseAvailableFor' -Label $case.label -Call {
        Test-LeaseAvailableFor -LeasesState $state -CandidateKey 'p/T-x' -Requires $case.value
    }.GetNewClosure()
    Assert-ThrowsForResource -Function 'Invoke-LeaseAcquire' -Label $case.label -Call {
        Invoke-LeaseAcquire -LeasesState @{} -Resource $case.value -CandidateKey 'p/T-x' -Now $now2
    }.GetNewClosure()
}

# Row #6c DIRECT-ACQUIRE PIN — Invoke-LeaseAcquire is called WITHOUT Test-LeaseAvailableFor first.
# Rejection must still hold; the authorisation lives in the mutation (msg-1959), so the guard
# must too. Use a representative rejection from the table (multi-element array — the R3 case).
$threwDirect = $false
try {
    Invoke-LeaseAcquire -LeasesState @{} -Resource @('editor','runner') -CandidateKey 'p/T-x' -Now $now2
}
catch { $threwDirect = $true }
CheckTrue "msg-2932 §3 #6c DIRECT-ACQUIRE PIN: rejection holds without Test-LeaseAvailableFor call" $threwDirect

# Accepted-set counterpart. Row #6b: 'editor' plain string works, AND a [string] arriving inside
# a PSObject shell (splat / pipeline / ConvertFrom-Json) is accepted after BaseObject unwrap.
$check = Test-LeaseAvailableFor -LeasesState $state -CandidateKey 'p/T-x' -Requires 'editor'
Check "row #6b accepted: plain 'editor' string" 'waiting' $check.status
# Simulate the PSObject-wrapped path: ConvertFrom-Json wraps scalars in PSObject, and the
# BaseObject IS the underlying string. The helper's unwrap step accepts this.
$wrapped = '"editor"' | ConvertFrom-Json    # this yields the string 'editor' wrapped in a PSObject shell
$check = Test-LeaseAvailableFor -LeasesState $state -CandidateKey 'p/T-x' -Requires $wrapped
Check "row #6b accepted: [string] arriving inside PSObject shell (splat/pipeline)" 'waiting' $check.status
# The direct-acquire path also accepts a wrapped string.
$acqStateDirect = @{}
$threwWrappedAcq = $false
try { Invoke-LeaseAcquire -LeasesState $acqStateDirect -Resource $wrapped -CandidateKey 'p/T-a' -Now $now2 }
catch { $threwWrappedAcq = $true }
CheckFalse "row #6b accepted: Invoke-LeaseAcquire on PSObject-wrapped [string] does NOT throw" $threwWrappedAcq
Check "row #6b accepted: Invoke-LeaseAcquire on wrapped string created the record" 'p/T-a' $acqStateDirect['editor']['holder']

# Acquire after a release preserves reclaim_required + reclaimed_from + reclaimed_reason —
# these are the PERMANENT audit trail the new holder inherits.
$state = @{
    editor = @{
        holder            = $null
        acquired_at       = $null
        last_progress_at  = $null
        idle_evaluations  = 0
        generation        = 4
        pinned            = $false
        expiring          = $false
        reclaimed_from    = 'p/T-cleared'
        reclaimed_at      = '2026-08-27T00:00:00Z'
        reclaimed_reason  = 'human-clear: PIE crashed'
        reclaim_required  = $true
        revoked_at        = '2026-08-27T01:00:00Z'
        revoked_reason    = 'idle'
        queue             = @()
    }
}
Invoke-LeaseAcquire -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-new' -Now $now2
Check "acquire-after-release: holder is new" 'p/T-new' $state['editor']['holder']
Check "acquire-after-release: generation bumped" 5 $state['editor']['generation']
CheckTrue "acquire-after-release: reclaim_required PRESERVED (new holder inherits duty)" ([bool]$state['editor']['reclaim_required'])
Check "acquire-after-release: reclaimed_from PRESERVED (audit)" 'p/T-cleared' $state['editor']['reclaimed_from']
Check "acquire-after-release: reclaimed_reason PRESERVED (audit)" 'human-clear: PIE crashed' $state['editor']['reclaimed_reason']
Check "acquire-after-release: revoked_at cleared (TRANSIENT Phase-1)" $null $state['editor']['revoked_at']
Check "acquire-after-release: revoked_reason cleared (TRANSIENT Phase-1)" $null $state['editor']['revoked_reason']

# Acquire dequeues self-waiter (a candidate that was queued when the lease was held then races
# in to acquire on the same tick the lease was freed must not stay in its own queue).
$state = @{
    editor = @{
        holder = $null; generation = 1; queue = @( @{ key = 'p/T-a'; waiting_since = $now2.ToUniversalTime().ToString("o") } )
    }
}
Invoke-LeaseAcquire -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-a' -Now $now2
Check "acquire on lease with self in queue: queue emptied" 0 $state['editor']['queue'].Count

# --- 10. Register-LeaseWaiter — creates the record + enqueues ------------------------------------
Write-Host ""
Write-Host "Register-LeaseWaiter — creates the resource record if absent, then enqueues"

$state = @{}
Register-LeaseWaiter -LeasesState $state -Resource 'runner' -WaiterKey 'p/T-x' -Now $now2
Check "Register-LeaseWaiter creates the resource record" 1 $state['runner']['queue'].Count
Check "Register-LeaseWaiter leaves holder empty" $null $state['runner']['holder']
Check "Register-LeaseWaiter generation starts at 0" 0 $state['runner']['generation']

# Idempotent — a second Register on the same key does not duplicate the queue entry.
Register-LeaseWaiter -LeasesState $state -Resource 'runner' -WaiterKey 'p/T-x' -Now $now2.AddMinutes(5)
Check "Register-LeaseWaiter is idempotent" 1 $state['runner']['queue'].Count

# Register on a resource with a live holder just enqueues (does not overwrite).
$state = @{ editor = @{ holder = 'p/T-a'; queue = @() } }
Register-LeaseWaiter -LeasesState $state -Resource 'editor' -WaiterKey 'p/T-b' -Now $now2
Check "Register on live holder: holder preserved" 'p/T-a' $state['editor']['holder']
Check "Register on live holder: waiter enqueued" 1 $state['editor']['queue'].Count

# --- 11. Read-JsonStateWithShape + Get-JsonState shape guard (msg-2172 reader collapse) ---------
Write-Host ""
Write-Host "Read-JsonStateWithShape / Get-JsonState — shape guard (msg-2172)"

# Setup: a scratch dir under [System.IO.Path]::GetTempPath() (portable across pwsh 7 on Windows +
# Linux CI runners; $env:TEMP is Windows-only and returns $null on Linux, which breaks Join-Path).
$tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("Lease-shape-guard-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

try {
    $shapePath = Join-Path $tmpDir 'shape.json'

    # Case 'missing': path does not exist.
    if (Test-Path -LiteralPath $shapePath) { Remove-Item -LiteralPath $shapePath }
    $r = Read-JsonStateWithShape -Path $shapePath
    Check "missing file: shape = 'missing'" 'missing' $r.shape
    Check "missing file: state is empty" 0 $r.state.Count

    # Case 'empty': blank / whitespace file.
    Set-Content -LiteralPath $shapePath -Value '   ' -Encoding utf8
    $r = Read-JsonStateWithShape -Path $shapePath
    Check "blank file: shape = 'empty'" 'empty' $r.shape
    Check "blank file: state is empty" 0 $r.state.Count

    # Case 'empty' via JSON `[]` (ConvertFrom-Json returns $null).
    Set-Content -LiteralPath $shapePath -Value '[]' -Encoding utf8
    $r = Read-JsonStateWithShape -Path $shapePath
    Check "empty JSON array []: shape = 'empty'" 'empty' $r.shape
    Check "empty JSON array []: state is empty" 0 $r.state.Count

    # Case 'object': a real JSON object round-trips normally.
    Set-Content -LiteralPath $shapePath -Value '{"editor":"x","runner":"y"}' -Encoding utf8
    $r = Read-JsonStateWithShape -Path $shapePath
    Check "JSON object: shape = 'object'" 'object' $r.shape
    Check "JSON object: two keys survive" 2 $r.state.Count
    Check "JSON object: editor value round-trips" 'x' $r.state['editor']

    # Case 'array': multi-element array root. THIS IS THE CORRUPTION CASE.
    # BEFORE the guard, this leaked Count/IsFixedSize/IsReadOnly/IsSynchronized/Length/LongLength/
    # Rank/SyncRoot as top-level "resource" keys. The guard rejects it and returns empty.
    Set-Content -LiteralPath $shapePath -Value '[{"editor":"x"},{"foo":"y"}]' -Encoding utf8
    $r = Read-JsonStateWithShape -Path $shapePath
    Check "root array (multi-element): shape = 'array'" 'array' $r.shape
    Check "root array: state is empty (no metadata leaked)" 0 $r.state.Count
    # Pin the metadata-leak keys explicitly — a regression that reintroduces them would show up
    # as ANY of these appearing in the map. Naming them literally documents the leak class.
    foreach ($leakedKey in @('Count','IsFixedSize','IsReadOnly','IsSynchronized','Length','LongLength','Rank','SyncRoot')) {
        CheckFalse "root array: '$leakedKey' NOT in state (metadata leak pinned)" $r.state.ContainsKey($leakedKey)
    }

    # Case 'array': scalar-only array root.
    Set-Content -LiteralPath $shapePath -Value '["editor","runner"]' -Encoding utf8
    $r = Read-JsonStateWithShape -Path $shapePath
    Check "root array (scalars): shape = 'array'" 'array' $r.shape
    Check "root array (scalars): state is empty" 0 $r.state.Count

    # Case 'scalar': root string. BEFORE the guard, this leaked `Length` (System.String.Length).
    # This is what pwsh 7's ConvertFrom-Json returns wrapped in a PSObject shell that answers
    # `-is [PSCustomObject]` as $true — hence the .GetType()-based check in the shape guard.
    Set-Content -LiteralPath $shapePath -Value '"just a string"' -Encoding utf8
    $r = Read-JsonStateWithShape -Path $shapePath
    Check "root string: shape = 'scalar'" 'scalar' $r.shape
    Check "root string: state is empty (no Length leak)" 0 $r.state.Count
    CheckFalse "root string: 'Length' NOT in state (System.String.Length leak pinned)" $r.state.ContainsKey('Length')

    # Case 'scalar': root number and root boolean.
    Set-Content -LiteralPath $shapePath -Value '42' -Encoding utf8
    $r = Read-JsonStateWithShape -Path $shapePath
    Check "root number: shape = 'scalar'" 'scalar' $r.shape
    Check "root number: state is empty" 0 $r.state.Count
    Set-Content -LiteralPath $shapePath -Value 'true' -Encoding utf8
    $r = Read-JsonStateWithShape -Path $shapePath
    Check "root boolean: shape = 'scalar'" 'scalar' $r.shape
    Check "root boolean: state is empty" 0 $r.state.Count

    # Case 'parse-error': broken JSON. Empty state, error message populated.
    Set-Content -LiteralPath $shapePath -Value '{not valid json' -Encoding utf8
    $r = Read-JsonStateWithShape -Path $shapePath
    Check "parse error: shape = 'parse-error'" 'parse-error' $r.shape
    Check "parse error: state is empty" 0 $r.state.Count
    CheckTrue "parse error: error message is populated" ([bool]$r.error)

    # PowerShell 7 unwraps a SINGLE-element array containing an object to just the object. That
    # happens BEFORE our type check, so a lone `[{"editor":"x"}]` legitimately reaches the
    # object branch. Pin that as an intentional behaviour — it is safe (no metadata to leak in
    # this specific shape) even though it does not match a strict "root must be `{...}`" reader.
    # Documenting the split prevents a future reader from "fixing" it into a false positive.
    Set-Content -LiteralPath $shapePath -Value '[{"editor":"y"}]' -Encoding utf8
    $r = Read-JsonStateWithShape -Path $shapePath
    Check "single-element array (pwsh 7 unwrap): shape = 'object'" 'object' $r.shape
    Check "single-element array: state has editor" 'y' $r.state['editor']

    # Get-JsonState wraps Read-JsonStateWithShape and discards the verdict for backward-compat.
    # Its keys must NEVER include metadata (that IS the fix's contract for every existing caller
    # that reads notify.json / quarantine.json / etc.).
    Set-Content -LiteralPath $shapePath -Value '[{"a":1},{"b":2}]' -Encoding utf8
    $s = Get-JsonState -Path $shapePath
    Check "Get-JsonState on root array: empty (canonical fix)" 0 $s.Count
    foreach ($leakedKey in @('Count','Length','LongLength','SyncRoot','Rank','IsFixedSize','IsReadOnly','IsSynchronized')) {
        CheckFalse "Get-JsonState on root array: '$leakedKey' NOT present" $s.ContainsKey($leakedKey)
    }
    Set-Content -LiteralPath $shapePath -Value '"corrupted"' -Encoding utf8
    $s = Get-JsonState -Path $shapePath
    Check "Get-JsonState on root string: empty" 0 $s.Count
    CheckFalse "Get-JsonState on root string: 'Length' NOT present" $s.ContainsKey('Length')

    # END-TO-END: read → Merge-LeasesStateForWrite → verify no metadata reaches the merged map.
    # This is what the 2026-08-28 measurement exercised on live disk; pinning it here catches a
    # future reader-refactor that reintroduces the leak past the merger.
    $leasesLive = Join-Path $tmpDir 'leases.json'
    Set-Content -LiteralPath $leasesLive -Value '[{"editor":"y"},{"foo":"z"}]' -Encoding utf8
    $merged = Merge-LeasesStateForWrite -Memory @{} -OriginalGenerations @{} -DiskPath $leasesLive
    Check "end-to-end: merged has no leaked metadata keys" 0 $merged.Count
}
finally {
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}

# --- 12. Save-CorruptedStateBackup — .bad-<utc> rename side effect (msg-1916 §2 design) ---------
Write-Host ""
Write-Host "Save-CorruptedStateBackup — rename corrupt file aside so the next flush cannot overwrite it"

$tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("Lease-backup-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

try {
    # Happy path: rename to .bad-<UTC-stamp>. The stamp is deterministic in shape (YYYY-MM-DDTHH-mm-ssZ).
    $badPath = Join-Path $tmpDir 'leases.json'
    Set-Content -LiteralPath $badPath -Value '[{"editor":"x"}]' -Encoding utf8
    $renamed = Save-CorruptedStateBackup -Path $badPath
    CheckTrue "backup: returns the renamed path (not null)" ([bool]$renamed)
    CheckTrue "backup: renamed path matches .bad-<utc> template" `
        ([bool]("$renamed" -match '\.bad-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$'))
    CheckFalse "backup: original path is gone" (Test-Path -LiteralPath $badPath)
    CheckTrue "backup: renamed path exists on disk" (Test-Path -LiteralPath $renamed)

    # Contents preserved — the operator's forensic trail is intact.
    $preserved = Get-Content -LiteralPath $renamed -Raw
    Check "backup: original bytes preserved" '[{"editor":"x"}]' ($preserved.Trim())

    # Missing file: no-op, returns $null (race with an external delete).
    $absent = Join-Path $tmpDir 'never-existed.json'
    $result = Save-CorruptedStateBackup -Path $absent
    Check "backup on missing file: returns null (no-op)" $null $result
}
finally {
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}

# --- 13. Grant seam — Get-NextLeaseWaiter / Invoke-LeasePromotion / Invoke-LeaseGrantFromEmpty ---
#
# Covers CHECKLIST rows #1 (end-to-end grant FIFO), #2 (no-steal invariant in the grant path),
# and #3 (expiry → reclaim → next-waiter promotion). These are the msg-1958 §5 pins from PR 3's
# core scope, previously listed as "will be added in PR 3" in the earlier header comment.
Write-Host ""
Write-Host "Grant seam (rows #1-#3) — FIFO promotion, no-steal, expiry → reclaim ordering"

$graceNow = [datetime]::Parse('2026-08-30T00:00:00Z').ToUniversalTime()

# --- ROW #1: end-to-end grant FIFO across candidates -------------------------------------------
# Waiter A enqueues first, waiter B enqueues second (later waiting_since). When the lease frees
# and Invoke-LeaseGrantFromEmpty runs, A must be promoted, not B — even if the sweep enumeration
# order lists B first (msg-1183 D-3: waiting-time FIFO wins over sweep order).
$state = @{ editor = @{ holder = 'p/T-holder'; generation = 1; queue = @() } }
Add-LeaseWaiter -Lease $state['editor'] -WaiterKey 'p/T-A' -Now $graceNow
Add-LeaseWaiter -Lease $state['editor'] -WaiterKey 'p/T-B' -Now $graceNow.AddMinutes(1)
# Sweep enumeration order deliberately puts B first. FIFO must still pick A.
$peeked = Get-NextLeaseWaiter -Lease $state['editor'] -SweepOrder @('p/T-B','p/T-A')
Check "row #1 peek: FIFO picks A over B (waiting_since wins over sweep order)" `
    'p/T-A' $(if ($peeked -is [hashtable]) { $peeked['key'] } else { $peeked.key })

# Now free the lease (holder released, permanent audit remains) and grant from empty.
$state['editor']['holder'] = $null
$state['editor']['reclaimed_from'] = 'p/T-holder'
$state['editor']['reclaimed_at']   = $graceNow.ToUniversalTime().ToString("o")
$state['editor']['reclaimed_reason'] = 'human-clear: PIE crashed'
$promoted = Invoke-LeaseGrantFromEmpty -Lease $state['editor'] -Now $graceNow.AddMinutes(2) `
    -SweepOrder @('p/T-B','p/T-A')
Check "row #1 grant: FIFO promoted A"          'p/T-A' $promoted
Check "row #1 grant: holder is A"              'p/T-A' $state['editor']['holder']
Check "row #1 grant: A dequeued"               1       $state['editor']['queue'].Count
Check "row #1 grant: B still in queue"         'p/T-B' $state['editor']['queue'][0].key
Check "row #1 grant: generation bumped"        2       $state['editor']['generation']
Check "row #1 grant: PERMANENT audit preserved (reclaimed_from)" 'p/T-holder' $state['editor']['reclaimed_from']
Check "row #1 grant: PERMANENT audit preserved (reclaimed_reason)" 'human-clear: PIE crashed' $state['editor']['reclaimed_reason']

# Tie-break: two waiters with the EXACT same waiting_since fall back to sweep order (not queue
# order, not alphabetical). This is what makes 'sweep order' the deterministic tie-break rather
# than "whatever the hashtable enumeration happens to be".
$state = @{ editor = @{ holder = $null; generation = 0; queue = @() } }
Add-LeaseWaiter -Lease $state['editor'] -WaiterKey 'p/T-Y' -Now $graceNow
Add-LeaseWaiter -Lease $state['editor'] -WaiterKey 'p/T-X' -Now $graceNow  # same waiting_since
$peeked = Get-NextLeaseWaiter -Lease $state['editor'] -SweepOrder @('p/T-X','p/T-Y')
Check "row #1 tie: same waiting_since -> sweep order wins (X before Y)" `
    'p/T-X' $(if ($peeked -is [hashtable]) { $peeked['key'] } else { $peeked.key })

# Eligible-set filter: a waiter not in the eligible set is skipped WITHOUT being removed from
# the queue (that removal is Remove-IneligibleLeaseWaiters' job).
$state = @{ editor = @{ holder = $null; generation = 0; queue = @() } }
Add-LeaseWaiter -Lease $state['editor'] -WaiterKey 'p/T-A' -Now $graceNow
Add-LeaseWaiter -Lease $state['editor'] -WaiterKey 'p/T-B' -Now $graceNow.AddMinutes(1)
$peeked = Get-NextLeaseWaiter -Lease $state['editor'] -SweepOrder @('p/T-A','p/T-B') -EligibleKeys @('p/T-B')
Check "row #1 eligibility: A skipped (not eligible), B picked" `
    'p/T-B' $(if ($peeked -is [hashtable]) { $peeked['key'] } else { $peeked.key })
Check "row #1 eligibility: peek did NOT mutate the queue" 2 $state['editor']['queue'].Count

# --- ROW #2: no-steal invariant preserved in the grant path -----------------------------------
# The grant path promotes a waiter into an OCCUPIED lease (during Invoke-LeasePromotion Phase 2),
# but does so through a `reclaimed_from`-annotated write — NOT through Invoke-LeaseAcquire. A
# raw Invoke-LeaseAcquire on the same occupied lease MUST still throw (msg-1958 §5), even after
# the grant path has fired earlier in the same tick. This is what stops a candidate outside the
# queue from "smuggling" itself in via a direct acquire on a busy lease.
$state = @{ editor = @{ holder = 'p/T-C'; generation = 7; queue = @() } }
$threwSteal = $false
try { Invoke-LeaseAcquire -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-outsider' -Now $graceNow }
catch { $threwSteal = $true }
CheckTrue "row #2 no-steal: raw acquire on occupied lease still throws" $threwSteal
Check "row #2 no-steal: holder is untouched by the failed acquire" 'p/T-C' $state['editor']['holder']
Check "row #2 no-steal: generation is untouched by the failed acquire" 7 $state['editor']['generation']

# And after a grant path fires (Phase 2 reclaim), the freshly-promoted lease is still protected
# from a direct steal — the grant did not open a window during which a non-queued outsider can
# race in on the same tick.
$state = @{ editor = @{ holder = 'p/T-holder'; generation = 3; queue = @( @{ key = 'p/T-A'; waiting_since = $graceNow.ToUniversalTime().ToString("o") } ); expiring = $true } }
$verdict = Invoke-LeasePromotion -Lease $state['editor'] -Reason 'idle' -Now $graceNow.AddMinutes(5) -SweepOrder @('p/T-A')
Check "row #2 pre-check: Phase-2 promotion promoted A" 'phase-2-promoted' $verdict
Check "row #2 pre-check: new holder is A" 'p/T-A' $state['editor']['holder']
$threwPostGrant = $false
try { Invoke-LeaseAcquire -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-outsider' -Now $graceNow.AddMinutes(6) }
catch { $threwPostGrant = $true }
CheckTrue "row #2 no-steal: outsider CANNOT acquire the freshly-promoted lease" $threwPostGrant
Check "row #2 no-steal: A is still the holder after the outsider's failed acquire" 'p/T-A' $state['editor']['holder']

# --- ROW #3: expiry -> reclaim -> next-waiter grant ordering ----------------------------------
# The state machine is TWO-PHASE (msg-1183 D-6'd): mark-expiring at tick T, promote at T+1.
# The 1-tick pre-emption window is what lets a legitimate holder come back before the queue
# takes over. Row #3 pins the ordering: Phase 1 does NOT change the holder, Phase 2 DOES, and
# the new holder is the FIFO next waiter.
$state = @{
    editor = @{
        holder            = 'p/T-holder'
        acquired_at       = '2026-08-29T00:00:00Z'
        last_progress_at  = '2026-08-29T00:00:00Z'
        idle_evaluations  = 6
        generation        = 3
        pinned            = $false
        expiring          = $false
        reclaimed_from    = $null
        reclaimed_at      = $null
        reclaimed_reason  = $null
        reclaim_required  = $false
        revoked_at        = $null
        revoked_reason    = $null
        queue             = @( @{ key = 'p/T-next'; waiting_since = $graceNow.ToUniversalTime().ToString("o") } )
    }
}
# Tick T: Phase 1. Mark expiring, record transient intent. Do NOT change holder.
$phase1 = Invoke-LeasePromotion -Lease $state['editor'] -Reason 'idle' -Now $graceNow -SweepOrder @('p/T-next')
Check "row #3 Phase 1: verdict = 'phase-1'" 'phase-1' $phase1
Check "row #3 Phase 1: holder UNCHANGED (1-tick pre-emption window)" 'p/T-holder' $state['editor']['holder']
CheckTrue "row #3 Phase 1: expiring flag set" ([bool]$state['editor']['expiring'])
Check "row #3 Phase 1: revoked_reason set (TRANSIENT)" 'idle' $state['editor']['revoked_reason']
Check "row #3 Phase 1: reclaimed_from still null (nothing reclaimed YET)" $null $state['editor']['reclaimed_from']
Check "row #3 Phase 1: generation UNCHANGED (no ownership change)" 3 $state['editor']['generation']

# Tick T+1: Phase 2. Actual reclamation. New holder is the FIFO next waiter.
$phase2 = Invoke-LeasePromotion -Lease $state['editor'] -Reason 'idle' -Now $graceNow.AddMinutes(5) -SweepOrder @('p/T-next')
Check "row #3 Phase 2: verdict = 'phase-2-promoted'" 'phase-2-promoted' $phase2
Check "row #3 Phase 2: new holder is the FIFO next waiter" 'p/T-next' $state['editor']['holder']
Check "row #3 Phase 2: reclaimed_from PAIRED with prior holder" 'p/T-holder' $state['editor']['reclaimed_from']
Check "row #3 Phase 2: reclaimed_reason PAIRED with reason" 'idle' $state['editor']['reclaimed_reason']
CheckFalse "row #3 Phase 2: expiring cleared" ([bool]$state['editor']['expiring'])
Check "row #3 Phase 2: revoked_at cleared (TRANSIENT consumed)" $null $state['editor']['revoked_at']
CheckTrue "row #3 Phase 2: reclaim_required=true (new holder must restart resource)" ([bool]$state['editor']['reclaim_required'])
Check "row #3 Phase 2: promoted waiter dequeued" 0 $state['editor']['queue'].Count
Check "row #3 Phase 2: generation bumped" 4 $state['editor']['generation']

# Phase 2 with an empty queue = release (holder null, permanent audit written, no successor).
# msg-2946 blocking objection fix: the release MUST set reclaim_required = $true because the
# flag is a property of the RESOURCE (dirty vs. clean), not the successor candidate. The
# previous holder was forcefully evicted, so the physical resource is dirty and remains so
# until SOMEONE restarts it. The next candidate that later acquires this empty record MUST
# inherit `$true` so it knows to restart before use — the end-to-end regression pin at the
# end of this section chains release-then-acquire to prove the signal survives.
$state = @{
    editor = @{
        holder = 'p/T-holder'; generation = 3; expiring = $true
        revoked_at = $graceNow.ToUniversalTime().ToString("o"); revoked_reason = 'idle'
        queue = @()
    }
}
$verdict = Invoke-LeasePromotion -Lease $state['editor'] -Reason 'idle' -Now $graceNow.AddMinutes(5) -SweepOrder @()
Check "row #3 released: verdict = 'phase-2-released'" 'phase-2-released' $verdict
Check "row #3 released: holder cleared (empty stub, caller MUST remove)" $null $state['editor']['holder']
Check "row #3 released: reclaimed_from PAIRED even on release" 'p/T-holder' $state['editor']['reclaimed_from']
Check "row #3 released: reclaimed_reason PAIRED even on release" 'idle' $state['editor']['reclaimed_reason']
CheckTrue "row #3 released: reclaim_required=TRUE (msg-2946: dirty-resource signal survives empty release)" ([bool]$state['editor']['reclaim_required'])
Check "row #3 released: generation bumped" 4 $state['editor']['generation']

# msg-2946 END-TO-END REGRESSION PIN. The blocking objection was: "when a lease is forcefully
# reclaimed with an empty queue, clearing reclaim_required to $false causes the next
# candidate that acquires the lease to inherit the false flag and use a physically dirty
# resource without restarting it". Chain the two operations here so a future refactor that
# reintroduces the clear-on-release semantics reds this pin regardless of whether the two
# functions still test in isolation.
$state = @{
    editor = @{
        holder = 'p/T-crashed'; generation = 7; expiring = $true
        revoked_at = $graceNow.ToUniversalTime().ToString("o"); revoked_reason = 'idle'
        queue = @()
    }
}
$verdict = Invoke-LeasePromotion -Lease $state['editor'] -Reason 'idle' -Now $graceNow.AddMinutes(5)
Check "msg-2946 end-to-end setup: release verdict = 'phase-2-released'" 'phase-2-released' $verdict
CheckTrue "msg-2946 end-to-end setup: reclaim_required survives release" ([bool]$state['editor']['reclaim_required'])
# Now a fresh candidate arrives (later tick) and acquires this empty record.
Invoke-LeaseAcquire -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-successor' -Now $graceNow.AddMinutes(30)
Check "msg-2946 end-to-end: successor becomes holder" 'p/T-successor' $state['editor']['holder']
CheckTrue "msg-2946 end-to-end: successor inherits reclaim_required=TRUE (would have red-ed under the pre-fix behaviour)" `
    ([bool]$state['editor']['reclaim_required'])
Check "msg-2946 end-to-end: successor inherits reclaimed_from audit" 'p/T-crashed' $state['editor']['reclaimed_from']
Check "msg-2946 end-to-end: successor inherits reclaimed_reason audit" 'idle' $state['editor']['reclaimed_reason']

# Phase 2 with an INELIGIBLE queue (waiter present but off-sweep) = release (no promotion).
$state = @{
    editor = @{
        holder = 'p/T-holder'; generation = 3; expiring = $true
        queue = @( @{ key = 'p/T-dead'; waiting_since = $graceNow.ToUniversalTime().ToString("o") } )
    }
}
$verdict = Invoke-LeasePromotion -Lease $state['editor'] -Reason 'idle' -Now $graceNow -SweepOrder @() -EligibleKeys @()
Check "row #3 released (ineligible queue): verdict = 'phase-2-released'" 'phase-2-released' $verdict
Check "row #3 released (ineligible queue): holder cleared" $null $state['editor']['holder']

# Non-expiring lease + Invoke-LeasePromotion = Phase 1 (idempotent — cannot skip to Phase 2 by
# calling twice without a tick boundary between them, because Phase 1 has to observe expiring=$true).
$state = @{ editor = @{ holder = 'p/T-holder'; generation = 3; expiring = $false; queue = @() } }
$phase1a = Invoke-LeasePromotion -Lease $state['editor'] -Reason 'idle' -Now $graceNow
Check "row #3 double-call: first call is Phase 1" 'phase-1' $phase1a
# Second call sees expiring=$true from the first call, so it advances to Phase 2. This is the
# tick-boundary contract: the caller must NOT call promotion twice in a single tick, but the
# state machine trusts the caller's tick discipline and does what it is told when it IS called.
$phase2a = Invoke-LeasePromotion -Lease $state['editor'] -Reason 'idle' -Now $graceNow.AddMinutes(5)
Check "row #3 double-call: second call advances to Phase 2 (release; no waiter)" 'phase-2-released' $phase2a

# --- 14. ROW #7 ORDERING PIN (msg-2644 §3) — un-rollbackable side effects gated by acquire ------
#
# ROW #8 COVERAGE DOCUMENTATION (msg-2645 advisory, msg-2653 Tier-C, msg-2738 §3). This pin
# is a NEGATIVE check bound to SPECIFIC command names. A silent green under a renamed or
# newly-added un-rollbackable command is EXACTLY the failure mode this item exists to catch.
#
#   COMMANDS THIS PIN GUARDS (enumerated exhaustively at PR 3 time):
#     - Invoke-HeadSkipCommitLaunch  (commits the head-skip in conductor state; msg-2644 §2)
#
#   NO OTHER UN-ROLLBACKABLE COMMANDS ARE REACHABLE FROM THE CANDIDATE LOOP AT PR 3 TIME.
#   The candidate loop's wrapper wiring lands in PR 4. Until it does, the only un-rollbackable
#   surface a caller could reach between Test-LeaseAvailableFor and Invoke-LeaseAcquire is
#   Invoke-HeadSkipCommitLaunch itself (per msg-2644 §2's enumeration: "head-skip commit /
#   process launch / write to sweep or loop-control state that a later abort cannot undo").
#   Neither `Update-LoopControlState` nor any process-launch shim (`Start-Editor`, `Invoke-Pie`,
#   the runner spawn) is present in this repository AT THIS COMMIT — they either live in
#   voxelworld / magickit-side scripts or land in PR 4. The pin below is therefore EXHAUSTIVE
#   for its scope; the moment PR 4 adds ANY new un-rollbackable command reachable from the
#   candidate loop, this comment must be updated in the SAME PR to name it and the pin below
#   must gain a matching zero-call spy. Row #9b's single-source rule (checklist canonical)
#   applies: DO NOT fork this enumeration.
#
# HOW THE PIN WORKS. We construct the caller-of-record — the candidate-loop step that reads
# a Test-LeaseAvailableFor verdict, then Invoke-LeaseAcquire, then (only on success) fires the
# un-rollbackable command. The spy replaces the un-rollbackable so we can count invocations.
# We drive the caller into the acquire-failure branch (lease is already held by someone else,
# so acquire throws) and assert the spy is called ZERO times. If a future refactor reverses
# the order (fire, then acquire) — even by accident — this pin will red.
#
# REVERT-AND-RERUN RECEIPT. Bohr msg-2644 §3 requires that this pin be demonstrably load-
# bearing: reversing the sequence in the caller must make the pin ACTUALLY fail. The reverted-
# caller variant is exercised inline below (with a local flag $revertOrder) so a maintainer
# can flip a single boolean and observe the pin flip red — the receipt lives in this test file
# rather than in an out-of-band scratch script.

Write-Host ""
Write-Host "Row #7 ordering pin — Invoke-HeadSkipCommitLaunch is called 0 times on acquire failure"

# The spy: a stand-in for Invoke-HeadSkipCommitLaunch that only records call counts. It NEVER
# runs the real head-skip commit (there is no real head-skip commit in this test scope — PR 4
# territory). Row #8 explicitly documents that this is the SINGLE guarded command at PR 3 time.
$script:orderingSpyCalls = 0
function Invoke-HeadSkipCommitLaunch { $script:orderingSpyCalls++ }

# The candidate-loop step under test. The parameter $revertOrder is the REVERT-AND-RERUN switch:
# when $false, we exercise the CORRECT order (acquire then head-skip); when $true, we exercise
# the REVERSED (broken) order (head-skip then acquire). Row #7 requires that the pin FAILS
# under the reversed order and PASSES under the correct order. Both branches share the same
# call to the spy so the spy count semantics are identical.
function Invoke-CandidateStep {
    param(
        [hashtable]$LeasesState,
        [string]$Resource,
        [string]$CandidateKey,
        [datetime]$Now,
        [bool]$RevertOrder = $false
    )
    $verdict = Test-LeaseAvailableFor -LeasesState $LeasesState -CandidateKey $CandidateKey -Requires $Resource
    if ($verdict.status -ne 'available') {
        # Waiting — the caller MUST NOT acquire, MUST NOT commit head-skip, MUST NOT launch,
        # and MUST call Register-LeaseWaiter (row #5 docstring contract).
        Register-LeaseWaiter -LeasesState $LeasesState -Resource $Resource -WaiterKey $CandidateKey -Now $Now
        return 'waiting'
    }
    if ($RevertOrder) {
        # BROKEN sequence: commit un-rollbackable side effect FIRST, then acquire. If the
        # acquire fails (TOCTOU), the head-skip is already committed — the candidate has
        # consumed its turn without holding the lease. This is precisely the failure the
        # ordering pin exists to detect.
        Invoke-HeadSkipCommitLaunch
        try { Invoke-LeaseAcquire -LeasesState $LeasesState -Resource $Resource -CandidateKey $CandidateKey -Now $Now }
        catch { return 'acquire-failed-after-headskip' }
        return 'launched'
    }
    # CORRECT sequence (msg-2644 §2 ORDERING REQUIREMENT verbatim in Test-LeaseAvailableFor
    # docstring): acquire MUST succeed BEFORE any un-rollbackable side effect.
    try { Invoke-LeaseAcquire -LeasesState $LeasesState -Resource $Resource -CandidateKey $CandidateKey -Now $Now }
    catch {
        # Acquire failure after 'available' verdict — an EXPECTED outcome (TOCTOU). The caller
        # MUST NOT proceed to the un-rollbackable side effect. Fall through to register-and-return.
        Register-LeaseWaiter -LeasesState $LeasesState -Resource $Resource -WaiterKey $CandidateKey -Now $Now
        return 'acquire-failed'
    }
    Invoke-HeadSkipCommitLaunch
    return 'launched'
}

# CORRECT ORDER, ACQUIRE SUCCEEDS. The head-skip commit fires exactly once — this is the
# healthy path, and the pin does NOT prevent it. This branch exists to prove the pin does
# not over-guard (a "spy always 0" pin is a tautology; we need it to be 1 in the happy path).
$script:orderingSpyCalls = 0
$state = @{}
$result = Invoke-CandidateStep -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-a' -Now $graceNow -RevertOrder:$false
Check "row #7 sanity: healthy path launches" 'launched' $result
Check "row #7 sanity: head-skip called exactly once on the healthy path" 1 $script:orderingSpyCalls

# CORRECT ORDER, ACQUIRE FAILS (TOCTOU). This is THE row-#7 pin.
# Set up: a foreign holder already owns the lease. Test-LeaseAvailableFor will return 'waiting'
# for our candidate — so the caller MUST NOT even attempt acquire, and definitely MUST NOT
# commit head-skip. Spy must be 0.
$script:orderingSpyCalls = 0
$state = @{ editor = @{ holder = 'p/T-other'; generation = 4; queue = @() } }
$result = Invoke-CandidateStep -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-a' -Now $graceNow -RevertOrder:$false
Check "row #7: verdict on foreign lease is 'waiting'" 'waiting' $result
Check "row #7 PIN: head-skip called 0 times when verdict is 'waiting'" 0 $script:orderingSpyCalls
CheckTrue "row #7 PIN: caller queued via Register-LeaseWaiter" ($state['editor']['queue'].Count -eq 1)

# CORRECT ORDER, TOCTOU RACE. Test-LeaseAvailableFor returned 'available', then a competing
# candidate acquired between the verdict and OUR acquire, so OUR acquire throws. Head-skip
# must be 0 (msg-2644 §3, the load-bearing case).
$script:orderingSpyCalls = 0
$state = @{ editor = @{ holder = 'p/T-competitor'; generation = 1; queue = @() } }
# Force 'available' by clearing the holder immediately before Test-LeaseAvailableFor sees it,
# then restore before Invoke-LeaseAcquire — this simulates the TOCTOU window mechanically.
$check = Test-LeaseAvailableFor -LeasesState @{} -CandidateKey 'p/T-a' -Requires 'editor'
Check "row #7 TOCTOU setup: bare-state verdict is 'available'" 'available' $check.status
# Now exercise the caller under the real state where our acquire will fail on the no-steal
# throw (competitor is still the holder). This is the exact failure the spec calls "acquire
# failure after an 'available' verdict — an EXPECTED outcome, not an error".
$script:orderingSpyCalls = 0
function Invoke-CandidateStepToctou {
    # Same as Invoke-CandidateStep, but uses an already-obtained 'available' verdict from
    # the empty state (pre-race), so the caller reaches the acquire branch and gets the
    # no-steal throw against the post-race state.
    param([hashtable]$LeasesState, [datetime]$Now, [bool]$RevertOrder = $false)
    if ($RevertOrder) {
        Invoke-HeadSkipCommitLaunch
        try { Invoke-LeaseAcquire -LeasesState $LeasesState -Resource 'editor' -CandidateKey 'p/T-a' -Now $Now }
        catch { return 'acquire-failed-after-headskip' }
        return 'launched'
    }
    try { Invoke-LeaseAcquire -LeasesState $LeasesState -Resource 'editor' -CandidateKey 'p/T-a' -Now $Now }
    catch {
        Register-LeaseWaiter -LeasesState $LeasesState -Resource 'editor' -WaiterKey 'p/T-a' -Now $Now
        return 'acquire-failed'
    }
    Invoke-HeadSkipCommitLaunch
    return 'launched'
}
$result = Invoke-CandidateStepToctou -LeasesState $state -Now $graceNow -RevertOrder:$false
Check "row #7 TOCTOU: verdict is 'acquire-failed' (no-steal throw)" 'acquire-failed' $result
Check "row #7 TOCTOU PIN: head-skip called 0 times when acquire throws" 0 $script:orderingSpyCalls
CheckTrue "row #7 TOCTOU: caller queued via Register-LeaseWaiter after failure" ($state['editor']['queue'].Count -eq 1)
Check "row #7 TOCTOU: competitor is still the holder (no accidental steal)" 'p/T-competitor' $state['editor']['holder']

# REVERT-AND-RERUN receipt (msg-2644 §3). Prove the pin is load-bearing: with the order reversed
# in the caller, the spy IS called before the acquire fails — the pin's zero-call assertion
# would NOW be wrong. We check that inline so the pin's proof lives next to the pin.
$script:orderingSpyCalls = 0
$state = @{ editor = @{ holder = 'p/T-competitor'; generation = 1; queue = @() } }
$result = Invoke-CandidateStepToctou -LeasesState $state -Now $graceNow -RevertOrder:$true
Check "row #7 REVERT: with reversed order, caller reports acquire-failed-after-headskip" `
    'acquire-failed-after-headskip' $result
Check "row #7 REVERT (load-bearing receipt): under reversed order the spy IS called (would have red-ed the pin)" `
    1 $script:orderingSpyCalls

# --- 15. Remove-IneligibleLeaseWaiters ---------------------------------------------------------
Write-Host ""
Write-Host "Remove-IneligibleLeaseWaiters — off-sweep / quarantined waiters are scrubbed"

$state = @{ editor = @{ holder = 'p/T-h'; queue = @() } }
Add-LeaseWaiter -Lease $state['editor'] -WaiterKey 'p/T-live'    -Now $graceNow
Add-LeaseWaiter -Lease $state['editor'] -WaiterKey 'p/T-dead'    -Now $graceNow.AddSeconds(1)
Add-LeaseWaiter -Lease $state['editor'] -WaiterKey 'p/T-jailed'  -Now $graceNow.AddSeconds(2)
$removed = Remove-IneligibleLeaseWaiters -Lease $state['editor'] `
    -SweepKeys @('p/T-live','p/T-jailed') `
    -QuarantinedKeys @('p/T-jailed')
Check "Remove-IneligibleLeaseWaiters: 2 removed (dead + jailed)" 2 $removed
Check "Remove-IneligibleLeaseWaiters: only p/T-live remains" 1 $state['editor']['queue'].Count
Check "Remove-IneligibleLeaseWaiters: preserves order of survivors" 'p/T-live' $state['editor']['queue'][0].key

# No-op on null lease / missing queue.
$removed = Remove-IneligibleLeaseWaiters -Lease $null -SweepKeys @('p/T-live')
Check "Remove-IneligibleLeaseWaiters on null lease: 0 removed" 0 $removed

# --- 16. Row #4 TOCTOU pin — 'available' can be followed by a failing acquire (docstring load-bearing) ---
Write-Host ""
Write-Host "Row #4 — 'available' verdict is ADVISORY: acquire MAY fail (TOCTOU); caller MUST NOT launch"

# The load-bearing docstring clause on Test-LeaseAvailableFor is "acquire failure after
# 'available' is an EXPECTED outcome, not an error". Row #4 pins that clause: we set up a
# state where verdict is 'available', then simulate a competing acquire, then observe that
# OUR subsequent acquire is refused by the no-steal throw.

$state = @{}
$check = Test-LeaseAvailableFor -LeasesState $state -CandidateKey 'p/T-a' -Requires 'editor'
Check "row #4 setup: verdict on empty state is 'available'" 'available' $check.status
# Meanwhile a different candidate acquired.
Invoke-LeaseAcquire -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-b' -Now $graceNow
# Our acquire (having read 'available' pre-race) now throws.
$threwToctou = $false
try { Invoke-LeaseAcquire -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-a' -Now $graceNow.AddSeconds(1) }
catch { $threwToctou = $true }
CheckTrue "row #4 PIN: acquire after 'available' verdict CAN fail (no-steal throw)" $threwToctou
Check "row #4 PIN: original holder (p/T-b) is UNCHANGED by our failed acquire" 'p/T-b' $state['editor']['holder']

# --- 17. Row #5 documentation pin — .DESCRIPTION docstring MUST spell out the three required things --
Write-Host ""
Write-Host "Row #5 — Test-LeaseAvailableFor docstring MUST document asymmetric contract + collapse rationale + ORDERING REQUIREMENT"

$help = Get-Help -Name Test-LeaseAvailableFor -Full | Out-String
CheckTrue "row #5a: docstring mentions 'ADVISORY' (asymmetric contract)" `
    ([bool]($help -match 'ADVISORY'))
CheckTrue "row #5a: docstring mentions 'BINDING' (asymmetric contract)" `
    ([bool]($help -match 'BINDING'))
CheckTrue "row #5b: docstring mentions the 'available' COLLAPSE (free OR held-by-self)" `
    ([bool]($help -match "free OR already held by this candidate|free.*OR.*HELD BY THIS candidate|folds two distinct situations"))
CheckTrue "row #5b: docstring mentions the collapse RATIONALE (idempotent acquire)" `
    ([bool]($help -match 'idempotent'))
CheckTrue "row #5c: docstring mentions ORDERING REQUIREMENT verbatim (msg-2644 §2)" `
    ([bool]($help -match 'ORDERING REQUIREMENT'))
CheckTrue "row #5c: docstring names Invoke-HeadSkipCommitLaunch in the un-rollbackable list" `
    ([bool]($help -match 'Invoke-HeadSkipCommitLaunch'))
CheckTrue "row #5c: docstring names Register-LeaseWaiter as the failure-path action" `
    ([bool]($help -match 'Register-LeaseWaiter'))
CheckTrue "row #5c: docstring calls out TOCTOU as EXPECTED (not error)" `
    ([bool]($help -match 'TOCTOU'))
# Cross-reference: entry validation lives on row #6a's shared helper — docstring MUST direct
# readers to Assert-LeaseResourceName rather than restating the accepted-input shape inline
# (msg-2932 §4).
CheckTrue "row #5 cross-ref: docstring points to Assert-LeaseResourceName for entry validation" `
    ([bool]($help -match 'Assert-LeaseResourceName'))

# Verdict domain is UNCHANGED (msg-2738 §5 correction 1): still exactly two values, still with
# the same 'free ∪ held-by-self -> available' collapse. Row #5 is documentation-only.
$s = @{}
$check = Test-LeaseAvailableFor -LeasesState $s -CandidateKey 'p/T-a' -Requires 'editor'
Check "row #5 verdict domain unchanged: free -> 'available'" 'available' $check.status
Invoke-LeaseAcquire -LeasesState $s -Resource 'editor' -CandidateKey 'p/T-a' -Now $graceNow
$check = Test-LeaseAvailableFor -LeasesState $s -CandidateKey 'p/T-a' -Requires 'editor'
Check "row #5 verdict domain unchanged: held-by-self -> 'available' (SAME verdict as free)" 'available' $check.status

Write-Host ""
if ($script:failures -gt 0) { Write-Host "lease gate: $($script:failures) check(s) FAILED"; exit 1 }
Write-Host "lease gate: all checks passed"
exit 0
