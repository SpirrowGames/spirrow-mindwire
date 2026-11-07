# Regression guard for the discard alerts of deploy/run-conductor-scheduled.ps1.
#
# T-dirty-clone-wip-retreat-and-silent-stall-alerts PR-3: design v3.0 (Bohr msg-8115) as revised by
# v3.1 (msg-8117), v3.2 (msg-8119) and v3.3 (msg-8121); naysayer go Einstein (objections []).
#   * every `discarded` / `discard_incomplete` record of state/clone-retreats.jsonl reaches the alert
#     channel once; order is the file's line order, never a comparison of record ids;
#   * the cursor (state/clone-discard-notify.json) moves past a record only once a message about it
#     was delivered: `skipped` holds, transient failures retry, a payload rejection resends one record
#     per message and falls back to a short ASCII line, a rejected fallback holds on a payload hash;
#   * every message, notice and `(part i/n)` marker included, is at most 1950 characters.
#
# Functions are lifted out of the sweep script's AST (dot-sourcing would run the sweep); the wiring
# into the real loop is checked on the AST.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sweepScript = Join-Path $repoRoot "deploy/run-conductor-scheduled.ps1"
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }

$script:logLines = @()
function Write-Log { param([string]$Message) $script:logLines += $Message }

$functions = $ast.FindAll(
    { param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
$lifted = 'Save-JsonState', 'Read-CloneRetreatRecordLines', 'Get-CloneDiscardRecordUuid', 'Get-CloneDiscardSha256',
          'Format-CloneDiscardRecordLines', 'Get-CloneDiscardPrefix', 'Split-CloneDiscardText',
          'New-CloneDiscardRecordMessages', 'New-CloneDiscardFallback', 'Test-CloneDiscardFallbackSafe',
          'Get-CloneDiscardPayloadHash', 'Resolve-CloneDiscardSendResult', 'Send-CloneDiscardRecord',
          'Update-CloneDiscardAlerts'
foreach ($name in $lifted) {
    $fn = $functions | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $fn) { throw "function not found in sweep script: $name" }
    Invoke-Expression $fn.Extent.Text
}
$assign = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $n.Left.Extent.Text -eq '$DecisionMessageDiscordBudget' }, $true) | Select-Object -First 1
$DecisionMessageDiscordBudget = [int]$assign.Right.Extent.Text
$Budget = $DecisionMessageDiscordBudget

