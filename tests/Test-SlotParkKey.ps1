# Regression guard for step 2d of T-ephemeral-impl-clone-slot-per-thread (D-5 in slot mode):
# Get-DirtyCloneParkKey, Get-DirtyCloneParkKeyKind, the re-judge exclusion in
# Update-DirtyCloneParking, keeping key-less slot parks in ConvertTo-DirtyCloneParking, and the
# single-key unpark `deploy/Clear-Quarantine.ps1 -SlotParkKey`.
#
# Bohr msg-9319 / msg-9321 (Obj-24-1, Einstein go msg-9322):
#   (a) a clone_slot_session_unknown slot park stays parked on a clean slot; clone-check never runs;
#   (b) a repo-keyed dirty-clone park is still released by clone-check exit 0;
#   (c) OFF (INV-0): the key is ConvertTo-DirtyCloneRepoKey's string and every key OFF writes is a
#       repo key (the unchanged loop itself is pinned by tests/Test-DirtyClone.ps1, which still passes);
#   (d) a provision_failed slot park is held the same way, whatever its code;
#   (e) -SlotParkKey releases exactly that one key; another slot of the same repo and the repo park
#       stay; a repo key is refused.
# A key that cannot be classified is held (never silently released) and survives a load.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$clearScript = Join-Path $repoRoot "deploy/Clear-Quarantine.ps1"
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }

foreach ($var in '$SlotParkKeyPrefix', '$DirtyCloneExitCode', '$ConductorKillUnconfirmedExitCode',
                 '$SlotKillUnconfirmedParkCode', '$SlotKillUnconfirmedDetail', '$SlotPrepCrashedParkCode') {
    $assign = $ast.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $n.Left.Extent.Text -eq $var }, $true) | Select-Object -First 1
    if (-not $assign) { throw "$var not assigned in sweep script" }
    Invoke-Expression $assign.Extent.Text
}
$functions = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'ConvertTo-DirtyCloneRepoKey', 'Get-DirtyCloneParkKey', 'Get-DirtyCloneParkKeyKind',
                  'ConvertTo-DirtyCloneParking', 'Set-DirtyCloneParked', 'Update-DirtyCloneParking',
                  'ConvertFrom-AlertIso', 'Read-LatestRetreatRecordsByRepo', 'Get-SlotKillUnconfirmedPark',
                  'Complete-SlotThreadPark') {
    $fn = $functions | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}

$script:logLines = [System.Collections.Generic.List[string]]::new()
function Write-Log { param([string]$Message) $script:logLines.Add($Message) }
function Send-NotificationIfChanged { param([hashtable]$State, [string]$Key, [string]$Signature, [string]$Message) }

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual) }
}

$tmp = [System.IO.Path]::GetTempPath()
$sharedClone = [System.IO.Path]::Combine($tmp, 'Shared-Clone', 'mindwire-impl')
$repoKey = ConvertTo-DirtyCloneRepoKey -RepoDir $sharedClone

# --- the key ------------------------------------------------------------------------------------------
Check '(c) OFF: park key is the repo key, character for character' $repoKey (Get-DirtyCloneParkKey -RepoDir $sharedClone -SlotMode $false -Repo 'Spirrow-Mindwire' -ThreadId 'T-a')
$kA = Get-DirtyCloneParkKey -RepoDir $sharedClone -SlotMode $true -Repo 'Spirrow-Mindwire' -ThreadId 'T-a'
$kB = Get-DirtyCloneParkKey -RepoDir $sharedClone -SlotMode $true -Repo 'spirrow-mindwire' -ThreadId 'T-b'
Check 'ON: slot key shape' 'slot|spirrow-mindwire|T-a' $kA
Check 'ON: two threads of one repo get two keys (D-5)' $true ($kA -ne $kB)
Check 'ON: slot key never equals the repo key' $true ($kA -ne $repoKey)
foreach ($bad in @(@('', 'T-a'), @('r|x', 'T-a'), @('r', ''), @('r', 'T a'), @('r', 'T|a'))) {
    $msg = ''
    try { $null = Get-DirtyCloneParkKey -RepoDir $sharedClone -SlotMode $true -Repo $bad[0] -ThreadId $bad[1] } catch { $msg = "$_" }
    Check "ON: refuses repo='$($bad[0])' thread='$($bad[1])'" $true ($msg -like 'Get-DirtyCloneParkKey:*')
}

