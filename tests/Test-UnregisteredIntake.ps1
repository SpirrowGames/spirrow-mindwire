# Regression guard for the sweep's auto-registration of unregistered live threads
# (T-sweep-intake-and-quarantine-stalls, Bohr msg-5889 D-2, amended by msg-5893 D-2.1-D-2.4, cleared
# by Einstein msg-5894). Code under test: deploy/lib/UnregisteredIntake.ps1 and its call sites in
# deploy/run-conductor-scheduled.ps1.
#
# What is pinned, and how:
#   R  Resolve-UnregisteredIntake — the environment probe (one distinct repo_dir, fully qualified,
#      existing), the "?" for an unmeasured project or a failed probe, and the PR-review refusal.
#   D-2.1  A refused thread never joins the candidate array, so none of the three state files the
#      sweep writes (quarantine.json, retry-pending.json, evaluated.json) can get its key. The state
#      writers are inline in the wrapper and there is no whole-tick harness, so this is pinned in two
#      parts: (a) functionally, the refused key is absent from the joined candidates and from the live
#      keys every state writer is driven by; (b) structurally, the refused list is consumed only by the
#      parked-humans poll, the digest and the log — never assigned into $candidates.
#   D-2.2  Auto-registered rows have exactly the Get-SweepCandidates shape and no marker, so nothing
#      downstream can treat them differently (no "ephemeral" quarantine bypass).
#   D-2.3  The append comes BEFORE every filter. Pinned on the wrapper's AST: the
#      Join-UnregisteredCandidates assignment precedes the first use of $quarantineState,
#      $retryState, $evaluatedState, Invoke-HeadSkipDecide and $liveKeys in the run section. And
#      functionally: a quarantined auto-registered key is in the joined candidates, which is where the
#      decide batch and the dispatch loop look it up in $quarantineState and skip it.
#   Digest  The 未登録（登録不可）section: "?" rows first, true totals in the header, the floor of the
#      sections after it intact under the shipped budget, and no change when no intake is passed.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$intakeLib = Join-Path $repoRoot "deploy/lib/UnregisteredIntake.ps1"
foreach ($f in $sweepScript, $intakeLib) { if (-not (Test-Path -LiteralPath $f)) { throw "not found: $f" } }

. $intakeLib

