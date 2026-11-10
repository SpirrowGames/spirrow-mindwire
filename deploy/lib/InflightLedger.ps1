# In-flight launch ledger and the hold acknowledgement made while a clone is parked
# (T-role-exit-leaves-clone-off-default-branch PR-B, Bohr msg-8838 / msg-8844 / msg-8846 / msg-8848).
#
# WHY THIS EXISTS. `observed_state` is written only by a running conductor. A HOLD set while the
# project's clone is parked therefore never got acknowledged: the hold check lets the unacknowledged
# candidate fall through, the parked check then skips it, and nothing launches to write the
# acknowledgement. The sweep now writes `observed=hold` itself, through
# `scripts/loop_control.py --report-observed-hold`, but only when it can show that no conductor is
# running the project.
#
# WHAT "NO CONDUCTOR IS RUNNING" RESTS ON. Ticks do not overlap (MultipleInstancesPolicy=IgnoreNew)
# and every launch is waited for, so within a tick only a run whose exit was NOT confirmed can still
# be alive: exit 7 (kill-unconfirmed), or a sweep that died mid-launch. The in-flight ledger
# `<data_dir>/state/conductor-inflight.json` records each launch BEFORE it starts and drops the row
# only when the run's exit is confirmed, so such a run always leaves a row behind:
#
#   { "<launch_id GUID>": { "project": P, "key": "P/T", "started_at": iso },
#     "*":                { "project": "*", "reason": "absent" | "corrupt", "at": iso } }
#
#   * Before launch the row is added. If that write fails nothing is launched (the caller breaks).
#   * After launch, on any exit code but 7, the launch's OWN row is removed. On exit 7, or when the
#     removal fails, the row stays.
#   * Every write is a temp file in the same directory, then an atomic [IO.File]::Replace (or a
#     non-overwriting Move when the file does not exist yet). The file is never deleted or renamed.
#   * A file that does not parse is COPIED to `.corrupt-<ts>` (the original stays in place) and then
#     replaced by a file holding the `*` sentinel (reason=corrupt). A missing file is created holding
#     the `*` sentinel (reason=absent): a missing file means first run after deploy or a person
#     deleted it, and either way an orphan from before may still be alive.
#   * A row left by exit 7, a sweep crash, or a failed delete is never cleared automatically, and
#     neither is a `*` sentinel (corrupt / absent). The operator clears them by following
#     docs/deploy.md. After a normal exit, the sweep deletes its own row as usual.
#
# EXECUTOR POOL. This reasoning holds for the single conductor host of ADR-2026-10-09-23 D15. Once
# the executor pool exists, "no executor anywhere is running this project" needs its own
# confirmation method; that is decided in unit 4, not here.
#
# The file I/O primitives come in through -Io (a hashtable of scriptblocks) so tests can fail any
# single step; production passes nothing and gets New-InflightLedgerIo.

function New-InflightLedgerIo {
    return @{
        WriteTemp = { param([string]$TempPath, [string]$Text)
            [System.IO.File]::WriteAllText($TempPath, $Text, (New-Object System.Text.UTF8Encoding($false))) }
        Replace   = { param([string]$TempPath, [string]$Path) 
            # [NullString]::Value, not $null: PowerShell passes $null to a .NET string as "", which
            # File.Replace rejects as an empty backup path.
            [System.IO.File]::Replace($TempPath, $Path, [NullString]::Value) }
        Move      = { param([string]$TempPath, [string]$Path) [System.IO.File]::Move($TempPath, $Path) }
        Copy      = { param([string]$Path, [string]$Dest) [System.IO.File]::Copy($Path, $Dest, $false) }
    }
}

# Strict read. status: 'ok' (rows = hashtable launch_id -> row hashtable), 'absent', or 'corrupt'.
# Every row must be an object carrying a non-empty string `project`; anything else is corrupt, so a
# half-understood file can never read as "no row for this project".
function Read-InflightLedger {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @{ status = 'absent'; rows = $null; error = $null } }
    try {
        $text = [System.IO.File]::ReadAllText($Path)
        $obj = $text | ConvertFrom-Json -ErrorAction Stop
    }
    catch { return @{ status = 'corrupt'; rows = $null; error = $_.Exception.Message } }
    if ($obj -isnot [System.Management.Automation.PSCustomObject]) {
        return @{ status = 'corrupt'; rows = $null; error = 'root is not a JSON object' }
    }
    $rows = @{}
    foreach ($p in $obj.PSObject.Properties) {
        $v = $p.Value
        if ($v -isnot [System.Management.Automation.PSCustomObject]) {
            return @{ status = 'corrupt'; rows = $null; error = "row '$($p.Name)' is not an object" }
        }
        $proj = $v.PSObject.Properties['project']
        if ($null -eq $proj -or $proj.Value -isnot [string] -or [string]::IsNullOrEmpty($proj.Value)) {
            return @{ status = 'corrupt'; rows = $null; error = "row '$($p.Name)' has no project" }
        }
        $row = [ordered]@{}
        foreach ($f in $v.PSObject.Properties) { $row[$f.Name] = if ($f.Value -is [datetime]) { $f.Value.ToUniversalTime().ToString('o') } else { $f.Value } }
        $rows[$p.Name] = $row
    }
    return @{ status = 'ok'; rows = $rows; error = $null }
}

