# The sweep's hold acknowledgement for a parked clone, and the in-flight launch ledger it rests on
# (T-role-exit-leaves-clone-off-default-branch PR-B; design Bohr msg-8838, revised through msg-8848).
#
# What this file pins:
#   * Invoke-HoldAckViaPark reports observed=hold once per pending project that launched nothing
#     this tick and whose ledger is clear; a launch this tick, a row of the project, a `*` sentinel,
#     a missing or unreadable ledger all skip it; a failed report is a WARN and the rest go on
#   * F1 (T-hold-observed-freshness-and-unlaunched-tick-ack): the pending set is every project whose
#     hold Test-HoldAckStale flags (stale ack included), with `--why no-launch`; the parked skip
#     narrows it to `dirty-clone-parked`; the why reaches the reporter. The span that builds the set
#     is lifted from the wrapper and run for real.
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

# A reporter stub shaped like Invoke-BoundedUvProbe's result; records the projects it was called for
# and the `--why` each was called with.
$script:reported = @()
$script:reportedWhy = @{}
$okReport = { param($p, $why) $script:reported += $p; $script:reportedWhy[$p] = $why; @{ ok = $true; code = 0; stdout = ''; stderr = ''; error = $null } }

function Invoke-Ack {
    param([string[]]$Pending, [string[]]$Launched = @(), [string]$Path, [scriptblock]$Report = $okReport, [string]$Why = 'dirty-clone-parked')
    $script:reported = @()
    $script:reportedWhy = @{}
    $pend = @{}; foreach ($p in $Pending) { $pend[$p] = $Why }
    $lp = @{}; foreach ($p in $Launched) { $lp[$p] = $true }
    # The comma keeps a one-result array an array, so callers can index [0].
    return , @(Invoke-HoldAckViaPark -Pending $pend -LaunchedProjects $lp -InflightPath $Path -Report $Report)
}

# Test-HoldAckStale (and its parser) come from the wrapper's AST: dot-sourcing the wrapper would run
# the sweep.
$wrapperPath = Join-Path $repoRoot 'deploy/run-conductor-scheduled.ps1'
$wrapperAst = [System.Management.Automation.Language.Parser]::ParseFile($wrapperPath, [ref]$null, [ref]$null)
foreach ($fname in @('ConvertFrom-ControlTimestamp', 'Test-HoldAckStale')) {
    $fdef = $wrapperAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $fname }, $true) | Select-Object -First 1
    if (-not $fdef) { throw "function not found in sweep script: $fname" }
    Invoke-Expression $fdef.Extent.Text
}

