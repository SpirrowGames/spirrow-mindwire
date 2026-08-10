#!/usr/bin/env pwsh
# deploy/Clear-Quarantine.ps1 — the ONE legitimate way to release a quarantined thread.
#
# Q3 of the sweep-failure-isolation design (spec/msg-814) is load-bearing: there is NO automatic
# clear path. `(head, control)` changes are shown as a HINT in the daily digest, never used to
# schedule. If the runner cleared automatically, "input changed" and "failure is resolved" would
# collapse into a question the runner cannot distinguish, and the whole quarantine story would slip
# back into the same silent-degradation failure mode this design exists to end.
#
# So a clear is a human act. This script is that act, and it does three things:
#   1. removes the entry from state/quarantine.json (so the next sweep tries the thread again),
#   2. appends the removed entry — plus the operator's `-Reason` and the timestamp — to
#      state/quarantine-history.json (append-only; never rewritten, never truncated), because
#      "why did you clear it?" is the primary data on whether the quarantine judgement was
#      over-sensitive, and
#   3. drops the head-skip cache entry for that thread — otherwise the next tick would look at an
#      unchanged head and skip the launch we JUST said should happen (the whole point of clearing
#      is to give the thread another chance to run).
#
# Not idempotent by design: clearing a thread that is not quarantined is an error, so the operator
# does not silently no-op when they mistype the key.
#
# Not `Remove-`, not `Reset-`: `Clear-` is the PowerShell verb that means "empty the container but
# leave it in place" (Approved Verbs table). The container is quarantine.json's map — the thread's
# entry is emptied, the map itself stays.

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    # State key: "project/thread_id". This is the composite key the sweep uses everywhere. Not just
    # thread_id because thread ids are only unique WITHIN a project (see sweep.json.example).
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[^/]+/[^/]+$')]
    [string]$Thread,

    # Free-text explanation. REQUIRED — the whole reason the history file exists is so this text
    # accumulates as first-hand data on which quarantines fired legitimately vs. from over-sensitive
    # judgement. Leaving it empty would defeat the purpose.
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Reason,

    # The data root; the sweep respects the same env var, and defaulting to the same location keeps
    # this script usable without any env setup on the operator's side.
    [string]$DataDir = $(if ($env:MINDWIRE_PATHS__DATA_DIR) { $env:MINDWIRE_PATHS__DATA_DIR } else { Join-Path $HOME "spirrow-mindwire-data" })
)

$ErrorActionPreference = "Stop"

$quarantineStatePath = Join-Path $DataDir "state\quarantine.json"
$quarantineHistoryPath = Join-Path $DataDir "state\quarantine-history.json"
$headsStatePath = Join-Path $DataDir "state\heads.json"

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

# Also drop the head-skip cache entry. Otherwise the next tick would see the current head match the
# quarantined-time head and skip the launch — which defeats the point of clearing (the clear IS the
# request to try again). The head-skip cache will regenerate on the next successful launch.
if (Test-Path -LiteralPath $headsStatePath) {
    $hraw = Get-Content -LiteralPath $headsStatePath -Raw -Encoding utf8
    if ($hraw.Trim()) {
        $hobj = $hraw | ConvertFrom-Json
        $hstate = @{}
        foreach ($p in $hobj.PSObject.Properties) { $hstate[$p.Name] = $p.Value }
        if ($hstate.ContainsKey($Thread)) {
            $hstate.Remove($Thread)
            [System.IO.File]::WriteAllText($headsStatePath, ($hstate | ConvertTo-Json -Depth 5), $utf8NoBom)
            Write-Host "head-skip cache entry for $Thread dropped — next tick will launch."
        }
    }
}

Write-Host "cleared: $Thread"
Write-Host "reason:  $Reason"
Write-Host "history: $quarantineHistoryPath ($($history.Count) entries)"
