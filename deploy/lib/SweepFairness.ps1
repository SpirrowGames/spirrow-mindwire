# deploy/lib/SweepFairness.ps1 — launch fairness for the scheduled sweep (T-sweep-starves-deep-candidates).
#
# Why this exists. The sweep used to walk sweep.json head-first and `break` on the first candidate
# that did work. As long as the top of the list kept doing work, everything below it never got a
# turn: on 2026-10-02 every sweep showed "worked 1 / head-skipped 5-10 / not-reached 96-100", and
# three PRs (#392, #361, #359) sat 21-31h waiting. The starvation metric did not catch it, because
# W-2c refreshes `last_evaluated_at` for every candidate that got a decide verdict, including the
# LAUNCH candidates the sweep then never reached. Confirmed on the host's evaluated.json on
# 2026-10-02: all three keys carried the same tick timestamp, and T-gate-review-submit-failure-
# handling was `not-reached` 73 times on 2026-10-01 without ever appearing in the starved list.
#
# Design: Bohr msg-5592 (consolidated), amended by msg-5596 §2'''' (the first-gate exception and
# the admission-attempt invariant). Einstein approved that amendment in the reply that followed it.
#
#   * `launch_wait_since` (per evaluated.json entry): set when the verdict is LAUNCH and the
#     candidate was not launched (never overwritten). Cleared when the candidate actually launches
#     (after commit-launch) or when the verdict is SKIP. Kept through DEFER, held, quarantined, and
#     any attempt that did not launch.
#   * Ordering: LAUNCH candidates oldest-`launch_wait_since` first, with sweep.json order as the
#     tie-break. Round-robin falls out of the state: a launched candidate's wait is cleared, so the
#     next time it is LAUNCH it joins at the back of the queue.
#   * Gate lane: a LAUNCH whose token is `pr-review`. Up to G=3 launches per tick, and the lane does
#     not break on work. After it, the role lane runs as before (it breaks on the first run that did
#     work).
#   * Two clocks. tickElapsed (from tick start) is compared with LaunchBudgetSeconds, which bounds
#     the whole tick. loopElapsed (from launch-loop start, so decide time is excluded) is compared
#     with GateBudgetSeconds, the share of launch time the gate lane may take.
#   * Admission, checked immediately before each launch:
#       - the first gate candidate evaluated: tickElapsed < Launch (the minimum-progress exception;
#         the candidate uses it up whether or not it launches)
#       - every later gate: tickElapsed < Launch AND loopElapsed < Gate AND gates launched < G
#       - a role turn: tickElapsed < Launch
#   * Invariant: if a tick has gate candidates and a role turn was admitted, at least one gate
#     candidate was attempted before it.
#
# This file is PURE: no I/O, no clock reads except Get-SweepNowUtc, no Write-Log. The wrapper calls
# these functions from inside its dispatch loop, and tests/Test-SweepFairness.ps1 drives the same
# functions. Keep the decisions here and keep the loop a thin skeleton around them, so the tests
# exercise the real rules and not a copy.

# The gate token. It is the normalised NEXT token (conductor/head_skip.py parse_head_token) of a
# `NEXT: pr-review <owner/repo#n>` head, and conductor/handoff.py PR_REVIEW_TOKEN spells it the same way.
$script:SweepGateToken = 'pr-review'
# Gate launches per tick (msg-5586 §2, G=3).
$script:SweepGateMax = 3
# Default budgets (Bohr msg-5588). The task actually runs on a 1-minute trigger with IgnoreNew (docs/deploy.md).
# Bohr msg-5588 chose them assuming a 5-minute interval; see docs/deploy.md "Launch fairness" for the
# measured interval and what that changes about them.
$script:SweepDefaultLaunchBudgetSeconds = 120
$script:SweepDefaultGateBudgetSeconds = 60
$script:SweepLaunchBudgetEnv = 'MINDWIRE_SWEEP_LAUNCH_BUDGET_SEC'
$script:SweepGateBudgetEnv = 'MINDWIRE_SWEEP_GATE_BUDGET_SEC'

