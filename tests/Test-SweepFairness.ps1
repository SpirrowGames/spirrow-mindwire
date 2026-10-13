# Launch fairness for the scheduled sweep (T-sweep-starves-deep-candidates).
#
# Spec: Bohr msg-5592 (consolidated design), amended by msg-5596 (first-gate exception and the
# admission-attempt invariant). What this file pins:
#   * round-robin: with 108 LAUNCH candidates and every launched thread doing work, every one of
#     them is launched within L ticks (the old head-first + break order launched only the head)
#   * the gate lane (token pr-review): launched in the same tick whatever the head did; at most G=3
#     launches; cut by G or by the gate budget, it comes first on the next tick
#   * K-budget stops both lanes and keeps launch_wait_since
#   * launch_wait_since: kept through DEFER / held, cleared by SKIP and by a launch, and not reset by
#     W-2c's last_evaluated_at refresh
#   * the two clocks and the admission rules, the first-gate exception, and the invariant (as a
#     randomised property test)
#   * budget resolution: argument > env > default; Gate >= Launch aborts
#   * launch-wait starvation at 6h, in the log helper and in the digest
#   * wiring: the wrapper's dispatch loop actually calls these functions
#
# The tick simulation (Invoke-SimTick) is a skeleton of the wrapper's dispatch loop that calls the
# SAME functions from deploy/lib/SweepFairness.ps1 the wrapper calls. The decisions (order, lane,
# admission, post-run break, wait bookkeeping) live in the lib, so the simulation exercises the real
# rules. The wiring pins at the end check that the wrapper's loop still calls them.
#
# One path here is modelled but does not exist in the wrapper today: an admitted attempt that does
# NOT launch (`lease-busy`). The wrapper has no lease gate in its candidate loop (deploy/lib/Lease.ps1
# is reader-only there), and commit-launch failure aborts the tick (fail-closed, unchanged). The
# scheduler still has to handle the case, because msg-5594/5596 define G and the exception over it,
# so the simulation injects it through $LeaseOk.
#
# The clock is simulated (seconds as numbers); nothing here sleeps.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'deploy/lib/SweepFairness.ps1')
. (Join-Path $repoRoot 'deploy/lib/Summon.ps1')
. (Join-Path $repoRoot 'deploy/lib/StopReason.ps1')

$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }
$functions = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'Update-EvaluatedTimestamp', 'ConvertTo-UtcInstant', 'Format-DurationDigest', 'Get-ParkedRowTag',
                  'New-DailyDigest', 'Get-StarvedKeys') {
    $fn = $functions | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}
$script:StarvedThreshold = [TimeSpan]::FromHours(24)

$script:failures = 0
function Check {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} {1}" -f $Name, $Detail) }
}

function New-Cand { param([string]$Key) [PSCustomObject]@{ key = $Key; project = ($Key -split '/')[0]; thread_id = ($Key -split '/')[1] } }
function New-Verdict {
    param([string]$Decision = 'launch', [string]$Token = 'heisenberg')
    [PSCustomObject]@{ decision = $Decision; token = $Token; reason = 'test' }
}

# One tick of the dispatch loop. Seconds are simulated: $PreLoop is tickElapsed at loop start
# (sync + probes + decide), and each launch advances the clock by & $RunSeconds.
function Invoke-SimTick {
    param(
        [array]$Candidates, [hashtable]$Verdicts, [hashtable]$State, [datetime]$Now,
        [int]$LaunchBudget = 120, [int]$GateBudget = 60, [double]$PreLoop = 10,
        [scriptblock]$RunSeconds = { param($k) 1 },
        [scriptblock]$Rounds = { param($k) 1 },
        [scriptblock]$LeaseOk = { param($k) $true },
        [scriptblock]$LeaseSeconds = { param($k) 0.5 },
        [scriptblock]$ExitCode = { param($k) 0 },
        [int]$K = 2
    )
    Update-LaunchWaitFromVerdicts -EvaluatedState $State -Verdicts $Verdicts -Now $Now
    $ordered = Get-OrderedSweepCandidates -Candidates $Candidates -Verdicts $Verdicts -EvaluatedState $State -Now $Now
    $gateCount = @($ordered | Where-Object { $Verdicts.ContainsKey($_.key) -and (Get-SweepLane -Verdict $Verdicts[$_.key]) -eq 'gate' }).Count
    $sched = New-LaunchScheduler -LaunchBudgetSeconds $LaunchBudget -GateBudgetSeconds $GateBudget
    $t = $PreLoop
    $launched = @()
    $breakReason = $null
    $failures = 0
    foreach ($c in $ordered) {
        $v = if ($Verdicts.ContainsKey($c.key)) { $Verdicts[$c.key] } else { $null }
        $lane = Get-SweepLane -Verdict $v
        if ($lane -eq 'none') { continue }
        $adm = Request-LaunchAdmission -Scheduler $sched -Lane $lane -Key $c.key -TickElapsed $t -LoopElapsed ($t - $PreLoop)
        if (-not $adm.admit) {
            if ($adm.scope -eq 'break') { $breakReason = 'time-budget'; break }
            continue
        }
        if (-not (& $LeaseOk $c.key)) {
            Complete-LaunchAttempt -Scheduler $sched -Lane $lane -Outcome 'lease-busy'
            $t += (& $LeaseSeconds $c.key)
            continue
        }
        Complete-LaunchAttempt -Scheduler $sched -Lane $lane -Outcome 'launched'
        Clear-LaunchWait -EvaluatedState $State -Key $c.key
        $launched += $c.key
        $t += (& $RunSeconds $c.key)
        if ((& $ExitCode $c.key) -ne 0) {
            $failures++
            if ($failures -ge $K) { $breakReason = 'k-budget-hit'; break }
            continue
        }
        if ((Get-PostRunAction -Lane $lane -Rounds (& $Rounds $c.key)) -eq 'break') { break }
    }
    return @{ launched = $launched; breakReason = $breakReason; sched = $sched; gateCount = $gateCount; ordered = $ordered }
}

$t0 = [datetime]::Parse('2026-10-02T00:00:00Z').ToUniversalTime()

# =============================================================================================
Write-Host "round-robin — 108 LAUNCH candidates, every launch does work, all launched within L ticks"
$cands108 = @(0..107 | ForEach-Object { New-Cand ("p/T-{0:D3}" -f $_) })
$v108 = @{}; foreach ($c in $cands108) { $v108[$c.key] = New-Verdict }
$st = @{}
$firstLaunchTick = @{}
$headLaunches = 0
for ($tick = 0; $tick -lt 108; $tick++) {
    $r = Invoke-SimTick -Candidates $cands108 -Verdicts $v108 -State $st -Now $t0.AddMinutes($tick)
    foreach ($k in $r.launched) { if (-not $firstLaunchTick.ContainsKey($k)) { $firstLaunchTick[$k] = $tick } }
    if ($r.launched -contains 'p/T-000') { $headLaunches++ }
}
Check "every one of the 108 candidates launched within 108 ticks" ($firstLaunchTick.Count -eq 108) "launched=$($firstLaunchTick.Count)"
Check "the head was launched once in those 108 ticks, not every tick" ($headLaunches -eq 1) "headLaunches=$headLaunches"
Check "the deepest candidate (sweep position 108) got its turn" ($firstLaunchTick.ContainsKey('p/T-107'))
$maxWait = ($firstLaunchTick.Values | Measure-Object -Maximum).Maximum
Check "worst-case first launch is tick L-1 = 107" ($maxWait -eq 107) "max=$maxWait"
# Second lap: the head waits behind everyone it already passed.
$r = Invoke-SimTick -Candidates $cands108 -Verdicts $v108 -State $st -Now $t0.AddMinutes(108)
Check "lap 2 starts again with the candidate that has waited longest (the head, launched first in lap 1)" ($r.launched[0] -eq 'p/T-000') "got=$($r.launched -join ',')"

# =============================================================================================
# T-head-skip-progress-path-launch-rate-premise (Bohr msg-6040 D2). head_skip.py gives the progress
# path no rate cap on purpose; what keeps a thread that LAUNCHes on every tick from starving the
# others is this ordering, so it is pinned here: a candidate launched on consecutive ticks while it
# was the only LAUNCH candidate does not keep the front once another LAUNCH candidate is waiting.
Write-Host "progress path — a candidate launched on consecutive ticks yields to a waiting LAUNCH candidate"
$cP = @((New-Cand 'p/T-progress'), (New-Cand 'p/T-other'))
$stP = @{}
$vAlone = @{ 'p/T-progress' = (New-Verdict); 'p/T-other' = (New-Verdict -Decision 'skip') }
$aloneLaunches = 0
for ($tick = 0; $tick -lt 3; $tick++) {
    $r = Invoke-SimTick -Candidates $cP -Verdicts $vAlone -State $stP -Now $t0.AddMinutes($tick)
    if (@($r.launched) -contains 'p/T-progress') { $aloneLaunches++ }
}
Check "while it is the only LAUNCH candidate, the progress-path thread launches on every tick (no cap)" ($aloneLaunches -eq 3) "launches=$aloneLaunches"
$vBoth = @{ 'p/T-progress' = (New-Verdict); 'p/T-other' = (New-Verdict) }
# Tick 3: both wait from this tick, so the tie falls to sweep.json order and p/T-progress goes again;
# the role lane breaks on its worked run, so p/T-other waits.
$r3 = Invoke-SimTick -Candidates $cP -Verdicts $vBoth -State $stP -Now $t0.AddMinutes(3)
Check "tick 3: the tie falls to sweep.json order and the role lane breaks after one worked run" ((@($r3.launched) -join ',') -eq 'p/T-progress') "got=$($r3.launched -join ',')"
# Tick 4: p/T-progress's wait was cleared by its launch, p/T-other has waited since tick 3.
$r4 = Invoke-SimTick -Candidates $cP -Verdicts $vBoth -State $stP -Now $t0.AddMinutes(4)
Check "tick 4: the waiting candidate is launched ahead of the one launched on consecutive ticks" ((@($r4.launched) -join ',') -eq 'p/T-other') "got=$($r4.launched -join ',')"
$r5 = Invoke-SimTick -Candidates $cP -Verdicts $vBoth -State $stP -Now $t0.AddMinutes(5)
Check "tick 5: the two alternate, so neither holds the front" ((@($r5.launched) -join ',') -eq 'p/T-progress') "got=$($r5.launched -join ',')"

