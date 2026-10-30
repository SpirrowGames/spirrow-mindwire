# ruff: noqa: RUF001, E501 - posted Japanese text: fullwidth punctuation and wide lines on purpose
"""The conductor's operator pass (D1', D3'' - D3⁸, D5, D2⁶ / D2⁷).

The ``MindWire-Conductor`` wrapper calls this once per tick, after its head probe and before its
own sweep (D1': dispatch is decided by the conductor's tick and nothing else). One pass does at
most ONE of these, in this order:

1. If a lock exists: leave a live one alone; otherwise settle it — reclaim an expired manual lease;
   for a conductor run, a failed spawn is retried (2 attempts in total, D5) and a finished or dead
   run goes through the completion table below.
2. Otherwise dispatch at most one task: pick the first operator head (D5), mark it ``dispatched``
   in ``heads`` (fail-closed: before anything starts), create the ``spawning`` lock, start the
   runner detached, and watch the lock for up to 30 s (D3⁶) so a runner that dies at once is
   settled in this tick.

Completion table (D2⁷; rows read top-down; the lock's ``result`` decides, ``posted()`` never does):

=========================================  ===========================  ===========================
lock ``result``                            ``<run_id>.md``              outcome
=========================================  ===========================  ===========================
``posted`` with ``msg_id``                 —                            done
``post_failed`` / none                     present, validates           re-post
``post_failed`` / none                     present, does not validate   quarantine (file path)
``invalid_report`` / ``session_failed`` /  absent (``.tmp`` only too)    quarantine
none
=========================================  ===========================  ===========================

``posted(run_id)`` is consulted only right before a re-post or a quarantine post, to avoid posting
twice. A re-post that fails keeps the lock, so the next tick tries once more (once per tick).

Slow work (chatroom reads and posts, the guard) runs OUTSIDE the mutex: a hand-driven
``lock acquire`` gives up on the mutex after 10 s, and must not lose to a network call.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..conductor.head_skip import record_from_json
from ..magickit.client import ThreadResolvedError
from . import report as rpt
from .chat import Chat
from .config import LaneConfig, disable, load_config
from .guard import OPEN, GuardVerdict
from .runner import is_operator_task_head
from .session import Spawner
from .state import (
    HOLDER_CONDUCTOR,
    HOLDER_MANUAL,
    RESULT_INVALID_REPORT,
    RESULT_POST_FAILED,
    RESULT_POSTED,
    RESULT_SESSION_FAILED,
    STATE_RUNNING,
    STATE_SPAWNING,
    HeadsUnavailableError,
    LanePaths,
    LockCorruptError,
    MutexTimeoutError,
    ProcessProbe,
    conductor_lock,
    create_lock,
    describe_lock,
    first_enable,
    head_key,
    hold_mutex,
    initialise,
    iso,
    load_heads,
    process_alive,
    read_lock,
    reclaim_lock,
    record_abort,
    replace_lock,
    save_heads,
    stale_reason,
)

#: D3⁶: how long the pass watches a fresh runner before moving on.
SPAWN_WATCH = timedelta(seconds=30)
SPAWN_WATCH_POLL_SEC = 0.5
#: D2⁶: finished reports are kept this long under ``done\``.
DONE_RETENTION = timedelta(days=7)
#: Operator heads whose thread is read per pass (each read is one chatroom call).
MAX_HEAD_READS = 3

#: Pre-screen (D4): words that mean the task needs a person or a Tier-C decision. A hit posts a
#: needs-hands report without starting a session. Whether it is Tier-C is the person's call; the
#: lane only declines to try.
PRESCREEN_WORDS = (
    "deploy",
    "デプロイ",
    "merge",
    "マージ",
    "再ログオン",
    "ログオン",
    "再ログイン",
    "logon",
    "login",
    "認証",
    "uac",
    "gui",
)

OPERATOR_TOKEN = "operator"


@dataclass
class PassDeps:
    paths: LanePaths
    chat: Chat
    spawner: Spawner
    probe: ProcessProbe
    guard: Callable[[LaneConfig], GuardVerdict]
    clock: Callable[[], datetime]
    wrapper_pid: int
    head_skip_path: Path
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    new_run_id: Callable[[], str] = field(default=lambda: uuid.uuid4().hex[:16])


@dataclass
class PassResult:
    action: str
    events: list[dict[str, str]] = field(default_factory=list)

    def notify(self, key: str, message: str) -> None:
        self.events.append({"level": "notify", "key": key, "message": message})

    def log(self, message: str) -> None:
        self.events.append({"level": "log", "key": "", "message": message})

    def to_json(self) -> str:
        return json.dumps({"action": self.action, "events": self.events}, ensure_ascii=False)


def run_pass(candidates: Sequence[dict[str, str]], deps: PassDeps) -> PassResult:
    try:
        config = load_config(deps.paths.config)
    except ValueError as exc:
        res = PassResult("config_invalid")
        res.notify(
            "operator-lane:config",
            f"operator lane の設定が読めません（{exc}）。lane は止まっています。",
        )
        return res
    if not config.enabled:
        return PassResult("disabled")
    try:
        return _run_pass(config, candidates, deps)
    except MutexTimeoutError as exc:
        res = PassResult("mutex_timeout")
        res.log(f"operator pass skipped this tick: {exc}")
        return res


def _run_pass(
    config: LaneConfig, candidates: Sequence[dict[str, str]], deps: PassDeps
) -> PassResult:
    paths = deps.paths
    with hold_mutex(paths.mutex):
        if first_enable(paths):
            initialise(paths)
        try:
            heads = load_heads(paths)
        except HeadsUnavailableError as exc:
            res = PassResult("heads_unavailable")
            res.notify(
                "operator-lane:heads",
                f"operator lane の状態ファイルが読めません（{exc}）。二重実行を避けるため dispatch を止めています。"
                "各スレッドの head を確かめてから `python -m spirrow_mindwire.operator_lane reset` を実行してください。",
            )
            return res
        try:
            lock = read_lock(paths)
        except LockCorruptError as exc:
            res = PassResult("lock_corrupt")
            res.notify(
                "operator-lane:lock",
                f"operator lane の lock が壊れています（{exc}）。確かめてから `lock release --force` で解放してください。",
            )
            return res
    _purge_done(paths, deps.clock())
    if lock is not None:
        return _settle(config, lock, deps)
    return _dispatch(config, candidates, heads, deps)


# --- settling an existing lock -----------------------------------------------------------------


def _settle(config: LaneConfig, lock: dict[str, Any], deps: PassDeps) -> PassResult:
    pending = lock.get("quarantine_pending")
    if isinstance(pending, list):
        # A quarantine was decided on an earlier tick and its post has not landed yet.
        return _quarantine(lock, [str(f) for f in pending], deps, PassResult("quarantined"))
    now = deps.clock()
    reason = stale_reason(lock, now, deps.probe)
    if reason is None:
        res = PassResult("busy")
        res.log(f"operator lane busy: {describe_lock(lock)}")
        return res
    if lock.get("holder") == HOLDER_MANUAL:
        with hold_mutex(deps.paths.mutex):
            current = read_lock(deps.paths)
            if current is None or current.get("lease_id") != lock.get("lease_id"):
                return PassResult("raced")
            reclaim_lock(deps.paths, now)
        res = PassResult("manual_lease_reclaimed")
        res.notify(
            f"operator-lane:lease:{lock.get('lease_id')}",
            f"手動 operator の lease が期限切れになったので回収しました（{describe_lock(lock)}）。",
        )
        return res
    if lock.get("state") == STATE_SPAWNING:
        return _settle_spawn_failure(config, lock, deps, reason)
    return _settle_finished(config, lock, deps)


def _settle_spawn_failure(
    config: LaneConfig, lock: dict[str, Any], deps: PassDeps, reason: str
) -> PassResult:
    """D3⁵ / D3⁶: no session was started. Retry, unless the cause says not to."""
    now = deps.clock()
    run_id = str(lock.get("run_id"))
    abort = str(lock.get("abort_reason") or "")
    key = head_key(str(lock.get("project")), str(lock.get("thread_id")))
    res = PassResult("spawn_failed")
    quarantine = False
    with hold_mutex(deps.paths.mutex):
        current = read_lock(deps.paths)
        if current is None or current.get("run_id") != run_id:
            return PassResult("raced")
        heads = load_heads(deps.paths)
        entry = dict(heads.get(key) or {})
        if abort.startswith(f"guard_{OPEN}"):
            entry["dispatched"] = False
            disable(deps.paths.config, reason=abort, now=now)
            res.action = "guard_open"
            res.notify(
                "operator-lane:guard",
                f"operator lane の隔離ガードが開いています（{abort}）。lane を無効にしました。直したら "
                "operator_lane.json の enabled を手で true に戻してください。",
            )
        elif abort.startswith("guard_") or abort.startswith("thread_unreadable"):
            entry["dispatched"] = False
            res.action = "spawn_deferred"
            res.log(f"operator run {run_id} not started this tick: {abort}")
        elif abort.startswith("head_moved"):
            res.action = "head_moved"
            res.log(f"operator run {run_id} dropped: {abort}")
        else:
            attempts = int(entry.get("spawn_attempts") or 0) + 1
            entry["spawn_attempts"] = attempts
            quarantine = attempts >= config.max_spawn_attempts
            entry["dispatched"] = quarantine
            res.log(
                f"operator run {run_id} failed to start ({abort or reason}); attempt {attempts}/"
                f"{config.max_spawn_attempts}"
            )
        heads[key] = entry
        save_heads(deps.paths, heads)
        if not quarantine:
            reclaim_lock(deps.paths, now)
            return res
        facts = [
            f"lane run {run_id} の runner が {config.max_spawn_attempts} 回続けて起動できませんでした"
            f"（最後の理由: {abort or reason}）。セッションは起動していません。",
            f"runner のログ: {deps.paths.logs / (run_id + '.runner.log')}",
        ]
        # Recorded before posting: a failed post retries the post next tick, never re-counts.
        current["quarantine_pending"] = facts
        replace_lock(deps.paths, current)
    return _quarantine(current, facts, deps, res)


def _settle_finished(config: LaneConfig, lock: dict[str, Any], deps: PassDeps) -> PassResult:
    """D2⁷'s completion table for a ``running`` lock whose runner finished or died."""
    run_id = str(lock.get("run_id"))
    result = lock.get("result") if isinstance(lock.get("result"), dict) else None
    kind = result.get("kind") if result else None
    report_path = deps.paths.report(run_id)
    if kind == RESULT_POSTED and result and result.get("msg_id"):
        res = PassResult("done")
        res.log(f"operator run {run_id} posted {result.get('msg_id')}")
        return _complete(lock, deps, res)
    if kind in (RESULT_POST_FAILED, None) and report_path.exists():
        text = report_path.read_text(encoding="utf-8")
        problems = rpt.validate_report(
            text, run_id=run_id, requester=str(lock.get("requester") or "")
        )
        if not problems:
            return _repost(lock, text, deps)
        facts = [
            f"report ファイルの検証に失敗しました: {report_path}（{'; '.join(problems)}）",
            f"lock の result: {json.dumps(result, ensure_ascii=False) if result else 'なし'}",
        ]
        return _quarantine(lock, facts, deps, PassResult("quarantined"))
    facts = [
        f"lock の result: {json.dumps(result, ensure_ascii=False) if result else 'なし（runner が result を書く前に終了）'}"
    ]
    if kind == RESULT_SESSION_FAILED and result:
        if result.get("timed_out"):
            facts.append(f"セッションが {config.session_timeout_sec} 秒で打ち切られました。")
        else:
            facts.append(f"セッションの終了コード: {result.get('exit_code')}")
    if kind == RESULT_INVALID_REPORT and result:
        facts.append(f"report の形式の問題: {'; '.join(result.get('problems') or [])}")
    tmp = deps.paths.report_tmp(run_id)
    if tmp.exists():
        facts.append(f"書きかけの report（判定には使いません）: {tmp}")
    facts.append(f"セッションのログ: {deps.paths.logs}\\{run_id}.session.jsonl")
    return _quarantine(lock, facts, deps, PassResult("quarantined"))


