# Regression guard for the slot-mode exit-7 park (deploy/run-conductor-scheduled.ps1):
# Get-SlotKillUnconfirmedPark and Complete-SlotThreadPark.
#
# T-ephemeral-impl-clone-slot-per-thread, Bohr msg-8246 (Obj-19-1) and step 2b of msg-8499; writer
# order and best-effort D-7 row from msg-8236 (Obj-18-1):
#   - slot mode + conductor exit 7 -> park THIS thread with clone_slot_session_unknown, detail
#     `kill-unconfirmed (exit=7)`, the slot left where it is; the notice says the slot was not moved
#     and to check for leftover mindwire-loop / claude processes before unparking;
#   - OFF (INV-0) -> nothing (exit 7 unchanged); any other exit code -> nothing;
#   - the park and notice stand even when the D-7 row cannot be written, and the row is still
#     written when the notifier throws; a missing state dir is not created.
#
# The functions are lifted out of the sweep script's AST (dot-sourcing would run the sweep).

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }

foreach ($var in '$ConductorKillUnconfirmedExitCode', '$SlotKillUnconfirmedParkCode', '$SlotKillUnconfirmedDetail') {
    $assign = $ast.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $n.Left.Extent.Text -eq $var }, $true) | Select-Object -First 1
    if (-not $assign) { throw "$var not assigned in sweep script" }
    Invoke-Expression $assign.Extent.Text
}
foreach ($name in 'Get-SlotKillUnconfirmedPark', 'Complete-SlotThreadPark', 'Set-DirtyCloneParked') {
    $fn = $ast.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $n.Name -eq $name }, $true) | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}

$script:logLines = [System.Collections.Generic.List[string]]::new()
function Write-Log { param([string]$Message) $script:logLines.Add($Message) }
$script:sent = [System.Collections.Generic.List[hashtable]]::new()
$script:sendThrows = $false
function Send-NotificationIfChanged {
    param([hashtable]$State, [string]$Key, [string]$Signature, [string]$Message)
    if ($script:sendThrows) { throw 'webhook exploded' }
    $script:sent.Add(@{ key = $Key; signature = $Signature; message = $Message })
}

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual) }
}

$cand = 'spirrow-mindwire/T-demo'
$slot = 'C:\slots\spirrow-mindwire\de9405c2c959'

# --- the verdict ---------------------------------------------------------------------------------------
Check 'exit-7 constant is the budget kill-unconfirmed code' 7 $ConductorKillUnconfirmedExitCode
Check 'park code is the existing clone_slot.PARK_SESSION_UNKNOWN (no new code)' 'clone_slot_session_unknown' $SlotKillUnconfirmedParkCode

$v = Get-SlotKillUnconfirmedPark -ExitCode 7 -SlotMode $false -CandidateKey $cand -SlotDir $slot
Check 'OFF (INV-0): exit 7 yields no slot park' $null $v

foreach ($c in 0, 1, 3, 4, 5, 6, 8, 11) {
    $v = Get-SlotKillUnconfirmedPark -ExitCode $c -SlotMode $true -CandidateKey $cand -SlotDir $slot
    Check "slot mode: exit $c yields no slot park" $null $v
}

$v = Get-SlotKillUnconfirmedPark -ExitCode 7 -SlotMode $true -CandidateKey $cand -SlotDir $slot
Check 'slot mode + exit 7 -> park' 'park' $v.action
Check 'park code' 'clone_slot_session_unknown' $v.park_code
Check 'detail' 'kill-unconfirmed (exit=7)' $v.detail
Check 'not a prep crash' $false $v.crashed
Check 'slot_dir is the slot (left where it is)' $slot $v.slot_dir
Check 'notice key is per candidate' "__clone_slot_park__/$cand" $v.notice_key
Check 'notice names the candidate' $true ($v.message.Contains($cand))
Check 'notice says the slot was not moved' $true ($v.message.Contains('slot は移動も変更もしていません'))
Check 'notice names the slot path' $true ($v.message.Contains($slot))
Check 'notice says to check mindwire-loop before unpark' $true ($v.message.Contains('mindwire-loop'))
Check 'notice says to check claude before unpark' $true ($v.message.Contains('claude のプロセス'))
Check 'notice says unpark' $true ($v.message.Contains('unpark'))

