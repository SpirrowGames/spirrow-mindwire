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

function Invoke-Fixture-Git {
    param([string]$Dir, [string[]]$GitArgs)
    $out = & git -C $Dir -c user.email='test@example.com' -c user.name='test' @GitArgs 2>&1
    if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') failed in ${Dir}: $out" }
    return (($out | ForEach-Object { "$_" }) -join "`n").Trim()
}

# Runs the script under test inside $work and returns its parsed JSON verdict.
function Get-SyncVerdict {
    param([string]$WorkDir)
    $raw = & pwsh -NoProfile -File (Join-Path $WorkDir "deploy/sync-repo.ps1") 2>&1
    $json = $raw | ForEach-Object { "$_" } | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
    if (-not $json) { throw "sync-repo.ps1 produced no JSON. Output: $(($raw | ForEach-Object { "$_" }) -join ' / ')" }
    return ($json | ConvertFrom-Json)
}

$root = Join-Path ([System.IO.Path]::GetTempPath()) ("mindwire-syncrepo-" + [guid]::NewGuid().ToString('N'))
$origin = Join-Path $root "origin"
$work = Join-Path $root "work"

try {
    New-Item -ItemType Directory -Path $origin -Force | Out-Null
    $null = Invoke-Fixture-Git $origin @('init', '--quiet', '-b', 'main')
    Set-Content -LiteralPath (Join-Path $origin "file.txt") -Value "one" -Encoding utf8
    $null = Invoke-Fixture-Git $origin @('add', '-A')
    $null = Invoke-Fixture-Git $origin @('commit', '--quiet', '-m', 'first')
    Set-Content -LiteralPath (Join-Path $origin "file.txt") -Value "two" -Encoding utf8
    $null = Invoke-Fixture-Git $origin @('commit', '--quiet', '-a', '-m', 'second')

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