# The one clock read. Kept as a function so the dispatch loop contains no static .NET invocation
# (tests/Test-SweepSequentiality.ps1 L3b), and so tests can see where the time comes from.
function Get-SweepNowUtc {
    return [DateTime]::UtcNow
}

function Get-SweepElapsedSeconds {
    param([datetime]$Since, [datetime]$Now)
    return [double](($Now - $Since).TotalSeconds)
}

# Resolve one budget value: argument > environment variable > default. The argument is "unset" when
# it is $null or 0 (a [int] script parameter that was not passed binds 0). The value must be a
# positive integer number of seconds; anything else is a configuration error and THROWS, so the tick
# aborts loudly (it is never quietly replaced by the default).
function Resolve-SweepBudgetValue {
    param($Argument, [string]$EnvValue, [int]$Default, [string]$Name)
    if ($null -ne $Argument -and "$Argument" -ne '' -and "$Argument" -ne '0') {
        $raw = "$Argument"; $source = 'argument'
    }
    elseif (-not [string]::IsNullOrWhiteSpace($EnvValue)) {
        $raw = $EnvValue.Trim(); $source = 'env'
    }
    else {
        return @{ value = $Default; source = 'default' }
    }
    $parsed = 0
    if (-not [int]::TryParse($raw, [ref]$parsed) -or $parsed -le 0) {
        throw "sweep budget $Name='$raw' (from $source) is not a positive integer number of seconds"
    }
    return @{ value = $parsed; source = $source }
}

# Resolve both budgets and validate GateBudgetSeconds < LaunchBudgetSeconds. THROWS on violation.
# The wrapper calls this inside its try{}, so a bad configuration aborts the tick (exit 1, logged)
# instead of running with a gate budget that could leave the role lane no room.
function Resolve-SweepBudgets {
    param($LaunchArgument, $GateArgument, [string]$LaunchEnv, [string]$GateEnv)
    $launch = Resolve-SweepBudgetValue -Argument $LaunchArgument -EnvValue $LaunchEnv `
        -Default $script:SweepDefaultLaunchBudgetSeconds -Name 'LaunchBudgetSeconds'
    $gate = Resolve-SweepBudgetValue -Argument $GateArgument -EnvValue $GateEnv `
        -Default $script:SweepDefaultGateBudgetSeconds -Name 'GateBudgetSeconds'
    if ($gate.value -ge $launch.value) {
        throw ("sweep budget configuration error: GateBudgetSeconds ($($gate.value), $($gate.source)) must be " +
               "less than LaunchBudgetSeconds ($($launch.value), $($launch.source)); the tick is aborted")
    }
    return @{
        launch = $launch.value; launch_source = $launch.source
        gate = $gate.value; gate_source = $gate.source
    }
}

# Which lane a decide verdict belongs to: 'gate' (LAUNCH, token pr-review), 'role' (any other
# LAUNCH), or 'none' (SKIP / DEFER / missing verdict; not a launch, so position does not matter).
function Get-SweepLane {
    param($Verdict)
    if ($null -eq $Verdict) { return 'none' }
    if ("$($Verdict.decision)" -ne 'launch') { return 'none' }
    if ("$($Verdict.token)" -eq $script:SweepGateToken) { return 'gate' }
    return 'role'
}

