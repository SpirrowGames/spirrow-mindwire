# Pins the sweep wrapper's per-project SEQUENTIALITY — the "runs at most one
# subprocess per project per tick" claim that gate_bootstrap_visibility.py's
# **Concurrency profile.** paragraph relies on to declare its tight
# load/modify/save window "not observable under the current sweep
# architecture". That correctness argument terminates in a claim about a
# different file (deploy/run-conductor-scheduled.ps1) in a different language
# (PowerShell), and until this file existed nothing in the tree would fire if
# a maintainer parallelized the sweep and silently invalidated it.
#
# Thread: T-sweep-sequentiality-load-bearing-but-unpinned.
# Protected prose lives in src/spirrow_mindwire/gate_bootstrap_visibility.py
# under **Concurrency profile.** (search for the phrase; line numbers drift).
#
# Design (msg-585 / msg-587):
#
#   L1  — no PipelineAst in the file has .Background -eq $true.  This closes
#         the postfix `&` async form STRUCTURALLY, without naming any cmdlet.
#         `Start-Job { … }`, `Invoke-Command -AsJob { … }`, PowerShell 7's
#         `& { … } &` all reduce to a Background pipeline; nothing that gets
#         it wrong appears at the source.
#
#   L2a — inside the dispatch ForEachStatementAst, exactly one CommandAst has
#         InvocationOperator = 'Ampersand'; its command element is $inner;
#         its result is consumed by an assignment.
#   L2b — $inner's assignment (anywhere in the script) has RHS
#         `Join-Path $PSScriptRoot 'run-conductor.ps1'`.  Without this,
#         L2a is trivially defeated by repointing $inner at a job wrapper
#         and changing nothing at the call site.
#   L2c — walking from the spawn site's CommandAst up the .Parent chain:
#           * the nearest enclosing loop is the dispatch ForEachStatementAst,
#           * no ancestor PipelineAst has .Background,
#           * no ancestor is a ScriptBlockExpressionAst passed as a command
#             argument (that is the SHAPE of `Start-Job { … }`,
#             `ForEach-Object -Parallel { … }`, `[Task]::Run({ … })`, etc.).
#
#   L3b — inside the dispatch body, every InvokeMemberExpressionAst whose
#         target is a TypeExpressionAst (i.e. every `[Type]::Method(...)`
#         static call) is on a one-entry allowlist:  [Math]::Min.  The
#         allowlist is one entry because static-type invocation is rare
#         here — extending it costs one line, made deliberately by whoever
#         reads the failure message.
#   L3d — inside the dispatch body, forbid AST-opaque constructs:
#         Invoke-Expression / iex, [scriptblock]::Create, [powershell]::Create.
#         Not because they are async (they are not) but because this test's
#         METHOD is AST inspection, and a construct that hides code from the
#         parser invalidates the method itself.
#
# Explicitly NOT covered here (declared, not hidden):
#   * A change to the INTERIOR of an already-allowlisted callee (e.g.
#     Invoke-HeadSkipCommitLaunch backgrounds its own work).  L1 catches it
#     only if the callee uses `&`; otherwise it is outside the dispatch body
#     and outside this pin.  Adding that class needs a separate thread.
#   * A parameter-level assertion (e.g. "Start-Process must carry -Wait").
#     Parameters cannot be verified statically (splatting, conditional
#     assignment), and a check that green-lights a construct it did not
#     actually inspect is worse than one that never looked.
#
# RED / GREEN demonstrations were run locally before commit (see the thread's
# msg-585 revised DoD): L1, L2b, L2c, L3d each went RED under a targeted
# mutation, and a benign body edit (log line + local variable + branch)
# stayed green.  See the PR description for the transcript.

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot 'deploy/run-conductor-scheduled.ps1'
if (-not (Test-Path -LiteralPath $sweepScript)) { throw "sweep script not found: $sweepScript" }

