"""Tests for the PR-review orchestrator (WIRING_ALLOWLIST_SPEC §A.2 → ADR-19 driver-化).

A fake :class:`McpToolCaller` + a fake :class:`NaysayerPrReviewDriver` exercise thread creation,
number derivation, and that ``fire_pr_review`` **drives the driver directly** (parses the PR ref,
posts the critique to the thread, returns the outcome). ``require_ci_success`` (the L2 merge gate)
and ``ChatroomWatcher.add_watch`` (the standing role watches) are tested directly.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from spirrow_mindwire.github.client import CiState, CiStatus, PrRef, ReviewEvent, ReviewInfo
from spirrow_mindwire.magickit.client import MagickitMcpError
from spirrow_mindwire.magickit.watcher import ChatroomWatcher, WatchSpec
from spirrow_mindwire.naysayer.pr_review import PostCritique, PrReviewOutcome
from spirrow_mindwire.orchestrator import (
    MergeBlockedError,
    PrReviewOrchestrator,
    ThreadIdCollisionError,
    require_ci_success,
)
from spirrow_mindwire.ulid_util import new_ulid
from spirrow_mindwire.value_objects import Role, SessionHandle, ThreadRef

_TS = datetime(2026, 5, 23, tzinfo=UTC)


class _FakeMcp:
    """Records call_tool invocations; returns programmed results by tool name."""

    def __init__(
        self,
        results: dict[str, Any] | None = None,
        raise_on: dict[str, Exception] | None = None,
    ) -> None:
        self._results = results or {}
        self._raise_on = raise_on or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if name in self._raise_on:
            raise self._raise_on[name]
        if name == "chatroom_get_thread" and name not in self._results:
            # A thread nobody programmed is a thread that is not there, and the
            # far end says so by raising -- it has no success response that omits
            # the thread. Returning `{}` here made "absent" and "unreadable" look
            # alike to the caller, which is the confusion under test below.
            raise MagickitMcpError(
                f"Thread '{arguments['thread_id']}' not found in project '{arguments['project']}'"
            )
        result = self._results.get(name, {})
        # A programmed result may be a callable so one fake can answer
        # `chatroom_get_thread` differently per thread_id (which is the whole
        # point of the collision tests below).
        return result(arguments) if callable(result) else result

    def args_for(self, name: str) -> dict[str, Any]:
        return next(args for n, args in self.calls if n == name)


class _FakeDriver:
    """Records the reviewed PR + posts a critique via the callback; returns a fixed outcome."""

    def __init__(self, outcome: PrReviewOutcome | None = None) -> None:
        self.reviewed: list[PrRef] = []
        self.closed = False
        self._outcome = outcome or PrReviewOutcome(
            verdict=ReviewEvent.APPROVE,
            body="LGTM\n\nVERDICT: APPROVE",
            ci_state=CiState.SUCCESS,
            head_sha="sha1",
        )

    async def review(self, pr: PrRef, *, post_critique: PostCritique) -> PrReviewOutcome:
        self.reviewed.append(pr)
        await post_critique(self._outcome.body)
        return self._outcome

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.anyio
async def test_fire_pr_review_thread_id_is_pr_derived() -> None:
    # Tier C decide msg-459: the thread id is deterministic from the PR number (no max+1 numbering).
    mcp = _FakeMcp()
    driver = _FakeDriver()
    orch = PrReviewOrchestrator(mcp, driver=driver)  # type: ignore[arg-type]
    ref, outcome = await orch.fire_pr_review(project="spirrow-mindwire", pr_ref="org/repo#42")
    assert ref.thread_id == "T-pr-review-repo-42"
    assert ref.project_id == "spirrow-mindwire"
    args = mcp.args_for("chatroom_open_thread")
    assert args["thread_id"] == "T-pr-review-repo-42"
    assert args["owner"] == "orchestrator"
    assert "org/repo#42" in args["propose_content"]
    assert "pr-review" in args["tags"]
    # The driver was driven with the parsed PR ref, and its critique was posted to the thread.
    assert driver.reviewed == [PrRef("org", "repo", 42)]
    post = mcp.args_for("chatroom_post_message")
    assert post["thread_id"] == "T-pr-review-repo-42"
    assert post["content"] == outcome.body
    assert post["author"] == "naysayer-pr-review"


@pytest.mark.anyio
async def test_fire_pr_review_returns_driver_outcome() -> None:
    outcome = PrReviewOutcome(
        verdict=ReviewEvent.REQUEST_CHANGES,
        body="bug\n\nVERDICT: REQUEST_CHANGES",
        ci_state=CiState.SUCCESS,
        head_sha="sha9",
    )
    orch = PrReviewOrchestrator(_FakeMcp(), driver=_FakeDriver(outcome))  # type: ignore[arg-type]
    _ref, got = await orch.fire_pr_review(project="p", pr_ref="o/r#7")
    assert got is outcome


@pytest.mark.anyio
async def test_fire_pr_review_unparseable_ref_raises() -> None:
    orch = PrReviewOrchestrator(_FakeMcp(), driver=_FakeDriver())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unparseable PR ref"):
        await orch.fire_pr_review(project="p", pr_ref="not a pr ref")


@pytest.mark.anyio
async def test_orchestrator_aclose_closes_driver() -> None:
    # Tier B #93 round-4: the driver is orchestrator-held (not registry-registered), so the loop
    # teardown closes it via the orchestrator.
    driver = _FakeDriver()
    orch = PrReviewOrchestrator(_FakeMcp(), driver=driver)  # type: ignore[arg-type]
    await orch.aclose()
    assert driver.closed


def _thread_payload(pr_ref: str, *, title: str | None = None) -> dict[str, Any]:
    """What ``chatroom_get_thread`` returns for a review thread the driver opened.

    Shaped like conclair's ``ThreadView``: the thread names its opening msg in
    ``created_by_msg`` (required there -- "the thread's first msg (the propose);
    it never changes") and every msg carries its ``type``. Both are things the
    far end always sends and the caller is entitled to use, so a payload without
    them is not a smaller answer, it is one the server cannot give.
    """
    return {
        "thread": {
            "title": f"PR review (develop→main) — {pr_ref}" if title is None else title,
            "status": "active",
            "created_by_msg": "msg-001",
        },
        "messages": [
            {
                "msg_id": "msg-001",
                "type": "propose",
                "content": f"naysayer review request — PR {pr_ref}",
            }
        ],
    }


def _existing_threads(
    threads: dict[str, dict[str, Any]],
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A ``chatroom_get_thread`` stub: known ids answer, unknown ids 404 like conclair.

    ``mode`` is answered the way conclair answers it (``api/threads.py``): a
    ``summary`` of a **resolved** thread carries only its ``decide`` msgs, and
    every other status returns the whole list whatever the mode. The stub reacts
    to the mode the caller asks for rather than always returning everything --
    otherwise the caller's choice of mode has no consequence here, and a mode
    that hides the message the caller depends on can only be discovered in
    production.
    """

    def _get(arguments: dict[str, Any]) -> dict[str, Any]:
        thread_id = arguments["thread_id"]
        if thread_id not in threads:
            raise MagickitMcpError(
                f"Thread '{thread_id}' not found in project '{arguments['project']}'"
            )
        payload = threads[thread_id]
        thread: dict[str, Any] = payload.get("thread", {})
        if arguments.get("mode") != "summary" or thread.get("status") != "resolved":
            return payload
        messages: list[dict[str, Any]] = payload.get("messages", [])
        return {**payload, "messages": [m for m in messages if m.get("type") == "decide"]}

    return _get


@pytest.mark.anyio
async def test_a_re_review_posts_into_the_existing_thread_without_reopening_it() -> None:
    """Re-firing an open PR is the ordinary path, and it costs one read (msg-459 idempotence).

    Resolving the id already proved the thread is there and is this PR's. Opening
    it anyway -- to be told "already exists", then re-reading the same thread to
    learn whose it is -- spends three round-trips to relearn the first answer, and
    makes every re-review indistinguishable from the race the handler exists for.
    The ``raise_on`` below is what the far end would say if the open were
    attempted; the point of the test is that it never is.
    """
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _existing_threads({"T-pr-review-r-7": _thread_payload("o/r#7")})
        },
        raise_on={
            "chatroom_open_thread": MagickitMcpError(
                "Thread 'T-pr-review-r-7' already exists in project 'p'"
            )
        },
    )
    orch = PrReviewOrchestrator(mcp, driver=_FakeDriver())  # type: ignore[arg-type]
    ref, _outcome = await orch.fire_pr_review(project="p", pr_ref="o/r#7")
    assert ref.thread_id == "T-pr-review-r-7"
    assert [name for name, _ in mcp.calls] == ["chatroom_get_thread", "chatroom_post_message"]


