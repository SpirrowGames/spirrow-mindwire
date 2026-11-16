# Regression guard for dirty-clones.json under its short lock (step 2e, T-ephemeral-impl-clone-slot-
# per-thread; Bohr msg-9328 / msg-9330, Obj-25-1 / 26-1 / 26-2, Einstein go after msg-9330):
# Update-DirtyClonesStateKey (the tick side) and deploy/Clear-Quarantine.ps1 -SlotParkKey (the
# person side) both read again -> change ONE key -> save inside state\dirty-clones.json.lock.
#   (f) a key a person removed mid-tick stays removed after the tick saves another change;
#   (g) a park the tick adds and one it removes in the same tick both reach the file;
#   (i) the person holds the lock while the tick saves another key: the tick waits, nothing is lost;
#   (j) the reverse: the tick holds it while the person unparks: the unpark waits, nothing is lost;
#   (k) same key: unpark then new park -> parked; park then unpark -> unparked (save order);
#   (l) lock held: the tick waits without saving or going on, logs every 30 s, notices once past
#       60 s, saves once free; the person side fails within its timeout, state and history unchanged;
#   (m) Obj-26-2 order: tick finds a failure -> lock held -> person removes the same key -> tick saves:
#       the new park stands; there is no deferred re-save in the function at all.
# A state file that cannot be read is never overwritten by either side.
# The process-to-process blocks need Windows share semantics (the wrapper runs only under Task
# Scheduler); elsewhere they are reported as SKIP, the rest runs everywhere. (h), OFF unchanged, is
# pinned where the sweep loop is wired: OFF never calls this function.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$clearScript = Join-Path $repoRoot "deploy/Clear-Quarantine.ps1"
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }
foreach ($var in '$SlotParkKeyPrefix', '$DirtyClonesLockLogSeconds', '$DirtyClonesLockNoticeSeconds', '$DirtyClonesLockNoticeKey') {
    $assign = $ast.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $n.Left.Extent.Text -eq $var }, $true) | Select-Object -First 1
    if (-not $assign) { throw "$var not assigned in sweep script" }
    Invoke-Expression $assign.Extent.Text
}
$functions = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'ConvertTo-DirtyCloneRepoKey', 'Get-DirtyCloneParkKeyKind', 'ConvertTo-DirtyCloneParking',
                  'Read-DirtyClonesStateForUpdate', 'Write-DirtyClonesStateAtomic', 'Update-DirtyClonesStateKey') {
    $fn = $functions | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}

$script:logLines = [System.Collections.Generic.List[string]]::new()
function Write-Log { param([string]$Message) $script:logLines.Add($Message) }
$script:notices = [System.Collections.Generic.List[hashtable]]::new()
function Send-NotificationIfChanged {
    param([hashtable]$State, [string]$Key, [string]$Signature, [string]$Message)
    $script:notices.Add(@{ key = $Key; signature = $Signature; message = $Message })
}

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual) }
}
function Skip { param([string]$Name) Write-Host ("  SKIP  {0} (needs Windows file-share semantics)" -f $Name) }
$onWindows = $IsWindows -or ($PSVersionTable.PSEdition -eq 'Desktop')

