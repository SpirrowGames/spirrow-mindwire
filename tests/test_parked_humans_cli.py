"""Tests for the parked-humans read-out CLI (``scripts/parked_humans.py``).

This CLI is the ``mindwire``-side owner of the "which sweep candidate is parked on a human?"
judgment (msg-1391 §13.2 / §13.3 for the D-32 grammar-ownership rule). The PowerShell sweep
wrapper calls it once per project per tick, so these tests exercise:

  * the parser is the SAME one the conductor uses (``spirrow_mindwire.conductor.handoff``) —
    there is no re-spelling of ``NEXT:`` on the CLI side. We verify by sending inputs pinned
    against ``tests/data/next_line_corpus.tsv``-shaped decoration (``**NEXT: human**``, etc.),
    which the parser handles.
  * fail-CLOSED on the parked side (msg-1391 §13.3): a fetch error is recorded in ``errors``
    and the candidate is EXCLUDED from ``parked``. A head that moved between the probe and
    the fetch is likewise excluded, no error row.
  * only :attr:`HandoffKind.HUMAN` counts as parked. Role handoffs, ``NEXT: none``, absent /
    unparseable ``NEXT:`` lines, and ``pr-review`` sentinels all yield NOT parked (no error).

The MCP fetch path is monkey-patched to isolate the tests from network I/O — same pattern as
``test_head_skip_cli.py``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from spirrow_mindwire.magickit.client import MagickitMcpError

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "parked_humans.py"


def _load_cli_module() -> object:
    """Load the standalone CLI script as a module (it is not on the import path)."""
    spec = importlib.util.spec_from_file_location("_parked_humans_cli_test_module", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_cli_module()


class _FakeMcp:
    """Stand-in for :class:`StreamableHttpChatroomMcp` — returns canned bodies per thread.

    A value of ``None`` for a thread simulates ``_fetch_last_message`` returning ``None`` (empty
    thread / malformed response). A :class:`MagickitMcpError` raises through ``call_tool``, which
    is the fetch-failure path.
    """

    def __init__(self, bodies: Mapping[str, object]) -> None:
        self._bodies = bodies

    async def call_tool(self, name: str, params: dict[str, object]) -> object:
        assert name == "chatroom_get_thread"
        thread_id = str(params["thread_id"])
        result = self._bodies.get(thread_id)
        if isinstance(result, Exception):
            raise result
        if result is None:
            return {"messages": []}
        # By construction the fixture stores (msg_id, body) tuples in the non-None / non-Exception
        # slots; help mypy narrow past the ``object`` element type of the invariant Mapping.
        assert isinstance(result, tuple)
        msg_id, body = result
        return {"messages": [{"msg_id": msg_id, "content": body}]}


def _patch_mcp(monkeypatch: pytest.MonkeyPatch, bodies: Mapping[str, object]) -> None:
    fake = _FakeMcp(bodies)
    monkeypatch.setattr(
        _MODULE,
        "StreamableHttpChatroomMcp",
        lambda *_args, **_kwargs: fake,
    )


# ---------------------------------------------------------------------------
# Happy path: HUMAN vs. non-HUMAN classification via the shared parser
# ---------------------------------------------------------------------------


def test_human_handoff_lands_in_parked(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mcp(
        monkeypatch,
        {
            "T-a": ("msg-a1", "some prose\n\nNEXT: human"),
        },
    )
    result = asyncio.run(
        _MODULE._poll(  # type: ignore[attr-defined]
            project="test", candidates=[{"thread_id": "T-a", "head_msg_id": "msg-a1"}], url=None
        )
    )
    assert result["polled"] == 1
    assert len(result["parked"]) == 1
    entry = result["parked"][0]
    assert entry["thread_id"] == "T-a"
    assert entry["head_msg_id"] == "msg-a1"
    assert entry["token"] == "human"
    assert result["errors"] == []


def test_role_handoff_is_not_parked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``NEXT: <persona>`` is a live loop, not a human park."""
    _patch_mcp(
        monkeypatch,
        {
            "T-a": ("msg-a1", "some prose\n\nNEXT: Bohr"),
            "T-b": ("msg-b1", "reply\n\nNEXT: Einstein"),
        },
    )
    result = asyncio.run(
        _MODULE._poll(  # type: ignore[attr-defined]
            project="test",
            candidates=[
                {"thread_id": "T-a", "head_msg_id": "msg-a1"},
                {"thread_id": "T-b", "head_msg_id": "msg-b1"},
            ],
            url=None,
        )
    )
    assert result["parked"] == []
    assert result["errors"] == []


