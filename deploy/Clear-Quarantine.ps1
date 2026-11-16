#!/usr/bin/env pwsh
# deploy/Clear-Quarantine.ps1 — the ONE legitimate way to release a quarantined thread.
#
# Q3 of the sweep-failure-isolation design (spec/msg-814) is load-bearing: there is NO automatic
# clear path. `(head, control)` changes are shown as a HINT in the daily digest, never used to
# schedule. If the runner cleared automatically, "input changed" and "failure is resolved" would
# collapse into a question the runner cannot distinguish, and the whole quarantine story would slip
# back into the same silent-degradation failure mode this design exists to end.
#
# So a clear is a human act. This script is that act, and it does two things:
#   1. removes the entry from state/quarantine.json (so the next sweep tries the thread again),
#   2. appends the removed entry — plus the operator's `-Reason` and the timestamp — to
#      state/quarantine-history.json (append-only; never rewritten, never truncated), because
#      "why did you clear it?" is the primary data on whether the quarantine judgement was
#      over-sensitive.
#
# Old third step (drop the state/heads.json entry so the head cache doesn't re-skip) is gone with
# heads.json itself (T-sweep-intake-and-quarantine-stalls Bohr msg-1432 §W-2). Under the new
# head-skip nomination predicate the "same head, same routing → SKIP" equality is not a state,
# so a cleared thread does not re-skip on unchanged head. The new predicate can still DEFER on
# backoff — up to CAP=60 min — for a thread whose nomination has not moved since its last (now-
# quarantined) LAUNCH; that DEFER is under the starvation floor (24h), so the operator's cleared
# thread cannot silently starve behind it.
#
# Not idempotent by design: clearing a thread that is not quarantined is an error, so the operator
# does not silently no-op when they mistype the key.
#
# Not `Remove-`, not `Reset-`: `Clear-` is the PowerShell verb that means "empty the container but
# leave it in place" (Approved Verbs table). The container is quarantine.json's map — the thread's
# entry is emptied, the map itself stays.
#
# -SlotParkKey (T-ephemeral-impl-clone-slot-per-thread step 2d, Bohr msg-9319 / msg-9321): the same
# human act for a per-thread clone-slot park. Such a park lives in state/dirty-clones.json under a
# slot key (`slot|<repo>|<thread_id>`, or any key the sweep cannot classify) and, unlike a
# repo-keyed dirty-clone park, is never released by the tick's clone-check: only this releases it.
# It removes exactly that ONE key — never a repo-keyed park (those are released by clone-check) and
# never another slot of the same repo — and appends the removed row to the same history file
# (`kind: slot_park`). No new command (msg-9321 §2). Key classification is the sweep's own
# Get-DirtyCloneParkKeyKind, lifted from its AST, so the two can never disagree.

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium', DefaultParameterSetName = 'Thread')]
param(
    # State key: "project/thread_id". This is the composite key the sweep uses everywhere. Not just
    # thread_id because thread ids are only unique WITHIN a project (see sweep.json.example).
    [Parameter(Mandatory = $true, ParameterSetName = 'Thread')]
    [ValidatePattern('^[^/]+/[^/]+$')]
    [string]$Thread,

    # A slot park's key in state/dirty-clones.json, exactly as the digest / log line shows it.
    [Parameter(Mandatory = $true, ParameterSetName = 'SlotPark')]
    [ValidateNotNullOrEmpty()]
    [string]$SlotParkKey,

    # Free-text explanation. REQUIRED — the whole reason the history file exists is so this text
    # accumulates as first-hand data on which quarantines fired legitimately vs. from over-sensitive
    # judgement. Leaving it empty would defeat the purpose.
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Reason,

    # The data root; the sweep respects the same env var, and defaulting to the same location keeps
    # this script usable without any env setup on the operator's side.
    [string]$DataDir = $(if ($env:MINDWIRE_PATHS__DATA_DIR) { $env:MINDWIRE_PATHS__DATA_DIR } else { Join-Path $HOME "spirrow-mindwire-data" }),

    # -SlotParkKey only: how long to wait for state/dirty-clones.json.lock before failing with no
    # change (Bohr msg-9328 §3). The sweep holds that lock only to read, change one key and save.
    [Parameter(ParameterSetName = 'SlotPark')]
    [ValidateRange(1, 600)]
    [int]$LockTimeoutSeconds = 10
)

