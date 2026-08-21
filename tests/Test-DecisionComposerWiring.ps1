# Regression guard for the T-decision-request-composer S2 wiring in
# deploy/run-conductor-scheduled.ps1. Same lift-from-AST pattern as
# Test-SweepHeadCache.ps1 (dot-sourcing would launch the sweep).
#
# Coverage:
#   * I-3 (A-3) — Get-DecisionEnvelope invokes the CLI exactly once per (Key, Signature).
#                 The second call with the same signature reuses the cached envelope, and no
#                 shell-out fires. Different signatures re-invoke.
#   * I-2 (A-2) — a broken composer (Invoke-ComposerCli returns { ok=$false }) leaves the cache
#                 untouched AND makes Format-DecisionMessage return $null, so the caller uses the
#                 raw ping. The wrapper's own call site substitutes RawFallback when the enriched
#                 message is $null — the branch below verifies that shape.
#   * Format-DecisionMessage — the D-9 truncation ladder drops slices in reverse priority order,
#                 the D-29 dashboard link is never dropped (or the function bails out entirely to
#                 the raw ping), and the header is emitted with escaped project + thread id.
#   * New-ComposerInputJson — round-trips through ConvertFrom-Json with the CLI's expected shape,
#                 including schema_version = 1, tail_requested and total_messages.

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot 'deploy/run-conductor-scheduled.ps1'
if (-not (Test-Path -LiteralPath $sweepScript)) { throw "sweep script not found: $sweepScript" }

$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) {
    $parseErrors | ForEach-Object { Write-Host "PARSE ERROR line $($_.Extent.StartLineNumber): $($_.Message)" }
    throw 'deploy/run-conductor-scheduled.ps1 does not parse'
}

# Silence the wrapper's own Write-Log so the test output stays readable.
function Write-Log { param([string]$Message) }

# Bring in the exact functions the sweep uses. Dot-sourcing would launch the sweep; parsing the
# AST and invoking just the function definitions keeps this test hermetic.
$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
$needed = @(
    'Get-CachedDecision',
    'Get-DecisionEnvelope',
    'Format-DecisionMessage',
    'New-ComposerInputJson',
    'Get-HumanParkedCandidates',
    'New-DailyDigest',
    'Format-DurationDigest',
    'Get-DerivedQuarantineState',
    'Get-FingerprintHint',
    'ConvertTo-UtcInstant',
    'Get-StarvedKeys',
    'Get-JsonState',
    'Save-JsonState'
)
foreach ($name in $needed) {
    $fn = $functions | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}

# The wrapper's Invoke-ComposerCli is what we override for A-2 / A-3 without ever running uv.
# The counter records how many times the composer was actually shelled out to.
$script:composerCallCount = 0
$script:composerReturn = $null
function Invoke-ComposerCli {
    param([string]$InputJson, [string]$Backend, [string]$Identity, [int]$TimeoutSeconds)
    $script:composerCallCount++
    return $script:composerReturn
}

# The wrapper's globals the functions read. Only the ones the tested functions actually touch —
# a full sweep would need the whole world, but these three suffice here.
$script:DecisionComposerBackend = 'stub'
$script:DecisionComposerIdentity = 'Composer'
$script:DecisionComposerTimeoutSeconds = 5
$script:DecisionComposerTailLimit = 5
$script:DecisionMessageDiscordBudget = 1950
$script:DecisionDashboardBaseUrl = 'https://example.invalid'
# Thresholds New-DailyDigest reaches through the script scope.
$script:StarvedThreshold = [TimeSpan]::FromHours(24)
$script:QuarantineEscalatedAfter = [TimeSpan]::FromHours(24)
$script:QuarantineStaleAfter = [TimeSpan]::FromDays(7)

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

