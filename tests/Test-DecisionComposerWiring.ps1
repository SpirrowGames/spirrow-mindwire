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
# Confirm-LogWorthKeeping is called by the material-push branches to flush the buffered log; the
# wrapper's real implementation touches module-scope state we do not lift. A no-op keeps the
# lifted functions callable without dragging in the whole logging module.
function Confirm-LogWorthKeeping { }

# Bring in the exact functions the sweep uses. Dot-sourcing would launch the sweep; parsing the
# AST and invoking just the function definitions keeps this test hermetic.
$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
$needed = @(
    'Get-CachedDecision',
    'Get-DecisionEnvelope',
    'Format-DecisionMessage',
    'New-ComposerInputJson',
    'Get-JsonState',
    'Save-JsonState',
    # S4 (D-32): the digest section renderer + a stub-able Invoke-ParkedHumansProbe wrapper so
    # the digest-side tests below never shell out to `uv run python scripts/parked_humans.py`.
    'New-DailyDigest',
    'Get-FingerprintHint',
    'Get-DerivedQuarantineState',
    'Format-DurationDigest',
    'ConvertTo-UtcInstant',
    # T-decision-material-push (msg-1445 §W-2 / §W-3): the material-push wiring.
    'New-DecisionLink',
    'New-MaterialUrl',
    'Get-ComposerReadHead',
    'Push-DecisionMaterial',
    'Test-NotificationSuppressed',
    'Send-NotificationIfChanged',
    'Send-HumanParkAlert'
)
foreach ($name in $needed) {
    $fn = $functions | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}

