# deploy/lib/UnregisteredIntake.ps1 — auto-registration of unregistered live threads into one tick's
# sweep candidates (T-sweep-intake-and-quarantine-stalls, Bohr msg-5889 D-2, amended by msg-5893
# D-2.1-D-2.4, cleared by Einstein msg-5894; Operator Board §F.1 row 3).
#
# Why this exists. The sweep only ever looks at the threads listed in sweep.json. A live thread that
# nobody listed is never picked up, and nothing says so (msg-1181 F-1: five threads stalled that way).
# scripts/unregistered_threads.py already enumerates those threads; this file turns its report into
# extra candidates for the CURRENT tick.
#
# What it does, and what it deliberately does not:
#   * The added candidates exist only in the tick's in-memory candidate array. sweep.json is never
#     written: the enumerator stays write-zero (ReadOnlyMcp / READ_ONLY_TOOLS), and no machine path
#     edits a config file.
#   * A thread is added only when the environment probe passes for its project (CON-P0-ENV, design
#     §17): the project's existing candidates carry exactly one distinct repo_dir, that repo_dir is
#     fully qualified, and it exists. Anything else is REFUSED, with the reason. The reason is
#     derived from scratch every tick and shown in the digest; it is never persisted, and a refused
#     thread gets no parked state and no quarantine.json entry (§F.1, msg-4900 §2). A refused thread
#     never enters the candidate array, so no state file the sweep writes (quarantine.json,
#     retry-pending.json, evaluated.json) can ever carry its key (D-2.1).
#   * An ADDED thread is an ordinary candidate from then on (D-2.2): if it fails it is retried once
#     and then quarantined, exactly like a sweep.json candidate. There is no "ephemeral" bypass —
#     a bypass would let a thread that always fails launch and fail on every tick (msg-5893 §2).
#   * The append happens right after Get-SweepCandidates, BEFORE the quarantine / retry-pending /
#     head-skip filters run (D-2.3). Appending after them would let an auto-registered thread that is
#     already quarantined slip past the quarantine check and launch on every tick.
#   * A project whose enumeration failed (unregistered_count = null) registers nothing and is shown
#     as "?" — "did not measure" is never rendered as 0 (msg-2531 §2 invariant 2).
#
# Pure: no I/O except the injectable $PathExists check, so tests/Test-UnregisteredIntake.ps1 can
# dot-source this file without running the sweep.

# The PR-review thread prefix. Those threads dispatch to the paid Tier B PR-gate and must never be
# swept on a timer (Get-SweepCandidates' header). The enumerator already excludes them
# (spirrow_mindwire.unregistered_threads.PR_REVIEW_PREFIX); this is the second, local check, so a
# regression there is refused loudly here instead of spending money.
$script:UnregisteredPrReviewPrefix = 'T-pr-review-'

# Environment probe for one project. Returns @{ ok; repo_dir; reason }.
function Test-UnregisteredRepoDir {
    param(
        [array]$Candidates,
        [string]$Project,
        [scriptblock]$PathExists = { param($p) Test-Path -LiteralPath $p -PathType Container }
    )
    $dirs = @($Candidates | Where-Object { $_.project -eq $Project } |
        ForEach-Object { [string]$_.repo_dir } | Sort-Object -Unique)
    if ($dirs.Count -ne 1) {
        # More than one value means guessing which clone the thread belongs to; a wrong guess checks
        # out the wrong repository (Einstein msg-5891). Zero cannot occur for a project the
        # enumerator read from sweep.json, but it is refused the same way rather than assumed away.
        return @{ ok = $false; repo_dir = $null; reason = "repo_dir ambiguous: $($dirs.Count) values" }
    }
    $dir = $dirs[0]
    # Checked before existence: Test-Path on a relative path would resolve it against this process's
    # working directory, which is not the project's clone.
    if (-not [System.IO.Path]::IsPathFullyQualified($dir)) {
        return @{ ok = $false; repo_dir = $null; reason = "repo_dir not absolute: $dir" }
    }
    if (-not (& $PathExists $dir)) {
        return @{ ok = $false; repo_dir = $null; reason = "repo_dir missing: $dir" }
    }
    return @{ ok = $true; repo_dir = $dir; reason = $null }
}