$script:failures = 0
function Check {
    param([string]$Name, $Expected, $Actual)
    if ($Expected -eq $Actual) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} — expected '{1}', got '{2}'" -f $Name, $Expected, $Actual) }
}

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("mw-discard-alerts-" + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $tmp
$utf8 = New-Object System.Text.UTF8Encoding($false)

$script:caseNo = 0
function New-Case {
    $script:caseNo++
    $d = Join-Path $tmp "case$($script:caseNo)"
    $null = New-Item -ItemType Directory -Path $d
    $script:records = Join-Path $d 'clone-retreats.jsonl'
    $script:statePath = Join-Path $d 'clone-discard-notify.json'
    $script:sent = @()
    $script:attempts = @()
    $script:logLines = @()
    $script:respond = { param($m) @{ status = 'sent'; class = 'ok'; http_status = 200 } }
}
function Add-Rec {
    param([string]$Outcome, [string]$Thread = 'T-1', [string]$Suffix = '', [string]$RepoDir = 'C:\w\clone-A',
          [string]$Patch = 'C:\w\state\patches\p.patch', [string]$At = '2026-11-07T01:00:00+00:00', [string]$Kind = 'retreat')
    if (-not $Suffix) { $Suffix = [guid]::NewGuid().ToString('N').Substring(0, 12) }
    $id = "$Thread@$At#$Suffix"
    $row = [ordered]@{ kind = $Kind; record_id = $id; at = $At; thread_id = $Thread; repo_dir = $RepoDir
                       stop_reason = 'no_handoff_to_human'; outcome = $Outcome; patch = $Patch }
    [System.IO.File]::AppendAllText($script:records, (($row | ConvertTo-Json -Compress) + "`n"), $utf8)
    return $id
}
function Get-Cursor {
    if (-not (Test-Path -LiteralPath $script:statePath)) { return '<no state>' }
    return "$((Get-Content -LiteralPath $script:statePath -Raw | ConvertFrom-Json).cursor_record_id)"
}
function Get-Hold {
    if (-not (Test-Path -LiteralPath $script:statePath)) { return $null }
    return (Get-Content -LiteralPath $script:statePath -Raw | ConvertFrom-Json).hold
}
# The Send stub: records every attempt (with the cursor at that moment) and delivered message.
$sendStub = {
    param($m)
    $script:attempts += @{ text = $m; cursor = (Get-Cursor) }
    $r = & $script:respond $m
    if ($r.status -eq 'sent') { $script:sent += $m }
    return $r
}
function Tick {
    param([bool]$Webhook = $true, [string]$Fingerprint = 'fp-1')
    Update-CloneDiscardAlerts -RecordsPath $script:records -StatePath $script:statePath -NowIso '2026-11-07T02:00:00Z' `
        -WebhookConfigured $Webhook -WebhookFingerprint $Fingerprint -Send $sendStub -Budget $Budget
}
$rejected = @{ status = 'failed'; class = 'deterministic-payload'; http_status = 400 }
$transient = @{ status = 'failed'; class = 'transient'; http_status = 503 }
$ok = @{ status = 'sent'; class = 'ok'; http_status = 200 }
function Test-IsFallback { param([string]$m) return $m.StartsWith('discard alert rejected by webhook') }

try {
    Write-Host 'first run / baseline'
    New-Case
    $old = Add-Rec -Outcome 'discarded'
    Tick
    Check 'no state + existing records: nothing sent' 0 $script:attempts.Count
    Check 'no state + existing records: baseline is the last record' $old (Get-Cursor)
    $new = Add-Rec -Outcome 'discarded' -Thread 'T-new'
    Tick
    Check 'a record added after the baseline is alerted' 1 $script:sent.Count
    Check 'the alert names the new record' $true ($script:sent[0] -match [regex]::Escape($new))
    Check 'the alert does not re-send the baseline record' $false ($script:sent[0] -match [regex]::Escape($old))
    Tick
    Check 'next tick sends nothing new' 1 $script:attempts.Count
    Check 'cursor is on the alerted record' $new (Get-Cursor)

    New-Case
    Tick
    Check 'no state + no file: state is written' '' (Get-Cursor)
    Check 'no state + no file: nothing sent' 0 $script:attempts.Count
    Tick
    Check 'an empty initialised state stays quiet without records' 0 $script:attempts.Count
    $first = Add-Rec -Outcome 'discarded'
    Tick
    Check 'the first discard after an empty initialisation is alerted (Obj-11-2)' 1 $script:sent.Count
    Check 'cursor moves to it' $first (Get-Cursor)

    Write-Host ''
    Write-Host 'order is line order, never id order'
    New-Case
    Tick
    $hi = Add-Rec -Outcome 'discarded' -Suffix 'ffffffffffff' -At '2026-11-07T01:00:00+00:00'
    Tick
    $lo = Add-Rec -Outcome 'discarded' -Suffix '000000000000' -At '2026-11-07T01:00:00+00:00'
    Tick
    Check 'same timestamp, uuid sorting before the cursor: two alerts' 2 $script:sent.Count
    Check 'the later line with the smaller id is not skipped' $true ($script:sent[1] -match [regex]::Escape($lo))
    $a = Add-Rec -Outcome 'discarded' -Suffix 'eeeeeeeeeeee'
    $b = Add-Rec -Outcome 'discarded' -Suffix '111111111111'
    Tick
    Check 'two discards in the same repo in one tick: one alert' 3 $script:sent.Count
    Check 'that alert lists both' $true (($script:sent[2] -match [regex]::Escape($a)) -and ($script:sent[2] -match [regex]::Escape($b)))
    Check 'cursor is the last line' $b (Get-Cursor)

    Write-Host ''
    Write-Host 'outcomes and wording'
    New-Case
    Tick
    $null = Add-Rec -Outcome 'retreated'
    $lastNonDiscard = Add-Rec -Outcome 'failed'
    Tick
    Check 'retreated / failed records: no discard alert' 0 $script:attempts.Count
    Check 'the cursor skips non-discard rows' $lastNonDiscard (Get-Cursor)
    $inc = Add-Rec -Outcome 'discard_incomplete' -Thread 'T-inc'
    Tick
    $m = $script:sent[0]
    Check 'discard_incomplete is alerted' 1 $script:sent.Count
    Check 'discard_incomplete: says the discard did not complete' $true ($m -match 'the wrapper tried to discard at end of launch T-inc, but the discard did not complete')
    Check 'discard_incomplete: check-the-clone line' $true ($m -match [regex]::Escape('check the clone (git status / current branch) and check whether the patch exists before recovering'))
    Check 'discard_incomplete: never "completed"' $false ($m -match 'completed')
    Check 'discard_incomplete: never "the clone is clean"' $false ($m -match 'the clone is clean')
    $null = Add-Rec -Outcome 'discarded' -Thread 'T-done'
    Tick
    $m = $script:sent[1]
    Check 'discarded: completed wording' $true ($m -match [regex]::Escape('discarded by wrapper at end of launch T-done (completed; the clone is clean)'))
    foreach ($field in 'repo_dir=C:\w\clone-A', 'thread=T-done', 'stop=no_handoff_to_human', 'outcome=discarded', 'at=2026-11-07T01:00:00', 'patch: C:\w\state\patches\p.patch') {
        Check "record line carries $field" $true ($m.Contains($field))
    }
    Check 'no cause is claimed' $false ($m -match 'cause')
    $null = Add-Rec -Outcome 'discarded' -Kind 'delivered'
    [System.IO.File]::AppendAllText($script:records, "{not json`n", $utf8)
    Tick
    Check 'delivered rows and malformed lines are never alerted' 2 $script:sent.Count

    Write-Host ''
    Write-Host 'cursor not found'
    New-Case
    Tick
    $c1 = Add-Rec -Outcome 'discarded' -Thread 'T-c1'
    $null = Add-Rec -Outcome 'retreated'
    $c2 = Add-Rec -Outcome 'discard_incomplete' -Thread 'T-c2' -Patch ('C:\' + ('p' * 5000))
    Save-JsonState -Path $script:statePath -State @{ initialized = $true; cursor_record_id = 'T-gone@x#abcabcabcabc'; hold = $null }
    Tick
    Check 'cursor not found: logged' 1 @($script:logLines | Where-Object { $_ -like 'discard-cursor-missing*' }).Count
    Check 'cursor not found: every message starts with the notice' $true (@($script:sent | Where-Object { -not $_.StartsWith('cursor not found — re-sending every discard record; some may repeat') }).Count -eq 0)
    Check 'cursor not found: both discards re-sent' $true ((($script:sent -join "`n") -match [regex]::Escape($c1)) -and (($script:sent -join "`n") -match 'T-c2'))
    Check 'cursor not found: every message, notice included, within 1950' 0 @($script:sent | Where-Object { $_.Length -gt 1950 }).Count
    Check 'cursor not found: cursor back in the file' $c2 (Get-Cursor)
    $sentBefore = $script:sent.Count
    Tick
    Check 'cursor not found: the next tick is quiet' $sentBefore $script:sent.Count

    Write-Host ''
    Write-Host 'delivery failures'
    New-Case
    Tick
    $t1 = Add-Rec -Outcome 'discarded'
    $script:respond = { param($m) $transient }
    Tick
    Check 'transient failure: cursor does not move' '' (Get-Cursor)
    Check 'transient failure: no hold' $null (Get-Hold)
    $script:respond = { param($m) $ok }
    Tick
    Check 'transient failure: the next tick sends again' 2 $script:attempts.Count
    Check 'transient failure: the cursor moves once that succeeds' $t1 (Get-Cursor)
    $null = Add-Rec -Outcome 'discarded'
    $t3 = Add-Rec -Outcome 'discarded'
    $script:respond = { param($m) $transient }
    Tick
    Check 'transient failure of a two-record batch: cursor does not move' $t1 (Get-Cursor)
    $script:respond = { param($m) $ok }
    Tick
    Check 'two-record batch: delivered on retry, cursor at its last record' $t3 (Get-Cursor)

    New-Case
    Tick
    $s1 = Add-Rec -Outcome 'discarded'
    for ($k = 0; $k -lt 10; $k++) { Tick -Webhook $false }
    Check 'no webhook: no send attempted' 0 $script:attempts.Count
    Check 'no webhook: cursor does not move' '' (Get-Cursor)
    Check 'no webhook: hold=no-webhook saved' 'no-webhook' "$((Get-Hold).kind)"
    Check 'no webhook: ten held ticks write one log line' 1 @($script:logLines | Where-Object { $_ -like 'discard-alert-held*' }).Count
    Tick
    Check 'webhook configured: the held discard is sent' 1 $script:sent.Count
    Check 'webhook configured: cursor moves' $s1 (Get-Cursor)
    Check 'webhook configured: hold cleared' $null (Get-Hold)

    New-Case
    Tick
    $null = Add-Rec -Outcome 'discarded'
    $script:respond = { param($m) @{ status = 'skipped'; class = 'no-webhook'; http_status = 0 } }
    Tick
    Check 'a skipped send holds and does not move the cursor' 'no-webhook' "$((Get-Hold).kind)"
    Check 'the skipped send left the cursor' '' (Get-Cursor)

    Write-Host ''
    Write-Host 'payload rejection and the fallback'
    New-Case
    Tick
    $null = Add-Rec -Outcome 'discarded'
    $f1 = Add-Rec -Outcome 'discarded' -Suffix 'abcdef012345'
    Tick  # deliver the first, set the cursor
    $script:respond = { param($m) if (Test-IsFallback $m) { $ok } else { $rejected } }
    $f2 = Add-Rec -Outcome 'discarded' -Suffix '0123456789ab' -Thread 'T-f2'
    Tick
    $fb = $script:sent[$script:sent.Count - 1]
    Check 'rejected record: the fallback is delivered' $true (Test-IsFallback $fb)
    Check 'fallback names the records file and line' $true ($fb -match 'see state/clone-retreats\.jsonl line 3, record uuid 0123456789ab')
    Check 'fallback carries the http status' $true ($fb -match 'http=400')
    Check 'fallback delivered: cursor moves' $f2 (Get-Cursor)
    Check 'fallback delivered: logged' 1 @($script:logLines | Where-Object { $_ -like 'discard-alert-fallback-sent*' }).Count

    New-Case
    Tick
    $h1 = Add-Rec -Outcome 'discarded' -Thread 'T-h1'
    $script:respond = { param($m) $rejected }
    Tick
    $hold = Get-Hold
    Check 'message and fallback both rejected: cursor does not move' '' (Get-Cursor)
    Check 'hold=payload-rejected saved' 'payload-rejected' "$($hold.kind)"
    Check 'hold names the one record' $h1 "$(@($hold.record_ids)[0])"
    Check 'hold carries the payload hash' 64 "$($hold.payload_sha256)".Length
    $n = $script:attempts.Count
    for ($k = 0; $k -lt 5; $k++) { Tick }
    Check 'unchanged hash: no send attempted' $n $script:attempts.Count
    Check 'unchanged hash: one log line for the hold' 1 @($script:logLines | Where-Object { $_ -like 'discard-alert-held*' }).Count
    $script:respond = { param($m) $ok }
    Tick -Fingerprint 'fp-2'
    Check 'webhook changed: the hold is lifted and the record sent' $h1 (Get-Cursor)
    Check 'webhook changed: hold cleared' $null (Get-Hold)

    New-Case
    Tick
    $p1 = Add-Rec -Outcome 'discarded'
    $script:respond = { param($m) $rejected }
    Tick
    $n = $script:attempts.Count
    Tick
    Check 'held: no attempt before the payload changes' $n $script:attempts.Count
    # A code fix that changes the fallback (and so the payload) lifts the hold.
    $origFallback = (Get-Command New-CloneDiscardFallback).ScriptBlock
    function New-CloneDiscardFallback { param([int]$HttpStatus, [int]$LineNo, [string]$RecordId) "discard alert rejected by webhook (http=$HttpStatus); fixed, line $LineNo" }
    $script:respond = { param($m) $ok }
    Tick
    Check 'payload changed: sent again and the cursor moves' $p1 (Get-Cursor)
    Set-Item -Path function:New-CloneDiscardFallback -Value $origFallback

    Write-Host ''
    Write-Host 'batch rejection: one record per message'
    New-Case
    Tick
    $A = Add-Rec -Outcome 'discarded' -Thread 'T-AAA'
    $B = Add-Rec -Outcome 'discarded' -Thread 'T-BBB'
    $script:respond = { param($m) if ((Test-IsFallback $m) -or -not $m.Contains('T-AAA')) { $ok } else { $rejected } }
    Tick
    $bOnly = @($script:attempts | Where-Object { $_.text.Contains($B) -and -not $_.text.Contains('T-AAA') })
    Check 'the batch with A and B was tried first' $true ($script:attempts[0].text.Contains($A) -and $script:attempts[0].text.Contains($B))
    Check 'B is delivered in full on its own' 1 $bOnly.Count
    Check 'B full text: its whole record lines' $true ($bOnly[0].text.Contains('discarded by wrapper at end of launch T-BBB (completed; the clone is clean)'))
    Check 'A fell back' 1 @($script:sent | Where-Object { Test-IsFallback $_ }).Count
    Check 'B was sent after the cursor stopped at A' $A $bOnly[0].cursor
    Check 'the cursor then stops after B' $B (Get-Cursor)
    Check 'batch rejection: logged' 1 @($script:logLines | Where-Object { $_ -like 'discard-alert-batch-rejected*' }).Count

    New-Case
    Tick
    $A = Add-Rec -Outcome 'discarded' -Thread 'T-AAA'
    $B = Add-Rec -Outcome 'discarded' -Thread 'T-BBB'
    $script:fallbacks = 0
    $script:respond = {
        param($m)
        if (Test-IsFallback $m) { $script:fallbacks++; if ($script:fallbacks -eq 1) { return $ok } return $transient }
        return $rejected
    }
    Tick
    Check 'A and B both rejected: two fallbacks attempted' 2 @($script:attempts | Where-Object { Test-IsFallback $_.text }).Count
    Check "A's fallback delivered, B's failed: the cursor stops after A" $A (Get-Cursor)
    Check "B's transient failure is no hold" $null (Get-Hold)

    Write-Host ''
    Write-Host 'the fallback text'
    $longRepo = 'C:\' + ('r' * 3000)
    foreach ($case in @(
            @{ name = 'repo_dir of 3,000 characters'; id = "T-x@2026-11-07T01:00:00+00:00#a1b2c3d4e5f6"; repo = $longRepo },
            @{ name = 'non-ASCII repo_dir'; id = "T-x@2026-11-07T01:00:00+00:00#a1b2c3d4e5f6"; repo = 'C:\作業\クローン' },
            @{ name = 'non-ASCII thread_id'; id = "T-日本語スレッド@2026-11-07T01:00:00+00:00#a1b2c3d4e5f6"; repo = 'C:\w' })) {
        $fbText = New-CloneDiscardFallback -HttpStatus 400 -LineNo 12345 -RecordId $case.id
        Check "fallback is ASCII ($($case.name))" $true ($fbText -cmatch '^[\x20-\x7E]+$')
        Check "fallback is under 200 characters ($($case.name))" $true ($fbText.Length -lt 200)
        Check "fallback passes its pre-send check ($($case.name))" $true (Test-CloneDiscardFallbackSafe -Text $fbText -Budget $Budget)
        Check "fallback carries no repo_dir or thread ($($case.name))" $false ($fbText.Contains($case.repo) -or $fbText.Contains('T-'))
    }
    New-Case
    Tick
    $null = Add-Rec -Outcome 'discarded' -Thread 'T-日本語' -RepoDir $longRepo
    $script:respond = { param($m) if (Test-IsFallback $m) { $ok } else { $rejected } }
    Tick
    $fbSent = @($script:sent | Where-Object { Test-IsFallback $_ })
    Check 'through the flow: long non-ASCII record falls back to an ASCII line under 200' $true ($fbSent.Count -eq 1 -and $fbSent[0].Length -lt 200 -and $fbSent[0] -cmatch '^[\x20-\x7E]+$')

    New-Case
    Tick
    $null = Add-Rec -Outcome 'discarded'
    $origCheck = (Get-Command Test-CloneDiscardFallbackSafe).ScriptBlock
    function Test-CloneDiscardFallbackSafe { param([string]$Text, [int]$Budget) return $false }
    $script:respond = { param($m) $rejected }
    Tick
    Check 'failed pre-send check: the fallback is not sent' 0 @($script:attempts | Where-Object { Test-IsFallback $_.text }).Count
    Check 'failed pre-send check: hold=payload-rejected' 'payload-rejected' "$((Get-Hold).kind)"
    Check 'failed pre-send check: cursor does not move' '' (Get-Cursor)
    Set-Item -Path function:Test-CloneDiscardFallbackSafe -Value $origCheck

    Write-Host ''
    Write-Host 'splitting'
    New-Case
    Tick
    $longPath = 'C:\patches\' + (-join (1..5000 | ForEach-Object { [char](97 + ($_ % 26)) }))
    $L = Add-Rec -Outcome 'discarded' -Patch $longPath
    Tick
    $parts = @($script:sent)
    Check 'a 5,000-character patch path is split into several parts' $true ($parts.Count -ge 3)
    Check 'every part is at most 1950 characters' 0 @($parts | Where-Object { $_.Length -gt 1950 }).Count
    Check 'every part carries its (part i/n) marker' $parts.Count @($parts | Where-Object { $_ -match "`n\(part \d+/$($parts.Count)\)`n" }).Count
    $bodies = $parts | ForEach-Object { ($_ -split "`n\(part \d+/\d+\)`n", 2)[1] }
    Check 'the full path can be rebuilt by joining the parts' $true ((-join $bodies).Contains($longPath))
    Check 'the full record_id survives' $true ((-join $bodies).Contains($L))
    Check 'the cursor did not move before the last part' $true (@($script:attempts | Select-Object -First ($parts.Count) | Where-Object { $_.cursor -ne '' }).Count -eq 0)
    Check 'the cursor moves after the last part' $L (Get-Cursor)

    New-Case
    Tick
    $L = Add-Rec -Outcome 'discarded' -Patch $longPath
    $script:calls = 0
    $script:respond = { param($m) $script:calls++; if ($script:calls -eq 2) { return $transient } return $ok }
    Tick
    Check 'part 2 fails: the cursor does not move' '' (Get-Cursor)
    $firstTry = $script:attempts.Count
    Tick
    Check 'next tick resends from part 1' $true ($script:attempts[$firstTry].text -match "`n\(part 1/\d+\)`n")
    Check 'and the cursor moves once every part is delivered' $L (Get-Cursor)

    $msgs = New-CloneDiscardRecordMessages -Prefix (Get-CloneDiscardPrefix -CursorMissing) -Lines @('x' * 9000) -Budget $Budget
    Check 'a part with the cursor-not-found notice is within 1950 too' 0 @($msgs | Where-Object { $_.Length -gt 1950 }).Count
    $emoji = -join (1..3000 | ForEach-Object { [char]::ConvertFromUtf32(0x1F600) })
    $pieces = Split-CloneDiscardText -Text $emoji -Max 101
    Check 'a cut never splits a surrogate pair' 0 @($pieces | Where-Object { [char]::IsHighSurrogate($_[$_.Length - 1]) }).Count
    Check 'cutting drops nothing' $emoji (-join $pieces)

    # --- the real loop wiring (AST) -----------------------------------------------------------------
    Write-Host ''
    Write-Host 'wiring in the real sweep'
    $scriptText = $ast.Extent.Text
    $discardFns = ($functions | Where-Object { $_.Name -in $lifted -and $_.Name -ne 'Save-JsonState' }) | ForEach-Object { $_.Extent.Text }
    Check 'discard alerts never use Send-NotificationIfChanged (#138 always-mark rule)' $false (($discardFns -join "`n") -match 'Send-NotificationIfChanged')
    Check 'discard alerts never post into a thread' $false (($discardFns -join "`n") -match 'chatroom')
    Check 'state path is state\clone-discard-notify.json' $true ($scriptText -match '\$cloneDiscardNotifyStatePath = Join-Path \$dataDir "state\\clone-discard-notify\.json"')
    $posCall = $scriptText.IndexOf('Update-CloneDiscardAlerts -RecordsPath $cloneRetreatRecordsPath -StatePath $cloneDiscardNotifyStatePath')
    $posRetreat = $scriptText.IndexOf('$retreatOutcome = Invoke-LaunchEndRetreat')
    $posFinalSave = $scriptText.LastIndexOf('Save-JsonState -Path $notifyStatePath -State $notifyState')
    Check 'discard alerts run after the launches that write the records' $true ($posRetreat -ge 0 -and $posCall -gt $posRetreat)
    Check 'discard alerts run before the end of the sweep' $true ($posCall -ge 0 -and $posCall -lt $posFinalSave)
    Check 'production passes the real webhook state and budget' $true ($scriptText -match '-WebhookConfigured \(\[bool\]\$notifyWebhook\)' -and $scriptText -match '-Send \{ param\(\$m\) Send-Notification -Message \$m \} -Budget \$DecisionMessageDiscordBudget')
}
finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) FAILED"; exit 1 }
Write-Host "all discard-alert checks passed"