# The wrapper's Invoke-ComposerCli is what we override for A-2 / A-3 without ever running uv.
# The counter records how many times the composer was actually shelled out to.
# S3: also records the TailCount so we can assert Get-DecisionEnvelope passes it correctly
# when the backend is claude-code (D-38 pipe).
$script:composerCallCount = 0
$script:composerReturn = $null
$script:composerLastCall = $null
function Invoke-ComposerCli {
    param([string]$InputJson, [string]$Backend, [string]$Identity, [int]$TimeoutSeconds, [int]$TailCount = 0)
    $script:composerCallCount++
    $script:composerLastCall = @{
        InputJson = $InputJson
        Backend = $Backend
        Identity = $Identity
        TimeoutSeconds = $TimeoutSeconds
        TailCount = $TailCount
    }
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

# ===============================================================================================
# S3 (D-38): --tail N is passed to Invoke-ComposerCli only when the backend fetches tail
# ===============================================================================================
#
# Coverage:
#   * claude-code backend → -TailCount = $DecisionComposerTailLimit (5)
#   * stub backend        → -TailCount = 0 (unchanged S2 behaviour, payload tail is what CLI sees)
#
# Rationale: D-38 says "tail は Python 側で取り、子には文字列として渡す" — the tail-fetch is the
# CLI's job, but the WRAPPER decides whether the current backend wants a fetch. Only claude-code
# does today; a future backend that wants a fetch adds itself to the branch in Get-DecisionEnvelope.

Write-Host ''
Write-Host 'Get-DecisionEnvelope — S3 (D-38): claude-code backend triggers --tail passthrough'
$state = @{}
$script:composerCallCount = 0
$script:composerLastCall = $null
$script:composerReturn = @{ ok = $true; envelope = $fakeOkEnvelope; error = $null }
$script:DecisionComposerBackend = 'claude-code'
$null = Get-DecisionEnvelope -State $state -Key 'p/T-s3-a' -Project 'p' -ThreadId 'T-s3-a' `
    -Signature 'human:msg-1' -LastMsgId 'msg-1' -StopReason 'human' -Rounds 3
Check 'claude-code TailCount == DecisionComposerTailLimit (5)' 5 $script:composerLastCall.TailCount
Check 'claude-code backend name is passed through' 'claude-code' $script:composerLastCall.Backend

Write-Host 'Get-DecisionEnvelope — S3: stub backend keeps -TailCount = 0 (S2 backward-compat)'
$state = @{}
$script:composerCallCount = 0
$script:composerLastCall = $null
$script:composerReturn = @{ ok = $true; envelope = $fakeOkEnvelope; error = $null }
$script:DecisionComposerBackend = 'stub'
$null = Get-DecisionEnvelope -State $state -Key 'p/T-s3-b' -Project 'p' -ThreadId 'T-s3-b' `
    -Signature 'human:msg-1' -LastMsgId 'msg-1' -StopReason 'human' -Rounds 3
Check 'stub TailCount == 0' 0 $script:composerLastCall.TailCount

# Restore for the S4 tests below.
$script:DecisionComposerBackend = 'stub'

# ===============================================================================================
# S4 (D-32): 判断待ち digest section
# ===============================================================================================
#
# Coverage:
#   * A-4          — the section is emitted at N=0 (silent-day contract) and at N>0.
#   * A-14         — a wiped composed-question cache leaves the row + count intact and only
#                    degrades the enrichment to the "(問い未生成)" placeholder.
#   * Enrichment   — a cache row with a matching signature ("human:<head>") folds the composer's
#                    question into the row (truncated to 80 chars, newlines flattened).
#   * Signature    — a cache row whose signature does NOT match the parked head degrades to the
#     strictness    placeholder (S2 A-3's "one CLI call per signature" contract is what makes this
#                    safe — an older signature is stale, not a match).
#   * Fetch errors — a non-empty $ParkedPollErrors renders as "取得失敗: N 件" so an outage is
#                    visible under the section (I-2 "黙って劣化しない"), and the section itself
#                    never disappears.

# $script:StarvedThreshold is read by New-DailyDigest indirectly via Get-StarvedKeys. Set it here
# so the digest can render a starvation section (no live keys, so it will be 0 件 anyway).
$script:StarvedThreshold = [TimeSpan]::FromHours(24)

$script:nowUtc = [DateTime]::Parse('2026-08-21T02:00:00Z', $null, [System.Globalization.DateTimeStyles]::AssumeUniversal -bor [System.Globalization.DateTimeStyles]::AdjustToUniversal)

function Get-DigestSection {
    param([string]$Digest, [string]$Header)
    # A digest section starts with a header line ("判断待ち: N 件") and runs to the next blank
    # line. Used by the tests below so they read only the relevant slice, not the whole message.
    $lines = $Digest -split "`n"
    $out = @()
    $inSection = $false
    foreach ($ln in $lines) {
        if ($ln -like "$Header*") { $inSection = $true; $out += $ln; continue }
        if ($inSection) {
            if ($ln -eq '' -or $ln -match '^[^\s]') {
                if ($ln -eq '') { break }
                # Next section header — end of ours.
                if ($ln -notlike '  *') { break }
            }
            $out += $ln
        }
    }
    return ($out -join "`n")
}

# --- A-4: 0 件 still emits the section ---------------------------------------------------------
Write-Host ''
Write-Host 'New-DailyDigest — 判断待ち: 0 件 still emits the section (A-4)'
$digest0 = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $script:nowUtc -LiveKeys @() `
    -HumanParked @() -PendingDecisionsState @{} -ParkedPollErrors @()
$section = Get-DigestSection -Digest $digest0 -Header '判断待ち:'
CheckTrue '判断待ち header present at 0 件' ($section -match '判断待ち: 0 件') $section
CheckTrue '該当なし marker present at 0 件' ($section -match '該当なし') $section
CheckTrue 'no 取得失敗 line when there are no fetch errors' (-not ($section -match '取得失敗')) $section

# --- N>0 with no cache: (問い未生成) placeholder (A-14) ----------------------------------------
Write-Host ''
Write-Host 'New-DailyDigest — 判断待ち: N>0 with an empty cache renders (問い未生成) placeholder (A-14)'
$parked1 = @(
    [PSCustomObject]@{ key = 'p/T-a'; project = 'p'; thread_id = 'T-a'; head_msg_id = 'msg-100' }
)
$digest1 = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $script:nowUtc -LiveKeys @() `
    -HumanParked $parked1 -PendingDecisionsState @{} -ParkedPollErrors @()
$section = Get-DigestSection -Digest $digest1 -Header '判断待ち:'
CheckTrue '判断待ち: 1 件 shown' ($section -match '判断待ち: 1 件') $section
CheckTrue 'row lists the key' ($section -match 'p/T-a') $section
CheckTrue 'row lists the head msg id' ($section -match '\[msg-100\]') $section
CheckTrue 'placeholder present when cache is empty' ($section -match '\(問い未生成\)') $section

# --- N>0 with a matching cache row: question is enriched onto the row --------------------------
Write-Host ''
Write-Host 'New-DailyDigest — matching cache row folds the question snippet onto the row'
$cacheOk = @{}
$cacheOk['p/T-a'] = @{
    signature = 'human:msg-100'
    envelope  = [PSCustomObject]@{
        composer_status = 'ok'
        output          = [PSCustomObject]@{
            question = 'Should we adopt approach A or B?'
        }
    }
}
$digest2 = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $script:nowUtc -LiveKeys @() `
    -HumanParked $parked1 -PendingDecisionsState $cacheOk -ParkedPollErrors @()
$section = Get-DigestSection -Digest $digest2 -Header '判断待ち:'
CheckTrue 'question snippet renders on the row' ($section -match 'Should we adopt approach A or B') $section
CheckTrue 'no placeholder when the question was folded in' (-not ($section -match '\(問い未生成\)')) $section

# --- Signature strictness: mismatched signature degrades to the placeholder ---------------------
Write-Host ''
Write-Host 'New-DailyDigest — cache row for a DIFFERENT signature degrades to (問い未生成)'
$cacheStale = @{}
$cacheStale['p/T-a'] = @{
    signature = 'human:msg-99'    # stale — thread head has advanced to msg-100
    envelope  = [PSCustomObject]@{
        composer_status = 'ok'
        output          = [PSCustomObject]@{ question = 'STALE QUESTION' }
    }
}
$digest3 = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $script:nowUtc -LiveKeys @() `
    -HumanParked $parked1 -PendingDecisionsState $cacheStale -ParkedPollErrors @()
$section = Get-DigestSection -Digest $digest3 -Header '判断待ち:'
CheckTrue 'placeholder shown when cache row is stale (signature mismatch)' ($section -match '\(問い未生成\)') $section
CheckTrue 'stale question is NOT rendered onto the row' (-not ($section -match 'STALE QUESTION')) $section

# --- Enrichment is strict on composer_status too -----------------------------------------------
Write-Host ''
Write-Host 'New-DailyDigest — a non-ok cache row (composer failed) does NOT enrich the row'
$cacheFail = @{}
$cacheFail['p/T-a'] = @{
    signature = 'human:msg-100'
    envelope  = [PSCustomObject]@{
        composer_status = 'error'
        output          = $null
        error           = 'CLI timeout'
    }
}
$digest4 = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $script:nowUtc -LiveKeys @() `
    -HumanParked $parked1 -PendingDecisionsState $cacheFail -ParkedPollErrors @()
$section = Get-DigestSection -Digest $digest4 -Header '判断待ち:'
CheckTrue 'placeholder shown when composer failed' ($section -match '\(問い未生成\)') $section
CheckTrue 'error text is NOT rendered on the row' (-not ($section -match 'CLI timeout')) $section

# --- Multi-line / very long question: flattened + truncated -----------------------------------
Write-Host ''
Write-Host 'New-DailyDigest — a multi-line, long question is flattened and truncated at 80 chars'
$cacheLong = @{}
$cacheLong['p/T-a'] = @{
    signature = 'human:msg-100'
    envelope  = [PSCustomObject]@{
        composer_status = 'ok'
        output          = [PSCustomObject]@{ question = "line one`nline two`n$( 'x' * 200 )" }
    }
}
$digest5 = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $script:nowUtc -LiveKeys @() `
    -HumanParked $parked1 -PendingDecisionsState $cacheLong -ParkedPollErrors @()
$section = Get-DigestSection -Digest $digest5 -Header '判断待ち:'
CheckTrue 'multi-line question is flattened (no bare newlines inside the row snippet)' `
    ($section -notmatch "line one`nline two") $section
CheckTrue 'truncation marker (…) present on long question' ($section -match '…') $section

# --- Fetch-error surface: I-2 "黙って劣化しない" ------------------------------------------------
Write-Host ''
Write-Host 'New-DailyDigest — fetch errors render as "取得失敗: N 件" under the section'
$errors = @(
    [PSCustomObject]@{ thread_id = 'T-b'; reason = 'chatroom_get_thread failed: connection refused' }
)
$digestErr = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $script:nowUtc -LiveKeys @() `
    -HumanParked @() -PendingDecisionsState @{} -ParkedPollErrors $errors
$section = Get-DigestSection -Digest $digestErr -Header '判断待ち:'
CheckTrue 'section still present when the parked list is empty but there are errors' `
    ($section -match '判断待ち: 0 件') $section
CheckTrue '取得失敗: N 件 line is present' ($section -match '取得失敗: 1 件') $section
CheckTrue 'the failing thread id is listed' ($section -match 'T-b') $section
CheckTrue 'the failure reason is listed' ($section -match 'connection refused') $section

# --- Multiple parked entries: order + separate rows --------------------------------------------
Write-Host ''
Write-Host 'New-DailyDigest — multiple parked entries render as separate rows in input order'
$parkedMulti = @(
    [PSCustomObject]@{ key = 'p1/T-a'; project = 'p1'; thread_id = 'T-a'; head_msg_id = 'msg-1' }
    [PSCustomObject]@{ key = 'p2/T-b'; project = 'p2'; thread_id = 'T-b'; head_msg_id = 'msg-2' }
)
$digestMulti = New-DailyDigest -QuarantineState @{} -EvaluatedState @{} `
    -HeadsByProject @{} -ControlByProject @{} -Now $script:nowUtc -LiveKeys @() `
    -HumanParked $parkedMulti -PendingDecisionsState @{} -ParkedPollErrors @()
$section = Get-DigestSection -Digest $digestMulti -Header '判断待ち:'
CheckTrue '判断待ち: 2 件 shown' ($section -match '判断待ち: 2 件') $section
CheckTrue 'first row present' ($section -match 'p1/T-a') $section
CheckTrue 'second row present' ($section -match 'p2/T-b') $section
CheckTrue 'input order preserved (p1 before p2)' `
    ($section.IndexOf('p1/T-a') -lt $section.IndexOf('p2/T-b')) $section

# ===============================================================================================
# T-decision-material-push (msg-1443 §3 / msg-1445 §W-2 / §W-3 / §5): material PUT is wired,
# ordered before the notification, fail-open, gated on the SAME dedup predicate, and shares
# {project}/{thread_id} with the human-facing link (I-17).
# ===============================================================================================
#
# Coverage (msg-1445 §5):
#   (a) order       — Push-DecisionMaterial is called BEFORE Send-Notification (D-34 ①→②)
#   (b) fail-open   — a throwing / 4xx / 5xx Invoke-MaterialPut leaves the notification body
#                     one-character-identical to the PUT-success body (D-34)
#   (c) gate same   — same-signature repeat tick fires neither PUT nor notification (DM-3)
#   (d) non-ok      — composer_status != "ok" skips the PUT; the raw ping still fires (DM-6)
#   (e) I-17        — the URL Invoke-MaterialPut receives shares {project}/{thread_id} with the
#                     link the notification body carries
#
# Auxiliary pins:
#   * head absent  — extras.head_msg_id_read missing => PUT is skipped (DM-4 / I-16)
#   * URL builders — New-DecisionLink and New-MaterialUrl share $DecisionDashboardBaseUrl and
#                    percent-encode the same way (I-17 at the layer below Format-DecisionMessage)
#   * Get-ComposerReadHead — duck-types both hashtable and PSCustomObject envelopes

Write-Host ''
Write-Host '--- T-decision-material-push ---'

# Fake seams. Both mirror the composer's { ok; ... } shape convention.
# The order log records the sequence of side-effecting calls so the "order" check is unambiguous
# even under a future refactor that changed the order without changing the count.
$script:materialCallCount = 0
$script:materialLastCall = $null
$script:materialReturn = @{ ok = $true; status = 200; body = '{"stored":true,"replaced":false}'; elapsed_ms = 42; error = $null }
$script:materialThrow = $null
$script:notificationCallCount = 0
$script:notificationLastMessage = $null
$script:callOrder = New-Object System.Collections.Generic.List[string]

function Invoke-MaterialPut {
    param([string]$Url, [string]$BodyJson, [int]$TimeoutSec = 10)
    $script:materialCallCount++
    $script:materialLastCall = @{ Url = $Url; BodyJson = $BodyJson; TimeoutSec = $TimeoutSec }
    $script:callOrder.Add('material')
    if ($script:materialThrow) { throw $script:materialThrow }
    return $script:materialReturn
}

function Send-Notification {
    param([string]$Message)
    $script:notificationCallCount++
    $script:notificationLastMessage = $Message
    $script:callOrder.Add('notification')
    return 'sent'
}

function Reset-MaterialSpy {
    $script:materialCallCount = 0
    $script:materialLastCall = $null
    $script:materialThrow = $null
    $script:notificationCallCount = 0
    $script:notificationLastMessage = $null
    $script:callOrder.Clear()
}

# --- URL builders share base + encoding (I-17 pin below the caller layer) ----------------------
Write-Host 'New-DecisionLink / New-MaterialUrl — share base URL and percent-encoding'
$link = New-DecisionLink -Project 'p 1' -ThreadId 'T/a?x'
$mUrl = New-MaterialUrl -Project 'p 1' -ThreadId 'T/a?x'
CheckTrue 'link uses the dashboard base' ($link.StartsWith('https://example.invalid/dashboard/decisions/')) $link
CheckTrue 'material URL uses the dashboard base' ($mUrl.StartsWith('https://example.invalid/v1/decisions/')) $mUrl
CheckTrue 'link percent-encodes the project' ($link -match 'p%201') $link
CheckTrue 'material URL percent-encodes the project (same encoding as link)' ($mUrl -match 'p%201') $mUrl
CheckTrue 'link percent-encodes the thread id' ($link -match 'T%2Fa%3Fx') $link
CheckTrue 'material URL percent-encodes the thread id (same encoding as link)' ($mUrl -match 'T%2Fa%3Fx') $mUrl
CheckTrue 'material URL ends in /material' ($mUrl.EndsWith('/material')) $mUrl

# --- Get-ComposerReadHead — duck-types both shapes --------------------------------------------
Write-Host ''
Write-Host 'Get-ComposerReadHead — duck-types hashtable and PSCustomObject envelopes'
$envHash = @{ composer_status = 'ok'; extras = @{ head_msg_id_read = 'msg-9001' } }
Check 'hashtable envelope, hashtable extras' 'msg-9001' (Get-ComposerReadHead -Envelope $envHash)
$envPso = [PSCustomObject]@{
    composer_status = 'ok'
    extras          = [PSCustomObject]@{ head_msg_id_read = 'msg-9002' }
}
Check 'PSCustomObject envelope, PSCustomObject extras' 'msg-9002' (Get-ComposerReadHead -Envelope $envPso)
$envNoExtras = [PSCustomObject]@{ composer_status = 'ok' }
CheckTrue 'no extras -> $null' ($null -eq (Get-ComposerReadHead -Envelope $envNoExtras))
$envNoKey = [PSCustomObject]@{ composer_status = 'ok'; extras = [PSCustomObject]@{ tail_count = '3' } }
CheckTrue 'extras present but head_msg_id_read absent -> $null' ($null -eq (Get-ComposerReadHead -Envelope $envNoKey))
CheckTrue 'null envelope -> $null' ($null -eq (Get-ComposerReadHead -Envelope $null))

# --- Fixture: an ok envelope carrying a composer-read head ------------------------------------
$freshEnvelope = [PSCustomObject]@{
    composer_status = 'ok'
    signature       = 'human:msg-1'
    extras          = [PSCustomObject]@{
        head_msg_id_read = 'msg-777'
        tail_count       = '3'
    }
    output          = [PSCustomObject]@{
        question              = 'Adopt A or B?'
        options               = @(
            [PSCustomObject]@{ id = 'A'; label = 'first'; gain = 'gg'; loss = 'll' }
        )
        recommendation        = 'A'
        recommendation_reason = 'because'
        unknowns              = @('unk-1')
    }
}

# ---------- (a) order: material PUT precedes the notification ---------------------------------
Write-Host ''
Write-Host '(a) Send-HumanParkAlert — material PUT is called BEFORE Send-Notification (D-34 order)'
Reset-MaterialSpy
$pending = @{}
$notified = @{}
$script:composerCallCount = 0
$script:composerReturn = @{ ok = $true; envelope = $freshEnvelope; error = $null }
$script:DecisionComposerBackend = 'claude-code'
Send-HumanParkAlert -PendingDecisionsState $pending -NotifyState $notified `
    -Key 'p/T-order' -Project 'p' -ThreadId 'T-order' `
    -Signature 'human:msg-1' -LastMsgId 'msg-777' `
    -StopReason 'human' -Rounds 3 `
    -RawFallback 'raw ping'
Check 'material PUT fired once' 1 $script:materialCallCount
Check 'notification fired once' 1 $script:notificationCallCount
CheckTrue 'material precedes notification in the call log' `
    (($script:callOrder.IndexOf('material')) -lt ($script:callOrder.IndexOf('notification'))) `
    ("callOrder=[" + ($script:callOrder -join ',') + "]")

# The body Push-DecisionMaterial sent includes the composer-read head (I-16 wire-side pin).
$sentBody = $script:materialLastCall.BodyJson | ConvertFrom-Json
Check 'PUT body carries composer-read head_msg_id' 'msg-777' $sentBody.head_msg_id
Check 'PUT body carries the signature (magickit stores it opaquely)' 'human:msg-1' $sentBody.signature
Check 'PUT body carries composer_status=ok' 'ok' $sentBody.composer_status
Check 'PUT body carries the question' 'Adopt A or B?' $sentBody.question

# --------- (b) fail-open: a throwing / 4xx / 5xx PUT does NOT change the notification --------
Write-Host ''
Write-Host '(b) Send-HumanParkAlert — a throwing PUT is fail-open: notification body unchanged'
Reset-MaterialSpy
$pending = @{}
$notified = @{}
$script:composerCallCount = 0
$script:composerReturn = @{ ok = $true; envelope = $freshEnvelope; error = $null }
# Baseline: capture the body the notification receives when the PUT succeeds.
Send-HumanParkAlert -PendingDecisionsState $pending -NotifyState $notified `
    -Key 'p/T-ok' -Project 'p' -ThreadId 'T-ok' `
    -Signature 'human:msg-1' -LastMsgId 'msg-777' `
    -StopReason 'human' -Rounds 3 -RawFallback 'raw ping'
$msgWithOkPut = $script:notificationLastMessage

Reset-MaterialSpy
$pending = @{}
$notified = @{}
$script:composerCallCount = 0
$script:composerReturn = @{ ok = $true; envelope = $freshEnvelope; error = $null }
$script:materialThrow = 'simulated tls handshake failure'
Send-HumanParkAlert -PendingDecisionsState $pending -NotifyState $notified `
    -Key 'p/T-throw' -Project 'p' -ThreadId 'T-throw' `
    -Signature 'human:msg-1' -LastMsgId 'msg-777' `
    -StopReason 'human' -Rounds 3 -RawFallback 'raw ping'
Check 'throwing PUT: notification still fired exactly once' 1 $script:notificationCallCount

Reset-MaterialSpy
$pending = @{}
$notified = @{}
$script:composerCallCount = 0
$script:composerReturn = @{ ok = $true; envelope = $freshEnvelope; error = $null }
$script:materialReturn = @{ ok = $false; status = 500; body = 'server exploded'; elapsed_ms = 12; error = 'HTTP 500' }
Send-HumanParkAlert -PendingDecisionsState $pending -NotifyState $notified `
    -Key 'p/T-500' -Project 'p' -ThreadId 'T-500' `
    -Signature 'human:msg-1' -LastMsgId 'msg-777' `
    -StopReason 'human' -Rounds 3 -RawFallback 'raw ping'
Check '500 PUT: notification still fired exactly once' 1 $script:notificationCallCount
# The body is scoped by {ThreadId}/{Project} in Format-DecisionMessage, so a comparison against
# the baseline needs to be per-thread; compare by replacing the thread id in the failing body
# with the OK's thread id so the only difference should be the enrichment (which is 0).
$normalizedFailBody = ($script:notificationLastMessage `
    -replace 'T-500', 'T-ok' `
    -replace [regex]::Escape('/T-500'), '/T-ok')
CheckTrue 'fail-open: notification body is character-identical to the OK-PUT baseline' `
    ($normalizedFailBody -eq $msgWithOkPut) `
    ("ok=[$msgWithOkPut] fail=[$normalizedFailBody]")

# Reset the fake back to success for the tests below.
$script:materialReturn = @{ ok = $true; status = 200; body = '{"stored":true,"replaced":false}'; elapsed_ms = 42; error = $null }

# ---------- (c) gate: same-signature repeat tick fires neither PUT nor notification -----------
Write-Host ''
Write-Host '(c) Send-HumanParkAlert — same signature: neither PUT nor notification (DM-3)'
Reset-MaterialSpy
$pending = @{}
$notified = @{}
$script:composerCallCount = 0
$script:composerReturn = @{ ok = $true; envelope = $freshEnvelope; error = $null }
Send-HumanParkAlert -PendingDecisionsState $pending -NotifyState $notified `
    -Key 'p/T-gate' -Project 'p' -ThreadId 'T-gate' `
    -Signature 'human:msg-1' -LastMsgId 'msg-777' `
    -StopReason 'human' -Rounds 3 -RawFallback 'raw ping'
Check 'first tick: PUT fired once' 1 $script:materialCallCount
Check 'first tick: notification fired once' 1 $script:notificationCallCount

# Same signature, second tick.
Send-HumanParkAlert -PendingDecisionsState $pending -NotifyState $notified `
    -Key 'p/T-gate' -Project 'p' -ThreadId 'T-gate' `
    -Signature 'human:msg-1' -LastMsgId 'msg-777' `
    -StopReason 'human' -Rounds 3 -RawFallback 'raw ping'
Check 'second same-signature tick: PUT count STILL 1 (DM-3 gate held)' 1 $script:materialCallCount
Check 'second same-signature tick: notification count STILL 1' 1 $script:notificationCallCount

# ---------- (d) non-ok composer_status: PUT skipped, notification fires -----------------------
Write-Host ''
Write-Host '(d) Send-HumanParkAlert — composer_status != "ok" skips PUT; raw ping fires (DM-6)'
$errorEnvelope = [PSCustomObject]@{
    composer_status = 'error'
    extras          = [PSCustomObject]@{ head_msg_id_read = 'msg-777' }
    output          = $null
    error           = 'simulated composer timeout'
}
Reset-MaterialSpy
$pending = @{}
$notified = @{}
$script:composerCallCount = 0
$script:composerReturn = @{ ok = $true; envelope = $errorEnvelope; error = $null }
Send-HumanParkAlert -PendingDecisionsState $pending -NotifyState $notified `
    -Key 'p/T-nonok' -Project 'p' -ThreadId 'T-nonok' `
    -Signature 'human:msg-2' -LastMsgId 'msg-777' `
    -StopReason 'human' -Rounds 4 -RawFallback 'raw ping for T-nonok'
Check 'non-ok envelope: PUT was NOT called' 0 $script:materialCallCount
Check 'non-ok envelope: notification still fired (raw ping fell through)' 1 $script:notificationCallCount
Check 'non-ok envelope: notification body is the raw fallback' 'raw ping for T-nonok' $script:notificationLastMessage

# ---------- extras.head_msg_id_read missing: PUT skipped (DM-4 / I-16) ------------------------
Write-Host ''
Write-Host 'Send-HumanParkAlert — missing extras.head_msg_id_read skips PUT (DM-4 / I-16)'
$noHeadEnvelope = [PSCustomObject]@{
    composer_status = 'ok'
    extras          = [PSCustomObject]@{ tail_fetch_error = 'chatroom outage' }
    output          = [PSCustomObject]@{
        question              = 'q?'
        options               = @()
        recommendation        = $null
        recommendation_reason = $null
        unknowns              = @()
    }
}
Reset-MaterialSpy
$pending = @{}
$notified = @{}
$script:composerCallCount = 0
$script:composerReturn = @{ ok = $true; envelope = $noHeadEnvelope; error = $null }
Send-HumanParkAlert -PendingDecisionsState $pending -NotifyState $notified `
    -Key 'p/T-nohead' -Project 'p' -ThreadId 'T-nohead' `
    -Signature 'human:msg-3' -LastMsgId 'msg-999' `
    -StopReason 'human' -Rounds 2 -RawFallback 'raw ping'
Check 'no head_msg_id_read: PUT was NOT called (fallback to last_msg_id is forbidden)' 0 $script:materialCallCount
Check 'no head_msg_id_read: notification still fired' 1 $script:notificationCallCount

# ---------- (e) I-17: PUT URL and link body share {project}/{thread_id} -----------------------
Write-Host ''
Write-Host '(e) Send-HumanParkAlert — PUT URL and notification body link share {project}/{thread_id}'
Reset-MaterialSpy
$pending = @{}
$notified = @{}
$script:composerCallCount = 0
$script:composerReturn = @{ ok = $true; envelope = $freshEnvelope; error = $null }
Send-HumanParkAlert -PendingDecisionsState $pending -NotifyState $notified `
    -Key 'proj-x/T-share' -Project 'proj-x' -ThreadId 'T-share' `
    -Signature 'human:msg-5' -LastMsgId 'msg-777' `
    -StopReason 'human' -Rounds 3 -RawFallback 'raw ping'
$putUrl = $script:materialLastCall.Url
$notifBody = $script:notificationLastMessage
CheckTrue 'PUT URL contains {project}=proj-x' ($putUrl -match '/decisions/proj-x/') $putUrl
CheckTrue 'PUT URL contains {thread_id}=T-share' ($putUrl -match '/proj-x/T-share/') $putUrl
CheckTrue 'notification body contains the SAME {project}/{thread_id} in its link' `
    ($notifBody -match 'dashboard/decisions/proj-x/T-share') $notifBody

Write-Host ''
if ($script:failures -gt 0) {
    Write-Host "decision-composer wiring: $($script:failures) check(s) FAILED"
    exit 1
}
Write-Host 'decision-composer wiring: all checks passed'
exit 0
