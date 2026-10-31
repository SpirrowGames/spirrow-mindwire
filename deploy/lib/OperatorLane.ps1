# deploy/lib/OperatorLane.ps1 — the conductor tick's operator pass (T-operator-task-lane PR2, D1').
#
# The lane that takes `NEXT: operator` heads is driven by THIS tick and nothing else (ADR-2026-10-09-23
# D1 / D15 as read in msg-7410: one conductor decides what runs; no second scheduled task). Once per
# tick, after the head probe, the wrapper hands the probed heads of every non-held project to
# `python -m spirrow_mindwire.operator_lane pass`, which dispatches at most one task (or settles the
# one in flight) and returns within ~30 s plus a few chatroom calls. The session itself runs detached
# under its own runner, so the conductor's own sweep is not held up by operator work.
#
# Fail-open for the sweep: a broken pass is logged and the tick goes on. The lane's own safety does not
# depend on this wrapper — the pass and the runner fail closed on their own state.

# The config file the pass reads. Read here only to avoid starting Python on a tick where the lane is
# off (the common case); the pass re-reads it and is the authority.
function Test-OperatorLaneEnabled {
    param([Parameter(Mandatory)][string]$ConfigPath)
    if (-not (Test-Path -LiteralPath $ConfigPath)) { return $false }
    try {
        $cfg = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        return ($cfg.enabled -eq $true)
    }
    catch { return $true }  # malformed: let the pass report it loudly
}

# One row per sweep candidate whose project the head probe answered. Held projects are never probed
# (the control loop `continue`s before Invoke-HeadProbe), so they are absent from $HeadsByProject and
# get no rows: D1' "control state の hold が付いた project には触りません".
function ConvertTo-OperatorLaneCandidates {
    param([object[]]$Candidates, [hashtable]$HeadsByProject)
    $rows = [System.Collections.Generic.List[object]]::new()
    foreach ($c in @($Candidates)) {
        $heads = $HeadsByProject[$c.project]
        if ($null -eq $heads -or -not $heads.ContainsKey($c.thread_id)) { continue }
        $rows.Add([ordered]@{ project = [string]$c.project; thread_id = [string]$c.thread_id; head_msg_id = [string]$heads[$c.thread_id] })
    }
    return , $rows.ToArray()
}

# Runs the pass. $Invoke is the bounded-probe runner (Invoke-BoundedUvProbe in production; a fake in
# tests) and receives -Arguments / -TimeoutSeconds / -Label. Returns the parsed result object
# (action, events) or $null when the pass did not complete; never throws.
function Invoke-OperatorLanePass {
    param(
        [object[]]$Rows,
        [Parameter(Mandatory)][string]$DataDir,
        [Parameter(Mandatory)][string]$HeadSkipPath,
        [Parameter(Mandatory)][scriptblock]$Invoke,
        [Parameter(Mandatory)][scriptblock]$NewInputFile,
        [Parameter(Mandatory)][scriptblock]$RemoveInputFile,
        [int]$WrapperPid = $PID,
        [int]$TimeoutSeconds = 180
    )
    $inputFile = $null
    try {
        $json = ConvertTo-Json -InputObject @($Rows) -Depth 4 -Compress
        $inputFile = & $NewInputFile $json 'operator-lane'
        $r = & $Invoke @('-m', 'spirrow_mindwire.operator_lane', 'pass',
            '--candidates-file', $inputFile, '--wrapper-pid', "$WrapperPid",
            '--head-skip-path', $HeadSkipPath, '--data-dir', $DataDir) $TimeoutSeconds 'operator-lane-pass'
        if (-not $r.ok -or $r.code -ne 0) {
            Write-Log "operator pass did not complete (exit $($r.code); $($r.error)) — sweep continues. $(([string]$r.stderr).Trim())"
            return $null
        }
        $line = @(([string]$r.stdout) -split "`r?`n" | Where-Object { $_.TrimStart().StartsWith('{') }) | Select-Object -Last 1
        if (-not $line) { Write-Log "operator pass printed no JSON — sweep continues"; return $null }
        return ($line | ConvertFrom-Json)
    }
    catch {
        Write-Log "operator pass failed ($($_.Exception.Message)) — sweep continues"
        return $null
    }
    finally {
        if ($inputFile) { & $RemoveInputFile $inputFile }
    }
}

# Logs every event; a `notify` event also goes to Discord, deduplicated by key + message like every
# other wrapper alert.
function Publish-OperatorLaneResult {
    param($Result, [hashtable]$NotifyState)
    if ($null -eq $Result) { return }
    if ([string]$Result.action -notin @('disabled', 'idle', 'busy')) { Confirm-LogWorthKeeping }
    Write-Log "operator pass: $($Result.action)"
    foreach ($e in @($Result.events)) {
        if ($null -eq $e) { continue }
        Write-Log "operator pass: $($e.message)"
        if ([string]$e.level -eq 'notify') {
            Send-NotificationIfChanged -State $NotifyState -Key ([string]$e.key) -Signature ([string]$e.message) `
                -Message ("MindWire: " + [string]$e.message)
        }
    }
}
