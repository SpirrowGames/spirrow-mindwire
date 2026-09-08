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
# Design (msg-585 / msg-587 / msg-596 / msg-599):
#
#   L1  — no PipelineAst in the file has .Background -eq $true.  This closes
#         the postfix `&` async form STRUCTURALLY, without naming any cmdlet.
#         `Start-Job { … }`, `Invoke-Command -AsJob { … }`, PowerShell 7's
#         `& { … } &` all reduce to a Background pipeline; nothing that gets
#         it wrong appears at the source.
#
#   Dispatch loop identity (S1-S4, msg-599):
#     S1  file-wide census of `& $inner` CommandAsts. Assert exactly 1.
#     S2  file-wide census of ForEachStatementAsts whose iteration expression
#         is a bare VariableExpressionAst named 'candidates'.
#     S3  the dispatch loop = { L in S2 | Test-IsDescendant -Node S1 -Ancestor L }.
#         Assert exactly 1. That is $dispatchLoop.
#     S4  fail-closed on both edges (zero, or more than one) with distinct
#         messages. Never fall back to "pick nearest".
#
#     Depth heuristics ("nearest", "outermost") are deliberately not used —
#     an attacker chooses the tree. Identity + containment is a predicate on
#     (loop, spawn), not on the loop's position among other loops.
#
#   L2a — the dispatch body contains exactly the S1 spawn: every `Ampersand`
#         CommandAst that is a descendant of $dispatchLoop is the S1 node,
#         and no other call-operator invocations live in the body. Also, the
#         spawn's result is consumed by an assignment (output captured, call
#         blocks).
#   L2b — $inner's assignment (anywhere in the script) has RHS
#         `Join-Path $PSScriptRoot 'run-conductor.ps1'`.  Without this,
#         L2a is trivially defeated by repointing $inner at a job wrapper
#         and changing nothing at the call site.
#   L2c — walking from the spawn site's CommandAst up the .Parent chain
#         asserts the RESHAPING boundary only: the walk sees the existing
#         spawn's ancestors and nothing else in the body. It catches
#         parallelization that MOVES `& $inner` into a script block; it
#         does NOT catch DUPLICATION (a launch path added BESIDE the
#         pinned spawn), and nothing else in this suite does either. See
#         the "Spawn DUPLICATION" bullet in "Explicitly NOT covered here"
#         for the design record (options considered, why each was refused,
#         measured cost of the extraction option).
#           * the walk reaches $dispatchLoop (not stopped by anything else),
#           * no ancestor PipelineAst has .Background,
#           * no ancestor is a ScriptBlockExpressionAst passed as a command
#             argument. The shapes `Start-Job { … }`, `ForEach-Object
#             -Parallel { … }`, `[Task]::Run({ … })`, etc. are caught by
#             this ONLY when the mutation reshapes the existing spawn into
#             one of them; the same shapes added as a SECOND launch path
#             beside `& $inner` are NOT reached by this walk.
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
#   * Spawn DUPLICATION — a launch path added BESIDE the pinned spawn
#     (e.g. keeping the sync path and adding `$jobs += Start-Job -FilePath
#     $inner` on a branch). L2c walks the ancestor chain of the EXISTING
#     spawn, so it catches parallelization that MOVES `& $inner` into a
#     script block, and does not see a second launch path adjacent to it.
#     Thread: T-sweep-pin-blind-to-launch-paths-added-beside-the-spawn.
#
#     Four closures were evaluated. The reason each was refused lives
#     here so a re-open does not pay the measurement twice:
#       (a) body-scoped `$inner`-occurrence check — WITHDRAWN, dies to a
#           recompute bypass that does not name the variable:
#           `Start-Job -FilePath (Join-Path $PSScriptRoot
#           'run-conductor-once.ps1')` has zero occurrences of `$inner`.
#       (b) denylist of async command names — REFUSED as structurally
#           leaky ([System.Threading.Tasks.Task]::Run, [runspacefactory],
#           `& $inner &` in PS7, and any wrapper defined elsewhere all
#           miss the list; a denylist that fails-closed on unknowns is a
#           category error).
#       (c) extract the dispatch body into a small named function and
#           allowlist it — REFUSED on measured T1 failure. The dispatch
#           foreach body at deploy/run-conductor-scheduled.ps1:3035-3287
#           measured 37 top-level statements (14 AssignmentStatementAst
#           + 14 IfStatementAst + 9 PipelineAst) against a pre-declared
#           threshold of ≤25, and ≥24 read-only captured names plus ≥10
#           mutated-inherited names (three flag/accumulator writes and
#           seven `++` counters, each needing [ref] or $script:
#           threading) against a threshold of ≤3. Thresholds were pinned
#           in advance (msg-615) so the reading could not be reshaped by
#           the result. Resolution method: identity walk (S1→S2→S3, same
#           as this file's dispatch-loop derivation) — not a depth
#           heuristic — so the number reproduces from the pin's own
#           logic without a separate script. Spawn-only extraction
#           ("extract just the `& $inner` line into Invoke-Inner") is
#           the same option in disguise: guarding a small named function
#           reduces to "assert this function is called once", which is
#           (a) with a function name substituted for the variable and
#           dies to the identical recompute bypass.
#       (d) accept and declare — SELECTED. This bullet IS (d). The hole
#           is documented, its shape is named, and the measured cost of
#           closing it is recorded so a future engineer who wants to
#           re-open the extraction option knows what they are paying
#           before they start.
#   * Renaming the candidate collection from `$candidates` to something
#     else (or changing the iteration to a pipeline expression) will trip
#     S2's zero-match assertion. This is a deliberate declared cost of
#     strict identity: a tolerant matcher (e.g. "the pipeline MENTIONS
#     $candidates") would green-light a construct it did not identify,
#     which is the false-positive class msg-582/msg-585 removed.
#
# RED / GREEN demonstrations were run locally before commit; see the PR
# description for the transcript, and thread msg-599 items 3(a)-(f).

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