$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual) }
}
function CheckTrue {
    param([string]$Name, [bool]$Actual, $Debug = $null)
    if ($Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — got '{1}'  debug={2}" -f $Name, $Actual, $Debug) }
}

function Cand { param($p, $t, $d) [pscustomobject]@{ project = $p; thread_id = $t; repo_dir = $d; key = "$p/$t" } }
function Proj {
    param($p, $ids, $count = 'auto', $err = $null)
    $c = if ($count -eq 'auto') { @($ids).Count } else { $count }
    [pscustomobject]@{ project = $p; unregistered_count = $c; unregistered = @($ids); error = $err; malformed_count = 0 }
}
$exists = { param($p) $true }
$missing = { param($p) $false }

$dirA = if ($IsWindows) { 'C:\clones\a' } else { '/clones/a' }
$dirB = if ($IsWindows) { 'C:\clones\b' } else { '/clones/b' }
$cands = @(
    (Cand 'mw' 'T-listed' $dirA),
    (Cand 'mk' 'T-k1' $dirA),
    (Cand 'mk' 'T-k2' $dirB)
)

Write-Host "R — Resolve-UnregisteredIntake: environment probe"
$rep = [pscustomobject]@{ projects = @((Proj 'mw' @('T-new1', 'T-new2'))) }
$i = Resolve-UnregisteredIntake -Candidates $cands -Report $rep -PathExists $exists
Check "one repo_dir + exists -> 2 added" 2 @($i.added).Count
Check "added row carries the project's repo_dir" $dirA $i.added[0].repo_dir
Check "added row key is project/thread_id" 'mw/T-new1' $i.added[0].key
Check "nothing refused" 0 @($i.refused).Count
Check "no probe error" $null $i.probe_error

$i = Resolve-UnregisteredIntake -Candidates $cands -Report ([pscustomobject]@{ projects = @((Proj 'mk' @('T-x'))) }) -PathExists $exists
Check "two distinct repo_dirs -> nothing added" 0 @($i.added).Count
Check "two distinct repo_dirs -> refused with 'ambiguous: 2 values'" 'repo_dir ambiguous: 2 values' $i.refused[0].reason

$i = Resolve-UnregisteredIntake -Candidates $cands -Report $rep -PathExists $missing
Check "repo_dir does not exist -> nothing added" 0 @($i.added).Count
Check "repo_dir does not exist -> refused 'missing'" "repo_dir missing: $dirA" $i.refused[0].reason
Check "every thread of the project is refused" 2 @($i.refused).Count

$i = Resolve-UnregisteredIntake -Candidates @((Cand 'rel' 'T-r' 'rel/path')) `
    -Report ([pscustomobject]@{ projects = @((Proj 'rel' @('T-y'))) }) -PathExists $exists
Check "relative repo_dir -> refused 'not absolute' (checked before existence)" 'repo_dir not absolute: rel/path' $i.refused[0].reason

$i = Resolve-UnregisteredIntake -Candidates $cands -Report ([pscustomobject]@{ projects = @((Proj 'mw' @('T-pr-review-spirrow-mindwire-9'))) }) -PathExists $exists
Check "PR-review thread is never added (paid gate)" 0 @($i.added).Count
CheckTrue "PR-review thread is refused, with a reason" ($i.refused[0].reason -like 'pr-review thread*') $i.refused[0].reason

$i = Resolve-UnregisteredIntake -Candidates $cands -Report ([pscustomobject]@{ projects = @((Proj 'mw' @('T-listed', 'T-z'))) }) -PathExists $exists
Check "already-listed thread is not added twice" 1 @($i.added).Count
Check "the other one is" 'mw/T-z' $i.added[0].key

Write-Host "R — unmeasured project / failed probe render '?', never 0"
$i = Resolve-UnregisteredIntake -Candidates $cands -Report ([pscustomobject]@{ projects = @(
        (Proj 'mw' @() -count $null -err 'chatroom_list_threads failed: Timeout'),
        (Proj 'mk' @()) ) }) -PathExists $exists
Check "unregistered_count=null -> nothing added" 0 @($i.added).Count
Check "unregistered_count=null -> one unmeasured row" 1 @($i.unmeasured).Count
Check "unmeasured carries the error" 'chatroom_list_threads failed: Timeout' $i.unmeasured[0].reason
CheckTrue "header shows '?' when a project was not measured" ((Get-UnregisteredIntakeHeader -Intake $i) -like '*0 件 + ?*') (Get-UnregisteredIntakeHeader -Intake $i)

$i = Resolve-UnregisteredIntake -Candidates $cands -Report $null -ProbeError 'exit=1: boom' -PathExists $exists
Check "probe error -> nothing added" 0 @($i.added).Count
Check "probe error is named" 'unregistered_threads probe failed: exit=1: boom' $i.probe_error
CheckTrue "probe error -> header '?'" ((Get-UnregisteredIntakeHeader -Intake $i) -like '*+ ?*') (Get-UnregisteredIntakeHeader -Intake $i)
$jf = Join-UnregisteredCandidates -Candidates $cands -Intake $i
Check "Join with a failed probe returns exactly the listed candidates" 3 $jf.Count

$i = Resolve-UnregisteredIntake -Candidates $cands -Report ([pscustomobject]@{ projects = @((Proj 'mw' @())) }) -PathExists $exists
Check "measured 0 -> header '0 件' without '?'" '未登録（登録不可）: 0 件（この tick の自動登録 0 件）' (Get-UnregisteredIntakeHeader -Intake $i)

Write-Host "D-2.2 — added rows are ordinary candidates (same shape, no marker)"
$i = Resolve-UnregisteredIntake -Candidates $cands -Report $rep -PathExists $exists
$props = @($i.added[0].PSObject.Properties.Name | Sort-Object) -join ','
$listedProps = @($cands[0].PSObject.Properties.Name | Sort-Object) -join ','
Check "added row has exactly the listed-candidate properties" $listedProps $props
# The shape Get-SweepCandidates emits, read from the wrapper itself rather than restated.
$gsc = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-SweepCandidates' }, $true) | Select-Object -First 1
$hashLit = $gsc.Body.FindAll({ param($n) $n -is [System.Management.Automation.Language.HashtableAst] }, $true) | Select-Object -Last 1
$gscProps = @($hashLit.KeyValuePairs | ForEach-Object { $_.Item1.Extent.Text } | Sort-Object) -join ','
Check "…and those are the properties Get-SweepCandidates emits" $gscProps $props
$joined = Join-UnregisteredCandidates -Candidates $cands -Intake $i
Check "Join: listed candidates first (sweep.json order kept)" 'mw/T-listed' $joined[0].key
Check "Join: auto-registered appended after them" 'mw/T-new2' $joined[-1].key
Check "Join: total" 5 @($joined).Count

Write-Host "D-2.1 — a refused thread cannot reach any state file"
$mixed = Resolve-UnregisteredIntake -Candidates $cands -Report ([pscustomobject]@{ projects = @(
        (Proj 'mw' @('T-ok')), (Proj 'mk' @('T-refused')) ) }) -PathExists $exists
Check "one added, one refused" '1/1' "$(@($mixed.added).Count)/$(@($mixed.refused).Count)"
$joined = Join-UnregisteredCandidates -Candidates $cands -Intake $mixed
$liveKeys = @($joined | ForEach-Object { $_.key })
CheckTrue "refused key is not a candidate" ($liveKeys -notcontains 'mk/T-refused') ($liveKeys -join ',')
CheckTrue "added key is a candidate" ($liveKeys -contains 'mw/T-ok') ($liveKeys -join ',')

# Structural half: in the run section (outside function bodies), the refused list is read only by
# the parked-humans poll and the log; nothing assigns it into $candidates.
$funcDefs = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true))
function Test-InFunction { param($Node) $p = $Node.Parent; while ($p) { if ($p -is [System.Management.Automation.Language.FunctionDefinitionAst]) { return $true }; $p = $p.Parent }; return $false }
$refusedUses = @($ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.MemberExpressionAst] -and
    $n.Member.Extent.Text -eq 'refused' -and $n.Expression.Extent.Text -eq '$unregisteredIntake' }, $true) |
    Where-Object { -not (Test-InFunction $_) })
CheckTrue "the run section reads `$unregisteredIntake.refused somewhere" ($refusedUses.Count -gt 0)
$badUse = @($refusedUses | Where-Object {
    $stmt = $_; while ($stmt -and -not ($stmt -is [System.Management.Automation.Language.AssignmentStatementAst])) { $stmt = $stmt.Parent }
    $stmt -and $stmt.Left.Extent.Text -eq '$candidates' })
Check "no assignment to `$candidates reads the refused list" 0 $badUse.Count
$candAssign = @($ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$candidates' }, $true) |
    Where-Object { -not (Test-InFunction $_) } | ForEach-Object { ($_.Right.Extent.Text -split '\s')[0] })
Check "`$candidates is assigned only by Get-SweepCandidates / Join-UnregisteredCandidates / ordering" `
    'Get-SweepCandidates,Join-UnregisteredCandidates,Get-OrderedSweepCandidates' (($candAssign | Where-Object { $_ -ne '$sweepOrderCandidates' }) -join ',')

Write-Host "D-2.3 — the append precedes every filter"
$joinAssign = @($ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$candidates' -and
    $n.Right.Extent.Text -like 'Join-UnregisteredCandidates*' }, $true))
Check "exactly one Join-UnregisteredCandidates assignment" 1 $joinAssign.Count
$joinAt = $joinAssign[0].Extent.StartOffset
$getAt = @($ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Get-SweepCandidates' }, $true) |
    Where-Object { -not (Test-InFunction $_) })[0].Extent.StartOffset
CheckTrue "append comes after Get-SweepCandidates" ($getAt -lt $joinAt)
foreach ($v in 'quarantineState', 'retryState', 'evaluatedState', 'liveKeys', 'headsByProject') {
    $first = @($ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.VariableExpressionAst] -and $n.VariablePath.UserPath -eq $v }, $true) |
        Where-Object { -not (Test-InFunction $_) } | ForEach-Object { $_.Extent.StartOffset } | Sort-Object)[0]
    CheckTrue "append precedes the first run-section use of `$$v" ($null -ne $first -and $joinAt -lt $first) "join=$joinAt first=$first"
}
foreach ($cmd in 'Invoke-HeadSkipDecide', 'Invoke-GateBootstrapTick', 'Invoke-HeadProbe', 'Get-OrderedSweepCandidates') {
    $first = @($ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq $cmd }, $true) |
        Where-Object { -not (Test-InFunction $_) } | ForEach-Object { $_.Extent.StartOffset } | Sort-Object)[0]
    CheckTrue "append precedes the first run-section call of $cmd" ($null -ne $first -and $joinAt -lt $first) "join=$joinAt first=$first"
}
# Functional half: a quarantined auto-registered thread is in the joined candidate array, so the
# decide batch and the dispatch loop — which both test $quarantineState.ContainsKey($c.key) for
# every candidate — see it and skip it.
$quarantineState = @{ 'mw/T-ok' = @{ state = 'quarantined'; first_failure_at = '2026-10-01T00:00:00Z' } }
$skipped = @($joined | Where-Object { $quarantineState.ContainsKey($_.key) } | ForEach-Object { $_.key })
Check "quarantined auto-registered key is seen by the quarantine check" 'mw/T-ok' ($skipped -join ',')
$launchable = @($joined | Where-Object { -not $quarantineState.ContainsKey($_.key) } | ForEach-Object { $_.key })
CheckTrue "…and is not among the launchable keys" ($launchable -notcontains 'mw/T-ok') ($launchable -join ',')
# The two inline quarantine checks really are of that form (so the functional half tests the real predicate).
$qChecks = @($ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and
    $n.Expression.Extent.Text -eq '$quarantineState' -and $n.Member.Extent.Text -eq 'ContainsKey' -and
    ($n.Arguments[0].Extent.Text -in '$c.key', '$cand.key') }, $true))