# Shaped like the control probe's ConvertFrom-Json result, timestamps included.
function New-Control {
    param([string]$Desired, $Observed, [string]$DesiredAt = '', [string]$ObservedAt = '')
    $j = [ordered]@{
        desired_state = $Desired; observed_state = $Observed
        desired_at = $(if ($DesiredAt) { $DesiredAt } else { $null })
        observed_at = $(if ($ObservedAt) { $ObservedAt } else { $null })
    }
    return ($j | ConvertTo-Json -Compress | ConvertFrom-Json)
}

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

    Check 'Test-HoldAckViaParkWanted is gone (one verdict: Test-HoldAckStale)' ($null -eq (Get-Command Test-HoldAckViaParkWanted -ErrorAction SilentlyContinue))

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
    $failFirst = { param($q, $w) $script:reported += $q; if ($q -eq 'A') { @{ ok = $true; code = 1; stderr = 'boom' } } else { @{ ok = $true; code = 0 } } }
    $r = Invoke-Ack -Pending @('A', 'B') -Path $p -Report $failFirst
    Check 'reporter fails -> WARN logged' (@($script:logs | Where-Object { $_ -like 'WARN hold-ack via park A: report failed*' }).Count -eq 1)
    Check 'reporter fails -> next project still reported' ((($r | Where-Object { $_.project -eq 'B' }).outcome) -eq 'reported')
    $throwing = { param($q, $w) throw 'launcher exploded' }
    $r = Invoke-Ack -Pending @('A') -Path $p -Report $throwing
    Check 'reporter throws -> failed outcome, no throw' ($r[0].outcome -eq 'failed')

    Write-Host "Invoke-HoldAckViaPark: the why reaches the reporter (F1)"
    $p = New-ClearLedger
    $script:logs = @()
    $r = Invoke-Ack -Pending @('P') -Path $p -Why 'no-launch'
    Check '(4) pending, no launch, ledger clear -> reported with why=no-launch' ((($script:reported -join ',') -eq 'P') -and $script:reportedWhy['P'] -eq 'no-launch' -and $r[0].why -eq 'no-launch')
    Check '(4) the log line names the why' (@($script:logs | Where-Object { $_ -eq 'hold-ack via park P: reported observed=hold (no launch; why=no-launch)' }).Count -eq 1)
    $r = Invoke-Ack -Pending @('P') -Launched @('P') -Path $p -Why 'no-launch'
    Check '(4) pending, launched this tick -> skipped' ($script:reported.Count -eq 0 -and $r[0].reason -eq 'launched this tick')
    $null = Add-InflightMark -Path $p -LaunchId 'Ln' -Project 'P' -Key 'P/T' -NowIso $now
    $r = Invoke-Ack -Pending @('P') -Path $p -Why 'no-launch'
    Check '(4) pending, a row of P in the ledger -> skipped' ($script:reported.Count -eq 0 -and $r[0].reason -eq 'inflight-row Ln')
    $p = New-LedgerPath; Write-Ledger $p '{"*": {"project": "*", "reason": "absent"}}'
    $r = Invoke-Ack -Pending @('P') -Path $p -Why 'no-launch'
    Check '(4) pending, a * row -> skipped' ($script:reported.Count -eq 0 -and $r[0].reason -eq 'inflight-sentinel (absent)')
    $p = New-ClearLedger
    $r = Invoke-Ack -Pending @('P') -Path $p -Why 'dirty-clone-parked'
    Check '(5) parked path -> reported with why=dirty-clone-parked' ($script:reportedWhy['P'] -eq 'dirty-clone-parked')

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

    # PR-gate #560 round 1, objection 1: the ledger must exist even when no tick ever launches.
    Write-Host "Tick-start initialization (every candidate parked)"
    $p = New-LedgerPath
    $init = Initialize-InflightLedger -Path $p -NowIso $now
    $read = Read-InflightLedger -Path $p
    Check 'missing ledger -> created holding only the * (absent) sentinel' ($init.ok -and $init.created -and $read.status -eq 'ok' -and $read.rows.Count -eq 1 -and $read.rows['*'].reason -eq 'absent')
    Check 'creation is logged as an event' ($init.events -contains 'inflight-file-created (absent; sentinel set)')
    # The all-parked tick: initialization, then no launch at all, then the acknowledgement pass.
    $r = Invoke-Ack -Pending @('P') -Path $p
    Check 'all-parked first tick: no report, reason is the * sentinel (not inflight-file-absent)' ($script:reported.Count -eq 0 -and $r[0].reason -eq 'inflight-sentinel (absent)')
    $obj = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json
    $obj.PSObject.Properties.Remove('*')
    Write-Ledger $p ($obj | ConvertTo-Json)
    $init2 = Initialize-InflightLedger -Path $p -NowIso $now
    $r = Invoke-Ack -Pending @('P') -Path $p
    Check 'after the operator clears *: the next tick does not re-add it and reports' ($init2.ok -and -not $init2.created -and ($script:reported -join ',') -eq 'P')
    $p = New-LedgerPath
    $seed = '{"La": {"project": "A", "key": "A/T"}}'
    Write-Ledger $p $seed
    $null = Initialize-InflightLedger -Path $p -NowIso $now
    Check 'existing ledger -> left byte-for-byte as it was' ([System.IO.File]::ReadAllText($p) -eq $seed)
    $p = New-LedgerPath
    Write-Ledger $p '{not json'
    $null = Initialize-InflightLedger -Path $p -NowIso $now
    Check 'corrupt ledger -> left as it was (the gate still refuses it)' ([System.IO.File]::ReadAllText($p) -eq '{not json' -and (Test-InflightClearForProject -Path $p -Project 'P').clear -eq $false)
    $p = New-LedgerPath
    $init = Initialize-InflightLedger -Path $p -NowIso $now -Io (New-FaultyIo -Fail 'Move')
    Check 'failed create -> ok=$false, no file, gate still says absent' (-not $init.ok -and -not (Test-Path -LiteralPath $p) -and (Test-InflightClearForProject -Path $p -Project 'P').reason -eq 'inflight-file-absent')
    $p = New-LedgerPath
    $race = New-InflightLedgerIo
    $race.Move = { param($t, $dest) [System.IO.File]::WriteAllText($dest, '{"Lz": {"project": "Z"}}'); throw 'exists' }
    $init = Initialize-InflightLedger -Path $p -NowIso $now -Io $race
    $rows = (Read-InflightLedger -Path $p).rows
    Check 'a launch row that appears under the create is kept' ($init.ok -and $rows.ContainsKey('Lz'))
}
finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Wiring (deploy/run-conductor-scheduled.ps1)"
$src = Get-Content -LiteralPath (Join-Path $repoRoot 'deploy/run-conductor-scheduled.ps1') -Raw
Check 'wrapper dot-sources the lib' ($src -match "\. \(Join-Path \`$PSScriptRoot 'lib/InflightLedger\.ps1'\)")
$iHeld = $src.IndexOf('if (Test-HoldObserved -Control $control) {')
$iParked = $src.IndexOf("`$dispositions[`$cand.key] = 'dirty-clone-parked'")
$iWanted = $src.IndexOf('if ($holdAckViaPark.ContainsKey($cand.project)) { $holdAckViaPark[$cand.project] = ''dirty-clone-parked'' }')
Check 'hold check precedes the parked check; the parked skip narrows the why' ($iHeld -gt 0 -and $iParked -gt $iHeld -and $iWanted -gt $iParked)
Check 'the PR-B predicate is no longer called' ($src -notmatch 'Test-HoldAckViaParkWanted')
$iMark = $src.IndexOf('$mark = Add-InflightMark')
$iInit = $src.IndexOf('$inflightInit = Initialize-InflightLedger -Path $inflightStatePath')
$iFirstCand = $src.IndexOf('foreach ($cand in $candidates) {')
Check 'the ledger is initialized every tick, before any candidate (incl. the parked check)' ($iInit -gt 0 -and $iFirstCand -gt $iInit -and $iParked -gt $iFirstCand -and $iMark -gt $iFirstCand)
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
Check 'report uses the hold-only flag through the bounded probe, with the why' ($src -match "Invoke-BoundedUvProbe -Label `"hold-ack-\`$proj`"" -and $src.Contains("'--report-observed-hold', '--project', `$proj, '--why', `$why") -and $src.Contains('param($proj, $why)'))