# =============================================================================================
Write-Host "gate lane — launched in the same tick whatever the head did"
$cG = @((New-Cand 'p/T-head'), (New-Cand 'p/T-mid'), (New-Cand 'p/T-gate-deep'))
$vG = @{ 'p/T-head' = (New-Verdict); 'p/T-mid' = (New-Verdict); 'p/T-gate-deep' = (New-Verdict -Token 'pr-review') }
$r = Invoke-SimTick -Candidates $cG -Verdicts $vG -State @{} -Now $t0
Check "gate at the bottom of sweep.json launched" ($r.launched -contains 'p/T-gate-deep')
Check "gate launched before the role turn" ($r.launched[0] -eq 'p/T-gate-deep') "order=$($r.launched -join ',')"
Check "one role turn after it (head did work and broke the role lane)" (($r.launched -join ',') -eq 'p/T-gate-deep,p/T-head') "order=$($r.launched -join ',')"

Write-Host "gate lane — gates that do work do not break the sweep"
$cG2 = @(1..3 | ForEach-Object { New-Cand "p/T-g$_" }) + @(New-Cand 'p/T-role')
$vG2 = @{}; foreach ($c in $cG2) { $vG2[$c.key] = if ($c.key -like '*-g*') { New-Verdict -Token 'pr-review' } else { New-Verdict } }
$r = Invoke-SimTick -Candidates $cG2 -Verdicts $vG2 -State @{} -Now $t0 -Rounds { param($k) 3 }
Check "3 working gates and then the role turn all ran" ($r.launched.Count -eq 4) "order=$($r.launched -join ',')"

Write-Host "gate lane — capped at G=3 launches; the cut gates lead the next tick"
$cG5 = @(1..5 | ForEach-Object { New-Cand "p/T-g$_" })
$vG5 = @{}; foreach ($c in $cG5) { $vG5[$c.key] = New-Verdict -Token 'pr-review' }
$stG5 = @{}
$r = Invoke-SimTick -Candidates $cG5 -Verdicts $vG5 -State $stG5 -Now $t0
Check "tick 1 launches exactly 3 gates" ($r.launched.Count -eq 3) "launched=$($r.launched -join ',')"
Check "the cap is recorded as gate-cap" ($r.sched.gate_closed_reason -eq 'gate-cap')
Check "g4/g5 keep launch_wait_since" (($null -ne (Get-LaunchWaitSince $stG5 'p/T-g4')) -and ($null -ne (Get-LaunchWaitSince $stG5 'p/T-g5')))
$r = Invoke-SimTick -Candidates $cG5 -Verdicts $vG5 -State $stG5 -Now $t0.AddMinutes(1)
Check "tick 2 launches g4 and g5 first" ((($r.launched | Select-Object -First 2) -join ',') -eq 'p/T-g4,p/T-g5') "launched=$($r.launched -join ',')"

Write-Host "gate lane — cut by the gate budget; the cut gate leads the next tick; the role turn still runs"
$cGB = @((New-Cand 'p/T-role'), (New-Cand 'p/T-g1'), (New-Cand 'p/T-g2'), (New-Cand 'p/T-g3'))
$vGB = @{ 'p/T-role' = (New-Verdict) }; foreach ($g in 'p/T-g1', 'p/T-g2', 'p/T-g3') { $vGB[$g] = New-Verdict -Token 'pr-review' }
$stGB = @{}
$r = Invoke-SimTick -Candidates $cGB -Verdicts $vGB -State $stGB -Now $t0 -PreLoop 10 -RunSeconds { param($k) if ($k -like '*-g*') { 40 } else { 1 } }
# g1 at loop 0 (exception), g2 at loop 40 (< 60), g3 at loop 80 (refused), role at tick 90 (< 120)
Check "g1 and g2 launched, g3 cut by gate-budget" ((($r.launched | Where-Object { $_ -like '*-g*' }) -join ',') -eq 'p/T-g1,p/T-g2') "launched=$($r.launched -join ',')"
Check "gate-budget recorded" ($r.sched.gate_closed_reason -eq 'gate-budget')
Check "role turn admitted after the gate lane closed" ($r.launched -contains 'p/T-role')
$r = Invoke-SimTick -Candidates $cGB -Verdicts $vGB -State $stGB -Now $t0.AddMinutes(1)
Check "next tick: g3 is the first launch" ($r.launched[0] -eq 'p/T-g3') "launched=$($r.launched -join ',')"

# =============================================================================================
Write-Host "K-budget — failures in either lane count to the same K, both lanes stop, waits kept"
$cK = @((New-Cand 'p/T-g1'), (New-Cand 'p/T-g2'), (New-Cand 'p/T-g3'), (New-Cand 'p/T-role'))
$vK = @{ 'p/T-role' = (New-Verdict) }; foreach ($g in 'p/T-g1', 'p/T-g2', 'p/T-g3') { $vK[$g] = New-Verdict -Token 'pr-review' }
$stK = @{}
$r = Invoke-SimTick -Candidates $cK -Verdicts $vK -State $stK -Now $t0 -ExitCode { param($k) 1 }
Check "breaks with k-budget-hit after 2 gate failures" (($r.breakReason -eq 'k-budget-hit') -and $r.launched.Count -eq 2) "reason=$($r.breakReason) launched=$($r.launched -join ',')"
Check "the role lane did not run" (-not ($r.launched -contains 'p/T-role'))
Check "g3 and the role turn keep launch_wait_since" (($null -ne (Get-LaunchWaitSince $stK 'p/T-g3')) -and ($null -ne (Get-LaunchWaitSince $stK 'p/T-role')))
Check "the launched (failed) gates had it cleared" ($null -eq (Get-LaunchWaitSince $stK 'p/T-g1'))

# =============================================================================================
Write-Host "launch_wait_since lifecycle — DEFER / held keep it, SKIP clears it, LAUNCH sets it once"
$old = $t0.AddHours(-3).ToString('o')
$stL = @{
    'p/T-defer' = @{ launch_wait_since = $old }
    'p/T-held'  = @{ launch_wait_since = $old }
    'p/T-skip'  = @{ launch_wait_since = $old }
    'p/T-again' = @{ launch_wait_since = $old }
}
$vL = @{ 'p/T-defer' = (New-Verdict -Decision 'defer'); 'p/T-skip' = (New-Verdict -Decision 'skip'); 'p/T-again' = (New-Verdict); 'p/T-new' = (New-Verdict) }
Update-LaunchWaitFromVerdicts -EvaluatedState $stL -Verdicts $vL -Now $t0
Check "DEFER keeps it" ((Get-LaunchWaitSince $stL 'p/T-defer') -eq [datetime]::Parse($old).ToUniversalTime())
Check "held (no verdict) keeps it" ((Get-LaunchWaitSince $stL 'p/T-held') -eq [datetime]::Parse($old).ToUniversalTime())
Check "SKIP clears it" ($null -eq (Get-LaunchWaitSince $stL 'p/T-skip'))
Check "LAUNCH does not move an existing value forward" ((Get-LaunchWaitSince $stL 'p/T-again') -eq [datetime]::Parse($old).ToUniversalTime())
Check "LAUNCH sets a missing value to now" ((Get-LaunchWaitSince $stL 'p/T-new') -eq $t0)

Write-Host "launch_wait_since survives the W-2c refresh (Update-EvaluatedTimestamp)"
$stW = @{ 'p/T-x' = @{ first_seen_at = $t0.AddDays(-2).ToString('o'); launch_wait_since = $t0.AddHours(-7).ToString('o') } }
Update-EvaluatedTimestamp -State $stW -Key 'p/T-x' -Now $t0
Check "last_evaluated_at refreshed" ((ConvertTo-UtcInstant $stW['p/T-x'].last_evaluated_at) -eq $t0)
Check "launch_wait_since unchanged" ((Get-LaunchWaitSince $stW 'p/T-x') -eq $t0.AddHours(-7))
Check "first_seen_at unchanged" ((ConvertTo-UtcInstant $stW['p/T-x'].first_seen_at) -eq $t0.AddDays(-2))
# JSON round trip: ConvertFrom-Json turns the ISO string into [DateTime] / PSCustomObject rows.
$rt = @{}; foreach ($p in (($stW | ConvertTo-Json -Depth 5 | ConvertFrom-Json).PSObject.Properties)) { $rt[$p.Name] = $p.Value }
Check "launch_wait_since reads back after a JSON round trip" ((Get-LaunchWaitSince $rt 'p/T-x') -eq $t0.AddHours(-7))
Update-EvaluatedTimestamp -State $rt -Key 'p/T-x' -Now $t0.AddMinutes(1)
Check "and survives a refresh of the round-tripped row" ((Get-LaunchWaitSince $rt 'p/T-x') -eq $t0.AddHours(-7))

Write-Host "ordering — LAUNCH candidates oldest wait first, ties in sweep.json order, non-launch first"
$cO = @('p/T-a', 'p/T-b', 'p/T-c', 'p/T-skip', 'p/T-d') | ForEach-Object { New-Cand $_ }
$vO = @{ 'p/T-a' = (New-Verdict); 'p/T-b' = (New-Verdict); 'p/T-c' = (New-Verdict); 'p/T-skip' = (New-Verdict -Decision 'skip'); 'p/T-d' = (New-Verdict -Token 'pr-review') }
$stO = @{ 'p/T-c' = @{ launch_wait_since = $t0.AddHours(-1).ToString('o') }; 'p/T-a' = @{ launch_wait_since = $t0.ToString('o') }; 'p/T-b' = @{ launch_wait_since = $t0.ToString('o') } }
$ord = (Get-OrderedSweepCandidates -Candidates $cO -Verdicts $vO -EvaluatedState $stO -Now $t0 | ForEach-Object { $_.key }) -join ','
Check "order = skip, gate, then c (older), a, b (tie by sweep order)" ($ord -eq 'p/T-skip,p/T-d,p/T-c,p/T-a,p/T-b') "got=$ord"

