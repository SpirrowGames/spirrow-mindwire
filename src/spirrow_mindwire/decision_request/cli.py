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
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

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


def _build_composer(backend: str, identity: str) -> DecisionRequestComposer:
    """Resolve a backend name into a port implementation.

    S1 ships ``stub`` and its three failure variants (``fail-timeout`` /
    ``fail-empty`` / ``fail-error``) — the failure variants are what
    A-2 exercises. S3 adds ``claude-code``.
    """
    if backend == "stub":
        return StubComposer(identity_name=identity)
    if backend == "fail-timeout":
        return FailingStubComposer(kind="timeout", identity_name=identity)
    if backend == "fail-empty":
        return FailingStubComposer(kind="empty", identity_name=identity)
    if backend == "fail-error":
        return FailingStubComposer(kind="error", identity_name=identity)
    # NB: an unknown backend is a usage error, not a composer failure —
    # exit non-zero (see module docstring).
    raise SystemExit(
        f"unknown backend: {backend!r} (choose stub / fail-timeout / fail-empty / fail-error)"
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
        )
    except DecisionComposerEmptyError as exc:
        return DecisionRequestEnvelope(
            **common,  # type: ignore[arg-type]
            tail_used=0,
            composer_status=ComposerStatus.EMPTY,
            output=None,
            error=str(exc) or "composer returned empty",
        )
    except DecisionComposerError as exc:
        return DecisionRequestEnvelope(
            **common,  # type: ignore[arg-type]
            tail_used=0,
            composer_status=ComposerStatus.ERROR,
            output=None,
            error=str(exc) or "composer failed",
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
        )
    return DecisionRequestEnvelope(
        **common,  # type: ignore[arg-type]
        tail_used=output.tail_used,
        composer_status=ComposerStatus.OK,
        output=output,
        error=None,
    )


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
            "regression coverage. S3 will add 'claude-code'."
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
    args = parser.parse_args(argv)

    if args.input == "-":
        raw = sys.stdin.read()
    else:
        with open(args.input, encoding="utf-8") as f:
            raw = f.read()

    request = _parse_input(raw)
    composer = _build_composer(args.backend, args.identity)
    envelope = compose_once(composer, request)
    json.dump(envelope.to_json(), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
