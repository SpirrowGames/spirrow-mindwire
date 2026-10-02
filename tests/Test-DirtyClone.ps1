# Regression guard for the sweep's dirty-shared-clone branch (deploy/run-conductor-scheduled.ps1).
#
# T-timed-out-implementer-turn-leaves-dirty-shared-clone, design v4 D-2 (Bohr msg-5781, endorsed by
# Einstein msg-5782). When mindwire-loop exits $DirtyCloneExitCode the wrapper must:
#   * NOT quarantine and NOT put the thread on retry-pending (the clone is at fault, not the thread);
#   * skip the rest of this tick's candidates on the SAME repo_dir (case / separator insensitive);
#   * keep launching candidates on OTHER repos (Einstein msg-5778 #1);
#   * notify on the per-repo key __dirty_clone__/<repo>, even when the payload row is unreadable,
#     and never on a GitHub credential key (Einstein msg-5776 #2).
# T-clone-guard-pin-ignored-only-in-mindwire, design v2.1 D-3 (Bohr msg-6162 / msg-6164): exit 8
# also reverts the LAUNCH commit (never STALLED), parks the repo, and re-judges it with one
# `mindwire clone-check` per tick until it is clean (see the second half of this file).
#
# Functions are lifted out of the sweep script's AST (dot-sourcing would run the sweep), and the
# loop's ORDERING — which a lifted function cannot show — is checked on the AST of the loop body.

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
                  'Test-NotificationSuppressed', 'Send-NotificationIfChanged') {
    $fn = $functions | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual) }
}

# --- exit code constant ---------------------------------------------------------------------------
$assign = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $n.Left.Extent.Text -eq '$DirtyCloneExitCode' }, $true) | Select-Object -First 1
Check 'DirtyCloneExitCode is assigned at script level' $true ($null -ne $assign)
$DirtyCloneExitCode = [int]$assign.Right.Extent.Text
Check 'DirtyCloneExitCode is 8 (not 5/6/7: wall-clock budget codes)' 8 $DirtyCloneExitCode

# --- repo key normalisation -------------------------------------------------------------------------
$base = Join-Path ([System.IO.Path]::GetTempPath()) 'MindWire-Clone'
$k1 = ConvertTo-DirtyCloneRepoKey -RepoDir $base
$k2 = ConvertTo-DirtyCloneRepoKey -RepoDir ($base.ToUpperInvariant() + [System.IO.Path]::DirectorySeparatorChar)
$k3 = ConvertTo-DirtyCloneRepoKey -RepoDir ($base.Replace('\', '/'))
Check 'repo key ignores case and a trailing separator' $k1 $k2
Check 'repo key ignores / vs \ separators' $k1 $k3

# --- notice: payload parsed ---------------------------------------------------------------------
$okOutput = @(
    'some log line',
    'MINDWIRE_DIRTY_CLONE_PAYLOAD {"repo_dir": "x", "reason": "wrong_head", "head": "feature/a", "porcelain": [], "detail": "HEAD is on feature/a"}'
)
$n = Get-DirtyCloneNotice -RepoDir $base -CandidateKey 'p::T-a' -Output $okOutput -ExitCode 8
Check 'notice key is per repo' "__dirty_clone__/$k1" $n.key
Check 'signature from reason + head' 'dirty-clone:wrong_head:feature/a' $n.signature
Check 'body names the reason' $true ($n.message -like '*reason=wrong_head*')

# --- notice: payload unreadable ------------------------------------------------------------------
$badOutput = @('MINDWIRE_DIRTY_CLONE_PAYLOAD {not json')
$nb = Get-DirtyCloneNotice -RepoDir $base -CandidateKey 'p::T-a' -Output $badOutput -ExitCode 8
Check 'unreadable payload: key still per repo' "__dirty_clone__/$k1" $nb.key
Check 'unreadable payload: parse-failed signature' 'dirty-clone:parse-failed' $nb.signature
$nn = Get-DirtyCloneNotice -RepoDir $base -CandidateKey 'p::T-a' -Output @() -ExitCode 8
Check 'missing payload: parse-failed signature' 'dirty-clone:parse-failed' $nn.signature
Check 'never a GitHub key' $false ($nb.key -like '*github*')

# --- simulated tick: two repos, A dirty, B clean ----------------------------------------------------
$repoA = Join-Path ([System.IO.Path]::GetTempPath()) 'clone-A'
$repoB = Join-Path ([System.IO.Path]::GetTempPath()) 'clone-B'
$candidates = @(
    @{ key = 'p::T-a1'; repo_dir = $repoA },
    @{ key = 'q::T-b1'; repo_dir = $repoB },
    @{ key = 'p::T-a2'; repo_dir = $repoA.ToUpperInvariant() },
    @{ key = 'q::T-b2'; repo_dir = $repoB }
)
$exitByRepo = @{ (ConvertTo-DirtyCloneRepoKey $repoA) = 8; (ConvertTo-DirtyCloneRepoKey $repoB) = 0 }
$dirtyRepoDirs = @{}
$dispositions = @{}
$launchedKeys = @()
$notifyState = @{}
$script:sent = @()
foreach ($cand in $candidates) {
    if (Test-DirtyCloneSkip -RepoDir $cand.repo_dir -DirtyRepoDirs $dirtyRepoDirs) {
        $dispositions[$cand.key] = 'dirty-clone-skip'
        continue
    }
    $launchedKeys += $cand.key
    $code = $exitByRepo[(ConvertTo-DirtyCloneRepoKey $cand.repo_dir)]
    if ($code -eq $DirtyCloneExitCode) {
        $dispositions[$cand.key] = 'dirty-clone'
        $notice = Get-DirtyCloneNotice -RepoDir $cand.repo_dir -CandidateKey $cand.key -Output @() -ExitCode $code
        $dirtyRepoDirs[$notice.repo_key] = $true
        Send-NotificationIfChanged -State $notifyState -Key $notice.key -Signature $notice.signature -Message $notice.message
        continue
    }
    $dispositions[$cand.key] = 'worked'
}
Check 'A first candidate launched and refused' 'dirty-clone' $dispositions['p::T-a1']
Check 'A second candidate (different case) skipped' 'dirty-clone-skip' $dispositions['p::T-a2']
Check 'B candidates still launched' 'worked,worked' (($dispositions['q::T-b1'], $dispositions['q::T-b2']) -join ',')
Check 'A launched once only' 3 $launchedKeys.Count
Check 'one notification' 1 $script:sent.Count

# --- the real loop: ordering and shape (AST) --------------------------------------------------------
$loop = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.ForEachStatementAst] -and
        $n.Variable.Extent.Text -eq '$cand' -and $n.Body.Extent.Text -match 'Invoke-HeadSkipCommitLaunch' }, $true) |
    Select-Object -First 1