# --- New-ComposerInputJson ---------------------------------------------------------------------
Write-Host 'New-ComposerInputJson — round-trips through ConvertFrom-Json with the CLI shape'
$sampleJson = New-ComposerInputJson `
    -Project 'p1' -ThreadId 'T-a' -LastMsgId 'msg-1' `
    -StopReason 'human' -Rounds 3 -ThreadTitle 'title' `
    -TailRequested 5 -TotalMessages 45 -Tail @()
$sample = $sampleJson | ConvertFrom-Json
Check 'schema_version' 1 $sample.schema_version
Check 'project' 'p1' $sample.project
Check 'thread_id' 'T-a' $sample.thread_id
Check 'last_msg_id' 'msg-1' $sample.last_msg_id
Check 'stop_reason' 'human' $sample.stop_reason
Check 'rounds' 3 $sample.rounds
Check 'tail_requested' 5 $sample.tail_requested
Check 'total_messages' 45 $sample.total_messages
CheckTrue 'tail is an array' ($sample.tail -is [array] -or $null -eq $sample.tail)

# --- I-3 / A-3: composer runs at most once per signature ---------------------------------------
Write-Host ''
Write-Host 'Get-DecisionEnvelope — same signature reuses the cache, no second CLI call (A-3 / I-3)'
$state = @{}
$script:composerCallCount = 0
$fakeOkEnvelope = [PSCustomObject]@{
    composer_status = 'ok'
    signature       = 'human:msg-1'
    output          = [PSCustomObject]@{
        question       = 'This is the question?'
        options        = @(
            [PSCustomObject]@{ id = 'A'; label = 'first'; gain = 'gg'; loss = 'll' }
        )
        recommendation = 'A'
        recommendation_reason = 'because'
        unknowns       = @('unk-1')
    }
}
$script:composerReturn = @{ ok = $true; envelope = $fakeOkEnvelope; error = $null }

$first = Get-DecisionEnvelope -State $state -Key 'p/T-a' -Project 'p' -ThreadId 'T-a' `
    -Signature 'human:msg-1' -LastMsgId 'msg-1' -StopReason 'human' -Rounds 3
$second = Get-DecisionEnvelope -State $state -Key 'p/T-a' -Project 'p' -ThreadId 'T-a' `
    -Signature 'human:msg-1' -LastMsgId 'msg-1' -StopReason 'human' -Rounds 3
Check 'exactly one CLI invocation across two same-signature reads' 1 $script:composerCallCount
CheckTrue 'first returns the composer envelope' ($null -ne $first) 'null returned on first call'
CheckTrue 'second returns the cached envelope' ($null -ne $second) 'null returned on second call'
CheckTrue 'row is written to state' ($state.ContainsKey('p/T-a')) 'no cache row was written'
Check 'row signature matches the composer input' 'human:msg-1' $state['p/T-a'].signature

Write-Host 'Get-DecisionEnvelope — a different signature RE-INVOKES the composer'
$script:composerCallCount = 0
$fakeOkEnvelope2 = [PSCustomObject]@{
    composer_status = 'ok'
    signature       = 'human:msg-2'
    output          = [PSCustomObject]@{
        question              = 'New question?'
        options               = @()
        recommendation        = $null
        recommendation_reason = $null
        unknowns              = @()
    }
}
$script:composerReturn = @{ ok = $true; envelope = $fakeOkEnvelope2; error = $null }
$third = Get-DecisionEnvelope -State $state -Key 'p/T-a' -Project 'p' -ThreadId 'T-a' `
    -Signature 'human:msg-2' -LastMsgId 'msg-2' -StopReason 'human' -Rounds 4
Check 'one CLI invocation on a fresh signature' 1 $script:composerCallCount
Check 'cache advanced to new signature' 'human:msg-2' $state['p/T-a'].signature
CheckTrue 'third returns the new envelope' ($null -ne $third) 'null returned on new-signature call'

