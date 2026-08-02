#!/usr/bin/env pwsh
# deploy/run-conductor-scheduled.ps1 — scheduled-run wrapper around deploy/run-conductor.ps1 (I-1c).
#
# Why: the Task Scheduler entry launched the conductor against whatever thread happened to be left in
# mindwire.toml, and wrote no log anywhere. This wrapper
#   1. asks the chatroom which threads have actually moved (scripts/thread_heads.py),
#   2. walks an explicit priority list, head first, SKIPPING every thread whose head message is
#      unchanged since the last time the conductor ran on it,
#   3. writes each remaining candidate into <data_dir>/config/mindwire.toml [conductor].task_thread_id,
#   4. runs the real launcher, stopping at the first thread that actually ran rounds, and
#   5. pushes a Discord notification whenever the loop parks on a human.
#
# Why (1)+(2) exist — this is what makes a 5-minute cadence affordable. A thread whose last message
# is `NEXT: none` / `NEXT: human` is finished or waiting on Takahito, so the conductor exits at once
# with `rounds=0`; that costs an MCP read and no inference, which is cheap but pointless. The
# expensive case is a thread whose `NEXT:` names a role: the conductor DISPATCHES that role, the role
# posts nothing, and the tick has burned an inference for no progress (measured 2026-08-02 on
# T-track-b-seam-octree-retirement). Polling that every 5 minutes would be 288 wasted dispatches a
# day. The head probe answers "did anything change?" from data — one `chatroom_my_unread` call, no
# message bodies, no inference, ~1 s for every thread at once — so unchanged threads are never
# launched at all. This replaces an earlier cooldown-timer design: a timer guesses, the head id knows.
#
# Fail-open everywhere: if the probe fails, or a thread is missing from its result, that thread is
# launched anyway. A probe gap then costs one cheap run instead of silently parking a live thread.
#
# Overlapping runs need no handling here — the task is registered MultipleInstancesPolicy=IgnoreNew,
# so a tick that fires while the previous sweep is still working is dropped by the scheduler.
#
# Fail-safe on the sweep itself: only a clean `rounds=0` advances to the next candidate. A non-zero
# exit or an unparseable run stops the sweep, so a genuine breakage cannot chew through the whole
# list and be reported as "everything idle".

$ErrorActionPreference = "Stop"

# --- paths -------------------------------------------------------------------------------------
# mindwire-loop reads <data_dir>/config/mindwire.toml; honour the same env var run-conductor.ps1 does.
$dataDir = if ($env:MINDWIRE_PATHS__DATA_DIR) { $env:MINDWIRE_PATHS__DATA_DIR } else { Join-Path $HOME "spirrow-mindwire-data" }
$configPath = Join-Path $dataDir "config\mindwire.toml"
$logDir = Join-Path $dataDir "logs"
$logPath = Join-Path $logDir ("conductor-" + (Get-Date -Format "yyyy-MM-dd") + ".log")
$notifyStatePath = Join-Path $dataDir "state\notified.json"
$headsStatePath = Join-Path $dataDir "state\heads.json"
$sweepConfigPath = Join-Path $dataDir "config\sweep.json"
$repoRoot = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

# --- logging ------------------------------------------------------------------------------------
# At a 5-minute cadence the common tick is "nothing moved", and writing a dozen lines for that would
# put ~3k lines of noise a day between the entries that matter. So detail is buffered and only
# committed once the tick proves it did something; an idle tick collapses to a single line.
$script:pendingLines = New-Object System.Collections.Generic.List[string]
$script:logCommitted = $false

function Format-LogLine {
    param([string]$Message)
    return "[" + (Get-Date -Format "yyyy-MM-dd HH:mm:ssK") + "] [wrapper] $Message"
}

function Write-Log {
    param([string]$Message)
    $line = Format-LogLine $Message
    Write-Host $line
    if ($script:logCommitted) { Add-Content -LiteralPath $logPath -Value $line -Encoding utf8 }
    else { $script:pendingLines.Add($line) | Out-Null }
}

# Call the moment the tick becomes worth a permanent record; flushes the buffer and switches to
# write-through so nothing after it is held back.
function Confirm-LogWorthKeeping {
    if ($script:logCommitted) { return }
    $script:logCommitted = $true
    if ($script:pendingLines.Count -gt 0) {
        Add-Content -LiteralPath $logPath -Value $script:pendingLines -Encoding utf8
        $script:pendingLines.Clear()
    }
}