# ---------- repo-qualified thread ids (T-pr-review-thread-id-not-repo-qualified) ---------- #


@pytest.mark.anyio
async def test_thread_id_carries_the_repo_case_folded() -> None:
    # The repo is part of the id, and `Spirrow-VoxelWorld` / `spirrow-voxelworld` are the
    # same repo -- case must not be able to open two ledgers for one PR.
    mcp = _FakeMcp()
    orch = PrReviewOrchestrator(mcp, driver=_FakeDriver())  # type: ignore[arg-type]
    ref, _ = await orch.fire_pr_review(project="p", pr_ref="SpirrowGames/Spirrow-VoxelWorld#12")
    assert ref.thread_id == "T-pr-review-spirrow-voxelworld-12"


@pytest.mark.anyio
async def test_another_repos_pr_with_the_same_number_gets_its_own_thread() -> None:
    """The regression: PR numbers are unique per repo, thread ids were not.

    ``Spirrow-VoxelWorld#12`` already owns the unqualified ``T-pr-review-12``. Firing
    the gate for ``spirrow-conclair#12`` must not append its critique there -- which is
    what the unconditional "already exists" swallow used to do, silently.
    """
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _existing_threads(
                {"T-pr-review-12": _thread_payload("SpirrowGames/Spirrow-VoxelWorld#12")}
            )
        }
    )
    orch = PrReviewOrchestrator(mcp, driver=_FakeDriver())  # type: ignore[arg-type]
    ref, _ = await orch.fire_pr_review(project="p", pr_ref="SpirrowGames/spirrow-conclair#12")
    assert ref.thread_id == "T-pr-review-spirrow-conclair-12"
    written_to = {
        args["thread_id"]
        for name, args in mcp.calls
        if name in ("chatroom_open_thread", "chatroom_post_message")
    }
    assert written_to == {"T-pr-review-spirrow-conclair-12"}