def test_empty_roster_never_raises_for_any_handoff_shape() -> None:
    """Direct source-level pin on the empty-roster invariant (Einstein's msg-1393 concern).

    Load-bearing: this CLI calls ``resolve_handoff(body, {})`` on the working assumption that a
    role handoff (``NEXT: Bohr``, ``NEXT: pr-review …``, ``NEXT: totally-unknown``, etc.) with
    an empty roster falls through to :attr:`HandoffKind.ABSENT` **without raising**. If a future
    refactor of ``handoff.py`` or ``_roster_lookup`` breaks that assumption, the per-candidate
    fallback in ``_poll`` would either crash the whole poll (whole digest section blanks) or
    log the role handoff as a spurious fetch error (permanent phantom entry under 取得失敗).

    Both failure modes would degrade the ``判断待ち`` section without an obvious signal. Rather
    than *only* leaning on the ``_poll``-level integration test (`test_role_handoff_is_not_parked`)
    which would silently keep passing if the underlying resolver started raising and our
    catch-all swallowed it, we pin the invariant directly on the resolver. Two guarantees hold
    together, one at each layer.
    """
    # A load-bearing sample: every non-HUMAN shape that could plausibly appear at the last
    # message of a sweep candidate. Empty roster on every call — the mode this CLI operates in.
    from spirrow_mindwire.conductor.handoff import HandoffKind, resolve_handoff

    role_shapes = [
        "NEXT: Bohr",
        "NEXT: Einstein",
        "NEXT: heisenberg",  # lowercase
        "NEXT: totally-unknown-persona",
        "**NEXT: Bohr**",  # decorated (last-wins still applies)
        "> NEXT: Bohr",  # quoted
        "reply text\n\nNEXT: Bohr — with a gloss",
    ]
    for body in role_shapes:
        result = resolve_handoff(body, {})
        assert result.kind is HandoffKind.ABSENT, (
            f"empty roster on {body!r} should degrade to ABSENT, got {result.kind.name}"
        )

    # ``none`` is a reserved sentinel — it resolves BEFORE the roster, so an empty roster does
    # not change its outcome. Included so a regression that starts routing NONE through the
    # roster (breaking the resolution order) is caught here too.
    assert resolve_handoff("NEXT: none", {}).kind is HandoffKind.NONE

    # ``human`` is the positive control. Same reservation — resolved before roster lookup —
    # so an empty roster must still resolve to HUMAN. This is the specific case parked_humans.py
    # depends on; if this ever fails, this whole file's premise is wrong.
    assert resolve_handoff("NEXT: human", {}).kind is HandoffKind.HUMAN


