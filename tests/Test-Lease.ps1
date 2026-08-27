# Regression guard for the exclusive-resource lease + queue (T-exclusive-resource-lease-queue,
# msg-1180 / 1183 / 1185 / 1187 design; msg-1188 Tier-C approval).
#
# What THIS PR covers (T-exclusive-resource-lease-queue PR 1 / 4 — state & persistence):
#   1. Merge-LeasesStateForWrite — the operator-write-mid-sweep survival semantics (per-resource
#      `generation` as an optimistic-concurrency token). This is the msg-1802 blocker fix, rebased
#      onto the split PR chain (Takahito msg-2098 §3): PR 1 lands the record shape, the merge
#      rule, and Get-JsonState / Get-LeaseSummaryLines. Nothing here is wired into the sweep yet;
#      wiring lands in PR 4.
#
# Sections for probe classification (Get-LeaseHolderClassification / Update-LeaseFromClassification
# / Test-LeaseExpiring / Test-LeaseAvailableFor / Invoke-LeaseAcquire) will be added in PR 2. Queue
# sections (Add/Remove waiter, Get-NextLeaseWaiter, Invoke-LeasePromotion, scrub, empty-drain,
# Register-LeaseWaiter) will be added in PR 3. Wrapper AST checks land in PR 4 with the wiring.

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
# that scenario is added to this section when PR 2 lands. A/B/D/E/F cover the merger's own logic
# in isolation and are complete without any other function. Scenario F is the msg-2103 regression
# guard for external deletion mid-sweep — the original condition silently resurrected deleted
# leases from stale memory and this test would have caught it on the first run.
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

Write-Host ""
if ($script:failures -gt 0) { Write-Host "lease gate: $($script:failures) check(s) FAILED"; exit 1 }
Write-Host "lease gate: all checks passed"
exit 0