# Parameterized descendancy walk — one helper, used both to derive
# $dispatchLoop (S3, ancestor = candidate loop) and to scope the body-level
# filters (L2a / L3b / L3d, ancestor = the resolved $dispatchLoop).
# Einstein msg (naysayer round after msg-599): S3 defines $dispatchLoop, so
# the helper cannot close over it; it must take the ancestor as a parameter.
function Test-IsDescendant {
    param(
        [System.Management.Automation.Language.Ast]$Node,
        [System.Management.Automation.Language.Ast]$Ancestor
    )
    if ($null -eq $Node -or $null -eq $Ancestor) { return $false }
    $n = $Node.Parent
    while ($null -ne $n) {
        if ($n -eq $Ancestor) { return $true }
        $n = $n.Parent
    }
    return $false
}

# Kept for the ancestor-chain walk in L2c (which needs to inspect every
# intermediate node's type, not just answer a yes/no descendancy question).
# `-Boundary` (optional): if the walk reaches this node before finding the
# requested type, return $null instead of walking further up. Used at the
# "spawn consumed by an assignment" check to keep the walk inside
# $dispatchLoop — an unbounded walk would false-positive if a maintainer
# wraps the whole dispatch loop in `$dummy = foreach ... { & $inner | Out-Null }`
# (PR-gate advisory #2 on round-2, addressed here).
function Get-EnclosingAst {
    param(
        [System.Management.Automation.Language.Ast]$Node,
        [Type]$Type,
        [System.Management.Automation.Language.Ast]$Boundary = $null
    )
    $n = $Node.Parent
    while ($null -ne $n) {
        if ($null -ne $Boundary -and $n -eq $Boundary) { return $null }
        if ($Type.IsInstanceOfType($n)) { return $n }
        $n = $n.Parent
    }
    return $null
}

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
# S1-S4 — derive $dispatchLoop by IDENTITY + CONTAINMENT, not by depth heuristics
# =====================================================================================
Write-Host ''
Write-Host 'S1 — file-wide census of ``& `$inner`` CommandAsts'
$allCommandAsts = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)
$ampCommands = @($allCommandAsts | Where-Object {
    $_.InvocationOperator -eq [System.Management.Automation.Language.TokenKind]::Ampersand
})
# S1: the sole `& $inner` in the file.
$s1Candidates = @($ampCommands | Where-Object {
    $first = $_.CommandElements[0]
    ($first -is [System.Management.Automation.Language.VariableExpressionAst]) -and
    ($first.VariablePath.UserPath -eq 'inner')
})
Write-Host ("  census: {0} ``& `$inner`` invocation(s) file-wide" -f $s1Candidates.Count)
if ($s1Candidates.Count -ne 1) {
    $script:failures++
    Write-Host ("  FAIL  S1 — expected exactly 1 ``& `$inner`` call-operator invocation, got $($s1Candidates.Count). " +
                "If this is deliberate: (a) confirm the new form still blocks until the tick exits " +
                "(e.g. ``Start-Process -Wait`` does NOT set `$LASTEXITCODE / `$output the way this " +
                "wrapper's verdict parse requires — see the block at the spawn site), " +
                "(b) update S1 (and L2a) to describe the new spawn shape, " +
                "(c) re-read $ProtectedProse")
    Write-Host ''
    Write-Host "sweep sequentiality: $($script:failures) check(s) FAILED"
    exit 1
}
$spawnCommand = $s1Candidates[0]
Write-Host ("  PASS  exactly one ``& `$inner`` at line {0}" -f $spawnCommand.Extent.StartLineNumber)