def _repost(lock: dict[str, Any], text: str, deps: PassDeps) -> PassResult:
    run_id = str(lock.get("run_id"))
    project, thread_id = str(lock.get("project")), str(lock.get("thread_id"))
    head_msg_id = str(lock.get("head_msg_id"))
    res = PassResult("reposted")
    try:
        messages = deps.chat.messages(project, thread_id)
        if rpt.posted(
            messages, head_msg_id=head_msg_id, run_id=run_id, operator_author=_author(deps)
        ):
            res.action = "done"
            res.log(f"operator run {run_id}: report already in the thread; not re-posting")
            return _complete(lock, deps, res)
        final = rpt.with_late_report(
            text, head_msg_id, len(rpt.messages_after(messages, head_msg_id))
        )
        problems = rpt.validate_report(
            final, run_id=run_id, requester=str(lock.get("requester") or "")
        )
        if problems:
            facts = [f"再投稿用の report が検証を通りません（{'; '.join(problems)}）"]
            return _quarantine(lock, facts, deps, PassResult("quarantined"))
        msg_id = deps.chat.post(project, thread_id, final)
    except ThreadResolvedError as exc:
        return _thread_gone(lock, deps, exc)
    except Exception as exc:
        res.action = "repost_failed"
        res.log(f"operator run {run_id}: re-post failed ({exc}); next tick tries again")
        return res
    res.log(f"operator run {run_id}: report re-posted as {msg_id}")
    return _complete(lock, deps, res)


