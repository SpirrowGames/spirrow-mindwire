# Regression guard for deploy/Clear-Quarantine.ps1.
#
# Q3 of the sweep-failure-isolation design (spec/msg-814) is load-bearing: there is NO auto-clear.
# So this script IS the clear mechanism, and every wrong answer here is a slow, invisible
# degradation of the whole quarantine story:
#   - forgetting to append to history throws away the -Reason data the design specifically wants;
#   - forgetting to drop the heads.json entry means the next tick head-skips the freshly cleared
#     thread back to the head it was quarantined under — the clear silently no-ops;
#   - silently clearing a thread that was not quarantined trains the operator to trust a mistype.
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
    $hPath = Join-Path $dataDir "state\heads.json"
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
    [System.IO.File]::WriteAllText($hPath, (@{
        'proj/T-broken' = @{ head = 'msg-100'; control = 'run' }
        'proj/T-other'  = @{ head = 'msg-200'; control = 'run' }
    } | ConvertTo-Json -Depth 5), $utf8NoBom)
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

    $h = (Get-Content -LiteralPath (Join-Path $dataDir "state\heads.json") -Raw -Encoding utf8) | ConvertFrom-Json
    $hkeys = @($h.PSObject.Properties.Name)
    # The point: dropping this stops the next tick head-skipping straight back to the same head
    # the failure was recorded under. Without it, the clear silently no-ops.
    Check "head-skip cache entry for cleared thread dropped" $false ($hkeys -contains 'proj/T-broken')
    Check "head-skip cache entry for sibling preserved" $true ($hkeys -contains 'proj/T-other')
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

Write-Host ""
if ($script:failures -gt 0) {
    Write-Host "Clear-Quarantine: $($script:failures) check(s) FAILED"
    exit 1
}
Write-Host "Clear-Quarantine: all checks passed"
exit 0
