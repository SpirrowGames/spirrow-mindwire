# settled_close (T-done-disposition-auto-close boundary 6, Bohr msg-8455 §2 6-3).
#
# Test-SettledCloseEnabled reads sweep.json's `settled_close.enabled`. The wrapper calls the probe
# only when it is true, so a disabled tick (the shipped default: the key is absent) spawns no
# process and makes no MCP call. Enabling it is a Tier-C decision together with registering the
# closer in Prismind and adding it to magickit `settled_close.closers`; this file never decides it.
#
# A file that does not parse reads as enabled on purpose: the probe then parses it, refuses to run
# (`settled_close.*` errors exit 1 before any MCP client exists) and the wrapper logs that loudly,
# instead of a typo silently reading as "off". Nothing can be closed on that path.

function Test-SettledCloseEnabled {
    param([Parameter(Mandatory)][string]$SweepConfigPath)
    if (-not (Test-Path -LiteralPath $SweepConfigPath)) { return $false }
    try {
        $cfg = Get-Content -LiteralPath $SweepConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch { return $true }
    if ($null -eq $cfg -or -not ($cfg.PSObject.Properties.Name -contains 'settled_close')) { return $false }
    $section = $cfg.settled_close
    if ($section -isnot [System.Management.Automation.PSCustomObject]) { return $true }
    if (-not ($section.PSObject.Properties.Name -contains 'enabled')) { return $false }
    if ($section.enabled -is [bool]) { return $section.enabled }
    return $true
}

# One invocation per tick over the projects whose HOLD check passed (the list 1c uses).
# Fail-open on the SWEEP, like Invoke-AutoGateTick: a broken pass must not stop the sweep.
# $Invoke is the bounded probe runner (the wrapper passes Invoke-BoundedUvProbe), so tests can
# stand in for it.
function Invoke-SettledCloseTick {
    param(
        [string[]]$Projects,
        [Parameter(Mandatory)][string]$SweepConfigPath,
        [Parameter(Mandatory)][scriptblock]$Invoke,
        [int]$TimeoutSeconds = 300
    )
    if (-not (Test-SettledCloseEnabled -SweepConfigPath $SweepConfigPath)) { return $null }
    if (@($Projects).Count -eq 0) { return $null }
    try {
        $arguments = @('-m', 'spirrow_mindwire.settled_close', '--tick', '--projects', ($Projects -join ','), '--sweep-config', $SweepConfigPath)
        $r = & $Invoke $arguments $TimeoutSeconds 'settled-close'
        if (-not $r.ok) {
            Write-Log "settled-close: probe did not complete ($($r.error)) — sweep continues (fail-open)"
            return $null
        }
        $json = Get-ProbeJsonLine -Result $r
        if (-not $json) {
            Write-Log "settled-close: no JSON on stdout (exit=$($r.code)) — failing open"
            return $null
        }
        Write-Log "settled-close: $json"
        return ($json | ConvertFrom-Json)
    }
    catch {
        Write-Log "settled-close failed ($($_.Exception.Message)) — sweep continues (fail-open)"
        return $null
    }
}
