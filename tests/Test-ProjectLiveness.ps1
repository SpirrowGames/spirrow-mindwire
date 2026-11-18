# tests/Test-ProjectLiveness.ps1 — deploy/lib/ProjectLiveness.ps1 (T-digest-lacks-project-liveness-view).
#
# Spec: Bohr msg-2191 .. msg-2203 (design v7), msg-8380 (implementation plan and the regression
# table), msg-8382 / msg-8384 / msg-8386 (R-10 .. R-12). What this file pins:
#   * 判定表 v3: every one of the 8 rows is reachable, and a sweep over signal combinations always
#     yields exactly one state and a tier (totality / exclusivity, closure rule throws otherwise)
#   * the msg-2193 .. msg-2203 holes, one regression case each (msg-8380 §3 table)
#   * L0 <= 140 / L1 <= 400, attention order, tail-only omission with the tier breakdown
#   * R-12 follow-ups: same row set and order as the omitted rows, <= budget each, rows never split,
#     (k/n) consecutive, no file path anywhere
#   * R-10 snapshot: generated_at matches the @stamp, every row present unfolded
#   * UNMEASURED (msg-9346 §2, msg-9560, R-13 msg-9562): a failed thread listing is a named tier-2
#     row; an unknown PR count never yields SHIP / DONE / EMPTY, ends in the explicit sink when no
#     PR-independent state matches, and keeps a known critical critical

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'deploy/lib/ProjectLiveness.ps1')

$script:failures = 0
function Check {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} {1}" -f $Name, $Detail) }
}

$now = [datetime]::new(2026, 11, 8, 13, 40, 0, [DateTimeKind]::Utc)
function Ago([double]$Days) { return $now.AddDays(-$Days).ToString('o') }
function T([string]$P, [string]$Id, [string]$Status, $Act) { [PSCustomObject]@{ project = $P; thread_id = $Id; status = $Status; last_activity_at = $Act } }
function S([string]$P, [string]$Id) { [PSCustomObject]@{ project = $P; thread_id = $Id } }
function PR([string]$P, [double]$Days) { [PSCustomObject]@{ project = $P; created_at = (Ago $Days) } }

