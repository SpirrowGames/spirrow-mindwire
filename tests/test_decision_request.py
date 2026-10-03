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
from typing import Any

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
from spirrow_mindwire.decision_request.cli import TailFetcher, TailFetchResult, compose_once, main
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

    def test_cli_stdout_is_ascii_only_pins_d33(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """D-33 (msg-1394 §14.3): the JSON written to stdout is pure ASCII.

        A NON-``capsys`` test on purpose. ``capsys`` captures at the io layer where the
        encoding is unconditionally UTF-8, so the previous ``ensure_ascii=False`` regression
        looked correct in every existing CLI test (msg-1394 §14.2). The invariant we actually
        care about is a property of the string the CLI writes: if it is ASCII-only, encoding
        it to any ASCII-superset (cp932, UTF-8, cp1252, latin-1, …) produces byte-identical
        output — which is exactly what makes the wrapper's UTF-8 read side robust to the
        deploy host's Windows console code page (cp932). ``StubComposer.compose`` emits
        Japanese in ``question`` and ``unknowns``; without D-33 the string this test captures
        contains those characters verbatim and ``.isascii()`` returns ``False``. With D-33,
        json.dumps emits ``\\uXXXX`` escapes and every byte is ASCII.
        """
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(self._payload())))
        # StringIO captures the string BEFORE any encoding step, so ``.isascii()`` on it
        # is a direct test of ``ensure_ascii=True`` at the ``json.dump`` call site.
        captured = StringIO()
        monkeypatch.setattr("sys.stdout", captured)
        rc = main(["--backend", "stub"])
        assert rc == 0
        emitted = captured.getvalue()
        # Sanity: the stub really did emit its Japanese payload — otherwise the assertion
        # below would be trivially true. The parsed envelope still round-trips.
        row = json.loads(emitted)
        assert "停止しました" in row["output"]["question"], (
            "stub composer output no longer contains Japanese — this test's premise is "
            "invalid; pick a different Japanese-bearing field to gate on"
        )
        assert emitted.isascii(), (
            "stdout JSON contains non-ASCII characters — D-33 (msg-1394 §14.3) "
            "requires ensure_ascii=True at cli.py's json.dump call. The failure mode "
            "this pin prevents: on the deploy host (Windows, cp932 console) the child's "
            "stdout encoder mojibakes the payload while the JSON structure characters "
            "remain valid, so the wrapper sees a parseable envelope with garbled "
            f"question text. First 200 chars of the offending output: {emitted[:200]!r}"
        )


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


# --------------------------------------------------------------------------- #
# S3 CLI wiring — --backend claude-code + --tail N fetch
# --------------------------------------------------------------------------- #
#
# These exercise the CLI seams S3 adds. They do NOT spawn the real ``claude``
# binary: the composer's ``runner`` is a fake, and the CLI's ``DEFAULT_TAIL_FETCHER``
# is monkey-patched to a synchronous coroutine.