Check 'sweep candidate loop found' $true ($null -ne $loop)
$body = $loop.Body.Extent.Text
$posSkip = $body.IndexOf('Test-DirtyCloneSkip')
$posCommit = $body.IndexOf('Invoke-HeadSkipCommitLaunch')
$posInner = $body.IndexOf('& $inner')
$posDirtyBranch = $body.IndexOf('if ($code -eq $DirtyCloneExitCode)')
$posNonZero = $body.IndexOf('if ($code -ne 0) {')
Check 'skip check precedes commit-launch (a skipped candidate is not counted as launched)' $true ($posSkip -ge 0 -and $posSkip -lt $posCommit)
Check 'skip check precedes the launch' $true ($posSkip -lt $posInner)
Check 'dirty-clone branch follows the launch' $true ($posDirtyBranch -gt $posInner)
Check 'dirty-clone branch precedes the generic non-zero (quarantine) branch' $true ($posDirtyBranch -ge 0 -and $posDirtyBranch -lt $posNonZero)

$branch = $loop.Body.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.IfStatementAst] -and
        $n.Clauses[0].Item1.Extent.Text -eq '$code -eq $DirtyCloneExitCode' }, $true) | Select-Object -First 1
$branchText = $branch.Clauses[0].Item2.Extent.Text
Check 'branch ends in continue' $true ($branchText -match '(?m)^\s*continue\s*$')
Check 'branch never quarantines' $false ($branchText -match 'quarantineState|New-QuarantineRecord')
Check 'branch never schedules a retry' $false ($branchText -match 'Register-CandidateFailure|New-RetryPendingRecord')
Check 'branch records the repo as dirty' $true ($branchText -match '\$dirtyRepoDirs\[')
Check 'branch does not stop the tick' $false ($branchText -match '(?m)^\s*break\s*$')

# --- exit 2 regression: the env-terminal branch is untouched by the new one ----------------------
$env2 = $loop.Body.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.IfStatementAst] -and
        $n.Clauses[0].Item1.Extent.Text -eq '$code -eq 2' }, $true) | Select-Object -First 1
$env2Text = $env2.Clauses[0].Item2.Extent.Text
Check 'exit 2 branch still present' $true ($null -ne $env2)
Check 'exit 2 still alerts on the GitHub credential fallback key' $true ($env2Text -match '__github_credential__')
Check 'exit 2 still does not quarantine' $false ($env2Text -match 'quarantineState|New-QuarantineRecord|Register-CandidateFailure')
Check 'exit 2 still continues the sweep' $true ($env2Text -match '(?m)^\s*continue\s*$')
Check 'exit 2 branch does not mark the repo dirty' $false ($env2Text -match 'dirtyRepoDirs')
$posEnv2 = $body.IndexOf('if ($code -eq 2)')
Check 'exit 2 branch still precedes the generic non-zero branch' $true ($posEnv2 -ge 0 -and $posEnv2 -lt $posNonZero)

