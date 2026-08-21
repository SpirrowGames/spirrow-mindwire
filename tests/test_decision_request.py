"""S1 unit tests for the decision-request composer subpackage.

Covers:

- value-object validators (input / output / envelope) — a bad shape is
  caught at construction, not at serialization time.
- lossless envelope round-trip via ``to_json`` / ``from_json``.
- the CLI's ``compose_once`` behaviour:

    * OK path: the stub produces an envelope with populated output.
    * every failure mode of the port maps to the right
      :class:`ComposerStatus`, keeps a non-empty error string, and
      leaves ``output`` = ``None``.
    * a backend that raises a bare :class:`Exception` (a bug in the
      backend) is still caught and reported — I-2 requires that the
      CLI does not let a raise escape and starve the raw ping.
    * a backend that lies about ``tail_used`` is caught before the
      envelope is stamped ``ok``.

- I-3 dedup precondition: the envelope carries a stable ``signature``
  computed from ``stop_reason:last_msg_id``; equal inputs produce equal
  signatures. The wrapper's own dedup is proved in S2 tests, but this
  is the value the wrapper keys on.

The composer identity check (I-5: not one of Bohr / Heisenberg /
Einstein) is a policy attribute of a concrete composer, verified against
the roster at spawn time in the wrapper — the port itself does not know
the roster. What the tests here verify is that ``identity_name`` is
recorded verbatim into the envelope, so an operator can tell if a
future composer accidentally picked a role persona.
"""

from __future__ import annotations

import json
from io import StringIO

import pytest

from spirrow_mindwire.decision_request import (
    ComposerStatus,
    DecisionComposerError,
    DecisionOption,
    DecisionRequestEnvelope,
    DecisionRequestInput,
    DecisionRequestOutput,
    StubComposer,
    ThreadTailMessage,
)
from spirrow_mindwire.decision_request.cli import compose_once, main
from spirrow_mindwire.decision_request.ports import DecisionRequestComposer
from spirrow_mindwire.decision_request.stub import (
    DEFAULT_STUB_IDENTITY,
    FailingStubComposer,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _sample_input(
    *,
    tail: tuple[ThreadTailMessage, ...] = (),
    tail_requested: int = 5,
    total_messages: int = 5,
) -> DecisionRequestInput:
    return DecisionRequestInput(
        project="spirrow-voxelworld",
        thread_id="T-slope-extension-dead-mode",
        last_msg_id="msg-2582",
        stop_reason="human",
        rounds=3,
        thread_title="slope extension dead mode",
        tail=tail,
        tail_requested=tail_requested,
        total_messages=total_messages,
    )


# --------------------------------------------------------------------------- #
# Value-object validators
# --------------------------------------------------------------------------- #


class TestDecisionRequestInput:
    def test_defaults_are_valid(self) -> None:
        inp = _sample_input()
        assert inp.omitted_count == 5  # tail=() with total=5

    def test_omitted_count_reflects_partial_view(self) -> None:
        inp = _sample_input(
            tail=(ThreadTailMessage(msg_id="msg-2581", author="Bohr", body="…"),),
            tail_requested=5,
            total_messages=45,
        )
        assert inp.omitted_count == 44

    def test_rejects_negative_rounds(self) -> None:
        with pytest.raises(ValueError, match="rounds"):
            DecisionRequestInput(
                project="p",
                thread_id="t",
                last_msg_id="m",
                stop_reason="human",
                rounds=-1,
                thread_title="",
                tail=(),
                tail_requested=0,
                total_messages=0,
            )

    def test_rejects_tail_longer_than_requested(self) -> None:
        # A caller that hands over more messages than it claimed to bound
        # itself to is silently expanding the surface (I-4).
        msg = ThreadTailMessage(msg_id="m", author="a", body="b")
        with pytest.raises(ValueError, match="tail is longer"):
            DecisionRequestInput(
                project="p",
                thread_id="t",
                last_msg_id="m",
                stop_reason="human",
                rounds=0,
                thread_title="",
                tail=(msg, msg),
                tail_requested=1,
                total_messages=5,
            )

    def test_rejects_tail_longer_than_total(self) -> None:
        msg = ThreadTailMessage(msg_id="m", author="a", body="b")
        with pytest.raises(ValueError, match="total_messages"):
            DecisionRequestInput(
                project="p",
                thread_id="t",
                last_msg_id="m",
                stop_reason="human",
                rounds=0,
                thread_title="",
                tail=(msg, msg),
                tail_requested=2,
                total_messages=1,
            )


class TestDecisionOption:
    def test_rejects_multi_char_id(self) -> None:
        with pytest.raises(ValueError, match="single uppercase letter"):
            DecisionOption(id="AA", label="l", gain="g", loss="l")

    def test_rejects_lowercase_id(self) -> None:
        with pytest.raises(ValueError, match="single uppercase letter"):
            DecisionOption(id="a", label="l", gain="g", loss="l")

    def test_rejects_blank_label(self) -> None:
        with pytest.raises(ValueError, match="label"):
            DecisionOption(id="A", label="  ", gain="g", loss="l")


class TestDecisionRequestOutput:
    def test_valid_full_shape(self) -> None:
        DecisionRequestOutput(
            question="q?",
            options=(
                DecisionOption(id="A", label="l", gain="g", loss="l"),
                DecisionOption(id="B", label="l2", gain="g2", loss="l2"),
            ),
            recommendation="A",
            recommendation_reason="because",
            unknowns=("something",),
            tail_used=3,
        )

    def test_rejects_blank_question(self) -> None:
        with pytest.raises(ValueError, match="question"):
            DecisionRequestOutput(
                question="",
                options=(),
                recommendation=None,
                recommendation_reason=None,
                unknowns=(),
                tail_used=0,
            )

    def test_rejects_duplicate_option_id(self) -> None:
        with pytest.raises(ValueError, match="duplicate option id"):
            DecisionRequestOutput(
                question="q?",
                options=(
                    DecisionOption(id="A", label="l", gain="g", loss="l"),
                    DecisionOption(id="A", label="l2", gain="g2", loss="l2"),
                ),
                recommendation=None,
                recommendation_reason=None,
                unknowns=(),
                tail_used=0,
            )

    def test_rejects_recommendation_absent_from_options(self) -> None:
        with pytest.raises(ValueError, match="recommendation"):
            DecisionRequestOutput(
                question="q?",
                options=(DecisionOption(id="A", label="l", gain="g", loss="l"),),
                recommendation="B",
                recommendation_reason="because",
                unknowns=(),
                tail_used=0,
            )

    def test_recommendation_requires_reason(self) -> None:
        with pytest.raises(ValueError, match="recommendation_reason"):
            DecisionRequestOutput(
                question="q?",
                options=(DecisionOption(id="A", label="l", gain="g", loss="l"),),
                recommendation="A",
                recommendation_reason="",
                unknowns=(),
                tail_used=0,
            )

    def test_reason_without_recommendation_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="recommendation_reason is set"):
            DecisionRequestOutput(
                question="q?",
                options=(),
                recommendation=None,
                recommendation_reason="orphan reason",
                unknowns=(),
                tail_used=0,
            )


