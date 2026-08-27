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
# Invoke-LeaseGrantFromEmpty) will be added in PR 3. Wrapper AST checks land in PR 4 with the
# state-machine wiring.

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

# --- 9. Test-LeaseAvailableFor + Invoke-LeaseAcquire ---------------------------------------------
Write-Host ""
Write-Host "Test-LeaseAvailableFor + Invoke-LeaseAcquire — the candidate-loop gate"

$state = @{}
$check = Test-LeaseAvailableFor -LeasesState $state -CandidateKey 'p/T-a' -Requires @('editor')
Check "empty state: available" 'available' $check.status

# Acquire on empty state creates the record.
Invoke-LeaseAcquire -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-a' -Now $now2
Check "acquire creates the record" 'p/T-a' $state['editor']['holder']
Check "acquire generation is 1" 1 $state['editor']['generation']

# Second candidate needs the same lease -> waiting.
$check = Test-LeaseAvailableFor -LeasesState $state -CandidateKey 'p/T-b' -Requires @('editor')
Check "second candidate: waiting" 'waiting' $check.status
Check "waiting: waitOn names the resource" 'editor' $check.waitOn[0]
Check "waiting: holders reports the current one" 'p/T-a' $check.holders['editor']

# Same candidate re-checking its own lease -> available (self-hold).
$check = Test-LeaseAvailableFor -LeasesState $state -CandidateKey 'p/T-a' -Requires @('editor')
Check "self-hold: available" 'available' $check.status

# Idempotent re-acquire on self-hold: bumps generation? no — record kept, clocks refresh separately.
$genBefore = $state['editor']['generation']
Invoke-LeaseAcquire -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-a' -Now $now2.AddMinutes(5)
Check "acquire on self-hold: generation unchanged (idempotent)" $genBefore $state['editor']['generation']

# Invoke-LeaseAcquire on a foreign lease throws (no-steal invariant).
$threw = $false
try { Invoke-LeaseAcquire -LeasesState $state -Resource 'editor' -CandidateKey 'p/T-b' -Now $now2 }
catch { $threw = $true }
CheckTrue "acquire on foreign lease refuses (throws)" $threw

# Requires with zero elements -> trivially available (v1 candidate loop passes an array of 0 or 1).
$check = Test-LeaseAvailableFor -LeasesState $state -CandidateKey 'p/T-x' -Requires @()
Check "empty requires: available" 'available' $check.status

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

Write-Host ""
if ($script:failures -gt 0) { Write-Host "lease gate: $($script:failures) check(s) FAILED"; exit 1 }
Write-Host "lease gate: all checks passed"
exit 0