# F1: the span that builds the pending set, lifted from the wrapper and run for real.
Write-Host "Pending set (F1): every hold Test-HoldAckStale flags, why=no-launch"
$iPendStart = $src.IndexOf('    $holdAckViaPark = @{}')
$iPendEnd = $src.IndexOf('    $launchedProjects = @{}', $iPendStart)
Check 'the pending set is built before the candidate loop' ($iPendStart -gt 0 -and $iPendEnd -gt $iPendStart -and $iPendEnd -lt $iFirstCand)
$pendSpan = $src.Substring($iPendStart, $iPendEnd - $iPendStart)
Check 'the pending set uses Test-HoldAckStale' ($pendSpan.Contains('Test-HoldAckStale -Control $controlByProject[$ackProj]'))
$controlByProject = @{
    'Stale'   = New-Control 'hold' 'hold' '2026-09-08T12:00:00.106309Z' '2026-09-08T12:00:00Z'
    'Old'     = New-Control 'hold' 'hold' '2026-09-08T12:00:00Z' '2026-09-08T11:00:00Z'
    'Never'   = New-Control 'hold' $null '2026-09-08T12:00:00Z'
    'RunObs'  = New-Control 'hold' 'run' '2026-09-08T12:00:00Z' '2026-09-08T12:00:05Z'
    'Fresh'   = New-Control 'hold' 'hold' '2026-09-08T12:00:00Z' '2026-09-08T12:00:00.106309Z'
    'NoTsAck' = New-Control 'hold' 'hold'
    'Run'     = New-Control 'run' 'run'
    'Unread'  = $null
}
. ([scriptblock]::Create($pendSpan))
$keys = (@($holdAckViaPark.Keys | Sort-Object) -join ',')
Check '(1)/(2a) stale, older, never, run-observed -> pending; fresh, no-timestamp ack, run, unreadable -> not' ($keys -eq 'Never,Old,RunObs,Stale') "got [$keys]"
Check 'every pending project starts as no-launch' (@($holdAckViaPark.Values | Where-Object { $_ -ne 'no-launch' }).Count -eq 0)

# F4c: the warning line is kept even on a tick that does nothing else.
Write-Host "HOLD NOT ACKNOWLEDGED is kept (F4c)"
$iWarn = $src.IndexOf('Write-Log ("HOLD NOT ACKNOWLEDGED [{0}]')
$iVerdict = $src.LastIndexOf('$ackVerdict = Test-HoldAckStale -Control $c', $iWarn)
$warnSpan = if ($iVerdict -gt 0 -and $iWarn -gt $iVerdict) { $src.Substring($iVerdict, $iWarn - $iVerdict) } else { '' }
$iIf = $warnSpan.IndexOf('if ($null -ne $ackVerdict) {')
$iKeep = $warnSpan.IndexOf('Confirm-LogWorthKeeping')
Check '(6) Confirm-LogWorthKeeping runs inside the warning branch, before the line' ($iIf -gt 0 -and $iKeep -gt $iIf)


