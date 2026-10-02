# deploy/lib/ConductorBudget.ps1 — the hard wall-clock budget for one conductor run (W-2).
#
# Thread: T-agmsg-transport-lessons-readiness-session-claim-board (Bohr msg-5496 W-2, revised in
# msg-5498 after Einstein msg-5497; endorsed by Einstein msg-5499 / msg-5501). Dot-sourced by
# deploy/run-conductor.ps1 and by tests/Test-ConductorWallClock.ps1.
#
# WHY HERE and not in the sweep: tests/Test-SweepSequentiality.ps1 pins the sweep's dispatch body
# (exactly one `& $inner`, no Background pipeline, $inner = run-conductor.ps1). This file runs
# INSIDE that one synchronous call, so the pin is untouched and the call stays blocking.
#
# The soft budget lives in Python (src/spirrow_mindwire/conductor/run_budget.py) and normally
# fires first: soft + 30 s (the stop-notice post budget) < hard, checked at daemon startup. This
# one is the backstop for what the soft one cannot see — an event loop blocked by a synchronous
# call never runs its timer.
#
# THE CONTRACT
#   * The child's stdout / stderr go to two FILES, not pipes (Einstein msg-5497 objection 3): a
#     file cannot fill up, so a chatty child can never deadlock against an unread pipe, and a
#     surviving grandchild holding a handle cannot hang the read either.
#   * After the child exits (or is killed), the files are written back to this script's output,
#     stderr first and then stdout. The sweep captures `& $inner *>&1`, merging both, so the text
#     it sees is the same as before. The interleaving between the two streams is not preserved;
#     stdout goes last because the daemon's exit-time lines (traceback, adapter-error stop line,
#     SDK marker; loop_runner.main) are on stdout and the PR #181 contract wants the marker last.
#     Python logging (every `conductor stopped:` / `conductor.*` event line) is on stderr.
#   * On the hard budget: kill the whole tree (Kill(entireProcessTree)), then confirm every
#     process that was in the tree just before the kill is gone. Append the line
#     `conductor.run_killed budget_s=<n> pid=<root>` and return exit 6. If any of them is still
#     alive after the grace period, or the tree could not be listed, return exit 7 (fail-closed:
#     an orphan may still be running, and the sweep stops the rest of its tick).
#   * The two files are created in a caller-named directory that is made private to the current
#     user (0700 on Linux/macOS), never in the shared system temp dir.
#   * The temp files are removed in `finally`; a failure to remove them is a warning, never a
#     change to the result.

$ConductorRunKilledExitCode = 6
$ConductorKillUnconfirmedExitCode = 7
# Mirrors src/spirrow_mindwire/conductor/run_budget.py EVENT_KIND_RUN_KILLED (a pytest pins the two).
$ConductorRunKilledEventKind = 'conductor.run_killed'
# Mirrors src/spirrow_mindwire/config.py DEFAULT_CONDUCTOR_RUN_HARD_BUDGET_S (a pytest pins the two).
$DefaultConductorRunHardBudgetSeconds = 4840
$ConductorKillGraceMs = 10000

# The hard budget, read the way the Python settings read it: the environment override first, then
# [conductor].run_hard_budget_s in mindwire.toml, then the default. A value that is not a positive
# number is an error (throw) rather than a silent fall back to the default: a typo must not quietly
# turn a 30-minute budget into 80 minutes.
function Get-ConductorHardBudgetSeconds {
    param(
        [string]$ConfigPath,
        [string]$EnvValue = $env:MINDWIRE_CONDUCTOR__RUN_HARD_BUDGET_S
    )
    $raw = $null
    $source = $null
    if (-not [string]::IsNullOrWhiteSpace($EnvValue)) {
        $raw = $EnvValue.Trim()
        $source = 'MINDWIRE_CONDUCTOR__RUN_HARD_BUDGET_S'
    }
    elseif ($ConfigPath -and (Test-Path -LiteralPath $ConfigPath)) {
        $section = $null
        foreach ($line in (Get-Content -LiteralPath $ConfigPath -Encoding utf8)) {
            $t = $line.Trim()
            if ($t -match '^\[([^\]]+)\]\s*(#.*)?$') { $section = $Matches[1].Trim(); continue }
            if ($section -eq 'conductor' -and $t -match '^run_hard_budget_s\s*=\s*([^#]+?)\s*(#.*)?$') {
                # A duplicate key is a TOML error: tomllib (which the Python settings use) refuses
                # the file ("Cannot overwrite a value"). Throw here too, rather than picking the
                # first or the last occurrence and silently disagreeing with the soft budget's view.
                if ($null -ne $raw) {
                    throw "$ConfigPath defines [conductor].run_hard_budget_s more than once (TOML forbids duplicate keys)"
                }
                $raw = $Matches[1].Trim()
                $source = "$ConfigPath [conductor].run_hard_budget_s"
            }
        }
    }
    if ($null -eq $raw) { return [double]$DefaultConductorRunHardBudgetSeconds }
    $value = 0.0
    $ok = [double]::TryParse($raw.Replace('_', ''), [System.Globalization.NumberStyles]::Float,
        [System.Globalization.CultureInfo]::InvariantCulture, [ref]$value)
    if (-not $ok -or $value -le 0) {
        throw "run_hard_budget_s from $source is not a positive number: '$raw'"
    }
    return $value
}

