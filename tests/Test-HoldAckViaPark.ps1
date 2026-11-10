# The sweep's hold acknowledgement for a parked clone, and the in-flight launch ledger it rests on
# (T-role-exit-leaves-clone-off-default-branch PR-B; design Bohr msg-8838, revised through msg-8848).
#
# What this file pins:
#   * Invoke-HoldAckViaPark reports observed=hold once per pending project that launched nothing
#     this tick and whose ledger is clear; a launch this tick, a row of the project, a `*` sentinel,
#     a missing or unreadable ledger all skip it; a failed report is a WARN and the rest go on
#   * Test-HoldAckViaParkWanted: hold not yet observed -> wanted; already observed / run -> not
#   * the ledger: a launch's row is added before and only its own row removed after; a failed write
#     leaves the old file untouched; a corrupt file is copied (never renamed) and replaced by a `*`
#     sentinel; a missing file is created holding a `*` sentinel; every failure path keeps the next
#     tick from reporting
#   * wiring in deploy/run-conductor-scheduled.ps1 (source contract)
#
# Repo convention: plain pwsh with a Check helper, not Pester.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'deploy/lib/InflightLedger.ps1')

$script:failures = 0
function Check {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} {1}" -f $Name, $Detail) }
}

$script:logs = @()
function Write-Log { param([string]$Message) $script:logs += $Message }

$now = '2026-11-10T01:00:00.0000000Z'

# A reporter stub shaped like Invoke-BoundedUvProbe's result; records the projects it was called for.
$script:reported = @()
$okReport = { param($p) $script:reported += $p; @{ ok = $true; code = 0; stdout = ''; stderr = ''; error = $null } }

function Invoke-Ack {
    param([string[]]$Pending, [string[]]$Launched = @(), [string]$Path, [scriptblock]$Report = $okReport)
    $script:reported = @()
    $pend = @{}; foreach ($p in $Pending) { $pend[$p] = $true }
    $lp = @{}; foreach ($p in $Launched) { $lp[$p] = $true }
    # The comma keeps a one-result array an array, so callers can index [0].
    return , @(Invoke-HoldAckViaPark -Pending $pend -LaunchedProjects $lp -InflightPath $Path -Report $Report)
}

function New-Control { param([string]$Desired, $Observed) [pscustomobject]@{ desired_state = $Desired; observed_state = $Observed } }