Write-Host ''
Write-Host 'S2 — file-wide census of ForEachStatementAsts iterating a bare $candidates'
$allForEach = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.ForEachStatementAst] }, $true)
$s2Loops = @($allForEach | Where-Object {
    $expr = $_.Condition
    # PipelineAst wrapping a single CommandExpressionAst wrapping a bare VariableExpressionAst
    # named 'candidates'. Strict identity: pipelines, sorts, .Where{} etc. are all rejected —
    # the declared cost documented in the header, not softened here.
    if (-not ($expr -is [System.Management.Automation.Language.PipelineAst])) { return $false }
    if ($expr.PipelineElements.Count -ne 1) { return $false }
    $only = $expr.PipelineElements[0]
    if (-not ($only -is [System.Management.Automation.Language.CommandExpressionAst])) { return $false }
    $inner = $only.Expression
    if (-not ($inner -is [System.Management.Automation.Language.VariableExpressionAst])) { return $false }
    $inner.VariablePath.UserPath -eq 'candidates'
})
Write-Host ("  census: {0} loop(s) iterate a bare `$candidates" -f $s2Loops.Count)
foreach ($l in $s2Loops) {
    Write-Host ("           line {0}: [{1}]" -f $l.Extent.StartLineNumber, `
        $l.Extent.Text.Substring(0, [Math]::Min(80, $l.Extent.Text.Length)))
}
if ($s2Loops.Count -eq 0) {
    $script:failures++
    Write-Host ("  FAIL  S2 — no ForEachStatementAst iterates a bare ``$candidates``. If the " +
                "collection has been renamed or the iteration made non-trivial " +
                "(e.g. `$candidates | Sort-Object), this is intentionally fail-closed: (a) confirm " +
                "the new dispatch shape is still sequential, (b) rebind S2's iteration-expression " +
                "predicate to name the new collection or expression, (c) re-read $ProtectedProse.")
    Write-Host ''
    Write-Host "sweep sequentiality: $($script:failures) check(s) FAILED"
    exit 1
}

