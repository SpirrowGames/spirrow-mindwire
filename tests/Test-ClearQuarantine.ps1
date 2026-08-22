# Regression guard for deploy/Clear-Quarantine.ps1.
#
# Q3 of the sweep-failure-isolation design (spec/msg-814) is load-bearing: there is NO auto-clear.
# So this script IS the clear mechanism, and every wrong answer here is a slow, invisible
# degradation of the whole quarantine story:
#   - forgetting to append to history throws away the -Reason data the design specifically wants;
#   - silently clearing a thread that was not quarantined trains the operator to trust a mistype.
#
# The old third failure mode ("forgetting to drop the heads.json entry means the next tick head-
# skips the freshly cleared thread back to the head it was quarantined under") is gone with
# heads.json itself (T-sweep-intake-and-quarantine-stalls Bohr msg-1432 §W-2). Under the new
# head-skip nomination predicate a cleared thread's launch is timed by backoff (CAP=60 min), not
# by head equality, so the "silent re-skip" failure this file used to pin is no longer reachable.
#
# The fixture is a data dir built from scratch in a temp directory — never this checkout's own
# state dir, which would corrupt live daemon state.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$clearScript = Join-Path $repoRoot "deploy/Clear-Quarantine.ps1"
if (-not (Test-Path -LiteralPath $clearScript)) { throw "clear script not found: $clearScript" }

$parseErrors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($clearScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) {
    $parseErrors | ForEach-Object { Write-Host "PARSE ERROR line $($_.Extent.StartLineNumber): $($_.Message)" }
    throw "deploy/Clear-Quarantine.ps1 does not parse"
}

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual) }
}