# ===================================================================================================
# T-clone-guard-pin-ignored-only-in-mindwire, design v2.1 D-3 (Bohr msg-6162 / msg-6164, endorsed by
# Einstein msg-6165): exit 8 is not counted against the thread; the REPO is parked and re-judged by
# one `mindwire clone-check` per tick.
# ===================================================================================================
foreach ($name in 'ConvertTo-DirtyCloneParking', 'Set-DirtyCloneParked', 'Update-DirtyCloneParking',
                  'Get-DirtyCloneDigestLines', 'New-DailyDigest', 'Get-FingerprintHint',
                  'Get-DerivedQuarantineState', 'Format-DurationDigest', 'Get-ParkedRowTag', 'ConvertTo-UtcInstant',
                  'Get-QuarantineReproHint') {
    $fn = $functions | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}

function New-Payload {
    param([string]$Reason, [string]$Head)
    $obj = [ordered]@{ repo_dir = 'x'; reason = $Reason; head = $Head; porcelain = @(); detail = "d-$Reason" }
    return 'MINDWIRE_DIRTY_CLONE_PAYLOAD ' + ($obj | ConvertTo-Json -Compress)
}

# A model of one sweep tick over ONE repo, wired the way the real loop is (the AST checks below pin
# the real loop to the same order): re-judge parked repos → skip parked candidates → commit-launch →
# daemon → on exit 8 revert the commit and park the repo. head_skip's launches_same_head is modelled
# as a counter that commit-launch increments and revert-launch decrements; T42 fires at 3.
$repoP = Join-Path ([System.IO.Path]::GetTempPath()) 'clone-P'
$repoKeyP = ConvertTo-DirtyCloneRepoKey $repoP
$script:sim = @{ launches = 0; daemonRuns = 0; commits = 0; reverts = 0; stalled = 0; lastLaunchCount = 0 }
function Invoke-SimTick {
    param([hashtable]$Parked, [hashtable]$NotifyState, [scriptblock]$Probe, [scriptblock]$Daemon, [string]$Now)
    $null = Update-DirtyCloneParking -Parked $Parked -NotifyState $NotifyState -Probe $Probe -NowIso $Now
    $disp = @{}
    foreach ($cand in @(@{ key = 'p::T-1'; repo_dir = $repoP }, @{ key = 'p::T-2'; repo_dir = $repoP })) {
        $dirtyThisTick = @{}
        if (Test-DirtyCloneSkip -RepoDir $cand.repo_dir -DirtyRepoDirs $Parked) { $disp[$cand.key] = 'dirty-clone-parked'; continue }
        $script:sim.commits++
        $script:sim.launches++
        $script:sim.lastLaunchCount = $script:sim.launches
        if ($script:sim.launches -ge 3) { $script:sim.stalled++ }
        $script:sim.daemonRuns++
        $r = & $Daemon
        if ($r.code -eq $DirtyCloneExitCode) {
            $script:sim.launches--          # Invoke-HeadSkipRevertLaunch
            $script:sim.reverts++
            $notice = Get-DirtyCloneNotice -RepoDir $cand.repo_dir -CandidateKey $cand.key -Output $r.output -ExitCode 8
            Set-DirtyCloneParked -Parked $Parked -RepoDir $cand.repo_dir -Notice $notice -NowIso $Now
            Send-NotificationIfChanged -State $NotifyState -Key $notice.key -Signature $notice.signature -Message $notice.message
            $disp[$cand.key] = 'dirty-clone'
            continue
        }
        $disp[$cand.key] = 'worked'
        break  # a worked thread ends the tick, as in the real sweep
    }
    return $disp
}