@pytest.mark.anyio
async def test_a_pr_already_mid_review_keeps_its_unqualified_thread() -> None:
    # Legacy bridge: a gate that already opened `T-pr-review-10` for THIS PR keeps
    # writing there, rather than sprouting a second ledger halfway through a review.
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _existing_threads(
                {"T-pr-review-10": _thread_payload("SpirrowGames/spirrow-conclair#10")}
            )
        }
    )
    orch = PrReviewOrchestrator(mcp, driver=_FakeDriver())  # type: ignore[arg-type]
    ref, _ = await orch.fire_pr_review(project="p", pr_ref="SpirrowGames/spirrow-conclair#10")
    assert ref.thread_id == "T-pr-review-10"


@pytest.mark.anyio
async def test_an_unidentifiable_thread_at_the_legacy_id_is_not_reused() -> None:
    # A thread whose title and messages name no PR is not this PR's, so it is left alone.
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _existing_threads(
                {
                    "T-pr-review-10": {
                        "thread": {"title": "something a human opened", "status": "active"},
                        "messages": [{"msg_id": "msg-001", "content": "no ref here"}],
                    }
                }
            )
        }
    )
    orch = PrReviewOrchestrator(mcp, driver=_FakeDriver())  # type: ignore[arg-type]
    ref, _ = await orch.fire_pr_review(project="p", pr_ref="o/r#10")
    assert ref.thread_id == "T-pr-review-r-10"


