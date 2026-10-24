# Regression guard for the sweep's discovery intake of unregistered live threads
# (T-sweep-intake-discovery-by-default, Bohr msg-6776 + msg-6778, cleared by Einstein msg-6779; on top
# of T-sweep-intake-and-quarantine-stalls msg-5889 D-2 / msg-5893 D-2.1-D-2.4). Code under test:
# deploy/lib/UnregisteredIntake.ps1 and its call sites in deploy/run-conductor-scheduled.ps1.
#
# What is pinned, and how:
#   C  ConvertFrom-SweepIntakeConfig — defaults (shadow / 14) and a throw on every shape error.
#   R  Resolve-UnregisteredIntake — the repo_dir comes from clones only (no #448 inference), the clone
#      probe (absolute, existing, not the daemon checkout, not a linked worktree), D-2.5 (mode /
#      max_idle_days / exclude / time fail-closed), explicit candidates never classified (msg-6778),
#      ordering, the candidate-vs-clones WARN, the "?" for an unmeasured project or a failed probe, and
#      the PR-review refusal. The schema-drift counter (msg-7445 F-3c): a field_absent row per measured
#      project with >= 1 item lacking last_activity_at (also with 0 unregistered), the N/M WARN and
#      header wording (a count, never "the API lacks it"), and silence for a present-but-null key.
#   D-2.1  A thread that is not added (shadow, idle, excluded, refused, time-unknown) never joins the candidate array, so none of the three state files the
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
# One enumerator project row. $ids: thread ids; $times: optional hashtable id -> idle_days (number),
# 'missing' or 'unparseable'. An id with no entry is fresh (1 day).
function Proj {
    param($p, $ids, $count = 'auto', $err = $null, $times = @{}, [switch]$NoThreads)
    $c = if ($count -eq 'auto') { @($ids).Count } else { $count }
    $rows = @(foreach ($id in @($ids)) {
        $t = if ($times.ContainsKey($id)) { $times[$id] } else { 1.0 }
        if ($t -is [string]) { [pscustomobject]@{ thread_id = $id; time_state = $t; last_activity_at = $null; idle_days = $null } }
        else { [pscustomobject]@{ thread_id = $id; time_state = 'ok'; last_activity_at = '2026-10-01T00:00:00+00:00'; idle_days = [double]$t } }
    })
    $o = [pscustomobject]@{ project = $p; unregistered_count = $c; unregistered = @($ids); error = $err; malformed_count = 0 }
    if (-not $NoThreads) { $o | Add-Member -NotePropertyName threads -NotePropertyValue $rows }
    $o
}
function Rep { param([object[]]$Projects, $Field = 'last_activity_at')
    $r = [pscustomobject]@{ projects = @($Projects) }
    if ($null -ne $Field) { $r | Add-Member -NotePropertyName activity_field -NotePropertyValue $Field }
    $r }
function Cfg {
    param($Clones = @{}, $Mode = 'on', $MaxIdle = 14, $Exclude = @())
    $o = [ordered]@{}
    foreach ($k in $Clones.Keys) { $o[$k] = $Clones[$k] }
    $json = [ordered]@{ clones = $o; intake = [ordered]@{ mode = $Mode; max_idle_days = $MaxIdle }; exclude = @($Exclude) } |
        ConvertTo-Json -Depth 5
    ConvertFrom-SweepIntakeConfig -Raw ($json | ConvertFrom-Json)
}
$exists = { param($p) $true }
$missing = { param($p) $false }
$notWorktree = { param($p) $false }
$isWorktree = { param($p) $true }