Write-Host ''
Write-Host 'parking — wrong_head held for 5 ticks'
$parked = @{}
$notify = @{}
$script:sent = @()
$wrongHead = @{ code = 8; output = @(New-Payload -Reason 'wrong_head' -Head 'feature/x'); stderr = '' }
$script:probeCalls = 0
$probeWrong = { param($d) $script:probeCalls++; return $wrongHead }
$daemonWrong = { return $wrongHead }
$t0 = '2026-10-03T00:00:00.0000000Z'
foreach ($i in 1..5) {
    $d = Invoke-SimTick -Parked $parked -NotifyState $notify -Probe $probeWrong -Daemon $daemonWrong -Now ("2026-10-03T00:0{0}:00.0000000Z" -f $i)
    if ($i -eq 1) { $t0 = $parked[$repoKeyP].since; $tick1 = $d }
}
Check 'tick 1: first candidate refused (exit 8)' 'dirty-clone' $tick1['p::T-1']
Check 'tick 1: second candidate on the same clone is parked, not launched' 'dirty-clone-parked' $tick1['p::T-2']
Check '5 ticks: the daemon ran once' 1 $script:sim.daemonRuns
Check '5 ticks: one notification' 1 $script:sent.Count
Check '5 ticks: no STALLED' 0 $script:sim.stalled
Check '5 ticks: net LAUNCH commits 0 (the one commit was reverted)' 0 ($script:sim.commits - $script:sim.reverts)
Check '5 ticks: clone-check ran once per tick after parking (4)' 4 $script:probeCalls
Check 'parked record carries reason' 'wrong_head' $parked[$repoKeyP].reason
Check 'parked record carries head' 'feature/x' $parked[$repoKeyP].head
Check 'parked record carries detail' 'd-wrong_head' $parked[$repoKeyP].detail
Check 'parked record carries signature' 'dirty-clone:wrong_head:feature/x' $parked[$repoKeyP].signature
Check 'parked record keeps repo_dir for the probe' $repoP $parked[$repoKeyP].repo_dir

Write-Host ''
Write-Host 'parking — the state changes while parked (wrong_head -> dirty_tree)'
$dirtyTree = @{ code = 8; output = @(New-Payload -Reason 'dirty_tree' -Head 'main'); stderr = '' }
$sinceBefore = $parked[$repoKeyP].since
$null = Update-DirtyCloneParking -Parked $parked -NotifyState $notify -Probe { param($d) $dirtyTree } -NowIso '2026-10-03T01:00:00.0000000Z'
Check 'changed state: a second notification' 2 $script:sent.Count
Check 'changed state: reason rewritten' 'dirty_tree' $parked[$repoKeyP].reason
Check 'changed state: signature rewritten' 'dirty-clone:dirty_tree:main' $parked[$repoKeyP].signature
Check 'changed state: since unchanged' $sinceBefore $parked[$repoKeyP].since
$null = Update-DirtyCloneParking -Parked $parked -NotifyState $notify -Probe { param($d) $dirtyTree } -NowIso '2026-10-03T01:05:00.0000000Z'
Check 'unchanged state: no further notification' 2 $script:sent.Count

Write-Host ''
Write-Host 'parking — clone-check could not judge (exit 1)'
$probeFail = @{ code = 1; output = @(); stderr = "clone-check: cannot judge x: RuntimeError: boom" }
$script:sim.daemonRuns = 0
foreach ($i in 1..2) {
    $d = Invoke-SimTick -Parked $parked -NotifyState $notify -Probe { param($x) $probeFail } -Daemon $daemonWrong -Now '2026-10-03T02:00:00.0000000Z'
}
Check 'probe-failed: repo stays parked' $true $parked.ContainsKey($repoKeyP)
Check 'probe-failed: candidates parked, no launch' 'dirty-clone-parked' $d['p::T-1']
Check 'probe-failed: daemon never started' 0 $script:sim.daemonRuns
Check 'probe-failed: exactly one notification over two ticks' 3 $script:sent.Count
Check 'probe-failed: signature names the exit' 'dirty-clone:probe-failed:1' $parked[$repoKeyP].signature
Check 'probe-failed: notice carries the stderr tail' $true ($script:sent[-1] -like '*RuntimeError: boom*')
Check 'probe-failed: since unchanged' $sinceBefore $parked[$repoKeyP].since

Write-Host ''
Write-Host 'parking — unparseable clone-check payload stays parked (parse-failed)'
$bad = @{ code = 8; output = @('MINDWIRE_DIRTY_CLONE_PAYLOAD {nope'); stderr = '' }
$null = Update-DirtyCloneParking -Parked $parked -NotifyState $notify -Probe { param($x) $bad } -NowIso '2026-10-03T02:30:00.0000000Z'
Check 'parse-failed: still parked' $true $parked.ContainsKey($repoKeyP)
Check 'parse-failed: signature' 'dirty-clone:parse-failed' $parked[$repoKeyP].signature

Write-Host ''
Write-Host 'parking — digest rows'
$digestLines = Get-DirtyCloneDigestLines -Parked $parked
Check 'digest: header counts parked repos' $true ($digestLines[0] -like '駐機中 repo*1 件')
Check 'digest: one row naming the repo, reason and since' $true ($digestLines[1] -like "*$repoP*reason=parse-failed*since=$sinceBefore*")
Check 'digest: nothing when nothing is parked' 0 (Get-DirtyCloneDigestLines -Parked @{}).Count
$script:StarvedThreshold = [TimeSpan]::FromHours(24)
$nowUtc = [DateTime]::Parse('2026-10-03T03:00:00Z', $null, [System.Globalization.DateTimeStyles]::AssumeUniversal -bor [System.Globalization.DateTimeStyles]::AdjustToUniversal)
$dg = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} -HeadsByProject @{} -ControlByProject @{} `
    -Now $nowUtc -LiveKeys @() -HumanParked @() -PendingDecisionsState @{} -ParkedPollErrors @() -Budget 1800 `
    -ParkedCloneLines $digestLines
