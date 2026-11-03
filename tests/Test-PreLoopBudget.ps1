# Pre-loop overhead: per-phase timing (D-3) and the over-budget alert state machine (D-4).
# T-sweep-preloop-overhead-exceeds-launch-budget, Bohr msg-7851 (v1.5) / msg-7853.
#
# What this file pins:
#   * the five v1.5 cases: over runs, near runs, near/over alternating, over -> near, back to ok
#   * at most one alert per tick, and the dedup signature `<kind>@<streak_started_at>` works with
#     the wrapper's Send-NotificationIfChanged predicate (Test-NotificationSuppressed)
#   * level boundaries (0.8 / 1.0), state normalisation of a broken file, sample retention, the
#     digest line, the phase line
#   * wiring: the wrapper dot-sources the lib, times the phases, steps the machine and sends
#     through Send-NotificationIfChanged with key sweep:preloop-budget
#
# Repo convention (the other tests/Test-*.ps1 the gate runs): plain pwsh with a Check helper, not
# the Pester module, so the gate needs nothing beyond pwsh.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'deploy/lib/PreLoopBudget.ps1')

$script:failures = 0
function Check {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} {1}" -f $Name, $Detail) }
}

# The wrapper's dedup predicate, lifted from the script so the simulation uses the real one.
$sweepScript = Join-Path $repoRoot 'deploy/run-conductor-scheduled.ps1'
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }
$fnAsts = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
$fn = $fnAsts | Where-Object { $_.Name -eq 'Test-NotificationSuppressed' } | Select-Object -First 1
if (-not $fn) { throw "Test-NotificationSuppressed not found in the sweep script" }
Invoke-Expression $fn.Extent.Text

$Budget = 120.0
$Seconds = @{ ok = 60.0; near = 100.0; over = 130.0 }

# Runs a level sequence through the machine and the dedup predicate, like the wrapper does.
# Returns one entry per tick: the alert kind that was actually SENT, or ''.
function Invoke-Sim {
    param([string[]]$Levels, [hashtable]$State = $null, [hashtable]$Notify = $null, [long]$T0 = 1700000000)
    if ($null -eq $State) { $State = ConvertTo-PreLoopBudgetState -Raw @{} }
    if ($null -eq $Notify) { $Notify = @{} }
    $sent = @()
    $t = $T0
    foreach ($lv in $Levels) {
        $t += 60
        $level = Get-PreLoopBudgetLevel -PreLoopSeconds $Seconds[$lv] -BudgetSeconds $Budget
        if ($level -ne $lv) { throw "fixture: $($Seconds[$lv]) is not $lv" }
        Add-PreLoopBudgetSample -State $State -NowEpoch $t -PreLoopSeconds $Seconds[$lv] -Level $level
        $a = Step-PreLoopBudgetState -State $State -Level $level -NowEpoch $t
        if ($null -ne $a -and -not (Test-NotificationSuppressed -State $Notify -Key $script:PreLoopNotifyKey -Signature $a.signature)) {
            $Notify[$script:PreLoopNotifyKey] = $a.signature
            $sent += $a.kind
        }
        else { $sent += '' }
    }
    return [PSCustomObject]@{ sent = $sent; state = $State; notify = $Notify }
}
function Get-Sent { param($Sim) @($Sim.sent | Where-Object { $_ }) }

Write-Host "levels"
Check 'ratio < 0.8 is ok'         ((Get-PreLoopBudgetLevel -PreLoopSeconds 95.9 -BudgetSeconds 120) -eq 'ok')
Check 'ratio = 0.8 is near'       ((Get-PreLoopBudgetLevel -PreLoopSeconds 96 -BudgetSeconds 120) -eq 'near')
Check 'ratio just < 1.0 is near'  ((Get-PreLoopBudgetLevel -PreLoopSeconds 119.9 -BudgetSeconds 120) -eq 'near')
Check 'ratio = 1.0 is over'       ((Get-PreLoopBudgetLevel -PreLoopSeconds 120 -BudgetSeconds 120) -eq 'over')
$threw = $false; try { Get-PreLoopBudgetLevel -PreLoopSeconds 1 -BudgetSeconds 0 | Out-Null } catch { $threw = $true }
Check 'non-positive budget throws' $threw

Write-Host "case 1: over keeps going (Einstein msg-7850: no late near warning after the over alert)"
$sim = Invoke-Sim -Levels (@('over') * 12)
Check 'over alert on tick 3' ($sim.sent[2] -eq 'over') "sent=$($sim.sent -join ',')"
Check 'nothing before tick 3' (($sim.sent[0] -eq '') -and ($sim.sent[1] -eq ''))
Check 'exactly one alert in 12 over ticks' ((Get-Sent $sim).Count -eq 1) "sent=$($sim.sent -join ',')"
Check 'no near warning on tick 6 or later' (-not ((Get-Sent $sim) -contains 'near'))