# PR #406 gate advisory: a value stored this tick (written as 'o', parsed back) and the $Now
# fallback for a missing value must compare EQUAL, so the tie falls to sweep.json order. A $Now
# with sub-tick-irrelevant but non-zero fractional ticks is used on purpose.
$nowFrac = $t0.AddTicks(1234567)
$cT = @('p/T-fallback', 'p/T-stored') | ForEach-Object { New-Cand $_ }
$vT = @{ 'p/T-fallback' = (New-Verdict); 'p/T-stored' = (New-Verdict) }
$stT2 = @{}
Set-LaunchWaitPending -EvaluatedState $stT2 -Key 'p/T-stored' -Now $nowFrac
$ordT = (Get-OrderedSweepCandidates -Candidates $cT -Verdicts $vT -EvaluatedState $stT2 -Now $nowFrac | ForEach-Object { $_.key }) -join ','
Check "stored-this-tick and fallback keys tie, so sweep.json order decides" ($ordT -eq 'p/T-fallback,p/T-stored') "got=$ordT"
Check "the stored value round-trips to the exact tick" ((Get-LaunchWaitSince $stT2 'p/T-stored').Ticks -eq $nowFrac.Ticks)

# =============================================================================================
Write-Host "budgets — slow decide (Gate < pre-loop < Launch) with one gate: gate exactly 1, then role 1"
$cS = @((New-Cand 'p/T-role'), (New-Cand 'p/T-gate'))
$vS = @{ 'p/T-role' = (New-Verdict); 'p/T-gate' = (New-Verdict -Token 'pr-review') }
$r = Invoke-SimTick -Candidates $cS -Verdicts $vS -State @{} -Now $t0 -PreLoop 65
Check "gate launched (not starved by the slow decide)" (($r.launched -join ',') -eq 'p/T-gate,p/T-role') "launched=$($r.launched -join ',')"

Write-Host "budgets — slow decide does not shrink the gate lane (loopElapsed excludes decide)"
$cS3 = @(1..3 | ForEach-Object { New-Cand "p/T-g$_" }) + @(New-Cand 'p/T-role')
$vS3 = @{}; foreach ($c in $cS3) { $vS3[$c.key] = if ($c.key -like '*-g*') { New-Verdict -Token 'pr-review' } else { New-Verdict } }
$r = Invoke-SimTick -Candidates $cS3 -Verdicts $vS3 -State @{} -Now $t0 -PreLoop 65
Check "3 gates and the role turn launched" ($r.launched.Count -eq 4) "launched=$($r.launched -join ',')"

Write-Host "budgets — loopElapsed past the gate budget stops later gates; the role turn is still admitted"
$r = Invoke-SimTick -Candidates $cS3 -Verdicts $vS3 -State @{} -Now $t0 -PreLoop 5 -RunSeconds { param($k) if ($k -like '*-g*') { 61 } else { 1 } }
Check "only the first gate launched" ((($r.launched | Where-Object { $_ -like '*-g*' }).Count) -eq 1) "launched=$($r.launched -join ',')"
Check "role turn launched (tick 66 < 120)" ($r.launched -contains 'p/T-role')

Write-Host "budgets — pre-loop >= Launch: nothing launches, time-budget, waits kept"
$stT = @{}
$r = Invoke-SimTick -Candidates $cS3 -Verdicts $vS3 -State $stT -Now $t0 -PreLoop 125
Check "zero launches" ($r.launched.Count -eq 0)
Check "breakReason=time-budget" ($r.breakReason -eq 'time-budget')
Check "every LAUNCH candidate keeps launch_wait_since" (@($cS3 | Where-Object { $null -eq (Get-LaunchWaitSince $stT $_.key) }).Count -eq 0)
$wrapperText = Get-Content -LiteralPath $sweepScript -Raw
Check "wrapper carries the WARN 'decide overhead exceeds launch budget'" ($wrapperText.Contains('decide overhead exceeds launch budget'))

Write-Host "budgets — role turn refused once the tick budget is spent"
$cR = @((New-Cand 'p/T-r1'), (New-Cand 'p/T-r2'))
$vR = @{ 'p/T-r1' = (New-Verdict); 'p/T-r2' = (New-Verdict) }
$r = Invoke-SimTick -Candidates $cR -Verdicts $vR -State @{} -Now $t0 -PreLoop 10 -Rounds { param($k) 0 } -RunSeconds { param($k) 115 }
Check "no-work role turn advances, but the next is refused at tick 125" ((($r.launched -join ',') -eq 'p/T-r1') -and $r.breakReason -eq 'time-budget') "launched=$($r.launched -join ',') reason=$($r.breakReason)"

Write-Host "budget resolution — argument > env > default; Gate >= Launch aborts"
$b = Resolve-SweepBudgets -LaunchArgument 0 -GateArgument 0 -LaunchEnv '' -GateEnv ''
Check "defaults 120 / 60" (($b.launch -eq 120) -and ($b.gate -eq 60) -and $b.launch_source -eq 'default')
$b = Resolve-SweepBudgets -LaunchArgument 0 -GateArgument 0 -LaunchEnv '200' -GateEnv '90'
Check "env over default" (($b.launch -eq 200) -and ($b.gate -eq 90) -and $b.gate_source -eq 'env')
$b = Resolve-SweepBudgets -LaunchArgument 300 -GateArgument 100 -LaunchEnv '200' -GateEnv '90'
Check "argument over env" (($b.launch -eq 300) -and ($b.gate -eq 100) -and $b.launch_source -eq 'argument')
$b = Resolve-SweepBudgets -LaunchArgument 300 -GateArgument 0 -LaunchEnv '' -GateEnv '90'
Check "each budget resolves independently" (($b.launch -eq 300) -and ($b.gate -eq 90))
foreach ($bad in @(@(60, 60), @(60, 61))) {
    $threw = $false
    try { [void](Resolve-SweepBudgets -LaunchArgument $bad[0] -GateArgument $bad[1] -LaunchEnv '' -GateEnv '') } catch { $threw = $_.Exception.Message -match 'must be less than' }
    Check "Gate=$($bad[1]) >= Launch=$($bad[0]) throws (tick aborts)" $threw
}
$threw = $false
try { [void](Resolve-SweepBudgets -LaunchArgument 0 -GateArgument 0 -LaunchEnv 'abc' -GateEnv '') } catch { $threw = $_.Exception.Message -match 'not a positive integer' }
Check "a non-integer env value throws rather than falling back" $threw
$launchParam = $ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'LaunchBudgetSeconds' }
$gateParam = $ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'GateBudgetSeconds' }
Check "wrapper declares -LaunchBudgetSeconds and -GateBudgetSeconds" (($null -ne $launchParam) -and ($null -ne $gateParam))

# =============================================================================================
Write-Host "lease-busy (modelled non-launch) — exception, G, and the gate budget"
$cL = @(1..5 | ForEach-Object { New-Cand "p/T-g$_" }) + @(New-Cand 'p/T-role')
$vL5 = @{}; foreach ($c in $cL) { $vL5[$c.key] = if ($c.key -like '*-g*') { New-Verdict -Token 'pr-review' } else { New-Verdict } }

$r = Invoke-SimTick -Candidates $cL -Verdicts $vL5 -State @{} -Now $t0 -PreLoop 65 -LeaseOk { param($k) $k -ne 'p/T-g1' }
Check "first gate lease-busy, second launches under the normal rule even with decide > Gate" ($r.launched[0] -eq 'p/T-g2') "launched=$($r.launched -join ',')"
$exc = @($r.sched.attempts | Where-Object { $_.exception })
Check "only the first gate candidate used the exception" (($exc.Count -eq 1) -and $exc[0].key -eq 'p/T-g1')

$stLB = @{}
$r = Invoke-SimTick -Candidates $cL -Verdicts $vL5 -State $stLB -Now $t0 -LeaseOk { param($k) $k -notlike '*-g*' }
Check "all gates lease-busy: role turn still launched" (($r.launched -join ',') -eq 'p/T-role') "launched=$($r.launched -join ',')"
Check "all gates keep launch_wait_since" (@(1..5 | Where-Object { $null -eq (Get-LaunchWaitSince $stLB "p/T-g$_") }).Count -eq 0)

$r = Invoke-SimTick -Candidates $cL -Verdicts $vL5 -State @{} -Now $t0 -LeaseOk { param($k) $k -notin @('p/T-g1', 'p/T-g2') }
Check "non-launch attempts do not count toward G (2 busy, then 3 launched)" ((@($r.launched | Where-Object { $_ -like '*-g*' }) -join ',') -eq 'p/T-g3,p/T-g4,p/T-g5') "launched=$($r.launched -join ',')"

$r = Invoke-SimTick -Candidates $cL -Verdicts $vL5 -State @{} -Now $t0 -PreLoop 5 -LeaseOk { param($k) $k -notlike '*-g*' } -LeaseSeconds { param($k) 25 }
# busy attempts at loop 0 (exception), 25, 50; at 75 the gate budget closes the lane; role at tick 80
Check "failing gate attempts stop at the gate budget (3 attempts, not 5)" (@($r.sched.attempts | Where-Object { $_.lane -eq 'gate' -and $_.admitted }).Count -eq 3)
Check "and the role turn is admitted in what is left of the launch budget" ($r.launched -contains 'p/T-role')