$dirA = if ($IsWindows) { 'C:\clones\a' } else { '/clones/a' }
$dirB = if ($IsWindows) { 'C:\clones\b' } else { '/clones/b' }
$dirD = if ($IsWindows) { 'C:\daemon' } else { '/daemon' }
$cands = @(
    (Cand 'mw' 'T-listed' $dirA),
    (Cand 'mk' 'T-k1' $dirA),
    (Cand 'mk' 'T-k2' $dirB)
)
$on = Cfg -Clones ([ordered]@{ mw = $dirA; mk = $dirB })
function Resolve { param($Report, $Config = $on, $PathExists = $exists, $IsLinkedWorktree = $notWorktree, $Candidates = $cands)
    Resolve-UnregisteredIntake -Candidates $Candidates -Report $Report -IntakeConfig $Config -DaemonRoot $dirD `
        -PathExists $PathExists -IsLinkedWorktree $IsLinkedWorktree }

Write-Host "C — ConvertFrom-SweepIntakeConfig: defaults, and throws on a shape error"
$d0 = ConvertFrom-SweepIntakeConfig -Raw ('{"candidates":[]}' | ConvertFrom-Json)
Check "no intake key -> mode shadow" 'shadow' $d0.mode
Check "no intake key -> max_idle_days 14" 14 $d0.max_idle_days
Check "no clones key -> no clones" 0 $d0.clones.Count
Check "no exclude key -> no exclusions" 0 $d0.exclude.Count
$d1 = ConvertFrom-SweepIntakeConfig -Raw ('{"clones":{"b":"/x","a":"/y"},"exclude":["p/T-a"]}' | ConvertFrom-Json)
Check "clones keep declaration order" 'b,a' (@($d1.clones.Keys) -join ',')
CheckTrue "exclude is a key set" $d1.exclude.ContainsKey('p/T-a')
foreach ($bad in @(
        '{"clones":["x"]}', '{"clones":"x"}', '{"clones":{"p":""}}', '{"clones":{"p":"  "}}', '{"clones":{"p":3}}',
        '{"intake":"on"}', '{"intake":{"mode":"ON"}}', '{"intake":{"mode":"off"}}', '{"intake":{"max_idle_days":0}}',
        '{"intake":{"max_idle_days":"14"}}', '{"intake":{"max_idle_days":-1}}',
        '{"exclude":"p/T-a"}', '{"exclude":["T-a"]}', '{"exclude":[3]}', '{"exclude":["/T-a"]}', '{"exclude":["p/"]}')) {
    $threw = $false
    try { $null = ConvertFrom-SweepIntakeConfig -Raw ($bad | ConvertFrom-Json) } catch { $threw = $true }
    CheckTrue "shape error throws: $bad" $threw
}

$ex = ConvertFrom-SweepIntakeConfig -Raw (Get-Content -LiteralPath (Join-Path $repoRoot 'deploy/sweep.json.example') -Raw -Encoding utf8 | ConvertFrom-Json)
Check "the shipped sweep.json.example parses, and documents the shadow default" 'shadow' $ex.mode
CheckTrue "…and declares at least one clone" ($ex.clones.Count -gt 0)

Write-Host "R — Resolve-UnregisteredIntake: the clone comes from clones only (no inference)"
$i = Resolve (Rep (Proj 'mw' @('T-new1', 'T-new2')))
Check "clones[mw] usable + mode on + fresh -> 2 added" 2 @($i.added).Count
Check "added row carries clones[project]" $dirA $i.added[0].repo_dir
Check "added row key is project/thread_id" 'mw/T-new1' $i.added[0].key
Check "nothing refused" 0 @($i.refused).Count
Check "no probe error" $null $i.probe_error

$i = Resolve (Rep (Proj 'mk' @('T-x')))
Check "mk has two candidate repo_dirs but one clone -> added (穴 2)" 1 @($i.added).Count
Check "…with the declared clone, not a candidate's repo_dir" $dirB $i.added[0].repo_dir

$i = Resolve (Rep (Proj 'pnew' @('T-n'))) -Config (Cfg -Clones ([ordered]@{ pnew = $dirA }))
Check "project with no candidate at all is added from clones (穴 1)" 'pnew/T-n' $i.added[0].key

$i = Resolve (Rep (Proj 'mw' @('T-y'))) -Config (Cfg -Clones ([ordered]@{ mk = $dirB }))
Check "project without a clones entry -> refused 'clones 未登録' (even though candidates name a repo_dir)" 'clones 未登録' $i.refused[0].reason
Check "…and nothing is added" 0 @($i.added).Count

$i = Resolve (Rep (Proj 'mw' @('T-a', 'T-b'))) -PathExists $missing
Check "clone does not exist -> refused 'missing'" "clone missing: $dirA" $i.refused[0].reason
Check "every thread of the project is refused" 2 @($i.refused).Count

$i = Resolve (Rep (Proj 'rel' @('T-y'))) -Config (Cfg -Clones ([ordered]@{ rel = 'rel/path' }))
Check "relative clone -> refused 'not absolute' (checked before existence)" 'clone not absolute: rel/path' $i.refused[0].reason

$i = Resolve (Rep (Proj 'self' @('T-s'))) -Config (Cfg -Clones ([ordered]@{ self = $dirD }))
Check "clone == daemon checkout -> refused" "clone is the daemon's own checkout: $dirD" $i.refused[0].reason
$inner = Join-Path $dirD 'sub'
$i = Resolve (Rep (Proj 'self' @('T-s'))) -Config (Cfg -Clones ([ordered]@{ self = $inner }))
Check "clone inside the daemon checkout -> refused" "clone is the daemon's own checkout: $inner" $i.refused[0].reason

$i = Resolve (Rep (Proj 'mw' @('T-w'))) -IsLinkedWorktree $isWorktree
Check "clone is a linked worktree -> refused" "clone is a linked worktree: $dirA" $i.refused[0].reason

$i = Resolve (Rep (Proj 'mw' @('T-pr-review-spirrow-mindwire-9')))
Check "PR-review thread is never added (paid gate)" 0 @($i.added).Count
CheckTrue "PR-review thread is refused, with a reason" ($i.refused[0].reason -like 'pr-review thread*') $i.refused[0].reason
$i = Resolve (Rep (Proj 'mw' @('T-pr-review-spirrow-mindwire-9'))) -Config (Cfg -Clones ([ordered]@{ mw = $dirA }) -Exclude @('mw/T-pr-review-spirrow-mindwire-9'))
CheckTrue "PR-review stays refused even when also excluded (the rule is not a config option)" ($i.refused[0].reason -like 'pr-review thread*')

$i = Resolve (Rep (Proj 'mw' @('T-listed', 'T-z')))
Check "already-listed thread is not added twice" 1 @($i.added).Count
Check "the other one is" 'mw/T-z' $i.added[0].key

Write-Host "R — D-2.5: mode / max_idle_days / exclude / time"
$shadowCfg = Cfg -Clones ([ordered]@{ mw = $dirA }) -Mode 'shadow'
$i = Resolve (Rep (Proj 'mw' @('T-s1', 'T-s2') -times @{ 'T-s1' = 3.25 })) -Config $shadowCfg
Check "mode shadow -> nothing added" 0 @($i.added).Count
Check "mode shadow -> both in shadow" 'mw/T-s1,mw/T-s2' ((@($i.shadow) | ForEach-Object { $_.key }) -join ',')
Check "shadow row carries idle_days" 3.25 $i.shadow[0].idle_days
$i = Resolve-UnregisteredIntake -Candidates $cands -Report (Rep (Proj 'mw' @('T-d'))) -PathExists $exists -IsLinkedWorktree $notWorktree
Check "no intake config at all -> default shadow, and no clones -> refused" 'clones 未登録' $i.refused[0].reason
Check "…default mode echoed" 'shadow' $i.mode

$i = Resolve (Rep (Proj 'mw' @('T-old', 'T-edge') -times @{ 'T-old' = 14.001; 'T-edge' = 14.0 }))
Check "idle beyond max_idle_days -> 放置" 'mw/T-old' ((@($i.idle) | ForEach-Object { $_.key }) -join ',')
Check "idle exactly at the limit -> still added" 'mw/T-edge' ((@($i.added) | ForEach-Object { $_.key }) -join ',')
$i = Resolve (Rep (Proj 'mw' @('T-old') -times @{ 'T-old' = 40 })) -Config (Cfg -Clones ([ordered]@{ mw = $dirA }) -Mode 'shadow')
Check "放置 wins over shadow (an idle thread would not join even when switched on)" '1/0' "$(@($i.idle).Count)/$(@($i.shadow).Count)"

$i = Resolve (Rep (Proj 'mw' @('T-ex', 'T-in'))) -Config (Cfg -Clones ([ordered]@{ mw = $dirA }) -Exclude @('mw/T-ex'))
Check "excluded -> counted only" 1 $i.excluded_count
Check "…not added, not listed anywhere" 'mw/T-in' ((@($i.added) | ForEach-Object { $_.key }) -join ',')
Check "…and no row anywhere" 0 (@($i.refused).Count + @($i.shadow).Count + @($i.idle).Count + @($i.time_unknown).Count)

$i = Resolve (Rep (Proj 'mw' @('T-m', 'T-u') -times @{ 'T-m' = 'missing'; 'T-u' = 'unparseable' }))
Check "missing / unparseable last_activity_at -> 時刻不明, not added (fail-closed)" '0/2' "$(@($i.added).Count)/$(@($i.time_unknown).Count)"
Check "時刻不明 reason names the field and the state" 'last_activity_at missing' $i.time_unknown[0].reason
$i = Resolve (Rep (Proj 'mw' @('T-old-enum') -NoThreads))
Check "a report row with no per-thread detail is 時刻不明 (never assumed fresh)" 'mw/T-old-enum' $i.time_unknown[0].key

Write-Host "R — schema drift: listed items with no last_activity_at key (msg-7445 F-3c)"
function WithAbsent { param($Row, $Absent, $Listed)
    $Row | Add-Member -NotePropertyName field_absent_count -NotePropertyValue $Absent -Force
    $Row | Add-Member -NotePropertyName listed_count -NotePropertyValue $Listed -Force
    $Row }
$i = Resolve (Rep (WithAbsent (Proj 'mw' @()) 23 23))
Check "all 23/23 lacking the key, 0 unregistered -> one field_absent row" 'mw:23/23' ((@($i.field_absent) | ForEach-Object { "$($_.project):$($_.absent)/$($_.listed)" }) -join ',')
Check "…tick-log WARN is the N/M count" 'WARN listing schema: mw 23/23 items lack last_activity_at' ((@(Get-UnregisteredIntakeSchemaWarnings -Intake $i)) -join '|')
$h = Get-UnregisteredIntakeHeader -Intake $i
CheckTrue "…時刻不明 header carries the fact" ($h.Contains('時刻不明 0 件［mw：一覧の 23/23 件に last_activity_at が無い］')) $h
$i = Resolve (Rep (WithAbsent (Proj 'mw' @('T-a')) 1 100))
$h = Get-UnregisteredIntakeHeader -Intake $i
CheckTrue "1/100 -> header says 1/100" ($h.Contains('mw：一覧の 1/100 件に last_activity_at が無い')) $h
CheckTrue "…and never claims the API lacks the field (Einstein msg-7444)" (-not ($h -match 'API|スキーマ|可能性')) $h
$i = Resolve (Rep (WithAbsent (Proj 'mw' @('T-n') -times @{ 'T-n' = 'missing' }) 0 5))
Check "key present but null (absent=0) -> no row, no WARN" '0/0' "$(@($i.field_absent).Count)/$(@(Get-UnregisteredIntakeSchemaWarnings -Intake $i).Count)"
Check "…the thread is still 時刻不明" 'mw/T-n' $i.time_unknown[0].key
CheckTrue "…and the header has no drift note" (-not (Get-UnregisteredIntakeHeader -Intake $i).Contains('［')) (Get-UnregisteredIntakeHeader -Intake $i)
$i = Resolve (Rep (Proj 'mw' @('T-a')))
Check "a report without the counter (older enumerator) -> no row" 0 @($i.field_absent).Count
$errRow = [pscustomobject]@{ project = 'vw'; unregistered_count = $null; unregistered = @(); error = 'boom'; malformed_count = $null
    field_absent_count = $null; listed_count = $null }
$i = Resolve (Rep $errRow)
Check "an unmeasured project -> no drift row (unknown, not 0 or N)" 0 @($i.field_absent).Count
$i = Resolve (Rep @((WithAbsent (Proj 'mw' @()) 2 4), (WithAbsent (Proj 'mk' @()) 3 3)))
Check "two projects -> one WARN each" 2 @(Get-UnregisteredIntakeSchemaWarnings -Intake $i).Count
# The field name is the enumerator's, not a literal here (PR #497 gate advisory).
$i = Resolve (Rep @((WithAbsent (Proj 'mw' @('T-m') -times @{ 'T-m' = 'missing' }) 1 2)) -Field 'x_at')
Check "field name comes from the report: WARN" 'WARN listing schema: mw 1/2 items lack x_at' ((@(Get-UnregisteredIntakeSchemaWarnings -Intake $i)) -join '|')
CheckTrue "…header" ((Get-UnregisteredIntakeHeader -Intake $i).Contains('mw：一覧の 1/2 件に x_at が無い')) (Get-UnregisteredIntakeHeader -Intake $i)
Check "…時刻不明 reason" 'x_at missing' $i.time_unknown[0].reason
$i = Resolve (Rep @((WithAbsent (Proj 'mw' @()) 1 2)) -Field $null)
Check "a report without activity_field -> '?' (never a guessed name)" 'WARN listing schema: mw 1/2 items lack ?' ((@(Get-UnregisteredIntakeSchemaWarnings -Intake $i)) -join '|')
$sweepText = Get-Content -LiteralPath $sweepScript -Raw
CheckTrue "the sweep writes the schema WARN lines to the tick log" ($sweepText -match '\$schemaWarnings = @\(Get-UnregisteredIntakeSchemaWarnings -Intake \$unregisteredIntake\)' -and
    $sweepText -match 'foreach \(\$w in \$schemaWarnings\) \{ Write-Log \$w \}')

Write-Host "R — explicit candidates are never classified (msg-6778 regression)"
# Defensive: even if a report DID list an explicit candidate's key, with a missing / unparseable /
# over-limit time, under shadow, and excluded, it stays in the tick's candidate array exactly once.
foreach ($t in @('missing', 'unparseable', 999)) {
    $cfgX = Cfg -Clones ([ordered]@{ mw = $dirA }) -Mode 'shadow' -MaxIdle 1 -Exclude @('mw/T-listed')
    $i = Resolve (Rep (Proj 'mw' @('T-listed') -times @{ 'T-listed' = $t })) -Config $cfgX
    $j = Join-UnregisteredCandidates -Candidates $cands -Intake $i
    $hits = @($j | Where-Object { $_.key -eq 'mw/T-listed' })
    Check "explicit candidate with time '$t' is in the tick candidates exactly once" 1 $hits.Count
    Check "…with its own repo_dir" $dirA $hits[0].repo_dir
    Check "…and in no bucket" 0 (@($i.shadow).Count + @($i.idle).Count + @($i.time_unknown).Count + @($i.refused).Count + $i.excluded_count)
}

Write-Host "R — ordering of discovered threads: clones order, then oldest updated_at first"
$ordCfg = Cfg -Clones ([ordered]@{ zz = $dirB; aa = $dirA })
$i = Resolve (Rep @((Proj 'aa' @('T-a1', 'T-a2') -times @{ 'T-a1' = 1; 'T-a2' = 5 }),
                    (Proj 'zz' @('T-z1', 'T-z2') -times @{ 'T-z1' = 2; 'T-z2' = 9 }))) -Config $ordCfg
Check "zz (declared first) before aa; oldest first within each" 'zz/T-z2,zz/T-z1,aa/T-a2,aa/T-a1' ((@($i.added) | ForEach-Object { $_.key }) -join ',')

Write-Host "R — WARN when an explicit candidate's repo_dir differs from clones"
$w = @(Get-CloneCandidateMismatches -Candidates $cands -Clones $on.clones)
Check "mw/T-listed agrees with clones[mw]; mk/T-k1 differs; mk/T-k2 agrees" 1 $w.Count
CheckTrue "WARN names the candidate and both paths" ($w[0] -like "WARN *mk/T-k1*$dirA*$dirB*") $w[0]

Write-Host "R — unmeasured project / failed probe render '?', never 0"
$i = Resolve (Rep @((Proj 'mw' @() -count $null -err 'chatroom_list_threads failed: Timeout'), (Proj 'mk' @())))
Check "unregistered_count=null -> nothing added" 0 @($i.added).Count
Check "unregistered_count=null -> one unmeasured row" 1 @($i.unmeasured).Count
Check "unmeasured carries the error" 'chatroom_list_threads failed: Timeout' $i.unmeasured[0].reason
CheckTrue "header shows '?' when a project was not measured" ((Get-UnregisteredIntakeHeader -Intake $i) -like '*0 件 + ?*') (Get-UnregisteredIntakeHeader -Intake $i)

$i = Resolve-UnregisteredIntake -Candidates $cands -Report $null -ProbeError 'exit=1: boom' -IntakeConfig $on -PathExists $exists
Check "probe error -> nothing added" 0 @($i.added).Count
Check "probe error is named" 'unregistered_threads probe failed: exit=1: boom' $i.probe_error
CheckTrue "probe error -> header '?'" ((Get-UnregisteredIntakeHeader -Intake $i) -like '*+ ?*') (Get-UnregisteredIntakeHeader -Intake $i)
$jf = Join-UnregisteredCandidates -Candidates $cands -Intake $i
Check "Join with a failed probe returns exactly the listed candidates" 3 $jf.Count

$i = Resolve (Rep (Proj 'mw' @()))
Check "measured 0 -> header '0 件' without '?'" '未登録（登録不可）: 0 件（この tick の自動登録 0 件 / 登録予定（shadow） 0 件 / 放置 0 件 / 時刻不明 0 件 / 除外 0 件）' (Get-UnregisteredIntakeHeader -Intake $i)

Write-Host "D-2.2 — added rows are ordinary candidates (same shape, no marker)"
$rep = Rep (Proj 'mw' @('T-new1', 'T-new2'))
$i = Resolve $rep
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

Write-Host "D-2.1 — a thread that is not added cannot reach any state file (all five kinds)"
$mixCfg = Cfg -Clones ([ordered]@{ mw = $dirA; sh = $dirA }) -Exclude @('mw/T-excluded')
$shCfg = $mixCfg
$mixed = Resolve (Rep @(
        (Proj 'mw' @('T-ok', 'T-idle', 'T-excluded', 'T-notime') -times @{ 'T-idle' = 30; 'T-notime' = 'unparseable' }),
        (Proj 'mk2' @('T-refused')))) -Config $mixCfg
$shadowOnly = Resolve (Rep (Proj 'sh' @('T-shadow'))) -Config (Cfg -Clones ([ordered]@{ sh = $dirA }) -Mode 'shadow')
Check "one added; one each of idle / excluded / refused / time-unknown" '1/1/1/1/1' `
    "$(@($mixed.added).Count)/$(@($mixed.idle).Count)/$($mixed.excluded_count)/$(@($mixed.refused).Count)/$(@($mixed.time_unknown).Count)"
Check "…and one shadow" 1 @($shadowOnly.shadow).Count
$joined = Join-UnregisteredCandidates -Candidates $cands -Intake $mixed
$joinedS = Join-UnregisteredCandidates -Candidates $cands -Intake $shadowOnly
$liveKeys = @(@($joined) + @($joinedS) | ForEach-Object { $_.key })
foreach ($k in 'mk2/T-refused', 'mw/T-idle', 'mw/T-excluded', 'mw/T-notime', 'sh/T-shadow') {
    CheckTrue "$k is not a candidate (so no state file can get its key)" ($liveKeys -notcontains $k) ($liveKeys -join ',')
}
CheckTrue "added key is a candidate" ($liveKeys -contains 'mw/T-ok') ($liveKeys -join ',')

# Structural half: in the run section (outside function bodies), none of the not-added lists is
# assigned into $candidates; only Join-UnregisteredCandidates (which reads `added`) is.
$funcDefs = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true))
function Test-InFunction { param($Node) $p = $Node.Parent; while ($p) { if ($p -is [System.Management.Automation.Language.FunctionDefinitionAst]) { return $true }; $p = $p.Parent }; return $false }
$refusedUses = @($ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.MemberExpressionAst] -and
    $n.Member.Extent.Text -eq 'refused' -and $n.Expression.Extent.Text -eq '$unregisteredIntake' }, $true) |
    Where-Object { -not (Test-InFunction $_) })
