# deploy/lib/PreLoopBudget.ps1 — pre-loop overhead: per-phase timing (D-3) and the over-budget
# notification state machine (D-4). T-sweep-preloop-overhead-exceeds-launch-budget.
#
# Why this exists. When the work before the launch loop (sync, intake, gate-bootstrap, control /
# head probes, 1b, park-wake, auto-gate, decide) takes longer than LaunchBudgetSeconds, the tick
# launches nothing. Until this file, that showed up only as a WARN line in the log, which nobody
# watches. From 2026-11-03 22:20 to 2026-11-04 00:45 JST no role and no gate were launched, and
# nobody was told (operator msg-7823).
#
# D-3 (Bohr msg-7853): one log line per tick with how long each pre-loop phase took, so we can see
# what grew and decide on D-1 / D-5 from the numbers.
#
# D-4 (Bohr msg-7851 v1.5, approved by Einstein msg-7852): one state machine for the alerts.
#   level per tick (ratio = pre-loop / launch budget):
#     ok    ratio <  0.8
#     near  0.8 <= ratio < 1.0
#     over  ratio >= 1.0   (the tick launches nothing)
#   state: elevated_streak (near-or-over ticks in a row), over_streak (over ticks in a row),
#          notified_level (none | near | over), streak_started_at (when the elevated run began,
#          stored as Unix seconds so a JSON round trip cannot reformat it — it is part of the
#          dedup signature).
#   rules, evaluated top to bottom, at most one alert per tick:
#     1. over_streak >= 3 and notified_level != over       -> over alert, notified_level = over
#     2. elevated_streak >= 6 and notified_level == none    -> near warning, notified_level = near
#        (after an over alert notified_level is over, so a late warning can never follow it)
#     3. notified_level == over and this tick is not over   -> recovered alert; notified_level
#        becomes near if this tick is near, none if it is ok
#     4. an ok tick resets the state (no alert for near -> ok; the digest records it)
#   The wrapper dedups through Send-NotificationIfChanged, key `sweep:preloop-budget`, signature
#   `<kind>@<streak_started_at>`.
#
# Who reads the alert: the operator, through the same notification webhook every other sweep alert
# uses (Send-NotificationIfChanged). This file posts nothing to a chatroom thread.
#
# This file is PURE: no I/O, no clock reads, no Write-Log. The wrapper does the timing, the state
# file and the send; tests/Test-PreLoopBudget.ps1 drives these functions directly.

$script:PreLoopNearRatio = 0.8
$script:PreLoopOverStreakToAlert = 3
$script:PreLoopElevatedStreakToWarn = 6
$script:PreLoopNotifyKey = 'sweep:preloop-budget'
# Per-tick samples kept for the digest line. 48h so a late digest still finds its last 24h.
$script:PreLoopSampleRetentionSeconds = 48 * 3600
$script:PreLoopDigestWindowSeconds = 24 * 3600

# --- D-3: phase timing -----------------------------------------------------------------------------

function New-PreLoopPhaseLog {
    # Ordered, so the log line lists phases in the order they ran.
    return [ordered]@{}
}

# Adds $Seconds to phase $Name (a phase run more than once in a tick, e.g. per project, is summed
# only when the caller uses the same name; per-project phases are named `control[proj]` etc.).
function Add-PreLoopPhase {
    param([System.Collections.Specialized.OrderedDictionary]$Phases, [string]$Name, [double]$Seconds)
    if ($Phases.Contains($Name)) { $Phases[$Name] = [double]$Phases[$Name] + $Seconds }
    else { $Phases[$Name] = $Seconds }
}

function Format-PreLoopPhasesLine {
    param([System.Collections.Specialized.OrderedDictionary]$Phases)
    $inv = [System.Globalization.CultureInfo]::InvariantCulture
    $parts = @(foreach ($k in $Phases.Keys) { "{0}={1}s" -f $k, ([double]$Phases[$k]).ToString('0.0', $inv) })
    if ($parts.Count -eq 0) { return 'pre-loop phases: (none measured)' }
    return 'pre-loop phases: ' + ($parts -join ' ')
}

# --- D-4: level and state machine ------------------------------------------------------------------

