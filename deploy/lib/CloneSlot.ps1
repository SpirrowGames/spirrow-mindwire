# deploy/lib/CloneSlot.ps1 — the sweep's side of the clone slot pool
# (T-ephemeral-impl-clone-slot-per-thread A3: msg-8047 D-4, Bohr msg-9386 D-1 改訂 2/4/6/7,
# msg-9395 Obj-35-1, msg-9400 §2, msg-9402 Obj-36-3/36-4, msg-9754 §1-§4).
#
# Pure functions only: what `mindwire slot-prep` / `mindwire slot-finish` printed and exited with
# is turned into a decision (launch / wait / park) and a list of operator notices. The sweep does
# the side effects (the bounded probe, the per-thread quarantine record, Send-NotificationIfChanged)
# so tests/Test-CloneSlot.ps1 can exercise every branch without git, uv or Discord.
#
# Slot mode is on only when [loop].slots_root is set in mindwire.toml (accepted since A4, off by
# default): with no slots_root the sweep keeps the shared-clone path byte-for-byte
# (Get-SlotModeConfig returns $null).
#
# Notice reader and fallback surface (OBL-CHATROOM-PRODUCER-READER-SURFACE): nothing here posts to
# a chatroom thread. The reader of every notice is the operator who has to look at a slot; the
# surface is the sweep's operator notification (Discord), deduplicated by key + signature. The
# durable record behind each one is written by Python before the notice is built: the
# slot_quarantine rows in state/clone-retreats.jsonl, the slot_turn_result rows in the
# continuation ledger, and state/slot-leases.json for leases that are kept.

# Exit codes of `mindwire slot-prep` / `mindwire slot-finish` (src/spirrow_mindwire/clone_slot_cli.py).
$SlotPrepReadyExitCode = 0
$SlotPrepWaitingExitCode = 3
$SlotParkedExitCode = 4
$SlotFinishRefusedExitCode = 2
$SlotFinishQuarantinedExitCode = 5
# Park code written by the sweep (not by Python) when slot-prep failed or printed nothing it can
# read: this thread only, on this tick (clone_slot.PARK_PREP_CRASHED).
$SlotPrepCrashedParkCode = 'clone_slot_prep_crashed'
$SlotUnrecoverableParkCode = 'clone_slot_unrecoverable'
# A4: the conductor refused a CONTINUE in slot mode (slot_continuation.PARK_CODE).
$SlotContinueRefusedParkCode = 'clone_slot_continue_refused'

# [loop].slots_root and [loop].slots_per_repo from mindwire.toml, line-oriented like
# Get-ConductorHardBudgetSeconds. $null when the file or the key is missing or slots_root is empty:
# the shared-clone path. slots_per_repo defaults to 1 (config.LoopSettings.resolved_slots_per_repo).
function Get-SlotModeConfig {
    param([string]$ConfigPath)
    if (-not $ConfigPath -or -not (Test-Path -LiteralPath $ConfigPath)) { return $null }
    $section = ''
    $root = $null
    $perRepo = 1
    foreach ($line in [System.IO.File]::ReadAllLines($ConfigPath)) {
        if ($line -match '^\s*\[([^\]]+)\]\s*$') { $section = $Matches[1]; continue }
        if ($section -ne 'loop') { continue }
        if ($line -match '^\s*slots_root\s*=\s*"(.*)"\s*(#.*)?$') { $root = $Matches[1] }
        elseif ($line -match '^\s*slots_per_repo\s*=\s*(\d+)\s*(#.*)?$') { $perRepo = [int]$Matches[1] }
    }
    if (-not $root) { return $null }
    return @{ slots_root = $root; slots_per_repo = [Math]::Max(1, $perRepo) }
}

# The slot pool's repo name: the leaf of the candidate's own clone path (A1 path functions use it as
# <slots_root>/<repo>/slot-NN). Separators of either kind are accepted.
function Get-SlotRepoName {
    param([string]$RepoDir)
    $trimmed = "$RepoDir".TrimEnd('\', '/')
    return ($trimmed -split '[\\/]')[-1]
}

# A turn id for one launch: no '/', a valid ref component (rescue/<thread>/<turn>/<NN>, msg-9404).
function New-SlotTurnId {
    param([datetime]$NowUtc)
    $rand = [guid]::NewGuid().ToString('N').Substring(0, 8)
    return ('{0}-{1}' -f $NowUtc.ToString('yyyyMMddTHHmmssZ'), $rand)
}

# ConvertFrom-Json, but timestamps stay text: pwsh turns ISO strings into [datetime], which would
# make a notice signature (waiting_since, ...) depend on the host's locale. -DateKind String exists
# from pwsh 7.5; on an older host every [datetime] is written back as round-trip ISO text.
function ConvertFrom-SlotJson {
    param([string]$Text)
    $opts = @{ ErrorAction = 'Stop' }
    if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) { $opts.DateKind = 'String' }
    $obj = $Text | ConvertFrom-Json @opts
    $fix = {
        param($o)
        if ($o -is [System.Management.Automation.PSCustomObject]) {
            foreach ($prop in @($o.PSObject.Properties)) {
                if ($prop.Value -is [datetime]) { $prop.Value = $prop.Value.ToUniversalTime().ToString('o') }
                else { & $fix $prop.Value }
            }
        }
        elseif ($o -is [System.Array]) { foreach ($e in $o) { & $fix $e } }
    }
    & $fix $obj
    return $obj
}

