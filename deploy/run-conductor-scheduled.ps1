#!/usr/bin/env pwsh
# deploy/run-conductor-scheduled.ps1 — scheduled-run wrapper around deploy/run-conductor.ps1 (I-1c).
#
# Why: the Task Scheduler entry launched the conductor against whatever thread happened to be left in
# mindwire.toml, and wrote no log anywhere. This wrapper
#   1. asks loop control whether each project is HELD (scripts/loop_control.py) and drops every
#      candidate of a held project — but only once the loop has acknowledged that hold, so the first
#      tick of a HOLD still launches and lands the acknowledgement (see Test-HoldObserved),
#   2. asks the chatroom which threads have actually moved (scripts/thread_heads.py),
#   3. walks an explicit priority list, head first, SKIPPING every thread whose head message AND
#      project control state are both unchanged since the last time the conductor ran on it,
#   4. writes each remaining candidate into <data_dir>/config/mindwire.toml [conductor].task_thread_id,
#   5. runs the real launcher, stopping at the first thread that actually ran rounds, and
#   6. pushes a Discord notification whenever the loop parks on a human OR a candidate is quarantined.
#
# (1) is an operator stop switch, set from the conclair dashboard or `loop_control_set` and stored
# per project. It is a cost optimisation here, NOT the enforcement — the conductor reads the same
# state every round and fails closed on it — which is why this probe fails open. See
# Invoke-ControlProbe.
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
# Signal / scheduling split (2026-08-11, T-sweep-failure-isolation).
# The prior fail-safe on the sweep itself was "any non-zero exit stops the sweep." Its INTENT was
# right — a real breakage must not be laundered into `everything is idle` — but the mechanism was
# itself silent: the loop just stopped, no downstream candidate ever ran, and the only signal was a
# quiet exit code in Task Scheduler. Measured 2026-08-11: five hours of unattended starvation on
# every candidate behind the broken one, invisible from every dashboard.
#
# Fix: SIGNAL and SCHEDULING are separated. A non-zero exit now
#   (a) is DECLARED — the candidate is quarantined (record written + Discord notification), and
#   (b) does NOT stop the sweep — the next candidate is tried, so downstream work still progresses.
# The old sweep-break fail-safe is retained but re-aimed: only a FAILURE TO WRITE THE DECLARATION
# breaks the sweep. That reason still holds — if we cannot even record what went wrong, we must not
# quietly move on. See the quarantine block below (Test-QuarantineDerivedState / New-DailyDigest
# / the K-budget short-circuit) for the full mechanism, and docs/deploy.md → "Quarantine and daily
# digest" for the operational contract.
#
# Not a re-fix of the specific 2026-08-11 5h starvation. Its DIRECT cause (a merge misclassified as
# a Tier-C denial) is already closed by PR #136 (OBL-MERGE-MECHANISM). This wrapper's job is to make
# sure the NEXT unknown failure does not die in the same silent way — i.e., that "the sweep died"
# announces itself, whatever the reason.

$ErrorActionPreference = "Stop"

# --- tunables ------------------------------------------------------------------------------------
# NONE OF THESE FOUR ARE DERIVED VALUES. They are policy calls, and are collected in one place so
# nobody has to hunt for a magic number scattered across the file. Change any one of them ONLY when a
# concrete observation says the current value is wrong, and record what that observation was.
#
#   $QuarantineFailureBudget (K)  = 2
#     One failure is local, two in a sweep suggest a systemic cause. K=1 kills the whole tick on any
#     flake; K>=3 spends inferences on non-local failures before stopping. Revisit if 3+ concurrent
#     quarantines are observed — that is a candidate-order problem, not a K problem.
#
#   $QuarantineEscalatedAfter      = 24h
#     sweep cadence is 5min; 24h = 288 unattended ticks. Matches "the human reads the digest once a
#     day" — below that "escalated overnight" becomes routine and the tier loses meaning.
#
#   $QuarantineStaleAfter          = 7d
#     Practical cutoff for "not being fixed." At 7d the digest wording changes from "look at this"
#     to "fix it or fold the thread"; that wording change IS the point of the threshold.
#
#   $StarvedThreshold              = 24h
#     Deliberately EQUAL to $QuarantineEscalatedAfter. A different value would create the
#     inexplicable state "quarantined 24h but not starved." Same value = "not evaluated in 24h" is
#     the only meaning both need to carry.
$QuarantineFailureBudget  = 2
$QuarantineEscalatedAfter = [TimeSpan]::FromHours(24)
$QuarantineStaleAfter     = [TimeSpan]::FromDays(7)
$StarvedThreshold         = [TimeSpan]::FromHours(24)

# Digest cadence and how many lines of session tail we keep with a quarantine record. These ARE
# derived from the four above (digest is daily because escalation is daily) but stated here to keep
# the whole tuning surface in one section.
$DailyDigestInterval  = [TimeSpan]::FromHours(24)
$SessionLogTailLines  = 50

# --- paths -------------------------------------------------------------------------------------
# mindwire-loop reads <data_dir>/config/mindwire.toml; honour the same env var run-conductor.ps1 does.
$dataDir = if ($env:MINDWIRE_PATHS__DATA_DIR) { $env:MINDWIRE_PATHS__DATA_DIR } else { Join-Path $HOME "spirrow-mindwire-data" }
$configPath = Join-Path $dataDir "config\mindwire.toml"
$logDir = Join-Path $dataDir "logs"
$logPath = Join-Path $logDir ("conductor-" + (Get-Date -Format "yyyy-MM-dd") + ".log")
$notifyStatePath = Join-Path $dataDir "state\notified.json"
$headsStatePath = Join-Path $dataDir "state\heads.json"
$sweepConfigPath = Join-Path $dataDir "config\sweep.json"
$quarantineStatePath = Join-Path $dataDir "state\quarantine.json"
$quarantineHistoryPath = Join-Path $dataDir "state\quarantine-history.json"
$evaluatedStatePath = Join-Path $dataDir "state\evaluated.json"
$digestStatePath = Join-Path $dataDir "state\digest.json"
# Composer cache (T-decision-request-composer S2). One row per parked thread key,
# keyed by the same "project/thread_id" the notified.json / heads.json use so a
# reader can cross-reference by eye. The row carries the last composer envelope
# (question + options) and its signature; the wrapper reuses it when the
# signature has not changed (I-3: ≤1 composer call per reason:last_msg stop).
$pendingDecisionsPath = Join-Path $dataDir "state\pending-decisions.json"
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

# Coerce a JSON-round-tripped timestamp into a UTC [DateTime]. Necessary because ConvertFrom-Json
# auto-parses ISO 8601 strings into [DateTime] LOCAL objects, so a naive `[datetime]::Parse($v)`
# on such a value implicitly stringifies via the current culture, parses back, and only "happens
# to survive" because ToUniversalTime() cancels out Parse's Local-assumption offset. That coupling
# is brittle: a culture change, a DateTimeKind change, or a future refactor that drops the trailing
# ToUniversalTime() would silently produce off-by-hours ages in the starvation metric. Centralise
# the coercion here and stop scattering `[datetime]::Parse` calls across timestamp readers.
# (Tier B naysayer, PR #138 round 5, weakest remaining point.)
function ConvertTo-UtcInstant {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [datetime]) { return $Value.ToUniversalTime() }
    return [datetime]::Parse("$Value").ToUniversalTime()
}

# Merge-on-write: re-read the file just before writing and preserve any keys the operator removed
# during the tick. Used for state files where BOTH the sweep and an external tool (Clear-Quarantine)
# may write during a sweep run — quarantine.json and heads.json.
#
# WHY IT EXISTS. A tick reads its state files once at start and holds them in memory for the whole
# sweep (measured minutes on a real AI-driven candidate). If an operator runs Clear-Quarantine
# midway through, their script perfectly removes the entry from disk — but a few minutes later, the
# sweep flushes its stale in-memory map back to disk and RESURRECTS the entry. The operator's
# action is silently destroyed, the thread stays skipped, and the whole point of "clearing is a
# human act" collapses. (Tier B naysayer, PR #138 round 3.)
#
# The fix narrows the race from "the entire tick duration" (minutes) to "the gap between the
# re-read and the write" (sub-millisecond). Closing that residual window fully would require a
# real file lock, which is not a good trade because a multi-minute sweep holding a lock blocks
# every operator action for that entire time — and the whole reason the operator has this button
# is that a human's turnaround is measured in seconds. Narrowing to sub-ms IS the operational fix.
#
# MERGE POLICY:
#   - Every key currently on disk that the sweep did not touch is kept from disk. This is why the
#     merge starts by copying the current disk state in full.
#   - For every key the sweep TOUCHED IN MEMORY:
#       * if it was present at sweep START (in $OriginalKeys) AND is still present on disk after
#         re-read → the sweep's in-memory version wins (age-driven state transitions, updates);
#       * if it was present at sweep START but NOT on disk after re-read → the operator removed it
#         during the sweep — DO NOT resurrect. This IS the operator-clear-survives contract;
#       * if it was NOT present at sweep start → it is a new add this tick, write through.
#         Only the sweep adds keys to these two files, so this branch is not contested.
function Merge-StateForWrite {
    param(
        [hashtable]$Memory,
        [string[]]$OriginalKeys,
        [string]$DiskPath
    )
    $current = Get-JsonState -Path $DiskPath
    $out = @{}
    foreach ($k in @($current.Keys)) { $out[$k] = $current[$k] }
    foreach ($k in @($Memory.Keys)) {
        $wasOriginal = ($OriginalKeys -contains $k)
        if ($wasOriginal) {
            # Present at sweep start. Respect an operator removal; otherwise apply our updates.
            if ($current.ContainsKey($k)) { $out[$k] = $Memory[$k] }
        }
        else {
            # New this tick — the sweep added it. Nothing external adds to these files.
            $out[$k] = $Memory[$k]
        }
    }
    return $out
}

# Refresh an evaluated.json entry's last_evaluated_at while preserving its first_seen_at. Called
# from every disposition that ACTUALLY REACHED the candidate — a launched verdict (worked, no-work,
# non-zero exit) AND a head-skip (the sweep probed the head, proved nothing has moved, and
# correctly fast-pathed).
#
# WHY HEAD-SKIP COUNTS. A chatroom thread being idle for 24h (a weekend) is normal behaviour. If
# head-skip did NOT refresh, every legitimately-idle thread would flag as `starved` on Monday
# morning — the starvation section would fill with perfectly healthy inactive threads and the
# metric would be trained into noise. The metric asks "how long since I actually reached this
# candidate?" and a head-skip IS reaching: the sweep probed, evaluated, decided. (Tier B naysayer,
# PR #138 round 4.)
#
# WHY OTHER DISPOSITIONS DO NOT. `held` / `quarantined-skipped` / `not-reached` all mean the sweep
# never actually asked the question this tick — a HELD candidate is deliberately withheld by the
# operator, a quarantined-skipped candidate is deferred until human clear, and a not-reached
# candidate is one the sweep never got to (K-budget hit, earlier candidate did work). None of them
# advance the "how long since I actually reached this?" clock. That is the design's Q4 honesty
# rule and it is load-bearing — the metric only means anything because these DO age past 24h.
#
# The value in $State[$Key] may be a hashtable (freshly written this tick) or a PSCustomObject
# (round-tripped from disk). Dot access to .first_seen_at works on both — same duck-typing pattern
# as Get-FingerprintHint.
function Update-EvaluatedTimestamp {
    param([hashtable]$State, [string]$Key, [datetime]$Now)

    # ConvertFrom-Json auto-parses ISO 8601 strings into DateTime objects. So a first_seen_at read
    # from disk is a [DateTime], not a [string]. Normalise back to a canonical ISO string on the
    # way out so downstream readers always receive the same shape regardless of whether the value
    # came from disk or from an in-memory write earlier this tick.
    $firstSeen = $null
    if ($State.ContainsKey($Key) -and $null -ne $State[$Key]) {
        $priorInstant = ConvertTo-UtcInstant $State[$Key].first_seen_at
        if ($priorInstant) { $firstSeen = $priorInstant.ToString("o") }
    }
    $row = @{ last_evaluated_at = $Now.ToUniversalTime().ToString("o") }
    if ($firstSeen) { $row.first_seen_at = $firstSeen }
    $State[$Key] = $row
}