def test_role_handoff_leaves_errors_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Einstein's msg-1393 concern (2): a role handoff must NOT inflate ``errors[]``.

    A role handoff is a live loop, not a fetch failure. If we accidentally recorded it in
    ``errors`` — e.g. because the resolver started raising and our per-candidate catch swept
    the exception into a phantom error row — the digest would show a permanent ``取得失敗``
    line for a thread that is actually running normally. This test verifies the negative:
    across a mixed candidate list (parked human + several role handoffs + a genuinely broken
    fetch), only the genuinely broken fetch lands in ``errors``.
    """
    _patch_mcp(
        monkeypatch,
        {
            "T-parked": ("msg-1", "some prose\n\nNEXT: human"),
            "T-role-a": ("msg-2", "NEXT: Bohr"),
            "T-role-b": ("msg-3", "NEXT: Einstein"),
            "T-none": ("msg-4", "NEXT: none"),
            "T-broken": MagickitMcpError("real fetch failure"),
        },
    )
    result = asyncio.run(
        _MODULE._poll(  # type: ignore[attr-defined]
            project="test",
            candidates=[
                {"thread_id": "T-parked", "head_msg_id": "msg-1"},
                {"thread_id": "T-role-a", "head_msg_id": "msg-2"},
                {"thread_id": "T-role-b", "head_msg_id": "msg-3"},
                {"thread_id": "T-none", "head_msg_id": "msg-4"},
                {"thread_id": "T-broken", "head_msg_id": "msg-5"},
            ],
            url=None,
        )
    )
    assert [p["thread_id"] for p in result["parked"]] == ["T-parked"]
    # Only the real fetch failure — the three role/none handoffs are NOT phantom errors.
    assert [e["thread_id"] for e in result["errors"]] == ["T-broken"]


def test_none_and_absent_and_pr_review_are_not_parked(monkeypatch: pytest.MonkeyPatch) -> None:
    """``NEXT: none``, no ``NEXT:`` at all, and ``NEXT: pr-review <ref>`` are not human parking."""
    _patch_mcp(
        monkeypatch,
        {
            "T-settled": ("msg-1", "done\n\nNEXT: none"),
            "T-absent": ("msg-2", "just a plain reply with no directive"),
            "T-pr": ("msg-3", "opened\n\nNEXT: pr-review acme/widgets#7"),
        },
    )
    result = asyncio.run(
        _MODULE._poll(  # type: ignore[attr-defined]
            project="test",
            candidates=[
                {"thread_id": "T-settled", "head_msg_id": "msg-1"},
                {"thread_id": "T-absent", "head_msg_id": "msg-2"},
                {"thread_id": "T-pr", "head_msg_id": "msg-3"},
            ],
            url=None,
        )
    )
    assert result["parked"] == []
    assert result["errors"] == []


def test_shared_parser_handles_the_same_decoration_as_the_conductor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bold/italic/quoted ``NEXT: human`` decorations resolve the same way for us as for the loop.

    The whole point of D-32 is that the parking answer comes from the SAME parser the conductor
    uses — no re-spelling. This test proves that by feeding shapes the corpus pins as
    handoff-tolerant and asserting we recognise them as HUMAN.
    """
    bodies = {
        "T-bold": ("msg-1", "**NEXT: human**"),
        "T-quote": ("msg-2", "> NEXT: human"),
        "T-underscore": ("msg-3", "_NEXT: human_"),
        "T-inside": ("msg-4", "**NEXT**: human"),
        "T-fullwidth": ("msg-5", "NEXT： human"),  # noqa: RUF001 (fullwidth colon on purpose)
    }
    _patch_mcp(monkeypatch, bodies)
    result = asyncio.run(
        _MODULE._poll(  # type: ignore[attr-defined]
            project="test",
            candidates=[{"thread_id": tid, "head_msg_id": hid} for tid, (hid, _) in bodies.items()],
            url=None,
        )
    )
    parked_ids = {p["thread_id"] for p in result["parked"]}
    assert parked_ids == set(bodies.keys()), (
        f"decoration shapes handled by the parser lost their parking status: {result}"
    )


def test_last_next_wins_earlier_quoted_next_does_not_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parser's last-wins rule protects us the same way it protects the conductor.

    An earlier quoted ``NEXT: Bohr`` in the body must not override the real final
    ``NEXT: human`` — otherwise a message that quotes an earlier handoff line during critique
    would flip the parking answer.
    """
    body = "> NEXT: Bohr — quoted for reference\n\nMy actual reply here.\n\nNEXT: human"
    _patch_mcp(monkeypatch, {"T-a": ("msg-1", body)})
    result = asyncio.run(
        _MODULE._poll(  # type: ignore[attr-defined]
            project="test", candidates=[{"thread_id": "T-a", "head_msg_id": "msg-1"}], url=None
        )
    )
    assert len(result["parked"]) == 1
    assert result["parked"][0]["thread_id"] == "T-a"


# ---------------------------------------------------------------------------
# Fail-CLOSED direction (msg-1391 §13.3)
# ---------------------------------------------------------------------------


def test_fetch_error_is_recorded_and_excluded_from_parked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``MagickitMcpError`` on ``chatroom_get_thread`` records ``errors`` and drops the candidate.

    Load-bearing: without this, an outage would blank the whole digest section (looks like "no
    one is parked" — the silent-degradation this file exists not to do). With this, the count
    goes down but the outage IS visible in the CLI output (and thus in the wrapper's log).
    """
    _patch_mcp(
        monkeypatch,
        {
            "T-a": ("msg-a1", "content\n\nNEXT: human"),
            "T-b": MagickitMcpError("simulated MCP transport down"),
        },
    )
    result = asyncio.run(
        _MODULE._poll(  # type: ignore[attr-defined]
            project="test",
            candidates=[
                {"thread_id": "T-a", "head_msg_id": "msg-a1"},
                {"thread_id": "T-b", "head_msg_id": "msg-b1"},
            ],
            url=None,
        )
    )
    assert [p["thread_id"] for p in result["parked"]] == ["T-a"]
    assert len(result["errors"]) == 1
    err = result["errors"][0]
    assert err["thread_id"] == "T-b"
    assert "simulated MCP transport down" in err["reason"]


