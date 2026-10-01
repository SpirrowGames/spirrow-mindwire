# Regression guard for T-parked-humans-probe-has-no-timeout (final spec: Bohr msg-5414).
#
# On 2026-10-01 `$payload | & uv run python scripts/parked_humans.py` sat at 0 CPU / 0 TCP for 20+
# minutes with no upper bound, the wrapper never returned, and every project's conductor stopped
# for ~77 minutes. This file pins the fix in deploy/run-conductor-scheduled.ps1:
#
#   H  — Invoke-BoundedUvProbe (the helper): a hung probe times out, the WHOLE tree (root and
#        grandchild) is dead when it returns, killConfirmed is reported; stdin is closed at start
#        (a probe that reads stdin sees EOF instead of hanging); exit codes pass through.
#   P  — Invoke-ParkedHumansProbe: payload via `--input <tmp>`; on timeout fail-closed with an
#        `errors[0]` row whose reason says "timed out"; the temp file is gone on both paths.
#   S  — head-skip decide / commit-launch / commit-terminal: `--candidates` / `--payload-file`,
#        never inline `--payload`; a payload with quotes, newlines and non-ASCII round-trips
#        through the file byte-exactly; temp file gone on both paths.
#   R  — Invoke-PredictedResourceProbe: `--input <tmp>`, no `--stdin-json`, no `--repo-dir` in argv;
#        plus one REAL end-to-end run through uv against scripts/resolve_resource.py.
#   T  — temp-file hygiene: a locked file yields a WARN line and no exception; the startup sweep
#        removes only `mindwire-probe-*.json` older than the age bound.
#
# Same lift-from-AST pattern as Test-SweepHeadCache.ps1 (dot-sourcing would launch the sweep).

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot 'deploy/run-conductor-scheduled.ps1'
if (-not (Test-Path -LiteralPath $sweepScript)) { throw "sweep script not found: $sweepScript" }

$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) {
    $parseErrors | ForEach-Object { Write-Host "PARSE ERROR line $($_.Extent.StartLineNumber): $($_.Message)" }
    throw 'deploy/run-conductor-scheduled.ps1 does not parse'
}

$script:logLines = [System.Collections.Generic.List[string]]::new()
function Write-Log { param([string]$Message) $script:logLines.Add($Message) }

$functions = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
function Import-SweepFunction {
    param([string]$Name)
    $fn = $functions | Where-Object { $_.Name -eq $Name } | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $Name" }
    Invoke-Expression $fn.Extent.Text
    # Invoke-Expression defines in this function's scope; re-export to script scope.
    Set-Item -Path "function:script:$Name" -Value (Get-Item "function:$Name").ScriptBlock
}

# The wrapper's top-level settings the lifted functions read. Read their production values from the
# AST so the test notices if a default is removed, then override what the test needs.
$assigns = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $false)
function Get-TopLevelValue {
    param([string]$Name)
    $a = $assigns | Where-Object { $_.Left.Extent.Text -eq "`$$Name" } | Select-Object -First 1
    if (-not $a) { throw "top-level setting not found in sweep script: `$$Name" }
    return (Invoke-Expression $a.Right.Extent.Text)
}

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host "  PASS  $Name" }
    else { Write-Host "  FAIL  $Name — expected [$Expected], got [$Actual]"; $script:failures++ }
}
function CheckTrue { param([string]$Name, [bool]$Cond) Check $Name $true $Cond }

Write-Host 'Settings — every probe has a bound, and parked-humans defaults to 120 s'
foreach ($n in 'HeadSkipProbeTimeoutSeconds', 'HeadProbeTimeoutSeconds', 'ParkedHumansProbeTimeoutSeconds',
               'ControlProbeTimeoutSeconds', 'PredictedResourceProbeTimeoutSeconds', 'GateBootstrapProbeTimeoutSeconds') {
    $v = Get-TopLevelValue $n
    CheckTrue "$n is a positive bound ($v)" ($v -is [int] -and $v -gt 0)
}
Check 'ParkedHumansProbeTimeoutSeconds = 120' 120 (Get-TopLevelValue 'ParkedHumansProbeTimeoutSeconds')
Check 'ProbeKillGraceMs = 5000' 5000 (Get-TopLevelValue 'ProbeKillGraceMs')
Check "ProbeInputFilePrefix = 'mindwire-probe-'" 'mindwire-probe-' (Get-TopLevelValue 'ProbeInputFilePrefix')
Check 'ProbeInputFileMaxAgeMinutes = 60' 60 (Get-TopLevelValue 'ProbeInputFileMaxAgeMinutes')