# --- head-state records ---------------------------------------------------------------------------
# A head record is @{ head; control } — the message id the conductor last reported for a thread, AND
# the project control state it reported it under. Both are needed to decide a skip; see Test-CanSkip.
#
# Entries written before this became a pair are bare strings. Those are read as control = $null,
# which never matches a live state, so a pre-upgrade state file costs one launch per thread and then
# self-heals into the new shape. That is deliberately the fail-open direction: the alternative
# (assume the old head was recorded under the current state) would reproduce the very bug this
# record exists to fix, once, silently, on the upgrade tick.
function Get-HeadRecord {
    param([hashtable]$State, [string]$Key)

    $empty = @{ head = $null; control = $null }
    if (-not $State.ContainsKey($Key)) { return $empty }
    $value = $State[$Key]
    if ($null -eq $value) { return $empty }
    if ($value -is [string]) { return @{ head = $value; control = $null } }
    return @{
        head    = if ($value.PSObject.Properties.Name -contains 'head') { $value.head } else { $null }
        control = if ($value.PSObject.Properties.Name -contains 'control') { $value.control } else { $null }
    }
}

# May a held project's launch be optimised away this tick?
#
# ONLY once the loop has already reported that it saw the hold. `desired_state` is the operator's
# request; `observed_state` is the loop's acknowledgement, and only a launched conductor writes it
# (``LoopControlReader.report_observed``). Dropping the launch on `desired` alone therefore starves
# the very write-back the dashboard uses to tell "stopping" from "stopped" — the hold shows as
# pending forever, which reads as the loop ignoring the stop switch.
#
# Confirmed in production, and the way it was confirmed is the point: on 2026-08-06 both projects sat
# at `hold`, and `spirrow-voxelworld` had `observed_state: null` — never acknowledged, because it was
# never launched. `spirrow-mindwire` had `observed: hold` only because its control probe happened to
# FAIL that tick, so the sweep failed open, launched, and the conductor landed the write-back. A
# transient outage was the only reason the feedback loop ever closed. (Tier B naysayer on PR #126.)
#
# So the first tick of a hold pays for one process start; every tick after it costs one MCP read.
# That is the whole cost of an operator being able to see their stop switch take effect.
function Test-HoldObserved {
    param($Control)

    if ($null -eq $Control) { return $false }          # unreadable probe — fail open, launch
    if ($Control.desired_state -ne 'hold') { return $false }
    return ($Control.observed_state -eq 'hold')
}

# The skip rule, stated in one place because getting it wrong is invisible.
#
# A thread may be skipped only when we can show the conductor would reach the SAME stop it reached
# last time. Two inputs decide that stop, not one:
#
#   1. the thread's head message — does the same handoff still sit at the end of the thread?
#   2. the project's loop control state — does that handoff still ROUTE the same way?
#
# (2) is not redundant. At an unchanged head, a naysayer→implementer handoff stops at the human gate
# under `hold`/`supervised` but dispatches the implementer under `run` (carve-out ③, conductor/
# core.py). Keying the cache on the head alone therefore skips forever exactly the threads a release
# from `hold` was meant to start, while the log reports a healthy `no thread moved … nothing to do`
# at exit 0 — measured 2026-08-06, 15+ minutes, indistinguishable from an idle loop.
#
# Anything unknown ($null) fails OPEN into a launch: an unreadable control probe, a thread the head
# probe did not report, a never-run thread, a pre-upgrade record. One cheap run beats a silent park,
# which is the same stance the rest of this wrapper takes.
function Test-CanSkip {
    param([string]$ProbeHead, [string]$KnownHead, [string]$CurrentControl, [string]$KnownControl)

    if (-not $ProbeHead -or -not $KnownHead) { return $false }
    if (-not $CurrentControl -or -not $KnownControl) { return $false }
    return ($ProbeHead -eq $KnownHead) -and ($CurrentControl -eq $KnownControl)
}

# --- quarantine ---------------------------------------------------------------------------------
# A quarantined thread has failed at least once and will not be launched again by this wrapper until
# a human clears it (deploy/Clear-Quarantine.ps1). Records live in <data_dir>/state/quarantine.json,
# one entry per `project/thread_id`, alongside heads.json — separate writers, separate concerns; the
# candidate filter reads both and ANDs the two out-decisions.
#
# WHY THIS EXISTS AT ALL — the old wrapper broke the sweep on any non-zero exit. That fail-safe
# stopped a real failure from being laundered into "everything idle" (right) but did it silently
# (wrong): the loop simply stopped, every downstream candidate was starved, and nothing announced
# the state anywhere. The rule now: DECLARE the failure (record + notify), then keep the sweep
# moving so downstream work still gets to run. Only a failure to write the declaration itself
# breaks the sweep.
#
# WHAT THIS FILE STORES — the minimum needed to (a) explain what broke last time and (b) let a
# human decide whether to clear. It does NOT store anything used to AUTO-CLEAR: there is no auto
# clear path (Q3, spec/msg-814). Fields:
#   state                  quarantined | escalated | stale (derived from first_failure_at)
#   first_failure_at       ISO 8601, UTC — set once
#   last_failure_at        ISO 8601, UTC — refreshed if the same fingerprint fails again
#   consecutive_failures   how many times the quarantine has been re-hit (usually 1: quarantined
#                          threads are SKIPPED, so a re-hit needs a manual re-run or a probe change)
#   exit_code              the conductor's exit code at the failure
#   stop_reason            the parsed `reason=...`, or $null if the run died before that line
#   failure_fingerprint    { head; control } observed at the failure — see the fingerprint rule
#   session_log_path       path to today's sweep log, so the tail can be found in context
#   session_log_tail       last $SessionLogTailLines of the conductor's stdout+stderr for this run
#
# FINGERPRINT — deliberately narrow. Its two components are (head, control), which the runner
# already computes every tick for the head-skip cache. NOTHING that requires reaching outside the
# runner's own observation surface goes in here — no `git rev`, no `config hash`. That rule is
# general: fingerprint components are limited to values the runner ALREADY HAS in-hand for its own
# purposes. Widening the surface always brings one of (a) coupling by overreach, (b) silent
# coverage-loss on the metric, (c) false positives. This whole record adds exactly zero new probes.
#
# HINT USE ONLY — the fingerprint changing has NO scheduling effect. It is displayed in the digest
# as a hint so the human's eye lands on threads whose input has moved since the failure, but auto
# clear is NEVER a consequence. The design's Q3 ("解除は人手のみ") is load-bearing; automating it
# here would collapse two questions ("did the input change" and "is the failure resolved") that the
# runner cannot distinguish.

function New-QuarantineRecord {
    param(
        [string]$FirstFailureAt,
        [int]$ExitCode,
        [string]$StopReason,
        [string]$FailureHead,
        [string]$FailureControl,
        [string]$SessionLogPath,
        [string[]]$SessionLogTail
    )

    return @{
        state                = 'quarantined'
        first_failure_at     = $FirstFailureAt
        last_failure_at      = $FirstFailureAt
        consecutive_failures = 1
        exit_code            = $ExitCode
        stop_reason          = $StopReason
        failure_fingerprint  = @{ head = $FailureHead; control = $FailureControl }
        session_log_path     = $SessionLogPath
        session_log_tail     = $SessionLogTail
    }
}

# Compute the age-derived state (quarantined -> escalated -> stale). The stored `state` field
# lags this until the sweep observes the transition and fires the notification for it.
function Get-DerivedQuarantineState {
    param(
        [datetime]$FirstFailureAt,
        [datetime]$Now,
        [TimeSpan]$EscalatedAfter = $script:QuarantineEscalatedAfter,
        [TimeSpan]$StaleAfter = $script:QuarantineStaleAfter
    )

    $age = $Now - $FirstFailureAt
    if ($age -ge $StaleAfter)      { return 'stale' }
    if ($age -ge $EscalatedAfter)  { return 'escalated' }
    return 'quarantined'
}

# Compare the fingerprint recorded at the failure against the current probe. Returns a short label
# describing what moved, or $null when nothing has moved. Deliberately names ONLY what the runner
# actually observes — "新規メッセージあり (head 変化)" / "control 変更あり" — never "input changed"
# in the abstract. The wider claim would silently over-promise coverage the fingerprint does not
# have (spec/msg-814 §1, §4). No claim is a legitimate outcome and it means: no head movement and
# no control change — not "not fixed."
#
# $Fingerprint is DELIBERATELY untyped. On the tick a failure occurs it is a native `hashtable`
# (from New-QuarantineRecord). On every subsequent tick the record is round-tripped through
# quarantine.json — ConvertFrom-Json parses nested objects into PSCustomObject, NOT hashtable, so a
# `[hashtable]$Fingerprint` parameter would throw a terminating "Cannot process argument
# transformation" the moment the daily digest touches an existing quarantine record. Untyped works
# for both because dot-notation duck-types on either shape. (Tier B naysayer, PR #138 round 2.)
function Get-FingerprintHint {
    param(
        $Fingerprint,
        [string]$CurrentHead,
        [string]$CurrentControl
    )

    if (-not $Fingerprint) { return $null }
    $storedHead    = $Fingerprint.head
    $storedControl = $Fingerprint.control
    $parts = @()
    if ($storedHead -and $CurrentHead -and $CurrentHead -ne $storedHead) {
        $parts += "新規メッセージあり (head 変化)"
    }
    if ($storedControl -and $CurrentControl -and $CurrentControl -ne $storedControl) {
        $parts += "control 変更あり"
    }
    if ($parts.Count -eq 0) { return $null }
    return ($parts -join " / ")
}

# Human-readable duration for the digest. "3d 4h" / "18h" / "45m". Kept deliberately coarse — this
# is a wall-clock display, not a metric, so seconds are pointless.
function Format-DurationDigest {
    param([TimeSpan]$Span)
    if ($Span.TotalMinutes -lt 1) { return "<1m" }
    $days = [int][Math]::Floor($Span.TotalDays)
    $hours = $Span.Hours
    $minutes = $Span.Minutes
    if ($days -gt 0) {
        if ($hours -gt 0) { return "${days}d ${hours}h" }
        return "${days}d"
    }
    if ($Span.TotalHours -ge 1) {
        if ($minutes -gt 0) { return "${hours}h ${minutes}m" }
        return "${hours}h"
    }
    return "${minutes}m"
}

