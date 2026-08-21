"""``mindwire-compose-decision`` CLI.

The PowerShell sweep wrapper invokes this on each ``NEXT: human`` stop
(one call per ``reason:last_msg`` signature — the wrapper enforces the
dedup by consulting ``pending-decisions.json`` before spawning us; this
CLI is a pure stateless converter from a well-formed request to a
well-formed envelope).

Invariants this CLI is on the hook for:

- **I-2** — every failure inside the port raises a
  :class:`~.exceptions.DecisionComposerError` and the CLI turns it into
  an envelope with a non-``ok`` :class:`~.value_objects.ComposerStatus`
  and a populated ``error`` string. The CLI **never** exits non-zero
  because the composer failed; the wrapper's I-2 test (A-2) requires
  that a broken composer still leave the sweep's own notification path
  intact, which means an envelope must always come back on stdout.
  Only a *usage* error (bad JSON, unknown backend) exits non-zero — a
  usage error is a bug in the sweep, not a runtime failure of the
  composer, and it should stop the pipeline loudly.
- **I-4** — the CLI trusts the caller (the wrapper) about the bound on
  the tail. The input schema requires ``tail_requested`` and
  ``total_messages`` alongside ``tail``, and the value-object
  validator refuses a ``tail`` longer than ``tail_requested``. The
  wrapper cannot silently expand the tail without lying in the
  envelope, and a lie shows up in the ``omitted_count`` diagnostic.

The input JSON shape is fixed and versioned by ``schema_version``
(currently ``1``). An unknown ``schema_version`` is a usage error, not
a composer failure, because the caller and the callee must agree on the
shape before the composer runs.

Input schema (stdin, UTF-8, JSON):

    {
      "schema_version": 1,
      "project": "spirrow-voxelworld",
      "thread_id": "T-slope-extension-dead-mode",
      "last_msg_id": "msg-2582",
      "stop_reason": "human",
      "rounds": 3,
      "thread_title": "…",
      "tail_requested": 5,
      "total_messages": 45,
      "tail": [
        {"msg_id": "msg-2578", "author": "Bohr", "body": "…"},
        …
      ]
    }

Output: one JSON object (the envelope) on stdout. Exit code 0 for a
composed or failed-composer envelope; non-zero only for a usage error.

Slice S3 additions (see ``spec/slices/S3-claude-code-composer.md``):

- ``--backend claude-code`` — routes through
  :class:`~.claude_code.ClaudeCodeComposer`, which shells out to the
  Claude Code CLI under D-35..D-43 (headless subprocess, no tools, no
  role context inherited, explicit UTF-8 decode).
- ``--tail N`` — D-38 tail fetch: when non-zero, the CLI fetches ``N``
  tail messages via ``chatroom_get_thread`` before invoking the composer,
  overriding whatever ``tail`` array was in the input JSON. The wrapper
  passes its ``$DecisionComposerTailLimit`` here for
  ``--backend claude-code``; other backends keep the S2 payload-tail
  behaviour by leaving ``--tail 0``.
- ``--body-cap C`` — per-body character cap for tail rendering (default
  4000; D-38).
- ``--claude-cli`` / ``--cwd`` / ``--timeout-seconds`` — knobs for
  ``claude-code`` used by tests and pinned deployments.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from .claude_code import (
    DEFAULT_CLAUDE_CLI,
    DEFAULT_TIMEOUT_SECONDS,
    ClaudeCodeComposer,
)
from .exceptions import (
    DecisionComposerEmptyError,
    DecisionComposerError,
    DecisionComposerTimeoutError,
)
from .ports import DecisionRequestComposer
from .stub import DEFAULT_STUB_IDENTITY, FailingStubComposer, StubComposer
from .value_objects import (
    ComposerStatus,
    DecisionRequestEnvelope,
    DecisionRequestInput,
    ThreadTailMessage,
)

INPUT_SCHEMA_VERSION = 1

DEFAULT_TAIL_BODY_CAP = 4000
"""Per-body character cap for tail messages (D-38).

