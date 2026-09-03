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

# Session-log tail length kept with a quarantine record. Kept here to keep the whole tuning
# surface in one section.
# (Historical note: $DailyDigestInterval = 24h used to live here alongside SessionLogTailLines.
# T-digest-exceeds-discord-limit-and-is-dropped D-6/§4 replaced interval-gating with period-gating
# (see $DailyDigestDeliveryTime below), retiring the constant. PR-gate round 3 caught it as dead
# code and it was removed here.)
$SessionLogTailLines  = 50

# T-digest-exceeds-discord-limit-and-is-dropped D-1: the digest renderer owns a fixed payload budget,
# so the delivered message length is DECOUPLED from the queue length. The invariant this constant
# defends is "the renderer emits ≤ this many characters, and any excess collapses to `+N 件` rather
# than to a 400". (Bohr msg-2099 §0 / D-1 — msg-2013 sent 63 sequential 400s because the ONLY
# defense was "hope the queue stays short.")
#
# The number must be ≤ the limit of the transport the digest ACTUALLY ships on. Send-Notification
# posts `content` (see the `@{ content = $Message }` payload), whose hard limit is 2,000 — the same
# limit $DecisionMessageDiscordBudget already names, and 1950 is the margin that constant already
# chose. Keep the two in step.
#
# Regression (Einstein msg-2396 E-2, Bohr msg-2401 §3, both measured on live state): this was 3,500,
# chosen on the assumption the digest would move to `embed.description` (4,096). The transport move
# never happened, so every full digest from #203 (b8b6a64) onward rendered 3,2xx chars and was
# rejected 400 — measured 3257 on 2026-09-01 and 3272 on 2026-09-02, with only the 78-char degraded
# fallback reaching the operator. The comment that stood here asserted "3,500 is well under the
# `content` 2,000 hard limit", which is false as arithmetic; it is deleted rather than renumbered.
#
# Not fixed by raising the budget to a bigger transport: "the queue does not fit, so enlarge the
# budget" is the manufacturing process for this whole defect class (Bohr msg-2401 §3-2). The budget
# is what the transport accepts, never what we wish to list; not fitting is the truncation ladder's
# job, and it has one.
$DigestBudget = 1950

# T-digest-exceeds-discord-limit-and-is-dropped D-6 / §4 (msg-2106): the daily digest is period-gated,
# not interval-gated. Runs once per LOCAL day at or after this wall-clock time — that is the promise
# the phone-side reader has ("my 09:00 digest"). Time drift (24h clock walking off) is impossible
# because the gate is "period ≠ last_sent_period AND local ≥ this time", not "elapsed ≥ 24h".
$DailyDigestDeliveryTime = [TimeSpan]::FromHours(9)

# --- paths -------------------------------------------------------------------------------------
# mindwire-loop reads <data_dir>/config/mindwire.toml; honour the same env var run-conductor.ps1 does.
$dataDir = if ($env:MINDWIRE_PATHS__DATA_DIR) { $env:MINDWIRE_PATHS__DATA_DIR } else { Join-Path $HOME "spirrow-mindwire-data" }
$configPath = Join-Path $dataDir "config\mindwire.toml"
$logDir = Join-Path $dataDir "logs"
$logPath = Join-Path $logDir ("conductor-" + (Get-Date -Format "yyyy-MM-dd") + ".log")
$notifyStatePath = Join-Path $dataDir "state\notified.json"
# State file for the head-skip nomination predicate (scripts/head_skip_decide.py, PR #140).
# See T-sweep-intake-and-quarantine-stalls Bohr msg-1430 §W-2 for the wiring contract: the CLI
# owns the file's shape (thread_id-keyed JSON object of Record dicts), the atomic write, and
# the corrupt/missing → empty semantics. The wrapper is a caller, never a co-writer. This
# supersedes the old $headsStatePath (state\heads.json) — that file is deleted, its readers/
# writers/merge-on-write have been removed as one atomic change (Bohr msg-1432 §W-2 update).
$headSkipStatePath = Join-Path $dataDir "state\head_skip.json"
$sweepConfigPath = Join-Path $dataDir "config\sweep.json"
$quarantineStatePath = Join-Path $dataDir "state\quarantine.json"
$quarantineHistoryPath = Join-Path $dataDir "state\quarantine-history.json"
$evaluatedStatePath = Join-Path $dataDir "state\evaluated.json"
$digestStatePath = Join-Path $dataDir "state\digest.json"
# T-digest-exceeds-discord-limit-and-is-dropped D-6 (msg-2106): notify-health carries just enough to
# derive the ⚠ line ("full digest is X periods overdue") from period-typed fields —
# last_full_success_period and first_attempt_period. Two, not one: a success record alone cannot
# express "has never succeeded", which is the state the ⚠ most needs to report (E-4, see
# Get-DigestPeriodsMissed). Deliberately does NOT carry a `consecutive_failures` counter (Einstein
# msg-2102 §1 → Bohr msg-2103): an unstored value cannot be miscleared by a degraded delivery, so the
# state minimisation IS the fix. Remaining fields (last_attempt_at / last_error / last_error_class)
# are DIAGNOSTIC ONLY — the ⚠ predicate consults ONLY period ids (D-6 predicate-discipline).
$notifyHealthPath = Join-Path $dataDir "state\notify-health.json"
# Composer cache (T-decision-request-composer S2). One row per parked thread key,
# keyed by the same "project/thread_id" the notified.json / evaluated.json use so a
# reader can cross-reference by eye. The row carries the last composer envelope
# (question + options) and its signature; the wrapper reuses it when the
# signature has not changed (I-3: ≤1 composer call per reason:last_msg stop).
$pendingDecisionsPath = Join-Path $dataDir "state\pending-decisions.json"
$repoRoot = Split-Path -Parent $PSScriptRoot

# --- library dot-sources -------------------------------------------------------------------------
# Small, PURE helpers that both this runner and its tests need. Kept out of this file because this
# file cannot be dot-sourced by a test — reading it launches the sweep (see the try/catch/finally
# at the bottom). Extracted per Bohr msg-1466 D-3 / Einstein msg-1467 §3-A4: the extracted file is
# the testability seam, not a refactor.
. (Join-Path $PSScriptRoot 'lib/StopReason.ps1')
# Lease.ps1 owns the canonical Get-JsonState (msg-2172 reader collapse). The wrapper's inline
# reader that used to live at line ~172 is gone; dot-sourcing here brings Get-JsonState into the
# wrapper's script scope. Order matters: Write-Log is defined further down and Get-JsonState's
# opportunistic log-through calls Write-Log if resolvable — but since Write-Log is a function
# (not a variable), PowerShell resolves it at CALL time via Get-Command, so the dot-source order
# above the Write-Log definition is safe. The wrapper's LEASE state-machine is still inert (the
# candidate-loop gate lands in PR 4); what activates in PR 2 is the READER half of Lease.ps1.
. (Join-Path $PSScriptRoot 'lib/Lease.ps1')

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
# Get-JsonState was collapsed into deploy/lib/Lease.ps1 (msg-2172 Tier-C, 2026-08-28). The
# canonical reader lives there — it added a JSON-root shape guard (root arrays / scalars now
# return empty rather than leaking Count/Length/... metadata as fake resource keys, which the
# 2026-08-28 measurement confirmed was a permanent one-way corruption vector) and an
# opportunistic log-through that fires the "state file unreadable — treating as empty" line
# through Write-Log when it resolves in the caller's scope. Save-CorruptedStateBackup, the
# `.bad-<utc>` rename side effect, is a Lease.ps1 helper the flush caller invokes; PR 4 wires
# it into the leases.json flush path.

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
# during the tick. Used for the quarantine.json state file, where BOTH the sweep and an external
# tool (Clear-Quarantine) may write during a sweep run. (The head-skip state file
# state\head_skip.json is written exclusively by the CLI, so it does not need this treatment; the
# old state\heads.json that shared this discipline is gone — see msg-1432 §W-2.)
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
# from every disposition that ACTUALLY REACHED the candidate — i.e. every candidate that received
# a decide() verdict this tick (LAUNCH, DEFER, or SKIP) — plus the launched thread's own
# post-verdict tick where the head_skip predicate's decision was consulted.
#
# W-2c (Bohr msg-1432): every decide verdict is an evaluation. That includes LAUNCHes the sweep
# actually spawned, LAUNCH verdicts the sweep did NOT act on (because the K-budget capped or an
# earlier candidate did work), DEFERs (backoff timing check happened), and SKIPs (Stage-1
# stop-token judgment happened). All of them mean "the sweep asked its question about this
# candidate this tick" — which is what this metric measures. This supersedes the old
# head-cache-match branch (Test-CanSkip) — that branch is gone, and the new predicate's
# SKIP/DEFER verdicts stand in for it (msg-1432 §W-2c; forbidden not to update: a chain-cutoff
# thread stuck on NEXT: NotAPersona SKIPs every tick under the new predicate and would otherwise
# be structurally invisible to starvation).
#
# WHY OTHER DISPOSITIONS DO NOT. `held` / `quarantined-skipped` / `not-reached` all mean the sweep
# never actually asked the question this tick — a HELD candidate is deliberately withheld by the
# operator (and never even reaches the decide batch), a quarantined-skipped candidate is deferred
# until human clear (excluded from decide by the wrapper), and a not-reached candidate is one the
# sweep never got to (K-budget hit, earlier candidate did work). None of them advance the "how
# long since I actually reached this?" clock. That is the design's Q4 honesty rule and it is
# load-bearing — the metric only means anything because these DO age past 24h.
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

# --- head-skip nomination predicate wiring (T-sweep-intake-and-quarantine-stalls) ---------------
#
# The skip rule now lives inside scripts/head_skip_decide.py (module: head_skip.py). The wrapper's
# only job is to feed the CLI a candidate batch (thread_id / head_msg_id / control_state), read
# back the verdicts, and act on them. See Bohr msg-1430 §W-2 for the frozen contract; the key
# points restated here (source-of-truth = the CLI):
#
#   - The CLI owns the state file at $headSkipStatePath. The wrapper does NOT read or write it.
#   - The atomicity of the write, the "corrupt or missing → empty" recovery, and the head-body
#     re-fetch on cache miss are all inside the CLI.
#   - The wrapper calls in TWO PHASES: `--mode decide` once at the start of the tick over ALL
#     the eligible candidates of one project (batch), then `--mode commit-launch` per candidate
#     it actually chooses to spawn, BEFORE the spawn. LAUNCH verdicts the wrapper does not act
#     on are left uncommitted — that is what prevents the "phantom-launched" starvation loop
#     the CLI's docstring calls out.
#   - Failure classification is two-layered (msg-1430 §W-3):
#       * candidate-data errors (a body fetch that fails for a single thread) are handled INSIDE
#         the CLI and returned as an UNRESOLVED-token LAUNCH — the wrapper does nothing.
#       * SYSTEMIC failures (interpreter missing, decide.py itself crashes, output not parseable,
#         non-zero exit) FAIL CLOSED: the wrapper `throw`s, the catch above sets $exitCode=1,
#         and the tick aborts without persisting anything. Rationale: if `decide` is broken then
#         `commit-launch` is broken too (same interpreter, same module), so a fail-open LAUNCH
#         would spawn conductors without ever recording their launches — bypassing the backoff
#         CAP entirely and reproducing the exponential-launch spiral #140 was written to fix.
#         The cost of the fail-closed direction is that a persistently-broken decide silences
#         every candidate at once — the digest's flooding starvation section is the signal.

# Invoke `head_skip_decide.py --mode decide` for one project. Returns:
#   @{
#       ok       = $true / $false
#       verdicts = @{ thread_id -> verdict-hashtable } on success, @{} on failure
#       error    = $null on success, or a diagnostic string on failure
#   }
# A returned `ok=$false` is a SYSTEMIC failure — the caller must fail-closed on it (W-3 layer 2).
function Invoke-HeadSkipDecide {
    param(
        [string]$Project,
        [array]$Candidates,
        [string]$StateFilePath,
        [string]$Mode = 'decide'
    )

    $decideScript = Join-Path $repoRoot "scripts\head_skip_decide.py"
    if (-not (Test-Path -LiteralPath $decideScript)) {
        return @{ ok = $false; verdicts = @{}; error = "head_skip_decide.py not found at $decideScript" }
    }

    # Build the JSON array of candidates. Empty / null head_msg_id and control_state are legal —
    # the CLI treats them as "unknown" and fails open in the fetch / progression checks.
    $items = @()
    foreach ($c in $Candidates) {
        $hid = if ($null -ne $c.head_msg_id) { "$($c.head_msg_id)" } else { "" }
        $ctl = if ($null -ne $c.control_state) { "$($c.control_state)" } else { "" }
        $items += @{
            thread_id     = "$($c.thread_id)"
            head_msg_id   = $hid
            control_state = $ctl
        }
    }
    $payload = ConvertTo-Json -InputObject @($items) -Depth 4 -Compress

    try {
        Push-Location $repoRoot
        try {
            # Same invocation pattern as Invoke-HeadProbe / Invoke-ParkedHumansProbe: run under
            # `uv run python` from the repo root so the module import resolves against this
            # checkout. Reusing the same interpreter path is deliberate (msg-1430 §W-3 tail): if
            # a future refactor needs a different python it needs to touch one place, not two,
            # so there is never a version where `decide` and `commit-launch` disagree on runtime.
            $raw = $payload | & uv run python $decideScript `
                --project $Project --state-file $StateFilePath --mode $Mode 2>&1
            $code = $LASTEXITCODE
        }
        finally { Pop-Location }
    }
    catch {
        return @{ ok = $false; verdicts = @{}; error = "head_skip decide invocation failed: $($_.Exception.Message)" }
    }

    if ($code -ne 0) {
        $tail = ($raw | ForEach-Object { "$_" }) -join ' / '
        return @{ ok = $false; verdicts = @{}; error = "head_skip decide exited ${code}: $tail" }
    }

    $json = $raw | ForEach-Object { "$_" } | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
    if (-not $json) {
        return @{ ok = $false; verdicts = @{}; error = "head_skip decide produced no JSON on stdout" }
    }

    try {
        $obj = $json | ConvertFrom-Json
    }
    catch {
        return @{ ok = $false; verdicts = @{}; error = "head_skip decide output not JSON: $($_.Exception.Message)" }
    }

    $verdictMap = @{}
    if ($obj -and $obj.PSObject.Properties.Name -contains 'verdicts') {
        foreach ($v in @($obj.verdicts)) {
            $tid = "$($v.thread_id)"
            if ($tid) { $verdictMap[$tid] = $v }
        }
    }
    return @{ ok = $true; verdicts = $verdictMap; error = $null }
}

# Invoke `head_skip_decide.py --mode commit-launch --payload <payload>` for one thread. Returns:
#   @{ ok = $true / $false; error = $null / diagnostic }
# Called BEFORE spawning the conductor session for the chosen candidate — that is the
# "session-start-before write" contract that survives a forced kill (head_skip.py docstring).
function Invoke-HeadSkipCommitLaunch {
    param(
        $Payload,
        [string]$StateFilePath
    )

    $decideScript = Join-Path $repoRoot "scripts\head_skip_decide.py"
    if (-not (Test-Path -LiteralPath $decideScript)) {
        return @{ ok = $false; error = "head_skip_decide.py not found at $decideScript" }
    }
    if ($null -eq $Payload) {
        return @{ ok = $false; error = "commit-launch payload is null" }
    }
    $payloadJson = ConvertTo-Json -InputObject $Payload -Depth 6 -Compress

    try {
        Push-Location $repoRoot
        try {
            $raw = & uv run python $decideScript `
                --state-file $StateFilePath --mode commit-launch --payload $payloadJson 2>&1
            $code = $LASTEXITCODE
        }
        finally { Pop-Location }
    }
    catch {
        return @{ ok = $false; error = "head_skip commit-launch invocation failed: $($_.Exception.Message)" }
    }
    if ($code -ne 0) {
        $tail = ($raw | ForEach-Object { "$_" }) -join ' / '
        return @{ ok = $false; error = "head_skip commit-launch exited ${code}: $tail" }
    }
    return @{ ok = $true; error = $null }
}