# Should the daily-digest clock advance given this Send-Notification result?
#
# 'sent'     -> yes, the notification landed and we do not want to re-attempt for 24h.
# 'skipped'  -> yes, the operator has no webhook configured. Retrying every 5 minutes accomplishes
#               nothing (the webhook will not appear on its own) and would log-spam the daemon.
# 'failed'   -> no, the webhook is configured but the POST failed. This is the ONLY case where the
#               next tick should retry — the whole reason the clock is gated on the result at all.
#
# Pulled out as a helper so the "which outcomes are terminal?" decision is testable and lives in
# one place. If a future refactor adds a fourth outcome, this table is the only line to touch.
# (Tier B naysayer, PR #138 round 5.)
function Test-DigestClockAdvances {
    param([string]$Result)
    return ($Result -eq 'sent' -or $Result -eq 'skipped')
}

# The signature Send-NotificationIfChanged uses to dedup the K-budget "systemic cause suspected"
# alert. Bucketed on the UTC date, NOT on the tick timestamp, because the failure this de-noise
# addresses is not "same tick, twice" (impossible — the sweep breaks at K) but "adjacent ticks,
# same underlying wave": during a real systemic outage, tick T fills its K=2 budget and stops; tick
# T+5min skips the first 2 quarantined threads, fails the next 2, and hits the budget again — and
# every one of the next 288 ticks does the same. A per-tick timestamp defeats the dedup and turns
# the day into a Discord flood; a per-day bucket fires ONCE per day of an ongoing wave, then falls
# silent. If the wave clears and returns days later, the day bucket has moved and the alert re-arms.
# (Tier B naysayer, PR #138 round 2.)
function Get-SystemicAlertSignature {
    param([datetime]$Now, [int]$Count)
    return "$($Now.ToUniversalTime().ToString('yyyy-MM-dd')):$Count"
}

# Build the daily digest string. Sent even when both lists are empty — a silent day IS the point,
# because "no alert" then still means "the channel is alive," which is what the 5h failure
# specifically lacked (spec/msg-814 §5, §7).
#
# The starvation report is pivoted on `$LiveKeys` — the sweep list of the current tick — not on the
# keys accumulated in `$EvaluatedState`. Two failure modes that pivot fixes:
#   1. False negatives. A candidate that never launched (permanently `not-reached` behind a K-budget
#      hit, or held/quarantined on creation) never enters `EvaluatedState` and would silently be
#      invisible to the metric. That reproduces the exact "suppressed dark area" Q4 forbids.
#   2. False positives. `EvaluatedState` accumulates keys forever; a folded thread stays in the
#      file, inevitably ages past the threshold, and spams the digest every day.
# Iterating over the live sweep list closes both — and it is the *only* metric that meaningfully
# answers "how long since we actually reached this candidate?", because "this candidate" only exists
# for the sweep in the first place through the sweep list.
#
# Age fallback: when a live key has no `last_evaluated_at` yet (never launched), the wrapper writes
# `first_seen_at` the first tick the candidate appears, and the age is computed against that. So a
# newly-added candidate is not "immediately starved" — its 24h clock starts ticking from first
# sight, which is the earliest moment the metric can honestly say the thread was in scope.
function New-DailyDigest {
    param(
        [hashtable]$QuarantineState,
        [hashtable]$EvaluatedState,
        [hashtable]$HeadsByProject,
        [hashtable]$ControlByProject,
        [datetime]$Now,
        [string[]]$LiveKeys = @(),
        [array]$HumanParked = @(),
        [hashtable]$PendingDecisionsState = @{}
    )

    # Split by derived state (based on age, not stored — the digest is a snapshot of reality now).
    $escalatedList = @()
    $quarantinedList = @()
    $staleList = @()
    foreach ($key in $QuarantineState.Keys) {
        $rec = $QuarantineState[$key]
        $firstAt = ConvertTo-UtcInstant $rec.first_failure_at
        $derived = Get-DerivedQuarantineState -FirstFailureAt $firstAt -Now $Now
        $age = Format-DurationDigest -Span ($Now - $firstAt)

        # Extract project id from key ("project/thread_id") to reach the right probe map.
        $project = ($key -split '/', 2)[0]
        $thread  = ($key -split '/', 2)[1]
        $curHead = $null
        if ($HeadsByProject.ContainsKey($project) -and $null -ne $HeadsByProject[$project]) {
            $h = $HeadsByProject[$project]
            if ($h.ContainsKey($thread)) { $curHead = $h[$thread] }
        }
        $curControl = $null
        if ($ControlByProject.ContainsKey($project) -and $null -ne $ControlByProject[$project]) {
            $curControl = $ControlByProject[$project].desired_state
        }
        $hint = Get-FingerprintHint -Fingerprint $rec.failure_fingerprint `
                                    -CurrentHead $curHead -CurrentControl $curControl

        $line = "  $key   $age"
        if ($hint) { $line += "   ⚠ $hint" }

        switch ($derived) {
            'stale'       { $staleList       += $line }
            'escalated'   { $escalatedList   += $line }
            default       { $quarantinedList += $line }
        }
    }

    # Starvation. Pivoted on $LiveKeys, NOT $EvaluatedState.Keys — the same reason the header of
    # this function spells out. A live key that is absent from $EvaluatedState (never launched) is
    # a legitimate starvation candidate; a state key that is not live (folded from the sweep list)
    # is not, and must not appear here.
    $starvedList = @()
    foreach ($key in $LiveKeys) {
        $entry = if ($EvaluatedState.ContainsKey($key)) { $EvaluatedState[$key] } else { $null }
        $lastAtRaw = if ($entry) { $entry.last_evaluated_at } else { $null }
        $firstSeenRaw = if ($entry) { $entry.first_seen_at } else { $null }
        # Fallback ladder: last_evaluated_at (best), else first_seen_at (candidate has never launched
        # but was in scope from tick T), else THIS tick (a candidate the sweep just discovered — its
        # 24h clock starts now, so it is not yet starved). ConvertTo-UtcInstant absorbs both string
        # (freshly written) and [DateTime] (JSON round-tripped) shapes.
        $lastAt = ConvertTo-UtcInstant $lastAtRaw
        $firstSeen = ConvertTo-UtcInstant $firstSeenRaw
        $ageBase = if ($lastAt) { $lastAt } elseif ($firstSeen) { $firstSeen } else { $Now }
        $age = $Now - $ageBase
        if ($age -ge $script:StarvedThreshold) {
            # A "never evaluated" thread carries a slightly different label so the operator does not
            # spend cognitive effort deciding whether the entry means "stuck" or "never touched."
            $suffix = if (-not $lastAtRaw) { "   (未評価)" } else { "" }
            $starvedList += "  $key   $(Format-DurationDigest -Span $age)$suffix"
        }
    }

    $lines = @()
    $lines += "MindWire 日次ダイジェスト ($(Get-Date -Date $Now.ToLocalTime() -Format 'yyyy-MM-dd HH:mm'))"
    $lines += ""

    $totalQ = $escalatedList.Count + $quarantinedList.Count + $staleList.Count
    $lines += "隔離中: $totalQ 件"
    if ($totalQ -eq 0) {
        $lines += "  (該当なし)"
    }
    else {
        if ($staleList.Count -gt 0) {
            $lines += "  [stale] — 直すか、スレッドを畳むか決めよ"
            $lines += $staleList
        }
        if ($escalatedList.Count -gt 0) {
            $lines += "  [escalated] — 24h 以上経過"
            $lines += $escalatedList
        }
        if ($quarantinedList.Count -gt 0) {
            $lines += "  [quarantined]"
            $lines += $quarantinedList
        }
    }

    $lines += ""
    # 判断待ち — T-decision-request-composer S4 (msg-1370 §0 欠陥 2 / A-4). Included even when
    # 0 件, for the same "silent day is the point" reason as 飢餓 (msg-814 §5). The count and the
    # keys come from the sweep-side poll (D-23), so this section restores from a deleted
    # pending-decisions.json — the cache would only enrich the row with the composed question.
    $lines += "判断待ち: $($HumanParked.Count) 件"
    if ($HumanParked.Count -eq 0) {
        $lines += "  (該当なし)"
    }
    else {
        foreach ($p in $HumanParked) {
            # Enrich the row from the composed-question cache when the same signature is in
            # pending-decisions.json. When it is not (A-14: cache deleted, cache stale, composer
            # broken), the row still shows the key and reason — nothing goes silent.
            $sig = "$($p.reason):$($p.head)"
            $questionSnippet = $null
            if ($PendingDecisionsState.ContainsKey($p.key)) {
                $row = $PendingDecisionsState[$p.key]
                # Duck-type on either shape (freshly written hashtable vs. JSON-loaded PSCustomObject).
                if ($row -is [hashtable]) { $rowSig = $row['signature']; $env = $row['envelope'] }
                else { $rowSig = $row.signature; $env = $row.envelope }
                if ($rowSig -eq $sig -and $env) {
                    $status = if ($env.PSObject.Properties.Name -contains 'composer_status') { $env.composer_status } else { $null }
                    $output = if ($env.PSObject.Properties.Name -contains 'output') { $env.output } else { $null }
                    if ($status -eq 'ok' -and $output) {
                        $q = if ($output.PSObject.Properties.Name -contains 'question') { $output.question } else { $null }
                        if ($q) {
                            # Keep the digest one-line-per-row readable: cap the snippet at 80 chars
                            # and never let a newline in the question wrap the digest layout.
                            $flat = ($q -replace "`r?`n", ' ').Trim()
                            if ($flat.Length -gt 80) { $flat = $flat.Substring(0, 79) + '…' }
                            $questionSnippet = $flat
                        }
                    }
                }
            }
            $suffix = if ($questionSnippet) { "   — $questionSnippet" } else { "   — (問い未生成)" }
            $lines += "  $($p.key)   [$($p.reason)]$suffix"
        }
    }

    $lines += ""
    $lines += "飢餓 (24h 以上評価されていない): $($starvedList.Count) 件"
    if ($starvedList.Count -eq 0) {
        $lines += "  (該当なし)"
    }
    else {
        $lines += $starvedList
    }

    $lines += ""
    $lines += "(0 件でも送信しています — 通知チャネル自体の生存確認を兼ねます。"
    $lines += " 人間がこのダイジェストを読まない状態はチャネル故障ではなく運用の放棄であり、機械では検知できません。)"
    return ($lines -join "`n")
}

# The starvation view. Pivoted on the LIVE sweep candidates ($LiveKeys), not on the keys that
# happen to have accumulated in $EvaluatedState. See the header of New-DailyDigest for why: pivoting
# on the state file opens exactly the two failure modes this whole design refuses (silently dropping
# never-launched candidates, and permanently spamming for folded ones).
#
# A live key IS starved when its effective evaluation age (last_evaluated_at, or first_seen_at when
# never launched) is at least $Threshold. Quarantined threads are INCLUDED — their timestamps are
# never refreshed while quarantined, so they age past the threshold on their own; the spec is
# explicit that this metric's honesty depends on the quarantine-induced dark area being visible
# (msg-814 Q4).
function Get-StarvedKeys {
    param(
        [hashtable]$EvaluatedState,
        [datetime]$Now,
        [TimeSpan]$Threshold = $script:StarvedThreshold,
        [string[]]$LiveKeys = @()
    )
    $out = @()
    foreach ($key in $LiveKeys) {
        $entry = if ($EvaluatedState.ContainsKey($key)) { $EvaluatedState[$key] } else { $null }
        $lastAtRaw = if ($entry) { $entry.last_evaluated_at } else { $null }
        $firstSeenRaw = if ($entry) { $entry.first_seen_at } else { $null }
        # ConvertTo-UtcInstant handles both string (freshly written) and [DateTime] (JSON round-
        # tripped) shapes. See its header for why raw [datetime]::Parse on a DateTime is brittle.
        $lastAt = ConvertTo-UtcInstant $lastAtRaw
        $firstSeen = ConvertTo-UtcInstant $firstSeenRaw
        $ageBase = if ($lastAt) { $lastAt } elseif ($firstSeen) { $firstSeen } else { $Now }
        if (($Now - $ageBase) -ge $Threshold) { $out += $key }
    }
    return $out
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
    # `last_msg=None` is Python's None reaching stdout, i.e. the conductor had no message to report.
    # Matched by \S+ like any id, so without this it was stored as the literal head "None" — never
    # equal to a real id (so it failed open, harmlessly) but it made "no head recorded yet"
    # indistinguishable from a real head in both the state file and the log.
    if ($line -match 'last_msg=(\S+)' -and $Matches[1] -ne 'None') { $verdict.last_msg = $Matches[1] }
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
        # 'skipped' — no webhook configured. Distinct from 'failed' because the operator has
        # deliberately chosen not to run with Discord alerts, and a retry loop makes no sense: the
        # webhook will not appear on its own. The digest gate advances its clock on 'skipped' so
        # a webhook-less run does not permanently flood the log with "sending daily digest" and
        # "notification skipped" every 5 minutes forever. (Tier B naysayer, PR #138 round 5.)
        Write-Log "notification skipped (MINDWIRE_NOTIFY_DISCORD_WEBHOOK not set)"
        return 'skipped'
    }
    try {
        $payload = @{ content = $Message } | ConvertTo-Json -Compress
        $null = Invoke-WebRequest -Uri $notifyWebhook -Method Post `
            -ContentType 'application/json; charset=utf-8' `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($payload)) `
            -Proxy $notifyProxy -TimeoutSec 30 -ErrorAction Stop
        # First line only, so a multi-line digest does not spam the log with its own body.
        $firstLine = ($Message -split "`n", 2)[0]
        Write-Log "notification sent: $firstLine"
        return 'sent'
    }
    catch {
        # 'failed' — webhook configured but the send failed (network, proxy, Discord outage). A
        # retry on the next tick is the right response, so the digest gate does NOT advance its
        # clock on this. Never fail the sweep because the notifier failed — the conductor's work
        # already happened. Same redaction rule as before: scrub the webhook out of any exception
        # text before it touches the log or a record. Quarantine records / digest lines / this log
        # line all go through this branch, so no path that touches user data can leak the bearer
        # secret.
        $reason = "$($_.Exception.Message)".Replace($notifyWebhook, '<webhook-redacted>')
        Write-Log "notification FAILED (non-fatal): $reason"
        return 'failed'
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
    # $null = drops Send-Notification's status string so it does not leak into the pipeline of
    # whatever call site invokes Send-NotificationIfChanged. The change record on the state map is
    # intentional either way — a failed send does not undo the dedup, or a webhook outage would
    # repeat every 5 minutes forever, retraining the channel into noise. A 'skipped' status (no
    # webhook) is treated the same: mark the signature so we do not spam the log with skip
    # messages on every re-attempt. (Endorsed by Tier B naysayer on round 2 of #138.)
    $null = Send-Notification -Message $Message
    $State[$Key] = $Signature
}