class TestDecisionRequestEnvelope:
    def _make_ok(self) -> DecisionRequestEnvelope:
        output = DecisionRequestOutput(
            question="q?",
            options=(DecisionOption(id="A", label="l", gain="g", loss="loss"),),
            recommendation="A",
            recommendation_reason="because",
            unknowns=("u1",),
            tail_used=2,
        )
        return DecisionRequestEnvelope(
            project="p",
            thread_id="t",
            signature="human:msg-1",
            composed_at="2026-08-21T09:22:00Z",
            composer_status=ComposerStatus.OK,
            identity_used=DEFAULT_STUB_IDENTITY,
            last_msg_id="msg-1",
            stop_reason="human",
            rounds=3,
            tail_requested=5,
            tail_used=2,
            omitted_count=0,
            output=output,
            error=None,
            extras={"model": "stub"},
        )

    def test_ok_shape_round_trips_losslessly(self) -> None:
        env = self._make_ok()
        row = env.to_json()
        # Round-trip via JSON to guarantee we do not accidentally rely on
        # a Python-only object in the on-disk shape.
        decoded = json.loads(json.dumps(row, ensure_ascii=False))
        recovered = DecisionRequestEnvelope.from_json(decoded)
        assert recovered == env

    def test_ok_requires_output(self) -> None:
        with pytest.raises(ValueError, match="requires a populated output"):
            DecisionRequestEnvelope(
                project="p",
                thread_id="t",
                signature="s",
                composed_at="t",
                composer_status=ComposerStatus.OK,
                identity_used="i",
                last_msg_id="m",
                stop_reason="r",
                rounds=0,
                tail_requested=0,
                tail_used=0,
                omitted_count=0,
                output=None,
                error=None,
            )

    def test_error_forbids_output(self) -> None:
        output = DecisionRequestOutput(
            question="q?",
            options=(),
            recommendation=None,
            recommendation_reason=None,
            unknowns=(),
            tail_used=0,
        )
        with pytest.raises(ValueError, match="must not carry an output"):
            DecisionRequestEnvelope(
                project="p",
                thread_id="t",
                signature="s",
                composed_at="t",
                composer_status=ComposerStatus.ERROR,
                identity_used="i",
                last_msg_id="m",
                stop_reason="r",
                rounds=0,
                tail_requested=0,
                tail_used=0,
                omitted_count=0,
                output=output,
                error="…",
            )

    def test_error_requires_error_string(self) -> None:
        with pytest.raises(ValueError, match="requires a non-empty error"):
            DecisionRequestEnvelope(
                project="p",
                thread_id="t",
                signature="s",
                composed_at="t",
                composer_status=ComposerStatus.ERROR,
                identity_used="i",
                last_msg_id="m",
                stop_reason="r",
                rounds=0,
                tail_requested=0,
                tail_used=0,
                omitted_count=0,
                output=None,
                error=None,
            )

    def test_tail_used_may_not_exceed_tail_requested(self) -> None:
        output = DecisionRequestOutput(
            question="q?",
            options=(),
            recommendation=None,
            recommendation_reason=None,
            unknowns=(),
            tail_used=6,
        )
        with pytest.raises(ValueError, match="tail_used"):
            DecisionRequestEnvelope(
                project="p",
                thread_id="t",
                signature="s",
                composed_at="t",
                composer_status=ComposerStatus.OK,
                identity_used="i",
                last_msg_id="m",
                stop_reason="r",
                rounds=0,
                tail_requested=5,
                tail_used=6,
                omitted_count=0,
                output=output,
                error=None,
            )


