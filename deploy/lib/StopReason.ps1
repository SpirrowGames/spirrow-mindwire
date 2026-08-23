# deploy/lib/StopReason.ps1 — human-readable phrasing for the conductor's StopReason values,
# plus the Discord notification header builder that uses them.
#
# Why this file exists (T-park-alert-says-judgement-when-it-is-a-fault, msg-1465):
# The Discord push notification's ONE-LINE header used to say a fixed literal "判断待ち"
# regardless of why the conductor actually stopped. Half of the reasons that fire this
# notification are NOT "judgement pending" — they are failures (no_progress_to_human,
# no_handoff_to_human), a runaway backstop (round_cap), or a config mistake (empty_thread) —
# and mis-labelling them as "判断待ち" trained the operator to distrust the notification.
#
# The phrase MAP (Get-StopReasonPhraseMap) is the ONE source of truth for the human wording
# of each reason. Two consumers read from it: the header (New-NotificationHeader) and the
# raw-ping fallback in the sweep body (deploy/run-conductor-scheduled.ps1 §2451). Do NOT
# duplicate the wording anywhere else — the point of centralising it here is that a future
# rewrite of one line updates both call sites at once, and the tests pin ONLY the pointer,
# not the wording (Test-StopReasonPhrase.ps1).
#
# The KEY SET of the map is doubly load-bearing:
#   1. In the sweep body it is the NOTIFICATION PREDICATE — a reason IS in the map iff the
#      operator must hear about it. §4 of the design (msg-1465) is explicit that this set
#      must NOT be narrowed. See also the comment above the $needsHuman assignment in
#      deploy/run-conductor-scheduled.ps1 (the 2026-08-02 first-live-sweep incident).
#   2. Here it is the KNOWN-REASON SET the header uses to pick a phrase.
# Get-StopReasonPhrase's fail-open default (unknown → warning phrase, no throw) is what makes
# adding a new StopReason to conductor/core.py without updating this map a LOUD failure at
# the notification surface instead of a silent regression back to "判断待ち" — the whole
# problem this file exists to end.
#
# NO TOP-LEVEL SIDE EFFECTS: this file is dot-sourced by both the runner and the tests, so
# any assignment at script scope here would mutate the caller's scope. Do NOT set
# $ErrorActionPreference here (PR-gate finding on #172): the runner already declares its own
# preference at deploy/run-conductor-scheduled.ps1 L58, tests declare theirs at the top of
# each test file, and the functions below use only hashtable lookups and string concatenation
# — operations whose failure modes are already terminating regardless of the preference.
# If a future function here ever adds a non-terminating call that MUST hard-fail, wrap that
# call in its own `try / catch { throw }` scoped to the function body, do not lift the
# preference into script scope.

function Get-StopReasonPhraseMap {
    # KEY SET = the sweep's notification predicate (see file header note 1).
    # VALUES  = human-readable phrasing shown in the Discord header AND in the raw-ping fallback.
    # Returns a FRESH hashtable on every call, so mutation by one caller cannot leak into another.
    # The map's contents mirror the $needsHuman literal that used to live in
    # deploy/run-conductor-scheduled.ps1 verbatim; the migration must not change any wording.
    return @{
        'human'                = "あなたの判断待ちで停止しました"
        'no_handoff_to_human'  = "NEXT: が読めず human に fallback して停止しました"
        'no_progress_to_human' = "dispatch した role が何も投稿せず停止しました"
        'round_cap'            = "ラウンド上限で停止しました（暴走バックストップ発動）"
        'empty_thread'         = "スレッドにメッセージがありません（優先リストの指定ミスの可能性）"
    }
}

function Get-StopReasonPhrase {
    # Look up the human-readable phrase for a StopReason value.
    #
    # Fail-open by design (Bohr msg-1466 D-2, Einstein msg-1467 §1): an UNKNOWN reason must
    # not throw and must not silently fall back to "判断待ち". Throwing here would swallow the
    # notification and reproduce the "silently nothing happens" mode this whole subsystem
    # exists to end; falling back to "判断待ち" would re-introduce the label the operator
    # learned to distrust. Instead, DEGRADE LOUDLY to a phrase that names the drift itself —
    # the operator sees a strange-looking header, the raw reason is still present in
    # `reason=…`, and the mismatch is unmissable in Discord's push preview.
    #
    # The default phrase MUST NOT contain the substring "判断待ち" (pinned in
    # Test-StopReasonPhrase.ps1). Empty / $null $StopReason takes the same default path.
    param([string]$StopReason)

    $map = Get-StopReasonPhraseMap
    if (-not [string]::IsNullOrEmpty($StopReason) -and $map.ContainsKey($StopReason)) {
        return $map[$StopReason]
    }
    return "未知の停止理由で停止しました（通知側がこの reason を知りません）"
}

function New-NotificationHeader {
    # Build the one-line notification header the operator reads in Discord's push preview.
    # Format:
    #   MindWire: **<ThreadId>** (<Project>) — <phrase> (reason=<StopReason>, rounds=<Rounds>, <LastMsgId>)
    #
    # Preserved BYTE-FOR-BYTE from the pre-change header (which used the literal "判断待ち"
    # instead of <phrase>) so the rest of the message-building pipeline — the dashboard link
    # appended right after, the D-9 truncation ladder, the D-29 header-and-link-always-survive
    # invariant — sees a shape identical to before. The ONLY difference is the label between
    # the em-dash and the opening parenthesis. Do NOT reorder these fields without checking
    # what depends on the order (`reason=` before `rounds=` is what one grep in the daily
    # digest keys on).
    #
    # Signature is deliberately verbose (five named params) rather than accepting a bag of
    # fields — the pin tests want to be able to vary each one independently.
    param(
        [string]$ThreadId,
        [string]$Project,
        [string]$StopReason,
        [int]$Rounds,
        [string]$LastMsgId
    )

    $phrase = Get-StopReasonPhrase -StopReason $StopReason
    return "MindWire: **$ThreadId** ($Project) — $phrase (reason=$StopReason, rounds=$Rounds, $LastMsgId)"
}