CheckTrue "decide batch and dispatch loop both check `$quarantineState.ContainsKey(<candidate>.key)" ($qChecks.Count -ge 2) $qChecks.Count

Write-Host "Parked-humans poll — refused threads are polled with head_msg_id ''"
$pp = $funcDefs | Where-Object { $_.Name -eq 'Invoke-ParkedHumansProbe' } | Select-Object -First 1
CheckTrue "Invoke-ParkedHumansProbe takes -ExtraThreads" ($pp.Body.ParamBlock.Parameters.Name.VariablePath.UserPath -contains 'ExtraThreads')
CheckTrue "extra threads are sent with an empty head" ($pp.Extent.Text -match "foreach \(\`$x in \`$projectExtra\) \{ \`$items \+= @\{ thread_id = `"\`$\(\`$x.thread_id\)`"; head_msg_id = '' \} \}")

Write-Host "Digest — 未登録（登録不可）section"
$budgetAssign = $ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$DigestBudget' }, $true)
$script:DigestBudget = [int]$budgetAssign[0].Right.Extent.Text
$script:QuarantineEscalatedAfter = [TimeSpan]::FromHours(24)
$script:QuarantineStaleAfter     = [TimeSpan]::FromDays(7)
$script:StarvedThreshold         = [TimeSpan]::FromHours(24)
function Write-Log { param([string]$Message) }
. (Join-Path $repoRoot 'deploy/lib/Lease.ps1')
foreach ($name in 'Get-DerivedQuarantineState', 'Format-DurationDigest', 'Get-ParkedRowTag', 'Get-StarvedKeys',
                  'New-DailyDigest', 'ConvertTo-UtcInstant', 'Get-FingerprintHint', 'Get-QuarantineReproHint') {
    $fn = $funcDefs | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}
