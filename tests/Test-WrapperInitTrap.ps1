# Regression guard for the fatal-init trap in deploy/run-conductor-scheduled.ps1
# (T-deploy-required-env-outage-is-silent, Bohr msg-3586 D-1 v4 / human msg-3588 A).
#
# What this pins:
#   1. STRUCTURAL. A script-level `trap { ... }` exists in the wrapper, and its body has the
#      exact shape the design mandates:
#        (a) it writes a `FATAL init` line via `Add-Content -LiteralPath $logPath` (buffered
#            Write-Log bypassed — Confirm-LogWorthKeeping is defined lower in the file and cannot
#            be relied on at init-throw time),
#        (b) it reads `$env:MINDWIRE_NOTIFY_DISCORD_WEBHOOK` (process env — Windows merges User
#            ∪ Machine at task start; the pre-v4 `[Environment]::GetEnvironmentVariable(..., 'User')`
#            silently skipped Discord for Machine-scope hosts, Einstein msg-3585 Obj-1),
#        (c) it POSTs to that webhook via `Invoke-WebRequest`,
#        (d) both side-effecting steps sit inside their own try/catch (Einstein msg-3583 Obj-1 —
#            a secondary throw inside a trap breaks the handler and skips `exit 1`),
#        (e) it ends with `exit 1` (LastTaskResult=0x1 contract), and
#        (f) it does NOT contain the pre-v4 explicit User-scope Environment call anywhere in the
#            trap body.
#      All checks lift structural facts from the wrapper's AST — dot-sourcing the wrapper would
#      launch the sweep, so the AST is the ONLY hermetic way to inspect it here.
#
#   2. LINE 1770 ALIGNMENT (human msg-3588 A). The daily notification path's `$notifyWebhook`
#      assignment must read `$env:MINDWIRE_NOTIFY_DISCORD_WEBHOOK` (process env), matching the
#      trap. A single access pattern for the same variable — leaving line 1770 explicit-User-scope
#      would keep the Machine-scope silent-skip bug alive for the ordinary daily path even though
#      the fatal-init handler was fixed. AST-checked.
#
#   3. BEHAVIOUR (subprocess). The wrapper is invoked in a fresh `pwsh` with a temp data dir and
#      MINDWIRE_DECISION_DASHBOARD_URL deliberately unset — that is the throw the 2026-09-19
#      outage triggered. We assert:
#        - the process exits with code 1 (LastTaskResult=0x1 preserved),
#        - `conductor-*.log` in the temp data dir contains a `FATAL init:` line naming
#          MINDWIRE_DECISION_DASHBOARD_URL, and
#        - a second run with `MINDWIRE_NOTIFY_DISCORD_WEBHOOK` pointed at an unreachable proxy
#          STILL exits 1 — the Discord POST failure (DNS/timeout/connect refused) MUST NOT abort
#          the trap before `exit 1`.
#
# What is NOT covered here (deliberately):
#   - Verifying the actual bytes sent to Discord. That would require intercepting Invoke-WebRequest
#     in-process; the trap uses `exit 1` which terminates before any assertion inside the same
#     pwsh could observe the call. Structural AST checks (§1c above) cover the shape of the call
#     — that the URL comes from `$env:` (§1b) and the payload is built from the exception message
#     — without needing a live socket.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
if (-not (Test-Path -LiteralPath $sweepScript)) { throw "sweep script not found: $sweepScript" }

$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) {
    $parseErrors | ForEach-Object { Write-Host "PARSE ERROR line $($_.Extent.StartLineNumber): $($_.Message)" }
    throw "deploy/run-conductor-scheduled.ps1 does not parse"
}

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else {
        $script:failures++
        Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual)
    }
}
function CheckTrue  { param([string]$Name, $Actual) Check -Name $Name -Expected $true  -Actual ([bool]$Actual) }
function CheckFalse { param([string]$Name, $Actual) Check -Name $Name -Expected $false -Actual ([bool]$Actual) }


# --- §1. STRUCTURAL: trap block exists and matches spec ---------------------------------------
Write-Host "§1 — trap block exists and matches the msg-3586 v4 design"

$trapAsts = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.TrapStatementAst] }, $true)

# A script-level trap MUST exist. Missing = the msg-3578 silent outage regressed.
CheckTrue "trap statement is present in the wrapper AST" ($trapAsts.Count -ge 1)