# --- decision-request composer (T-decision-request-composer S2) ---------------------------------
# Turns the current bare "the loop is waiting on Takahito" ping into a self-contained question by
# invoking `mindwire-compose-decision` on every NEW human-terminal stop, then caching the result in
# pending-decisions.json so a re-notify or a dashboard read (S6' / S5') never re-fires the composer.
#
# Two invariants this block is on the hook for:
#   I-2 — a broken composer NEVER sinks the notification. Every failure mode below (missing repo,
#         CLI usage error, timeout, non-zero exit, unparseable stdout) falls through to a $null
#         envelope, and the caller's raw ping fires exactly as it did before this block existed.
#         Silence-on-failure is the failure mode the whole thread was opened to end (msg-1370 §1).
#   I-3 — the composer is invoked ONCE per (project/thread_id, signature) stop. The dedup is done
#         BEFORE the CLI is invoked, by consulting pending-decisions.json: same signature => cached
#         envelope, no CLI call. That is what makes the composer affordable at a 5-minute cadence
#         (measured 08-20: the same signature straddled several ticks; naive dispatch would burn
#         one inference per tick).
#
# The pending-decisions.json shape is defined by DecisionRequestEnvelope.to_json (see
# src/spirrow_mindwire/decision_request/value_objects.py) — the wrapper never writes fields the
# Python side does not know about, so a reader can trust the shape end-to-end. The wrapper wraps
# each envelope in a row {signature; envelope; cached_at} so a future addition (renotify_count for
# S6', last_notified_at) can grow on the row without touching the envelope itself.

# The Discord notification budget. 2000 is the Discord single-message limit; the truncation ladder
# below reserves a small margin for the tail marker so the ladder cannot itself overflow.
$DecisionMessageDiscordBudget = 1950

# Bounded tail for the composer input (I-4). S2 does not fetch the tail bodies — the stub does not
# need them and S3 lands the tail-fetching path with the real composer. Keeping the bound here (as
# opposed to inlined in Get-DecisionEnvelope) means the config lever is in one place when S3 wires
# it in.
$DecisionComposerTailLimit = 5

# How long the wrapper waits for the CLI. 30s is generous for the stub (measured <1s) and gives S3's
# LLM-backed composer a workable ceiling. On timeout the wrapper KILLS the process and returns $null
# — I-2 says a stuck composer must never delay the raw ping.
$DecisionComposerTimeoutSeconds = 30

# The default composer backend when the env var is unset. S1/S2 ships 'stub'; S3 will ship
# 'claude-code' and flip the default from a config change, not a code edit.
$DecisionComposerBackend = if ($env:MINDWIRE_DECISION_COMPOSER_BACKEND) {
    $env:MINDWIRE_DECISION_COMPOSER_BACKEND
} else { 'stub' }

# The persona label the composer records into the envelope (I-5: MUST NOT be one of the design
# roster names Bohr / Heisenberg / Einstein). The Python side has its own default; this only makes
# the identity a config knob on the deploy side.
$DecisionComposerIdentity = if ($env:MINDWIRE_DECISION_COMPOSER_IDENTITY) {
    $env:MINDWIRE_DECISION_COMPOSER_IDENTITY
} else { 'Composer' }

# Dashboard base URL for D-29 (the link always survives the truncation ladder). Read from env with
# the Tailscale-visible default from Takahito's §11.1 measurement — an operator relocating the
# dashboard sets MINDWIRE_DECISION_DASHBOARD_URL and every link updates without touching the format
# code. Path composition (`/dashboard/decisions/<project>/<thread>`) is the single call in
# Format-DecisionMessage — a URL-shape change is one edit, not a search across the wrapper.
$DecisionDashboardBaseUrl = if ($env:MINDWIRE_DECISION_DASHBOARD_URL) {
    $env:MINDWIRE_DECISION_DASHBOARD_URL.TrimEnd('/')
} else { 'https://sg-ai-server-01.taile861db.ts.net:8443' }

