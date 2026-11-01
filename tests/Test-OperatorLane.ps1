# Regression guard for the conductor tick's operator pass hook (T-operator-task-lane PR2, D1').
# Code under test: deploy/lib/OperatorLane.ps1 and its call site in deploy/run-conductor-scheduled.ps1.
#
#   E  Test-OperatorLaneEnabled — no Python start when the config is missing or says enabled=false.
#   C  ConvertTo-OperatorLaneCandidates — only probed (i.e. non-held) projects' threads become rows.
#   I  Invoke-OperatorLanePass — parses the one JSON line; a failed / non-JSON run is $null (fail-open
#      for the sweep); the candidates file is removed on every path.
#   P  Publish-OperatorLaneResult — every event is logged, `notify` events also notify.
#   S  The call site sits after the head probe and before the head-skip decide (pinned on the AST).

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$lib = Join-Path $repoRoot "deploy/lib/OperatorLane.ps1"
. $lib

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual) }
}

$script:logged = @()
$script:notified = @()
$script:confirmed = 0
function Write-Log { param([string]$m) $script:logged += $m }
function Confirm-LogWorthKeeping { $script:confirmed++ }
function Send-NotificationIfChanged { param($State, $Key, $Signature, $Message) $script:notified += $Message }

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("oplane-test-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    Write-Host "E  Test-OperatorLaneEnabled"
    $cfg = Join-Path $tmp 'operator_lane.json'
    Check 'missing config is off' $false (Test-OperatorLaneEnabled -ConfigPath $cfg)
    Set-Content -LiteralPath $cfg -Value '{"enabled": false}' -Encoding UTF8
    Check 'enabled=false is off' $false (Test-OperatorLaneEnabled -ConfigPath $cfg)
    Set-Content -LiteralPath $cfg -Value '{"enabled": true}' -Encoding UTF8
    Check 'enabled=true is on' $true (Test-OperatorLaneEnabled -ConfigPath $cfg)
    Set-Content -LiteralPath $cfg -Value '{not json' -Encoding UTF8
    Check 'malformed config goes to the pass (which reports it)' $true (Test-OperatorLaneEnabled -ConfigPath $cfg)

    Write-Host "C  ConvertTo-OperatorLaneCandidates"
    $cands = @(
        [pscustomobject]@{ project = 'spirrow-mindwire'; thread_id = 'T-a' },
        [pscustomobject]@{ project = 'spirrow-mindwire'; thread_id = 'T-gone' },
        [pscustomobject]@{ project = 'held-project'; thread_id = 'T-h' }
    )
    $rows = ConvertTo-OperatorLaneCandidates -Candidates $cands -HeadsByProject @{ 'spirrow-mindwire' = @{ 'T-a' = 'msg-9' } }
    Check 'one row' 1 @($rows).Count
    Check 'row thread' 'T-a' $rows[0].thread_id
    Check 'row head' 'msg-9' $rows[0].head_msg_id

    Write-Host "I  Invoke-OperatorLanePass"
    $script:files = @()
    $script:removed = @()
    $newFile = { param($j, $l) $p = Join-Path $tmp ("in-" + [guid]::NewGuid().ToString('N') + '.json'); Set-Content -LiteralPath $p -Value $j; $script:files += $p; $p }
    $removeFile = { param($f) Remove-Item -LiteralPath $f -Force; $script:removed += $f }
    $script:seenArgs = $null
    $ok = { param($a, $t, $l) $script:seenArgs = $a; @{ ok = $true; code = 0; stdout = "noise`n{`"action`": `"dispatched`", `"events`": [{`"level`": `"notify`", `"key`": `"k`", `"message`": `"hello`"}]}`n"; stderr = '' } }
    $res = Invoke-OperatorLanePass -Rows $rows -DataDir 'D:\data' -HeadSkipPath 'D:\data\state\head_skip.json' -Invoke $ok -NewInputFile $newFile -RemoveInputFile $removeFile -WrapperPid 4321
    Check 'action parsed' 'dispatched' $res.action
    Check 'wrapper pid passed' '4321' $script:seenArgs[$script:seenArgs.IndexOf('--wrapper-pid') + 1]
    Check 'module invoked' 'spirrow_mindwire.operator_lane' $script:seenArgs[1]
    Check 'input file removed' 1 @($script:removed).Count
    $bad = { param($a, $t, $l) @{ ok = $false; code = $null; stdout = ''; stderr = ''; error = 'timed out' } }
    $res2 = Invoke-OperatorLanePass -Rows $rows -DataDir 'D:\data' -HeadSkipPath 'x' -Invoke $bad -NewInputFile $newFile -RemoveInputFile $removeFile
    Check 'failed pass is null' $true ($null -eq $res2)
    Check 'input file removed on failure' 2 @($script:removed).Count
    $noJson = { param($a, $t, $l) @{ ok = $true; code = 0; stdout = 'Traceback ...'; stderr = '' } }
    Check 'no JSON is null' $true ($null -eq (Invoke-OperatorLanePass -Rows $rows -DataDir 'D' -HeadSkipPath 'x' -Invoke $noJson -NewInputFile $newFile -RemoveInputFile $removeFile))
    $throws = { param($a, $t, $l) throw 'boom' }
    Check 'throwing invoke is null' $true ($null -eq (Invoke-OperatorLanePass -Rows $rows -DataDir 'D' -HeadSkipPath 'x' -Invoke $throws -NewInputFile $newFile -RemoveInputFile $removeFile))
    Check 'every input file removed' 4 @($script:removed).Count

    Write-Host "P  Publish-OperatorLaneResult"
    $script:logged = @(); $script:notified = @(); $script:confirmed = 0
    Publish-OperatorLaneResult -Result $res -NotifyState @{}
    Check 'notified once' 1 @($script:notified).Count
    Check 'notification text' 'MindWire: hello' $script:notified[0]
    Check 'kept the log' 1 $script:confirmed
    $script:notified = @(); $script:confirmed = 0
    Publish-OperatorLaneResult -Result ([pscustomobject]@{ action = 'idle'; events = @() }) -NotifyState @{}
    Check 'idle notifies nothing' 0 @($script:notified).Count
    Check 'idle keeps no log' 0 $script:confirmed
    Publish-OperatorLaneResult -Result $null -NotifyState @{}

    Write-Host "S  call site order"
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
    if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }
    $calls = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)
    # Function bodies come first in the file; compare the run-section call sites.
    $runStart = $ast.Extent.Text.IndexOf('# --- run ---')
    $probeRun = ($calls | Where-Object { $_.GetCommandName() -eq 'Invoke-HeadProbe' -and $_.Extent.StartOffset -gt $runStart } | Select-Object -First 1).Extent.StartOffset
    $pass = ($calls | Where-Object { $_.GetCommandName() -eq 'Invoke-OperatorLanePass' } | Select-Object -First 1).Extent.StartOffset
    $decideRun = ($calls | Where-Object { $_.GetCommandName() -eq 'Invoke-HeadSkipDecide' -and $_.Extent.StartOffset -gt $runStart } | Select-Object -First 1).Extent.StartOffset
    Check 'pass is called in the run section' $true ($pass -gt $runStart)
    Check 'pass after the head probe' $true ($pass -gt $probeRun)
    Check 'pass before the head-skip decide' $true ($pass -lt $decideRun)
    Check 'lib is dot-sourced' $true ($ast.Extent.Text.Contains(". (Join-Path `$PSScriptRoot 'lib/OperatorLane.ps1')"))
}
finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

if ($script:failures -gt 0) { Write-Host "$script:failures FAILURE(S)"; exit 1 }
Write-Host "all passed"
exit 0