# Turns one unregistered_threads.py report into this tick's intake. Returns
#   @{ added = [candidate]; refused = [{ project; thread_id; key; reason }];
#      unmeasured = [{ project; reason }]; probe_error = <string or $null> }
# `added` rows have exactly the shape Get-SweepCandidates returns (project, thread_id, repo_dir, key)
# and carry no marker: nothing downstream may treat them differently (D-2.2).
# $ProbeError non-empty = the enumerator itself did not produce a report: nothing is added, and the
# digest shows "?" with that reason.
function Resolve-UnregisteredIntake {
    param(
        [array]$Candidates,
        $Report,
        [string]$ProbeError = $null,
        [scriptblock]$PathExists = { param($p) Test-Path -LiteralPath $p -PathType Container }
    )
    $out = @{ added = @(); refused = @(); unmeasured = @(); probe_error = $null }
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

    foreach ($p in @($Report.projects)) {
        if ($null -eq $p) { continue }
        $proj = [string]$p.project
        if ($null -eq $p.unregistered_count) {
            $why = if ($p.PSObject.Properties.Name -contains 'error' -and $p.error) { [string]$p.error } else { 'not measured' }
            $out.unmeasured += [pscustomobject]@{ project = $proj; reason = $why }
            continue
        }
        $ids = @(@($p.unregistered) | ForEach-Object { [string]$_ } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($ids.Count -eq 0) { continue }
        $envProbe = Test-UnregisteredRepoDir -Candidates $Candidates -Project $proj -PathExists $PathExists
        foreach ($tid in $ids) {
            $key = "$proj/$tid"
            if ($known.ContainsKey($key)) { continue }
            $known[$key] = $true
            if ($tid.StartsWith($script:UnregisteredPrReviewPrefix)) {
                $out.refused += [pscustomobject]@{ project = $proj; thread_id = $tid; key = $key
                    reason = 'pr-review thread: never swept (paid PR-gate)' }
                continue
            }
            if (-not $envProbe.ok) {
                $out.refused += [pscustomobject]@{ project = $proj; thread_id = $tid; key = $key; reason = $envProbe.reason }
                continue
            }
            $out.added += [pscustomobject]@{
                project   = $proj
                thread_id = $tid
                repo_dir  = $envProbe.repo_dir
                key       = $key
            }
        }
    }
    return $out
}

# The candidate array for the tick: sweep.json's candidates followed by the auto-registered ones, in
# that order (sweep.json order is the tie-break everywhere else, so listed threads keep precedence).
function Join-UnregisteredCandidates {
    param([array]$Candidates, $Intake)
    if ($null -eq $Intake) { return , @($Candidates) }
    return , (@($Candidates) + @($Intake.added))
}

# Digest rows for New-DailyDigest's 未登録 section, measured-but-unknown first. Each row is
# @{ Line; AgeSeconds } like every other section's rows, so the budget ladder treats it the same.
function Get-UnregisteredIntakeDigestRows {
    param($Intake)
    $rows = @()
    if ($null -eq $Intake) { return , $rows }
    if ($Intake.probe_error) {
        $rows += [pscustomobject]@{ Line = "  (全 project) — ? $($Intake.probe_error)"; AgeSeconds = 0 }
    }
    foreach ($u in @($Intake.unmeasured)) {
        $rows += [pscustomobject]@{ Line = "  $($u.project) — ? 測れなかった: $($u.reason)"; AgeSeconds = 0 }
    }
    foreach ($r in @($Intake.refused)) {
        $rows += [pscustomobject]@{ Line = "  $($r.key) — $($r.reason)"; AgeSeconds = 0 }
    }
    return , $rows
}

# The section header. "?" is appended whenever some part of the population was not measured, so a
# day that could not look is never shown as a clean 0.
function Get-UnregisteredIntakeHeader {
    param($Intake)
    $refused = @($Intake.refused).Count
    $added = @($Intake.added).Count
    $unknown = if ($Intake.probe_error -or @($Intake.unmeasured).Count -gt 0) { ' + ?' } else { '' }
    return "未登録（登録不可）: $refused 件$unknown（この tick の自動登録 $added 件）"
}
