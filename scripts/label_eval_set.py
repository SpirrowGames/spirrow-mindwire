"""Label the Tier-C evaluation set with two model families — T-decider-tierc-replay-eval.

Spec: Bohr msg-4229 §2 (material, batches of 15), msg-4231 (a script instead of chatroom
personas, a symmetric prompt, no naysayer preamble, prompt hash-locked), msg-4233 (fail-fast;
transport retries ≤3 with backoff; a format failure gets 1 same-prompt retry and then stops with
exit ≠ 0; a batch is written whole or not at all; raw responses kept under ``failures/``; one
client when Lexora exposes an Anthropic backend). Einstein approved after msg-4233.

**One client.** Both labellers go through :class:`LexoraClient.chat_completion`; only the
``model`` (a Lexora tier) differs. The probe on 2026-09-27 found the ``frontier`` tier (backend
``frontier``, ``claude-fable-5``) that takes a raw prompt; ``claude-code-opus`` works but adds
a ~22.8k-token harness prompt (the one-sided preamble msg-4231 rules out), and
``claude-sonnet-4-20250514`` returns 404 upstream. The backend actually used is read back from
Lexora's cost row for each call and written on every label line.

**What is sent.** ``system`` = ``label_prompt.md`` + ``RUBRIC.md`` verbatim; ``user`` = the
batch's material rows as JSON. Nothing else — never the other labeller's output, never Jev's,
never :func:`spirrow_mindwire.naysayer.principles.build_preamble` (pinned by a test).

**What is written.** ``labels.<labeller>.jsonl``: each line is the model's object unchanged plus
provenance (``labeller``, ``model``, ``backend``, ``batch``, ``prompt_sha256``). The raw
response of every accepted batch is kept under ``raw/<labeller>/``. A run resumes by skipping
keys already in the label file; nothing is ever edited by hand.

**Reader.** Files only; nothing is posted to a chatroom thread.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from spirrow_mindwire.lexora.client import (
    ChatCompletion,
    ChatMessage,
    LexoraAPIError,
    LexoraClient,
    LexoraHTTPError,
    LexoraTimeoutError,
)

_reconfigure_err = getattr(sys.stderr, "reconfigure", None)
if _reconfigure_err is not None:
    _reconfigure_err(errors="backslashreplace")

LABELLERS: dict[str, str] = {"naysayer-tier": "naysayer", "claude": "frontier"}
"""labeller name → Lexora tier (msg-4231 names the files by model, not by persona)."""

LABELS: frozenset[str] = frozenset(
    {"genuine", "genuine-merge", "genuine-action", "spurious", "ambiguous"}
)
CATEGORIES: frozenset[str] = frozenset(
    {
        "GOAL",
        "COST",
        "IRREVERSIBLE",
        "MERGE",
        "HUMAN_ONLY_ACTION",
        "IMPL",
        "ROUTING_ARTIFACT",
        "OTHER",
    }
)
BATCH_SIZE = 15
MAX_TOKENS = 32000
TRANSPORT_RETRIES = 3
BACKOFF_SECONDS: tuple[float, ...] = (10.0, 30.0, 90.0)

Key = tuple[str, int]


class LabelStopError(RuntimeError):
    """The run must stop (msg-4233 fail-fast). The label file has not been touched for the
    failing batch."""


class ChatClient(Protocol):
    async def chat_completion(
        self, *, model: str, messages: list[ChatMessage], max_tokens: int
    ) -> ChatCompletion: ...

    async def stats_costs_recent(self, *, limit: int = 50) -> list[dict[str, Any]]: ...


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def system_prompt(prompt_text: str, rubric_text: str) -> str:
    return prompt_text.rstrip() + "\n\n---\n\n" + rubric_text


def user_message(batch: Sequence[Mapping[str, Any]]) -> str:
    items = [
        {
            "thread_id": m["thread_id"],
            "round_index": m["round_index"],
            "project": m["project"],
            "author": m["author"],
            "posted_at": m["posted_at"],
            "body": m["body"]
            + (
                f"\n[... truncated: {m['body_chars']} chars in full]" if m["body_truncated"] else ""
            ),
            "prior": m["prior"],
            "following": m["following"],
        }
        for m in batch
    ]
    return (
        f"Label these {len(items)} items. Reply with one ```jsonl block, one line per item.\n\n"
        + json.dumps(items, ensure_ascii=False, indent=1)
    )


_BLOCK_RE = re.compile(r"```(?:jsonl|json)?\s*\n(.*?)```", re.DOTALL)


def parse_answer(text: str, expected: Sequence[Key]) -> list[dict[str, Any]]:
    """Validate one batch answer. Raises ``ValueError`` on any format failure (msg-4233)."""
    blocks = _BLOCK_RE.findall(text)
    if len(blocks) != 1:
        raise ValueError(f"expected exactly one ```jsonl block, found {len(blocks)}")
    out: list[dict[str, Any]] = []
    for n, line in enumerate(blocks[0].splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"line {n}: not JSON: {e}") from e
        if not isinstance(obj, dict):
            raise ValueError(f"line {n}: not an object")
        if set(obj) != {"thread_id", "round_index", "label", "category", "rationale"}:
            raise ValueError(f"line {n}: keys {sorted(obj)}")
        if obj["label"] not in LABELS:
            raise ValueError(f"line {n}: label {obj['label']!r}")
        if obj["category"] not in CATEGORIES:
            raise ValueError(f"line {n}: category {obj['category']!r}")
        if not isinstance(obj["rationale"], str):
            raise ValueError(f"line {n}: rationale not a string")
        if not isinstance(obj["round_index"], int) or not isinstance(obj["thread_id"], str):
            raise ValueError(f"line {n}: key types")
        out.append(obj)
    got = [(o["thread_id"], o["round_index"]) for o in out]
    if sorted(got) != sorted(expected) or len(set(got)) != len(got):
        raise ValueError(
            f"keys differ: missing={sorted(set(expected) - set(got))} "
            f"extra={sorted(set(got) - set(expected))} dup={len(got) - len(set(got))}"
        )
    return out


@dataclass(frozen=True)
class CallResult:
    text: str
    model: str | None
    backend: str | None
    usage: dict[str, Any]
    cost_usd: float | None


async def _backend_of(client: ChatClient, comp: ChatCompletion) -> tuple[str | None, float | None]:
    """The cost row for this call: newest row with the same model and input-token count."""
    tokens_in = comp.usage.get("prompt_tokens")
    try:
        rows = await client.stats_costs_recent(limit=20)
    except LexoraHTTPError:
        return None, None
    for row in rows:
        if row.get("model") == comp.model and row.get("tokens_input") == tokens_in:
            cost = row.get("cost_usd")
            return (
                str(row["backend"]) if row.get("backend") else None,
                float(cost) if isinstance(cost, int | float) else None,
            )
    return None, None


async def call_with_transport_retry(
    client: ChatClient,
    *,
    model: str,
    messages: list[ChatMessage],
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> CallResult:
    """Transport failures (timeout / 5xx / connection) are retried up to 3 times with backoff;
    they do not count as format failures. A 4xx or the 4th failure stops the run."""
    last: Exception | None = None
    for attempt in range(TRANSPORT_RETRIES + 1):
        try:
            comp = await client.chat_completion(
                model=model, messages=messages, max_tokens=MAX_TOKENS
            )
        except LexoraHTTPError as e:
            retryable = (
                isinstance(e, LexoraTimeoutError) or e.status_code is None or e.status_code >= 500
            )
            if not retryable:
                raise LabelStopError(f"transport: non-retryable {e}") from e
            last = e
            if attempt < TRANSPORT_RETRIES:
                await sleep(BACKOFF_SECONDS[attempt])
            continue
        except LexoraAPIError as e:
            # A 2xx without an assistant turn is a malformed answer, not a transport failure.
            return CallResult(
                text=f"[no assistant turn: {e}]", model=None, backend=None, usage={}, cost_usd=None
            )
        backend, cost = await _backend_of(client, comp)
        return CallResult(
            text=comp.content, model=comp.model, backend=backend, usage=comp.usage, cost_usd=cost
        )
    raise LabelStopError(f"transport: {TRANSPORT_RETRIES} retries exhausted: {last}")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


async def label_all(
    *,
    client: ChatClient,
    labeller: str,
    materials: Sequence[Mapping[str, Any]],
    system: str,
    out_dir: Path,
    batch_size: int = BATCH_SIZE,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> dict[str, Any]:
    """Label every material row not yet in ``labels.<labeller>.jsonl``. Raises
    :class:`LabelStopError` on the first batch that cannot be labelled cleanly."""
    model = LABELLERS[labeller]
    prompt_sha = sha256_text(system)
    label_path = out_dir / f"labels.{labeller}.jsonl"
    done = {(r["thread_id"], r["round_index"]) for r in read_jsonl(label_path)}
    todo = [m for m in materials if (m["thread_id"], m["round_index"]) not in done]
    stats: dict[str, Any] = {
        "batches": 0,
        "rows": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
    }
    batch_no = len({r.get("batch") for r in read_jsonl(label_path)})
    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        expected = [(m["thread_id"], m["round_index"]) for m in batch]
        messages = [ChatMessage("system", system), ChatMessage("user", user_message(batch))]
        raws: list[str] = []
        parsed: list[dict[str, Any]] | None = None
        result: CallResult | None = None
        for _ in range(2):  # the first try + exactly one same-prompt retry (msg-4233)
            result = await call_with_transport_retry(
                client, model=model, messages=messages, sleep=sleep
            )
            raws.append(result.text)
            stats["tokens_in"] += int(result.usage.get("prompt_tokens") or 0)
            stats["tokens_out"] += int(result.usage.get("completion_tokens") or 0)
            stats["cost_usd"] += result.cost_usd or 0.0
            try:
                parsed = parse_answer(result.text, expected)
                break
            except ValueError as e:
                raws[-1] = f"[format failure: {e}]\n{result.text}"
        if parsed is None or result is None:
            fail_dir = out_dir / "failures"
            fail_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = fail_dir / f"{labeller}-batch{batch_no:03d}-{stamp}.txt"
            path.write_text("\n\n=====\n\n".join(raws), encoding="utf-8")
            raise LabelStopError(f"format failure twice on batch {batch_no}; raw kept at {path}")
        raw_dir = out_dir / "raw" / labeller
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"batch{batch_no:03d}.txt").write_text(raws[-1], encoding="utf-8")
        with label_path.open("a", encoding="utf-8", newline="\n") as fh:
            for obj in parsed:
                fh.write(
                    json.dumps(
                        {
                            **obj,
                            "labeller": labeller,
                            "model": result.model,
                            "backend": result.backend,
                            "batch": batch_no,
                            "prompt_sha256": prompt_sha,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        stats["batches"] += 1
        stats["rows"] += len(parsed)
        print(
            f"label_eval_set: {labeller} batch {batch_no} ok ({len(parsed)} rows, "
            f"backend={result.backend}, model={result.model})",
            file=sys.stderr,
        )
        batch_no += 1
    return stats


TRANSPORT_DECISION = (
    "one client (LexoraClient) for both labellers — msg-4233 'ある場合'. Probe 2026-09-27 via "
    "/v1/models + cost rows: tier 'naysayer' -> backend gemini (gemini-3.1-pro-preview), raw "
    "prompt; tier 'frontier' -> backend frontier (claude-fable-5), raw prompt; "
    "'claude-code-opus' -> backend claude_code adds a ~22.8k-token harness prompt (the one-sided "
    "preamble msg-4231 rules out); 'claude-sonnet-4-20250514' -> 404 upstream."
)

LOCKED_FILES: tuple[str, ...] = (
    "RUBRIC.md",
    "label_prompt.md",
    "materials.jsonl",
    "fixture.jsonl",
    "labels.naysayer-tier.jsonl",
    "labels.claude.jsonl",
)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_lock(directory: Path) -> dict[str, Any]:
    """Record the hashes that must exist before any evaluation row goes to Jev (msg-4224 /
    msg-4231). Refuses unless both label files cover every material row."""
    materials = read_jsonl(directory / "materials.jsonl")
    keys = {(m["thread_id"], m["round_index"]) for m in materials}
    coverage: dict[str, int] = {}
    for who in LABELLERS:
        got = {
            (r["thread_id"], r["round_index"])
            for r in read_jsonl(directory / f"labels.{who}.jsonl")
        }
        if got != keys:
            raise LabelStopError(f"{who}: labels cover {len(got & keys)}/{len(keys)} rows")
        coverage[who] = len(got)
    prompt = (directory / "label_prompt.md").read_text(encoding="utf-8")
    rubric = (directory / "RUBRIC.md").read_text(encoding="utf-8")
    runs = read_jsonl(directory / "label_runs.jsonl")
    manifest = {
        "locked_at": datetime.now(UTC).isoformat(),
        "sha256": {name: file_sha256(directory / name) for name in LOCKED_FILES},
        "system_prompt_sha256": sha256_text(system_prompt(prompt, rubric)),
        "labellers": {
            who: {"tier": tier, "rows": coverage[who]} for who, tier in LABELLERS.items()
        },
        "transport": TRANSPORT_DECISION,
        "label_runs": runs,
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--labeller", choices=sorted(LABELLERS), default=None)
    parser.add_argument("--lock", action="store_true", help="write the hash lock (manifest.json)")
    parser.add_argument("--dir", type=Path, default=Path("eval/tierc"))
    parser.add_argument("--endpoint", default="http://100.79.84.62:8110")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args(argv)

    if args.lock:
        try:
            manifest = write_lock(args.dir)
        except LabelStopError as e:
            print(f"label_eval_set: STOP — {e}", file=sys.stderr)
            return 1
        print(json.dumps(manifest["sha256"], indent=2), file=sys.stderr)
        return 0
    if args.labeller is None:
        parser.error("--labeller or --lock is required")

    prompt = (args.dir / "label_prompt.md").read_text(encoding="utf-8")
    rubric = (args.dir / "RUBRIC.md").read_text(encoding="utf-8")
    system = system_prompt(prompt, rubric)
    materials = read_jsonl(args.dir / "materials.jsonl")

    async def run() -> dict[str, Any]:
        async with LexoraClient(args.endpoint, timeout_seconds=900) as client:
            return await label_all(
                client=client,
                labeller=args.labeller,
                materials=materials,
                system=system,
                out_dir=args.dir,
                batch_size=args.batch_size,
            )

    started = datetime.now(UTC).isoformat()
    status = "ok"
    stats: dict[str, Any] = {}
    try:
        stats = asyncio.run(run())
    except LabelStopError as e:
        status = f"stop: {e}"
        print(f"label_eval_set: STOP — {e}", file=sys.stderr)
    # Every run (including a stopped one and any re-batching) is recorded (msg-4233).
    record = {
        "labeller": args.labeller,
        "started_at": started,
        "batch_size": args.batch_size,
        "prompt_sha256": sha256_text(system),
        "status": status,
        **stats,
    }
    with (args.dir / "label_runs.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False), file=sys.stderr)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
