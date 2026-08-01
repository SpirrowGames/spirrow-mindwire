#!/usr/bin/env pwsh
# deploy/sync-clock-http.ps1 — keep this host's clock correct without NTP (I-4).
#
# Why this exists instead of w32time: every firewall profile on sg-tomtebo-01 is
# DefaultOutboundAction=Block and nothing grants UDP/123, so w32time cannot reach ANY time source —
# external or on the tailnet (measured 2026-08-02: `w32tm /stripchart` returns 0x800705B4 against
# time.windows.com, ntp.nict.jp, time.google.com and 100.79.84.62 alike). w32time therefore reports
# "Source: Free-running System Clock" and had never synchronised; the clock had drifted 173 s.
#
# The egress that DOES work is HTTPS through the local squid proxy, so this takes the time from an
# HTTP `Date` response header instead. That header has one-second granularity, so this corrects to
# roughly ±1 s — three orders of magnitude better than the drift it replaces, and enough for log
# correlation and token/TLS validity. It is NOT a substitute for real NTP: w32time still reports
# unsynchronised, and if UDP/123 is ever opened for W32Time this script should be retired.
#
# Registered as a scheduled task under SYSTEM because Set-Date needs SeSystemtimePrivilege.

[CmdletBinding()]
param(
    # Any endpoint that answers cheaply and is on squid's allow-list. generate_204 returns an empty
    # body, so this costs one request header exchange.
    [string]$SourceUrl = "https://www.google.com/generate_204",
    [string]$ProxyUrl = "http://127.0.0.1:3128",
    # Only correct when the clock is off by more than this. Without a dead band the clock would be
    # nudged every single run by the sub-second noise of a 1-second-resolution header.
    [double]$ThresholdSeconds = 5.0,
    # Absolute, NOT $HOME-derived: this runs as SYSTEM, whose profile is
    # C:\Windows\system32\config\systemprofile — $HOME would silently write logs somewhere nobody looks.
    [string]$LogDir = "C:\Users\tomtar\spirrow-mindwire-data\logs",
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$logPath = Join-Path $LogDir ("clock-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

function Write-Log {
    param([string]$Message)
    $line = "[" + (Get-Date -Format "yyyy-MM-dd HH:mm:ss.fffK") + "] [clock] $Message"
    Write-Host $line
    Add-Content -LiteralPath $logPath -Value $line -Encoding utf8
}

try {
    # Bracket the request so the reading can be attributed to the midpoint of the round trip rather
    # than to either end of it — otherwise the whole RTT lands in the measurement as one-sided error.
    $t0 = [DateTimeOffset]::UtcNow
    $resp = Invoke-WebRequest -Uri $SourceUrl -Method Head -Proxy $ProxyUrl `
        -MaximumRedirection 0 -TimeoutSec $TimeoutSeconds -ErrorAction Stop
    $t1 = [DateTimeOffset]::UtcNow

    $dateHeader = $resp.Headers['Date']
    if ($dateHeader -is [array]) { $dateHeader = $dateHeader[0] }
    if (-not $dateHeader) { throw "response from $SourceUrl carried no Date header" }

    $server = [DateTimeOffset]::Parse($dateHeader, [System.Globalization.CultureInfo]::InvariantCulture)
    $rttMs = ($t1 - $t0).TotalMilliseconds
    $localAtMidpoint = $t0.AddTicks([long](($t1 - $t0).Ticks / 2))
    $drift = ($localAtMidpoint - $server).TotalSeconds   # positive = local clock is AHEAD

    Write-Log ("source={0} rtt={1:N0}ms server={2:yyyy-MM-dd HH:mm:ssK} local={3:yyyy-MM-dd HH:mm:ssK} drift={4:N1}s" -f `
        $SourceUrl, $rttMs, $server.ToLocalTime(), $localAtMidpoint.ToLocalTime(), $drift)

    if ([math]::Abs($drift) -le $ThresholdSeconds) {
        Write-Log ("within +/-{0}s dead band — no correction" -f $ThresholdSeconds)
        exit 0
    }

    # Re-read the clock at the moment of correction so the wait since measurement is not baked in.
    $target = [DateTimeOffset]::Now.AddSeconds(-$drift)
    Set-Date -Date $target.LocalDateTime | Out-Null
    Write-Log ("CORRECTED by {0:N1}s -> {1:yyyy-MM-dd HH:mm:ssK}" -f (-$drift), [DateTimeOffset]::Now)
    exit 0
}
catch {
    # A failed sync must not look like a success in the task history, but it also must not be noisy
    # beyond one line — the next run is 15 minutes away.
    Write-Log "FAILED: $($_.Exception.Message)"
    exit 1
}