function New-InflightSentinelRow {
    param([Parameter(Mandatory)][string]$Reason, [Parameter(Mandatory)][string]$NowIso)
    return [ordered]@{ project = '*'; reason = $Reason; at = $NowIso }
}

# Read, repair (absent / corrupt -> `*` sentinel), apply $Mutate to the rows, write atomically.
# Returns @{ ok; error; events } — events are log lines the caller writes (created / corrupt copy).
# Never throws.
function Update-InflightLedger {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][scriptblock]$Mutate,
        [Parameter(Mandatory)][string]$NowIso,
        [hashtable]$Io = $null
    )
    if ($null -eq $Io) { $Io = New-InflightLedgerIo }
    $events = @()
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        $tmp = $null
        try {
            $read = Read-InflightLedger -Path $Path
            $rows = @{}
            if ($read.status -eq 'ok') { $rows = $read.rows }
            elseif ($read.status -eq 'absent') {
                $rows['*'] = New-InflightSentinelRow -Reason 'absent' -NowIso $NowIso
            }
            else {
                # Copy, never rename: the original stays at $Path until the sentinel file replaces it,
                # so a crash between the two steps still leaves a file that reads as corrupt.
                $stamp = ([datetime]$NowIso).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')
                $copy = "$Path.corrupt-$stamp"
                & $Io.Copy $Path $copy
                $events += "inflight-file-corrupt ($($read.error)); copied to $copy; sentinel set"
                $rows['*'] = New-InflightSentinelRow -Reason 'corrupt' -NowIso $NowIso
            }
            & $Mutate $rows
            $dir = Split-Path -Parent $Path
            if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
            $tmp = Join-Path $dir ("conductor-inflight.json.tmp-" + [guid]::NewGuid().ToString('N'))
            & $Io.WriteTemp $tmp (ConvertTo-Json -InputObject $rows -Depth 5)
            if ($read.status -eq 'absent') {
                try { & $Io.Move $tmp $Path }
                catch {
                    # The file appeared between our read and the move: re-read it and apply the change
                    # as a Replace instead of overwriting whatever is there now.
                    if ($attempt -eq 1 -and (Test-Path -LiteralPath $Path -PathType Leaf)) { continue }
                    throw
                }
                $events += 'inflight-file-created (absent; sentinel set)'
            }
            else { & $Io.Replace $tmp $Path }
            $tmp = $null
            return @{ ok = $true; error = $null; events = $events }
        }
        catch { return @{ ok = $false; error = $_.Exception.Message; events = $events } }
        finally {
            if ($null -ne $tmp -and (Test-Path -LiteralPath $tmp)) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
        }
    }
    return @{ ok = $false; error = 'conductor-inflight.json changed twice under the update'; events = $events }
}

# At the start of every tick, before any candidate is looked at: create a MISSING ledger holding the
# `*` (absent) sentinel. Without this a tick where every candidate is parked never reaches
# Add-InflightMark, so the file is never created, the acknowledgement gate stays at
# `inflight-file-absent` forever and the operator has no `*` row to clear (PR-gate #560 round 1,
# objection 1). An existing file — ok or corrupt — is left exactly as it is: the create is a
# non-overwriting Move, and a file that appears under it is re-read and kept (Update-InflightLedger).
# Never throws; a failed create is reported and the gate keeps saying `inflight-file-absent`.
function Initialize-InflightLedger {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$NowIso, [hashtable]$Io = $null)
    if (Test-Path -LiteralPath $Path -PathType Leaf) { return @{ ok = $true; error = $null; events = @(); created = $false } }
    $result = Update-InflightLedger -Path $Path -NowIso $NowIso -Io $Io -Mutate { param($rows) }
    $result.created = ($result.ok -and ($result.events -contains 'inflight-file-created (absent; sentinel set)'))
    return $result
}