class TestCliClaudeCodeBackend:
    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "project": "spirrow-voxelworld",
            "thread_id": "T-slope-extension-dead-mode",
            "last_msg_id": "msg-2582",
            "stop_reason": "human",
            "rounds": 3,
            "thread_title": "slope extension dead mode",
            "tail_requested": 0,
            "total_messages": 0,
            "tail": [],
        }

    def _fake_composer_cli_bytes(self) -> bytes:
        """Bytes a real ``claude`` CLI would emit under ``--output-format json``."""
        composer_payload = {
            "question": "案 A と案 B のどちらを採るか?",
            "options": [
                {"id": "A", "label": "採用する", "gain": "問いが届く", "loss": "コスト増"},
                {"id": "B", "label": "見送る", "gain": "コストゼロ", "loss": "問いが届かない"},
            ],
            "recommendation": "A",
            "recommendation_reason": "msg-2582 の実測遅延を根拠に",
            "unknowns": ["夜間停止頻度の実測"],
        }
        cli_payload = {
            "result": json.dumps(composer_payload, ensure_ascii=False),
            "duration_ms": 12345,
            "total_cost_usd": 0.021,
            "num_turns": 1,
            "model": "claude-3-5-sonnet-20241022",
        }
        return json.dumps(cli_payload, ensure_ascii=False).encode("utf-8")

    def test_backend_claude_code_writes_ok_envelope_with_extras(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Inject a fake runner into ClaudeCodeComposer by monkey-patching the
        # default runner used when a composer is constructed without ``runner=``.
        from spirrow_mindwire.decision_request import claude_code as cc_mod
        from spirrow_mindwire.decision_request.claude_code import SubprocessResult

        def fake_default_runner(**_: object) -> SubprocessResult:
            return SubprocessResult(0, self._fake_composer_cli_bytes(), b"")

        monkeypatch.setattr(cc_mod, "_default_runner", fake_default_runner)

        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(self._payload())))
        rc = main(["--backend", "claude-code", "--identity", "Composer"])
        assert rc == 0
        row = json.loads(capsys.readouterr().out)
        assert row["composer_status"] == "ok"
        assert row["identity_used"] == "Composer"
        assert row["extras"]["backend"] == "claude-code"
        assert row["extras"]["model"] == "claude-3-5-sonnet-20241022"
        assert row["extras"]["duration_ms"] == "12345"
        assert row["extras"]["prompt_version"]  # PROMPT_VERSION populated

    def test_tail_n_replaces_payload_tail_via_fetcher(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Fake the tail fetcher so we never hit the network. It returns 3
        # messages, one of them longer than the body cap so we can assert
        # ``tail_truncated == "true"``.
        from spirrow_mindwire.decision_request import claude_code as cc_mod
        from spirrow_mindwire.decision_request import cli as cli_mod
        from spirrow_mindwire.decision_request.claude_code import SubprocessResult

        async def fake_fetch(
            project: str, thread_id: str, count: int, body_cap: int
        ) -> TailFetchResult:
            assert project == "spirrow-voxelworld"
            assert thread_id == "T-slope-extension-dead-mode"
            assert count == 3
            assert body_cap == 100
            bodies = [
                "short body",
                "medium body " * 3,
                "x" * 250,  # exceeds body_cap=100 => truncated
            ]

            def _cap(text: str) -> str:
                return text[:body_cap] + ("… (省略)" if len(text) > body_cap else "")

            msgs = tuple(
                ThreadTailMessage(msg_id=f"msg-{i}", author=f"a-{i}", body=_cap(b))
                for i, b in enumerate(bodies)
            )
            total_chars = sum(len(m.body) for m in msgs)
            return msgs, 42, total_chars, True, bodies[-1]

        monkeypatch.setattr(cli_mod, "DEFAULT_TAIL_FETCHER", fake_fetch)

        def fake_default_runner(**_: object) -> SubprocessResult:
            return SubprocessResult(0, self._fake_composer_cli_bytes(), b"")

        monkeypatch.setattr(cc_mod, "_default_runner", fake_default_runner)

        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(self._payload())))
        rc = main(
            [
                "--backend",
                "claude-code",
                "--tail",
                "3",
                "--body-cap",
                "100",
            ]
        )
        assert rc == 0
        row = json.loads(capsys.readouterr().out)
        assert row["composer_status"] == "ok"
        # tail_requested reflects --tail N (D-38).
        assert row["tail_requested"] == 3
        # total_messages was updated from the fetch.
        assert row["omitted_count"] >= 0  # 42 - 3 = 39, but at least non-negative
        # Fetch telemetry in extras.
        assert row["extras"]["tail_count"] == "3"
        assert row["extras"]["tail_truncated"] == "true"
        # Composer telemetry also present.
        assert row["extras"]["backend"] == "claude-code"

    def test_tail_n_fetch_failure_is_recorded_but_does_not_sink_the_envelope(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # I-2 at the tail-fetch layer: a chatroom outage must not stop the
        # composer path. We fall back to the payload tail (which is empty
        # here), record the failure in extras, and let the composer proceed.
        from spirrow_mindwire.decision_request import claude_code as cc_mod
        from spirrow_mindwire.decision_request import cli as cli_mod
        from spirrow_mindwire.decision_request.claude_code import SubprocessResult

        async def failing_fetch(*_args: object, **_kwargs: object) -> TailFetchResult:
            raise RuntimeError("chatroom is on fire")

        monkeypatch.setattr(cli_mod, "DEFAULT_TAIL_FETCHER", failing_fetch)

        def fake_default_runner(**_: object) -> SubprocessResult:
            return SubprocessResult(0, self._fake_composer_cli_bytes(), b"")

        monkeypatch.setattr(cc_mod, "_default_runner", fake_default_runner)

        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(self._payload())))
        rc = main(["--backend", "claude-code", "--tail", "5"])
        assert rc == 0
        row = json.loads(capsys.readouterr().out)
        # The composer still ran (empty tail is legal).
        assert row["composer_status"] == "ok"
        # Fetch failure surfaced in extras (visible degradation, I-2).
        assert row["extras"]["tail_fetch_error"].startswith("RuntimeError:")

    def test_zero_tail_default_keeps_payload_tail_untouched(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # S2 backward-compat: --tail 0 (the default) means "use payload tail
        # as-is" — the fetcher is NEVER called. Pin this so a future refactor
        # cannot silently start fetching for the stub path.
        from spirrow_mindwire.decision_request import cli as cli_mod

        async def must_not_be_called(*_args: object, **_kwargs: object) -> TailFetchResult:
            pytest.fail("--tail 0 must not call the fetcher")

        monkeypatch.setattr(cli_mod, "DEFAULT_TAIL_FETCHER", must_not_be_called)
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(self._payload())))
        rc = main(["--backend", "stub"])
        assert rc == 0
        row = json.loads(capsys.readouterr().out)
        assert row["composer_status"] == "ok"
        # No tail_count key when fetch did not run.
        assert "tail_count" not in row["extras"]

    # --------------------------------------------------------------------- #
    # I-16 pin (T-decision-material-push msg-1443 §3 / msg-1445 §DM-4)
    # --------------------------------------------------------------------- #
    #
    # The wrapper's material push reads envelope.extras["head_msg_id_read"]
    # AND ONLY THAT KEY to decide the head_msg_id it sends to magickit. Two
    # invariants must hold on the CLI side:
    #
    #   (a) When the fetcher returns a non-empty tail, the key is present
    #       and equals the msg_id of the LAST element (chatroom_get_thread
    #       in mode="full" returns messages in msg_id-ascending order, so
    #       the tail's last element IS the thread head at the moment of
    #       the fetch — the SOT the composer actually read).
    #   (b) When the fetch raises OR returns an empty tuple, the key is
    #       ABSENT — never falls back to last_msg_id (the conductor stop
    #       line, not what the composer read). This is what the wrapper
    #       depends on to skip the PUT and produce J-absent instead of
    #       claiming a fresh head the composer never observed. The
    #       receiver cannot detect that lie, so the pin lives here.

    def test_head_msg_id_read_is_the_last_fetched_msg_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from spirrow_mindwire.decision_request import claude_code as cc_mod
        from spirrow_mindwire.decision_request import cli as cli_mod
        from spirrow_mindwire.decision_request.claude_code import SubprocessResult

        async def fake_fetch(
            project: str, thread_id: str, count: int, body_cap: int
        ) -> TailFetchResult:
            # Ascending msg_id order — the head is msg-2702, the last
            # element. Any tuple order regression would flip this pin.
            msgs = (
                ThreadTailMessage(msg_id="msg-2700", author="a", body="b0"),
                ThreadTailMessage(msg_id="msg-2701", author="b", body="b1"),
                ThreadTailMessage(msg_id="msg-2702", author="c", body="b2"),
            )
            return msgs, 42, sum(len(m.body) for m in msgs), False, "b2"

        monkeypatch.setattr(cli_mod, "DEFAULT_TAIL_FETCHER", fake_fetch)
        monkeypatch.setattr(
            cc_mod,
            "_default_runner",
            lambda **_: SubprocessResult(0, self._fake_composer_cli_bytes(), b""),
        )
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(self._payload())))

        rc = main(["--backend", "claude-code", "--tail", "3"])
        assert rc == 0
        row = json.loads(capsys.readouterr().out)
        assert row["composer_status"] == "ok"
        # I-16: the head the composer read is the LAST fetched msg_id, not
        # the payload's last_msg_id. The payload sets last_msg_id="msg-2582"
        # (see _payload), so a regression that copies last_msg_id in place
        # would silently ship the wrong head — this assertion is what
        # forces the read-time value to travel to the wrapper.
        assert row["extras"]["head_msg_id_read"] == "msg-2702"
        assert row["last_msg_id"] == "msg-2582"

    def test_head_msg_id_read_absent_when_fetch_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from spirrow_mindwire.decision_request import claude_code as cc_mod
        from spirrow_mindwire.decision_request import cli as cli_mod
        from spirrow_mindwire.decision_request.claude_code import SubprocessResult

        async def failing_fetch(*_args: object, **_kwargs: object) -> TailFetchResult:
            raise RuntimeError("chatroom outage")

        monkeypatch.setattr(cli_mod, "DEFAULT_TAIL_FETCHER", failing_fetch)
        monkeypatch.setattr(
            cc_mod,
            "_default_runner",
            lambda **_: SubprocessResult(0, self._fake_composer_cli_bytes(), b""),
        )
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(self._payload())))
        rc = main(["--backend", "claude-code", "--tail", "5"])
        assert rc == 0
        row = json.loads(capsys.readouterr().out)
        # The composer ran (empty tail is legal) and the failure surfaced.
        assert row["composer_status"] == "ok"
        assert row["extras"]["tail_fetch_error"].startswith("RuntimeError:")
        # The KEY MUST BE ABSENT. Not "empty string", not "None" — absent.
        # The wrapper reads only this key; if it were present-but-empty a
        # defensive check downstream would still let the wrong head slip
        # through under a future refactor. The pin here is the strongest
        # form the CLI can offer.
        assert "head_msg_id_read" not in row["extras"]

    def test_head_msg_id_read_absent_when_fetch_returns_empty_tuple(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Defensive branch (Einstein msg-1446 §1): a well-behaved fetcher
        # that returned an EMPTY tuple without raising must not crash the
        # composer with IndexError, and must not write a bogus head. The
        # composer path continues; the key stays absent for the same reason
        # as the fetch-failure case above.
        from spirrow_mindwire.decision_request import claude_code as cc_mod
        from spirrow_mindwire.decision_request import cli as cli_mod
        from spirrow_mindwire.decision_request.claude_code import SubprocessResult

        async def empty_fetch(*_args: object, **_kwargs: object) -> TailFetchResult:
            return (), 0, 0, False, None

        monkeypatch.setattr(cli_mod, "DEFAULT_TAIL_FETCHER", empty_fetch)
        monkeypatch.setattr(
            cc_mod,
            "_default_runner",
            lambda **_: SubprocessResult(0, self._fake_composer_cli_bytes(), b""),
        )
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(self._payload())))
        rc = main(["--backend", "claude-code", "--tail", "5"])
        assert rc == 0
        row = json.loads(capsys.readouterr().out)
        assert row["composer_status"] == "ok"
        # tail_count telemetry is written (fetch did not raise), but the
        # head-read key is not — no head was actually read.
        assert row["extras"]["tail_count"] == "0"
        assert "head_msg_id_read" not in row["extras"]