Write-Host "admission — the first-gate exception, unit level"
# In the dispatch order gates come right after the non-launch candidates, so loopElapsed is ~0 at
# the first gate and the exception rarely decides anything in a tick. Pin it directly, so removing
# it fails here and not only in the bookkeeping check above.
$sx = New-LaunchScheduler -LaunchBudgetSeconds 120 -GateBudgetSeconds 60
$a1 = Request-LaunchAdmission -Scheduler $sx -Lane 'gate' -Key 'g1' -TickElapsed 90 -LoopElapsed 70
$a2 = Request-LaunchAdmission -Scheduler $sx -Lane 'gate' -Key 'g2' -TickElapsed 90 -LoopElapsed 70
$a3 = Request-LaunchAdmission -Scheduler $sx -Lane 'role' -Key 'r1' -TickElapsed 90 -LoopElapsed 70
Check "first gate admitted on the tick budget alone (loopElapsed 70 >= gate 60)" ($a1.admit -and $a1.exception)
Check "second gate refused by the gate budget (scope skip-lane)" ((-not $a2.admit) -and $a2.reason -eq 'gate-budget' -and $a2.scope -eq 'skip-lane')
Check "role turn admitted (tick 90 < 120)" ($a3.admit)
$sy = New-LaunchScheduler -LaunchBudgetSeconds 120 -GateBudgetSeconds 60
$b1 = Request-LaunchAdmission -Scheduler $sy -Lane 'gate' -Key 'g1' -TickElapsed 120 -LoopElapsed 0
Check "first gate refused once tickElapsed >= Launch (scope break), and the exception is spent" ((-not $b1.admit) -and $b1.scope -eq 'break' -and $sy.first_gate_evaluated)
$sz = New-LaunchScheduler -LaunchBudgetSeconds 120 -GateBudgetSeconds 60
$c1 = Request-LaunchAdmission -Scheduler $sz -Lane 'role' -Key 'r1' -TickElapsed 119.9 -LoopElapsed 0
$c2 = Request-LaunchAdmission -Scheduler $sz -Lane 'role' -Key 'r2' -TickElapsed 120 -LoopElapsed 0
Check "role: 119.9 admitted, 120 refused (strict <)" ($c1.admit -and (-not $c2.admit) -and $c2.reason -eq 'time-budget')

# =============================================================================================
Write-Host "property — gate candidates present and a role turn admitted => a gate was attempted first"
$rng = [System.Random]::new(20261002)
$violations = 0; $exceptionMisuse = 0; $rolesAdmittedWithGates = 0
for ($i = 0; $i -lt 400; $i++) {
    $n = $rng.Next(1, 12)
    $cP = @(0..($n - 1) | ForEach-Object { New-Cand "p/T-$_" })
    $vP = @{}
    foreach ($c in $cP) {
        $roll = $rng.Next(0, 10)
        $vP[$c.key] = if ($roll -lt 4) { New-Verdict -Token 'pr-review' } elseif ($roll -lt 8) { New-Verdict } elseif ($roll -lt 9) { New-Verdict -Decision 'defer' } else { New-Verdict -Decision 'skip' }
    }
    $durations = @{}; $busy = @{}; $busySec = @{}; $rounds = @{}
    foreach ($c in $cP) {
        $durations[$c.key] = $rng.Next(0, 90); $busy[$c.key] = ($rng.Next(0, 3) -eq 0)
        $busySec[$c.key] = $rng.Next(0, 30); $rounds[$c.key] = $rng.Next(0, 2)
    }
    $launchB = $rng.Next(30, 200); $gateB = $rng.Next(1, $launchB)
    $r = Invoke-SimTick -Candidates $cP -Verdicts $vP -State @{} -Now $t0 -LaunchBudget $launchB -GateBudget $gateB `
        -PreLoop ($rng.Next(0, 220)) -RunSeconds { param($k) $durations[$k] }.GetNewClosure() `
        -LeaseOk { param($k) -not $busy[$k] }.GetNewClosure() -LeaseSeconds { param($k) $busySec[$k] }.GetNewClosure() `
        -Rounds { param($k) $rounds[$k] }.GetNewClosure()
    if ($null -ne (Test-GateAdmissionInvariant -Scheduler $r.sched -GateCandidateCount $r.gateCount)) { $violations++ }
    $gateAttempts = @($r.sched.attempts | Where-Object { $_.lane -eq 'gate' })
    $exc = @($r.sched.attempts | Where-Object { $_.exception })
    if ($exc.Count -gt 1 -or ($exc.Count -eq 1 -and $exc[0].key -ne $gateAttempts[0].key)) { $exceptionMisuse++ }
    if ($r.gateCount -gt 0 -and @($r.sched.attempts | Where-Object { $_.lane -eq 'role' -and $_.admitted }).Count -gt 0) { $rolesAdmittedWithGates++ }
}
Check "invariant holds in 400 randomised ticks" ($violations -eq 0) "violations=$violations"
Check "exception used only by the first gate candidate" ($exceptionMisuse -eq 0) "misuse=$exceptionMisuse"
Check "the property was exercised (role admitted alongside gate candidates in some ticks)" ($rolesAdmittedWithGates -gt 20) "n=$rolesAdmittedWithGates"

Write-Host "property — the invariant checker itself catches a violation"
$bad = New-LaunchScheduler -LaunchBudgetSeconds 120 -GateBudgetSeconds 60
[void]$bad.attempts.Add([PSCustomObject]@{ key = 'p/T-r'; lane = 'role'; admitted = $true; reason = 'ok'; exception = $false; outcome = 'launched' })
Check "role admitted with gate candidates and no gate attempt -> reported" ($null -ne (Test-GateAdmissionInvariant -Scheduler $bad -GateCandidateCount 1))

# =============================================================================================
Write-Host "launch-wait starvation — 6h, separate from the evaluation metric"
$six = [TimeSpan]::FromHours(6)
$stS = @{
    'p/T-old'   = @{ last_evaluated_at = $t0.ToString('o'); launch_wait_since = $t0.AddHours(-31).ToString('o') }
    'p/T-edge'  = @{ last_evaluated_at = $t0.ToString('o'); launch_wait_since = $t0.AddHours(-6).ToString('o') }
    'p/T-young' = @{ last_evaluated_at = $t0.ToString('o'); launch_wait_since = $t0.AddHours(-5).ToString('o') }
    'p/T-gone'  = @{ last_evaluated_at = $t0.ToString('o'); launch_wait_since = $t0.AddHours(-40).ToString('o') }
}
$live = @('p/T-old', 'p/T-edge', 'p/T-young')
$lw = Get-LaunchWaitStarved -EvaluatedState $stS -LiveKeys $live -Now $t0 -Threshold $six
Check "31h and exactly 6h are reported, 5h is not, non-live is not" ((($lw | ForEach-Object { $_.key }) -join ',') -eq 'p/T-old,p/T-edge') "got=$(($lw | ForEach-Object { $_.key }) -join ',')"
Check "the evaluation metric does not see them (all evaluated this tick)" ((Get-StarvedKeys -EvaluatedState $stS -Now $t0 -LiveKeys $live).Count -eq 0)
foreach ($k in $live) { Update-EvaluatedTimestamp -State $stS -Key $k -Now $t0.AddMinutes(1) }
$lw2 = Get-LaunchWaitStarved -EvaluatedState $stS -LiveKeys $live -Now $t0.AddMinutes(1) -Threshold $six
Check "a W-2c refresh does not reset launch-wait starvation" ($lw2.Count -eq 2)

Write-Host "digest — 起動待ち飢餓 section"
$dg = New-DailyDigest -QuarantineState @{} -EvaluatedState $stS -HeadsByProject @{} -ControlByProject @{} -Now $t0 `
    -LiveKeys $live -LaunchWaitStarved $lw -LaunchWaitThreshold $six -Budget 1950
Check "section header carries the count and the threshold" ($dg -match '起動待ち飢餓 \(LAUNCH 判定のまま 6h 以上起動されていない\): 2 件')
Check "rows list the keys, oldest first" ($dg.IndexOf('  p/T-old ') -gt 0 -and $dg.IndexOf('  p/T-old ') -lt $dg.IndexOf('  p/T-edge '))
Check "summary line counts it" ($dg -match '/ 起動待ち 2 /')
Check "飢餓 (evaluation) section is still there, separately" ($dg -match '飢餓 \(24h 以上評価されていない\): 0 件')
$dg0 = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} -HeadsByProject @{} -ControlByProject @{} -Now $t0 -LiveKeys @()
Check "0 件 still renders the section" ($dg0 -match '起動待ち飢餓 .*: 0 件')
$many = @(0..59 | ForEach-Object { [PSCustomObject]@{ key = ("spirrow-mindwire/T-launch-wait-uniform-{0:D2}" -f $_); age = [TimeSpan]::FromHours(30) } })
$dgMany = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} -HeadsByProject @{} -ControlByProject @{} -Now $t0 `
    -LiveKeys @() -LaunchWaitStarved $many -Budget 1950
Check "60 rows still fit the budget" ($dgMany.Length -le 1950) "len=$($dgMany.Length)"
Check "and the overflow is counted" ($dgMany -match '\+\d+ 件（省略）')

# =============================================================================================
Write-Host "wiring — the wrapper's dispatch loop calls the lib (the simulation above is not a copy)"
$spawn = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and
    $n.InvocationOperator -eq 'Ampersand' -and $n.CommandElements[0].Extent.Text -eq '$inner' }, $true))
$loop = $spawn[0].Parent
while ($null -ne $loop -and -not ($loop -is [System.Management.Automation.Language.ForEachStatementAst])) { $loop = $loop.Parent }
Check "dispatch loop found" ($null -ne $loop)
$calls = @($loop.Body.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) | ForEach-Object { $_.GetCommandName() })
foreach ($fnName in 'Get-SweepLane', 'Request-LaunchAdmission', 'Complete-LaunchAttempt', 'Clear-LaunchWait', 'Get-PostRunAction') {
    Check "dispatch loop calls $fnName" ($calls -contains $fnName)
}
$admIdx = $loop.Body.Extent.Text.IndexOf('Request-LaunchAdmission')
$commitIdx = $loop.Body.Extent.Text.IndexOf('Invoke-HeadSkipCommitLaunch')
$spawnIdx = $loop.Body.Extent.Text.IndexOf('& $inner')
$clearIdx = $loop.Body.Extent.Text.IndexOf('Clear-LaunchWait')
Check "admission is checked before commit-launch" (($admIdx -gt 0) -and ($admIdx -lt $commitIdx))
Check "the wait clears after commit-launch and before the spawn" (($clearIdx -gt $commitIdx) -and ($clearIdx -lt $spawnIdx))
# PR #406 gate round 2: Invoke-SimTick is a skeleton of the dispatch loop, so a control-flow guard
# that exists only in the wrapper (for example `if ($didWork) { continue }` at the top of the body)
# would be invisible to the simulation and could silently cut the lanes short. The pins below close
# that gap on the REAL loop's AST:
#   (a) $didWork is never READ inside the dispatch loop. It is only assigned there, and its single
#       reader is the ALL-CANDIDATES-IDLE summary after the loop.
#   (b) the loop's own `break` statements (nearest enclosing loop = the dispatch loop) are exactly
#       the five exits the design names: time-budget, kill-unconfirmed (conductor exit 7, PR #407
#       W-3), k-budget-hit, undeclared-verdict, and the role-lane post-run break. Adding one forces
#       this test to be revisited.
#   (b') the kill-unconfirmed break sits under an `if` on `$ConductorKillUnconfirmedExitCode`, so it
#       fires only on exit 7 and cannot widen into a general failure break.
#   (c) the post-run break sits under an `if` that calls Get-PostRunAction, so whether a worked run
#       ends the sweep is decided by the lib function the simulation exercises.
function Get-NearestLoop {
    param($Node)
    $p = $Node.Parent
    while ($null -ne $p -and -not ($p -is [System.Management.Automation.Language.LoopStatementAst] -or
                                   $p -is [System.Management.Automation.Language.ForEachStatementAst])) { $p = $p.Parent }
    return $p
}
$didWorkReads = @($loop.Body.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] -and
    $n.VariablePath.UserPath -eq 'didWork' -and -not ($n.Parent -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $n.Parent.Left -eq $n) }, $true))
Check "(a) `$didWork is never read inside the dispatch loop" ($didWorkReads.Count -eq 0) "reads at line(s) $(($didWorkReads | ForEach-Object { $_.Extent.StartLineNumber }) -join ',')"
$ownBreaks = @($loop.Body.FindAll({ param($n) $n -is [System.Management.Automation.Language.BreakStatementAst] }, $true) |
    Where-Object { (Get-NearestLoop $_) -eq $loop })
