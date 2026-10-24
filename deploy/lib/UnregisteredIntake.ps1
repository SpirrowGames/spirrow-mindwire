# deploy/lib/UnregisteredIntake.ps1 — discovery-by-default intake of live threads sweep.json does not
# list (T-sweep-intake-discovery-by-default, Bohr msg-6776 as amended by msg-6778, cleared by Einstein
# msg-6779; builds on T-sweep-intake-and-quarantine-stalls msg-5889 D-2 / msg-5893 D-2.1-D-2.4).
#
# Why this exists. The sweep used to look only at the threads listed in sweep.json, and a live thread
# nobody listed was never picked up (msg-1181 F-1). #448 added threads on the fly by INFERRING their
# clone from the project's other candidates, which could not work for a project with no candidate, one
# with several repo_dirs, or one named nowhere (msg-6740 穴 1-3). The clone is now DECLARED: sweep.json's
# top-level `clones` maps project -> clone path, and that is the only source a discovered thread's
# repo_dir comes from. The #448 inference (Test-UnregisteredRepoDir) is gone (msg-6770 §2): one
# resolution mechanism, not two.
#
# The rule (D-2.5). A thread scripts/unregistered_threads.py reports joins THIS tick's candidates only
# when ALL of these hold:
#   * intake.mode is 'on' (default 'shadow' — Fermi's concern in msg-6740: a long-forgotten thread must
#     not start moving because discovery was switched on);
#   * its project has a `clones` entry that passes the environment probe (fully qualified, exists, not
#     the daemon's own checkout, not a linked worktree);
#   * its last_activity_at is known and at most intake.max_idle_days (default 14) old;
#   * its key is not in `exclude`.
# Everything else lands in exactly one digest bucket — 登録予定（shadow）, 放置, 除外 (count only),
# 登録不可 (clones 未登録 or a failed probe, or a PR-review thread), 時刻不明 — and in no state file.
#
# Explicit candidates are untouched by all of it (msg-6778): they come from Get-SweepCandidates, the
# enumerator never reports a listed pair, and this function skips any key it already holds before a
# single rule is looked at. mode, exclude, the time rules and max_idle_days apply ONLY to threads
# sweep.json does not list.
#
# What it does, and what it deliberately does not (unchanged from #448):
#   * The added candidates exist only in the tick's in-memory candidate array. sweep.json is never
#     written: the enumerator stays write-zero (ReadOnlyMcp / READ_ONLY_TOOLS), and no machine path
#     edits a config file.
#   * Every reason is derived from scratch every tick and shown in the digest; it is never persisted.
#     A thread that is not added gets no parked state and no quarantine.json entry, and since it never
#     enters the candidate array, no state file the sweep writes (quarantine.json, retry-pending.json,
#     evaluated.json) can ever carry its key (D-2.1).
#   * An ADDED thread is an ordinary candidate from then on (D-2.2): if it fails it is retried once
#     and then quarantined, exactly like a sweep.json candidate. No marker, no bypass.
#   * The append happens right after Get-SweepCandidates, BEFORE the quarantine / retry-pending /
#     head-skip filters run (D-2.3).
#   * A project whose enumeration failed (unregistered_count = null) registers nothing and is shown
#     as "?" — "did not measure" is never rendered as 0 (D-2.4, msg-2531 §2 invariant 2).
#   * No timestamp is parsed here. The enumerator classifies last_activity_at once, at its listing
#     boundary, and hands over idle_days (a number) and time_state; this file only compares (msg-6770 §1).
#
# The field is last_activity_at, magickit chatroom_list_threads' own name, end to end (msg-7441 F-1):
# the enumerator first read `updated_at`, which that listing never carries, and every discovered thread
# sat in 時刻不明 for eight days with nothing saying why (msg-7381 §4). The enumerator now also counts,
# per project and before the registered filter, the listed items with no last_activity_at key at all
# (field_absent_count of listed_count, msg-7443 F-3c as narrowed by msg-7445). Whenever that is >= 1 the
# tick log gets a WARN and the digest's 時刻不明 header the same N/M — stated as a count, never as a claim
# about the API (Einstein msg-7444).
#
# Pure: no I/O except the injectable $PathExists / $IsLinkedWorktree checks, so
# tests/Test-UnregisteredIntake.ps1 can dot-source this file without running the sweep.

