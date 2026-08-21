"""Value objects for the decision-request composer (T-decision-request-composer).

Plain ``@dataclass(frozen=True)`` in the style of
:mod:`spirrow_mindwire.value_objects` — no on-disk representation on these
types themselves. Serialization to and from ``pending-decisions.json`` is
concentrated on :class:`DecisionRequestEnvelope`.

Design intent (msg-1370 §2 / msg-1384 §1 D-22):

- The **input** carries only what the composer needs to phrase the
  question: the where (project / thread / last_msg), the why (stop
  reason / rounds), and a **bounded** tail of the thread + its title.
  It never carries the whole thread (I-4: measured 180,926 chars on
  the longest live thread).
- The **output** is the structured question: one line, the options with
  their trade-offs, the composer's recommendation (with a reason so it
  is inspectable, not authoritative — I-1), and things the composer
  could not confirm. Rendering (Discord body, digest line, dashboard
  cards) lives outside these types.
- The **envelope** is the on-disk row. It carries the ``composer_status``
  so the dashboard can degrade visibly (D-22 / D-28: "black-box degrade
  is worse than a visible one"), and the ``signature`` so the wrapper
  can dedup (I-3: one composer call per ``reason:last_msg``, not one
  per five-minute tick).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

# --------------------------------------------------------------------------- #
# Tail message (a trimmed-down mirror of ThreadContextMessage)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ThreadTailMessage:
    """One prior message the composer is allowed to see.

    Deliberately trimmed to the three fields the composer needs (id / author
    / body) — a full mirror of
    :class:`spirrow_mindwire.value_objects.ThreadContextMessage` would drag
    in the dispatcher's whole context model on a wrapper-facing surface, and
    the composer does not need it.
    """

    msg_id: str
    author: str
    body: str


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DecisionRequestInput:
    """Everything a composer is allowed to see for one stop.

    The bounded-tail rule (I-4) is enforced by construction: ``tail`` is a
    tuple whose length the caller (the CLI, in production) has already
    trimmed to a sweep-wide bound. ``tail_requested`` records **what bound
    was asked for** and ``total_messages`` records the true thread length;
    the difference is how many messages were omitted, and it is written to
    the envelope so a diagnostic can tell "the composer had 5 of 45" from
    "the composer had 5 out of 5" (F-1: "did N=5 suffice, or was the
    question inadequate because there was more the composer never saw?").
    """

    project: str
    thread_id: str
    last_msg_id: str
    stop_reason: str
    rounds: int
    thread_title: str
    tail: tuple[ThreadTailMessage, ...]
    tail_requested: int
    total_messages: int

    def __post_init__(self) -> None:
        # Guardrails against a caller silently expanding the surface. A
        # zero-length tail is legal (a brand-new thread parked on its
        # opener), but a negative bound is a programming error and a
        # ``tail_requested`` smaller than the actual tail is a lie.
        if self.rounds < 0:
            raise ValueError(f"rounds must be >= 0, got {self.rounds}")
        if self.tail_requested < 0:
            raise ValueError(f"tail_requested must be >= 0, got {self.tail_requested}")
        if self.total_messages < 0:
            raise ValueError(f"total_messages must be >= 0, got {self.total_messages}")
        if len(self.tail) > self.tail_requested:
            raise ValueError(
                f"tail is longer ({len(self.tail)}) than tail_requested "
                f"({self.tail_requested}); the caller violated its own bound"
            )
        if len(self.tail) > self.total_messages:
            raise ValueError(
                f"tail is longer ({len(self.tail)}) than total_messages "
                f"({self.total_messages}); the count is a lie"
            )

    @property
    def omitted_count(self) -> int:
        """How many messages the composer did not see. Reported in the envelope."""
        return max(0, self.total_messages - len(self.tail))


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


_OPTION_ID_RE = re.compile(r"^[A-Z]$")


@dataclass(frozen=True)
class DecisionOption:
    """One row of the human-facing options list.

    ``id`` is a single uppercase letter (``A``, ``B``, ...). This is the
    label the dashboard puts on its button (D-22: "ボタンに載せる
    ``label`` は ``pending-decisions.json`` の ``options`` から取る") and
    the letter the Discord message shows the operator. Enforcing the
    shape here means a broken composer cannot smuggle an option id that
    the UI would then reject.

    ``label`` is a one-line label. ``gain`` / ``loss`` are single-line
    trade-off descriptions — the msg-1370 §2 "得るもの / 失うもの"
    fields. They are strings because the UI treats them as text, not
    structured data; a composer that wants to leave one empty can (an
    empty ``loss`` means "no meaningful downside was identified", which
    is different from an unset field).
    """

    id: str
    label: str
    gain: str
    loss: str

    def __post_init__(self) -> None:
        if not _OPTION_ID_RE.match(self.id):
            raise ValueError(
                f"option id must be a single uppercase letter (A, B, ...); got {self.id!r}"
            )
        if not self.label.strip():
            raise ValueError(f"option {self.id}: label must be non-empty")


@dataclass(frozen=True)
class DecisionRequestOutput:
    """The composer's structured answer.

    ``question`` is a one-line prompt. ``options`` may be empty (the
    composer legitimately could not slice the decision; that state is
    recorded, not hidden). ``recommendation`` names an option id or is
    ``None``; a recommendation that names an id absent from ``options``
    is a shape error (caught in ``__post_init__``).

    ``recommendation_reason`` accompanies a non-null recommendation.
    Displaying the reason keeps I-1 in force at the UI level: the operator
    reads *why* it is the recommendation, they do not read "the system
    chose A". The recommendation is not an authorized decision, and the
    dashboard does NOT pre-select it — it is a suggestion, presented in
    the same shape as every other option.
    """

    question: str
    options: tuple[DecisionOption, ...]
    recommendation: str | None
    recommendation_reason: str | None
    unknowns: tuple[str, ...]
    tail_used: int
    """How many tail messages the composer actually consulted.

    Written to the envelope so a diagnostic can distinguish "the composer
    was given 5 messages and used all 5" from "was given 5 and used only
    the last 2" — the F-1 report ("what wasn't enough?") depends on this.
    Callers set this to ``len(input.tail)`` when they had no finer
    reading, which is honest under-counting rather than an invented
    number.
    """

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError("question must be non-empty")
        if self.tail_used < 0:
            raise ValueError(f"tail_used must be >= 0, got {self.tail_used}")

        # Option ids must be unique — a duplicate would collide on the
        # dashboard's button state and on the wrapper's rendered body.
        seen: set[str] = set()
        for opt in self.options:
            if opt.id in seen:
                raise ValueError(f"duplicate option id {opt.id!r}")
            seen.add(opt.id)

        if self.recommendation is not None:
            if self.recommendation not in seen:
                raise ValueError(
                    f"recommendation {self.recommendation!r} is not one of the option ids "
                    f"({sorted(seen)})"
                )
            if not self.recommendation_reason or not self.recommendation_reason.strip():
                raise ValueError(
                    "recommendation_reason must be non-empty when recommendation is set"
                )
        else:
            if self.recommendation_reason:
                raise ValueError(
                    "recommendation_reason is set but recommendation is None; "
                    "the reason belongs to the recommendation"
                )


# --------------------------------------------------------------------------- #
# Envelope — what lands in pending-decisions.json
# --------------------------------------------------------------------------- #


class ComposerStatus(StrEnum):
    """Outcome of one composer call.

    Recorded verbatim in the envelope so the dashboard and the wrapper
    can degrade visibly (D-22 / D-28). ``ok`` is the only status that
    carries a populated ``output``; the others carry an ``error`` string
    and a ``None`` output.
    """

    OK = "ok"
    TIMEOUT = "timeout"
    ERROR = "error"
    EMPTY = "empty"


@dataclass(frozen=True)
class DecisionRequestEnvelope:
    """The JSON row a wrapper stores in ``pending-decisions.json``.

    Keyed in the cache by ``project/thread_id``. The wrapper dedups on
    :attr:`signature` (I-3: one composer call per ``reason:last_msg``,
    not per tick) and the dashboard reads :attr:`output` (or, on a
    non-``ok`` status, the ``error`` string) to render the decision UI.

    Serialization is centralised on :meth:`to_json` / :meth:`from_json`
    so the on-disk shape has exactly one source of truth. Both are
    lossless round-trips of the value-object fields.
    """

    project: str
    thread_id: str
    signature: str
    composed_at: str
    composer_status: ComposerStatus
    identity_used: str
    last_msg_id: str
    stop_reason: str
    rounds: int
    tail_requested: int
    tail_used: int
    omitted_count: int
    output: DecisionRequestOutput | None
    error: str | None = None
    extras: dict[str, str] = field(default_factory=dict)
    """Free-form key/value pairs a backend may attach (e.g. model id, latency in ms).

    Kept as ``dict[str, str]`` so the on-disk shape stays boringly JSON;
    a backend that wants richer telemetry belongs behind its own log,
    not here.
    """

    def __post_init__(self) -> None:
        if self.composer_status is ComposerStatus.OK and self.output is None:
            raise ValueError("composer_status=ok requires a populated output")
        if self.composer_status is not ComposerStatus.OK and self.output is not None:
            raise ValueError(
                f"composer_status={self.composer_status.value} must not carry an output "
                "(the output field is reserved for the ok status)"
            )
        if self.composer_status is not ComposerStatus.OK and not self.error:
            raise ValueError(
                f"composer_status={self.composer_status.value} requires a non-empty error string"
            )
        if self.tail_used > self.tail_requested:
            raise ValueError(
                f"tail_used ({self.tail_used}) > tail_requested ({self.tail_requested})"
            )
        if self.omitted_count < 0:
            raise ValueError(f"omitted_count must be >= 0, got {self.omitted_count}")

    # ----- serialization -------------------------------------------------- #

    def to_json(self) -> dict[str, object]:
        """Serialize to a JSON-safe dict.

        The shape is stable; callers (the PS wrapper, the dashboard) read
        it. Fields present here are the fields ``pending-decisions.json``
        will always carry.
        """
        row: dict[str, object] = {
            "project": self.project,
            "thread_id": self.thread_id,
            "signature": self.signature,
            "composed_at": self.composed_at,
            "composer_status": self.composer_status.value,
            "identity_used": self.identity_used,
            "last_msg_id": self.last_msg_id,
            "stop_reason": self.stop_reason,
            "rounds": self.rounds,
            "tail_requested": self.tail_requested,
            "tail_used": self.tail_used,
            "omitted_count": self.omitted_count,
            "error": self.error,
            "extras": dict(self.extras),
        }
        if self.output is not None:
            row["output"] = {
                "question": self.output.question,
                "options": [
                    {"id": o.id, "label": o.label, "gain": o.gain, "loss": o.loss}
                    for o in self.output.options
                ],
                "recommendation": self.output.recommendation,
                "recommendation_reason": self.output.recommendation_reason,
                "unknowns": list(self.output.unknowns),
                "tail_used": self.output.tail_used,
            }
        else:
            row["output"] = None
        return row

    @classmethod
    def from_json(cls, row: dict[str, object]) -> DecisionRequestEnvelope:
        """Parse a dict produced by :meth:`to_json`.

        Any missing required field raises :class:`KeyError`; a shape
        error raises :class:`ValueError` through the value-object
        validators. Callers should treat both as "the on-disk cache is
        corrupt" and fall back to composing afresh — the sweep's
        state-file convention (:func:`Get-JsonState` in the wrapper).
        """
        output_row = row.get("output")
        output: DecisionRequestOutput | None
        if output_row is None:
            output = None
        else:
            output_dict = _as_dict(output_row, "output")
            options = tuple(
                DecisionOption(
                    id=str(_as_dict(o, "options[]")["id"]),
                    label=str(_as_dict(o, "options[]")["label"]),
                    gain=str(_as_dict(o, "options[]")["gain"]),
                    loss=str(_as_dict(o, "options[]")["loss"]),
                )
                for o in _as_list(output_dict.get("options", []))
            )
            recommendation = output_dict.get("recommendation")
            recommendation_reason = output_dict.get("recommendation_reason")
            output = DecisionRequestOutput(
                question=str(output_dict["question"]),
                options=options,
                recommendation=None if recommendation is None else str(recommendation),
                recommendation_reason=(
                    None if recommendation_reason is None else str(recommendation_reason)
                ),
                unknowns=tuple(str(u) for u in _as_list(output_dict.get("unknowns", []))),
                tail_used=_as_int(output_dict.get("tail_used", 0), "output.tail_used"),
            )
        extras_raw = row.get("extras") or {}
        extras_dict = _as_dict(extras_raw, "extras")
        return cls(
            project=str(row["project"]),
            thread_id=str(row["thread_id"]),
            signature=str(row["signature"]),
            composed_at=str(row["composed_at"]),
            composer_status=ComposerStatus(str(row["composer_status"])),
            identity_used=str(row["identity_used"]),
            last_msg_id=str(row["last_msg_id"]),
            stop_reason=str(row["stop_reason"]),
            rounds=_as_int(row["rounds"], "rounds"),
            tail_requested=_as_int(row["tail_requested"], "tail_requested"),
            tail_used=_as_int(row["tail_used"], "tail_used"),
            omitted_count=_as_int(row["omitted_count"], "omitted_count"),
            output=output,
            error=None if row.get("error") is None else str(row["error"]),
            extras={str(k): str(v) for k, v in extras_dict.items()},
        )


def _as_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"expected list, got {type(value).__name__}")
    return value


def _as_dict(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a dict, got {type(value).__name__}")
    return value


def _as_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {type(value).__name__}")
    return value


__all__ = [
    "ComposerStatus",
    "DecisionOption",
    "DecisionRequestEnvelope",
    "DecisionRequestInput",
    "DecisionRequestOutput",
    "ThreadTailMessage",
]