# The one MINDWIRE_<Prefix> json row of a probe's stdout, parsed; $null when absent or unreadable.
function Get-SlotRow {
    param([string]$Stdout, [string]$Prefix)
    $row = $null
    foreach ($line in @("$Stdout" -split "`r?`n")) {
        if ($line.StartsWith($Prefix)) {
            try { $row = ConvertFrom-SlotJson -Text $line.Substring($Prefix.Length) } catch { $row = $null }
        }
    }
    return $row
}

function New-SlotNotice {
    param([string]$Key, [string]$Signature, [string]$Message)
    return @{ key = $Key; signature = $Signature; message = $Message }
}

# Notices for leases slot-prep reported as kept (msg-9395 Obj-35-1 §2, msg-9402 Obj-36-4): a wrapper
# that died without returning its slot (job_containment_unverified — Job containment of the --bg
# session is not confirmed, so there is no automatic reclaim) or a slot whose quarantine failed.
# One notice per (repo, slot, turn, reason): it does not repeat every tick.
function Get-SlotKeptNotices {
    param($Row, [string]$Repo)
    $out = @()
    if ($null -eq $Row -or $null -eq $Row.kept) { return $out }
    foreach ($k in @($Row.kept)) {
        $reason = "$($k.reason)"
        $msg = "MindWire: clone slot **$Repo/$($k.slot)** は返却されないまま残っています (reason=$reason, thread=$($k.thread_id), turn=$($k.turn_id), state=$($k.state))。"
        if ($reason -eq 'job_containment_unverified') {
            $msg += "wrapper が slot を返さずに終了しました。セッションが Job 内にあると確認できないため自動回収しません。プロセスが残っていないことを確認してから lease を手で外してください。"
        } else {
            $msg += "終了時の隔離 (rename) に失敗した slot で、二度と貸し出しません。slot のディレクトリを確認してください。"
        }
        $msg += " 同じ repo の他スレッドは残りの slot を使うか待ちます。"
        $out += New-SlotNotice -Key "__slot_kept__/$Repo/$($k.slot)" -Signature "$($k.turn_id):$reason" -Message $msg
    }
    return $out
}

# PR #594 gate Obj-1-1: a turn whose end is not confirmed — exit 7 (the hard budget's kill could
# not be confirmed) or a throw out of the run — may have left a session still writing in the slot.
# slot-finish does not run then: a save check proves nothing about a directory still in use, a
# release would lend it to the next borrower, and a rescue failure would rename it under the
# session. The lease stays held (this wrapper's pid); once the wrapper is gone, slot-prep reports it
# as kept with job_containment_unverified, and the operator removes it after checking that no
# process is left. Same key and signature as that kept notice, so the operator is told once.
function Get-SlotRetainedNotice {
    param([string]$Repo, [string]$SlotName, [string]$Slot, [string]$TurnId, [string]$ThreadId,
          [string]$CandidateKey, [string]$Cause)
    $msg = ("MindWire: **$CandidateKey** のターンは終了を確認できませんでした ($Cause)。" +
            "セッションが残っている可能性があるため、slot $Repo/$SlotName ($Slot) の保存確認・返却・隔離はしていません。" +
            "lease は保持したままで、二度目以降の貸し出しもしません (reason=job_containment_unverified, thread=$ThreadId, turn=$TurnId)。" +
            "プロセスが残っていないことを確認してから lease を手で外してください。同じ repo の他スレッドは残りの slot を使うか待ちます。")
    return New-SlotNotice -Key "__slot_kept__/$Repo/$SlotName" -Signature "$($TurnId):job_containment_unverified" -Message $msg
}