function Rows {
    param($Threads = @(), $Sweep = @(), $Nom = @{}, $Prs = @(), $Projects = @(), $PrsU = @(), $ThreadsU = @())
    $facts = Get-ProjectLivenessFacts -Threads $Threads -SweepEntries $Sweep -Nominations $Nom -OpenPrs $Prs -Projects $Projects `
        -PrsUnmeasured $PrsU -ThreadsUnmeasured $ThreadsU -Now $now
    return , @($facts | ForEach-Object { Get-ProjectLivenessRow -Facts $_ })
}
function One { param($Threads = @(), $Sweep = @(), $Nom = @{}, $Prs = @(), $Projects = @(), $PrsU = @(), $ThreadsU = @()) (Rows $Threads $Sweep $Nom $Prs $Projects $PrsU $ThreadsU)[0] }

Write-Host "status reading (single interpretation point)"
Check 'active -> open' ((ConvertTo-LivenessThreadClass 'active') -eq 'open')
Check 'awaiting_reply -> open' ((ConvertTo-LivenessThreadClass 'awaiting_reply') -eq 'open')
Check 'parked -> parked' ((ConvertTo-LivenessThreadClass 'parked') -eq 'parked')
Check 'resolved/superseded -> closed' ((ConvertTo-LivenessThreadClass 'resolved') -eq 'closed' -and (ConvertTo-LivenessThreadClass 'superseded') -eq 'closed')
Check 'unknown status is not guessed' ((ConvertTo-LivenessThreadClass 'weird') -eq 'unknown')

Write-Host "判定表 v3 — each row reachable"
$r = One @(T p a active (Ago 20)) @() ; Check '1 UNTRACKED' ($r.state -eq 'UNTRACKED') $r.full
$r = One @(T p a active (Ago 1)) @(S p a) @{ 'p/a' = 'human' } ; Check '2 HUMAN' ($r.state -eq 'HUMAN') $r.full
$r = One @(T p a active (Ago 5)) @(S p a) @{ 'p/a' = 'Bohr' } ; Check '3 STALLED' ($r.state -eq 'STALLED') $r.full
$r = One @(T p a active (Ago 1)) @(S p a) @{ 'p/a' = 'Bohr' } ; Check '4 RUNNING' ($r.state -eq 'RUNNING' -and $r.tier -eq 2) $r.full
$r = One @(T p a resolved (Ago 1)) @() @{} @(PR p 2) ; Check '5 SHIP' ($r.state -eq 'SHIP' -and $r.tier -eq 2) $r.full
$r = One @(T p a parked (Ago 40)) ; Check '6 PARKED (tier 2, age does not promote)' ($r.state -eq 'PARKED' -and $r.tier -eq 2) $r.full
$r = One @(T p a resolved (Ago 1)) ; Check '7 DONE(tracked) quiet' ($r.state -eq 'DONE' -and $r.tier -eq 0) $r.full
$r = One @() @() @{} @() @('p') ; Check '8 EMPTY quiet' ($r.state -eq 'EMPTY' -and $r.tier -eq 0) $r.full

Write-Host "totality / exclusivity sweep (with the R-13 PR-unknown axis)"
$ok = $true; $n = 0; $nUnknown = 0; $prDepSeen = @(); $sinkSeen = 0
foreach ($prKnown in $true, $false) {
foreach ($openTracked in 0, 1) { foreach ($openDark in 0, 1) { foreach ($parkedN in 0, 1) { foreach ($closedN in 0, 1) {
foreach ($prN in 0, 1) { foreach ($rotN in 0, 1) { foreach ($nom in 'human', 'Bohr', 'none') { foreach ($age in 1, 5, 10, $null) {
    $th = @(); $sw = @(); $nm = @{}
    if ($openTracked) { $th += T q t active $(if ($null -eq $age) { $null } else { Ago $age }); $sw += S q t; $nm['q/t'] = $nom }
    if ($openDark) { $th += T q d awaiting_reply (Ago 2) }
    if ($parkedN) { $th += T q k parked (Ago 30) }
    if ($closedN) { $th += T q c resolved (Ago 9) }
    if ($rotN) { $sw += S q ghost }
    $prs = @(); if ($prN) { $prs += PR q 3 }
    $prsU = if ($prKnown) { @() } else { @('q') }
    try {
        $all = Rows $th $sw $nm $prs @('q') $prsU; $n++
        # exactly one row (one state) per project
        if (@($all).Count -ne 1) { $ok = $false; Write-Host "    not one row: $(@($all).Count)" }
        $row = $all[0]
        if (-not ($row.state -in $LivenessStateOrder) -or $row.tier -lt 0 -or $row.tier -gt 3) { $ok = $false }
        if (-not $prKnown) {
            $nUnknown++
            if ($row.state -in @('SHIP', 'DONE', 'EMPTY')) { $prDepSeen += $row.full }
            if ($row.state -eq 'UNMEASURED') { $sinkSeen++ }
            if ($row.tier -lt 2) { $ok = $false; Write-Host "    PR-unknown row below tier 2: $($row.full)" }
            if ($row.full -notlike '* · PRs unmeasured*' -or $row.minimal -notlike '* · PRs unmeasured*') { $ok = $false; Write-Host "    PR-unknown row without the marker: $($row.full)" }
            if ($row.full -match '· PR \d') { $ok = $false; Write-Host "    PR-unknown row shows a PR count: $($row.full)" }
        }
        elseif ($row.state -eq 'UNMEASURED') { $ok = $false; Write-Host "    measured row in the sink: $($row.full)" }
        # INV-TIER: tier 3 <=> ratio > 1
        if (($row.tier -eq 3) -ne ($row.ratio -gt 1)) { $ok = $false; Write-Host "    INV-TIER broken: $($row.full) tier=$($row.tier) ratio=$($row.ratio)" }
        if ($rotN -and $row.tier -lt 1) { $ok = $false }
    } catch { $ok = $false; Write-Host "    threw: $_" }
} } } } } } } } }
Check "every combination ($n) yields one state, a tier, and INV-TIER" ($ok -and $n -eq 1536) "n=$n"
Check "PR count unknown ($nUnknown inputs) -> never SHIP / DONE / EMPTY" ($prDepSeen.Count -eq 0 -and $nUnknown -eq 768) ($prDepSeen -join ' | ')
Check "PR count unknown -> the sink is reached" ($sinkSeen -gt 0) "sink=$sinkSeen"

Write-Host "closure rule"
$threw = $false; try { $null = Get-LivenessElementTier -Table $LivenessStateTier -Key 'NOPE' -Agg $null -Use 'max' -Kind 'state' } catch { $threw = $true }
Check 'unmapped state throws' $threw
$threw = $false; try { $null = Get-LivenessElementTier -Table $LivenessSignalTier -Key 'nope' -Agg $null -Use 'max' -Kind 'signal' } catch { $threw = $true }
Check 'unmapped signal throws' $threw
$threw = $false; $saved = $LivenessStateTier['PARKED']
try {
    $LivenessStateTier['PARKED'] = @{ threshold = $null; under = 2; over = 2 }
    try { $null = Test-LivenessStatePrDependent 'PARKED' } catch { $threw = $true }
} finally { $LivenessStateTier['PARKED'] = $saved }
Check 'a state without the pr_dependent column throws (R-13 closure)' $threw
Check 'pr_dependent column: SHIP / DONE / EMPTY only' ((@($LivenessTableOrder | Where-Object { Test-LivenessStatePrDependent $_ }) -join ',') -eq 'SHIP,DONE,EMPTY')

Write-Host "msg-8380 regression table"
# vacuous truth: every open thread dark, nominations human — must be UNTRACKED, never HUMAN.
$r = One @((T p a active (Ago 1)), (T p b active (Ago 2))) @() @{ 'p/a' = 'human'; 'p/b' = 'human' }
Check 'empty-set HUMAN -> UNTRACKED' ($r.state -eq 'UNTRACKED' -and $r.full -notmatch '^p HUMAN') $r.full
# all parked: never DONE.
$r = One @((T p a parked (Ago 3)), (T p b parked (Ago 5)))
Check 'all parked -> PARKED (no DONE)' ($r.state -eq 'PARKED' -and $r.full -notmatch 'DONE') $r.full
# threads=0 with rot: own line, not folded.
$res = Get-ProjectLivenessDigestLines -Facts (Get-ProjectLivenessFacts -SweepEntries @(S ghost x) -Now $now) -GeneratedAt $now
Check 'threads=0 ∧ rot -> EMPTY (sweep-only) · rot 1 row' (@($res.L1) -contains 'ghost EMPTY (sweep-only) · rot 1') ($res.L1 -join ' | ')
Check '... and not folded into quiet' (-not (@($res.L1) | Where-Object { $_ -like 'quiet:*' })) ($res.L1 -join ' | ')
# HUMAN 20d! above rot.
$facts = Get-ProjectLivenessFacts -Threads @(T h a active (Ago 20)) -SweepEntries @((S h a), (S ghost x)) -Nominations @{ 'h/a' = 'human' } -Now $now
$res = Get-ProjectLivenessDigestLines -Facts $facts -GeneratedAt $now
$iH = [array]::IndexOf(@($res.L1), (@($res.L1) | Where-Object { $_ -like 'h HUMAN 20d!*' } | Select-Object -First 1))
$iR = [array]::IndexOf(@($res.L1), (@($res.L1) | Where-Object { $_ -like 'ghost *rot 1*' } | Select-Object -First 1))
Check 'HUMAN 20d! row above the rot row' ($iH -ge 0 -and $iR -ge 0 -and $iH -lt $iR) ($res.L1 -join ' | ')
# UNTRACKED 1d is tier 2.
$r = One @(T p a active (Ago 1))
Check 'UNTRACKED 1d -> tier 2' ($r.state -eq 'UNTRACKED' -and $r.tier -eq 2) "tier=$($r.tier)"
# all tracked ages missing -> STALLED.
$r = One @((T p a active $null), (T p b active 'garbage')) @((S p a), (S p b)) @{ 'p/a' = 'Bohr'; 'p/b' = 'Bohr' }
Check 'all tracked age missing -> STALLED' ($r.state -eq 'STALLED' -and $r.tier -eq 3) $r.full
# mixed missing + today -> RUNNING · stale 1/2 (age?).
$r = One @((T p a active $null), (T p b active (Ago 0.1))) @((S p a), (S p b)) @{ 'p/a' = 'Bohr'; 'p/b' = 'Bohr' }
Check 'missing + today -> RUNNING · stale 1/2 (age?)' ($r.state -eq 'RUNNING' -and $r.tier -eq 3 -and $r.full -like '*stale 1/2 (age?)*') $r.full
Check 'missing age ratio is 1+delta (critical, lowest)' ([math]::Abs($r.ratio - (1 + $LivenessRatioDelta)) -lt 1e-9) "ratio=$($r.ratio)"

Write-Host "budget and omission"
$th = @(); $sw = @()
for ($i = 0; $i -lt 30; $i++) {
    $p = "project-number-{0:d2}" -f $i
    $th += T $p a active (Ago (1 + $i)); $th += T $p b active (Ago 2)
    $sw += S $p a
    if ($i % 3 -eq 0) { $sw += S $p ghost }
}
$facts = Get-ProjectLivenessFacts -Threads $th -SweepEntries $sw -Nominations @{} -Now $now
$res = Get-ProjectLivenessDigestLines -Facts $facts -GeneratedAt $now
$l1Cost = 0; foreach ($x in $res.L1) { $l1Cost += $x.Length + 1 }
Check 'L0 <= 140' ($res.L0.Length -le 140) "$($res.L0.Length): $($res.L0)"
Check 'L1 <= 400' ($l1Cost -le 400) "$l1Cost"
$omitLine = @($res.L1) | Where-Object { $_ -like '+* rows omitted (*' }
Check 'omitted line with breakdown and follow-up stamp' ($omitLine -and $omitLine -match '^\+\d+ rows omitted \(\d+ (critical|watch|hygiene).*\) → 続報 @13:40Z$') "$omitLine"
Check 'omitted count matches' ($omitLine -match "^\+$(@($res.Omitted).Count) rows")
$sorted = @($res.Rows | Where-Object { $_.tier -gt 0 })
$shownN = $sorted.Count - @($res.Omitted).Count
Check 'omission is from the tail of attention order only' ((@($res.Omitted | ForEach-Object name) -join ',') -eq (@($sorted[$shownN..($sorted.Count - 1)] | ForEach-Object name) -join ','))
Check 'no file path in L1' (-not (($res.L1 -join "`n") -match 'project-liveness\.json|state[\\/]'))

Write-Host "R-12 follow-ups"
$chunks = Get-ProjectLivenessContinuationChunks -Omitted $res.Omitted -GeneratedAt $now
Check 'no omission -> no follow-up' (@(Get-ProjectLivenessContinuationChunks -Omitted @() -GeneratedAt $now).Count -eq 0)
$rowsBack = @($chunks | ForEach-Object { @($_ -split "`n" | Select-Object -Skip 1) })
Check 'follow-up rows == omitted rows, same order' (($rowsBack -join '|') -eq (@($res.Omitted | ForEach-Object minimal) -join '|'))
# force several chunks with a small budget
$small = Get-ProjectLivenessContinuationChunks -Omitted $res.Omitted -GeneratedAt $now -Budget 200
$allOk = $true; $k = 0
foreach ($c in $small) {
    $k++
    if ($c.Length -gt 200) { $allOk = $false }
    if (-not ($c -split "`n")[0].EndsWith("($k/$(@($small).Count))")) { $allOk = $false }
}
Check 'each chunk <= budget, (k/n) consecutive' ($allOk -and @($small).Count -gt 1) "n=$(@($small).Count)"
$rowsSmall = @($small | ForEach-Object { @($_ -split "`n" | Select-Object -Skip 1) })
Check 'rows never split across chunks' (($rowsSmall -join '|') -eq (@($res.Omitted | ForEach-Object minimal) -join '|'))
Check 'no file path in any follow-up' (-not (($small + $chunks) -join "`n" -match 'project-liveness\.json|state[\\/]|\.json'))
Check 'follow-up header stamp matches L1 stamp' ((($chunks[0] -split "`n")[0]) -like '続報 @13:40Z (1/*')

Write-Host "R-10 snapshot"
$snap = ConvertTo-ProjectLivenessSnapshot -Rows $res.Rows -GeneratedAt $now
Check 'generated_at matches the @stamp' ($snap.generated_at -eq '2026-11-08T13:40:00Z' -and $snap.generated_at.Substring(11, 5) + 'Z' -eq '13:40Z')
$snapNames = @($snap.rows | ForEach-Object { $_.name })
Check 'every omitted row is in the snapshot' (@($res.Omitted | Where-Object { $snapNames -notcontains $_.name }).Count -eq 0)
Check 'snapshot is unfolded (quiet rows included)' ($snapNames.Count -eq @($res.Rows).Count)

Write-Host "quiet fold"
$res = Get-ProjectLivenessDigestLines -Facts (Get-ProjectLivenessFacts -Threads @(T u a resolved (Ago 9)) -Projects @('e1', 'e2') -Now $now) -GeneratedAt $now
Check 'quiet line counts tier-0 projects' (@($res.L1) -contains 'quiet: 3 projects — EMPTY/DONE, no defects') ($res.L1 -join ' | ')
Check 'no omitted line when nothing cut' (-not (@($res.L1) | Where-Object { $_ -like '+*omitted*' }))

Write-Host "UNMEASURED (msg-9346 §2 / msg-9560 / R-13 msg-9562)"
# thread listing failed: named row, tier 2, no thread fact
$r = One @() @() @{} @() @() @() @('p')
Check 'threads unmeasured -> UNMEASURED tier 2, named' ($r.state -eq 'UNMEASURED' -and $r.tier -eq 2 -and $r.full -eq 'p ? · threads unmeasured') $r.full
$r = One @() @() @{} @() @() @('p') @('p')
Check 'threads and PRs unmeasured -> both named' ($r.full -eq 'p ? · threads unmeasured, PRs unmeasured') $r.full
# DONE-equivalent: only closed threads, PR count unknown -> sink, never DONE
$r = One @(T p a resolved (Ago 1)) @() @{} @() @() @('p')
Check 'DONE-equivalent + PRs unknown -> UNMEASURED tier 2' ($r.state -eq 'UNMEASURED' -and $r.tier -eq 2 -and $r.full -eq 'p ? · PRs unmeasured') $r.full
# EMPTY-equivalent (Einstein msg-9561's example): threads 0, sweep entry 0, PR count unknown
$r = One @() @() @{} @() @('p') @('p')
Check 'EMPTY-equivalent + PRs unknown -> UNMEASURED tier 2' ($r.state -eq 'UNMEASURED' -and $r.tier -eq 2 -and $r.full -eq 'p ? · PRs unmeasured') $r.full
# a PR-independent state is kept, with the marker after it
$r = One @(T p a parked (Ago 3)) @() @{} @() @() @('p')
Check 'PARKED + PRs unknown -> PARKED · PRs unmeasured' ($r.state -eq 'PARKED' -and $r.full -like 'p PARKED · PRs unmeasured · *') $r.full
# critical kept: a stalled project whose PR listing failed
$r = One @(T p a active (Ago 10)) @(S p a) @{ 'p/a' = 'Bohr' } @() @() @('p')
Check 'STALLED + PRs unknown -> STALLED · PRs unmeasured, tier 3' ($r.state -eq 'STALLED' -and $r.tier -eq 3 -and $r.full -like 'p STALLED · PRs unmeasured*' -and $r.full -notmatch '· PR \d') $r.full
# ... and when it is cut, it is counted as critical in the omitted breakdown and carried in the follow-up
$th = @(); $sw = @(); $nm = @{}
for ($i = 0; $i -lt 30; $i++) {
    $pn = "critical-project-{0:d2}" -f $i
    $th += T $pn a active (Ago (20 + $i)); $sw += S $pn a; $nm["$pn/a"] = 'Bohr'
}
$th += T zz-pr-unknown a active (Ago 4); $sw += S zz-pr-unknown a; $nm['zz-pr-unknown/a'] = 'Bohr'
$facts = Get-ProjectLivenessFacts -Threads $th -SweepEntries $sw -Nominations $nm -PrsUnmeasured @('zz-pr-unknown') -Now $now
$res = Get-ProjectLivenessDigestLines -Facts $facts -GeneratedAt $now
$cut = @($res.Omitted | Where-Object { $_.name -eq 'zz-pr-unknown' })
Check 'the PR-unknown critical row is omitted from the tail' ($cut.Count -eq 1 -and $cut[0].tier -eq 3) (@($res.Omitted | ForEach-Object name) -join ',')
$omitLine = @($res.L1) | Where-Object { $_ -like '+* rows omitted (*' }
Check '... and counted as critical in the omitted breakdown' ($omitLine -match "^\+$(@($res.Omitted).Count) rows omitted \($(@($res.Omitted).Count) critical\)") "$omitLine"
$chunks = Get-ProjectLivenessContinuationChunks -Omitted $res.Omitted -GeneratedAt $now
Check '... and its follow-up row carries the marker' ((($chunks -join "`n") -split "`n") -contains 'zz-pr-unknown STALLED · PRs unmeasured · stale 1/1') ($chunks -join ' | ')
Check 'L0 counts rows with an unmeasured fact' ($res.L0 -match '· unmeasured 1$') $res.L0

Write-Host "ages from ConvertFrom-Json-typed timestamps"
# ConvertFrom-Json turns an ISO string into a [datetime]; the age must not shift by the host offset.
$typed = ('{"t":"' + (Ago 2) + '"}' | ConvertFrom-Json).t
$a = ConvertTo-LivenessAge -Value $typed -Now $now
Check 'typed [datetime] age is 2 days' ([math]::Abs($a.days - 2.0) -lt 0.001) "$($a.days) ($($typed.GetType().Name) $($typed.Kind))"
$a = ConvertTo-LivenessAge -Value ([DateTimeOffset]::new($now.AddDays(-3), [TimeSpan]::Zero)) -Now $now
Check 'typed [DateTimeOffset] age is 3 days' ([math]::Abs($a.days - 3.0) -lt 0.001) "$($a.days)"

Write-Host "probe reports -> facts inputs (ConvertFrom-LivenessProbeReports)"
$listingJson = @'
{"liveness":{
  "pa":{"threads":[{"thread_id":"T-1","status":"active","last_activity_at":"2026-11-07T13:40:00+00:00"},
                   {"thread_id":"T-2","status":"resolved","last_activity_at":null}],"error":null},
  "pb":{"threads":null,"error":"chatroom_list_threads failed: boom"},
  "pc":{"threads":[],"error":null}}}
'@ | ConvertFrom-Json
$prJson = '{"projects":{"pa":{"prs":[{"number":5,"created_at":"2026-11-01T00:00:00+00:00"}],"error":null},"pc":{"prs":null,"error":"http_error: 502"}}}' | ConvertFrom-Json
$hsJson = '{"T-1":{"last_observed_nomination":"human"},"T-9":{"last_observed_nomination":"bohr"}}' | ConvertFrom-Json
$in = ConvertFrom-LivenessProbeReports -ListingReport $listingJson -PrReport $prJson -SweepEntries @((S pa T-1), (S pb T-3)) -HeadSkip $hsJson
Check 'a listed project with threads and PRs is measured' (@($in.Projects) -contains 'pa' -and @($in.ThreadsUnmeasured) -notcontains 'pa' -and @($in.PrsUnmeasured) -notcontains 'pa') (@($in.Projects) -join ',')
Check 'a failed listing is threads-unmeasured and still enumerated (a named row)' (@($in.ThreadsUnmeasured) -contains 'pb' -and @($in.Projects) -contains 'pb' -and $in.Unmeasured['pb'] -match 'boom')
Check 'a failed PR listing is PRs-unmeasured, never PR 0' (@($in.PrsUnmeasured) -contains 'pc' -and @($in.ThreadsUnmeasured) -notcontains 'pc' -and $in.Unmeasured['pc'] -match 'http_error')
Check 'every status is carried (rot needs the closed set)' (@($in.Threads | Where-Object { $_.status -eq 'resolved' }).Count -eq 1)
Check 'nominations are looked up by thread_id and keyed project/thread_id' ($in.Nominations['pa/T-1'] -eq 'human' -and $in.Nominations.Count -eq 1)
Check 'sweep entries of a threads-unmeasured project are dropped' (@($in.SweepEntries | Where-Object { $_.project -eq 'pb' }).Count -eq 0)
Check 'open PRs carry their project' (@($in.OpenPrs).Count -eq 1 -and $in.OpenPrs[0].project -eq 'pa')
$f = Get-ProjectLivenessFacts -Threads $in.Threads -SweepEntries $in.SweepEntries -Nominations $in.Nominations -OpenPrs $in.OpenPrs -Projects $in.Projects `
    -ThreadsUnmeasured $in.ThreadsUnmeasured -PrsUnmeasured $in.PrsUnmeasured -Now $now
$res = Get-ProjectLivenessDigestLines -Facts $f -GeneratedAt $now
$stateOf = { param($n) (@($res.Rows) | Where-Object { $_.name -eq $n }).state }
$diag = (($res.Rows | ForEach-Object { "$($_.name)=$($_.state)" }) -join ',')
Check 'the joined facts render: pa is HUMAN' ((& $stateOf 'pa') -eq 'HUMAN') $diag
Check 'pb (listing failed) is an UNMEASURED row' ((& $stateOf 'pb') -eq 'UNMEASURED') $diag
Check 'pc (no threads, PR listing failed) is UNMEASURED, not EMPTY' ((& $stateOf 'pc') -eq 'UNMEASURED') $diag
Check 'L0 counts the unmeasured' ($res.L0 -match 'unmeasured 2') $res.L0
$whole = ConvertFrom-LivenessProbeReports -ListingError 'timed out' -PrReport $prJson -SweepEntries @((S pa T-1))
Check 'a whole listing failure makes every project threads-unmeasured' ((@($whole.ThreadsUnmeasured) -join ',') -eq 'pa,pc' -and $whole.Unmeasured['pa'] -match 'timed out' -and @($whole.Threads).Count -eq 0)
$noPr = ConvertFrom-LivenessProbeReports -ListingReport $listingJson -PrReport ('{"projects":{}}' | ConvertFrom-Json)
Check 'a project with no clones entry is PRs-unmeasured, not PR 0 (msg-9560 Obj-12-1)' (@($noPr.NoPrSource) -contains 'pa' -and @($noPr.PrsUnmeasured) -contains 'pa' -and $noPr.Unmeasured['pa'] -match 'no clones entry')
# A measured empty list is measured: `[]` threads / `[]` PRs must not read as `null` (a scriptblock
# getter unrolls an empty array to $null — found while wiring R-13).
$emptyIn = ConvertFrom-LivenessProbeReports -ListingReport ('{"liveness":{"pe":{"threads":[],"error":null}}}' | ConvertFrom-Json) `
    -PrReport ('{"projects":{"pe":{"prs":[],"error":null}}}' | ConvertFrom-Json)
Check 'a measured empty listing and empty PR list are measured (EMPTY is reachable)' (@($emptyIn.ThreadsUnmeasured).Count -eq 0 -and @($emptyIn.PrsUnmeasured).Count -eq 0) (($emptyIn.Unmeasured.Keys | ForEach-Object { "$_=$($emptyIn.Unmeasured[$_])" }) -join '; ')
$fe = Get-ProjectLivenessFacts -Threads $emptyIn.Threads -Projects $emptyIn.Projects -ThreadsUnmeasured $emptyIn.ThreadsUnmeasured -PrsUnmeasured $emptyIn.PrsUnmeasured -Now $now
Check '... and renders EMPTY' ((Get-ProjectLivenessRow -Facts $fe[0]).state -eq 'EMPTY')
$wholePr = ConvertFrom-LivenessProbeReports -ListingReport $listingJson -PrError 'timed out'
Check 'a whole PR-probe failure makes every project PRs-unmeasured' (@($wholePr.PrsUnmeasured).Count -eq 3 -and @($wholePr.OpenPrs).Count -eq 0)

if ($script:failures -gt 0) { Write-Host "$script:failures failure(s)"; exit 1 }
Write-Host "all passed"