def test_moved_head_is_silently_excluded_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the fetched thread's actual head differs from the probe's, do NOT claim parking.

    Not an error — the head moved legitimately between the probe and the fetch (a naysayer may
    have posted, another sweep tick may have advanced the thread). Recording it in ``errors``
    would confuse the operator; excluding it from ``parked`` is the correct silent skip.
    """
    _patch_mcp(
        monkeypatch,
        {
            # Probe said the head was msg-100, but the thread has moved to msg-101 by fetch time.
            "T-a": ("msg-101", "new reply\n\nNEXT: human"),
        },
    )
    result = asyncio.run(
        _MODULE._poll(  # type: ignore[attr-defined]
            project="test",
            candidates=[{"thread_id": "T-a", "head_msg_id": "msg-100"}],
            url=None,
        )
    )
    assert result["parked"] == []
    assert result["errors"] == []


def test_empty_expected_head_trusts_fetched_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the probe did not report a head (empty ``head_msg_id``), skip the cross-check.

    ``thread_heads.py`` documents this failure mode: a thread absent from the probe's inbox
    result may still be alive. Our caller passes ``head_msg_id=""`` in that case and we trust
    the fetched thread's own last-message id.
    """
    _patch_mcp(monkeypatch, {"T-a": ("msg-fresh", "content\n\nNEXT: human")})
    result = asyncio.run(
        _MODULE._poll(  # type: ignore[attr-defined]
            project="test",
            candidates=[{"thread_id": "T-a", "head_msg_id": ""}],
            url=None,
        )
    )
    assert len(result["parked"]) == 1
    assert result["parked"][0]["head_msg_id"] == "msg-fresh"


def test_unusable_fetched_shape_is_recorded_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A thread returned with no messages (or a mangled shape) is an error, not parked."""
    _patch_mcp(
        monkeypatch,
        {
            "T-empty": None,  # → ``{"messages": []}`` → ``_fetch_last_message`` returns None
        },
    )
    result = asyncio.run(
        _MODULE._poll(  # type: ignore[attr-defined]
            project="test",
            candidates=[{"thread_id": "T-empty", "head_msg_id": "msg-1"}],
            url=None,
        )
    )
    assert result["parked"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["thread_id"] == "T-empty"


def test_unexpected_backend_exception_does_not_crash_whole_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend that raised something outside ``MagickitMcpError`` still keeps the poll running.

    Load-bearing: a single misbehaving thread must not blank the entire digest count. The
    outage IS recorded in ``errors`` so it is visible.
    """
    _patch_mcp(
        monkeypatch,
        {
            "T-good": ("msg-1", "content\n\nNEXT: human"),
            "T-boom": RuntimeError("client returned nonsense"),
        },
    )
    result = asyncio.run(
        _MODULE._poll(  # type: ignore[attr-defined]
            project="test",
            candidates=[
                {"thread_id": "T-good", "head_msg_id": "msg-1"},
                {"thread_id": "T-boom", "head_msg_id": "msg-2"},
            ],
            url=None,
        )
    )
    assert [p["thread_id"] for p in result["parked"]] == ["T-good"]
    assert len(result["errors"]) == 1
    err = result["errors"][0]
    assert err["thread_id"] == "T-boom"
    assert "RuntimeError" in err["reason"]
    assert "client returned nonsense" in err["reason"]


# ---------------------------------------------------------------------------
# Shape / bookkeeping
# ---------------------------------------------------------------------------


def test_polled_count_matches_input_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """``polled`` is the input candidate count, regardless of how many were parked or errored.

    This is the "何件にしたか記録する" contract from msg-1370 §I-4: the caller must be able to
    see how much of the corpus we actually looked at, not just the survivor count.
    """
    _patch_mcp(
        monkeypatch,
        {
            "T-a": ("msg-1", "NEXT: human"),
            "T-b": ("msg-2", "NEXT: Bohr"),
            "T-c": MagickitMcpError("boom"),
        },
    )
    result = asyncio.run(
        _MODULE._poll(  # type: ignore[attr-defined]
            project="test",
            candidates=[
                {"thread_id": "T-a", "head_msg_id": "msg-1"},
                {"thread_id": "T-b", "head_msg_id": "msg-2"},
                {"thread_id": "T-c", "head_msg_id": "msg-3"},
            ],
            url=None,
        )
    )
    assert result["polled"] == 3
    assert len(result["parked"]) == 1
    assert len(result["errors"]) == 1