# What the sweep does with one slot-prep run. Returns
#   @{ action = 'launch' | 'wait' | 'park'; slot; slot_name; turn_id; park_code; detail; notices }
# * exit 0 with a row naming the slot: launch, with the turn's repo_dir = the slot;
# * exit 3: wait — not launched and not parked (msg-9386 改訂 4); one notice once the wait passed
#   30 minutes (the row's wait_notice is true exactly once per wait);
# * exit 4: park this thread only with the row's park_code (clone_slot_unrecoverable,
#   workspace-untrusted, clone_slot_provision_failed);
# * anything else, or a row that cannot be read: park with clone_slot_prep_crashed.
# A borrow-time quarantine whose rescue of the previous borrower failed gets its own notice
# (msg-9386 改訂 6), whatever the action.
function Get-SlotPrepDecision {
    param([int]$Code, $Row, [string]$Repo, [string]$CandidateKey, [string]$Stderr = '')
    $notices = @()
    if ($null -ne $Row) { $notices += @(Get-SlotKeptNotices -Row $Row -Repo $Repo) }
    $q = if ($null -ne $Row) { $Row.quarantine } else { $null }
    if ($null -ne $q -and "$($q.rescue_outcome)" -eq 'rescue_failed') {
        $notices += New-SlotNotice -Key "__slot_borrow_rescue_failed__/$Repo/$($Row.slot_name)" `
            -Signature "$($q.quarantine_path):$($q.previous_thread)" `
            -Message ("MindWire: clone slot **$Repo/$($Row.slot_name)** を借りる前の救出 push に失敗しました。" +
                      "前の借り手 thread=$($q.previous_thread) の作業は push できていません。" +
                      "隔離先: $($q.quarantine_path) (reason=$($q.reason))。詳細: $($q.rescue_detail)")
    }
    $base = @{ slot = $null; slot_name = $null; turn_id = $null; park_code = $null; detail = ''; notices = $notices }
    if ($Code -eq $SlotPrepReadyExitCode -and $null -ne $Row -and $Row.slot) {
        $base.action = 'launch'; $base.slot = "$($Row.slot)"; $base.slot_name = "$($Row.slot_name)"
        $base.turn_id = "$($Row.turn_id)"
        return $base
    }
    if ($Code -eq $SlotPrepWaitingExitCode -and $null -ne $Row) {
        $base.action = 'wait'; $base.detail = "$($Row.waiting_reason)"
        if ($Row.wait_notice -eq $true) {
            $base.notices += New-SlotNotice -Key "__slot_wait__/$CandidateKey" -Signature "$($Row.waiting_since)" `
                -Message ("MindWire: **$CandidateKey** は $Repo の空き clone slot を 30 分以上待っています " +
                          "(reason=$($Row.waiting_reason), since=$($Row.waiting_since))。park はしていません。" +
                          "slot が返却されない理由を slot-leases.json で確認してください。")
        }
        return $base
    }
    $base.action = 'park'
    if ($Code -eq $SlotParkedExitCode -and $null -ne $Row -and $Row.park_code) {
        $base.park_code = "$($Row.park_code)"; $base.detail = "$($Row.detail)"
        $base.slot = if ($Row.slot) { "$($Row.slot)" } else { $null }
    } else {
        $base.park_code = $SlotPrepCrashedParkCode
        $base.detail = "slot-prep exit=$Code $Stderr".Trim()
    }
    $where = if ($base.slot) { $base.slot } else { $Repo }
    $how = if ($base.park_code -eq 'workspace-untrusted') { " docs/deploy.md の手順で slot の path に trust を付けてください。" } else { '' }
    $base.notices += New-SlotNotice -Key "__slot_park__/$CandidateKey" -Signature "$($base.park_code):$where" `
        -Message ("MindWire: **$CandidateKey** を clone slot の準備で park しました (reason=$($base.park_code))。" +
                  "path: $where。$($base.detail)$how 他のスレッドは止めていません。")
    return $base
}

# What the sweep does with one slot-finish run (msg-9400 §2, msg-9402 Obj-36-4). Returns
#   @{ park_code (or $null); outcome; notices }
# * exit 0: released, nothing to say;
# * exit 5: rescue_failed — the slot was renamed into quarantine and returned; notice with the
#   quarantine path, the thread and the reason. The thread is not parked here: whether it may
#   continue is the conductor's ledger decision (A4);
# * exit 4: rescue_failed and every rename failed — the lease stays quarantine_failed; park this
#   thread only with clone_slot_unrecoverable; notice with the slot's own path;
# * exit 2: the lease was not this turn's; nothing was touched; notice;
# * anything else: slot-finish did not complete. The lease stays held (it is never released as
#   saved); notice. The next slot-prep reports it as kept once this wrapper is gone.
function Get-SlotFinishDecision {
    param([int]$Code, $Row, [string]$Repo, [string]$SlotName, [string]$CandidateKey, [string]$Stderr = '')
    $n = if ($null -ne $Row) { $Row.notice } else { $null }
    $res = @{ park_code = $null; outcome = 'failed'; notices = @() }
    if ($Code -eq 0 -and $null -ne $Row) { $res.outcome = "$($Row.outcome)"; return $res }
    if ($Code -eq $SlotFinishQuarantinedExitCode -and $null -ne $Row -and $null -eq $n -and
        "$($Row.outcome)" -eq 'quarantined' -and "$($Row.turn_outcome)" -in @('pushed', 'rescued')) {
        # A5 D-5: a guard-refused slot (exit 8) quarantined with its work saved needs nobody.
        $res.outcome = 'quarantined'
        return $res
    }
    if ($Code -eq $SlotFinishQuarantinedExitCode -and $null -ne $n) {
        $res.outcome = 'quarantined'
        $res.notices += New-SlotNotice -Key "__slot_finish__/$CandidateKey" -Signature "$($n.path)" `
            -Message ("MindWire: **$CandidateKey** のターン終了時に作業を remote へ保存できませんでした (reason=$($n.reason))。" +
                      "slot $Repo/$SlotName は隔離しました: $($n.path)。詳細: $($n.detail)")
        return $res
    }
    if ($Code -eq $SlotParkedExitCode -and $null -ne $n) {
        $res.outcome = 'parked'; $res.park_code = "$($n.reason)"
        $res.notices += New-SlotNotice -Key "__slot_finish__/$CandidateKey" -Signature "$($n.reason):$($n.path)" `
            -Message ("MindWire: **$CandidateKey** を park しました (reason=$($n.reason))。ターン終了時の保存と隔離 (rename) の両方に失敗し、" +
                      "slot は元の場所に残っています: $($n.path)。この slot は二度と貸し出しません。詳細: $($n.detail)")
        return $res
    }
    if ($Code -eq $SlotFinishRefusedExitCode) {
        $res.outcome = 'refused'
        $detail = if ($null -ne $Row) { "$($Row.detail)" } else { $Stderr }
        $res.notices += New-SlotNotice -Key "__slot_finish__/$CandidateKey" -Signature "refused:$SlotName" `
            -Message ("MindWire: **$CandidateKey** の slot $Repo/$SlotName を返却できませんでした (lease がこのターンのものではありません)。" +
                      "slot には触れていません。詳細: $detail")
        return $res
    }
    $res.notices += New-SlotNotice -Key "__slot_finish__/$CandidateKey" -Signature "failed:$SlotName" `
        -Message ("MindWire: **$CandidateKey** のターン終了処理 (slot-finish) が完了しませんでした (exit=$Code)。" +
                  "slot $Repo/$SlotName の lease は保存確認前のまま残しています。$Stderr")
    return $res
}

# A5 D-5: the clone guard's reason in an exit-8 run's MINDWIRE_DIRTY_CLONE_PAYLOAD row (the last such
# row), or 'unknown' when there is none or it does not parse. slot-finish maps it to a D-7 reason
# (an unknown one becomes `modified`); the slot is quarantined either way.
function Get-DirtyClonePayloadReason {
    param([string]$Output)
    $row = Get-SlotRow -Stdout $Output -Prefix 'MINDWIRE_DIRTY_CLONE_PAYLOAD '
    if ($null -ne $row -and $row.reason -and "$($row.reason)" -match '^[a-z_]+$') { return "$($row.reason)" }
    return 'unknown'
}

# A4 (msg-9400 §3): the conductor's exit-14 run. Its MINDWIRE_SLOT_CONTINUE_REFUSED row says why the
# thread's CONTINUE was refused (reason = not_saved / no_record / lease_held) and where the work is
# (path = the quarantine target, the slot still leased, or the slot whose quarantine failed).
# Returns @{ park_code; path; detail; notice }. An unreadable row still parks (same code): the
# refusal is the exit code, the row only says where to look.
function Get-SlotContinueRefusedDecision {
    param([string]$Output, [string]$CandidateKey)
    $row = Get-SlotRow -Stdout $Output -Prefix 'MINDWIRE_SLOT_CONTINUE_REFUSED '
    $path = if ($null -ne $row -and $row.path) { "$($row.path)" } else { '(unknown: no readable row)' }
    $reason = if ($null -ne $row -and $row.reason) { "$($row.reason)" } else { 'unknown' }
    $detail = if ($null -ne $row) { "$($row.detail)" } else { 'the conductor exited 14 without a readable row' }
    $turn = if ($null -ne $row -and $row.turn_id) { "$($row.turn_id)" } else { '-' }
    $msg = ("MindWire: **$CandidateKey** の CONTINUE を続行しませんでした (reason=$SlotContinueRefusedParkCode/$reason, turn=$turn)。" +
            "前のターンの終了処理が作業を remote に保存したと記録していないためです。作業の場所: $path。詳細: $detail " +
            "このスレッドだけを park しました。同じ repo の他スレッドは止めていません。")
    return @{ park_code = $SlotContinueRefusedParkCode; path = $path; detail = $detail
              notice = (New-SlotNotice -Key "__slot_continue__/$CandidateKey" -Signature "$($reason):$($turn):$path" -Message $msg) }
}