# Before a launch: add this launch's row. ok=$false means: do not launch. Without -LaunchId a fresh
# GUID is used; the result's launch_id is the key the caller later passes to Remove-InflightMark.
function Add-InflightMark {
    param(
        [Parameter(Mandatory)][string]$Path, [string]$LaunchId = '',
        [Parameter(Mandatory)][string]$Project, [Parameter(Mandatory)][string]$Key,
        [Parameter(Mandatory)][string]$NowIso, [hashtable]$Io = $null
    )
    if ([string]::IsNullOrEmpty($LaunchId)) { $LaunchId = [guid]::NewGuid().ToString() }
    $row = [ordered]@{ project = $Project; key = $Key; started_at = $NowIso }
    $result = Update-InflightLedger -Path $Path -NowIso $NowIso -Io $Io -Mutate { param($rows) $rows[$LaunchId] = $row }.GetNewClosure()
    $result.launch_id = $LaunchId
    return $result
}

# After a launch whose exit is confirmed: remove ONLY this launch's row. Other rows (another launch
# of the same project, a `*` sentinel) are left alone. ok=$false leaves the row in place.
function Remove-InflightMark {
    param(
        [Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$LaunchId,
        [Parameter(Mandatory)][string]$NowIso, [hashtable]$Io = $null
    )
    return (Update-InflightLedger -Path $Path -NowIso $NowIso -Io $Io -Mutate { param($rows) $rows.Remove($LaunchId) }.GetNewClosure())
}

# The acknowledgement gate: may the sweep report observed=hold for $Project? Read-only and strict;
# a missing file, an unreadable file, a `*` row or a row of the project all say no.
function Test-InflightClearForProject {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Project)
    $read = Read-InflightLedger -Path $Path
    if ($read.status -eq 'absent') { return @{ clear = $false; reason = 'inflight-file-absent' } }
    if ($read.status -ne 'ok') { return @{ clear = $false; reason = "inflight-file-unreadable: $($read.error)" } }
    if ($read.rows.ContainsKey('*')) { return @{ clear = $false; reason = "inflight-sentinel ($($read.rows['*'].reason))" } }
    foreach ($id in $read.rows.Keys) {
        if ($read.rows[$id].project -ceq $Project) { return @{ clear = $false; reason = "inflight-row $id" } }
    }
    return @{ clear = $true; reason = $null }
}

# At the dirty-clone-parked skip: does this project's hold still need an acknowledgement? A hold the
# loop already observed never reaches the parked check (Test-HoldObserved skips it first), but the
# predicate checks both sides so it means the same thing wherever it is called.
function Test-HoldAckViaParkWanted {
    param($Control)
    if ($null -eq $Control) { return $false }
    if ($Control.desired_state -ne 'hold') { return $false }
    return ($Control.observed_state -ne 'hold')
}

# After the candidate loop: report observed=hold once for each project in $Pending that launched
# nothing this tick and whose in-flight ledger is clear. $Report is the bounded probe call
# (param($project) -> the Invoke-BoundedUvProbe result). Observability only: never throws, and a
# failed report is a WARN line. Returns one @{ project; outcome; reason } per pending project.
function Invoke-HoldAckViaPark {
    param(
        [hashtable]$Pending,
        [hashtable]$LaunchedProjects,
        [Parameter(Mandatory)][string]$InflightPath,
        [Parameter(Mandatory)][scriptblock]$Report
    )
    $results = @()
    if ($null -eq $Pending) { return $results }
    foreach ($proj in @($Pending.Keys | Sort-Object)) {
        $reason = $null
        if ($null -ne $LaunchedProjects -and $LaunchedProjects.ContainsKey($proj)) { $reason = 'launched this tick' }
        else {
            $gate = Test-InflightClearForProject -Path $InflightPath -Project $proj
            if (-not $gate.clear) { $reason = $gate.reason }
        }
        if ($null -ne $reason) {
            Write-Log "hold-ack via park ${proj}: skipped ($reason)"
            $results += @{ project = $proj; outcome = 'skipped'; reason = $reason }
            continue
        }
        try {
            $r = & $Report $proj
            if ($null -ne $r -and $r.ok -and $r.code -eq 0) {
                Write-Log "hold-ack via park ${proj}: reported observed=hold (no launch; clone parked)"
                $results += @{ project = $proj; outcome = 'reported'; reason = $null }
            }
            else {
                $why = if ($null -eq $r) { 'no result' } elseif (-not $r.ok) { "$($r.error)" } else { "exit $($r.code): $("$($r.stderr)".Trim())" }
                Write-Log "WARN hold-ack via park ${proj}: report failed ($why) — tick continues"
                $results += @{ project = $proj; outcome = 'failed'; reason = $why }
            }
        }
        catch {
            Write-Log "WARN hold-ack via park ${proj}: report failed ($($_.Exception.Message)) — tick continues"
            $results += @{ project = $proj; outcome = 'failed'; reason = $_.Exception.Message }
        }
    }
    return $results
}