Applied to each individual message body BEFORE assembly into the child's
prompt (spec §D-38: "件数を減らす前に body を切る" — cap bodies before
reducing count). 4000 was picked with a 5-message tail in mind:
5 * 4000 = 20 000 chars worst case, well below any prompt ceiling and
still large enough to hold an entire long naysayer message. Overridable
per invocation via ``--body-cap``.
"""


def _build_composer(
    backend: str,
    identity: str,
    *,
    claude_cli: str = DEFAULT_CLAUDE_CLI,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cwd: str | None = None,
) -> DecisionRequestComposer:
    """Resolve a backend name into a port implementation.

    S1 ships ``stub`` and its three failure variants (``fail-timeout`` /
    ``fail-empty`` / ``fail-error``) — the failure variants are what
    A-2 exercises. S3 adds ``claude-code``, which shells out to the
    Claude Code CLI under the D-35..D-43 constraints spelled out in
    ``spec/slices/S3-claude-code-composer.md``.
    """
    if backend == "stub":
        return StubComposer(identity_name=identity)
    if backend == "fail-timeout":
        return FailingStubComposer(kind="timeout", identity_name=identity)
    if backend == "fail-empty":
        return FailingStubComposer(kind="empty", identity_name=identity)
    if backend == "fail-error":
        return FailingStubComposer(kind="error", identity_name=identity)
    if backend == "claude-code":
        return ClaudeCodeComposer(
            identity_name=identity,
            cli_path=claude_cli,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
        )
    # NB: an unknown backend is a usage error, not a composer failure —
    # exit non-zero (see module docstring).
    raise SystemExit(
        f"unknown backend: {backend!r} "
        "(choose stub / fail-timeout / fail-empty / fail-error / claude-code)"
    )


def _parse_input(raw: str) -> DecisionRequestInput:
    """Parse the stdin JSON payload into a :class:`DecisionRequestInput`.

    Usage errors (invalid JSON, wrong schema_version, missing required
    field) raise :class:`SystemExit` — see module docstring for why they
    are treated differently from composer failures.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid input JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"input must be a JSON object, got {type(payload).__name__}")
    version = payload.get("schema_version")
    if version != INPUT_SCHEMA_VERSION:
        raise SystemExit(
            f"unsupported input schema_version={version!r}; this CLI accepts "
            f"schema_version={INPUT_SCHEMA_VERSION}"
        )
    tail_raw = payload.get("tail", [])
    if not isinstance(tail_raw, list):
        raise SystemExit(f"tail must be a list, got {type(tail_raw).__name__}")
    tail: list[ThreadTailMessage] = []
    for i, item in enumerate(tail_raw):
        if not isinstance(item, dict):
            raise SystemExit(f"tail[{i}] must be an object, got {type(item).__name__}")
        try:
            tail.append(
                ThreadTailMessage(
                    msg_id=str(item["msg_id"]),
                    author=str(item["author"]),
                    body=str(item["body"]),
                )
            )
        except KeyError as exc:
            raise SystemExit(f"tail[{i}] is missing required field {exc.args[0]!r}") from exc
    try:
        return DecisionRequestInput(
            project=str(payload["project"]),
            thread_id=str(payload["thread_id"]),
            last_msg_id=str(payload["last_msg_id"]),
            stop_reason=str(payload["stop_reason"]),
            rounds=int(payload["rounds"]),
            thread_title=str(payload.get("thread_title", "")),
            tail=tuple(tail),
            tail_requested=int(payload["tail_requested"]),
            total_messages=int(payload["total_messages"]),
        )
    except KeyError as exc:
        raise SystemExit(f"input missing required field {exc.args[0]!r}") from exc
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"input rejected: {exc}") from exc