# Read launch_wait_since off an evaluated.json row as a UTC [datetime], or $null. Handles the two
# shapes a row can have: a hashtable written this tick, and a PSCustomObject or [DateTime] value
# that came back from ConvertFrom-Json.
function Get-LaunchWaitSince {
    param([hashtable]$EvaluatedState, [string]$Key)
    if (-not $EvaluatedState.ContainsKey($Key)) { return $null }
    $row = $EvaluatedState[$Key]
    if ($null -eq $row) { return $null }
    $raw = if ($row -is [hashtable]) { $row['launch_wait_since'] }
           elseif ($row.PSObject.Properties.Name -contains 'launch_wait_since') { $row.launch_wait_since }
           else { $null }
    if ($null -eq $raw -or "$raw" -eq '') { return $null }
    if ($raw -is [datetime]) { return $raw.ToUniversalTime() }
    try {
        return [datetime]::Parse("$raw", [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
    } catch { return $null }
}

# Normalise a row to a hashtable so it can be edited in place. Every field is kept.
function ConvertTo-EvaluatedRow {
    param($Row)
    if ($null -eq $Row) { return @{} }
    if ($Row -is [hashtable]) { return $Row }
    $h = @{}
    foreach ($p in $Row.PSObject.Properties) { $h[$p.Name] = $p.Value }
    return $h
}

# Set launch_wait_since to $Now if it is absent. Never overwrites: the start of the wait is the
# FIRST tick on which the candidate was LAUNCH but not launched, and it is never moved forward.
function Set-LaunchWaitPending {
    param([hashtable]$EvaluatedState, [string]$Key, [datetime]$Now)
    if ($null -ne (Get-LaunchWaitSince -EvaluatedState $EvaluatedState -Key $Key)) { return }
    $row = ConvertTo-EvaluatedRow ($EvaluatedState[$Key])
    $row['launch_wait_since'] = $Now.ToUniversalTime().ToString('o')
    $EvaluatedState[$Key] = $row
}

function Clear-LaunchWait {
    param([hashtable]$EvaluatedState, [string]$Key)
    if (-not $EvaluatedState.ContainsKey($Key) -or $null -eq $EvaluatedState[$Key]) { return }
    $row = ConvertTo-EvaluatedRow ($EvaluatedState[$Key])
    if ($row.ContainsKey('launch_wait_since')) { [void]$row.Remove('launch_wait_since') }
    $EvaluatedState[$Key] = $row
}

# The per-tick wait bookkeeping that depends only on the decide verdicts. It runs before the launch
# loop:
#   SKIP   -> clear (the thread is stopped or finished; there is nothing left to wait for)
#   LAUNCH -> set if absent (cleared again below for each candidate that actually launches)
#   DEFER  -> keep
# Candidates with no verdict (held, quarantined) are not in $Verdicts and keep their value.
function Update-LaunchWaitFromVerdicts {
    param([hashtable]$EvaluatedState, [hashtable]$Verdicts, [datetime]$Now)
    foreach ($k in @($Verdicts.Keys)) {
        $d = "$($Verdicts[$k].decision)"
        if ($d -eq 'skip') { Clear-LaunchWait -EvaluatedState $EvaluatedState -Key $k }
        elseif ($d -eq 'launch') { Set-LaunchWaitPending -EvaluatedState $EvaluatedState -Key $k -Now $Now }
    }
}

# The dispatch order for one tick. Three blocks:
#   1. every non-launch candidate (held / quarantined / SKIP / DEFER), in sweep.json order. These
#      are only judged, never launched, so putting them first means a break among the launches can
#      no longer misreport them as not-reached.
#   2. gate-lane LAUNCHes, oldest launch_wait_since first
#   3. role-lane LAUNCHes, oldest launch_wait_since first
# Ties (and a missing wait value, read as $Now) fall back to sweep.json order. The sort is made
# stable by sorting on (wait, index) explicitly; Sort-Object alone is not guaranteed stable.
function Get-OrderedSweepCandidates {
    param([array]$Candidates, [hashtable]$Verdicts, [hashtable]$EvaluatedState, [datetime]$Now)
    $others = @(); $gates = @(); $roles = @()
    for ($i = 0; $i -lt $Candidates.Count; $i++) {
        $c = $Candidates[$i]
        $v = if ($Verdicts.ContainsKey($c.key)) { $Verdicts[$c.key] } else { $null }
        $lane = Get-SweepLane -Verdict $v
        if ($lane -eq 'none') { $others += $c; continue }
        $wait = Get-LaunchWaitSince -EvaluatedState $EvaluatedState -Key $c.key
        # A missing value reads as $Now, and $Now goes through the same 'o' round trip that a stored
        # value has been through (Set-LaunchWaitPending writes 'o', Get-LaunchWaitSince parses it).
        # Every sort key therefore comes from one representation, and a fallback key compares
        # equal to a value stored this tick. Equality does not depend on a native [datetime] and a
        # parsed one agreeing to the tick (PR #406 gate advisory).
        if ($null -eq $wait) {
            $wait = [datetime]::Parse($Now.ToUniversalTime().ToString('o'), [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
        }
        $item = [PSCustomObject]@{ Cand = $c; WaitTicks = $wait.Ticks; Index = $i }
        if ($lane -eq 'gate') { $gates += $item } else { $roles += $item }
    }
    $gatesSorted = @($gates | Sort-Object -Property WaitTicks, Index | ForEach-Object { $_.Cand })
    $rolesSorted = @($roles | Sort-Object -Property WaitTicks, Index | ForEach-Object { $_.Cand })
    return @($others) + $gatesSorted + $rolesSorted
}

# --- admission -----------------------------------------------------------------------------------
# One scheduler per tick. `attempts` records every admission decision in order (admitted or not),
# which is what the invariant and the property test read: lane, key, admitted, reason, whether
# the first-gate exception applied, and what happened to an admitted attempt.
function New-LaunchScheduler {
    param([int]$LaunchBudgetSeconds, [int]$GateBudgetSeconds, [int]$GateMax = $script:SweepGateMax)
    return @{
        launch_budget = $LaunchBudgetSeconds
        gate_budget = $GateBudgetSeconds
        gate_max = $GateMax
        first_gate_evaluated = $false
        gate_launched = 0
        role_launched = 0
        gate_closed_reason = $null
        attempts = [System.Collections.ArrayList]::new()
    }
}

# Decide whether the next launch may start. Returns @{ admit; reason; scope; exception }:
#   admit=$true                 -> launch it (call Complete-LaunchAttempt afterwards)
#   scope='break'               -> nothing else can be admitted this tick (tick budget spent). The
#                                  caller breaks with breakReason 'time-budget'.
#   scope='skip-lane'           -> this gate is refused (gate-budget / gate-cap) but the role lane may
#                                  still run. The caller leaves the candidate undisposed (not-reached).
# The first gate candidate evaluated is admitted on the tick budget alone (the minimum-progress
# exception) and uses the exception up whether or not it launches (msg-5596 §2'''').
function Request-LaunchAdmission {
    param([hashtable]$Scheduler, [string]$Lane, [string]$Key, [double]$TickElapsed, [double]$LoopElapsed)
    $exception = $false
    if ($TickElapsed -ge $Scheduler.launch_budget) {
        $r = @{ admit = $false; reason = 'time-budget'; scope = 'break'; exception = $false }
    }
    elseif ($Lane -eq 'gate' -and -not $Scheduler.first_gate_evaluated) {
        $exception = $true
        $r = @{ admit = $true; reason = 'first-gate'; scope = $null; exception = $true }
    }
    elseif ($Lane -eq 'gate' -and $Scheduler.gate_launched -ge $Scheduler.gate_max) {
        $r = @{ admit = $false; reason = 'gate-cap'; scope = 'skip-lane'; exception = $false }
    }
    elseif ($Lane -eq 'gate' -and $LoopElapsed -ge $Scheduler.gate_budget) {
        $r = @{ admit = $false; reason = 'gate-budget'; scope = 'skip-lane'; exception = $false }
    }
    else {
        $r = @{ admit = $true; reason = 'ok'; scope = $null; exception = $false }
    }
    if ($Lane -eq 'gate') { $Scheduler.first_gate_evaluated = $true }
    if ($r.scope -eq 'skip-lane' -and -not $Scheduler.gate_closed_reason) { $Scheduler.gate_closed_reason = $r.reason }
    [void]$Scheduler.attempts.Add([PSCustomObject]@{
        key = $Key; lane = $Lane; admitted = [bool]$r.admit; reason = $r.reason; exception = $exception
        tick_elapsed = $TickElapsed; loop_elapsed = $LoopElapsed; outcome = $null
    })
    return $r
}

# Record what happened to an admitted attempt. $Outcome is 'launched' when a conductor was spawned
# (whatever its exit code), or a non-launch disposition such as 'lease-busy' / 'commit-refused'.
# G counts LAUNCHES, never attempts (msg-5594, kept by msg-5596).
function Complete-LaunchAttempt {
    param([hashtable]$Scheduler, [string]$Lane, [string]$Outcome)
    $last = $Scheduler.attempts[$Scheduler.attempts.Count - 1]
    $last.outcome = $Outcome
    if ($Outcome -eq 'launched') {
        if ($Lane -eq 'gate') { $Scheduler.gate_launched++ } else { $Scheduler.role_launched++ }
    }
}

# What the loop does after a conductor run that exited 0 with a parseable verdict: a gate never
# breaks the sweep; a role turn breaks it when it did work (rounds > 0), as before this change.
function Get-PostRunAction {
    param([string]$Lane, [int]$Rounds)
    if ($Lane -eq 'gate') { return 'continue' }
    if ($Rounds -gt 0) { return 'break' }
    return 'continue'
}

# The conductor arguments that depend on the lane (T-sweep-starves-deep-candidates PR-B, Bohr
# msg-6313 §1′). A gate-lane launch gets `--gate-only`: the conductor posts the gate's relay (or the
# R4 ci-route) and stops on `slice_end` instead of spawning the implementer, so the implementer's
# turn waits in the fair role queue rather than riding the priority lane. Every other lane gets
# nothing. The flag is spelled only here; the wrapper's dispatch loop is the one caller, and
# deploy/run-conductor.ps1 (a hand run) never adds it.
function Get-ConductorLaneArgs {
    param([string]$Lane)
    if ($Lane -eq 'gate') { return @('--gate-only') }
    return @()
}

# The invariant (msg-5596): when the tick had gate candidates and a role turn was admitted, at
# least one gate candidate was attempted before that role turn. Returns $null when it holds, or a
# description of the violation.
function Test-GateAdmissionInvariant {
    param([hashtable]$Scheduler, [int]$GateCandidateCount)
    if ($GateCandidateCount -le 0) { return $null }
    $attempts = @($Scheduler.attempts)
    for ($i = 0; $i -lt $attempts.Count; $i++) {
        if ($attempts[$i].lane -eq 'role' -and $attempts[$i].admitted) {
            $gateBefore = @($attempts[0..$i] | Where-Object { $_.lane -eq 'gate' })
            if ($gateBefore.Count -eq 0) {
                return "role turn $($attempts[$i].key) admitted with $GateCandidateCount gate candidate(s) and no gate attempt before it"
            }
            return $null
        }
    }
    return $null
}

# Launch-wait starvation (msg-5586 §5): live keys whose launch_wait_since is at least $Threshold old.
# This is a different failure from the existing `starved` metric ("not evaluated"); W-2c's
# last_evaluated_at refresh does not touch launch_wait_since, so this one cannot be masked by it.
# Returns objects { key; age } sorted oldest first.
function Get-LaunchWaitStarved {
    param([hashtable]$EvaluatedState, [string[]]$LiveKeys = @(), [datetime]$Now, [TimeSpan]$Threshold)
    $out = @()
    foreach ($k in $LiveKeys) {
        $since = Get-LaunchWaitSince -EvaluatedState $EvaluatedState -Key $k
        if ($null -eq $since) { continue }
        $age = $Now.ToUniversalTime() - $since
        if ($age -ge $Threshold) { $out += [PSCustomObject]@{ key = $k; age = $age } }
    }
    return @($out | Sort-Object -Property @{ Expression = { $_.age }; Descending = $true })
}
