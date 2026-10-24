# deploy/lib/Summon.ps1 — the sweep's half of Summon (T-human-call-priority-interrupt, PR-2).
#
# Design: Bohr msg-6743 (v1) revised through msg-6755 (v1.6, the implementation summary), msg-6800
# (v1.7: the active_summon contract) and msg-6802 (v1.8: scope); Einstein approved in msg-6756 /
# msg-6803. Takahito's requirement (msg-6741 / msg-6742): a person's thread is handled first, and
# it stops the other work of the same project — (b).
#
# What lives here (all PURE: no I/O, no clock, no Write-Log — the wrapper passes the facts in):
#   * ConvertFrom-ActiveSummonJson — the v1.7 contract. Absent / null = no Summon; a well-formed
#     {thread_id, since, by} = a Summon; anything else = malformed, which the sweep treats as HOLD.
#     Read with System.Text.Json, NOT ConvertFrom-Json: ConvertFrom-Json turns any ISO-looking
#     string into a [datetime] and drops its offset, so `since` could no longer be checked. The
#     probe (scripts/loop_control.py) hands the value over as a JSON string for the same reason.
#   * Get-SummonYieldDecision — the (since, thread_id) total order (v1.1 指摘 2). The Python twin
#     is spirrow_mindwire.conductor.summon.summon_yield_decision; tests/data/summon/ holds the
#     shared tables both are run against (tests/Test-SweepFairness.ps1, tests/test_conductor_summon.py).
#   * Get-SummonDisposition — what one candidate is this tick: the Summon itself (lane 0), held by
#     a Summon, held because a Summon read was malformed, or unaffected.
#   * Update-SummonYield — the ONE function that changes an evaluated.json `summon_yield` record
#     (I-SY1..I-SY5, v1.6 signature). Both writers call it: (a) after a conductor run, with the
#     stop line converted to -Delivered / -YieldTo, and (b) when a LAUNCH candidate is held by a
#     Summon, with -Delivered $false.
#   * Get-ResumeNoticeArgs — the conductor arguments for a thread that has a record (v1.4: always,
#     even when main did not move; the conductor then sends its "main did not change" wording).
#   * Get-SummonDigestLines — the lines at the very top of the daily digest (PR-3): each active
#     Summon with the number of threads it holds and its age, and each malformed read, including a
#     project held only because its OWNER project's read is malformed.
#
# The record (I-SY1..I-SY4, msg-6747): evaluated.json[<key>].summon_yield =
#   { main_sha_at_yield = <sha | 'unknown'>; yielded_at = <ISO>; summon_threads = @(<thread_id>...) }
# I-SY1  main_sha_at_yield / yielded_at are written only when there is no record; never overwritten.
# I-SY2  a later yield to another Summon only appends to summon_threads (no duplicates).
# I-SY3  the record is removed only when the conductor's stop line says resume_notice=delivered.
# I-SY4  the SHA is the candidate repo_dir's origin/main, freshly fetched by the sweep ('unknown'
#        when it cannot be read — a range with an unknown end is never "empty", so the notice shows).
# I-SY5  after a run: first remove (delivered), then create / append (SUMMON_YIELD).
#
# NO TOP-LEVEL SIDE EFFECTS beyond the constants below: this file is dot-sourced by the wrapper and
# by the tests (same rule as StopReason.ps1).

$script:SummonSincePattern = '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$'
$script:SummonUnknownSha = 'unknown'

# `since` as a UTC [datetime], or $null when it is not in the contract's format. The pattern is the
# Python half's (_SINCE_RE in conductor/summon.py): restricting the syntax to what both parsers read
# identically keeps a value from being valid on one side and malformed on the other.
function ConvertFrom-SummonSince {
    param([AllowNull()][string]$Value)
    if ([string]::IsNullOrEmpty($Value) -or $Value -notmatch $script:SummonSincePattern) { return $null }
    try {
        $dto = [DateTimeOffset]::Parse($Value, [Globalization.CultureInfo]::InvariantCulture)
    } catch { return $null }
    return $dto.UtcDateTime
}

