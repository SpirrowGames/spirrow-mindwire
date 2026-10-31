# Regression guard for deploy/sync-repo.ps1 — the auto-deploy step.
#
# What makes this worth testing: every wrong answer here is quiet. Pull when it should not and a
# human's work-in-progress is yanked out from under a running daemon; refuse when it should not and
# the loop runs stale code while GitHub shows the fix merged and the task history shows exit 0. Both
# look like nothing happening.
#
# The fixture is a git repo built from scratch in a temp directory — NOT this checkout. Depending on
# the real repo's history would make the test hostage to CI clone depth (`actions/checkout` fetches
# one commit by default, so `HEAD~1` would not exist) and would risk a test that mutates the working
# tree it is running from.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$syncScript = Join-Path $repoRoot "deploy/sync-repo.ps1"
if (-not (Test-Path -LiteralPath $syncScript)) { throw "sync script not found: $syncScript" }

$parseErrors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($syncScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) {
    $parseErrors | ForEach-Object { Write-Host "PARSE ERROR line $($_.Extent.StartLineNumber): $($_.Message)" }
    throw "deploy/sync-repo.ps1 does not parse"
}

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual) }
}
function CheckTrue {
    param([string]$Name, [bool]$Actual, [string]$Detail = '')
    if ($Actual) { Write-Host ("  PASS  {0}" -f $Name); return }
    $script:failures++
    Write-Host ("  FAIL  {0}{1}" -f $Name, $(if ($Detail) { " — $Detail" } else { '' }))
}

function Invoke-Fixture-Git {
    param([string]$Dir, [string[]]$GitArgs)
    $out = & git -C $Dir -c user.email='test@example.com' -c user.name='test' @GitArgs 2>&1
    if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') failed in ${Dir}: $out" }
    return (($out | ForEach-Object { "$_" }) -join "`n").Trim()
}