# --------------------------------------------------------------------------- #
# Parked-lane classification on the head the composer read
# (T-decision-material-parked-lane-push, Bohr msg-6236 §2-§6, Einstein msg-6237)
# --------------------------------------------------------------------------- #


def _marked_stand_down(reason_name: str, notice: str) -> str:
    """A stand-down notice carrying the conductor's real stop marker (core._with_stop_marker)."""
    from spirrow_mindwire.conductor.core import _with_stop_marker
    from spirrow_mindwire.conductor.stand_down import (
        StandDownReason,
        UnresolvedItem,
        stand_down_event,
    )

    event = stand_down_event(
        unresolved=UnresolvedItem.IDENTITY,
        reason=StandDownReason[reason_name],
        project="p",
        thread="t",
        detail="d",
    )
    return _with_stop_marker(notice, event)


_OPERATOR_TAIL = "OPERATOR-TASK: re-run the deploy by hand\nTIER-C-CHECK: none\nNEXT: operator"


def _fetch_of(head_body: str) -> TailFetcher:
    """A fetcher honouring the TailFetcher contract: capped tail body, raw 5th element."""

    async def fetch(project: str, thread_id: str, count: int, body_cap: int) -> TailFetchResult:
        capped = head_body
        truncated = len(capped) > body_cap
        if truncated:
            capped = capped[:body_cap] + "… (省略)"
        msgs = (
            ThreadTailMessage(msg_id="msg-1", author="a", body="earlier"),
            ThreadTailMessage(msg_id="msg-2", author="b", body=capped),
        )
        return msgs, 2, sum(len(m.body) for m in msgs), truncated, head_body

    return fetch