# Discard the buffered detail of an uneventful tick and keep exactly one line.
function Write-QuietSummary {
    param([string]$Message)
    if ($script:logCommitted) { Write-Log $Message; return }
    $script:pendingLines.Clear()
    $line = Format-LogLine $Message
    Write-Host $line
    Add-Content -LiteralPath $logPath -Value $line -Encoding utf8
    $script:logCommitted = $true
}

# --- small JSON state files ---------------------------------------------------------------------
function Get-JsonState {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return @{} }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw -Encoding utf8
        if (-not $raw.Trim()) { return @{} }
        $obj = $raw | ConvertFrom-Json
        $map = @{}
        foreach ($p in $obj.PSObject.Properties) { $map[$p.Name] = $p.Value }
        return $map
    }
    catch {
        # Corrupt state must not block the sweep; worst case is one duplicate alert / one extra run.
        Write-Log "state file unreadable ($Path): $($_.Exception.Message) — treating as empty"
        return @{}
    }
}

function Save-JsonState {
    param([string]$Path, [hashtable]$State)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    [System.IO.File]::WriteAllText($Path, ($State | ConvertTo-Json -Depth 5), (New-Object System.Text.UTF8Encoding($false)))
}

# --- parse the conductor's own verdict ----------------------------------------------------------
# The daemon's last word on stdout looks like:
#   conductor stopped: reason=none rounds=0 forced_naysayer=0 ... last_msg=msg-1919
# Returns @{ reason; rounds; last_msg }; nulls mean "could not tell".
function Get-ConductorVerdict {
    param([string[]]$Output)

    $line = $Output | Where-Object { $_ -match 'conductor stopped:' } | Select-Object -Last 1
    $verdict = @{ reason = $null; rounds = $null; last_msg = $null }
    if (-not $line) { return $verdict }
    if ($line -match 'reason=(\S+)') { $verdict.reason = $Matches[1] }
    if ($line -match 'rounds=(\d+)') { $verdict.rounds = [int]$Matches[1] }
    if ($line -match 'last_msg=(\S+)') { $verdict.last_msg = $Matches[1] }
    return $verdict
}

# --- push notification (C-2) --------------------------------------------------------------------
# Delivers "the loop is waiting on Takahito" to his phone via a Discord webhook. Chosen over the
# Claude Code PushNotification tool because that one only exists inside a live Claude Code session
# with Remote Control; this conductor is an unattended Windows scheduled task driving headless SDK
# roles, so it has no such session to notify from.
#
# The webhook URL is a bearer secret (anyone holding it can post to the channel), so it is read from
# the environment, never committed, and never written to the log — including on failure, where the
# URL is scrubbed out of the exception text before logging.
#
# Delivery goes through the local squid proxy on purpose: pwsh.exe has no outbound firewall
# permission of its own, so a request that skipped the proxy would be blocked rather than silently
# escaping the egress chokepoint.
$notifyWebhook = [Environment]::GetEnvironmentVariable('MINDWIRE_NOTIFY_DISCORD_WEBHOOK', 'User')
$notifyProxy = if ($env:MINDWIRE_NOTIFY_PROXY) { $env:MINDWIRE_NOTIFY_PROXY } else { "http://127.0.0.1:3128" }