@pytest.mark.anyio
async def test_a_taken_qualified_id_fails_before_the_review_is_paid_for() -> None:
    # If the id cannot be used, nothing should be spent finding out: the check runs
    # before driver.review, so no Gemini judgement is produced and then thrown away.
    driver = _FakeDriver()
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _existing_threads(
                {"T-pr-review-r-9": _thread_payload("other/r#9")}
            )
        }
    )
    orch = PrReviewOrchestrator(mcp, driver=driver)  # type: ignore[arg-type]
    with pytest.raises(ThreadIdCollisionError, match="other/r#9"):
        await orch.fire_pr_review(project="p", pr_ref="o/r#9")
    assert driver.reviewed == []
    assert all(name == "chatroom_get_thread" for name, _ in mcp.calls)


@pytest.mark.anyio
async def test_an_unidentifiable_thread_on_the_qualified_id_also_fails_before_the_review() -> None:
    """A thread that exists but names no PR occupies the id just the same.

    Reading "cannot tell whose this is" as "nobody's" made the resolve step
    hand the review an id it could not have: the run then paid for a Gemini
    judgement and threw it away at open time, which is precisely the cost
    resolving early exists to avoid. Absent and unreadable are different
    answers; only the first means free.

    (At the *legacy* id the same thread is merely not ours -- see
    `test_an_unidentifiable_thread_at_the_legacy_id_is_not_reused`, which
    proceeds normally. The asymmetry is deliberate.)
    """
    driver = _FakeDriver()
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _existing_threads(
                {
                    "T-pr-review-r-9": {
                        "thread": {
                            "title": "something a human opened",
                            "status": "resolved",
                            "created_by_msg": "msg-001",
                        },
                        "messages": [
                            {"msg_id": "msg-001", "type": "propose", "content": "no ref here"},
                            {"msg_id": "msg-002", "type": "decide", "content": "closing this"},
                        ],
                    }
                }
            )
        }
    )
    orch = PrReviewOrchestrator(mcp, driver=driver)  # type: ignore[arg-type]
    with pytest.raises(ThreadIdCollisionError, match="unidentifiable"):
        await orch.fire_pr_review(project="p", pr_ref="o/r#9")
    assert driver.reviewed == []
    assert all(name == "chatroom_get_thread" for name, _ in mcp.calls)


@pytest.mark.anyio
async def test_the_same_pr_spelled_with_different_case_is_not_a_collision() -> None:
    """Case-folding the id without case-folding the comparison is a self-collision.

    The id lower-cases the repo, so both spellings land on one thread -- and the
    chatroom's live titles use both (`Spirrow-VoxelWorld` in the existing gate
    threads, `spirrow-voxelworld` elsewhere). Comparing the refs exactly would
    make the gate declare a PR's own thread to be someone else's and refuse to
    re-fire.
    """
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _existing_threads(
                {
                    "T-pr-review-spirrow-voxelworld-12": _thread_payload(
                        "SpirrowGames/Spirrow-VoxelWorld#12"
                    )
                }
            )
        },
        raise_on={
            "chatroom_open_thread": MagickitMcpError(
                "Thread 'T-pr-review-spirrow-voxelworld-12' already exists in project 'p'"
            )
        },
    )
    orch = PrReviewOrchestrator(mcp, driver=_FakeDriver())  # type: ignore[arg-type]
    ref, _ = await orch.fire_pr_review(project="p", pr_ref="spirrowgames/spirrow-voxelworld#12")
    assert ref.thread_id == "T-pr-review-spirrow-voxelworld-12"
    # Reused, not refused: the critique still reaches the thread.
    assert mcp.args_for("chatroom_post_message")["thread_id"] == (
        "T-pr-review-spirrow-voxelworld-12"
    )