function Row { param([string]$Reason, [string]$Dir = 'C:\slots\x') return @{ repo_dir = $Dir; reason = $Reason; head = ''; detail = 'd'; since = 's'; signature = "sig:$Reason" } }
function New-Fixture {
    $data = Join-Path ([System.IO.Path]::GetTempPath()) ("dirty-clones-lock-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path (Join-Path $data 'state') -Force | Out-Null
    $script:data = $data
    $script:statePath = Join-Path $data 'state\dirty-clones.json'
    $script:histPath = Join-Path $data 'state\quarantine-history.json'
    $script:lockPath = "$($script:statePath).lock"
}
function Set-File { param([hashtable]$State) Write-DirtyClonesStateAtomic -StatePath $script:statePath -State $State }
function Get-File { return (Read-DirtyClonesStateForUpdate -StatePath $script:statePath).state }
function Hold-Lock { return [System.IO.FileStream]::new($script:lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None) }
function Invoke-Unpark {
    param([string]$Key, [int]$Timeout = 10)
    $out = & pwsh -NoProfile -File $clearScript -SlotParkKey $Key -Reason 'checked' -DataDir $script:data -LockTimeoutSeconds $Timeout -Confirm:$false 2>&1
    return @{ code = $LASTEXITCODE; out = "$out" }
}
function Start-Unpark {
    param([string]$Key)
    $psi = [System.Diagnostics.ProcessStartInfo]::new('pwsh')
    foreach ($a in @('-NoProfile', '-File', $clearScript, '-SlotParkKey', $Key, '-Reason', 'checked', '-DataDir', $script:data, '-LockTimeoutSeconds', '30', '-Confirm:$false')) { $psi.ArgumentList.Add($a) }
    $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    return [System.Diagnostics.Process]::Start($psi)
}
$noWait = { param($ms) }

$kA = 'slot|spirrow-mindwire|T-a'; $kC = 'slot|spirrow-mindwire|T-c'; $kE = 'slot|spirrow-mindwire|T-e'

try {
    # --- (f) a person's mid-tick unpark is not written back ----------------------------------------
    New-Fixture
    Set-File @{ $kA = (Row 'clone_slot_session_unknown'); $kC = (Row 'clone_slot_provision_failed') }
    $parked = ConvertTo-DirtyCloneParking -State (Get-File)          # the tick loads at its start
    $r = Invoke-Unpark -Key $kA                                      # a person unparks kA mid-tick
    Check '(f) unpark ran' 0 $r.code
    $null = Update-DirtyClonesStateKey -StatePath $statePath -Key $kE -Row (Row 'clone_slot_prep_crashed') -Parked $parked -NotifyState @{} -Wait $noWait
    $f = Get-File
    Check '(f) the unparked key stays removed after the tick saves' $false $f.ContainsKey($kA)
    Check '(f) the tick''s new park is saved' $true $f.ContainsKey($kE)
    Check '(f) the untouched park stays' $true $f.ContainsKey($kC)
    Check '(f) the tick''s in-memory map now matches the file (no kA)' $false $parked.ContainsKey($kA)
    Check '(f) the tick''s in-memory map has its new park' $true $parked.ContainsKey($kE)

    # --- (g) add and remove in one tick -------------------------------------------------------------
    $null = Update-DirtyClonesStateKey -StatePath $statePath -Key $kA -Row (Row 'clone_slot_session_unknown') -Parked $parked -NotifyState @{} -Wait $noWait
    $null = Update-DirtyClonesStateKey -StatePath $statePath -Key $kC -Remove -Parked $parked -NotifyState @{} -Wait $noWait
    $g = Get-File
    Check '(g) added park in the file' $true $g.ContainsKey($kA)
    Check '(g) removed park gone from the file' $false $g.ContainsKey($kC)
    Check '(g) earlier park kept' $true $g.ContainsKey($kE)
    Check '(g) reason as set' 'clone_slot_session_unknown' $g[$kA].reason
    Check '(g) no temp file left' $false (Test-Path -LiteralPath "$statePath.tmp")

    # --- (k) same key, order of the saves decides ---------------------------------------------------
    $r = Invoke-Unpark -Key $kA
    $null = Update-DirtyClonesStateKey -StatePath $statePath -Key $kA -Row (Row 'clone_slot_session_unknown') -Parked $parked -NotifyState @{} -Wait $noWait
    Check '(k) unpark then new park -> parked' $true (Get-File).ContainsKey($kA)
    $r = Invoke-Unpark -Key $kA
    Check '(k) park then unpark -> unparked' $false (Get-File).ContainsKey($kA)

    # --- unreadable file: never overwritten by either side ------------------------------------------
    [System.IO.File]::WriteAllText($statePath, '{"slot|x|y": {"repo_dir": ', [System.Text.UTF8Encoding]::new($false))
    $r = Invoke-Unpark -Key $kE -Timeout 2
    Check 'unreadable: unpark fails' $true ($r.code -ne 0)
    Check 'unreadable: unpark says nothing changed' $true ($r.out -match 'nothing changed')
    Check 'unreadable: file untouched by the unpark' '{"slot|x|y": {"repo_dir": ' ([System.IO.File]::ReadAllText($statePath))
    $script:seen = @(); $script:tries = 0
    $fixLater = { $script:tries++; $script:seen += [System.IO.File]::ReadAllText($statePath)
                  if ($script:tries -eq 3) { Set-File @{ $kC = (Row 'clone_slot_provision_failed') } } }
    $res = Update-DirtyClonesStateKey -StatePath $statePath -Key $kE -Row (Row 'x') -Parked @{} -NotifyState @{} -Wait $noWait -OnWait $fixLater
    Check 'unreadable: the tick waited instead of writing' 3 $script:tries
    Check 'unreadable: file never overwritten while unreadable' 0 @($script:seen | Where-Object { $_ -ne '{"slot|x|y": {"repo_dir": ' } | Select-Object -First 2).Count
    $u = Get-File
    Check 'unreadable: saved once readable, other park kept' $true ($u.ContainsKey($kC) -and $u.ContainsKey($kE))

    # --- (l) the tick waits on a held lock; logs, one notice, then saves ----------------------------
    if ($onWindows) {
        Set-File @{ $kC = (Row 'clone_slot_provision_failed') }
        $before = [System.IO.File]::ReadAllText($statePath)
        $script:logLines.Clear(); $script:notices.Clear()
        $script:clock = [datetime]'2026-11-16T08:00:00Z'
        $script:held = Hold-Lock
        $script:unchangedWhileHeld = $true
        $tick = { $script:clock = $script:clock.AddSeconds(10)
                  if ([System.IO.File]::ReadAllText($statePath) -ne $before) { $script:unchangedWhileHeld = $false }
                  if ($script:clock -ge ([datetime]'2026-11-16T08:01:15Z') -and $null -ne $script:held) { $script:held.Dispose(); $script:held = $null } }
        $res = Update-DirtyClonesStateKey -StatePath $statePath -Key $kE -Row (Row 'clone_slot_session_unknown') -Parked @{} -NotifyState @{} `
            -Now { $script:clock } -Wait $noWait -OnWait $tick
        Check '(l) nothing saved while the lock was held' $true $script:unchangedWhileHeld
        Check '(l) saved once the lock was free' $true (Get-File).ContainsKey($kE)
        Check '(l) the wait is reported in the result' $true ($res.waited_seconds -ge 70)
        Check '(l) a waiting log line at 30 s and at 60 s' 2 @($script:logLines | Where-Object { $_ -like 'dirty-clones.json: waiting *' }).Count
        Check '(l) exactly one notice past 60 s' 1 $script:notices.Count
        Check '(l) notice key' $DirtyClonesLockNoticeKey $script:notices[0].key
        Check '(l) notice names the lock and Clear-Quarantine' $true ($script:notices[0].message -match 'dirty-clones.json.lock' -and $script:notices[0].message -match 'Clear-Quarantine.ps1')
        Check '(l) the save after a long wait is logged' 1 @($script:logLines | Where-Object { $_ -like 'dirty-clones.json saved after waiting*' }).Count

        $histBefore = if (Test-Path -LiteralPath $histPath) { [System.IO.File]::ReadAllText($histPath) } else { '' }
        $stateBefore = [System.IO.File]::ReadAllText($statePath)
        $h = Hold-Lock
        try { $r = Invoke-Unpark -Key $kE -Timeout 2 } finally { $h.Dispose() }
        Check '(l) person side: fails while the lock is held' $true ($r.code -ne 0)
        Check '(l) person side: says to run it again' $true ($r.out -match 'run this again')
        Check '(l) person side: state unchanged' $stateBefore ([System.IO.File]::ReadAllText($statePath))
        $histAfter = if (Test-Path -LiteralPath $histPath) { [System.IO.File]::ReadAllText($histPath) } else { '' }
        Check '(l) person side: history unchanged' $histBefore $histAfter
    } else { Skip '(l) held lock' }

    # --- (i) person holds the lock; the tick saves another key after it ------------------------------
    if ($onWindows) {
        Set-File @{ $kA = (Row 'clone_slot_session_unknown'); $kC = (Row 'clone_slot_provision_failed') }
        $script:held = Hold-Lock
        $script:waits = 0
        $person = { $script:waits++
                    if ($null -ne $script:held) {   # the person, inside the lock: read again, drop kA, save, release
                        $s = Get-File; [void]$s.Remove($kA); Set-File $s
                        $script:held.Dispose(); $script:held = $null } }
        $null = Update-DirtyClonesStateKey -StatePath $statePath -Key $kE -Row (Row 'clone_slot_session_unknown') -Parked @{} -NotifyState @{} -Wait $noWait -OnWait $person
        $i = Get-File
        Check '(i) the tick waited for the person' $true ($script:waits -ge 1)
        Check '(i) the person''s unpark stands' $false $i.ContainsKey($kA)
        Check '(i) the tick''s new park is not lost' $true $i.ContainsKey($kE)
        Check '(i) the other park stays' $true $i.ContainsKey($kC)
    } else { Skip '(i) person holds the lock' }

    # --- (j) tick holds the lock; the person's unpark waits and lands after ---------------------------
    if ($onWindows) {
        Set-File @{ $kA = (Row 'clone_slot_session_unknown'); $kC = (Row 'clone_slot_provision_failed') }
        $h = Hold-Lock                                  # the tick is inside its read-change-save
        $proc = Start-Unpark -Key $kA
        Start-Sleep -Milliseconds 1500
        $s = Get-File; $s[$kE] = (Row 'clone_slot_session_unknown'); Set-File $s
        $stillThere = (Get-File).ContainsKey($kA)
        $h.Dispose()
        $proc.WaitForExit(30000) | Out-Null
        $j = Get-File
        Check '(j) the unpark had to wait (kA still there while the tick held the lock)' $true $stillThere
        Check '(j) unpark succeeded after the tick' 0 $proc.ExitCode
        Check '(j) the unpark stands' $false $j.ContainsKey($kA)
        Check '(j) the tick''s park is not lost' $true $j.ContainsKey($kE)
    } else { Skip '(j) tick holds the lock' }

    # --- (m) Obj-26-2 order on one key ---------------------------------------------------------------
    if ($onWindows) {
        Set-File @{ $kA = (Row 'clone_slot_provision_failed') }      # an OLD park on kA
        $script:held = Hold-Lock                                     # the tick found a new failure; the lock is busy
        $personSameKey = { if ($null -ne $script:held) { $s = Get-File; [void]$s.Remove($kA); Set-File $s; $script:held.Dispose(); $script:held = $null } }
        $null = Update-DirtyClonesStateKey -StatePath $statePath -Key $kA -Row (Row 'clone_slot_session_unknown') -Parked @{} -NotifyState @{} -Wait $noWait -OnWait $personSameKey
        $m = Get-File
        Check '(m) the new failure saved after the person''s unpark stands' $true $m.ContainsKey($kA)
        Check '(m) it is the NEW park, not the old one' 'clone_slot_session_unknown' $m[$kA].reason
    } else { Skip '(m) same key behind a held lock' }
    $fnText = ($functions | Where-Object { $_.Name -eq 'Update-DirtyClonesStateKey' } | Select-Object -First 1).Extent.Text
    Check '(m) no deferred / save-later path: the only exit is a completed save' 1 ([regex]::Matches($fnText, '\breturn\b').Count)
} finally {
    if ($null -ne $script:held) { $script:held.Dispose() }
    if ($script:data) { Remove-Item -LiteralPath $script:data -Recurse -Force -ErrorAction SilentlyContinue }
}

if ($script:failures -gt 0) { Write-Host "$($script:failures) failure(s)"; exit 1 }
Write-Host 'dirty-clones lock: all checks passed'
