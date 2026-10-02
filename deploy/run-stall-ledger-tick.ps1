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