# Invoke the CLI with the input JSON on stdin, return @{ ok; envelope; error }. This is the seam
# tests override: replacing the function with a stub lets the wiring be exercised without spawning
# uv. On success `ok=$true` and `envelope` holds the parsed hash; on any failure `ok=$false` and
# `error` describes what happened (used for logging only — the caller falls back to the raw ping
# either way per I-2).
function Invoke-ComposerCli {
    param(
        [string]$InputJson,
        [string]$Backend = $DecisionComposerBackend,
        [string]$Identity = $DecisionComposerIdentity,
        [int]$TimeoutSeconds = $DecisionComposerTimeoutSeconds
    )

    if (-not (Test-Path -LiteralPath $repoRoot)) {
        return @{ ok = $false; envelope = $null; error = "repo root not found: $repoRoot" }
    }

    # System.Diagnostics.Process for real stdio streams. `& uv run ... < $tmp` in PowerShell buffers
    # stdout across the venv sync and prints occasional progress lines to stderr, which turn the
    # parse below into guesswork. A raw Process gives clean separated streams and a hard kill on
    # timeout — the composer runs unattended, so a stuck process must not hold the sweep back.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'uv'
    $psi.Arguments = "run mindwire-compose-decision --backend $Backend --identity `"$Identity`""
    $psi.WorkingDirectory = $repoRoot
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    # UTF-8 both ways — the input carries Japanese and the CLI output is also UTF-8.
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $proc = $null
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
    }
    catch {
        return @{ ok = $false; envelope = $null; error = "cannot start uv: $($_.Exception.Message)" }
    }
    try {
        # StandardInput needs an explicit UTF-8 write; the default writer would honour the console's
        # code page, which is usually not UTF-8 on Windows.
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        $bytes = $utf8.GetBytes($InputJson)
        $proc.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
        $proc.StandardInput.BaseStream.Flush()
        $proc.StandardInput.Close()

        # Read the streams in tasks so the process cannot deadlock on a full stderr pipe.
        $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
        $stderrTask = $proc.StandardError.ReadToEndAsync()

        if (-not $proc.WaitForExit([int]([TimeSpan]::FromSeconds($TimeoutSeconds).TotalMilliseconds))) {
            try { $proc.Kill() } catch { }
            return @{ ok = $false; envelope = $null; error = "composer CLI timed out after ${TimeoutSeconds}s" }
        }

        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()

        if ($proc.ExitCode -ne 0) {
            $tail = if ($stderr) { ($stderr -split "`r?`n" | Select-Object -Last 5) -join ' | ' } else { '<no stderr>' }
            return @{ ok = $false; envelope = $null;
                error = "composer CLI exit=$($proc.ExitCode) stderr=$tail" }
        }

        # The CLI writes exactly one JSON object on stdout on the ok path. uv may print progress on
        # stderr while syncing the environment — that noise stays out of the parse because we only
        # look at stdout.
        $line = ($stdout -split "`r?`n" | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1)
        if (-not $line) {
            return @{ ok = $false; envelope = $null; error = "composer CLI produced no JSON on stdout" }
        }
        try {
            $envelope = $line | ConvertFrom-Json
            return @{ ok = $true; envelope = $envelope; error = $null }
        }
        catch {
            return @{ ok = $false; envelope = $null; error = "composer CLI output not JSON: $($_.Exception.Message)" }
        }
    }
    finally {
        if ($proc -and -not $proc.HasExited) { try { $proc.Kill() } catch { } }
        if ($proc) { $proc.Dispose() }
    }
}

# Build the JSON payload the CLI expects. Extracted so tests can compare a real payload against a
# known-good literal without spawning the CLI itself.
function New-ComposerInputJson {
    param(
        [string]$Project,
        [string]$ThreadId,
        [string]$LastMsgId,
        [string]$StopReason,
        [int]$Rounds,
        [string]$ThreadTitle = '',
        [int]$TailRequested = 0,
        [int]$TotalMessages = 0,
        [array]$Tail = @()
    )
    $payload = @{
        schema_version = 1
        project        = $Project
        thread_id      = $ThreadId
        last_msg_id    = $LastMsgId
        stop_reason    = $StopReason
        rounds         = $Rounds
        thread_title   = $ThreadTitle
        tail_requested = $TailRequested
        total_messages = $TotalMessages
        tail           = @($Tail)
    }
    return ($payload | ConvertTo-Json -Depth 6 -Compress)
}

# The pending-decisions cache. Same JSON shape as heads.json / notified.json, keyed by the sweep's
# "project/thread_id" key. Rows carry {signature; envelope; cached_at}. On a corrupt file
# Get-JsonState logs and returns an empty map — the composer will fire fresh, one signature at most
# once per (project/thread_id, signature), so a lost cache costs at most one extra CLI call per
# parked thread and self-heals on the next tick.
function Get-CachedDecision {
    param([hashtable]$State, [string]$Key, [string]$Signature)

    if (-not $State.ContainsKey($Key)) { return $null }
    $row = $State[$Key]
    if ($null -eq $row) { return $null }
    # Duck-type on either shape (freshly written hashtable vs. JSON-loaded PSCustomObject).
    if ($row -is [hashtable]) {
        $rowSig = $row['signature']
        $rowEnv = $row['envelope']
    }
    else {
        $rowSig = $row.signature
        $rowEnv = $row.envelope
    }
    if ($rowSig -ne $Signature) { return $null }
    return $rowEnv
}

# Compose (or return the cached envelope for) one stop. Returns the parsed envelope hash on
# success, or $null on any failure — including a CLI usage error — so the caller falls back to the
# raw ping. Two side effects: writes a fresh row into $State on a successful compose (and only
# then, so a broken composer never pollutes the cache), and logs one line per outcome so A-3 is
# observable in the log.
function Get-DecisionEnvelope {
    param(
        [hashtable]$State,
        [string]$Key,
        [string]$Project,
        [string]$ThreadId,
        [string]$Signature,
        [string]$LastMsgId,
        [string]$StopReason,
        [int]$Rounds,
        [string]$ThreadTitle = ''
    )

    # I-3 dedup: same signature -> reuse the cache. This is the ONLY branch that must not fire the
    # CLI; every other branch (missing row / different signature) proceeds to compose. Logged so the
    # dedup is visible in the sweep log (A-3 verification).
    $cached = Get-CachedDecision -State $State -Key $Key -Signature $Signature
    if ($null -ne $cached) {
        Write-Log "composer reuse: $Key signature=$Signature (cache hit — no CLI call)"
        return $cached
    }

    $inputJson = New-ComposerInputJson `
        -Project $Project -ThreadId $ThreadId -LastMsgId $LastMsgId `
        -StopReason $StopReason -Rounds $Rounds -ThreadTitle $ThreadTitle `
        -TailRequested $DecisionComposerTailLimit -TotalMessages 0 -Tail @()

    $result = Invoke-ComposerCli -InputJson $inputJson `
        -Backend $DecisionComposerBackend -Identity $DecisionComposerIdentity `
        -TimeoutSeconds $DecisionComposerTimeoutSeconds
    if (-not $result.ok) {
        # I-2: never let a composer failure delay or block the raw ping. Log the failure so it is
        # visible in the sweep log, do NOT cache the failure (a future S6' renotify would keep
        # replaying the failed envelope forever), and return $null. The caller falls back to the
        # bare "you have a decision to make" body.
        Write-Log "composer failed: $Key signature=$Signature — $($result.error) — raw ping will fire"
        return $null
    }

    # Cache the fresh envelope. Log with the composer_status so A-3 (dedup) and A-2 (composer
    # broken but envelope reported non-ok) are distinguishable in the log.
    $envelope = $result.envelope
    $status = if ($envelope.PSObject.Properties.Name -contains 'composer_status') { $envelope.composer_status } else { '<unknown>' }
    Write-Log "composer fire: $Key signature=$Signature status=$status"
    $State[$Key] = @{
        signature = $Signature
        envelope  = $envelope
        cached_at = (Get-Date).ToUniversalTime().ToString('o')
    }
    return $envelope
}

# Compose the Discord message body from an envelope + the raw "someone is waiting" context. The
# ladder (D-9 msg-1373 / D-29 msg-1384) reserves a background block (header + dashboard link) that
# is ALWAYS present — a truncated message that lost its link would tell an operator to go look at
# nothing in particular, which is worse than the pre-composer ping. Everything else falls off in
# reverse priority order: unknowns first, then recommendation reason, then option gains/losses,
# then option labels, then finally the question — the last thing to drop, followed by a "詳細は
# chatroom" marker. In the extreme failure case the caller falls back to the raw ping entirely.
function Format-DecisionMessage {
    param(
        [string]$Project,
        [string]$ThreadId,
        [string]$StopReason,
        [int]$Rounds,
        [string]$LastMsgId,
        [string]$RawFallback,
        $Envelope = $null,
        [int]$Budget = $DecisionMessageDiscordBudget
    )

    # Background: 2 lines the ladder never touches (D-29). A truncated form without them collapses
    # to the raw ping in the caller — we do NOT render a headerless body.
    $encodedProject = [uri]::EscapeDataString($Project)
    $encodedThread = [uri]::EscapeDataString($ThreadId)
    $link = "$DecisionDashboardBaseUrl/dashboard/decisions/$encodedProject/$encodedThread"
    $header = "MindWire: **$ThreadId** ($Project) — 判断待ち (reason=$StopReason, rounds=$Rounds, $LastMsgId)"
    $background = "$header`n$link"

    if (-not $Envelope) { return $null }   # caller falls back to $RawFallback
    $status = if ($Envelope.PSObject.Properties.Name -contains 'composer_status') { $Envelope.composer_status } else { $null }
    if ($status -ne 'ok') { return $null } # non-ok envelope carries no question — fall back to raw ping

    $output = if ($Envelope.PSObject.Properties.Name -contains 'output') { $Envelope.output } else { $null }
    if (-not $output) { return $null }

    $question = if ($output.PSObject.Properties.Name -contains 'question') { $output.question } else { '' }
    if (-not $question) { return $null }

    # Build layered slices. Each slice is a string; we try to append them in priority order and
    # stop when the next one would exceed the budget. The tail marker is only added if at least one
    # slice was dropped.
    $slices = New-Object System.Collections.Generic.List[string]
    $slices.Add("`n$question")

    $options = @()
    if ($output.PSObject.Properties.Name -contains 'options') { $options = @($output.options) }

    if ($options.Count -gt 0) {
        # Option labels only — the cheap slice.
        $labelLines = @('選択肢:')
        foreach ($o in $options) { $labelLines += "  $($o.id): $($o.label)" }
        $slices.Add("`n" + ($labelLines -join "`n"))

        # Gains / losses — richer slice, dropped one below labels.
        $gainLossLines = @()
        foreach ($o in $options) {
            if ($o.gain) { $gainLossLines += "  $($o.id) 得: $($o.gain)" }
            if ($o.loss) { $gainLossLines += "  $($o.id) 失: $($o.loss)" }
        }
        if ($gainLossLines.Count -gt 0) {
            $slices.Add("`n" + ($gainLossLines -join "`n"))
        }
    }

    $recommendation = if ($output.PSObject.Properties.Name -contains 'recommendation') { $output.recommendation } else { $null }
    $recReason = if ($output.PSObject.Properties.Name -contains 'recommendation_reason') { $output.recommendation_reason } else { $null }
    if ($recommendation -and $recReason) {
        $slices.Add("`n推奨: $recommendation — $recReason")
    }

    $unknowns = @()
    if ($output.PSObject.Properties.Name -contains 'unknowns') { $unknowns = @($output.unknowns) }
    if ($unknowns.Count -gt 0) {
        $unknownLines = @('未確認:')
        foreach ($u in $unknowns) { $unknownLines += "  - $u" }
        $slices.Add("`n" + ($unknownLines -join "`n"))
    }

    # Ladder: keep the background, then try to append slices in the DROP order (recommendation
    # reason / unknowns / gains-losses / labels / question) from LAST to FIRST — we walk the
    # priority list forward from the question and stop appending as soon as one does not fit. That
    # way "question" is the first slice we try and "unknowns" is the last; if unknowns don't fit
    # we drop unknowns, if gains/losses don't fit we drop both, and so on.
    $body = $background
    $tailMarker = "`n… 詳細は chatroom を参照してください。"
    $tailMarkerLen = $tailMarker.Length
    $droppedAny = $false
    foreach ($slice in $slices) {
        # Reserve space for the tail marker in case a LATER slice is dropped. If nothing gets
        # dropped we strip the reservation.
        $projected = $body + $slice
        if ($projected.Length + $tailMarkerLen -le $Budget) {
            $body = $projected
        }
        else {
            $droppedAny = $true
            break
        }
    }
    if ($droppedAny) {
        # If even the header+link+question doesn't fit... only slot the tail marker and return the
        # background; the caller can decide whether to fall back to the raw ping when we return
        # something too small. In practice a 1950-char budget swallows the header + link + a normal
        # question line, so this is a defensive branch, not a common one.
        $body = $body + $tailMarker
    }
    return $body
}