$parseErrors = $null
$tokensOut = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $sweepScript, [ref]$tokensOut, [ref]$parseErrors)
if ($parseErrors) {
    $parseErrors | ForEach-Object { Write-Host "PARSE ERROR line $($_.Extent.StartLineNumber): $($_.Message)" }
    throw 'deploy/run-conductor-scheduled.ps1 does not parse'
}

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) {
        Write-Host ('  PASS  {0}' -f $Name)
    }
    else {
        $script:failures++
        Write-Host ('  FAIL  {0} — expected [{1}], got [{2}]' -f $Name, $Expected, $Actual)
    }
}
function CheckTrue {
    param([string]$Name, [bool]$Actual, [string]$Detail = '')
    if ($Actual) { Write-Host ('  PASS  {0}' -f $Name); return }
    $script:failures++
    Write-Host ('  FAIL  {0}{1}' -f $Name, $(if ($Detail) { " — $Detail" } else { '' }))
}

# Locations used in every failure message so the person who trips a check is
# sent to the prose the pin protects.
$ProtectedProse = @(
    'src/spirrow_mindwire/gate_bootstrap_visibility.py — **Concurrency profile.**'
    '(the Level-3 argument names deploy/run-conductor-scheduled.ps1''s sequential foreach)'
) -join ' '

# =====================================================================================
# L1 — no Background pipeline anywhere in the file
# =====================================================================================
Write-Host 'L1 — no Background pipeline (postfix ``&``) anywhere in the sweep script'
$allPipelines = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.PipelineAst] }, $true)
$bgPipelines = @($allPipelines | Where-Object {
    # .Background exists on PipelineAst in PowerShell 7+. On 5.x it does not; the sweep
    # requires pwsh 7 so pwsh 5 is not a target — but keep the property lookup defensive.
    $_.PSObject.Properties.Name -contains 'Background' -and $_.Background
})
if ($bgPipelines.Count -eq 0) {
    Write-Host '  PASS  no PipelineAst.Background = $true anywhere'
}
else {
    foreach ($p in $bgPipelines) {
        $script:failures++
        Write-Host ("  FAIL  L1 background pipeline at line {0}: [{1}]" -f `
            $p.Extent.StartLineNumber, $p.Extent.Text.Substring(0, [Math]::Min(80, $p.Extent.Text.Length)))
    }
    Write-Host ("        A postfix `&` makes the enclosing pipeline background. This puts the tick " +
                "onto an async path and falsifies " + $ProtectedProse)
}

# =====================================================================================
# Locate the spawn site and the dispatch ForEachStatementAst STRUCTURALLY
# (not by grep — a `&` in a string constant is not a call operator).
# =====================================================================================
$allCommandAsts = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)
$ampCommands = @($allCommandAsts | Where-Object {
    $_.InvocationOperator -eq [System.Management.Automation.Language.TokenKind]::Ampersand
})
# The spawn site: the sole `& $inner` in the file.
$spawnCandidates = @($ampCommands | Where-Object {
    $first = $_.CommandElements[0]
    ($first -is [System.Management.Automation.Language.VariableExpressionAst]) -and
    ($first.VariablePath.UserPath -eq 'inner')
})
if ($spawnCandidates.Count -ne 1) {
    $script:failures++
    Write-Host ("  FAIL  L2a — the spawn is no longer ``& `$inner`` with captured output (found " +
                "$($spawnCandidates.Count) ``& `$inner`` call-operator invocation(s) in the file). " +
                "If this is deliberate: (a) confirm the new form still blocks until the tick exits " +
                "(e.g. `Start-Process -Wait` does NOT set `$LASTEXITCODE / `$output the way this " +
                "wrapper's verdict parse requires — see the block at the spawn site), " +
                "(b) update this assertion (L2a) to describe the new spawn shape, " +
                "(c) re-read $ProtectedProse")
    # Cannot proceed with L2 without a unique spawn site; skip layers that scope to it.
    Write-Host ''
    Write-Host "sweep sequentiality: $($script:failures) check(s) FAILED"
    exit 1
}
$spawnCommand = $spawnCandidates[0]

# Walk up to find the enclosing ForEachStatementAst — that is the "dispatch body"
# every subsequent layer scopes to.
function Get-EnclosingAst {
    param([System.Management.Automation.Language.Ast]$Node, [Type]$Type)
    $n = $Node.Parent
    while ($null -ne $n) {
        if ($Type.IsInstanceOfType($n)) { return $n }
        $n = $n.Parent
    }
    return $null
}

$dispatchLoop = Get-EnclosingAst -Node $spawnCommand `
    -Type ([System.Management.Automation.Language.ForEachStatementAst])
if ($null -eq $dispatchLoop) {
    $script:failures++
    Write-Host ("  FAIL  the spawn site has no enclosing ForEachStatementAst — the dispatch loop " +
                "was removed or reshaped. Protected prose: $ProtectedProse")
}

# =====================================================================================
# L2a — the spawn site, positively identified
# =====================================================================================
Write-Host ''
Write-Host 'L2a — exactly one ``& `$inner`` in the dispatch body, consumed by an assignment'
if ($null -ne $dispatchLoop) {
    $ampInBody = @($ampCommands | Where-Object {
        (Get-EnclosingAst -Node $_ -Type ([System.Management.Automation.Language.ForEachStatementAst])) -eq $dispatchLoop
    })
    Check 'exactly one call-operator invocation in dispatch body' 1 $ampInBody.Count

    # Its command element is $inner (already true for spawnCommand — we found it that way — but
    # this restates the assertion in the form a maintainer reading the test can defend).
    $first = $spawnCommand.CommandElements[0]
    CheckTrue 'spawn command element is $inner' `
        (($first -is [System.Management.Automation.Language.VariableExpressionAst]) -and
         ($first.VariablePath.UserPath -eq 'inner')) `
        "spawn command element is $($first.GetType().Name) [$($first.Extent.Text)]"

    # "Consumed by an assignment": walk ancestors until we see an AssignmentStatementAst.
    $enclosingAssignment = Get-EnclosingAst -Node $spawnCommand `
        -Type ([System.Management.Automation.Language.AssignmentStatementAst])
    if ($null -eq $enclosingAssignment) {
        $script:failures++
        Write-Host ("  FAIL  the spawn is no longer ``& `$inner`` with captured output. " +
                    "If this is deliberate: (a) confirm the new form still blocks until the tick " +
                    "exits, (b) update this assertion (L2a), (c) re-read $ProtectedProse")
    }
    else {
        Write-Host '  PASS  spawn is consumed by an assignment (output captured, call blocks)'
    }
}
else {
    Write-Host '  SKIP  L2a — no dispatch loop identified'
}