Write-Host "case 2: near six times"
$sim = Invoke-Sim -Levels (@('near') * 10)
Check 'near warning on tick 6' ($sim.sent[5] -eq 'near') "sent=$($sim.sent -join ',')"
Check 'exactly one near warning' ((Get-Sent $sim).Count -eq 1)

Write-Host "case 3: near and over alternate (the case fix_min would have silenced)"
$sim = Invoke-Sim -Levels @('near', 'over', 'near', 'over', 'near', 'over', 'over', 'over', 'over')
Check 'near warning on tick 6' ($sim.sent[5] -eq 'near') "sent=$($sim.sent -join ',')"
Check 'over alert after three over in a row (tick 8)' ($sim.sent[7] -eq 'over') "sent=$($sim.sent -join ',')"
Check 'two alerts in total' ((Get-Sent $sim).Count -eq 2)

Write-Host "case 4: over -> near"
$sim = Invoke-Sim -Levels (@('over', 'over', 'over') + @('near') * 8)
Check 'over alert on tick 3' ($sim.sent[2] -eq 'over')
Check 'recovered alert on tick 4' ($sim.sent[3] -eq 'recovered') "sent=$($sim.sent -join ',')"
Check 'no near warning while near continues' ((Get-Sent $sim).Count -eq 2) "sent=$($sim.sent -join ',')"
Check 'notified_level is near' ($sim.state.notified_level -eq 'near')
$a = Step-PreLoopBudgetState -State (ConvertTo-PreLoopBudgetState -Raw @{ elevated_streak = 3; over_streak = 3; notified_level = 'over'; streak_started_at = 5 }) -Level 'near' -NowEpoch 10
$msg = Format-PreLoopBudgetMessage -Alert $a -PreLoopSeconds 100 -BudgetSeconds 120
Check 'recovered-to-near message says the margin is small' ($msg -match '余裕は小さい')

Write-Host "case 5: back to ok resets, and the next run alerts again"
$sim = Invoke-Sim -Levels (@('over') * 3 + @('ok'))
Check 'over then recovered' ((($sim.sent | Where-Object { $_ }) -join ',') -eq 'over,recovered') "sent=$($sim.sent -join ',')"
Check 'state back to initial' (($sim.state.elevated_streak -eq 0) -and ($sim.state.over_streak -eq 0) -and
                               ($sim.state.notified_level -eq 'none') -and ($null -eq $sim.state.streak_started_at))
$sim2 = Invoke-Sim -Levels (@('over') * 3) -State $sim.state -Notify $sim.notify -T0 1700100000
Check 'the next run alerts again' ($sim2.sent[2] -eq 'over') "sent=$($sim2.sent -join ',')"
$sim = Invoke-Sim -Levels (@('near') * 7 + @('ok'))
Check 'near -> ok sends nothing' ((Get-Sent $sim).Count -eq 1) "sent=$($sim.sent -join ',')"
Check 'near -> ok resets notified_level' ($sim.state.notified_level -eq 'none')

Write-Host "over / recovered oscillation keeps telling the operator"
$sim = Invoke-Sim -Levels (@('over') * 3 + @('near') + @('over') * 3 + @('near'))
Check 'over, recovered, over, recovered' (((Get-Sent $sim) -join ',') -eq 'over,recovered,over,recovered') "sent=$($sim.sent -join ',')"

Write-Host "dedup signature"
$st = ConvertTo-PreLoopBudgetState -Raw @{}
$null = Step-PreLoopBudgetState -State $st -Level 'over' -NowEpoch 100
$null = Step-PreLoopBudgetState -State $st -Level 'over' -NowEpoch 160
$a = Step-PreLoopBudgetState -State $st -Level 'over' -NowEpoch 220
Check 'signature is <kind>@<streak_started_at>' ($a.signature -eq 'over@100') "got $($a.signature)"
# A state file reloaded mid-run gives the same signature, so a repeat cannot re-send.
$reloaded = ConvertTo-PreLoopBudgetState -Raw ((ConvertTo-PreLoopBudgetStateFile -State $st) | ConvertTo-Json -Depth 5 | ConvertFrom-Json -AsHashtable)
Check 'round trip keeps streak_started_at' ($reloaded.streak_started_at -eq 100)
Check 'round trip keeps notified_level' ($reloaded.notified_level -eq 'over')

Write-Host "broken state file"
$st = ConvertTo-PreLoopBudgetState -Raw @{ elevated_streak = 'x'; over_streak = -4; notified_level = 'loud'; samples = 'nope' }
Check 'broken fields fall back to initial' (($st.elevated_streak -eq 0) -and ($st.over_streak -eq 0) -and ($st.notified_level -eq 'none'))
Check 'broken samples dropped' (@($st.samples).Count -eq 0)
$st = ConvertTo-PreLoopBudgetState -Raw $null
Check 'null raw is initial' ($st.notified_level -eq 'none')