# --- I-2 / A-2: broken composer does NOT poison the cache, and Format returns null -------------
Write-Host ''
Write-Host 'Get-DecisionEnvelope — a failed composer returns $null and does NOT cache (I-2)'
$state = @{}
$script:composerCallCount = 0
$script:composerReturn = @{ ok = $false; envelope = $null; error = 'simulated failure' }
$fail = Get-DecisionEnvelope -State $state -Key 'p/T-a' -Project 'p' -ThreadId 'T-a' `
    -Signature 'human:msg-1' -LastMsgId 'msg-1' -StopReason 'human' -Rounds 3
Check 'CLI was called' 1 $script:composerCallCount
CheckTrue 'null envelope on failure' ($null -eq $fail) 'a non-null envelope leaked out of a failed composer'
CheckTrue 'no cache row on failure' (-not $state.ContainsKey('p/T-a')) 'the cache was polluted with a failed row'

Write-Host 'Format-DecisionMessage — a null envelope returns $null so the caller falls back to raw ping'
$formatted = Format-DecisionMessage -Project 'p' -ThreadId 'T-a' `
    -StopReason 'human' -Rounds 3 -LastMsgId 'msg-1' `
    -RawFallback 'raw ping' -Envelope $null
CheckTrue 'null envelope -> null message' ($null -eq $formatted)

Write-Host 'Format-DecisionMessage — a non-ok envelope returns $null too (I-2 at the format layer)'
$errorEnvelope = [PSCustomObject]@{ composer_status = 'error'; output = $null; error = 'CLI timeout' }
$formatted = Format-DecisionMessage -Project 'p' -ThreadId 'T-a' `
    -StopReason 'human' -Rounds 3 -LastMsgId 'msg-1' `
    -RawFallback 'raw ping' -Envelope $errorEnvelope
CheckTrue 'non-ok envelope -> null message' ($null -eq $formatted)

# --- Format-DecisionMessage: link + header + question ------------------------------------------
Write-Host ''
Write-Host 'Format-DecisionMessage — header, dashboard link, question, options all render'
$rendered = Format-DecisionMessage -Project 'p1' -ThreadId 'T-slope' `
    -StopReason 'human' -Rounds 3 -LastMsgId 'msg-999' `
    -RawFallback 'raw ping' -Envelope $fakeOkEnvelope
CheckTrue 'header names the thread' ($rendered -match 'T-slope') $rendered
CheckTrue 'header names the project' ($rendered -match 'p1') $rendered
CheckTrue 'dashboard link is present (D-29)' ($rendered -match 'https://example.invalid/dashboard/decisions/p1/T-slope') $rendered
CheckTrue 'question is present' ($rendered -match 'This is the question') $rendered
CheckTrue 'option label renders' ($rendered -match 'A: first') $rendered
CheckTrue 'gain / loss render' (($rendered -match 'A 得') -and ($rendered -match 'A 失')) $rendered
CheckTrue 'recommendation renders' ($rendered -match '推奨: A') $rendered
CheckTrue 'unknowns render' ($rendered -match 'unk-1') $rendered
CheckTrue 'no tail marker on the roomy path' (-not ($rendered -match '詳細は chatroom')) 'the ladder dropped a slice unnecessarily'

# --- Format-DecisionMessage: truncation ladder --------------------------------------------------
Write-Host ''
Write-Host 'Format-DecisionMessage — truncation ladder drops slices in reverse priority order'
# Build an envelope where every slice is 400 chars, forcing the ladder to drop from the tail (D-9).
$bigChunk = 'x' * 400
$bulkyEnvelope = [PSCustomObject]@{
    composer_status = 'ok'
    output          = [PSCustomObject]@{
        question       = "Q $bigChunk"
        options        = @(
            [PSCustomObject]@{ id = 'A'; label = "L $bigChunk"; gain = "G $bigChunk"; loss = "X $bigChunk" }
        )
        recommendation = 'A'
        recommendation_reason = "R $bigChunk"
        unknowns       = @("U1 $bigChunk", "U2 $bigChunk")
    }
}
$tight = Format-DecisionMessage -Project 'p' -ThreadId 'T-a' `
    -StopReason 'human' -Rounds 3 -LastMsgId 'msg-1' `
    -RawFallback 'raw ping' -Envelope $bulkyEnvelope -Budget 800
CheckTrue 'body fits within budget' ($tight.Length -le 800) "length=$($tight.Length)"
CheckTrue 'header and link always survive (D-29)' `
    (($tight -match 'T-a') -and ($tight -match 'https://example.invalid/dashboard/decisions/p/T-a')) $tight