Check 'digest: the 駐機中 row is rendered' $true ($dg -like "*駐機中 repo*$repoP*")
Check 'digest: budget still holds' $true ($dg.Length -le 1800)
$dg0 = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} -HeadsByProject @{} -ControlByProject @{} `
    -Now $nowUtc -LiveKeys @() -HumanParked @() -PendingDecisionsState @{} -ParkedPollErrors @()
Check 'digest: no 駐機中 section without parked repos' $false ($dg0 -like '*駐機中 repo*')

Write-Host ''
Write-Host 'parking — the clone is clean again'
$script:sim.launches = 0; $script:sim.daemonRuns = 0
$clean = @{ code = 0; output = @(); stderr = '' }
$d = Invoke-SimTick -Parked $parked -NotifyState $notify -Probe { param($x) $clean } -Daemon { @{ code = 0; output = @() } } -Now '2026-10-03T04:00:00.0000000Z'
Check 'clean: repo released' $false $parked.ContainsKey($repoKeyP)
Check 'clean: launched in the same tick' 'worked' $d['p::T-1']
Check 'clean: launches_same_head starts at 1' 1 $script:sim.lastLaunchCount
Check 'clean: alert signature forgotten, so a recurrence alerts again' $false $notify.ContainsKey("__dirty_clone__/$repoKeyP")

Write-Host ''
Write-Host 'parking — release survives a notified.json round trip (PR #435 gate round 1)'
# The sweep reloads notified.json every tick, so an in-memory forget only counts once it is
# persisted. "Disk" is modelled as the JSON text Save-JsonState writes (ConvertTo-Json -Depth 5)
# and Get-JsonState reads back as a hashtable, so the check needs no file I/O.
function Save-Sim { param([hashtable]$State) return ($State | ConvertTo-Json -Depth 5) }
function Load-Sim { param([string]$Text) if (-not $Text) { return @{} } return ($Text | ConvertFrom-Json -AsHashtable) }
$rtParked = @{}
$script:sent = @()
$st = @{}
$n1 = Get-DirtyCloneNotice -RepoDir $repoP -CandidateKey 'p::T-1' -Output $wrongHead.output -ExitCode 8
Set-DirtyCloneParked -Parked $rtParked -RepoDir $repoP -Notice $n1 -NowIso '2026-10-03T05:00:00.0000000Z'
Send-NotificationIfChanged -State $st -Key $n1.key -Signature $n1.signature -Message $n1.message
$disk = Save-Sim $st
Check 'round trip: first park alerts' 1 $script:sent.Count
# tick N: clean -> released, then persisted the way the re-judge block persists it
$st = Load-Sim $disk
$null = Update-DirtyCloneParking -Parked $rtParked -NotifyState $st -Probe { param($x) $clean } -NowIso '2026-10-03T06:00:00.0000000Z'
$disk = Save-Sim $st
# tick N+1: fresh load, same fault again -> must alert
$st = Load-Sim $disk
Check 'round trip: forgotten signature is gone after reload' $false $st.ContainsKey("__dirty_clone__/$repoKeyP")
Send-NotificationIfChanged -State $st -Key $n1.key -Signature $n1.signature -Message $n1.message
Check 'round trip: the same fault after a release alerts again' 2 $script:sent.Count

Write-Host ''
Write-Host 'parking — the notice contract fails loudly on drift (PR #435 gate round 2)'
$okNotice = Get-DirtyCloneNotice -RepoDir $repoP -CandidateKey 'p::T-1' -Output $wrongHead.output -ExitCode 8
$pfNotice = Get-DirtyCloneNotice -RepoDir $repoP -CandidateKey 'p::T-1' -Output @('garbage') -ExitCode 8
$cParked = @{}
$threw = $false; try { Set-DirtyCloneParked -Parked $cParked -RepoDir $repoP -Notice $pfNotice -NowIso 'n' } catch { $threw = $true }
Check 'contract: a parse-failed notice (null head) still parks' $false $threw
foreach ($drop in 'repo_key', 'reason', 'head', 'detail', 'signature') {
    $bad = $okNotice.Clone(); $bad.Remove($drop)
    $msg = ''
    try { Set-DirtyCloneParked -Parked @{} -RepoDir $repoP -Notice $bad -NowIso 'n' } catch { $msg = "$_" }
    Check "contract: dropping '$drop' throws and names it" $true ($msg -match "missing key\(s\): $drop")
}
foreach ($blank in 'repo_key', 'reason', 'signature') {
    $bad = $okNotice.Clone(); $bad[$blank] = ''
    $p = @{}; $msg = ''
    try { Set-DirtyCloneParked -Parked $p -RepoDir $repoP -Notice $bad -NowIso 'n' } catch { $msg = "$_" }
    Check "contract: empty '$blank' throws" $true ($msg -match "'$blank' is empty")
    Check "contract: empty '$blank' writes nothing" 0 $p.Count
}

Write-Host ''
Write-Host 'parking — state file rows normalise'
$fromJson = '{"k": {"repo_dir": "C:/x", "reason": "wrong_head", "head": "f", "detail": "", "since": "s", "signature": "g"}, "bad": {"reason": "x"}}' | ConvertFrom-Json -AsHashtable
$norm = ConvertTo-DirtyCloneParking -State $fromJson
Check 'normalise: row kept' 'wrong_head' $norm['k'].reason
Check 'normalise: row without repo_dir dropped' $false $norm.ContainsKey('bad')

# --- the real loop: D-3 wiring (AST) ------------------------------------------------------------------
Write-Host ''
Write-Host 'parking — the real sweep is wired the same way'
$posParkedSkip = $body.IndexOf('-DirtyRepoDirs $dirtyClonesParked')
Check 'parked skip precedes commit-launch' $true ($posParkedSkip -ge 0 -and $posParkedSkip -lt $posCommit)
Check 'branch reverts the LAUNCH commit' $true ($branchText -match 'Invoke-HeadSkipRevertLaunch')
Check 'branch parks the repo' $true ($branchText -match 'Set-DirtyCloneParked')
Check 'branch persists the parking' $true ($branchText -match 'Save-JsonState -Path \$dirtyClonesStatePath')
$scriptText = $ast.Extent.Text
$posUpdate = $scriptText.IndexOf('Update-DirtyCloneParking -Parked $dirtyClonesParked')
Check 're-judge runs before the candidate loop' $true ($posUpdate -ge 0 -and $posUpdate -lt $loop.Extent.StartOffset)
$rejudgeIf = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.IfStatementAst] -and
        $n.Extent.Text -match 'Update-DirtyCloneParking -Parked \$dirtyClonesParked' -and
        $n.Extent.Text.Length -lt 4000 }, $true) | Select-Object -First 1
$rejudgeText = if ($rejudgeIf) { $rejudgeIf.Extent.Text } else { '' }
Check 're-judge block persists the parking' $true ($rejudgeText -match 'Save-JsonState -Path \$dirtyClonesStatePath')
Check 're-judge block persists notified.json with it (a release must stick)' $true ($rejudgeText -match 'Save-JsonState -Path \$notifyStatePath -State \$notifyState')
Check 'exit-8 branch persists notified.json with the parking' $true ($branchText -match 'Save-JsonState -Path \$notifyStatePath -State \$notifyState')
Check 're-judge probes with Invoke-CloneCheck' $true ($scriptText -match 'Probe \{ param\(\$d\) Invoke-CloneCheck -RepoDir \$d \}')
Check 'digest receives the parked rows' $true ($scriptText -match '-ParkedCloneLines \(Get-DirtyCloneDigestLines -Parked \$dirtyClonesParked\)')
$cc = $functions | Where-Object { $_.Name -eq 'Invoke-CloneCheck' } | Select-Object -First 1
Check 'Invoke-CloneCheck runs the clone-check subcommand' $true ($cc.Extent.Text -match "'spirrow_mindwire\.cli', 'clone-check', '--repo-dir'")

# ===================================================================================================
# PR #435 gate round 3, finding 1 (edge-case): the 駐機中 rows are budgeted like every other section.
# Before, they were spliced in above the reserve ladder with no bound on their NUMBER, so enough parked
# repos alone could push the digest past $Budget and no later truncation could bring it back.
# ===================================================================================================
Write-Host ''
Write-Host 'digest budget — many parked repos'
$manyParked = @{}
for ($i = 0; $i -lt 40; $i++) {
    $dir = 'C:/workspace/sandbox/' + ('some-long-repo-name-{0:D2}-impl' -f $i)
    $manyParked[(ConvertTo-DirtyCloneRepoKey $dir)] = @{
        repo_dir = $dir; reason = 'wrong_head'; head = 'feature/T-a-rather-long-feature-branch-name-for-width'
        detail = 'x'; since = ('2026-10-03T00:{0:D2}:00.0000000Z' -f $i); signature = 's'
    }
}
$manyLines = Get-DirtyCloneDigestLines -Parked $manyParked
$naive = (($manyLines | ForEach-Object { "$_" }) -join "`n").Length
Check 'many parked: the rows alone exceed the budget (the boundary the gate named)' $true ($naive -gt 1800)
$bigHumanParked = @(for ($i = 0; $i -lt 5; $i++) { [PSCustomObject]@{ key = "p/T-h$i"; head_msg_id = "msg-$i" } })
$dgMany = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} -HeadsByProject @{} -ControlByProject @{} `
    -Now $nowUtc -LiveKeys @() -HumanParked $bigHumanParked -PendingDecisionsState @{} -ParkedPollErrors @() -Budget 1800 `
    -ParkedCloneLines $manyLines