# Parse `ps -o pid -o ppid` output into @{ pid = ppid }. Lines whose first two fields are not both
# integers (the header, blank lines) are skipped. A one-column output yields an empty table, which
# the caller treats as "cannot confirm".
function ConvertFrom-PsPidPpidRows {
    param([AllowNull()][AllowEmptyCollection()][object[]]$Rows)
    $parentOf = @{}
    foreach ($r in @($Rows)) {
        $f = @("$r".Trim() -split '\s+')
        if ($f.Count -lt 2) { continue }
        $id = 0; $parent = 0
        if ([int]::TryParse($f[0], [ref]$id) -and [int]::TryParse($f[1], [ref]$parent)) {
            $parentOf[$id] = $parent
        }
    }
    return $parentOf
}

# Create $Path if needed and make it private to the current user. The conductor's stdout/stderr
# files hold the whole thread context, prompts and source, so they must not land where other users
# can read them (PR #407 gate, REQUEST_CHANGES @ 42a6b24). On Linux/macOS the directory is set to
# 0700, so the files in it are unreachable to other users whatever the umask. On Windows the
# directory sits under the data dir and inherits its ACL. A failure to restrict it throws: the run
# does not start with its output exposed.
function Initialize-ConductorPrivateDirectory {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        New-Item -ItemType Directory -Path $Path -Force -ErrorAction Stop | Out-Null
    }
    if (-not $IsWindows) {
        [System.IO.File]::SetUnixFileMode($Path, [System.IO.UnixFileMode]'UserRead, UserWrite, UserExecute')
    }
}

# Every process in the tree rooted at $RootId, as @{ id; start } (start = StartTime ticks, or $null
# when the OS will not say). Returns $null when the process table cannot be read — the caller treats
# that as "cannot confirm".
function Get-ProcessTreeSnapshot {
    param([Parameter(Mandatory)][int]$RootId)
    $parentOf = @{}
    try {
        if ($IsWindows) {
            foreach ($p in (Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)) {
                $parentOf[[int]$p.ProcessId] = [int]$p.ParentProcessId
            }
        }
        else {
            # `-o pid -o ppid` (one keyword per -o) is accepted by both procps (Linux) and BSD ps
            # (macOS); BSD rejects the combined `pid=,ppid=` form. It prints a header line, which
            # ConvertFrom-PsPidPpidRows skips.
            $rows = & ps -A -o pid -o ppid 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $rows) { return $null }
            $parentOf = ConvertFrom-PsPidPpidRows -Rows $rows
        }
    }
    catch { return $null }
    # No parent relation at all means the table was not really read (wrong ps syntax, one column):
    # say "cannot confirm" rather than return a root-only tree that would miss every descendant.
    if ($null -eq $parentOf -or $parentOf.Count -eq 0) { return $null }

    $ids = [System.Collections.Generic.List[int]]::new()
    $ids.Add($RootId)
    $frontier = @($RootId)
    while ($frontier.Count -gt 0) {
        $next = @()
        foreach ($childId in @($parentOf.Keys)) {
            if ($frontier -contains $parentOf[$childId] -and -not $ids.Contains($childId) -and $childId -ne 0) {
                $ids.Add($childId)
                $next += $childId
            }
        }
        $frontier = $next
    }
    $snapshot = @()
    foreach ($id in $ids) {
        $start = $null
        try { $start = (Get-Process -Id $id -ErrorAction Stop).StartTime.Ticks } catch { }
        $snapshot += @{ id = $id; start = $start }
    }
    return , $snapshot
}

# Is the process recorded in the snapshot entry still the same live process? A PID that now belongs
# to a process with a different start time was reused, and the original is gone.
function Test-SnapshotProcessAlive {
    param([Parameter(Mandatory)][hashtable]$Entry)
    try { $p = Get-Process -Id $Entry.id -ErrorAction Stop } catch { return $false }
    if ($p.HasExited) { return $false }
    if ($null -ne $Entry.start) {
        try { if ($p.StartTime.Ticks -ne $Entry.start) { return $false } } catch { }
    }
    return $true
}

