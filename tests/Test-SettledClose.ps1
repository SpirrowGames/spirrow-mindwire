# settled_close wiring (T-done-disposition-auto-close boundary 6, Bohr msg-8455 §2 6-3).
#
# What this file pins:
#   * Test-SettledCloseEnabled: absent file / absent key / absent `enabled` / `false` are off;
#     `true` is on; a file or section that does not parse reads as on, so the probe reports it
#     loudly instead of a typo silently reading as off (the probe refuses before any MCP call)
#   * Invoke-SettledCloseTick: off spawns nothing (the probe runner is never called); on calls the
#     module once per tick with --tick, the projects, and the sweep config
#   * wiring: the wrapper dot-sources the lib and calls the tick with the 1c project list
#
# Repo convention: plain pwsh with a Check helper, not Pester.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'deploy/lib/SettledClose.ps1')

$script:failures = 0
function Check {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} {1}" -f $Name, $Detail) }
}

# Stand-ins for the wrapper's helpers the lib calls at call time.
$script:logs = @()
function Write-Log { param([string]$Message) $script:logs += $Message }
function Get-ProbeJsonLine { param($Result) return $Result.stdout }

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("settled-close-test-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    function Write-Sweep { param([string]$Text) $p = Join-Path $tmp 'sweep.json'; Set-Content -LiteralPath $p -Value $Text -Encoding UTF8; return $p }

    Check 'missing file is off' (-not (Test-SettledCloseEnabled -SweepConfigPath (Join-Path $tmp 'nope.json')))
    Check 'no key is off' (-not (Test-SettledCloseEnabled -SweepConfigPath (Write-Sweep '{"clones": {}}')))
    Check 'no enabled is off' (-not (Test-SettledCloseEnabled -SweepConfigPath (Write-Sweep '{"settled_close": {}}')))
    Check 'enabled false is off' (-not (Test-SettledCloseEnabled -SweepConfigPath (Write-Sweep '{"settled_close": {"enabled": false}}')))
    Check 'enabled true is on' (Test-SettledCloseEnabled -SweepConfigPath (Write-Sweep '{"settled_close": {"enabled": true}}'))
    Check 'unparseable file reads as on' (Test-SettledCloseEnabled -SweepConfigPath (Write-Sweep '{not json'))
    Check 'non-object section reads as on' (Test-SettledCloseEnabled -SweepConfigPath (Write-Sweep '{"settled_close": true}'))
    Check 'non-bool enabled reads as on' (Test-SettledCloseEnabled -SweepConfigPath (Write-Sweep '{"settled_close": {"enabled": "true"}}'))

    $script:invoked = @()
    $fake = { param($a, $t, $l) $script:invoked += , @($a); return [PSCustomObject]@{ ok = $true; code = 0; stdout = '{"phase":"settled_close","enabled":true,"close_calls":0}' } }

    $off = Write-Sweep '{"settled_close": {"enabled": false}}'
    $r = Invoke-SettledCloseTick -Projects @('p1') -SweepConfigPath $off -Invoke $fake
    Check 'off: nothing is spawned' ($script:invoked.Count -eq 0 -and $null -eq $r)

    $on = Write-Sweep '{"settled_close": {"enabled": true}}'
    $r = Invoke-SettledCloseTick -Projects @() -SweepConfigPath $on -Invoke $fake
    Check 'on with no project: nothing is spawned' ($script:invoked.Count -eq 0 -and $null -eq $r)

    $r = Invoke-SettledCloseTick -Projects @('p1', 'p2') -SweepConfigPath $on -Invoke $fake
    $args0 = if ($script:invoked.Count -ge 1) { $script:invoked[0] } else { @() }
    Check 'on: one call' ($script:invoked.Count -eq 1)
    Check 'on: module and --tick' (($args0 -join ' ') -like '-m spirrow_mindwire.settled_close --tick *')
    Check 'on: projects joined' (($args0 -join ' ') -like "*--projects p1,p2*")
    Check 'on: sweep config passed' (($args0 -join ' ') -like "*--sweep-config $on*")
    Check 'on: result parsed' ($null -ne $r -and $r.enabled -eq $true)

    $bad = { param($a, $t, $l) return [PSCustomObject]@{ ok = $false; error = 'timeout' } }
    $r = Invoke-SettledCloseTick -Projects @('p1') -SweepConfigPath $on -Invoke $bad
    Check 'probe failure fails open' ($null -eq $r -and ($script:logs -join "`n") -like '*fail-open*')

    # Wiring in the wrapper.
    $wrapper = Get-Content -LiteralPath (Join-Path $repoRoot 'deploy/run-conductor-scheduled.ps1') -Raw
    Check 'wrapper dot-sources the lib' ($wrapper -match "\. \(Join-Path \`$PSScriptRoot 'lib/SettledClose\.ps1'\)")
    Check 'wrapper calls the tick with the 1c list' ($wrapper -match 'Invoke-SettledCloseTick -Projects \$passedHoldProjects -SweepConfigPath \$sweepConfigPath')
    Check '1c and settled_close share the list' ($wrapper -match 'Invoke-AutoGateTick -Projects \$passedHoldProjects')
}
finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

if ($script:failures -gt 0) { Write-Host "$script:failures failure(s)"; exit 1 }
Write-Host "all passed"
