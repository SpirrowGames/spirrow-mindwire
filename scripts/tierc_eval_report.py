"""Tier-C replay evaluation report — Jev's answers joined to the double-blind labels.

Spec: T-decider-tierc-replay-eval — Bohr msg-4219 §0 / §3 (metrics), msg-4224 (two labellers,
agreement → truth, disagreement → ambiguous, 3-way sensitivity, Cohen's κ), msg-4226 §6 (state
that the gate is today's rules; count ``current_fallback`` rosters), Fermi msg-4221 (put the
(A)/(B) split at the top).

**Inputs** (all JSONL, joined on ``(thread_id, round_index)``):

* ``--replay`` — ``decider_replay.py --endpoint`` output (records carrying ``decision``);
* ``--fixture`` — ``build_tierc_eval_fixture.py`` output (gate result, author, ``live_entry``,
  ``roster_source``);
* ``--labels NAME=PATH`` — repeatable; one filled label file per labeller.

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

**Reader of the output.** Markdown on stdout / ``--out``. Nothing is posted anywhere.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spirrow_mindwire.adapters.decider_lexora import _extract_noul
from spirrow_mindwire.decider.verdict import (
    TIER_C_GENUINE_KEYS,
    TIER_C_SPURIOUS_KEYS,
    TierCThresholds,
    TierCVerdictKind,
    evaluate_tierc,
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
            label=str(raw),
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
    body_head: str
    scores: dict[str, float] | None
    server_verdict: str | None
    outcome: str | None
    latency_ms: int | None


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
                body_head=str(state.get("head_summary", ""))[:160].replace("\n", " "),
                scores=scores_of(rec),
                server_verdict=str(sv["kind"]) if isinstance(sv, Mapping) else None,
                outcome=str(d["outcome"]) if d.get("outcome") else None,
                latency_ms=int(d["latency_ms"]) if d.get("latency_ms") is not None else None,
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
    pos = [
        sum(r.scores[k] for k in TIER_C_GENUINE_KEYS)
        for r in scored
        if r.scores is not None and truth[r.key] in GENUINE_CLASSES
    ]
    neg = [
        sum(r.scores[k] for k in TIER_C_GENUINE_KEYS)
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
    rows: Sequence[Row],
    labellers: Mapping[str, Mapping[Key, Label]],
    th: TierCThresholds,
) -> str:
    truth = consensus(labellers)
    lines: list[str] = ["# Tier-C replay evaluation — Jev", ""]

    n = len(rows)
    grey = [r for r in rows if r.grey_zone]
    live = [r for r in rows if r.live_entry]
    lines += [
        "## (A) / (B) — read this first (msg-4219 §0, msg-4221)",
        "",
        f"- rows replayed: {n}",
        f"- **(B) grey zone (ADMIT_UNSURE / second_time_force_admit): {_pct(len(grey), n)}** — "
        "under today's D18 gating only these reach Jev's verdict; (B) = this share x (A).",
        f"- rows the live hook's entry check would admit (proposer-authored `NEXT: human`): "
        f"{_pct(len(live), n)}; of those in the grey zone: "
        f"{sum(1 for r in live if r.grey_zone)}",
        "- The gate is **today's** `compute_gate_result` (allowed labels, label migration), not "
        "the rules in force when each message was posted (msg-4226).",
        "- Changing D18 or the allowed labels would be an amendment grounded in ADR-2026-05-23-07 "
        "(msg-4224); that decision is out of scope here.",
        f"- rosters from today's config (`current_fallback`): "
        f"{sum(1 for r in rows if r.roster_source == 'current_fallback')}",
        f"- thresholds: genuine_min={th.genuine_min} genuine_max={th.genuine_max} "
        f"spurious_min={th.spurious_min}",
        "",
        "## (A) Jev's discrimination on all rows",
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

    lines += ["### Confusion (consensus truth)", ""]
    lines += [*table(confusion(rows, truth, th)["all"]), ""]

    lines += ["## Misses — genuine judged LIKELY_NOT (one by one)", ""]
    miss = misses(rows, truth, labellers, th)
    lines += miss or ["- none"]
    lines.append("")

    lines += ["## Breakdown by labelled category (consensus truth)", ""]
    cat_of: dict[Key, str] = {}
    for m in labellers.values():
        for k, v in m.items():
            if v.category:
                cat_of.setdefault(k, v.category)
    by_cat: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    for r in rows:
        by_cat[cat_of.get(r.key, "(none)")][
            (truth.get(r.key, "unlabelled"), recompute(r.scores, th)[0])
        ] += 1
    for cat, c in sorted(by_cat.items()):
        lines += [f"### {cat}", "", *table(c), ""]

    lines += ["## Breakdown by gate result", ""]
    for g, c in sorted(confusion(rows, truth, th, by="gate_bucket").items()):
        lines += [f"### {g}", "", *table(c), ""]

    lines += ["## Calibration", ""]
    bins, a = calibration(rows, truth)
    lines += ["| spurious max | n | actually spurious |", "|---|---|---|"]
    lines += [f"| {b} | {bn} | {_pct(bs, bn)} |" for b, bn, bs in bins]
    lines += [
        "",
        f"- AUC of genuine-sum (genuine* vs spurious): {'n/a' if a is None else f'{a:.3f}'}",
        "",
    ]
    lines += ["### Threshold sweep (genuine_max = genuine_min x 2/3)", ""]
    lines += ["| genuine_min | spurious_min | reduction | recall genuine |", "|---|---|---|---|"]
    lines += [f"| {g} | {s} | {red} | {rec} |" for g, s, red, rec in sweep(rows, truth)]
    lines.append("")

    lines += [f"## Grey zone only (n = {len(grey)})", ""]
    if grey:
        lines += [f"- {k}: {v}" for k, v in headline(grey, truth, th).items()]
        lines += table(confusion(grey, truth, th)["all"])
    else:
        lines.append("- no rows — nothing to measure; see (B) above.")
    lines.append("")

    lines += ["## Operations", ""]
    oc = Counter(r.outcome or "not_called" for r in rows)
    lines += [f"- outcome: {dict(oc)}"]
    lines += [f"- error rate: {_pct(n - oc.get('evaluated', 0), n)}"]
    lat = [r.latency_ms for r in rows if r.latency_ms is not None]
    lines += [f"- latency ms p50={_pctl(lat, 0.5)} p95={_pctl(lat, 0.95)}"]
    agree = sum(
        1 for r in rows if r.server_verdict is not None and r.scores is not None and not r.grey_zone
    )
    lines += [
        f"- server verdict is scope=out_of_gate (UNSURE by construction) on {agree} rows; the "
        "recomputed verdict above is what (A) reads.",
        "- cost per call: not in the replay record — read from Lexora usage.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--labels", action="append", default=[], help="NAME=PATH (repeatable)")
    parser.add_argument("--genuine-min", type=float, default=None)
    parser.add_argument("--genuine-max", type=float, default=None)
    parser.add_argument("--spurious-min", type=float, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    th_kwargs = {
        k: v
        for k, v in (
            ("genuine_min", args.genuine_min),
            ("genuine_max", args.genuine_max),
            ("spurious_min", args.spurious_min),
        )
        if v is not None
    }
    th = TierCThresholds(**th_kwargs)
    labellers: dict[str, dict[Key, Label]] = {}
    for spec in args.labels:
        name, _, path = spec.partition("=")
        if not path:
            print(f"tierc_eval_report: --labels wants NAME=PATH, got {spec!r}", file=sys.stderr)
            return 2
        labellers[name] = load_labels(read_jsonl(Path(path)))
    rows = join(read_jsonl(args.replay), read_jsonl(args.fixture))
    text = render(rows, labellers, th)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