@pytest.mark.anyio
async def test_a_thread_that_appears_between_resolve_and_open_is_not_written_into() -> None:
    """The swallow is re-checked, not trusted.

    ``_resolve_thread_id`` sees nothing, the review runs, and by the time the thread is
    opened someone else holds the id. "already exists" must not be read as "mine".
    """
    threads: dict[str, dict[str, Any]] = {}

    class _RacingDriver(_FakeDriver):
        async def review(self, pr: PrRef, *, post_critique: PostCritique) -> PrReviewOutcome:
            threads["T-pr-review-r-9"] = _thread_payload("other/r#9")  # the race
            return await super().review(pr, post_critique=post_critique)

    mcp = _FakeMcp(
        results={"chatroom_get_thread": _existing_threads(threads)},
        raise_on={
            "chatroom_open_thread": MagickitMcpError(
                "Thread 'T-pr-review-r-9' already exists in project 'p'"
            )
        },
    )
    orch = PrReviewOrchestrator(mcp, driver=_RacingDriver())  # type: ignore[arg-type]
    with pytest.raises(ThreadIdCollisionError, match="other/r#9"):
        await orch.fire_pr_review(project="p", pr_ref="o/r#9")
    assert all(name != "chatroom_post_message" for name, _ in mcp.calls)


@pytest.mark.anyio
async def test_a_race_that_opens_this_prs_own_thread_is_still_swallowed() -> None:
    """The idempotent swallow survives -- for the case it was actually meant for.

    Now that the re-review path skips the open entirely, "already exists" can only
    mean the id was free when the review started and was taken while it ran. When
    the taker is another gate for the *same* PR that is still one ledger, and the
    critique belongs in it. This is the branch that used to be exercised (and its
    cost hidden) by every ordinary re-review.
    """
    threads: dict[str, dict[str, Any]] = {}

    class _SamePrRacingDriver(_FakeDriver):
        async def review(self, pr: PrRef, *, post_critique: PostCritique) -> PrReviewOutcome:
            threads["T-pr-review-r-9"] = _thread_payload("o/r#9")  # another gate, same PR
            return await super().review(pr, post_critique=post_critique)

    mcp = _FakeMcp(
        results={"chatroom_get_thread": _existing_threads(threads)},
        raise_on={
            "chatroom_open_thread": MagickitMcpError(
                "Thread 'T-pr-review-r-9' already exists in project 'p'"
            )
        },
    )
    orch = PrReviewOrchestrator(mcp, driver=_SamePrRacingDriver())  # type: ignore[arg-type]
    ref, _ = await orch.fire_pr_review(project="p", pr_ref="o/r#9")
    assert ref.thread_id == "T-pr-review-r-9"
    assert mcp.args_for("chatroom_post_message")["thread_id"] == "T-pr-review-r-9"


@pytest.mark.anyio
async def test_a_pr_mentioned_further_down_the_thread_is_not_its_subject() -> None:
    """A thread names its PR in its title and its opening request, not in its replies.

    Scanning every message for the first ref it could find made the subject depend
    on message order: a critique that happens to mention another PR would redefine
    what the thread is about, and the gate would then report a collision naming
    that other PR. "Unidentifiable" blocks the qualified id just the same -- but
    for the reason that is actually true.
    """
    driver = _FakeDriver()
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _existing_threads(
                {
                    "T-pr-review-r-9": {
                        "thread": {
                            "title": "a human renamed this",
                            "status": "active",
                            "created_by_msg": "msg-001",
                        },
                        "messages": [
                            {"msg_id": "msg-001", "type": "propose", "content": "no ref here"},
                            {
                                "msg_id": "msg-002",
                                "type": "report",
                                "content": "cf. other/elsewhere#3",
                            },
                        ],
                    }
                }
            )
        },
    )
    orch = PrReviewOrchestrator(mcp, driver=driver)  # type: ignore[arg-type]
    with pytest.raises(ThreadIdCollisionError, match="unidentifiable") as excinfo:
        await orch.fire_pr_review(project="p", pr_ref="o/r#9")
    assert "other/elsewhere#3" not in str(excinfo.value)
    assert driver.reviewed == []