Write-Host "samples and digest line"
$st = ConvertTo-PreLoopBudgetState -Raw @{}
$now = 1700000000
Add-PreLoopBudgetSample -State $st -NowEpoch ($now - 50 * 3600) -PreLoopSeconds 999 -Level 'over'
Add-PreLoopBudgetSample -State $st -NowEpoch ($now - 30 * 3600) -PreLoopSeconds 888 -Level 'over'
Add-PreLoopBudgetSample -State $st -NowEpoch ($now - 3) -PreLoopSeconds 100 -Level 'near'
Add-PreLoopBudgetSample -State $st -NowEpoch ($now - 2) -PreLoopSeconds 130 -Level 'over'
Add-PreLoopBudgetSample -State $st -NowEpoch ($now - 1) -PreLoopSeconds 60 -Level 'ok'
Add-PreLoopBudgetSample -State $st -NowEpoch $now -PreLoopSeconds 110 -Level 'near'
Check 'samples older than 48h are dropped' (@($st.samples | Where-Object { $_.s -eq 999 }).Count -eq 0)
Check 'samples within 48h are kept' (@($st.samples).Count -eq 5)
$lines = @(Get-PreLoopBudgetDigestLines -State $st -NowEpoch $now)
Check 'one digest line' ($lines.Count -eq 1)
Check 'digest counts only the last 24h' ($lines[0] -match ' 4 tick') $lines[0]
Check 'digest over count' ($lines[0] -match '起動なし）1 tick') $lines[0]
Check 'digest median (even count)' ($lines[0] -match '中央値 105\.0s') $lines[0]
Check 'digest max' ($lines[0] -match '最大 130\.0s') $lines[0]
Check 'no samples -> no digest line' (@(Get-PreLoopBudgetDigestLines -State (ConvertTo-PreLoopBudgetState -Raw @{}) -NowEpoch $now).Count -eq 0)

Write-Host "phase line"
$ph = New-PreLoopPhaseLog
Add-PreLoopPhase -Phases $ph -Name 'intake' -Seconds 1.24
Add-PreLoopPhase -Phases $ph -Name 'head[mindwire]' -Seconds 2
Add-PreLoopPhase -Phases $ph -Name 'intake' -Seconds 1
Check 'phases in run order, summed, one decimal' ((Format-PreLoopPhasesLine -Phases $ph) -eq 'pre-loop phases: intake=2.2s head[mindwire]=2.0s') (Format-PreLoopPhasesLine -Phases $ph)
Check 'epoch conversion' ((Get-PreLoopEpochSeconds -Utc ([datetime]::new(1970, 1, 2, 0, 0, 0, [DateTimeKind]::Utc))) -eq 86400)

Write-Host "messages"
$over = @{ kind = 'over'; level = 'over'; over_streak = 3; elevated_streak = 3 }
$m = Format-PreLoopBudgetMessage -Alert $over -PreLoopSeconds 130 -BudgetSeconds 120 -PhasesLine 'pre-loop phases: decide=12.0s'
Check 'over message carries the phase line' ($m -match 'decide=12\.0s')
Check 'over message says nothing launched' ($m -match '起動していません')

Write-Host "wiring"
$src = Get-Content -Raw -LiteralPath $sweepScript
Check 'wrapper dot-sources the lib' ($src -match "lib/PreLoopBudget\.ps1")
Check 'wrapper logs the phase line' ($src -match 'Format-PreLoopPhasesLine -Phases \$preLoopPhases')
foreach ($p in @("'sync'", "'intake'", "'gatebootstrap'", '"control\[\$proj\]"', '"head\[\$proj\]"', '"1b\[\$proj\]"',
                 '"parkwake\[\$proj\]"', "'autogate'", "'decide'")) {
    Check "wrapper times phase $p" ($src -match ("Add-PreLoopPhase -Phases \`$preLoopPhases -Name " + $p))
}
Check 'wrapper steps the machine' ($src -match 'Step-PreLoopBudgetState -State \$preLoopBudgetState')
Check 'wrapper sends through Send-NotificationIfChanged with the lib key' ($src -match 'Send-NotificationIfChanged -State \$notifyState -Key \$script:PreLoopNotifyKey')
Check 'lib key is sweep:preloop-budget' ($script:PreLoopNotifyKey -eq 'sweep:preloop-budget')
Check 'wrapper saves the machine state' ($src -match 'Save-JsonState -Path \$preLoopBudgetStatePath')
Check 'wrapper passes the digest line' ($src -match 'Get-PreLoopBudgetDigestLines -State \$preLoopBudgetState')
$stepAt = $src.IndexOf('Step-PreLoopBudgetState -State')
$loopAt = $src.IndexOf('$preLoopSeconds = Get-SweepElapsedSeconds -Since $tickStartUtc -Now $loopStartUtc')
Check 'machine runs after pre-loop is measured' ($loopAt -gt 0 -and $stepAt -gt $loopAt)

if ($script:failures -gt 0) { Write-Host "$($script:failures) failure(s)"; exit 1 }
Write-Host "all passed"
