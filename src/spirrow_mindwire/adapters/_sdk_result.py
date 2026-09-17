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


def _scalarize_denial_value(value: Any) -> str:
    """Reduce a denial-element value to a bounded string. Never a container.

    This is a *strict* scalarizer used inside :func:`_project_denial_element`:
    nested containers deliberately collapse to ``type(len=N)`` rather than
    recursing. The projection's whole purpose is to hand ``_summarize_value``
    a list whose members are already scalars, so ANY container reappearing
    here would defeat the point (the outer scalar-only predicate at line 168
    would reject it and the entire ``permission_denials`` field would fall
    back to ``list(len=1)`` — the exact defect this thread exists to fix).
    """
    if value is None or isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # Reuse the per-field string truncation so bounds match the rest of
        # the pipeline exactly.
        summarised = _summarize_value(value)
        return summarised if isinstance(summarised, str) else str(summarised)
    if hasattr(value, "__len__"):
        try:
            length = len(value)
        except Exception:
            return f"{type(value).__name__}(repr_omitted)"
        return f"{type(value).__name__}(len={length})"
    return f"{type(value).__name__}(repr_omitted)"


def _project_denial_element(elem: Any) -> str:
    """Project one ``permission_denials`` element to a bounded string.

    The SDK types ``ResultMessage.permission_denials`` as ``list[Any] | None``
    (`claude_agent_sdk/types.py:1159`, SDK 0.1.77 observed 2026-09-17) and the
    parser at ``_internal/message_parser.py:262`` passes the CLI's raw JSON
    array through unchanged, so there is no stable element schema to key off.
    Per msg-3156's fallback rule (``schema undefined / unstable → generic
    scalarizer alone``) this is a *generic* scalarizer: no priority key set,
    no field-name specialisation. For a mapping-shaped element it emits
    ``'"k1"="v1" "k2"="v2" …'`` with keys sorted for determinism and BOTH
    keys and values wrapped via :func:`json.dumps` on a stringified value so
    ``"``, ``\\``, newlines, and other control characters cannot break the
    key/value boundary or the marker's line-integrity (msg-3328: the marker
    is a single line and must stay a single line under all values); for an
    object with ``vars()`` it treats the attribute dict the same way; for a
    scalar it stringifies; for anything else it emits ``type(repr_omitted)``.

    ``json.dumps`` is used rather than a hand-rolled escaper deliberately: a
    bespoke escaper for a subset of characters would leave others (notably
    ``\\n`` and ``\\r``) to reach the marker verbatim and split the log line,
    which is exactly the defect Einstein's msg-3327 objection identified.
    ``str(...)`` normalises the input first so a numeric or boolean value
    doesn't get emitted as a bare token (``k=42``) that reintroduces boundary
    ambiguity — every key and every value is a quoted JSON string.

    Keys get the same treatment as values because the SDK types the field as
    ``list[Any]`` and the CLI populates dict elements from an untrusted CLI
    JSON blob whose key shape is not enforced (msg-3231 PR-gate follow-up on
    PR #288: an input mapping can easily contain a key with a newline
    character (e.g., ``{"bad\\nkey": "value"}``) and that newline would be
    emitted verbatim if the key were injected raw). Applying ``json.dumps``
    symmetrically closes the invariant on both sides of ``=``.

    The result is bounded by the same per-field length cap as every other
    captured string (``_FIELD_VALUE_MAX_LEN``), because the last line of this
    function feeds the projected text back through :func:`_summarize_value`'s
    string branch. The projection therefore preserves the ``bounded is
    bounded`` invariant that PR #181 round 3's docstring (lines 148-153)
    identified as load-bearing.
    """
    if elem is None or isinstance(elem, (bool, int, float, str)):
        return _scalarize_denial_value(elem)
    if isinstance(elem, dict):
        source: dict[Any, Any] = elem
    else:
        try:
            source = dict(vars(elem))
        except TypeError:
            source = {}
        except Exception:
            # A hostile __dict__ (property that raises, etc.) — surface the
            # type so a reader knows *something* was there.
            return f"{type(elem).__name__}(repr_omitted)"
    if not source:
        text = f"{type(elem).__name__}(repr_omitted)"
    else:
        try:
            keys = sorted(source.keys(), key=str)
        except Exception:
            # Sorting failed (mixed unorderable types after str() cast is
            # unlikely, but be defensive): fall back to insertion order.
            keys = list(source.keys())
        pairs = [
            f"{json.dumps(str(k))}={json.dumps(str(_scalarize_denial_value(source[k])))}"
            for k in keys
        ]
        text = " ".join(pairs)
    summarised = _summarize_value(text)
    return summarised if isinstance(summarised, str) else str(summarised)


