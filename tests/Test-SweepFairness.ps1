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

$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }
$functions = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'Update-EvaluatedTimestamp', 'ConvertTo-UtcInstant', 'Format-DurationDigest',
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
$orderAssign = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $n.Left.Extent.Text -eq '$candidates' -and $n.Right.Extent.Text -like 'Get-OrderedSweepCandidates*' }, $true))
Check "the loop's `$candidates is the ordered list" ($orderAssign.Count -eq 1 -and $orderAssign[0].Extent.StartOffset -lt $loop.Extent.StartOffset)
$runTopCalls = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) | ForEach-Object { $_.GetCommandName() })
foreach ($fnName in 'Resolve-SweepBudgets', 'Update-LaunchWaitFromVerdicts', 'Get-LaunchWaitStarved', 'Test-GateAdmissionInvariant') {
    Check "wrapper calls $fnName" ($runTopCalls -contains $fnName)
}

Write-Host ""
if ($script:failures -gt 0) { Write-Host "$($script:failures) FAILURE(S)"; exit 1 }
Write-Host "all launch-fairness checks passed"
exit 0