# The PR-review thread prefix. Those threads dispatch to the paid Tier B PR-gate and must never be
# swept on a timer (Get-SweepCandidates' header). The enumerator already excludes them
# (spirrow_mindwire.unregistered_threads.PR_REVIEW_PREFIX); this is the second, local check, so a
# regression there is refused loudly here instead of spending money. The two values are pinned equal by
# tests/test_unregistered_threads.py::test_powershell_pr_review_prefix_matches_python, so this copy
# cannot drift from the Python one without a red gate.
$script:UnregisteredPrReviewPrefix = 'T-pr-review-'

# Defaults when sweep.json has no `intake` key (msg-6776).
$script:IntakeDefaultMode = 'shadow'
$script:IntakeDefaultMaxIdleDays = 14

# Parses the discovery keys of an already-ConvertFrom-Json'd sweep.json root. Returns
#   @{ clones = [ordered]@{ project = path }; mode = 'shadow'|'on'; max_idle_days = <double>;
#      exclude = @{ '<project>/<thread_id>' = $true } }
# THROWS on a shape error — like owner_map, a malformed config stops the tick (msg-6768 A): `clones`
# not an object or with a blank key/value; `intake` not an object, `mode` not shadow/on,
# `max_idle_days` not a positive number; `exclude` not an array of '<project>/<thread_id>' strings.
# Per-project ENVIRONMENT problems (path missing, relative, ...) are NOT errors here: they are judged
# per tick by Test-UnregisteredClone and only make that project 登録不可.
function ConvertFrom-SweepIntakeConfig {
    param($Raw, [string]$Source = 'sweep.json')
    $cfg = @{ clones = [ordered]@{}; mode = $script:IntakeDefaultMode
        max_idle_days = [double]$script:IntakeDefaultMaxIdleDays; exclude = @{} }
    if ($Raw -isnot [System.Management.Automation.PSCustomObject]) { return $cfg }
    $names = @($Raw.PSObject.Properties.Name)

    if ($names -contains 'clones') {
        $c = $Raw.clones
        if ($c -isnot [System.Management.Automation.PSCustomObject]) {
            $kind = if ($null -eq $c) { 'null' } else { $c.GetType().Name }
            throw "$Source 'clones' must be a JSON object (project -> absolute clone path); found $kind."
        }
        foreach ($p in $c.PSObject.Properties) {
            $key = [string]$p.Name
            if ([string]::IsNullOrWhiteSpace($key) -or $p.Value -isnot [string] -or [string]::IsNullOrWhiteSpace($p.Value)) {
                throw "$Source 'clones' has a blank key or a blank / non-string value: '$key' => $($p.Value | ConvertTo-Json -Compress -Depth 5)"
            }
            $cfg.clones[$key.Trim()] = ([string]$p.Value).Trim()
        }
    }

    if ($names -contains 'intake') {
        $in = $Raw.intake
        if ($in -isnot [System.Management.Automation.PSCustomObject]) {
            throw "$Source 'intake' must be a JSON object { mode, max_idle_days }."
        }
        $inNames = @($in.PSObject.Properties.Name)
        if ($inNames -contains 'mode') {
            if ($in.mode -isnot [string] -or $in.mode -cnotin @('shadow', 'on')) {
                throw "$Source 'intake.mode' must be ""shadow"" or ""on""; found $($in.mode | ConvertTo-Json -Compress)."
            }
            $cfg.mode = [string]$in.mode
        }
        if ($inNames -contains 'max_idle_days') {
            $v = $in.max_idle_days
            $isNum = ($v -is [int] -or $v -is [long] -or $v -is [double] -or $v -is [decimal])
            if (-not $isNum -or [double]$v -le 0) {
                throw "$Source 'intake.max_idle_days' must be a positive number; found $($v | ConvertTo-Json -Compress)."
            }
            $cfg.max_idle_days = [double]$v
        }
    }

    if ($names -contains 'exclude') {
        $ex = $Raw.exclude
        if ($ex -isnot [System.Array]) {
            throw "$Source 'exclude' must be an array of ""<project>/<thread_id>"" strings."
        }
        foreach ($e in $ex) {
            if ($e -isnot [string] -or $e.Trim() -notmatch '^[^/\s][^/]*/\S.*$') {
                throw "$Source 'exclude' has an entry that is not ""<project>/<thread_id>"": $($e | ConvertTo-Json -Compress -Depth 5)"
            }
            $cfg.exclude[$e.Trim()] = $true
        }
    }
    return $cfg
}