function Send-Notification {
    param([string]$Message)

    Confirm-LogWorthKeeping
    if (-not $notifyWebhook) {
        Write-Log "notification skipped (MINDWIRE_NOTIFY_DISCORD_WEBHOOK not set)"
        return
    }
    try {
        $payload = @{ content = $Message } | ConvertTo-Json -Compress
        $null = Invoke-WebRequest -Uri $notifyWebhook -Method Post `
            -ContentType 'application/json; charset=utf-8' `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($payload)) `
            -Proxy $notifyProxy -TimeoutSec 30 -ErrorAction Stop
        Write-Log "notification sent: $Message"
    }
    catch {
        # Never fail the sweep because the notifier failed — the conductor's work already happened.
        $reason = "$($_.Exception.Message)".Replace($notifyWebhook, '<webhook-redacted>')
        Write-Log "notification FAILED (non-fatal): $reason"
    }
}

# A thread parked on `NEXT: human` stays parked until Takahito acts, and the sweep re-reads it on
# every tick. Without this, one unattended weekend would fire the same alert repeatedly and the
# channel would be trained into noise. Fires only when $Signature differs from the last alert.
function Send-NotificationIfChanged {
    param([hashtable]$State, [string]$Key, [string]$Signature, [string]$Message)

    if ($State.ContainsKey($Key) -and $State[$Key] -eq $Signature) {
        Write-Log "notification suppressed (unchanged since last alert: $Key = $Signature)"
        return
    }
    Send-Notification -Message $Message
    $State[$Key] = $Signature
}

# --- the sweep list (config, NOT code) -----------------------------------------------------------
# Each candidate is a (project, thread_id, repo_dir) triple. Two reasons it is a file and not a
# literal in this script:
#   1. adding one thread to the loop used to require a PR against this repo — absurd friction for
#      what is an operational decision, and it meant a live thread simply did not get worked;
#   2. a thread that is not listed is never picked up *at all*, silently. That is how
#      `T-pr-gate-adr-index-scope` sat stranded after being opened.
#
# It is JSON, not TOML, and separate from mindwire.toml on purpose: the sweep is the wrapper's
# concern, not the daemon's, and `MindwireSettings` is a strict model — putting it there would force
# a package change for what is a deployment list. PowerShell also parses JSON natively and has no
# TOML reader.
#
# `T-pr-review-*` threads must NOT be listed: they resolve to `NEXT: pr-review <ref>`, which fires
# the Tier B PR-gate against the paid Lexora/Gemini backend. Driving those from an unattended sweep
# would spend money on a timer, so it stays an explicit human action.
function Get-SweepCandidates {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        # Fail loud. A silent fallback to a hardcoded list is exactly the failure this file exists
        # to end — the operator would never learn the config was not being read.
        throw ("sweep config not found: $Path — copy deploy/sweep.json.example there and edit it. " +
               "Refusing to fall back to a built-in list.")
    }
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding utf8 | ConvertFrom-Json
    $out = @()
    foreach ($c in @($raw.candidates)) {
        foreach ($f in 'project', 'thread_id', 'repo_dir') {
            if (-not $c.$f) { throw "sweep config entry missing '$f': $($c | ConvertTo-Json -Compress)" }
        }
        $out += [pscustomobject]@{
            project   = $c.project
            thread_id = $c.thread_id
            repo_dir  = $c.repo_dir
            key       = "$($c.project)/$($c.thread_id)"   # state key: thread ids are only unique per project
        }
    }
    if ($out.Count -eq 0) { throw "sweep config has no candidates: $Path" }
    return $out
}

# --- config writer --------------------------------------------------------------------------------
# Line-oriented, section-aware: only the named key inside the named section is touched, everything
# else (comments, ordering, [conductor.roster], [naysayer_gating]) is preserved byte-for-byte.
function Set-TomlValue {
    param([string]$Path, [string]$Section, [string]$Key, [string]$Value)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "config not found: $Path (set MINDWIRE_PATHS__DATA_DIR to the data root holding config/mindwire.toml)"
    }

    $lines = [System.IO.File]::ReadAllLines($Path)
    $section = ""
    $index = -1
    $current = $null

    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^\s*\[([^\]]+)\]\s*$') { $section = $Matches[1]; continue }
        if ($section -eq $Section -and $lines[$i] -match ('^\s*' + [regex]::Escape($Key) + '\s*=\s*"(.*)"\s*$')) {
            $index = $i
            $current = $Matches[1]
            break
        }
    }

    if ($index -lt 0) {
        throw "no [$Section].$Key line found in $Path — refusing to guess; fix the config by hand."
    }
    if ($current -eq $Value) { return }

    $lines[$index] = $Key + ' = "' + $Value + '"'
    # UTF-8 without BOM, LF-preserving via explicit join (ReadAllLines already stripped the endings).
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, ($lines -join "`n") + "`n", $utf8NoBom)
    Write-Log "[$Section].$Key : `"$current`" -> `"$Value`""
}