@pytest.mark.anyio
async def test_a_thread_whose_title_carries_no_ref_is_identified_by_its_request() -> None:
    """The title is not the only place a thread names its PR.

    ``fire_pr_review`` takes a caller-supplied title, so a gate can open a thread
    whose title says nothing about the PR. The review request it posts always
    does -- and without that fallback the PR's own thread would read back as
    unidentifiable and its next gate would refuse to touch it.
    """
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _existing_threads(
                {
                    "T-pr-review-r-7": {
                        "thread": {
                            "title": "nightly gate",
                            "status": "active",
                            "created_by_msg": "msg-001",
                        },
                        "messages": [
                            {
                                "msg_id": "msg-001",
                                "type": "propose",
                                "content": "naysayer review request — PR o/r#7",
                            }
                        ],
                    }
                }
            )
        }
    )
    orch = PrReviewOrchestrator(mcp, driver=_FakeDriver())  # type: ignore[arg-type]
    ref, _ = await orch.fire_pr_review(project="p", pr_ref="o/r#7")
    assert ref.thread_id == "T-pr-review-r-7"
    assert mcp.args_for("chatroom_post_message")["thread_id"] == "T-pr-review-r-7"


@pytest.mark.anyio
async def test_a_closing_note_naming_this_pr_does_not_make_the_thread_ours() -> None:
    """A resolved thread answers with its *closing* note, and closing notes name other PRs.

    Asking for ``mode="summary"`` and then reading whichever message came back
    first as "the opening request" is only the same thing while the thread is
    open: conclair filters a resolved thread down to its ``decide`` msgs, so the
    first (and only) message returned is the note that closed it -- and those say
    things like "superseded by owner/repo#9" as a matter of habit. When the PR
    named there is the one being gated, a stranger's thread reads back as this
    PR's own and the critique is posted into it. Nothing objects: conclair takes
    a ``report`` into a resolved thread (only ``decide`` is status-gated), so the
    ledger of a finished review quietly acquires an unrelated PR's verdict.

    The opener is fetched by the id the thread gives for it, so the summary's
    substitution cannot happen; blocking here is the whole point of the id check.
    """
    driver = _FakeDriver()
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _existing_threads(
                {
                    "T-pr-review-r-9": {
                        "thread": {
                            "title": "a review someone else already finished",
                            "status": "resolved",
                            "created_by_msg": "msg-001",
                        },
                        "messages": [
                            {
                                "msg_id": "msg-001",
                                "type": "propose",
                                "content": "naysayer review request — PR other/elsewhere#3",
                            },
                            {
                                "msg_id": "msg-002",
                                "type": "decide",
                                "content": "closing — superseded by o/r#9",
                            },
                        ],
                    }
                }
            )
        }
    )
    orch = PrReviewOrchestrator(mcp, driver=driver)  # type: ignore[arg-type]
    with pytest.raises(ThreadIdCollisionError, match="other/elsewhere#3"):
        await orch.fire_pr_review(project="p", pr_ref="o/r#9")
    assert driver.reviewed == []
    assert all(name == "chatroom_get_thread" for name, _ in mcp.calls)