CheckTrue 'a truncation marker is present when a slice was dropped' ($tight -match '詳細は chatroom') $tight
CheckTrue 'higher-priority slices ("question") drop LAST — none of R/L/U/G should appear before question does' `
    ($tight -notmatch "U2 xxxx") "budget of 800 could not fit the unknowns tail"

# --- Get-CachedDecision: legacy / duck-typed cache rows ----------------------------------------
Write-Host ''
Write-Host 'Get-CachedDecision — duck-types PSCustomObject rows (JSON round-trip shape)'
$state = @{}
$state['p/T-a'] = [PSCustomObject]@{
    signature = 'human:msg-1'
    envelope  = [PSCustomObject]@{ composer_status = 'ok'; output = [PSCustomObject]@{ question = 'q' } }
    cached_at = '2026-08-21T09:22:00Z'
}
$env = Get-CachedDecision -State $state -Key 'p/T-a' -Signature 'human:msg-1'
CheckTrue 'PSCustomObject cache hit' ($null -ne $env)
$env = Get-CachedDecision -State $state -Key 'p/T-a' -Signature 'human:msg-2'
CheckTrue 'signature mismatch is a miss' ($null -eq $env)
$env = Get-CachedDecision -State $state -Key 'not-there' -Signature 'human:msg-1'
CheckTrue 'absent key is a miss' ($null -eq $env)

# --- S4: Get-HumanParkedCandidates + digest section --------------------------------------------
Write-Host ''
Write-Host 'Get-HumanParkedCandidates — polls the sweep-side notified state (D-23)'
$candidates = @(
    [PSCustomObject]@{ key = 'p1/T-parked'; project = 'p1'; thread_id = 'T-parked'; repo_dir = 'x' },
    [PSCustomObject]@{ key = 'p1/T-moved';  project = 'p1'; thread_id = 'T-moved';  repo_dir = 'x' },
    [PSCustomObject]@{ key = 'p1/T-idle';   project = 'p1'; thread_id = 'T-idle';   repo_dir = 'x' },
    [PSCustomObject]@{ key = 'p1/T-notseen'; project = 'p1'; thread_id = 'T-notseen'; repo_dir = 'x' }
)
$notify = @{
    'p1/T-parked' = 'human:msg-100'
    'p1/T-moved'  = 'human:msg-50'         # head has moved to msg-51 -> excluded
    'p1/T-idle'   = 'none:msg-200'         # not a human-terminal reason -> excluded
    '__all_idle__' = 'sig-does-not-matter' # non-candidate key -> not confused for a candidate
    # p1/T-notseen: no entry -> excluded
}
$heads = @{
    'p1' = @{ 'T-parked' = 'msg-100'; 'T-moved' = 'msg-51'; 'T-idle' = 'msg-200'; 'T-notseen' = 'msg-1' }
}
$needsHuman = @{ 'human' = 'x'; 'no_progress_to_human' = 'x'; 'round_cap' = 'x' }
$parked = @(Get-HumanParkedCandidates -Candidates $candidates `
    -NotifyState $notify -HeadsByProject $heads -NeedsHuman $needsHuman)
Check 'exactly one candidate reported parked' 1 $parked.Count
Check 'the parked candidate is T-parked' 'p1/T-parked' $parked[0].key
Check 'parked candidate carries its reason' 'human' $parked[0].reason

Write-Host 'Get-HumanParkedCandidates — head-probe outage keeps the candidate reported (fail closed on the parked side)'
$headsMissing = @{ 'p1' = @{ 'T-moved' = 'msg-51'; 'T-idle' = 'msg-200' } }   # T-parked absent
$parked = @(Get-HumanParkedCandidates -Candidates $candidates `
    -NotifyState $notify -HeadsByProject $headsMissing -NeedsHuman $needsHuman)
$parkedKeys = ($parked | ForEach-Object { $_.key })
CheckTrue 'T-parked still reported when probe head unknown' ($parkedKeys -contains 'p1/T-parked')

Write-Host 'Get-HumanParkedCandidates — non-candidate keys in notified state are ignored'
$candidatesOnly = @([PSCustomObject]@{ key = 'p1/T-parked'; project = 'p1'; thread_id = 'T-parked'; repo_dir = 'x' })
$notifyWithNoise = @{
    'p1/T-parked'  = 'human:msg-100'
    '__quarantine__/p1/T-quar' = 'foo'
    '__all_idle__' = 'sig|sig'
}
$parked = @(Get-HumanParkedCandidates -Candidates $candidatesOnly `
    -NotifyState $notifyWithNoise -HeadsByProject $heads -NeedsHuman $needsHuman)