Write-Host 'Census — no unbounded `& uv` and no `$payload |` stdin feed left in the sweep script'
# DECLARED GAP, not hidden: Get-FailureClass feeds the session-log tail to `uv run ... -m
# spirrow_mindwire.stall_ledger` on stdin with no bound. It was not in the agreed scope (Bohr
# msg-5414 §3 lists eight call sites) and is reported back to the thread for a decision; it is the
# ONE allowed `uv` command here so that a NEW unbounded call still reds this census.
$uvAllowed = @('Get-FailureClass')
$uvCalls = $ast.FindAll({
        param($n) $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.GetCommandName() -eq 'uv' }, $true)
$uvUnexpected = @($uvCalls | Where-Object {
        $p = $_.Parent
        while ($p -and $p -isnot [System.Management.Automation.Language.FunctionDefinitionAst]) { $p = $p.Parent }
        -not ($p -and $uvAllowed -contains $p.Name) })
Check 'no direct `uv` command invocations outside the declared gap' 0 $uvUnexpected.Count
Check 'the declared gap is still exactly one call (remove the allowance when it is fixed)' 1 (@($uvCalls).Count - $uvUnexpected.Count)
$stdinFeeds = $ast.FindAll({
        param($n) $n -is [System.Management.Automation.Language.PipelineAst] -and
        $n.PipelineElements.Count -ge 2 -and
        $n.PipelineElements[0].Extent.Text -match '^\$payload\w*$' -and
        $n.PipelineElements[1].Extent.Text -match '^&' }, $true)
Check 'no `$payload | & ...` stdin feeds' 0 @($stdinFeeds).Count
$inlinePayload = $ast.FindAll({
        param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] -and $n.Value -eq '--payload' }, $true)
Check "no inline '--payload' argument anywhere" 0 @($inlinePayload).Count