# T-decision-request-composer S4: which sweep candidates are currently parked on a human decision?
#
# D-23 puts the SOT of "is thread X parked?" on POLLING — not on pending-decisions.json, which is
# only the cache of the composed question. This function is the poll: it iterates the sweep
# candidates and returns those whose most-recent recorded stop reason is human-terminal AND whose
# recorded head still matches the current probe head. Pending-decisions.json is not consulted here,
# so deleting it does not blank the digest section (A-14 — the section restores from this poll on
# the next tick).
#
# WHY notified.json is the source. Every human-terminal branch of the candidate loop above calls
# Send-NotificationIfChanged with a signature "<reason>:<head>". That signature IS the "the sweep
# last saw this thread parked here" record — it is written exactly when parking is observed, and
# it holds the reason too, so we can filter to human-terminal without re-running the conductor.
# The head cross-check against heads.json protects against a stale signature: if the thread has
# moved past the parked head, we DON'T know it is still parked (a naysayer might have posted, the
# next handoff might not be human), so we deliberately EXCLUDE it. False-negative is better than
# a lie — a thread that has moved but is still parked shows up on the next launch.
#
# Failure modes are deliberately narrow: an unrecognised signature is skipped (never crashes the
# digest). A missing head probe entry is treated as unknown-and-therefore-not-included.
#
# D-24' unresolved item 2: the magickit-side ops dashboard has its own block-axis judgment. When
# the S5' work integrates them, the SOT should live in one place — either magickit exposes the
# judgment via an API mindwire consumes, or the judgment code is shared across the repo boundary.
# Until that lands, this mindwire-side poll is the digest's own source, and any per-tick
# disagreement with the dashboard is a known gap to close, NOT a fresh feature.
#
# Returns an array of PSCustomObjects: { key; project; thread_id; reason; head; parked_since }.
# parked_since is the notify-state signature's write time when readable, else $null.
function Get-HumanParkedCandidates {
    param(
        [array]$Candidates,
        [hashtable]$NotifyState,
        [hashtable]$HeadsByProject,
        [hashtable]$NeedsHuman
    )

    $out = @()
    foreach ($cand in $Candidates) {
        if (-not $NotifyState.ContainsKey($cand.key)) { continue }
        $sig = "$($NotifyState[$cand.key])"
        if (-not $sig) { continue }
        # signatures are "<reason>:<head>" for candidate keys; other __keys use different shapes
        # (e.g. __quarantine__/..., __all_idle__). Candidate keys are never __-prefixed, so this
        # branch is safe: the ':' split just gives us one part when no colon exists, which we
        # then skip.
        $parts = $sig -split ':', 2
        if ($parts.Count -ne 2) { continue }
        $reason = $parts[0]
        $sigHead = $parts[1]
        if (-not $NeedsHuman.ContainsKey($reason)) { continue }

        # Head cross-check: only report parked when the current probe head matches the signature's
        # head. When we cannot read the probe, treat as UNKNOWN-and-parked (fail closed on the
        # "still parked" side, so an outage does not silently blank the digest count). This is the
        # opposite direction from the head-skip cache (which fails open into a launch) — different
        # question, different safe answer.
        $probeHead = $null
        if ($HeadsByProject.ContainsKey($cand.project) -and $null -ne $HeadsByProject[$cand.project]) {
            $h = $HeadsByProject[$cand.project]
            if ($h.ContainsKey($cand.thread_id)) { $probeHead = $h[$cand.thread_id] }
        }
        if ($probeHead -and $probeHead -ne $sigHead) {
            # Head has moved past what we notified on — cannot claim it is still parked.
            continue
        }

        $out += [PSCustomObject]@{
            key          = $cand.key
            project      = $cand.project
            thread_id    = $cand.thread_id
            reason       = $reason
            head         = $sigHead
            parked_since = $null   # notify-state does not currently record write time; S6' will add it
        }
    }
    return $out
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

# --- loop control probe --------------------------------------------------------------------------
# Returns the project's desired control state ("run" / "supervised" / "hold"), or $null when it
# could not be read. $null means UNKNOWN and the caller launches anyway.
#
# Failing OPEN here is deliberate and is not a hole: the conductor reads the same state itself every
# round and fails CLOSED on it, so an unreadable state stops the loop one round in regardless. What
# this probe buys is not safety but cost — a held project would otherwise pay for a process start, a
# venv resolve and an MCP round trip on every tick just to be stopped. Failing closed here instead
# would park every project the moment magickit hiccups, which is the silent-stop failure this
# wrapper exists to prevent.
function Invoke-ControlProbe {
    param([string]$Project)

    $probe = Join-Path $repoRoot "scripts\loop_control.py"
    if (-not (Test-Path -LiteralPath $probe)) {
        Write-Log "control probe not found at $probe — failing open (the conductor still enforces)"
        return $null
    }
    try {
        Push-Location $repoRoot
        try { $raw = & uv run python $probe --project $Project 2>&1; $code = $LASTEXITCODE }
        finally { Pop-Location }

        if ($code -ne 0) {
            Write-Log "control probe exited $code — failing open. Output: $(($raw | ForEach-Object { "$_" }) -join ' / ')"
            return $null
        }
        $json = $raw | ForEach-Object { "$_" } | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
        if (-not $json) { Write-Log "control probe produced no JSON — failing open"; return $null }
        return ($json | ConvertFrom-Json)
    }
    catch {
        Write-Log "control probe failed ($($_.Exception.Message)) — failing open"
        return $null
    }
}

# --- deploy probe ---------------------------------------------------------------------------------
# Fast-forwards this checkout to origin/main before the tick decides anything. Returns the parsed
# verdict from deploy/sync-repo.ps1, or $null when it could not be run at all.
#
# Merging is not deploying: the scheduled task runs `uv run mindwire-loop` from this checkout and
# nothing pulled it, so a merged fix could sit undeployed indefinitely with GitHub showing it merged
# and the task history showing exit 0. Deploying merged `main` needs no separate approval — `main`
# only advances through a human's Tier-C merge — so what this adds is delivery, not authority.
function Invoke-RepoSync {
    $probe = Join-Path $PSScriptRoot "sync-repo.ps1"
    if (-not (Test-Path -LiteralPath $probe)) {
        Write-Log "repo sync script not found at $probe — running whatever code is checked out"
        return $null
    }
    try {
        $raw = & pwsh -NoProfile -File $probe 2>&1
        $code = $LASTEXITCODE
        if ($code -ne 0) {
            Write-Log "repo sync exited $code — running whatever code is checked out. Output: $(($raw | ForEach-Object { "$_" }) -join ' / ')"
            return $null
        }
        $json = $raw | ForEach-Object { "$_" } | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
        if (-not $json) { Write-Log "repo sync produced no JSON — running whatever code is checked out"; return $null }
        return ($json | ConvertFrom-Json)
    }
    catch {
        Write-Log "repo sync failed ($($_.Exception.Message)) — running whatever code is checked out"
        return $null
    }
}

# --- run ---------------------------------------------------------------------------------------
$exitCode = 0
try {
    Write-Log "=== scheduled conductor run starting (host $env:COMPUTERNAME, user $env:USERNAME) ==="

    # Loaded before the deploy step, which already needs it to dedupe its own alerts.
    $notifyState = Get-JsonState -Path $notifyStatePath

    # Composer cache (T-decision-request-composer S2). Loaded up front for the same reason as
    # $notifyState — the human-terminal branch inside the candidate loop reads and writes it, and a
    # corrupt file collapses to an empty map (see Get-JsonState). An empty map costs at most one
    # fresh composer call per parked thread this tick and self-heals; a missing pending-decisions
    # file is not a fatal condition.
    $pendingDecisionsState = Get-JsonState -Path $pendingDecisionsPath

    # Deploy first, so a tick either updates the code or uses it — never both. When the pull moves
    # HEAD this tick STOPS: the wrapper was parsed from the old file at startup while
    # run-conductor.ps1 would be read from disk after the pull, and a sweep spanning two versions is
    # not a thing worth debugging later. The cost is one 5-minute cycle of latency after a merge.
    $sync = Invoke-RepoSync
    if ($null -ne $sync) {
        if ($sync.status -eq 'updated') {
            Confirm-LogWorthKeeping
            $depsNote = if ($sync.synced_deps) { ", deps synced" } else { "" }
            Write-Log "deployed $($sync.from) -> $($sync.to) ($($sync.commits) commit(s)$depsNote)"
            Send-NotificationIfChanged -State $notifyState -Key "__deploy__" `
                -Signature "$($sync.from)->$($sync.to)" `
                -Message ("MindWire: main を取り込みました（$($sync.from) → $($sync.to)、$($sync.commits) commit$depsNote）。" +
                          "この tick は起動せず、次の tick から新しいコードで動きます。")
            Save-JsonState -Path $notifyStatePath -State $notifyState
            Save-JsonState -Path $pendingDecisionsPath -State $pendingDecisionsState
            Write-Log "not launching this tick — the next one runs entirely on the new code"
            return
        }
        elseif ($sync.status -eq 'current') {
            # Buffered, not committed: on an idle tick this collapses away with everything else.
            Write-Log "repo up to date ($($sync.head))"
        }
        else {
            # skipped / blocked / failed. Deliberately NOT committed to the log on its own: the log is
            # not a channel anyone watches, so the notification is the report and the log line is
            # context that flushes with any tick that does something. Suppressed while unchanged, so a
            # week on a feature branch costs one alert, not 2016.
            Write-Log "repo sync $($sync.status): $($sync.reason)"
            Send-NotificationIfChanged -State $notifyState -Key "__deploy_health__" `
                -Signature "$($sync.status):$($sync.reason)" `
                -Message ("MindWire: main の自動取り込みが **$($sync.status)** です — $($sync.reason)。" +
                          "ループは現在チェックアウトされているコードで動き続けます（古い可能性があります）。")
        }
    }

    $candidates = Get-SweepCandidates -Path $sweepConfigPath
    Write-Log "sweep list ($($candidates.Count) candidates from $sweepConfigPath): $(($candidates | ForEach-Object { $_.key }) -join ', ')"

    # One probe per distinct project, not per candidate — the probe returns every thread of a project
    # in a single call, so N candidates in one project still cost one call.
    $headsByProject = @{}
    $controlByProject = @{}
    foreach ($proj in ($candidates | ForEach-Object { $_.project } | Sort-Object -Unique)) {
        # Control first: a project whose hold has already landed needs no head probe at all, so asking
        # in this order keeps a settled HOLD to exactly one MCP read per project per tick. A hold the
        # loop has not acknowledged yet still gets probed and launched — see Test-HoldObserved.
        $c = Invoke-ControlProbe -Project $proj
        $controlByProject[$proj] = $c
        if ($null -ne $c) {
            Write-Log "control probe [$proj]: desired=$($c.desired_state) observed=$($c.observed_state) configured=$($c.configured)"
        }
        if (Test-HoldObserved -Control $c) { continue }
        $h = Invoke-HeadProbe -Project $proj
        $headsByProject[$proj] = $h
        if ($null -ne $h) { Write-Log "head probe [$proj]: $($h.Count) threads reported" }
    }
    $headsState = Get-JsonState -Path $headsStatePath
    $quarantineState = Get-JsonState -Path $quarantineStatePath
    $evaluatedState = Get-JsonState -Path $evaluatedStatePath
    $digestState = Get-JsonState -Path $digestStatePath
    # Snapshot the keys present at sweep start. Used by Merge-StateForWrite at flush time to
    # distinguish "operator removed this during the sweep" (must not resurrect) from "sweep never
    # touched this" (already-agreed value, keep). See Merge-StateForWrite's header for the full
    # contract.
    $quarantineOriginalKeys = @($quarantineState.Keys)
    $headsOriginalKeys = @($headsState.Keys)
    $nowUtc = [DateTime]::UtcNow

    # Age-driven state transitions on quarantine entries. Run BEFORE the candidate loop so the
    # transition notification fires even on ticks where nothing else happens (an escalated thread is
    # by definition one nobody has touched for 24h — we cannot wait for its own launch to notice).
    foreach ($k in @($quarantineState.Keys)) {
        $rec = $quarantineState[$k]
        if (-not $rec.first_failure_at) { continue }
        $firstAt = ConvertTo-UtcInstant $rec.first_failure_at
        $derived = Get-DerivedQuarantineState -FirstFailureAt $firstAt -Now $nowUtc
        if ($rec.state -ne $derived) {
            $prev = $rec.state
            $rec.state = $derived
            $quarantineState[$k] = $rec
            $age = Format-DurationDigest -Span ($nowUtc - $firstAt)
            Confirm-LogWorthKeeping
            Write-Log "quarantine state transition: $k $prev -> $derived (age $age)"
            $msg = switch ($derived) {
                'escalated' { "MindWire: 隔離スレッド **$k** が escalated になりました (経過 $age)。ダイジェスト先頭に別掲されます。" }
                'stale'     { "MindWire: 隔離スレッド **$k** が stale です (経過 $age)。直すか、スレッドを畳むか決めてください。" }
                default     { $null }
            }
            if ($msg) {
                Send-NotificationIfChanged -State $notifyState -Key "__quarantine_transition__/$k" `
                    -Signature "${derived}:$($rec.first_failure_at)" -Message $msg
            }
        }
    }

    # The live sweep list, as a bare key list. Passed to the starvation report (which pivots on it,
    # not on $EvaluatedState.Keys) and used below to prune stale keys and record first-seen times.
    $liveKeys = @($candidates | ForEach-Object { $_.key })

    # Prune any key that is no longer on the sweep list. Left in place, folded threads sit in the
    # state file forever and their timestamps eventually age past the threshold — spamming the daily
    # digest with entries the operator can no longer act on. Pruning is safe: a thread put back on
    # the list will get a fresh first_seen_at on the very same tick.
    foreach ($k in @($evaluatedState.Keys)) {
        if ($liveKeys -notcontains $k) { [void]$evaluatedState.Remove($k) }
    }

    # Record first-seen for every live candidate that has never entered the file. This is the
    # timestamp the starvation clock ticks from when the candidate never actually launches (held on
    # creation, immediately quarantined, or permanently 'not-reached' behind a K-budget hit). Set
    # BEFORE any per-candidate decision so a held/quarantined-on-creation thread still starts its
    # clock.
    foreach ($k in $liveKeys) {
        if (-not $evaluatedState.ContainsKey($k)) {
            $evaluatedState[$k] = @{ first_seen_at = $nowUtc.ToString("o") }
        }
    }

    $inner = Join-Path $PSScriptRoot "run-conductor.ps1"
    $didWork = $false
    $attempt = 0
    $launched = 0
    $skipped = 0
    $held = 0
    $quarantineSkipped = 0     # candidates dropped because already quarantined
    $newlyQuarantined = 0      # non-zero exits this tick
    $notReached = 0            # candidates the sweep never got to (K-cap, worked-and-broke)
    $sweepSignature = @()
    $dispositions = @{}        # key -> "worked" | "no-work" | "head-skipped" | "quarantined-skipped" | "held" | "not-reached"
    $breakReason = $null       # human-readable reason for a mid-sweep break, or $null if it ran to end

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

        # HOLD is per project, so it takes every one of that project's candidates out at once — but
        # only once the loop has acknowledged the hold (Test-HoldObserved). Until then the candidate
        # falls through and launches, which is what lands the acknowledgement.
        #
        # Not counted as "skipped" (that word means "head unchanged, nothing to do"); a held project
        # has work and is being deliberately withheld, and reporting the two the same way would make
        # an operator's HOLD look like an idle loop in the log and the summary.
        $control = $controlByProject[$cand.project]
        if (Test-HoldObserved -Control $control) {
            $held++
            $dispositions[$cand.key] = 'held'
            Write-Log "candidate $attempt/$($candidates.Count): $($cand.key) — project HELD (desired=hold, loop observed hold), not launching"
            continue
        }

        # Quarantine check — before the head-skip cache. A quarantined thread is not launched by
        # this wrapper until a human runs Clear-Quarantine. It IS counted toward starvation (the
        # design's Q4 honesty rule: hiding a suppressed area on the "how long since I actually
        # evaluated?" metric is exactly the silent-degradation this file exists to end).
        if ($quarantineState.ContainsKey($cand.key)) {
            $rec = $quarantineState[$cand.key]
            $stateLabel = if ($rec.state) { $rec.state } else { 'quarantined' }
            $ageStr = ""
            if ($rec.first_failure_at) {
                try {
                    $firstAt = ConvertTo-UtcInstant $rec.first_failure_at
                    $ageStr = " age=$(Format-DurationDigest -Span ($nowUtc - $firstAt))"
                } catch { }
            }
            $quarantineSkipped++
            $dispositions[$cand.key] = 'quarantined-skipped'
            Write-Log "candidate $attempt/$($candidates.Count): $($cand.key) — [$stateLabel]$ageStr, not launching (Clear-Quarantine to release)"
            continue
        }

        $heads = $headsByProject[$cand.project]
        $probeHead = if ($null -ne $heads -and $heads.ContainsKey($thread)) { $heads[$thread] } else { $null }
        $record = Get-HeadRecord -State $headsState -Key $cand.key
        $knownHead = $record.head
        $knownControl = $record.control
        # $null when the control probe could not read the state — unknown, so nothing is skipped.
        $currentControl = if ($null -ne $control) { $control.desired_state } else { $null }

        # The whole point: same head AND same routing means the conductor would resolve the same
        # handoff and reach the same stop, so do not pay for the launch. See Test-CanSkip for why
        # both halves are load-bearing and why every unknown fails open.
        if (Test-CanSkip -ProbeHead $probeHead -KnownHead $knownHead `
                         -CurrentControl $currentControl -KnownControl $knownControl) {
            $skipped++
            $dispositions[$cand.key] = 'head-skipped'
            $sweepSignature += "$($cand.key)=$probeHead"
            # Refresh the starvation clock — a head-skip IS an evaluation. See
            # Update-EvaluatedTimestamp for why. Without this, a legitimately-idle thread over a
            # weekend would flag as starved on Monday and flood the digest with healthy threads.
            Update-EvaluatedTimestamp -State $evaluatedState -Key $cand.key -Now $nowUtc
            Write-Log "candidate $attempt/$($candidates.Count): $($cand.key) — head unchanged ($probeHead, control $currentControl), not launching"
            continue
        }

        Confirm-LogWorthKeeping
        $why = if (-not $probeHead) { "head unknown (probe gap) — failing open" }
               elseif (-not $knownHead) { "no recorded head yet" }
               elseif ($probeHead -ne $knownHead) { "head moved $knownHead -> $probeHead" }
               elseif (-not $currentControl) { "control unknown (probe gap) — failing open" }
               elseif (-not $knownControl) { "head $probeHead recorded before control was tracked — re-running once" }
               else { "control changed $knownControl -> $currentControl at head $probeHead — same head can route differently" }
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

        # An actual evaluation happened — reset the starvation clock for this thread. Any launched
        # verdict counts, even a non-zero exit: it still tells us the thread was tried this tick,
        # which is the metric's meaning ("how long since I actually reached this candidate?").
        # See Update-EvaluatedTimestamp for the full contract (which dispositions refresh, which do
        # not, and why); first_seen_at is preserved by the helper.
        Update-EvaluatedTimestamp -State $evaluatedState -Key $cand.key -Now $nowUtc

        # NON-ZERO EXIT — quarantine, notify, keep going. The old wrapper broke the sweep here
        # (silent), which was the exact failure mode of the 2026-08-11 5h starvation on threads
        # BEHIND the broken candidate. The direct cause of that starvation is fixed elsewhere
        # (#136 / OBL-MERGE-MECHANISM); this branch exists to keep the NEXT unknown breakage from
        # dying in the same silent way — quarantine declares it, and the sweep continues.
        if ($code -ne 0) {
            $dispositions[$cand.key] = 'failed'
            $tail = @()
            if ($output.Count -gt 0) {
                $take = [Math]::Min($SessionLogTailLines, $output.Count)
                $tail = $output[($output.Count - $take)..($output.Count - 1)]
            }
            $nowIso = $nowUtc.ToString("o")
            $rec = New-QuarantineRecord `
                -FirstFailureAt $nowIso -ExitCode $code -StopReason $verdict.reason `
                -FailureHead $probeHead -FailureControl $currentControl `
                -SessionLogPath $logPath -SessionLogTail $tail
            $quarantineState[$cand.key] = $rec
            $newlyQuarantined++
            Write-Log "quarantined $($cand.key): exit=$code reason=$($verdict.reason) — sweep CONTINUES (signal is the notification, not the stop)"

            # Initial-quarantine notification. Fires once per newly-recorded quarantine. Signature
            # is the failure fingerprint so a re-quarantine after Clear-Quarantine (which drops the
            # signature) re-alerts, but a same-tick duplicate is impossible (candidate is skipped
            # once quarantined, above).
            Send-NotificationIfChanged -State $notifyState -Key "__quarantine__/$($cand.key)" `
                -Signature "${nowIso}:${code}:$($verdict.reason):${probeHead}" `
                -Message ("MindWire: **$($cand.key)** を隔離しました (exit=$code, reason=$($verdict.reason))。" +
                          "以後この tick からは skip されます。復帰するには " +
                          "``pwsh deploy/Clear-Quarantine.ps1 -Thread '$($cand.key)' -Reason '...'``。" +
                          "ダイジェストにも別掲されます。")

            # K-budget short-circuit. Two quarantines in one sweep suggest a shared cause; keep
            # spending inferences past the second is the exact "keep bleeding" failure mode this
            # design refuses. The sweep breaks and fires a systemic-cause notification.
            if ($newlyQuarantined -ge $QuarantineFailureBudget) {
                Write-Log "quarantine budget K=$QuarantineFailureBudget hit in one sweep — stopping (systemic cause suspected)"
                # Day-bucketed signature (see Get-SystemicAlertSignature): one alert per UTC day of
                # an ongoing systemic wave, then silence. A tick-level timestamp here spammed every
                # 5 minutes — exactly the "retraining the channel into noise" mode this file avoids
                # elsewhere.
                Send-NotificationIfChanged -State $notifyState -Key "__quarantine_systemic__" `
                    -Signature (Get-SystemicAlertSignature -Now $nowUtc -Count $newlyQuarantined) `
                    -Message ("MindWire: 同一 sweep で K=$QuarantineFailureBudget 件の quarantine が発生しました。" +
                              "systemic な原因の可能性が高いため、この tick を打ち切ります。" +
                              "残候補はスキップ (`not-reached`) 扱いで飢餓計測に載ります。")
                $breakReason = 'k-budget-hit'
                break
            }
            continue
        }
        # Fail-safe: no parseable verdict on a zero-exit means we do not actually know whether work
        # happened. That is a declaration failure — the whole point of the record-on-fail path above
        # is that we CAN write a declaration. Without a declaration, the old rule still holds:
        # break, so a genuine "no signal" tick cannot be laundered into a healthy sweep.
        if ($null -eq $verdict.rounds) {
            $dispositions[$cand.key] = 'undeclared'
            Write-Log "no parseable 'conductor stopped:' line — stopping the sweep (unknown state, not idle)"
            $breakReason = 'undeclared-verdict'
            break
        }

        # Record the state this head was reached under, not just the head. Written even when
        # $currentControl is $null (an unreadable probe): storing the unknown keeps the next tick
        # failing open rather than silently inheriting a state nobody confirmed.
        if ($verdict.last_msg) {
            $headsState[$cand.key] = @{ head = $verdict.last_msg; control = $currentControl }
        }
        $sweepSignature += "$($cand.key)=$($verdict.last_msg)"

        if ($verdict.reason -and $needsHuman.ContainsKey($verdict.reason)) {
            # Signature carries the reason too, so a thread that changes *how* it is stuck re-alerts
            # even when last_msg has not moved.
            $sig = "$($verdict.reason):$($verdict.last_msg)"

            # T-decision-request-composer S2: fold in an enriched question (with options + a
            # dashboard link) when the composer can produce one; fall back to the raw ping on any
            # failure (I-2). Get-DecisionEnvelope enforces I-3 by caching per signature — a same-
            # signature repeat tick reuses the cached envelope and never invokes the CLI.
            $rawFallback = ("MindWire: **$thread** ($($cand.project)) — " +
                            $needsHuman[$verdict.reason] +
                            " (reason=$($verdict.reason), rounds=$($verdict.rounds), $($verdict.last_msg))。" +
                            "chatroom を確認してください。")
            $envelope = Get-DecisionEnvelope -State $pendingDecisionsState `
                -Key $cand.key -Project $cand.project -ThreadId $thread `
                -Signature $sig -LastMsgId $verdict.last_msg `
                -StopReason $verdict.reason -Rounds $verdict.rounds
            $enriched = Format-DecisionMessage -Project $cand.project -ThreadId $thread `
                -StopReason $verdict.reason -Rounds $verdict.rounds `
                -LastMsgId $verdict.last_msg -RawFallback $rawFallback -Envelope $envelope
            $message = if ($enriched) { $enriched } else { $rawFallback }

            Send-NotificationIfChanged -State $notifyState -Key $cand.key `
                -Signature $sig -Message $message
        }

        if ($verdict.rounds -gt 0) {
            $dispositions[$cand.key] = 'worked'
            Write-Log "thread did work (rounds=$($verdict.rounds), reason=$($verdict.reason)) — sweep done"
            $didWork = $true
            break
        }
        $dispositions[$cand.key] = 'no-work'
        Write-Log "no work (rounds=0, reason=$($verdict.reason)) — advancing to the next candidate"
    }

    # Everything the sweep never touched is 'not-reached'. Deliberately NOT rolled into
    # 'head-skipped': head-skipped means "we asked and the answer was no change"; not-reached means
    # "we did not ask." Different failure modes, different fixes (candidate order vs. probe gap vs.
    # sweep break). Not-reached does NOT reset the evaluation timestamp — that is how a permanently
    # backed-up sweep shows up as starvation instead of "healthy and idle."
    foreach ($cand in $candidates) {
        if (-not $dispositions.ContainsKey($cand.key)) {
            $dispositions[$cand.key] = 'not-reached'
            $notReached++
        }
    }
    Write-Log ("dispositions: " + (($dispositions.Keys | Sort-Object | ForEach-Object { "$($_)=$($dispositions[$_])" }) -join ', '))

    # Merge-on-write for the two files an operator may edit concurrently (heads.json and
    # quarantine.json). The merge re-reads disk right before writing, so a mid-sweep
    # Clear-Quarantine survives the sweep's end-of-tick flush. See Merge-StateForWrite's header.
    # evaluated.json is written directly — it has no external writer, and the sweep prunes ex-live
    # keys on it every tick (merge-on-write would resurrect those pruned keys from disk).
    $mergedHeads = Merge-StateForWrite -Memory $headsState `
        -OriginalKeys $headsOriginalKeys -DiskPath $headsStatePath
    Save-JsonState -Path $headsStatePath -State $mergedHeads
    $mergedQuarantine = Merge-StateForWrite -Memory $quarantineState `
        -OriginalKeys $quarantineOriginalKeys -DiskPath $quarantineStatePath
    Save-JsonState -Path $quarantineStatePath -State $mergedQuarantine
    Save-JsonState -Path $evaluatedStatePath -State $evaluatedState

    # Starvation report. Included in the log every tick that logs anything (an idle tick still
    # collapses to one line), so the metric is visible without waiting for the digest. The digest is
    # the "someone WILL see this" channel; the log line is "someone COULD see this in context."
    # Pivoted on $liveKeys — see New-DailyDigest's header for the two failure modes this closes.
    $starved = Get-StarvedKeys -EvaluatedState $evaluatedState -Now $nowUtc -LiveKeys $liveKeys
    if ($starved.Count -gt 0) {
        Confirm-LogWorthKeeping
        Write-Log "starved threads (>=$(Format-DurationDigest -Span $StarvedThreshold) since last evaluation): $($starved -join ', ')"
    }

    # Human-parked poll (T-decision-request-composer S4 / D-23). Computed here so BOTH the log and
    # the digest see the same list. The scope is the sweep candidates only (msg-1379 §10.4: "全
    # project 全スレッドを舐めない") — the count carries an implicit upper bound of $candidates.Count.
    $humanParked = @(Get-HumanParkedCandidates -Candidates $candidates `
        -NotifyState $notifyState -HeadsByProject $headsByProject -NeedsHuman $needsHuman)
    if ($humanParked.Count -gt 0) {
        Confirm-LogWorthKeeping
        $summary = ($humanParked | ForEach-Object { "$($_.key)/$($_.reason)" }) -join ', '
        Write-Log "human-parked ($($humanParked.Count) of $($candidates.Count) candidates polled): $summary"
    }
    else {
        # A single log line even at zero, so the poll's own liveness is visible in the log — the
        # digest section is the same idea (0 件 is still an entry, not silence).
        Write-Log "human-parked (0 of $($candidates.Count) candidates polled)"
    }

    # Daily digest. Sent even when both quarantine and starvation lists are empty (spec/msg-814 §5).
    # A silent day IS the point: "no alert" then still means "the channel is alive," which is what
    # the 5h failure specifically lacked. Attempted at most once per $DailyDigestInterval.
    #
    # The clock advances on 'sent' AND 'skipped' — both are terminal outcomes: 'sent' means the
    # notification landed, 'skipped' means the operator has no webhook configured, and neither
    # merits a 5-minute retry. Only 'failed' (webhook configured but the POST failed — network,
    # proxy, Discord outage) holds the clock back for retry.
    #
    # Also gated on $notifyWebhook up front: without a webhook, computing the digest, calling
    # Confirm-LogWorthKeeping (which promotes the buffered log to disk), and writing the "sending
    # daily digest" line every 5 minutes for the life of the daemon is nothing but log spam. It
    # violates the whole point of Write-QuietSummary. So when there is no webhook, the whole block
    # is silent — the clock still advances so we do not loop, but nothing is computed, logged, or
    # persisted for a channel nobody is listening to. (Tier B naysayer, PR #138 round 5.)
    $lastDigestAt = $null
    if ($digestState.ContainsKey('last_sent_at') -and $digestState['last_sent_at']) {
        try { $lastDigestAt = (ConvertTo-UtcInstant $digestState['last_sent_at']) } catch { }
    }
    $digestDue = ($null -eq $lastDigestAt) -or (($nowUtc - $lastDigestAt) -ge $DailyDigestInterval)
    if ($digestDue) {
        if (-not $notifyWebhook) {
            # No point building or logging a digest for a channel that does not exist. Advance the
            # clock silently so we do not re-enter this branch until the next full interval.
            $digestState['last_sent_at'] = $nowUtc.ToString("o")
            Save-JsonState -Path $digestStatePath -State $digestState
        }
        else {
            $digest = New-DailyDigest -QuarantineState $quarantineState `
                -EvaluatedState $evaluatedState `
                -HeadsByProject $headsByProject -ControlByProject $controlByProject -Now $nowUtc `
                -LiveKeys $liveKeys `
                -HumanParked $humanParked -PendingDecisionsState $pendingDecisionsState
            Confirm-LogWorthKeeping
            Write-Log "sending daily digest ($($quarantineState.Count) quarantined, $($starved.Count) starved)"
            $result = Send-Notification -Message $digest
            # See Test-DigestClockAdvances for the terminal-outcomes contract. 'skipped' here
            # would be from a webhook that was set at $digestDue evaluation but has since been
            # unset — very narrow race, but the same reasoning still holds.
            if (Test-DigestClockAdvances -Result $result) {
                $digestState['last_sent_at'] = $nowUtc.ToString("o")
                Save-JsonState -Path $digestStatePath -State $digestState
            }
        }
    }

    # Summary line categories, ranked so the most-informative wording wins. Order matters:
    # quarantine and K-hit are louder than a plain idle sweep.
    if ($newlyQuarantined -gt 0) {
        Confirm-LogWorthKeeping
        Write-Log "sweep summary: $newlyQuarantined newly quarantined, $quarantineSkipped skipped-as-quarantined, $held held, $skipped head-skipped, $launched launched, $notReached not-reached"
    }
    elseif ($held -gt 0 -and ($held + $skipped + $quarantineSkipped) -eq $candidates.Count) {
        # Nothing ran, and at least part of the reason was a deliberate HOLD. Said separately from
        # the line below so the log never describes a withheld loop as an idle one — those need
        # opposite responses from a reader, and only one of them is a problem.
        Write-QuietSummary "nothing to run ($held/$($candidates.Count) held by loop control, $skipped heads unchanged, $quarantineSkipped quarantined)"
    }
    elseif ($skipped -eq $candidates.Count) {
        # The steady state at a 5-minute cadence. One line, no notification: by definition nothing
        # changed, so there is nothing new to tell anyone.
        Write-QuietSummary "no thread moved ($skipped/$($candidates.Count) heads unchanged) — nothing to do"
    }
    elseif (($skipped + $quarantineSkipped) -eq $candidates.Count -and $quarantineSkipped -gt 0) {
        # Every live candidate was quarantined; the digest carries the escalation state.
        Write-QuietSummary "nothing to run ($quarantineSkipped/$($candidates.Count) quarantined, $skipped heads unchanged) — see digest"
    }
    elseif (-not $didWork -and $exitCode -eq 0 -and $attempt -eq $candidates.Count -and $launched -gt 0 -and $held -eq 0 -and $quarantineSkipped -eq 0) {
        # Every candidate was settled or blocked on the human: the loop has run out of work and only
        # Takahito can give it more. Keyed on the whole sweep's head set, so a list that is still idle
        # for the same reason stays quiet and only a genuine change re-alerts.
        Write-Log "ALL CANDIDATES IDLE — no thread in the priority list had work; the loop needs a human"
        Send-NotificationIfChanged -State $notifyState -Key "__all_idle__" `
            -Signature ($sweepSignature -join '|') `
            -Message "MindWire: sweep 対象の $($candidates.Count) スレッド全てに仕事がありません。ループは停止したままです — 次に取り組む対象の指定が要ります。"
    }

    Save-JsonState -Path $notifyStatePath -State $notifyState
    Save-JsonState -Path $pendingDecisionsPath -State $pendingDecisionsState
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