Check 'many parked: digest stays within the budget' $true ($dgMany.Length -le 1800)
Check 'many parked: header still carries the true count' $true ($dgMany -like '*駐機中 repo*: 40 件*')
Check 'many parked: the dropped rows are counted, not silent' $true ($dgMany -match '\+\d+ 件（省略')
Check 'many parked: the oldest parked repo keeps its row' $true ($dgMany -like '*some-long-repo-name-00-impl*')
Check 'many parked: later sections keep their floor (判断待ち row 0 rendered)' $true ($dgMany -like '*p/T-h0*')
Check 'many parked: 隔離中 and 飢餓 headers still rendered' $true (($dgMany -like '*隔離中: 0 件*') -and ($dgMany -like '*飢餓*'))
$dgUnbounded = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} -HeadsByProject @{} -ControlByProject @{} `
    -Now $nowUtc -LiveKeys @() -HumanParked @() -PendingDecisionsState @{} -ParkedPollErrors @() `
    -ParkedCloneLines $manyLines
Check 'many parked: Budget 0 (legacy) still emits every row' 40 ([regex]::Matches($dgUnbounded, 'some-long-repo-name-\d\d-impl')).Count

# PR #435 gate round 4 (correctness): with $totalQ -gt 0, the 駐機中 reserve must cover the floor of
# EVERY 隔離中 tier (stale / escalated / quarantined), not just the count line. The round-3 test passed
# -QuarantineState @{} and so never reached the $totalQ -gt 0 branch. Here all three tiers are
# populated, several rows each, and 40 parked repos press on the same 1800-char budget.
# The sweep's own thresholds (run-conductor-scheduled.ps1 lines 101-102); this harness dot-sources
# functions only, so the script-scope values are restated here as Test-SweepDigest.ps1 does.
$script:QuarantineEscalatedAfter = [TimeSpan]::FromHours(24)
$script:QuarantineStaleAfter     = [TimeSpan]::FromDays(7)
$quarMixed = @{}
for ($i = 0; $i -lt 4; $i++) {
    $quarMixed[('p/T-stale-quarantine-thread-{0:D2}' -f $i)] = @{ state = 'quarantined'
        first_failure_at = $nowUtc.AddDays(-8 - $i).ToString('o'); failure_fingerprint = @{ head = "msg-s$i"; control = 'run' } }
    $quarMixed[('p/T-escalated-quarantine-thread-{0:D2}' -f $i)] = @{ state = 'quarantined'
        first_failure_at = $nowUtc.AddHours(-30 - $i).ToString('o'); failure_fingerprint = @{ head = "msg-e$i"; control = 'run' } }
    $quarMixed[('p/T-fresh-quarantine-thread-{0:D2}' -f $i)] = @{ state = 'quarantined'
        first_failure_at = $nowUtc.AddHours(-2 - $i).ToString('o'); failure_fingerprint = @{ head = "msg-f$i"; control = 'run' } }
}
$dgQ = New-DailyDigest -QuarantineState $quarMixed -EvaluatedState @{} -HeadsByProject @{} -ControlByProject @{} `
    -Now $nowUtc -LiveKeys @() -HumanParked $bigHumanParked -PendingDecisionsState @{} -ParkedPollErrors @() -Budget 1800 `
    -ParkedCloneLines $manyLines