function Get-PreLoopBudgetLevel {
    param([double]$PreLoopSeconds, [double]$BudgetSeconds)
    if ($BudgetSeconds -le 0) { throw "Get-PreLoopBudgetLevel: budget must be positive (got $BudgetSeconds)" }
    $ratio = $PreLoopSeconds / $BudgetSeconds
    if ($ratio -ge 1.0) { return 'over' }
    if ($ratio -ge $script:PreLoopNearRatio) { return 'near' }
    return 'ok'
}

# Unix seconds of a UTC instant (the machine's clock unit). Pure: the instant is passed in.
function Get-PreLoopEpochSeconds {
    param([datetime]$Utc)
    return [long]([DateTimeOffset]::new([DateTime]::SpecifyKind($Utc, [DateTimeKind]::Utc))).ToUnixTimeSeconds()
}

function ConvertTo-PreLoopInt {
    param($Value)
    if ($null -eq $Value) { return 0 }
    $n = 0L
    if ([long]::TryParse("$Value", [ref]$n) -and $n -ge 0) { return $n }
    return 0
}

# Normalises the raw state file (Get-JsonState: a hashtable, empty on any read failure) into the
# shape the machine uses. Unknown or broken fields fall back to the initial state, which can only
# make an alert come later, never suppress one forever: the streaks restart from this tick.
function ConvertTo-PreLoopBudgetState {
    param([hashtable]$Raw)
    if ($null -eq $Raw) { $Raw = @{} }
    $level = "$($Raw['notified_level'])"
    if ($level -notin @('none', 'near', 'over')) { $level = 'none' }
    $samples = @()
    foreach ($s in @($Raw['samples'])) {
        if ($null -eq $s) { continue }
        $at = $null; $sec = $null; $lv = $null
        if ($s -is [System.Collections.IDictionary]) { $at = $s['at']; $sec = $s['s']; $lv = $s['level'] }
        else { $at = $s.at; $sec = $s.s; $lv = $s.level }
        # A number is taken as it is; only a string is parsed, and always with InvariantCulture, so
        # the result does not depend on the host's culture (PR #517 gate round 1, objection 2).
        if ($null -eq $at -or $null -eq $sec) { continue }
        $d = 0.0
        if ($sec -is [double] -or $sec -is [int] -or $sec -is [long] -or $sec -is [decimal] -or $sec -is [single]) { $d = [double]$sec }
        elseif ($sec -isnot [string] -or -not [double]::TryParse($sec, [System.Globalization.NumberStyles]::Float,
                [System.Globalization.CultureInfo]::InvariantCulture, [ref]$d)) { continue }
        if ("$lv" -notin @('ok', 'near', 'over')) { continue }
        $samples += @{ at = (ConvertTo-PreLoopInt $at); s = $d; level = "$lv" }
    }
    $started = $null
    if ($null -ne $Raw['streak_started_at']) { $started = ConvertTo-PreLoopInt $Raw['streak_started_at'] }
    return @{
        elevated_streak   = [int](ConvertTo-PreLoopInt $Raw['elevated_streak'])
        over_streak       = [int](ConvertTo-PreLoopInt $Raw['over_streak'])
        notified_level    = $level
        streak_started_at = $started
        samples           = $samples
    }
}

