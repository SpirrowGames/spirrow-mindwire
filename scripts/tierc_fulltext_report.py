"""Tier-C fulltext eval comparison — T-decider-tierc-fulltext-eval (Bohr msg-4318 / 4327 / 4329).

Joins three replays of the **same rows** — ``orig`` (the original 500-char run), ``base`` (the
500-char re-run, same input, the noise baseline) and ``full`` (``--head-m 8000``) — to the
labels, and prints a markdown comparison.

**AUC** is the original report's definition, unchanged (:func:`tierc_eval_report.calibration`):
the genuine-key sum as score, ``genuine*`` rows positive, ``spurious`` negative, ``ambiguous``
left out, over every row given (body + supplement) — the definition behind the original 0.705.
The 95% intervals are a paired row bootstrap (``--boot`` resamples, ``--seed``): the same
resampled rows score all three runs, so ΔAUC intervals are paired.

**Change rates** (noise vs fulltext): for each pair of runs on the same row,

* ``verdict`` — the verdict recomputed at the fixed thresholds (0.60 / 0.40 / 0.60) differs;
* ``answer≥0.05`` — the share of the 6 answers that moved by 0.05 or more;
* ``mean |Δ|`` — mean absolute change over the 6 answers.

``orig→base`` is the same input sent twice (noise); ``orig→full`` and ``base→full`` are the
input change. With 12 spurious rows these are descriptive only (msg-4327): no significance is
claimed.

**Reader of the output.** Markdown on stdout / ``--out``. Nothing is posted anywhere.
"""

from __future__ import annotations

import argparse
import random
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tierc_eval_report as rep

from spirrow_mindwire.decider.verdict import TierCThresholds

RUNS = ("orig", "base", "full")
Key = tuple[str, int]


def auc_of(
    scores: Mapping[Key, Mapping[str, float]], truth: Mapping[Key, str], keys: Sequence[Key]
) -> float | None:
    pos = [
        sum(scores[k][q] for q in rep.TIER_C_GENUINE_KEYS)
        for k in keys
        if truth.get(k) in rep.GENUINE_CLASSES
    ]
    neg = [
        sum(scores[k][q] for q in rep.TIER_C_GENUINE_KEYS)
        for k in keys
        if truth.get(k) == "spurious"
    ]
    return rep.auc(pos, neg)


def bootstrap(
    runs: Mapping[str, Mapping[Key, Mapping[str, float]]],
    truth: Mapping[Key, str],
    keys: Sequence[Key],
    *,
    n: int,
    seed: int,
) -> dict[str, tuple[float, float]]:
    """Paired row bootstrap: 95% percentile intervals for each run's AUC and each ΔAUC."""
    rng = random.Random(seed)
    samples: dict[str, list[float]] = {}
    for _ in range(n):
        ks = [keys[rng.randrange(len(keys))] for _ in keys]
        a = {r: auc_of(runs[r], truth, ks) for r in RUNS}
        if any(v is None for v in a.values()):
            continue
        vals = {f"AUC {r}": a[r] for r in RUNS} | {
            "Δ full-orig": a["full"] - a["orig"],  # type: ignore[operator]
            "Δ base-orig (noise)": a["base"] - a["orig"],  # type: ignore[operator]
            "Δ full-base": a["full"] - a["base"],  # type: ignore[operator]
        }
        for name, v in vals.items():
            samples.setdefault(name, []).append(float(v))  # type: ignore[arg-type]
    out: dict[str, tuple[float, float]] = {}
    for name, xs in samples.items():
        xs.sort()
        out[name] = (xs[int(0.025 * len(xs))], xs[min(len(xs) - 1, int(0.975 * len(xs)))])
    return out


def change(
    a: Mapping[str, float], b: Mapping[str, float], th: TierCThresholds
) -> tuple[bool, float, float]:
    va, _ = rep.recompute(a, th)
    vb, _ = rep.recompute(b, th)
    diffs = [abs(a[q] - b[q]) for q in a]
    return va != vb, sum(d >= 0.05 for d in diffs) / len(diffs), sum(diffs) / len(diffs)


def change_table(
    runs: Mapping[str, Mapping[Key, Mapping[str, float]]],
    truth: Mapping[Key, str],
    keys: Sequence[Key],
    th: TierCThresholds,
) -> list[str]:
    lines = [
        "| rows | pair | n | verdict changed | answers moved ≥0.05 | mean abs change |",
        "|---|---|---|---|---|---|",
    ]
    groups = {
        "spurious": [k for k in keys if truth.get(k) == "spurious"],
        "genuine*": [k for k in keys if truth.get(k) in rep.GENUINE_CLASSES],
        "all": list(keys),
    }
    for g, ks in groups.items():
        for x, y, label in (
            ("orig", "base", "orig→base (noise, same input)"),
            ("orig", "full", "orig→full (500→8000)"),
            ("base", "full", "base→full (same session)"),
        ):
            cs = [change(runs[x][k], runs[y][k], th) for k in ks]
            if not cs:
                continue
            v = sum(c[0] for c in cs)
            lines.append(
                f"| {g} | {label} | {len(cs)} | {v}/{len(cs)} | "
                f"{sum(c[1] for c in cs) / len(cs):.1%} | {sum(c[2] for c in cs) / len(cs):.3f} |"
            )
    return lines


