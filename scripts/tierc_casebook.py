"""Tier-C casebook 2026-10-08 (measurement only, PR1).

Thread: T-tierc-overpass-self-contradicting-escalations.

Spec: Bohr msg-6664 (design), msg-6666 (Einstein's two objections taken: whole-body detector,
mutually exclusive B → A → observe rule), summarised in msg-6668; approved by Einstein
msg-6667 / msg-6669 and the hand-off to the implementer. Operator audit: msg-6653.

Four sub-commands, run in this order. Only ``build`` and ``measure-b`` touch the network.

``build`` (read-only chatroom calls + conductor logs → files)
    * ``casebook.jsonl`` — the 7 audited escalations. ``jev_input`` is the ``state_wire`` string
      the live hook logged on its ``decider_decision`` row (the bytes Jev was sent) — copied,
      never rebuilt (msg-6664 §1). ``ask_score`` is the logged answer. Truth and failure type
      come from ``truth.json`` (source ``audit-operator``), not from any labeller.
      For a row whose ``truth.json`` entry names a ``negation_sentence``, the build records
      where that sentence sits in the full body and whether it is inside the ``head_summary``
      Jev was given (msg-6664 §1 "additional measurement").
    * ``genuine_fulltext.jsonl`` — set (ii): the rows of ``eval/tierc/fulltext-2026-09-28`` whose
      consensus (both labellers agree, as ``tierc_eval_report.consensus``) is genuine,
      genuine-merge or genuine-action, with the full body fetched from the chatroom.
    * ``next_human.jsonl`` — set (iii): every message by a ``[conductor.roster]`` identity, posted
      in ``[--since, --as-of]``, whose last ``NEXT:`` token (``parse_next_token``, the
      conductor's own parser) is ``human``. Bounced or not — the label gate is not consulted.
    * ``build.json`` — the inputs, counts and the sha256 of every file written.

``measure-a`` (offline) — runs :func:`tierc_selfneg.detect_self_negation` on the three sets and
writes ``selfneg.jsonl`` (one line per row, hits included).

``measure-b`` (``/v1/decide``) — the 7 casebook rows x ``--runs`` (3) x two question sets:
``tierc-v3`` (the live set, built by the same ``tierc_v2_questions``) and ``tierc-v4-candidate``
(the same plus one sentence, :data:`V4_CANDIDATE_SENTENCE`; record only, never live). The
``state`` sent is the logged ``state_wire`` verbatim. 42 calls; written as each returns.

``report`` (offline) — ``report.md`` from the files above, applying the decision rule fixed in
msg-6666 before any measurement (:func:`decide`).

**Reader of the output.** Files only; nothing is posted to any chatroom thread.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tierc_selfneg import detect_self_negation, looks_quoted_or_fenced

from spirrow_mindwire.decider.hook import BODY_HEAD_M
from spirrow_mindwire.decider.questions import (
    SHOULD_ASK_HUMAN_KEY,
    TIERC_ESCALATION_QUESTIONS_VERSION,
    TierCRules,
    load_tierc_rules,
    tierc_v2_questions,
)
from spirrow_mindwire.decider.verdict import DEFAULT_V2_ASK_MIN, DEFAULT_V2_NOT_ASK_MAX
from spirrow_mindwire.decider.wire import POLICY_REPLAY_TIERC

LOG_PREFIX = "decider_decision "
REGISTERED_RULES_SHA256 = "159979a7ff2193b1ce8425a1f2b7501960a8d24885547922ee017664a72ccb1b"
"""``eval/tierc/shadow-prereg.md`` §2 — the rules file the live rows were asked under."""

V3 = TIERC_ESCALATION_QUESTIONS_VERSION
V4_CANDIDATE = "tierc-v4-candidate"
V4_CANDIDATE_SENTENCE = (
    "本文自身が人の判断は不要・時期尚早と述べているハンドオフは、どの条にも当たらない。"
)
"""msg-6664 §3 (the one sentence), appended to the ``should_ask_human`` question frame.
Record only: never imported by the conductor, never a registered version."""
VARIANTS: tuple[str, ...] = (V3, V4_CANDIDATE)
EXPECTED_PROVIDER = "jev"

GENUINE_CLASSES: tuple[str, ...] = ("genuine", "genuine-merge", "genuine-action")
SELF_NEGATION = "self_negation"

CASEBOOK = "casebook.jsonl"
GENUINE_FULLTEXT = "genuine_fulltext.jsonl"
NEXT_HUMAN = "next_human.jsonl"
BUILD = "build.json"
SELFNEG = "selfneg.jsonl"
JEV = "jev.jsonl"
REPORT = "report.md"

REPO = Path(__file__).resolve().parent.parent
FULLTEXT_DIR = REPO / "eval" / "tierc" / "fulltext-2026-09-28"
LABEL_FILES = (
    REPO / "eval" / "tierc" / "labels.naysayer-tier.jsonl",
    REPO / "eval" / "tierc" / "labels.frontier-tier.jsonl",
)


class CasebookError(RuntimeError):
    """An input does not have the shape the spec assumes; the run stops instead of guessing."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_IPV4 = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9]|\.[0-9])")
