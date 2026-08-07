#!/usr/bin/env pwsh
# deploy/sync-repo.ps1 — fast-forward the daemon's own checkout to origin/main, and say what it did.
#
# Why this exists: merging a PR did not deploy it. The scheduled task runs `uv run mindwire-loop`
# from this checkout, and nothing pulled it, so the loop kept running whatever commit the working
# tree happened to sit on. The gap is invisible from both ends — GitHub shows the fix merged, the
# task history shows exit 0 — and the only way to notice was to compare `git log` against
# `origin/main` by hand.
#
# Deploying merged `main` needs no separate approval: `main` only advances through a human's Tier-C
# merge, so the decision has already been made by the time this script can see it. What this adds is
# delivery, not authority.
#
# **Structured output, no side effects on the caller's decisions.** Like scripts/thread_heads.py and
# scripts/loop_control.py, this prints one JSON object on stdout and lets the caller decide what is
# worth logging or notifying. That keeps notification policy in one place (the sweep wrapper) instead
# of duplicating a webhook here.
#
#   {"status":"updated","branch":"main","from":"b79a698","to":"3ec2290","commits":2,"synced_deps":true}
#   {"status":"current","branch":"main","head":"b79a698"}
#   {"status":"skipped","branch":"fix/foo","reason":"not on main - a human is working on this checkout"}
#
# The emitted strings are ASCII on purpose: this JSON crosses a process boundary into the wrapper and
# on into a Discord message, and a stray em dash came back as `?` when it did.
#   {"status":"blocked","branch":"main","reason":"tracked files are modified locally"}
#   {"status":"failed","branch":"main","reason":"fetch failed: ..."}
#
# `status` is the whole contract. Exit code is 0 whenever a verdict was reached (including `blocked`
# and `failed`) and non-zero only when this script could not run at all — the caller must not treat
# "the sync failed" as "the sweep failed", or one unreachable GitHub would stop the loop entirely.
#
# What each non-happy status means, and why none of them pull anyway:
#
# - `skipped` — HEAD is not `main`. Someone is working on this checkout; yanking their branch out
#   from under them is worse than running slightly stale code, and it would also destroy work in
#   progress. The caller still reports it, because "the loop host is running a feature branch" is
#   exactly the fact that otherwise goes unnoticed for days.
# - `blocked` — tracked files are modified, or the branch has diverged from origin. Both mean a
#   fast-forward would either lose work or is impossible. Fail loud, change nothing.
# - `failed` — the fetch or the merge itself errored (network, auth, proxy). Keep running the code we
#   have; it is known-good, just possibly old.
#
# Untracked files never block: the live host deliberately carries untracked working notes
# (`spec/loop-autonomy-control.md`), and a fast-forward cannot conflict with them.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot

function Write-Result {
    param([hashtable]$Result)
    $Result | ConvertTo-Json -Compress
    exit 0
}

# `git -C` throughout: this script must not depend on, or change, the caller's working directory.
function Invoke-Git {
    param([string[]]$GitArgs)
    $out = & git -C $repoRoot @GitArgs 2>&1
    return [pscustomobject]@{
        Ok     = ($LASTEXITCODE -eq 0)
        Output = (($out | ForEach-Object { "$_" }) -join "`n").Trim()
    }
}

# git's failure text is several lines ("fatal: …" + "Please make sure you have…"). A `reason` ends up
# in a Discord message and a log line, so it is flattened to one line here rather than at each site.
function Get-OneLine {
    param([string]$Text)
    return (($Text -replace "\r?\n", " ") -replace "\s{2,}", " ").Trim()
}

if (-not (Test-Path -LiteralPath (Join-Path $repoRoot ".git"))) {
    Write-Error "not a git checkout: $repoRoot"
    exit 2
}

$branchResult = Invoke-Git @('rev-parse', '--abbrev-ref', 'HEAD')
if (-not $branchResult.Ok) {
    Write-Result @{ status = "failed"; branch = $null; reason = "cannot read HEAD: $(Get-OneLine $branchResult.Output)" }
}
$branch = $branchResult.Output

if ($branch -ne 'main') {
    Write-Result @{
        status = "skipped"; branch = $branch
        reason = "not on main - a human is working on this checkout"
    }
}

# Tracked modifications only. Untracked files are expected on the live host and cannot block a
# fast-forward.
$dirty = Invoke-Git @('status', '--porcelain', '--untracked-files=no')
if (-not $dirty.Ok) {
    Write-Result @{ status = "failed"; branch = $branch; reason = "cannot read status: $(Get-OneLine $dirty.Output)" }
}
if ($dirty.Output) {
    Write-Result @{
        status = "blocked"; branch = $branch
        reason = "tracked files are modified locally: $(($dirty.Output -split "`n") -join '; ')"
    }
}

$fetch = Invoke-Git @('fetch', 'origin', 'main', '--quiet')
if (-not $fetch.Ok) {
    Write-Result @{ status = "failed"; branch = $branch; reason = "fetch failed: $(Get-OneLine $fetch.Output)" }
}

$before = (Invoke-Git @('rev-parse', 'HEAD')).Output
$target = Invoke-Git @('rev-parse', 'origin/main')
if (-not $target.Ok) {
    Write-Result @{ status = "failed"; branch = $branch; reason = "cannot resolve origin/main: $(Get-OneLine $target.Output)" }
}
if ($before -eq $target.Output) {
    Write-Result @{ status = "current"; branch = $branch; head = $before.Substring(0, 7) }
}

# Refuse anything that is not a pure fast-forward. A diverged checkout means someone committed here;
# merging or resetting would be this script inventing a resolution nobody asked for.
$ff = Invoke-Git @('merge-base', '--is-ancestor', 'HEAD', 'origin/main')
if (-not $ff.Ok) {
    Write-Result @{
        status = "blocked"; branch = $branch
        reason = "local main has diverged from origin/main - not a fast-forward"
    }
}

$countResult = Invoke-Git @('rev-list', '--count', 'HEAD..origin/main')
$commits = if ($countResult.Ok) { [int]$countResult.Output } else { $null }

$merge = Invoke-Git @('merge', '--ff-only', 'origin/main')
if (-not $merge.Ok) {
    Write-Result @{ status = "failed"; branch = $branch; reason = "ff-only merge failed: $(Get-OneLine $merge.Output)" }
}
$after = (Invoke-Git @('rev-parse', 'HEAD')).Output

# `uv run` syncs the environment on its own, so this is not strictly required — but a dependency
# change that cannot install should surface HERE, attributed to the deploy that caused it, rather
# than as an unexplained failure on the next tick. Only run it when the manifests actually moved.
$syncedDeps = $false
$touched = Invoke-Git @('diff', '--name-only', "$before..$after", '--', 'pyproject.toml', 'uv.lock')
if ($touched.Ok -and $touched.Output) {
    Push-Location $repoRoot
    try {
        $uvOut = & uv sync 2>&1
        $uvCode = $LASTEXITCODE
    }
    finally { Pop-Location }
    if ($uvCode -ne 0) {
        Write-Result @{
            status = "failed"; branch = $branch
            from   = $before.Substring(0, 7); to = $after.Substring(0, 7)
            reason = "pulled, but 'uv sync' failed: $((($uvOut | ForEach-Object { "$_" }) -join ' ').Trim())"
        }
    }
    $syncedDeps = $true
}

Write-Result @{
    status      = "updated"; branch = $branch
    from        = $before.Substring(0, 7); to = $after.Substring(0, 7)
    commits     = $commits
    synced_deps = $syncedDeps
}