def load_scores(path: Path, keys: set[Key]) -> dict[Key, dict[str, float]]:
    """Each key's scores from its **last** scored record.

    A run may hold a failed attempt (no scores) followed by a ``--resume`` retry: the failed
    record is skipped, and a key with no scored record at all raises below.
    """
    out: dict[Key, dict[str, float]] = {}
    for rec in rep.read_jsonl(path):
        k = rep._key(rec)
        if k in keys:
            s = rep.scores_of(rec)
            if s is not None:
                out[k] = s
    missing = keys - set(out)
    if missing:
        raise ValueError(f"{path}: {len(missing)} rows missing, e.g. {sorted(missing)[:3]}")
    return out


def render(
    runs: Mapping[str, Mapping[Key, Mapping[str, float]]],
    truths: Mapping[str, Mapping[Key, str]],
    keys: Sequence[Key],
    *,
    boot: int,
    seed: int,
) -> str:
    th = TierCThresholds()
    lines = [
        "# Tier-C fulltext eval — 500 vs 8,000 chars (T-decider-tierc-fulltext-eval)",
        "",
        f"rows: {len(keys)}; bootstrap: {boot} paired resamples, seed {seed}; thresholds "
        f"genuine_min={th.genuine_min} genuine_max={th.genuine_max} "
        f"spurious_min={th.spurious_min}",
        "",
    ]
    for name, truth in truths.items():
        n_pos = sum(1 for k in keys if truth.get(k) in rep.GENUINE_CLASSES)
        n_neg = sum(1 for k in keys if truth.get(k) == "spurious")
        ci = bootstrap(runs, truth, keys, n=boot, seed=seed)
        lines += [f"## truth = {name} (genuine* {n_pos} / spurious {n_neg})", ""]
        lines += ["| measure | value | 95% CI |", "|---|---|---|"]
        a = {r: auc_of(runs[r], truth, keys) for r in RUNS}
        for r in RUNS:
            lo, hi = ci[f"AUC {r}"]
            lines.append(f"| AUC {r} | {a[r]:.3f} | [{lo:.3f}, {hi:.3f}] |")
        for d, (x, y) in (
            ("Δ full-orig", ("full", "orig")),
            ("Δ base-orig (noise)", ("base", "orig")),
            ("Δ full-base", ("full", "base")),
        ):
            lo, hi = ci[d]
            lines.append(f"| {d} | {a[x] - a[y]:+.3f} | [{lo:+.3f}, {hi:+.3f}] |")  # type: ignore[operator]
        for r in RUNS:
            spur = [k for k in keys if truth.get(k) == "spurious"]
            ln = sum(1 for k in spur if rep.recompute(runs[r][k], th)[0] == "LIKELY_NOT")
            gen = [k for k in keys if truth.get(k) in rep.GENUINE_CLASSES]
            hr = sum(1 for k in gen if rep.reaches_human(rep.recompute(runs[r][k], th)[0]))
            lines.append(
                f"| reduction / recall genuine* — {r} | {ln}/{len(spur)} / {hr}/{len(gen)} | |"
            )
        lines += ["", *change_table(runs, truth, keys, th), ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    for r in RUNS:
        p.add_argument(f"--{r}", type=Path, required=True)
    p.add_argument("--fixture", type=Path, required=True, help="the original fixture.jsonl")
    p.add_argument("--corrections", type=Path, required=True)
    p.add_argument("--labels", action="append", default=[], help="NAME=PATH (repeatable)")
    p.add_argument("--boot", type=int, default=10000)
    p.add_argument("--seed", type=int, default=20260928)
    p.add_argument("--out", type=Path, default=None)
    a = p.parse_args(argv)

    fixture = rep.read_jsonl(a.fixture)
    corr = rep.load_corrections(a.corrections, fixture, rep.file_sha256(a.fixture))
    keys = sorted(corr.keep)
    labellers: dict[str, Any] = {}
    for spec in a.labels:
        name, _, path = spec.partition("=")
        labellers[name] = rep.load_labels(rep.read_jsonl(Path(path)))
    truths: dict[str, Mapping[Key, str]] = {"consensus": rep.consensus(labellers)}
    for name, labs in labellers.items():
        truths[name] = {k: v.label for k, v in labs.items()}
    runs = {r: load_scores(getattr(a, r), set(keys)) for r in RUNS}
    text = render(runs, truths, keys, boot=a.boot, seed=a.seed)
    if a.out:
        a.out.write_text(text, encoding="utf-8", newline="\n")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