Check "(b) the dispatch loop has exactly 5 own break exits" ($ownBreaks.Count -eq 5) "found $($ownBreaks.Count) at line(s) $(($ownBreaks | ForEach-Object { $_.Extent.StartLineNumber }) -join ',')"
$killBreak = @($ownBreaks | Where-Object {
    $p = $_.Parent; $found = $false
    while ($null -ne $p -and $p -ne $loop) {
        if ($p -is [System.Management.Automation.Language.IfStatementAst] -and
            $p.Clauses[0].Item1.Extent.Text -match '\$ConductorKillUnconfirmedExitCode') { $found = $true; break }
        $p = $p.Parent
    }
    $found })
Check "(b') the kill-unconfirmed break is governed by `$ConductorKillUnconfirmedExitCode" ($killBreak.Count -eq 1)
$postRunBreak = @($ownBreaks | Where-Object {
    $p = $_.Parent; $found = $false
    while ($null -ne $p -and $p -ne $loop) {
        if ($p -is [System.Management.Automation.Language.IfStatementAst] -and
            $p.Clauses[0].Item1.Extent.Text -match 'Get-PostRunAction') { $found = $true; break }
        $p = $p.Parent
    }
    $found })
Check "(c) the worked-run break is governed by Get-PostRunAction" ($postRunBreak.Count -eq 1)
$orderAssign = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $n.Left.Extent.Text -eq '$candidates' -and $n.Right.Extent.Text -like 'Get-OrderedSweepCandidates*' }, $true))
Check "the loop's `$candidates is the ordered list" ($orderAssign.Count -eq 1 -and $orderAssign[0].Extent.StartOffset -lt $loop.Extent.StartOffset)
$runTopCalls = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) | ForEach-Object { $_.GetCommandName() })
foreach ($fnName in 'Resolve-SweepBudgets', 'Update-LaunchWaitFromVerdicts', 'Get-LaunchWaitStarved', 'Test-GateAdmissionInvariant') {
    Check "wrapper calls $fnName" ($runTopCalls -contains $fnName)
}

# =============================================================================================
# Gate-only slice (PR-B, Bohr msg-6313 §1′ / msg-6316): a gate-lane launch passes --gate-only, so the
# conductor stops before the implementer's turn and that turn waits in the role lane instead.
Write-Host "gate-only slice — lane arguments"
Check "gate lane gets --gate-only" ((@(Get-ConductorLaneArgs -Lane 'gate') -join ' ') -eq '--gate-only')
Check "role lane gets nothing" (@(Get-ConductorLaneArgs -Lane 'role').Count -eq 0)
Check "a non-launch lane gets nothing" (@(Get-ConductorLaneArgs -Lane 'none').Count -eq 0)

Write-Host "gate-only slice — wiring (only the gate-lane launch carries the flag)"
$laneArgCalls = @($loop.Body.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and
    $n.GetCommandName() -eq 'Get-ConductorLaneArgs' }, $true))
Check "dispatch loop calls Get-ConductorLaneArgs exactly once" ($laneArgCalls.Count -eq 1)
Check "and passes it the loop's own lane (-Lane `$lane)" ($laneArgCalls.Count -eq 1 -and $laneArgCalls[0].Extent.Text -match '-Lane\s+\$lane\b')
$laneAppend = @($loop.Body.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $n.Left.Extent.Text -eq '$laneArgs' -and $n.Operator -eq 'Equals' -and $n.Right.Extent.Text -match 'Get-ConductorLaneArgs' }, $true))
Check "its result is (re-)assigned to its own `$laneArgs on every candidate, not appended" ($laneAppend.Count -eq 1)
Check "before the conductor is spawned" ($laneAppend.Count -eq 1 -and $laneAppend[0].Extent.StartOffset -lt $spawn[0].Extent.StartOffset)
Check "and the spawn splats it" (@($spawn[0].CommandElements | Where-Object { $_.Extent.Text -eq '@laneArgs' }).Count -eq 1)
$stallAppend = @($loop.Body.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $n.Left.Extent.Text -eq '$stallArgs' -and $n.Right.Extent.Text -match 'Get-ConductorLaneArgs' }, $true))
Check "lane arguments never go into `$stallArgs" ($stallAppend.Count -eq 0)
$wrapperText = Get-Content -LiteralPath $sweepScript -Raw
$innerText = Get-Content -LiteralPath (Join-Path $repoRoot 'deploy/run-conductor.ps1') -Raw
Check "the wrapper never spells --gate-only itself (the lib decides)" (-not $wrapperText.Contains('--gate-only'))
Check "a hand run (deploy/run-conductor.ps1) never adds --gate-only" (-not $innerText.Contains('--gate-only'))