# Read the head-skip mode from the environment. Named after the CLI's REPORT_MODE_ENV constant
# so a `git grep MINDWIRE_HEADSKIP_MODE` finds both sides. Values: "decide" (default) or
# "report" (dry-run: the CLI computes verdicts but writes nothing on disk, and the wrapper does
# not commit-launch or spawn — the operator's pre-wire measurement tool per msg-1430 §W-5).
function Get-HeadSkipMode {
    $m = $env:MINDWIRE_HEADSKIP_MODE
    if (-not $m) { return 'decide' }
    if ($m -eq 'report' -or $m -eq 'decide') { return $m }
    Write-Log "MINDWIRE_HEADSKIP_MODE=$m is not a recognised mode; falling back to 'decide'"
    return 'decide'
}

# --- quarantine ---------------------------------------------------------------------------------
# A quarantined thread has failed at least once and will not be launched again by this wrapper until
# a human clears it (deploy/Clear-Quarantine.ps1). Records live in <data_dir>/state/quarantine.json,
# one entry per `project/thread_id`. The head-skip state file $headSkipStatePath is separate —
# different owner (the CLI), different concern (nomination-predicate observation), different life
# cycle. The candidate filter reads both and excludes a candidate that lands on either one.
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

# Build a repro_hint string from a quarantine record's existing fields
# (T-sdk-is-error-loses-the-reason D-6 / S-8). NEVER accepts a new probe input:
# the whole regularity the design leans on is "runner uses only what it already
# has in hand for its own purposes" (see the FINGERPRINT rule in the record
# docblock). What was missing was DRAWING — the head / control / log path values
# were there, but nothing composed them into something a human could paste into
# a terminal.
#
# Deliberately named `repro_hint` not `repro`: the H2 (systematic) judgement
# means "same failure twice", NOT "deterministic under these conditions".
# Overclaiming determinism here is the exact overclaim the wider design refuses.
#
# $Fingerprint is untyped for the same reason Get-FingerprintHint's is untyped —
# it may be a native hashtable (fresh) or a PSCustomObject (JSON round-tripped).
function Get-QuarantineReproHint {
    param(
        $Fingerprint,
        [string]$SessionLogPath,
        [string]$Key
    )

    if (-not $Fingerprint) { return $null }
    $head    = $Fingerprint.head
    $control = $Fingerprint.control
    # Both components must be present for the line to be useful; a partial hint
    # would suggest the runner has a repro command it does not have.
    if (-not $head -or -not $control) { return $null }

    # Wall-clock ISO 8601 timestamp is deliberately absent — the record already
    # carries first_failure_at / last_failure_at, and duplicating them in the
    # hint invites drift when they refresh.
    $line = "repro-hint: $Key で head=$head control=$control のとき失敗"
    if ($SessionLogPath) {
        $line += " — ログ: $SessionLogPath"
    }
    return $line
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

# Result classifier for a Send-Notification return. Two questions come out of one shape so the two
# never diverge:
#   - Test-DigestDelivered: should the cadence gate advance for this period? (i.e., "retrying THIS
#     period cannot help — either something landed or nothing WILL land")
#     If yes, `last_sent_period` advances → cadence gate closes for the period.
#   - Test-DigestFullSuccess: did the FULL digest land? (only 'sent' with class 'ok')
#     If yes, `last_full_success_period` advances → ⚠ predicate clears.
#
# Cadence-advancing outcomes:
#   * sent(ok)              — the full digest landed.
#   * degraded(ok)          — the fixed-length fallback landed after a full 400.
#   * skipped(no-webhook)   — the operator has deliberately no channel; retrying every 5 minutes
#                             accomplishes nothing and would log-spam the daemon.
#   * failed(deterministic-permanent) — the webhook is gone (401/403/404). PR-gate review
#                             (2026-08-30): my Get-NotificationFailureClass docstring literally
#                             said "Do NOT send a second POST" for this class, but the earlier
#                             predicate ignored $class and refused to advance, spamming 404s every
#                             5 minutes for the rest of the day. Advance the period so the
#                             next-tick check sees "already sent" and stops.
#   * failed(deterministic-payload) — only reached when the FULL digest 400s AND the degraded
#                             fallback ALSO 400s. Retrying the same tick will fail the same way;
#                             advance to prevent spam. (This branch is defensive — the degraded
#                             message is fixed and small; if it 400s, something more fundamental
#                             is broken and 5-minute spam does not help.)
#
# Held (returns $false):
#   * failed(transient)     — network / 5xx / 429 / unknown. The next tick has a real chance to
#                             succeed; this is the WHOLE reason the cadence gate exists.
#
# `skipped` and every non-transient failure count for cadence but NOT for full-success — the human
# was not informed, so ⚠ must keep ticking. This distinction is what surfaces "wired the webhook
# but Discord side keeps 400ing" without the operator having to guess.
#
# (T-digest-exceeds-discord-limit-and-is-dropped D-6; replaces the older Test-DigestClockAdvances.
# PR-gate naysayer 2026-08-30 caught that the earlier version of THIS function inspected $status
# only and mis-held cadence on non-retryable failures.)
function Test-DigestDelivered {
    param($Result)
    if ($Result -is [hashtable]) {
        $status = $Result['status']
        $class = $Result['class']
    } else {
        $status = $Result
        $class = $null
    }
    if ($status -eq 'sent' -or $status -eq 'skipped' -or $status -eq 'degraded') { return $true }
    # A non-retryable failure class still advances cadence — the whole point of "non-retryable" is
    # that a 2nd POST this tick, or a 3rd POST 5 minutes later, is guaranteed to fail the same way.
    # Held would mean spam. Advanced means "we tried once, we know it won't work, don't try again
    # until tomorrow"; ⚠ still lights up because Test-DigestFullSuccess is separate.
    if ($status -eq 'failed' -and ($class -eq 'deterministic-permanent' -or $class -eq 'deterministic-payload')) {
        return $true
    }
    return $false
}
function Test-DigestFullSuccess {
    param($Result)
    if ($Result -is [hashtable]) {
        return ($Result['status'] -eq 'sent' -and $Result['class'] -eq 'ok')
    }
    return ($Result -eq 'sent')
}

# T-digest-exceeds-discord-limit-and-is-dropped D-2: combine the full-digest send result with
# the (optional) degraded-fallback send result into the single result that drives cadence and
# health decisions downstream.
#
# The rule is tight:
#   * No degraded attempt (fallback was not triggered) → pass-through the full result.
#   * Degraded landed (status='sent') → emit `{status='degraded'; class='ok'}` so
#     Test-DigestFullSuccess stays false (the operator got the fallback, not the queue) while
#     Test-DigestDelivered advances cadence.
#   * Degraded also failed → USE the degraded's actual result. This is the crux: its `class`
#     (transient vs deterministic-*) is what the cadence predicate needs. PR-gate round 3 caught
#     the earlier revision preserving the ORIGINAL `deterministic-payload` on degraded-transient-
#     failure, which Test-DigestDelivered treats as non-retryable, silently advancing cadence
#     and abandoning what was actually a retryable delivery.
#
# Extracted so the transformation is testable in isolation — tests/Test-SweepDigest.ps1's
# "PR-gate regression" block pins the four combinations. If a future edit ever collapses this
# back to inline code that discards the degraded class, the pinned tests fail loudly.
function Resolve-DigestSendResult {
    param($FullResult, $DegradedResult = $null)
    if ($null -eq $DegradedResult) { return $FullResult }
    if ($DegradedResult -is [hashtable] -and $DegradedResult['status'] -eq 'sent') {
        $httpStatus = if ($DegradedResult.ContainsKey('http_status')) { $DegradedResult['http_status'] } else { 200 }
        return @{ status = 'degraded'; class = 'ok'; http_status = $httpStatus; error = 'full-payload rejected, degraded landed' }
    }
    return $DegradedResult
}

# T-digest-exceeds-discord-limit-and-is-dropped D-6 (msg-2106 §3, msg-2104): the digest cadence
# is per-PERIOD, not per-interval. A period id is a wall-clock local date string; the predicate
# "period P ≠ period Q" is jitter-immune because a 5-minute tick offset does not cross a date
# boundary. This is the whole point of the shift from `last_sent_at` (a timestamp) to
# `last_sent_period` (a discrete id) — the jitter tolerance that Einstein msg-2104 proposed as a
# fudge constant becomes structurally unnecessary.
#
# The period id is the LOCAL calendar day (yyyy-MM-dd). "Local" means the machine running the
# scheduler; that is the same clock Takahito reads on his phone at breakfast, which is the
# consumer this digest is FOR.
function Get-DigestPeriod {
    param([datetime]$Now)
    # Get-Date .ToLocalTime() gives us a DateTime with Kind=Local. Format as ISO date.
    return $Now.ToLocalTime().ToString('yyyy-MM-dd')
}

# Are we AT or PAST today's delivery time? Period-gated cadence has two parts (msg-2106 §4): the
# period must be new AND the local wall clock must be ≥ configured delivery time. Without the second
# check, a period boundary at midnight (23:58 send + 00:03 tick) would double-send.
function Test-DigestDeliveryDue {
    param([datetime]$Now, [TimeSpan]$DeliveryTime)
    $local = $Now.ToLocalTime()
    return $local.TimeOfDay -ge $DeliveryTime
}

# Compute the "periods missed since the operator was last told the queue" from health state.
#
# Uses date arithmetic on the parsed period ids so daylight-savings transitions do not skew the
# count. The predicate is "the number of local calendar days elapsed", which is the same thing an
# operator counts on a wall calendar; no time-difference math is involved. D-6 predicate discipline
# holds: every field this reads is period-typed. `last_attempt_at` / `last_error*` stay display-only.
#
# TWO histories, because "no success recorded" has two meanings and conflating them is fail-open
# (Einstein msg-2396 E-4, accepted whole by Bohr msg-2401 §5):
#
#   * `LastFullSuccessPeriod` present — a full digest HAS landed before. Missed = the periods
#     between then and now: delta ≤ 1 is healthy (today's send follows yesterday's success).
#   * absent, `FirstAttemptPeriod` present — the digest has been ATTEMPTED and has NEVER once
#     landed. Every period from the first attempt up to (not including) the current one is a period
#     the operator was not told: missed = delta. Note this is one more than the branch above for
#     the same delta, and correctly so — there the boundary period succeeded, here it failed.
#   * both absent — genuinely nothing has ever been attempted. 0, do not alarm on a first run.
#
# What this closes: `last_full_success_period` records a SUCCESS EDGE, so a system that has never
# succeeded has no record to read, and the old rule read that emptiness as "healthy first run". The
# ⚠ therefore went permanently dark in the exact state it exists to report — measured on live
# `state/notify-health.json`, which carried only last_error / last_error_class / last_attempt_at for
# the whole of the #203 regression. A level condition ("has not succeeded") cannot be derived from
# an edge record alone; `first_attempt_period` supplies the missing lower bound.
function Get-DigestPeriodsMissed {
    param([string]$CurrentPeriod, [string]$LastFullSuccessPeriod, [string]$FirstAttemptPeriod)
    $anchor = if ($LastFullSuccessPeriod) { $LastFullSuccessPeriod } else { $FirstAttemptPeriod }
    if (-not $anchor) { return 0 }
    try {
        $cur = [datetime]::ParseExact($CurrentPeriod, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
        $from = [datetime]::ParseExact($anchor, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
    } catch { return 0 }
    $delta = ($cur - $from).Days
    if ($LastFullSuccessPeriod) {
        if ($delta -le 1) { return 0 }  # 1 = healthy (today's send follows yesterday's success)
        return ($delta - 1)             # 2 = missed 1, 4 = missed 3, ...
    }
    # Never-succeeded branch: the anchor period itself is a period that failed, so it counts.
    if ($delta -lt 1) { return 0 }      # 0 = the first attempt is happening right now
    return $delta                       # 1 = missed 1, 3 = missed 3, ...
}

# T-digest-exceeds-discord-limit-and-is-dropped D-6 (msg-2106): the ⚠ line is DERIVED from
# `last_full_success_period`. No stored counter, no threshold constant — just "period arithmetic
# says N days without a full delivery, and N ≥ 2". The predicate discipline (D-6) forbids
# consulting `last_attempt_at` / `last_error*` here: those are display-only, and letting them
# influence the predicate would recreate the "cleared by a transient recovery" bug Einstein
# msg-2102 §2 found in the earlier draft.
function Get-DigestHealthWarning {
    param([hashtable]$Health, [string]$CurrentPeriod)
    $lastFull = $null
    if ($Health -and $Health.ContainsKey('last_full_success_period')) { $lastFull = $Health['last_full_success_period'] }
    $firstAttempt = $null
    if ($Health -and $Health.ContainsKey('first_attempt_period')) { $firstAttempt = $Health['first_attempt_period'] }
    $missed = Get-DigestPeriodsMissed -CurrentPeriod $CurrentPeriod `
                                      -LastFullSuccessPeriod $lastFull -FirstAttemptPeriod $firstAttempt
    if ($missed -lt 1) { return $null }
    $errClass = $null
    if ($Health -and $Health.ContainsKey('last_error_class')) { $errClass = $Health['last_error_class'] }
    $errSuffix = if ($errClass) { " / 直近: $errClass" } else { '' }
    # Two wordings, because the two states call for different operator action: "it stopped working"
    # sends you to what changed, "it has never worked" sends you to the wiring. The old single
    # wording could not even render the second — it interpolated an empty $lastFull into
    # "（最後の成功 ）", which is the shape of a bug report about the warning rather than a warning.
    if (-not $lastFull) {
        return "⚠ フル digest は一度も配送できていません（$missed 期間連続 / 初回試行 $firstAttempt$errSuffix）"
    }
    return "⚠ フル digest が $missed 期間配送できていません（最後の成功 $lastFull$errSuffix）"
}

# T-digest-exceeds-discord-limit-and-is-dropped D-2 (msg-2099): the fallback message the operator
# sees when the full digest hits a deterministic-payload rejection. Fixed length, self-describing,
# and honest about what happened: the point is that a DEGRADED delivery is still a delivery, and
# the operator reading it should be told "the mechanism is broken, not the queue".
function New-DegradedDigestMessage {
    param([int]$WaitingCount, [string]$CurrentPeriod)
    return "MindWire 日次ダイジェスト ($CurrentPeriod) — フル本文の組み立てに失敗しました（$WaitingCount 件待機中）。chatroom を確認してください。"
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
        # T-decision-request-composer S4 (D-32).
        # $HumanParked is an array of PSCustomObjects: { key; project; thread_id; head_msg_id }.
        # Its judgment comes from ``scripts/parked_humans.py`` (D-32: re-uses
        # ``spirrow_mindwire.conductor.handoff.resolve_handoff`` — the single owner of the ``NEXT:``
        # grammar). NEVER computed inside this function; passed in so the log line above the digest
        # and the digest section itself see the exact same list.
        # $PendingDecisionsState is the composed-question cache (S2), consulted ONLY to enrich the
        # row with the composer's question when a matching signature is present. A missing cache
        # entry leaves the row's placeholder "(問い未生成)" — A-14 (the section survives a wiped
        # cache; only the enrichment degrades).
        [array]$HumanParked = @(),
        [hashtable]$PendingDecisionsState = @{},
        # $ParkedPollErrors is the errors[] list from scripts/parked_humans.py (per-candidate fetch
        # failures). Rendered as "取得失敗: N 件" under the 判断待ち section so an outage does not
        # silently under-report; the section itself never disappears (I-2 "黙って劣化しない").
        [array]$ParkedPollErrors = @(),
        # T-digest-exceeds-discord-limit-and-is-dropped D-1 (msg-2099): budget in characters.
        # 0 or omitted = unbounded (legacy callers). When > 0, per-section entry lists are truncated
        # (oldest-first is preserved) and a `+N 件` marker records the exact number dropped, so the
        # header count is always the true total. If the fixed overhead alone exceeds the budget,
        # the digest is emitted anyway (its overhead is small and self-consistent); a caller that
        # cannot afford even the overhead should call New-DegradedDigestMessage directly.
        [int]$Budget = 0,
        # T-digest-exceeds-discord-limit-and-is-dropped D-6: the ⚠ line derived from
        # notify-health.json. Prepended above the header when set — non-null iff the last full
        # success is ≥ 2 periods old. Never affects the dedup signature: msg-2101 D-7 forbids
        # letting rendering-side ephemera reach any suppression predicate.
        [string]$HealthWarning = $null
    )

    # Split by derived state (based on age, not stored — the digest is a snapshot of reality now).
    # Age (seconds since first_failure_at) is carried alongside each line so the renderer can sort
    # oldest-first — msg-2099 D-1: truncation must drop what is LEAST likely to have been forgotten.
    $escalatedList = @()
    $quarantinedList = @()
    $staleList = @()
    $oldestQuarantineAge = $null
    foreach ($key in $QuarantineState.Keys) {
        $rec = $QuarantineState[$key]
        $firstAt = ConvertTo-UtcInstant $rec.first_failure_at
        $derived = Get-DerivedQuarantineState -FirstFailureAt $firstAt -Now $Now
        $ageSpan = ($Now - $firstAt)
        if ($null -eq $oldestQuarantineAge -or $ageSpan -gt $oldestQuarantineAge) { $oldestQuarantineAge = $ageSpan }
        $age = Format-DurationDigest -Span $ageSpan

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
        # T-sdk-is-error-loses-the-reason S-8: a repro_hint from record-fields
        # only (fingerprint + session_log_path). Rendered on its OWN
        # continuation line, indented, rather than suffixed to the entry line —
        # measured, the full hint string ("$key で head=$head control=$control
        # のとき失敗 — ログ: $path") is 100–150 chars including the path, and
        # jamming it after the age + movement-hint makes the entry line wrap
        # inside a Discord embed / terminal digest to the point of being
        # unreadable. The digest's structure remains "one entry per record"
        # (never two records interleaved), it just gains a second line per
        # entry when there is a repro-hint to show. Only appended when the
        # fields the hint needs are actually present — a partial line
        # ("head=None") would be worse than none. (PR #181 round 4 naysayer
        # review: earlier the comment CLAIMED one-line while the code emitted
        # a newline; reconciled here in favour of the newline, which is what
        # the content actually needs.)
        $reproHint = Get-QuarantineReproHint -Fingerprint $rec.failure_fingerprint `
                                             -SessionLogPath $rec.session_log_path `
                                             -Key $key

        $line = "  $key   $age"
        if ($hint) { $line += "   ⚠ $hint" }
        if ($reproHint) { $line += "`n    $reproHint" }

        # Carry the age-in-seconds beside the line so the sort step can order oldest-first without
        # re-parsing formatted durations. PSCustomObject with .Line and .AgeSeconds so the sort key
        # is unambiguous even if a formatted string happens to contain digits (unlikely, but the
        # explicit numeric key is honest about what "oldest first" means).
        $entry = [PSCustomObject]@{ Line = $line; AgeSeconds = [int64]$ageSpan.TotalSeconds }
        switch ($derived) {
            'stale'       { $staleList       += $entry }
            'escalated'   { $escalatedList   += $entry }
            default       { $quarantinedList += $entry }
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
            $starvedLine = "  $key   $(Format-DurationDigest -Span $age)$suffix"
            $starvedList += [PSCustomObject]@{ Line = $starvedLine; AgeSeconds = [int64]$age.TotalSeconds }
        }
    }

    # Sort each section oldest-first (largest AgeSeconds first) — msg-2099 D-1: what's most likely
    # to have been forgotten goes first, and truncation drops the newest.
    $staleList       = @($staleList       | Sort-Object -Property AgeSeconds -Descending)
    $escalatedList   = @($escalatedList   | Sort-Object -Property AgeSeconds -Descending)
    $quarantinedList = @($quarantinedList | Sort-Object -Property AgeSeconds -Descending)
    $starvedList     = @($starvedList     | Sort-Object -Property AgeSeconds -Descending)

    # Build the compact summary line first (msg-2099 D-1: "1 行目で行動が決まる — 件数と最古の
    # 待ち日数"). Emitted even when both sections are 0 so the format is stable across empty and
    # non-empty days.
    $totalQ = $escalatedList.Count + $quarantinedList.Count + $staleList.Count
    $oldestQuarantineDays = if ($oldestQuarantineAge) { [int]($oldestQuarantineAge.TotalDays) } else { 0 }
    $summary = "human-parked $($HumanParked.Count) / 隔離 $totalQ / 飢餓 $($starvedList.Count) / 最古 ${oldestQuarantineDays}d"

    $lines = @()
    # T-digest-exceeds-discord-limit-and-is-dropped D-6: ⚠ line ABOVE the header when set. Prepending
    # rather than appending because the mobile reader sees the first ~2 lines in the notification
    # preview, and the whole point of the ⚠ is that the operator sees it without opening.
    if ($HealthWarning) { $lines += $HealthWarning }
    $lines += "MindWire 日次ダイジェスト ($(Get-Date -Date $Now.ToLocalTime() -Format 'yyyy-MM-dd HH:mm'))"
    $lines += $summary
    $lines += ""

    # T-digest-exceeds-discord-limit-and-is-dropped D-1: emit-with-budget helper. When $Budget is 0
    # every list is emitted in full (legacy behaviour, preserves existing tests). When $Budget > 0,
    # each section is truncated so the ENTIRE output stays ≤ budget; the count in the header stays
    # correct because it is computed from the full list before truncation.
    #
    # Budget accounting has TWO reserves so nothing after a truncation can push the total over:
    #   * $overflowMarker = the "+N 件（省略）" line that MAY follow a truncated section.
    #   * $trailingReserve = every line the renderer WILL emit after any given section — headers of
    #     the sections that come later, and the invariant footer at the bottom. Without this the
    #     first section could consume everything and later sections would push the total over.
    # The trailing content is emitted verbatim in the block below, so its size is exactly counted;
    # a future edit that adds/removes content there must also update these constants. Guarded by
    # tests/Test-SweepDigest.ps1 (the 200-entry check will fail loudly if the actual total exceeds
    # budget for any reason).
    $overflowMarker = 60
    # Trailing content estimate (in characters, generous):
    #   - 判断待ち header + fetch-error rows       : ~ 80
    #   - 飢餓 header + entries (bounded by _AddSectionEntries call)
    #   - blank line + 3-line footer               : ~200
    # Sum ~= 280; use 400 for safety margin. If the bounded renderer's total ever creeps over
    # $Budget, tighten this by measuring the actual emitted trailing content and subtracting.
    $trailingReserve = 400
    $currentLen = ($lines -join "`n").Length
    function _AddSectionEntries {
        param([array]$Entries, [int]$MaxLen, [int]$Reserve, [ref]$RunningLen)
        # Returns @(linesToEmit, droppedCount). Adds newline+entry pairs from $Entries in order,
        # stopping when adding the NEXT line would push (RunningLen + line + Reserve) past MaxLen.
        $out = @()
        $dropped = 0
        for ($i = 0; $i -lt $Entries.Count; $i++) {
            $entry = $Entries[$i]
            $line = if ($entry -is [PSCustomObject] -and $entry.PSObject.Properties.Name -contains 'Line') { $entry.Line } else { [string]$entry }
            # +1 for the newline that will join this line to whatever came before.
            $cost = 1 + $line.Length
            if ($MaxLen -gt 0 -and ($RunningLen.Value + $cost + $Reserve) -gt $MaxLen) {
                $dropped = $Entries.Count - $i
                break
            }
            $out += $line
            $RunningLen.Value += $cost
        }
        return @{ Emitted = $out; Dropped = $dropped }
    }

    $lines += "隔離中: $totalQ 件"
    if ($totalQ -eq 0) {
        $lines += "  (該当なし)"
    }
    else {
        # Sections in order of urgency: stale (7d+) → escalated (24h+) → quarantined (fresh). The
        # order was already this in the legacy renderer; the change is only that within each
        # section, entries are oldest-first (see the sort step above).
        # $totalReserve = overflow marker + everything the renderer WILL still emit after this
        # section (see the block comment above for the count). Passed to every section emitter so
        # a later section is never starved by an earlier one that consumed the entire budget.
        $totalReserve = $overflowMarker + $trailingReserve
        if ($staleList.Count -gt 0) {
            $lines += "  [stale] — 直すか、スレッドを畳むか決めよ"
            $runLen = [ref]($lines -join "`n").Length
            $result = _AddSectionEntries -Entries $staleList -MaxLen $Budget -Reserve $totalReserve -RunningLen $runLen
            $lines += $result.Emitted
            if ($result.Dropped -gt 0) { $lines += "  +$($result.Dropped) 件（省略）" }
        }
        if ($escalatedList.Count -gt 0) {
            $lines += "  [escalated] — 24h 以上経過"
            $runLen = [ref]($lines -join "`n").Length
            $result = _AddSectionEntries -Entries $escalatedList -MaxLen $Budget -Reserve $totalReserve -RunningLen $runLen
            $lines += $result.Emitted
            if ($result.Dropped -gt 0) { $lines += "  +$($result.Dropped) 件（省略）" }
        }
        if ($quarantinedList.Count -gt 0) {
            $lines += "  [quarantined]"
            $runLen = [ref]($lines -join "`n").Length
            $result = _AddSectionEntries -Entries $quarantinedList -MaxLen $Budget -Reserve $totalReserve -RunningLen $runLen
            $lines += $result.Emitted
            if ($result.Dropped -gt 0) { $lines += "  +$($result.Dropped) 件（省略）" }
        }
    }

    # 判断待ち — T-decision-request-composer S4 (msg-1370 §0 defect 2 / §4 / A-4; D-32 for the
    # grammar-ownership rule). Emitted even at 0 件, mirroring the "silent day is the point"
    # contract of 飢餓 (msg-814 §5). The count and the row order come from $HumanParked, which is
    # itself the output of scripts/parked_humans.py — so this section restores fully from a wiped
    # pending-decisions.json (A-14); the cache only enriches the row with the composer's question.
    $lines += ""
    $lines += "判断待ち: $($HumanParked.Count) 件"
    if ($HumanParked.Count -eq 0) {
        $lines += "  (該当なし)"
    }
    else {
        # Pre-build all rows into an entry list so the budget helper can operate uniformly. Row
        # order is preserved (msg-1370's caller-owned order — this renderer does not resort
        # human-parked; only quarantine sections are sorted oldest-first here).
        $parkedEntries = @()
        foreach ($p in $HumanParked) {
            $key = $p.key
            $head = $p.head_msg_id
            # Look up the composer question by (key, signature = "human:<head>"). Any other
            # signature shape came from a different reason and does not match this row — the
            # cache row is intentionally strict on signature equality (S2 A-3).
            $sig = "human:$head"
            $questionSnippet = $null
            if ($PendingDecisionsState.ContainsKey($key)) {
                $row = $PendingDecisionsState[$key]
                if ($row -is [hashtable]) { $rowSig = $row['signature']; $env = $row['envelope'] }
                else { $rowSig = $row.signature; $env = $row.envelope }
                if ($rowSig -eq $sig -and $env) {
                    $status = if ($env.PSObject.Properties.Name -contains 'composer_status') { $env.composer_status } else { $null }
                    $output = if ($env.PSObject.Properties.Name -contains 'output') { $env.output } else { $null }
                    if ($status -eq 'ok' -and $output) {
                        $q = if ($output.PSObject.Properties.Name -contains 'question') { $output.question } else { $null }
                        if ($q) {
                            # One-line-per-row readability: cap at 80 chars, flatten any
                            # embedded newlines, so a multi-line composed question cannot wrap
                            # the digest layout.
                            $flat = ($q -replace "`r?`n", ' ').Trim()
                            if ($flat.Length -gt 80) { $flat = $flat.Substring(0, 79) + '…' }
                            $questionSnippet = $flat
                        }
                    }
                }
            }
            $suffix = if ($questionSnippet) { "   — $questionSnippet" } else { "   — (問い未生成)" }
            $parkedEntries += [PSCustomObject]@{ Line = "  $key   [$head]$suffix"; AgeSeconds = 0 }
        }
        # 判断待ち section: 飢餓 + footer remain after this. Reserve marker + trailing.
        $runLen = [ref]($lines -join "`n").Length
        $result = _AddSectionEntries -Entries $parkedEntries -MaxLen $Budget -Reserve ($overflowMarker + $trailingReserve) -RunningLen $runLen
        $lines += $result.Emitted
        if ($result.Dropped -gt 0) { $lines += "  +$($result.Dropped) 件（省略）" }
    }
    # Fetch-error surface (I-2 "黙って劣化しない"). When scripts/parked_humans.py could not read
    # some threads' bodies, the section stays but the count above under-reports. Say so out loud.
    #
    # PR-gate review round 2 (2026-08-30): the per-row loop was unconditional, so a spike of ~20+
    # fetch errors could push the total past $Budget and trigger the exact Discord 400 this PR
    # exists to prevent. Route the row list through _AddSectionEntries so the same budget
    # discipline that governs every other row-emitting section governs this one too. The count-line
    # (single, small, informational) stays unconditional — the operator needs to see "取得失敗: N"
    # even if the individual rows had to be dropped for budget.
    if ($ParkedPollErrors.Count -gt 0) {
        $lines += "  取得失敗: $($ParkedPollErrors.Count) 件（判断待ちに含まれていない可能性あり）"
        $errorEntries = @()
        foreach ($e in $ParkedPollErrors) {
            $tid = if ($e.PSObject.Properties.Name -contains 'thread_id') { $e.thread_id } else { $e['thread_id'] }
            $reason = if ($e.PSObject.Properties.Name -contains 'reason') { $e.reason } else { $e['reason'] }
            $errorEntries += [PSCustomObject]@{ Line = "    $tid — $reason"; AgeSeconds = 0 }
        }
        # 飢餓 + footer still to come; reserve marker + trailing.
        $runLen = [ref]($lines -join "`n").Length
        $result = _AddSectionEntries -Entries $errorEntries -MaxLen $Budget -Reserve ($overflowMarker + $trailingReserve) -RunningLen $runLen
        $lines += $result.Emitted
        if ($result.Dropped -gt 0) { $lines += "    +$($result.Dropped) 件（省略）" }
    }

    $lines += ""
    $lines += "飢餓 (24h 以上評価されていない): $($starvedList.Count) 件"
    if ($starvedList.Count -eq 0) {
        $lines += "  (該当なし)"
    }
    else {
        # 飢餓 is the LAST list-emitting section. Only the footer remains, so reserve only marker
        # + a smaller trailing (footer ~200 alone).
        $runLen = [ref]($lines -join "`n").Length
        $result = _AddSectionEntries -Entries $starvedList -MaxLen $Budget -Reserve ($overflowMarker + 250) -RunningLen $runLen
        $lines += $result.Emitted
        if ($result.Dropped -gt 0) { $lines += "  +$($result.Dropped) 件（省略）" }
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

# T-digest-exceeds-discord-limit-and-is-dropped D-5 (msg-2099): the CONSUMPTION contract for a
# failure class is defined by this thread — the STATUS→CLASS mapping is not. That mapping's proper
# home is T-gate-review-submit-failure-handling; while it lands, the inline switch below is the
# provisional table with the caveat spelled out here so a reader knows to check the other thread
# once it merges (and to update `provisional=$true` → `$false` in one place).
#
# Three classes are meaningful to the caller:
#   - deterministic-payload: the request itself was rejected by the peer. Retrying with the SAME
#     payload will fail the SAME way. Only path that can succeed is a SMALLER payload → degraded
#     fallback (D-2). Digest cadence: mark last_sent_period so we do not spam, do NOT mark
#     last_full_success_period so ⚠ stays lit.
#   - deterministic-permanent: the webhook itself is gone (401/403/404). Retrying anything to this
#     URL will fail the same way. Do NOT send a second POST (that is the whole point of "permanent").
#     Digest cadence: mark last_sent_period (webhook-less days should not spam either).
#   - transient: network flake, proxy hiccup, Discord outage, or a rate-limit. Retryable on the
#     next tick. This is the ONLY class where digest cadence should hold its clock.
#   - unknown: fail-safe = transient (D-5). If we cannot tell, assume it might resolve on retry.
function Get-NotificationFailureClass {
    param([int]$HttpStatus, [string]$ExceptionMessage)
    # Payload-derived rejections. 400 is the exact failure msg-2013 measured 63 times in 3 days.
    # 413 (Payload Too Large) is included even though Discord returns 400 for oversize `content` —
    # some intermediaries (proxies, WAFs) translate one into the other, and both mean the same thing
    # to the caller: sending SMALLER is the only way through.
    if ($HttpStatus -eq 400 -or $HttpStatus -eq 413) { return 'deterministic-payload' }
    # Webhook itself is gone. 401 (bad token) / 403 (forbidden) / 404 (webhook deleted from the
    # Discord side) all mean "this URL will never accept your POST"; retrying with a smaller
    # payload will not help, so degraded fallback is skipped.
    if ($HttpStatus -eq 401 -or $HttpStatus -eq 403 -or $HttpStatus -eq 404) { return 'deterministic-permanent' }
    # 429 is 4xx but transient — the whole point of the class is that its taxonomy is not "status
    # code >= 400" but "same POST retries succeed". This is exactly why D-5 refuses to define the
    # taxonomy inline: too many rules and only one place they can drift out of sync.
    if ($HttpStatus -eq 429) { return 'transient' }
    # 5xx: server-side, retryable.
    if ($HttpStatus -ge 500 -and $HttpStatus -lt 600) { return 'transient' }
    # No status code at all: network / DNS / proxy. Retryable.
    if ($HttpStatus -eq 0) { return 'transient' }
    # Unknown 4xx that wasn't caught above — safest default is transient (D-5, fail-safe = do the
    # thing that already worked, per #138 round 2's rationale for the outage case).
    return 'transient'
}

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
        return @{ status = 'skipped'; class = 'no-webhook'; http_status = 0; error = $null }
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
        return @{ status = 'sent'; class = 'ok'; http_status = 200; error = $null }
    }
    catch {
        # 'failed' — webhook configured but the send failed. NEVER fail the sweep because the
        # notifier failed — the conductor's work already happened. Redaction: scrub the webhook
        # out of any exception text before it touches the log or a record. Quarantine records /
        # digest lines / this log line all go through this branch, so no path that touches user
        # data can leak the bearer secret.
        $reason = "$($_.Exception.Message)".Replace($notifyWebhook, '<webhook-redacted>')
        # Extract the HTTP status from the exception if the response object survived. 0 = no status
        # (network / DNS / proxy failure). See Get-NotificationFailureClass for the taxonomy.
        $httpStatus = 0
        $resp = $_.Exception.Response
        if ($null -ne $resp) {
            try { $httpStatus = [int]$resp.StatusCode } catch { $httpStatus = 0 }
        }
        $class = Get-NotificationFailureClass -HttpStatus $httpStatus -ExceptionMessage $reason
        Write-Log "notification FAILED (non-fatal, class=$class, http=$httpStatus): $reason"
        return @{ status = 'failed'; class = $class; http_status = $httpStatus; error = $reason }
    }
}

# The dedup predicate the notification path consults. Extracted from Send-NotificationIfChanged so
# the material-push path (T-decision-material-push §DM-3, msg-1445) can consult the SAME predicate
# — a same-signature repeat tick must not fire the PUT any more than it fires the notification. Two
# independent predicates would drift; there is only one truth about "have we already alerted on this
# signature", so there is only one function that answers it.
function Test-NotificationSuppressed {
    param([hashtable]$State, [string]$Key, [string]$Signature)
    return ($State.ContainsKey($Key) -and $State[$Key] -eq $Signature)
}

# A thread parked on `NEXT: human` stays parked until Takahito acts, and the sweep re-reads it on
# every tick. Without this, one unattended weekend would fire the same alert repeatedly and the
# channel would be trained into noise. Fires only when $Signature differs from the last alert.
function Send-NotificationIfChanged {
    param([hashtable]$State, [string]$Key, [string]$Signature, [string]$Message)

    if (Test-NotificationSuppressed -State $State -Key $Key -Signature $Signature) {
        Write-Log "notification suppressed (unchanged since last alert: $Key = $Signature)"
        return
    }
    # The dedup record is intentional on every outcome (sent / skipped / any failure class). A
    # failed send does NOT undo the dedup, or a webhook outage would repeat every 5 minutes forever,
    # retraining the channel into noise. A 'skipped' status (no webhook) is treated the same: mark
    # the signature so we do not spam the log with skip messages on every re-attempt.
    # (Endorsed by Tier B naysayer on round 2 of #138.)
    #
    # T-digest-exceeds-discord-limit-and-is-dropped: an earlier revision of this PR skipped
    # recording on `deterministic-payload` (400/413) failures, chasing the letter of msg-2099 D-3.
    # That was wrong and the PR-gate naysayer caught it (2026-08-30): the dedup MAP IS KEYED BY
    # $Key (the thread id), not by $Signature. A DIFFERENT signature for the SAME key already
    # bypasses the check naturally — `Test-NotificationSuppressed` returns false when the recorded
    # $State[$Key] does not equal the new $Signature — so recording the failed signature never
    # blocks a new-signature alert from firing. Skipping the record produced the exact spam loop
    # this path exists to prevent: same alert, same signature, 400 every 5 minutes, forever.
    #
    # The msg-2013 §3(b) concern ("永久に失われる") that D-3 was addressing is already covered by
    # msg-2099 D-4: the digest is a state sync and re-lists every currently-waiting thread daily,
    # so a "lost" delta alert re-surfaces in the digest regardless of what notified.json records.
    # ∴ ALWAYS record. D-3 is superseded on this specific point; the letter of D-3 predicted an
    # asymmetry that the map-shape does not actually create. (msg-2013 → msg-2099 → PR-gate review.)
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

# How long the wrapper waits for the CLI. 60s is generous for the stub (measured <1s) and gives S3's
# LLM-backed composer a workable ceiling. On timeout the wrapper KILLS the process and returns $null
# — I-2 says a stuck composer must never delay the raw ping.
#
# D-45 (Tier-C msg §25.2): originally 30s. Raised to 60s after A-18 measured 33,812 ms end-to-end on
# a live parked thread (tail 6 msgs / 21,026 chars). Composer failure fails-open through I-2, so the
# ceiling only bounds how long the wrapper waits before falling back to the raw ping — measured
# 8-11h human response latency dwarfs the extra 30s. The three sites (this constant, the Python
# DEFAULT_TIMEOUT_SECONDS, and the CLI's --timeout-seconds default) MUST stay in sync.
$DecisionComposerTimeoutSeconds = 60

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
# code. Path composition (`/dashboard/decisions/<project>/<thread>` for the human link,
# `/v1/decisions/<project>/<thread>/material` for the magickit PUT) lives in New-DecisionLink /
# New-MaterialUrl below — a URL-shape change is one edit in one place, not a search across the
# wrapper.
$DecisionDashboardBaseUrl = if ($env:MINDWIRE_DECISION_DASHBOARD_URL) {
    $env:MINDWIRE_DECISION_DASHBOARD_URL.TrimEnd('/')
} else { 'https://sg-ai-server-01.taile861db.ts.net:8443' }

# --- decision-material push (T-decision-material-push, msg-1445) -------------------------------
# The material push (mindwire composer → magickit `/v1/decisions/.../material`) shares a base URL,
# a {project}/{thread_id} encoding, and a dedup predicate with the human-facing Discord link. The
# functions below hold I-17: the link in the notification and the PUT target are BUILT FROM THE SAME
# VARIABLES, in one place. A second independent assembly would let the human open page X while the
# material lands at Y and the page correctly shows "material absent" — the exact split the composer
# was written to close.
#
# Wire measurements from sg-tomtebo-01 (M-1, msg-1445 §6):
#   * Invoke-WebRequest → https://sg-ai-server-01.taile861db.ts.net:8443/... returns 404 in ~190 ms
#     with the wire proxy DISABLED (the tailnet cert validates under the default TLS handler and
#     pwsh has outbound permission for that host).
#   * With `-Proxy http://127.0.0.1:3128` the same request is refused by squid (403). Do NOT
#     thread the notification proxy in here.
# ∴ -TimeoutSec 10 is >50× the observed RTT, and the request goes direct.
#
# The push is fail-open (D-34, msg-1443 §3): every failure — HTTP 4xx/5xx, TLS, DNS, connect refused,
# timeout — writes ONE log line and returns to the caller. The notification then fires REGARDLESS
# of the PUT's fate. A material-side outage must not turn into a notification-side outage; that
# would trade an easy-to-see problem (dashboard says "material not stored") for the exact problem
# the composer exists to end (nobody knows they are being asked).

# How long the wrapper waits for the material PUT. Kept small (10 s) because a slow PUT would delay
# the notification that follows; the notification is the "someone is being asked" signal and must
# not wait on the material store. Measured RTT (M-1): ~190 ms round trip for a fresh connection —
# a ceiling of 10 s allows for a 50× stall before the wrapper gives up and moves on. If a live
# measurement ever comes back over 2 s, that is the signal that the assumption behind this constant
# is wrong; raise the alarm before raising the ceiling.
$DecisionMaterialTimeoutSeconds = 10

# Build the URL a human will open in Discord. Same {project}/{thread_id} encoding as the PUT target;
# see New-MaterialUrl. The single source of truth for the D-29 dashboard link.
function New-DecisionLink {
    param([string]$Project, [string]$ThreadId)
    $encodedProject = [uri]::EscapeDataString($Project)
    $encodedThread = [uri]::EscapeDataString($ThreadId)
    return "$DecisionDashboardBaseUrl/dashboard/decisions/$encodedProject/$encodedThread"
}

# Build the URL the wrapper PUTs the material to. Uses the SAME base URL and the SAME
# EscapeDataString call as New-DecisionLink; the two must never diverge (I-17). If a future
# environment splits the material API off onto a different host, the split is one variable
# (add a `$DecisionMaterialBaseUrl` env override here), not two independent assemblies.
function New-MaterialUrl {
    param([string]$Project, [string]$ThreadId)
    $encodedProject = [uri]::EscapeDataString($Project)
    $encodedThread = [uri]::EscapeDataString($ThreadId)
    return "$DecisionDashboardBaseUrl/v1/decisions/$encodedProject/$encodedThread/material"
}

# The SEAM the tests override. Isolated for the same reason Invoke-ComposerCli is: a real HTTP
# request cannot run inside pytest / Test-DecisionComposerWiring, and reaching for the network from
# inside the higher-level Push-DecisionMaterial would make its unit-test coverage impossible.
#
# Return shape mirrors the composer's: `@{ ok=$bool; status=$int|$null; body=$string|$null;
# elapsed_ms=$int; error=$string|$null }`. Never throws — the caller (Push-DecisionMaterial) is
# fail-open by design, and any exception escaping this seam is a wiring bug, not a runtime failure
# the caller should handle.
function Invoke-MaterialPut {
    param(
        [string]$Url,
        [string]$BodyJson,
        [int]$TimeoutSec = $DecisionMaterialTimeoutSeconds
    )
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        # UTF-8 bytes on purpose: the body carries Japanese and .NET's default encoding on this host
        # is not UTF-8. Same pattern as Send-Notification below.
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($BodyJson)
        # -SkipHttpErrorCheck so a 4xx / 5xx returns a response object instead of throwing — the
        # caller wants to log the status code, not a "The remote server returned an error" wrapper.
        # Not routed through $notifyProxy: M-1 confirmed the tailnet host is reachable directly and
        # squid denies the tunnel. Keep this call OUT of the notification proxy path.
        $resp = Invoke-WebRequest -Uri $Url -Method Put `
            -ContentType 'application/json; charset=utf-8' `
            -Body $bytes -TimeoutSec $TimeoutSec `
            -SkipHttpErrorCheck -ErrorAction Stop
        $sw.Stop()
        $status = [int]$resp.StatusCode
        $body = if ($resp.Content) { "$($resp.Content)" } else { '' }
        $ok = ($status -ge 200 -and $status -lt 300)
        return @{
            ok = $ok
            status = $status
            body = $body
            elapsed_ms = [int]$sw.ElapsedMilliseconds
            error = if ($ok) { $null } else { "HTTP $status" }
        }
    }
    catch {
        $sw.Stop()
        return @{
            ok = $false
            status = $null
            body = $null
            elapsed_ms = [int]$sw.ElapsedMilliseconds
            error = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
        }
    }
}

# Read one named field off an envelope-shaped object, returning $null on absence. Handles BOTH the
# JSON-loaded shape (PSCustomObject; all wrapper-facing production envelopes come from
# `ConvertFrom-Json` in Invoke-ComposerCli) AND the freshly-written / test-authored shape
# (hashtable or [ordered]@{}). The two shapes are exchangeable in the sweep — a value written into
# a state map as a hashtable and later re-read from disk comes back as a PSCustomObject — so any
# reader that only understands one of them silently drops fields for the other. That drop is exactly
# the bug PR #171 pre-merge review flagged: `.PSObject.Properties.Name` on a hashtable returns
# ["Count","IsFixedSize","IsReadOnly","Keys","SyncRoot",…] (properties of the hashtable OBJECT, not
# its keys), so a `-contains 'question'` check on a hashtable envelope returns $false and the PUT
# body ships without the material fields. `IDictionary` covers both `[hashtable]` and
# `[System.Collections.Specialized.OrderedDictionary]` (`[ordered]@{}`).
function Get-EnvelopeField {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
        return $null
    }
    if ($Object.PSObject.Properties.Name -contains $Name) { return $Object.$Name }
    return $null
}

# Extract the head msg id the composer *actually read* (see cli.py: `extras.head_msg_id_read`).
# Duck-types both the freshly-written hashtable shape and the JSON-loaded PSCustomObject shape,
# same as Get-CachedDecision.
#
# DM-4 forbids fallback to `last_msg_id` (which is the conductor stop line, not what the composer
# read). If `head_msg_id_read` is missing, this returns $null and the caller (Push-DecisionMaterial)
# SKIPS the PUT — "material absent" (J-absent) is the safe display, "read from something we did not
# read" would defeat the receiver's freshness check.
function Get-ComposerReadHead {
    param($Envelope)
    $extras = Get-EnvelopeField -Object $Envelope -Name 'extras'
    $val = Get-EnvelopeField -Object $extras -Name 'head_msg_id_read'
    if (-not $val) { return $null }
    return "$val"
}

# Push the composer's material to magickit's `/v1/decisions/{project}/{thread_id}/material`.
# Never throws, never blocks the notification path. Returns the raw Invoke-MaterialPut result for
# the test seam; the caller ignores the return value in production (D-34: the notification body
# does NOT branch on the PUT's fate).
#
# Log lines (DM-5, msg-1445 §3): every branch produces exactly one line and calls Confirm-
# LogWorthKeeping so nothing gets dropped by the idle-tick collapse. Textually pinned so an
# operator grep can tell success from a skip from a failure.
function Push-DecisionMaterial {
    param(
        [hashtable]$NotifyState,
        [string]$Key,
        [string]$Signature,
        [string]$Project,
        [string]$ThreadId,
        $Envelope
    )

    # DM-3: same dedup predicate as the notification. A same-signature repeat tick must not
    # re-fire the PUT any more than it re-fires the notification. Without this gate a driven-by-
    # tick 5-minute cadence would hammer the receiver with an unbounded PUT stream on every parked
    # thread — the YAGNI-rejected "retry queue" fallen in through the back door.
    if (Test-NotificationSuppressed -State $NotifyState -Key $Key -Signature $Signature) {
        # Silent-by-design: the same tick will also suppress the notification below, and we already
        # logged this same signature once. Adding another line would double the log volume on every
        # parked-thread tick.
        return $null
    }

    if ($null -eq $Envelope) {
        # No composer output at all — the wrapper will fire the raw ping. Do not PUT; there is
        # nothing to PUT.
        return $null
    }

    # Every field access below goes through Get-EnvelopeField so a hashtable envelope (from a
    # freshly-written cache row) is read identically to a PSCustomObject envelope (from a JSON
    # round-trip). Reading these with `.PSObject.Properties.Name` on a hashtable silently returns
    # $false for every material key — PR #171 pre-merge review flagged exactly that regression on
    # this function's earlier version.
    $status = Get-EnvelopeField -Object $Envelope -Name 'composer_status'
    $status = if ($null -ne $status) { "$status" } else { $null }
    # DM-6: magickit's S5 slice §1.3 says a `composer_status != "ok"` PUT is rejected with 400 and
    # no partial save. Sending it produces a guaranteed 400 that does nothing useful. Skip cleanly.
    if ($status -and $status -ne 'ok') {
        Confirm-LogWorthKeeping
        Write-Log "material push skipped: $Key — composer_status=$status ∴ 材料なし"
        return $null
    }

    # DM-4 (I-16): the head msg id must be the one the composer *actually read*. If missing (e.g.
    # tail_fetch_error path), skip — do NOT fall back to `last_msg_id` (that would claim a head the
    # composer never observed, which the receiver cannot detect).
    $head = Get-ComposerReadHead -Envelope $Envelope
    if (-not $head) {
        Confirm-LogWorthKeeping
        Write-Log "material push skipped: $Key — composer が読んだ head が不明 (extras.head_msg_id_read 無し) ∴ 材料を送らない (ページは J-absent)"
        return $null
    }

    # Build the PUT body. Follows magickit's S5 slice §1.1 field table verbatim. All fields except
    # head_msg_id are optional; we send whatever the envelope carries.
    $output = Get-EnvelopeField -Object $Envelope -Name 'output'

    # DM-6 second half (msg-1445 §DM-6): "composer_status != ok / output 無しには PUT しない".
    # The Python-side value-object validator refuses to construct a DecisionRequestEnvelope with
    # composer_status=ok AND output=None (see value_objects.py post_init), so this branch fires
    # ONLY on a pathologically shaped envelope (hand-edit, corrupt JSON re-read, future-refactor
    # bug). Sending it anyway would either land on the receiver as a valid empty material
    # (making the page render J-fresh with an empty prompt, worse than J-absent) or produce a
    # 400 the sweep would then re-generate on every parked-thread tick until the signature
    # advances. DM-6's headline explicitly rules it out — the wrapper enforces both halves, not
    # just the composer_status half.
    if ($null -eq $output) {
        Confirm-LogWorthKeeping
        Write-Log "material push skipped: $Key — envelope に output が無い (composer_status=ok だが output=null) ∴ 材料を送らない"
        return $null
    }

    # $output is guaranteed non-null here by the DM-6 guard above.
    $question             = Get-EnvelopeField -Object $output -Name 'question'
    $rawOptions           = Get-EnvelopeField -Object $output -Name 'options'
    $recommendation       = Get-EnvelopeField -Object $output -Name 'recommendation'
    $recommendationReason = Get-EnvelopeField -Object $output -Name 'recommendation_reason'
    $rawUnknowns          = Get-EnvelopeField -Object $output -Name 'unknowns'

    $options = if ($null -eq $rawOptions) { @() } else { @($rawOptions) }
    $unknowns = if ($null -eq $rawUnknowns) { @() } else { @($rawUnknowns) }

    $body = [ordered]@{
        head_msg_id     = $head
        signature       = $Signature
        composer_status = 'ok'
    }
    # PR #171 pre-merge review round 2: the guard here must NOT use `if ($x)`. PowerShell
    # evaluates the string literal `"0"` as $false under implicit boolean cast, so a composer
    # output where `question` or `recommendation` or `recommendation_reason` equals "0"
    # would be silently DROPPED from the PUT body — J-fresh would render an empty question,
    # or the recommendation card would vanish. `[string]::IsNullOrEmpty` treats "0" as a
    # non-empty string, which is what "present" actually means here. The Python composer's
    # own validator (`DecisionRequestOutput`) rejects empty / whitespace-only questions and
    # requires a non-empty reason when there is a recommendation, so we cannot silently lose
    # those on the wire.
    #
    # Note: `if (-not $head)` above is intentionally left alone — msg ids in this repo carry
    # the "msg-" prefix (e.g. "msg-2702"), so the string "0" is not a reachable value for a
    # head msg id. The trap applies where a payload string could plausibly BE "0".
    if (-not [string]::IsNullOrEmpty($question)) { $body['question'] = "$question" }
    if ($options.Count -gt 0) {
        $body['options'] = @($options | ForEach-Object {
            # Same duck-typing dance for each option element — a hashtable-authored envelope
            # will have hashtable options too, and stripping gain / loss to '' silently would
            # let J-fresh render option cards without the trade-off text the operator needs.
            $optId    = Get-EnvelopeField -Object $_ -Name 'id'
            $optLabel = Get-EnvelopeField -Object $_ -Name 'label'
            $optGain  = Get-EnvelopeField -Object $_ -Name 'gain'
            $optLoss  = Get-EnvelopeField -Object $_ -Name 'loss'
            [ordered]@{
                id    = "$optId"
                label = "$optLabel"
                gain  = if ($null -ne $optGain) { "$optGain" } else { '' }
                loss  = if ($null -ne $optLoss) { "$optLoss" } else { '' }
            }
        })
    }
    if (-not [string]::IsNullOrEmpty($recommendation)) { $body['recommendation'] = "$recommendation" }
    if (-not [string]::IsNullOrEmpty($recommendationReason)) { $body['recommendation_reason'] = "$recommendationReason" }
    if ($unknowns.Count -gt 0) { $body['unknowns'] = @($unknowns | ForEach-Object { "$_" }) }

    $bodyJson = $body | ConvertTo-Json -Depth 6 -Compress
    $url = New-MaterialUrl -Project $Project -ThreadId $ThreadId
    $link = New-DecisionLink -Project $Project -ThreadId $ThreadId

    # Belt and braces (msg-1445 §DM-5): Invoke-MaterialPut already promises never to throw, but the
    # caller catches too so a wiring bug in the seam cannot sink the notification path. Fail-open
    # is the whole point of D-34; a single try-catch here is the physical barrier that makes it so.
    try {
        $result = Invoke-MaterialPut -Url $url -BodyJson $bodyJson
    }
    catch {
        $result = @{
            ok = $false; status = $null; body = $null; elapsed_ms = 0
            error = "unhandled from Invoke-MaterialPut: $($_.Exception.GetType().Name): $($_.Exception.Message)"
        }
    }
    Confirm-LogWorthKeeping
    if ($result.ok) {
        $replaced = 'unknown'
        if ($result.body) {
            try {
                $parsed = $result.body | ConvertFrom-Json -ErrorAction Stop
                if ($parsed.PSObject.Properties.Name -contains 'replaced') { $replaced = "$($parsed.replaced)" }
            }
            catch { }
        }
        Write-Log "material push: $Key head=$head replaced=$replaced ($($result.elapsed_ms) ms) url=$url link=$link"
    }
    elseif ($result.status) {
        $bodySnippet = if ($result.body) {
            $flat = ($result.body -replace "\s+", ' ').Trim()
            if ($flat.Length -gt 120) { $flat.Substring(0, 120) + '…' } else { $flat }
        } else { '<no body>' }
        Write-Log "material push FAILED (non-fatal): $Key head=$head — HTTP $($result.status) $bodySnippet — 通知は継続"
    }
    else {
        Write-Log "material push FAILED (non-fatal): $Key head=$head — $($result.error) — 通知は継続"
    }
    return $result
}

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
        [int]$TimeoutSeconds = $DecisionComposerTimeoutSeconds,
        [int]$TailCount = 0
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
    # S3 spec D-38: pass --tail N when the caller asks for it (currently: only the claude-code
    # backend does). The stub backend explicitly leaves TailCount=0 so its existing behaviour
    # (payload tail is what the CLI sees) is untouched. Kept as a caller-supplied parameter rather
    # than an in-function branch on $Backend so the seam stays testable without threading the
    # backend through Format-DecisionMessage etc.
    $tailArg = if ($TailCount -gt 0) { " --tail $TailCount" } else { '' }
    $psi.Arguments = "run mindwire-compose-decision --backend $Backend --identity `"$Identity`"$tailArg"
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

# The pending-decisions cache. Same JSON shape as notified.json / evaluated.json, keyed by the sweep's
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

    # S3 spec D-38: only claude-code fetches the tail via chatroom_get_thread (Python side, per
    # D-36). The stub keeps its empty-payload path unchanged. If a future backend also wants a
    # fetched tail, add it here — the CLI's --tail flag is the single shared knob.
    $tailForBackend = if ($DecisionComposerBackend -eq 'claude-code') { $DecisionComposerTailLimit } else { 0 }

    $result = Invoke-ComposerCli -InputJson $inputJson `
        -Backend $DecisionComposerBackend -Identity $DecisionComposerIdentity `
        -TimeoutSeconds $DecisionComposerTimeoutSeconds `
        -TailCount $tailForBackend
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
    # I-17 (T-decision-material-push §DM-2): the link is built by the SAME function the material
    # PUT target is built with (New-DecisionLink / New-MaterialUrl share $DecisionDashboardBaseUrl
    # and the same EscapeDataString call). Do NOT re-build the link inline here.
    $link = New-DecisionLink -Project $Project -ThreadId $ThreadId
    # Header phrasing per T-park-alert-says-judgement-when-it-is-a-fault msg-1465 §2: pick the
    # phrase for the actual stop reason from the SOT map in deploy/lib/StopReason.ps1 instead of
    # the fixed "判断待ち" literal. Every other field on this line is preserved byte-for-byte.
    $header = New-NotificationHeader -ThreadId $ThreadId -Project $Project `
        -StopReason $StopReason -Rounds $Rounds -LastMsgId $LastMsgId
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

# The full "the loop is parked on a human decision" sequence, extracted from the sweep tick so the
# order (material PUT → notification) and the fail-open behaviour (a broken PUT never suppresses
# the notification) are testable in isolation (msg-1445 §5 / W-3). Inlined, this sequence is
# unreachable from the AST-lift test harness — the only way to pin that the PUT precedes the
# notification and that the notification body is 1 character identical whether the PUT threw or
# returned 4xx or 5xx is to make the sequence a named function.
#
# Contract (msg-1443 §3 D-34 / msg-1445 §DM-1 / §DM-5):
#   1. Compose (or reuse the cached) envelope for this ({Key}, {Signature}).
#   2. Push the material to magickit — non-blocking, fail-open, its result is ignored.
#   3. Format the Discord body from the envelope. If enrichment fails for any reason, use the
#      $RawFallback the caller built.
#   4. Send-NotificationIfChanged: the caller sees exactly the body the enrichment produced,
#      whether the PUT succeeded, failed, or was skipped for freshness.
#
# The dedup on step 2 is `Test-NotificationSuppressed` (the SAME predicate step 4 consults) —
# without it the PUT would fire on every 5-minute tick against a driven-by-human-response wait,
# which is the "無設計の再試行" msg-1445 §DM-3 rules out.
function Send-HumanParkAlert {
    param(
        [hashtable]$PendingDecisionsState,
        [hashtable]$NotifyState,
        [string]$Key,
        [string]$Project,
        [string]$ThreadId,
        [string]$Signature,
        [string]$LastMsgId,
        [string]$StopReason,
        [int]$Rounds,
        [string]$RawFallback
    )

    $envelope = Get-DecisionEnvelope -State $PendingDecisionsState `
        -Key $Key -Project $Project -ThreadId $ThreadId `
        -Signature $Signature -LastMsgId $LastMsgId `
        -StopReason $StopReason -Rounds $Rounds

    # STEP 2 (D-34: ①→②) — material PUT BEFORE the notification. Its failure is logged and
    # discarded; the notification body below does NOT branch on it.
    $null = Push-DecisionMaterial -NotifyState $NotifyState -Key $Key -Signature $Signature `
        -Project $Project -ThreadId $ThreadId -Envelope $envelope

    $enriched = Format-DecisionMessage -Project $Project -ThreadId $ThreadId `
        -StopReason $StopReason -Rounds $Rounds `
        -LastMsgId $LastMsgId -RawFallback $RawFallback -Envelope $envelope
    $message = if ($enriched) { $enriched } else { $RawFallback }

    Send-NotificationIfChanged -State $NotifyState -Key $Key `
        -Signature $Signature -Message $message
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
        # Reject `$null`, `""`, AND whitespace-only strings uniformly and loudly. PowerShell's `-not`
        # operator handles the first two but treats `" "` as truthy, which would silently pass a bad
        # value downstream. `IsNullOrWhiteSpace` closes that hole here — at the single validation
        # point — so every consumer of a candidate can trust that all three required fields are
        # non-blank. This is the load-bearing check for the CWD-probing attack path the T-new-
        # project-gate-bootstrap PR-gate identified on PR #192: an empty/whitespace `repo_dir` that
        # reached `gate_bootstrap_tick.py` would resolve `Path("") / ".mindwire-gate"` against the
        # sweep's CWD (this MindWire host repo) and falsely report `DECLARED`. The Python side of
        # the same defence lives in `inspect_gate`, which returns `UNUSABLE` for a non-absolute
        # `repo_dir` regardless of caller — but this upstream check is what makes the failure mode
        # uniform: a broken sweep config halts the sweep, it does not silently skip candidates.
        foreach ($f in 'project', 'thread_id', 'repo_dir') {
            if ([string]::IsNullOrWhiteSpace([string]$c.$f)) {
                throw "sweep config entry missing or blank '$f': $($c | ConvertTo-Json -Compress)"
            }
        }
        # `repo_dir` MUST be an absolute path — this is the contract documented in
        # `deploy/sweep.json.example` ("repo_dir is the implementer's own CLONE for that project").
        # A relative value like `"some/relative/path"` would pass the blank check above and reach
        # `gate_bootstrap_tick.py`, where `inspect_gate` correctly returns `UNUSABLE` — but a
        # UNUSABLE verdict is a silent SKIP of that candidate, not a loud halt. The T-new-project-
        # gate-bootstrap PR-gate identified this asymmetry: if the goal is uniform failure modes,
        # the PowerShell side must enforce the same absolute-path contract the Python side does,
        # so a broken config `throw`s from `Get-SweepCandidates` instead of quietly disappearing
        # into the fail-closed Python branch.
        #
        # `IsPathFullyQualified` is the correct predicate here (not `IsPathRooted`, which the
        # earlier revision used): `IsPathRooted` returns `$true` for drive-relative paths like
        # `C:foo` (drive but no root) and root-relative paths like `\foo` (root but no drive),
        # both of which Python's `pathlib.PureWindowsPath.is_absolute()` correctly rejects.
        # Managing the same "must be absolute" rule with two different definitions is exactly the
        # dual-management asymmetry the PR-gate flagged (2026-08-29 review #3). Empirically
        # verified on pwsh 7 / .NET on the daemon host: `IsPathFullyQualified` returns `False`
        # for `'C:foo'`, `'\relative\path'`, `''`, `' '`, `'./relative'` — matching Python
        # `PureWindowsPath.is_absolute()` for each. The shebang (`#!/usr/bin/env pwsh`) and the
        # Task Scheduler invocation both pin PowerShell 7+, where `IsPathFullyQualified` is
        # available (added in .NET Core 2.1).
        if (-not [System.IO.Path]::IsPathFullyQualified([string]$c.repo_dir)) {
            throw ("sweep config entry has non-absolute 'repo_dir': $($c | ConvertTo-Json -Compress) " +
                   "— use a fully-qualified absolute path (see deploy/sweep.json.example).")
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

# --- parked-humans probe (T-decision-request-composer S4 / D-32) --------------------------------
# Poll the sweep candidates and return the subset currently parked on a human decision. Delegates
# to ``scripts/parked_humans.py``, which re-uses the ``spirrow_mindwire.conductor.handoff``
# parser (msg-1391 §13.2 / §13.3 for the D-32 grammar-ownership rule): a previous S4 (commit
# 31a0373, since reverted) derived parking from notified.json signatures — a *derived* record,
# not a judgment — and Einstein correctly rejected it as a D-24' violation. This wrapper's
# ONLY job is to shell out; the parking answer is produced Python-side, by the module that owns
# the ``NEXT:`` grammar. There is NO regex in this file.
#
# Input: list of candidates for one project ($Project + array of @{ thread_id; head_msg_id }).
# Output: a hashtable @{ parked = [...]; errors = [...]; polled = <int> } — order-preserved from
# the input. Each parked entry has { thread_id; head_msg_id; token }. A whole-poll failure
# (script missing, non-zero exit, unparseable JSON) is treated as fail-CLOSED on the parked side:
# no parked entries, but an error row so the digest never silently blanks. This is opposite to
# Invoke-HeadProbe's fail-OPEN — same as msg-1391 §13.3 (different question, different safe
# answer).
function Invoke-ParkedHumansProbe {
    param(
        [string]$Project,
        [array]$Candidates,
        [hashtable]$HeadsByProject
    )

    $empty = @{ parked = @(); errors = @(); polled = 0 }

    $probe = Join-Path $repoRoot "scripts\parked_humans.py"
    if (-not (Test-Path -LiteralPath $probe)) {
        Write-Log "parked-humans probe not found at $probe — treating as no-parked (fail-closed, digest under-reports)"
        return $empty
    }

    # Build the per-project candidate list. head_msg_id comes from the head probe when we have
    # it; empty string when the probe did not report the thread (parked_humans.py skips the
    # cross-check in that case and trusts the fetched head). All-or-nothing: an empty candidate
    # list means "no work for this project" and the probe returns polled=0 immediately.
    $projectCands = @($Candidates | Where-Object { $_.project -eq $Project })
    if ($projectCands.Count -eq 0) { return $empty }

    $heads = if ($HeadsByProject.ContainsKey($Project)) { $HeadsByProject[$Project] } else { $null }
    $items = @()
    foreach ($c in $projectCands) {
        $hid = ''
        if ($null -ne $heads -and $heads.ContainsKey($c.thread_id)) { $hid = "$($heads[$c.thread_id])" }
        $items += @{ thread_id = "$($c.thread_id)"; head_msg_id = $hid }
    }
    $payload = @{ candidates = $items } | ConvertTo-Json -Depth 5 -Compress

    try {
        Push-Location $repoRoot
        try {
            # Same invocation pattern as Invoke-HeadProbe: run under `uv run` from the repo root
            # so the module import resolves against this checkout's environment.
            $raw = $payload | & uv run python $probe --project $Project 2>&1
            $code = $LASTEXITCODE
        }
        finally { Pop-Location }
    }
    catch {
        Write-Log "parked-humans probe [$Project] threw ($($_.Exception.Message)) — treating as no-parked (fail-closed)"
        return @{ parked = @(); errors = @(@{ thread_id = '__probe__'; reason = "invocation failed: $($_.Exception.Message)" }); polled = 0 }
    }

    if ($code -ne 0) {
        $tail = ($raw | ForEach-Object { "$_" }) -join ' / '
        Write-Log "parked-humans probe [$Project] exited $code — treating as no-parked. Output: $tail"
        return @{ parked = @(); errors = @(@{ thread_id = '__probe__'; reason = "exit=${code}: $tail" }); polled = 0 }
    }

    $json = $raw | ForEach-Object { "$_" } | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
    if (-not $json) {
        Write-Log "parked-humans probe [$Project] produced no JSON — treating as no-parked"
        return @{ parked = @(); errors = @(@{ thread_id = '__probe__'; reason = 'no JSON on stdout' }); polled = 0 }
    }

    try {
        $obj = $json | ConvertFrom-Json
    }
    catch {
        Write-Log "parked-humans probe [$Project] JSON unparseable ($($_.Exception.Message)) — treating as no-parked"
        return @{ parked = @(); errors = @(@{ thread_id = '__probe__'; reason = 'JSON unparseable' }); polled = 0 }
    }

    # Convert each parked entry into a wrapper-side record. Add the composite key
    # ("project/thread_id") the digest and log line index by, so the downstream code does not
    # have to reconstruct it.
    $parkedOut = @()
    foreach ($p in @($obj.parked)) {
        $parkedOut += [PSCustomObject]@{
            key         = "$Project/$($p.thread_id)"
            project     = $Project
            thread_id   = "$($p.thread_id)"
            head_msg_id = "$($p.head_msg_id)"
            token       = "$($p.token)"
        }
    }
    $errorsOut = @()
    foreach ($e in @($obj.errors)) {
        $errorsOut += [PSCustomObject]@{
            project   = $Project
            thread_id = "$($e.thread_id)"
            reason    = "$($e.reason)"
        }
    }
    $polled = if ($obj.PSObject.Properties.Name -contains 'polled') { [int]$obj.polled } else { $projectCands.Count }
    return @{ parked = $parkedOut; errors = $errorsOut; polled = $polled }
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

# --- gate-bootstrap tick -------------------------------------------------------------------------
# For each distinct (project, repo_dir) in the sweep list, delegate to
# scripts/gate_bootstrap_tick.py — the LLM-free predicate that opens
# `T-gate-bootstrap-<project>` idempotently when `.mindwire-gate` is not declared,
# and takes it back down (also idempotently) once it is.
#
# The full design lives in the chatroom thread `T-new-project-gate-bootstrap`
# (msg-1962 request; msg-1963/1965/1967 design; msg-1964/1966/1968 naysayer);
# see the module docstring on `src/spirrow_mindwire/gate_bootstrap.py` for the
# summary.
#
# Fail-open on the SWEEP. A broken gate-bootstrap tick MUST NOT stop the main sweep — the
# alert-opener is a nice-to-have; the sweep itself is load-bearing. Non-zero exits from the
# probe are logged and the loop moves on. This matches Invoke-ControlProbe's fail-open contract
# for the same reason: the main sweep is what actually runs conductors.
function Invoke-GateBootstrapTick {
    param([string]$Project, [string]$RepoDir)

    $probe = Join-Path $repoRoot "scripts\gate_bootstrap_tick.py"
    if (-not (Test-Path -LiteralPath $probe)) {
        Write-Log "gate-bootstrap probe not found at $probe — skipping (alert-opener is best-effort)"
        return $null
    }
    try {
        Push-Location $repoRoot
        try {
            $raw = & uv run python $probe --project $Project --repo-dir $RepoDir 2>&1
            $code = $LASTEXITCODE
        }
        finally { Pop-Location }

        $json = $raw | ForEach-Object { "$_" } | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
        if (-not $json) {
            Write-Log "gate-bootstrap [$Project]: no JSON on stdout (exit=$code) — failing open"
            return $null
        }
        $obj = $json | ConvertFrom-Json
        if ($code -ne 0) {
            # Non-zero is a magickit failure (open / close). Log the reason; the next tick retries.
            Write-Log "gate-bootstrap [$Project]: status=$($obj.status) action=$($obj.action) error=$($obj.error)"
        }
        else {
            Write-Log "gate-bootstrap [$Project]: status=$($obj.status) action=$($obj.action) reason=$($obj.reason)"
        }
        return $obj
    }
    catch {
        Write-Log "gate-bootstrap [$Project] failed ($($_.Exception.Message)) — sweep continues (fail-open)"
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

    # Gate-bootstrap tick, once per distinct (project, repo_dir). Runs BEFORE the main sweep so the
    # alert thread is open by the time the first candidate on a fresh project actually gets picked up.
    # Fail-open by design (Invoke-GateBootstrapTick swallows every kind of local failure and returns
    # $null): a broken alert-opener must not stop the main sweep, which is what actually runs conductors.
    #
    # `repo_dir` is guaranteed non-blank AND fully-qualified absolute here because
    # `Get-SweepCandidates` validates every required field with `IsNullOrWhiteSpace` and then
    # applies `IsPathFullyQualified` to `repo_dir` specifically — both `throw` loudly on the first
    # offender. That is the single validation point for sweep-config well-formedness; do NOT add a
    # silent `continue` here to tolerate what should have been rejected upstream — a broken config
    # must halt the sweep, not cause candidates to vanish without a log line. The PowerShell
    # `IsPathFullyQualified` and Python `PureWindowsPath.is_absolute()` predicates were verified
    # to agree on the same set of accepted paths, so a sweep-driven caller cannot reach Python's
    # fail-closed `UNUSABLE` branch — that branch is now only reachable by non-sweep callers (test
    # harness, ad-hoc CLI use), which is what the Python-side defence is for.
    $gateBootstrapPairs = @{}
    foreach ($c in $candidates) {
        $pairKey = "$($c.project)::$($c.repo_dir)"
        if (-not $gateBootstrapPairs.ContainsKey($pairKey)) {
            $gateBootstrapPairs[$pairKey] = @{ project = $c.project; repo_dir = $c.repo_dir }
        }
    }
    foreach ($pair in $gateBootstrapPairs.Values) {
        [void](Invoke-GateBootstrapTick -Project $pair.project -RepoDir $pair.repo_dir)
    }

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
    $quarantineState = Get-JsonState -Path $quarantineStatePath
    $evaluatedState = Get-JsonState -Path $evaluatedStatePath
    $digestState = Get-JsonState -Path $digestStatePath
    # Snapshot the keys present at sweep start. Used by Merge-StateForWrite at flush time to
    # distinguish "operator removed this during the sweep" (must not resurrect) from "sweep never
    # touched this" (already-agreed value, keep). See Merge-StateForWrite's header for the full
    # contract. Only quarantine.json needs this — the head-skip state file has a single writer
    # (scripts/head_skip_decide.py) and is never edited by an operator during a sweep.
    $quarantineOriginalKeys = @($quarantineState.Keys)
    $nowUtc = [DateTime]::UtcNow

    # --- head-skip decide batch (Bohr msg-1430 §W-2) --------------------------------------------
    # One `--mode decide` invocation per project, over the candidates that have NOT already been
    # excluded by hold / quarantine. Held projects (the ones the loop has acknowledged) do not
    # need a decide at all — every candidate on them is dropped in the candidate loop below.
    # Verdicts are collected into a single map keyed by the "$project/$thread_id" sweep key so
    # the candidate loop can look one up without knowing which project it came from.
    #
    # W-3 layer 2: an `ok=$false` return is a SYSTEMIC failure (interpreter missing, decide
    # crashed, output not JSON). It THROWS out of the try{} above, which lands in the wrapper's
    # top-level catch → $exitCode=1 → tick aborts with no state writes. If `decide` is broken,
    # `commit-launch` is broken too (same interpreter, same module), and spawning a conductor
    # anyway would launch without recording — bypassing backoff entirely.
    #
    # W-2c: every candidate that got a decide verdict this tick (LAUNCH / DEFER / SKIP) is
    # counted as an evaluation for the starvation clock. Refreshed in a dedicated pass below,
    # before the launch loop, so a candidate that we then choose NOT to spawn (a DEFER, or a
    # LAUNCH-verdict candidate that arrives after the tick's first LAUNCH) still has its clock
    # updated. Without W-2c, the new SKIP verdicts (stop-token = human / none) would be
    # structurally invisible to the starvation metric and re-play msg-1427 §1's 12-hour silence
    # on the metric layer.
    $headSkipMode = Get-HeadSkipMode
    $decideVerdicts = @{}
    foreach ($proj in ($candidates | ForEach-Object { $_.project } | Sort-Object -Unique)) {
        $projControl = $controlByProject[$proj]
        if (Test-HoldObserved -Control $projControl) { continue }
        $projHeads = $headsByProject[$proj]
        $projCands = @($candidates | Where-Object { $_.project -eq $proj })
        # Exclude anything already quarantined — the wrapper does not launch it either way, and
        # asking `decide` about it would mint an observation-only record for a thread we will
        # not launch. Better to leave the CLI's state untouched for a thread we are not driving.
        $eligible = @()
        foreach ($c in $projCands) {
            if ($quarantineState.ContainsKey($c.key)) { continue }
            $hid = if ($null -ne $projHeads -and $projHeads.ContainsKey($c.thread_id)) { "$($projHeads[$c.thread_id])" } else { "" }
            $ctl = if ($null -ne $projControl) { "$($projControl.desired_state)" } else { "" }
            $eligible += @{ thread_id = $c.thread_id; head_msg_id = $hid; control_state = $ctl }
        }
        if ($eligible.Count -eq 0) { continue }
        $decideResult = Invoke-HeadSkipDecide -Project $proj -Candidates $eligible `
            -StateFilePath $headSkipStatePath -Mode $headSkipMode
        if (-not $decideResult.ok) {
            # W-3 layer 2 — fail closed. Any non-zero exit / crash / parse failure lands here.
            Confirm-LogWorthKeeping
            Write-Log "head_skip decide FAILED for project $proj — $($decideResult.error)"
            throw ("head_skip decide systemic failure on project '$proj': $($decideResult.error). " +
                   "The tick is aborted (fail-closed per Bohr msg-1430 §W-3): a broken decide " +
                   "implies a broken commit-launch, and launching conductors without recording " +
                   "them would bypass the backoff CAP.")
        }
        foreach ($tid in $decideResult.verdicts.Keys) {
            $decideVerdicts["$proj/$tid"] = $decideResult.verdicts[$tid]
        }
    }

    # W-2c: refresh the starvation clock for every candidate that received a decide verdict.
    # See Update-EvaluatedTimestamp's header for the full "which dispositions count as evaluated"
    # rule. Doing this BEFORE the launch loop guarantees a DEFER'd or SKIPped candidate cannot
    # slip through — even if a break earlier in the loop stops the sweep short.
    foreach ($k in $decideVerdicts.Keys) {
        Update-EvaluatedTimestamp -State $evaluatedState -Key $k -Now $nowUtc
    }

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
    #
    # The map (both the KEY SET, used below as the notification predicate, and the VALUES, used in
    # the raw-ping fallback body a few lines down) is the SOT in deploy/lib/StopReason.ps1. The
    # header of the ENRICHED notification (Format-DecisionMessage's $header) reads through the same
    # SOT via New-NotificationHeader — see T-park-alert-says-judgement-when-it-is-a-fault msg-1465.
    $needsHuman = Get-StopReasonPhraseMap

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
        # $null when the control probe could not read the state — unknown, so nothing is skipped.
        $currentControl = if ($null -ne $control) { $control.desired_state } else { $null }

        # --- head-skip nomination-predicate verdict lookup (Bohr msg-1430 §W-2) ------------------
        # The batch decide call above populated $decideVerdicts for every non-held, non-quarantined
        # candidate. Its verdict per key is the sole authority for LAUNCH / DEFER / SKIP; the
        # old Test-CanSkip is gone. See head_skip.py for the two-stage judgment (Stage 1 =
        # stop tokens {none, human} → SKIP; Stage 2 = LAUNCH or DEFER, never SKIP).
        $v = if ($decideVerdicts.ContainsKey($cand.key)) { $decideVerdicts[$cand.key] } else { $null }
        if ($null -eq $v) {
            # No verdict for a candidate that was not held / quarantined is a wiring bug —
            # decide's contract is: every eligible candidate we sent, we receive a verdict for.
            # Fail closed rather than silently launch (which would bypass backoff on subsequent
            # ticks: no verdict this tick means no commit-launch, so the record is missing for
            # the next).
            throw "head_skip decide returned no verdict for $($cand.key) — sweep aborted (contract violation)"
        }
        $decision = "$($v.decision)"

        if ($decision -eq 'skip') {
            $skipped++
            $dispositions[$cand.key] = 'head-skipped'
            $sweepSignature += "$($cand.key)=$probeHead"
            # No Update-EvaluatedTimestamp here — the batch pass above (W-2c) already refreshed
            # every decide-visited candidate's clock. Repeating it would be a no-op but the
            # duplication would be a lie: it would suggest the refresh depends on the disposition,
            # which is the exact confusion the batch refresh exists to end.
            Write-Log "candidate $attempt/$($candidates.Count): $($cand.key) — head_skip SKIP (stop-token: $($v.token), head $probeHead), not launching"
            continue
        }
        if ($decision -eq 'defer') {
            $skipped++     # counted as "skipped" for the summary — same operator meaning: "not launching this tick"
            $dispositions[$cand.key] = 'head-deferred'
            # No Update-EvaluatedTimestamp for the same reason as SKIP above (W-2c batch refresh).
            $delaySec = if ($v.PSObject.Properties.Name -contains 'delay_seconds') { [int]$v.delay_seconds } else { 0 }
            $eligibleAt = if ($v.PSObject.Properties.Name -contains 'eligible_at') { $v.eligible_at } else { '' }
            Write-Log "candidate $attempt/$($candidates.Count): $($cand.key) — head_skip DEFER (attempts=$($v.attempts_before), delay=${delaySec}s, eligible_at=$eligibleAt), not launching"
            continue
        }
        # decision must be 'launch' — anything else is a contract violation.
        if ($decision -ne 'launch') {
            throw "head_skip decide returned unexpected decision '$decision' for $($cand.key)"
        }
        # Report mode: dry-run. The CLI touched no state, so we must not commit-launch or spawn.
        # This is how an operator measures "what would come out of a wire commit" per msg-1430 §W-5.
        if ($headSkipMode -eq 'report') {
            $dispositions[$cand.key] = 'report-launch'
            Write-Log "candidate $attempt/$($candidates.Count): $($cand.key) — head_skip LAUNCH (report mode: not spawning, token=$($v.token))"
            continue
        }

        Confirm-LogWorthKeeping
        Write-Log "--- candidate $attempt/$($candidates.Count): $($cand.key) (head_skip LAUNCH, reason=$($v.reason), token=$($v.token)) ---"
        # commit-launch BEFORE spawn: the "session-start-before write" contract that survives a
        # forced kill (head_skip.py docstring, test #10). The record is written with
        # attempts_after=v.attempts_after so the backoff floor applies to any retry, even one after
        # a mid-flight OS-level kill.
        $commitPayload = if ($v.PSObject.Properties.Name -contains 'commit_launch_payload') { $v.commit_launch_payload } else { $null }
        if ($null -eq $commitPayload) {
            throw "head_skip decide returned a LAUNCH verdict without a commit_launch_payload for $($cand.key)"
        }
        $commitResult = Invoke-HeadSkipCommitLaunch -Payload $commitPayload -StateFilePath $headSkipStatePath
        if (-not $commitResult.ok) {
            # Systemic failure of the same class as decide fail-close: if commit-launch is broken
            # then decide is broken too, so we must abort the tick rather than spawn a conductor
            # that will not be recorded.
            Write-Log "head_skip commit-launch FAILED for $($cand.key) — $($commitResult.error)"
            throw ("head_skip commit-launch systemic failure on $($cand.key): $($commitResult.error). " +
                   "The tick is aborted (fail-closed per Bohr msg-1430 §W-3).")
        }

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

        # Note: the batch W-2c refresh above already advanced the starvation clock for every
        # decide-visited candidate (including this one), so there is no per-launch refresh here.
        # first_seen_at is preserved by Update-EvaluatedTimestamp.

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
            $reproHint = Get-QuarantineReproHint -Fingerprint $rec.failure_fingerprint `
                                                 -SessionLogPath $rec.session_log_path `
                                                 -Key $cand.key
            $notificationBody = "MindWire: **$($cand.key)** を隔離しました (exit=$code, reason=$($verdict.reason))。" +
                                "以後この tick からは skip されます。復帰するには " +
                                "``pwsh deploy/Clear-Quarantine.ps1 -Thread '$($cand.key)' -Reason '...'``。" +
                                "ダイジェストにも別掲されます。"
            if ($reproHint) { $notificationBody += "`n$reproHint" }
            Send-NotificationIfChanged -State $notifyState -Key "__quarantine__/$($cand.key)" `
                -Signature "${nowIso}:${code}:$($verdict.reason):${probeHead}" `
                -Message $notificationBody

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

        # The head-skip state file (state\head_skip.json) is written exclusively by the CLI's
        # `--mode commit-launch` call above — the wrapper does NOT double-write it here. Under the
        # new predicate a candidate's launch record is committed BEFORE spawn (and only for
        # candidates the wrapper actually chose to spawn); a "phantom-launched" record for a
        # candidate we did not act on would reproduce the exponential-starvation loop #140 was
        # written to fix. See head_skip_decide.py's docstring for the two-phase protocol.
        $sweepSignature += "$($cand.key)=$($verdict.last_msg)"

        if ($verdict.reason -and $needsHuman.ContainsKey($verdict.reason)) {
            # Signature carries the reason too, so a thread that changes *how* it is stuck re-alerts
            # even when last_msg has not moved.
            $sig = "$($verdict.reason):$($verdict.last_msg)"

            # T-decision-request-composer S2 + T-decision-material-push (msg-1445 §W-3): the whole
            # "the loop is parked on a human decision" sequence is one call. Send-HumanParkAlert
            # owns the order (material PUT → notification) and the fail-open guarantees; keeping
            # the sequence a named function is what makes the two invariants testable in isolation
            # (the AST-lift harness in Test-DecisionComposerWiring.ps1 cannot reach code inlined
            # inside the sweep body).
            $rawFallback = ("MindWire: **$thread** ($($cand.project)) — " +
                            $needsHuman[$verdict.reason] +
                            " (reason=$($verdict.reason), rounds=$($verdict.rounds), $($verdict.last_msg))。" +
                            "chatroom を確認してください。")
            Send-HumanParkAlert -PendingDecisionsState $pendingDecisionsState `
                -NotifyState $notifyState -Key $cand.key `
                -Project $cand.project -ThreadId $thread `
                -Signature $sig -LastMsgId $verdict.last_msg `
                -StopReason $verdict.reason -Rounds $verdict.rounds `
                -RawFallback $rawFallback
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

    # Merge-on-write for quarantine.json — the one state file an operator may edit concurrently
    # with the sweep (via Clear-Quarantine). The merge re-reads disk right before writing, so a
    # mid-sweep Clear-Quarantine survives the sweep's end-of-tick flush. See Merge-StateForWrite's
    # header. evaluated.json is written directly — it has no external writer, and the sweep prunes
    # ex-live keys on it every tick (merge-on-write would resurrect those pruned keys from disk).
    # head_skip.json is not written here at all — its only writer is scripts/head_skip_decide.py
    # (owned atomicity per msg-1430 §W-2), so any concurrency question is answered on the CLI side.
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

    # T-decision-request-composer S4 (D-32). Poll for threads currently parked on a human
    # decision, once per distinct project, and aggregate. Same order as the sweep candidate list
    # (per-project runs preserve order internally). The result feeds BOTH the log line below and
    # the digest section, so the two never disagree.
    #
    # The parking answer comes from scripts/parked_humans.py, which re-uses the conductor's own
    # ``NEXT:`` parser (:mod:`spirrow_mindwire.conductor.handoff`) — the wrapper does NOT
    # re-spell that grammar. See Invoke-ParkedHumansProbe's header for the D-32 rationale.
    $humanParked = @()
    $parkedPollErrors = @()
    $parkedPolled = 0
    foreach ($proj in ($candidates | ForEach-Object { $_.project } | Sort-Object -Unique)) {
        $probe = Invoke-ParkedHumansProbe -Project $proj -Candidates $candidates -HeadsByProject $headsByProject
        $humanParked      += @($probe.parked)
        $parkedPollErrors += @($probe.errors)
        $parkedPolled     += [int]$probe.polled
    }
    # One log line every tick — msg-1370 §I-4 ("何件にしたか記録すること"). Includes the
    # per-project fetch error count so an outage is visible in the wrapper's log without
    # waiting for the digest.
    if ($humanParked.Count -gt 0 -or $parkedPollErrors.Count -gt 0) {
        Confirm-LogWorthKeeping
        $summary = if ($humanParked.Count -gt 0) {
            ($humanParked | ForEach-Object { "$($_.key)" }) -join ', '
        } else { '(none)' }
        Write-Log "human-parked ($($humanParked.Count) of $parkedPolled candidates polled, $($parkedPollErrors.Count) fetch error(s)): $summary"
    }
    else {
        # 0 件 still gets one line so the poll's own liveness is visible in the log (same idea
        # as the 0 件 digest section — silent-day-is-the-point).
        Write-Log "human-parked (0 of $parkedPolled candidates polled)"
    }

    # Daily digest. Sent even when both quarantine and starvation lists are empty (spec/msg-814 §5).
    # A silent day IS the point: "no alert" then still means "the channel is alive," which is what
    # the 5h failure specifically lacked.
    #
    # T-digest-exceeds-discord-limit-and-is-dropped (msg-2099 through msg-2106): cadence is
    # PERIOD-gated, not interval-gated. Two predicates must both hold:
    #   (a) $currentPeriod ≠ $digestState['last_sent_period'] — the digest has not landed today yet
    #   (b) local wall clock ≥ $DailyDigestDeliveryTime — the operator's delivery time has arrived
    #
    # Why period + delivery-time instead of "24h since last send" (msg-2106 §4):
    #   * "24h since last send" drifts forward a few minutes per day (any tick jitter, any run-time
    #     variance). Weeks later the send has walked into 3am, defeating the point of a phone
    #     notification.
    #   * "24h since last send" also double-sends around a period boundary: 23:58 send → 00:03 tick
    #     is still <24h from delivery time, but "period ≠ last_sent_period AND local ≥ delivery"
    #     correctly refuses to re-send.
    # Period + delivery-time drops both by CONSTRUCTION — no jitter constant, no drift math.
    #
    # Webhook-less runs (msg-2106 D-3 preservation of #138 R5): still advance last_sent_period so
    # the loop does not re-enter this branch every 5 minutes. But `last_full_success_period` is NOT
    # advanced — a channel that does not exist has not been informed, and the ⚠ predicate is
    # deliberately blind to whether the reason is "no webhook" or "webhook 400s" (both mean the
    # human has not gotten the digest).
    $currentPeriod = Get-DigestPeriod -Now $nowUtc
    $lastSentPeriod = $null
    if ($digestState.ContainsKey('last_sent_period') -and $digestState['last_sent_period']) {
        $lastSentPeriod = [string]$digestState['last_sent_period']
    }
    $deliveryDue = Test-DigestDeliveryDue -Now $nowUtc -DeliveryTime $DailyDigestDeliveryTime
    $digestDue = ($lastSentPeriod -ne $currentPeriod) -and $deliveryDue
    if ($digestDue) {
        # Load notify-health for ⚠ derivation (D-6). Missing file = empty hashtable (state files
        # are corrupt-tolerant per Get-JsonState). Never fails the sweep on health-file trouble.
        $notifyHealth = Get-JsonState -Path $notifyHealthPath
        if ($null -eq $notifyHealth) { $notifyHealth = @{} }
        if (-not $notifyWebhook) {
            # No point building or logging a digest for a channel that does not exist. Advance the
            # sent-period so we do not re-enter this branch until tomorrow, but leave
            # last_full_success_period alone: a webhook-less day is not a healthy day for the
            # human-consumer perspective. When the operator eventually wires a webhook, the ⚠ shows
            # correctly how many days went unreported.
            $digestState['last_sent_period'] = $currentPeriod
            Save-JsonState -Path $digestStatePath -State $digestState
        }
        else {
            $healthWarning = Get-DigestHealthWarning -Health $notifyHealth -CurrentPeriod $currentPeriod
            $digest = New-DailyDigest -QuarantineState $quarantineState `
                -EvaluatedState $evaluatedState `
                -HeadsByProject $headsByProject -ControlByProject $controlByProject -Now $nowUtc `
                -LiveKeys $liveKeys `
                -HumanParked $humanParked -PendingDecisionsState $pendingDecisionsState `
                -ParkedPollErrors $parkedPollErrors `
                -Budget $DigestBudget `
                -HealthWarning $healthWarning
            Confirm-LogWorthKeeping
            Write-Log "sending daily digest ($($quarantineState.Count) quarantined, $($starved.Count) starved, $($humanParked.Count) human-parked, payload=$($digest.Length) chars)"
            $result = Send-Notification -Message $digest

            # The lower bound the ⚠ predicate needs in order to distinguish "never succeeded" from
            # "first run" (Einstein msg-2396 E-4 / Bohr msg-2401 §5). Period-typed, so D-6's
            # predicate discipline still holds on the read side; write-once, so it records the FIRST
            # attempt and not the latest. Unconditional: evaluated on every attempt whatever the
            # outcome turned out to be — unlike last_full_success_period below, which is gated on a
            # full success. What carries that is NOT statement order, so the durability boundary is
            # stated here rather than implied: this field and the rest of the record reach disk
            # through the single Save-JsonState at the end of this branch, and Send-Notification
            # does not rethrow — its catch converts the failure into a result hashtable ("NEVER
            # fail the sweep because the notifier failed"), and all it runs before that try is a
            # webhook-presence test plus Confirm-LogWorthKeeping, which the Write-Log above has
            # already committed so it returns immediately. So a failing attempt still runs this
            # block down to that save. Moving the assignment above the send would change nothing
            # about what survives a crash, while implying a crash-ordering guarantee the single
            # save does not give; PR #215 gate round 1 asked for that move, Bohr msg-2418 §2
            # refuted it on those two facts.
            if (-not $notifyHealth.ContainsKey('first_attempt_period') -or -not $notifyHealth['first_attempt_period']) {
                $notifyHealth['first_attempt_period'] = $currentPeriod
            }
            # Diagnostic fields (D-6 predicate discipline: recorded but NEVER consulted by the ⚠
            # predicate). Written on every attempt regardless of outcome.
            $notifyHealth['last_attempt_at'] = $nowUtc.ToString("o")
            if ($result -is [hashtable]) {
                $notifyHealth['last_error'] = $result['error']
                $notifyHealth['last_error_class'] = $result['class']
            }

            # T-digest-exceeds-discord-limit-and-is-dropped D-2 (msg-2099): on a
            # deterministic-payload rejection, immediately try a fixed-length degraded message.
            # NOT on deterministic-permanent (401/403/404 → the webhook is dead, a second POST
            # will fail the same way — Test-DigestDelivered still advances cadence, no spam) and
            # NOT on transient (429/5xx/network → the retry belongs to the next tick).
            #
            # Whatever the degraded attempt returns, its ACTUAL outcome must drive the cadence
            # decision — PR-gate review round 2 (2026-08-30) caught the earlier version dropping
            # the degraded's failure CLASS: if degraded also failed but transiently (503, network
            # flake), the earlier code preserved the ORIGINAL `deterministic-payload` result,
            # which Test-DigestDelivered treats as non-retryable, silently advancing cadence and
            # abandoning what was actually a retryable delivery. Now the whole `$result` is
            # replaced by the degraded's real return, with only the success case renamed to
            # `degraded/ok` so Test-DigestFullSuccess stays false (the human was not told the
            # queue contents).
            if ($result -is [hashtable] -and $result['status'] -eq 'failed' -and $result['class'] -eq 'deterministic-payload') {
                $degradedMessage = New-DegradedDigestMessage `
                    -WaitingCount ($humanParked.Count + $quarantineState.Count) `
                    -CurrentPeriod $currentPeriod
                Write-Log "digest full payload rejected (400/413) — attempting degraded fallback ($($degradedMessage.Length) chars)"
                $degradedResult = Send-Notification -Message $degradedMessage
                # Combine into the single result that drives cadence/health decisions below.
                # See Resolve-DigestSendResult's docstring for the four combinations.
                $result = Resolve-DigestSendResult -FullResult $result -DegradedResult $degradedResult
            }

            # Cadence advance: any DELIVERED outcome (sent full, sent degraded, skipped-no-webhook)
            # OR any non-retryable failure (deterministic-permanent when the webhook is dead;
            # deterministic-payload when even the degraded fallback failed) closes the period.
            # ONLY a transient failure holds the clock so the NEXT tick retries. See
            # Test-DigestDelivered's docstring for the full table.
            if (Test-DigestDelivered -Result $result) {
                $digestState['last_sent_period'] = $currentPeriod
                Save-JsonState -Path $digestStatePath -State $digestState
            }
            # ⚠ advance: only a FULL success (status=sent, class=ok) clears the ⚠. A degraded
            # delivery satisfies cadence but not health — the operator was NOT told the queue
            # contents, so ⚠ must stay lit until a full digest lands.
            if (Test-DigestFullSuccess -Result $result) {
                $notifyHealth['last_full_success_period'] = $currentPeriod
            }
            Save-JsonState -Path $notifyHealthPath -State $notifyHealth
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