$now = [datetime]'2026-10-03T00:00:00Z'
$base = @{ QuarantineState = @{}; EvaluatedState = @{}; HeadsByProject = @{}; ControlByProject = @{}; Now = $now; LiveKeys = @() }
$without = New-DailyDigest @base
$withNull = New-DailyDigest @base -UnregisteredIntake $null
Check "no intake passed -> digest unchanged" $without $withNull
CheckTrue "no intake passed -> no 未登録 section" (-not ($without -match '未登録（登録不可）'))

$dIntake = @{ added = @((Cand 'mw' 'T-a' $dirA)); probe_error = $null
    unmeasured = @([pscustomobject]@{ project = 'vw'; reason = 'Timeout' })
    refused = @([pscustomobject]@{ project = 'mk'; thread_id = 'T-r'; key = 'mk/T-r'; reason = 'repo_dir ambiguous: 3 values' }) }
$d = New-DailyDigest @base -UnregisteredIntake $dIntake
CheckTrue "section header with count, '?', and auto-registered count" ($d.Contains('未登録（登録不可）: 1 件 + ?（この tick の自動登録 1 件）')) $d
CheckTrue "refused row names key and reason" ($d -match '  mk/T-r — repo_dir ambiguous: 3 values') $d
CheckTrue "unmeasured row shows '?'" ($d -match '  vw — \? 測れなかった: Timeout') $d
$lines = $d -split "`n"
$uIdx = [array]::IndexOf($lines, ($lines | Where-Object { $_ -like '  vw — ?*' } | Select-Object -First 1))
$rIdx = [array]::IndexOf($lines, ($lines | Where-Object { $_ -like '  mk/T-r*' } | Select-Object -First 1))
CheckTrue "'?' rows come before refused rows" ($uIdx -ge 0 -and $uIdx -lt $rIdx) "$uIdx/$rIdx"
$hdrStale = [array]::IndexOf($lines, ($lines | Where-Object { $_ -like '停止中*' } | Select-Object -First 1))
$hdrUnreg = [array]::IndexOf($lines, ($lines | Where-Object { $_ -like '未登録*' } | Select-Object -First 1))
$hdrStarved = [array]::IndexOf($lines, ($lines | Where-Object { $_ -like '飢餓*' } | Select-Object -First 1))
CheckTrue "section sits between 停止中 and 飢餓" ($hdrStale -lt $hdrUnreg -and $hdrUnreg -lt $hdrStarved) "$hdrStale/$hdrUnreg/$hdrStarved"