def _project_denials(value: Any) -> Any:
    """Project ``permission_denials`` list elements to bounded strings.

    Applied in :func:`_capture_known` for the ``permission_denials`` field
    only, BEFORE :func:`_summarize_value` runs. The projection changes only
    *what* gets summarised: the scalar-only predicate at line 168, the
    per-field length cap, and ``_SMALL_LIST_ELEM_LIMIT`` are all unchanged.
    After projection the list contains only strings, so the predicate accepts
    it and the element-wise preservation branch takes over — exactly what
    ``errors=[…]`` already gets for free because the SDK types errors as
    ``list[str]``.

    Non-list / non-tuple inputs pass through unchanged so downstream
    :func:`_summarize_value` handles ``None`` / scalars / dicts the same way
    it did before this change. Empty lists also pass through unchanged, which
    keeps the ``[]`` non-regression case identical to prior behaviour
    (``[]`` → ``[]`` → :func:`_is_empty_reason_value` returns True →
    ``reason_source`` falls through to the next candidate, as it did before).

    Long lists are truncated to ``_SMALL_LIST_ELEM_LIMIT`` elements with a
    trailing ``"+K more"`` string so the count of dropped entries is itself
    a diagnostic surface — a reader can tell "one denial" from "twenty
    denials, first eight preserved".
    """
    if not isinstance(value, (list, tuple)):
        return value
    if not value:
        return value
    # If the input already fits within the outer preservation bound, keep
    # every element. Otherwise reserve one slot for the ``"+K more"`` overflow
    # marker so the FINAL list length is still ``<= _SMALL_LIST_ELEM_LIMIT``
    # and passes the scalar-only predicate in :func:`_summarize_value`; if
    # instead we merely truncated at the raw limit and then appended, the
    # resulting length would be ``_SMALL_LIST_ELEM_LIMIT + 1`` and the whole
    # field would collapse right back to ``list(len=N)`` — the very defect
    # this projection exists to prevent.
    keep = len(value) if len(value) <= _SMALL_LIST_ELEM_LIMIT else _SMALL_LIST_ELEM_LIMIT - 1
    projected: list[str] = []
    for elem in value[:keep]:
        try:
            projected.append(_project_denial_element(elem))
        except Exception as exc:
            # Never let a single hostile element destroy the whole projection.
            projected.append(f"project_failed:{type(exc).__name__}")
    if len(value) > keep:
        projected.append(f"+{len(value) - keep} more")
    return projected


