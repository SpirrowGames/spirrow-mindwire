# Regression guard for Get-SlotPrepVerdict (deploy/run-conductor-scheduled.ps1).
#
# T-ephemeral-impl-clone-slot-per-thread, Bohr msg-8236 (Obj-18-1, Einstein go): `mindwire
# slot-prep` exit 1, or any code it does not define, parks the thread on THAT tick with
# clone_slot_prep_crashed (detail = exit code + first stderr line); there is no crash counter, and
# a crash never yields a directory to launch in (no fallback to the shared repo_dir).
#
# The function is lifted out of the sweep script's AST (dot-sourcing would run the sweep).

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }

$assign = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $n.Left.Extent.Text -eq '$SlotPrepCrashedParkCode' }, $true) | Select-Object -First 1
if (-not $assign) { throw 'SlotPrepCrashedParkCode not assigned in sweep script' }
Invoke-Expression $assign.Extent.Text
$fn = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $n.Name -eq 'Get-SlotPrepVerdict' }, $true) | Select-Object -First 1
if (-not $fn) { throw 'function not found in sweep script: Get-SlotPrepVerdict' }
Invoke-Expression $fn.Extent.Text

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual) }
}

function Row { param([hashtable]$P) return 'MINDWIRE_SLOT_PREP_PAYLOAD ' + ($P | ConvertTo-Json -Compress -Depth 5) }

Check 'crash park code matches clone_slot.PARK_PREP_CRASHED' 'clone_slot_prep_crashed' $SlotPrepCrashedParkCode

# --- decided runs -----------------------------------------------------------------------------------
$slot = 'C:\slots\demo\de9405c2c959'
$v = Get-SlotPrepVerdict -ExitCode 0 -Output @('noise', (Row @{ action = 'launch'; slot_dir = $slot; detail = 'ok' })) -Stderr @()
Check 'exit 0 + launch payload -> launch' 'launch' $v.action
Check 'launch carries the slot_dir' $slot $v.slot_dir
Check 'launch is not a crash' $false $v.crashed

$v = Get-SlotPrepVerdict -ExitCode 3 -Output @((Row @{ action = 'skip'; slot_dir = $slot; detail = 'live session' })) -Stderr @()
Check 'exit 3 -> skip' 'skip' $v.action

$v = Get-SlotPrepVerdict -ExitCode 4 -Output @((Row @{ action = 'park'; slot_dir = $slot; park_code = 'clone_slot_index_lock_stale'; detail = 'd' })) -Stderr @()
Check 'exit 4 -> park' 'park' $v.action
Check 'exit 4 keeps the procedure park code' 'clone_slot_index_lock_stale' $v.park_code
Check 'exit 4 is not a crash' $false $v.crashed

# --- crashes: parked on the first tick, never a directory to launch in ------------------------------
$v = Get-SlotPrepVerdict -ExitCode 1 -Output @() -Stderr @('', 'slot-prep: cannot prepare demo/T-x: RuntimeError: boom', 'more')
Check 'exit 1 -> park on this tick' 'park' $v.action
Check 'exit 1 park code' 'clone_slot_prep_crashed' $v.park_code
Check 'exit 1 is a crash' $true $v.crashed
Check 'exit 1 yields no slot_dir (no fallback to the shared repo_dir)' $null $v.slot_dir
Check 'exit 1 detail = exit code + first non-blank stderr line' 'slot-prep exit=1: slot-prep: cannot prepare demo/T-x: RuntimeError: boom' $v.detail

foreach ($code in 2, 5, 8, 137, -1) {
    $v = Get-SlotPrepVerdict -ExitCode $code -Output @((Row @{ action = 'launch'; slot_dir = $slot })) -Stderr @()
    Check "undefined exit $code -> crash park" 'clone_slot_prep_crashed' $v.park_code
    Check "undefined exit $code yields no slot_dir" $null $v.slot_dir
}

$v = Get-SlotPrepVerdict -ExitCode 0 -Output @('MINDWIRE_SLOT_PREP_PAYLOAD {not json') -Stderr @()
Check 'exit 0 with an unreadable payload -> crash park' 'clone_slot_prep_crashed' $v.park_code
Check 'unreadable payload yields no slot_dir' $null $v.slot_dir

$v = Get-SlotPrepVerdict -ExitCode 0 -Output @((Row @{ action = 'launch' })) -Stderr @()
Check 'exit 0 without slot_dir -> crash park' 'clone_slot_prep_crashed' $v.park_code

$v = Get-SlotPrepVerdict -ExitCode 0 -Output @((Row @{ action = 'park'; slot_dir = $slot; park_code = 'x' })) -Stderr @()
Check 'exit/action mismatch -> crash park' 'clone_slot_prep_crashed' $v.park_code

$v = Get-SlotPrepVerdict -ExitCode 4 -Output @((Row @{ action = 'park'; slot_dir = $slot })) -Stderr @()
Check 'exit 4 without park_code -> crash park' 'clone_slot_prep_crashed' $v.park_code

$v = Get-SlotPrepVerdict -ExitCode 1 -Output @() -Stderr @()
Check 'exit 1 with empty stderr still has a detail' 'slot-prep exit=1: -' $v.detail

# --- no crash counter: the verdict is a pure function of this run ------------------------------------
$fnText = $fn.Extent.Text
Check 'Get-SlotPrepVerdict writes no state file' $false ($fnText -match 'Set-Content|Out-File|Add-Content|WriteAll')

if ($script:failures -gt 0) { Write-Host "$($script:failures) failure(s)"; exit 1 }
Write-Host 'all passed'