Write-Host ''
Write-Host 'S3 — dispatchLoop = { L in S2 | L contains the S1 spawn }'
$s3Loops = @($s2Loops | Where-Object { Test-IsDescendant -Node $spawnCommand -Ancestor $_ })
Write-Host ("  census: {0} candidate loop(s) contain the S1 spawn" -f $s3Loops.Count)
if ($s3Loops.Count -eq 0) {
    $script:failures++
    Write-Host ("  FAIL  S3 — no ``foreach (`$x in `$candidates)`` contains the ``& `$inner`` spawn. " +
                "The spawn has been moved out of every candidate-dispatch loop. " +
                "Protected prose: $ProtectedProse")
    Write-Host ''
    Write-Host "sweep sequentiality: $($script:failures) check(s) FAILED"
    exit 1
}
if ($s3Loops.Count -gt 1) {
    $script:failures++
    $lines = ($s3Loops | ForEach-Object { $_.Extent.StartLineNumber }) -join ', '
    Write-Host ("  FAIL  S4 — more than one ``foreach (`$x in `$candidates)`` contains the S1 spawn " +
                "(lines: $lines). The dispatch identity is ambiguous, which usually means the spawn " +
                "has been wrapped in a NESTED candidate-loop — that is exactly the shape S4 " +
                "fail-closes on. Protected prose: $ProtectedProse")
    Write-Host ''
    Write-Host "sweep sequentiality: $($script:failures) check(s) FAILED"
    exit 1
}
$dispatchLoop = $s3Loops[0]
Write-Host ("  PASS  dispatchLoop identified at line {0}" -f $dispatchLoop.Extent.StartLineNumber)

# =====================================================================================
# L2a — every ampersand descendant of $dispatchLoop IS the S1 spawn
#       (restated from msg-585's count/id form; msg-599 topology change:
#        the derivation now runs spawn → loop, so L2a describes what the
#        loop must contain rather than counting inside a pre-known body).
# =====================================================================================
Write-Host ''
Write-Host 'L2a — every ampersand-CommandAst in the dispatch body IS the S1 spawn'
$ampInBody = @($ampCommands | Where-Object { Test-IsDescendant -Node $_ -Ancestor $dispatchLoop })
Check 'exactly one call-operator invocation in dispatch body' 1 $ampInBody.Count
foreach ($a in $ampInBody) {
    if ($a -ne $spawnCommand) {
        $script:failures++
        Write-Host ("  FAIL  L2a — an ampersand invocation other than the pinned spawn appears in the " +
                    "dispatch body at line $($a.Extent.StartLineNumber): [$($a.Extent.Text.Substring(0, [Math]::Min(80, $a.Extent.Text.Length)))]. " +
                    "If this is a second synchronous invocation, add it to the pin deliberately. " +
                    "Protected prose: $ProtectedProse")
    }
}

# Positive identity checks on the S1 node itself (retained from msg-585).
$first = $spawnCommand.CommandElements[0]
CheckTrue 'spawn command element is $inner' `
    (($first -is [System.Management.Automation.Language.VariableExpressionAst]) -and
     ($first.VariablePath.UserPath -eq 'inner')) `
    "spawn command element is $($first.GetType().Name) [$($first.Extent.Text)]"

# "Consumed by an assignment": walk ancestors until we see an AssignmentStatementAst.
# Bounded at $dispatchLoop so an assignment outside the loop (e.g. someone wrapping the
# whole loop with `$dummy = foreach ... { & $inner | Out-Null }` and dropping the spawn's
# own assignment) does not falsely satisfy this check.
$enclosingAssignment = Get-EnclosingAst -Node $spawnCommand `
    -Type ([System.Management.Automation.Language.AssignmentStatementAst]) `
    -Boundary $dispatchLoop