# Runs the script under test inside $work and returns its parsed JSON verdict. -StatePath and -Uv
# are always passed: the venv-sync record must land in the fixture, never in the host's data root,
# and uv is a stub so no test ever syncs a real environment.
function Get-SyncVerdict {
    param([string]$WorkDir)
    $raw = & pwsh -NoProfile -File (Join-Path $WorkDir "deploy/sync-repo.ps1") `
        -StatePath $script:venvStatePath -Uv $script:uvStub 2>&1
    $json = $raw | ForEach-Object { "$_" } | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
    if (-not $json) { throw "sync-repo.ps1 produced no JSON. Output: $(($raw | ForEach-Object { "$_" }) -join ' / ')" }
    return ($json | ConvertFrom-Json)
}

$root = Join-Path ([System.IO.Path]::GetTempPath()) ("mindwire-syncrepo-" + [guid]::NewGuid().ToString('N'))
$origin = Join-Path $root "origin"
$work = Join-Path $root "work"
$script:venvStatePath = Join-Path $root "state/venv-sync.json"
$script:uvLog = Join-Path $root "uv-calls.log"
$script:uvStub = Join-Path $root "uv-stub.ps1"

# The uv stub: logs each call's argument list as one line and exits with the code the test sets via
# env (sync and import check separately). Prints a per-call-varying message on failure, so the
# dedup-key check below proves the key does not depend on uv's text.
function Reset-UvStub {
    param([int]$SyncExit = 0, [int]$ImportExit = 0)
    $env:STUB_UV_SYNC_EXIT = "$SyncExit"
    $env:STUB_UV_IMPORT_EXIT = "$ImportExit"
    if (Test-Path -LiteralPath $script:uvLog) { Remove-Item -LiteralPath $script:uvLog -Force }
}
function Get-UvCalls {
    if (-not (Test-Path -LiteralPath $script:uvLog)) { return , @() }
    return , @(Get-Content -LiteralPath $script:uvLog -Encoding utf8)
}
# The deps_hash the script must compute: name-prefixed per-file SHA-256s, joined, hashed again.
function Get-ExpectedDepsHash {
    param([string]$Dir)
    $parts = foreach ($name in @('pyproject.toml', 'uv.lock')) {
        $f = Join-Path $Dir $name
        if (Test-Path -LiteralPath $f) { "${name}:$((Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash.ToLowerInvariant())" }
        else { "${name}:<missing>" }
    }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes(($parts -join "`n"))) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}
function Get-RecordedHash {
    if (-not (Test-Path -LiteralPath $script:venvStatePath)) { return $null }
    return (Get-Content -LiteralPath $script:venvStatePath -Raw | ConvertFrom-Json).deps_hash
}

try {
    New-Item -ItemType Directory -Path $origin -Force | Out-Null
    $null = Invoke-Fixture-Git $origin @('init', '--quiet', '-b', 'main')
    Set-Content -LiteralPath (Join-Path $origin "file.txt") -Value "one" -Encoding utf8
    $null = Invoke-Fixture-Git $origin @('add', '-A')
    $null = Invoke-Fixture-Git $origin @('commit', '--quiet', '-m', 'first')
    Set-Content -LiteralPath (Join-Path $origin "file.txt") -Value "two" -Encoding utf8
    $null = Invoke-Fixture-Git $origin @('commit', '--quiet', '-a', '-m', 'second')

    New-Item -ItemType Directory -Path $root -Force | Out-Null
    Set-Content -LiteralPath $script:uvStub -Encoding utf8 -Value @'
Add-Content -LiteralPath $env:STUB_UV_LOG -Value ($args -join ' ') -Encoding utf8
if ($args[0] -eq 'sync') { $code = [int]$env:STUB_UV_SYNC_EXIT } else { $code = [int]$env:STUB_UV_IMPORT_EXIT }
if ($code -ne 0) { Write-Output "stub uv failure $([guid]::NewGuid())" }
exit $code
'@
    $env:STUB_UV_LOG = $script:uvLog
    Reset-UvStub

    $null = & git clone --quiet $origin $work 2>&1
    if ($LASTEXITCODE -ne 0) { throw "clone failed" }
    New-Item -ItemType Directory -Path (Join-Path $work "deploy") -Force | Out-Null
    Copy-Item -LiteralPath $syncScript -Destination (Join-Path $work "deploy/sync-repo.ps1") -Force

    Write-Host "sync-repo — the happy paths"
    $v = Get-SyncVerdict $work
    Check "already at origin/main -> current" 'current' $v.status

    $head = Invoke-Fixture-Git $work @('rev-parse', 'HEAD')
    $null = Invoke-Fixture-Git $work @('reset', '--quiet', '--hard', 'HEAD~1')
    $v = Get-SyncVerdict $work
    Check "one commit behind -> updated" 'updated' $v.status
    Check "updated reports 1 commit" 1 $v.commits
    Check "fast-forward actually moved HEAD" $head (Invoke-Fixture-Git $work @('rev-parse', 'HEAD'))
    Check "no dependency manifests touched -> no uv sync" $false $v.synced_deps
    Check "no pyproject.toml / uv.lock at all -> uv never called" 0 (Get-UvCalls).Count

    Write-Host "sync-repo — refuses to touch anything it should not"
    Add-Content -LiteralPath (Join-Path $work "file.txt") -Value "local edit"
    $v = Get-SyncVerdict $work
    Check "modified tracked file -> blocked" 'blocked' $v.status
    $null = Invoke-Fixture-Git $work @('checkout', '--', 'file.txt')

    # The live host carries untracked notes (spec/loop-autonomy-control.md); they must never block.
    Set-Content -LiteralPath (Join-Path $work "untracked-note.md") -Value "scratch" -Encoding utf8
    $v = Get-SyncVerdict $work
    Check "untracked file does NOT block" 'current' $v.status
    Remove-Item -LiteralPath (Join-Path $work "untracked-note.md") -Force

    $null = Invoke-Fixture-Git $work @('commit', '--quiet', '--allow-empty', '-m', 'local only')
    $v = Get-SyncVerdict $work
    Check "diverged from origin -> blocked" 'blocked' $v.status
    $null = Invoke-Fixture-Git $work @('reset', '--quiet', '--hard', 'origin/main')

    $null = Invoke-Fixture-Git $work @('switch', '--quiet', '-c', 'feature/x')
    $v = Get-SyncVerdict $work
    Check "not on main -> skipped" 'skipped' $v.status
    Check "skipped names the branch" 'feature/x' $v.branch
    $null = Invoke-Fixture-Git $work @('switch', '--quiet', 'main')


    # --- D-2' (T-composer-entrypoint-missing-drops-decision-cards): venv sync on deps_hash ---------
    Write-Host "sync-repo — venv sync keyed on deps_hash, --locked, import check, retry until success"
    $null = Invoke-Fixture-Git $work @('remote', 'set-url', 'origin', $origin)
    Set-Content -LiteralPath (Join-Path $origin "pyproject.toml") -Value "[project]`nname = 'x'" -Encoding utf8
    Set-Content -LiteralPath (Join-Path $origin "uv.lock") -Value "version = 1" -Encoding utf8
    $null = Invoke-Fixture-Git $origin @('add', '-A')
    $null = Invoke-Fixture-Git $origin @('commit', '--quiet', '-m', 'add manifests')

    Reset-UvStub
    $v = Get-SyncVerdict $work
    $expected = Get-ExpectedDepsHash $work
    Check "manifests arrive -> updated" 'updated' $v.status
    Check "manifests arrive -> synced_deps" $true $v.synced_deps
    $calls = Get-UvCalls
    Check "uv is called twice (sync + import check)" 2 $calls.Count
    Check "uv sync runs with --locked" 'sync --locked' $calls[0]
    Check "import check runs the two launched modules with --no-sync" `
        'run --no-sync python -c import spirrow_mindwire.loop_runner, spirrow_mindwire.decision_request.cli' $calls[1]
    Check "recorded hash is the hash computed before the sync" $expected (Get-RecordedHash)

    Reset-UvStub
    $v = Get-SyncVerdict $work
    Check "hash matches the record -> current" 'current' $v.status
    Check "hash matches the record -> no sync" $false $v.synced_deps
    Check "hash matches the record -> uv not called" 0 (Get-UvCalls).Count

    # pyproject.toml alone changes (no uv.lock change): still a new hash, still a sync.
    Add-Content -LiteralPath (Join-Path $origin "pyproject.toml") -Value "version = '2'" -Encoding utf8
    $null = Invoke-Fixture-Git $origin @('commit', '--quiet', '-a', '-m', 'pyproject only')
    Reset-UvStub
    $v = Get-SyncVerdict $work
    Check "pyproject.toml-only change -> synced" $true $v.synced_deps
    Check "pyproject.toml-only change -> uv sync --locked ran" 'sync --locked' (Get-UvCalls)[0]
    Check "pyproject.toml-only change -> new hash recorded" (Get-ExpectedDepsHash $work) (Get-RecordedHash)

    # A lock that does not match: uv sync --locked fails. Nothing recorded, retried next tick,
    # same deps_hash both times (the wrapper's dedup key), regardless of uv's varying text.
    Add-Content -LiteralPath (Join-Path $origin "pyproject.toml") -Value "dependencies = ['y']" -Encoding utf8
    $null = Invoke-Fixture-Git $origin @('commit', '--quiet', '-a', '-m', 'stale lock')
    $before = Get-RecordedHash
    Reset-UvStub -SyncExit 2
    $v1 = Get-SyncVerdict $work
    Check "lock mismatch -> failed" 'failed' $v1.status
    Check "lock mismatch -> deps_hash reported" (Get-ExpectedDepsHash $work) $v1.deps_hash
    Check "lock mismatch -> record untouched" $before (Get-RecordedHash)
    Check "lock mismatch -> no import check after a failed sync" 1 (Get-UvCalls).Count
    CheckTrue "lock mismatch after a pull -> reason says pulled, and names --locked" `
        ($v1.reason.StartsWith('pulled, but') -and $v1.reason.Contains('--locked')) $v1.reason
    Check "lock mismatch -> the tracked uv.lock was not rewritten (worktree clean)" '' `
        (Invoke-Fixture-Git $work @('status', '--porcelain', '--untracked-files=no'))
    Reset-UvStub -SyncExit 2
    $v2 = Get-SyncVerdict $work
    Check "next tick (git current) -> retried and failed again" 'failed' $v2.status
    Check "next tick -> uv sync retried" 'sync --locked' (Get-UvCalls)[0]
    Check "same state -> same deps_hash (stable dedup key)" $v1.deps_hash $v2.deps_hash
    CheckTrue "uv's text differed between the two failures (the key does not depend on it)" ($v1.reason -ne $v2.reason)

    # Sync exits 0 but the launched modules do not import: also a failure, also keyed on deps_hash.
    Reset-UvStub -SyncExit 0 -ImportExit 1
    $v = Get-SyncVerdict $work
    Check "import check fails -> failed" 'failed' $v.status
    Check "import check fails -> deps_hash reported" (Get-ExpectedDepsHash $work) $v.deps_hash
    CheckTrue "import check fails -> reason names the import" ($v.reason.Contains('do not import')) $v.reason
    Check "import check fails -> record untouched" $before (Get-RecordedHash)

    # The fix arrives (environment repaired): the retry succeeds without any new commit.
    Reset-UvStub
    $v = Get-SyncVerdict $work
    Check "repaired -> current with synced_deps" $true ($v.status -eq 'current' -and $v.synced_deps)
    Check "repaired -> hash recorded" (Get-ExpectedDepsHash $work) (Get-RecordedHash)

    Write-Host "wrapper — Get-DeployHealthSignature keys venv-sync failures on deps_hash"
    $wrapper = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
    $wAst = [System.Management.Automation.Language.Parser]::ParseFile($wrapper, [ref]$null, [ref]$null)
    $fn = $wAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-DeployHealthSignature' }, $true) | Select-Object -First 1
    if (-not $fn) { throw "Get-DeployHealthSignature not found in the wrapper" }
    Invoke-Expression $fn.Extent.Text
    Check "same deps_hash, different uv text -> same signature" `
        (Get-DeployHealthSignature -Sync $v1) (Get-DeployHealthSignature -Sync $v2)
    Check "deps signature shape" "failed:deps:$($v1.deps_hash)" (Get-DeployHealthSignature -Sync $v1)
    $other = [pscustomobject]@{ status = 'failed'; deps_hash = 'abc'; reason = 'x' }
    CheckTrue "different deps_hash -> different signature (a new commit re-alerts)" `
        ((Get-DeployHealthSignature -Sync $other) -ne (Get-DeployHealthSignature -Sync $v1))
    $gitFail = [pscustomobject]@{ status = 'failed'; reason = 'fetch failed: x' }
    Check "git-step failure keeps the status:reason key" 'failed:fetch failed: x' (Get-DeployHealthSignature -Sync $gitFail)

    Write-Host "sync-repo — an unreachable origin is reported, not thrown"
    $null = Invoke-Fixture-Git $work @('remote', 'set-url', 'origin', (Join-Path $root "does-not-exist"))
    $v = Get-SyncVerdict $work
    Check "unreachable origin -> failed" 'failed' $v.status
    Check "failure reason is a single line" $true (-not $v.reason.Contains("`n"))
}
finally {
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }
}

Write-Host ""
if ($script:failures -gt 0) { Write-Host "sync-repo: $($script:failures) check(s) FAILED"; exit 1 }
Write-Host "sync-repo: all checks passed"
exit 0
