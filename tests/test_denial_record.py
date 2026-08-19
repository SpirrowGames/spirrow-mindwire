"""Tests for the allow-list denial record — spec ``spec/design/T-denial-detail-and-overdeny.md``.

The M matrix originally characterised the fs.delete over-deny case that halted
six sessions on 2026-08-11 (denial loud about the *rule*, silent about the
*act*). ``fs.delete`` was retired on 2026-08-19 (T-drop-branch-prediction-
from-allowlist §3, msg-1272 §1), so that specific characterisation is now moot
— but the *record shape* it forced into being (Layer A input-safe by
construction, Layer B redacted, single-entry per denial) is not tied to any
one operation, and every surviving test here exercises it around the sole
remaining Tier-C route: ``git.merge_to_main`` via ``gh pr merge`` / MCP
``merge_pull_request``. If a future Tier-C operation is added, the shape
survives; if the record design regresses (e.g. Layer A quotes the command), it
fails here regardless of which Tier-C rule fired.
"""

from __future__ import annotations

import pytest

from spirrow_mindwire.adapters.implementer import classify_tool_call
from spirrow_mindwire.allowlist import AllowlistDecision, ClassifiedAction, Operation
from spirrow_mindwire.denial_record import build_denial_record, layer_a, layer_b, redact


def _bash(command: str) -> ClassifiedAction:
    return classify_tool_call("Bash", {"command": command})


# --------------------------------------------------------------------------- #
# Verdict shapes still surfaced by the classifier (post-retirement)
# --------------------------------------------------------------------------- #


def test_gh_pr_merge_is_structural() -> None:
    """The name-match Tier-C route reports as ``structural`` (not raw_coarse)."""
    action = _bash("gh pr merge 5 --squash")
    assert action.operation is Operation.GIT_MERGE_TO_MAIN
    assert action.rule_id == "structural"


def test_wrapped_gh_pr_merge_still_denied() -> None:
    """T27: direct == wrapped. The record's ``rule_id`` becomes ``raw_coarse``
    when the tokenizer sees the wrapper's ``eval`` rather than the inner verb,
    but the operation is unchanged.
    """
    action = _bash('bash -c "gh pr merge 5"')
    assert action.operation is Operation.GIT_MERGE_TO_MAIN


def test_bash_command_that_only_mentions_the_verb_does_not_classify() -> None:
    """A commit message mentioning `gh pr merge` (as prose) still classifies
    to GIT_COMMIT — the structural pass looks at the command shape, not the
    contents of a `-m` value.
    """
    action = _bash('git commit -m "docs: describe gh pr merge flow"')
    assert action.operation is Operation.GIT_COMMIT


def test_a_read_grep_for_the_verb_stays_read() -> None:
    action = _bash('grep -rn "gh pr merge" scripts/')
    assert action.operation is Operation.EXEC_CODE  # grep is not a git/gh classifier route


def test_write_tool_is_path_classified_not_bash_scanned() -> None:
    """A `Write` for a file whose *content* mentions Tier-C verbs is still
    just a path-classified FS_WRITE. The Bash raw-text scan is not involved.
    """
    action = classify_tool_call(
        "Write", {"file_path": "scripts/promote.sh", "content": "gh pr merge $1"}
    )
    assert action.operation is Operation.FS_WRITE
    assert action.rule_id == "path"
    assert action.indirection_gate is False


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
    action = ClassifiedAction(
        Operation.GIT_MERGE_TO_MAIN, detail="gh pr merge 5 --token secret", match_offset=0
    )
    assert mod.layer_b(action) == {"context_window": "<redact-failed>"}


# --------------------------------------------------------------------------- #
# T2 — Layer A carries no input-derived data
# --------------------------------------------------------------------------- #


def test_t2_layer_a_carries_no_input_derived_data() -> None:
    """Every value in Layer A is an identity, a boolean, or a count/offset —
    so no substring of the command can appear. Adding a field that quotes the
    input would fail here.
    """
    marker = "SUPERSECRETMARKER"
    action = _bash(f"gh pr merge 5 # {marker}")
    decision = AllowlistDecision(False, Operation.GIT_MERGE_TO_MAIN, "forbidden")
    record = layer_a(decision, action)
    for key, value in record.items():
        assert not isinstance(value, str) or marker not in value, key
        assert not isinstance(value, str) or action.detail not in value, key


def test_t2_layer_b_is_bounded_and_does_not_carry_the_whole_command() -> None:
    long_tail = "x" * 5000
    action = _bash(f"gh pr merge 5 # {long_tail}")
    window = layer_b(action).get("context_window", "")
    assert len(window) < 500
    assert long_tail not in window


# --------------------------------------------------------------------------- #
# T4 — record shape / message compatibility
# --------------------------------------------------------------------------- #


def test_t4_record_answers_ac1_in_a_single_entry() -> None:
    """AC1: one record says which verdict fired and where the match was."""
    action = _bash("gh pr merge 5 --squash")
    record = build_denial_record(
        AllowlistDecision(False, Operation.GIT_MERGE_TO_MAIN, "main への merge は Tier C"),
        action,
    )
    assert record["rule_id"] == "structural"
    assert record["operation"] == "git.merge_to_main"
    assert record["indirection_gate"] is False


def test_t4_structural_denial_reports_no_match_window() -> None:
    action = _bash("gh pr merge 5")
    record = build_denial_record(AllowlistDecision(False, Operation.GIT_MERGE_TO_MAIN, "r"), action)
    assert record["rule_id"] == "structural"
    assert record["match_offset"] == -1
    assert record["context_window"] == "<no-match>"


def test_t4_denial_error_message_is_unchanged() -> None:
    """S1: the sink has structured fields, so the message string must not change."""
    from spirrow_mindwire.adapters.implementer import _violation

    decision = AllowlistDecision(
        False,
        Operation.GIT_MERGE_TO_MAIN,
        "main への merge は Tier C (Takahito 事前承認)。loop からは実行不可。",
    )
    err = _violation(decision, _bash("gh pr merge 5"))
    assert str(err).startswith("allow-list denied git.merge_to_main: ")
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

    ts = datetime(2026, 8, 20, tzinfo=UTC)
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
    err = _violation(
        AllowlistDecision(False, Operation.GIT_MERGE_TO_MAIN, "r"),
        _bash("gh pr merge 5"),
    )
    event = delivery_failed_event(handle, chat_event, err)
    assert event.fields[EVENT_FIELD_DENIAL]["operation"] == "git.merge_to_main"