# =====================================================================================
# L2b — $inner's assignment binds to `Join-Path $PSScriptRoot 'run-conductor.ps1'`
# =====================================================================================
Write-Host ''
Write-Host 'L2b — $inner is bound to Join-Path $PSScriptRoot ''run-conductor.ps1'''
$allAssignments = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)
$innerAssignments = @($allAssignments | Where-Object {
    $lhs = $_.Left
    # Unwrap `[type]$var = ...` (ConvertExpressionAst around a VariableExpressionAst).
    if ($lhs -is [System.Management.Automation.Language.ConvertExpressionAst]) {
        $lhs = $lhs.Child
    }
    ($lhs -is [System.Management.Automation.Language.VariableExpressionAst]) -and
    ($lhs.VariablePath.UserPath -eq 'inner')
})
Check '$inner has exactly one assignment' 1 $innerAssignments.Count
if ($innerAssignments.Count -ge 1) {
    $rhs = $innerAssignments[0].Right
    # The RHS is a PipelineAst wrapping a single CommandExpression/Command. Reach in.
    $rhsCommand = $null
    if ($rhs -is [System.Management.Automation.Language.PipelineAst] -and
        $rhs.PipelineElements.Count -eq 1) {
        $only = $rhs.PipelineElements[0]
        if ($only -is [System.Management.Automation.Language.CommandAst]) { $rhsCommand = $only }
    }
    if ($null -eq $rhsCommand) {
        $script:failures++
        Write-Host ("  FAIL  L2b — `$inner's RHS is not a single command; got [{0}]. " +
                    "Protected prose: {1}") -f $rhs.Extent.Text, $ProtectedProse
    }
    else {
        $elems = @($rhsCommand.CommandElements)
        $ok =
            ($elems.Count -eq 3) -and
            ($elems[0] -is [System.Management.Automation.Language.StringConstantExpressionAst]) -and
            ($elems[0].Value -eq 'Join-Path') -and
            ($elems[1] -is [System.Management.Automation.Language.VariableExpressionAst]) -and
            ($elems[1].VariablePath.UserPath -eq 'PSScriptRoot') -and
            ($elems[2] -is [System.Management.Automation.Language.StringConstantExpressionAst]) -and
            ($elems[2].Value -eq 'run-conductor.ps1')
        if ($ok) {
            Write-Host '  PASS  $inner = Join-Path $PSScriptRoot ''run-conductor.ps1'''
        }
        else {
            $script:failures++
            $rendered = ($elems | ForEach-Object { $_.Extent.Text }) -join ' | '
            Write-Host ("  FAIL  L2b — `$inner is bound to something else: [$rendered]. " +
                        "If deliberate: (a) confirm the new target still runs the tick synchronously, " +
                        "(b) update this assertion, (c) re-read $ProtectedProse")
        }
    }
}

