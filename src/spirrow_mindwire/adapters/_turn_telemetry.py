"""Identity-only turn telemetry for the Claude SDK sessions.

T-implementer-context-drop-unexplained D-1'.

**Why this exists.** An implementer run started without the proposer message two
places before its trigger (``spirrow-voxelworld`` / T227 msg-2503), although that
message sat well inside the bounded window :mod:`~spirrow_mindwire.thread_context`
selects. Nothing recorded what was actually built or sent, so the loss could not be
placed: (a) the context was never injected, or (b) it was injected and the SDK
session lost it afterwards (auto-compaction of a session the conductor reuses for
every round). These lines are the instruments that tell the two apart in prod:

- Point-1 lives in :func:`~spirrow_mindwire.thread_context.build_thread_context`
  (one place, both builder call sites) — what the builder selected.
- Point-2 (:func:`log_prompt`) — the prompt the adapter actually sent still names
  every message the builder selected, and how much this session has been sent.
- Point-3 (:func:`log_context_usage`) — how full the session's context is after
  each turn.
- Point-4 (:func:`precompact_hooks`) — the session compacted, on which turn.

**Tier A boundary: identity only.** Every line carries ids, counts, lengths and a
hash prefix — never message text, prompt text, ``custom_instructions`` or a
``transcript_path``. Point-4 is the one place an SDK payload arrives with content
fields on it, so its key set is pinned by :data:`PRECOMPACT_LOG_KEYS` and asserted
in ``tests/test_turn_telemetry.py``; a contribution that adds a field fails CI.

**Never fails a turn.** Telemetry that raises would turn an instrument into a new
failure mode, so Point-3's control call is caught and logged as unavailable.

**Primary path only.** Point-4 is the in-process ``hooks={"PreCompact": [...]}``
callback. The ``include_hook_events=True`` stream path is deliberately not built
(Einstein msg-3294 / Bohr msg-3295): if the callback turns out not to fire, that is
a design question for the thread, not a reason to add a second path here.
"""

from __future__ import annotations

import hashlib
import logging
import uuid as _uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from claude_agent_sdk import HookContext, HookInput, HookJSONOutput, HookMatcher

from ..value_objects import ChatroomEvent

logger = logging.getLogger(__name__)

PRECOMPACT_LOG_KEYS: frozenset[str] = frozenset(
    {"session_id", "trigger", "uuid", "timestamp", "upstream_msg_id"}
)
"""The exact key set a Point-4 line may carry (Bohr msg-3295, Einstein msg-3296).

Load-bearing for the Tier A boundary: ``PreCompactHookInput`` also carries
``custom_instructions`` and ``transcript_path``, and neither may reach a log."""


@dataclass
class TurnTelemetryState:
    """Per-session state the telemetry needs across turns.

    Created at spawn, before the options are built, because the Point-4 callback is
    registered in the options and must read which turn is in flight when it fires.
    """

    upstream_msg_id: str | None = None
    """The trigger ``msg_id`` of the turn currently being delivered."""

    cumulative_bytes: int = 0
    """UTF-8 bytes of every prompt this session has been sent, this one included."""


def _recent_ids(event: ChatroomEvent) -> tuple[str, ...]:
    ctx = event.thread_context
    return () if ctx is None else tuple(m.msg_id for m in ctx.recent)


def log_prompt(event: ChatroomEvent, prompt: str, state: TurnTelemetryState) -> None:
    """Point-2: one line describing the prompt about to be sent.

    ``contains_all_recent_ids`` checks each selected message's rendered header
    (``--- <msg_id> · ``), not a bare id substring, so an id merely quoted in some
    other message's body cannot satisfy it.
    """
    msg_id = event.payload.msg_id
    state.upstream_msg_id = msg_id
    state.cumulative_bytes += len(prompt.encode("utf-8"))
    recent = _recent_ids(event)
    contains_all = all(f"--- {rid} · " in prompt for rid in recent)
    logger.info(
        "turn-telemetry prompt trigger_msg_id=%s contains_all_recent_ids=%s prompt_len=%d "
        "prompt_sha256=%s session_cumulative_bytes=%d",
        msg_id,
        str(contains_all).lower(),
        len(prompt),
        hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8],
        state.cumulative_bytes,
    )


async def log_context_usage(client: Any, state: TurnTelemetryState) -> None:
    """Point-3: one line with the session's context fill, read right after the drain.

    A client without the control call (a test fake, an older CLI) or a call that
    fails is logged as unavailable; the turn's outcome is never affected.
    """
    getter = getattr(client, "get_context_usage", None)
    if getter is None:
        logger.info(
            "turn-telemetry context-usage trigger_msg_id=%s unavailable=no-control-call",
            state.upstream_msg_id,
        )
        return
    try:
        usage = await getter()
    except Exception as exc:  # telemetry must never fail the turn
        logger.warning(
            "turn-telemetry context-usage trigger_msg_id=%s unavailable=%s",
            state.upstream_msg_id,
            type(exc).__name__,
        )
        return
    usage = usage if isinstance(usage, Mapping) else {}
    logger.info(
        "turn-telemetry context-usage trigger_msg_id=%s total_tokens=%s max_tokens=%s "
        "percentage=%s",
        state.upstream_msg_id,
        usage.get("totalTokens"),
        usage.get("maxTokens"),
        usage.get("percentage"),
    )


def precompact_fields(
    input_data: Mapping[str, Any],
    *,
    upstream_msg_id: str | None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    new_uuid: Callable[[], str] = lambda: str(_uuid.uuid4()),
) -> dict[str, Any]:
    """Point-4: the fields of one PreCompact line — exactly :data:`PRECOMPACT_LOG_KEYS`.

    ``PreCompactHookInput`` (SDK 0.1.77) carries ``session_id`` and ``trigger`` but
    no event uuid or timestamp, so ``uuid`` is minted here (one per firing, so two
    lines are never confused) and ``timestamp`` is the callback's UTC wall clock.
    Everything else on ``input_data`` is dropped by construction: the result is
    built from named keys, never from a copy of the input.
    """
    return {
        "session_id": str(input_data.get("session_id", "")),
        "trigger": str(input_data.get("trigger", "")),
        "uuid": new_uuid(),
        "timestamp": now().isoformat(),
        "upstream_msg_id": upstream_msg_id,
    }


def precompact_hooks(state: TurnTelemetryState) -> dict[Any, list[HookMatcher]]:
    """Point-4: the ``ClaudeAgentOptions.hooks`` value that logs every compaction.

    The callback returns ``{}`` — no decision — so it observes compaction and never
    blocks or alters it.
    """

    async def _on_precompact(
        input_data: HookInput, _tool_use_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        fields = precompact_fields(input_data, upstream_msg_id=state.upstream_msg_id)
        logger.info(
            "turn-telemetry precompact %s",
            " ".join(f"{k}={fields[k]}" for k in sorted(fields)),
        )
        return {}

    return {"PreCompact": [HookMatcher(matcher=None, hooks=[_on_precompact])]}


__all__ = [
    "PRECOMPACT_LOG_KEYS",
    "TurnTelemetryState",
    "log_context_usage",
    "log_prompt",
    "precompact_fields",
    "precompact_hooks",
]
