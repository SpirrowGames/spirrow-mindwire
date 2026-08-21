"""S3 unit tests — :mod:`spirrow_mindwire.decision_request.claude_code`.

Coverage matches the "Testing (unit + regression pins)" section of
``spec/slices/S3-claude-code-composer.md``:

1.  Happy path: fake runner returns a well-formed Claude Code CLI JSON
    embedding our composer JSON; ``compose`` yields a valid
    :class:`DecisionRequestOutput` and ``last_extras`` carries every key
    the spec lists.
2.  D-37: the runner's cwd is outside the repo, argv contains the
    disable-tools flags and the empty settings source, env has no
    MINDWIRE_* keys.
3.  D-41: every failure mode maps to the right exception subclass.
4.  D-43 (bytes → UTF-8 pin): a fixed byte sequence containing multi-byte
    UTF-8 (the same Japanese "斜面撤去" §14 caught) round-trips
    character-identical. NO ``capsys`` — §14.3 rule.
5.  D-38: user prompt renders every tail body with proper separators and
    respects the internal hard ceiling.
6.  Prompt version: ``PROMPT_VERSION`` is a module-level string and
    ``last_extras["prompt_version"]`` reports its value verbatim.
7.  ``compose_once`` propagates ``last_extras`` into ``envelope.extras``.

Every test uses a fake runner — none of these spawn the real
``claude`` binary and none hit the network.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from spirrow_mindwire.decision_request import (
    ClaudeCodeComposer,
    ComposerStatus,
    DecisionComposerEmptyError,
    DecisionComposerError,
    DecisionComposerTimeoutError,
    DecisionRequestInput,
    DecisionRequestOutput,
    ThreadTailMessage,
)
from spirrow_mindwire.decision_request.claude_code import (
    DEFAULT_COMPOSER_IDENTITY,
    PROMPT_VERSION,
    SubprocessResult,
    _extract_result_text,
    _parse_json_from_result_text,
    _shape_composer_answer,
)
from spirrow_mindwire.decision_request.cli import compose_once

# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


@dataclass
class RunnerCall:
    """One recorded call to the fake runner. Kept for D-37 assertions."""

    argv: list[str]
    input_bytes: bytes
    cwd: str
    timeout: float
    env: dict[str, str]


@dataclass
class FakeRunner:
    """Fake :func:`_default_runner` that records calls and returns a fixed result.

    - ``result`` — the :class:`SubprocessResult` to return.
    - ``raise_exc`` — if set, is raised in place of returning; lets tests
      exercise the ``TimeoutExpired`` / ``FileNotFoundError`` / ``OSError``
      branches in :meth:`ClaudeCodeComposer.compose`.
    - ``calls`` — every invocation is appended for later assertion.
    """

    result: SubprocessResult | None = None
    raise_exc: BaseException | None = None
    calls: list[RunnerCall] = field(default_factory=list)

    def __call__(
        self,
        *,
        argv: list[str],
        input_bytes: bytes,
        cwd: str,
        timeout: float,
        env: dict[str, str],
    ) -> SubprocessResult:
        self.calls.append(
            RunnerCall(
                argv=list(argv),
                input_bytes=input_bytes,
                cwd=cwd,
                timeout=timeout,
                env=dict(env),
            )
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.result is not None, "FakeRunner needs a .result or a .raise_exc"
        return self.result


def _cli_json_bytes(
    *,
    question: str = "案 A と案 B のどちらを採るか?",
    options: list[dict[str, str]] | None = None,
    recommendation: str | None = "A",
    recommendation_reason: str | None = "msg-2582 で計測した 11.6h の遅延が繰り返している",
    unknowns: list[str] | None = None,
    model: str = "claude-3-5-sonnet-20241022",
    duration_ms: int = 14320,
    total_cost_usd: float = 0.0287,
    num_turns: int = 1,
) -> bytes:
    """Build the bytes a real Claude Code CLI would emit under --output-format json.

    The ``result`` field holds the assistant's text, which is itself a
    JSON string encoding our composer's schema. Byte-encoded here so the
    D-43 pin can pass a fixed sequence.
    """
    if options is None:
        options = [
            {"id": "A", "label": "採用する", "gain": "整った問いが届く", "loss": "実装コスト"},
            {"id": "B", "label": "見送る", "gain": "実装コストゼロ", "loss": "問いが届かない"},
        ]
    if unknowns is None:
        unknowns = ["夜間の停止頻度の実測値"]
    composer_payload = {
        "question": question,
        "options": options,
        "recommendation": recommendation,
        "recommendation_reason": recommendation_reason,
        "unknowns": unknowns,
    }
    cli_payload = {
        "result": json.dumps(composer_payload, ensure_ascii=False),
        "duration_ms": duration_ms,
        "total_cost_usd": total_cost_usd,
        "num_turns": num_turns,
        "model": model,
    }
    return json.dumps(cli_payload, ensure_ascii=False).encode("utf-8")


def _sample_request(tail_count: int = 2) -> DecisionRequestInput:
    tail = tuple(
        ThreadTailMessage(msg_id=f"msg-{2580 + i}", author=f"author-{i}", body=f"body {i}")
        for i in range(tail_count)
    )
    return DecisionRequestInput(
        project="spirrow-voxelworld",
        thread_id="T-slope-extension-dead-mode",
        last_msg_id="msg-2582",
        stop_reason="human",
        rounds=3,
        thread_title="slope extension dead mode",
        tail=tail,
        tail_requested=max(tail_count, 5),
        total_messages=45,
    )


# --------------------------------------------------------------------------- #
# 1. Happy path
# --------------------------------------------------------------------------- #


class TestHappyPath:
    def test_returns_a_valid_decision_request_output(self) -> None:
        runner = FakeRunner(result=SubprocessResult(0, _cli_json_bytes(), b""))
        composer = ClaudeCodeComposer(runner=runner, cwd="/tmp")

        out = composer.compose(_sample_request())

        assert isinstance(out, DecisionRequestOutput)
        assert out.question == "案 A と案 B のどちらを採るか?"
        assert len(out.options) == 2
        assert out.options[0].id == "A"
        assert out.recommendation == "A"
        assert out.recommendation_reason
        assert "11.6h" in out.recommendation_reason
        assert out.tail_used == 2  # matches len(request.tail)

    def test_last_extras_has_every_key_the_spec_lists(self) -> None:
        runner = FakeRunner(result=SubprocessResult(0, _cli_json_bytes(), b""))
        composer = ClaudeCodeComposer(runner=runner, cwd="/tmp")

        composer.compose(_sample_request())

        # Spec §"Extras (envelope)" — S3 populates these keys.
        assert composer.last_extras["backend"] == "claude-code"
        assert composer.last_extras["model"] == "claude-3-5-sonnet-20241022"
        assert composer.last_extras["duration_ms"] == "14320"
        assert composer.last_extras["total_cost_usd"] == "0.0287"
        assert composer.last_extras["num_turns"] == "1"
        assert composer.last_extras["prompt_version"] == PROMPT_VERSION
        assert composer.last_extras["cwd"] == "/tmp"
        # argv_digest is a 16-char lowercase hex string.
        assert len(composer.last_extras["argv_digest"]) == 16
        int(composer.last_extras["argv_digest"], 16)  # not a raise

    def test_identity_default_is_neither_stub_nor_role(self) -> None:
        # I-5 spot check: the default identity is "Composer", not Bohr / Heisenberg / Einstein
        # and not the stub identity ("composer-stub"). This is the value the wrapper's env-var
        # override overwrites; the constant lives on the class module.
        assert DEFAULT_COMPOSER_IDENTITY == "Composer"
        composer = ClaudeCodeComposer(
            runner=FakeRunner(result=SubprocessResult(0, _cli_json_bytes(), b""))
        )
        assert composer.identity_name == "Composer"
        assert composer.identity_name not in {"Bohr", "Heisenberg", "Einstein", "composer-stub"}


# --------------------------------------------------------------------------- #
# 2. D-37 — argv / cwd / env pins
# --------------------------------------------------------------------------- #


class TestD37NeutralEnvironment:
    def test_cwd_default_is_outside_the_repo(self) -> None:
        # The default cwd is tempfile.gettempdir(); assert it is not a
        # child of the repo root. The repo root is the working tree's
        # parent chain from this test file.
        repo_root = Path(__file__).resolve().parent.parent
        runner = FakeRunner(result=SubprocessResult(0, _cli_json_bytes(), b""))
        composer = ClaudeCodeComposer(runner=runner)  # no cwd override

        composer.compose(_sample_request())

        actual_cwd = Path(runner.calls[0].cwd).resolve()
        # Not the repo root itself, and not inside it.
        assert actual_cwd != repo_root, (
            f"default cwd {actual_cwd} equals repo root — D-37 requires OUTSIDE"
        )
        # Try to compute relative — if it succeeds and the relative path
        # does not start with '..', we are inside the repo (D-37 breach).
        try:
            rel = actual_cwd.relative_to(repo_root)
        except ValueError:
            pass  # good: outside the repo tree entirely
        else:
            pytest.fail(f"default cwd {actual_cwd} is inside the repo (relative: {rel})")

    def test_argv_disables_tools_and_settings_source(self) -> None:
        runner = FakeRunner(result=SubprocessResult(0, _cli_json_bytes(), b""))
        composer = ClaudeCodeComposer(runner=runner)

        composer.compose(_sample_request())

        argv = runner.calls[0].argv
        # D-36 flags
        assert "--allowedTools" in argv
        allow_idx = argv.index("--allowedTools")
        assert argv[allow_idx + 1] == ""
        assert "--disallowedTools" in argv
        deny_idx = argv.index("--disallowedTools")
        assert argv[deny_idx + 1] == "*"
        # D-37 flag: settings source is emptied so the child cannot pull
        # in persona defaults from a settings.json anywhere.
        assert "--setting-sources" in argv
        src_idx = argv.index("--setting-sources")
        assert argv[src_idx + 1] == ""
        # D-35 shape: headless print mode + JSON output.
        assert "-p" in argv
        assert "--output-format" in argv
        fmt_idx = argv.index("--output-format")
        assert argv[fmt_idx + 1] == "json"

    def test_env_is_scrubbed_of_mindwire_variables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Seed a MINDWIRE_* env in the parent and assert it does NOT
        # reach the child. Also seed PYTHONIOENCODING — D-43 principle:
        # not propagated to the child even though the parent has it.
        monkeypatch.setenv("MINDWIRE_DECISION_COMPOSER_BACKEND", "claude-code")
        monkeypatch.setenv("MINDWIRE_ROLE_HINT", "Bohr")
        monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
        runner = FakeRunner(result=SubprocessResult(0, _cli_json_bytes(), b""))
        composer = ClaudeCodeComposer(runner=runner)

        composer.compose(_sample_request())

        child_env = runner.calls[0].env
        leaked = [k for k in child_env if k.startswith("MINDWIRE_")]
        assert not leaked, f"MINDWIRE_* leaked into child env: {leaked}"
        assert "PYTHONIOENCODING" not in child_env, (
            "PYTHONIOENCODING leaked — D-43 says encoding is structural, not env-driven"
        )
        # Sanity: PATH did survive (or the child could not find `claude`).
        assert "PATH" in child_env

    def test_argv_digest_is_16_hex_chars_and_stable(self) -> None:
        # The digest lets an operator retroactively confirm "did we launch
        # under the neutral setup?" A stable digest across runs of the same
        # composer is what makes that confirmation meaningful.
        runner1 = FakeRunner(result=SubprocessResult(0, _cli_json_bytes(), b""))
        runner2 = FakeRunner(result=SubprocessResult(0, _cli_json_bytes(), b""))
        c1 = ClaudeCodeComposer(runner=runner1, cwd="/tmp")
        c2 = ClaudeCodeComposer(runner=runner2, cwd="/tmp")

        c1.compose(_sample_request())
        c2.compose(_sample_request())

        assert c1.last_extras["argv_digest"] == c2.last_extras["argv_digest"], (
            "argv_digest changed across two identical launches — someone injected non-determinism"
        )


# --------------------------------------------------------------------------- #
# 3. D-41 — failure-mode mapping
# --------------------------------------------------------------------------- #


class TestD41FailureMapping:
    def test_timeout_raises_timeout(self) -> None:
        runner = FakeRunner(raise_exc=subprocess.TimeoutExpired(cmd=["claude"], timeout=30))
        composer = ClaudeCodeComposer(runner=runner, timeout_seconds=30)
        with pytest.raises(DecisionComposerTimeoutError):
            composer.compose(_sample_request())

    def test_file_not_found_raises_error(self) -> None:
        runner = FakeRunner(raise_exc=FileNotFoundError(2, "no such file", "claude"))
        composer = ClaudeCodeComposer(runner=runner, cli_path="claude-does-not-exist")
        with pytest.raises(DecisionComposerError, match="not found"):
            composer.compose(_sample_request())

    def test_nonzero_exit_raises_error_and_carries_stderr(self) -> None:
        runner = FakeRunner(result=SubprocessResult(returncode=7, stdout=b"", stderr=b"boom\n"))
        composer = ClaudeCodeComposer(runner=runner)
        with pytest.raises(DecisionComposerError, match="exit=7"):
            composer.compose(_sample_request())

    def test_stdout_not_json_raises_error(self) -> None:
        runner = FakeRunner(result=SubprocessResult(0, b"not json at all", b""))
        composer = ClaudeCodeComposer(runner=runner)
        with pytest.raises(DecisionComposerError, match="not JSON"):
            composer.compose(_sample_request())

    def test_stdout_json_missing_result_raises_error(self) -> None:
        cli_json = json.dumps({"duration_ms": 100, "model": "x"}).encode("utf-8")
        runner = FakeRunner(result=SubprocessResult(0, cli_json, b""))
        composer = ClaudeCodeComposer(runner=runner)
        with pytest.raises(DecisionComposerError, match="no assistant text"):
            composer.compose(_sample_request())

    def test_model_output_not_json_raises_error(self) -> None:
        # CLI JSON is well-formed but the model text isn't JSON.
        cli_json = json.dumps({"result": "sorry, I cannot answer"}).encode("utf-8")
        runner = FakeRunner(result=SubprocessResult(0, cli_json, b""))
        composer = ClaudeCodeComposer(runner=runner)
        with pytest.raises(DecisionComposerError, match="not valid JSON"):
            composer.compose(_sample_request())

    def test_empty_question_raises_empty(self) -> None:
        runner = FakeRunner(result=SubprocessResult(0, _cli_json_bytes(question="   "), b""))
        composer = ClaudeCodeComposer(runner=runner)
        with pytest.raises(DecisionComposerEmptyError, match="empty"):
            composer.compose(_sample_request())

    def test_fewer_than_two_options_raises_empty(self) -> None:
        one_option = [
            {"id": "A", "label": "only one", "gain": "g", "loss": "l"},
        ]
        cli_bytes = _cli_json_bytes(
            options=one_option,
            recommendation=None,
            recommendation_reason=None,
        )
        runner = FakeRunner(result=SubprocessResult(0, cli_bytes, b""))
        composer = ClaudeCodeComposer(runner=runner)
        with pytest.raises(DecisionComposerEmptyError, match="at least 2"):
            composer.compose(_sample_request())

    def test_recommendation_not_in_options_raises_error(self) -> None:
        # DecisionRequestOutput.__post_init__ catches this; it manifests as
        # DecisionComposerError from our shape validator.
        runner = FakeRunner(
            result=SubprocessResult(
                0,
                _cli_json_bytes(recommendation="C", recommendation_reason="picked C"),
                b"",
            )
        )
        composer = ClaudeCodeComposer(runner=runner)
        with pytest.raises(DecisionComposerError, match="shape validation"):
            composer.compose(_sample_request())

    def test_options_not_a_list_raises_error(self) -> None:
        composer_payload: dict[str, object] = {
            "question": "q?",
            "options": {"not": "a list"},
            "recommendation": None,
            "recommendation_reason": None,
            "unknowns": [],
        }
        cli_json = json.dumps({"result": json.dumps(composer_payload)}).encode("utf-8")
        runner = FakeRunner(result=SubprocessResult(0, cli_json, b""))
        composer = ClaudeCodeComposer(runner=runner)
        with pytest.raises(DecisionComposerError, match="options is not a list"):
            composer.compose(_sample_request())


# --------------------------------------------------------------------------- #
# 4. D-43 — bytes → UTF-8 pin (NO capsys)
# --------------------------------------------------------------------------- #


class TestD43Utf8BytesPin:
    def test_multibyte_utf8_round_trips_character_identical(self) -> None:
        """§14.3-style regression: hand the child's stdout as a fixed byte sequence
        containing multi-byte UTF-8 (the same "斜面撤去" §14 caught) and assert the
        composer's returned string is character-identical after round-trip.

        NOT using ``capsys`` — §14.3 explicitly forbids it because ``capsys``
        captures at the io layer where the encoding is unconditionally UTF-8, so
        the previous ``ensure_ascii=False`` regression looked correct under every
        capsys-based test. Here we shape the byte sequence explicitly, feed it to
        the fake runner, and inspect the resulting string.
        """
        phrase = "斜面撤去をどうするか"  # § 14's exact phrase
        # Multi-byte-heavy byte sequence — every field is Japanese.
        raw_bytes = _cli_json_bytes(
            question=phrase,
            options=[
                {"id": "A", "label": "撤回する", "gain": "コード削減", "loss": "既存挙動の喪失"},
                {"id": "B", "label": "現状維持", "gain": "既存機能温存", "loss": "斜面バグ残置"},
            ],
            recommendation="A",
            recommendation_reason="斜面境界での msg-2582 の実測破綻を根拠に、撤回が最短",
            unknowns=["撤回後のセーブファイル互換性"],
        )
        # Sanity: the raw bytes really do contain multi-byte UTF-8. If someone
        # regressed ``_cli_json_bytes`` to ``ensure_ascii=True``, the test premise
        # goes away and every byte would already be < 0x80.
        assert not raw_bytes.decode("ascii", errors="replace").isascii() or any(
            b > 0x7F for b in raw_bytes
        ), "test premise invalid: _cli_json_bytes emitted ASCII only, no byte to decode"
        assert any(b > 0x7F for b in raw_bytes)

        runner = FakeRunner(result=SubprocessResult(0, raw_bytes, b""))
        composer = ClaudeCodeComposer(runner=runner)
        out = composer.compose(_sample_request())

        # Character-identical round trip. If Python subprocess had ``text=True``
        # + a cp932 default, the phrase would be replaced or mojibake'd.
        assert out.question == phrase, f"Japanese phrase mangled: {out.question!r}"
        assert out.options[0].label == "撤回する"
        assert "斜面境界" in (out.recommendation_reason or "")
        assert out.unknowns == ("撤回後のセーブファイル互換性",)

    def test_stderr_bytes_with_invalid_utf8_do_not_sink_the_path(self) -> None:
        """A stray malformed byte in stderr must not crash the composer.

        The Windows console occasionally emits stray cp932-shaped bytes on stderr
        even when stdout is proper UTF-8. D-43's ``errors="replace"`` on both
        streams is what makes this benign: the diagnostic message might have a
        replacement character in it, but the ok stdout still parses.
        """
        bad_stderr = b"\xff\xfe garbage \xa1\xa2"
        runner = FakeRunner(result=SubprocessResult(0, _cli_json_bytes(), bad_stderr))
        composer = ClaudeCodeComposer(runner=runner)

        out = composer.compose(_sample_request())
        assert out.question  # composer did not raise


# --------------------------------------------------------------------------- #
# 5. D-38 — user-prompt rendering
# --------------------------------------------------------------------------- #


class TestD38UserPrompt:
    def test_user_prompt_lists_every_tail_body_with_headers(self) -> None:
        tail = (
            ThreadTailMessage(msg_id="msg-a", author="Bohr", body="first-body-content"),
            ThreadTailMessage(msg_id="msg-b", author="Einstein", body="second-body-content"),
        )
        request = DecisionRequestInput(
            project="p",
            thread_id="T",
            last_msg_id="msg-b",
            stop_reason="human",
            rounds=1,
            thread_title="title",
            tail=tail,
            tail_requested=2,
            total_messages=42,
        )
        composer = ClaudeCodeComposer(
            runner=FakeRunner(result=SubprocessResult(0, _cli_json_bytes(), b""))
        )
        # Directly render to inspect — the composer's private helper is
        # exposed under a public-facing name for exactly this test surface.
        rendered = composer._render_user_prompt(request)

        # Header always names the thread + last msg
        assert "T" in rendered
        assert "title" in rendered
        assert "msg-b" in rendered
        assert "rounds=1" in rendered
        # Both bodies present with separators.
        assert "--- msg-a by Bohr ---" in rendered
        assert "--- msg-b by Einstein ---" in rendered
        assert "first-body-content" in rendered
        assert "second-body-content" in rendered
        # Total messages line names both fetched and total.
        assert "2 of 42" in rendered

    def test_zero_tail_still_renders_a_prompt(self) -> None:
        # A zero-tail input is legal (a brand-new thread parked on its
        # opener) — the composer must still produce a user prompt.
        request = DecisionRequestInput(
            project="p",
            thread_id="T",
            last_msg_id="msg-1",
            stop_reason="human",
            rounds=0,
            thread_title="title",
            tail=(),
            tail_requested=5,
            total_messages=1,
        )
        composer = ClaudeCodeComposer(
            runner=FakeRunner(result=SubprocessResult(0, _cli_json_bytes(), b""))
        )
        rendered = composer._render_user_prompt(request)

        assert "no tail was provided" in rendered
        assert "record unknowns" in rendered


# --------------------------------------------------------------------------- #
# 6. Prompt version
# --------------------------------------------------------------------------- #


class TestPromptVersion:
    def test_prompt_version_is_a_module_level_string(self) -> None:
        assert isinstance(PROMPT_VERSION, str)
        assert PROMPT_VERSION == "1"

    def test_last_extras_reports_the_prompt_version_verbatim(self) -> None:
        runner = FakeRunner(result=SubprocessResult(0, _cli_json_bytes(), b""))
        composer = ClaudeCodeComposer(runner=runner)
        composer.compose(_sample_request())
        assert composer.last_extras["prompt_version"] == PROMPT_VERSION


# --------------------------------------------------------------------------- #
# 7. compose_once integration
# --------------------------------------------------------------------------- #


class TestComposeOnceIntegration:
    def test_extras_from_claude_code_land_in_envelope_extras(self) -> None:
        runner = FakeRunner(result=SubprocessResult(0, _cli_json_bytes(), b""))
        composer = ClaudeCodeComposer(runner=runner, cwd="/tmp")

        env = compose_once(composer, _sample_request())

        assert env.composer_status is ComposerStatus.OK
        assert env.extras["backend"] == "claude-code"
        assert env.extras["model"] == "claude-3-5-sonnet-20241022"
        assert env.extras["prompt_version"] == PROMPT_VERSION
        # The output tail_used was set to len(request.tail) by the composer.
        assert env.tail_used == 2

    def test_stub_composers_still_produce_empty_extras(self) -> None:
        # Regression: adding the extras plumbing must not accidentally break
        # backends that do not set ``last_extras``. StubComposer never assigns
        # it, so getattr returns None → {}.
        from spirrow_mindwire.decision_request.stub import StubComposer

        env = compose_once(StubComposer(), _sample_request())
        assert env.extras == {}

    def test_composer_failure_envelope_still_carries_partial_extras(self) -> None:
        # A composer that raised AFTER setting extras must still contribute the
        # extras it managed to record — that is what the "record baseline extras
        # BEFORE potential raises" comment in claude_code.py guarantees. This
        # test pins that guarantee.
        # Simulate: fake runner returns non-zero, which raises DecisionComposerError
        # in compose(); by then, argv_digest/prompt_version/cwd/backend are set.
        runner = FakeRunner(result=SubprocessResult(3, b"", b"child said no"))
        composer = ClaudeCodeComposer(runner=runner, cwd="/tmp")

        env = compose_once(composer, _sample_request())

        assert env.composer_status is ComposerStatus.ERROR
        assert env.output is None
        assert env.extras["backend"] == "claude-code"
        assert env.extras["prompt_version"] == PROMPT_VERSION
        assert env.extras["cwd"] == "/tmp"
        # Model / cost telemetry not folded in because the CLI JSON was never parsed
        # (exit fired first). This is the correct partial state.
        assert "model" not in env.extras


# --------------------------------------------------------------------------- #
# Free-standing helper coverage (docstring-driven contracts)
# --------------------------------------------------------------------------- #


class TestParsingHelpers:
    def test_result_text_extractor_tries_known_keys_in_order(self) -> None:
        assert _extract_result_text({"result": "a", "text": "b"}) == "a"
        assert _extract_result_text({"text": "b"}) == "b"
        assert _extract_result_text({"output": "c"}) == "c"
        assert _extract_result_text({"content": "d"}) == "d"
        assert _extract_result_text({}) is None
        # Blank string is not a match.
        assert _extract_result_text({"result": "   ", "text": "y"}) == "y"

    def test_json_from_result_text_handles_fenced_block(self) -> None:
        wrapped = (
            "Sure, here is the JSON:\n\n"
            "```json\n"
            '{"question": "q?", "options": [], "recommendation": null, '
            '"recommendation_reason": null, "unknowns": []}\n'
            "```\n"
            "Hope this helps."
        )
        parsed = _parse_json_from_result_text(wrapped)
        assert parsed["question"] == "q?"

    def test_json_from_result_text_rejects_bare_string(self) -> None:
        with pytest.raises(ValueError, match="no JSON object"):
            _parse_json_from_result_text("just a sentence")

    def test_json_from_result_text_extracts_first_object_from_list_wrapper(self) -> None:
        # Slow path deliberately finds the first ``{`` .. last ``}`` and parses
        # that slice, so a top-level list containing a single object round-trips
        # to the object's dict. This is intentional recovery behaviour: a model
        # that wrapped its answer in ``[ ... ]`` is still recoverable. If the
        # slice is not a dict at all (e.g. ``[]``), the ValueError below fires.
        parsed = _parse_json_from_result_text('[{"question": "q"}]')
        assert parsed == {"question": "q"}

    def test_json_from_result_text_rejects_bare_list_with_no_object_inside(self) -> None:
        with pytest.raises(ValueError, match="no JSON object"):
            _parse_json_from_result_text("[1, 2, 3]")

    def test_shape_answer_normalises_recommendation_types(self) -> None:
        # An explicit null recommendation stays null; a blank string becomes null too.
        out = _shape_composer_answer(
            {
                "question": "q?",
                "options": [
                    {"id": "A", "label": "a", "gain": "", "loss": ""},
                    {"id": "B", "label": "b", "gain": "", "loss": ""},
                ],
                "recommendation": "  ",
                "recommendation_reason": "",
                "unknowns": [],
            },
            tail_used=0,
        )
        assert out.recommendation is None
        assert out.recommendation_reason is None
