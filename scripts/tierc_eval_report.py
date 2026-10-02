"""Tier-C replay evaluation report — Jev's answers joined to the double-blind labels.

Spec: T-decider-tierc-replay-eval — Bohr msg-4219 §0 / §3 (metrics), msg-4224 (two labellers,
agreement → truth, disagreement → ambiguous, 3-way sensitivity, Cohen's κ), msg-4226 §6 (state
that the gate is today's rules; count ``current_fallback`` rosters), Fermi msg-4221 (put the
(A)/(B) split at the top).

**Inputs** (all JSONL, joined on ``(thread_id, round_index)``):

* ``--replay`` — ``decider_replay.py --endpoint`` output (records carrying ``decision``);
* ``--fixture`` — ``build_tierc_eval_fixture.py`` output (gate result, author, ``live_entry``,
  ``roster_source``);
* ``--labels NAME=PATH`` — repeatable; one filled label file per labeller;
* ``--corrections PATH`` — optional; a ``build_tierc_eval_fixture.py --corrections-out`` file
  naming which fixture rows are real escalations (msg-4298 / msg-4302). It is bound to one
  fixture by sha and only ever *removes* rows; see :func:`load_corrections`.

**Verdicts are recomputed** from each record's ``raw_answers`` with ``evaluate_tierc`` (the pure
§4.4 function; thresholds settable) for every row — in-gate and out-of-gate alike — because (A)
asks what Jev *would* say. The server's own ``decision.verdict.kind`` is shown beside it.

**Label vocabulary** (``label`` field; msg-4219 §1):

* ``genuine`` — GOAL / COST / IRREVERSIBLE: the headline recall;
* ``genuine-merge`` — a merge that needed a human; reported on its own row (D9 keeps merge out of
  the Decider's vocabulary, so it is not mixed into the headline);
* ``genuine-action`` — a human-only action; its own row;
* ``spurious`` — IMPL / ROUTING_ARTIFACT / a self-doable action / a loop-mergeable merge;
* ``ambiguous`` — excluded from headline numbers, counted with reasons.

"Human reached" = CONFIRMED or UNSURE (D2 monotonicity: UNSURE goes to the human).

**Two question versions (T-decider-conductor-hook msg-4639 DECIDED 2d-5).** A record whose
``questions_version`` is in :data:`V2_STRUCTURE_VERSIONS` (``tierc-v2`` / ``tierc-v3``, msg-5753
DECIDED 2d-15 item 3) is recomputed from ``raw_answers["should_ask_human"]`` with
``evaluate_tierc_v2`` (``TierCV2Thresholds``); every other record takes the v1 path above,
unchanged. v1 and v2 rows get separate sections — headline, sweep and AUC per version. An
input with no v2 row renders exactly as before (the committed ``eval/tierc/report.md`` is
pinned byte for byte). The v2 section applies msg-4639 DECIDED 2d-4: a single ``NO_VERDICT``
among its rows makes the set **INVALID** and no headline is computed; ``MALFORMED`` is counted.

**One version per run, and the export lock (msg-4648 / msg-4650 DECIDED 2d-11).** Straight after
reading ``--replay`` the report refuses a mix of ``tierc-v2`` and other records ("a run is v1
only or v2 only"). A v2 run requires ``--export-manifest PATH`` — the exporter's ``export.json``,
no default and never guessed from a location — and refuses unless the sha256 of ``--replay``,
``--fixture`` and the manifest's ``materials.jsonl`` (beside the manifest) all equal the
manifest's. ``as_of`` is not compared (nothing else carries it); it is printed at the top of the
v2 section with the three hashes and the manifest's own sha256. A v1 run given
``--export-manifest`` is an error: nothing would be checked, so nothing may claim it was.

**Reader of the output.** Markdown on stdout / ``--out``. Nothing is posted anywhere.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spirrow_mindwire.adapters.decider_lexora import _extract_noul
from spirrow_mindwire.decider.questions import SHOULD_ASK_HUMAN_KEY
from spirrow_mindwire.decider.verdict import (
    TIER_C_GENUINE_KEYS,
    TIER_C_SPURIOUS_KEYS,
    TierCThresholds,
    TierCV2Thresholds,
    TierCVerdictKind,
    evaluate_tierc,
    evaluate_tierc_v2,
)

_reconfigure_out = getattr(sys.stdout, "reconfigure", None)
if _reconfigure_out is not None:
    _reconfigure_out(encoding="utf-8", errors="backslashreplace")

LABELS: tuple[str, ...] = ("genuine", "genuine-merge", "genuine-action", "spurious", "ambiguous")
GENUINE_CLASSES: tuple[str, ...] = ("genuine", "genuine-merge", "genuine-action")
VERDICTS: tuple[str, ...] = ("CONFIRMED", "UNSURE", "LIKELY_NOT", "NO_VERDICT")

Key = tuple[str, int]


def _key(row: Mapping[str, Any]) -> Key:
    return (str(row["thread_id"]), int(row["round_index"]))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(json.loads(s))
    return out


# ---------------------------------------------------------------------------
# corrections (msg-4298 §2, msg-4302)
# ---------------------------------------------------------------------------

UNCORRECTED_FIXTURE_SHA256 = "280bdd49ea725541570dc60c8f58cbcc4b59b6cc7f7e09b418c879ac02cc69ad"
"""The 2026-09-28 ``fixture.jsonl`` (``manifest.json``'s lock). 143 of its 209 rows are not
escalations: the builder selected them against an empty roster (PR-gate #349). Reporting it
without ``--corrections`` would silently re-issue the wrong headline, so that one input is
refused (msg-4302 §2). Every other fixture — including every fresh one — runs with or without
the flag."""

CORRECTIONS_SCHEMA = 1
CORRECTIONS_PATH_2026_09_28 = "eval/tierc/corrections/2026-09-28-roster-selection.json"


class CorrectionsError(ValueError):
    """The corrections file does not fit the fixture it is applied to. Always fatal."""


class InputError(ValueError):
    """The inputs cannot be evaluated as one run (mixed versions, export lock). Always fatal."""


@dataclass(frozen=True)
class ExportLock:
    """The verified ``export.json`` (msg-4650): what the v2 section prints at its top."""

    path: str
    sha256: str
    as_of: str
    materials_sha256: str
    fixture_sha256: str
    replay_sha256: str


def record_version(rec: Mapping[str, Any]) -> str:
    d = rec.get("decision") if isinstance(rec.get("decision"), Mapping) else {}
    assert isinstance(d, Mapping)
    return str(rec.get("questions_version") or d.get("questions_version") or "")


def run_version(replay: Sequence[Mapping[str, Any]]) -> str:
    """``"v2"`` when every record has the v2 structure (:data:`V2_STRUCTURE_VERSIONS`), ``"v1"``
    when none does; a mix is an :class:`InputError` (msg-4650: one evaluation is v1 only or v2
    only)."""
    v2 = sum(1 for r in replay if record_version(r) in V2_STRUCTURE_VERSIONS)
    if v2 and v2 != len(replay):
        raise InputError(
            f"--replay mixes {v2} tierc-v2/v3 record(s) with {len(replay) - v2} other(s); "
            "one evaluation run is v1 only or v2 only"
        )
    return "v2" if v2 else "v1"


def verify_export_lock(manifest: Path, fixture: Path, replay: Path) -> ExportLock:
    """msg-4650 DECIDED 2d-11: the three sha256 must equal ``export.json``'s; ``as_of`` is read,
    not compared."""
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        files = raw["files"]
        want = {k: str(files[k]["sha256"]) for k in ("materials", "fixture", "replay")}
        materials = manifest.parent / str(files["materials"]["name"])
        as_of = str(raw["as_of"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InputError(f"{manifest}: not an export.json: {exc}") from exc
    got = {"materials": materials, "fixture": fixture, "replay": replay}
    for key, path in got.items():
        try:
            sha = file_sha256(path)
        except OSError as exc:
            raise InputError(f"{key} {path}: unreadable: {exc}") from exc
        if sha != want[key]:
            raise InputError(
                f"{key} {path} has sha256 {sha}, but {manifest} records {want[key]}; "
                "the export was changed after it was written"
            )
    return ExportLock(
        path=str(manifest),
        sha256=file_sha256(manifest),
        as_of=as_of,
        materials_sha256=want["materials"],
        fixture_sha256=want["fixture"],
        replay_sha256=want["replay"],
    )


@dataclass(frozen=True)
class Corrections:
    path: str
    sha256: str
    keep: frozenset[Key]
    exclude: frozenset[Key]
    selection_code_commit: str


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_corrections(
    path: Path, fixture: Sequence[Mapping[str, Any]], fixture_sha256: str
) -> Corrections:
    """Read ``path`` and check it against the fixture it is about to filter.

    Raises :class:`CorrectionsError` when the file is unreadable or not schema 1, was written for
    another fixture (sha), names a row the fixture lacks or whose ``msg_id`` differs, lists a row
    as both kept and excluded, or leaves a fixture row in neither list (a partial file must not
    decide by omission).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorrectionsError(f"{path}: cannot read corrections: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != CORRECTIONS_SCHEMA:
        raise CorrectionsError(f"{path}: not a schema-{CORRECTIONS_SCHEMA} corrections file")
    if raw.get("fixture_sha256") != fixture_sha256:
        raise CorrectionsError(
            f"{path}: written for fixture sha {raw.get('fixture_sha256')!r}, "
            f"but the fixture read has sha {fixture_sha256!r}"
        )
    msg_id_of = {_key(r): str(r.get("msg_id", "")) for r in fixture}

    def keys(field: str) -> frozenset[Key]:
        entries = raw.get(field)
        if not isinstance(entries, list):
            raise CorrectionsError(f"{path}: {field!r} must be a list")
        out: set[Key] = set()
        for e in entries:
            k = _key(e)
            if k not in msg_id_of:
                raise CorrectionsError(f"{path}: {field} names {k}, which is not in the fixture")
            if msg_id_of[k] != str(e.get("msg_id", "")):
                raise CorrectionsError(
                    f"{path}: {field} {k} has msg_id {e.get('msg_id')!r}; "
                    f"the fixture has {msg_id_of[k]!r}"
                )
            out.add(k)
        return frozenset(out)

    keep, exclude = keys("keep"), keys("exclude")
    if both := keep & exclude:
        raise CorrectionsError(f"{path}: kept and excluded at once: {sorted(both)}")
    if unlisted := set(msg_id_of) - keep - exclude:
        raise CorrectionsError(f"{path}: fixture rows in neither list: {sorted(unlisted)}")
    return Corrections(
        path=str(path),
        sha256=file_sha256(path),
        keep=keep,
        exclude=exclude,
        selection_code_commit=str(raw.get("selection_code_commit", "")),
    )


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Label:
    label: str
    category: str | None
    rationale: str


def load_labels(rows: Iterable[Mapping[str, Any]]) -> dict[Key, Label]:
    """One labeller's file → ``{key: Label}``. Blank ``label`` rows are skipped; an unknown
    label value raises (a typo must not silently become "not labelled")."""
    out: dict[Key, Label] = {}
    for r in rows:
        raw = r.get("label")
        if raw is None or raw == "":
            continue
        if raw not in LABELS:
            raise ValueError(f"{_key(r)}: unknown label {raw!r} (allowed: {LABELS})")
        cat = r.get("category")
        out[_key(r)] = Label(
            # RUBRIC: category OTHER counts as ambiguous, whatever the label says (msg-4229 §3-3).
            label="ambiguous" if cat == "OTHER" else str(raw),
            category=str(cat) if cat else None,
            rationale=str(r.get("rationale") or ""),
        )
    return out


def consensus(labellers: Mapping[str, Mapping[Key, Label]]) -> dict[Key, str]:
    """msg-4224: every labeller agrees → that label; any disagreement or a missing label →
    ``ambiguous``. A key only one file carries is ambiguous (not independently labelled)."""
    keys: set[Key] = set()
    for m in labellers.values():
        keys |= set(m)
    out: dict[Key, str] = {}
    for k in keys:
        vals = {m[k].label if k in m else None for m in labellers.values()}
        out[k] = vals.pop() if len(vals) == 1 and None not in vals else "ambiguous"
    return out


def cohen_kappa(a: Mapping[Key, str], b: Mapping[Key, str]) -> tuple[float | None, int]:
    """Cohen's κ over the keys both labellers labelled. ``None`` when undefined."""
    keys = sorted(set(a) & set(b))
    n = len(keys)
    if n == 0:
        return None, 0
    po = sum(1 for k in keys if a[k] == b[k]) / n
    ca = Counter(a[k] for k in keys)
    cb = Counter(b[k] for k in keys)
    pe = sum((ca[c] / n) * (cb[c] / n) for c in set(ca) | set(cb))
    if pe >= 1.0:
        return None, n
    return (po - pe) / (1 - pe), n


# ---------------------------------------------------------------------------
# answers / verdicts
# ---------------------------------------------------------------------------


def scores_of(record: Mapping[str, Any]) -> dict[str, float] | None:
    decision = record.get("decision")
    if not isinstance(decision, Mapping):
        return None
    scores, _ = _extract_noul(decision.get("raw_answers"))
    return scores


def ask_score_of(record: Mapping[str, Any]) -> float | None:
    """v2: ``raw_answers["should_ask_human"]["noul"]``, or ``None`` when absent / malformed."""
    decision = record.get("decision")
    if not isinstance(decision, Mapping):
        return None
    scores, _ = _extract_noul(decision.get("raw_answers"), (SHOULD_ASK_HUMAN_KEY,))
    return None if scores is None else scores[SHOULD_ASK_HUMAN_KEY]


def recompute(scores: Mapping[str, float] | None, th: TierCThresholds) -> tuple[str, str | None]:
    if scores is None:
        return "NO_VERDICT", None
    v = evaluate_tierc(scores, th)
    return v.kind.value, v.fired_reason


def reaches_human(verdict: str) -> bool:
    """D2: CONFIRMED and UNSURE reach the human; a missing verdict also does (fail-safe)."""
    return verdict != TierCVerdictKind.LIKELY_NOT.value


@dataclass(frozen=True)
class Row:
    key: Key
    msg_id: str
    author: str
    project: str
    gate_bucket: str
    grey_zone: bool
    live_entry: bool
    roster_source: str
    eval_set: str
    body_head: str
    scores: dict[str, float] | None
    server_verdict: str | None
    outcome: str | None
    latency_ms: int | None
    questions_version: str = ""
    ask_score: float | None = None
    following_n: int | None = None
    rules_sha256: str | None = None


def gate_bucket(g: Mapping[str, Any] | None) -> str:
    if g is None:
        return "gate_none"
    if g.get("verdict") == "bounce":
        return f"BOUNCED({g.get('bounce_reason')})"
    if g.get("kind"):
        return str(g["kind"])
    return f"ADMIT({g.get('label')})"


def join(replay: Sequence[Mapping[str, Any]], fixture: Sequence[Mapping[str, Any]]) -> list[Row]:
    fx = {_key(r): r for r in fixture}
    rows: list[Row] = []
    for rec in replay:
        k = _key(rec)
        f = fx.get(k, {})
        state = rec.get("state") or {}
        g = state.get("gate_result")
        d = rec.get("decision") if isinstance(rec.get("decision"), Mapping) else {}
        assert isinstance(d, Mapping)
        sv = d.get("verdict")
        rows.append(
            Row(
                key=k,
                msg_id=str(f.get("msg_id", "")),
                author=str(f.get("author", "")),
                project=str(f.get("project", "")),
                gate_bucket=gate_bucket(g),
                grey_zone=bool(g and g.get("is_grey_zone")),
                live_entry=bool(f.get("live_entry")),
                roster_source=str(f.get("roster_source", "")),
                eval_set=str(f.get("eval_set", "")),
                body_head=str(state.get("head_summary", ""))[:160].replace("\n", " "),
                scores=scores_of(rec),
                server_verdict=str(sv["kind"]) if isinstance(sv, Mapping) else None,
                outcome=str(d["outcome"]) if d.get("outcome") else None,
                latency_ms=int(d["latency_ms"]) if d.get("latency_ms") is not None else None,
                questions_version=str(
                    rec.get("questions_version") or d.get("questions_version") or ""
                ),
                ask_score=ask_score_of(rec),
                following_n=int(f["following_n"]) if f.get("following_n") is not None else None,
                rules_sha256=_rules_sha256_of(rec, d),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def _pct(num: int, den: int) -> str:
    return "n/a (0)" if den == 0 else f"{num}/{den} = {100 * num / den:.1f}%"


def headline(rows: Sequence[Row], truth: Mapping[Key, str], th: TierCThresholds) -> dict[str, str]:
    """Reduction (spurious → LIKELY_NOT), the §4.5 bounce share, and recall per genuine class."""
    out: dict[str, str] = {}
    spur = [r for r in rows if truth.get(r.key) == "spurious"]
    ln = [r for r in spur if recompute(r.scores, th)[0] == "LIKELY_NOT"]
    bounce = [r for r in ln if recompute(r.scores, th)[1] == "answerable_from_thread"]
    out["reduction (spurious → LIKELY_NOT)"] = _pct(len(ln), len(spur))
    out["  of which fired_reason=answerable_from_thread (§4.5 bounce)"] = _pct(
        len(bounce), len(spur)
    )
    for cls in GENUINE_CLASSES:
        g = [r for r in rows if truth.get(r.key) == cls]
        hit = [r for r in g if reaches_human(recompute(r.scores, th)[0])]
        out[f"recall {cls} (reaches human)"] = _pct(len(hit), len(g))
    out["ambiguous (excluded)"] = str(sum(1 for r in rows if truth.get(r.key) == "ambiguous"))
    out["unlabelled (excluded)"] = str(sum(1 for r in rows if r.key not in truth))
    return out


def confusion(
    rows: Sequence[Row], truth: Mapping[Key, str], th: TierCThresholds, by: str | None = None
) -> dict[str, Counter[tuple[str, str]]]:
    tables: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    for r in rows:
        t = truth.get(r.key, "unlabelled")
        group = "all" if by is None else str(getattr(r, by))
        tables[group][(t, recompute(r.scores, th)[0])] += 1
    return dict(tables)


def auc(pos: Sequence[float], neg: Sequence[float]) -> float | None:
    """Mann-Whitney AUC: P(score(pos) > score(neg)), ties count half."""
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for q in neg:
            wins += 1.0 if p > q else 0.5 if p == q else 0.0
    return wins / (len(pos) * len(neg))


def calibration(
    rows: Sequence[Row], truth: Mapping[Key, str]
) -> tuple[list[tuple[str, int, int]], float | None]:
    """Spurious-max bins (label, n, n spurious) and AUC of the genuine sum (genuine* vs
    spurious)."""
    bins = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]
    table: list[tuple[str, int, int]] = []
    scored = [
        r for r in rows if r.scores is not None and truth.get(r.key) not in (None, "ambiguous")
    ]
    for lo, hi in bins:
        inb = [
            r
            for r in scored
            if r.scores is not None and lo <= max(r.scores[k] for k in TIER_C_SPURIOUS_KEYS) < hi
        ]
        table.append(
            (
                f"[{lo:.1f},{min(hi, 1.0):.1f}{']' if hi > 1 else ')'}",
                len(inb),
                sum(1 for r in inb if truth[r.key] == "spurious"),
            )
        )
    # ``math.fsum``, not ``sum``: the AUC counts exact ties, and 3.12+ ``sum`` (compensated) and
    # 3.11 ``sum`` (plain) land on different last bits — 0.705 vs 0.706 on the committed replay.
    # ``fsum`` is correctly rounded on every version and gives the committed 0.705.
    pos = [
        math.fsum(r.scores[k] for k in TIER_C_GENUINE_KEYS)
        for r in scored
        if r.scores is not None and truth[r.key] in GENUINE_CLASSES
    ]
    neg = [
        math.fsum(r.scores[k] for k in TIER_C_GENUINE_KEYS)
        for r in scored
        if r.scores is not None and truth[r.key] == "spurious"
    ]
    return table, auc(pos, neg)


SWEEP_GENUINE_MIN: tuple[float, ...] = (0.6, 0.9, 1.2, 1.5, 1.8)
SWEEP_SPURIOUS_MIN: tuple[float, ...] = (0.4, 0.5, 0.6, 0.7, 0.8)


def sweep(rows: Sequence[Row], truth: Mapping[Key, str]) -> list[tuple[float, float, str, str]]:
    """Threshold sweep (no extra calls — raw answers are on disk). ``genuine_max`` stays below
    ``genuine_min`` at its default ratio (0.40 / 0.60)."""
    out: list[tuple[float, float, str, str]] = []
    for gmin in SWEEP_GENUINE_MIN:
        for smin in SWEEP_SPURIOUS_MIN:
            th = TierCThresholds(genuine_min=gmin, genuine_max=gmin * 2 / 3, spurious_min=smin)
            h = headline(rows, truth, th)
            out.append(
                (
                    gmin,
                    smin,
                    h["reduction (spurious → LIKELY_NOT)"],
                    h["recall genuine (reaches human)"],
                )
            )
    return out


def misses(
    rows: Sequence[Row],
    truth: Mapping[Key, str],
    labellers: Mapping[str, Mapping[Key, Label]],
    th: TierCThresholds,
) -> list[str]:
    """msg-4219 §3-2: every genuine* judged LIKELY_NOT, one block each. The §6a loss line is
    left for a human to write — it is a judgement about the thread, not a computed number."""
    out: list[str] = []
    for r in rows:
        t = truth.get(r.key)
        if t not in GENUINE_CLASSES:
            continue
        verdict, fired = recompute(r.scores, th)
        if verdict != "LIKELY_NOT":
            continue
        why = "; ".join(
            f"{who}: {m[r.key].rationale}" for who, m in labellers.items() if r.key in m
        )
        sc = ", ".join(f"{k}={v:.2f}" for k, v in (r.scores or {}).items())
        out.append(
            f"- **{r.msg_id}** `{r.key[0]}` r{r.key[1]} ({t}) — fired_reason={fired}\n"
            f"  - scores: {sc}\n  - label rationale: {why or '(none)'}\n"
            f"  - body head: {r.body_head}\n  - §6a loss (if bounced): _to be written_"
        )
    return out


def _pctl(xs: Sequence[int], q: float) -> str:
    if not xs:
        return "n/a"
    s = sorted(xs)
    return str(s[min(len(s) - 1, round(q * (len(s) - 1)))])


def render(
    all_rows: Sequence[Row],
    labellers: Mapping[str, Mapping[Key, Label]],
    th: TierCThresholds,
    population: Sequence[Mapping[str, Any]] = (),
    corrections: Corrections | None = None,
    replayed_before_corrections: int | None = None,
    th_v2: TierCV2Thresholds | None = None,
    export: ExportLock | None = None,
    rules_snapshots: RulesSnapshotReport | None = None,
) -> str:
    """One version per run (msg-4639 DECIDED 2d-5, msg-4650). No v2 row → the v1 report,
    unchanged; all v2 → the v2 section; a mix is an :class:`InputError`."""
    v2 = [r for r in all_rows if r.questions_version in V2_STRUCTURE_VERSIONS]
    if not v2:
        return _render_v1(
            all_rows,
            labellers,
            th,
            population,
            corrections,
            replayed_before_corrections,
            rules_snapshots,
        )
    if len(v2) != len(all_rows):
        raise InputError("rows mix tierc-v2 with other versions; one run is v1 only or v2 only")
    return render_v2(
        v2,
        labellers,
        th_v2 if th_v2 is not None else TierCV2Thresholds(),
        corrections,
        replayed_before_corrections,
        export,
        rules_snapshots,
    )


def _render_v1(
    all_rows: Sequence[Row],
    labellers: Mapping[str, Mapping[Key, Label]],
    th: TierCThresholds,
    population: Sequence[Mapping[str, Any]] = (),
    corrections: Corrections | None = None,
    replayed_before_corrections: int | None = None,
    rules_snapshots: RulesSnapshotReport | None = None,
) -> str:
    """The v1 report (unchanged by 2d-5). With ``corrections``, ``all_rows`` / ``labellers``
    must already be filtered
    (:func:`apply_corrections`); this function only states that it happened (msg-4302 §4).
    The same holds for ``rules_snapshots`` (:func:`archive_rules_snapshots`)."""
    truth = consensus(labellers)
    lines: list[str] = ["# Tier-C replay evaluation — Jev", ""]
    if rules_snapshots is not None:
        lines += [*render_rules_snapshots(rules_snapshots), ""]
    if corrections is None:
        lines += ["- corrections: **none applied** — every fixture row is counted.", ""]
    else:
        lines += [
            f"- corrections: **applied** — `{corrections.path}` "
            f"(sha256 `{corrections.sha256}`, selection code "
            f"`{corrections.selection_code_commit}`); rows counted: "
            f"**{len(all_rows)} / {replayed_before_corrections}** replayed. "
            "The population line below is not corrected.",
            "",
        ]

    # Headline numbers are on the body set; supplement rows are reported on their own lines
    # (msg-4229 §1). A row with no eval_set (e.g. a hand-made fixture) counts as body.
    rows = [r for r in all_rows if r.eval_set in ("body", "")]
    supplement = [r for r in all_rows if r.eval_set == "supplement"]
    n = len(rows)
    grey = [r for r in all_rows if r.grey_zone]
    live = [r for r in rows if r.live_entry]
    pop_grey = sum(
        1 for p in population if p.get("gate_result") and p["gate_result"].get("is_grey_zone")
    )
    lines += [
        "## (A) / (B) — read this first (msg-4219 §0, msg-4221)",
        "",
        "- msg-3630's 179 rows cannot be reproduced (no per-row list was published); this "
        "evaluation is not compared with that breakdown (msg-4229 §1).",
        f"- harvested population (gate only, no Jev call): {len(population)} rows; "
        f"**in the grey zone: {pop_grey}**",
        f"- rows replayed: body {n}, supplement {len(supplement)}",
        f"- **(B) grey zone (ADMIT_UNSURE / second_time_force_admit): "
        f"{_pct(len(grey), len(all_rows))} of replayed rows** — "
        "under today's D18 gating only these reach Jev's verdict; (B) = this share x (A).",
        f"- rows the live hook's entry check would admit (proposer-authored `NEXT: human`): "
        f"{_pct(len(live), n)}; of those in the grey zone: "
        f"{sum(1 for r in live if r.grey_zone)}",
        "- The gate is **today's** `compute_gate_result` (allowed labels, label migration), not "
        "the rules in force when each message was posted (msg-4226).",
        "- Changing D18 or the allowed labels would be an amendment grounded in ADR-2026-05-23-07 "
        "(msg-4224); that decision is out of scope here.",
        f"- rosters from today's config (`current_fallback`): "
        f"{sum(1 for r in all_rows if r.roster_source == 'current_fallback')}",
        f"- thresholds (fixed in advance, RUBRIC): genuine_min={th.genuine_min} "
        f"genuine_max={th.genuine_max} spurious_min={th.spurious_min}",
        "",
        "## (A) Jev's discrimination — body set (headline)",
        "",
    ]
    views: dict[str, Mapping[Key, str]] = {"consensus": truth}
    for who, m in labellers.items():
        views[who] = {k: v.label for k, v in m.items()}
    for name, view in views.items():
        lines.append(f"### truth = {name}")
        lines += [f"- {k}: {v}" for k, v in headline(rows, view, th).items()]
        lines.append("")
    if len(labellers) == 2:
        (a, am), (b, bm) = labellers.items()
        k, kn = cohen_kappa(
            {x: y.label for x, y in am.items()}, {x: y.label for x, y in bm.items()}
        )
        lines += [f"- Cohen's κ ({a} vs {b}): {'n/a' if k is None else f'{k:.3f}'} over {kn}", ""]

    lines += ["## Supplement set (not in the headline)", ""]
    if supplement:
        lines += [f"- {k}: {v}" for k, v in headline(supplement, truth, th).items()]
    else:
        lines.append("- no rows")
    lines.append("")

    def table(counter: Counter[tuple[str, str]]) -> list[str]:
        truths = [*LABELS, "unlabelled"]
        out = [
            "| truth \\ Jev | " + " | ".join(VERDICTS) + " |",
            "|---" * (len(VERDICTS) + 1) + "|",
        ]
        for t in truths:
            if any(counter[(t, v)] for v in VERDICTS):
                out.append(f"| {t} | " + " | ".join(str(counter[(t, v)]) for v in VERDICTS) + " |")
        return out

    lines += ["### Confusion — body (consensus truth)", ""]
    lines += [*table(confusion(rows, truth, th)["all"]), ""]
    if supplement:
        lines += ["### Confusion — supplement (consensus truth)", ""]
        lines += [*table(confusion(supplement, truth, th)["all"]), ""]

    lines += ["## Misses — genuine judged LIKELY_NOT (one by one, body and supplement)", ""]
    miss = misses(all_rows, truth, labellers, th)
    lines += miss or ["- none"]
    lines.append("")

    lines += ["## Breakdown by labelled category (consensus truth)", ""]
    cat_of: dict[Key, str] = {}
    for m in labellers.values():
        for k, v in m.items():
            if v.category:
                cat_of.setdefault(k, v.category)
    by_cat: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    for r in all_rows:
        by_cat[f"{cat_of.get(r.key, '(none)')} [{r.eval_set or 'body'}]"][
            (truth.get(r.key, "unlabelled"), recompute(r.scores, th)[0])
        ] += 1
    for cat, c in sorted(by_cat.items()):
        lines += [f"### {cat}", "", *table(c), ""]

    lines += ["## Breakdown by gate result", ""]
    for g, c in sorted(confusion(all_rows, truth, th, by="gate_bucket").items()):
        lines += [f"### {g}", "", *table(c), ""]

    lines += ["## Calibration (body + supplement; genuine rows are too few in body alone)", ""]
    bins, a = calibration(all_rows, truth)
    lines += ["| spurious max | n | actually spurious |", "|---|---|---|"]
    lines += [f"| {b} | {bn} | {_pct(bs, bn)} |" for b, bn, bs in bins]
    lines += [
        "",
        f"- AUC of genuine-sum (genuine* vs spurious): {'n/a' if a is None else f'{a:.3f}'}",
        "",
    ]
    lines += ["### Threshold sweep — reference only, never a headline (RUBRIC), body set", ""]
    lines += ["| genuine_min | spurious_min | reduction | recall genuine |", "|---|---|---|---|"]
    lines += [f"| {g} | {s} | {red} | {rec} |" for g, s, red, rec in sweep(rows, truth)]
    lines.append("")

    lines += [f"## Grey zone only (n = {len(grey)} of replayed rows)", ""]
    if grey:
        lines += [f"- {k}: {v}" for k, v in headline(grey, truth, th).items()]
        lines += table(confusion(grey, truth, th)["all"])
    else:
        lines.append("- no rows — nothing to measure; see (B) above.")
    lines.append("")

    lines += ["## Operations", ""]
    oc = Counter(r.outcome or "not_called" for r in all_rows)
    lines += [f"- outcome: {dict(oc)}"]
    lines += [f"- error rate: {_pct(len(all_rows) - oc.get('evaluated', 0), len(all_rows))}"]
    lat = [r.latency_ms for r in all_rows if r.latency_ms is not None]
    lines += [f"- latency ms p50={_pctl(lat, 0.5)} p95={_pctl(lat, 0.95)}"]
    # Every gate result outside the grey zone — a valid-label ADMIT as much as a BOUNCE — takes
    # ``build_out_of_gate_verdict`` in ``adapters/decider_lexora.py`` (step 5), so the ADMIT rows
    # counted here are out_of_gate / UNSURE too (pinned by test_decider_replay.py
    # ``test_dry_run_admit_valid_label_emits_out_of_gate``).
    out_of_gate = sum(
        1
        for r in all_rows
        if r.server_verdict is not None and r.scores is not None and not r.grey_zone
    )
    lines += [
        f"- mindwire-side verdict (``adapters/decider_lexora.py``, not the server) is "
        f"scope=out_of_gate (UNSURE by construction) on {out_of_gate} rows — every non-grey-zone "
        "gate result, valid-label ADMIT and BOUNCE alike; the recomputed verdict above is what "
        "(A) reads.",
        "- cost per call: not in the replay record — read from Lexora usage.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# tierc-v2 (msg-4639 DECIDED 2d-4 / 2d-5, msg-4641 DECIDED 2d-7 / advisory)
# ---------------------------------------------------------------------------

V2_STRUCTURE_VERSIONS: frozenset[str] = frozenset({"tierc-v2", "tierc-v3"})
"""msg-5753 DECIDED 2d-15 item 3: every version of the single-``should_ask_human`` structure goes
to the v2 report. v3 only added one sentence to the question frame (main ``b224540``). The report
describes rows and counts nothing, so it may hold both; which version is *counted* is the
exporter's registered version, not this set. Each row's version is shown."""

SWEEP_V2: tuple[tuple[float, float], ...] = ((0.50, 0.33), (0.60, 0.40), (0.70, 0.47), (0.80, 0.53))
"""msg-4641: the v2 sweep as a constant table of ``(ask_min, not_ask_max)`` — no arithmetic, so
no float noise. Exploratory only: a pick from it is the next shadow period's hypothesis, never
a result on the same data (msg-4634 2d-1)."""

MALFORMED_OUTCOME = "no_verdict_malformed"


def recompute_v2(ask: float | None, th: TierCV2Thresholds) -> str:
    return "NO_VERDICT" if ask is None else evaluate_tierc_v2(ask, th).kind.value


def validity_v2(rows: Sequence[Row], th: TierCV2Thresholds) -> tuple[list[Row], int]:
    """(rows recomputed to ``NO_VERDICT``, ``MALFORMED`` count). Any of the former → INVALID."""
    nv = [r for r in rows if recompute_v2(r.ask_score, th) == "NO_VERDICT"]
    return nv, sum(1 for r in rows if r.outcome == MALFORMED_OUTCOME)


def headline_v2(
    rows: Sequence[Row], truth: Mapping[Key, str], th: TierCV2Thresholds
) -> dict[str, str]:
    """Reduction (spurious → LIKELY_NOT) and recall per genuine class, on the v2 verdict."""
    out: dict[str, str] = {}
    spur = [r for r in rows if truth.get(r.key) == "spurious"]
    ln = [r for r in spur if recompute_v2(r.ask_score, th) == "LIKELY_NOT"]
    out["spurious labelled (criterion: >= 12 to decide)"] = str(len(spur))
    out["reduction (spurious → LIKELY_NOT)"] = _pct(len(ln), len(spur))
    for cls in GENUINE_CLASSES:
        g = [r for r in rows if truth.get(r.key) == cls]
        hit = [r for r in g if reaches_human(recompute_v2(r.ask_score, th))]
        out[f"recall {cls} (reaches human)"] = _pct(len(hit), len(g))
    out["ambiguous (excluded)"] = str(sum(1 for r in rows if truth.get(r.key) == "ambiguous"))
    out["unlabelled (excluded)"] = str(sum(1 for r in rows if r.key not in truth))
    return out


def auc_v2(rows: Sequence[Row], truth: Mapping[Key, str]) -> tuple[float | None, int, int]:
    """AUC of ``should_ask_human`` (genuine* vs spurious), with the two counts."""
    pos = [
        r.ask_score for r in rows if r.ask_score is not None and truth.get(r.key) in GENUINE_CLASSES
    ]
    neg = [r.ask_score for r in rows if r.ask_score is not None and truth.get(r.key) == "spurious"]
    return auc(pos, neg), len(pos), len(neg)


def sweep_v2(rows: Sequence[Row], truth: Mapping[Key, str]) -> list[tuple[float, float, str, str]]:
    out: list[tuple[float, float, str, str]] = []
    for ask_min, not_ask_max in SWEEP_V2:
        h = headline_v2(rows, truth, TierCV2Thresholds(ask_min=ask_min, not_ask_max=not_ask_max))
        out.append(
            (
                ask_min,
                not_ask_max,
                h["reduction (spurious → LIKELY_NOT)"],
                h["recall genuine (reaches human)"],
            )
        )
    return out


def _verdict_table(counter: Counter[tuple[str, str]]) -> list[str]:
    out = [
        "| truth \\ Jev | " + " | ".join(VERDICTS) + " |",
        "|---" * (len(VERDICTS) + 1) + "|",
    ]
    for t in [*LABELS, "unlabelled"]:
        if any(counter[(t, v)] for v in VERDICTS):
            out.append(f"| {t} | " + " | ".join(str(counter[(t, v)]) for v in VERDICTS) + " |")
    return out


def render_v2(
    rows: Sequence[Row],
    labellers: Mapping[str, Mapping[Key, Label]],
    th: TierCV2Thresholds,
    corrections: Corrections | None = None,
    replayed_before_corrections: int | None = None,
    export: ExportLock | None = None,
    rules_snapshots: RulesSnapshotReport | None = None,
) -> str:
    """The tierc-v2 section (msg-4639 DECIDED 2d-4 / 2d-5, msg-4641 DECIDED 2d-7, msg-4650)."""
    truth = consensus(labellers)
    n = len(rows)
    lines: list[str] = ["# Tier-C evaluation — Jev, tierc-v2", ""]
    if rules_snapshots is not None:
        lines += [*render_rules_snapshots(rules_snapshots), ""]
    if export is None:
        lines += ["- export lock: **none** (not verified — library call, not the CLI)", ""]
    else:
        lines += [
            "## Export lock (msg-4650 DECIDED 2d-11)",
            "",
            f"- as_of: {export.as_of}",
            f"- export.json: `{export.path}` sha256 `{export.sha256}`",
            f"- materials.jsonl sha256 `{export.materials_sha256}` — verified",
            f"- fixture.jsonl sha256 `{export.fixture_sha256}` — verified",
            f"- replay sha256 `{export.replay_sha256}` — verified",
            "",
        ]
    if corrections is None:
        lines += ["- corrections: **none applied** — every fixture row is counted.", ""]
    else:
        lines += [
            f"- corrections: **applied** — `{corrections.path}` (sha256 `{corrections.sha256}`); "
            f"rows counted: **{n} / {replayed_before_corrections}** replayed.",
            "",
        ]
    no_verdict, malformed = validity_v2(rows, th)
    lines += ["## Validity — read this first (msg-4639 DECIDED 2d-4)", ""]
    lines += [
        f"- rows: {n}",
        f"- thresholds (pre-registered, `verdict.py` DEFAULT_V2_*): ask_min={th.ask_min} "
        f"not_ask_max={th.not_ask_max}",
        f"- NO_VERDICT rows: {len(no_verdict)}",
        f"- MALFORMED (outcome `{MALFORMED_OUTCOME}`): {_pct(malformed, n)}",
        f"- outcome: {dict(Counter(r.outcome or 'not_called' for r in rows))}",
        f"- questions_version: {dict(sorted(Counter(r.questions_version for r in rows).items()))}",
    ]
    if no_verdict:
        lines += [
            "",
            f"**INVALID** — {len(no_verdict)} row(s) have no v2 verdict. A missing verdict "
            "reaches the human, so recall would pass without Jev having answered. No headline, "
            "sweep or AUC is computed.",
            "",
        ]
        lines += [
            f"- {r.msg_id} `{r.key[0]}` r{r.key[1]} [{r.questions_version}] — outcome={r.outcome}"
            for r in no_verdict
        ]
        return "\n".join(lines)
    lines += ["- **valid**: every row has a v2 verdict.", ""]

    lines += ["## Headline", ""]
    views: dict[str, Mapping[Key, str]] = {"consensus": truth}
    for who, m in labellers.items():
        views[who] = {k: v.label for k, v in m.items()}
    for name, view in views.items():
        lines.append(f"### truth = {name}")
        lines += [f"- {k}: {v}" for k, v in headline_v2(rows, view, th).items()]
        lines.append("")
    if len(labellers) == 2:
        (a, am), (b, bm) = labellers.items()
        k, kn = cohen_kappa(
            {x: y.label for x, y in am.items()}, {x: y.label for x, y in bm.items()}
        )
        lines += [f"- Cohen's κ ({a} vs {b}): {'n/a' if k is None else f'{k:.3f}'} over {kn}", ""]

    conf: Counter[tuple[str, str]] = Counter(
        (truth.get(r.key, "unlabelled"), recompute_v2(r.ask_score, th)) for r in rows
    )
    lines += ["### Confusion (consensus truth)", "", *_verdict_table(conf), ""]

    lines += ["## Misses — genuine judged LIKELY_NOT", ""]
    miss = [
        f"- **{r.msg_id}** `{r.key[0]}` r{r.key[1]} [{r.questions_version}] ({truth[r.key]}) — "
        f"should_ask_human={r.ask_score:.2f}\n  - body head: {r.body_head}"
        for r in rows
        if r.ask_score is not None
        and truth.get(r.key) in GENUINE_CLASSES
        and recompute_v2(r.ask_score, th) == "LIKELY_NOT"
    ]
    lines += miss or ["- none"]
    lines.append("")

    short = [r for r in rows if r.following_n is not None and r.following_n < 3]
    by_truth = dict(sorted(Counter(truth.get(r.key, "unlabelled") for r in short).items()))
    lines += ["## Rows with following_n < 3 (msg-4641 DECIDED 2d-7)", ""]
    lines += [f"- rows: {_pct(len(short), n)}", f"- consensus truth: {by_truth}", ""]

    a, npos, nneg = auc_v2(rows, truth)
    lines += ["## Separation", ""]
    lines += [
        f"- AUC of should_ask_human (genuine* vs spurious): "
        f"{'n/a' if a is None else f'{a:.3f}'} ({npos} vs {nneg})",
        "",
    ]
    lines += ["### Threshold sweep v2 — exploratory only, never a headline (msg-4634 2d-1)", ""]
    lines += ["| ask_min | not_ask_max | reduction | recall genuine |", "|---|---|---|---|"]
    lines += [f"| {x:.2f} | {y:.2f} | {red} | {rec} |" for x, y, red, rec in sweep_v2(rows, truth)]
    lines.append("")

    lines += ["## Operations", ""]
    lat = [r.latency_ms for r in rows if r.latency_ms is not None]
    lines += [f"- latency ms p50={_pctl(lat, 0.5)} p95={_pctl(lat, 0.95)}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# rules snapshots (T-decider-tierc-v2-all-escalations, Bohr msg-4631 / 4633 / 5130)
# ---------------------------------------------------------------------------

RULES_ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "eval" / "tierc" / "rules"
"""Default ``--rules-archive-dir``: ``eval/tierc/rules/`` (the replay snapshot dir, msg-4382)."""


@dataclass(frozen=True)
class RulesSnapshotReport:
    """Result of :func:`archive_rules_snapshots`: the rows that stay, and why the rest left."""

    kept: list[Row]
    archived: dict[str, Path]
    excluded: dict[str, Counter[str]]
    """reason (``missing`` / ``mismatch``) → rows excluded per ``rules_sha256``."""


def _rules_sha256_of(rec: Mapping[str, Any], decision: Mapping[str, Any]) -> str | None:
    """A record's ``rules_sha256`` — on the decision (live / ``--endpoint``) or the record."""
    for src in (decision, rec):
        v = src.get("rules_sha256")
        if isinstance(v, str) and v:
            return v
    return None


def archive_rules_snapshots(
    rows: Sequence[Row], snapshot_dir: Path, archive_dir: Path
) -> RulesSnapshotReport:
    """Copy each row's live snapshot ``<snapshot_dir>/<sha>.toml`` to ``<archive_dir>/<sha>.toml``.

    Only copying — the snapshot was written by the live Decider when it computed the hash
    (msg-4631); this step never reads the canonical rules file. The bytes are re-hashed on the
    way (msg-4633): a missing snapshot excludes its rows as ``missing``, bytes that do not hash
    to the name exclude them as ``mismatch``. Nothing aborts; the counts go in the report.
    Rows with no ``rules_sha256`` (tierc-v1) are kept untouched. ``*.tmp`` / ``*.corrupt-*``
    files are never read (only ``<sha>.toml`` is opened).
    """
    status: dict[str, str] = {}
    archived: dict[str, Path] = {}
    for sha in sorted({r.rules_sha256 for r in rows if r.rules_sha256 is not None}):
        try:
            data = (snapshot_dir / f"{sha}.toml").read_bytes()
        except OSError:
            status[sha] = "missing"
            continue
        if hashlib.sha256(data).hexdigest() != sha:
            status[sha] = "mismatch"
            continue
        dest = archive_dir / f"{sha}.toml"
        if not dest.exists() or hashlib.sha256(dest.read_bytes()).hexdigest() != sha:
            archive_dir.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        status[sha] = "ok"
        archived[sha] = dest
    kept: list[Row] = []
    excluded: dict[str, Counter[str]] = {"missing": Counter(), "mismatch": Counter()}
    for r in rows:
        st = "ok" if r.rules_sha256 is None else status[r.rules_sha256]
        if st == "ok":
            kept.append(r)
        else:
            assert r.rules_sha256 is not None
            excluded[st][r.rules_sha256] += 1
    return RulesSnapshotReport(kept=kept, archived=archived, excluded=excluded)


def render_rules_snapshots(rep: RulesSnapshotReport) -> list[str]:
    out = [
        "- rules snapshots (msg-4631 / 4633): "
        f"archived {len(rep.archived)} hash(es); excluded rows: "
        f"missing={sum(rep.excluded['missing'].values())}, "
        f"mismatch={sum(rep.excluded['mismatch'].values())}"
    ]
    for reason in ("missing", "mismatch"):
        for sha, n in sorted(rep.excluded[reason].items()):
            out.append(f"  - {reason}: `{sha}` — {n} row(s)")
    return out


def apply_corrections(
    rows: Sequence[Row], labellers: Mapping[str, Mapping[Key, Label]], corr: Corrections
) -> tuple[list[Row], dict[str, dict[Key, Label]]]:
    """Keep only ``corr.keep``. Labels are filtered too, so κ and consensus see the same rows."""
    kept = [r for r in rows if r.key in corr.keep]
    return kept, {n: {k: v for k, v in m.items() if k in corr.keep} for n, m in labellers.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--labels", action="append", default=[], help="NAME=PATH (repeatable)")
    parser.add_argument(
        "--population", type=Path, default=None, help="population.jsonl (gate-only rows)"
    )
    parser.add_argument(
        "--corrections", type=Path, default=None, help="corrections JSON (optional; msg-4302)"
    )
    parser.add_argument(
        "--export-manifest",
        type=Path,
        default=None,
        help="the exporter's export.json; required for a tierc-v2 run (msg-4648 / msg-4650)",
    )
    parser.add_argument(
        "--rules-snapshot-dir",
        type=Path,
        default=None,
        help="live snapshot dir (<data_dir>/decider/rules): archive each row's rules_sha256 "
        "snapshot and exclude rows whose snapshot is missing / mismatched (msg-4631 / 4633)",
    )
    parser.add_argument(
        "--rules-archive-dir",
        type=Path,
        default=RULES_ARCHIVE_DIR,
        help="where verified snapshots are copied (default eval/tierc/rules)",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    replay_records = read_jsonl(args.replay)
    export: ExportLock | None = None
    try:
        version = run_version(replay_records)
        if version == "v2":
            if args.export_manifest is None:
                raise InputError("a tierc-v2 run requires --export-manifest (msg-4648)")
            export = verify_export_lock(args.export_manifest, args.fixture, args.replay)
        elif args.export_manifest is not None:
            raise InputError(
                "--export-manifest given for a v1 run; nothing would be checked (msg-4648)"
            )
    except InputError as exc:
        print(f"tierc_eval_report: {exc}", file=sys.stderr)
        return 2

    # Thresholds are fixed in advance (RUBRIC, msg-4229 §3-4): the live defaults, no CLI knob.
    th = TierCThresholds()
    labellers: dict[str, dict[Key, Label]] = {}
    for spec in args.labels:
        name, _, path = spec.partition("=")
        if not path:
            print(f"tierc_eval_report: --labels wants NAME=PATH, got {spec!r}", file=sys.stderr)
            return 2
        labellers[name] = load_labels(read_jsonl(Path(path)))
    fixture = read_jsonl(args.fixture)
    fixture_sha = file_sha256(args.fixture)
    rows = join(replay_records, fixture)
    population = read_jsonl(args.population) if args.population is not None else []
    corr: Corrections | None = None
    replayed = len(rows)
    if args.corrections is not None:
        try:
            corr = load_corrections(args.corrections, fixture, fixture_sha)
        except CorrectionsError as exc:
            print(f"tierc_eval_report: {exc}", file=sys.stderr)
            return 2
        rows, labellers = apply_corrections(rows, labellers, corr)
    elif fixture_sha == UNCORRECTED_FIXTURE_SHA256:
        print(
            f"tierc_eval_report: {args.fixture} is the 2026-09-28 fixture; 143 of its 209 rows "
            f"are not escalations (PR-gate #349). Pass --corrections "
            f"{CORRECTIONS_PATH_2026_09_28}.",
            file=sys.stderr,
        )
        return 2
    snap: RulesSnapshotReport | None = None
    if args.rules_snapshot_dir is not None:
        snap = archive_rules_snapshots(rows, args.rules_snapshot_dir, args.rules_archive_dir)
        kept_keys = {r.key for r in snap.kept}
        rows = snap.kept
        labellers = {
            n: {k: v for k, v in m.items() if k in kept_keys} for n, m in labellers.items()
        }
    text = render(
        rows,
        labellers,
        th,
        population,
        corr,
        replayed,
        th_v2=TierCV2Thresholds(),
        export=export,
        rules_snapshots=snap,
    )
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