@pytest.mark.anyio
async def test_a_resolved_thread_is_still_identified_by_its_opening_request() -> None:
    """The other direction of the same substitution: a PR locked out of its own ledger.

    Same shape as the test above -- a resolved thread whose title carries no ref,
    so the messages have to answer -- but here the thread *is* this PR's and its
    closing note mentions a different one. Reading the summary's first message
    attributed the thread to that other PR, and the qualified id then refused the
    PR its own thread on every later gate, permanently and with a reason that was
    never true.
    """
    mcp = _FakeMcp(
        results={
            "chatroom_get_thread": _existing_threads(
                {
                    "T-pr-review-r-7": {
                        "thread": {
                            "title": "nightly gate",
                            "status": "resolved",
                            "created_by_msg": "msg-001",
                        },
                        "messages": [
                            {
                                "msg_id": "msg-001",
                                "type": "propose",
                                "content": "naysayer review request — PR o/r#7",
                            },
                            {
                                "msg_id": "msg-002",
                                "type": "decide",
                                "content": "closing — follow-up lives in other/elsewhere#3",
                            },
                        ],
                    }
                }
            )
        }
    )
    orch = PrReviewOrchestrator(mcp, driver=_FakeDriver())  # type: ignore[arg-type]
    ref, _ = await orch.fire_pr_review(project="p", pr_ref="o/r#7")
    assert ref.thread_id == "T-pr-review-r-7"
    assert mcp.args_for("chatroom_post_message")["thread_id"] == "T-pr-review-r-7"
    # Resolving found the thread, so there is nothing to open (the re-review path).
    assert all(name != "chatroom_open_thread" for name, _ in mcp.calls)


@pytest.mark.anyio
async def test_fire_pr_review_non_exists_open_error_raises() -> None:
    # Only the "already exists" collision is idempotent — any other open error must propagate
    # (no masking; msg-459).
    mcp = _FakeMcp(
        raise_on={"chatroom_open_thread": MagickitMcpError("magickit MCP call failed: 500")}
    )
    orch = PrReviewOrchestrator(mcp, driver=_FakeDriver())  # type: ignore[arg-type]
    with pytest.raises(MagickitMcpError):
        await orch.fire_pr_review(project="p", pr_ref="o/r#7")
    assert all(name != "chatroom_post_message" for name, _ in mcp.calls)  # never posted


class _RaisingDriver:
    """A driver whose review fails before any critique (transient remote error)."""

    async def review(self, pr: PrRef, *, post_critique: PostCritique) -> PrReviewOutcome:
        raise RuntimeError("transient github/lexora error")


@pytest.mark.anyio
async def test_fire_pr_review_does_not_leak_thread_on_review_error() -> None:
    # Tier B msg-453: the thread is opened lazily inside post_critique, so a review that raises
    # before producing a critique must NOT leave an abandoned empty T-pr-review-<n> behind.
    mcp = _FakeMcp()
    orch = PrReviewOrchestrator(mcp, driver=_RaisingDriver())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await orch.fire_pr_review(project="p", pr_ref="o/r#1")
    assert all(name != "chatroom_open_thread" for name, _ in mcp.calls)
    assert all(name != "chatroom_post_message" for name, _ in mcp.calls)


@pytest.mark.anyio
async def test_fire_pr_review_opens_thread_then_posts_on_success() -> None:
    # On a successful review the thread is opened (with the request) and the critique posted —
    # open precedes post (lazy-open happens inside the single post_critique call).
    mcp = _FakeMcp()
    orch = PrReviewOrchestrator(mcp, driver=_FakeDriver())  # type: ignore[arg-type]
    await orch.fire_pr_review(project="p", pr_ref="o/r#1")
    # The leading reads are the thread-id resolution; the writes are what this pins.
    names = [name for name, _ in mcp.calls if name != "chatroom_get_thread"]
    assert names == ["chatroom_open_thread", "chatroom_post_message"]


# ---------- L2 merge gate: require_ci_success (ADR-2026-06-03-16 D-3) ------ #