# --- the classifier -----------------------------------------------------------------------------------
foreach ($d in @($sharedClone, [System.IO.Path]::Combine($tmp, 'a b', 'C'), $tmp)) {
    $k = ConvertTo-DirtyCloneRepoKey -RepoDir $d
    Check "(c) every key ConvertTo-DirtyCloneRepoKey writes is 'repo': $k" 'repo' (Get-DirtyCloneParkKeyKind -Key $k)
}
Check 'slot key is slot' 'slot' (Get-DirtyCloneParkKeyKind -Key $kA)
foreach ($u in @('', 'bad', 'slot|', 'slot|a', 'slot|a|b|c', 'slot||b', 'SLOT|a|b', 'relative/path', ($repoKey + '\'))) {
    Check "unclassifiable key '$u' is unknown (held, never auto-released)" 'unknown' (Get-DirtyCloneParkKeyKind -Key $u)
}

# --- (a)(b)(d) the re-judge loop ---------------------------------------------------------------------------
$script:probed = [System.Collections.Generic.List[string]]::new()
$clean = { param($d) $script:probed.Add("$d"); return @{ code = 0; output = @(); stderr = '' } }
$slotDirA = [System.IO.Path]::Combine($tmp, 'slots', 'spirrow-mindwire', 'aaaaaaaaaaaa')
$slotDirC = [System.IO.Path]::Combine($tmp, 'slots', 'spirrow-mindwire', 'cccccccccccc')
$kC = Get-DirtyCloneParkKey -RepoDir '' -SlotMode $true -Repo 'spirrow-mindwire' -ThreadId 'T-c'
$parked = @{}
$parked[$repoKey] = @{ repo_dir = $sharedClone; reason = 'modified'; head = 'h'; detail = ''; since = 's0'; signature = 'g0' }
$parked[$kA] = @{ repo_dir = $slotDirA; reason = 'clone_slot_session_unknown'; head = ''; detail = 'kill-unconfirmed (exit=7)'; since = 's1'; signature = 'g1' }
$parked[$kC] = @{ repo_dir = $slotDirC; reason = 'clone_slot_provision_failed'; head = ''; detail = 'trust'; since = 's2'; signature = 'g2' }
$parked['corrupt-key'] = @{ repo_dir = $slotDirC; reason = 'x'; head = ''; detail = ''; since = 's3'; signature = 'g3' }
$released = Update-DirtyCloneParking -Parked $parked -NotifyState @{} -Probe $clean -NowIso '2026-11-16T08:00:00Z'
Check '(b) repo-keyed park released on clone-check exit 0' $false $parked.ContainsKey($repoKey)
Check '(b) released list names the repo key only' $repoKey (@($released) -join ',')
Check '(a) session_unknown slot park on a clean slot stays parked' $true $parked.ContainsKey($kA)
Check '(d) provision_failed slot park on a clean slot stays parked' $true $parked.ContainsKey($kC)
Check 'unclassifiable key stays parked' $true $parked.ContainsKey('corrupt-key')
Check '(a)(d) clone-check ran only for the repo park, never on a slot' $sharedClone (@($script:probed) -join ',')
Check 'held slot parks are logged' 3 (@($script:logLines | Where-Object { $_ -like 'slot-park-held *' }).Count)
# The sweep loop skips a candidate whose park key is in this map; in slot mode that key is
# Get-DirtyCloneParkKey (wired in 2e), so the held slot park keeps prep from running.
Check '(a) next tick: the thread''s slot key is still parked (prep not reached)' $true $parked.ContainsKey((Get-DirtyCloneParkKey -RepoDir '' -SlotMode $true -Repo 'spirrow-mindwire' -ThreadId 'T-a'))
Check '(a) a second tick still holds it' $true ($null -ne (Update-DirtyCloneParking -Parked $parked -NotifyState @{} -Probe $clean -NowIso '2026-11-16T09:00:00Z') -and $parked.ContainsKey($kA))

# --- a wrapper park without a slot_dir survives a save/load round trip ---------------------------------
$crash = @{ action = 'park'; crashed = $true; slot_dir = $null; park_code = $SlotPrepCrashedParkCode
            detail = 'slot-prep exit=1: boom'; notice_key = '__clone_slot_park__/p/T-d'; signature = 'c'; message = 'm' }
$kD = Get-DirtyCloneParkKey -RepoDir '' -SlotMode $true -Repo 'spirrow-mindwire' -ThreadId 'T-d'
$p2 = @{}
$null = Complete-SlotThreadPark -Parked $p2 -ParkKey $kD -Verdict $crash -NowIso '2026-11-16T08:00:00Z' -NotifyState @{} `
    -LogPath ([System.IO.Path]::Combine($tmp, 'no-such-dir-' + [guid]::NewGuid().ToString('N'), 'x.jsonl')) -ThreadId 'T-d' -Repo 'spirrow-mindwire'
$reloaded = ConvertTo-DirtyCloneParking -State (($p2 | ConvertTo-Json -Depth 5) | ConvertFrom-Json -AsHashtable)
Check 'crash park (no slot_dir) survives save/load' $true $reloaded.ContainsKey($kD)
Check 'crash park keeps its code after load' 'clone_slot_prep_crashed' $reloaded[$kD].reason

# --- (e) the single-key unpark ----------------------------------------------------------------------------
$data = Join-Path $tmp ("slot-unpark-test-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $data 'state') -Force | Out-Null
try {
    $statePath = Join-Path $data 'state\dirty-clones.json'
    $histPath = Join-Path $data 'state\quarantine-history.json'
    $s = @{}
    $s[$repoKey] = @{ repo_dir = $sharedClone; reason = 'modified'; head = 'h'; detail = ''; since = 's0'; signature = 'g0' }
    $s[$kA] = @{ repo_dir = $slotDirA; reason = 'clone_slot_session_unknown'; head = ''; detail = 'kill-unconfirmed (exit=7)'; since = 's1'; signature = 'g1' }
    $s[$kC] = @{ repo_dir = $slotDirC; reason = 'clone_slot_provision_failed'; head = ''; detail = 'trust'; since = 's2'; signature = 'g2' }
    [System.IO.File]::WriteAllText($statePath, ($s | ConvertTo-Json -Depth 5), [System.Text.UTF8Encoding]::new($false))

    $out = & pwsh -NoProfile -File $clearScript -SlotParkKey $kA -Reason 'checked: no mindwire-loop / claude left' -DataDir $data -Confirm:$false 2>&1
    Check '(e) unpark exits 0' 0 $LASTEXITCODE
    $after = (Get-Content -LiteralPath $statePath -Raw -Encoding utf8) | ConvertFrom-Json -AsHashtable
    Check '(e) the named slot park is gone' $false $after.ContainsKey($kA)
    Check '(e) the other slot of the same repo stays' $true $after.ContainsKey($kC)
    Check '(e) the repo-keyed park stays' $true $after.ContainsKey($repoKey)
    $hist = @((Get-Content -LiteralPath $histPath -Raw -Encoding utf8) | ConvertFrom-Json)
    Check '(e) one history entry' 1 $hist.Count
    Check '(e) history entry kind' 'slot_park' $hist[0].kind
    Check '(e) history entry key' $kA $hist[0].park_key
    Check '(e) history keeps the reason' 'checked: no mindwire-loop / claude left' $hist[0].reason
    Check '(e) history keeps the removed record' 'clone_slot_session_unknown' $hist[0].record.reason

    $out = & pwsh -NoProfile -File $clearScript -SlotParkKey $repoKey -Reason 'r' -DataDir $data -Confirm:$false 2>&1
    Check '(e) a repo key is refused' $true ($LASTEXITCODE -ne 0)
    Check '(e) refusal says clone-check releases it' $true ("$out" -match 'clone-check releases it')
    $after = (Get-Content -LiteralPath $statePath -Raw -Encoding utf8) | ConvertFrom-Json -AsHashtable
    Check '(e) refused: repo park untouched' $true $after.ContainsKey($repoKey)

    $out = & pwsh -NoProfile -File $clearScript -SlotParkKey 'slot|spirrow-mindwire|T-none' -Reason 'r' -DataDir $data -Confirm:$false 2>&1
    Check '(e) an unknown key is an error, not a silent no-op' $true ($LASTEXITCODE -ne 0)
    $hist = @((Get-Content -LiteralPath $histPath -Raw -Encoding utf8) | ConvertFrom-Json)
    Check '(e) refusals add no history' 1 $hist.Count
} finally {
    Remove-Item -LiteralPath $data -Recurse -Force -ErrorAction SilentlyContinue
}

if ($script:failures -gt 0) { Write-Host "$($script:failures) failure(s)"; exit 1 }
Write-Host 'slot park key: all checks passed'