# --- head probe ----------------------------------------------------------------------------------
# Returns @{ thread_id = latest_msg_id } for every thread the chatroom reports, or $null when the
# probe could not run. $null and "thread absent from the map" both mean UNKNOWN, and the caller
# launches the conductor for those — see the fail-open note in the header.
function Invoke-HeadProbe {
    param([string]$Project)

    $probe = Join-Path $repoRoot "scripts\thread_heads.py"
    if (-not (Test-Path -LiteralPath $probe)) {
        Write-Log "head probe not found at $probe — failing open (every candidate will be launched)"
        return $null
    }
    try {
        Push-Location $repoRoot
        try { $raw = & uv run python $probe --project $Project 2>&1; $code = $LASTEXITCODE }
        finally { Pop-Location }

        if ($code -ne 0) {
            Write-Log "head probe exited $code — failing open. Output: $(($raw | ForEach-Object { "$_" }) -join ' / ')"
            return $null
        }
        $json = $raw | ForEach-Object { "$_" } | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
        if (-not $json) { Write-Log "head probe produced no JSON — failing open"; return $null }

        $obj = $json | ConvertFrom-Json
        $map = @{}
        foreach ($p in $obj.heads.PSObject.Properties) { $map[$p.Name] = $p.Value }
        return $map
    }
    catch {
        Write-Log "head probe failed ($($_.Exception.Message)) — failing open"
        return $null
    }
}

