# ledger_drift wiring (T-ledger-writeback-on-merge-and-thread-close-on-task-done, design v0.3).
#
# What this file pins:
#   * Test-LedgerDriftEnabled: absent file / key / `enabled` / `false` are off; `true` is on; a file
#     or section that does not parse reads as on (the probe then refuses loudly)
#   * Invoke-LedgerDriftTick: off spawns nothing; on calls the module once with --tick, the
#     projects and the sweep config; a non-zero exit is logged with stderr (a save failure)
#   * Get-LedgerDriftDigestLines: off returns nothing and spawns nothing; on runs --show once and
#     frames its rows as the digest section; a --show that fails is shown, never dropped
#   * wiring: the wrapper dot-sources the lib, calls the tick once per sweep, and hands the digest
#     section to New-DailyDigest
#
# Repo convention: plain pwsh with a Check helper, not Pester.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'deploy/lib/LedgerDrift.ps1')

$script:failures = 0
function Check {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} {1}" -f $Name, $Detail) }
}

$script:logs = @()
function Write-Log { param([string]$Message) $script:logs += $Message }
function Get-ProbeJsonLine { param($Result) return $Result.stdout }

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("ledger-drift-test-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    function Write-Sweep { param([string]$Text) $p = Join-Path $tmp 'sweep.json'; Set-Content -LiteralPath $p -Value $Text -Encoding UTF8; return $p }

    Check 'missing file is off' (-not (Test-LedgerDriftEnabled -SweepConfigPath (Join-Path $tmp 'nope.json')))
    Check 'no key is off' (-not (Test-LedgerDriftEnabled -SweepConfigPath (Write-Sweep '{"clones": {}}')))
    Check 'no enabled is off' (-not (Test-LedgerDriftEnabled -SweepConfigPath (Write-Sweep '{"ledger_drift": {}}')))
    Check 'enabled false is off' (-not (Test-LedgerDriftEnabled -SweepConfigPath (Write-Sweep '{"ledger_drift": {"enabled": false}}')))
    Check 'enabled true is on' (Test-LedgerDriftEnabled -SweepConfigPath (Write-Sweep '{"ledger_drift": {"enabled": true}}'))
    Check 'unparseable file reads as on' (Test-LedgerDriftEnabled -SweepConfigPath (Write-Sweep '{not json'))
    Check 'non-object section reads as on' (Test-LedgerDriftEnabled -SweepConfigPath (Write-Sweep '{"ledger_drift": true}'))

    $script:invoked = @()
    $fake = { param($a, $t, $l) $script:invoked += , @($a); return [PSCustomObject]@{ ok = $true; code = 0; stdout = '{"phase":"ledger_drift","enabled":true,"projects":["p1"]}'; stderr = '' } }

    $off = Write-Sweep '{"ledger_drift": {"enabled": false}}'
    $r = Invoke-LedgerDriftTick -Projects @('p1') -SweepConfigPath $off -Invoke $fake
    Check 'off: nothing is spawned' ($script:invoked.Count -eq 0 -and $null -eq $r)

    $on = Write-Sweep '{"ledger_drift": {"enabled": true}}'
    $r = Invoke-LedgerDriftTick -Projects @() -SweepConfigPath $on -Invoke $fake
    Check 'on with no project: nothing is spawned' ($script:invoked.Count -eq 0 -and $null -eq $r)

    $r = Invoke-LedgerDriftTick -Projects @('p1', 'p2') -SweepConfigPath $on -Invoke $fake
    $args0 = if ($script:invoked.Count -ge 1) { $script:invoked[0] } else { @() }
    Check 'on: one call' ($script:invoked.Count -eq 1)
    Check 'on: module and --tick' (($args0 -join ' ') -like '-m spirrow_mindwire.ledger_drift --tick *')
    Check 'on: projects joined' (($args0 -join ' ') -like "*--projects p1,p2*")
    Check 'on: sweep config passed' (($args0 -join ' ') -like "*--sweep-config $on*")
    Check 'on: result parsed' ($null -ne $r -and $r.enabled -eq $true)

    $script:logs = @()
    $saveFail = { param($a, $t, $l) return [PSCustomObject]@{ ok = $true; code = 1; stdout = '{"phase":"ledger_drift","enabled":true,"projects":["p1"]}'; stderr = 'ledger_drift: save failed for p1: PermissionError' } }
    $null = Invoke-LedgerDriftTick -Projects @('p1') -SweepConfigPath $on -Invoke $saveFail
    Check 'save failure is logged with stderr' (($script:logs -join "`n") -like '*exit=1*save failed for p1*')

    $bad = { param($a, $t, $l) return [PSCustomObject]@{ ok = $false; error = 'timeout' } }
    $r = Invoke-LedgerDriftTick -Projects @('p1') -SweepConfigPath $on -Invoke $bad
    Check 'probe failure fails open' ($null -eq $r -and ($script:logs -join "`n") -like '*fail-open*')

    # Digest section (PR #585 gate objection 1). Write-Sweep reuses one path, so the off / on
    # configs get their own files here.
    $offFile = Join-Path $tmp 'sweep-off.json'; Set-Content -LiteralPath $offFile -Value '{"ledger_drift": {"enabled": false}}' -Encoding UTF8
    $on = Join-Path $tmp 'sweep-on.json'; Set-Content -LiteralPath $on -Value '{"ledger_drift": {"enabled": true}}' -Encoding UTF8
    $script:invoked = @()
    $lines = Get-LedgerDriftDigestLines -SweepConfigPath $offFile -Invoke $fake
    Check 'digest off: no lines, nothing spawned' (@($lines).Count -eq 0 -and $script:invoked.Count -eq 0)
    $showOut = { param($a, $t, $l) $script:invoked += , @($a); return [PSCustomObject]@{ ok = $true; code = 0; stdout = "[p1]`n台帳のずれ: 古い結果 (x 時点, 3 時間 前。件数は参考値)`n  (a) merge 済み PR があるのに not_started: 2`n"; stderr = '' } }
    $lines = Get-LedgerDriftDigestLines -SweepConfigPath $on -Invoke $showOut
    $a1 = if ($script:invoked.Count -ge 1) { $script:invoked[0] -join ' ' } else { '' }
    Check 'digest on: one --show call with the sweep config' ($script:invoked.Count -eq 1 -and $a1 -like "-m spirrow_mindwire.ledger_drift --show --sweep-config $on")
    Check 'digest on: header, rows, trailing blank' ($lines.Count -eq 5 -and $lines[0] -like '台帳のずれ*' -and $lines[-1] -eq '')
    Check 'digest on: stale verdict passes through' (($lines -join "`n") -like '*古い結果*参考値*')
    $lines = Get-LedgerDriftDigestLines -SweepConfigPath $on -Invoke $bad
    Check 'digest: a --show that did not run is shown, not dropped' ($lines.Count -eq 3 -and $lines[1] -like '*表示に失敗*timeout*')
    $exit1 = { param($a, $t, $l) return [PSCustomObject]@{ ok = $true; code = 1; stdout = ''; stderr = 'boom' } }
    $lines = Get-LedgerDriftDigestLines -SweepConfigPath $on -Invoke $exit1
    Check 'digest: a failing --show is shown' (($lines -join "`n") -like '*表示に失敗: exit=1 boom*')

    $wrapper = Get-Content -LiteralPath (Join-Path $repoRoot 'deploy/run-conductor-scheduled.ps1') -Raw
    Check 'wrapper dot-sources the lib' ($wrapper -match "\. \(Join-Path \`$PSScriptRoot 'lib/LedgerDrift\.ps1'\)")
    Check 'wrapper calls the tick once' (([regex]::Matches($wrapper, 'Invoke-LedgerDriftTick -Projects')).Count -eq 1)
    Check 'digest renderer emits the section' ($wrapper -match '\$lines \+= \$LedgerDriftLines')
    Check 'digest call passes the section once' (([regex]::Matches($wrapper, '-LedgerDriftLines \(Get-LedgerDriftDigestLines')).Count -eq 1)
}
finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

if ($script:failures -gt 0) { Write-Host "$script:failures failure(s)"; exit 1 }
Write-Host "all passed"