# One tick of the machine. Mutates $State in place and returns the alert to send, or $null.
#   $NowEpoch : this tick, Unix seconds (the wrapper passes the loop-start instant)
#   returns   : $null, or @{ kind = 'over' | 'near' | 'recovered'; signature; level }
function Step-PreLoopBudgetState {
    param([hashtable]$State, [string]$Level, [long]$NowEpoch, [double]$PreLoopSeconds = 0)
    if ($Level -notin @('ok', 'near', 'over')) { throw "Step-PreLoopBudgetState: unknown level '$Level'" }

    # Streaks.
    if ($Level -eq 'ok') {
        $State.elevated_streak = 0
        $State.over_streak = 0
    }
    else {
        if ($State.elevated_streak -eq 0 -or $null -eq $State.streak_started_at) { $State.streak_started_at = $NowEpoch }
        $State.elevated_streak = [int]$State.elevated_streak + 1
        if ($Level -eq 'over') { $State.over_streak = [int]$State.over_streak + 1 } else { $State.over_streak = 0 }
    }
    # The run this tick belongs to (or, on an ok tick, the run it just ended) — the signature's anchor.
    $anchor = if ($null -ne $State.streak_started_at) { $State.streak_started_at } else { $NowEpoch }

    $alert = $null
    if ($State.over_streak -ge $script:PreLoopOverStreakToAlert -and $State.notified_level -ne 'over') {
        $State.notified_level = 'over'
        $alert = @{ kind = 'over' }
    }
    elseif ($State.elevated_streak -ge $script:PreLoopElevatedStreakToWarn -and $State.notified_level -eq 'none') {
        $State.notified_level = 'near'
        $alert = @{ kind = 'near' }
    }
    elseif ($State.notified_level -eq 'over' -and $Level -ne 'over') {
        $State.notified_level = if ($Level -eq 'near') { 'near' } else { 'none' }
        $alert = @{ kind = 'recovered' }
    }
    # Rule 4: an ok tick ends the run, whatever else happened above.
    if ($Level -eq 'ok') {
        $State.notified_level = 'none'
        $State.streak_started_at = $null
    }
    if ($null -ne $alert) {
        $alert.signature = "$($alert.kind)@$anchor"
        $alert.level = $Level
        $alert.over_streak = $State.over_streak
        $alert.elevated_streak = $State.elevated_streak
    }
    return $alert
}

# Records this tick for the digest line and drops samples older than the retention window.
function Add-PreLoopBudgetSample {
    param([hashtable]$State, [long]$NowEpoch, [double]$PreLoopSeconds, [string]$Level)
    $cutoff = $NowEpoch - $script:PreLoopSampleRetentionSeconds
    $kept = @($State.samples | Where-Object { $null -ne $_ -and [long]$_.at -ge $cutoff })
    $kept += @{ at = $NowEpoch; s = [math]::Round($PreLoopSeconds, 1); level = $Level }
    $State.samples = $kept
}

# The state file as written: the four machine fields plus the samples.
function ConvertTo-PreLoopBudgetStateFile {
    param([hashtable]$State)
    return @{
        elevated_streak   = $State.elevated_streak
        over_streak       = $State.over_streak
        notified_level    = $State.notified_level
        streak_started_at = $State.streak_started_at
        samples           = @($State.samples)
    }
}

function Format-PreLoopSeconds {
    param([double]$Seconds)
    return ([double]$Seconds).ToString('0.0', [System.Globalization.CultureInfo]::InvariantCulture)
}

# The alert text. $PhasesLine is Format-PreLoopPhasesLine's output for this tick.
function Format-PreLoopBudgetMessage {
    param([hashtable]$Alert, [double]$PreLoopSeconds, [double]$BudgetSeconds, [string]$PhasesLine = '')
    $pct = [int][math]::Round(100.0 * $PreLoopSeconds / $BudgetSeconds)
    $now = "pre-loop=$(Format-PreLoopSeconds $PreLoopSeconds)s / launch 予算 $([int]$BudgetSeconds)s（$pct%）"
    $tail = if ($PhasesLine) { "`n内訳: $PhasesLine" } else { '' }
    switch ($Alert.kind) {
        'over' {
            return ("MindWire: 起動前処理が launch 予算を **$($Alert.over_streak) tick 連続**で超えています（$now）。" +
                    "この間、role も gate も 1 本も起動していません。予算を上げるか、遅い段階を直してください。$tail")
        }
        'near' {
            return ("MindWire: 起動前処理が launch 予算の 80% 以上の状態で $($Alert.elevated_streak) tick 続いています（$now）。" +
                    "このまま伸びると、予算を超えた tick は何も起動しなくなります。$tail")
        }
        'recovered' {
            $margin = if ($Alert.level -eq 'near') { 'ただし、まだ予算の 80% 以上で、余裕は小さいです。' } else { '' }
            return "MindWire: 起動前処理が launch 予算内に戻り、launch を再開しました（$now）。$margin$tail"
        }
    }
    throw "Format-PreLoopBudgetMessage: unknown alert kind '$($Alert.kind)'"
}