async def _failing_fetch(
    project: str, thread_id: str, count: int, body_cap: int
) -> TailFetchResult:
    raise RuntimeError("down")


async def _empty_fetch(project: str, thread_id: str, count: int, body_cap: int) -> TailFetchResult:
    return (), 0, 0, False, None


class TestCliParkedLaneExtras:
    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        fetch: TailFetcher,
        *,
        composer_extras: dict[str, str] | None = None,
    ) -> dict[str, str]:
        from spirrow_mindwire.decision_request import cli as cli_mod

        monkeypatch.setattr(cli_mod, "DEFAULT_TAIL_FETCHER", fetch)
        if composer_extras is not None:
            original = cli_mod._build_composer
            injected = dict(composer_extras)

            def build(backend: str, identity: str, **kwargs: Any) -> DecisionRequestComposer:
                composer = original(backend, identity, **kwargs)
                composer.last_extras = injected  # type: ignore[attr-defined]
                return composer

            monkeypatch.setattr(cli_mod, "_build_composer", build)
        payload = TestCliClaudeCodeBackend()._payload()
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(payload)))
        rc = main(["--backend", "stub", "--tail", "3", "--body-cap", "4000"])
        assert rc == 0
        row = json.loads(capsys.readouterr().out)
        assert row["composer_status"] == "ok"
        extras: dict[str, str] = row["extras"]
        return extras

    def test_long_operator_head_is_classified_on_the_pre_cap_body(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # P-2 regression pin: the 3 operator lines sit past the 4000-char cap.
        body = "x" * 5000 + "\n\n" + _OPERATOR_TAIL
        extras = self._run(monkeypatch, capsys, _fetch_of(body))
        assert extras["tail_truncated"] == "true"
        assert extras["head_msg_id_read"] == "msg-2"
        assert extras["parked_lane"] == "operator_work"
        assert extras["operator_task"] == "re-run the deploy by hand"
        assert extras["protocol_violation"] == "false"

    def test_production_fetcher_returns_the_pre_cap_head_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The production fetcher honours its own contract (Einstein msg-6237): the 5th element
        # is the LAST appended message's body before the cap, skipping a trailing non-dict item.
        import asyncio

        from spirrow_mindwire.decision_request import cli as cli_mod
        from spirrow_mindwire.magickit import client as client_mod

        long_body = "y" * 50 + "\n" + _OPERATOR_TAIL

        class FakeMcp:
            def __init__(self, _cfg: object) -> None:
                pass

            async def call_tool(self, name: str, args: dict[str, object]) -> object:
                return {
                    "messages": [
                        {"msg_id": "msg-1", "author": "a", "content": "first"},
                        {"msg_id": "msg-2", "author": "b", "content": long_body},
                        "not-a-dict",
                    ]
                }

        monkeypatch.setattr(client_mod, "StreamableHttpChatroomMcp", FakeMcp)
        msgs, total, _chars, truncated, raw = asyncio.run(
            cli_mod._default_fetch_tail("p", "t", 3, 10)
        )
        assert total == 3
        assert truncated is True
        assert msgs[-1].msg_id == "msg-2"
        assert msgs[-1].body.endswith("… (省略)")
        assert raw == long_body

    def test_identity_unresolved_stand_down_is_misroute(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body = _marked_stand_down("IDENTITY_UNRESOLVED", "could not route\n\nNEXT: human")
        extras = self._run(monkeypatch, capsys, _fetch_of(body))
        assert extras["parked_lane"] == "misroute"
        assert extras["protocol_violation"] == "false"
        assert "operator_task" not in extras

    def test_operator_tier_c_conflict_is_a_decision_with_protocol_violation(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body = _marked_stand_down("OPERATOR_TIER_C_CONFLICT", "tier-c + operator\n\nNEXT: human")
        extras = self._run(monkeypatch, capsys, _fetch_of(body))
        assert extras["parked_lane"] == "decision"
        assert extras["protocol_violation"] == "true"
        assert "operator_task" not in extras

    def test_fetch_failure_writes_no_parked_lane_keys(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        extras = self._run(monkeypatch, capsys, _failing_fetch)
        assert "tail_fetch_error" in extras
        for key in ("parked_lane", "operator_task", "protocol_violation"):
            assert key not in extras

    def test_empty_tail_writes_no_parked_lane_keys(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        extras = self._run(monkeypatch, capsys, _empty_fetch)
        assert "head_msg_id_read" not in extras
        for key in ("parked_lane", "operator_task", "protocol_violation"):
            assert key not in extras

    def test_fetch_side_classification_wins_over_composer_extras(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        extras = self._run(
            monkeypatch,
            capsys,
            _fetch_of("plain decision\n\nNEXT: human"),
            composer_extras={
                "parked_lane": "misroute",
                "operator_task": "bogus",
                "protocol_violation": "true",
                "backend": "stub-x",
            },
        )
        assert extras["parked_lane"] == "decision"
        assert extras["protocol_violation"] == "false"
        assert "operator_task" not in extras
        assert extras["backend"] == "stub-x"  # other composer keys still merge

    def test_composer_parked_keys_dropped_when_fetch_failed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        extras = self._run(
            monkeypatch,
            capsys,
            _failing_fetch,
            composer_extras={"parked_lane": "misroute", "protocol_violation": "false"},
        )
        assert "parked_lane" not in extras
        assert "protocol_violation" not in extras
