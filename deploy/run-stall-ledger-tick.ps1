#!/usr/bin/env pwsh
# deploy/run-stall-ledger-tick.ps1 — the scheduled call site of the stall-ledger tick (D-16c C1).
#
# Spec: T-stalled-pr-has-no-detector msg-5744 §3 (C1) / msg-5746. Registered with Task Scheduler by
# deploy/Register-StallLedgerTask.ps1, which sets the heartbeat interval H and the kill limit
# T_LOCK_STALE; both values live in src/spirrow_mindwire/stall_ledger/timing.py and a test pins the
# two files together.
#
# One run = one heartbeat: `scripts/stall_ledger_tick.py` takes the ledger lock, evaluates, writes
# the store and prints JSON lines. This wrapper only decides where those lines go:
#   <data_dir>/logs/stall-ledger-YYYY-MM-DD.jsonl   stdout, one JSON object per line (append)
#   <data_dir>/logs/stall-ledger-YYYY-MM-DD.err.log stderr (append), kept out of the JSONL file
#
# Log-only (D-16ab §0 still holds): nothing here posts to Discord, the chatroom or GitHub. Alerts
# are D-16d. A tick that cannot evaluate still writes its `heartbeat` line with `evaluated=false`,
# so a stop is visible in the JSONL file rather than silent.
#
# Overlap needs no handling here: the task is MultipleInstancesPolicy=IgnoreNew, and the ledger's
# own OS lock makes a second concurrent tick log `tick_skipped=locked` and exit.

param(
    # owner/name, repeatable. Each becomes one `--repo`.
    [string[]]$Repo = @(),
    # chatroom project, repeatable. Each becomes one `--project`.
    [string[]]$Project = @()
)

$ErrorActionPreference = 'Stop'

# `pwsh -File` hands a comma list over as ONE string, so split it here.
$Repo = @($Repo | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
$Project = @($Project | ForEach-Object { $_ -split ',' } | Where-Object { $_ })

$repoRoot = Split-Path -Parent $PSScriptRoot
$dataDir = if ($env:MINDWIRE_PATHS__DATA_DIR) { $env:MINDWIRE_PATHS__DATA_DIR } else { Join-Path $HOME "spirrow-mindwire-data" }
$logDir = Join-Path $dataDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$day = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd')
$outLog = Join-Path $logDir "stall-ledger-$day.jsonl"
$errLog = Join-Path $logDir "stall-ledger-$day.err.log"

# GitHub identity (T-stalled-pr-has-no-detector msg-6415 Q1). The tick reads GitHub with the token
# in MINDWIRE_STALL_LEDGER_GITHUB_TOKEN (the implementer PAT, read-only use; never the naysayer
# token), handed to the child process ONLY, as MINDWIRE_GITHUB_TOKEN. It is a dedicated variable so
# that setting it does not also reach the conductor, and the wrapper drops any inherited
# MINDWIRE_GITHUB_TOKEN / GITHUB_TOKEN so the ledger never reads under an identity nobody chose for
# it. Unset or blank: the child gets no token and the github source reports `auth_missing` on the
# heartbeat (loud), instead of reading unauthenticated into the per-IP rate limit (msg-6414).
Remove-Item Env:MINDWIRE_GITHUB_TOKEN -ErrorAction SilentlyContinue
Remove-Item Env:GITHUB_TOKEN -ErrorAction SilentlyContinue
if ($env:MINDWIRE_STALL_LEDGER_GITHUB_TOKEN -and $env:MINDWIRE_STALL_LEDGER_GITHUB_TOKEN.Trim()) {
    $env:MINDWIRE_GITHUB_TOKEN = $env:MINDWIRE_STALL_LEDGER_GITHUB_TOKEN
}

$tickArgs = @('run', 'python', (Join-Path $repoRoot 'scripts/stall_ledger_tick.py'), '--data-dir', $dataDir)
foreach ($r in $Repo) { $tickArgs += @('--repo', $r) }
foreach ($p in $Project) { $tickArgs += @('--project', $p) }

Push-Location $repoRoot
try {
    & uv @tickArgs 1>> $outLog 2>> $errLog
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code