# --- private dirs -------------------------------------------------------------------------------
$scratch = Join-Path ([System.IO.Path]::GetTempPath()) ("mindwire-bounded-probe-test-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $scratch | Out-Null
$probeDir = Join-Path $scratch 'probe-inputs'
New-Item -ItemType Directory -Path $probeDir | Out-Null

$ProbeKillGraceMs = 5000
$ProbeInputFilePrefix = 'mindwire-probe-'
$ProbeInputFileMaxAgeMinutes = 60
$ProbeInputDirectory = $probeDir

foreach ($name in 'Invoke-BoundedUvProbe', 'Get-ProbeOutputLines', 'Get-ProbeJsonLine',
                  'New-ProbeInputFile', 'Remove-ProbeInputFile', 'Remove-StaleProbeInputFiles') {
    Import-SweepFunction $name
}
$pwshExe = (Get-Process -Id $PID).Path
$fakeLauncher = @($pwshExe, '-NoProfile', '-NonInteractive', '-File')

function Test-ProcessAlive { param([int]$Id) return ($null -ne (Get-Process -Id $Id -ErrorAction SilentlyContinue)) }

try {
    # --- H: the helper ---------------------------------------------------------------------------
    Write-Host 'H1 — a hung probe is bounded and its whole tree is killed'
    $hang = Join-Path $scratch 'hang.ps1'
    $childPidFile = Join-Path $scratch 'grandchild.pid'
    Set-Content -LiteralPath $hang -Encoding utf8 -Value @'
param([string]$PidFile)
$gc = Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList @('-NoProfile', '-Command', 'Start-Sleep 600') -PassThru -NoNewWindow
Set-Content -LiteralPath $PidFile -Value $gc.Id
Start-Sleep 600
'@
    $script:logLines.Clear()
    $r = Invoke-BoundedUvProbe -Launcher $fakeLauncher -Arguments @($hang, $childPidFile) -TimeoutSeconds 8 -Label 'h1'
    CheckTrue 'timedOut = $true' $r.timedOut
    CheckTrue 'killConfirmed = $true' $r.killConfirmed
    Check 'ok = $false' $false $r.ok
    CheckTrue 'returned well before the probe would have (elapsed < 30 s)' ($r.elapsedSec -lt 30)
    CheckTrue 'root PID is dead on return' (-not (Test-ProcessAlive $r.pid))
    CheckTrue 'grandchild PID was recorded before the bound fired' (Test-Path -LiteralPath $childPidFile)
    if (Test-Path -LiteralPath $childPidFile) {
        $gcPid = [int](Get-Content -LiteralPath $childPidFile -Raw).Trim()
        $deadline = (Get-Date).AddSeconds(5)
        while ((Test-ProcessAlive $gcPid) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 100 }
        CheckTrue 'grandchild PID is dead (tree kill, not root-only kill)' (-not (Test-ProcessAlive $gcPid))
        if (Test-ProcessAlive $gcPid) { Stop-Process -Id $gcPid -Force -ErrorAction SilentlyContinue }
    }
    CheckTrue 'TIMEOUT line logged with label, elapsed and pid' (@($script:logLines | Where-Object { $_ -match "^TIMEOUT label=h1 elapsed=[\d.]+s pid=$($r.pid)\b" }).Count -eq 1)
    CheckTrue 'no KILL-UNCONFIRMED line when the kill was confirmed' (@($script:logLines | Where-Object { $_ -like 'KILL-UNCONFIRMED*' }).Count -eq 0)
    CheckTrue 'error says timed out' ($r.error -like 'timed out after 8s*')

    Write-Host 'H2 — stdin is closed at start: a probe that reads stdin sees EOF, not a hang'
    $reader = Join-Path $scratch 'reader.ps1'
    Set-Content -LiteralPath $reader -Encoding utf8 -Value @'
$in = [Console]::In.ReadToEnd()
[Console]::Error.WriteLine('diagnostic on stderr')
Write-Output ('{"stdin_len": ' + $in.Length + '}')
exit 0
'@
    $r = Invoke-BoundedUvProbe -Launcher $fakeLauncher -Arguments @($reader) -TimeoutSeconds 30 -Label 'h2'
    Check 'ok = $true' $true $r.ok
    Check 'timedOut = $false' $false $r.timedOut
    Check 'exit code 0' 0 $r.code
    Check 'stdin was empty (EOF at start)' '{"stdin_len": 0}' (Get-ProbeJsonLine -Result $r)
    CheckTrue 'stderr captured separately' ($r.stderr -like '*diagnostic on stderr*')
    $lines = Get-ProbeOutputLines -Result $r
    CheckTrue 'output lines carry stdout then stderr' ($lines.Count -eq 2 -and $lines[0] -like '{*' -and $lines[1] -eq 'diagnostic on stderr')

    Write-Host 'H3 — a non-zero exit passes through as ok=$true with the code (policy is the caller''s)'
    $fail = Join-Path $scratch 'fail.ps1'
    Set-Content -LiteralPath $fail -Encoding utf8 -Value 'exit 7'
    $r = Invoke-BoundedUvProbe -Launcher $fakeLauncher -Arguments @($fail) -TimeoutSeconds 30 -Label 'h3'
    Check 'ok = $true' $true $r.ok
    Check 'code = 7' 7 $r.code

    Write-Host 'H4 — a launcher that cannot start is an error result, not an exception'
    $r = Invoke-BoundedUvProbe -Launcher @('mindwire-no-such-executable-xyz') -Arguments @('x') -TimeoutSeconds 5 -Label 'h4'
    Check 'ok = $false' $false $r.ok
    CheckTrue 'error names the start failure' ($r.error -like 'cannot start*')

    # --- caller tests: stub the helper, record what it was handed ---------------------------------
    $script:probeCalls = [System.Collections.Generic.List[object]]::new()
    $script:probeMode = 'ok'
    $script:probeStdout = ''
    function Invoke-BoundedUvProbe {
        param([string[]]$Arguments, [int]$TimeoutSeconds, [string]$Label, [int]$KillGraceMs, [string]$WorkingDirectory, [string[]]$Launcher)
        $fileArg = $null
        foreach ($flag in '--input', '--candidates', '--payload-file') {
            $i = [array]::IndexOf($Arguments, $flag)
            if ($i -ge 0) { $fileArg = $Arguments[$i + 1] }
        }
        $content = $null
        if ($fileArg -and (Test-Path -LiteralPath $fileArg)) { $content = [System.IO.File]::ReadAllBytes($fileArg) }
        $script:probeCalls.Add(@{ Arguments = $Arguments; TimeoutSeconds = $TimeoutSeconds; Label = $Label; File = $fileArg; Bytes = $content })
        if ($script:probeMode -eq 'timeout') {
            return @{ ok = $false; timedOut = $true; killConfirmed = $true; code = $null; stdout = ''; stderr = '';
                elapsedSec = [double]$TimeoutSeconds; pid = 4242; error = "timed out after ${TimeoutSeconds}s (process tree killed)" }
        }
        return @{ ok = $true; timedOut = $false; killConfirmed = $false; code = 0; stdout = $script:probeStdout; stderr = '';
            elapsedSec = 0.1; pid = 4242; error = $null }
    }
    function Get-ProbeInputLeftovers { return @(Get-ChildItem -LiteralPath $probeDir -File) }

    $HeadSkipProbeTimeoutSeconds = 111
    $ParkedHumansProbeTimeoutSeconds = 120
    $PredictedResourceProbeTimeoutSeconds = 113
    foreach ($name in 'Invoke-ParkedHumansProbe', 'Invoke-HeadSkipDecide', 'Invoke-HeadSkipCommitLaunch',
                      'Invoke-HeadSkipCommitTerminal', 'Invoke-PredictedResourceProbe') {
        Import-SweepFunction $name
    }

    # --- P: parked-humans ------------------------------------------------------------------------
    Write-Host 'P1 — parked-humans normal path: --input <tmp>, parse, temp file removed'
    $cands = @(@{ project = 'p1'; thread_id = 'T-a' }, @{ project = 'p1'; thread_id = 'T-b' }, @{ project = 'other'; thread_id = 'T-x' })
    $heads = @{ p1 = @{ 'T-a' = 'msg-1' } }
    $script:probeCalls.Clear(); $script:probeMode = 'ok'
    $script:probeStdout = "uv noise`n" + '{"project":"p1","polled":2,"parked":[{"thread_id":"T-a","head_msg_id":"msg-1","token":"human"}],"errors":[]}'
    $res = Invoke-ParkedHumansProbe -Project 'p1' -Candidates $cands -HeadsByProject $heads
    Check 'one probe call' 1 $script:probeCalls.Count
    $call = $script:probeCalls[0]
    CheckTrue "argv has --project p1 and --input <file>" (($call.Arguments -join ' ') -match '--project p1 --input ')
    Check 'bound = $ParkedHumansProbeTimeoutSeconds' 120 $call.TimeoutSeconds
    CheckTrue 'temp file named mindwire-probe-parked-humans-p1-*.json' ((Split-Path -Leaf $call.File) -like 'mindwire-probe-parked-humans-p1-*.json')
    $sent = [System.Text.Encoding]::UTF8.GetString($call.Bytes) | ConvertFrom-Json
    Check 'payload carried the project''s 2 candidates only' 2 @($sent.candidates).Count
    Check 'payload carried the head from the head probe' 'msg-1' (@($sent.candidates | Where-Object { $_.thread_id -eq 'T-a' })[0].head_msg_id)
    Check 'parsed 1 parked entry' 1 @($res.parked).Count
    Check 'parked key' 'p1/T-a' $res.parked[0].key
    Check 'polled = 2' 2 $res.polled
    Check 'temp file removed (normal)' 0 (Get-ProbeInputLeftovers).Count

    Write-Host 'P2 — parked-humans timeout: fail-closed, error row says timed out, temp file removed'
    $script:probeCalls.Clear(); $script:probeMode = 'timeout'; $script:logLines.Clear()
    $res = Invoke-ParkedHumansProbe -Project 'p1' -Candidates $cands -HeadsByProject $heads
    Check 'no parked entries' 0 @($res.parked).Count
    Check 'polled = 0' 0 $res.polled
    Check 'one error row' 1 @($res.errors).Count
    Check "errors[0].thread_id = '__probe__'" '__probe__' $res.errors[0].thread_id
    CheckTrue "errors[0].reason contains 'timed out'" ($res.errors[0].reason -like '*timed out*')
    Check 'errors[0].reason verbatim' 'timed out after 120s (process tree killed)' $res.errors[0].reason
    CheckTrue 'a log line names the timeout' (@($script:logLines | Where-Object { $_ -like 'parked-humans probe `[p1`] timed out*' }).Count -eq 1)
    Check 'temp file removed (timeout)' 0 (Get-ProbeInputLeftovers).Count

    # --- S: head-skip ----------------------------------------------------------------------------
    $stateFile = Join-Path $scratch 'head_skip_state.json'
    Write-Host 'S1 — head-skip decide: --candidates <tmp>, temp file removed on both paths'
    $script:probeCalls.Clear(); $script:probeMode = 'ok'
    $script:probeStdout = '{"verdicts":[{"thread_id":"T-a","verdict":"LAUNCH"}]}'
    $res = Invoke-HeadSkipDecide -Project 'p1' -Candidates @(@{ thread_id = 'T-a'; head_msg_id = 'msg-1'; control_state = 'run' }) -StateFilePath $stateFile
    Check 'ok' $true $res.ok
    Check 'verdict parsed' 'LAUNCH' $res.verdicts['T-a'].verdict
    CheckTrue 'argv has --candidates <file>' ($null -ne $script:probeCalls[0].File -and ($script:probeCalls[0].Arguments -contains '--candidates'))
    Check 'bound = $HeadSkipProbeTimeoutSeconds' 111 $script:probeCalls[0].TimeoutSeconds
    Check 'candidates file is a JSON array of 1' 1 @([System.Text.Encoding]::UTF8.GetString($script:probeCalls[0].Bytes) | ConvertFrom-Json).Count
    Check 'temp file removed (normal)' 0 (Get-ProbeInputLeftovers).Count
    $script:probeMode = 'timeout'
    $res = Invoke-HeadSkipDecide -Project 'p1' -Candidates @(@{ thread_id = 'T-a' }) -StateFilePath $stateFile
    Check 'timeout -> ok=$false (systemic, caller fails closed)' $false $res.ok
    CheckTrue 'timeout error is named' ($res.error -like '*timed out*')
    Check 'temp file removed (timeout)' 0 (Get-ProbeInputLeftovers).Count

    Write-Host 'S2 — commit-launch: --payload-file, never --payload; hostile payload round-trips byte-exactly'
    $hostile = [ordered]@{
        thread_id   = 'T-"quoted" \back\slash'
        head_msg_id = "line1`nline2`r`nline3"
        note        = '日本語 — ünïcödé “smart” ''single'' & | < > %PATH% $env:X `tick'
    }
    $expectedJson = ConvertTo-Json -InputObject $hostile -Depth 6 -Compress
    $script:probeCalls.Clear(); $script:probeMode = 'ok'
    $script:probeStdout = '{"record":{"launches_same_head":1,"head_msg_id_at_launch":"msg-1"}}'
    $res = Invoke-HeadSkipCommitLaunch -Payload $hostile -StateFilePath $stateFile
    Check 'ok' $true $res.ok
    Check 'launches_same_head read back' 1 $res.launches_same_head
    $argv = $script:probeCalls[0].Arguments
    CheckTrue "argv has '--payload-file'" ($argv -contains '--payload-file')
    CheckTrue "argv has no '--payload'" (-not ($argv -contains '--payload'))
    CheckTrue 'no argv element carries the JSON' (-not ($argv | Where-Object { $_ -like '*thread_id*' }))
    $bytes = $script:probeCalls[0].Bytes
    CheckTrue 'file has no UTF-8 BOM' (-not ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF))
    Check 'file content == the JSON the wrapper built (byte-exact UTF-8)' $expectedJson ([System.Text.UTF8Encoding]::new($false, $true).GetString($bytes))
    $back = [System.Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
    Check 'round-trip: thread_id' $hostile.thread_id $back.thread_id
    Check 'round-trip: newlines' $hostile.head_msg_id $back.head_msg_id
    Check 'round-trip: non-ASCII and shell metacharacters' $hostile.note $back.note
    Check 'temp file removed (normal)' 0 (Get-ProbeInputLeftovers).Count
    $script:probeMode = 'timeout'
    $res = Invoke-HeadSkipCommitLaunch -Payload $hostile -StateFilePath $stateFile
    Check 'timeout -> ok=$false (fail-closed)' $false $res.ok
    Check 'temp file removed (timeout)' 0 (Get-ProbeInputLeftovers).Count

    Write-Host 'S3 — commit-terminal: --payload-file, never --payload; temp file removed on both paths'
    $script:probeCalls.Clear(); $script:probeMode = 'ok'; $script:probeStdout = '{}'
    $res = Invoke-HeadSkipCommitTerminal -ThreadId 'T-"q"' -StopReason "human`nx" -HeadMsgId 'msg-9' -StateFilePath $stateFile
    Check 'ok' $true $res.ok
    $argv = $script:probeCalls[0].Arguments
    CheckTrue "argv has '--payload-file'" ($argv -contains '--payload-file')
    CheckTrue "argv has no '--payload'" (-not ($argv -contains '--payload'))
    $back = [System.Text.Encoding]::UTF8.GetString($script:probeCalls[0].Bytes) | ConvertFrom-Json
    Check 'round-trip: thread_id with quotes' 'T-"q"' $back.thread_id
    Check 'round-trip: reason with newline' "human`nx" $back.reason
    Check 'temp file removed (normal)' 0 (Get-ProbeInputLeftovers).Count
    $script:probeMode = 'timeout'
    $res = Invoke-HeadSkipCommitTerminal -ThreadId 'T-a' -StopReason 'human' -HeadMsgId 'msg-9' -StateFilePath $stateFile
    Check 'timeout -> ok=$false (caller logs, fail-open)' $false $res.ok
    Check 'temp file removed (timeout)' 0 (Get-ProbeInputLeftovers).Count

    # --- R: predicted-resource -------------------------------------------------------------------
    Write-Host 'R1 — predicted-resource: --input <tmp>; no --stdin-json, no --repo-dir in argv'
    $dirs = @('C:\a b\one', 'C:\"two"', 'D:\日本語')
    $script:probeCalls.Clear(); $script:probeMode = 'ok'
    $script:probeStdout = '{"resolutions":[{"repo_dir":"C:\\a b\\one","resource":"github.com/o/r","reason":null}]}'
    $map = Invoke-PredictedResourceProbe -RepoDirs $dirs
    $argv = $script:probeCalls[0].Arguments
    CheckTrue "argv has '--input'" ($argv -contains '--input')
    CheckTrue "argv has no '--stdin-json'" (-not ($argv -contains '--stdin-json'))
    CheckTrue "argv has no '--repo-dir'" (-not ($argv -contains '--repo-dir'))
    Check 'bound = $PredictedResourceProbeTimeoutSeconds' 113 $script:probeCalls[0].TimeoutSeconds
    $sent = [System.Text.Encoding]::UTF8.GetString($script:probeCalls[0].Bytes) | ConvertFrom-Json
    Check 'payload repo_dirs round-trip' ($dirs -join '|') (@($sent.repo_dirs) -join '|')
    Check 'parsed resolution' 'github.com/o/r' $map['C:\a b\one'].predicted_resource
    Check 'temp file removed (normal)' 0 (Get-ProbeInputLeftovers).Count
    $script:probeMode = 'timeout'
    $map = Invoke-PredictedResourceProbe -RepoDirs $dirs
    Check 'timeout -> $null (fail-open)' $null $map
    Check 'temp file removed (timeout)' 0 (Get-ProbeInputLeftovers).Count

    Write-Host 'R2 — end to end through the REAL helper and uv: resolve_resource.py --input'
    Remove-Item function:script:Invoke-BoundedUvProbe -ErrorAction SilentlyContinue
    Remove-Item function:Invoke-BoundedUvProbe -ErrorAction SilentlyContinue
    Import-SweepFunction 'Invoke-BoundedUvProbe'
    $PredictedResourceProbeTimeoutSeconds = 120
    $script:logLines.Clear()
    $map = Invoke-PredictedResourceProbe -RepoDirs @('not-absolute', 'also-not-absolute')
    CheckTrue "real run returned a map (log: $($script:logLines -join ' / '))" ($null -ne $map)
    if ($null -ne $map) {
        Check 'both repo_dirs resolved (to a reason)' 2 $map.Count
        CheckTrue 'relative path yields an empty resource with a reason' ($map['not-absolute'].predicted_resource -eq '' -and $map['not-absolute'].reason -ne '')
    }
    Check 'temp file removed (real run)' 0 (Get-ProbeInputLeftovers).Count

    # --- T: temp-file hygiene --------------------------------------------------------------------
    Write-Host 'T1 — an undeletable temp file: WARN logged, no exception escapes'
    # The delete failure is injected by shadowing Remove-Item, not by holding a FileShare.None
    # handle: on Linux (the CI runner) an open handle does not block unlink, so a real lock only
    # reproduces the failure on Windows. The shadow reproduces it on every platform.
    $locked = New-ProbeInputFile -Json '{}' -Label 'lock test'
    CheckTrue 'label is sanitised into the name' ((Split-Path -Leaf $locked) -like 'mindwire-probe-lock-test-*.json')
    $script:removeAttempts = 0
    $script:failRemove = $true
    # Flag-gated shadow: while $script:failRemove is set it fails like a locked file; otherwise it
    # passes straight through to the real cmdlet, so nothing after T1 is affected.
    function script:Remove-Item {
        if ($script:failRemove) {
            $script:removeAttempts++
            throw [System.IO.IOException]::new('The process cannot access the file because it is being used by another process.')
        }
        Microsoft.PowerShell.Management\Remove-Item @args
    }
    $script:logLines.Clear()
    $threw = $false
    try { Remove-ProbeInputFile -Path $locked -DelayMs 10 } catch { $threw = $true }
    finally { $script:failRemove = $false }
    Check 'no exception' $false $threw
    Check 'delete retried 3 times' 3 $script:removeAttempts
    CheckTrue 'WARN line names the path' (@($script:logLines | Where-Object { $_ -like "WARN probe temp file not removed: $locked (*being used by another process*" }).Count -eq 1)
    CheckTrue 'file still present after failed removal' (Test-Path -LiteralPath $locked)
    Remove-ProbeInputFile -Path $locked
    CheckTrue 'removable once the failure clears' (-not (Test-Path -LiteralPath $locked))

    Write-Host 'T2 — startup sweep removes only mindwire-probe-*.json older than the bound'
    $sweepDir = Join-Path $scratch 'sweep'
    New-Item -ItemType Directory -Path $sweepDir | Out-Null
    $old = Join-Path $sweepDir 'mindwire-probe-x-old.json'
    $new = Join-Path $sweepDir 'mindwire-probe-x-new.json'
    $otherOld = Join-Path $sweepDir 'someone-else-old.json'
    $oldTxt = Join-Path $sweepDir 'mindwire-probe-x-old.txt'
    foreach ($f in $old, $new, $otherOld, $oldTxt) { Set-Content -LiteralPath $f -Value '{}' }
    $past = (Get-Date).ToUniversalTime().AddMinutes(-61)
    foreach ($f in $old, $otherOld, $oldTxt) { (Get-Item -LiteralPath $f).LastWriteTimeUtc = $past }
    (Get-Item -LiteralPath $new).LastWriteTimeUtc = (Get-Date).ToUniversalTime().AddMinutes(-59)
    Remove-StaleProbeInputFiles -Directory $sweepDir
    CheckTrue 'old mindwire-probe-*.json removed' (-not (Test-Path -LiteralPath $old))
    CheckTrue 'recent mindwire-probe-*.json kept' (Test-Path -LiteralPath $new)
    CheckTrue 'old file with another prefix kept' (Test-Path -LiteralPath $otherOld)
    CheckTrue 'old mindwire-probe-* with another extension kept' (Test-Path -LiteralPath $oldTxt)

    Write-Host 'T3 — the sweep calls the startup cleanup before reading the sweep list'
    $text = Get-Content -LiteralPath $sweepScript -Raw
    $iClean = $text.IndexOf("`n    Remove-StaleProbeInputFiles")
    $iSweep = $text.IndexOf('$candidates = Get-SweepCandidates -Path $sweepConfigPath')
    CheckTrue 'Remove-StaleProbeInputFiles is called in the run body before the sweep' ($iClean -gt 0 -and $iSweep -gt $iClean)
}
finally {
    Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ''
if ($script:failures -gt 0) {
    Write-Host "FAILED: $($script:failures) check(s)"
    exit 1
}
Write-Host 'All bounded-probe checks passed.'
exit 0