# The digest line: the last 24h of samples — tick count, over-budget ticks, median and max. Nothing when
# there are no samples (the digest is then unchanged).
function Get-PreLoopBudgetDigestLines {
    param([hashtable]$State, [long]$NowEpoch)
    $from = $NowEpoch - $script:PreLoopDigestWindowSeconds
    $win = @($State.samples | Where-Object { $null -ne $_ -and [long]$_.at -ge $from -and [long]$_.at -le $NowEpoch })
    if ($win.Count -eq 0) { return }
    $secs = @($win | ForEach-Object { [double]$_.s } | Sort-Object)
    $n = $secs.Count
    $median = if ($n % 2 -eq 1) { $secs[($n - 1) / 2] } else { ($secs[$n / 2 - 1] + $secs[$n / 2]) / 2.0 }
    $over = @($win | Where-Object { $_.level -eq 'over' }).Count
    $line = ("起動前処理（直近 24h）: {0} tick、予算超え（起動なし）{1} tick、中央値 {2}s、最大 {3}s" -f
             $n, $over, (Format-PreLoopSeconds $median), (Format-PreLoopSeconds $secs[$n - 1]))
    return $line
}

# --- D-1: heads hand-off between head probe, 1b and park-wake --------------------------------------
# The sweep runs, per project: control -> head probe -> 1b -> park-wake (Bohr msg-7853 D-1). 1b and
# park-wake get the heads through `--heads-json` and serve threads whose head has not moved from
# their tail cache (src/spirrow_mindwire/tail_cache.py). They print `posted_heads`
# ({thread_id: msg_id}) for every thread they wrote to, and the sweep writes those over its heads
# before the next step, so it never runs the head probe a second time (msg-7845 Obj-2-1).

# The heads file 1b / park-wake read, in scripts/thread_heads.py's shape. $null heads (the probe
# failed, or a step's result was unusable) gives `{}`: no heads, so every thread is read in full.
function ConvertTo-HeadsProbeJson {
    param([hashtable]$Heads)
    if ($null -eq $Heads) { return '{}' }
    return (@{ heads = $Heads; count = $Heads.Count } | ConvertTo-Json -Depth 3 -Compress)
}

# Writes a step's posted_heads over $Heads and returns the heads to use next:
#   * a thread with a msg id gets that id as its head;
#   * a thread with "" (the post's id could not be read, or the post may have landed without an
#     answer) is REMOVED, so it counts as unknown and is launched / re-read (fail-open);
#   * a step whose result is missing or has no posted_heads (timeout, crash, exit 1) may have
#     written anywhere, so the whole map becomes $null — every thread of the project is unknown.
#   * $null heads stay $null.
function Update-HeadsFromPosted {
    param([hashtable]$Heads, $Result)
    if ($null -eq $Heads) { return $null }
    if ($null -eq $Result) { return $null }
    # The sweep parses the steps' stdout with plain ConvertFrom-Json (a PSCustomObject), but a
    # hashtable result (ConvertFrom-Json -AsHashtable) must work too: on a hashtable,
    # .PSObject.Properties does not list its keys (PR #517 gate round 1, objection 1).
    if ($Result -is [System.Collections.IDictionary]) {
        if (-not $Result.Contains('posted_heads')) { return $null }
        $posted = $Result['posted_heads']
    }
    else {
        if ($null -eq $Result.PSObject.Properties['posted_heads']) { return $null }
        $posted = $Result.posted_heads
    }
    if ($null -eq $posted) { return $null }
    $pairs = if ($posted -is [System.Collections.IDictionary]) { @($posted.GetEnumerator() | ForEach-Object { @{ Name = $_.Key; Value = $_.Value } }) }
             else { @($posted.PSObject.Properties | ForEach-Object { @{ Name = $_.Name; Value = $_.Value } }) }
    foreach ($p in $pairs) {
        $id = "$($p.Value)"
        if ([string]::IsNullOrEmpty($id)) { [void]$Heads.Remove($p.Name) } else { $Heads[$p.Name] = $id }
    }
    return , $Heads
}

# The tail cache file of a project, under state\tail-cache. Characters outside [A-Za-z0-9._-] are
# replaced so a project name cannot leave the directory.
function Get-TailCacheFileName {
    param([string]$Project)
    return (($Project -replace '[^A-Za-z0-9._-]', '-') + '.json')
}