# Six simulated hours of wall-clock with gate candidates arriving all the time. Durations come from
# the §0 measurement (msg-6312): a gate review ~200s, the implementer turn after it ~500s, a role
# turn ~300s. Without the slice the gate run carries the implementer turn (700s); with it the gate
# run is 200s and the implementer turn comes back next tick as a role-lane LAUNCH, queued by
# launch_wait_since like any other. The scheduling is the real lib, through Invoke-SimTick.
function Invoke-SliceSim {
    param([bool]$Slice, [int]$Horizon = 21600, [int]$GateEvery = 900, [int]$RoleEvery = 1200)
    $pending = [ordered]@{}    # key -> @{ kind = 'gate'|'role'; since = seconds; seconds = run length; orig = 'role'|'impl' }
    $arrivals = [System.Collections.ArrayList]::new()
    for ($s = 0; $s -lt $Horizon; $s += $GateEvery) { [void]$arrivals.Add(@{ at = $s; key = "p/T-g$s"; kind = 'gate' }) }
    foreach ($i in 0..3) { [void]$arrivals.Add(@{ at = 0; key = "p/T-r0-$i"; kind = 'role' }) }
    for ($s = $RoleEvery; $s -lt $Horizon; $s += $RoleEvery) { [void]$arrivals.Add(@{ at = $s; key = "p/T-r$s"; kind = 'role' }) }
    $arrivals = @($arrivals | Sort-Object { $_.at })
    $next = 0; $T = 0.0; $state = @{}
    $roleWaits = @(); $implWaits = @()
    while ($T -lt $Horizon) {
        while ($next -lt $arrivals.Count -and $arrivals[$next].at -le $T) {
            $a = $arrivals[$next]; $next++
            $pending[$a.key] = @{ kind = $a.kind; since = [double]$a.at; seconds = $(if ($a.kind -eq 'gate') { 200 } else { 300 }); orig = 'role' }
        }
        if ($pending.Count -eq 0) { $T += 60; continue }
        $cands = @($pending.Keys | ForEach-Object { New-Cand $_ })
        $verdicts = @{}
        foreach ($k in $pending.Keys) { $verdicts[$k] = New-Verdict -Token $(if ($pending[$k].kind -eq 'gate') { 'pr-review' } else { 'heisenberg' }) }
        $dur = @{}
        foreach ($k in $pending.Keys) {
            $p = $pending[$k]
            $dur[$k] = if ($p.kind -eq 'gate' -and -not $Slice) { 700 } else { $p.seconds }
        }
        $r = Invoke-SimTick -Candidates $cands -Verdicts $verdicts -State $state -Now $t0.AddSeconds($T) -PreLoop 10 `
            -RunSeconds { param($k) $dur[$k] }.GetNewClosure()
        $clock = $T + 10
        foreach ($k in $r.launched) {
            $p = $pending[$k]
            $end = $clock + $dur[$k]
            if ($p.kind -eq 'gate') {
                $pending.Remove($k)
                if ($Slice) { $pending[$k] = @{ kind = 'role'; since = $end; seconds = 500; orig = 'impl' } }
            }
            else {
                if ($p.orig -eq 'impl') { $implWaits += ($clock - $p.since) } else { $roleWaits += ($clock - $p.since) }
                $pending.Remove($k)
            }
            $clock = $end
        }
        $T = if ($r.launched.Count -gt 0) { $clock } else { $T + 60 }
    }
    $stillWaiting = @($pending.Values | Where-Object { $_.kind -eq 'role' -and $_.orig -eq 'role' } | ForEach-Object { $Horizon - $_.since })
    $allRole = @($roleWaits) + $stillWaiting
    return @{
        role_max = ($allRole | Measure-Object -Maximum).Maximum
        role_done = $roleWaits.Count
        impl_max = $(if ($implWaits.Count) { ($implWaits | Measure-Object -Maximum).Maximum } else { 0 })
        impl_done = $implWaits.Count
    }
}
Write-Host "gate-only slice — simulation (6h, a gate candidate every 15 min)"
$before = Invoke-SliceSim -Slice $false
$after = Invoke-SliceSim -Slice $true
Write-Host ("  before: role wait max {0:N0}s over {1} role turns" -f $before.role_max, $before.role_done)
Write-Host ("  after:  role wait max {0:N0}s over {1} role turns; implementer turns {2}, wait max {3:N0}s" -f $after.role_max, $after.role_done, $after.impl_done, $after.impl_max)
Check "the longest role-lane wait shrinks with the slice" ($after.role_max -lt $before.role_max) "before=$($before.role_max) after=$($after.role_max)"
Check "more role turns complete with the slice" ($after.role_done -gt $before.role_done) "before=$($before.role_done) after=$($after.role_done)"
Check "sliced implementer turns do get launched from the role lane" ($after.impl_done -gt 0)

# =============================================================================================
# Summon (T-human-call-priority-interrupt PR-2). Spec: Bohr msg-6755 (v1.6) — lane 0, held-by-summon,
# the summon_yield record and Update-SummonYield — with msg-6747 (I-SY1..I-SY4), msg-6749 (v1.3
# tests), msg-6751 (v1.4 single lifecycle), msg-6753 (v1.5 I-SY5 tests) and msg-6800 (v1.7 contract).
# The two shared fixtures under tests/data/summon/ are run here and by tests/test_conductor_summon.py.
$summonData = Join-Path $repoRoot 'tests/data/summon'

# A fixture value's raw JSON text ($null when the property is absent), read with System.Text.Json so
# nothing is date-converted — the same reason the probe hands the value over as a string.
function Get-RawJsonProp {
    param([System.Text.Json.JsonElement]$Element, [string]$Name)
    foreach ($p in $Element.EnumerateObject()) { if ($p.Name -eq $Name) { return $p.Value.GetRawText() } }
    return $null
}

Write-Host "summon — active_summon contract fixture (shared with pytest)"
$contractDoc = [System.Text.Json.JsonDocument]::Parse((Get-Content -LiteralPath (Join-Path $summonData 'active_summon_contract.json') -Raw))
$contractCases = 0
foreach ($case in $contractDoc.RootElement.GetProperty('cases').EnumerateArray()) {
    $contractCases++
    $name = $case.GetProperty('name').GetString()
    $payload = $case.GetProperty('payload')
    $r = ConvertFrom-ActiveSummonJson -Json (Get-RawJsonProp -Element $payload -Name 'active_summon')
    $expect = $case.GetProperty('expect')
    $kind = $expect.GetProperty('kind').GetString()
    $ok = $r.kind -eq $kind
    if ($ok -and $kind -eq 'valid') {
        # -ceq: PowerShell's -eq ignores case, which would hide a case-folding parser.
        $ok = $r.thread_id -ceq $expect.GetProperty('thread_id').GetString() -and
              $r.by -ceq $expect.GetProperty('by').GetString() -and
              $r.since -eq (ConvertFrom-SummonSince -Value $expect.GetProperty('since').GetString())
    }
    Check "contract: $name -> $kind" $ok "got kind=$($r.kind) thread=$($r.thread_id) since=$($r.since)"
}
Check "contract fixture has cases" ($contractCases -ge 10)
Check "a since that ConvertFrom-Json would turn into a [datetime] still reads as the contract says" (
    (ConvertFrom-ActiveSummonJson -Json '{"thread_id":"T","since":"2026-10-09T01:02:03","by":"h"}').kind -eq 'malformed')

Write-Host "summon — summon_yield_decision fixture (shared with pytest)"
$decisionDoc = [System.Text.Json.JsonDocument]::Parse((Get-Content -LiteralPath (Join-Path $summonData 'summon_yield_decision.json') -Raw))
foreach ($case in $decisionDoc.RootElement.GetProperty('cases').EnumerateArray()) {
    $name = $case.GetProperty('name').GetString()
    $own = ConvertFrom-ActiveSummonJson -Json (Get-RawJsonProp -Element $case -Name 'own')
    $owner = ConvertFrom-ActiveSummonJson -Json (Get-RawJsonProp -Element $case -Name 'owner')
    $ownerProject = $case.GetProperty('owner_project'); $ownerProject = if ("$($ownerProject.ValueKind)" -eq 'Null') { $null } else { $ownerProject.GetString() }
    $exp = $case.GetProperty('expect_yield_to'); $exp = if ("$($exp.ValueKind)" -eq 'Null') { $null } else { $exp.GetString() }
    $got = Get-SummonYieldDecision -Project $case.GetProperty('project').GetString() -ThreadId $case.GetProperty('thread_id').GetString() `
        -OwnSummon $own -OwnerProject $ownerProject -OwnerSummon $owner
    Check "decision: $name -> $(if ($exp) { $exp } else { 'none' })" ($got -eq $exp) "got '$got'"
}

Write-Host "summon — Update-SummonYield (I-SY5, v1.6 signature; paths (a) and (b) in one table)"
$tNow = [datetime]::new(2026, 10, 9, 3, 0, 0, [DateTimeKind]::Utc)
function New-SY { param([string]$Sha, [string[]]$Threads) @{ main_sha_at_yield = $Sha; yielded_at = '2026-10-09T01:00:00.0000000Z'; summon_threads = $Threads } }
function Format-SY { param($E) if ($null -eq $E) { 'none' } else { "$($E.main_sha_at_yield)[$($E.summon_threads -join ',')]" } }
$syTable = @(
    @{ n = '(a) first yield, no record: created at the post-run SHA';      e = $null;                 d = $false; y = 'S1'; sha = 'c'; want = 'c[S1]' }
    @{ n = '(b) first held-by-summon, no record: created';                  e = $null;                 d = $false; y = 'S';  sha = 'c'; want = 'c[S]' }
    @{ n = '(a) then (b) to another Summon: SHA kept, appended';            e = (New-SY 'a' @('S1')); d = $false; y = 'S2'; sha = 'c'; want = 'a[S1,S2]' }
    @{ n = '(b) then (a) to another Summon: same';                          e = (New-SY 'a' @('S2')); d = $false; y = 'S1'; sha = 'c'; want = 'a[S2,S1]' }
    @{ n = 'the same Summon again: no duplicate, SHA kept';                 e = (New-SY 'a' @('S1')); d = $false; y = 'S1'; sha = 'c'; want = 'a[S1]' }
    @{ n = 'delivered then yielded again: only the new wait remains';      e = (New-SY 'a' @('S1')); d = $true;  y = 'S2'; sha = 'c'; want = 'c[S2]' }
    @{ n = 'yielded before delivering: the first SHA is kept';              e = (New-SY 'a' @('S1')); d = $false; y = 'S2'; sha = 'c'; want = 'a[S1,S2]' }
    @{ n = 'delivered only: removed';                                       e = (New-SY 'a' @('S1')); d = $true;  y = $null; sha = 'c'; want = 'none' }
    @{ n = 'neither: kept as it was';                                       e = (New-SY 'a' @('S1')); d = $false; y = $null; sha = 'c'; want = 'a[S1]' }
    @{ n = 'no record, nothing happened: still none';                       e = $null;                 d = $false; y = $null; sha = 'c'; want = 'none' }
    @{ n = 'SHA unreadable on create: unknown';                             e = $null;                 d = $false; y = 'S1'; sha = $null; want = 'unknown[S1]' }
)
foreach ($row in $syTable) {
    $got = Update-SummonYield -Entry $row.e -Delivered $row.d -YieldTo $row.y -MainSha $row.sha -Now $tNow
    Check "Update-SummonYield: $($row.n)" ((Format-SY $got) -eq $row.want) "got $(Format-SY $got)"
}
$created = Update-SummonYield -Entry $null -Delivered $false -YieldTo 'S1' -MainSha 'c' -Now $tNow
Check "a created record carries yielded_at" ($created.yielded_at -eq $tNow.ToString('o'))
$roundTrip = (@{ k = @{ summon_yield = (New-SY 'a' @('S1')) } } | ConvertTo-Json -Depth 5 | ConvertFrom-Json)
$rtEntry = ConvertTo-SummonYieldEntry -Entry $roundTrip.k.summon_yield
Check "a record read back from JSON (yielded_at as [datetime], one-element array) normalises" (
    (Format-SY $rtEntry) -eq 'a[S1]' -and $rtEntry.yielded_at -like '2026-10-09T01:00:00*')
Check "Test-SummonYieldNeedsSha: only when a record is created" (
    (Test-SummonYieldNeedsSha -Entry $null -Delivered $false -YieldTo 'S') -and
    (Test-SummonYieldNeedsSha -Entry (New-SY 'a' @('S1')) -Delivered $true -YieldTo 'S2') -and
    -not (Test-SummonYieldNeedsSha -Entry (New-SY 'a' @('S1')) -Delivered $false -YieldTo 'S2') -and
    -not (Test-SummonYieldNeedsSha -Entry $null -Delivered $true -YieldTo $null))

Write-Host "summon — the stop line (Get-SummonStopFields, StopReason.ps1)"
$sl = 'conductor stopped: reason=summon_yield rounds=1 forced_naysayer=0 forced_naysayer_saveable=0 last_msg=m2'
$f = Get-SummonStopFields -Output @('noise', "$sl yield_to=S2 resume_notice=delivered")
Check "delivered and yield_to on one line are both read" ($f.delivered -and $f.yield_to -eq 'S2')
$f = Get-SummonStopFields -Output @("$sl yield_to=S1")
Check "yield_to alone" (-not $f.delivered -and $f.yield_to -eq 'S1')
$f = Get-SummonStopFields -Output @('conductor stopped: reason=none rounds=1 forced_naysayer=0 forced_naysayer_saveable=0 last_msg=m3 resume_notice=delivered')
Check "delivered alone" ($f.delivered -and $null -eq $f.yield_to)
$f = Get-SummonStopFields -Output @('conductor stopped: reason=hold rounds=0 forced_naysayer=0 forced_naysayer_saveable=0 last_msg=None')
Check "neither" (-not $f.delivered -and $null -eq $f.yield_to)
$f = Get-SummonStopFields -Output @('crashed before any stop line')
Check "no stop line reads as neither (the record is kept)" (-not $f.delivered -and $null -eq $f.yield_to)
$f = Get-SummonStopFields -Output @('conductor stopped: reason=human rounds=1 forced_naysayer=0 forced_naysayer_saveable=0 last_msg=m3 yield_to=X')
Check "yield_to is read only on reason=summon_yield" ($null -eq $f.yield_to)

# The wrapper's two record paths, as a skeleton calling the SAME lib functions in the SAME order the
# dispatch loop does (the wiring pins below check the loop still does). The conductor is a stub
# that returns the stop line it is given; $Sha is the clone's origin/main at that moment.
function Invoke-SummonSimLaunch {
    param([hashtable]$State, [string]$Key, [string]$Sha, [string]$StopLine)
    $entry = Get-SummonYieldEntry -EvaluatedState $State -Key $Key
    $passed = @(Get-ResumeNoticeArgs -Entry $entry -MainSha $Sha)
    $sf = Get-SummonStopFields -Output @('...', $StopLine)
    if ($sf.delivered -or $sf.yield_to) {
        $before = Get-SummonYieldEntry -EvaluatedState $State -Key $Key
        $after = Update-SummonYield -Entry $before -Delivered $sf.delivered -YieldTo $sf.yield_to -MainSha $Sha -Now $tNow
        Set-SummonYieldEntry -EvaluatedState $State -Key $Key -Entry $after
    }
    return , $passed
}
function Invoke-SummonSimHeld {
    param([hashtable]$State, [string]$Key, [string]$YieldTo, [string]$Sha)
    $e = Get-SummonYieldEntry -EvaluatedState $State -Key $Key
    Set-SummonYieldEntry -EvaluatedState $State -Key $Key -Entry (
        Update-SummonYield -Entry $e -Delivered $false -YieldTo $YieldTo -MainSha $Sha -Now $tNow)
}
function Get-SimRecord { param([hashtable]$State, [string]$Key) Format-SY (Get-SummonYieldEntry -EvaluatedState $State -Key $Key) }
function Join-Args { param($A) ($A -join ' ') }
$stop = { param([string]$Reason, [string]$Extra = '') ("conductor stopped: reason=$Reason rounds=1 forced_naysayer=0 forced_naysayer_saveable=0 last_msg=m9" + $Extra) }
$k = 'p/T-x'

Write-Host "summon — end to end through a stub conductor (v1.3 / v1.4 / v1.5 / I-SY1)"
# (a) first: the launched run yields.
$st = @{ $k = @{ first_seen_at = 'x' } }
$a0 = Invoke-SummonSimLaunch -State $st -Key $k -Sha 'a' -StopLine (& $stop 'summon_yield' ' yield_to=S1')
Check "(a) first: no notice arguments on a launch without a record" ($a0.Count -eq 0)
Check "(a) first: the record is {a,[S1]}" ((Get-SimRecord $st $k) -eq 'a[S1]')
Check "(a) first: other row fields kept" ($st[$k].first_seen_at -eq 'x')
# then (b) for three ticks while main moves: SHA never moves (I-SY1), S2 appended once (I-SY2).
Invoke-SummonSimHeld -State $st -Key $k -YieldTo 'S2' -Sha 'b1'
Invoke-SummonSimHeld -State $st -Key $k -YieldTo 'S2' -Sha 'b2'
Invoke-SummonSimHeld -State $st -Key $k -YieldTo 'S2' -Sha 'b3'
Check "(a)->(b) x3 with main moving: SHA is still the first one, S2 once" ((Get-SimRecord $st $k) -eq 'a[S1,S2]')
# (a) again (yields to S1 again): no duplicate, SHA kept.
[void](Invoke-SummonSimLaunch -State $st -Key $k -Sha 'b4' -StopLine (& $stop 'summon_yield' ' yield_to=S1'))
Check "(a) twice: no duplicate Summon, SHA kept" ((Get-SimRecord $st $k) -eq 'a[S1,S2]')
# released: the launch passes a..now; a stop with no delivered keeps the record (at least once).
$a1 = Invoke-SummonSimLaunch -State $st -Key $k -Sha 'c' -StopLine (& $stop 'hold')
Check "release: the launch passes the summons and a..<now>" ((Join-Args $a1) -eq '--resume-notice-summons S1,S2 --resume-notice-main-range a..c')
Check "no delivered on the stop line: the record stays" ((Get-SimRecord $st $k) -eq 'a[S1,S2]')
$a2 = Invoke-SummonSimLaunch -State $st -Key $k -Sha 'd' -StopLine (& $stop 'none' ' resume_notice=delivered')
Check "next launch: the same a, up to the current SHA" ((Join-Args $a2) -eq '--resume-notice-summons S1,S2 --resume-notice-main-range a..d')
Check "delivered: the record is removed" ((Get-SimRecord $st $k) -eq 'none')
$a3 = Invoke-SummonSimLaunch -State $st -Key $k -Sha 'd' -StopLine (& $stop 'none')
Check "after delivery: no notice arguments any more" ($a3.Count -eq 0)

# (b) first, then (a): same record shape.
$st = @{}
Invoke-SummonSimHeld -State $st -Key $k -YieldTo 'S2' -Sha 'a'
[void](Invoke-SummonSimLaunch -State $st -Key $k -Sha 'b' -StopLine (& $stop 'summon_yield' ' yield_to=S1'))
Check "(b) then (a): first SHA kept, both Summons" ((Get-SimRecord $st $k) -eq 'a[S2,S1]')

# v1.4: the empty range is passed too, and only delivered removes the record.
$st = @{}
Invoke-SummonSimHeld -State $st -Key $k -YieldTo 'S1' -Sha 'a'
$e1 = Invoke-SummonSimLaunch -State $st -Key $k -Sha 'a' -StopLine (& $stop 'hold')
Check "empty range: still passed (a..a)" ((Join-Args $e1) -eq '--resume-notice-summons S1 --resume-notice-main-range a..a')
Check "empty range, stopped before the first turn (no delivered): kept" ((Get-SimRecord $st $k) -eq 'a[S1]')
$e2 = Invoke-SummonSimLaunch -State $st -Key $k -Sha 'c' -StopLine (& $stop 'empty_thread')
Check "main moved to c meanwhile: the next launch passes a..c" ((Join-Args $e2) -eq '--resume-notice-summons S1 --resume-notice-main-range a..c')
$st = @{}
Invoke-SummonSimHeld -State $st -Key $k -YieldTo 'S1' -Sha 'a'
[void](Invoke-SummonSimLaunch -State $st -Key $k -Sha 'a' -StopLine (& $stop 'none' ' resume_notice=delivered'))
Check "empty range and delivered: removed" ((Get-SimRecord $st $k) -eq 'none')

# v1.5: delivered and yielded again in one run -> exactly one new record, at the post-run SHA.
$st = @{}
Invoke-SummonSimHeld -State $st -Key $k -YieldTo 'S1' -Sha 'a'
[void](Invoke-SummonSimLaunch -State $st -Key $k -Sha 'c' -StopLine (& $stop 'summon_yield' ' yield_to=S2 resume_notice=delivered'))
Check "delivered + yielded: only the new wait remains {c,[S2]}" ((Get-SimRecord $st $k) -eq 'c[S2]')
$j = $st | ConvertTo-Json -Depth 5 | ConvertFrom-Json -AsHashtable
Check "and evaluated.json would hold exactly one summon_yield record" (@($j.Values | Where-Object { $_.ContainsKey('summon_yield') }).Count -eq 1)
$v2 = Invoke-SummonSimLaunch -State $st -Key $k -Sha 'e' -StopLine (& $stop 'none' ' resume_notice=delivered')
Check "the next launch after release passes c..<now>" ((Join-Args $v2) -eq '--resume-notice-summons S2 --resume-notice-main-range c..e')

Write-Host "summon — Get-SummonDisposition"
function New-Ctl { param([string]$Json) [PSCustomObject]@{ desired_state = 'run'; active_summon_json = $Json } }
$sumJson = { param($t, $since = '2026-10-09T01:00:00Z') "{`"thread_id`":`"$t`",`"since`":`"$since`",`"by`":`"h`"}" }
$cA = [PSCustomObject]@{ key = 'A/T-x'; project = 'A'; thread_id = 'T-x'; repo_dir = 'C:\r1' }
$cS = [PSCustomObject]@{ key = 'A/S1'; project = 'A'; thread_id = 'S1'; repo_dir = 'C:\r1' }
$cB = [PSCustomObject]@{ key = 'B/T-y'; project = 'B'; thread_id = 'T-y'; repo_dir = 'C:\r1' }
$pr = @{ 'C:\r1' = @{ predicted_resource = 'github.com/o/r1'; reason = '' } }
$om = @{ 'github.com/o/r1' = 'A' }
$ctl = @{ A = (New-Ctl (& $sumJson 'S1')); B = (New-Ctl $null) }
Check "a same-project bystander is held by the Summon" ((Get-SummonDisposition -Candidate $cA -ControlByProject $ctl -PredictedResourceByRepoDir $pr -OwnerMap $om).yield_to -eq 'S1')
$dS = Get-SummonDisposition -Candidate $cS -ControlByProject $ctl -PredictedResourceByRepoDir $pr -OwnerMap $om
Check "the Summon itself is 'summoned' with its since" ($dS.kind -eq 'summoned' -and $dS.since -eq (ConvertFrom-SummonSince '2026-10-09T01:00:00Z'))
$dB = Get-SummonDisposition -Candidate $cB -ControlByProject $ctl -PredictedResourceByRepoDir $pr -OwnerMap $om
Check "another project writing to the Summon project's repository is held" ($dB.kind -eq 'yield' -and $dB.yield_to -eq 'S1')
Check "and its owner project is handed to the conductor" ((@(Get-SummonOwnerArgs -Disposition $dB) -join ' ') -eq '--summon-owner-project A')
Check "no owner argument when the owner is the candidate's own project" (@(Get-SummonOwnerArgs -Disposition $dS).Count -eq 0)
$dB2 = Get-SummonDisposition -Candidate $cB -ControlByProject $ctl -PredictedResourceByRepoDir $null -OwnerMap $om
Check "resource unresolved: fail-open (not held, no owner)" ($dB2.kind -eq 'none' -and $null -eq $dB2.owner_project)
$dBad = Get-SummonDisposition -Candidate $cA -ControlByProject @{ A = (New-Ctl '{"thread_id":"S1"}') } -PredictedResourceByRepoDir $pr -OwnerMap $om
Check "a malformed active_summon holds the candidate (fail-safe)" ($dBad.kind -eq 'malformed' -and $dBad.malformed_project -eq 'A')
$dNo = Get-SummonDisposition -Candidate $cA -ControlByProject @{ A = $null } -PredictedResourceByRepoDir $pr -OwnerMap $om
Check "a failed control probe is no Summon (fail-open; the conductor enforces)" ($dNo.kind -eq 'none')
$dOld = Get-SummonDisposition -Candidate $cA -ControlByProject @{ A = [PSCustomObject]@{ desired_state = 'run' } } -PredictedResourceByRepoDir $pr -OwnerMap $om
Check "a probe row without the field (older probe) is no Summon" ($dOld.kind -eq 'none')

Write-Host "summon — lane 0"
$lv = New-Verdict -Token 'heisenberg'; $gv = New-Verdict -Token 'pr-review'
Check "a summoned LAUNCH is lane 'summon'" ((Get-SweepLane -Verdict $lv -Summon $dS) -eq 'summon')
Check "a summoned pr-review head is lane 'summon' too, so not --gate-only" ((Get-SweepLane -Verdict $gv -Summon $dS) -eq 'summon' -and @(Get-ConductorLaneArgs -Lane 'summon').Count -eq 0)
Check "a held-by-summon LAUNCH is not a launch (block 1)" ((Get-SweepLane -Verdict $lv -Summon @{ kind = 'yield'; yield_to = 'S1' }) -eq 'none')
Check "a summoned DEFER stays 'none'" ((Get-SweepLane -Verdict (New-Verdict -Decision 'defer') -Summon $dS) -eq 'none')
Check "no Summon read: lanes as before" ((Get-SweepLane -Verdict $gv) -eq 'gate' -and (Get-SweepLane -Verdict $lv -Summon @{ kind = 'none' }) -eq 'role')
$oc = @((New-Cand 'p/T-g'), (New-Cand 'p/T-r'), (New-Cand 'p/S-late'), (New-Cand 'p/T-held'), (New-Cand 'q/S-early'))
$ov = @{ 'p/T-g' = $gv; 'p/T-r' = $lv; 'p/S-late' = $lv; 'p/T-held' = $lv; 'q/S-early' = $gv }
$os = @{
    'p/S-late' = @{ kind = 'summoned'; since = (ConvertFrom-SummonSince '2026-10-09T02:00:00Z') }
    'q/S-early' = @{ kind = 'summoned'; since = (ConvertFrom-SummonSince '2026-10-09T01:00:00Z') }
    'p/T-held' = @{ kind = 'yield'; yield_to = 'S-late' }
}
$ordered = @(Get-OrderedSweepCandidates -Candidates $oc -Verdicts $ov -EvaluatedState @{} -Now $tNow -SummonByKey $os | ForEach-Object { $_.key })
Check "order: held first (block 1), then lane 0 oldest since first, then gate, then role" (
    ($ordered -join ',') -eq 'p/T-held,q/S-early,p/S-late,p/T-g,p/T-r') "got $($ordered -join ',')"
$sch = New-LaunchScheduler -LaunchBudgetSeconds 120 -GateBudgetSeconds 60
$adm = Request-LaunchAdmission -Scheduler $sch -Lane 'summon' -Key 'q/S-early' -TickElapsed 100 -LoopElapsed 90
Check "lane 0 is admitted on the tick budget alone (gate budget already spent)" ($adm.admit -and $adm.reason -eq 'summon')
Check "and does not use up the first-gate exception" (-not $sch.first_gate_evaluated)
Complete-LaunchAttempt -Scheduler $sch -Lane 'summon' -Outcome 'launched'
Check "a lane-0 launch counts as neither a gate nor a role launch" ($sch.summon_launched -eq 1 -and $sch.gate_launched -eq 0 -and $sch.role_launched -eq 0)
$adm2 = Request-LaunchAdmission -Scheduler $sch -Lane 'summon' -Key 'p/S-late' -TickElapsed 130 -LoopElapsed 120
Check "lane 0 still stops at the tick budget" (-not $adm2.admit -and $adm2.scope -eq 'break')
Check "a worked lane-0 run does not break the sweep" ((Get-PostRunAction -Lane 'summon' -Rounds 3) -eq 'continue')
$sch3 = New-LaunchScheduler -LaunchBudgetSeconds 120 -GateBudgetSeconds 60
[void](Request-LaunchAdmission -Scheduler $sch3 -Lane 'summon' -Key 's' -TickElapsed 1 -LoopElapsed 0); Complete-LaunchAttempt -Scheduler $sch3 -Lane 'summon' -Outcome 'launched'
[void](Request-LaunchAdmission -Scheduler $sch3 -Lane 'gate' -Key 'g' -TickElapsed 2 -LoopElapsed 1); Complete-LaunchAttempt -Scheduler $sch3 -Lane 'gate' -Outcome 'launched'
[void](Request-LaunchAdmission -Scheduler $sch3 -Lane 'role' -Key 'r' -TickElapsed 3 -LoopElapsed 2)
Check "the gate-before-role invariant still holds with lane 0 in front" ($null -eq (Test-GateAdmissionInvariant -Scheduler $sch3 -GateCandidateCount 1))

Write-Host "summon — wiring (the wrapper's loop calls the lib in the order the simulation above does)"
$loopCalls = @($loop.Body.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true))
$updateCalls = @($loopCalls | Where-Object { $_.GetCommandName() -eq 'Update-SummonYield' })
Check "the loop calls Update-SummonYield exactly twice (paths (a) and (b)), and nothing else writes the record" (
    $updateCalls.Count -eq 2 -and @($loopCalls | Where-Object { $_.GetCommandName() -eq 'Set-SummonYieldEntry' }).Count -eq 2)