function New-DataFixture {
    $dataDir = Join-Path ([System.IO.Path]::GetTempPath()) ("mindwire-clearq-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path (Join-Path $dataDir "state") -Force | Out-Null
    $qPath = Join-Path $dataDir "state\quarantine.json"
    $qState = @{
        'proj/T-broken' = @{
            state = 'quarantined'; first_failure_at = '2026-08-10T00:00:00Z'
            last_failure_at = '2026-08-10T00:00:00Z'; consecutive_failures = 1
            exit_code = 1; stop_reason = 'no_handoff_to_human'
            failure_fingerprint = @{ head = 'msg-100'; control = 'run' }
            session_log_path = 'C:/logs/foo.log'; session_log_tail = @('line1')
        }
        'proj/T-other'  = @{
            state = 'quarantined'; first_failure_at = '2026-08-11T00:00:00Z'
            last_failure_at = '2026-08-11T00:00:00Z'; consecutive_failures = 1
            exit_code = 1; stop_reason = 'timeout'
            failure_fingerprint = @{ head = 'msg-200'; control = 'run' }
            session_log_path = 'C:/logs/foo.log'; session_log_tail = @('line1')
        }
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($qPath, ($qState | ConvertTo-Json -Depth 5), $utf8NoBom)
    return $dataDir
}

function Invoke-Clear {
    param([string]$DataDir, [string]$Thread, [string]$Reason)
    # -Confirm:$false so ShouldProcess does not block in unattended tests.
    return (& pwsh -NoProfile -File $clearScript -Thread $Thread -Reason $Reason -DataDir $DataDir -Confirm:$false 2>&1)
}

Write-Host "Clear-Quarantine — happy path removes the entry, keeps the sibling, records reason"
$dataDir = New-DataFixture
try {
    $out = Invoke-Clear -DataDir $dataDir -Thread 'proj/T-broken' -Reason 'PR #999 fixed it'
    Check "exit 0 on happy path" 0 $LASTEXITCODE

    $q = (Get-Content -LiteralPath (Join-Path $dataDir "state\quarantine.json") -Raw -Encoding utf8) | ConvertFrom-Json
    $keys = @($q.PSObject.Properties.Name)
    Check "cleared thread is removed" $false ($keys -contains 'proj/T-broken')
    Check "sibling is preserved" $true ($keys -contains 'proj/T-other')

    $hist = (Get-Content -LiteralPath (Join-Path $dataDir "state\quarantine-history.json") -Raw -Encoding utf8) | ConvertFrom-Json
    $hist = @($hist)   # ConvertFrom-Json unwraps single-element arrays
    Check "history has one entry" 1 $hist.Count
    Check "history entry names the thread" 'proj/T-broken' $hist[0].thread
    Check "history entry records the reason" 'PR #999 fixed it' $hist[0].reason
    Check "history entry preserves the failed exit code" 1 $hist[0].record.exit_code

    # Clear-Quarantine must not touch state/heads.json (it does not exist anymore under the new
    # head-skip predicate — msg-1432 §W-2) nor state/head_skip.json (single-writer, owned by
    # scripts/head_skip_decide.py). Assert the script did not create either file behind our back.
    $legacyHeads = Join-Path $dataDir "state\heads.json"
    Check "Clear-Quarantine does not create state/heads.json" $false (Test-Path -LiteralPath $legacyHeads)
    $headSkip = Join-Path $dataDir "state\head_skip.json"
    Check "Clear-Quarantine does not create state/head_skip.json" $false (Test-Path -LiteralPath $headSkip)
}
finally { Remove-Item -LiteralPath $dataDir -Recurse -Force -ErrorAction SilentlyContinue }

Write-Host "Clear-Quarantine — clearing a thread that is not quarantined is an error, not a no-op"
$dataDir = New-DataFixture
try {
    $out = Invoke-Clear -DataDir $dataDir -Thread 'proj/T-not-there' -Reason 'oops'
    Check "non-zero exit when thread is not quarantined" $true ($LASTEXITCODE -ne 0)
    # The sibling entries must still be untouched — a failed clear must not partially corrupt state.
    $q = (Get-Content -LiteralPath (Join-Path $dataDir "state\quarantine.json") -Raw -Encoding utf8) | ConvertFrom-Json
    $keys = @($q.PSObject.Properties.Name)
    Check "state file untouched on error" 2 $keys.Count
}
finally { Remove-Item -LiteralPath $dataDir -Recurse -Force -ErrorAction SilentlyContinue }

Write-Host "Clear-Quarantine — second clear appends, never overwrites (history is append-only)"
$dataDir = New-DataFixture
try {
    $null = Invoke-Clear -DataDir $dataDir -Thread 'proj/T-broken' -Reason 'reason 1'
    $null = Invoke-Clear -DataDir $dataDir -Thread 'proj/T-other'  -Reason 'reason 2'
    $hist = (Get-Content -LiteralPath (Join-Path $dataDir "state\quarantine-history.json") -Raw -Encoding utf8) | ConvertFrom-Json
    $hist = @($hist)
    Check "history has both entries" 2 $hist.Count
    $reasons = @($hist | ForEach-Object { $_.reason })
    Check "first reason preserved" $true ($reasons -contains 'reason 1')
    Check "second reason preserved" $true ($reasons -contains 'reason 2')
}
finally { Remove-Item -LiteralPath $dataDir -Recurse -Force -ErrorAction SilentlyContinue }

Write-Host "Clear-Quarantine — history root is ALWAYS a JSON array (never flip-flops with count)"
# The PR-gate review on #138 called this out: without -AsArray, PowerShell's ConvertTo-Json unrolls
# a one-element array into a bare object, so the file's root shape depended on how many clears had
# happened. The script itself tolerates both on read, but any external reader (log ingester, dashboard,
# a naysayer-tool built later) would be surprised in exactly the way that reproduces silent-schema-drift.
# This test locks the shape in.
$dataDir = New-DataFixture
try {
    # First clear -> exactly 1 element. THIS is the case that used to emit a JSON object.
    $null = Invoke-Clear -DataDir $dataDir -Thread 'proj/T-broken' -Reason 'reason 1'
    $raw = Get-Content -LiteralPath (Join-Path $dataDir "state\quarantine-history.json") -Raw -Encoding utf8
    Check "history file starts with '[' after the first clear" $true ($raw.TrimStart().StartsWith('['))
    Check "history file ends with ']' after the first clear" $true ($raw.TrimEnd().EndsWith(']'))

    # Second clear -> 2 elements. Should still be an array (this always worked, but re-checking
    # here means a future editor cannot 'simplify' the writer back into the flip-flop.)
    $null = Invoke-Clear -DataDir $dataDir -Thread 'proj/T-other' -Reason 'reason 2'
    $raw = Get-Content -LiteralPath (Join-Path $dataDir "state\quarantine-history.json") -Raw -Encoding utf8
    Check "history file still starts with '[' after the second clear" $true ($raw.TrimStart().StartsWith('['))
}
finally { Remove-Item -LiteralPath $dataDir -Recurse -Force -ErrorAction SilentlyContinue }

Write-Host ""
if ($script:failures -gt 0) {
    Write-Host "Clear-Quarantine: $($script:failures) check(s) FAILED"
    exit 1
}
Write-Host "Clear-Quarantine: all checks passed"
exit 0
