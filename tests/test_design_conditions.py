"""Conditional proceed, PR-gate side (T-naysayer-conditional-proceed-on-small-fixmin).

Every test that decides a verdict goes through the real path: the resolver the orchestrator builds
(:meth:`DesignConditionsPolicy.resolver`) reads a fake chatroom, and its check reaches the real
:class:`NaysayerPrReviewDriver` with fake Lexora / GitHub. The model's own judgement is not
reproduced; the fake model says APPROVE so that every non-APPROVE below is the gate's doing.

Test names carry the design's ids: T2' (msg-7954 / msg-7956), the 7-row table (msg-7960
Obj-5-2), the three-state setting (msg-7960 Obj-5-1), the stateless ``Thread:`` rule (msg-7958
Obj-4-2).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_orchestrator import _DESIGN_THREAD, _FakeMcp
from test_pr_review_driver import _capture, _FakeGitHub, _FakeLexora, _pr

from spirrow_mindwire.design_conditions import (
    ConditionalProceedMode,
    DesignConditionsPolicy,
    ThreadMessage,
    ThreadPolicy,
    build_policy,
    canonical_conditions,
    check_pr_body,
    design_conditions_section,
    evaluate_thread,
    mode_for,
    resolve_design_conditions,
)
from spirrow_mindwire.github.client import CiState, CiStatus, PrRef, ReviewEvent, ReviewInfo
from spirrow_mindwire.magickit.client import MagickitMcpError
from spirrow_mindwire.naysayer.conditions_check import ConditionsOutcome
from spirrow_mindwire.naysayer.pr_review import (
    _MARKER_C_SUPPRESSED,
    _MARKER_F_CONDITIONS,
    NaysayerPrReviewDriver,
    PrReviewOutcome,
    _build_messages,
)
from spirrow_mindwire.orchestrator import PrReviewOrchestrator
from spirrow_mindwire.repo_projects import PrProject
from spirrow_mindwire.value_objects import Role

PROJECT = "spirrow-playproof"
THREAD = "T-design-under-test"
ROSTER = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER, "Heisenberg": Role.IMPLEMENTER}
POLICY = ThreadPolicy(roster=ROSTER, human_identity="human")

FIX_1 = "reject an empty input before parsing it"
FIX_2 = "add a test for the empty-input case"


def _objections(*entries: dict[str, str]) -> str:
    return (
        "<details><summary>objections</summary>\n\n"
        "<!-- mindwire:objections v1 -->\n```json\n"
        + json.dumps(list(entries), ensure_ascii=False, indent=2)
        + "\n```\n\n</details>"
    )


def _blocking(fix_min: str, cls: str = "correctness") -> dict[str, str]:
    return {
        "class": cls,
        "where": "D3",
        "evidence": "an empty input reaches the parser",
        "fix_min": fix_min,
        "fix_cut": "drop the feature",
        "fix_weight": "small",
    }


ADVISORY = {"class": "structure", "where": "D1", "evidence": "two copies of one list"}


def _conditional_go(round_no: int = 6, *, fixes: tuple[str, ...] = (FIX_1, FIX_2)) -> str:
    ids = "\n".join(f"Cond-{round_no}-{i}" for i in range(1, len(fixes) + 1))
    block = _objections(*(_blocking(f) for f in fixes), ADVISORY)
    return (
        "principles_version: 2\n\nSound once these are applied.\n\n"
        f"Conditions ({len(fixes)}):\n{ids}\n\n{block}\n\n"
        "TIER-C-CHECK: none\nNEXT: Heisenberg"
    )


PLAIN_GO = (
    "principles_version: 2\n\nNo blocking objection.\n\n"
    + _objections()
    + "\n\nTIER-C-CHECK: none\nNEXT: Heisenberg"
)
DESIGN = "Design v1.\n\nNEXT: Einstein"
SEND_BACK = "Two blockers.\n\n" + _objections(_blocking(FIX_1)) + "\n\nNEXT: Bohr"


def _msgs(*items: tuple[str, str]) -> list[dict[str, Any]]:
    return [
        {"msg_id": f"msg-{i}", "author": author, "content": content}
        for i, (author, content) in enumerate(items, start=1)
    ]


def _pr_body(*rows: str, thread: str | None = THREAD, section: bool = True) -> str:
    parts = ["Summary of the change."]
    if thread is not None:
        parts.append(f"Thread: {thread}")
    if section:
        parts.append("## Design conditions\n" + "\n".join(["Design go: x/msg-2", *rows]))
    parts.append("## Read-back\nnothing here is a condition: Cond-9-9: ignore — applied: nowhere")
    return "\n\n".join(parts)


GOOD_ROWS = (
    f"Cond-6-1: {FIX_1} — applied: src/parser.py",
    f"Cond-6-2: {FIX_2} — applied: tests/test_parser.py::test_empty",
)


class _Chatroom:
    """The chatroom the resolver reads: one design thread, or a read that fails."""

    def __init__(self, messages: list[dict[str, Any]] | None, *, fail: bool = False) -> None:
        self._messages = messages
        self._fail = fail
        self.reads: list[dict[str, Any]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        assert name == "chatroom_get_thread"
        self.reads.append(arguments)
        if self._fail:
            raise MagickitMcpError("transport down")
        return {"thread": {"title": "design"}, "messages": self._messages}


async def _review(
    messages: list[dict[str, Any]] | None,
    pr_body: str | None,
    *,
    modes: dict[str, str] | None = None,
    fail_read: bool = False,
    model: str = "Looks fine.\n\nVERDICT: APPROVE",
    github: _FakeGitHub | None = None,
    driver_kwargs: dict[str, Any] | None = None,
) -> tuple[PrReviewOutcome, _FakeLexora, _FakeGitHub]:
    policy = DesignConditionsPolicy(
        modes=modes if modes is not None else {PROJECT: "issuing"}, roster=ROSTER
    )
    resolver = policy.resolver(_Chatroom(messages, fail=fail_read), project=PROJECT)
    assert resolver is not None
    lexora = _FakeLexora(content=model)
    gh = github if github is not None else _FakeGitHub()
    if pr_body is None:

        async def unreadable(pr: Any) -> Any:
            from spirrow_mindwire.github.client import PrResolution, PrState

            return PrState(ref=pr, resolution=PrResolution.UNRESOLVABLE)

        gh.fetch_pr_state = unreadable  # type: ignore[method-assign]
    else:
        gh.pr_body = pr_body  # type: ignore[attr-defined]
    _posted, post = _capture()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=gh, **(driver_kwargs or {}))
    outcome = await driver.review(_pr(), post_critique=post, design_conditions=resolver)
    return outcome, lexora, gh


def _user_prompt(lexora: _FakeLexora) -> str:
    return str(lexora.calls[0][1][1].content)


THREAD_WITH_GO = _msgs(("Bohr", DESIGN), ("Einstein", _conditional_go()))
GO_ID = "msg-2"


# ─── T2': the conditional go governs the PR (row 6) ─────────────────────────────────────────


@pytest.mark.anyio
async def test_matching_section_verifies_and_carries_the_canonical_list_to_the_model() -> None:
    outcome, lexora, _gh = await _review(THREAD_WITH_GO, _pr_body(*GOOD_ROWS))
    assert outcome.verdict is ReviewEvent.APPROVE
    prompt = _user_prompt(lexora)
    assert f"- Cond-6-1: {FIX_1}" in prompt
    assert f"- Cond-6-2: {FIX_2}" in prompt
    assert f"{THREAD}/{GO_ID}" in prompt
    assert "is a blocking objection" in prompt
    # Only the section reaches the model, not the rest of the PR body.
    assert "Summary of the change." not in prompt
    assert "Cond-9-9" not in prompt
    assert _MARKER_F_CONDITIONS not in outcome.body


@pytest.mark.anyio
async def test_a_model_request_changes_stands_under_a_verified_go() -> None:
    outcome, _lexora, _gh = await _review(
        THREAD_WITH_GO,
        _pr_body(*GOOD_ROWS),
        model="Cond-6-2 not applied.\n\nVERDICT: REQUEST_CHANGES",
    )
    assert outcome.verdict is ReviewEvent.REQUEST_CHANGES


@pytest.mark.parametrize(
    ("label", "body", "reason"),
    [
        ("section missing", _pr_body(section=False), "(i)"),
        ("one id missing", _pr_body(GOOD_ROWS[0], f"Cond-6-3: {FIX_2} — applied: x"), "(ii)"),
        ("fix_min mismatch", _pr_body(GOOD_ROWS[0], "Cond-6-2: a test — applied: x"), "(iii)"),
        ("row count", _pr_body(*GOOD_ROWS, GOOD_ROWS[1]), "(iv)"),
        # Einstein msg-7953: one row copied AND the count copied as one — still caught, because
        # k comes from the thread, never from the PR body.
        ("id and count both short", _pr_body(GOOD_ROWS[0]), "(ii)"),
        ("not in applied form", _pr_body(GOOD_ROWS[0], f"Cond-6-2: {FIX_2}"), "applied"),
    ],
)
@pytest.mark.anyio
async def test_t2_a_missing_or_wrong_copy_is_never_approved(
    label: str, body: str, reason: str
) -> None:
    outcome, _lexora, _gh = await _review(THREAD_WITH_GO, body)
    assert outcome.verdict is ReviewEvent.REQUEST_CHANGES, label
    assert _MARKER_F_CONDITIONS in outcome.body
    assert reason in outcome.body
    # The C note blames a partial review; there was none.
    assert _MARKER_C_SUPPRESSED not in outcome.body


def test_whitespace_differences_in_a_copied_fix_min_still_match() -> None:
    verdict = evaluate_thread(_as_messages(THREAD_WITH_GO), POLICY)
    body = _pr_body(f"Cond-6-1:   {FIX_1.replace(' ', '  ')}  — applied: a", GOOD_ROWS[1])
    assert check_pr_body(verdict, body, thread_id=THREAD).outcome is ConditionsOutcome.VERIFY


# ─── (v): the canonical list cannot be read → human ─────────────────────────────────────────


@pytest.mark.anyio
async def test_v_unreadable_thread_holds_for_the_human() -> None:
    outcome, _lexora, _gh = await _review(None, _pr_body(*GOOD_ROWS), fail_read=True)
    assert outcome.verdict is ReviewEvent.COMMENT
    assert "could not be read" in outcome.body


@pytest.mark.anyio
async def test_v_unparsed_objections_in_the_go_holds_for_the_human() -> None:
    broken = _conditional_go().replace("```json\n[", "```json\n[[", 1)
    outcome, _lexora, _gh = await _review(
        _msgs(("Bohr", DESIGN), ("Einstein", broken)), _pr_body(*GOOD_ROWS)
    )
    assert outcome.verdict is ReviewEvent.COMMENT
    assert "objections_unparsed" in outcome.body


_GOOD_GO_ENTRY = {"msg_id": "msg-2", "author": "Einstein", "content": _conditional_go()}


@pytest.mark.parametrize(
    ("label", "messages"),
    [
        # PR-gate #520 round 1: each of these used to drop or coerce the entry, leaving a thread
        # with no recognised conditional go → NOT_APPLICABLE → the model's APPROVE stood.
        ("null entry", [None]),
        ("non-object before the go", ["msg-1", _GOOD_GO_ENTRY]),
        ("go missing author", [{"msg_id": "msg-2", "content": _conditional_go()}]),
        ("go with empty author", [{**_GOOD_GO_ENTRY, "author": ""}]),
        ("go with non-string author", [{**_GOOD_GO_ENTRY, "author": 7}]),
        ("go missing msg_id", [{"author": "Einstein", "content": _conditional_go()}]),
        ("go missing content", [{"msg_id": "msg-2", "author": "Einstein"}]),
        ("go with non-string content", [{**_GOOD_GO_ENTRY, "content": ["x"]}]),
        (
            "malformed abort after the go",
            [_GOOD_GO_ENTRY, {"msg_id": "msg-3", "content": "Conditions-aborted: msg-2"}],
        ),
    ],
)
@pytest.mark.anyio
async def test_v_a_malformed_thread_message_holds_and_is_never_approved(
    label: str, messages: list[Any]
) -> None:
    for body in (_pr_body(*GOOD_ROWS), _pr_body(section=False)):
        outcome, _lexora, _gh = await _review(messages, body)
        assert outcome.verdict is ReviewEvent.COMMENT, label
        assert "could not be read" in outcome.body, label


def test_v_a_go_whose_ids_do_not_cover_every_blocking_objection_is_unreadable() -> None:
    go = _conditional_go().replace(
        "Conditions (2):\nCond-6-1\nCond-6-2", "Conditions (1):\nCond-6-1"
    )
    conditions, problem = canonical_conditions(go)
    assert conditions == ()
    assert problem is not None and "not among its conditions" in problem


def test_v_a_condition_naming_an_advisory_objection_is_unreadable() -> None:
    go = _conditional_go().replace("Cond-6-2", "Cond-6-3")
    _conditions, problem = canonical_conditions(go)
    assert problem is not None and "not blocking" in problem


# ─── the msg-7960 table, rows 1-5 and 7 ─────────────────────────────────────────────────────


def _as_messages(raw: list[dict[str, Any]]) -> list[ThreadMessage]:
    return [ThreadMessage(m["msg_id"], m["author"], m["content"]) for m in raw]


ABORT = (
    f"Cond-6-1 conflicts with the acceptance scope.\n\nConditions-aborted: {GO_ID}\n\nNEXT: Bohr"
)


@pytest.mark.parametrize(
    ("row", "thread", "outcome"),
    [
        (
            "1: two conditional gos",
            [("Bohr", DESIGN), ("Einstein", _conditional_go()), ("Einstein", _conditional_go(7))],
            ConditionsOutcome.HOLD,
        ),
        (
            "2: abort by a non-naysayer",
            [("Bohr", DESIGN), ("Einstein", _conditional_go()), ("Bohr", ABORT)],
            ConditionsOutcome.HOLD,
        ),
        (
            "2: abort naming another id",
            [
                ("Bohr", DESIGN),
                ("Einstein", _conditional_go()),
                ("Einstein", ABORT.replace(GO_ID, "msg-1")),
            ],
            ConditionsOutcome.HOLD,
        ),
        (
            "2: abort before the go",
            [("Einstein", ABORT), ("Einstein", _conditional_go())],
            ConditionsOutcome.HOLD,
        ),
        (
            "3: aborted, restart pending",
            [("Bohr", DESIGN), ("Einstein", _conditional_go()), ("Einstein", ABORT)],
            ConditionsOutcome.HOLD,
        ),
        (
            "3: aborted, only a proposer reply since",
            [
                ("Bohr", DESIGN),
                ("Einstein", _conditional_go()),
                ("Einstein", ABORT),
                ("Bohr", DESIGN),
            ],
            ConditionsOutcome.HOLD,
        ),
        (
            "4: aborted, then a normal go",
            [
                ("Bohr", DESIGN),
                ("Einstein", _conditional_go()),
                ("Einstein", ABORT),
                ("Bohr", DESIGN),
                ("Einstein", PLAIN_GO),
            ],
            ConditionsOutcome.NOT_APPLICABLE,
        ),
        (
            "4: aborted, then a human go",
            [
                ("Bohr", DESIGN),
                ("Einstein", _conditional_go()),
                ("Einstein", ABORT),
                ("human", "Go.\n\nNEXT: Heisenberg"),
            ],
            ConditionsOutcome.NOT_APPLICABLE,
        ),
        (
            "5: a later go with no abort",
            [("Bohr", DESIGN), ("Einstein", _conditional_go()), ("Einstein", PLAIN_GO)],
            ConditionsOutcome.HOLD,
        ),
        (
            "6: one conditional go",
            [("Bohr", DESIGN), ("Einstein", _conditional_go())],
            ConditionsOutcome.VERIFY,
        ),
        (
            "6: a relay NEXT to the implementer is not a go",
            [
                ("Bohr", DESIGN),
                ("Einstein", _conditional_go()),
                ("relay", "RC.\n\nNEXT: Heisenberg"),
            ],
            ConditionsOutcome.VERIFY,
        ),
        (
            "6: a go without TIER-C-CHECK is not a go",
            [
                ("Bohr", DESIGN),
                ("Einstein", _conditional_go()),
                ("Einstein", "ok\n\nNEXT: Heisenberg"),
            ],
            ConditionsOutcome.VERIFY,
        ),
        (
            "7: no conditional go",
            [("Bohr", DESIGN), ("Einstein", SEND_BACK), ("Bohr", DESIGN), ("Einstein", PLAIN_GO)],
            ConditionsOutcome.NOT_APPLICABLE,
        ),
        (
            "7: Conditions from a proposer is not a conditional go",
            [("Bohr", _conditional_go())],
            ConditionsOutcome.NOT_APPLICABLE,
        ),
        (
            "1 after abort: a second conditional go",
            [
                ("Bohr", DESIGN),
                ("Einstein", _conditional_go()),
                ("Einstein", ABORT),
                ("Einstein", _conditional_go(8)),
            ],
            ConditionsOutcome.HOLD,
        ),
    ],
)
def test_thread_table(row: str, thread: list[tuple[str, str]], outcome: ConditionsOutcome) -> None:
    assert evaluate_thread(_as_messages(_msgs(*thread)), POLICY).outcome is outcome, row


@pytest.mark.anyio
async def test_row3_a_matching_pr_is_not_approved_while_the_restart_is_pending() -> None:
    thread = _msgs(("Bohr", DESIGN), ("Einstein", _conditional_go()), ("Einstein", ABORT))
    outcome, _lexora, _gh = await _review(thread, _pr_body(*GOOD_ROWS))
    assert outcome.verdict is ReviewEvent.COMMENT
    assert "no go has followed yet" in outcome.body


@pytest.mark.anyio
async def test_row4_after_a_restart_the_old_conditions_are_not_checked() -> None:
    thread = _msgs(
        ("Bohr", DESIGN),
        ("Einstein", _conditional_go()),
        ("Einstein", ABORT),
        ("Bohr", DESIGN),
        ("Einstein", PLAIN_GO),
    )
    outcome, lexora, _gh = await _review(thread, _pr_body(section=False))
    assert outcome.verdict is ReviewEvent.APPROVE
    assert "DESIGN CONDITIONS" not in _user_prompt(lexora)


# ─── the Thread: line (Obj-3-2 / Obj-4-2): unknown correspondence is never approved ─────────


@pytest.mark.parametrize(
    "body",
    [
        _pr_body(section=False, thread=None),
        _pr_body(section=False, thread="see the design discussion"),
        _pr_body(section=False, thread=f"{THREAD} T-another-thread"),
        "",
    ],
)
@pytest.mark.anyio
async def test_a_verifying_project_never_approves_a_pr_whose_thread_is_unknown(body: str) -> None:
    # The thread has no conditional go at all: the hold is about the PR, not the thread. The check
    # is recomputed on every review from the PR and the thread, so no merge of another PR and no
    # resolve of the thread can turn this into "no conditions".
    outcome, _lexora, _gh = await _review(_msgs(("Einstein", PLAIN_GO)), body)
    assert outcome.verdict is ReviewEvent.COMMENT
    assert "names no single design thread" in outcome.body


@pytest.mark.anyio
async def test_an_unreadable_pr_body_holds() -> None:
    outcome, _lexora, _gh = await _review(THREAD_WITH_GO, None)
    assert outcome.verdict is ReviewEvent.COMMENT


@pytest.mark.anyio
async def test_a_pr_whose_thread_has_no_conditional_go_reviews_as_before() -> None:
    body = _pr_body(section=False)
    outcome, lexora, _gh = await _review(_msgs(("Einstein", PLAIN_GO)), body)
    assert outcome.verdict is ReviewEvent.APPROVE
    assert (
        _user_prompt(lexora) == _build_messages("diff --git a/x b/x\n+added", _pr().slug)[1].content
    )
    assert _MARKER_F_CONDITIONS not in outcome.body


@pytest.mark.anyio
async def test_two_prs_of_one_conditional_go_are_each_checked_in_full() -> None:
    # msg-7956: conditions are not split across PRs. Each PR is checked against all of them.
    complete, _l1, _g1 = await _review(THREAD_WITH_GO, _pr_body(*GOOD_ROWS))
    partial, _l2, _g2 = await _review(THREAD_WITH_GO, _pr_body(GOOD_ROWS[1]))
    assert complete.verdict is ReviewEvent.APPROVE
    assert partial.verdict is ReviewEvent.REQUEST_CHANGES


# ─── reuse paths: an APPROVE is only posted by a review that saw the check ──────────────────


@pytest.mark.anyio
async def test_a_cached_approve_is_not_reused_while_a_check_applies() -> None:
    gh = _FakeGitHub(
        ci=CiStatus(CiState.SUCCESS, "headsha", []),
        reviews=[ReviewInfo("spirrowgames-ops", "APPROVED", "headsha", "2026-06-10T00:00:00Z")],
    )
    outcome, lexora, _gh = await _review(
        THREAD_WITH_GO,
        _pr_body(section=False),
        github=gh,
        driver_kwargs={"skip_if_head_unchanged": True},
    )
    assert outcome.skipped_head_unchanged is False
    assert lexora.calls, "a full review ran instead of reusing the APPROVE"
    assert outcome.verdict is ReviewEvent.REQUEST_CHANGES


# ─── the three-state setting (Obj-5-1) ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "mode", "verifies", "issues"),
    [
        (None, ConditionalProceedMode.ABSENT, False, False),
        ("issuing", ConditionalProceedMode.ISSUING, True, True),
        ("verify_only", ConditionalProceedMode.VERIFY_ONLY, True, False),
        ("off", ConditionalProceedMode.UNKNOWN, True, False),
        ("", ConditionalProceedMode.UNKNOWN, True, False),
    ],
)
def test_mode_for(
    value: str | None, mode: ConditionalProceedMode, verifies: bool, issues: bool
) -> None:
    modes = {} if value is None else {PROJECT: value}
    resolved = mode_for(modes, PROJECT)
    assert resolved is mode
    assert resolved.verifies is verifies
    assert resolved.issues is issues


@pytest.mark.parametrize("before_after", [("issuing", "verify_only"), ("issuing", "bogus")])
@pytest.mark.anyio
async def test_stopping_issuance_keeps_checking_issued_conditions(
    before_after: tuple[str, str],
) -> None:
    for value in before_after:
        missing, _l, _g = await _review(
            THREAD_WITH_GO, _pr_body(GOOD_ROWS[0]), modes={PROJECT: value}
        )
        no_thread, _l, _g = await _review(
            THREAD_WITH_GO, _pr_body(thread=None), modes={PROJECT: value}
        )
        assert missing.verdict is ReviewEvent.REQUEST_CHANGES, value
        assert no_thread.verdict is ReviewEvent.COMMENT, value


def test_a_project_that_never_set_the_key_builds_no_check() -> None:
    policy = DesignConditionsPolicy(modes={"other-project": "issuing"}, roster=ROSTER)
    assert policy.resolver(_Chatroom([]), project=PROJECT) is None
    assert build_policy({}, ROSTER, human_identity="human") is None


class _RecordingDriver:
    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []

    async def review(self, pr: PrRef, **kwargs: Any) -> PrReviewOutcome:
        self.kwargs.append(kwargs)
        await kwargs["post_critique"]("LGTM\n\nVERDICT: APPROVE")
        return PrReviewOutcome(
            verdict=ReviewEvent.APPROVE,
            body="LGTM\n\nVERDICT: APPROVE",
            ci_state=CiState.SUCCESS,
            head_sha="sha1",
        )


@pytest.mark.parametrize(
    ("modes", "passed"),
    [
        ({}, False),
        ({"other": "issuing"}, False),
        ({PROJECT: "issuing"}, True),
        ({PROJECT: "verify_only"}, True),
    ],
)
@pytest.mark.anyio
async def test_the_orchestrator_passes_a_check_only_for_a_project_that_set_the_key(
    modes: dict[str, str], passed: bool
) -> None:
    driver = _RecordingDriver()
    orch = PrReviewOrchestrator(
        _FakeMcp(),
        driver=driver,  # type: ignore[arg-type]
        design_conditions=build_policy(modes, ROSTER, human_identity="human"),
    )
    await orch.fire_pr_review(
        project=PROJECT,
        pr_ref="org/repo#5",
        design_thread=_DESIGN_THREAD,
        implementer="Heisenberg",
    )
    assert ("design_conditions" in driver.kwargs[0]) is passed
    if not passed:
        # Byte-identical call: exactly the arguments the driver was called with before.
        assert set(driver.kwargs[0]) == {"post_critique", "read_review_thread"}


# ─── pure helpers ───────────────────────────────────────────────────────────────────────────


def test_the_section_is_cut_at_the_next_heading() -> None:
    section = design_conditions_section(_pr_body(*GOOD_ROWS))
    assert section is not None
    assert "Cond-6-1" in section and "Cond-9-9" not in section
    assert design_conditions_section("no section here") is None


def test_inline_mentions_of_the_formats_are_not_markers() -> None:
    prose = (
        "We will write `Conditions (k):` and `Conditions-aborted: <msg-id>` lines.\n\n"
        "TIER-C-CHECK: none\nNEXT: Heisenberg"
    )
    verdict = evaluate_thread(_as_messages(_msgs(("Einstein", prose))), POLICY)
    assert verdict.outcome is ConditionsOutcome.NOT_APPLICABLE


# ─── the scoped hand-run driver (no chatroom): fail-closed where the setting is in use ──────


def _load_scoped_driver() -> Any:
    """Import ``scripts/naysayer_review_scoped.py`` for real (``scripts/`` is not a package).

    PR 1 could only parse it: on origin/main it imported ``_DEFAULT_MAX_TOKENS``, a name
    ``pr_review`` no longer defines, so the fail-closed cap below had never been able to run.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "naysayer_review_scoped.py"
    spec = importlib.util.spec_from_file_location("naysayer_review_scoped_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_scoped_driver_imports() -> None:
    module = _load_scoped_driver()
    assert callable(module.main)


class _ScopedLexora:
    def __init__(self, **_: Any) -> None:
        pass

    async def chat_completion(self, **_: Any) -> Any:
        from spirrow_mindwire.lexora.client import ChatCompletion

        body = "Looks fine.\n\n<!-- mindwire:objections v1 -->\n[]\n\nVERDICT: APPROVE"
        return ChatCompletion(body, None, "stop", "m", {"completion_tokens": 7})

    async def aclose(self) -> None:
        pass


class _ScopedGitHub:
    submitted: list[ReviewEvent]

    def __init__(self, *_: Any) -> None:
        self.submitted = []
        _ScopedGitHub.last = self

    async def fetch_ci_status(self, _pr: PrRef) -> CiStatus:
        return CiStatus(state=CiState.SUCCESS, head_sha="abc123", failing=[])

    async def fetch_pr_diff(self, _pr: PrRef) -> str:
        return "diff --git a/x b/x\n+one line\n"

    async def submit_review(self, _pr: PrRef, *, event: ReviewEvent, body: str) -> None:
        self.submitted.append(event)

    async def aclose(self) -> None:
        pass

    last: _ScopedGitHub


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({"spirrow-playproof": "issuing"}, ReviewEvent.COMMENT),
        ({"spirrow-playproof": "verify_only"}, ReviewEvent.COMMENT),
        ({}, ReviewEvent.APPROVE),
        (None, ReviewEvent.COMMENT),  # unreadable settings count as "in use"
    ],
)
def test_the_scoped_driver_caps_its_approve_where_the_setting_is_in_use(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    settings: dict[str, str] | None,
    expected: ReviewEvent,
) -> None:
    """The real ``main()``: the model says APPROVE, CI is green; what reaches GitHub?"""
    import asyncio
    import sys
    from types import SimpleNamespace

    module = _load_scoped_driver()

    def load_settings() -> Any:
        if settings is None:
            raise RuntimeError("settings unreadable")
        return SimpleNamespace(naysayer_gating=SimpleNamespace(conditional_proceed=settings))

    scope = tmp_path / "scope.txt"
    scope.write_text("only this", encoding="utf-8")
    monkeypatch.setattr(module, "load_settings", load_settings)
    monkeypatch.setattr(module, "LexoraClient", _ScopedLexora)
    monkeypatch.setattr(module, "GitHubClient", _ScopedGitHub)
    monkeypatch.setattr(module, "naysayer_github_token", lambda: "token")
    monkeypatch.setattr(
        sys, "argv", ["naysayer_review_scoped.py", "--pr", "o/r#1", "--scope-file", str(scope)]
    )
    asyncio.run(module.main())
    assert _ScopedGitHub.last.submitted == [expected]


