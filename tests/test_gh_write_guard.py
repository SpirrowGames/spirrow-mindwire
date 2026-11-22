"""L-p, the gh-write guard (``spirrow_mindwire.gh_write_guard``).

Thread ``T-real-infra-values-egress-from-agent-context`` (msg-9297 §3, msg-9299 §5, msg-9301).
The classification cases are the ones the M-10f / M-10g measurement script was tested and run with
(msg-8648 §3, msg-8650 §4 as revised by msg-9289 §1, msg-9289 §4, msg-9292 §1); every input here
is synthetic.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire import gh_write_guard as g
from spirrow_mindwire.config import MindwireSettings, Stage3LoopConfig
from spirrow_mindwire.loop_runner import _build_dispatcher
from spirrow_mindwire.value_objects import Role

#: A synthetic stand-in for a real value; it must never come back out of the guard.
MARK = "ZZSYNTHMARKER" + "4c1a"
H = "<<'EOF'\nbody " + MARK + "\nEOF"

CASES: list[tuple[str, str]] = [
    # the A' forms
    ("gh pr comment 1 --body-file - " + H, "a_prime_heredoc"),
    ('gh pr comment 1 --body-file - <<"EOF"\nb\nEOF', "a_prime_heredoc"),
    ("gh pr comment 1 --body-file - <<\\EOF\nb\nEOF", "a_prime_heredoc"),
    ("gh pr create --title t --body-file - " + H, "a_prime_heredoc"),
    ("gh api repos/o/r/issues/1/comments --input - " + H, "a_prime_heredoc"),
    ("gh api repos/o/r/issues -F body=@- " + H, "a_prime_heredoc"),
    ("gh pr comment 1 -R o/r --body-file - <<-'EOF'\n\tx\n\tEOF", "a_prime_heredoc"),
    ("gh pr comment 1 --body x", "a_prime_inline"),
    ("gh pr edit 5 --add-label x", "a_prime_inline"),
    ("gh pr review 3 --approve --body 'lgtm'", "a_prime_inline"),
    ("gh pr review 3 --comment -b 'x'", "a_prime_inline"),
    ("gh api -X POST repos/o/r/issues -f title=x", "a_prime_inline"),
    ("gh pr close 1 --comment x", "a_prime_inline"),
    # msg-8648 §3
    ("cd x && gh pr comment 1 --body-file f", "compound"),
    ("gh pr comment 1 --body-file f", "body_file_path"),
    ("gh api -F k=@f", "body_file_path"),
    ("gh pr comment 1 --body-file -", "stdin_no_heredoc"),
    ('gh pr comment 1 --body "$X"', "body_expansion"),
    # msg-8650 §4 as revised by msg-9289 §1
    ("gh gist create f", "body_file_path"),
    ("gh gist create -", "body_file_path"),
    ("gh gist create - " + H, "body_file_path"),
    ("gh release create v1 a.zip", "body_file_path"),
    ("gh release upload v1 -", "body_file_path"),
    ("gh release create v1 --notes x", "body_file_path"),
    ("gh gist edit 123 f", "body_file_path"),
    ("gh pr comment 1 --body x --foo y", "other"),
    ("gh label create x", "other"),
    # order and overlaps
    ("gh pr comment 1 --body-file - <<EOF\nb $X\nEOF", "heredoc_unquoted"),
    ("gh pr comment 1 --body-file - <<EOF\n$(id)\nEOF", "compound"),
    ('gh pr comment 1 --body "$(cat f)"', "compound"),
    ("gh pr comment 1 --body x | cat", "compound"),
    ("gh pr comment 1 --body x; echo", "compound"),
    ("(gh pr comment 1 --body x)", "compound"),
    ("gh pr comment 1 --body x &", "compound"),
    ("gh pr comment 1 --body-file f <<EOF\nx\nEOF", "heredoc_unquoted"),
    ('gh pr comment 1 --body-file "$F"', "body_file_path"),
    ("gh pr create -F f -t x", "body_file_path"),
    ("gh release create v1 --notes-file n", "body_file_path"),
    ('gh api x -f "body=$X"', "body_expansion"),
    ('gh pr comment 1 --body x -R "$R"', "other"),
    ("gh pr comment 1 2 --body x", "other"),
    ("gh api a b -f x=1", "other"),
    ("gh pr comment 1 --body x " + H, "other"),
    ("gh pr comment 1 --body-file - " + H + "\ncat <<'E'\nx\nE", "compound"),
    ("gh pr comment 1 --body-file - <<'A' <<'B'\nx\nA\ny\nB", "ts_parse_fail"),
    ("gh pr comment 1 --body-file - < f", "stdin_no_heredoc"),
    # '-' dropped before a non-heredoc redirect: not the recovery case, so fail-closed
    ("gh pr comment 1 --body-file - 2>&1 " + H, "ts_parse_fail"),
    ("gh pr comment 1 --body-file '-' 2>&1 " + H, "other"),
    ("gh pr comment 1 --body-file - x 2>&1", "stdin_no_heredoc"),
    ("GH_X=1 gh pr comment 1 --body x", "other"),
    ('pwsh -c "gh pr comment 1 --body-file f"', "other"),
    ("gh pr comment 1 --body 'unterminated", "ts_parse_fail"),
    ("gh pr merge 1 --squash", "other"),
]


@pytest.mark.parametrize(("cmd", "want"), CASES)
def test_classification(cmd: str, want: str) -> None:
    assert g.classify(cmd) == want


def test_classes_are_the_nine_and_two_are_allowed() -> None:
    assert len(g.CLASSES) == 9
    assert {"a_prime_heredoc", "a_prime_inline"} == g.ALLOWED_CLASSES


# ---------------------------------------------------------------- msg-8646 §1 parser gate
@pytest.mark.parametrize("start", ["'EOF'", '"EOF"', "\\EOF"])
def test_quoted_heredocs_parse_and_quote_is_visible(start: str) -> None:
    src = f"gh pr comment 1 --body-file - <<{start}\nhello $X\nEOF\n".encode()
    root = g.parse(src)
    assert not root.has_error
    starts = [n for n in g.walk(root) if n.type == "heredoc_start"]
    assert [(n.text or b"").decode() for n in starts] == [start]
    body = next(n for n in g.walk(root) if n.type == "heredoc_body")
    assert not any(c.type in g.NONLITERAL for c in g.walk(body))


def test_compound_forms_are_detected() -> None:
    assert g.classify("cd x && gh pr comment 1 --body y") == "compound"
    assert g.classify("cd x\ngh pr comment 1 --body y") == "compound"


# ------------------------------------------------- msg-9289 §4: the tree-level version watch
def test_dash_version_watch() -> None:
    """Fails if a tree-sitter-bash upgrade starts keeping the bare '-' before '<<'.

    When it does, decide again whether the recovery exception should stay (msg-9289 §4).
    """
    src = ("gh pr comment 1 --body-file - " + H).encode()
    root = g.parse(src)
    assert not root.has_error
    stmt = root.named_children[0]
    cmd = stmt.child_by_field_name("body")
    assert cmd is not None
    assert "-" not in [g.value(a) for a in cmd.children_by_field_name("argument")]
    hd = next(c for c in stmt.children if c.type == "heredoc_redirect")
    assert src[cmd.end_byte : hd.start_byte].strip() == b"-"
    ok, dash = g.gap_scan(root, src)
    assert ok
    assert len(dash) == 1


def test_recovery_flag_and_posfile_exclusion() -> None:
    assert g.classify_ex("gh pr comment 1 --body-file - " + H)[2]
    assert not g.classify_ex("gh pr comment 1 --body x")[2]
    # msg-9289 §4 third condition: no recovery for a class-4 command
    assert g.classify_ex("gh gist create - " + H) == ("body_file_path", True, False)


@pytest.mark.parametrize(
    "cmd",
    [
        "cd x && gh pr comment 1 --body-file - " + H,
        "git push || gh pr comment 1 --body-file - " + H,
        "echo a | gh pr comment 1 --body-file - " + H,
        "a; gh pr comment 1 --body-file - " + H,
        "x && y | gh api r --input - " + H,
    ],
)
def test_widened_recovery_stays_compound(cmd: str) -> None:
    """msg-9292 §1: recovery at the tail of a list / pipeline never reaches an allowed class."""
    assert g.classify_ex(cmd) == ("compound", True, True)


# ---------------------------------------------------------------------- gh-write detection
@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr comment 1 --body x",
        "gh issue create -t x",
        "gh api -X PATCH x",
        "gh api x --input -",
        "gh api x -fa=b",
        "gh pr close 1 -c x",
        "gh release create v1 --notes x",
        "gh pr merge 1 --body x",
        "cd x && gh pr comment 1 --body-file f",
        'pwsh -c "gh pr comment 1 --body x"',
        "gh pr comment 1 --body 'unterminated",
        "gh release create v1 a.zip",
        "gh gist create f",
        "gh gist edit 1 f",
        "gh release upload v1 a",
    ],
)
def test_is_gh_write(cmd: str) -> None:
    assert g.is_gh_write(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr view 1",
        "gh pr view 1 -c",
        "gh api repos/x",
        "gh pr list",
        "echo gh pr comment",
        "cat <<'EOF'\ngh pr comment 1 --body x\nEOF",
        "gh release list",
        "gh gist view 1",
        "git status",
    ],
)
def test_is_not_gh_write(cmd: str) -> None:
    assert not g.is_gh_write(cmd)


def test_positional_file_commands_are_new_writes_only() -> None:
    assert g.gh_write_kind("gh gist create f") == (True, False)
    assert g.gh_write_kind("gh release create v1 --notes x") == (True, True)


# ---------------------------------------------------------------------------- the hook
def _bash(cmd: object) -> dict[str, Any]:
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}}


def _run(data: Any) -> Any:
    return asyncio.run(g.gh_write_pretooluse(data, None, {"signal": None}))


def _reason(out: Any) -> str:
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "deny"
    reason = spec["permissionDecisionReason"]
    assert isinstance(reason, str)
    return reason


@pytest.mark.parametrize(
    "cmd",
    [
        "git status",
        "uv run pytest",
        "gh pr view 1",
        "gh pr comment 1 --body-file - " + H,
        "gh pr comment 1 --body x",
    ],
)
def test_hook_passes_non_writes_and_allowed_forms(cmd: str) -> None:
    assert _run(_bash(cmd)) == {}


def test_hook_ignores_other_tools() -> None:
    assert _run({"tool_name": "Read", "tool_input": {"file_path": MARK}}) == {}


#: gh calls outside the approved gh-write definition (msg-8646 §4, msg-9287 §2). Before L-p2
#: they ran unjudged (msg-9306 §1); L-p2 (msg-9821 §2) judges them by the table instead.
OUT_OF_DEFINITION = {
    "gh label create x": "gh_not_in_table",
    "gh pr merge 1 --squash": None,
    "gh variable set X --body v": "gh_secret_value",
    "gh secret set X": "gh_secret_value",
    "gh repo edit --description d": "gh_not_in_table",
}
#: CASES entries outside the definition (``classify`` still names their L-p class for parity).
NOT_WRITES = set(OUT_OF_DEFINITION) & {c for c, _ in CASES}


def test_calls_outside_the_definition_are_judged_by_the_table() -> None:
    for cmd, want in OUT_OF_DEFINITION.items():
        assert not g.is_gh_write(cmd), cmd
        out = _run(_bash(cmd))
        if want is None:
            assert out == {}, cmd
        else:
            assert f"[{want}]" in _reason(out), cmd


@pytest.mark.parametrize(
    ("cmd", "want"),
    [(c, w) for c, w in CASES if w not in g.ALLOWED_CLASSES and c not in NOT_WRITES],
)
def test_hook_denies_every_other_class_with_a_fixed_text(cmd: str, want: str) -> None:
    reason = _reason(_run(_bash(cmd)))
    assert f"[{want}]" in reason
    assert MARK not in reason


def test_deny_text_is_chosen_by_family() -> None:
    gist = _reason(_run(_bash("gh gist create - " + H)))
    pr = _reason(_run(_bash("gh pr comment 1 --body-file f")))
    api = _reason(_run(_bash("gh api x -F k=@f")))
    assert "no allowed form" in gist
    assert "--body-file -" in pr
    assert "endpoint" in api


def test_deny_text_echoes_nothing_from_the_command() -> None:
    cmd = f"cd {MARK} && gh pr comment 1 --body-file {MARK} -R {MARK}/{MARK}"
    reason = _reason(_run(_bash(cmd)))
    assert MARK not in reason
    # every deny text is built from constants only
    for key in (*g.CLASSES, g.GUARD_ERROR_KEY):
        for fam in (g.FAMILY_PR_ISSUE, g.FAMILY_API, g.FAMILY_UNSUPPORTED, g.FAMILY_UNKNOWN):
            assert MARK not in g.deny_text(key, fam)


# --------------------------------------------- fail-closed, including the exception paths
def test_non_string_command_is_denied() -> None:
    assert "[guard_error]" in _reason(_run(_bash(None)))


def test_parser_exception_is_denied_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(_src: bytes) -> Any:
        raise RuntimeError(f"parser failed on {MARK}")

    monkeypatch.setattr(g, "parse", boom)
    caplog.set_level(logging.DEBUG)
    # detection is indeterminate and the text names gh -> a write; classification raises -> 1
    reason = _reason(_run(_bash(f"gh pr comment 1 --body {MARK}")))
    assert "[ts_parse_fail]" in reason
    out = capsys.readouterr()
    for text in (reason, out.out, out.err, caplog.text):
        assert MARK not in text


def test_any_exception_in_the_guard_is_denied_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(_cmd: str) -> bool:
        raise ValueError(f"guard failed on {MARK}")

    monkeypatch.setattr(g, "is_gh_write", boom)
    caplog.set_level(logging.DEBUG)
    reason = _reason(_run(_bash(f"anything {MARK}")))
    assert "[guard_error]" in reason
    out = capsys.readouterr()
    for text in (reason, out.out, out.err, caplog.text):
        assert MARK not in text


def test_exception_in_decide_itself_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_cmd: object) -> tuple[bool, str | None]:
        raise RuntimeError(MARK)

    monkeypatch.setattr(g, "decide", boom)
    reason = _reason(_run(_bash("gh pr comment 1 --body x")))
    assert "[guard_error]" in reason
    assert MARK not in reason


# ----------------------------------------------------- production wiring (msg-9299 §5)
class _InertMcp:
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:  # pragma: no cover
        raise AssertionError(f"must not call magickit ({name})")


def _implementer_adapter(tmp_path: Path) -> Any:
    settings = MindwireSettings(loop=Stage3LoopConfig(repo_dir=tmp_path))
    _, registry, _ = _build_dispatcher(
        settings, mcp=_InertMcp(), proposer=None, implementer=None, naysayer=None
    )
    return registry.qualified_for(Role.IMPLEMENTER)[0]


def _bash_guards(options: Any) -> list[Any]:
    matchers = (options.hooks or {}).get("PreToolUse", [])
    return [h for m in matchers if m.matcher == "Bash" for h in m.hooks]


def test_production_implementer_attaches_the_guard_to_bash(tmp_path: Path) -> None:
    from spirrow_mindwire.adapters._turn_telemetry import TurnTelemetryState

    adapter = _implementer_adapter(tmp_path)
    for options in (
        adapter._make_options(),
        adapter._make_options(telemetry=TurnTelemetryState()),
    ):
        assert g.gh_write_pretooluse in _bash_guards(options)
        assert "Bash" in options.tools
    # the compaction observer is still there on the primary path
    assert "PreCompact" in adapter._make_options(telemetry=TurnTelemetryState()).hooks


def test_production_bash_hook_is_the_one_the_live_session_checked(tmp_path: Path) -> None:
    """msg-9304 §2 / msg-9306 §2. Guarantees only this: the production builder registers the
    same PreToolUse callback with the same matcher as ``gh_write_guard_hooks()``, which is what the
    one live SDK session (fake ``gh`` on PATH, PR #579) was run with. It does NOT show that the
    other production options, the hook's coexistence with ``PreCompact``, or the hand-off to the
    SDK behave as in that session; no live session was run with the production options."""
    from spirrow_mindwire.adapters._turn_telemetry import TurnTelemetryState

    [checked] = g.gh_write_guard_hooks()["PreToolUse"]
    adapter = _implementer_adapter(tmp_path)
    for options in (
        adapter._make_options(),
        adapter._make_options(telemetry=TurnTelemetryState()),
    ):
        [prod] = options.hooks["PreToolUse"]
        assert prod.matcher == checked.matcher == "Bash"
        assert prod.hooks == checked.hooks == [g.gh_write_pretooluse]
        assert prod.timeout == checked.timeout


def test_prompts_carry_p1() -> None:
    from spirrow_mindwire.adapters.claude_code_sdk import _DEFAULT_SYSTEM_PROMPT
    from spirrow_mindwire.adapters.implementer import _DEFAULT_IMPLEMENTER_SYSTEM_PROMPT

    assert g.P1_PROMPT_BLOCK in _DEFAULT_IMPLEMENTER_SYSTEM_PROMPT
    assert g.P1_PROMPT_BLOCK in _DEFAULT_SYSTEM_PROMPT


# ------------------------------------------- PR-gate on #579: the four blocking findings
#: Objection 1: options before the group or the sub must not hide a write from detection.
@pytest.mark.parametrize(
    "cmd",
    [
        "gh --repo o/r pr comment 1 --body-file /tmp/" + MARK,
        "gh -R o/r pr comment 1 --body-file /tmp/" + MARK,
        "gh -Ro/r pr comment 1 --body x",
        "gh --repo=o/r pr comment 1 --body x",
        "gh pr -R o/r create --fill",
        "gh pr --repo o/r comment 1 --body-file - " + H,
        "gh --unknown-flag v pr comment 1 --body x",
        "gh --unknown-bool pr comment 1 --body x",
        "gh --repo o/r api repos/o/r/issues -f title=x",
    ],
)
def test_leading_options_are_detected_and_denied(cmd: str) -> None:
    assert g.is_gh_write(cmd), cmd
    allowed, reason = g.decide(cmd)
    assert not allowed
    assert reason is not None and "[other]" in reason and MARK not in reason


@pytest.mark.parametrize("cmd", ["gh --version", "gh --help", "gh -R o/r pr view 1"])
def test_leading_options_on_reads_are_not_writes_but_not_in_the_table(cmd: str) -> None:
    # Not a write (the L-p definition is unchanged), but L-p2 has no row for an option before
    # the group: msg-9819 §1 step 3 / msg-9821 §1 step 4.
    assert not g.is_gh_write(cmd)
    allowed, reason = g.decide(cmd)
    assert not allowed
    assert reason is not None and "[gh_not_in_table]" in reason


def test_leading_option_readings_are_bounded() -> None:
    cmd = "gh " + " ".join(f"--u{i}" for i in range(40)) + " pr view 1"
    assert g.is_gh_write(cmd)  # past the bound, detection says write (fail-closed)
    assert not g.decide(cmd)[0]


#: Objection 2: the heredoc form validates assignments, every argument and the option list.
@pytest.mark.parametrize(
    "cmd",
    [
        'gh api repos/o/r/issues --input - -H "X-Leak: $SECRET" ' + H,
        'gh api repos/o/r/issues --input - -H "X-Leak: $(cat f)" ' + H,
        "X=1 gh pr comment 1 --body-file - " + H,
        'gh pr comment 1 --body-file - -R "$R" ' + H,
        "gh pr comment 1 --body-file - --foo y " + H,
        "gh pr comment 1 2 --body-file - " + H,
        "gh pr comment ~ --body-file - " + H,
        "gh pr checkout 1 --body-file - " + H,
        "gh release edit v1 --notes-file - " + H,
        "/usr/bin/gh pr comment 1 --body-file - " + H,
    ],
)
def test_heredoc_form_validates_the_rest_of_the_command(cmd: str) -> None:
    cls = g.classify(cmd)
    assert cls not in g.ALLOWED_CLASSES, (cmd, cls)
    if g.is_gh_write(cmd):
        assert not g.decide(cmd)[0]


@pytest.mark.parametrize(
    "cmd",
    [
        'gh api repos/o/r/issues --input - -H "X-A: b" ' + H,
        "gh pr comment 1 -R o/r --body-file - " + H,
        "gh issue create -t x --body-file - " + H,
        "gh pr review 3 --approve --body-file - " + H,
    ],
)
def test_heredoc_form_with_listed_literal_options_is_allowed(cmd: str) -> None:
    assert g.classify(cmd) == "a_prime_heredoc"
    assert g.decide(cmd) == (True, None)


#: Objection 3: an unquoted word that Bash expands is not a literal.
@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr comment 1 --body ~",
        "gh pr comment 1 --body ~/x",
        "gh pr comment 1 --body=~",
        "gh pr comment 1 --body *",
        "gh pr comment 1 --body a?",
        "gh pr comment 1 --body [ab]",
        "gh pr comment 1 --body {a,b}",
        "gh pr comment 1 --body x{1..3}",
        "gh api repos/o/r/* -f title=x",
        "gh api repos/o/r/issues -f title=~",
    ],
)
def test_expanding_unquoted_words_are_not_literal(cmd: str) -> None:
    assert g.is_gh_write(cmd)
    assert g.classify(cmd) not in g.ALLOWED_CLASSES
    assert not g.decide(cmd)[0]


@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr comment 1 --body '~'",
        'gh pr comment 1 --body "a*b"',
        r"gh pr comment 1 --body \~",
        r"gh pr comment 1 --body a\*",
    ],
)
def test_quoted_or_escaped_expansion_characters_stay_literal(cmd: str) -> None:
    assert g.classify(cmd) == "a_prime_inline"


#: Objection 4: a parser failure leaves detection indeterminate, which is denied for any command
#: naming gh — never settled by the gh-write regex.
@pytest.mark.parametrize(
    "cmd",
    [
        "'gh' pr comment 1 --body-file /tmp/" + MARK,
        '"gh" pr comment 1 --body-file /tmp/' + MARK,
        'g"h" pr comment 1 --body x',
        r"g\h pr comment 1 --body x",
        "/usr/bin/gh pr comment 1 --body-file /tmp/" + MARK,
        r"C:\tools\gh.exe pr comment 1 --body x",
        "gh --repo o/r pr comment 1 --body x",
        "gh pr view 1",
    ],
)
def test_parser_exception_denies_any_command_naming_gh(
    monkeypatch: pytest.MonkeyPatch, cmd: str
) -> None:
    def boom(_src: bytes) -> Any:
        raise RuntimeError(MARK)

    monkeypatch.setattr(g, "parse", boom)
    assert g.is_gh_write(cmd)
    reason = _reason(_run(_bash(cmd)))
    assert "[ts_parse_fail]" in reason
    assert MARK not in reason


def test_parser_exception_lets_commands_without_gh_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_src: bytes) -> Any:
        raise RuntimeError(MARK)

    monkeypatch.setattr(g, "parse", boom)
    for cmd in ("git status", "uv run pytest", "cat github.txt", "ls ./ghost"):
        assert _run(_bash(cmd)) == {}, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "'gh' pr comment 1 --body 'unterminated",
        "gh --repo o/r pr comment 1 --body 'unterminated",
        "bash -c \"'gh' pr comment 1 --body-file f\"",
        'bash -c "gh --repo o/r pr comment 1 --body-file f"',
    ],
)
def test_unparsable_or_wrapped_gh_writes_are_denied(cmd: str) -> None:
    assert g.is_gh_write(cmd)
    assert not g.decide(cmd)[0]


# ------------------------------------------ PR-gate on #583: attached short body options
#: gh accepts a short option's value attached to it (``-b"text"``) and short options bundled
#: behind one dash (``-sb"text"``). Both carry a body, so both must be detected as writes.
@pytest.mark.parametrize(
    "cmd",
    [
        'gh pr merge 1 --squash -b"$(cat private.txt)"',
        "gh pr merge 1 --squash -b$(cat private.txt)",
        'gh pr merge 1 -sb"$(cat private.txt)"',
        'gh pr merge 1 -sdb"$(cat private.txt)"',
        'gh pr merge 1 --squash -t"$(cat private.txt)"',
        'gh pr close 1 -c"$(cat private.txt)"',
        'gh issue close 1 -c"$(cat private.txt)"',
        'gh pr merge 1 --squash -b="$(cat private.txt)"',
    ],
)
def test_attached_short_body_options_are_detected_and_denied(cmd: str) -> None:
    assert g.is_gh_write(cmd), cmd
    allowed, reason = g.decide(cmd)
    assert not allowed, cmd
    assert reason is not None and "private.txt" not in reason


@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr merge 1 -sbliteral",
        "gh pr merge 1 --squash -bliteral",
        "gh release edit v1 -nnotes",
    ],
)
def test_attached_short_body_options_with_literal_values_are_writes(cmd: str) -> None:
    assert g.is_gh_write(cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr merge 1 -sd",  # bundled flags, no body letter
        "gh pr merge 1 --squash -Robot/content",  # -R takes a value: its letters are not options
        "gh pr merge 1 -sRobot/tub",
        "gh pr close 1 -Robot/content",
        "gh pr ready 1 -Rbot/cnt",
    ],
)
def test_attached_values_of_other_options_are_not_body_options(cmd: str) -> None:
    assert not g.is_gh_write(cmd), cmd
    # Not a write, so L-p2 judges it: bundled short options and attached values are not among
    # the argument shapes R-d accepts (msg-9821 §2.1).
    assert "[gh_not_in_table]" in _reason(_run(_bash(cmd))), cmd


# ------------- T-lp-allowlist-lacks-base-head-so-develop-flow-repos-cannot-open-prs (D1)
#: ``gh pr create`` takes ``-B/--base`` and ``-H/--head`` as a plain literal branch name, once each.
_PRC = "gh pr create -R o/r "


@pytest.mark.parametrize(
    "cmd",
    [
        _PRC + "--base develop --head feature/x --title t --body-file - " + H,
        _PRC + "-B main -H fix/y --title t --body-file - " + H,
        _PRC + "--head feature/x --title t --body-file - " + H,
        _PRC + "--base=develop --title t --body-file - " + H,
        _PRC + "--base develop --title t --body x",
    ],
)
def test_pr_create_accepts_branch_name_base_and_head(cmd: str) -> None:
    assert g.classify(cmd) in g.ALLOWED_CLASSES
    assert g.decide(cmd) == (True, None)


@pytest.mark.parametrize(
    "cmd",
    [
        _PRC + '--base "$B" --title t --body-file - ' + H,
        _PRC + "--head owner:feature/x --title t --body-file - " + H,
        _PRC + "--base ../x --title t --body-file - " + H,
        _PRC + "--base a..b --title t --body-file - " + H,
        _PRC + "--base a//b --title t --body-file - " + H,
        _PRC + "--base a/ --title t --body-file - " + H,
        _PRC + "--base a.lock --title t --body-file - " + H,
        _PRC + "--base -x --title t --body-file - " + H,
        _PRC + "--head a b --title t --body-file - " + H,
        _PRC + "--base develop --base main --title t --body-file - " + H,
        _PRC + "-B develop --base main --title t --body-file - " + H,
        _PRC + "--title t --body-file - --base " + H,
        "gh pr edit 1 -R o/r --base develop",
        "gh pr edit 1 -R o/r --base develop --body-file - " + H,
        "gh issue create -R o/r --base x --title t --body-file - " + H,
        "gh pr comment 1 -R o/r --head x --body-file - " + H,
        _PRC + "--base develop --title t --body-file - --fill " + H,
        _PRC + "--base develop --title t --body-file - --dry-run " + H,
        # D2 / D4 were cut from this thread (msg-9964): still denied.
        _PRC + "--draft --base develop --title t --body-file - " + H,
        "gh api repos/o/r/pulls/1 -X PATCH -f title=t --jq .title",
        "gh api repos/o/r/pulls/1 -X PATCH -f title=t --silent",
        'gh api repos/o/r/pulls/1 -X PATCH -f title=t --jq "$Q"',
    ],
)
def test_pr_create_ref_options_outside_the_shape_stay_denied(cmd: str) -> None:
    assert g.classify(cmd) not in g.ALLOWED_CLASSES
    allowed, reason = g.decide(cmd)
    assert not allowed and reason is not None


def test_api_header_is_not_the_pr_create_head_option() -> None:
    # ``-H`` stays ``--header`` to ``gh api``; the branch-name rule does not apply there.
    assert g.classify("gh api -X POST repos/o/r/issues -H Accept:x -f title=t") == (
        "a_prime_inline"
    )


def test_p1_names_base_and_head_for_pr_create() -> None:
    assert "`gh pr create` also accepts `--base`/`--head`" in g.P1_PROMPT_BLOCK
    assert "--draft" not in g.P1_PROMPT_BLOCK


# ============================================================================ L-p2 (msg-9821)
def _deny_key(cmd: str) -> str:
    allowed, reason = g.decide(cmd)
    assert not allowed, cmd
    assert reason is not None
    m = [k for k in (*g.L2_CLASSES, *g.CLASSES) if f"[{k}]" in reason]
    assert len(m) == 1, (cmd, reason)
    return m[0]


#: msg-9821 §2: at least one command per allowed row, aliases included (msg-9819 §4).
L2_ALLOWED_CMDS = [
    "gh pr list",
    "gh pr ls -s open -L 20",
    "gh pr view 12 -R o/r --json state",
    "gh pr view 12 --json state,title --jq .state",
    "gh pr diff 12",
    "gh pr checks 12 --watch --exit-status",
    "gh pr status",
    "gh pr checkout 12",
    "gh pr co 12",
    "gh pr ready 12",
    "gh pr update-branch 12",
    "gh pr close 12",
    "gh pr reopen 12",
    "gh pr merge 12 --squash -d",
    "gh pr merge 12 --merge --auto",
    "gh issue list --state=closed",
    "gh issue ls",
    "gh issue view 3",
    "gh issue status",
    "gh issue close 3",
    "gh issue reopen 3",
    "gh run list -L 5",
    "gh run ls",
    "gh run view 99 --log-failed",
    "gh run watch 99 --exit-status",
    "gh run download 99 -n artifact-1",
    "gh workflow list",
    "gh workflow view ci.yml",
    "gh repo view o/r",
    "gh repo clone o/r",
    "gh release list",
    "gh release view v1.2.0",
    "gh release download v1.2.0 -p dist.zip",
    "gh label list",
    "gh secret list",
    "gh cache list",
    "gh ruleset list",
    "gh rs view 7",
    "gh ruleset check main",
    "gh auth setup-git",
    "gh auth status",
    "gh auth status -a",
    "gh auth status --active",
]


@pytest.mark.parametrize("cmd", L2_ALLOWED_CMDS)
def test_table_allowed_rows(cmd: str) -> None:
    assert not g.is_gh_write(cmd)
    assert g.decide(cmd) == (True, None)
    assert _run(_bash(cmd)) == {}


L2_DENIED_CMDS: list[tuple[str, str]] = [
    # gh_auth_token (msg-9797 §2, msg-9819 §2)
    ("gh auth token", "gh_auth_token"),
    ("gh auth token -h example.invalid", "gh_auth_token"),
    ("gh auth status -t", "gh_auth_token"),
    ("gh auth status --show-token", "gh_auth_token"),
    ("gh auth status -at", "gh_auth_token"),
    ("gh auth status -h example.invalid", "gh_auth_token"),
    # gh_secret_value
    ("gh variable list", "gh_secret_value"),
    ("gh variable ls", "gh_secret_value"),
    ("gh variable get X", "gh_secret_value"),
    ("gh secret set X", "gh_secret_value"),
    ("gh secret delete X", "gh_secret_value"),
    ("gh secret remove X", "gh_secret_value"),
    # gh_search_text
    ("gh search issues x", "gh_search_text"),
    ("gh pr list -S x", "gh_search_text"),
    ("gh issue list --search x", "gh_search_text"),
    ("gh pr list --search=x", "gh_search_text"),
    # gh_not_in_table: auth rows
    ("gh auth login", "gh_not_in_table"),
    ("gh auth logout", "gh_not_in_table"),
    ("gh auth refresh", "gh_not_in_table"),
    ("gh auth switch", "gh_not_in_table"),
    # gh_not_in_table: groups and subs not on the table
    ("gh gist list", "gh_not_in_table"),
    ("gh codespace list", "gh_not_in_table"),
    ("gh cs list", "gh_not_in_table"),
    ("gh extension list", "gh_not_in_table"),
    ("gh alias list", "gh_not_in_table"),
    ("gh config get editor", "gh_not_in_table"),
    ("gh ssh-key list", "gh_not_in_table"),
    ("gh browse", "gh_not_in_table"),
    ("gh status", "gh_not_in_table"),
    ("gh repo list", "gh_not_in_table"),
    ("gh repo ls", "gh_not_in_table"),
    ("gh repo delete o/r", "gh_not_in_table"),
    ("gh release delete v1", "gh_not_in_table"),
    ("gh label create x", "gh_not_in_table"),
    ("gh workflow run ci.yml", "gh_not_in_table"),
    ("gh run rerun 99", "gh_not_in_table"),
    ("gh issue delete 3", "gh_not_in_table"),
    ("gh pr lock 3", "gh_not_in_table"),
    ("gh pr frobnicate 3", "gh_not_in_table"),  # not in the vocabulary
    ("gh frobnicate", "gh_not_in_table"),
    ("gh", "gh_not_in_table"),
    ("gh --version", "gh_not_in_table"),
    ("gh help", "gh_not_in_table"),
    ("gh pr", "gh_not_in_table"),
    # gh api outside the write definition (Obj-26-1 cut)
    ("gh api '/repos/O/R/issues?probe=" + MARK + "'", "gh_not_in_table"),
    ("gh api /user", "gh_not_in_table"),
    ("gh api", "gh_not_in_table"),
    # R-d: arguments outside the accepted shapes
    ("gh repo view 'x:" + MARK + "'", "gh_not_in_table"),
    ("gh pr list --author " + MARK, "gh_not_in_table"),
    ("gh pr view https://github.com/o/r/pull/1", "gh_not_in_table"),
    ("gh run download 1 -D /tmp/" + MARK, "gh_not_in_table"),
    ("gh pr list --label x", "gh_not_in_table"),
    ("gh pr list --head x", "gh_not_in_table"),
    ("gh pr list -s weird", "gh_not_in_table"),
    ("gh pr view 1 -R notarepo", "gh_not_in_table"),
    ("gh pr view 1 -L x", "gh_not_in_table"),
    ("gh pr view 1 --json", "gh_not_in_table"),
    ("gh pr view '1 2'", "gh_not_in_table"),
    ("gh pr view a@b", "gh_not_in_table"),
    ("gh pr view a=b", "gh_not_in_table"),
    ("gh pr view 'a?b'", "gh_not_in_table"),
    ("gh pr view 1 --squash", "gh_not_in_table"),  # a flag of another row
    ("gh pr view 1 --", "gh_not_in_table"),
    ('gh pr view "$X"', "gh_not_in_table"),  # not literal
    ("gh pr -R o/r view 1", "gh_not_in_table"),
    # gh in a wrapper shell or behind a launcher: the shape is not visible
    ('bash -c "gh auth token"', "gh_not_in_table"),
    ('pwsh -c "gh pr view 1"', "gh_not_in_table"),
    ("env gh auth token", "gh_not_in_table"),
    ("xargs gh auth token", "gh_not_in_table"),
    ("command gh auth token", "gh_not_in_table"),
]


@pytest.mark.parametrize(("cmd", "want"), L2_DENIED_CMDS)
def test_table_denied_rows(cmd: str, want: str) -> None:
    assert _deny_key(cmd) == want


def test_l2_classes_are_the_four() -> None:
    assert g.L2_CLASSES == ("gh_auth_token", "gh_secret_value", "gh_search_text", "gh_not_in_table")
    assert not set(g.L2_CLASSES) & set(g.CLASSES)


def test_gh_auth_token_text_is_the_approved_one() -> None:
    _, reason = g.decide("gh auth token")
    assert reason == (
        "L-p denied this gh call [gh_auth_token]: printing a GitHub token into the session is "
        "not allowed. If you need it for git authentication, run `gh auth setup-git` once in its "
        "own Bash call and then use git as usual. If your use cannot be replaced that way, stop "
        "and raise it to a human in the thread (`NEXT: human`). To ask for this command to be "
        "added to the allowed table, post in the thread the command shape with every real value "
        "(host names, paths, tokens, IDs) replaced by a placeholder, why it is needed, and why "
        "no allowed form works. Do not paste the token or the command's output."
    )
    # --show-token is the same class and the same text
    assert g.decide("gh auth status --show-token") == (False, reason)


@pytest.mark.parametrize("key", ["gh_secret_value", "gh_search_text", "gh_not_in_table"])
def test_other_l2_texts_carry_the_placeholder_request(key: str) -> None:
    text = g.deny_text(key, g.FAMILY_UNKNOWN)
    assert text.startswith(f"L-p denied this gh call [{key}]: ")
    assert text.endswith(
        "post in the thread the command shape with every real value (host names, paths, tokens, "
        "IDs) replaced by a placeholder, why it is needed, and why no allowed form works. Do not "
        "paste the token or the command's output."
    )


def test_existing_texts_still_say_gh_write() -> None:
    for key in g.CLASSES:
        assert g.deny_text(key, g.FAMILY_PR_ISSUE).startswith(f"L-p denied this gh write [{key}]")


@pytest.mark.parametrize(
    "cmd",
    [
        "gh auth token -h " + MARK,
        "gh auth status -h " + MARK,
        "gh variable get " + MARK,
        "gh search code " + MARK,
        "gh pr list -S " + MARK,
        "gh api '/repos/O/R/issues?probe=" + MARK + "'",
        "gh " + MARK,
        "gh repo view 'x:" + MARK + "'",
        "env " + MARK + "=1 gh auth token",
        "gh pr view 1 && gh auth token -h " + MARK,
    ],
)
def test_l2_deny_texts_echo_nothing_from_the_command(cmd: str) -> None:
    reason = _reason(_run(_bash(cmd)))
    assert MARK not in reason
    for key in g.L2_CLASSES:
        for fam in (g.FAMILY_PR_ISSUE, g.FAMILY_API, g.FAMILY_UNSUPPORTED, g.FAMILY_UNKNOWN):
            assert MARK not in g.deny_text(key, fam)


# ------------------------------------------------- combine (msg-9821 §1, msg-9823 Obj-27-1)
OLD_ALLOW = g.Verdict(True)  # what an allowed gh write node (a_prime_*) carries
OLD_DENY = g.Verdict(False, "body_file_path")
NEW_ALLOW = g.Verdict(True)  # what an allowed table row carries
NEW_DENY = g.Verdict(False, "gh_auth_token")


@pytest.mark.parametrize(
    ("verdicts", "want"),
    [
        # old allow / new deny, both orders
        ([OLD_ALLOW, NEW_DENY], NEW_DENY),
        ([NEW_DENY, OLD_ALLOW], NEW_DENY),
        # old deny / new allow, both orders
        ([OLD_DENY, NEW_ALLOW], OLD_DENY),
        ([NEW_ALLOW, OLD_DENY], OLD_DENY),
        # both deny: the first one names the class, in either order
        ([OLD_DENY, NEW_DENY], OLD_DENY),
        ([NEW_DENY, OLD_DENY], NEW_DENY),
        # an allow never ends the judgement early, however many come first
        ([OLD_ALLOW, NEW_ALLOW, NEW_ALLOW, NEW_DENY], NEW_DENY),
        ([OLD_ALLOW, NEW_ALLOW], g.Verdict(True)),
        ([], g.Verdict(True)),
    ],
)
def test_combine_folds_old_and_new_verdicts(verdicts: list[g.Verdict], want: g.Verdict) -> None:
    assert g.combine(verdicts) == want


def test_combine_a_deny_without_a_class_is_a_guard_error() -> None:
    assert g.combine([g.Verdict(False)]) == g.Verdict(False, "guard_error")


def test_decide_feeds_every_node_to_combine(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[g.Verdict]] = []
    real = g.combine

    def spy(vs: Any) -> g.Verdict:
        seen.append(list(vs))
        return real(vs)

    monkeypatch.setattr(g, "combine", spy)
    g.decide("gh pr view 1 && gh pr checks 1 && gh auth token")
    assert seen == [[g.Verdict(True), g.Verdict(True), g.Verdict(False, "gh_auth_token")]]


#: msg-9821 §4: command-level regressions for mixed commands, both orders (kept alongside the
#: combine tests; they do not by themselves show the folding, since `compound` denies first).
@pytest.mark.parametrize(
    "cmd",
    [
        "gh issue comment 1 --body-file - <<'EOF'\nx\nEOF\ngh auth token",
        "gh auth token; gh issue comment 1 --body-file - <<'EOF'\nx\nEOF",
        "gh issue comment 1 --body safe; gh auth token",
        "gh pr view 1 && gh auth token",
        "gh auth token && gh pr view 1",
    ],
)
def test_mixed_commands_are_denied_in_either_order(cmd: str) -> None:
    assert not g.decide(cmd)[0]


def test_reads_combined_with_reads_are_allowed() -> None:
    assert g.decide("gh pr view 1 && gh pr checks 1") == (True, None)


@pytest.mark.parametrize(
    ("cmd", "want"),
    [
        ("gh api --method GET -f probe=" + MARK + " /repos/O/R", "gh_not_in_table"),
        ("gh api -X GET /repos/O/R -f probe=" + MARK, "gh_not_in_table"),
        ("gh api --method=get -f probe=" + MARK + " /repos/O/R", "gh_not_in_table"),
        ("gh pr merge 1 -t " + MARK, "gh_search_text"),
        ("gh pr merge 1 --subject " + MARK, "gh_search_text"),
    ],
)
def test_writes_the_lp_allowlist_admits_are_still_denied_overall(cmd: str, want: str) -> None:
    # Writes under the L-p definition, and allowed by L-p (a_prime_inline) — yet the whole result
    # is deny (msg-9821 §4: "書き込み定義の側で先に判定される場合も、全体の結果が deny").
    assert g.is_gh_write(cmd)
    assert g.classify(cmd) == "a_prime_inline"
    assert _deny_key(cmd) == want
    assert MARK not in (g.decide(cmd)[1] or "")


@pytest.mark.parametrize(
    "cmd",
    [
        "gh api -X POST repos/o/r/issues -f title=x",
        "gh pr merge 1 --body x",
        "gh pr close 1 --comment x",
    ],
)
def test_lp_allowed_writes_without_the_overlay_shapes_still_pass(cmd: str) -> None:
    assert g.decide(cmd) == (True, None)