# =====================================================================================
# L2c — ancestor chain: no Background pipeline; no script-block-as-command-argument
# =====================================================================================
Write-Host ''
Write-Host 'L2c — spawn is not wrapped in a script-block passed as a command argument'
if ($null -ne $dispatchLoop) {
    $badAncestors = New-Object System.Collections.Generic.List[string]
    $n = $spawnCommand.Parent
    $reachedDispatchLoop = $false
    while ($null -ne $n -and -not $reachedDispatchLoop) {
        if ($n -eq $dispatchLoop) { $reachedDispatchLoop = $true; break }
        # Background pipeline on any ancestor (redundant with L1's file-wide sweep, but the
        # message here is scoped to the spawn and points the reader at the right line).
        if ($n -is [System.Management.Automation.Language.PipelineAst] -and
            $n.PSObject.Properties.Name -contains 'Background' -and $n.Background) {
            $badAncestors.Add("Background PipelineAst at line $($n.Extent.StartLineNumber)")
        }
        # SHAPE of `Start-Job { … }`, `ForEach-Object -Parallel { … }`,
        # `[Task]::Run({ … })`, `Invoke-Command -AsJob { … }` — anything whose spawn is
        # wrapped in a script-block literal handed to a callee.  If the spawn is inside a
        # ScriptBlockExpressionAst that is a command argument, that is the wrap.
        if ($n -is [System.Management.Automation.Language.ScriptBlockExpressionAst]) {
            $p = $n.Parent
            if ($p -is [System.Management.Automation.Language.CommandAst] -or
                $p -is [System.Management.Automation.Language.CommandExpressionAst] -or
                $p -is [System.Management.Automation.Language.InvokeMemberExpressionAst]) {
                $badAncestors.Add(
                    "spawn is inside a ScriptBlockExpressionAst passed to $($p.GetType().Name) " +
                    "at line $($p.Extent.StartLineNumber): [$($p.Extent.Text.Substring(0, [Math]::Min(80, $p.Extent.Text.Length)))]")
            }
        }
        $n = $n.Parent
    }
    if (-not $reachedDispatchLoop) {
        $script:failures++
        Write-Host ("  FAIL  L2c — walking up from the spawn did not reach the dispatch " +
                    "ForEachStatementAst before running out of ancestors. Protected prose: $ProtectedProse")
    }
    if ($badAncestors.Count -eq 0) {
        Write-Host '  PASS  no Background pipeline / script-block-as-command-argument between spawn and dispatch loop'
    }
    else {
        foreach ($msg in $badAncestors) {
            $script:failures++
            Write-Host "  FAIL  L2c ancestor: $msg"
        }
        Write-Host ("        The spawn has been wrapped in an async form (Start-Job { … }, " +
                    "ForEach-Object -Parallel { … }, [Task]::Run({ … }), &-postfix, …). This " +
                    "falsifies $ProtectedProse")
    }
}
else {
    Write-Host '  SKIP  L2c — no dispatch loop identified'
}