# Classify the JSON TEXT of an active_summon value. $null / '' = the key was absent.
# Returns @{ kind = 'none' | 'valid' | 'malformed'; thread_id; since (UTC [datetime]); by }.
function ConvertFrom-ActiveSummonJson {
    param([AllowNull()][string]$Json)
    $none = @{ kind = 'none'; thread_id = $null; since = $null; by = $null }
    $bad = @{ kind = 'malformed'; thread_id = $null; since = $null; by = $null }
    if ([string]::IsNullOrWhiteSpace($Json)) { return $none }
    try { $doc = [System.Text.Json.JsonDocument]::Parse($Json) } catch { return $bad }
    try {
        $root = $doc.RootElement
        $kind = "$($root.ValueKind)"
        if ($kind -eq 'Null') { return $none }
        if ($kind -ne 'Object') { return $bad }
        # Last occurrence wins on a duplicated key, as in Python's json. Keys are matched ORDINALLY
        # (case-sensitive), as Python's dict does: a PowerShell @{} is case-insensitive, which made
        # {"THREAD_ID": ...} valid here while Python reads it as missing thread_id (malformed), and
        # let a case-variant duplicate overwrite the exact key (PR #475 review).
        $props = [System.Collections.Generic.Dictionary[string, object]]::new([System.StringComparer]::Ordinal)
        foreach ($p in $root.EnumerateObject()) { $props[$p.Name] = $p.Value }
        $fields = @{}
        foreach ($name in 'thread_id', 'since', 'by') {
            if (-not $props.ContainsKey($name)) { return $bad }
            $el = $props[$name]
            if ("$($el.ValueKind)" -ne 'String') { return $bad }
            $fields[$name] = $el.GetString()
        }
        if ([string]::IsNullOrWhiteSpace($fields.thread_id) -or [string]::IsNullOrWhiteSpace($fields.by)) { return $bad }
        $since = ConvertFrom-SummonSince -Value $fields.since
        if ($null -eq $since) { return $bad }
        return @{ kind = 'valid'; thread_id = $fields.thread_id; since = $since; by = $fields.by }
    }
    finally { $doc.Dispose() }
}

# The active_summon of one project as the control probe reported it, classified. A project whose
# probe failed ($null) or that has no row is 'none': the sweep is the optimisation layer and fails
# open; the conductor's own read decides (it fails safe).
function Get-ProjectSummon {
    param($ControlByProject, [string]$Project)
    if ($null -eq $ControlByProject -or [string]::IsNullOrWhiteSpace($Project)) { return (ConvertFrom-ActiveSummonJson -Json $null) }
    $c = $ControlByProject[$Project]
    if ($null -eq $c) { return (ConvertFrom-ActiveSummonJson -Json $null) }
    $json = if ($c.PSObject.Properties.Name -contains 'active_summon_json') { $c.active_summon_json }
            elseif ($c -is [hashtable] -and $c.ContainsKey('active_summon_json')) { $c['active_summon_json'] }
            else { $null }
    return (ConvertFrom-ActiveSummonJson -Json $json)
}

# -1 / 0 / 1 for the (since, thread_id, project) order. project is only the last tie-break (two
# Summons in different projects with the same since AND the same thread id); ordinal comparison,
# as Python compares str.
function Compare-SummonOrder {
    param($A, $B)
    $c = $A.since.Ticks.CompareTo($B.since.Ticks)
    if ($c -ne 0) { return [math]::Sign($c) }
    $c = [string]::CompareOrdinal($A.thread_id, $B.thread_id)
    if ($c -ne 0) { return [math]::Sign($c) }
    return [math]::Sign([string]::CompareOrdinal($A.project, $B.project))
}

# The thread id of the Summon this thread must yield to, or $null. $OwnSummon / $OwnerSummon are
# ConvertFrom-ActiveSummonJson results (only kind 'valid' counts). The owner's Summon is in scope
# only when $OwnerProject differs from $Project. A Summon never yields to itself; a summoned thread
# yields only to an in-scope Summon earlier in the order; when several apply the first is named.
function Get-SummonYieldDecision {
    param([string]$Project, [string]$ThreadId, $OwnSummon, [AllowNull()][string]$OwnerProject, $OwnerSummon)
    $inScope = @()
    if ($null -ne $OwnSummon -and $OwnSummon.kind -eq 'valid') {
        $inScope += @{ project = $Project; thread_id = $OwnSummon.thread_id; since = $OwnSummon.since }
    }
    if ($null -ne $OwnerSummon -and $OwnerSummon.kind -eq 'valid' -and
        -not [string]::IsNullOrWhiteSpace($OwnerProject) -and $OwnerProject -ne $Project) {
        $inScope += @{ project = $OwnerProject; thread_id = $OwnerSummon.thread_id; since = $OwnerSummon.since }
    }
    $me = $null
    foreach ($s in $inScope) { if ($s.project -eq $Project -and $s.thread_id -eq $ThreadId) { $me = $s } }
    $blockers = @($inScope | Where-Object { -not [object]::ReferenceEquals($_, $me) })
    if ($null -ne $me) { $blockers = @($blockers | Where-Object { (Compare-SummonOrder -A $_ -B $me) -lt 0 }) }
    if ($blockers.Count -eq 0) { return $null }
    $first = $blockers[0]
    foreach ($b in $blockers) { if ((Compare-SummonOrder -A $b -B $first) -lt 0) { $first = $b } }
    return $first.thread_id
}

