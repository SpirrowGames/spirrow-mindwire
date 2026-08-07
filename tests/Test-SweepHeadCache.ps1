# Regression guard for the sweep's head-skip cache (deploy/run-conductor-scheduled.ps1).
#
# Why this file exists at all: the skip decision is the one place in the wrapper where being wrong is
# INVISIBLE. A cache that skips too little costs a cheap launch and says so in the log; a cache that
# skips too much parks live threads forever while reporting `no thread moved … nothing to do` at
# exit 0. That happened — measured 2026-08-06, 15+ minutes after both projects were released from
# `hold`, indistinguishable from a healthy idle loop until someone read `observed_state` by hand.
#
# The wrapper is PowerShell, so pytest cannot reach it. Rather than leave the decision untested (or
# leave the checks in a scratch file nobody runs), this is wired into `.mindwire-gate` — the single
# SOT that CI and the loop's own implementer both execute.
#
# The functions are lifted out of the script's AST instead of dot-sourced, because dot-sourcing would
# run the sweep: probe the chatroom, rewrite mindwire.toml and launch the conductor.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
if (-not (Test-Path -LiteralPath $sweepScript)) { throw "sweep script not found: $sweepScript" }

$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) {
    $parseErrors | ForEach-Object { Write-Host "PARSE ERROR line $($_.Extent.StartLineNumber): $($_.Message)" }
    throw "deploy/run-conductor-scheduled.ps1 does not parse"
}

# Get-JsonState logs through Write-Log on unreadable input; stub it so the lift stays self-contained.
function Write-Log { param([string]$Message) }

$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'Get-HeadRecord', 'Test-CanSkip', 'Get-ConductorVerdict', 'Get-JsonState', 'Save-JsonState') {
    $fn = $functions | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) {
        Write-Host ("  PASS  {0}" -f $Name)
    }
    else {
        $script:failures++
        Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual)
    }
}

Write-Host "Test-CanSkip — a skip needs BOTH an unchanged head and an unchanged control state"
# The regression itself: at an unchanged head, a naysayer->implementer handoff stops at the human
# gate under hold/supervised but dispatches under `run` (carve-out (3), conductor/core.py). Keying on
# the head alone skips exactly the threads a release from `hold` was meant to start.
Check "same head + same control -> skip" $true `
    (Test-CanSkip -ProbeHead 'msg-1' -KnownHead 'msg-1' -CurrentControl 'run' -KnownControl 'run')
Check "same head + hold -> run must LAUNCH" $false `
    (Test-CanSkip -ProbeHead 'msg-1' -KnownHead 'msg-1' -CurrentControl 'run' -KnownControl 'hold')
Check "same head + run -> supervised must LAUNCH" $false `
    (Test-CanSkip -ProbeHead 'msg-1' -KnownHead 'msg-1' -CurrentControl 'supervised' -KnownControl 'run')
Check "head moved + same control must LAUNCH" $false `
    (Test-CanSkip -ProbeHead 'msg-2' -KnownHead 'msg-1' -CurrentControl 'run' -KnownControl 'run')

Write-Host "Test-CanSkip — every unknown fails OPEN into a launch"
Check "control probe gap" $false `
    (Test-CanSkip -ProbeHead 'msg-1' -KnownHead 'msg-1' -CurrentControl $null -KnownControl 'run')
Check "record predates control tracking" $false `
    (Test-CanSkip -ProbeHead 'msg-1' -KnownHead 'msg-1' -CurrentControl 'run' -KnownControl $null)
Check "head probe gap" $false `
    (Test-CanSkip -ProbeHead $null -KnownHead 'msg-1' -CurrentControl 'run' -KnownControl 'run')
Check "thread never run before" $false `
    (Test-CanSkip -ProbeHead 'msg-1' -KnownHead $null -CurrentControl 'run' -KnownControl 'run')

Write-Host "Get-HeadRecord — reads both the current shape and pre-upgrade bare strings"
$legacy = @{ 'p/t' = 'msg-9' }
$r = Get-HeadRecord -State $legacy -Key 'p/t'
Check "legacy string yields its head" 'msg-9' $r.head
Check "legacy string yields unknown control" $null $r.control
$r = Get-HeadRecord -State @{} -Key 'absent'
Check "absent key yields unknown head" $null $r.head
Check "absent key yields unknown control" $null $r.control

Write-Host "Get-ConductorVerdict — 'last_msg=None' is absence, not a head called 'None'"
$v = Get-ConductorVerdict -Output @('conductor stopped: reason=hold rounds=0 forced_naysayer=0 last_msg=None')
Check "None becomes null" $null $v.last_msg
Check "reason still parsed" 'hold' $v.reason
$v = Get-ConductorVerdict -Output @('conductor stopped: reason=human rounds=17 forced_naysayer=0 last_msg=msg-2167')
Check "real id parsed" 'msg-2167' $v.last_msg
Check "rounds parsed" 17 $v.rounds

Write-Host "state file round-trip — a record survives save/load, and a mixed file is readable"
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("mindwire-headstate-" + [guid]::NewGuid().ToString('N') + ".json")
try {
    $state = @{}
    $state['proj/T-a'] = @{ head = 'msg-100'; control = 'run' }
    $state['proj/T-b'] = @{ head = 'msg-200'; control = $null }   # written when the probe was unreadable
    Save-JsonState -Path $tmp -State $state
    $loaded = Get-JsonState -Path $tmp
    $a = Get-HeadRecord -State $loaded -Key 'proj/T-a'
    Check "round-trip head" 'msg-100' $a.head
    Check "round-trip control" 'run' $a.control
    $b = Get-HeadRecord -State $loaded -Key 'proj/T-b'
    Check "round-trip keeps an unknown control unknown" $null $b.control
}
finally { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force } }

# The upgrade tick: a state file written by the previous version, read by this one.
$tmp2 = Join-Path ([System.IO.Path]::GetTempPath()) ("mindwire-headstate-" + [guid]::NewGuid().ToString('N') + ".json")
try {
    '{"proj/T-old":"msg-705","proj/T-new":{"head":"msg-2167","control":"run"}}' |
        Set-Content -LiteralPath $tmp2 -Encoding utf8
    $loaded = Get-JsonState -Path $tmp2
    $old = Get-HeadRecord -State $loaded -Key 'proj/T-old'
    Check "mixed file: legacy head" 'msg-705' $old.head
    Check "mixed file: legacy control unknown" $null $old.control
    $new = Get-HeadRecord -State $loaded -Key 'proj/T-new'
    Check "mixed file: new head" 'msg-2167' $new.head
    Check "mixed file: new control" 'run' $new.control
}
finally { if (Test-Path -LiteralPath $tmp2) { Remove-Item -LiteralPath $tmp2 -Force } }

Write-Host ""
if ($script:failures -gt 0) {
    Write-Host "sweep head-cache: $($script:failures) check(s) FAILED"
    exit 1
}
Write-Host "sweep head-cache: all checks passed"
exit 0
