# Regression guard for the slot-mode tick lock (deploy/run-conductor-scheduled.ps1):
# Test-TickLockContention, Enter-ConductorTickLock, Complete-TickLockFailure.
#
# T-ephemeral-impl-clone-slot-per-thread, Bohr msg-8249 / msg-8251 (Obj-20-1, Einstein go msg-8252):
# only an OS lock contention (ERROR_SHARING_VIOLATION 32 / ERROR_LOCK_VIOLATION 33) is the quiet
# exit-0 path; every other acquisition failure is logged with type, HResult and absolute path,
# notified once per cause, and ends with a non-zero exit even when the notifier throws. A missing
# state\ directory is never created. The handle is not inherited by child processes.
#
# The functions are lifted out of the sweep script's AST (dot-sourcing would run the sweep).
# Contention and inheritance are Windows semantics (the wrapper runs only under Task Scheduler);
# on another OS those two blocks are reported as SKIP, the rest runs everywhere.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }

foreach ($var in '$ConductorTickLockFileName', '$ConductorTickLockFailedExitCode', '$ConductorTickLockNoticeKey') {
    $assign = $ast.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $n.Left.Extent.Text -eq $var }, $true) | Select-Object -First 1
    if (-not $assign) { throw "$var not assigned in sweep script" }
    Invoke-Expression $assign.Extent.Text
}
foreach ($name in 'Test-TickLockContention', 'Enter-ConductorTickLock', 'Complete-TickLockFailure',
                  'Test-NotificationSuppressed', 'Send-NotificationIfChanged') {
    $fn = $ast.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $n.Name -eq $name }, $true) | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}

$script:logLines = [System.Collections.Generic.List[string]]::new()
function Write-Log { param([string]$Message) $script:logLines.Add($Message) }
$script:sent = [System.Collections.Generic.List[string]]::new()
$script:sendThrows = $false
function Send-Notification {
    param([string]$Message)
    if ($script:sendThrows) { throw 'webhook exploded' }
    $script:sent.Add($Message); return 'sent'
}

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual) }
}