# --- run ---------------------------------------------------------------------------------------
$exitCode = 0
try {
    Write-Log "=== scheduled conductor run starting (host $env:COMPUTERNAME, user $env:USERNAME) ==="
    $candidates = Get-SweepCandidates -Path $sweepConfigPath
    Write-Log "sweep list ($($candidates.Count) candidates from $sweepConfigPath): $(($candidates | ForEach-Object { $_.key }) -join ', ')"

    # One probe per distinct project, not per candidate — the probe returns every thread of a project
    # in a single call, so N candidates in one project still cost one call.
    $headsByProject = @{}
    foreach ($proj in ($candidates | ForEach-Object { $_.project } | Sort-Object -Unique)) {
        $h = Invoke-HeadProbe -Project $proj
        $headsByProject[$proj] = $h
        if ($null -ne $h) { Write-Log "head probe [$proj]: $($h.Count) threads reported" }
    }
    $headsState = Get-JsonState -Path $headsStatePath
    $notifyState = Get-JsonState -Path $notifyStatePath

    $inner = Join-Path $PSScriptRoot "run-conductor.ps1"
    $didWork = $false
    $attempt = 0
    $launched = 0
    $skipped = 0
    $sweepSignature = @()

    # Stop reasons that need Takahito. Mirrors StopReason in conductor/core.py — `human` plus every
    # `*_to_human` fallback are all "the loop parked on a human", and round_cap / empty_thread are
    # anomalies he would otherwise never hear about. Only `none` (settled) is silent, because that is
    # the normal end of a thread and the sweep just moves on.
    #
    # Do NOT narrow this to `reason -eq 'human'`: measured 2026-08-02, the first live sweep produced
    # `no_progress_to_human`, which such a check silently drops.
    $needsHuman = @{
        'human'                = "あなたの判断待ちで停止しました"
        'no_handoff_to_human'  = "NEXT: が読めず human に fallback して停止しました"
        'no_progress_to_human' = "dispatch した role が何も投稿せず停止しました"
        'round_cap'            = "ラウンド上限で停止しました（暴走バックストップ発動）"
        'empty_thread'         = "スレッドにメッセージがありません（優先リストの指定ミスの可能性）"
    }

    foreach ($cand in $candidates) {
        $attempt++
        $thread = $cand.thread_id
        $heads = $headsByProject[$cand.project]
        $probeHead = if ($null -ne $heads -and $heads.ContainsKey($thread)) { $heads[$thread] } else { $null }
        $knownHead = if ($headsState.ContainsKey($cand.key)) { $headsState[$cand.key] } else { $null }

        # The whole point: an unchanged head means the conductor would resolve the same handoff and
        # reach the same stop, so do not pay for the launch. Requires BOTH ids — a thread we have
        # never run, or one the probe did not report, is unknown and gets launched.
        if ($probeHead -and $knownHead -and $probeHead -eq $knownHead) {
            $skipped++
            $sweepSignature += "$($cand.key)=$probeHead"
            Write-Log "candidate $attempt/$($candidates.Count): $($cand.key) — head unchanged ($probeHead), not launching"
            continue
        }

        Confirm-LogWorthKeeping
        $why = if (-not $probeHead) { "head unknown (probe gap) — failing open" } elseif (-not $knownHead) { "no recorded head yet" } else { "head moved $knownHead -> $probeHead" }
        Write-Log "--- candidate $attempt/$($candidates.Count): $($cand.key) ($why) ---"
        # All three must move together: the daemon reads the thread from [conductor] but the project
        # and the implementer's clone from [loop], so a stale [loop] would drive the right thread
        # against the wrong repo.
        Set-TomlValue -Path $configPath -Section 'loop'      -Key 'project'        -Value $cand.project
        Set-TomlValue -Path $configPath -Section 'loop'      -Key 'repo_dir'       -Value $cand.repo_dir
        Set-TomlValue -Path $configPath -Section 'conductor' -Key 'task_thread_id' -Value $thread

        $launched++
        $output = (& $inner *>&1) | ForEach-Object { "$_" }
        $code = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
        $verdict = Get-ConductorVerdict -Output $output
        # Keep the daemon's raw output only when the run was eventful; a plain `rounds=0` stop is
        # fully described by the summary line below.
        if ($code -ne 0 -or $null -eq $verdict.rounds -or $verdict.rounds -gt 0) {
            Add-Content -LiteralPath $logPath -Value $output -Encoding utf8
        }
        Write-Log "$($cand.key) -> exit=$code reason=$($verdict.reason) rounds=$($verdict.rounds) last_msg=$($verdict.last_msg)"

        if ($verdict.last_msg) { $headsState[$cand.key] = $verdict.last_msg }
        $sweepSignature += "$($cand.key)=$($verdict.last_msg)"

        if ($code -eq 0 -and $verdict.reason -and $needsHuman.ContainsKey($verdict.reason)) {
            # Signature carries the reason too, so a thread that changes *how* it is stuck re-alerts
            # even when last_msg has not moved.
            Send-NotificationIfChanged -State $notifyState -Key $cand.key `
                -Signature "$($verdict.reason):$($verdict.last_msg)" `
                -Message ("MindWire: **$thread** ($($cand.project)) — " + $needsHuman[$verdict.reason] +
                          " (reason=$($verdict.reason), rounds=$($verdict.rounds), $($verdict.last_msg))。chatroom を確認してください。")
        }

        # Fail-safe 1: a real breakage must not be laundered into "this thread had no work".
        if ($code -ne 0) {
            Write-Log "conductor exited non-zero — stopping the sweep (this is a failure, not an idle thread)"
            $exitCode = $code
            break
        }
        # Fail-safe 2: no parseable verdict means we do not know whether work happened.
        if ($null -eq $verdict.rounds) {
            Write-Log "no parseable 'conductor stopped:' line — stopping the sweep (unknown state, not idle)"
            break
        }
        if ($verdict.rounds -gt 0) {
            Write-Log "thread did work (rounds=$($verdict.rounds), reason=$($verdict.reason)) — sweep done"
            $didWork = $true
            break
        }
        Write-Log "no work (rounds=0, reason=$($verdict.reason)) — advancing to the next candidate"
    }

    Save-JsonState -Path $headsStatePath -State $headsState

    if ($skipped -eq $candidates.Count) {
        # The steady state at a 5-minute cadence. One line, no notification: by definition nothing
        # changed, so there is nothing new to tell anyone.
        Write-QuietSummary "no thread moved ($skipped/$($candidates.Count) heads unchanged) — nothing to do"
    }
    elseif (-not $didWork -and $exitCode -eq 0 -and $attempt -eq $candidates.Count -and $launched -gt 0) {
        # Every candidate was settled or blocked on the human: the loop has run out of work and only
        # Takahito can give it more. Keyed on the whole sweep's head set, so a list that is still idle
        # for the same reason stays quiet and only a genuine change re-alerts.
        Write-Log "ALL CANDIDATES IDLE — no thread in the priority list had work; the loop needs a human"
        Send-NotificationIfChanged -State $notifyState -Key "__all_idle__" `
            -Signature ($sweepSignature -join '|') `
            -Message "MindWire: sweep 対象の $($candidates.Count) スレッド全てに仕事がありません。ループは停止したままです — 次に取り組む対象の指定が要ります。"
    }

    Save-JsonState -Path $notifyStatePath -State $notifyState
}
catch {
    $exitCode = 1
    Confirm-LogWorthKeeping
    Write-Log "FAILED: $($_.Exception.Message)"
    Write-Log ($_.ScriptStackTrace | Out-String)
}
finally {
    if ($script:logCommitted) { Write-Log "=== scheduled conductor run finished (exit $exitCode) ===" }
    else { Write-QuietSummary "sweep finished with nothing to do (exit $exitCode)" }
}

exit $exitCode