# Environment probe for one project's declared clone. Returns @{ ok; repo_dir; reason }. Never throws:
# a clone that is momentarily unusable (a drive not mounted, a path being re-cloned) makes only its own
# project 登録不可 for this tick, it does not stop the sweep (msg-6768 A).
function Test-UnregisteredClone {
    param(
        $Clones,
        [string]$Project,
        [string]$DaemonRoot = '',
        [scriptblock]$PathExists = { param($p) Test-Path -LiteralPath $p -PathType Container },
        # A linked worktree has a `.git` FILE (gitdir: pointer) instead of a directory.
        [scriptblock]$IsLinkedWorktree = { param($p) Test-Path -LiteralPath (Join-Path $p '.git') -PathType Leaf }
    )
    if ($null -eq $Clones -or -not $Clones.Contains($Project)) {
        return @{ ok = $false; repo_dir = $null; reason = 'clones 未登録' }
    }
    $dir = [string]$Clones[$Project]
    # Checked before existence: Test-Path on a relative path would resolve it against this process's
    # working directory, which is not the project's clone.
    if (-not [System.IO.Path]::IsPathFullyQualified($dir)) {
        return @{ ok = $false; repo_dir = $null; reason = "clone not absolute: $dir" }
    }
    if (-not (& $PathExists $dir)) {
        return @{ ok = $false; repo_dir = $null; reason = "clone missing: $dir" }
    }
    if (-not [string]::IsNullOrWhiteSpace($DaemonRoot)) {
        $sep = [char[]]@('\', '/')
        $a = [System.IO.Path]::GetFullPath($dir).TrimEnd($sep)
        $b = [System.IO.Path]::GetFullPath($DaemonRoot).TrimEnd($sep)
        $cmp = if ($IsWindows) { [System.StringComparison]::OrdinalIgnoreCase } else { [System.StringComparison]::Ordinal }
        if ([string]::Equals($a, $b, $cmp) -or $a.StartsWith($b + [System.IO.Path]::DirectorySeparatorChar, $cmp) -or
            $a.StartsWith($b + '/', $cmp)) {
            # The implementer would be editing the daemon's own running code.
            return @{ ok = $false; repo_dir = $null; reason = "clone is the daemon's own checkout: $dir" }
        }
    }
    if (& $IsLinkedWorktree $dir) {
        return @{ ok = $false; repo_dir = $null; reason = "clone is a linked worktree: $dir" }
    }
    return @{ ok = $true; repo_dir = $dir; reason = $null }
}

# WARN lines for explicit candidates whose repo_dir differs from their project's declared clone. The
# candidate's own repo_dir is used regardless (msg-6768 A): it only ever applies to that one thread.
# Emits the lines one by one (wrap the call in @() to collect them).
function Get-CloneCandidateMismatches {
    param([array]$Candidates, $Clones)
    $out = @()
    if ($null -eq $Clones) { return , $out }
    $sep = [char[]]@('\', '/')
    foreach ($c in @($Candidates)) {
        if ($null -eq $c -or -not $Clones.Contains([string]$c.project)) { continue }
        $declared = [string]$Clones[[string]$c.project]
        if (-not [string]::Equals(([string]$c.repo_dir).TrimEnd($sep), $declared.TrimEnd($sep), [System.StringComparison]::OrdinalIgnoreCase)) {
            $out += "WARN sweep.json candidate $($c.key) repo_dir '$($c.repo_dir)' differs from clones['$($c.project)'] '$declared' — the candidate's repo_dir is used for that thread"
        }
    }
    return $out
}

# Turns one unregistered_threads.py report into this tick's intake. Returns
#   @{ added = [candidate]; shadow = [row]; idle = [row]; time_unknown = [row]; excluded_count = <int>;
#      refused = [{ project; thread_id; key; reason }]; unmeasured = [{ project; reason }];
#      field_absent = [{ project; absent; listed }];
#      probe_error = <string or $null>; mode; max_idle_days }   (the last two echo the config in force)
# field_absent has one row per MEASURED project whose listing had >= 1 item with no last_activity_at
# key (msg-7445), whether or not that project has any unregistered thread.
# shadow / idle rows are { project; thread_id; key; idle_days }, time_unknown rows { ...; reason }.
# `added` rows have exactly the shape Get-SweepCandidates returns (project, thread_id, repo_dir, key)
# and carry no marker: nothing downstream may treat them differently (D-2.2). They are ordered by the
# `clones` declaration order of their project, then oldest last_activity_at first (msg-6776 並び順).
# $ProbeError non-empty = the enumerator itself did not produce a report: nothing is added, and the
# digest shows "?" with that reason. $IntakeConfig $null = the defaults (no clones, shadow, 14 days).
function Resolve-UnregisteredIntake {
    param(
        [array]$Candidates,
        $Report,
        [string]$ProbeError = $null,
        $IntakeConfig = $null,
        [string]$DaemonRoot = '',
        [scriptblock]$PathExists = { param($p) Test-Path -LiteralPath $p -PathType Container },
        [scriptblock]$IsLinkedWorktree = { param($p) Test-Path -LiteralPath (Join-Path $p '.git') -PathType Leaf }
    )
    $cfg = if ($null -ne $IntakeConfig) { $IntakeConfig } else { ConvertFrom-SweepIntakeConfig -Raw $null }
    $out = @{ added = @(); shadow = @(); idle = @(); time_unknown = @(); excluded_count = 0
        refused = @(); unmeasured = @(); field_absent = @(); probe_error = $null; mode = $cfg.mode; max_idle_days = $cfg.max_idle_days }
    if (-not [string]::IsNullOrWhiteSpace($ProbeError)) {
        $out.probe_error = "unregistered_threads probe failed: $ProbeError"
        return $out
    }
    if ($null -eq $Report) {
        $out.probe_error = 'unregistered_threads probe failed: no report'
        return $out
    }
    $known = @{}
    foreach ($c in @($Candidates)) { $known[[string]$c.key] = $true }
    $cloneOrder = @{}
    $n = 0
    foreach ($k in @($cfg.clones.Keys)) { $cloneOrder[[string]$k] = $n; $n++ }
    $added = @()

    foreach ($p in @($Report.projects)) {
        if ($null -eq $p) { continue }
        $proj = [string]$p.project
        if ($null -eq $p.unregistered_count) {
            $why = if ($p.PSObject.Properties.Name -contains 'error' -and $p.error) { [string]$p.error } else { 'not measured' }
            $out.unmeasured += [pscustomobject]@{ project = $proj; reason = $why }
            continue
        }
        # Schema-drift counter (msg-7445). Read before the "no unregistered thread" early exit below: a
        # project whose live threads are all in sweep.json is still checked. A report without the key (an
        # older enumerator) reads as 0 — it can only fail to warn, never warn falsely.
        $pNames = @($p.PSObject.Properties.Name)
        $absent = 0
        if ($pNames -contains 'field_absent_count' -and $null -ne $p.field_absent_count) { $absent = [int]$p.field_absent_count }
        if ($absent -ge 1) {
            $listed = if ($pNames -contains 'listed_count' -and $null -ne $p.listed_count) { [string][int]$p.listed_count } else { '?' }
            $out.field_absent += [pscustomobject]@{ project = $proj; absent = $absent; listed = $listed }
        }
        # Per-thread detail from the enumerator. A row it did not describe (an id in `unregistered` with
        # no `threads` entry) is time-unknown: fail closed, never assume fresh.
        $detail = @{}
        if ($p.PSObject.Properties.Name -contains 'threads') {
            foreach ($t in @($p.threads)) { if ($null -ne $t) { $detail[[string]$t.thread_id] = $t } }
        }
        $ids = @(@($p.unregistered) | ForEach-Object { [string]$_ } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($ids.Count -eq 0) { continue }
        $clone = Test-UnregisteredClone -Clones $cfg.clones -Project $proj -DaemonRoot $DaemonRoot `
            -PathExists $PathExists -IsLinkedWorktree $IsLinkedWorktree
        foreach ($tid in $ids) {
            $key = "$proj/$tid"
            # An explicit candidate is never classified at all (msg-6778).
            if ($known.ContainsKey($key)) { continue }
            $known[$key] = $true
            if ($tid.StartsWith($script:UnregisteredPrReviewPrefix)) {
                $out.refused += [pscustomobject]@{ project = $proj; thread_id = $tid; key = $key
                    reason = 'pr-review thread: never swept (paid PR-gate)' }
                continue
            }
            if ($cfg.exclude.ContainsKey($key)) { $out.excluded_count++; continue }
            if (-not $clone.ok) {
                $out.refused += [pscustomobject]@{ project = $proj; thread_id = $tid; key = $key; reason = $clone.reason }
                continue
            }
            $d = $detail[$tid]
            $state = if ($null -ne $d) { [string]$d.time_state } else { 'missing' }
            $idleDays = $null
            if ($state -eq 'ok' -and $null -ne $d.idle_days) {
                try { $idleDays = [double]$d.idle_days } catch { $idleDays = $null }
            }
            if ($null -eq $idleDays) {
                $why = if ($state -eq 'ok') { 'idle_days absent' } else { "last_activity_at $state" }
                $out.time_unknown += [pscustomobject]@{ project = $proj; thread_id = $tid; key = $key; reason = $why }
                continue
            }
            $row = [pscustomobject]@{ project = $proj; thread_id = $tid; key = $key; idle_days = $idleDays }
            if ($idleDays -gt [double]$cfg.max_idle_days) { $out.idle += $row; continue }
            if ($cfg.mode -ne 'on') { $out.shadow += $row; continue }
            $added += [pscustomobject]@{
                order     = $cloneOrder[$proj]
                idle_days = $idleDays
                candidate = [pscustomobject]@{
                    project   = $proj
                    thread_id = $tid
                    repo_dir  = $clone.repo_dir
                    key       = $key
                }
            }
        }
    }
    $out.added = @($added | Sort-Object -Stable -Property @{ Expression = 'order' }, @{ Expression = 'idle_days'; Descending = $true } |
        ForEach-Object { $_.candidate })
    return $out
}

# The candidate array for the tick: sweep.json's candidates followed by the auto-registered ones, in
# that order (sweep.json order is the tie-break everywhere else, so listed threads keep precedence).
function Join-UnregisteredCandidates {
    param([array]$Candidates, $Intake)
    if ($null -eq $Intake) { return , @($Candidates) }
    return , (@($Candidates) + @($Intake.added))
}

function Format-IntakeIdleDays { param($Days) return [string]::Format([System.Globalization.CultureInfo]::InvariantCulture, '{0:0.#}', [double]$Days) }

# One bucket of an intake result, with an absent key read as empty (`@($null)` would count 1).
function Get-IntakeBucket {
    param($Intake, [string]$Name)
    if ($null -eq $Intake) { return , @() }
    return , @($Intake[$Name] | Where-Object { $null -ne $_ })
}

# Digest rows for New-DailyDigest's 未登録 section, "?" rows first. Each row is @{ Line; AgeSeconds }
# like every other section's rows, so the budget ladder treats it the same. Excluded threads are a
# count in the header only (msg-6768 C).
function Get-UnregisteredIntakeDigestRows {
    param($Intake)
    $rows = @()
    if ($null -eq $Intake) { return , $rows }
    if ($Intake.probe_error) {
        $rows += [pscustomobject]@{ Line = "  (全 project) — ? $($Intake.probe_error)"; AgeSeconds = 0 }
    }
    foreach ($u in (Get-IntakeBucket $Intake 'unmeasured')) {
        $rows += [pscustomobject]@{ Line = "  $($u.project) — ? 測れなかった: $($u.reason)"; AgeSeconds = 0 }
    }
    foreach ($t in (Get-IntakeBucket $Intake 'time_unknown')) {
        $rows += [pscustomobject]@{ Line = "  $($t.key) — ? 時刻不明（登録しない）: $($t.reason)"; AgeSeconds = 0 }
    }
    foreach ($r in (Get-IntakeBucket $Intake 'refused')) {
        $rows += [pscustomobject]@{ Line = "  $($r.key) — $($r.reason)"; AgeSeconds = 0 }
    }
    foreach ($s in (Get-IntakeBucket $Intake 'shadow')) {
        $rows += [pscustomobject]@{ Line = "  $($s.key) — 登録予定（shadow）: 最終更新から $(Format-IntakeIdleDays $s.idle_days) 日"; AgeSeconds = 0 }
    }
    $limit = if ($null -ne $Intake.max_idle_days) { " > 上限 $(Format-IntakeIdleDays $Intake.max_idle_days) 日" } else { '' }
    foreach ($s in (Get-IntakeBucket $Intake 'idle')) {
        $rows += [pscustomobject]@{ Line = "  $($s.key) — 放置（登録しない）: 最終更新から $(Format-IntakeIdleDays $s.idle_days) 日$limit"; AgeSeconds = 0 }
    }
    return , $rows
}

# Tick-log lines for the schema-drift counter, one per project with >= 1 listed item lacking the key
# (msg-7445). The wording is a count, not a diagnosis. Emits the lines (wrap the call in @()).
function Get-UnregisteredIntakeSchemaWarnings {
    param($Intake)
    $out = @()
    foreach ($f in (Get-IntakeBucket $Intake 'field_absent')) {
        $out += "WARN listing schema: $($f.project) $($f.absent)/$($f.listed) items lack last_activity_at"
    }
    return $out
}

# The section header. "?" is appended whenever some part of the population was not measured, so a
# day that could not look is never shown as a clean 0. Every bucket is counted, so a truncated row
# list never hides how many threads are waiting in shadow. The 時刻不明 count carries the schema-drift
# fact beside it — "<project>：一覧の N/M 件に last_activity_at が無い" — whenever some project's listing
# lacked the key (msg-7445): in the header, so the row budget can never truncate it away.
function Get-UnregisteredIntakeHeader {
    param($Intake)
    $refused = (Get-IntakeBucket $Intake 'refused').Count
    $added = (Get-IntakeBucket $Intake 'added').Count
    $shadow = (Get-IntakeBucket $Intake 'shadow').Count
    $idle = (Get-IntakeBucket $Intake 'idle').Count
    $timeUnknown = (Get-IntakeBucket $Intake 'time_unknown').Count
    $excluded = [int]$Intake['excluded_count']
    $unknown = if ($Intake['probe_error'] -or (Get-IntakeBucket $Intake 'unmeasured').Count -gt 0) { ' + ?' } else { '' }
    $drift = @(foreach ($f in (Get-IntakeBucket $Intake 'field_absent')) { "$($f.project)：一覧の $($f.absent)/$($f.listed) 件に last_activity_at が無い" })
    $driftNote = if ($drift.Count -gt 0) { "［$($drift -join ' / ')］" } else { '' }
    return ("未登録（登録不可）: $refused 件$unknown（この tick の自動登録 $added 件 / 登録予定（shadow） $shadow 件 / " +
            "放置 $idle 件 / 時刻不明 $timeUnknown 件$driftNote / 除外 $excluded 件）")
}