$work = Join-Path ([System.IO.Path]::GetTempPath()) ("tick-lock-test-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    Check 'lock file name' 'conductor-tick.lock' $ConductorTickLockFileName
    Check 'failure exit code is non-zero and not a daemon code' 11 $ConductorTickLockFailedExitCode

    # --- HResult classifier: 32 / 33 only ---------------------------------------------------------
    Check 'sharing violation (0x80070020) is contention' $true (Test-TickLockContention -HResult ([int]0x80070020))
    Check 'lock violation (0x80070021) is contention' $true (Test-TickLockContention -HResult ([int]0x80070021))
    Check 'bare 32 is contention' $true (Test-TickLockContention -HResult 32)
    Check 'access denied (0x80070005) is not contention' $false (Test-TickLockContention -HResult ([int]0x80070005))
    Check 'path not found (0x80070003) is not contention' $false (Test-TickLockContention -HResult ([int]0x80070003))
    Check 'file not found (0x80070002) is not contention' $false (Test-TickLockContention -HResult ([int]0x80070002))
    Check 'zero is not contention' $false (Test-TickLockContention -HResult 0)

    # --- acquire, release, re-acquire ------------------------------------------------------------
    $lockPath = Join-Path $work 'conductor-tick.lock'
    $a = Enter-ConductorTickLock -Path $lockPath
    Check 'free lock -> acquired' 'acquired' $a.status
    Check 'acquired path is absolute' ([System.IO.Path]::GetFullPath($lockPath)) $a.path

    if ($IsWindows) {
        # --- contention: another holder -> quiet exit-0 path ------------------------------------
        $b = Enter-ConductorTickLock -Path $lockPath
        Check 'held lock -> contended' 'contended' $b.status
        Check 'contended carries no stream' $null $b.stream
        $a.stream.Dispose()
        $c = Enter-ConductorTickLock -Path $lockPath
        Check 'released lock -> next run acquires' 'acquired' $c.status
        $c.stream.Dispose()
        Check 'lock file is left in place (never deleted)' $true (Test-Path -LiteralPath $lockPath)

        # --- the handle is not inherited: a child outliving the holder does not keep the lock ---
        $h = Enter-ConductorTickLock -Path $lockPath
        $pwshExe = (Get-Process -Id $PID).Path
        # UseShellExecute = false -> CreateProcess with bInheritHandles = TRUE, the way the wrapper
        # starts mindwire-loop; every INHERITABLE handle of this process reaches the child.
        $psi = [System.Diagnostics.ProcessStartInfo]::new($pwshExe)
        foreach ($arg in '-NoProfile', '-Command', 'Start-Sleep -Seconds 30') { $psi.ArgumentList.Add($arg) }
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $child = [System.Diagnostics.Process]::Start($psi)
        try {
            Start-Sleep -Milliseconds 300
            $h.stream.Dispose()
            Check 'child still running after the holder released' $false $child.HasExited
            $d = Enter-ConductorTickLock -Path $lockPath
            Check 'child of the holder does not hold the lock' 'acquired' $d.status
            if ($d.stream) { $d.stream.Dispose() }
        } finally {
            if (-not $child.HasExited) { Stop-Process -Id $child.Id -Force }
        }
    } else {
        $a.stream.Dispose()
        Write-Host '  SKIP  contention / handle inheritance (Windows semantics; the wrapper runs only on Windows)'
    }

    # --- missing parent directory: failed, never created ------------------------------------------
    $missingDir = Join-Path $work 'no-state-dir'
    $f1 = Enter-ConductorTickLock -Path (Join-Path $missingDir 'conductor-tick.lock')
    Check 'missing state dir -> failed (not contended)' 'failed' $f1.status
    Check 'missing state dir type' 'System.IO.DirectoryNotFoundException' $f1.exception_type
    Check 'missing state dir is not created' $false (Test-Path -LiteralPath $missingDir)
    Check 'failure detail names the absolute path' $true ($f1.detail.Contains([System.IO.Path]::GetFullPath((Join-Path $missingDir 'conductor-tick.lock'))))
    Check 'failure detail names the type' $true ($f1.detail.Contains('DirectoryNotFoundException'))
    Check 'failure detail names the HResult' $true ($f1.detail -match 'HResult=0x[0-9A-F]{8}')

    # --- access denied: the lock path is a directory ----------------------------------------------
    $dirAsLock = Join-Path $work 'lock-is-a-dir'
    New-Item -ItemType Directory -Path $dirAsLock | Out-Null
    $f2 = Enter-ConductorTickLock -Path $dirAsLock
    Check 'unopenable path -> failed (not contended)' 'failed' $f2.status
    Check 'unopenable path is not classed as contention' $false (Test-TickLockContention -HResult $f2.hresult)

    # --- access denied: a read-only lock file -----------------------------------------------------
    $roLock = Join-Path $work 'ro.lock'
    Set-Content -LiteralPath $roLock -Value '' -NoNewline
    (Get-Item -LiteralPath $roLock).IsReadOnly = $true
    try {
        $f3 = Enter-ConductorTickLock -Path $roLock
        Check 'read-only lock file -> failed' 'failed' $f3.status
        Check 'read-only lock file type' 'System.UnauthorizedAccessException' $f3.exception_type
    } finally {
        (Get-Item -LiteralPath $roLock).IsReadOnly = $false
    }

    # --- failure handling: log + notice once per cause + non-zero exit --------------------------
    $state = @{}
    $script:logLines.Clear(); $script:sent.Clear()
    $code = Complete-TickLockFailure -State $state -Failure $f1
    Check 'failure -> non-zero exit code' $ConductorTickLockFailedExitCode $code
    Check 'failure is logged with its detail' $true ($script:logLines -contains $f1.detail)
    Check 'failure notice is sent' 1 $script:sent.Count
    Check 'notice names the absolute path' $true ($script:sent[0].Contains($f1.path))
    Check 'notice key recorded' $true $state.ContainsKey($ConductorTickLockNoticeKey)

    $f1b = Enter-ConductorTickLock -Path (Join-Path $missingDir 'conductor-tick.lock')
    $code = Complete-TickLockFailure -State $state -Failure $f1b
    Check 'same cause on the next tick -> still non-zero' $ConductorTickLockFailedExitCode $code
    Check 'same cause on the next tick -> no second notice' 1 $script:sent.Count

    $code = Complete-TickLockFailure -State $state -Failure $f3
    Check 'different cause -> a new notice' 2 $script:sent.Count

    # --- a throwing notifier never stops the log line or the non-zero exit ------------------------
    $script:sendThrows = $true
    $script:logLines.Clear()
    $code = Complete-TickLockFailure -State @{} -Failure $f2
    Check 'notifier throws -> still non-zero exit' $ConductorTickLockFailedExitCode $code
    Check 'notifier throws -> failure still logged' $true ($script:logLines -contains $f2.detail)
    $script:sendThrows = $false
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}

if ($script:failures -gt 0) { Write-Host "$($script:failures) failure(s)"; exit 1 }
Write-Host "all passed"
