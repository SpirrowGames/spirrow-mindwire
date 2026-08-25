"""Shared SDK ``ResultMessage`` failure-capture helper (T-sdk-is-error-loses-the-reason).

Both ``ClaudeCodeSdkAdapter`` and ``NaysayerSdkAdapter`` drain a Claude
Agent SDK stream and, on ``ResultMessage.is_error``, need to raise with a
useful reason. The pre-existing site was:

.. code-block:: python

    raise RuntimeError(getattr(final, "result", None) or "SDK session reported is_error")

which collapses two very different situations into one constant string:

* ``result`` *is* empty (SDK carried no reason on that field), and
* the code did not look anywhere else (``.errors`` / ``.subtype`` / ``.api_error_status`` /
  ``.stop_reason`` / ``.permission_denials`` were never consulted).

Downstream, all that reaches the quarantine record's ``session_log_tail`` is the
constant, so a reader cannot tell "SDK sent no reason" from "we did not look".

This module builds the ``reason_source`` axis Bohr's design (thread msg-1719 /
msg-1721) requires:

``"result"``
    ``final.result`` was non-empty. That string IS the reason.

``"field:<name>"``
    ``result`` was empty **but** another captured known field carried a value
    (e.g. ``field:errors`` when the SDK put a per-turn error list there).

``"absent"``
    Every known field was empty/None. The reason is genuinely missing on this
    ``ResultMessage``. In this branch — and ONLY here — a defensive reflection
    dump of the object is attached so the next iteration can widen the
    hand-picked list from evidence (§1-2 in msg-1721: the reflection is confined
    to the ``absent`` branch precisely because that is the one branch where
    "the object was our only source of truth and it was empty" is already
    established, so a secondary exception during introspection cannot destroy a
    reason we already have).

``"capture_failed"``
    The capture pipeline itself raised. Kept as a **separate** value from
    ``absent`` (§1-3): collapsing "we looked and found nothing" with "we could
    not look" would reproduce exactly the potshot this arc exists to remove.

The known-field list is derived from a one-time observation of the currently
installed SDK's ``claude_agent_sdk.ResultMessage`` (S-0), not from what a
future SDK version *might* carry. If the SDK grows a new reason-carrying field
the ``absent`` branch's reflection dump surfaces it in a real quarantine
record, and the list here is widened from that evidence — never speculatively.

The module also exposes ``emit_sdk_error_marker`` and ``find_sdk_error_signal``.
Structured detail reaches the ``session_log_tail`` (which is PowerShell-side —
:func:`New-QuarantineRecord` in ``deploy/run-conductor-scheduled.ps1`` writes
the record from the child process's raw stdout/stderr) via a single-line JSON
marker on stdout. The marker is emitted **twice** with identical payload:

1. immediately at the raise site (so a subsequent hard-kill still leaves it in
   the log), and
2. once more at the loop-runner's top-level failure handler, right before
   ``main`` returns, so the marker is the last thing on stdout and cannot be
   pushed out of the record's 50-line ``session_log_tail`` window by
   post-teardown noise from the SDK subprocess.

``session_id`` for correlation is deliberately reused from the SDK's own
``ResultMessage.session_id`` (Einstein's constraint at thread msg-1722): no new
UUID mechanism is introduced solely for marker de-duplication.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import sys
from typing import Any, TextIO

# Known fields on ``claude_agent_sdk.ResultMessage`` (SDK observed 2026-08-26,
# S-0). ``result`` is enumerated so the general capture pass records it in
# ``captured_fields`` alongside the others; the reason-source selection logic
# below treats it as the highest-priority reason field independently.
_KNOWN_REASON_FIELDS: tuple[str, ...] = (
    "subtype",
    "stop_reason",
    "errors",
    "api_error_status",
    "permission_denials",
    "result",
)

# Session facts to always capture on failure so the quarantine marker carries
# what the successful path already puts on ``ReplyDraft.adapter_metadata``
# (P-1c in ``naysayer_sdk._session_facts``). The failure path has been
# strictly *poorer* than the success path here, which is upside-down: this
# closes that gap (S-4). ``model`` is intentionally excluded — the reasoning is
# spelled out at the ``_session_facts`` docstring in ``naysayer_sdk.py`` and
# applies verbatim here.
_SESSION_FACT_FIELDS: tuple[str, ...] = ("session_id", "duration_ms", "num_turns")

# Hard cap on any single captured string value. Keeps the marker line, which is
# emitted to stdout and lands in ``session_log_tail``, bounded.
_FIELD_VALUE_MAX_LEN = 500

# Marker prefix. A prefix rather than a bare JSON blob keeps a plain ``grep``
# from a human reader working on the tail array.
SDK_ERROR_MARKER_PREFIX = "sdk_error_detail="

# Cap on ``absent_dump`` field names. The dump exists to widen the known-field
# list from evidence, not to be a general object dumper.
_ABSENT_DUMP_FIELDS_LIMIT = 32


class SdkIsErrorSignal(RuntimeError):  # noqa: N818 — "Signal" names its role
    """Raised in place of the pre-change constant-string ``RuntimeError``.

    The exception's ``str()`` is the human-readable ``message`` from
    :func:`capture_is_error_detail`; the full structured detail dict lives on
    :attr:`detail` so a top-level handler can re-emit the stdout marker at
    process exit (see :func:`find_sdk_error_signal` and the marker discussion
    in the module docstring).

    Subclassing :class:`RuntimeError` — not :class:`Exception` — preserves the
    pre-change caller behaviour: existing ``except RuntimeError`` (and the
    outer ``except Exception``) in the adapters keep catching it.
    """

    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(detail.get("message") or "SDK session reported is_error")


# Sentinel returned by :func:`_raw_field` when the ``getattr`` on a field
# raised. Distinct from ``None`` / ``""`` / any user value so
# :func:`_pick_reason` can distinguish "the field was empty" from "the field
# could not be read". A bare ``None`` would collapse the two into the same axis
# — the very pattern this whole thread is fixing.
_CAPTURE_ERROR_SENTINEL: Any = object()

# Small scalar containers are dumped verbatim (bounded per-element) so the
# marker line actually carries the reason a real SDK ``errors=["…"]`` put
# there, instead of the useless ``list(len=1)`` summary. Larger or non-scalar
# containers still fall back to ``type(len=N)``.
_SMALL_LIST_ELEM_LIMIT = 8


def _summarize_value(value: Any) -> Any:
    """Reduce ``value`` to a JSON-safe, length-bounded summary.

    Small lists / tuples of scalars are preserved element-wise (bounded), so a
    real ``errors=["Anthropic returned 429"]`` reaches the marker as text
    rather than as the opaque ``list(len=1)``. Larger containers, or
    containers of non-scalars, fall back to ``type(len=N)`` — the marker line
    is capped by the per-field length limit above, so an unexpectedly large
    ``permission_denials`` blob cannot balloon it.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= _FIELD_VALUE_MAX_LEN:
            return value
        return value[:_FIELD_VALUE_MAX_LEN] + f"…(+{len(value) - _FIELD_VALUE_MAX_LEN}ch)"
    if isinstance(value, (list, tuple)):
        # Preserve small scalar containers verbatim so a real ``errors=[…]``
        # message actually reaches the marker (PR #181 round 3: the earlier
        # unconditional ``type(len=N)`` summary hid the very reason text this
        # whole thread exists to preserve). Non-scalar or long lists still
        # fall back to the length-only summary — bounded is bounded.
        if len(value) <= _SMALL_LIST_ELEM_LIMIT and all(
            v is None or isinstance(v, (bool, int, float, str)) for v in value
        ):
            return [_summarize_value(v) for v in value]
        return f"{type(value).__name__}(len={len(value)})"
    if isinstance(value, (set, frozenset)):
        return f"{type(value).__name__}(len={len(value)})"
    if isinstance(value, dict):
        return f"dict(len={len(value)})"
    return f"{type(value).__name__}(repr_omitted)"