# Kill the tree and confirm. Returns @{ confirmed; survivors } — confirmed is $false when the tree
# could not be listed or a member outlived $GraceMs.
function Stop-ConductorProcessTree {
    param(
        [Parameter(Mandatory)][System.Diagnostics.Process]$Process,
        [int]$GraceMs = $ConductorKillGraceMs
    )
    $snapshot = Get-ProcessTreeSnapshot -RootId $Process.Id
    try { $Process.Kill($true) } catch { }
    $deadline = [DateTime]::UtcNow.AddMilliseconds($GraceMs)
    $survivors = @()
    do {
        $rootGone = $Process.HasExited
        $survivors = @()
        if ($null -ne $snapshot) {
            foreach ($e in $snapshot) { if (Test-SnapshotProcessAlive -Entry $e) { $survivors += $e.id } }
        }
        if ($rootGone -and $survivors.Count -eq 0) { break }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    $confirmed = ($null -ne $snapshot) -and $Process.HasExited -and ($survivors.Count -eq 0)
    return @{ confirmed = $confirmed; survivors = $survivors; listed = ($null -ne $snapshot) }
}

# Quote one argument for the Windows command line (Start-Process joins -ArgumentList with spaces
# and does not quote). Arguments without whitespace or quotes pass through unchanged.
function ConvertTo-CommandLineArgument {
    param([AllowEmptyString()][string]$Value)
    if ($Value -ne '' -and $Value -notmatch '[\s"]') { return $Value }
    $escaped = [regex]::Replace($Value, '(\\*)"', { param($m) ($m.Groups[1].Value * 2) + '\"' })
    $escaped = [regex]::Replace($escaped, '(\\+)$', { param($m) $m.Groups[1].Value * 2 })
    return '"' + $escaped + '"'
}

# Run $FilePath $Arguments with the hard budget. Returns @{ code; lines; killed; confirmed; pid }:
#   code      the child's exit code; 6 if killed and confirmed; 7 if the kill is not confirmed
#   lines     the child's stderr lines then stdout lines, plus the run_killed line when killed
function Invoke-ConductorBounded {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory)][double]$HardBudgetSeconds,
        [string]$WorkingDirectory = (Get-Location).Path,
        # No default: the system temp dir (/tmp on Linux) is shared, and these files carry the
        # full run output. The caller names a directory under its data dir; it is made private here.
        [Parameter(Mandatory)][string]$TempDirectory,
        [int]$KillGraceMs = $ConductorKillGraceMs
    )
    Initialize-ConductorPrivateDirectory -Path $TempDirectory
    $stem = Join-Path $TempDirectory ('mindwire-conductor-' + [guid]::NewGuid().ToString('N'))
    $outFile = "$stem.out"
    $errFile = "$stem.err"
    $result = @{ code = $null; lines = @(); killed = $false; confirmed = $true; pid = $null }
    $proc = $null
    try {
        $argLine = (@($Arguments) | ForEach-Object { ConvertTo-CommandLineArgument -Value "$_" }) -join ' '
        $startArgs = @{
            FilePath               = $FilePath
            WorkingDirectory       = $WorkingDirectory
            NoNewWindow            = $true
            PassThru               = $true
            RedirectStandardOutput = $outFile
            RedirectStandardError  = $errFile
        }
        if ($argLine) { $startArgs.ArgumentList = $argLine }
        $proc = Start-Process @startArgs
        # Touch the handle once now: without it, a -PassThru process reports ExitCode = $null.
        $null = $proc.Handle
        $result.pid = $proc.Id

        $budgetMs = [int][Math]::Min([double][int]::MaxValue, $HardBudgetSeconds * 1000)
        if ($proc.WaitForExit($budgetMs)) {
            $proc.WaitForExit()  # files, not pipes: returns at once; settles ExitCode
            $result.code = $proc.ExitCode
        }
        else {
            $result.killed = $true
            $stop = Stop-ConductorProcessTree -Process $proc -GraceMs $KillGraceMs
            $result.confirmed = $stop.confirmed
            $result.code = if ($stop.confirmed) { $ConductorRunKilledExitCode } else { $ConductorKillUnconfirmedExitCode }
        }

        $lines = @()
        foreach ($f in @($errFile, $outFile)) {
            if (Test-Path -LiteralPath $f) {
                try {
                    $lines += @(Get-Content -LiteralPath $f -Encoding utf8 -ErrorAction Stop)
                }
                catch {
                    $lines += "WARNING: could not read conductor output file ${f}: $($_.Exception.Message)"
                }
            }
        }
        if ($result.killed) {
            $lines += "$ConductorRunKilledEventKind budget_s=$HardBudgetSeconds pid=$($result.pid)"
            if (-not $result.confirmed) {
                $lines += ("conductor.run_kill_unconfirmed pid=$($result.pid) survivors=" +
                           ((@($stop.survivors) -join ',')) + " listed=$($stop.listed)")
            }
        }
        $result.lines = $lines
        return $result
    }
    finally {
        if ($proc) { $proc.Dispose() }
        foreach ($f in @($outFile, $errFile)) {
            if (Test-Path -LiteralPath $f) {
                try { Remove-Item -LiteralPath $f -Force -ErrorAction Stop }
                catch { Write-Warning "conductor output file not removed: $f ($($_.Exception.Message))" }
            }
        }
    }
}