def _quarantine(
    lock: dict[str, Any], facts: list[str], deps: PassDeps, res: PassResult
) -> PassResult:
    """D5: post the lane's own stop and hand the task to a person. Never retried as a session."""
    run_id = str(lock.get("run_id"))
    project, thread_id = str(lock.get("project")), str(lock.get("thread_id"))
    head_msg_id = str(lock.get("head_msg_id"))
    res.action = "quarantined"
    try:
        messages = deps.chat.messages(project, thread_id)
        if rpt.posted(
            messages, head_msg_id=head_msg_id, run_id=run_id, operator_author=_author(deps)
        ):
            res.log(f"operator run {run_id}: a post of this run is already in the thread")
            return _complete(lock, deps, res)
        text = rpt.render_quarantine(
            run_id=run_id,
            head_msg_id=head_msg_id,
            operator_task=str(lock.get("operator_task") or ""),
            facts=facts,
        )
        text = rpt.with_late_report(
            text, head_msg_id, len(rpt.messages_after(messages, head_msg_id))
        )
        msg_id = deps.chat.post(project, thread_id, text)
    except ThreadResolvedError as exc:
        return _thread_gone(lock, deps, exc)
    except Exception as exc:
        res.action = "quarantine_post_failed"
        res.log(f"operator run {run_id}: quarantine post failed ({exc}); next tick tries again")
        return res
    res.notify(
        f"operator-lane:quarantine:{run_id}",
        f"operator lane: {project}/{thread_id} の OPERATOR-TASK（{head_msg_id}）を quarantine にしました"
        f"（{msg_id}）。手動の operator が引き取ってください。",
    )
    return _complete(lock, deps, res)


