# Regression guard for the sweep's side of the clone slot pool (T-ephemeral-impl-clone-slot-per-thread
# A3: msg-8047 D-4, Bohr msg-9386 D-1 改訂 2/4/6/7, msg-9395 Obj-35-1, msg-9400 §2, msg-9402
# Obj-36-3/36-4, msg-9754 §1-§4).
#
# 1. deploy/lib/CloneSlot.ps1's pure functions: the config read, the prep decision (launch / wait /
#    park, and each notice), and the finish decision (released / quarantined / parked / refused /
#    failed).
# 2. The wiring in deploy/run-conductor-scheduled.ps1, read from the source text: slot mode only
#    when [loop].slots_root is set; the borrow happens after admission and before the inflight
#    mark; repo_dir is the slot; slot-finish runs in the finally around the conductor run; the
#    shared-clone retreat is skipped for a slot launch; a not-launched borrow is released.
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'deploy/lib/CloneSlot.ps1')

$script:failures = 0
function Check {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} {1}" -f $Name, $Detail) }
}

$tmp = Join-Path $repoRoot ".git/mindwire-scratch/test-cloneslot-$PID"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
try {
    # --- 1a. Get-SlotModeConfig -----------------------------------------------------------------
    $cfg = Join-Path $tmp 'mindwire.toml'
    Set-Content -LiteralPath $cfg -Value "[loop]`nproject = `"p`"`nrepo_dir = `"C:/r`"`n[conductor]`nslots_root = `"C:/wrong`"`n"
    Check 'no [loop].slots_root -> shared-clone path ($null)' ($null -eq (Get-SlotModeConfig -ConfigPath $cfg))
    Set-Content -LiteralPath $cfg -Value "[loop]`nslots_root = `"`"`n"
    Check 'empty slots_root -> $null' ($null -eq (Get-SlotModeConfig -ConfigPath $cfg))
    Set-Content -LiteralPath $cfg -Value "[loop]`nslots_root = `"C:/slots`"  # pool`n[other]`nslots_per_repo = 9`n"
    $m = Get-SlotModeConfig -ConfigPath $cfg
    Check 'slots_root read; slots_per_repo defaults to 1 (other sections ignored)' ($m.slots_root -eq 'C:/slots' -and $m.slots_per_repo -eq 1)
    Set-Content -LiteralPath $cfg -Value "[loop]`nslots_root = `"C:/slots`"`nslots_per_repo = 3`n"
    Check 'slots_per_repo read' ((Get-SlotModeConfig -ConfigPath $cfg).slots_per_repo -eq 3)
    Check 'missing config file -> $null' ($null -eq (Get-SlotModeConfig -ConfigPath (Join-Path $tmp 'nope.toml')))
}
finally { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }

# --- 1b. small helpers ------------------------------------------------------------------------
Check 'repo name is the clone leaf (either separator, trailing one ignored)' (
    (Get-SlotRepoName -RepoDir 'C:\work\spirrow-mindwire\') -eq 'spirrow-mindwire' -and
    (Get-SlotRepoName -RepoDir '/home/x/lexora') -eq 'lexora')
$tid = New-SlotTurnId -NowUtc ([datetime]::new(2026, 11, 19, 3, 4, 5, [DateTimeKind]::Utc))
Check 'turn id has no slash and is a ref component' ($tid -match '^20261119T030405Z-[0-9a-f]{8}$') $tid
Check 'Get-SlotRow parses the prefixed line' ((Get-SlotRow -Stdout "noise`nMINDWIRE_SLOT_PREP {`"outcome`":`"ready`"}`n" -Prefix 'MINDWIRE_SLOT_PREP ').outcome -eq 'ready')
Check 'Get-SlotRow: unreadable json -> $null' ($null -eq (Get-SlotRow -Stdout "MINDWIRE_SLOT_PREP {oops" -Prefix 'MINDWIRE_SLOT_PREP '))

function Row([string]$Json) { return (ConvertFrom-SlotJson -Text $Json) }

# --- 1c. Get-SlotPrepDecision -----------------------------------------------------------------
$ready = Row '{"outcome":"ready","slot":"C:/slots/r/slot-01","slot_name":"slot-01","turn_id":"t1","kept":[],"quarantine":null}'
$d = Get-SlotPrepDecision -Code 0 -Row $ready -Repo 'r' -CandidateKey 'p/T-a'
Check 'ready -> launch on the slot, no notice' ($d.action -eq 'launch' -and $d.slot -eq 'C:/slots/r/slot-01' -and $d.slot_name -eq 'slot-01' -and $d.turn_id -eq 't1' -and @($d.notices).Count -eq 0)

$wait = Row '{"outcome":"waiting","waiting_reason":"no_free_slot","waiting_since":"2026-11-19T00:00:00Z","wait_notice":false,"kept":[]}'
$d = Get-SlotPrepDecision -Code 3 -Row $wait -Repo 'r' -CandidateKey 'p/T-a'
Check 'waiting -> wait, no park, no notice before 30 min' ($d.action -eq 'wait' -and $null -eq $d.park_code -and @($d.notices).Count -eq 0)
$wait.wait_notice = $true
$d = Get-SlotPrepDecision -Code 3 -Row $wait -Repo 'r' -CandidateKey 'p/T-a'
Check 'waiting past 30 min -> one notice keyed on the candidate, signed by waiting_since' (
    $d.action -eq 'wait' -and @($d.notices).Count -eq 1 -and $d.notices[0].key -eq '__slot_wait__/p/T-a' -and
    $d.notices[0].signature.StartsWith('2026-11-19T00:00:00') -and $d.notices[0].message.Contains('2026-11-19T00:00:00'))

foreach ($pc in 'clone_slot_unrecoverable', 'workspace-untrusted', 'clone_slot_provision_failed') {
    $park = Row ('{"outcome":"parked","park_code":"' + $pc + '","slot":"C:/slots/r/slot-01","slot_name":"slot-01","detail":"why","kept":[]}')
    $d = Get-SlotPrepDecision -Code 4 -Row $park -Repo 'r' -CandidateKey 'p/T-a'
    Check "parked ($pc) -> park this thread with that code, notice names the slot path" (
        $d.action -eq 'park' -and $d.park_code -eq $pc -and @($d.notices).Count -eq 1 -and
        $d.notices[0].message.Contains('C:/slots/r/slot-01') -and $d.notices[0].message.Contains($pc))
}
$d = Get-SlotPrepDecision -Code 4 -Row (Row '{"outcome":"parked","park_code":"workspace-untrusted","slot":"C:/s","slot_name":"slot-01","detail":"","kept":[]}') -Repo 'r' -CandidateKey 'k'
Check 'workspace-untrusted notice points at docs/deploy.md' ($d.notices[0].message.Contains('docs/deploy.md'))
foreach ($case in @(@{ c = 1; r = $null }, @{ c = -1; r = $null }, @{ c = 0; r = $null }, @{ c = 7; r = $ready })) {
    $d = Get-SlotPrepDecision -Code $case.c -Row $case.r -Repo 'r' -CandidateKey 'p/T-a' -Stderr 'boom'
    Check "exit $($case.c) / row=$($null -ne $case.r) -> park clone_slot_prep_crashed" ($d.action -eq 'park' -and $d.park_code -eq 'clone_slot_prep_crashed')
}

$kept = Row '{"outcome":"ready","slot":"C:/s/slot-02","slot_name":"slot-02","turn_id":"t2","quarantine":null,"kept":[{"slot":"slot-01","thread_id":"T-x","turn_id":"t0","state":"running","reason":"job_containment_unverified"},{"slot":"slot-03","thread_id":"T-y","turn_id":"t9","state":"quarantine_failed","reason":"quarantine_failed"}]}'
$d = Get-SlotPrepDecision -Code 0 -Row $kept -Repo 'r' -CandidateKey 'p/T-a'
$keys = @($d.notices | ForEach-Object { $_.key })
Check 'kept leases -> one notice each, launch still goes ahead' ($d.action -eq 'launch' -and $keys.Count -eq 2 -and $keys -contains '__slot_kept__/r/slot-01' -and $keys -contains '__slot_kept__/r/slot-03')
Check 'job_containment_unverified notice says no automatic reclaim' ($d.notices[0].message.Contains('job_containment_unverified') -and $d.notices[0].message.Contains('自動回収しません'))
Check 'kept notice signature is turn + reason (not every tick)' ($d.notices[0].signature -eq 't0:job_containment_unverified')

$rq = Row '{"outcome":"ready","slot":"C:/s/slot-01","slot_name":"slot-01","turn_id":"t2","kept":[],"quarantine":{"reason":"modified","previous_thread":"T-old","quarantine_path":"C:/s/_quarantine/r/slot-01-T-old-x","rescue_outcome":"rescue_failed","rescue_detail":"push refused"}}'
$d = Get-SlotPrepDecision -Code 0 -Row $rq -Repo 'r' -CandidateKey 'p/T-a'
Check 'borrow-time rescue failure -> notice with quarantine path and previous thread (改訂 6)' (
    $d.action -eq 'launch' -and @($d.notices).Count -eq 1 -and $d.notices[0].message.Contains('C:/s/_quarantine/r/slot-01-T-old-x') -and
    $d.notices[0].message.Contains('T-old'))
$rq.quarantine.rescue_outcome = 'rescued'
Check 'borrow-time rescue that succeeded -> no notice' (@((Get-SlotPrepDecision -Code 0 -Row $rq -Repo 'r' -CandidateKey 'k').notices).Count -eq 0)

# --- 1d. Get-SlotFinishDecision ---------------------------------------------------------------
$f = Get-SlotFinishDecision -Code 0 -Row (Row '{"outcome":"released","notice":null}') -Repo 'r' -SlotName 'slot-01' -CandidateKey 'k'
Check 'released -> no park, no notice' ($null -eq $f.park_code -and $f.outcome -eq 'released' -and @($f.notices).Count -eq 0)
$f = Get-SlotFinishDecision -Code 5 -Row (Row '{"outcome":"quarantined","notice":{"reason":"rescue_failed","path":"C:/s/_quarantine/r/q1","detail":"push failed"}}') -Repo 'r' -SlotName 'slot-01' -CandidateKey 'p/T-a'
Check 'rescue_failed -> quarantined, NOT parked here (A4 decides continuation), notice with path and reason' (
    $null -eq $f.park_code -and $f.outcome -eq 'quarantined' -and @($f.notices).Count -eq 1 -and
    $f.notices[0].message.Contains('C:/s/_quarantine/r/q1') -and $f.notices[0].message.Contains('rescue_failed') -and $f.notices[0].message.Contains('p/T-a'))
$f = Get-SlotFinishDecision -Code 4 -Row (Row '{"outcome":"parked","notice":{"reason":"clone_slot_unrecoverable","path":"C:/s/r/slot-01","detail":"rename failed"}}') -Repo 'r' -SlotName 'slot-01' -CandidateKey 'p/T-a'
Check 'rename failed -> park clone_slot_unrecoverable, notice names the slot itself' (
    $f.park_code -eq 'clone_slot_unrecoverable' -and $f.notices[0].message.Contains('C:/s/r/slot-01'))
$f = Get-SlotFinishDecision -Code 2 -Row (Row '{"outcome":"refused","detail":"leased to t9","notice":null}') -Repo 'r' -SlotName 'slot-01' -CandidateKey 'k'
Check 'refused -> notice, no park' ($null -eq $f.park_code -and $f.outcome -eq 'refused' -and @($f.notices).Count -eq 1)
$f = Get-SlotFinishDecision -Code -1 -Row $null -Repo 'r' -SlotName 'slot-01' -CandidateKey 'k' -Stderr 'timeout'
Check 'finish did not complete -> notice, lease left as is, no park' ($null -eq $f.park_code -and $f.outcome -eq 'failed' -and $f.notices[0].message.Contains('timeout'))

# --- 2. the wiring in the sweep -----------------------------------------------------------------
$src = Get-Content -Raw -LiteralPath (Join-Path $repoRoot 'deploy/run-conductor-scheduled.ps1')
Check 'the lib is dot-sourced' ($src.Contains(". (Join-Path `$PSScriptRoot 'lib/CloneSlot.ps1')"))
Check 'slot mode comes from the config (and only from it)' ($src.Contains('$slotMode = Get-SlotModeConfig -ConfigPath $configPath'))
$adm = $src.IndexOf('$admission = Request-LaunchAdmission')
$prep = $src.IndexOf('$prepRun = Invoke-SlotPrep')
$mark = $src.IndexOf('$mark = Add-InflightMark')
$commit = $src.IndexOf('$commitResult = Invoke-HeadSkipCommitLaunch')
Check 'borrow is after admission and before the inflight mark and commit-launch' ($adm -gt 0 -and $prep -gt $adm -and $mark -gt $prep -and $commit -gt $mark) "$adm $prep $mark $commit"
Check 'gate lane borrows nothing' ($src.Contains("if (`$null -ne `$slotMode -and `$lane -ne 'gate')"))
Check 'repo_dir is the borrowed slot in slot mode' ($src.Contains('$turnRepoDir = if ($null -ne $slotLease) { $slotLease.slot } else { $cand.repo_dir }') -and
    $src.Contains("-Key 'repo_dir'       -Value `$turnRepoDir"))
$spawn = $src.IndexOf("Invoke-SlotLease -Action 'spawning'")
$inner = $src.IndexOf('$output = (& $inner @stallArgs @laneArgs @summonArgs *>&1)')
$fin = $src.IndexOf('$slotAfter = Complete-SlotAfterRun -Lease $slotLease -Code $code')
$retreat = $src.IndexOf('$retreatOutcome = Invoke-LaunchEndRetreat')
Check 'spawning mark before the run; slot-finish right after it' ($spawn -gt 0 -and $inner -gt $spawn -and $fin -gt $inner) "$spawn $inner $fin"
$open = $src.IndexOf('$script:slotOpen = @{ lease = $slotLease; spawned = $false')
$flag = $src.IndexOf('if ($null -ne $script:slotOpen) { $script:slotOpen.spawned = $true }')
Check 'the lease is open from the borrow; spawned is set right before the run' ($open -gt $prep -and $flag -gt $open -and $inner -gt $flag -and
    $src.Substring($flag, $inner - $flag).Split("`n").Count -le 3)
$top = $src.LastIndexOf("`nfinally {")
Check 'the top-level finally closes a lease a throw left open' ($top -gt 0 -and $src.Substring($top).Contains('Close-OpenSlot -Open $script:slotOpen'))
Check 'shared-clone retreat skipped for a slot launch' ($retreat -gt $fin -and $src.Contains("if (`$slotFinished) { `$retreatOutcome = 'skipped' }"))
Check 'inflight-mark failure releases the borrow at once' (([regex]::Matches($src, 'Undo-SlotBorrow -Lease \$slotLease')).Count -eq 1)
Check 'slot parks use the per-thread quarantine record with the park code' (
    ([regex]::Matches($src, '-StopReason \$slot(Decision|After)\.park_code')).Count -eq 2)

# PR #594 gate Obj-1-1: exit 7 (kill not confirmed) keeps the lease. The decision lives in one
# function, Complete-SlotAfterRun; section 3 below runs it with stubs.
Check 'slot-finish is called from one place (Complete-SlotAfterRun), none in Close-OpenSlot' (
    ([regex]::Matches($src, 'Complete-SlotTurn -Lease')).Count -eq 1 -and
    ([regex]::Matches($src, 'Complete-SlotAfterRun -Lease')).Count -eq 1)
$rn = Get-SlotRetainedNotice -Repo 'r' -SlotName 'slot-01' -Slot 'C:/s/r/slot-01' -TurnId 't1' -ThreadId 'T-a' -CandidateKey 'p/T-a' -Cause 'exit=7, kill not confirmed'
Check 'retained notice: same key/signature as the later kept notice, names the slot and the cause' (
    $rn.key -eq '__slot_kept__/r/slot-01' -and $rn.signature -eq 't1:job_containment_unverified' -and
    $rn.message.Contains('C:/s/r/slot-01') -and $rn.message.Contains('exit=7') -and $rn.message.Contains('job_containment_unverified'))

# Close-OpenSlot, lifted from the sweep and run with stubs: spawned -> lease kept + notice (no
# slot-finish: the end of a run that threw is not confirmed); not spawned -> release.
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$null, [ref]$null)
$fnClose = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Close-OpenSlot' }, $true) | Select-Object -First 1
Invoke-Expression $fnClose.Extent.Text
$script:calls = @()
function Complete-SlotTurn { param($Lease, $SlotsRoot, $Project, $StateDir, $ThreadId, $CandidateKey) $script:calls += "finish:$($Lease.slot_name)" }
function Undo-SlotBorrow { param($Lease, $StateDir, $CandidateKey) $script:calls += "release:$($Lease.slot_name)" }
function Write-Log { param($m) $script:calls += "log" }
function Send-SlotNotices { param($NotifyState, $Notices) foreach ($n in @($Notices)) { $script:calls += "notice:$($n.signature)" } }
$notifyState = @{}
$lease = @{ repo = 'r'; slot = 'C:/s/r/slot-01'; slot_name = 'slot-01'; turn_id = 't1' }
Close-OpenSlot -Open @{ lease = $lease; spawned = $true; key = 'k'; thread = 'T'; project = 'p'; slots_root = 'C:/s'; state_dir = 'C:/st' }
Close-OpenSlot -Open @{ lease = $lease; spawned = $false; key = 'k'; thread = 'T'; project = 'p'; slots_root = 'C:/s'; state_dir = 'C:/st' }
Close-OpenSlot -Open $null
Check 'Close-OpenSlot: spawned -> kept + notice (no finish), not spawned -> release, $null -> nothing' (($script:calls -join ',') -eq 'notice:t1:job_containment_unverified,log,release:slot-01') ($script:calls -join ',')
function Undo-SlotBorrow { param($Lease, $StateDir, $CandidateKey) throw 'boom' }
$script:calls = @()
Close-OpenSlot -Open @{ lease = $lease; spawned = $false; key = 'k' }
Check 'Close-OpenSlot never throws (it runs inside finally)' (($script:calls -join ',') -eq 'log')

# --- 3. A4 (msg-9877 (b)): the exit-7 branch and Close-OpenSlot, executed with stubs ----------
# A fake lease store stands in for state/slot-leases.json. slot-finish, release and rename are
# stubs that would change it, so "never called" and "lease still spawning" are both observable.
$fnAfter = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Complete-SlotAfterRun' }, $true) | Select-Object -First 1
Invoke-Expression $fnAfter.Extent.Text
$ConductorKillUnconfirmedExitCode = 7
function Reset-SlotStubs {
    $script:calls = @(); $script:notices = @()
    $script:leaseStore = @{ 'slot-01' = @{ state = 'running'; spawning = $true; turn_id = 't1' } }
}
function Complete-SlotTurn { param($Lease, $SlotsRoot, $Project, $StateDir, $ThreadId, $CandidateKey)
    $script:calls += 'finish'; $script:leaseStore.Remove($Lease.slot_name); return @{ park_code = $null } }
function Invoke-SlotFinish { $script:calls += 'slot-finish'; $script:leaseStore.Clear(); return @{ code = 0; stdout = ''; stderr = '' } }
function Invoke-SlotLease { param($Action) $script:calls += "lease:$Action"; if ($Action -eq 'release') { $script:leaseStore.Clear() }; return 0 }
function Undo-SlotBorrow { param($Lease, $StateDir, $CandidateKey) $script:calls += 'release'; $script:leaseStore.Remove($Lease.slot_name) }
function Rename-Item { $script:calls += 'rename' }
function Move-Item { $script:calls += 'rename' }
function Send-SlotNotices { param($NotifyState, $Notices) foreach ($n in @($Notices)) { if ($null -ne $n) { $script:notices += $n } } }
function Write-Log { param($m) }
$open7 = @{ lease = $lease; spawned = $true; key = 'p/T-a'; thread = 'T-a'; project = 'p'; slots_root = 'C:/s'; state_dir = 'C:/st' }

Reset-SlotStubs
$script:slotOpen = $open7
$r7 = Complete-SlotAfterRun -Lease $lease -Code 7 -SlotsRoot 'C:/s' -Project 'p' -StateDir 'C:/st' -ThreadId 'T-a' -CandidateKey 'p/T-a'
Close-OpenSlot -Open $script:slotOpen   # the top-level finally, after the run returned exit 7
Check 'exit 7: no slot-finish, no release, no rename' (@($script:calls).Count -eq 0) ($script:calls -join ',')
Check 'exit 7: the lease is still held (spawning/running)' ($script:leaseStore.ContainsKey('slot-01') -and
    $script:leaseStore['slot-01'].state -eq 'running' -and $script:leaseStore['slot-01'].spawning)
Check 'exit 7: exactly one job_containment_unverified notice, even with the finally after it' (
    @($script:notices).Count -eq 1 -and $script:notices[0].signature -eq 't1:job_containment_unverified' -and
    $script:notices[0].message.Contains('exit=7'))
Check 'exit 7: retained, nothing parked, the lease is no longer open' ($r7.retained -and $null -eq $r7.park_code -and $null -eq $script:slotOpen)

Reset-SlotStubs
$script:slotOpen = $open7
Close-OpenSlot -Open $script:slotOpen   # a throw out of the run after spawn
Check 'throw after spawn: no slot-finish, no release, no rename; lease held; one notice' (
    @($script:calls).Count -eq 0 -and $script:leaseStore.ContainsKey('slot-01') -and
    @($script:notices).Count -eq 1 -and $script:notices[0].signature -eq 't1:job_containment_unverified') ($script:calls -join ',')

foreach ($c in @(0, 1, 6)) {
    Reset-SlotStubs
    $script:slotOpen = $open7
    $rc = Complete-SlotAfterRun -Lease $lease -Code $c -SlotsRoot 'C:/s' -Project 'p' -StateDir 'C:/st' -ThreadId 'T-a' -CandidateKey 'p/T-a'
    Check "exit ${c}: slot-finish runs once, no retained notice" ((($script:calls -join ',') -eq 'finish') -and -not $rc.retained -and @($script:notices).Count -eq 0) ($script:calls -join ',')
}

# --- 4. A4 (msg-9400 §3): the exit-14 park -----------------------------------------------------
$out14 = "conductor stopped`nMINDWIRE_SLOT_CONTINUE_REFUSED {`"project`":`"p`",`"thread`":`"T-a`",`"park_code`":`"clone_slot_continue_refused`",`"reason`":`"not_saved`",`"turn_id`":`"t2`",`"outcomes`":[`"rescue_failed`"],`"path`":`"C:/s/_quarantine/r/slot-01-x`",`"detail`":`"turn t2 ended rescue_failed`"}`n"
$d14 = Get-SlotContinueRefusedDecision -Output $out14 -CandidateKey 'p/T-a'
Check 'exit 14: parks with clone_slot_continue_refused, notice names the path and the reason' (
    $d14.park_code -eq 'clone_slot_continue_refused' -and $d14.path -eq 'C:/s/_quarantine/r/slot-01-x' -and
    $d14.notice.message.Contains('C:/s/_quarantine/r/slot-01-x') -and $d14.notice.message.Contains('not_saved') -and
    $d14.notice.key -eq '__slot_continue__/p/T-a')
$d14b = Get-SlotContinueRefusedDecision -Output 'no row here' -CandidateKey 'p/T-a'
Check 'exit 14 without a readable row still parks (same code)' ($d14b.park_code -eq 'clone_slot_continue_refused' -and $d14b.path.Contains('unknown'))
$b14 = $src.IndexOf('if ($code -eq $SlotContinueRefusedExitCode) {')
$b8 = $src.LastIndexOf('if ($code -eq $DirtyCloneExitCode) {')
$generic = $src.IndexOf('# handle (i.e. anything other than the 0/2/')
Check 'exit 14 branch: after the slot is finished, above exit 8 and the generic branch; per-thread record; continue' (
    $b14 -gt $fin -and $b14 -lt $b8 -and $b14 -lt $generic -and
    $src.Substring($b14, $b8 - $b14).Contains('-StopReason $refused.park_code') -and
    $src.Substring($b14, $b8 - $b14).Contains("-FailureClass 'clone_slot'") -and
    $src.Substring($b14, $b8 - $b14).Contains('continue') -and
    -not $src.Substring($b14, $b8 - $b14).Contains('Set-DirtyCloneParked')) "$fin $b14 $b8 $generic"
Check 'exit code 14 is declared' ($src.Contains('$SlotContinueRefusedExitCode = 14'))

if ($script:failures -gt 0) { Write-Host "$($script:failures) failure(s)"; exit 1 }
Write-Host "all passed"