CheckTrue "the run section reads `$unregisteredIntake.refused somewhere" ($refusedUses.Count -gt 0)
$bucketUses = @($ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.MemberExpressionAst] -and
    $n.Member.Extent.Text -in @('refused', 'shadow', 'idle', 'time_unknown', 'excluded_count') -and
    $n.Expression.Extent.Text -eq '$unregisteredIntake' }, $true) |
    Where-Object { -not (Test-InFunction $_) })
$badUse = @($bucketUses | Where-Object {
    $stmt = $_; while ($stmt -and -not ($stmt -is [System.Management.Automation.Language.AssignmentStatementAst])) { $stmt = $stmt.Parent }
    $stmt -and $stmt.Left.Extent.Text -eq '$candidates' })
Check "no assignment to `$candidates reads any not-added list" 0 $badUse.Count
$candAssign = @($ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$candidates' }, $true) |
    Where-Object { -not (Test-InFunction $_) } | ForEach-Object { ($_.Right.Extent.Text -split '\s')[0] })
Check "`$candidates is assigned only by Get-SweepCandidates / Join-UnregisteredCandidates / ordering" `
    'Get-SweepCandidates,Join-UnregisteredCandidates,Get-OrderedSweepCandidates' (($candAssign | Where-Object { $_ -ne '$sweepOrderCandidates' }) -join ',')
$inferenceGone = @($ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Test-UnregisteredRepoDir' }, $true))
Check "the #448 repo_dir inference is not called anywhere (msg-6770 §2)" 0 $inferenceGone.Count
CheckTrue "…nor defined in the intake library" (-not (Get-Command Test-UnregisteredRepoDir -ErrorAction SilentlyContinue))
$resolveCall = @($ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Resolve-UnregisteredIntake' }, $true) |
    Where-Object { -not (Test-InFunction $_) })
Check "exactly one Resolve-UnregisteredIntake call in the run section" 1 $resolveCall.Count
$params = @($resolveCall[0].CommandElements | Where-Object { $_ -is [System.Management.Automation.Language.CommandParameterAst] } | ForEach-Object { $_.ParameterName })
CheckTrue "…which passes -IntakeConfig and -DaemonRoot" (($params -contains 'IntakeConfig') -and ($params -contains 'DaemonRoot')) ($params -join ',')

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

$dIntake = @{ added = @((Cand 'mw' 'T-a' $dirA)); probe_error = $null; max_idle_days = 14; excluded_count = 2
    unmeasured = @([pscustomobject]@{ project = 'vw'; reason = 'Timeout' })
    refused = @([pscustomobject]@{ project = 'mk'; thread_id = 'T-r'; key = 'mk/T-r'; reason = 'clones 未登録' })
    shadow = @([pscustomobject]@{ project = 'mw'; thread_id = 'T-s'; key = 'mw/T-s'; idle_days = 3.25 })
    idle = @([pscustomobject]@{ project = 'mw'; thread_id = 'T-i'; key = 'mw/T-i'; idle_days = 40 })
    time_unknown = @([pscustomobject]@{ project = 'mw'; thread_id = 'T-u'; key = 'mw/T-u'; reason = 'last_activity_at missing' }) }
$d = New-DailyDigest @base -UnregisteredIntake $dIntake
CheckTrue "section header counts every bucket, with '?'" ($d.Contains('未登録（登録不可）: 1 件 + ?（この tick の自動登録 1 件 / 登録予定（shadow） 1 件 / 放置 1 件 / 時刻不明 1 件 / 除外 2 件）')) $d
CheckTrue "refused row names key and reason" ($d -match '  mk/T-r — clones 未登録') $d
CheckTrue "unmeasured row shows '?'" ($d -match '  vw — \? 測れなかった: Timeout') $d
CheckTrue "時刻不明 row shows '?' and the state" ($d -match '  mw/T-u — \? 時刻不明（登録しない）: last_activity_at missing') $d
CheckTrue "shadow row shows 登録予定 and idle days" ($d -match '  mw/T-s — 登録予定（shadow）: 最終更新から 3\.3 日|  mw/T-s — 登録予定（shadow）: 最終更新から 3\.2 日') $d
CheckTrue "放置 row shows idle days and the limit" ($d -match '  mw/T-i — 放置（登録しない）: 最終更新から 40 日 > 上限 14 日') $d
CheckTrue "excluded threads get no row (count only)" (-not ($d -match '除外（')) $d
$legacy = New-DailyDigest @base -UnregisteredIntake @{ added = @(); refused = @(); unmeasured = @(); probe_error = $null }
CheckTrue "an intake without the new keys renders zeros, not phantom rows" ($legacy.Contains('未登録（登録不可）: 0 件（この tick の自動登録 0 件 / 登録予定（shadow） 0 件 / 放置 0 件 / 時刻不明 0 件 / 除外 0 件）')) $legacy
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
CheckTrue "measured and nothing refused -> header at 0 件 plus (該当なし)" ($d0 -match "未登録（登録不可）: 0 件（この tick の自動登録 0 件 / 登録予定（shadow） 0 件 / 放置 0 件 / 時刻不明 0 件 / 除外 0 件）`n  \(該当なし\)") $d0

# Budget: 80 refused rows + 10 starved rows under the shipped budget.
$many = @{ added = @(); unmeasured = @(); probe_error = $null; refused = @() }
for ($k = 0; $k -lt 80; $k++) {
    $many.refused += [pscustomobject]@{ project = 'mk'; thread_id = "T-refused-$k"; key = "mk/T-refused-$k"; reason = 'clones 未登録' }
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
