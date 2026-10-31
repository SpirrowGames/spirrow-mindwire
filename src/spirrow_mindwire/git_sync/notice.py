"""The park notice the conductor posts when the git-sync hook parks a thread (design v1.2 B-6).

The first line of the body is the reason line ``git.<code>: <thread_id> <branch> <要約>``. The
body ends ``NEXT: human`` with a ``mindwire:stop v1`` marker above it (the placement rule of
:mod:`~spirrow_mindwire.conductor.stop_marker`), so head_skip parks the thread and the board reads
the stop by its marker rather than by the prose.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..ulid_util import new_ulid
from ..value_objects import Event
from .errors import EVENT_KIND_GIT_SYNC_PARK, GitParkCode, GitSyncParkError, GitSyncPhase

_NEXT_STEP: dict[GitParkCode, str] = {
    GitParkCode.FETCH_FAILED: "remote への fetch が通るようにしてください (ネットワーク / 認証)。",
    GitParkCode.BRANCH_RESOLVE_FAILED: (
        "ブランチ名を決められませんでした。GitHub の認証・remote URL・スレッド ID を確認するか、"
        "`[turn.git.branch_override]` で名前を指定してください。"
    ),
    GitParkCode.LOCAL_AHEAD: (
        "ローカルのスレッドブランチに remote にないコミットがあります。push するか取り除くかを"
        "決めてください (自動では push も削除もしません)。"
    ),
    GitParkCode.DIVERGED: (
        "ローカルと remote のスレッドブランチが分岐しています。どちらを正とするかを決めて"
        "揃えてください (自動では揃えません)。"
    ),
    GitParkCode.CHECKOUT_FAILED: (
        "git の switch / fast-forward が失敗しました。clone を確認してください。"
    ),
    GitParkCode.LFS_FAILED: (
        "LFS の実体化が完了しませんでした。`git lfs pull` が通るようにしてください。"
    ),
    GitParkCode.DIRTY_AFTER_TURN: (
        "ターンが未コミットの変更 (または途中の git 操作) を残しました。何も片付けていません。"
        "必要な変更を退避し、clone をデフォルトブランチの clean な状態に戻してください。"
        "戻るまで、この clone はどのスレッドの dispatch も受け付けません (exit 8)。"
    ),
    GitParkCode.HEAD_MOVED: (
        "ターンの終わりに HEAD がスレッドブランチ以外を指していました。何も片付けていません。"
        "clone をデフォルトブランチに戻してください。"
    ),
    GitParkCode.PUSH_FAILED: (
        "スレッドブランチの push が拒否されました (`--force` は使いません)。原因を確認して"
        "ください。コミットはローカルのスレッドブランチに残っています。"
    ),
}

_PHASE_LABEL = {GitSyncPhase.PRE_TURN: "ターン前", GitSyncPhase.POST_TURN: "ターン後"}


def git_sync_park_event(exc: GitSyncParkError, *, project: str) -> Event:
    """The ``git_sync.park`` event (no message body, per the I6 Event convention)."""
    return Event(
        event_id=new_ulid(),
        occurred_at=datetime.now(UTC),
        kind=EVENT_KIND_GIT_SYNC_PARK,
        fields={
            "code": f"git.{exc.code.value}",
            "phase": exc.phase.value,
            "project": project,
            "thread": exc.thread_id,
            "branch": exc.branch or "",
            "summary": exc.summary,
        },
    )


def render_git_sync_park_notice(exc: GitSyncParkError) -> str:
    """The thread body, ending ``NEXT: human``. The caller inserts the stop marker above it."""
    return (
        f"{exc.reason_line}\n\n"
        f"Conductor park — git 同期 ({_PHASE_LABEL[exc.phase]}) でこのスレッドを止めました。\n\n"
        f"次にやること: {_NEXT_STEP[exc.code]} 直したら、このスレッドの head に進め先の "
        f"`NEXT:` を書いてください。head が動くまでループは再開しません。\n\n"
        f"NEXT: human"
    )


__all__ = ["git_sync_park_event", "render_git_sync_park_notice"]