def _project_denials_safely(value: Any) -> Any:
    """:func:`_project_denials` with a top-level fail-safe.

    If the projection pipeline itself raises, fall through to the raw value
    so :func:`_summarize_value` still produces *something* — a ``list(len=N)``
    summary is a regression, but a crash that shadows the underlying
    ``SdkIsErrorSignal`` is strictly worse.
    """
    try:
        return _project_denials(value)
    except Exception:
        return value


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
        # Same reasoning as the list branch above (PR #181 round 4): a
        # non-string ``result={"code": …, "message": …}`` — plausible on a
        # future SDK version — deserves to reach the marker as the actual dict
        # contents, not as the opaque ``dict(len=2)`` that renders no reason
        # text. Bounded by size AND by the scalar-only key/value predicate.
        if len(value) <= _SMALL_LIST_ELEM_LIMIT and all(
            isinstance(k, str) and (v is None or isinstance(v, (bool, int, float, str)))
            for k, v in value.items()
        ):
            return {k: _summarize_value(v) for k, v in value.items()}
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

    ``permission_denials`` gets a dedicated projection pass BEFORE the shared
    summariser sees it (T-quarantine-reasons-captured-but-never-read, msg-2944
    §5): the SDK types this field as ``list[Any]`` and the CLI populates it
    with mapping-shaped elements, so ``_summarize_value``'s scalar-only
    predicate rejects the whole list and it falls back to the useless
    ``list(len=1)``. The projection reduces each element to a bounded string
    ahead of time so the same predicate accepts it and the element-wise
    preservation branch takes over. Nothing else about capture changes —
    ``permission_denials`` remains an independent key in ``captured_fields``
    (Einstein msg-2943 (c) constraint: authorisation-failure surfaces must
    NOT be merged into ``errors[]``).
    """
    raw: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    for name in (*_SESSION_FACT_FIELDS, *_KNOWN_REASON_FIELDS):
        value = _raw_field(final, name)
        raw[name] = value
        if name == "permission_denials" and value is not _CAPTURE_ERROR_SENTINEL:
            summary[name] = _summarize_safely(_project_denials_safely(value))
        else:
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

    Precedence:

    1. ``result`` as a non-empty string → ``reason_source="result"`` and the
       verbatim string as the message. This is the ordinary happy path — the
       SDK put its reason text on this field.

    2. Any known field (including ``result``) whose RAW value is not empty
       (per :func:`_is_empty_reason_value`) → ``field:<name>``, message
       formatted from the SUMMARY so bounds are enforced.

    3. Everything empty → ``absent`` with the sentence explaining that N
       fields were captured and none carried a reason.

    Note the deliberate difference between step 1 and step 2: step 1 fires ONLY
    for non-empty strings on ``result`` (the common case), but if ``result``
    holds a non-string non-empty value (dict / list — plausible on a future
    SDK version) the loop in step 2 STILL considers it. An earlier version
    skipped ``result`` in the loop (having "already handled it" in step 1),
    which silently dropped non-string ``result`` values into ``absent`` — the
    reason vanished into the very shadow this whole thread exists to remove.
    (PR #181 round 4 naysayer review.)
    """
    result = raw.get("result")
    if isinstance(result, str) and result:
        return "result", result

    for name in _KNOWN_REASON_FIELDS:
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
            # ``dir()`` never raises; per-name ``getattr`` can (a property whose
            # getter raises RuntimeError / ValueError is likely on a broken SDK
            # object, which is *exactly* the input this branch is here for).
            # An earlier version used a bare ``getattr(final, n, None)`` inside
            # this list comprehension — a single hostile property raised out of
            # the comprehension, the outer except caught it, and the ENTIRE
            # dump collapsed to ``{}``: the reflection evidence for ``absent``
            # was destroyed by the very defect the reflection existed to
            # surface. (PR #181 round 4 naysayer review.)
            #
            # Fix: each ``getattr`` is wrapped. A raising property KEEPS its
            # name in ``names`` — so its failure surfaces later in the fields
            # dump as ``capture_failed: true`` via ``_capture_field`` rather
            # than silently disappearing. Only names whose value is
            # unambiguously callable are excluded here.
            def _keep(name: str) -> bool:
                if name.startswith("_"):
                    return False
                try:
                    value = getattr(final, name)
                except Exception:
                    # Property raised; include so its failure is visible.
                    return True
                return not callable(value)

            try:
                names = [n for n in dir(final) if _keep(n)]
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