# ---- a project-qualified ``Thread:`` line (T-gate-fires-on-pr-open, msg-8022 / msg-8024) ---- #


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "read_in"),
    [
        ("Thread: spirrow-mindwire/T-cond", "spirrow-mindwire"),
        (f"Thread: {PROJECT}/T-cond", PROJECT),
        ("Thread: T-cond", PROJECT),
    ],
)
async def test_qualified_thread_line_is_read_in_the_project_it_names(
    body: str, read_in: str
) -> None:
    chat = _Chatroom([])
    await resolve_design_conditions(chat, project=PROJECT, pr_body=body, policy=POLICY)
    assert [(r["project"], r["thread_id"]) for r in chat.reads] == [(read_in, "T-cond")]


# ---- the PR's own project, not the gate's (PR #525 gate RC, design_conditions.py:449) ---- #


def _map(**repos: str) -> Any:
    """``clones`` whose origins are ``github.com/<repo>``; ``repos``: project -> ``owner/repo``."""
    from spirrow_mindwire.repo_projects import build_repo_project_map

    return build_repo_project_map(
        {p: f"/clone/{p}" for p in repos},
        origin_url=lambda clone: f"https://github.com/{repos[clone.rsplit('/', 1)[1]]}.git",
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "pr_project",
    [PrProject("proj"), PrProject(None, "none"), PrProject(None, "multiple", ("a", "b"))],
)
async def test_qualified_plus_bare_on_another_projects_pr_is_ambiguous_not_collapsed(
    pr_project: PrProject,
) -> None:
    """The gate's RC: gate ``hub``, PR of ``proj`` (or unresolved) -> two links, a HOLD."""
    chat = _Chatroom([])
    check = await resolve_design_conditions(
        chat,
        project="hub",
        pr_body="Thread: hub/T-1\nCloses-thread: T-1",
        policy=POLICY,
        pr_project=pr_project,
    )
    assert check.outcome is ConditionsOutcome.HOLD
    assert "ambiguous" in check.reasons[0]
    assert chat.reads == []