# PR-B addendum (Bohr msg-8857, go Einstein): a launch that ends between the mark and `& $inner`
# never spawned a child, so it removes its OWN row only. The span is lifted from the wrapper source
# and run for real against a commit-launch that declines.
Write-Host "Not launched after the mark (commit-launch declines)"
$iTry = $src.IndexOf("        try {`n            `$commitResult = Invoke-HeadSkipCommitLaunch")
$iCatchEnd = $src.IndexOf("            throw`n        }`n", $iTry)
Check 'the try opens after the mark block and before commit-launch' ($iTry -gt $iMark -and $iTry -lt $iCommit)
Check 'the catch closes before the spawn; `& $inner` is outside the try' ($iCatchEnd -gt $iCommit -and $iCatchEnd -lt $iInner -and
    $src.Substring($iCatchEnd, $iInner - $iCatchEnd) -notmatch '\btry\s*\{')
$span = $src.Substring($iTry, ($iCatchEnd + "            throw`n        }`n".Length) - $iTry)
Check 'the catch removes only this launch''s row and rethrows' ($span -match 'catch \{\s*\$undo = Remove-InflightMark -Path \$inflightStatePath -LaunchId \$inflightLaunchId' -and $span -match '\n\s*throw\n\s*\}\s*$')
function Invoke-HeadSkipCommitLaunch { param($Payload, $StateFilePath) @{ ok = $false; error = 'stub decline' } }
$tmp2 = Join-Path ([System.IO.Path]::GetTempPath()) ("hold-ack-park-decline-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp2 | Out-Null
try {
    foreach ($withSentinel in @($true, $false)) {
        $inflightStatePath = Join-Path $tmp2 "ledger-$withSentinel.json"
        $seed = if ($withSentinel) { '{"Lq": {"project": "Q", "key": "Q/T"}, "*": {"project": "*", "reason": "absent"}}' } else { '{"Lq": {"project": "Q", "key": "Q/T"}}' }
        [System.IO.File]::WriteAllText($inflightStatePath, $seed)
        $mark = Add-InflightMark -Path $inflightStatePath -Project 'P' -Key 'P/T' -NowIso $now
        $inflightLaunchId = $mark.launch_id
        $cand = [pscustomobject]@{ key = 'P/T'; project = 'P' }
        $commitPayload = @{}; $headSkipStatePath = 'unused'
        $thrown = $null
        try { . ([scriptblock]::Create($span)) } catch { $thrown = $_.Exception.Message }
        $rows = (Read-InflightLedger -Path $inflightStatePath).rows
        $tag = if ($withSentinel) { '[with *]' } else { '[no *]' }
        Check "$tag the decline is rethrown (tick still aborts)" ("$thrown" -like '*commit-launch systemic failure*')
        Check "$tag own row removed" ($mark.ok -and -not $rows.ContainsKey($inflightLaunchId))
        Check "$tag other project's row kept" ($rows.ContainsKey('Lq'))
        if ($withSentinel) {
            Check "$tag * sentinel kept" ($rows.ContainsKey('*'))
            $r = Invoke-Ack -Pending @('P') -Path $inflightStatePath
            Check "$tag next tick: * present -> still no report" ($script:reported.Count -eq 0 -and $r[0].reason -eq 'inflight-sentinel (absent)')
        }
        else {
            $r = Invoke-Ack -Pending @('P') -Path $inflightStatePath
            Check "$tag next tick: parked, on hold, no row, no * -> reports" (($script:reported -join ',') -eq 'P')
        }
    }
}
finally { Remove-Item -LiteralPath $tmp2 -Recurse -Force -ErrorAction SilentlyContinue }

# PR-gate #560 round 1, objection 2: the operator edits the ledger only with the sweep stopped.
Write-Host "Clearing procedure (docs/deploy.md)"
$doc = Get-Content -LiteralPath (Join-Path $repoRoot 'docs/deploy.md') -Raw
$iClear = $doc.IndexOf('**Clearing (operator).**')
$iEnd = $doc.IndexOf('## Stall ledger tick')
$proc = $doc.Substring($iClear, $iEnd - $iClear)
$iDisable = $proc.IndexOf('Disable-ScheduledTask'); $iWait = $proc.IndexOf("-match 'run-conductor-scheduled' }) { Start-Sleep")
$iProc = $proc.IndexOf('**Check the processes.**'); $iSet = $proc.IndexOf('Set-Content -LiteralPath $p'); $iEnable = $proc.IndexOf('Enable-ScheduledTask')
Check 'stop -> wait for the running sweep -> process check -> edit -> resume, in that order' ($iDisable -gt 0 -and $iWait -gt $iDisable -and $iProc -gt $iWait -and $iSet -gt $iProc -and $iEnable -gt $iSet)

if ($script:failures -gt 0) { Write-Host "$script:failures check(s) FAILED"; exit 1 }
Write-Host "all checks passed"
exit 0