# --------------------------------------------------------------------------- #
# StubComposer
# --------------------------------------------------------------------------- #


class TestStubComposer:
    def test_is_deterministic(self) -> None:
        # A-3 (I-3) rests on the wrapper caching a composer's output.
        # Determinism here means the cache key can be a shape hash if
        # ever wanted; the wrapper actually keys on the input signature,
        # not the output, so this is a cheap belt-and-braces.
        composer = StubComposer()
        req = _sample_input()
        out_a = composer.compose(req)
        out_b = composer.compose(req)
        assert out_a == out_b

    def test_records_tail_used_honestly(self) -> None:
        composer = StubComposer()
        tail = (
            ThreadTailMessage(msg_id="m-1", author="Bohr", body="…"),
            ThreadTailMessage(msg_id="m-2", author="Einstein", body="…"),
        )
        req = _sample_input(tail=tail, tail_requested=5, total_messages=45)
        out = composer.compose(req)
        assert out.tail_used == len(tail) == 2

    def test_identity_is_not_a_role_persona(self) -> None:
        # I-5: the default stub identity must not shadow one of the
        # design-roster role personas. A concrete production composer
        # is checked at spawn time against the loaded roster; here we
        # only pin the stub's default.
        assert DEFAULT_STUB_IDENTITY not in {"Bohr", "Heisenberg", "Einstein"}
        assert StubComposer().identity_name == DEFAULT_STUB_IDENTITY


# --------------------------------------------------------------------------- #
# CLI / compose_once
# --------------------------------------------------------------------------- #