def _thread_gone(lock: dict[str, Any], deps: PassDeps, exc: Exception) -> PassResult:
    """Fallback surface (1): the thread is resolved, so the notification carries the outcome."""
    run_id = str(lock.get("run_id"))
    res = PassResult("thread_resolved")
    res.notify(
        f"operator-lane:resolved:{run_id}",
        f"operator lane: {lock.get('project')}/{lock.get('thread_id')} は解決済みで投稿できません"
        f"（{exc}）。run {run_id} の結果は {deps.paths.reports_done} に残しています。",
    )
    return _complete(lock, deps, res)


def _complete(lock: dict[str, Any], deps: PassDeps, res: PassResult) -> PassResult:
    """Archive the report (D2⁶: ``done\\``, kept 7 days), drop the ``.tmp``, reclaim the lock."""
    run_id = str(lock.get("run_id"))
    paths = deps.paths
    paths.reports_done.mkdir(parents=True, exist_ok=True)
    if paths.report(run_id).exists():
        paths.report(run_id).replace(paths.reports_done / f"{run_id}.md")
    with contextlib.suppress(FileNotFoundError):
        paths.report_tmp(run_id).unlink()
    with hold_mutex(paths.mutex):
        current = read_lock(paths)
        if current is not None and current.get("run_id") == run_id:
            reclaim_lock(paths, deps.clock())
    return res