# =====================================================================================
# L3b — static-type invocations in the dispatch body are on a one-entry allowlist
# =====================================================================================
Write-Host ''
Write-Host 'L3b — static-type invocations in the dispatch body allowlisted (one entry)'
if ($null -ne $dispatchLoop) {
    $staticAllowlist = @(
        'Math.Min'
    )
    $memberInvocations = $ast.FindAll(
        { param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)
    $bodyStatic = @($memberInvocations | Where-Object {
        $isStatic = $_.Static -and ($_.Expression -is [System.Management.Automation.Language.TypeExpressionAst])
        $encl = Get-EnclosingAst -Node $_ -Type ([System.Management.Automation.Language.ForEachStatementAst])
        $isStatic -and ($encl -eq $dispatchLoop)
    })
    $seen = @()
    foreach ($m in $bodyStatic) {
        $typeName = $m.Expression.TypeName.FullName
        # Normalise `System.Math` and `Math` both to `Math`.
        $short = $typeName.Split('.')[-1]
        $memberName = if ($m.Member -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
            $m.Member.Value
        }
        else { "$($m.Member.Extent.Text)" }
        $key = "$short.$memberName"
        $seen += $key
        if ($staticAllowlist -notcontains $key) {
            $script:failures++
            Write-Host ("  FAIL  L3b — [$typeName]::$memberName at line $($m.Extent.StartLineNumber) " +
                        "is not on the allowlist ($($staticAllowlist -join ', ')). " +
                        "If this call is genuinely synchronous, add its short key ('$key') to the " +
                        "allowlist deliberately and re-read $ProtectedProse. If it is `[Task]::Run` " +
                        "or any other async entry point, do NOT add it.")
        }
    }
    if ($script:failures -eq 0 -or $bodyStatic.Count -eq $seen.Count -and ($seen | Where-Object { $staticAllowlist -notcontains $_ }).Count -eq 0) {
        Write-Host ("  PASS  {0} static-type invocation(s) in dispatch body, all allowlisted" -f $bodyStatic.Count)
    }
}
else {
    Write-Host '  SKIP  L3b — no dispatch loop identified'
}

# =====================================================================================
# L3d — AST-opaque constructs are forbidden in the dispatch body
# =====================================================================================
Write-Host ''
Write-Host 'L3d — no AST-opaque constructs (Invoke-Expression / [scriptblock]::Create / [powershell]::Create) in dispatch body'
if ($null -ne $dispatchLoop) {
    $bodyCommands = @($allCommandAsts | Where-Object {
        (Get-EnclosingAst -Node $_ -Type ([System.Management.Automation.Language.ForEachStatementAst])) -eq $dispatchLoop
    })
    $bannedCommandNames = @('Invoke-Expression', 'iex')
    $opaqueHits = New-Object System.Collections.Generic.List[string]
    foreach ($c in $bodyCommands) {
        $name = if ($c.CommandElements.Count -ge 1) { $c.GetCommandName() } else { $null }
        if ($null -ne $name -and $bannedCommandNames -contains $name) {
            $opaqueHits.Add("$name at line $($c.Extent.StartLineNumber)")
        }
    }
    # [scriptblock]::Create, [powershell]::Create — a static call whose Member is 'Create'
    # on either type name. Search the whole file once; we already have the InvokeMember list.
    $memberInvocations = $ast.FindAll(
        { param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)
    foreach ($m in $memberInvocations) {
        if (-not ($m.Static -and ($m.Expression -is [System.Management.Automation.Language.TypeExpressionAst]))) { continue }
        $encl = Get-EnclosingAst -Node $m -Type ([System.Management.Automation.Language.ForEachStatementAst])
        if ($encl -ne $dispatchLoop) { continue }
        $typeName = $m.Expression.TypeName.FullName
        $short = $typeName.Split('.')[-1]
        $memberName = if ($m.Member -is [System.Management.Automation.Language.StringConstantExpressionAst]) { $m.Member.Value } else { "$($m.Member.Extent.Text)" }
        if ((($short -eq 'ScriptBlock') -or ($short -eq 'PowerShell')) -and ($memberName -eq 'Create')) {
            $opaqueHits.Add("[$typeName]::$memberName at line $($m.Extent.StartLineNumber)")
        }
    }
    if ($opaqueHits.Count -eq 0) {
        Write-Host '  PASS  no AST-opaque constructs in dispatch body'
    }
    else {
        foreach ($h in $opaqueHits) {
            $script:failures++
            Write-Host "  FAIL  L3d — $h"
        }
        Write-Host ("        AST-opaque constructs hide code from the parser this test relies on. " +
                    "Remove them from the dispatch body, or (if the runtime string is genuinely " +
                    "synchronous and cannot be expressed statically) split the change into a " +
                    "separate PR that carries a hand-written justification. Protected prose: $ProtectedProse")
    }
}
else {
    Write-Host '  SKIP  L3d — no dispatch loop identified'
}

Write-Host ''
if ($script:failures -gt 0) {
    Write-Host "sweep sequentiality: $($script:failures) check(s) FAILED"
    exit 1
}
Write-Host 'sweep sequentiality: all checks passed'
exit 0