Check "path (b) passes -Delivered `$false" (@($updateCalls | Where-Object { $_.Extent.Text -match '-Delivered\s+\$false' }).Count -eq 1)
$stopFieldCalls = @($loopCalls | Where-Object { $_.GetCommandName() -eq 'Get-SummonStopFields' })
Check "path (a) reads the stop line with Get-SummonStopFields, after the spawn" (
    $stopFieldCalls.Count -eq 1 -and $stopFieldCalls[0].Extent.StartOffset -gt $spawn[0].Extent.StartOffset)
$resumeCalls = @($loopCalls | Where-Object { $_.GetCommandName() -eq 'Get-ResumeNoticeArgs' })
Check "the resume notice arguments are built before the spawn" ($resumeCalls.Count -eq 1 -and $resumeCalls[0].Extent.StartOffset -lt $spawn[0].Extent.StartOffset)
Check "and the spawn splats `$summonArgs" (@($spawn[0].CommandElements | Where-Object { $_.Extent.Text -eq '@summonArgs' }).Count -eq 1)
$summonArgsAssign = @($loop.Body.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $n.Left.Extent.Text -eq '$summonArgs' -and $n.Operator -eq 'Equals' }, $true))
Check "`$summonArgs is re-assigned on every candidate" ($summonArgsAssign.Count -eq 1)
Check "the ordered list is built with the Summon dispositions" ($orderAssign.Count -eq 1 -and $orderAssign[0].Right.Extent.Text -match '-SummonByKey\s+\$summonByKey')
Check "the wrapper never spells the resume flags itself (the lib decides)" (-not $wrapperText.Contains('--resume-notice-') -and -not $wrapperText.Contains('--summon-owner-project'))

Write-Host ""
if ($script:failures -gt 0) { Write-Host "$($script:failures) FAILURE(S)"; exit 1 }
Write-Host "all launch-fairness checks passed"
exit 0