class TestComposeOnce:
    def test_ok_envelope_shape(self) -> None:
        env = compose_once(StubComposer(), _sample_input())
        assert env.composer_status is ComposerStatus.OK
        assert env.output is not None
        assert env.error is None
        assert env.signature == "human:msg-2582"

    def test_signature_is_stable_across_calls(self) -> None:
        # I-3 rests on this — the wrapper compares signatures, and if
        # two identical inputs produce two different signatures the
        # dedup falls apart.
        env_a = compose_once(StubComposer(), _sample_input())
        env_b = compose_once(StubComposer(), _sample_input())
        assert env_a.signature == env_b.signature

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            ("timeout", ComposerStatus.TIMEOUT),
            ("empty", ComposerStatus.EMPTY),
            ("error", ComposerStatus.ERROR),
        ],
    )
    def test_each_failure_kind_maps_to_the_matching_status(
        self, kind: str, expected: ComposerStatus
    ) -> None:
        env = compose_once(FailingStubComposer(kind=kind), _sample_input())  # type: ignore[arg-type]
        assert env.composer_status is expected
        assert env.output is None
        assert env.error, "envelope must carry a non-empty error string"

    def test_bare_exception_from_backend_is_still_captured(self) -> None:
        # I-2 requires that a broken backend does not sink the pipeline.
        # A production-safe backend raises DecisionComposerError; this
        # test proves that a *broken* backend (anything else) still
        # produces an envelope, so the wrapper's raw ping keeps firing.

        class KaboomComposer:
            identity_name = "composer-stub"

            def compose(self, request: DecisionRequestInput) -> DecisionRequestOutput:
                raise RuntimeError("something went terribly wrong")

        env = compose_once(KaboomComposer(), _sample_input())
        assert env.composer_status is ComposerStatus.ERROR
        assert env.output is None
        assert env.error is not None and "RuntimeError" in env.error

    def test_backend_lying_about_tail_used_is_rejected(self) -> None:
        # A composer that returns tail_used=10 when it was given 2
        # messages is claiming to have used context it never received.
        # Recording the lie would defeat the F-1 diagnostic ("was N
        # enough?"); we downgrade the envelope to ERROR instead.

        class LyingComposer:
            identity_name = "composer-stub"

            def compose(self, request: DecisionRequestInput) -> DecisionRequestOutput:
                return DecisionRequestOutput(
                    question="q?",
                    options=(),
                    recommendation=None,
                    recommendation_reason=None,
                    unknowns=(),
                    tail_used=10,  # was given 0
                )

        env = compose_once(LyingComposer(), _sample_input())
        assert env.composer_status is ComposerStatus.ERROR
        assert env.output is None
        assert env.error is not None and "tail_used" in env.error


class TestCliMain:
    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "project": "spirrow-voxelworld",
            "thread_id": "T-slope-extension-dead-mode",
            "last_msg_id": "msg-2582",
            "stop_reason": "human",
            "rounds": 3,
            "thread_title": "slope extension dead mode",
            "tail_requested": 5,
            "total_messages": 45,
            "tail": [
                {"msg_id": "msg-2581", "author": "Bohr", "body": "…"},
            ],
        }

    def test_writes_envelope_to_stdout_on_stub(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(self._payload())))
        rc = main(["--backend", "stub"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        row = json.loads(out)
        assert row["composer_status"] == "ok"
        assert row["signature"] == "human:msg-2582"
        # tail_used cannot exceed 1 (that's what was in the payload).
        assert row["tail_used"] == 1
        assert row["omitted_count"] == 44

    def test_writes_error_envelope_on_failing_backend(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # I-2 at the CLI boundary: a failed composer still leaves rc=0
        # and a well-formed envelope. That is what lets the wrapper
        # keep firing the raw ping.
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(self._payload())))
        rc = main(["--backend", "fail-error"])
        assert rc == 0
        row = json.loads(capsys.readouterr().out)
        assert row["composer_status"] == "error"
        assert row["error"]
        assert row["output"] is None

    def test_unknown_backend_is_a_usage_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(self._payload())))
        with pytest.raises(SystemExit) as exc:
            main(["--backend", "nonsense"])
        # Usage errors exit non-zero — see cli.py docstring.
        assert exc.value.code != 0

    def test_bad_schema_version_is_a_usage_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = self._payload()
        payload["schema_version"] = 999
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(payload)))
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code != 0

    def test_identity_flag_is_recorded_in_envelope(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(self._payload())))
        rc = main(["--backend", "stub", "--identity", "Composer"])
        assert rc == 0
        row = json.loads(capsys.readouterr().out)
        assert row["identity_used"] == "Composer"


# --------------------------------------------------------------------------- #
# Protocol conformance (a duck-typed protocol test)
# --------------------------------------------------------------------------- #


def _accept_composer(c: DecisionRequestComposer) -> str:
    return c.identity_name


def test_stub_composer_satisfies_protocol() -> None:
    assert _accept_composer(StubComposer()) == DEFAULT_STUB_IDENTITY


def test_failing_stub_composer_satisfies_protocol() -> None:
    assert _accept_composer(FailingStubComposer()) == DEFAULT_STUB_IDENTITY


def test_direct_exception_raise_shape_is_a_decision_composer_error() -> None:
    # Belt-and-braces: the exception hierarchy is one-rooted, so a
    # broad ``except DecisionComposerError`` catches every declared
    # subclass. If a future refactor slipped an outside-hierarchy
    # exception in, this test would flag it.
    from spirrow_mindwire.decision_request.exceptions import (
        DecisionComposerEmptyError,
        DecisionComposerTimeoutError,
    )

    assert issubclass(DecisionComposerTimeoutError, DecisionComposerError)
    assert issubclass(DecisionComposerEmptyError, DecisionComposerError)
