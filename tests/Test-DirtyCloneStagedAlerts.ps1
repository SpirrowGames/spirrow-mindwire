# Regression guard for the staged re-alerts of deploy/run-conductor-scheduled.ps1.
#
# T-dirty-clone-wip-retreat-and-silent-stall-alerts PR-2 (D-2): design v2.0 (Bohr msg-8091) as
# revised by v2.1 (Bohr msg-8093), naysayer go Einstein on the reply to msg-8093 (objections []).
#   * `__dirty_clone__/<repo>` alerts carry `|stage=<s>`: 0 / 1h / 6h / 24h, nothing after 24h;
#   * the body always names the repo_dir, how long it has been parked, the parked candidates, and
#     `cause: unknown`; a retreat record for the same repo is one line marked as not the cause;
#   * `__launch_wait_starved__` alerts on the sorted `<thread key>@<stage>` list (6h / 24h) and the
#     key is cleared when nothing is starved;
#   * replay of 10/29: 9.5 hours of parking alerts exactly three times (0, 1h, 6h).
#
# Functions are lifted out of the sweep script's AST (dot-sourcing would run the sweep); the wiring
# into the real loop is checked on the AST.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }

$script:logLines = @()
function Write-Log { param([string]$Message) $script:logLines += $Message }
$script:sent = @()
function Send-Notification { param([string]$Message) $script:sent += $Message; return @{ status = 'sent' } }

$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'ConvertTo-DirtyCloneRepoKey', 'Test-DirtyCloneSkip', 'Get-DirtyCloneNotice',
                  'Test-NotificationSuppressed', 'Send-NotificationIfChanged', 'ConvertTo-DirtyCloneParking',
                  'Set-DirtyCloneParked', 'Update-DirtyCloneParking', 'Format-DurationDigest',
                  'ConvertFrom-AlertIso', 'Get-DirtyCloneAlertStage', 'Get-LaunchWaitAlertStage',
                  'Get-CandidateCountsByRepo', 'Get-LatestRetreatRecordForRepo', 'Read-LatestRetreatRecordsByRepo', 'Find-RetreatRecordForRepo', 'Format-RetreatRecordAlertLine',
                  'Get-DirtyCloneStagedAlert', 'Get-LaunchWaitStarvedAlert', 'Update-LaunchWaitStarvedAlert') {
    $fn = $functions | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}
$assign = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $n.Left.Extent.Text -eq '$DirtyCloneExitCode' }, $true) | Select-Object -First 1
$DirtyCloneExitCode = [int]$assign.Right.Extent.Text

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual) }
}