@pytest.mark.anyio
async def test_qualified_plus_bare_is_one_link_when_the_pr_belongs_to_the_named_project() -> None:
    chat = _Chatroom([])
    await resolve_design_conditions(
        chat,
        project="gate",
        pr_body="Thread: hub/T-1\nCloses-thread: T-1",
        policy=POLICY,
        pr_project=PrProject("hub"),
    )
    assert [(r["project"], r["thread_id"]) for r in chat.reads] == [("hub", "T-1")]


@pytest.mark.anyio
async def test_a_bare_id_is_read_in_the_prs_project_not_the_gates() -> None:
    chat = _Chatroom([])
    await resolve_design_conditions(
        chat, project="hub", pr_body="Thread: T-cond", policy=POLICY, pr_project=PrProject("proj")
    )
    assert [(r["project"], r["thread_id"]) for r in chat.reads] == [("proj", "T-cond")]


@pytest.mark.anyio
async def test_a_bare_id_with_an_unresolved_pr_project_holds_without_reading() -> None:
    chat = _Chatroom([])
    check = await resolve_design_conditions(
        chat,
        project="hub",
        pr_body="Thread: T-cond",
        policy=POLICY,
        pr_project=PrProject(None, "incomplete", ("app",)),
    )
    assert check.outcome is ConditionsOutcome.HOLD
    assert "incomplete: app" in check.reasons[0]
    assert check.thread_id == "T-cond"
    assert chat.reads == []