def _author(deps: PassDeps) -> str:
    return load_config(deps.paths.config).operator_author


def _purge_done(paths: LanePaths, now: datetime) -> None:
    if not paths.reports_done.exists():
        return
    cutoff = (now - DONE_RETENTION).timestamp()
    stale_copies = paths.state_dir.glob(f"{paths.lock.name}.stale-*")
    for f in [*paths.reports_done.glob("*.md"), *stale_copies]:
        with contextlib.suppress(OSError):
            if f.stat().st_mtime < cutoff:
                f.unlink()


# --- dispatch ---------------------------------------------------------------------------------


def _skip_records(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def eligible_candidates(
    config: LaneConfig,
    candidates: Sequence[dict[str, str]],
    heads: dict[str, dict[str, Any]],
    skip_records: dict[str, Any],
) -> list[dict[str, str]]:
    """D5's cheap filters: allowlisted project, the sweep saw ``NEXT: operator`` on this very head,
    and this head was neither dispatched nor exhausted its spawn attempts."""
    out = []
    for c in candidates:
        project, thread_id, head = (
            c.get("project", ""),
            c.get("thread_id", ""),
            c.get("head_msg_id", ""),
        )
        if project not in config.projects or not thread_id or not head:
            continue
        rec = record_from_json(skip_records.get(thread_id))
        if rec is None or rec.last_observed_nomination != OPERATOR_TOKEN:
            continue
        if rec.last_observed_head_msg_id != head:
            continue
        entry = heads.get(head_key(project, thread_id)) or {}
        if entry.get("head_msg_id") == head and (
            entry.get("dispatched")
            or entry.get("not_a_task")
            or int(entry.get("spawn_attempts") or 0) >= config.max_spawn_attempts
        ):
            continue
        out.append({"project": project, "thread_id": thread_id, "head_msg_id": head})
    return out


def _mark_not_a_task(deps: PassDeps, cand: dict[str, str]) -> None:
    with hold_mutex(deps.paths.mutex):
        heads = load_heads(deps.paths)
        heads[head_key(cand["project"], cand["thread_id"])] = {
            "head_msg_id": cand["head_msg_id"],
            "spawn_attempts": 0,
            "not_a_task": True,
        }
        save_heads(deps.paths, heads)


def prescreen(task: str) -> str | None:
    folded = task.casefold()
    for word in PRESCREEN_WORDS:
        if word.casefold() in folded:
            return word
    return None


def _dispatch(
    config: LaneConfig,
    candidates: Sequence[dict[str, str]],
    heads: dict[str, dict[str, Any]],
    deps: PassDeps,
) -> PassResult:
    pool = eligible_candidates(config, candidates, heads, _skip_records(deps.head_skip_path))
    if not pool:
        return PassResult("idle")
    res = PassResult("idle")
    target: dict[str, str] | None = None
    for cand in pool[:MAX_HEAD_READS]:
        try:
            messages = deps.chat.messages(cand["project"], cand["thread_id"])
        except Exception as exc:
            res.log(f"operator pass: cannot read {cand['thread_id']} ({exc})")
            continue
        head = messages[-1] if messages else {}
        if head.get("msg_id") != cand["head_msg_id"]:
            continue
        task = is_operator_task_head(str(head.get("content") or ""))
        if task is None:
            # e.g. the lane's own post: record it so it stops taking a read slot (PR #506 gate)
            _mark_not_a_task(deps, cand)
            continue
        target = {
            **cand,
            "requester": str(head.get("author") or ""),
            "operator_task": task,
        }
        break
    if target is None:
        return res
    hit = prescreen(target["operator_task"])
    if hit is not None:
        return _post_prescreen(target, hit, deps)
    verdict = deps.guard(config)
    if verdict.status == OPEN:
        disable(
            deps.paths.config, reason=f"guard_open: {'; '.join(verdict.problems)}", now=deps.clock()
        )
        res = PassResult("guard_open")
        res.notify(
            "operator-lane:guard",
            f"operator lane の隔離ガードが開いています（{'; '.join(verdict.problems)}）。lane を無効にしました。"
            "直したら operator_lane.json の enabled を手で true に戻してください。",
        )
        return res
    if not verdict.ok:
        res = PassResult("guard_undetermined")
        res.log(
            f"operator pass: guard undetermined ({'; '.join(verdict.problems)}); not dispatching"
        )
        return res
    return _start(config, target, deps)


def _post_prescreen(target: dict[str, str], word: str, deps: PassDeps) -> PassResult:
    """D4 pre-screen: post the needs-hands stop without starting a session."""
    run_id = deps.new_run_id()
    key = head_key(target["project"], target["thread_id"])
    with hold_mutex(deps.paths.mutex):
        heads = load_heads(deps.paths)
        prior = heads.get(key)
        heads[key] = {"head_msg_id": target["head_msg_id"], "spawn_attempts": 0, "dispatched": True}
        save_heads(deps.paths, heads)
    body = (
        f"{rpt.header(run_id, target['head_msg_id'], target['operator_task'])}\n"
        f"{rpt.marker(run_id, rpt.KIND_REPORT)}\n"
        f"{rpt.LANE_LINE_PREFIX} needs-hands prescreen「{word}」\n\n"
        f"{rpt.SECTIONS[0]}\n- OPERATOR-TASK の本文に「{word}」が含まれます。人の手か Tier-C の判断が要る可能性があります。\n\n"
        f"{rpt.SECTIONS[1]}\n- なし（セッションは起動していません）。\n\n"
        f"{rpt.SECTIONS[2]}\n- 手動の operator が内容を確かめて実行してください。Tier-C に当たる場合は human に上げてください。\n\n"
        f"OPERATOR-TASK: {target['head_msg_id']} の OPERATOR-TASK を手で確認・実行する\n"
        "TIER-C-CHECK: none\n"
        "NEXT: operator\n"
    )
    res = PassResult("prescreened")
    problems = rpt.validate_report(body, run_id=run_id, requester=target["requester"])
    if problems:
        res.notify("operator-lane:prescreen", f"pre-screen report invalid: {'; '.join(problems)}")
        return res
    try:
        msg_id = deps.chat.post(target["project"], target["thread_id"], body)
    except ThreadResolvedError as exc:
        # Terminal: keep the record (no retry); fallback surface (1) is this notification
        res.action = "thread_resolved"
        res.notify(
            f"operator-lane:prescreen:{target['head_msg_id']}",
            f"operator lane: {target['thread_id']} は resolved のため pre-screen 報告を投稿できません（{exc}）。"
            f"{target['head_msg_id']} の OPERATOR-TASK「{word}」を手で確認してください。",
        )
        return res
    except Exception as exc:
        # Undo the record so the next tick retries; a post that did land moved the head (PR #506)
        with hold_mutex(deps.paths.mutex):
            heads = load_heads(deps.paths)
            if prior is None:
                heads.pop(key, None)
            else:
                heads[key] = prior
            save_heads(deps.paths, heads)
        res.notify(
            f"operator-lane:prescreen:{target['head_msg_id']}",
            f"operator lane: {target['thread_id']} の pre-screen 報告を投稿できませんでした（{exc}）。"
            "次の tick でもう一度投稿します。",
        )
        return res
    res.log(f"operator pass: {target['thread_id']} pre-screened ({word}) -> {msg_id}")
    return res


def _start(config: LaneConfig, target: dict[str, str], deps: PassDeps) -> PassResult:
    """D3⁵ steps 1-3 and the D3⁶ in-tick watch."""
    run_id = deps.new_run_id()
    key = head_key(target["project"], target["thread_id"])
    now = deps.clock()
    with hold_mutex(deps.paths.mutex):
        if read_lock(deps.paths) is not None:
            return PassResult("raced")
        heads = load_heads(deps.paths)
        prior = heads.get(key) or {}
        attempts = (
            int(prior.get("spawn_attempts") or 0)
            if prior.get("head_msg_id") == target["head_msg_id"]
            else 0
        )
        heads[key] = {
            "head_msg_id": target["head_msg_id"],
            "spawn_attempts": attempts,
            "dispatched": True,
        }
        save_heads(deps.paths, heads)  # step 1, fail-closed: before anything starts
        create_lock(
            deps.paths,
            conductor_lock(
                run_id=run_id,
                wrapper_pid=deps.wrapper_pid,
                wrapper_started_at=deps.probe.started_at(deps.wrapper_pid),
                now=now,
                target=target,
            ),
        )  # step 2
    res = PassResult("dispatched")
    try:
        runner_pid = deps.spawner.spawn_runner(
            run_id=run_id,
            data_dir=deps.paths.data_dir,
            log_path=deps.paths.logs / f"{run_id}.runner.log",
        )  # step 3
    except Exception as exc:
        res.log(f"operator run {run_id}: runner did not start ({exc})")
        _mark_abort(deps, run_id, f"spawn_error: {exc}")
        settle = _settle_spawn_failure(config, _read(deps) or {}, deps, "spawn_error")
        settle.events[:0] = res.events
        return settle
    runner_started = deps.probe.started_at(runner_pid)
    res.log(
        f"operator run {run_id}: runner pid {runner_pid} started for {target['thread_id']} {target['head_msg_id']}"
    )
    deadline = deps.monotonic() + SPAWN_WATCH.total_seconds()
    while deps.monotonic() < deadline:
        lock = _read(deps)
        if lock is None or lock.get("run_id") != run_id or lock.get("state") == STATE_RUNNING:
            return res
        runner_alive = runner_started is not None and process_alive(
            deps.probe, runner_pid, iso(runner_started)
        )
        if not runner_alive:
            settle = _settle_spawn_failure(config, lock, deps, "runner_exited_while_spawning")
            settle.events[:0] = res.events
            return settle
        deps.sleep(SPAWN_WATCH_POLL_SEC)
    res.log(f"operator run {run_id}: still spawning after 30 s; spawn_deadline settles it later")
    return res


def _read(deps: PassDeps) -> dict[str, Any] | None:
    with hold_mutex(deps.paths.mutex):
        return read_lock(deps.paths)


def _mark_abort(deps: PassDeps, run_id: str, reason: str) -> None:
    with hold_mutex(deps.paths.mutex):
        record_abort(deps.paths, run_id=run_id, reason=reason)


__all__ = [
    "HOLDER_CONDUCTOR",
    "PRESCREEN_WORDS",
    "PassDeps",
    "PassResult",
    "eligible_candidates",
    "prescreen",
    "run_pass",
]