$work = Join-Path ([System.IO.Path]::GetTempPath()) ("slot-ku-park-test-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    $now = '2026-11-16T08:00:00.0000000Z'
    $log = Join-Path $work 'clone-retreats.jsonl'
    $key = 'slot-key-supplied-by-caller'

    # --- happy path: park, notice, one D-7 row --------------------------------------------------------
    $parked = @{}
    $r = Complete-SlotThreadPark -Parked $parked -ParkKey $key -Verdict $v -NowIso $now -NotifyState @{} `
        -LogPath $log -ThreadId 'T-demo' -Repo 'spirrow-mindwire'
    Check 'parked' $true $r.parked
    Check 'notified' $true $r.notified
    Check 'row written' $true $r.row_written
    Check 'park stored under the key the caller passed' $true $parked.ContainsKey($key)
    Check 'park reason is the park code' 'clone_slot_session_unknown' $parked[$key].reason
    Check 'park detail' 'kill-unconfirmed (exit=7)' $parked[$key].detail
    Check 'park keeps the slot path' $slot $parked[$key].repo_dir
    Check 'park since' $now $parked[$key].since
    Check 'one notice' 1 $script:sent.Count
    Check 'notice key' "__clone_slot_park__/$cand" $script:sent[0].key
    $lines = @(Get-Content -LiteralPath $log -Encoding utf8)
    Check 'one row' 1 $lines.Count
    $row = $lines[0] | ConvertFrom-Json
    Check 'row kind' 'slot_quarantine' $row.kind
    Check 'row action' 'parked' $row.action
    Check 'row quarantine_reason (msg-8236)' 'provision_failed' $row.quarantine_reason
    Check 'row park_code' 'clone_slot_session_unknown' $row.park_code
    Check 'row detail' 'kill-unconfirmed (exit=7)' $row.detail
    Check 'row slot_path' $slot $row.slot_path
    Check 'row thread_id' 'T-demo' $row.thread_id
    Check 'row repo' 'spirrow-mindwire' $row.repo
    Check 'row quarantine_path is null' $null $row.quarantine_path
    Check 'row patch is null' $null $row.patch
    Check 'row bundle is null' $null $row.bundle
    # Raw text: ConvertFrom-Json would turn the stamp into a local DateTime.
    Check 'row at is UTC iso (as clone_retreat_log writes it)' $true ($lines[0].Contains('"at":"2026-11-16T08:00:00.000000+00:00"'))
    Check 'row record_id prefix' $true ("$($row.record_id)".StartsWith('T-demo@2026-11-16T08:00:00.000000+00:00#'))
    $bytes = [System.IO.File]::ReadAllBytes($log)
    Check 'file ends with LF' 10 $bytes[$bytes.Length - 1]
    Check 'no BOM' $true ($bytes[0] -ne 0xEF)
    Check 'no CR' $false ($bytes -contains 13)

    # --- a torn earlier line stays its own line -------------------------------------------------------
    $torn = Join-Path $work 'torn.jsonl'
    [System.IO.File]::WriteAllText($torn, '{"kind": "retr', [System.Text.UTF8Encoding]::new($false))
    $r = Complete-SlotThreadPark -Parked @{} -ParkKey $key -Verdict $v -NowIso $now -NotifyState @{} `
        -LogPath $torn -ThreadId 'T-demo' -Repo 'spirrow-mindwire'
    $tl = @(Get-Content -LiteralPath $torn -Encoding utf8)
    Check 'torn file: row appended on its own line' 2 $tl.Count
    Check 'torn file: new row parses' 'slot_quarantine' (($tl[1] | ConvertFrom-Json).kind)

    # --- row cannot be written: park and notice stand, state dir not created ---------------------------
    $script:sent.Clear(); $script:logLines.Clear()
    $missingDir = Join-Path $work 'no-state'
    $parked = @{}
    $r = Complete-SlotThreadPark -Parked $parked -ParkKey $key -Verdict $v -NowIso $now -NotifyState @{} `
        -LogPath (Join-Path $missingDir 'clone-retreats.jsonl') -ThreadId 'T-demo' -Repo 'spirrow-mindwire'
    Check 'missing state dir -> row not written' $false $r.row_written
    Check 'missing state dir -> still parked' $true $parked.ContainsKey($key)
    Check 'missing state dir -> notice still sent' 1 $script:sent.Count
    Check 'missing state dir -> not created' $false (Test-Path -LiteralPath $missingDir)
    Check 'missing row is logged' $true (@($script:logLines | Where-Object { $_ -like '*D-7 row not written*' }).Count -eq 1)

    $asDir = Join-Path $work 'log-is-a-dir.jsonl'
    New-Item -ItemType Directory -Path $asDir | Out-Null
    $parked = @{}
    $r = Complete-SlotThreadPark -Parked $parked -ParkKey $key -Verdict $v -NowIso $now -NotifyState @{} `
        -LogPath $asDir -ThreadId 'T-demo' -Repo 'spirrow-mindwire'
    Check 'unwritable log -> row not written' $false $r.row_written
    Check 'unwritable log -> still parked' $true $parked.ContainsKey($key)

    # --- notifier throws: park and row stand ---------------------------------------------------------
    $script:sendThrows = $true; $script:logLines.Clear()
    $log2 = Join-Path $work 'notify-throws.jsonl'
    $parked = @{}
    $r = Complete-SlotThreadPark -Parked $parked -ParkKey $key -Verdict $v -NowIso $now -NotifyState @{} `
        -LogPath $log2 -ThreadId 'T-demo' -Repo 'spirrow-mindwire'
    Check 'notifier throws -> not notified' $false $r.notified
    Check 'notifier throws -> still parked' $true $parked.ContainsKey($key)
    Check 'notifier throws -> row still written' $true $r.row_written
    Check 'notifier throws -> logged' $true (@($script:logLines | Where-Object { $_ -like '*notice could not be sent*' }).Count -eq 1)
    $script:sendThrows = $false

    # --- since is kept on a re-park ------------------------------------------------------------------
    $parked = @{ $key = @{ since = '2026-11-01T00:00:00.0000000Z' } }
    $null = Complete-SlotThreadPark -Parked $parked -ParkKey $key -Verdict $v -NowIso $now -NotifyState @{} `
        -LogPath $log2 -ThreadId 'T-demo' -Repo 'spirrow-mindwire'
    Check 're-park keeps the first since' '2026-11-01T00:00:00.0000000Z' $parked[$key].since
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}

if ($script:failures -gt 0) { Write-Host "$($script:failures) failure(s)"; exit 1 }
Write-Host "all passed"