@pytest.mark.anyio
async def test_a_qualified_id_needs_no_pr_project() -> None:
    chat = _Chatroom([])
    await resolve_design_conditions(
        chat,
        project="gate",
        pr_body="Thread: hub/T-cond",
        policy=POLICY,
        pr_project=PrProject(None, "none"),
    )
    assert [(r["project"], r["thread_id"]) for r in chat.reads] == [("hub", "T-cond")]


def test_the_policy_resolves_the_pr_project_from_the_repo_table() -> None:
    policy = DesignConditionsPolicy(
        roster=ROSTER, repo_projects=lambda: _map(proj="org/repo", hub="org/hub")
    )
    assert policy.pr_project(project="hub", owner="org", repo="repo") == PrProject("proj")
    assert policy.pr_project(project="hub", owner="org", repo="else").project is None
    assert policy.pr_project(project="hub", owner=None, repo=None).project is None
    # Two projects on one repo: unresolved, never the gate's project.
    shared = DesignConditionsPolicy(
        roster=ROSTER, repo_projects=lambda: _map(hub="org/repo", proj="org/repo")
    )
    assert shared.pr_project(project="hub", owner="org", repo="repo").reason == "multiple"
    # No table wired: the pre-#525 reading (the gate's project).
    assert DesignConditionsPolicy(roster=ROSTER).pr_project(
        project="hub", owner="org", repo="repo"
    ) == PrProject("hub")