function New-Payload {
    param([string]$Reason, [string]$Head)
    $obj = [ordered]@{ repo_dir = 'x'; reason = $Reason; head = $Head; porcelain = @(); detail = "d-$Reason" }
    return 'MINDWIRE_DIRTY_CLONE_PAYLOAD ' + ($obj | ConvertTo-Json -Compress)
}
function At { param([string]$Iso) return (ConvertFrom-AlertIso $Iso) }

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("mw-staged-alerts-" + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $tmp
$repoA = Join-Path $tmp 'clone-A'
$repoB = Join-Path $tmp 'clone-B'
$keyA = ConvertTo-DirtyCloneRepoKey $repoA
$dirty = @{ code = 8; output = @(New-Payload -Reason 'dirty_tree' -Head 'main'); stderr = '' }
$clean = @{ code = 0; output = @(); stderr = '' }

# One tick of the real order: re-judge the parked repo (Update-DirtyCloneParking), or, when nothing is
# parked, a launch that the daemon refuses with exit 8 (the exit-8 branch of the sweep).
function Invoke-Tick {
    param([hashtable]$Parked, [hashtable]$Notify, [hashtable]$Result, [string]$Now, [string]$RecordsPath = '')
    if ($Parked.Count -gt 0) {
        $probeResult = $Result
        $null = Update-DirtyCloneParking -Parked $Parked -NotifyState $Notify -Probe ({ param($d) $probeResult }.GetNewClosure()) `
            -NowIso $Now -ParkedCounts @{ $keyA = 3 } -RecordsPath $RecordsPath
        return
    }
    if ($Result.code -ne $DirtyCloneExitCode) { return }
    $notice = Get-DirtyCloneNotice -RepoDir $repoA -CandidateKey 'p::T-1' -Output $Result.output -ExitCode 8
    Set-DirtyCloneParked -Parked $Parked -RepoDir $repoA -Notice $notice -NowIso $Now
    $s = Get-DirtyCloneStagedAlert -Signature $notice.signature -Message $notice.message -RepoDir $repoA `
        -SinceIso "$($Parked[$notice.repo_key].since)" -Now (At $Now) -ParkedCount 3 -RecordsPath $RecordsPath
    Send-NotificationIfChanged -State $Notify -Key $notice.key -Signature $s.signature -Message $s.message
}

try {
    # --- stage helpers ------------------------------------------------------------------------------
    Write-Host 'stages'
    Check 'dirty stage 59m is 0' '0' (Get-DirtyCloneAlertStage -Age ([TimeSpan]::FromMinutes(59)))
    Check 'dirty stage 1h is 1h' '1h' (Get-DirtyCloneAlertStage -Age ([TimeSpan]::FromHours(1)))
    Check 'dirty stage 6h is 6h' '6h' (Get-DirtyCloneAlertStage -Age ([TimeSpan]::FromHours(6)))
    Check 'dirty stage 24h is 24h' '24h' (Get-DirtyCloneAlertStage -Age ([TimeSpan]::FromHours(24)))
    Check 'dirty stage 72h is still 24h (no stage after 24h)' '24h' (Get-DirtyCloneAlertStage -Age ([TimeSpan]::FromHours(72)))
    Check 'launch-wait stage 6h' '6h' (Get-LaunchWaitAlertStage -Age ([TimeSpan]::FromHours(6)))
    Check 'launch-wait stage 23h is 6h' '6h' (Get-LaunchWaitAlertStage -Age ([TimeSpan]::FromHours(23)))
    Check 'launch-wait stage 48h is still 24h' '24h' (Get-LaunchWaitAlertStage -Age ([TimeSpan]::FromHours(48)))

    # --- dirty clone: alerts at 0 / 1h / 6h / 24h, nothing at 48h / 72h -----------------------------
    Write-Host ''
    Write-Host 'dirty clone — staged alerts'
    $parked = @{}; $notify = @{}; $script:sent = @()
    $steps = @(
        @('2026-10-29T04:21:00Z', 1, 'exit 8 at 0 alerts'),
        @('2026-10-29T04:22:00Z', 1, 'same stage (0) a minute later sends nothing'),
        @('2026-10-29T05:20:59Z', 1, '59m59s is still stage 0'),
        @('2026-10-29T05:21:00Z', 2, '1h alerts'),
        @('2026-10-29T05:40:00Z', 2, 'same stage (1h) sends nothing'),
        @('2026-10-29T10:21:00Z', 3, '6h alerts'),
        @('2026-10-29T12:00:00Z', 3, 'same stage (6h) sends nothing'),
        @('2026-10-30T04:21:00Z', 4, '24h alerts'),
        @('2026-10-31T04:21:00Z', 4, '48h sends nothing'),
        @('2026-11-01T04:21:00Z', 4, '72h sends nothing')
    )
    foreach ($s in $steps) {
        Invoke-Tick -Parked $parked -Notify $notify -Result $dirty -Now $s[0]
        Check $s[2] $s[1] $script:sent.Count
    }
    Check 'the stage is in the stored signature' 'dirty-clone:dirty_tree:main|stage=24h' $notify["__dirty_clone__/$keyA"]
    Check 'the 1h alert body says stage=1h' $true ($script:sent[1] -like '*stage=1h*')
    Check 'the 1h alert body names how long it has been parked' $true ($script:sent[1] -like '*parked for: 1h (since*')
    Check 'since never moves while parked' $true ("$($parked[$keyA].since)" -like '2026-10-29T04:21:00*')

    Write-Host ''
    Write-Host 'dirty clone — release, then the same fault alerts again'
    Invoke-Tick -Parked $parked -Notify $notify -Result $clean -Now '2026-11-01T05:00:00Z'
    Check 'released' $false $parked.ContainsKey($keyA)
    Check 'release forgets the alert key' $false $notify.ContainsKey("__dirty_clone__/$keyA")
    Invoke-Tick -Parked $parked -Notify $notify -Result $dirty -Now '2026-11-01T05:01:00Z'
    Check 'the same fault after a release alerts again (stage 0)' 5 $script:sent.Count
    Check 'a fresh episode starts at stage 0' 'dirty-clone:dirty_tree:main|stage=0' $notify["__dirty_clone__/$keyA"]

    Write-Host ''
    Write-Host 'dirty clone — probe-failed is staged too'
    $parked = @{}; $notify = @{}; $script:sent = @()
    Invoke-Tick -Parked $parked -Notify $notify -Result $dirty -Now '2026-10-29T04:21:00Z'
    $pf = @{ code = 1; output = @(); stderr = 'clone-check: boom' }
    Invoke-Tick -Parked $parked -Notify $notify -Result $pf -Now '2026-10-29T04:30:00Z'
    Invoke-Tick -Parked $parked -Notify $notify -Result $pf -Now '2026-10-29T04:40:00Z'
    Check 'probe-failed: one alert at stage 0' 2 $script:sent.Count
    Invoke-Tick -Parked $parked -Notify $notify -Result $pf -Now '2026-10-29T05:30:00Z'
    Check 'probe-failed: another at 1h' 3 $script:sent.Count
    Check 'probe-failed: signature is staged' 'dirty-clone:probe-failed:1|stage=1h' $notify["__dirty_clone__/$keyA"]
    Check 'probe-failed: body has the context lines' $true (($script:sent[-1] -like '*cause: unknown*') -and ($script:sent[-1] -like "*repo_dir: $repoA*"))

    # --- 10/29 replay: 13:21 -> 22:47 JST, a tick a minute ----------------------------------------
    Write-Host ''
    Write-Host 'replay of 10/29 — 9.5 hours parked, one tick a minute'
    $parked = @{}; $notify = @{}; $script:sent = @()
    $start = At '2026-10-29T04:21:00Z'   # 13:21 JST
    $end = At '2026-10-29T13:47:00Z'     # 22:47 JST
    $ticks = 0
    for ($t = $start; $t -le $end; $t = $t.AddMinutes(1)) {
        Invoke-Tick -Parked $parked -Notify $notify -Result $dirty -Now $t.ToString('o')
        $ticks++
    }
    Check 'replay: hundreds of ticks ran' $true ($ticks -gt 500)
    Check 'replay: exactly three alerts (0, 1h, 6h)' 3 $script:sent.Count
    Check 'replay: the third alert is the 6h stage' $true ($script:sent[2] -like '*stage=6h*')

    # --- alert text -------------------------------------------------------------------------------
    Write-Host ''
    Write-Host 'alert text'
    $records = Join-Path $tmp 'clone-retreats.jsonl'
    $base = Get-DirtyCloneNotice -RepoDir $repoA -CandidateKey 'p::T-1' -Output $dirty.output -ExitCode 8
    $now = At '2026-10-29T06:00:00Z'

    # (a) no record at all
    $a = Get-DirtyCloneStagedAlert -Signature $base.signature -Message $base.message -RepoDir $repoA `
        -SinceIso '2026-10-29T04:21:00Z' -Now $now -ParkedCount 3 -RecordsPath $records
    Check '(a) no record: cause: unknown' $true ($a.message -like '*cause: unknown*')
    Check '(a) no record: no record line' $false ($a.message -like '*latest retreat record*')
    Check '(a) names the repo_dir' $true ($a.message -like "*repo_dir: $repoA*")
    Check '(a) names the parked duration' $true ($a.message -like '*parked for: 1h 39m*')
    Check '(a) names the parked candidates' $true ($a.message -like '*parked candidates: 3*')

    # (b) Einstein's counterexample: thread A's retreat succeeded earlier, new dirt in the same repo.
    $recA = [ordered]@{ kind = 'retreat'; record_id = 'T-A@x#1'; at = '2026-10-29T03:00:00+00:00'; thread_id = 'T-A'
        repo_dir = $repoA; head_msg = 'msg-1'; stop_reason = 'turn_timeout'; outcome = 'retreated'; pushed = $true
        branch = 'wip/T-A-20261029T030000Z'; files = 2; insertions = 10; deletions = 1; patch = $null
        untracked = $null; marker = $null; detail = '' }
    $recOther = [ordered]@{ kind = 'retreat'; record_id = 'T-Z@x#1'; at = '2026-10-29T03:30:00+00:00'; thread_id = 'T-Z'
        repo_dir = $repoB; head_msg = 'msg-9'; stop_reason = 'x'; outcome = 'retreated'; pushed = $true
        branch = 'wip/T-Z-1'; files = 1; insertions = 1; deletions = 0; patch = $null; untracked = $null; marker = $null; detail = '' }
    $delivered = [ordered]@{ kind = 'delivered'; record_id = 'T-A@x#1'; thread_id = 'T-A'; at = '2026-10-29T03:10:00+00:00' }
    $lines = @(($recA | ConvertTo-Json -Compress), '{torn', ($delivered | ConvertTo-Json -Compress), ($recOther | ConvertTo-Json -Compress))
    [System.IO.File]::WriteAllText($records, (($lines -join "`n") + "`n"), [System.Text.UTF8Encoding]::new($false))
    $b = Get-DirtyCloneStagedAlert -Signature $base.signature -Message $base.message -RepoDir $repoA `
        -SinceIso '2026-10-29T04:21:00Z' -Now $now -ParkedCount 3 -RecordsPath $records
    $bLines = @($b.message -split "`n")
    $recLine = @($bLines | Where-Object { $_ -like 'latest retreat record for this repo (not confirmed as the cause of this parking):*' })
    Check '(b) cause: unknown' $true ($b.message -like '*cause: unknown*')
    Check '(b) exactly one record line' 1 $recLine.Count
    Check '(b) the record line names thread A and its branch' $true (($recLine[0] -like '*thread=T-A *') -and ($recLine[0] -like '*branch=wip/T-A-20261029T030000Z*'))
    Check '(b) the record line carries at / stop / outcome' $true (($recLine[0] -like '*at=2026-10-29T03:00:00*') -and ($recLine[0] -like '*stop=turn_timeout*') -and ($recLine[0] -like '*outcome=retreated*'))
    Check '(b) thread A appears on no other line' 0 @($bLines | Where-Object { $_ -like '*T-A*' -and $_ -notlike 'latest retreat record*' }).Count
    Check '(b) a record of another repo is not used' $false ($b.message -like '*T-Z*')
    Check '(b) the record does not change the signature' "$($base.signature)|stage=1h" $b.signature

    # (c) an existing failed record: same layout, branch shown
    $recF = [ordered]@{ kind = 'retreat'; record_id = 'T-F@x#1'; at = '2026-10-29T04:20:00+00:00'; thread_id = 'T-F'
        repo_dir = ($repoA.ToUpperInvariant() + '\'); head_msg = 'msg-2'; stop_reason = 'no_handoff_to_human'; outcome = 'failed'
        pushed = $false; branch = 'wip/T-F-20261029T042000Z'; files = 4; insertions = 324; deletions = 30; patch = $null
        untracked = $null; marker = $null; detail = 'could not switch back' }
    Add-Content -LiteralPath $records -Value ($recF | ConvertTo-Json -Compress) -Encoding UTF8
    $c = Get-DirtyCloneStagedAlert -Signature $base.signature -Message $base.message -RepoDir $repoA `
        -SinceIso '2026-10-29T04:21:00Z' -Now $now -ParkedCount 3 -RecordsPath $records
    $cRec = @(@($c.message -split "`n") | Where-Object { $_ -like 'latest retreat record for this repo (not confirmed as the cause*' })
    Check '(c) cause: unknown' $true ($c.message -like '*cause: unknown*')
    Check '(c) the newest record for the repo is used (repo key ignores case / trailing separator)' $true ($cRec[0] -like '*thread=T-F *')
    Check '(c) the failed record line shows its branch' $true ($cRec[0] -like '*outcome=failed*branch=wip/T-F-20261029T042000Z*')
    Check '(c) the older record is not shown' $false ($c.message -like '*T-A*')

    # discarded record: the patch path is shown (msg-7863 §1: the discard goes into the D-2 alert text)
    $recD = [ordered]@{ kind = 'retreat'; record_id = 'T-D@x#1'; at = '2026-10-29T05:00:00+00:00'; thread_id = 'T-D'
        repo_dir = $repoA; head_msg = 'msg-3'; stop_reason = 'turn_timeout'; outcome = 'discard_incomplete'; pushed = $false
        branch = $null; files = 0; insertions = 0; deletions = 0; patch = 'S/clone-a-discarded-T-D-x.patch'
        untracked = 'S/clone-a-discarded-T-D-x.untracked.txt'; marker = 'BISECT_START'; detail = '' }
    Add-Content -LiteralPath $records -Value ($recD | ConvertTo-Json -Compress) -Encoding UTF8
    $d = Get-DirtyCloneStagedAlert -Signature $base.signature -Message $base.message -RepoDir $repoA `
        -SinceIso '2026-10-29T04:21:00Z' -Now $now -ParkedCount 3 -RecordsPath $records
    Check 'discard record: the patch path is on the record line' $true ($d.message -like '*outcome=discard_incomplete*patch=S/clone-a-discarded-T-D-x.patch*')
    Check 'discard record: still cause: unknown' $true ($d.message -like '*cause: unknown*')

    $m = Get-DirtyCloneStagedAlert -Signature 's' -Message 'm' -RepoDir $repoA -SinceIso '' -Now $now -ParkedCount 0 `
        -RecordsPath (Join-Path $tmp 'missing.jsonl')
    Check 'missing records file and unparseable since: stage 0, no record line' $true (($m.signature -eq 's|stage=0') -and ($m.message -notlike '*latest retreat record*'))

    # one read per tick (PR-2 gate advisory): a table read once gives the same alert as the file path
    $table = Read-LatestRetreatRecordsByRepo -RecordsPath $records
    Check 'records table: one entry per repo' 2 $table.Count
    $dT = Get-DirtyCloneStagedAlert -Signature $base.signature -Message $base.message -RepoDir $repoA `
        -SinceIso '2026-10-29T04:21:00Z' -Now $now -ParkedCount 3 -RecordsPath (Join-Path $tmp 'missing.jsonl') -RecordsByRepo $table
    Check 'records table: same alert text as reading the file (newest record of the repo)' $d.message $dT.message
    $eB = Get-DirtyCloneStagedAlert -Signature 's' -Message 'm' -RepoDir $repoB -SinceIso '' -Now $now -ParkedCount 0 `
        -RecordsPath '' -RecordsByRepo $table
    Check 'records table: the other repo gets its own record' $true ($eB.message -like '*thread=T-Z *')
    $eN = Get-DirtyCloneStagedAlert -Signature 's' -Message 'm' -RepoDir $repoA -SinceIso '' -Now $now -ParkedCount 0 `
        -RecordsPath $records -RecordsByRepo @{}
    Check 'records table: an empty table means no record line (the file is not re-read)' $false ($eN.message -like '*latest retreat record*')
    Check 'records table: a missing file gives an empty table' 0 (Read-LatestRetreatRecordsByRepo -RecordsPath (Join-Path $tmp 'missing.jsonl')).Count

    # --- parked candidate counts ---------------------------------------------------------------------
    $counts = Get-CandidateCountsByRepo -Candidates @(
        @{ key = 'p::T-1'; repo_dir = $repoA }, @{ key = 'p::T-2'; repo_dir = ($repoA.ToUpperInvariant() + '\') },
        @{ key = 'q::T-3'; repo_dir = $repoB }, @{ key = 'q::T-4'; repo_dir = '' })
    Check 'candidate counts: same repo in another spelling counts once more' 2 $counts[$keyA]
    Check 'candidate counts: other repo' 1 $counts[(ConvertTo-DirtyCloneRepoKey $repoB)]

    # --- launch-wait starved ------------------------------------------------------------------------
    Write-Host ''
    Write-Host 'launch-wait starved'
    $notify = @{}; $script:sent = @()
    $parkedLW = @{ $keyA = @{ repo_dir = $repoA; reason = 'dirty_tree'; head = 'main'; detail = ''; since = 's'; signature = 'x' } }
    $dirs = @{ 'p::T-1' = $repoA; 'q::T-2' = $repoB }
    function Starved { param([double]$H1, [double]$H2 = -1)
        $o = @([PSCustomObject]@{ key = 'p::T-1'; age = [TimeSpan]::FromHours($H1) })
        if ($H2 -ge 0) { $o += [PSCustomObject]@{ key = 'q::T-2'; age = [TimeSpan]::FromHours($H2) } }
        return , $o
    }
    $r = Update-LaunchWaitStarvedAlert -NotifyState $notify -Starved (Starved 6) -Parked $parkedLW -RepoDirByKey $dirs
    Check 'launch-wait: alert at 6h' 1 $script:sent.Count
    Check 'launch-wait: signature is the sorted key@stage list' 'launch-wait-starved:p::T-1@6h' $notify['__launch_wait_starved__']
    Check 'launch-wait: a parked repo is named as the block' $true ($script:sent[0] -like "*p::T-1*blocked by: parked clone $repoA*")
    $null = Update-LaunchWaitStarvedAlert -NotifyState $notify -Starved (Starved 7) -Parked $parkedLW -RepoDirByKey $dirs
    Check 'launch-wait: unchanged tick sends nothing' 1 $script:sent.Count
    $null = Update-LaunchWaitStarvedAlert -NotifyState $notify -Starved (Starved 24) -Parked $parkedLW -RepoDirByKey $dirs
    Check 'launch-wait: alert at 24h' 2 $script:sent.Count
    $null = Update-LaunchWaitStarvedAlert -NotifyState $notify -Starved (Starved 48) -Parked $parkedLW -RepoDirByKey $dirs
    Check 'launch-wait: nothing at 48h' 2 $script:sent.Count
    $null = Update-LaunchWaitStarvedAlert -NotifyState $notify -Starved (Starved 49 6) -Parked $parkedLW -RepoDirByKey $dirs
    Check 'launch-wait: a new thread alerts' 3 $script:sent.Count
    Check 'launch-wait: two-thread signature sorted' 'launch-wait-starved:p::T-1@24h,q::T-2@6h' $notify['__launch_wait_starved__']
    Check 'launch-wait: a thread on an unparked repo says cause unknown' $true ($script:sent[2] -like '*q::T-2*blocked by: not parked (cause unknown)*')
    $r = Update-LaunchWaitStarvedAlert -NotifyState $notify -Starved @() -Parked $parkedLW -RepoDirByKey $dirs
    Check 'launch-wait: an empty list clears the key' $false $notify.ContainsKey('__launch_wait_starved__')
    Check 'launch-wait: clearing reports a state change' $true $r
    Check 'launch-wait: clearing sends nothing' 3 $script:sent.Count
    $r = Update-LaunchWaitStarvedAlert -NotifyState $notify -Starved @() -Parked $parkedLW -RepoDirByKey $dirs
    Check 'launch-wait: empty again is no change' $false $r
    $null = Update-LaunchWaitStarvedAlert -NotifyState $notify -Starved (Starved 6) -Parked $parkedLW -RepoDirByKey $dirs
    Check 'launch-wait: the same starvation after it emptied alerts again' 4 $script:sent.Count
    $null = Update-LaunchWaitStarvedAlert -NotifyState $notify -Starved (Starved 6) -Parked @{} -RepoDirByKey @{}
    Check 'launch-wait: unknown repo and no parking still alert-safe (signature unchanged, nothing sent)' 4 $script:sent.Count

    # --- the real loop wiring (AST) -----------------------------------------------------------------
    Write-Host ''
    Write-Host 'wiring in the real sweep'
    $scriptText = $ast.Extent.Text
    $upd = $functions | Where-Object { $_.Name -eq 'Update-DirtyCloneParking' } | Select-Object -First 1
    $updSends = [regex]::Matches($upd.Extent.Text, 'Send-NotificationIfChanged[^\r\n]*')
    Check 'Update-DirtyCloneParking sends on two paths' 2 $updSends.Count
    Check 'every send in Update-DirtyCloneParking uses the staged signature' 2 @($updSends | Where-Object { $_.Value -match '-Signature \$staged\.signature -Message \$staged\.message' }).Count
    $updLoop = $upd.Body.FindAll({ param($n) $n -is [System.Management.Automation.Language.ForEachStatementAst] }, $true) | Select-Object -First 1
    Check 'Update-DirtyCloneParking reads the records file once, before its loop' 1 ([regex]::Matches($upd.Extent.Text, 'Read-LatestRetreatRecordsByRepo')).Count
    Check 'the records file is not read inside the loop' $false ($updLoop.Body.Extent.Text -match 'Read-LatestRetreatRecordsByRepo|Get-LatestRetreatRecordForRepo')
    Check 'both alert paths pass the table read once' 2 ([regex]::Matches($updLoop.Body.Extent.Text, '-RecordsByRepo \$recordsByRepo')).Count
    $loop = $ast.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.ForEachStatementAst] -and
            $n.Variable.Extent.Text -eq '$cand' -and $n.Body.Extent.Text -match 'Invoke-HeadSkipCommitLaunch' }, $true) |
        Select-Object -First 1
    $branch = $loop.Body.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.IfStatementAst] -and
            $n.Clauses[0].Item1.Extent.Text -eq '$code -eq $DirtyCloneExitCode' }, $true) | Select-Object -First 1
    $branchText = $branch.Clauses[0].Item2.Extent.Text
    Check 'exit-8 branch stages its alert' $true ($branchText -match 'Get-DirtyCloneStagedAlert')
    Check 'exit-8 branch sends the staged signature' $true ($branchText -match '-Signature \$stagedDirty\.signature -Message \$stagedDirty\.message')
    Check 'exit-8 branch reads the retreat records' $true ($branchText -match '-RecordsPath \$cloneRetreatRecordsPath')
    # PR-3 (msg-8104 advisory): the exit-8 path passes the per-tick table instead of re-reading the file.
    Check 'exit-8 branch passes the per-tick table (-RecordsByRepo)' $true ($branchText -match '-RecordsByRepo \$recordsByRepoTick')
    # PR-3 gate advisory (#530 @ f25978f): the caller-side match above cannot see the callee, so check the
    # binding itself — every named parameter the exit-8 call passes is declared by Get-DirtyCloneStagedAlert
    # (an undeclared one would throw ParameterBindingException at run time).
    $stagedFn = $functions | Where-Object { $_.Name -eq 'Get-DirtyCloneStagedAlert' } | Select-Object -First 1
    $declared = @($stagedFn.Body.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
    $exit8Call = $branch.Clauses[0].Item2.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Get-DirtyCloneStagedAlert' }, $true) |
        Select-Object -First 1
    $passed = @($exit8Call.CommandElements | Where-Object { $_ -is [System.Management.Automation.Language.CommandParameterAst] } |
        ForEach-Object { $_.ParameterName })
    Check 'exit-8 call passes -RecordsByRepo (from the call AST)' $true ($passed -contains 'RecordsByRepo')
    Check 'every parameter of the exit-8 call is declared by Get-DirtyCloneStagedAlert' '' (($passed | Where-Object { $_ -notin $declared }) -join ',')
    Check 'exit-8 branch reads the table only when it is not yet read' $true ($branchText -match 'if \(\$null -eq \$recordsByRepoTick\) \{ \$recordsByRepoTick = Read-LatestRetreatRecordsByRepo')
    Check 'a launch that may have written a record clears the table' $true ($loop.Body.Extent.Text -match "if \(`"\`$retreatOutcome`" -notin @\('skipped', 'clean', 'not_eligible'\)\) \{ \`$recordsByRepoTick = \`$null \}")
    Check 'the table starts empty before the candidate loop' $true ($scriptText.IndexOf('$recordsByRepoTick = $null') -ge 0 -and $scriptText.IndexOf('$recordsByRepoTick = $null') -lt $loop.Extent.StartOffset)
    Check 're-judge passes counts and records' $true ($scriptText -match '-ParkedCounts \$candidateCountsByRepo -RecordsPath \$cloneRetreatRecordsPath')
    $posCounts = $scriptText.IndexOf('$candidateCountsByRepo = Get-CandidateCountsByRepo -Candidates $candidates')
    $posUpdate = $scriptText.IndexOf('Update-DirtyCloneParking -Parked $dirtyClonesParked')
    Check 'counts are computed before the re-judge' $true ($posCounts -ge 0 -and $posCounts -lt $posUpdate)
    Check 'records path is state\clone-retreats.jsonl (PR-1b CLONE_RETREAT_LOG_FILENAME)' $true ($scriptText -match '\$cloneRetreatRecordsPath = Join-Path \$dataDir "state\\clone-retreats\.jsonl"')
    $posStarved = $scriptText.IndexOf('$launchWaitStarved = Get-LaunchWaitStarved')
    $posLW = $scriptText.IndexOf('Update-LaunchWaitStarvedAlert -NotifyState $notifyState -Starved $launchWaitStarved')
    $posFinalSave = $scriptText.LastIndexOf('Save-JsonState -Path $notifyStatePath -State $notifyState')
    Check 'launch-wait alert follows the starvation computation' $true ($posStarved -ge 0 -and $posLW -gt $posStarved)
    Check 'launch-wait alert precedes the final notified.json save' $true ($posLW -ge 0 -and $posLW -lt $posFinalSave)
    Check 'the alerts never post into a thread' $false ((($functions | Where-Object { $_.Name -in 'Get-DirtyCloneStagedAlert', 'Update-LaunchWaitStarvedAlert', 'Get-LaunchWaitStarvedAlert' }) | ForEach-Object { $_.Extent.Text }) -join "`n" -match 'chatroom')
}
finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) FAILED"; exit 1 }
Write-Host "all staged-alert checks passed"