def _raw_field(final: Any, name: str) -> Any:
    """``getattr`` with per-field fail-safe.

    Returns the raw value when the read succeeds (including ``None``, empty
    string, empty container — all legitimate values callers need to
    distinguish), or :data:`_CAPTURE_ERROR_SENTINEL` when the attribute access
    itself raised. The sentinel is intentionally NOT ``None``: collapsing "the
    field was absent" onto "the read raised" is the pattern this thread is
    fixing, so it must not silently reappear inside the helper.
    """
    try:
        return getattr(final, name, None)
    except Exception:
        return _CAPTURE_ERROR_SENTINEL


def _summarize_safely(value: Any) -> Any:
    """Summarize with per-field fail-safe. Distinguishes read-failure and
    summarize-failure so a reader of ``captured_fields`` can tell them apart.
    """
    if value is _CAPTURE_ERROR_SENTINEL:
        return {"capture_failed": True}
    try:
        return _summarize_value(value)
    except Exception as exc:
        return {"summarize_failed": type(exc).__name__}


def _capture_known(final: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(raw, summary)`` for the known-field slice.

    Two dicts, same keys, same order. The RAW dict is consumed by
    :func:`_pick_reason` for emptiness tests — an empty list must remain an
    empty list at that point, not a ``"list(len=0)"`` string that
    :func:`_pick_reason` cannot recognise as empty (PR #181 round 3 defect).
    The SUMMARY dict feeds ``captured_fields`` in the marker, so it is
    length-bounded and JSON-safe.
    """
    raw: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    for name in (*_SESSION_FACT_FIELDS, *_KNOWN_REASON_FIELDS):
        value = _raw_field(final, name)
        raw[name] = value
        summary[name] = _summarize_safely(value)
    return raw, summary


def _is_empty_reason_value(value: Any) -> bool:
    """True when ``value`` carries no reason and must be skipped.

    Deliberately explicit rather than a bare ``if not value`` — ``0`` and
    ``False`` are legitimate reason-carrying values (a real
    ``api_error_status=0`` from some gateways, hypothetical boolean fields on
    a future SDK) and a bare truthiness check would eat them. The sentinel
    is treated as empty because "could not read" is not a reason we can
    report on this axis; it shows up in ``captured_fields`` as
    ``capture_failed: true`` so a reader still sees it.
    """
    if value is None or value is _CAPTURE_ERROR_SENTINEL:
        return True
    if isinstance(value, str) and not value:
        return True
    return isinstance(value, (list, tuple, set, frozenset, dict)) and not value


def _pick_reason(raw: dict[str, Any], summary: dict[str, Any]) -> tuple[str, str]:
    """Compute ``(reason_source, message)`` from RAW values, formatted from summary.

    ``result`` wins if it carries a non-empty string. Otherwise the first
    known field whose RAW value passes :func:`_is_empty_reason_value` is named
    as ``field:<name>``, using its SUMMARY for the marker message so bounds
    are enforced.

    Using summary for the emptiness check was the round-2 defect: an
    ``errors=[]`` becomes the string ``"list(len=0)"`` under summarisation,
    which is non-empty, so it was incorrectly picked as the reason —
    shadowing any trailing reason-carrying field (e.g. ``api_error_status``).
    Fixed here by branching on RAW.
    """
    result = raw.get("result")
    if isinstance(result, str) and result:
        return "result", result

    for name in _KNOWN_REASON_FIELDS:
        if name == "result":
            continue
        value = raw.get(name)
        if _is_empty_reason_value(value):
            continue
        return f"field:{name}", f"SDK is_error; {name}={summary.get(name)!r}"

    return (
        "absent",
        (
            f"SDK reported is_error; {len(_KNOWN_REASON_FIELDS)} known reason "
            "fields captured, none carried a reason"
        ),
    )


def _capture_field(final: Any, name: str) -> Any:
    """Back-compat single-field capture used by :func:`_absent_dump`.

    :func:`_absent_dump` records ``{name: summary}`` per field; the reflection
    dump is display-only, so it does not care about raw-vs-summary the way
    :func:`_pick_reason` does. Keeping this helper avoids duplicating the
    getattr-then-summarize pattern inside the dump.
    """
    return _summarize_safely(_raw_field(final, name))


def _absent_dump(final: Any) -> dict[str, Any]:
    """Reflection dump used ONLY when ``reason_source == 'absent'`` (§1-2).

    The one branch where the whole capture pipeline is *known* to have failed
    to find a reason is the one branch where a secondary exception here cannot
    destroy a reason we already had — so the reflection lives here and nowhere
    else. Records how enumeration was reached (``dataclass`` / ``vars`` / ``dir``
    / ``none``) so a reader can trace back to what the object actually was.
    """
    try:
        result: dict[str, Any] = {"type": type(final).__name__}

        names: list[str] | None = None
        introspection = "none"
        if dataclasses.is_dataclass(final):
            try:
                names = [f.name for f in dataclasses.fields(final)]
                introspection = "dataclass"
            except Exception:
                names = None
        if names is None:
            try:
                names = list(vars(final).keys())
                introspection = "vars"
            except Exception:
                names = None
        if names is None:
            try:
                names = [
                    n
                    for n in dir(final)
                    if not n.startswith("_") and not callable(getattr(final, n, None))
                ]
                introspection = "dir"
            except Exception:
                names = []
                introspection = "none"

        result["introspection"] = introspection
        # ``model`` is deliberately excluded from the dump for the same reason
        # ``naysayer_sdk._session_facts`` excludes it: the Lexora gateway echoes
        # back the tier alias, and letting that leak into any surface that
        # LOOKS like provenance manufactures exactly the overclaim
        # ADR-19 P-1c avoids.
        fields_dump: dict[str, Any] = {}
        for name in names[:_ABSENT_DUMP_FIELDS_LIMIT]:
            if name == "model":
                continue
            fields_dump[name] = _capture_field(final, name)
        if len(names) > _ABSENT_DUMP_FIELDS_LIMIT:
            fields_dump["__truncated__"] = f"{len(names) - _ABSENT_DUMP_FIELDS_LIMIT} more"
        result["fields"] = fields_dump
        return result
    except Exception as exc:
        return {
            "capture_failed": type(exc).__name__,
            "capture_error_message": _summarize_value(str(exc)),
        }


def capture_is_error_detail(final: Any) -> dict[str, Any]:
    """Build the structured detail dict for an SDK ``is_error`` failure.

    Return shape (all keys always present except ``absent_dump`` /
    ``capture_error``):

    * ``reason_source``: ``"result"`` | ``"field:<name>"`` | ``"absent"`` | ``"capture_failed"``
    * ``message``: human-readable text (also the exception's ``str()``)
    * ``captured_fields``: dict of the known-field slice (session facts +
      reason candidates), each value already length-bounded
    * ``absent_dump``: reflection dump — present ONLY when
      ``reason_source == "absent"``
    * ``capture_error`` / ``capture_error_message``: present ONLY when the
      pipeline itself raised (``capture_failed``)

    The whole function is wrapped so an entirely unexpected explosion still
    returns something the caller can raise with — ``capture_failed`` is a
    legitimate outcome, not a crash.
    """
    try:
        raw, summary = _capture_known(final)
        reason_source, message = _pick_reason(raw, summary)
        detail: dict[str, Any] = {
            "reason_source": reason_source,
            "message": message,
            "captured_fields": summary,
        }
        if reason_source == "absent":
            detail["absent_dump"] = _absent_dump(final)
        return detail
    except Exception as exc:
        return {
            "reason_source": "capture_failed",
            "message": (
                f"SDK reported is_error; capture pipeline raised "
                f"{type(exc).__name__}: {_summarize_value(str(exc))}"
            ),
            "capture_error": type(exc).__name__,
            "capture_error_message": _summarize_value(str(exc)),
        }


def emit_sdk_error_marker(
    detail: dict[str, Any],
    *,
    stream: TextIO | None = None,
) -> None:
    """Write a single-line JSON marker to stdout (S-6).

    Deliberately best-effort: a failure to write the marker must not shadow the
    ``SdkIsErrorSignal`` that is being raised around it. The marker is a
    diagnostic, not a control-plane message; losing one to a closed stdout is
    strictly better than turning the underlying failure into a different one.

    ``json.dumps`` uses ``default=repr`` so any residual non-JSON object in
    ``detail`` (unlikely given ``_summarize_value``, but future-proof) still
    produces a marker rather than crashing the serializer.
    """
    target = stream if stream is not None else sys.stdout
    try:
        payload = json.dumps(detail, default=repr, ensure_ascii=False)
    except Exception:
        payload = json.dumps(
            {"reason_source": "capture_failed", "encoding_failed": True},
            ensure_ascii=False,
        )
    with contextlib.suppress(Exception):
        target.write(f"{SDK_ERROR_MARKER_PREFIX}{payload}\n")
        with contextlib.suppress(Exception):
            target.flush()


def find_sdk_error_signal(exc: BaseException) -> SdkIsErrorSignal | None:
    """Walk ``exc``'s ``__cause__`` / ``__context__`` chain for a signal.

    Used by ``loop_runner.main`` at the top-level try/except so the exit-time
    marker re-emission (S-6, second copy) can locate the signal even after the
    adapter has wrapped it in ``ClaudeCodeSdkDeliveryError`` /
    ``NaysayerSdkDeliveryError``.

    Cycle-safe: an exception whose ``__context__`` refers back to itself would
    otherwise loop.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, SdkIsErrorSignal):
            return current
        current = current.__cause__ or current.__context__
    return None


__all__ = [
    "SDK_ERROR_MARKER_PREFIX",
    "SdkIsErrorSignal",
    "capture_is_error_detail",
    "emit_sdk_error_marker",
    "find_sdk_error_signal",
]