def test_an_unreadable_repo_table_resolves_nothing() -> None:
    def boom() -> Any:
        raise RuntimeError("sweep.json gone")

    policy = DesignConditionsPolicy(roster=ROSTER, repo_projects=boom)
    assert policy.pr_project(project="hub", owner="org", repo="repo").project is None


@pytest.mark.anyio
async def test_the_orchestrator_hands_the_prs_repo_to_the_resolver() -> None:
    """End to end: gate project ``hub``, PR ``org/repo#5`` of ``proj``, bare ``Thread:``."""
    seen: list[tuple[str, str]] = []

    class _Table:
        def resolve(self, owner: str, repo: str) -> PrProject:
            seen.append((owner, repo))
            return PrProject("proj")

    mcp = _FakeMcp()
    driver = _RecordingDriver()
    orch = PrReviewOrchestrator(
        mcp,
        driver=driver,  # type: ignore[arg-type]
        design_conditions=build_policy(
            {"hub": "issuing"},
            ROSTER,
            human_identity="human",
            repo_projects=lambda: _Table(),  # type: ignore[arg-type,return-value]
        ),
    )
    await orch.fire_pr_review(
        project="hub", pr_ref="org/repo#5", design_thread=_DESIGN_THREAD, implementer="Heisenberg"
    )
    resolver = driver.kwargs[0]["design_conditions"]
    mcp.calls.clear()
    await resolver("Thread: T-cond")
    assert seen == [("org", "repo")]
    reads = [(a["project"], a["thread_id"]) for n, a in mcp.calls if n == "chatroom_get_thread"]
    assert reads == [("proj", "T-cond")]