if ($null -eq $enclosingAssignment) {
    $script:failures++
    Write-Host ("  FAIL  the spawn is no longer ``& `$inner`` with captured output. " +
                "If this is deliberate: (a) confirm the new form still blocks until the tick " +
                "exits, (b) update this assertion (L2a), (c) re-read $ProtectedProse")
}
else {
    Write-Host '  PASS  spawn is consumed by an assignment (output captured, call blocks)'
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
    # wrapped in a script-block literal that will be evaluated later.
    #
    # ANY ScriptBlockExpressionAst in the ancestor chain implies the spawn is a
    # deferred expression literal. Normal synchronous control flow (bodies of
    # foreach/if/while/try/functions) parses as ScriptBlockAst / StatementBlockAst,
    # NOT ScriptBlockExpressionAst. A parent-type filter (CommandAst /
    # InvokeMemberExpressionAst / …) creates a bypass when the script-block literal
    # is wrapped in an intermediate expression — `({ … })`, `@({ … })`, or
    # `[scriptblock]({ … })` — because the immediate parent becomes a
    # ParenExpressionAst / ArrayLiteralAst / ConvertExpressionAst. Detecting the
    # ScriptBlockExpressionAst directly, with no parent filter, closes that class.
    # (PR-gate blocking #1 on round-2, addressed here.)
    if ($n -is [System.Management.Automation.Language.ScriptBlockExpressionAst]) {
        $badAncestors.Add(
            "spawn is inside a ScriptBlockExpressionAst at line $($n.Extent.StartLineNumber): " +
            "[$($n.Extent.Text.Substring(0, [Math]::Min(80, $n.Extent.Text.Length)))] " +
            "(sync control-flow bodies parse as ScriptBlockAst/StatementBlockAst, not " +
            "ScriptBlockExpressionAst — a ScriptBlockExpressionAst on the chain means the " +
            "spawn is a deferred script-block literal)")
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

# =====================================================================================
# L3b — static-type invocations in the dispatch body are on a one-entry allowlist
# =====================================================================================
Write-Host ''
Write-Host 'L3b — static-type invocations in the dispatch body allowlisted (one entry)'
$staticAllowlist = @(
    'Math.Min'
)
$memberInvocations = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)
$bodyStatic = @($memberInvocations | Where-Object {
    $isStatic = $_.Static -and ($_.Expression -is [System.Management.Automation.Language.TypeExpressionAst])
    $isStatic -and (Test-IsDescendant -Node $_ -Ancestor $dispatchLoop)
})
$disallowed = 0
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
        $disallowed++
        Write-Host ("  FAIL  L3b — [$typeName]::$memberName at line $($m.Extent.StartLineNumber) " +
                    "is not on the allowlist ($($staticAllowlist -join ', ')). " +
                    "If this call is genuinely synchronous, add its short key ('$key') to the " +
                    "allowlist deliberately and re-read $ProtectedProse. If it is ``[Task]::Run`` " +
                    "or any other async entry point, do NOT add it.")
    }
}
if ($disallowed -eq 0) {
    Write-Host ("  PASS  {0} static-type invocation(s) in dispatch body, all allowlisted" -f $bodyStatic.Count)
}

# =====================================================================================
# L3d — AST-opaque constructs are forbidden in the dispatch body
# =====================================================================================
Write-Host ''
Write-Host 'L3d — no AST-opaque constructs (Invoke-Expression / [scriptblock]::Create / [powershell]::Create) in dispatch body'
$bodyCommands = @($allCommandAsts | Where-Object { Test-IsDescendant -Node $_ -Ancestor $dispatchLoop })
$bannedCommandNames = @('Invoke-Expression', 'iex')
$opaqueHits = New-Object System.Collections.Generic.List[string]
foreach ($c in $bodyCommands) {
    $name = if ($c.CommandElements.Count -ge 1) { $c.GetCommandName() } else { $null }
    if ($null -ne $name -and $bannedCommandNames -contains $name) {
        $opaqueHits.Add("$name at line $($c.Extent.StartLineNumber)")
    }
}
# [scriptblock]::Create, [powershell]::Create — a static call whose Member is 'Create'
# on either type name. Reuse $memberInvocations from L3b's scan.
foreach ($m in $memberInvocations) {
    if (-not ($m.Static -and ($m.Expression -is [System.Management.Automation.Language.TypeExpressionAst]))) { continue }
    if (-not (Test-IsDescendant -Node $m -Ancestor $dispatchLoop)) { continue }
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

Write-Host ''
if ($script:failures -gt 0) {
    Write-Host "sweep sequentiality: $($script:failures) check(s) FAILED"
    exit 1
}
Write-Host 'sweep sequentiality: all checks passed'
exit 0