$emptyIntake = @{ added = @(); refused = @(); unmeasured = @(); probe_error = $null }
$d0 = New-DailyDigest @base -UnregisteredIntake $emptyIntake
CheckTrue "measured and nothing refused -> header at 0 件 plus (該当なし)" ($d0 -match "未登録（登録不可）: 0 件（この tick の自動登録 0 件）`n  \(該当なし\)") $d0

# Budget: 80 refused rows + 10 starved rows under the shipped budget.
$many = @{ added = @(); unmeasured = @(); probe_error = $null; refused = @() }
for ($k = 0; $k -lt 80; $k++) {
    $many.refused += [pscustomobject]@{ project = 'mk'; thread_id = "T-refused-$k"; key = "mk/T-refused-$k"; reason = 'repo_dir ambiguous: 3 values' }
}
$ev = @{}
for ($k = 0; $k -lt 10; $k++) { $ev["p/T-starved-$k"] = @{ last_evaluated_at = $now.AddDays(-3).ToString('o'); first_seen_at = $now.AddDays(-9).ToString('o') } }
$dm = New-DailyDigest -QuarantineState @{} -EvaluatedState $ev -HeadsByProject @{} -ControlByProject @{} -Now $now `
    -LiveKeys @($ev.Keys) -Budget $script:DigestBudget -UnregisteredIntake $many
CheckTrue "80 refused + 10 starved fit the shipped budget" ($dm.Length -le $script:DigestBudget) $dm.Length
CheckTrue "未登録 header keeps the true total (80)" ($dm -match '未登録（登録不可）: 80 件') $dm
CheckTrue "未登録 keeps at least one row" ($dm -match '  mk/T-refused-0 — ') $dm
CheckTrue "未登録 truncation is counted" ($dm -match '\+\d+ 件（省略）') $dm
CheckTrue "飢餓 keeps its floor after 未登録" ($dm -match '  p/T-starved-') $dm

Write-Host "Get-ParkedRowTag — merge_wait lane survives the helper extraction"
Check "merge_wait tag" '   — [merge 待ち・PR 一覧に掲載]' (Get-ParkedRowTag -Row ([pscustomobject]@{ lane = 'merge_wait' }) -Default 'x')
Check "decision row keeps its default" 'x' (Get-ParkedRowTag -Row ([pscustomobject]@{ lane = 'decision' }) -Default 'x')

if ($script:failures -gt 0) {
    Write-Host ""
    Write-Host "unregistered intake: $script:failures check(s) FAILED"
    exit 1
}
Write-Host ""
Write-Host "unregistered intake: all checks passed"