Check 'noise keys not confused for parked candidates' 1 $parked.Count

# --- Digest section: 判断待ち renders (A-4) and restores from a deleted cache (A-14) ------------
Write-Host ''
Write-Host 'New-DailyDigest — 判断待ち section renders when N > 0 and restores from a deleted cache (A-4 / A-14)'
$now = [datetime]::UtcNow
# Cache is empty (simulating a deleted pending-decisions.json). The section should still list the
# candidate, with "(問い未生成)" instead of a snippet — no silent 0.
$digest = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject $heads -ControlByProject @{} `
    -Now $now -LiveKeys @('p1/T-parked') `
    -HumanParked @([PSCustomObject]@{ key = 'p1/T-parked'; project = 'p1'; thread_id = 'T-parked'; reason = 'human'; head = 'msg-100'; parked_since = $null }) `
    -PendingDecisionsState @{}
CheckTrue '判断待ち header names 1 件' ($digest -match '判断待ち: 1 件') $digest
CheckTrue '判断待ち row names the parked key' ($digest -match 'p1/T-parked') $digest
CheckTrue 'row shows placeholder when the cache is empty (A-14)' ($digest -match '\(問い未生成\)') $digest

Write-Host 'New-DailyDigest — 判断待ち section is emitted at 0 件 too (msg-1370 §4)'
$digest = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject $heads -ControlByProject @{} `
    -Now $now -LiveKeys @('p1/T-parked') `
    -HumanParked @() -PendingDecisionsState @{}
CheckTrue '0 件 is not silent' ($digest -match '判断待ち: 0 件') $digest
CheckTrue '0 件 keeps the (該当なし) marker' ($digest -match '判断待ち: 0 件[\r\n]+  \(該当なし\)') $digest

Write-Host 'New-DailyDigest — a fresh cache enriches the row with the question snippet'
$pending = @{}
$pending['p1/T-parked'] = @{
    signature = 'human:msg-100'
    envelope  = [PSCustomObject]@{
        composer_status = 'ok'
        output          = [PSCustomObject]@{
            question              = 'Should we ship option A or option B?'
            options               = @()
            recommendation        = $null
            recommendation_reason = $null
            unknowns              = @()
        }
    }
    cached_at = $now.ToString('o')
}
$digest = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject $heads -ControlByProject @{} `
    -Now $now -LiveKeys @('p1/T-parked') `
    -HumanParked @([PSCustomObject]@{ key = 'p1/T-parked'; project = 'p1'; thread_id = 'T-parked'; reason = 'human'; head = 'msg-100'; parked_since = $null }) `
    -PendingDecisionsState $pending
CheckTrue 'question snippet renders on cache hit' ($digest -match 'Should we ship option A or option B\?') $digest
CheckTrue 'no (問い未生成) when the cache is populated' (-not ($digest -match '\(問い未生成\)')) $digest

Write-Host 'New-DailyDigest — stale cache (different signature) falls back to the placeholder'
$pending = @{}
$pending['p1/T-parked'] = @{
    signature = 'human:msg-99'   # differs from the parked head
    envelope  = [PSCustomObject]@{ composer_status = 'ok'; output = [PSCustomObject]@{ question = 'old' } }
    cached_at = $now.ToString('o')
}
$digest = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject $heads -ControlByProject @{} `
    -Now $now -LiveKeys @('p1/T-parked') `
    -HumanParked @([PSCustomObject]@{ key = 'p1/T-parked'; project = 'p1'; thread_id = 'T-parked'; reason = 'human'; head = 'msg-100'; parked_since = $null }) `
    -PendingDecisionsState $pending
CheckTrue 'stale cache row does NOT bleed into the section' ($digest -match '\(問い未生成\)') $digest
CheckTrue 'stale question text does NOT appear' (-not ($digest -match 'old')) $digest

Write-Host ''
if ($script:failures -gt 0) {
    Write-Host "decision-composer wiring: $($script:failures) check(s) FAILED"
    exit 1
}
Write-Host 'decision-composer wiring: all checks passed'
exit 0