# Fault-injecting I/O: the named step throws, the others are the real ones.
function New-FaultyIo {
    param([string]$Fail)
    $io = New-InflightLedgerIo
    $io[$Fail] = { throw "injected $Fail failure" }
    return $io
}

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("hold-ack-park-test-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    $n = 0
    function New-LedgerPath { $script:n++; $d = Join-Path $tmp "case$script:n"; New-Item -ItemType Directory -Path $d | Out-Null; return (Join-Path $d 'conductor-inflight.json') }
    function Write-Ledger { param([string]$Path, [string]$Text) [System.IO.File]::WriteAllText($Path, $Text) }
    # A ledger that exists and is empty: the state after an operator cleared the `*` row.
    function New-ClearLedger { $p = New-LedgerPath; Write-Ledger $p '{}'; return $p }

    Write-Host "Test-HoldAckViaParkWanted"
    Check 'hold, not acknowledged -> wanted' (Test-HoldAckViaParkWanted -Control (New-Control 'hold' 'run'))
    Check 'hold, never acknowledged (null) -> wanted' (Test-HoldAckViaParkWanted -Control (New-Control 'hold' $null))
    Check 'hold, already acknowledged -> not wanted' (-not (Test-HoldAckViaParkWanted -Control (New-Control 'hold' 'hold')))
    Check 'run -> not wanted' (-not (Test-HoldAckViaParkWanted -Control (New-Control 'run' 'run')))
    Check 'unreadable control -> not wanted' (-not (Test-HoldAckViaParkWanted -Control $null))

    Write-Host "Invoke-HoldAckViaPark (msg-8838 cases)"
    $p = New-ClearLedger
    $r = Invoke-Ack -Pending @('P') -Path $p
    Check 'hold, not acknowledged, parked -> one call for P' (($script:reported -join ',') -eq 'P') "got [$($script:reported -join ',')]"
    Check 'outcome reported' ($r[0].outcome -eq 'reported')
    Check 'log names the report' (@($script:logs | Where-Object { $_ -like 'hold-ack via park P: reported observed=hold*' }).Count -eq 1)
    $r = Invoke-Ack -Pending @() -Path $p
    Check 'nothing pending (acknowledged / run) -> no call' ($script:reported.Count -eq 0)
    $r = Invoke-Ack -Pending @('P') -Launched @('P') -Path $p
    Check 'another candidate of P launched this tick -> no call' ($script:reported.Count -eq 0)
    Check 'skip reason names the launch' ($r[0].reason -eq 'launched this tick')
    $script:logs = @()
    $failFirst = { param($q) $script:reported += $q; if ($q -eq 'A') { @{ ok = $true; code = 1; stderr = 'boom' } } else { @{ ok = $true; code = 0 } } }
    $r = Invoke-Ack -Pending @('A', 'B') -Path $p -Report $failFirst
    Check 'reporter fails -> WARN logged' (@($script:logs | Where-Object { $_ -like 'WARN hold-ack via park A: report failed*' }).Count -eq 1)
    Check 'reporter fails -> next project still reported' ((($r | Where-Object { $_.project -eq 'B' }).outcome) -eq 'reported')
    $throwing = { param($q) throw 'launcher exploded' }
    $r = Invoke-Ack -Pending @('A') -Path $p -Report $throwing
    Check 'reporter throws -> failed outcome, no throw' ($r[0].outcome -eq 'failed')

    Write-Host "Ledger: normal exit"
    $p = New-ClearLedger
    $add = Add-InflightMark -Path $p -LaunchId 'L1' -Project 'P' -Key 'P/T1' -NowIso $now
    Check 'mark written before launch' ($add.ok -and (Read-InflightLedger -Path $p).rows.ContainsKey('L1'))
    $auto = Add-InflightMark -Path $p -Project 'R' -Key 'R/T' -NowIso $now
    Check 'no -LaunchId -> a fresh GUID row, returned as launch_id' ($auto.ok -and $auto.launch_id -match '^[0-9a-f-]{36}$' -and (Read-InflightLedger -Path $p).rows.ContainsKey($auto.launch_id))
    $null = Remove-InflightMark -Path $p -LaunchId $auto.launch_id -NowIso $now
    $r = Invoke-Ack -Pending @('P') -Path $p
    Check 'while the row is there -> no report' ($script:reported.Count -eq 0 -and $r[0].reason -eq 'inflight-row L1')
    $null = Add-InflightMark -Path $p -LaunchId 'Lq' -Project 'Q' -Key 'Q/T' -NowIso $now
    $rm = Remove-InflightMark -Path $p -LaunchId 'L1' -NowIso $now
    $rows = (Read-InflightLedger -Path $p).rows
    Check '(a) normal exit removes only its own row' ($rm.ok -and -not $rows.ContainsKey('L1') -and $rows.ContainsKey('Lq'))
    $r = Invoke-Ack -Pending @('P') -Path $p
    Check '(b) next tick, parked + hold, no row -> report' (($script:reported -join ',') -eq 'P')
    $r = Invoke-Ack -Pending @('P') -Launched @('P') -Path $p
    Check '(c) same tick P launched and exited normally -> no report' ($script:reported.Count -eq 0)

    Write-Host "Ledger: exit 7 / failed delete"
    $p = New-ClearLedger
    $null = Add-InflightMark -Path $p -LaunchId 'L7' -Project 'P' -Key 'P/T' -NowIso $now
    # exit 7: the wrapper does not call Remove-InflightMark.
    $r = Invoke-Ack -Pending @('P') -Path $p
    Check 'exit 7: row stays -> next tick no report' ($script:reported.Count -eq 0)
    $p = New-ClearLedger
    $null = Add-InflightMark -Path $p -LaunchId 'Ld' -Project 'P' -Key 'P/T' -NowIso $now
    $before = [System.IO.File]::ReadAllText($p)
    $rm = Remove-InflightMark -Path $p -LaunchId 'Ld' -NowIso $now -Io (New-FaultyIo 'Replace')
    Check 'delete fails -> ok=false' (-not $rm.ok)
    Check 'delete fails -> old file kept byte for byte' ([System.IO.File]::ReadAllText($p) -eq $before)
    Check 'delete fails -> no temp file left' (@(Get-ChildItem -LiteralPath (Split-Path $p) -Filter '*.tmp-*').Count -eq 0)
    $r = Invoke-Ack -Pending @('P') -Path $p
    Check 'delete fails -> next tick no report' ($script:reported.Count -eq 0)

    Write-Host "Ledger: pre-launch write fails"
    $p = New-ClearLedger
    $null = Add-InflightMark -Path $p -LaunchId 'L0' -Project 'Q' -Key 'Q/T' -NowIso $now
    $before = [System.IO.File]::ReadAllText($p)
    foreach ($step in @('WriteTemp', 'Replace')) {
        $add = Add-InflightMark -Path $p -LaunchId 'Lx' -Project 'P' -Key 'P/T' -NowIso $now -Io (New-FaultyIo $step)
        Check "pre-launch $step fails -> ok=false (caller does not launch)" (-not $add.ok)
        Check "pre-launch $step fails -> old file unchanged" ([System.IO.File]::ReadAllText($p) -eq $before)
    }

    Write-Host "Ledger: corrupt file"
    $p = New-LedgerPath
    Write-Ledger $p '{not json'
    $add = Add-InflightMark -Path $p -LaunchId 'L1' -Project 'P' -Key 'P/T' -NowIso $now
    $copies = @(Get-ChildItem -LiteralPath (Split-Path $p) -Filter 'conductor-inflight.json.corrupt-*')
    $read = Read-InflightLedger -Path $p
    Check 'corrupt: launch continues (mark ok)' $add.ok
    Check 'corrupt: copy created with the original bytes' ($copies.Count -eq 1 -and [System.IO.File]::ReadAllText($copies[0].FullName) -eq '{not json')
    Check 'corrupt: path now holds the * sentinel (corrupt) and the new row' ($read.status -eq 'ok' -and $read.rows['*'].reason -eq 'corrupt' -and $read.rows.ContainsKey('L1'))
    Check 'corrupt: an event line is returned' (@($add.events | Where-Object { $_ -like 'inflight-file-corrupt*' }).Count -eq 1)
    $null = Remove-InflightMark -Path $p -LaunchId 'L1' -NowIso $now
    $r = Invoke-Ack -Pending @('P', 'Q') -Path $p
    Check 'corrupt: no project gets a report' ($script:reported.Count -eq 0)

    $p = New-LedgerPath
    Write-Ledger $p '[1,2]'
    $add = Add-InflightMark -Path $p -LaunchId 'L1' -Project 'P' -Key 'P/T' -NowIso $now -Io (New-FaultyIo 'Replace')
    Check 'copy ok, Replace fails -> ok=false (nothing launches)' (-not $add.ok)
    Check 'copy ok, Replace fails -> path still holds the corrupt original' ([System.IO.File]::ReadAllText($p) -eq '[1,2]')
    $r = Invoke-Ack -Pending @('P') -Path $p
    Check 'copy ok, Replace fails -> next tick still no report' ($script:reported.Count -eq 0 -and $r[0].reason -like 'inflight-file-unreadable*')
    $add = Add-InflightMark -Path $p -LaunchId 'L2' -Project 'P' -Key 'P/T' -NowIso '2026-11-10T01:05:00Z'
    Check 'next tick goes through the same steps and recovers' ($add.ok -and (Read-InflightLedger -Path $p).rows['*'].reason -eq 'corrupt')
    $p = New-LedgerPath
    Write-Ledger $p '{"L1": {"key": "P/T"}}'
    Check 'a row without project reads as corrupt' ((Read-InflightLedger -Path $p).status -eq 'corrupt')

    Write-Host "Ledger: missing file"
    $p = New-LedgerPath
    $r = Invoke-Ack -Pending @('P') -Path $p
    Check 'path missing at the strict read -> no report' ($script:reported.Count -eq 0 -and $r[0].reason -eq 'inflight-file-absent')
    $add = Add-InflightMark -Path $p -LaunchId 'L1' -Project 'Q' -Key 'Q/T' -NowIso $now
    $read = Read-InflightLedger -Path $p
    Check 'missing: created, launch continues' ($add.ok -and $read.status -eq 'ok')
    Check 'missing: created holding the * sentinel (absent)' ($read.rows['*'].reason -eq 'absent')
    Check 'missing: the log line is returned' (@($add.events | Where-Object { $_ -eq 'inflight-file-created (absent; sentinel set)' }).Count -eq 1)
    $null = Remove-InflightMark -Path $p -LaunchId 'L1' -NowIso $now
    # The objection's case: an orphan from before the deploy is alive (nothing in this tick knows
    # about it), P is parked and on hold, first tick after deploy. The sentinel alone must stop it.
    $r = Invoke-Ack -Pending @('P') -Path $p
    Check 'pre-deploy orphan, first tick -> no report' ($script:reported.Count -eq 0 -and $r[0].reason -eq 'inflight-sentinel (absent)')
    # Operator clears the * row by hand (docs/deploy.md).
    $obj = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json
    $obj.PSObject.Properties.Remove('*')
    Write-Ledger $p ($obj | ConvertTo-Json)
    $r = Invoke-Ack -Pending @('P') -Path $p
    Check 'after the * row is removed -> next tick reports' (($script:reported -join ',') -eq 'P')
    $p2 = New-LedgerPath
    $moveRace = New-InflightLedgerIo
    $moveRace.Move = { param($t, $dest) [System.IO.File]::WriteAllText($dest, '{"Lz": {"project": "Z"}}'); throw 'exists' }
    $add = Add-InflightMark -Path $p2 -LaunchId 'L1' -Project 'P' -Key 'P/T' -NowIso $now -Io $moveRace
    $rows = (Read-InflightLedger -Path $p2).rows
    Check 'file appears before the move -> change re-applied over it, nothing lost' ($add.ok -and $rows.ContainsKey('Lz') -and $rows.ContainsKey('L1'))

    Write-Host "Ledger: rows are separate"
    $p = New-ClearLedger
    $null = Add-InflightMark -Path $p -LaunchId 'Lp' -Project 'P' -Key 'P/T' -NowIso $now
    $r = Invoke-Ack -Pending @('P', 'Q') -Path $p
    Check "P's row does not exclude Q" (($script:reported -join ',') -eq 'Q')
    $null = Add-InflightMark -Path $p -LaunchId 'Lp2' -Project 'P' -Key 'P/T2' -NowIso $now
    $null = Remove-InflightMark -Path $p -LaunchId 'Lp2' -NowIso $now
    Check 'two launches of P keep each other''s rows' ((Read-InflightLedger -Path $p).rows.ContainsKey('Lp'))
    $p = New-ClearLedger
    $null = Add-InflightMark -Path $p -LaunchId 'Lx' -Project 'P-x' -Key 'P-x/T' -NowIso $now
    $r = Invoke-Ack -Pending @('P') -Path $p
    Check "P-x's row does not exclude P (exact match)" (($script:reported -join ',') -eq 'P')
}
finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Wiring (deploy/run-conductor-scheduled.ps1)"
$src = Get-Content -LiteralPath (Join-Path $repoRoot 'deploy/run-conductor-scheduled.ps1') -Raw
Check 'wrapper dot-sources the lib' ($src -match "\. \(Join-Path \`$PSScriptRoot 'lib/InflightLedger\.ps1'\)")
$iHeld = $src.IndexOf('if (Test-HoldObserved -Control $control) {')
$iParked = $src.IndexOf("`$dispositions[`$cand.key] = 'dirty-clone-parked'")
$iWanted = $src.IndexOf('if (Test-HoldAckViaParkWanted -Control $control) { $holdAckViaPark[$cand.project] = $true }')
Check 'hold check precedes the parked check; the parked skip records the project' ($iHeld -gt 0 -and $iParked -gt $iHeld -and $iWanted -gt $iParked)
$iMark = $src.IndexOf('$mark = Add-InflightMark')
$iCommit = $src.IndexOf('$commitResult = Invoke-HeadSkipCommitLaunch')
$iInner = $src.IndexOf('$output = (& $inner')
Check 'mark is written before commit-launch and before the spawn' ($iMark -gt 0 -and $iMark -lt $iCommit -and $iCommit -lt $iInner)
$markBlock = $src.Substring($iMark, $iCommit - $iMark)
Check 'a refused mark breaks with inflight-mark-unwritable' ($markBlock -match "breakReason = 'inflight-mark-unwritable'" -and $markBlock -match '\bbreak\b')
$iUnmark = $src.IndexOf('$unmark = Remove-InflightMark')
Check 'own row removed right after the run, guarded on exit 7' ($iUnmark -gt $iInner -and $src.Substring($iInner, $iUnmark - $iInner) -match 'if \(\$code -ne \$ConductorKillUnconfirmedExitCode\)')
$iAck = $src.IndexOf('Invoke-HoldAckViaPark -Pending $holdAckViaPark')
$iNotReached = $src.IndexOf("`$dispositions[`$cand.key] = 'not-reached'")
Check 'report runs after the candidate loop' ($iAck -gt $iUnmark -and $iAck -lt $iNotReached)
Check 'report uses the hold-only flag through the bounded probe' ($src -match "Invoke-BoundedUvProbe -Label `"hold-ack-\`$proj`"" -and $src -match "'--report-observed-hold', '--project', \`$proj")

if ($script:failures -gt 0) { Write-Host "$script:failures check(s) FAILED"; exit 1 }
Write-Host "all checks passed"
exit 0