$ErrorActionPreference = "Stop"

if ($PSCmdlet.ParameterSetName -eq 'SlotPark') {
    $dirtyClonesStatePath = Join-Path $DataDir "state\dirty-clones.json"
    $slotHistoryPath = Join-Path $DataDir "state\quarantine-history.json"
    $sweepScript = Join-Path $PSScriptRoot "run-conductor-scheduled.ps1"
    $parseErrors = $null
    $sweepAst = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
    if ($parseErrors) { throw "cannot classify the key: $sweepScript does not parse" }
    $prefixAssign = $sweepAst.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $n.Left.Extent.Text -eq '$SlotParkKeyPrefix' }, $true) | Select-Object -First 1
    if (-not $prefixAssign) { throw "cannot classify the key: `$SlotParkKeyPrefix not found in $sweepScript" }
    Invoke-Expression $prefixAssign.Extent.Text
    foreach ($fname in 'ConvertTo-DirtyCloneRepoKey', 'Get-DirtyCloneParkKeyKind', 'Read-DirtyClonesStateForUpdate',
                       'Write-DirtyClonesStateAtomic') {
        $fn = $sweepAst.FindAll({ param($n)
                $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $fname }, $true) |
            Select-Object -First 1
        if (-not $fn) { throw "cannot classify the key: $fname not found in $sweepScript" }
        Invoke-Expression $fn.Extent.Text
    }
    if (-not (Test-Path -LiteralPath $dirtyClonesStatePath)) {
        throw "no dirty-clone state file at $dirtyClonesStatePath — nothing to unpark"
    }
    $kind = Get-DirtyCloneParkKeyKind -Key $SlotParkKey
    if ($kind -eq 'repo') {
        throw "$SlotParkKey is a repo-keyed dirty-clone park: the tick's clone-check releases it once the clone is clean; -SlotParkKey only releases slot parks"
    }
    # Confirm before taking the lock, so a -Confirm prompt never holds it.
    if (-not $PSCmdlet.ShouldProcess($SlotParkKey, "unpark clone slot (reason: $Reason)")) { return }
    # The read-again -> remove ONE key -> save happens inside the same short lock the sweep uses
    # (Bohr msg-9328 §3, Obj-25-1), so a park the sweep saves meanwhile is never lost and this unpark
    # is never written back by it. No lock within -LockTimeoutSeconds, or a file that cannot be read:
    # fail with no change to the state or the history.
    $lockPath = "$dirtyClonesStatePath.lock"
    $deadline = [DateTime]::UtcNow.AddSeconds($LockTimeoutSeconds)
    $lockStream = $null
    $lockError = ''
    while ($null -eq $lockStream) {
        try {
            $lockStream = [System.IO.FileStream]::new($lockPath, [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        } catch {
            $lockError = $_.Exception.Message
            if ([DateTime]::UtcNow -ge $deadline) {
                throw "could not take $lockPath within $LockTimeoutSeconds s (the sweep may be saving; run this again): $lockError — nothing changed"
            }
            Start-Sleep -Milliseconds 200
        }
    }
    try {
        $read = Read-DirtyClonesStateForUpdate -StatePath $dirtyClonesStatePath
        if (-not $read.ok) { throw "$($read.error) — nothing changed" }
        $dstate = $read.state
        if (-not $dstate.ContainsKey($SlotParkKey)) {
            throw "not parked: $SlotParkKey (state keys: $($dstate.Keys -join ', '))"
        }
        $sremoved = $dstate[$SlotParkKey]
        [void]$dstate.Remove($SlotParkKey)
        Write-DirtyClonesStateAtomic -StatePath $dirtyClonesStatePath -State $dstate
    } finally {
        $lockStream.Dispose()
    }
    # History only after the state was saved.
    $utf8NoBomSlot = New-Object System.Text.UTF8Encoding($false)
    $shistory = @()
    if (Test-Path -LiteralPath $slotHistoryPath) {
        $shraw = Get-Content -LiteralPath $slotHistoryPath -Raw -Encoding utf8
        if ($shraw.Trim()) { $shistory = @($shraw | ConvertFrom-Json) }
    }
    $shistory += [ordered]@{
        kind       = 'slot_park'
        park_key   = $SlotParkKey
        key_kind   = $kind
        cleared_at = [DateTime]::UtcNow.ToString("o")
        cleared_by = if ($env:USERNAME) { $env:USERNAME } else { "unknown" }
        reason     = $Reason
        record     = $sremoved
    }
    [System.IO.File]::WriteAllText($slotHistoryPath, (ConvertTo-Json -InputObject $shistory -Depth 6 -AsArray), $utf8NoBomSlot)
    Write-Host "unparked: $SlotParkKey ($kind)"
    Write-Host "reason:   $Reason"
    Write-Host "history:  $slotHistoryPath ($($shistory.Count) entries)"
    return
}

$quarantineStatePath = Join-Path $DataDir "state\quarantine.json"
$quarantineHistoryPath = Join-Path $DataDir "state\quarantine-history.json"

if (-not (Test-Path -LiteralPath $quarantineStatePath)) {
    throw "no quarantine state file at $quarantineStatePath — nothing to clear (the sweep has not written one yet, or the data dir is wrong)"
}

# Read the state file directly rather than dot-sourcing the sweep. Dot-sourcing would run the sweep.
$raw = Get-Content -LiteralPath $quarantineStatePath -Raw -Encoding utf8
$obj = if ($raw.Trim()) { $raw | ConvertFrom-Json } else { $null }
$state = @{}
if ($null -ne $obj) {
    foreach ($p in $obj.PSObject.Properties) { $state[$p.Name] = $p.Value }
}

if (-not $state.ContainsKey($Thread)) {
    throw "thread not quarantined: $Thread (state keys: $($state.Keys -join ', '))"
}

$removed = $state[$Thread]
if (-not $PSCmdlet.ShouldProcess($Thread, "clear quarantine (reason: $Reason)")) { return }

$state.Remove($Thread)

# Save state — same UTF-8-no-BOM, ConvertTo-Json with the same depth as the sweep, so the file
# shape is byte-compatible with what the wrapper wrote.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($quarantineStatePath, ($state | ConvertTo-Json -Depth 5), $utf8NoBom)

# Append to history. Read-modify-write is fine here — the file only grows on human action, so a
# race with anything else is not a concern.
$history = @()
if (Test-Path -LiteralPath $quarantineHistoryPath) {
    $hraw = Get-Content -LiteralPath $quarantineHistoryPath -Raw -Encoding utf8
    if ($hraw.Trim()) {
        $hobj = $hraw | ConvertFrom-Json
        if ($hobj -is [System.Array]) { $history = @($hobj) } else { $history = @($hobj) }
    }
}
$entry = [ordered]@{
    thread       = $Thread
    cleared_at   = [DateTime]::UtcNow.ToString("o")
    cleared_by   = if ($env:USERNAME) { $env:USERNAME } else { "unknown" }
    reason       = $Reason
    record       = $removed
}
$history += $entry
# -AsArray forces the root to be a JSON array even when $history has exactly one element. Without
# it, ConvertTo-Json emits a single JSON object (PowerShell pipeline unrolls a one-element array),
# so the root schema flip-flops between Object and Array depending on the entry count. This script
# tolerates both shapes on read, but a file whose shape depends on its length is fragile for any
# outside reader; making it always an array closes that surprise once.
[System.IO.File]::WriteAllText($quarantineHistoryPath, (ConvertTo-Json -InputObject $history -Depth 6 -AsArray), $utf8NoBom)

# Nothing to do for the head-skip state file. state/head_skip.json is owned by the CLI (single
# writer, atomic replace) and its schema records a per-thread Record rather than a head/control
# pair. A cleared thread with a prior record and unchanged nomination will DEFER on backoff for
# up to CAP=60 min under the new predicate — still far under the 24h starvation threshold, so it
# cannot silently starve behind the clear (msg-1432 §W-2). Editing head_skip.json from here would
# be a second writer for a single-writer file; the frozen spec is explicit that we do not.

Write-Host "cleared: $Thread"
Write-Host "reason:  $Reason"
Write-Host "history: $quarantineHistoryPath ($($history.Count) entries)"
