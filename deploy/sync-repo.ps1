#!/usr/bin/env pwsh
# deploy/sync-repo.ps1 — fast-forward the daemon's own checkout to origin/main, and say what it did.
#
# Why this exists: merging a PR did not deploy it. The scheduled task runs the loop
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
#
# **The venv is synced here and nowhere else** (T-composer-entrypoint-missing-drops-decision-cards
# D-2'). Every launch the wrapper makes is `uv run --no-sync python -m ...`, so nothing re-syncs the
# venv in the middle of a tick. That makes this step the only place a broken venv gets repaired, so
# it must not give up after one failed try:
#
# - Trigger: `deps_hash` (SHA-256 over `pyproject.toml` + `uv.lock`) differs from the hash recorded
#   by the last SUCCESSFUL sync in -StatePath (default `<data_dir>/state/venv-sync.json`). It is NOT
#   "the manifests moved in this pull": that rule never retried a failed sync, so one bad deploy
#   left a broken venv running for good. A failed sync records nothing, so the next tick tries
#   again. A checkout carrying neither file has nothing to sync.
# - `uv sync --locked`, never a bare `uv sync`. A bare sync may rewrite `uv.lock` here, and a
#   modified tracked file blocks every later deploy (`blocked` above). `--locked` refuses a lock
#   that does not match `pyproject.toml` and changes nothing, so the fix is a commit, made where
#   commits are made. (`--frozen` was rejected: it installs a stale lock and reports success.)
# - Success = exit 0 AND the two modules the wrapper launches import from the venv
#   (`spirrow_mindwire.loop_runner`, `spirrow_mindwire.decision_request.cli`). The check is on the
#   modules actually launched, not on `.venv\Scripts\*.exe`, which nothing launches any more.
# - Every failure of this step reports `status=failed` with `deps_hash`, and the wrapper keys its
#   alert dedup on that hash, not on uv's message text: the same broken state alerts once, and a
#   new commit (new hash) that still fails alerts again.
#
#   {"status":"current","branch":"main","head":"b79a698","synced_deps":true}
#   {"status":"failed","branch":"main","deps_hash":"3f2a...","reason":"'uv sync --locked' failed ..."}

param(
    # Where the last successful sync's deps_hash is recorded. The wrapper passes its own
    # state\venv-sync.json; a hand-run falls back to the same file under the data root.
    [string]$StatePath = '',
    # The uv executable. A parameter only so tests/Test-SyncRepo.ps1 can substitute a stub.
    [string]$Uv = 'uv'
)

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
    $gitResult = @{ status = "current"; branch = $branch; head = $before.Substring(0, 7) }
}
else {
    # Refuse anything that is not a pure fast-forward. A diverged checkout means someone committed
    # here; merging or resetting would be this script inventing a resolution nobody asked for.
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
    $gitResult = @{
        status  = "updated"; branch = $branch
        from    = $before.Substring(0, 7); to = $after.Substring(0, 7)
        commits = $commits
    }
}

# --- venv sync (D-2', see the header) --------------------------------------------------------------

# SHA-256 over pyproject.toml + uv.lock: each file's SHA-256, prefixed with its name, in a fixed
# order, joined with newlines and hashed again. $null when neither file exists (nothing to sync).
function Get-DepsHash {
    $parts = @()
    $present = 0
    foreach ($name in @('pyproject.toml', 'uv.lock')) {
        $path = Join-Path $repoRoot $name
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $present++
            $parts += "${name}:$((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant())"
        }
        else { $parts += "${name}:<missing>" }
    }
    if ($present -eq 0) { return $null }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($parts -join "`n"))
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}

# The deps_hash of the last successful sync, or $null. No record and an unreadable record get the
# same answer, "sync again", which costs one no-op `uv sync --locked`.
function Get-RecordedDepsHash {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        $rec = Get-Content -LiteralPath $Path -Raw -Encoding utf8 | ConvertFrom-Json
        if ($rec -and $rec.deps_hash) { return [string]$rec.deps_hash }
    }
    catch { }
    return $null
}

function Save-DepsHash {
    param([string]$Path, [string]$Hash)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $json = @{ deps_hash = $Hash; synced_at = (Get-Date).ToUniversalTime().ToString('o') } | ConvertTo-Json -Compress
    $tmp = "$Path.tmp"
    [System.IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Invoke-Uv {
    param([string[]]$UvArgs)
    Push-Location $repoRoot
    try {
        $out = & $Uv @UvArgs 2>&1
        $code = $LASTEXITCODE
    }
    finally { Pop-Location }
    return [pscustomobject]@{ Code = $code; Output = (Get-OneLine (($out | ForEach-Object { "$_" }) -join ' ')) }
}

if (-not $StatePath) {
    $dataRoot = if ($env:MINDWIRE_PATHS__DATA_DIR) { $env:MINDWIRE_PATHS__DATA_DIR } else { Join-Path $HOME "spirrow-mindwire-data" }
    $StatePath = Join-Path $dataRoot "state/venv-sync.json"
}

# Computed ONCE, before the sync: `--locked` never writes a tracked file, so the value cannot move
# under us, and the hash recorded on success is exactly the one that decided to sync.
$depsHash = Get-DepsHash
$syncedDeps = $false
if ($null -ne $depsHash -and $depsHash -ne (Get-RecordedDepsHash -Path $StatePath)) {
    $failure = $null
    $sync = Invoke-Uv @('sync', '--locked')
    if ($sync.Code -ne 0) {
        $failure = "'uv sync --locked' failed (exit $($sync.Code)): $($sync.Output)"
    }
    else {
        $import = Invoke-Uv @('run', '--no-sync', 'python', '-c',
            'import spirrow_mindwire.loop_runner, spirrow_mindwire.decision_request.cli')
        if ($import.Code -ne 0) {
            $failure = "'uv sync --locked' exited 0 but the launched modules do not import (exit $($import.Code)): $($import.Output)"
        }
    }
    if ($failure) {
        # Nothing recorded, so the next tick tries again. deps_hash is the wrapper's dedup key.
        $result = @{ status = "failed"; branch = $branch; deps_hash = $depsHash; reason = $failure }
        if ($gitResult.status -eq 'updated') {
            $result.from = $gitResult.from; $result.to = $gitResult.to
            $result.reason = "pulled, but $failure"
        }
        Write-Result $result
    }
    Save-DepsHash -Path $StatePath -Hash $depsHash
    $syncedDeps = $true
}

$gitResult.synced_deps = $syncedDeps
Write-Result $gitResult