def _now_iso() -> str:
    """Compose-time timestamp, ISO 8601 UTC.

    Extracted so tests can monkey-patch it if they ever need to freeze
    the clock. Not exported.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_extras(composer: DecisionRequestComposer) -> dict[str, str]:
    """Pull ``last_extras`` off a composer instance, tolerating its absence.

    S3 spec "Extras carrier": rather than adding an ``extras`` field to
    :class:`DecisionRequestOutput` (which would change a value-object
    shape S3 committed not to touch), backends signal per-call telemetry
    by writing to a mutable :attr:`last_extras` attribute. This helper
    reads it defensively so :class:`StubComposer` and other backends that
    don't set it keep working.

    Only string→string entries survive; a backend that shoves an int in
    gets it coerced (``str(...)``), matching the envelope's declared
    shape ``dict[str, str]``.
    """
    raw = getattr(composer, "last_extras", None) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def compose_once(
    composer: DecisionRequestComposer,
    request: DecisionRequestInput,
) -> DecisionRequestEnvelope:
    """Invoke the composer once and package the outcome into an envelope.

    Extracted from :func:`main` so both the CLI and any in-process test
    caller go through the exact same envelope-shaping rules. If this
    function raises, that is a bug in the value objects (the failure
    branches all produce a well-formed envelope by construction).
    """
    signature = f"{request.stop_reason}:{request.last_msg_id}"
    common: dict[str, object] = {
        "project": request.project,
        "thread_id": request.thread_id,
        "signature": signature,
        "composed_at": _now_iso(),
        "identity_used": composer.identity_name,
        "last_msg_id": request.last_msg_id,
        "stop_reason": request.stop_reason,
        "rounds": request.rounds,
        "tail_requested": request.tail_requested,
        "omitted_count": request.omitted_count,
    }
    try:
        output = composer.compose(request)
    except DecisionComposerTimeoutError as exc:
        return DecisionRequestEnvelope(
            **common,  # type: ignore[arg-type]
            tail_used=0,
            composer_status=ComposerStatus.TIMEOUT,
            output=None,
            error=str(exc) or "composer timed out",
            extras=_extract_extras(composer),
        )
    except DecisionComposerEmptyError as exc:
        return DecisionRequestEnvelope(
            **common,  # type: ignore[arg-type]
            tail_used=0,
            composer_status=ComposerStatus.EMPTY,
            output=None,
            error=str(exc) or "composer returned empty",
            extras=_extract_extras(composer),
        )
    except DecisionComposerError as exc:
        return DecisionRequestEnvelope(
            **common,  # type: ignore[arg-type]
            tail_used=0,
            composer_status=ComposerStatus.ERROR,
            output=None,
            error=str(exc) or "composer failed",
            extras=_extract_extras(composer),
        )
    except Exception as exc:  # I-2: never let a bare raise escape.
        # A backend that raised something outside DecisionComposerError is
        # a bug in the backend, not a bug in the composer contract. Report
        # it in the envelope so the wrapper still fires the raw ping and
        # the failure is visible in the digest.
        return DecisionRequestEnvelope(
            **common,  # type: ignore[arg-type]
            tail_used=0,
            composer_status=ComposerStatus.ERROR,
            output=None,
            error=f"composer raised {type(exc).__name__}: {exc}",
            extras=_extract_extras(composer),
        )
    # Success path.
    if output.tail_used > len(request.tail):
        return DecisionRequestEnvelope(
            **common,  # type: ignore[arg-type]
            tail_used=0,
            composer_status=ComposerStatus.ERROR,
            output=None,
            error=(
                f"composer claimed tail_used={output.tail_used} but was given "
                f"{len(request.tail)} messages; refusing to record the lie"
            ),
            extras=_extract_extras(composer),
        )
    return DecisionRequestEnvelope(
        **common,  # type: ignore[arg-type]
        tail_used=output.tail_used,
        composer_status=ComposerStatus.OK,
        output=output,
        error=None,
        extras=_extract_extras(composer),
    )


# --------------------------------------------------------------------------- #
# Tail fetching (D-38)
# --------------------------------------------------------------------------- #

# ``TailFetcher`` is the seam tests inject to avoid hitting the network. In
# production it is :func:`_default_fetch_tail`, which delegates to the same
# magickit MCP tool ``scripts/parked_humans.py`` uses.
TailFetcher = Callable[
    [str, str, int, int],  # project, thread_id, count, body_cap
    Awaitable[tuple[tuple[ThreadTailMessage, ...], int, int, bool]],
]
# Returns: (tail_messages, total_messages, total_chars_after_cap, any_body_truncated).


async def _default_fetch_tail(
    project: str,
    thread_id: str,
    count: int,
    body_cap: int,
) -> tuple[tuple[ThreadTailMessage, ...], int, int, bool]:
    """Fetch the last ``count`` messages of ``thread_id`` via magickit MCP.

    Reuses the exact tool ``scripts/parked_humans.py`` uses
    (``chatroom_get_thread`` in ``mode="full"``) so a thread that looks
    parked to S4 looks identically shaped to S3's tail fetch — no
    divergence in "what is the thread" between the two probes.

    Bodies exceeding ``body_cap`` are truncated with a trailing ``… (省略)``
    marker; the caller records whether any body was truncated in the
    envelope's extras (``tail_truncated``).
    """
    # Lazy import: keeps the CLI importable in environments that lack the
    # magickit dependency (unit-test-only harness, dry-run tooling).
    from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp

    mcp = StreamableHttpChatroomMcp(None)
    fetched: object = await mcp.call_tool(
        "chatroom_get_thread",
        {"project": project, "thread_id": thread_id, "mode": "full"},
    )
    if not isinstance(fetched, dict):
        raise DecisionComposerError(
            f"chatroom_get_thread returned non-dict: type={type(fetched).__name__}"
        )
    messages_raw = fetched.get("messages")
    if not isinstance(messages_raw, list):
        raise DecisionComposerError("chatroom_get_thread returned no messages list")
    total_messages = len(messages_raw)

    tail_slice = messages_raw[-count:] if count > 0 else []
    out: list[ThreadTailMessage] = []
    total_chars = 0
    any_truncated = False
    for m in tail_slice:
        if not isinstance(m, dict):
            continue
        body = str(m.get("content") or "")
        if len(body) > body_cap:
            body = body[:body_cap] + "… (省略)"
            any_truncated = True
        total_chars += len(body)
        out.append(
            ThreadTailMessage(
                msg_id=str(m.get("msg_id") or ""),
                author=str(m.get("author") or ""),
                body=body,
            )
        )
    return tuple(out), total_messages, total_chars, any_truncated


def _apply_fetched_tail(
    request: DecisionRequestInput,
    tail: tuple[ThreadTailMessage, ...],
    total_messages: int,
    tail_requested: int,
) -> DecisionRequestInput:
    """Return a new input with the fetched tail folded in.

    Uses :func:`dataclasses.replace` so any future field additions to
    :class:`DecisionRequestInput` do not need to be mirrored here.
    ``tail_requested`` is set to the CLI-side ``--tail N`` bound (which
    may exceed the actual fetched count when the thread is short), so
    ``omitted_count`` reports honestly.
    """
    return dataclasses.replace(
        request,
        tail=tail,
        tail_requested=tail_requested,
        total_messages=max(total_messages, len(tail)),
    )


def _run_tail_fetch(
    fetcher: TailFetcher,
    project: str,
    thread_id: str,
    count: int,
    body_cap: int,
) -> tuple[tuple[ThreadTailMessage, ...], int, int, bool]:
    """Run an async :type:`TailFetcher` and return its result synchronously.

    Extracted so tests can pass a coroutine-returning fake without having
    to think about the asyncio boundary. Production path calls
    :func:`_default_fetch_tail`, which is what
    :data:`DEFAULT_TAIL_FETCHER` binds.
    """
    import asyncio

    async def _run() -> tuple[tuple[ThreadTailMessage, ...], int, int, bool]:
        # Wrap the caller-supplied awaitable in a coroutine so ``asyncio.run``
        # (which requires a coroutine, not any awaitable) accepts it. Awaiting
        # a coroutine object returned by an ``async def`` fake works too.
        return await fetcher(project, thread_id, count, body_cap)

    return asyncio.run(_run())


# Module-level default: swappable in tests by monkey-patching this attribute
# on the :mod:`.cli` module (kept separate from a --flag so tests do not
# need to route a fetcher through argparse).
DEFAULT_TAIL_FETCHER: TailFetcher = _default_fetch_tail


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns 0 on a successfully composed envelope AND on a caught
    composer failure (both write a JSON envelope to stdout). Returns
    non-zero only on a usage error — the sweep's caller side treats
    that as "the pipe is broken", the same category as "config file
    missing".
    """
    parser = argparse.ArgumentParser(
        prog="mindwire-compose-decision",
        description="Compose a structured decision request for a parked thread.",
    )
    parser.add_argument(
        "--backend",
        default="stub",
        help=(
            "Composer backend to use. S1 ships 'stub' plus the failure "
            "variants 'fail-timeout' / 'fail-empty' / 'fail-error' for A-2 "
            "regression coverage. S3 adds 'claude-code' (D-35 headless "
            "subprocess, tool-less, neutral cwd)."
        ),
    )
    parser.add_argument(
        "--identity",
        default=DEFAULT_STUB_IDENTITY,
        help=(
            "Persona label to record in the envelope's identity_used field "
            "(I-5). Must not be one of the design-roster role personas."
        ),
    )
    parser.add_argument(
        "--input",
        default="-",
        help="Path to the input JSON payload; '-' reads stdin (default).",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=0,
        help=(
            "D-38: fetch this many tail messages via chatroom_get_thread before "
            "invoking the composer, overriding the 'tail' array in the input "
            "JSON. 0 (default) preserves the S2 stub/test path where the payload "
            "carries its own tail. The wrapper (deploy/run-conductor-scheduled.ps1) "
            "passes $DecisionComposerTailLimit here for --backend claude-code."
        ),
    )
    parser.add_argument(
        "--body-cap",
        type=int,
        default=DEFAULT_TAIL_BODY_CAP,
        help=(
            "D-38: per-body character cap applied to each tail message BEFORE "
            "assembly. Cap fires before any count reduction so F-1's "
            "'was N enough?' diagnostic stays honest."
        ),
    )
    parser.add_argument(
        "--claude-cli",
        default=DEFAULT_CLAUDE_CLI,
        help=(
            "Override the 'claude' executable path/name (for tests and pinned "
            "deployments). Ignored unless --backend claude-code."
        ),
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help=(
            "Override the child's working directory (D-37: default is a system "
            "temp dir OUTSIDE the repo so the child cannot find CLAUDE.md or "
            "persona settings)."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=(
            "Wall-clock ceiling for the child (D-35). Matches the wrapper's "
            "$DecisionComposerTimeoutSeconds; kept as a knob for tests."
        ),
    )
    args = parser.parse_args(argv)

    if args.input == "-":
        raw = sys.stdin.read()
    else:
        with open(args.input, encoding="utf-8") as f:
            raw = f.read()

    request = _parse_input(raw)

    # D-38 tail fetch — Python side pulls the tail so the child never
    # touches the chatroom (D-36). Runs BEFORE composer construction so
    # the composer receives an already-populated request.
    fetch_extras: dict[str, str] = {}
    if args.tail > 0:
        try:
            fetched_tail, total_messages, total_chars, any_truncated = _run_tail_fetch(
                DEFAULT_TAIL_FETCHER,
                request.project,
                request.thread_id,
                args.tail,
                args.body_cap,
            )
        except Exception as exc:  # I-2: a tail-fetch failure must not sink the
            # notification path. We fall back to the payload tail (which may be
            # empty), record the failure in extras, and let the composer proceed —
            # the composer will produce a legitimate "EMPTY" (no useful tail) or
            # an on-the-header question, both of which are visible degradation.
            fetch_extras["tail_fetch_error"] = f"{type(exc).__name__}: {exc}"
        else:
            request = _apply_fetched_tail(
                request,
                fetched_tail,
                total_messages=total_messages,
                tail_requested=args.tail,
            )
            fetch_extras["tail_count"] = str(len(fetched_tail))
            fetch_extras["tail_chars"] = str(total_chars)
            fetch_extras["tail_truncated"] = "true" if any_truncated else "false"

    composer = _build_composer(
        args.backend,
        args.identity,
        claude_cli=args.claude_cli,
        timeout_seconds=args.timeout_seconds,
        cwd=args.cwd,
    )
    envelope = compose_once(composer, request)
    if fetch_extras:
        # Merge fetch telemetry into whatever the composer recorded. The
        # composer's own extras take precedence when a key collides — an
        # unlikely case since the fetch keys are distinct — because the
        # composer authored the model call and knows better than we do
        # what its extras mean.
        merged = dict(fetch_extras)
        merged.update(envelope.extras)
        envelope = dataclasses.replace(envelope, extras=merged)
    # D-33 (msg-1394 §14.3): stdout JSON is ASCII-only. The wrapper reads this pipe as UTF-8,
    # but on Windows the child's ``sys.stdout`` encoding is inherited from the console code page
    # (cp932 on the deploy host) and there is no exception path when the two disagree — the
    # JSON structure characters happen to be ASCII either way, so ``json.loads`` succeeds and
    # only the Japanese payload is silently mojibake'd. ``ensure_ascii=True`` (the json default)
    # emits ``\uXXXX`` escapes, which are byte-equivalent under any single-byte-ASCII-compatible
    # encoding. This is the structural fix (msg-1394 §14.3); do NOT rely on PYTHONIOENCODING —
    # that closes the hole only for callers who remember to set it.
    json.dump(envelope.to_json(), sys.stdout, ensure_ascii=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
