"""Tests for the allow-list denial record — spec ``spec/design/T-denial-detail-and-overdeny.md``.

The M-matrix below is a **characterization** test: the expectations record what the
classifier does *today*, not what it ought to do. That is deliberate. The open
question this record exists to settle is whether the coarse floor over-denies — and
answering it by first changing the classifier would destroy the evidence. So PR-1
pins current behaviour (AC2: not one verdict changes) and makes the provenance
visible; any change to these values is a separate, deliberate decision.
"""

from __future__ import annotations

import pytest

from spirrow_mindwire.adapters.implementer import classify_tool_call
from spirrow_mindwire.allowlist import AllowlistDecision, ClassifiedAction, Operation
from spirrow_mindwire.denial_record import build_denial_record, layer_a, layer_b, redact

# The exact body that was being written when six sessions halted on 2026-08-11:
# PowerShell test cleanup. It contains `Remove-Item` (a _RAW_COARSE keyword) *and*
# `$(` (which opens the _INDIRECTION_RE gate), which is what makes M1 the crux.
_PS_BODY = """\
$parseErrors | ForEach-Object { Write-Host "PARSE ERROR line $($_.Extent.StartLineNumber)" }
try { Check "fresh (0h) -> quarantined" 'quarantined' }
finally { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force } }
"""

_HEREDOC_WRITE = f"cat <<'EOF' > tests/Test-SweepQuarantine.ps1\n{_PS_BODY}EOF"


def _bash(command: str) -> ClassifiedAction:
    return classify_tool_call("Bash", {"command": command})


# --------------------------------------------------------------------------- #
# M — characterization matrix (T3)
# --------------------------------------------------------------------------- #


def test_m1_heredoc_writing_powershell_with_remove_item() -> None:
    """M1 — the crux. Writing a file whose *body* mentions deletion.

    Nothing is deleted here: the command writes a file. Whether it is denied, and by
    which rule, is precisely the two-branch question the 2026-08-11 halts could not
    answer because no record said what had been attempted.
    """
    action = _bash(_HEREDOC_WRITE)
    assert action.operation is Operation.FS_DELETE
    assert action.rule_id == "raw_coarse"
    assert action.indirection_gate is True
    assert action.match_offset >= 0
    # The structural pass saw a file write, not a delete — so the floor stands alone.
    assert action.corroborated == "no"


def test_m2_same_body_through_the_write_tool() -> None:
    """M2 — identical content, different tool. The Bash text scan is not involved."""
    action = classify_tool_call(
        "Write", {"file_path": "tests/Test-SweepQuarantine.ps1", "content": _PS_BODY}
    )
    assert action.operation is Operation.FS_WRITE
    assert action.rule_id == "path"
    assert action.indirection_gate is False


def test_m3_read_only_grep_for_the_keyword() -> None:
    """M3 — a read-only search whose *pattern* is a Tier C verb."""
    action = _bash('grep -rn "Remove-Item" tests/')
    assert action.operation is not Operation.FS_DELETE
    assert action.rule_id == "structural"
    assert action.indirection_gate is False


def test_m4_commit_message_mentioning_rm() -> None:
    """M4 — the verb appears only inside a commit message."""
    action = _bash('git commit -m "rm dead code"')
    assert action.operation is not Operation.FS_DELETE
    assert action.rule_id == "structural"


def test_m5_real_delete_is_still_denied() -> None:
    """M5 — under-deny regression guard. A real delete stays a delete."""
    action = _bash("rm -rf build/")
    assert action.operation is Operation.FS_DELETE


def test_m6_heredoc_documenting_force_push() -> None:
    """M6 — same shape as M1 for a different Tier C verb."""
    action = _bash("cat <<'EOF' > doc.md\nrun $(true); git push --force origin main\nEOF")
    assert action.rule_id == "raw_coarse"
    assert action.operation is Operation.FORCE_PUSH


def test_m7_heredoc_documenting_history_rewrite() -> None:
    """M7 — a runbook body mentioning rebase / reset --hard."""
    action = _bash("cat <<'EOF' > runbook.md\nstep $(1): git reset --hard HEAD~1\nEOF")
    assert action.rule_id == "raw_coarse"
    assert action.operation is Operation.HISTORY_REWRITE


def test_m8_wrapped_real_delete_is_still_denied() -> None:
    """M8 — the floor's reason for existing: a delete smuggled through ``bash -c``."""
    action = _bash('bash -c "rm -rf /"')
    assert action.operation is Operation.FS_DELETE


def test_m9_heredoc_piped_into_a_shell_is_still_denied() -> None:
    """M9 — heredoc that is *executed*, not written to a file."""
    action = _bash("cat <<'EOF' | bash\nrm -rf /tmp/x\nEOF")
    assert action.operation is Operation.FS_DELETE


# --------------------------------------------------------------------------- #
# T1 — redaction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_abcdefghijklmnopqrstuvwxyz0123",
        "github_pat_11ABCDEFG0abcdefghijklmnop",
        "xoxb-123456789012-abcdefghijkl",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    ],
)
def test_t1_known_token_shapes_do_not_survive_redaction(secret: str) -> None:
    assert secret not in redact(f"curl -H 'Authorization: Bearer {secret}' https://x/y")