_WIN_PROFILE = re.compile(r"(?i)(C:[\\/]{1,2}Users[\\/]{1,2})[^\\/\s`'\"]+")
REDACTED_IPV4 = "<ipv4>"
REDACTED_USER = "<user>"


def redact_infra(text: str) -> str:
    r"""Replace IPv4 addresses and the Windows user-profile name in ``C:\Users\<name>`` with
    placeholders before anything is written into this (public) repository — the C-42 convention
    of ``T-real-infra-values-egress-from-agent-context`` (report placeholders, not values). Ports
    and paths after the profile stay. Each committed text keeps the sha256 of its original
    (``*_sha256``); ``measure-b`` sends the original, re-read from the conductor log."""
    return _WIN_PROFILE.sub(lambda m: m.group(1) + REDACTED_USER, _IPV4.sub(REDACTED_IPV4, text))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def parse_ts(raw: str) -> datetime:
    ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise CasebookError(f"timestamp {raw!r} has no timezone")
    return ts


def decider_rows(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Every ``decider_decision`` JSON payload in the log lines (unparseable lines are skipped:
    a log line can be cut by a restart; the casebook lookup then fails loudly if it needed it)."""
    out: list[dict[str, Any]] = []
    for line in lines:
        i = line.find(LOG_PREFIX)
        if i < 0:
            continue
        try:
            row = json.loads(line[i + len(LOG_PREFIX) :])
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def logged_row_for(
    rows: Sequence[Mapping[str, Any]], thread: str, msg_id: str
) -> Mapping[str, Any]:
    """The one logged row with point-in-time input for ``(thread, msg_id)``. Zero or several stop
    the build: the casebook must carry what Jev saw, not a pick among candidates."""
    hits = [
        r
        for r in rows
        if r.get("thread_id") == thread
        and r.get("latest_msg_id") == msg_id
        and isinstance(r.get("state_wire"), str)
    ]
    if len(hits) != 1:
        raise CasebookError(f"{thread}/{msg_id}: {len(hits)} logged rows with state_wire, need 1")
    return hits[0]


def ask_score_of(row: Mapping[str, Any]) -> float | None:
    raw = row.get("raw_answers")
    entry = raw.get(SHOULD_ASK_HUMAN_KEY) if isinstance(raw, Mapping) else None
    value = entry.get("noul") if isinstance(entry, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def consensus_of(label_rows: Sequence[Sequence[Mapping[str, Any]]]) -> dict[tuple[str, int], str]:
    """Same rule as ``tierc_eval_report.consensus`` (category OTHER → ambiguous; any disagreement
    or a missing labeller → ambiguous)."""
    per: list[dict[tuple[str, int], str]] = []
    for rows in label_rows:
        per.append(
            {
                (str(r["thread_id"]), int(r["round_index"])): (
                    "ambiguous" if r.get("category") == "OTHER" else str(r["label"])
                )
                for r in rows
            }
        )
    keys: set[tuple[str, int]] = set().union(*per) if per else set()
    out: dict[tuple[str, int], str] = {}
    for k in keys:
        vals = {m.get(k) for m in per}
        out[k] = vals.pop() if len(vals) == 1 and None not in vals else "ambiguous"
    return out


def is_role_next_human(author: str, body: str, roster_names: Iterable[str]) -> bool:
    """Set (iii) membership: a roster identity (case-insensitive) whose last ``NEXT:`` is human."""
    from spirrow_mindwire.conductor.handoff import parse_next_token

    names = {n.casefold() for n in roster_names}
    if author.casefold() not in names:
        return False
    token = parse_next_token(body)
    return token is not None and token.casefold() == "human"


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def casebook_row(
    truth: Mapping[str, Any], logged: Mapping[str, Any], msg: Mapping[str, Any]
) -> dict[str, Any]:
    state = json.loads(str(logged["state_wire"]))
    head = str(state.get("head_summary") or "")
    gate = state.get("gate_result") or {}
    body = str(msg.get("content", ""))
    verdict = logged.get("verdict") or {}
    row: dict[str, Any] = {
        "msg_id": truth["msg_id"],
        "project": truth["project"],
        "thread": truth["thread"],
        "author": str(msg.get("author", "")),
        "posted_at": str(msg.get("timestamp", "")),
        "label": gate.get("label"),
        "jev_input": redact_infra(str(logged["state_wire"])),
        "jev_input_sha256": sha256_text(str(logged["state_wire"])),
        "ask_score": ask_score_of(logged),
        "logged_verdict_kind": verdict.get("kind") if isinstance(verdict, Mapping) else None,
        "logged_decision_id": logged.get("decision_id"),
        "logged_at": logged.get("logged_at"),
        "questions_version": logged.get("questions_version"),
        "rules_sha256": logged.get("rules_sha256"),
        "truth": truth["truth"],
        "truth_source": "audit-operator",
        "failure_type": truth["failure_type"],
        "evidence": list(truth["evidence"]),
        "evidence_note": truth.get("evidence_note"),
        "body": redact_infra(body),
        "body_sha256": sha256_text(body),
        "body_chars": len(body),
        "jev_head_chars": len(head),
        "head_is_body_prefix": body.startswith(head),
    }
    sentence = truth.get("negation_sentence")
    if sentence is not None:
        offset = body.find(sentence)
        if offset < 0:
            raise CasebookError(f"{truth['msg_id']}: negation_sentence not found in the body")
        row["negation_sentence"] = sentence
        row["negation_offset"] = offset
        row["negation_in_jev_input"] = sentence in head
    return row


async def _get_thread(mcp: Any, project: str, thread: str) -> list[dict[str, Any]]:
    body = await mcp.call_tool(
        "chatroom_get_thread", {"project": project, "thread_id": thread, "mode": "full"}
    )
    return [m for m in (body.get("messages") or []) if isinstance(m, dict) and m.get("timestamp")]


async def _list_threads(mcp: Any, project: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await mcp.call_tool(
            "chatroom_list_threads", {"project": project, "limit": 200, "offset": offset}
        )
        items = page.get("items") or []
        out.extend(items)
        offset += len(items)
        if not items or offset >= page.get("total", len(out)):
            return out


def _msg(msgs: Sequence[Mapping[str, Any]], msg_id: str, where: str) -> Mapping[str, Any]:
    found = [m for m in msgs if m.get("msg_id") == msg_id]
    if len(found) != 1:
        raise CasebookError(f"{where}/{msg_id}: {len(found)} messages")
    return found[0]


async def build(
    out_dir: Path,
    logs: Sequence[Path],
    projects: Sequence[str],
    since: datetime,
    as_of: datetime,
    roster_names: Sequence[str],
) -> dict[str, Any]:
    from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp

    mcp = StreamableHttpChatroomMcp()
    truth = json.loads((out_dir / "truth.json").read_text(encoding="utf-8"))
    lines: list[str] = []
    for p in logs:
        lines.extend(p.read_text(encoding="utf-8", errors="replace").splitlines())
    logged = decider_rows(lines)

    cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    async def thread(project: str, tid: str) -> list[dict[str, Any]]:
        if (project, tid) not in cache:
            cache[(project, tid)] = await _get_thread(mcp, project, tid)
        return cache[(project, tid)]

    # (i) casebook
    cb: list[dict[str, Any]] = []
    for t in truth["rows"]:
        row = logged_row_for(logged, t["thread"], t["msg_id"])
        msg = _msg(await thread(t["project"], t["thread"]), t["msg_id"], t["thread"])
        cb.append(casebook_row(t, row, msg))

    # (ii) fulltext consensus-genuine rows
    fx = read_jsonl(FULLTEXT_DIR / "fixture.fulltext.m8000.jsonl")
    cons = consensus_of([read_jsonl(p) for p in LABEL_FILES])
    gen: list[dict[str, Any]] = []
    for r in fx:
        key = (str(r["thread_id"]), int(r["round_index"]))
        c = cons.get(key, "ambiguous")
        if c not in GENUINE_CLASSES:
            continue
        msg = _msg(await thread(str(r["project"]), key[0]), str(r["msg_id"]), key[0])
        gen.append(
            {
                "project": r["project"],
                "thread": key[0],
                "round_index": key[1],
                "msg_id": r["msg_id"],
                "author": msg.get("author"),
                "consensus": c,
                "body": redact_infra(str(msg.get("content", ""))),
                "body_sha256": sha256_text(str(msg.get("content", ""))),
            }
        )

    # (iii) every role NEXT: human in [since, as_of]
    nh: list[dict[str, Any]] = []
    for project in projects:
        for t in await _list_threads(mcp, project):
            last = t.get("last_activity_at")
            if isinstance(last, str) and parse_ts(last) < since:
                continue
            for m in await thread(project, str(t["thread_id"])):
                ts = parse_ts(str(m["timestamp"]))
                if not since <= ts <= as_of:
                    continue
                body = str(m.get("content", ""))
                if is_role_next_human(str(m.get("author", "")), body, roster_names):
                    nh.append(
                        {
                            "project": project,
                            "thread": t["thread_id"],
                            "msg_id": m["msg_id"],
                            "author": m.get("author"),
                            "posted_at": m["timestamp"],
                            "body": redact_infra(body),
                            "body_sha256": sha256_text(body),
                        }
                    )
    nh.sort(key=lambda r: (r["posted_at"], r["project"], r["msg_id"]))

    write_jsonl(out_dir / CASEBOOK, cb)
    write_jsonl(out_dir / GENUINE_FULLTEXT, gen)
    write_jsonl(out_dir / NEXT_HUMAN, nh)
    meta = {
        "as_of": as_of.isoformat(),
        "since": since.isoformat(),
        "projects": list(projects),
        "roster": list(roster_names),
        "logs": [p.name for p in logs],
        "counts": {CASEBOOK: len(cb), GENUINE_FULLTEXT: len(gen), NEXT_HUMAN: len(nh)},
        "sha256": {
            n: sha256_file(out_dir / n)
            for n in ("truth.json", CASEBOOK, GENUINE_FULLTEXT, NEXT_HUMAN)
        },
        "body_head_m": BODY_HEAD_M,
        "redaction": "IPv4 -> <ipv4>, C:/Users/<name> -> <user> (redact_infra); "
        "*_sha256 = sha256 of the original",
    }
    (out_dir / BUILD).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return meta


# ---------------------------------------------------------------------------
# measure A
# ---------------------------------------------------------------------------


def selfneg_record(set_name: str, row: Mapping[str, Any], truth: str) -> dict[str, Any]:
    body = str(row["body"])
    lines = body.splitlines()
    hits = []
    for h in detect_self_negation(body):
        hits.append(
            {
                "pattern": h.pattern,
                "line_no": h.line_no,
                "match": h.match,
                "line": h.line,
                "before": lines[h.line_no - 2] if h.line_no >= 2 else "",
                "after": lines[h.line_no] if h.line_no < len(lines) else "",
                "looks": looks_quoted_or_fenced(lines, h.line_no),
            }
        )
    return {
        "set": set_name,
        "project": row["project"],
        "thread": row["thread"],
        "msg_id": row["msg_id"],
        "author": row.get("author"),
        "truth": truth,
        "hits": hits,
    }


def measure_a(out_dir: Path) -> list[dict[str, Any]]:
    cb = read_jsonl(out_dir / CASEBOOK)
    known = {(r["project"], r["msg_id"]): r["truth"] for r in cb}
    recs = [selfneg_record("i", r, str(r["truth"])) for r in cb]
    recs += [
        selfneg_record("ii", r, str(r["consensus"])) for r in read_jsonl(out_dir / GENUINE_FULLTEXT)
    ]
    recs += [
        selfneg_record("iii", r, known.get((r["project"], r["msg_id"]), "unlabelled"))
        for r in read_jsonl(out_dir / NEXT_HUMAN)
    ]
    write_jsonl(out_dir / SELFNEG, recs)
    return recs


# ---------------------------------------------------------------------------
# measure B
# ---------------------------------------------------------------------------


def questions_for(variant: str, rules: TierCRules) -> dict[str, dict[str, Any]]:
    """The live v3 set, or v3 with :data:`V4_CANDIDATE_SENTENCE` appended to the
    ``should_ask_human`` frame. Nothing else differs."""
    q = tierc_v2_questions(rules)
    if variant == V3:
        return q
    if variant == V4_CANDIDATE:
        sa = dict(q[SHOULD_ASK_HUMAN_KEY])
        sa["instructions"] = str(sa["instructions"]) + V4_CANDIDATE_SENTENCE
        q[SHOULD_ASK_HUMAN_KEY] = sa
        return q
    raise ValueError(f"unknown variant {variant!r}")


def original_state(row: Mapping[str, Any], logged: Sequence[Mapping[str, Any]]) -> str:
    """The logged ``state_wire`` of a casebook row, byte for byte (msg-6664 §1). The committed
    ``jev_input`` is redacted, so the original is re-read from the conductor log and must match
    ``jev_input_sha256``; anything else stops the run."""
    wire = str(logged_row_for(logged, str(row["thread"]), str(row["msg_id"]))["state_wire"])
    if sha256_text(wire) != row["jev_input_sha256"]:
        raise CasebookError(f"{row['msg_id']}: logged state_wire does not match jev_input_sha256")
    if redact_infra(wire) != row["jev_input"]:
        raise CasebookError(f"{row['msg_id']}: committed jev_input is not the redacted log row")
    return wire


def request_for(state: str, variant: str, rules: TierCRules) -> dict[str, Any]:
    """``state`` is the logged ``state_wire`` (:func:`original_state`); never rebuilt."""
    return {
        "state": state,
        "questions": questions_for(variant, rules),
        "policy": POLICY_REPLAY_TIERC,
        "questions_version": variant,
    }


@dataclass(frozen=True)
class Step:
    run: int
    msg_id: str
    variant: str


def plan(msg_ids: Sequence[str], runs: int, seed: int) -> list[Step]:
    """Run by run; inside a run every (row, variant) pair once, in a seeded shuffle."""
    rng = random.Random(seed)
    steps: list[Step] = []
    for run in range(1, runs + 1):
        pairs = [Step(run, m, v) for m in msg_ids for v in VARIANTS]
        rng.shuffle(pairs)
        steps.extend(pairs)
    return steps


def noul_of(answers: Any) -> float | None:
    entry = answers.get(SHOULD_ASK_HUMAN_KEY) if isinstance(answers, Mapping) else None
    value = entry.get("noul") if isinstance(entry, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


async def measure_b(
    out_dir: Path, rules: TierCRules, runs: int, seed: int, logs: Sequence[Path]
) -> int:
    from spirrow_mindwire.adapters.decider_lexora import DECIDER_TIMEOUT_SECONDS
    from spirrow_mindwire.lexora.client import LexoraClient, LexoraError

    if rules.sha256 != REGISTERED_RULES_SHA256:
        raise CasebookError(f"rules sha256 {rules.sha256} is not the registered one")
    cb = {r["msg_id"]: r for r in read_jsonl(out_dir / CASEBOOK)}
    lines: list[str] = []
    for p in logs:
        lines.extend(p.read_text(encoding="utf-8", errors="replace").splitlines())
    logged = decider_rows(lines)
    states = {m: original_state(r, logged) for m, r in cb.items()}
    path = out_dir / JEV
    done = (
        {
            (r["run"], r["msg_id"], r["variant"])
            for r in read_jsonl(path)
            if r.get("score") is not None
        }
        if path.is_file()
        else set()
    )
    calls = 0
    async with LexoraClient(timeout_seconds=DECIDER_TIMEOUT_SECONDS) as client:
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            for s in plan(list(cb), runs, seed):
                if (s.run, s.msg_id, s.variant) in done:
                    continue
                body = request_for(states[s.msg_id], s.variant, rules)
                rec: dict[str, Any] = {
                    "run": s.run,
                    "msg_id": s.msg_id,
                    "variant": s.variant,
                    "request_sha256": hashlib.sha256(
                        json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                }
                try:
                    payload = await client.decide(body)
                except LexoraError as exc:
                    rec |= {"error": f"{type(exc).__name__}: {exc}", "score": None}
                else:
                    answers = payload.get("answers")
                    mr = answers.get("matched_rule") if isinstance(answers, Mapping) else None
                    rec |= {
                        "decision_id": payload.get("decision_id"),
                        "provider": payload.get("provider"),
                        "latency_ms": payload.get("latency_ms"),
                        "score": noul_of(answers),
                        "matched_rule": mr.get("choice") if isinstance(mr, Mapping) else None,
                    }
                calls += 1
                fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
                fh.flush()
                if rec.get("provider") not in (None, EXPECTED_PROVIDER) or (
                    "error" not in rec and rec["score"] is None
                ):
                    raise CasebookError(f"contract mismatch, stopping: {rec}")
    return calls


# ---------------------------------------------------------------------------
# decision rule (msg-6666 §4, fixed before measurement)
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    outcome: str  # "B" | "A" | "observe"
    b_holds: bool
    a_holds: bool | None  # None: not evaluated because B held
    reasons: list[str] = field(default_factory=list)


def decide(
    casebook: Sequence[Mapping[str, Any]],
    jev: Sequence[Mapping[str, Any]],
    selfneg: Sequence[Mapping[str, Any]],
    runs: int = 3,
) -> Decision:
    """msg-6666 §4, in order:

    1. B holds iff the negation sentence was inside Jev's input (msg-6666 補足) and, on the
       ``tierc-v4-candidate`` set, every one of ``runs`` scores of the self-negation row is
       ``< 0.40`` and every one of ``runs`` scores of every genuine casebook row is ``>= 0.40``.
       B → A is discarded, not evaluated.
    2. Only when B does not hold: A holds iff the self-negation row has ≥ 1 hit and the genuine
       casebook rows and every set (ii) row have 0 hits.
    3. Neither → keep observing.

    A missing or errored score counts as "not met": the rule needs all ``runs`` answers."""
    reasons: list[str] = []
    selfneg_rows = [r for r in casebook if r["failure_type"] == SELF_NEGATION]
    genuine_rows = [r for r in casebook if r["truth"] in GENUINE_CLASSES]
    if len(selfneg_rows) != 1:
        raise CasebookError(f"expected 1 self-negation row, got {len(selfneg_rows)}")
    sn = selfneg_rows[0]

    def scores(msg_id: str) -> list[float | None]:
        got = [
            r.get("score") for r in jev if r["msg_id"] == msg_id and r["variant"] == V4_CANDIDATE
        ]
        return [float(s) if isinstance(s, int | float) else None for s in got]

    b_holds = True
    if not sn.get("negation_in_jev_input", False):
        b_holds = False
        reasons.append(
            f"B: the negation sentence of {sn['msg_id']} sits at offset {sn.get('negation_offset')}"
            f", outside Jev's {BODY_HEAD_M}-char input — B cannot hold (cause: input range)"
        )
    sn_scores = scores(str(sn["msg_id"]))
    if len(sn_scores) != runs or any(s is None or s >= DEFAULT_V2_NOT_ASK_MAX for s in sn_scores):
        b_holds = False
        reasons.append(
            f"B: {sn['msg_id']} on {V4_CANDIDATE} = {sn_scores}; "
            f"needs {runs} x < {DEFAULT_V2_NOT_ASK_MAX}"
        )
    for g in genuine_rows:
        gs = scores(str(g["msg_id"]))
        if len(gs) != runs or any(s is None or s < DEFAULT_V2_NOT_ASK_MAX for s in gs):
            b_holds = False
            reasons.append(
                f"B: genuine {g['msg_id']} on {V4_CANDIDATE} = {gs}; "
                f"needs {runs} x >= {DEFAULT_V2_NOT_ASK_MAX}"
            )
    if b_holds:
        return Decision("B", True, None, ["B holds; A is discarded (msg-6666 §4-1)"])

    def hits(set_name: str, msg_id: str | None = None) -> list[Mapping[str, Any]]:
        return [
            r
            for r in selfneg
            if r["set"] == set_name and (msg_id is None or r["msg_id"] == msg_id) and r["hits"]
        ]

    a_holds = True
    if not hits("i", str(sn["msg_id"])):
        a_holds = False
        reasons.append(f"A: no hit on {sn['msg_id']}")
    gen_ids = {str(g["msg_id"]) for g in genuine_rows}
    gen_hit = [r for r in hits("i") if r["msg_id"] in gen_ids]
    if gen_hit:
        a_holds = False
        reasons.append(f"A: hit on genuine casebook rows {[r['msg_id'] for r in gen_hit]}")
    ii_hit = hits("ii")
    if ii_hit:
        a_holds = False
        reasons.append(
            f"A: hit on set (ii) genuine rows {[(r['thread'], r['msg_id']) for r in ii_hit]}"
        )
    if a_holds:
        reasons.append("A holds (msg-6666 §4-2): PR2 (log-only check) follows")
        return Decision("A", False, True, reasons)
    reasons.append("neither holds (msg-6666 §4-3): keep observing")
    return Decision("observe", False, False, reasons)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


OUTCOME_TEXT = {"B": "B adopted", "A": "A adopted", "observe": "neither — keep observing"}


def _fmt(xs: Sequence[float | None]) -> str:
    return " / ".join("—" if x is None else f"{x:.2f}" for x in xs)


def _cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ").strip()


def render_report(out_dir: Path, runs: int) -> str:
    meta = json.loads((out_dir / BUILD).read_text(encoding="utf-8"))
    cb = read_jsonl(out_dir / CASEBOOK)
    sn = read_jsonl(out_dir / SELFNEG)
    jev = read_jsonl(out_dir / JEV) if (out_dir / JEV).is_file() else []
    d = decide(cb, jev, sn, runs)
    md: list[str] = []
    md += [
        "# Tier-C casebook 2026-10-08 — report",
        "",
        "Thread `T-tierc-overpass-self-contradicting-escalations`. "
        "Design: Bohr msg-6664 / msg-6666 / "
        "msg-6668, approved by Einstein. Truth: operator audit msg-6653 (`audit-operator`). "
        "Exploration under `eval/tierc/shadow-prereg.md` §6 — "
        "never part of the shadow count, labels "
        "or threshold decision.",
        "",
        f"## Result: **{OUTCOME_TEXT[d.outcome]}**",
        "",
    ]
    md += [f"- {r}" for r in d.reasons]
    md += [
        "",
        f"Thresholds unchanged: not_ask_max = {DEFAULT_V2_NOT_ASK_MAX}, "
        f"ask_min = {DEFAULT_V2_ASK_MIN}.",
        "",
        "## Casebook (7 rows, the input Jev was sent)",
        "",
        "| msg | thread | author | label | logged ask_score | logged verdict | truth "
        "| failure_type | evidence |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in cb:
        md.append(
            f"| {r['project']}/{r['msg_id']} | {r['thread']} | {r['author']} | {r['label']} | "
            f"{r['ask_score']} | {r['logged_verdict_kind']} | {r['truth']} | {r['failure_type']} | "
            f"{', '.join(r['evidence'])} |"
        )
    md += ["", "## Was the negating sentence inside Jev's input? (msg-6664 §1)", ""]
    for r in cb:
        if "negation_sentence" in r:
            md.append(
                f"- {r['msg_id']}: “{r['negation_sentence']}” is at character offset "
                f"{r['negation_offset']} of a {r['body_chars']}-char body; "
                f"Jev's `head_summary` is the "
                f"first {r['jev_head_chars']} chars (head is a prefix of the body: "
                f"{r['head_is_body_prefix']}). "
                f"Inside Jev's input: **{r['negation_in_jev_input']}**."
            )
    md += [
        "",
        "## Measurement A — `detect_self_negation` (whole body, msg-6666 Objection 1)",
        "",
        f"Inputs: (i) casebook {meta['counts'][CASEBOOK]}; (ii) fulltext-2026-09-28 consensus "
        f"genuine* {meta['counts'][GENUINE_FULLTEXT]}; (iii) every roster `NEXT: human` from "
        f"{meta['since']} to {meta['as_of']}: {meta['counts'][NEXT_HUMAN]}.",
        "",
        "| set | rows | rows with ≥1 hit |",
        "|---|---|---|",
    ]
    for s in ("i", "ii", "iii"):
        rows = [r for r in sn if r["set"] == s]
        md.append(f"| {s} | {len(rows)} | {sum(1 for r in rows if r['hits'])} |")
    md += [
        "",
        "Every hit (the `looks` column is display only — msg-6666; no decision reads it):",
        "",
        "| set | msg | author | truth | pattern | line before | hit line | line after | looks |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sn:
        for h in r["hits"]:
            md.append(
                f"| {r['set']} | {r['project']}/{r['thread']}/{r['msg_id']} | {r['author']} | "
                f"{r['truth']} | {h['pattern']} | {_cell(h['before'])[:120]} | "
                f"{_cell(h['line'])[:200]} | {_cell(h['after'])[:120]} | {h['looks']} |"
            )
    md += [
        "",
        f"## Measurement B — Jev, {runs} runs x 7 rows x ({V3}, {V4_CANDIDATE})",
        "",
        f"Calls recorded: {len(jev)}; "
        f"with a score: {sum(1 for r in jev if r.get('score') is not None)}; "
        f"providers: {sorted({str(r.get('provider')) for r in jev})}.",
        "",
        f"| msg | truth | logged (live) | {V3} runs | {V4_CANDIDATE} runs |",
        "|---|---|---|---|---|",
    ]
    for r in cb:

        def sc(v: str, mid: str = str(r["msg_id"])) -> list[float | None]:
            got = sorted(
                (x for x in jev if x["msg_id"] == mid and x["variant"] == v), key=lambda x: x["run"]
            )
            return [x.get("score") for x in got]

        md.append(
            f"| {r['msg_id']} | {r['truth']} | {r['ask_score']} | {_fmt(sc(V3))} "
            f"| {_fmt(sc(V4_CANDIDATE))} |"
        )
    md += [
        "",
        "## Fixed in advance, and the implementer's choices",
        "",
        "- Decision rule: msg-6666 §4 (B → A → observe, mutually exclusive), coded as "
        "`tierc_casebook.decide` before any measurement was run.",
        "- Detector patterns: msg-6664 §2, verbatim. Implementer's choices made before the run: "
        "case-insensitive; one line at a time; the excluded-line prefixes are matched at column 0.",
        f"- `{V4_CANDIDATE}` = the live `{V3}` set with this sentence appended to the "
        f"`should_ask_human` frame: 「{V4_CANDIDATE_SENTENCE}」",
        "- Set (ii) takes all three genuine classes (genuine / genuine-merge / genuine-action), "
        "the `genuine*` of `report.fulltext.md`.",
        "- Set (iii) is read from the chatroom (every roster message whose last `NEXT:` "
        "is human), not "
        "from the conductor log, so it includes turns the label gate bounced.",
        "- msg-754 (false premise) is out of Jev's scope by design (msg-6664 §3): its score is "
        "recorded, no detection is expected, and no decision uses it.",
        "- Committed texts are redacted (`redact_infra`: IPv4 addresses and the Windows "
        "user-profile name), because this repository is public; each keeps the sha256 of its "
        "original. Measurement A ran on the redacted texts (none of the patterns involve those "
        "values). Measurement B sent the original logged `state_wire`, re-read from the conductor "
        "log and checked against `jev_input_sha256`.",
    ]
    return "\n".join(md) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--dir", type=Path, required=True)
    b.add_argument("--log", type=Path, action="append", required=True)
    b.add_argument("--project", action="append", required=True)
    b.add_argument("--since", required=True, help="timezone-aware ISO 8601")
    b.add_argument("--as-of", required=True, help="timezone-aware ISO 8601")
    a = sub.add_parser("measure-a")
    a.add_argument("--dir", type=Path, required=True)
    j = sub.add_parser("measure-b")
    j.add_argument("--dir", type=Path, required=True)
    j.add_argument("--rules", type=Path, required=True)
    j.add_argument("--log", type=Path, action="append", required=True)
    j.add_argument("--runs", type=int, default=3)
    j.add_argument("--seed", type=int, default=20261008)
    r = sub.add_parser("report")
    r.add_argument("--dir", type=Path, required=True)
    r.add_argument("--runs", type=int, default=3)
    args = p.parse_args(argv)

    if args.cmd == "build":
        from spirrow_mindwire.config import load_settings

        roster = list(load_settings().conductor.roster)
        meta = asyncio.run(
            build(
                args.dir, args.log, args.project, parse_ts(args.since), parse_ts(args.as_of), roster
            )
        )
        print(json.dumps(meta["counts"]))
    elif args.cmd == "measure-a":
        recs = measure_a(args.dir)
        print(f"{len(recs)} rows, {sum(1 for x in recs if x['hits'])} with hits")
    elif args.cmd == "measure-b":
        n = asyncio.run(
            measure_b(args.dir, load_tierc_rules(args.rules), args.runs, args.seed, args.log)
        )
        print(f"{n} calls")
    else:
        (args.dir / REPORT).write_text(
            render_report(args.dir, args.runs), encoding="utf-8", newline="\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
