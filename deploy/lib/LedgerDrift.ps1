# ledger_drift (T-ledger-writeback-on-merge-and-thread-close-on-task-done, design v0.3,
# Bohr msg-9524 / msg-9526). Read only: it never writes to the ledger or posts to a thread.
#
# Test-LedgerDriftEnabled reads sweep.json's `ledger_drift.enabled`. Off (the shipped default: the
# key is absent) the wrapper spawns nothing. A file or section that does not parse reads as on, so
# the probe parses it, refuses (exit 1, before any client exists) and the wrapper logs that loudly
# instead of a typo silently reading as "off" (the settled_close convention).
#
# Invoke-LedgerDriftTick runs the module once per tick over the given projects; the module writes
# <logs_dir>/ledger_drift/<project>.json per project. A non-zero exit (a result file that could not
# be saved, msg-9526 Obj-2-2) is logged with the probe's stderr: the digest then sees the old file's
# generated_at and calls it 古い結果, and this log line says why. Fail-open on the sweep.

function Test-LedgerDriftEnabled {
    param([Parameter(Mandatory)][string]$SweepConfigPath)
    if (-not (Test-Path -LiteralPath $SweepConfigPath)) { return $false }
    try {
        $cfg = Get-Content -LiteralPath $SweepConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch { return $true }
    if ($null -eq $cfg -or -not ($cfg.PSObject.Properties.Name -contains 'ledger_drift')) { return $false }
    $section = $cfg.ledger_drift
    if ($section -isnot [System.Management.Automation.PSCustomObject]) { return $true }
    if (-not ($section.PSObject.Properties.Name -contains 'enabled')) { return $false }
    if ($section.enabled -is [bool]) { return $section.enabled }
    return $true
}

function Invoke-LedgerDriftTick {
    param(
        [string[]]$Projects,
        [Parameter(Mandatory)][string]$SweepConfigPath,
        [Parameter(Mandatory)][scriptblock]$Invoke,
        [int]$TimeoutSeconds = 300
    )
    if (-not (Test-LedgerDriftEnabled -SweepConfigPath $SweepConfigPath)) { return $null }
    if (@($Projects).Count -eq 0) { return $null }
    try {
        $arguments = @('-m', 'spirrow_mindwire.ledger_drift', '--tick', '--projects', ($Projects -join ','), '--sweep-config', $SweepConfigPath)
        $r = & $Invoke $arguments $TimeoutSeconds 'ledger-drift'
        if (-not $r.ok) {
            Write-Log "ledger-drift: probe did not complete ($($r.error)) — result files keep their old generated_at; sweep continues (fail-open)"
            return $null
        }
        if ($r.code -ne 0) {
            Write-Log "ledger-drift: exit=$($r.code) — a result was not saved: $("$($r.stderr)".Trim())"
        }
        $json = Get-ProbeJsonLine -Result $r
        if (-not $json) {
            Write-Log "ledger-drift: no JSON on stdout (exit=$($r.code)) — failing open"
            return $null
        }
        Write-Log "ledger-drift: $json"
        return ($json | ConvertFrom-Json)
    }
    catch {
        Write-Log "ledger-drift failed ($($_.Exception.Message)) — sweep continues (fail-open)"
        return $null
    }
}