def test_t1_labelled_values_and_url_userinfo_are_masked() -> None:
    assert "hunter2" not in redact("gh auth login --token hunter2")
    assert "s3cr3t" not in redact("git clone https://user:s3cr3t@example.com/r.git")
    assert "abc123XYZ" not in redact("export DEPLOY_SECRET=abc123XYZ")


def test_t1_redaction_failure_drops_the_window_rather_than_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed: if redaction raises, layer B must not fall back to raw text."""
    import spirrow_mindwire.denial_record as mod

    monkeypatch.setattr(mod, "redact", lambda _text: (_ for _ in ()).throw(RuntimeError("boom")))
    action = ClassifiedAction(Operation.FS_DELETE, detail="rm -rf secret_dir", match_offset=0)
    assert mod.layer_b(action) == {"context_window": "<redact-failed>"}


# --------------------------------------------------------------------------- #
# T2 — layer A carries no input-derived data
# --------------------------------------------------------------------------- #


def test_t2_layer_a_carries_no_input_derived_data() -> None:
    """Layer A must be safe by construction, not by redaction.

    Every value is an identity, a boolean or a count — so no substring of the command
    (and therefore no secret inside it) can appear. This test is the enforcement:
    adding a field that quotes the input fails here.
    """
    marker = "SUPERSECRETMARKER"
    action = _bash(f"cat <<'EOF' > f.md\n{marker} $(x) Remove-Item\nEOF")
    decision = AllowlistDecision(False, Operation.FS_DELETE, "forbidden")
    record = layer_a(decision, action)
    for key, value in record.items():
        assert not isinstance(value, str) or marker not in value, key
        assert not isinstance(value, str) or action.detail not in value, key


def test_t2_layer_b_is_bounded_and_does_not_carry_the_whole_command() -> None:
    long_tail = "x" * 5000
    action = _bash(f"cat <<'EOF' > f.md\n$(x) Remove-Item {long_tail}\nEOF")
    window = layer_b(action)["context_window"]
    assert len(window) < 500
    assert long_tail not in window


# --------------------------------------------------------------------------- #
# T4 — record shape / message compatibility
# --------------------------------------------------------------------------- #


def test_t4_record_answers_ac1_in_a_single_entry() -> None:
    """AC1: one record says which verdict fired and whether the match was in a heredoc."""
    action = _bash(_HEREDOC_WRITE)
    record = build_denial_record(
        AllowlistDecision(False, Operation.FS_DELETE, "ファイル削除は Tier C"), action
    )
    assert record["rule_id"] == "raw_coarse"
    assert record["has_heredoc"] is True
    assert record["indirection_gate"] is True
    assert record["match_line"] > 1  # the match is inside the body, not on the command line
    assert record["operation"] == "fs.delete"


def test_t4_structural_denial_reports_no_match_window() -> None:
    action = _bash("rm -rf build/")
    record = build_denial_record(AllowlistDecision(False, Operation.FS_DELETE, "r"), action)
    assert record["rule_id"] == "structural"
    assert record["match_offset"] == -1
    assert record["context_window"] == "<no-match>"


def test_t4_denial_error_message_is_unchanged() -> None:
    """S1: the sink has structured fields, so the message string must not change."""
    from spirrow_mindwire.adapters.implementer import _violation

    decision = AllowlistDecision(False, Operation.FS_DELETE, "ファイル削除は Tier C (不可逆)。")
    err = _violation(decision, _bash("rm -rf build/"))
    assert str(err) == "allow-list denied fs.delete: ファイル削除は Tier C (不可逆)。"
    assert err.denial_record is not None


def test_t4_delivery_failed_event_carries_the_record() -> None:
    from datetime import UTC, datetime

    from spirrow_mindwire.adapters.implementer import _violation
    from spirrow_mindwire.dispatcher.event_log import EVENT_FIELD_DENIAL, delivery_failed_event
    from spirrow_mindwire.value_objects import (
        ChatroomEvent,
        EventType,
        NewMessagePayload,
        Role,
        SessionHandle,
        ThreadRef,
    )

    ts = datetime(2026, 8, 12, tzinfo=UTC)
    thread_ref = ThreadRef(
        chatroom_uri="mcp://local", project_id="spirrow-mindwire", thread_id="T-x"
    )
    handle = SessionHandle(
        session_id="01JSESSION",
        instance_id="implementer-1",
        adapter_id="a1",
        thread_ref=thread_ref,
        role=Role.IMPLEMENTER,
        started_at=ts,
    )
    chat_event = ChatroomEvent(
        event_id="T-x:msg-1",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=thread_ref,
        occurred_at=ts,
        payload=NewMessagePayload(msg_id="msg-1", author="human", body="b", parent_msg_id=None),
    )
    err = _violation(AllowlistDecision(False, Operation.FS_DELETE, "r"), _bash(_HEREDOC_WRITE))
    event = delivery_failed_event(handle, chat_event, err)
    assert event.fields[EVENT_FIELD_DENIAL]["rule_id"] == "raw_coarse"
