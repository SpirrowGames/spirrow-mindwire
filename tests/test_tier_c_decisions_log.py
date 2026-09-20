"""Tests for :mod:`spirrow_mindwire.tier_c_decisions_log`.

Two things this suite pins:

1. The JSONL row shape — every kind serialises with ``kind`` at the top
   level plus the payload flattened one level, so ``jq '.kind'`` and
   ``jq '.reason'`` both work. This is the on-disk contract the
   ``/decisions`` dashboard reads; any drift would silently break
   aggregation.

2. The RETRY store behaviour — a BOUNCED row makes an UUID unresolved
   for its author; a subsequent RETRY_ADMIT with the same UUID resolves
   it. This is the rule the admission gate depends on to prevent
   infinite retry loops (msg-3648 v1 §2).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spirrow_mindwire.tier_c_admission_gate import (
    LogEntry,
    LogKind,
    decide_admission,
)
from spirrow_mindwire.tier_c_decisions_log import (
    append_log_entries,
    append_log_entry,
    build_retry_lookup,
)

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


def _read_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


class TestLogEntryImmutability:
    """Structural pin for PR-gate objection #322-9.

    ``@dataclass(frozen=True)`` on its own only protects field
    reassignment (``entry.payload = ...``). The payload dict itself
    is still mutable through ``entry.payload[key] = value``, which
    silently violates the freeze promise. ``__post_init__`` wraps
    the payload in a :class:`types.MappingProxyType` so mutation
    through the dict interface raises :class:`TypeError`.
    """

    def test_payload_mutation_raises(self) -> None:
        entry = LogEntry(
            kind=LogKind.BOUNCED,
            payload={"ts": NOW.isoformat(), "author": "alice"},
        )
        with pytest.raises(TypeError):
            entry.payload["author"] = "eve"  # type: ignore[index]

    def test_payload_read_still_works(self) -> None:
        entry = LogEntry(
            kind=LogKind.BOUNCED,
            payload={"ts": NOW.isoformat(), "author": "alice"},
        )
        assert entry.payload["author"] == "alice"

    def test_payload_source_mutation_does_not_leak(self) -> None:
        """The wrapper is over a defensive COPY of the caller's dict,
        so mutating the source dict after construction does not sneak
        past the freeze either.
        """
        source = {"ts": NOW.isoformat(), "author": "alice"}
        entry = LogEntry(kind=LogKind.BOUNCED, payload=source)
        source["author"] = "eve"
        assert entry.payload["author"] == "alice"


class TestAppend:
    def test_row_shape_is_flat(self, tmp_path: Path) -> None:
        entry = LogEntry(
            kind=LogKind.BOUNCED,
            payload={"ts": NOW.isoformat(), "author": "alice", "reason": "no-label"},
        )
        log = tmp_path / "decisions.jsonl"
        append_log_entry(log, entry, thread="T-x", msg_id="msg-1")
        rows = _read_rows(log)
        assert rows == [
            {
                "author": "alice",
                "kind": "BOUNCED",
                "msg_id": "msg-1",
                "reason": "no-label",
                "thread": "T-x",
                "ts": NOW.isoformat(),
            }
        ]

    def test_thread_and_msg_id_optional(self, tmp_path: Path) -> None:
        entry = LogEntry(
            kind=LogKind.ADMIT_UNSURE,
            payload={"ts": NOW.isoformat(), "author": "alice", "label": "unsure:goal?"},
        )
        log = tmp_path / "decisions.jsonl"
        append_log_entry(log, entry)
        row = _read_rows(log)[0]
        assert "thread" not in row
        assert "msg_id" not in row

    def test_append_creates_parent_dirs(self, tmp_path: Path) -> None:
        entry = LogEntry(
            kind=LogKind.BOUNCED,
            payload={"ts": NOW.isoformat(), "author": "alice"},
        )
        log = tmp_path / "state" / "logs" / "decisions.jsonl"
        append_log_entry(log, entry)
        assert log.exists()

    def test_append_entries_writes_all_in_order(self, tmp_path: Path) -> None:
        entries = [
            LogEntry(
                kind=LogKind.LABEL_MIGRATION,
                payload={"ts": NOW.isoformat(), "author": "alice", "from": "scope", "to": "goal"},
            ),
            LogEntry(
                kind=LogKind.RETRY_ADMIT,
                payload={"ts": NOW.isoformat(), "author": "alice", "retry_uuid": "u1"},
            ),
        ]
        log = tmp_path / "decisions.jsonl"
        append_log_entries(log, entries)
        kinds = [row["kind"] for row in _read_rows(log)]
        assert kinds == ["LABEL_MIGRATION", "RETRY_ADMIT"]


class TestRetryLookup:
    def test_empty_log_returns_false(self, tmp_path: Path) -> None:
        lookup = build_retry_lookup(tmp_path / "decisions.jsonl")
        assert lookup("any-uuid", "alice") is False

    def test_bounced_uuid_matches_same_author(self, tmp_path: Path) -> None:
        log = tmp_path / "decisions.jsonl"
        append_log_entry(
            log,
            LogEntry(
                kind=LogKind.BOUNCED,
                payload={
                    "ts": NOW.isoformat(),
                    "author": "alice",
                    "retry_uuid": "u1",
                    "reason": "no-label",
                },
            ),
        )
        lookup = build_retry_lookup(log)
        assert lookup("u1", "alice") is True
        # Wrong author does not match — bounces are scoped to their author.
        assert lookup("u1", "bob") is False

    def test_retry_admit_resolves_the_bounce(self, tmp_path: Path) -> None:
        log = tmp_path / "decisions.jsonl"
        append_log_entries(
            log,
            [
                LogEntry(
                    kind=LogKind.BOUNCED,
                    payload={
                        "ts": NOW.isoformat(),
                        "author": "alice",
                        "retry_uuid": "u1",
                        "reason": "no-label",
                    },
                ),
                LogEntry(
                    kind=LogKind.RETRY_ADMIT,
                    payload={
                        "ts": NOW.isoformat(),
                        "author": "alice",
                        "retry_uuid": "u1",
                        "reason": "label_corrected",
                    },
                ),
            ],
        )
        lookup = build_retry_lookup(log)
        assert lookup("u1", "alice") is False

    def test_unrelated_uuid_does_not_match(self, tmp_path: Path) -> None:
        log = tmp_path / "decisions.jsonl"
        append_log_entry(
            log,
            LogEntry(
                kind=LogKind.BOUNCED,
                payload={
                    "ts": NOW.isoformat(),
                    "author": "alice",
                    "retry_uuid": "u1",
                    "reason": "no-label",
                },
            ),
        )
        lookup = build_retry_lookup(log)
        assert lookup("u-other", "alice") is False

    def test_reused_uuid_after_admit_reopens_state(self, tmp_path: Path) -> None:
        """Regression pin for PR-gate objection #322-1 (BLOCKING).

        A UUID may be reused across independent bounce/admit cycles
        (short-token spaces collide; sequence counters wrap). The
        lookup must reflect the LAST observation, not the first
        ``RETRY_ADMIT`` it stumbles onto. Before the fix, forward
        iteration returned ``False`` early on the resolved cycle and
        never reached the fresh ``BOUNCED`` — trapping the author in
        an infinite loop.
        """
        log = tmp_path / "decisions.jsonl"
        append_log_entries(
            log,
            [
                # First cycle: bounce, then admit — resolved.
                LogEntry(
                    kind=LogKind.BOUNCED,
                    payload={
                        "ts": NOW.isoformat(),
                        "author": "alice",
                        "retry_uuid": "u-reused",
                        "reason": "no-label",
                    },
                ),
                LogEntry(
                    kind=LogKind.RETRY_ADMIT,
                    payload={
                        "ts": NOW.isoformat(),
                        "author": "alice",
                        "retry_uuid": "u-reused",
                        "reason": "label_corrected",
                    },
                ),
                # Second cycle: same UUID reused for a fresh bounce.
                LogEntry(
                    kind=LogKind.BOUNCED,
                    payload={
                        "ts": NOW.isoformat(),
                        "author": "alice",
                        "retry_uuid": "u-reused",
                        "reason": "unknown-label",
                    },
                ),
            ],
        )
        # The LAST event for this UUID is a fresh BOUNCED ∴ unresolved.
        assert build_retry_lookup(log)("u-reused", "alice") is True

        # And a second RETRY_ADMIT closes the second cycle again.
        append_log_entry(
            log,
            LogEntry(
                kind=LogKind.RETRY_ADMIT,
                payload={
                    "ts": NOW.isoformat(),
                    "author": "alice",
                    "retry_uuid": "u-reused",
                    "reason": "label_corrected",
                },
            ),
        )
        assert build_retry_lookup(log)("u-reused", "alice") is False

    def test_cross_author_uuid_collision_is_scoped(self, tmp_path: Path) -> None:
        """Regression pin for PR-gate objection #322-4 (BLOCKING).

        A reused UUID can collide across authors (e.g. two authors
        each holding a small integer counter). ``RETRY_ADMIT`` rows
        must clear ONLY the state of their own author — otherwise
        Bob's successful retry would silently resolve Alice's
        unresolved bounce and lock her out of her retry path.
        """
        log = tmp_path / "decisions.jsonl"
        append_log_entries(
            log,
            [
                # Alice bounces on UUID "u1".
                LogEntry(
                    kind=LogKind.BOUNCED,
                    payload={
                        "ts": NOW.isoformat(),
                        "author": "alice",
                        "retry_uuid": "u1",
                        "reason": "no-label",
                    },
                ),
                # Bob independently bounces on the SAME UUID "u1".
                LogEntry(
                    kind=LogKind.BOUNCED,
                    payload={
                        "ts": NOW.isoformat(),
                        "author": "bob",
                        "retry_uuid": "u1",
                        "reason": "no-label",
                    },
                ),
                # Bob retries successfully — this must NOT clear Alice.
                LogEntry(
                    kind=LogKind.RETRY_ADMIT,
                    payload={
                        "ts": NOW.isoformat(),
                        "author": "bob",
                        "retry_uuid": "u1",
                        "reason": "label_corrected",
                    },
                ),
            ],
        )
        lookup = build_retry_lookup(log)
        # Alice's bounce is still unresolved.
        assert lookup("u1", "alice") is True
        # Bob's is resolved.
        assert lookup("u1", "bob") is False


class TestMalformedRowsAreSkipped:
    """Regression pins for PR-gate objection #322-7 (BLOCKING untested).

    The log-scanner's docstring promises malformed rows will be
    skipped rather than raised on, so an external touch to the file
    (rotation race, half-written line, a value that isn't a JSON
    object) can never break the RETRY store. That resilience was
    coded and documented but not exercised — these tests pin the
    three flavours of malformed content the scanner must survive.
    """

    def test_malformed_json_line_is_skipped(self, tmp_path: Path) -> None:
        log = tmp_path / "decisions.jsonl"
        # Write a well-formed BOUNCED, then a syntactically-broken line,
        # then another well-formed row. The lookup must see the surrounding
        # rows without failing on the middle one.
        append_log_entry(
            log,
            LogEntry(
                kind=LogKind.BOUNCED,
                payload={
                    "ts": NOW.isoformat(),
                    "author": "alice",
                    "retry_uuid": "u1",
                    "reason": "no-label",
                },
            ),
        )
        with log.open("a", encoding="utf-8") as fh:
            fh.write('{"broken":\n')  # unterminated JSON
        assert build_retry_lookup(log)("u1", "alice") is True

    def test_non_dict_json_line_is_skipped(self, tmp_path: Path) -> None:
        """A valid JSON value that isn't a dict (list, number, string)
        must not crash the scanner — the docstring is explicit that
        ``isinstance(row, dict)`` is the acceptance condition."""
        log = tmp_path / "decisions.jsonl"
        append_log_entry(
            log,
            LogEntry(
                kind=LogKind.BOUNCED,
                payload={
                    "ts": NOW.isoformat(),
                    "author": "alice",
                    "retry_uuid": "u1",
                    "reason": "no-label",
                },
            ),
        )
        with log.open("a", encoding="utf-8") as fh:
            fh.write('["a", "list", "not", "a", "dict"]\n')
            fh.write("42\n")
            fh.write('"a bare string"\n')
        assert build_retry_lookup(log)("u1", "alice") is True

    def test_empty_lines_are_skipped(self, tmp_path: Path) -> None:
        """Blank lines (from a partial write, or a tool that appends a
        stray newline) must be skipped without incident."""
        log = tmp_path / "decisions.jsonl"
        append_log_entry(
            log,
            LogEntry(
                kind=LogKind.BOUNCED,
                payload={
                    "ts": NOW.isoformat(),
                    "author": "alice",
                    "retry_uuid": "u1",
                    "reason": "no-label",
                },
            ),
        )
        with log.open("a", encoding="utf-8") as fh:
            fh.write("\n\n   \n\n")
        assert build_retry_lookup(log)("u1", "alice") is True

    def test_malformed_row_between_two_wellformed_still_processes_both(
        self, tmp_path: Path
    ) -> None:
        """The scanner must fully cross a malformed row and continue
        processing the tail. If it silently stopped at the bad line,
        a later ``RETRY_ADMIT`` that resolves the bounce would be
        invisible to the lookup and the author would stay locked out.
        """
        log = tmp_path / "decisions.jsonl"
        append_log_entry(
            log,
            LogEntry(
                kind=LogKind.BOUNCED,
                payload={
                    "ts": NOW.isoformat(),
                    "author": "alice",
                    "retry_uuid": "u1",
                    "reason": "no-label",
                },
            ),
        )
        with log.open("a", encoding="utf-8") as fh:
            fh.write("not json at all\n")
        append_log_entry(
            log,
            LogEntry(
                kind=LogKind.RETRY_ADMIT,
                payload={
                    "ts": NOW.isoformat(),
                    "author": "alice",
                    "retry_uuid": "u1",
                    "reason": "label_corrected",
                },
            ),
        )
        # The RETRY_ADMIT sitting past the bad line still resolves the
        # bounce — bad line traversed, not fatal.
        assert build_retry_lookup(log)("u1", "alice") is False


class TestGateEndToEndAgainstLog:
    """Small integration: run the admission gate against a real JSONL
    RETRY store to confirm the two modules compose correctly.

    This is the shape the caller / infrastructure will actually use:
    * append a BOUNCED row when the gate first rejected the author.
    * on retry, build a fresh lookup from the log path and pass it in.
    * append the returned entries.
    """

    def test_bounce_then_retry_admits_on_the_second_call(self, tmp_path: Path) -> None:
        log = tmp_path / "decisions.jsonl"

        # First call: no label → BOUNCE. The transport pre-generates
        # a fresh UUID and hands it to the gate; the gate stamps it
        # into the BOUNCED payload; the transport appends the entries
        # verbatim — no unpack/re-instantiate step (fix for #322-6).
        no_lookup = build_retry_lookup(log)
        first = decide_admission(
            body="NEXT: human\n",
            author="alice",
            retry_lookup=no_lookup,
            now=NOW,
            bounce_uuid="u1",
        )
        assert first.verdict.value == "bounce"
        append_log_entries(log, first.log_entries)

        # Second call: the author sends RETRY: u1 with a corrected label.
        retry_lookup = build_retry_lookup(log)
        second = decide_admission(
            body="RETRY: u1\nTIER-C: goal\nNEXT: human\n",
            author="alice",
            retry_lookup=retry_lookup,
            now=NOW,
            bounce_uuid="u2",  # unused — the arm chosen is not a bounce
        )
        assert second.verdict.value == "admit"
        assert second.rule == "R0-RETRY:label_corrected"

        # Persist the retry admit — the uuid is now resolved.
        append_log_entries(log, second.log_entries)
        assert build_retry_lookup(log)("u1", "alice") is False