Check 'many parked + quarantine: digest stays within the budget' $true ($dgQ.Length -le 1800)
Check 'many parked + quarantine: 隔離中 count line carries all 12' $true ($dgQ -like '*隔離中: 12 件*')
Check 'many parked + quarantine: stale tier keeps its floor row (oldest)' $true ($dgQ -like '*T-stale-quarantine-thread-03*')
Check 'many parked + quarantine: escalated tier keeps its floor row (oldest)' $true ($dgQ -like '*T-escalated-quarantine-thread-03*')
Check 'many parked + quarantine: quarantined tier keeps its floor row (oldest)' $true ($dgQ -like '*T-fresh-quarantine-thread-03*')
Check 'many parked + quarantine: no tier reports a floor-unmet 0-row section' $false ($dgQ -like '*予算不足で 0 行*')
Check 'many parked + quarantine: 判断待ち row 0 still rendered' $true ($dgQ -like '*p/T-h0*')
Check 'many parked + quarantine: 駐機中 rows were the ones truncated' $true ($dgQ -match '\+\d+ 件（省略）')

# ===================================================================================================
# PR #435 gate round 3, finding 2 (untested): Invoke-HeadSkipRevertLaunch is EXECUTED, not modelled.
# The real commit-launch and revert-launch wrappers run the real head_skip_decide.py through the real
# bounded uv probe and the real probe input file, so a serialisation mismatch on the verbatim
# commit_output string (e.g. a ConvertTo-Json that double-encodes it) fails here.
# ===================================================================================================
Write-Host ''
Write-Host 'revert-launch — real execution through uv'
$uvOk = $false
try { $null = & uv --version 2>$null; if ($LASTEXITCODE -eq 0) { $uvOk = $true } } catch { $uvOk = $false }
if (-not $uvOk) {
    # Loud, and RED: this path is the one the gate said was untested, so a host without uv must not
    # report it as passing.
    $script:failures++
    Write-Host '  FAIL  uv not on PATH — the real revert-launch execution could not run'
}
else {
    foreach ($name in 'Get-BoundedProbeCommandLine', 'Invoke-BoundedUvProbe', 'Get-ProbeOutputLines',
                      'New-ProbeInputFile', 'Remove-ProbeInputFile',
                      'Invoke-HeadSkipCommitLaunch', 'Invoke-HeadSkipRevertLaunch') {
        $fn = $functions | Where-Object { $_.Name -eq $name } | Select-Object -First 1
        if (-not $fn) { throw "function not found in sweep script: $name" }
        Invoke-Expression $fn.Extent.Text
    }
    $HeadSkipProbeTimeoutSeconds = 120
    $ProbeKillGraceMs = 5000
    $ProbeInputFilePrefix = 'mindwire-probe-'
    $ProbeInputDirectory = [System.IO.Path]::GetTempPath()
    $hsState = Join-Path ([System.IO.Path]::GetTempPath()) ('head-skip-revert-test-{0}.json' -f [guid]::NewGuid().ToString('N'))
    try {
        $hsPayload = [ordered]@{ thread_id = 'T-revert'; head_msg_id = 'msg-10'; token = 'einstein'; token_raw = 'Einstein'
                                 attempts_after = 1; control_state = 'run'; head_fetched = $true; thread_status = '' }
        $c1 = Invoke-HeadSkipCommitLaunch -Payload $hsPayload -StateFilePath $hsState
        Check 'real: first commit-launch ok' $true ([bool]$c1.ok)
        Check 'real: first commit-launch counts 1' 1 $c1.launches_same_head
        $snapshot = [System.IO.File]::ReadAllText($hsState)
        $allReverted = $true
        $maxSeen = 0
        for ($i = 0; $i -lt 3; $i++) {
            $c = Invoke-HeadSkipCommitLaunch -Payload $hsPayload -StateFilePath $hsState
            if ($c.launches_same_head -gt $maxSeen) { $maxSeen = $c.launches_same_head }
            Check "real: commit_output is a raw JSON object string (run $i)" $true ("$($c.commit_output)" -match '^\s*\{')
            $rv = Invoke-HeadSkipRevertLaunch -CommitOutput $c.commit_output -StateFilePath $hsState
            if (-not $rv.ok) { $allReverted = $false; Write-Host "    revert error: $($rv.error)" }
            if ([System.IO.File]::ReadAllText($hsState) -ne $snapshot) { $allReverted = $false }
        }
        Check 'real: every revert-launch succeeded and restored the state byte-for-byte' $true $allReverted
        Check 'real: three refused runs never reach launches_same_head 3' 2 $maxSeen
        $c5 = Invoke-HeadSkipCommitLaunch -Payload $hsPayload -StateFilePath $hsState
        Check 'real: next real launch after the reverts counts 2' 2 $c5.launches_same_head
        # A revert whose record has moved since is refused, never applied.
        $stale = Invoke-HeadSkipRevertLaunch -CommitOutput $c1.commit_output -StateFilePath $hsState
        Check 'real: revert of a superseded commit is refused' $false ([bool]$stale.ok)
        # The double-serialisation the gate described is rejected by the CLI, and the wrapper reports it.
        $dbl = ConvertTo-Json -InputObject ([string]$c5.commit_output) -Compress
        $bad = Invoke-HeadSkipRevertLaunch -CommitOutput $dbl -StateFilePath $hsState
        Check 'real: a double-encoded payload is refused (not silently accepted)' $false ([bool]$bad.ok)
    }
    finally { if (Test-Path -LiteralPath $hsState) { Remove-Item -LiteralPath $hsState -Force } }
}

if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) FAILED"; exit 1 }
Write-Host "all dirty-clone checks passed"
