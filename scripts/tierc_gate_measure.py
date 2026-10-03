"""Measure what the enforced Tier-C admission gate would have done to past ``NEXT: human`` turns.

Spec: T-decider-conductor-hook DECIDED 2e-1a (Bohr msg-5141; Takahito decide via operator proxy).
No Lexora call, no cost: the gate is :func:`~spirrow_mindwire.tier_c_admission_gate.
decide_admission`, a pure function, run here exactly as the live gate runs it.

**Selection.** Every chatroom message in ``[--since, --until)`` that the conductor's own resolver
(``resolve_handoff``, against the same routing roster ``build_tierc_eval_fixture.py`` uses) reads
as an author-written ``NEXT: human``. Each falls into exactly one bucket:

* ``i_bounce`` — a proposer / implementer / naysayer turn the gate bounces (no label, ``other:``,
  ``release-cross-repo``, unknown). The upper bound of what stops reaching Takahito under
  ``[tierc_gate] mode = "enforce"``. Split further by bounce reason (counts only).
* ``ii_migrated`` — admitted by rewriting a legacy label (``scope`` → ``goal``, ``billing`` →
  ``cost``).
* ``iii_admitted`` — admitted on a valid label (``unsure:goal?`` included; counted separately as
  ``iii_unsure``).
* ``iv_pr_gate_relay`` — authored by ``pr-gate-relay``. Outside the hook and the gate (msg-4361),
  counted on its own; it is not part of the role total.
* ``other_author`` — anyone else (operator, the human, off-roster authors). Never gated.

**U4c additions** (T-tier-c-admission-gate Bohr msg-6488 / 6490, endorsed by Einstein):

* ``iii_admitted_by_label`` — ``iii_admitted`` split by the label the gate admitted on
  (:attr:`AdmissionDecision.normalized_label`, the gate's own output; the body is not parsed a
  second time). ``goal`` / ``cost`` / ``irreversible`` / ``unsure:goal?`` are always present; the
  values add up to ``counts.iii_admitted``.
* ``must_admit`` / ``must_admit_dropped`` — the regression refs (``--must-admit``; default the two
  goal-level messages magickit msg-829 / msg-740) mapped to the bucket each landed in.
  ``dropped`` = ``i_bounce`` or ``not_found``; a ref outside the window or the harvest is
  ``not_found``, so "not measured" never reads as "admitted". No pass/fail verdict: the reader
  judges (msg-6252).
* ``mode_by_project`` / ``i_bounce_by_mode`` — each project's ``[tierc_gate].mode`` as an input.
  ``enforced`` = bounced in an ``enforce`` project (it never reached the human); ``shadow`` =
  would have bounced but reached the human (``off``); ``mode_unmeasured`` = the mode could not be
  read (``unmeasured: <reason>``, never a default). The mode is the one in the config file at
  measurement time: the daemon reads a single ``mindwire.toml`` for every project, and no history
  of the setting exists, so a window that spans a mode switch is attributed to the current mode.

``retry_lookup`` is ``never_retry``: no past turn carries a ``RETRY:`` token issued by a live
gate, so "no unresolved bounce exists" is the true answer for history.

**Output.** Counts and ``msg_id`` lists only (``--out`` JSON, LF). No message body, no label text
and no Jev score is written — the pre-registration in T-decider-tierc-replay-eval forbids looking
at the shadow scores before evaluation, and this script has none to look at. Nothing is posted to
any chatroom thread.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spirrow_mindwire.conductor.core import roster_role
from spirrow_mindwire.conductor.gate_records import RELAY_AUTHOR
from spirrow_mindwire.conductor.handoff import HandoffKind, resolve_handoff
from spirrow_mindwire.config import MindwireSettings
from spirrow_mindwire.decider.hook import TIERC_ENTRY_ROLES, never_retry
from spirrow_mindwire.tier_c_admission_gate import (
    ADMIT_LABELS,
    UNSURE_LABEL,
    AdmissionVerdict,
    LogKind,
    decide_admission,
)
from spirrow_mindwire.value_objects import Role


def _load_builder() -> Any:
    """The fixture builder, loaded by path: its harvest / roster code is reused, not copied."""
    name = "build_tierc_eval_fixture_for_gate_measure"
    path = Path(__file__).resolve().parent / "build_tierc_eval_fixture.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


builder = _load_builder()

BUCKETS: tuple[str, ...] = (
    "i_bounce",
    "ii_migrated",
    "iii_admitted",
    "iv_pr_gate_relay",
    "other_author",
)


#: The goal-level regression set of msg-3630 §3: the gate must not bounce these.
DEFAULT_MUST_ADMIT: tuple[str, ...] = (
    "spirrow-magickit/T-merged-to-main-without-gate-artifact#msg-829",
    "spirrow-magickit/T-dashboard-system-page-retirement-unfiled#msg-740",
)

#: Labels always reported in ``iii_admitted_by_label``, zero or not.
ADMITTED_LABELS: tuple[str, ...] = (*sorted(ADMIT_LABELS), UNSURE_LABEL)

NOT_FOUND = "not_found"
BOUNCE_MODES: tuple[str, ...] = ("enforced", "shadow", "mode_unmeasured")


def _bounce_mode(mode: str | None) -> str:
    if mode == "enforce":
        return "enforced"
    if mode == "off":
        return "shadow"
    return "mode_unmeasured"


def classify(
    threads: Sequence[tuple[str, str, Sequence[Any]]],
    current_roster: Mapping[str, Role],
    *,
    since: datetime,
    until: datetime,
    mode_by_project: Mapping[str, str] | None = None,
    must_admit: Sequence[str] = DEFAULT_MUST_ADMIT,
) -> dict[str, Any]:
    """Bucket every author-written ``NEXT: human`` in ``[since, until)``. Pure; counts + ids.

    ``mode_by_project`` maps a project to its ``[tierc_gate].mode`` (``off`` / ``enforce``) or to
    an ``unmeasured: <reason>`` string; a project missing from it is unmeasured too.
    """
    modes = dict(mode_by_project or {})
    ids: dict[str, list[str]] = {b: [] for b in BUCKETS}
    unsure: list[str] = []
    by_label: dict[str, list[str]] = {label: [] for label in ADMITTED_LABELS}
    by_mode: dict[str, list[str]] = {m: [] for m in BOUNCE_MODES}
    projects_seen: set[str] = set()
    bounce_reasons: Counter[str] = Counter()
    by_author: Counter[str] = Counter()
    for project, thread_id, messages in threads:
        projects_seen.add(project)
        for i, m in enumerate(messages):
            if not (since <= m.timestamp < until):
                continue
            roster = builder.routing_roster(messages[: i + 1], current_roster)
            handoff = resolve_handoff(m.content, roster, next_participant=m.next_participant)
            if handoff.kind is not HandoffKind.HUMAN or not handoff.author_requested_human:
                continue
            ref = f"{project}/{thread_id}#{m.msg_id}"
            if m.author == RELAY_AUTHOR:
                ids["iv_pr_gate_relay"].append(ref)
                continue
            if roster_role(roster, m.author) not in TIERC_ENTRY_ROLES:
                ids["other_author"].append(ref)
                continue
            by_author[m.author] += 1
            decision = decide_admission(
                body=m.content,
                author=m.author,
                retry_lookup=never_retry,
                now=m.timestamp,
                bounce_uuid="measure",
            )
            kinds = {e.kind for e in decision.log_entries}
            if decision.verdict is AdmissionVerdict.BOUNCE:
                ids["i_bounce"].append(ref)
                assert decision.bounce_reason is not None
                bounce_reasons[decision.bounce_reason.value] += 1
                by_mode[_bounce_mode(modes.get(project))].append(ref)
            elif LogKind.LABEL_MIGRATION in kinds:
                ids["ii_migrated"].append(ref)
            else:
                ids["iii_admitted"].append(ref)
                by_label.setdefault(str(decision.normalized_label), []).append(ref)
                if LogKind.ADMIT_UNSURE in kinds:
                    unsure.append(ref)
    role_total = len(ids["i_bounce"]) + len(ids["ii_migrated"]) + len(ids["iii_admitted"])
    bucket_of = {ref: bucket for bucket, refs in ids.items() for ref in refs}
    must = {ref: bucket_of.get(ref, NOT_FOUND) for ref in must_admit}
    return {
        "schema": 1,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "counts": {
            **{b: len(v) for b, v in ids.items()},
            "iii_unsure": len(unsure),
            "role_total": role_total,
        },
        "i_bounce_by_reason": dict(sorted(bounce_reasons.items())),
        "role_total_by_author": dict(sorted(by_author.items())),
        "msg_ids": {**ids, "iii_unsure": unsure},
        "iii_admitted_by_label": {label: len(v) for label, v in by_label.items()},
        "iii_admitted_by_label_msg_ids": by_label,
        "mode_by_project": {
            p: modes.get(p, "unmeasured: no mode given for this project")
            for p in sorted(projects_seen | set(modes))
        },
        "i_bounce_by_mode": {m: len(v) for m, v in by_mode.items()},
        "i_bounce_by_mode_msg_ids": by_mode,
        "must_admit": must,
        "must_admit_dropped": [r for r, b in must.items() if b in ("i_bounce", NOT_FOUND)],
    }


def load_config(config_path: Path | None) -> MindwireSettings | str:
    """Read ``mindwire.toml`` once: the settings, or ``unmeasured: <reason>`` — never a default.

    ``load_settings`` turns a missing file into the built-in defaults (mode ``off``, empty
    roster); that is the fall-back this must not take, so a missing or unreadable file comes
    back as the reason string instead. ``main`` derives both the roster and the modes from this
    one result; on a reason string it measures nothing (no fetch, no counts) and exits 2.
    """
    from spirrow_mindwire.config import _default_config_path, load_settings

    path = config_path if config_path is not None else _default_config_path()
    if not path.is_file():
        return f"unmeasured: config not found at {path}"
    try:
        return load_settings(path)
    except Exception as exc:  # an unreadable config is an unmeasured mode, not a crash
        return f"unmeasured: config unreadable ({type(exc).__name__})"


def modes_from(projects: Sequence[str], loaded: MindwireSettings | str) -> dict[str, str]:
    """``[tierc_gate].mode`` for each project from a :func:`load_config` result.

    The daemon reads one ``mindwire.toml`` for every project it drives, so every project gets the
    same value; an unmeasured config gives every project its reason string.
    """
    if isinstance(loaded, str):
        return dict.fromkeys(projects, loaded)
    return dict.fromkeys(projects, str(loaded.tierc_gate.mode))


def read_modes(projects: Sequence[str], config_path: Path | None) -> dict[str, str]:
    """``[tierc_gate].mode`` for each project, or ``unmeasured: <reason>`` — never a default."""
    return modes_from(projects, load_config(config_path))


def _date(raw: str) -> datetime:
    return builder.parse_timestamp(raw)  # type: ignore[no-any-return]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", type=_date, required=True, help="ISO date/time, inclusive")
    parser.add_argument("--until", type=_date, required=True, help="ISO date/time, exclusive")
    parser.add_argument("--project", action="append", default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--must-admit",
        action="append",
        default=None,
        metavar="PROJECT/THREAD#MSG",
        help="regression ref the gate must not bounce (repeatable; default: msg-829, msg-740)",
    )
    parser.add_argument("--config", type=Path, default=None, help="default: the daemon's toml")
    args = parser.parse_args(argv)

    projects = tuple(args.project or builder.PROJECTS)
    loaded = load_config(args.config)
    if isinstance(loaded, str):
        # No roster, no measurement. Selection (``routing_roster``) and the role filter both
        # read the configured roster; an empty stand-in pushes every author without a
        # historical ``role`` into ``other_author`` and reports a clean 0 / 0 / 0 with an empty
        # ``must_admit_dropped`` — a false "no regressions". So nothing is fetched and no count
        # is written: the output names the reason and the run exits non-zero.
        _write(
            args.out,
            {
                "schema": 1,
                "measured_at": datetime.now(UTC).isoformat(),
                "projects": list(projects),
                "unmeasured": loaded,
            },
        )
        print(loaded, file=sys.stderr)
        return 2
    threads = asyncio.run(builder.harvest(projects))
    result = classify(
        threads,
        dict(loaded.conductor.roster),
        since=args.since,
        until=args.until,
        mode_by_project=modes_from(projects, loaded),
        must_admit=tuple(args.must_admit or DEFAULT_MUST_ADMIT),
    )
    result["measured_at"] = datetime.now(UTC).isoformat()
    result["projects"] = list(projects)
    _write(args.out, result)
    print(json.dumps(result["counts"], ensure_ascii=False), file=sys.stderr)
    return 0


def _write(out: Path, result: Mapping[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    raise SystemExit(main())