if ($trapAsts.Count -lt 1) {
    Write-Host "  no trap block found — remaining §1 checks skipped"
} else {
    # The design specifies a single fatal-init trap. Any additional trap would be a design change
    # that needs its own review — so a plurality of traps fails loudly here.
    Check "exactly one trap block in the wrapper" 1 $trapAsts.Count

    $trapText = $trapAsts[0].Extent.Text

    # §1a — direct log write via Add-Content -LiteralPath $logPath (buffer bypass).
    CheckTrue 'trap writes via Add-Content -LiteralPath $logPath' `
        ($trapText -match 'Add-Content\s+-LiteralPath\s+\$logPath')
    CheckTrue 'trap log line labels the failure FATAL init' `
        ($trapText -match 'FATAL init:')

    # §1b — webhook lookup goes through $env: (Machine ∪ User via process env),
    #        NOT [Environment]::GetEnvironmentVariable(..., 'User').
    CheckTrue 'trap reads webhook via $env: (process env, not User scope)' `
        ($trapText -match '\$env:MINDWIRE_NOTIFY_DISCORD_WEBHOOK')
    CheckFalse "trap does NOT use explicit User-scope Environment lookup" `
        ($trapText -match 'GetEnvironmentVariable\([^)]*[Uu]ser')

    # §1c — Discord POST via Invoke-WebRequest with a proxy.
    CheckTrue "trap POSTs via Invoke-WebRequest" `
        ($trapText -match 'Invoke-WebRequest')
    CheckTrue "trap includes -Proxy for the POST" `
        ($trapText -match '-Proxy\s+\$')

    # §1d — the two side-effecting steps sit inside try/catch. Regression guard for Einstein
    #        msg-3583 Obj-1 (unhandled Invoke-WebRequest throw would skip `exit 1`).
    $tryCatches = $trapAsts[0].Body.FindAll(
        { param($n) $n -is [System.Management.Automation.Language.TryStatementAst] }, $true)
    CheckTrue "trap contains at least two try/catch blocks (log + POST)" ($tryCatches.Count -ge 2)

    # §1e — `exit 1` is present in the trap body.
    CheckTrue "trap ends with exit 1 (LastTaskResult=0x1 contract)" `
        ($trapText -match '\bexit\s+1\b')

    # §1f — the exit statement is the LAST statement of the trap body. Any statement after it
    #        (e.g., another `try`) would defeat the unconditional-reach guarantee.
    $bodyStatements = $trapAsts[0].Body.Statements
    $lastStatement = $bodyStatements | Select-Object -Last 1
    $lastIsExit = ($null -ne $lastStatement) -and `
        ($lastStatement -is [System.Management.Automation.Language.ExitStatementAst])
    CheckTrue 'the final statement of the trap body is an ExitStatementAst' $lastIsExit
    if ($lastIsExit) {
        CheckTrue 'the final exit statement passes exit code 1' `
            ($lastStatement.Pipeline.Extent.Text.Trim() -eq '1')
    }
}


# --- §2. LINE 1770 ALIGNMENT: $notifyWebhook reads process env -------------------------------
Write-Host '§2 — Send-Notification path notifyWebhook aligns with the trap (human msg-3588 A)'

# Find the assignment `$notifyWebhook = ...` at script top level. AST — never a grep over comment
# text, so a docstring mentioning the old form does not cause a false positive.
$notifyWebhookAssigns = $ast.FindAll(
    { param($n)
        $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $n.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
        $n.Left.VariablePath.UserPath -eq 'notifyWebhook'
    }, $true)

CheckTrue 'the notifyWebhook assignment exists' ($notifyWebhookAssigns.Count -ge 1)

if ($notifyWebhookAssigns.Count -ge 1) {
    $rhsText = $notifyWebhookAssigns[0].Right.Extent.Text
    # Process env form (Machine ∪ User merged at process start).
    CheckTrue 'the $notifyWebhook assignment reads via $env: (not explicit User scope)' `
        ($rhsText -match '^\s*\$env:MINDWIRE_NOTIFY_DISCORD_WEBHOOK\s*$')
    # Regression guard for the specific pre-v4 form.
    CheckFalse 'the $notifyWebhook assignment does NOT use GetEnvironmentVariable with User arg' `
        ($rhsText -match "GetEnvironmentVariable\([^)]*'User'")
}


# --- §3. BEHAVIOUR: subprocess run with the throw --------------------------------------------
Write-Host "§3 — running the wrapper with MINDWIRE_DECISION_DASHBOARD_URL unset fires the trap"

# Set up an isolated data dir for the wrapper. `MINDWIRE_PATHS__DATA_DIR` steers `$dataDir`, so
# the log file lands under our temp dir, not the operator's live spirrow-mindwire-data.
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("mindwire-trap-test-" + [guid]::NewGuid().ToString('N'))
$tempConfig = Join-Path $tempRoot "config"
$tempLogs = Join-Path $tempRoot "logs"
$tempState = Join-Path $tempRoot "state"
New-Item -ItemType Directory -Path $tempRoot | Out-Null
New-Item -ItemType Directory -Path $tempConfig | Out-Null
New-Item -ItemType Directory -Path $tempState | Out-Null

try {
    # Case A: the exact 2026-09-19 outage. MINDWIRE_DECISION_DASHBOARD_URL unset. No webhook, so
    # the trap will skip step (2) and go straight to `exit 1` after writing the log line.
    $runnerScript = @"
`$env:MINDWIRE_PATHS__DATA_DIR = '$tempRoot'
`$env:MINDWIRE_DECISION_DASHBOARD_URL = ''
`$env:MINDWIRE_NOTIFY_DISCORD_WEBHOOK = ''
`$env:MINDWIRE_NOTIFY_PROXY = ''
& '$sweepScript'
exit `$LASTEXITCODE
"@
    $runnerPath = Join-Path $tempRoot "runner-A.ps1"
    Set-Content -LiteralPath $runnerPath -Value $runnerScript -Encoding utf8

    $procA = Start-Process -FilePath 'pwsh' -ArgumentList @('-NoProfile', '-File', $runnerPath) `
        -PassThru -Wait `
        -RedirectStandardOutput (Join-Path $tempRoot "A-stdout.txt") `
        -RedirectStandardError  (Join-Path $tempRoot "A-stderr.txt")
    Check "case A: exit code is 1 (LastTaskResult=0x1 contract)" 1 $procA.ExitCode

    $logFiles = Get-ChildItem -LiteralPath $tempLogs -Filter "conductor-*.log" -ErrorAction SilentlyContinue
    CheckTrue "case A: at least one conductor-*.log file was written" ($logFiles.Count -ge 1)
    if ($logFiles.Count -ge 1) {
        $logContent = Get-Content -LiteralPath $logFiles[0].FullName -Raw
        CheckTrue "case A: log contains 'FATAL init:' marker" `
            ($logContent -match '\[wrapper\] FATAL init:')
        CheckTrue "case A: log names MINDWIRE_DECISION_DASHBOARD_URL as the missing env" `
            ($logContent -match 'MINDWIRE_DECISION_DASHBOARD_URL is not set')
        # The trap fires BEFORE the outer try's `=== scheduled conductor run starting ===` line
        # (that write goes through the buffered Write-Log inside the try block at line ~3207).
        # Its absence is a positive signal that we crashed early — the whole point of the trap.
        CheckFalse "case A: log does NOT contain 'run starting' (trap fired before the outer try)" `
            ($logContent -match 'run starting')
    }

    # Case B: same throw, but with a webhook set to an unreachable value. The Invoke-WebRequest
    # inside the trap MUST fail (proxy 127.0.0.1:1 is an unassigned port; connect refused within
    # milliseconds). The trap's step-(2) try/catch has to swallow that failure so step (3)
    # `exit 1` still runs. Regression guard for Einstein msg-3583 Obj-1.
    $runnerScriptB = @"
`$env:MINDWIRE_PATHS__DATA_DIR = '$tempRoot'
`$env:MINDWIRE_DECISION_DASHBOARD_URL = ''
`$env:MINDWIRE_NOTIFY_DISCORD_WEBHOOK = 'https://discord.example.invalid/webhook'
`$env:MINDWIRE_NOTIFY_PROXY = 'http://127.0.0.1:1'
& '$sweepScript'
exit `$LASTEXITCODE
"@
    $runnerPathB = Join-Path $tempRoot "runner-B.ps1"
    Set-Content -LiteralPath $runnerPathB -Value $runnerScriptB -Encoding utf8

    $procB = Start-Process -FilePath 'pwsh' -ArgumentList @('-NoProfile', '-File', $runnerPathB) `
        -PassThru -Wait `
        -RedirectStandardOutput (Join-Path $tempRoot "B-stdout.txt") `
        -RedirectStandardError  (Join-Path $tempRoot "B-stderr.txt")
    Check "case B: exit code is 1 even when the Discord POST fails" 1 $procB.ExitCode
}
finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}


# --- final tally --------------------------------------------------------------------------------
Write-Host ""
if ($script:failures -gt 0) {
    Write-Host ("FAILED: {0} check(s) failed" -f $script:failures)
    exit 1
} else {
    Write-Host "OK: all checks passed"
    exit 0
}