# What a candidate is this tick, from the control probe answers:
#   kind 'summoned'  — the active Summon of its own project, and it yields to nobody (lane 0)
#   kind 'yield'     — held-by-summon:<yield_to>
#   kind 'malformed' — its project's (or its owner project's) active_summon is malformed: HOLD
#   kind 'none'      — unaffected
# owner_project is the project owning the repository the candidate writes to (owner_map over the
# predicted resource), $null when unresolved or equal to the candidate's own project; the wrapper
# hands it to the conductor as --summon-owner-project. Resolution fails open exactly as
# Test-HoldForCandidate does.
function Get-SummonDisposition {
    param($Candidate, $ControlByProject, $PredictedResourceByRepoDir, $OwnerMap)
    $owner = $null
    if ($null -ne $PredictedResourceByRepoDir -and $null -ne $OwnerMap) {
        $entry = $PredictedResourceByRepoDir[$Candidate.repo_dir]
        if ($null -ne $entry -and -not [string]::IsNullOrWhiteSpace([string]$entry.predicted_resource)) {
            $o = [string]$OwnerMap[[string]$entry.predicted_resource]
            if (-not [string]::IsNullOrWhiteSpace($o) -and $o -ne $Candidate.project) { $owner = $o }
        }
    }
    $own = Get-ProjectSummon -ControlByProject $ControlByProject -Project $Candidate.project
    $ownerSummon = if ($owner) { Get-ProjectSummon -ControlByProject $ControlByProject -Project $owner }
                   else { ConvertFrom-ActiveSummonJson -Json $null }
    $result = @{ kind = 'none'; yield_to = $null; owner_project = $owner; since = $null; malformed_project = $null }
    if ($own.kind -eq 'malformed') { $result.kind = 'malformed'; $result.malformed_project = $Candidate.project; return $result }
    if ($ownerSummon.kind -eq 'malformed') { $result.kind = 'malformed'; $result.malformed_project = $owner; return $result }
    $yieldTo = Get-SummonYieldDecision -Project $Candidate.project -ThreadId $Candidate.thread_id `
        -OwnSummon $own -OwnerProject $owner -OwnerSummon $ownerSummon
    if ($yieldTo) { $result.kind = 'yield'; $result.yield_to = $yieldTo; return $result }
    if ($own.kind -eq 'valid' -and $own.thread_id -eq $Candidate.thread_id) {
        $result.kind = 'summoned'; $result.since = $own.since
    }
    return $result
}

# --- the summon_yield record ---------------------------------------------------------------------

# Normalise a stored record (hashtable written this tick, or PSCustomObject back from JSON) to a
# fresh hashtable, or $null. yielded_at may come back from ConvertFrom-Json as a [datetime].
function ConvertTo-SummonYieldEntry {
    param($Entry)
    if ($null -eq $Entry) { return $null }
    $get = { param($n) if ($Entry -is [hashtable]) { $Entry[$n] } elseif ($Entry.PSObject.Properties.Name -contains $n) { $Entry.$n } else { $null } }
    $sha = & $get 'main_sha_at_yield'
    $at = & $get 'yielded_at'
    if ($at -is [datetime]) { $at = $at.ToUniversalTime().ToString('o') }
    $threads = @(& $get 'summon_threads' | Where-Object { -not [string]::IsNullOrWhiteSpace("$_") } | ForEach-Object { "$_" })
    return @{
        main_sha_at_yield = if ([string]::IsNullOrWhiteSpace("$sha")) { $script:SummonUnknownSha } else { "$sha" }
        yielded_at = "$at"
        summon_threads = $threads
    }
}

# I-SY5 with the v1.6 signature. Pure: returns the new record (a fresh hashtable) or $null.
#   1. -Delivered  -> the record is removed first.
#   2. -YieldTo S  -> on no record: create {MainSha, Now, [S]} (I-SY1); on a record: keep its SHA and
#                     time and append S without duplicates (I-SY2).
# -MainSha is read only when a record is created; $null / '' stores 'unknown'.
function Update-SummonYield {
    param($Entry, [bool]$Delivered, [AllowNull()][string]$YieldTo, [AllowNull()][string]$MainSha, [datetime]$Now = [datetime]::MinValue)
    $e = ConvertTo-SummonYieldEntry -Entry $Entry
    if ($Delivered) { $e = $null }
    if ([string]::IsNullOrWhiteSpace($YieldTo)) { return $e }
    if ($null -eq $e) {
        return @{
            main_sha_at_yield = if ([string]::IsNullOrWhiteSpace($MainSha)) { $script:SummonUnknownSha } else { $MainSha }
            yielded_at = $Now.ToUniversalTime().ToString('o')
            summon_threads = @($YieldTo)
        }
    }
    if ($e.summon_threads -notcontains $YieldTo) { $e.summon_threads = @($e.summon_threads) + @($YieldTo) }
    return $e
}

# Whether Update-SummonYield will read -MainSha for these inputs (only when it creates a record), so
# the wrapper fetches origin/main only when the answer is used.
function Test-SummonYieldNeedsSha {
    param($Entry, [bool]$Delivered, [AllowNull()][string]$YieldTo)
    if ([string]::IsNullOrWhiteSpace($YieldTo)) { return $false }
    return ($Delivered -or $null -eq (ConvertTo-SummonYieldEntry -Entry $Entry))
}

function Get-SummonYieldEntry {
    param([hashtable]$EvaluatedState, [string]$Key)
    if (-not $EvaluatedState.ContainsKey($Key) -or $null -eq $EvaluatedState[$Key]) { return $null }
    $row = $EvaluatedState[$Key]
    $raw = if ($row -is [hashtable]) { $row['summon_yield'] }
           elseif ($row.PSObject.Properties.Name -contains 'summon_yield') { $row.summon_yield }
           else { $null }
    return (ConvertTo-SummonYieldEntry -Entry $raw)
}

# Store (or, for $null, remove) the record on the row. Every other field of the row is kept.
function Set-SummonYieldEntry {
    param([hashtable]$EvaluatedState, [string]$Key, $Entry)
    $row = ConvertTo-EvaluatedRow ($EvaluatedState[$Key])
    if ($null -eq $Entry) { if ($row.ContainsKey('summon_yield')) { [void]$row.Remove('summon_yield') } }
    else { $row['summon_yield'] = $Entry }
    $EvaluatedState[$Key] = $row
}

# The conductor arguments for a launch (v1.4): always when there is a record, whether or not main
# moved (a == b is the empty range). $MainSha is the launch-time origin/main ('unknown' allowed).
function Get-ResumeNoticeArgs {
    param($Entry, [AllowNull()][string]$MainSha)
    $e = ConvertTo-SummonYieldEntry -Entry $Entry
    if ($null -eq $e) { return @() }
    $b = if ([string]::IsNullOrWhiteSpace($MainSha)) { $script:SummonUnknownSha } else { $MainSha }
    $summons = if ($e.summon_threads.Count -gt 0) { $e.summon_threads -join ',' } else { $script:SummonUnknownSha }
    return @('--resume-notice-summons', $summons, '--resume-notice-main-range', "$($e.main_sha_at_yield)..$b")
}

# The --summon-owner-project argument for a launch (v1.6 §4), or nothing.
function Get-SummonOwnerArgs {
    param($Disposition)
    if ($null -eq $Disposition -or [string]::IsNullOrWhiteSpace($Disposition.owner_project)) { return @() }
    return @('--summon-owner-project', $Disposition.owner_project)
}

# --- the digest's Summon lines (PR-3, Bohr msg-6965 items 1 and 2) ------------------------------

# Most Summon lines the digest shows; the rest become one "+N 件" line. One line per project with a
# Summon or a malformed read, so this is reached only by an unusual day — but the digest has a
# character budget and these lines sit above it, so their number is bounded here.
$script:SummonDigestMaxLines = 5

# "経過 X 時間" for a Summon's since. $null since (Einstein's PR-3 advisory: a valid read never
# carries one, but this renderer must not do arithmetic on it if one ever arrives) and a since in
# the future (clock skew) are said, not computed.
function Format-SummonElapsed {
    param($Since, [datetime]$Now)
    if ($null -eq $Since -or $Since -isnot [datetime]) { return '経過時間不明' }
    $span = $Now.ToUniversalTime() - $Since.ToUniversalTime()
    if ($span.TotalHours -lt 1) { return '経過 1 時間未満' }
    return "経過 $([int][Math]::Floor($span.TotalHours)) 時間"
}

# The lines the daily digest puts at its very top (PR-3). PURE: every fact is passed in.
#   * one line per project whose Summon is active:
#       ヒューマンコール中: <thread_id> (<project>)、巻き添えで N 本停止中、経過 X 時間
#     N counts the OTHER threads this Summon is holding: this tick's held-by-summon:<thread_id>
#     dispositions plus the evaluated.json summon_yield records naming it (a thread stopped at a
#     round boundary, or held on an earlier tick, has a record but may have no LAUNCH this tick),
#     each key once, and only keys in the Summon's scope (its project, or a thread whose owner
#     project is the Summon's — the scope Get-SummonDisposition uses).
#   * one line per project whose active_summon is malformed (its own work is HOLD, fail-safe);
#   * one line per (project, owner) pair where a project is HOLD only because the OWNER project's
#     active_summon is malformed (the #476 advisory, msg-6920): its own control may still say RUN,
#     so the digest names the cause. An owner whose probe row is missing is read as no Summon (the
#     sweep fails open, Get-ProjectSummon), so no line is shown for it.
# Empty when nothing applies, which leaves the digest exactly as it was.
function Get-SummonDigestLines {
    param($ControlByProject, $SummonByKey, $Dispositions, $EvaluatedState, [datetime]$Now)
    if ($null -eq $ControlByProject) { $ControlByProject = @{} }
    if ($null -eq $SummonByKey) { $SummonByKey = @{} }
    if ($null -eq $Dispositions) { $Dispositions = @{} }
    if ($null -eq $EvaluatedState) { $EvaluatedState = @{} }
    $projectOf = { param($k) ("$k" -split '/', 2)[0] }
    $threadOf = { param($k) $parts = "$k" -split '/', 2; if ($parts.Count -gt 1) { $parts[1] } else { '' } }
    $active = @()
    $malformed = @()
    foreach ($proj in @($ControlByProject.Keys | ForEach-Object { "$_" } | Sort-Object)) {
        $ps = Get-ProjectSummon -ControlByProject $ControlByProject -Project $proj
        if ($ps.kind -eq 'valid') { $active += @{ project = $proj; summon = $ps } }
        elseif ($ps.kind -eq 'malformed') { $malformed += $proj }
    }
    $lines = @()
    foreach ($a in $active) {
        $tid = $a.summon.thread_id
        $proj = $a.project
        $held = @{}
        $keys = @(@($Dispositions.Keys) + @($EvaluatedState.Keys) | ForEach-Object { "$_" } | Sort-Object -Unique)
        foreach ($k in $keys) {
            if ((& $projectOf $k) -eq $proj -and (& $threadOf $k) -eq $tid) { continue }  # the Summon itself
            $disp = $SummonByKey[$k]
            $owner = if ($null -ne $disp) { $disp.owner_project } else { $null }
            if ((& $projectOf $k) -ne $proj -and $owner -ne $proj) { continue }
            $isHeld = ("$($Dispositions[$k])" -ceq "held-by-summon:$tid")
            if (-not $isHeld) {
                $rec = Get-SummonYieldEntry -EvaluatedState $EvaluatedState -Key $k
                $isHeld = ($null -ne $rec -and @($rec.summon_threads | Where-Object { "$_" -ceq $tid }).Count -gt 0)
            }
            if ($isHeld) { $held[$k] = $true }
        }
        $lines += "ヒューマンコール中: $tid ($proj)、巻き添えで $($held.Count) 本停止中、$(Format-SummonElapsed -Since $a.summon.since -Now $Now)"
    }
    foreach ($m in $malformed) {
        $lines += "⚠ ヒューマンコール読取不能: $m の active_summon の形が壊れている — $m の作業は HOLD 中 (fail-safe)"
    }
    $pairs = @{}
    foreach ($k in @($SummonByKey.Keys | ForEach-Object { "$_" } | Sort-Object)) {
        $d = $SummonByKey[$k]
        if ($null -eq $d -or $d.kind -ne 'malformed' -or [string]::IsNullOrWhiteSpace($d.malformed_project)) { continue }
        $q = & $projectOf $k
        if ($d.malformed_project -eq $q) { continue }
        $pairs["$q`t$($d.malformed_project)"] = $true
    }
    foreach ($pk in @($pairs.Keys | Sort-Object)) {
        $q, $m = $pk -split "`t", 2
        $lines += "⚠ $q の HOLD の原因: owner プロジェクト $m、active_summon の形が壊れている ($q 自身の設定ではない)"
    }
    if ($lines.Count -gt $script:SummonDigestMaxLines) {
        $rest = $lines.Count - $script:SummonDigestMaxLines
        $lines = @($lines[0..($script:SummonDigestMaxLines - 1)]) + @("  +$rest 件")
    }
    return $lines
}