def test_parked_preserves_input_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """The digest reads ``parked`` top-to-bottom; the order must be stable across ticks."""
    _patch_mcp(
        monkeypatch,
        {
            "T-a": ("msg-1", "NEXT: human"),
            "T-b": ("msg-2", "NEXT: Bohr"),
            "T-c": ("msg-3", "NEXT: human"),
        },
    )
    result = asyncio.run(
        _MODULE._poll(  # type: ignore[attr-defined]
            project="test",
            candidates=[
                {"thread_id": "T-a", "head_msg_id": "msg-1"},
                {"thread_id": "T-b", "head_msg_id": "msg-2"},
                {"thread_id": "T-c", "head_msg_id": "msg-3"},
            ],
            url=None,
        )
    )
    assert [p["thread_id"] for p in result["parked"]] == ["T-a", "T-c"]


def test_empty_candidate_list_returns_empty_parked(monkeypatch: pytest.MonkeyPatch) -> None:
    """No candidates → ``polled=0`` and ``parked=[]``. The digest still emits a "0 件" section."""
    _patch_mcp(monkeypatch, {})
    result = asyncio.run(_MODULE._poll(project="test", candidates=[], url=None))  # type: ignore[attr-defined]
    assert result == {"project": "test", "polled": 0, "parked": [], "errors": []}


def test_malformed_candidate_entries_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A candidate missing ``thread_id`` is silently skipped (same rule as head_skip_decide).

    Silently, because the sweep wrapper controls the input and a missing thread_id is a wrapper
    bug — the CLI's job is to be robust, not to double-report on programmer errors. The wrapper's
    own tests pin the input shape.
    """
    _patch_mcp(monkeypatch, {"T-a": ("msg-1", "NEXT: human")})
    result = asyncio.run(
        _MODULE._poll(  # type: ignore[attr-defined]
            project="test",
            candidates=[
                {"head_msg_id": "msg-x"},  # no thread_id
                {"thread_id": "T-a", "head_msg_id": "msg-1"},
                {"thread_id": "", "head_msg_id": "msg-y"},  # empty thread_id
            ],
            url=None,
        )
    )
    assert [p["thread_id"] for p in result["parked"]] == ["T-a"]


# ---------------------------------------------------------------------------
# CLI wrapper: JSON I/O
# ---------------------------------------------------------------------------


def test_cli_main_reads_stdin_and_writes_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main()`` reads JSON from stdin (or --input) and prints the result as JSON on stdout."""
    input_path = tmp_path / "candidates.json"
    input_path.write_text(
        json.dumps(
            {
                "candidates": [
                    {"thread_id": "T-a", "head_msg_id": "msg-1"},
                    {"thread_id": "T-b", "head_msg_id": "msg-2"},
                ]
            }
        ),
        encoding="utf-8",
    )
    _patch_mcp(
        monkeypatch,
        {
            "T-a": ("msg-1", "content\n\nNEXT: human"),
            "T-b": ("msg-2", "content\n\nNEXT: Bohr"),
        },
    )
    monkeypatch.setattr(
        sys, "argv", ["parked_humans.py", "--project", "test", "--input", str(input_path)]
    )
    rc = _MODULE.main()  # type: ignore[attr-defined]
    assert rc == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["project"] == "test"
    assert result["polled"] == 2
    assert [p["thread_id"] for p in result["parked"]] == ["T-a"]


def test_cli_main_rejects_non_object_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "bad.json"
    input_path.write_text('["not", "an", "object"]', encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["parked_humans.py", "--project", "test", "--input", str(input_path)]
    )
    rc = _MODULE.main()  # type: ignore[attr-defined]
    assert rc == 1
    err = capsys.readouterr().err
    assert "must be a JSON object" in err


def test_cli_main_rejects_bad_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "bad.json"
    input_path.write_text("{ not: valid json", encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["parked_humans.py", "--project", "test", "--input", str(input_path)]
    )
    rc = _MODULE.main()  # type: ignore[attr-defined]
    assert rc == 1


def test_cli_main_handles_empty_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty input file (or empty stdin) becomes ``polled=0``, not a crash.

    The wrapper may send an empty candidate list when every project is on hold. That is a valid
    state, not an error, and the digest still emits its "0 件" section.
    """
    input_path = tmp_path / "empty.json"
    input_path.write_text("", encoding="utf-8")
    _patch_mcp(monkeypatch, {})
    monkeypatch.setattr(
        sys, "argv", ["parked_humans.py", "--project", "test", "--input", str(input_path)]
    )
    rc = _MODULE.main()  # type: ignore[attr-defined]
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"project": "test", "polled": 0, "parked": [], "errors": []}