class _FakeGitHubCi:
    """Minimal GitHubReviewClient stub returning a fixed CI status."""

    def __init__(self, ci: CiStatus) -> None:
        self._ci = ci

    async def fetch_pr_diff(self, pr: PrRef) -> str:
        raise NotImplementedError

    async def fetch_ci_status(self, pr: PrRef) -> CiStatus:
        return self._ci

    async def fetch_pr_reviews(self, pr: PrRef) -> list[ReviewInfo]:
        return []

    async def submit_review(self, pr: PrRef, *, event: ReviewEvent, body: str) -> dict[str, Any]:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


@pytest.mark.anyio
async def test_require_ci_success_returns_status_when_green() -> None:
    gh = _FakeGitHubCi(CiStatus(CiState.SUCCESS, "sha1", []))
    status = await require_ci_success(gh, PrRef("o", "r", 1))
    assert status.state is CiState.SUCCESS


@pytest.mark.anyio
@pytest.mark.parametrize(
    "ci",
    [
        CiStatus(CiState.FAILURE, "sha1", ["test"]),
        CiStatus(CiState.PENDING, "sha1", []),
        CiStatus(CiState.UNKNOWN, None, []),
    ],
)
async def test_require_ci_success_blocks_when_not_green(ci: CiStatus) -> None:
    # L2 is the deterministic merge gate — it must block on anything but SUCCESS, independently of
    # any naysayer APPROVE (fail-closed).
    gh = _FakeGitHubCi(ci)
    with pytest.raises(MergeBlockedError):
        await require_ci_success(gh, PrRef("o", "r", 1))


# ---------- ChatroomWatcher.add_watch (standing role watches) ------------- #


class _FakeDispatcher:
    def __init__(self) -> None:
        self.spawned: list[tuple[ThreadRef, Role]] = []
        self.dispatched: list[Any] = []

    async def spawn_instance(
        self, thread_ref: ThreadRef, role: Role, instance_id: str
    ) -> SessionHandle:
        self.spawned.append((thread_ref, role))
        return SessionHandle(
            session_id=new_ulid(),
            instance_id=instance_id,
            adapter_id="fake",
            thread_ref=thread_ref,
            role=role,
            started_at=_TS,
        )

    async def dispatch(self, handle: SessionHandle, event: Any) -> None:
        self.dispatched.append(event)

    async def halt(self, handle: SessionHandle) -> None:
        return None


class _StubMcp:
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self._messages = messages or []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return {"messages": self._messages}


@pytest.mark.anyio
async def test_add_watch_spawns_and_registers_for_polling() -> None:
    dispatcher = _FakeDispatcher()
    watcher = ChatroomWatcher(_StubMcp(), dispatcher, watches=[])  # type: ignore[arg-type]
    ref = ThreadRef(project_id="p", thread_id="T-x-1", chatroom_uri="mc://t")
    spec = WatchSpec(thread_ref=ref, role=Role.PROPOSER)
    await watcher.add_watch(spec, baseline=False)
    assert dispatcher.spawned == [(ref, Role.PROPOSER)]
    # C1 regression guard: the watch must be registered for polling, not only spawned —
    # otherwise poll_once() (which iterates _watches) never polls it.
    assert spec in watcher._watches
    # idempotent: adding the same watch again does not spawn or register twice.
    await watcher.add_watch(spec, baseline=False)
    assert len(dispatcher.spawned) == 1
    assert watcher._watches.count(spec) == 1


@pytest.mark.anyio
async def test_add_watch_then_poll_dispatches() -> None:
    dispatcher = _FakeDispatcher()
    mcp = _StubMcp([{"msg_id": "m1", "author": "proposer", "content": "hello", "timestamp": ""}])
    watcher = ChatroomWatcher(mcp, dispatcher, watches=[])  # type: ignore[arg-type]
    ref = ThreadRef(project_id="p", thread_id="T-x-1", chatroom_uri="mc://t")
    await watcher.add_watch(WatchSpec(thread_ref=ref, role=Role.PROPOSER), baseline=False)
    dispatched = await watcher.poll_once()
    assert dispatched == 1
    assert len(dispatcher.dispatched) == 1
