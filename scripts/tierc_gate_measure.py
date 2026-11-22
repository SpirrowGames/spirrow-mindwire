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
  counted on its own; it is not part of the role total. Split by kind in ``iv_by_kind`` (below).
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

**2e-1a-iv-split** (T-decider-conductor-hook Bohr msg-7265 / msg-7267, approved by Einstein
msg-7266). ``iv_pr_gate_relay`` is unchanged; ``iv_by_kind`` splits the same refs into exactly
one of three lists (counts in ``iv_by_kind_counts``), checked in this order:

* ``gate_ci_hold`` — the body carries a ci-hold marker (:func:`~spirrow_mindwire.conductor.
  gate_records.ci_hold_head`): the L1 CI gate's pending hold, not a review. Checked first because
  it shares the verdict relay's heading.
* ``gate_verdict`` — the first line is the verdict relay heading (:func:`~spirrow_mindwire.
  conductor.gate_records.relay_heading_pr_ref`; covers legacy verdicts without a footer and
  bodies with two footers) or the body carries exactly one verdict footer
  (:func:`~spirrow_mindwire.github.reviews.parse_verdict_footer`).
* ``relay_notice`` — everything else: conductor stop / stand-down notices, R3/R5 admission and
  R4 ci-route posts. These are not gate verdicts.

All three readers are the production ones, imported, so the measurement cannot drift from what
the pipeline counts as a verdict. Bodies are read for the check and never written out.

``retry_lookup`` is ``never_retry``: no past turn carries a ``RETRY:`` token issued by a live
gate, so "no unresolved bounce exists" is the true answer for history.

**Security** (T-tier-c-six-articles-security-boundary part 4; Bohr msg-9912 B → msg-9914 →
msg-9916, Einstein msg-9913 / 9915 / 9917). What happened to ``security`` heads is read **only**
from the gate's decisions log (``--decisions-log``, default the daemon's), never from a rerun
(:func:`security_section`):

* ``heads`` — one outcome per ``(thread, msg_id)`` carrying a ``label=security`` row inside the
  window: ``delivered`` (an ``ESCALATED`` row), else ``bounced`` (a ``BOUNCED`` row, split by
  reason), else ``no_outcome``. Only rows inside ``[--since, --until)`` decide an outcome; within
  them ``ESCALATED`` wins. Rows after the window are not read, so a later delivery never changes
  a closed window (msg-9916 Obj-2-2).
* ``hold_bounces`` — ``BOUNCED`` rows in the window with reason ``security-hold`` /
  ``security-hold-no-verdict``, whatever the head's label.
* ``holds_opened`` / ``holds_released`` — distinct hold ids (a refusal merged into an open
  identical hold opens none), replayed by :func:`~spirrow_mindwire.tier_c_security_holds.
  replay_hold_rows`. Rows from ``--replay-from`` (default: the start of the log) up to
  ``--since`` only set up which holds are open; bounces and releases inside the window count even
  for a hold opened before it (msg-9914 Obj-4).
* a release's origin: ``human`` when the thread holds a ``HOLD-RELEASE: <id>`` line by the
  conductor's ``human_identity`` posted at or before the stamped row; else ``delivery`` when the
  stamp is on an ``ESCALATED`` row and the thread was read; else ``unknown`` (the thread was not
  harvested, the row has no time, or neither applies). Text alone never makes it ``human``.

``formal_recheck`` is the rerun of :func:`decide_admission` over the same window, restricted to
``security``: a formal check, not an outcome, never added to or compared with ``security``. A log
that cannot be read gives ``security = "unmeasured: <reason>"``, never 0.

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
from spirrow_mindwire.conductor.gate_records import (
    RELAY_AUTHOR,
    ci_hold_head,
    relay_heading_pr_ref,
)
from spirrow_mindwire.conductor.handoff import HandoffKind, resolve_handoff
from spirrow_mindwire.conductor.human_identity import is_human_identity
from spirrow_mindwire.config import MindwireSettings
from spirrow_mindwire.decider.hook import TIERC_ENTRY_ROLES, never_retry
from spirrow_mindwire.github.reviews import parse_verdict_footer
from spirrow_mindwire.tier_c_admission_gate import (
    ADMIT_LABELS,
    SECURITY_LABEL,
    UNSURE_LABEL,
    AdmissionVerdict,
    BounceReason,
    LogKind,
    decide_admission,
)
from spirrow_mindwire.tier_c_decisions_log import iter_rows
from spirrow_mindwire.tier_c_security_holds import hold_release_ids, replay_hold_rows
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


#: ``iv_by_kind`` sub-bucket names. :func:`iv_kind` returns only these constants, so a
#: rename happens in one place and ``IV_KINDS`` cannot drift from what it returns.
IV_GATE_CI_HOLD = "gate_ci_hold"
IV_GATE_VERDICT = "gate_verdict"
IV_RELAY_NOTICE = "relay_notice"
#: ``iv_by_kind`` sub-buckets, in the order :func:`iv_kind` checks them.
IV_KINDS: tuple[str, ...] = (IV_GATE_CI_HOLD, IV_GATE_VERDICT, IV_RELAY_NOTICE)


def iv_kind(body: str) -> str:
    """Which ``iv_by_kind`` list a ``pr-gate-relay`` body belongs in (msg-7267 order)."""
    if ci_hold_head(body) is not None:
        return IV_GATE_CI_HOLD
    if relay_heading_pr_ref(body) is not None or parse_verdict_footer(body) is not None:
        return IV_GATE_VERDICT
    return IV_RELAY_NOTICE


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
    by_kind: dict[str, list[str]] = {k: [] for k in IV_KINDS}
    projects_seen: set[str] = set()
    bounce_reasons: Counter[str] = Counter()
    by_author: Counter[str] = Counter()
    formal_admitted: list[str] = []
    formal_bounced: dict[str, list[str]] = {}
    for project, thread_id, messages in threads:
        projects_seen.add(project)
        for i, m in enumerate(messages):
            if not (since <= m.timestamp < until):
                continue
            roster = builder.routing_roster(messages[: i + 1], current_roster)
            handoff = resolve_handoff(
                m.content, roster, next_participant=m.next_participant, posted_at=m.timestamp
            )
            if handoff.kind is not HandoffKind.HUMAN or not handoff.author_requested_human:
                continue
            ref = f"{project}/{thread_id}#{m.msg_id}"
            if m.author == RELAY_AUTHOR:
                ids["iv_pr_gate_relay"].append(ref)
                by_kind[iv_kind(m.content)].append(ref)
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
            if decision.normalized_label == SECURITY_LABEL:
                if decision.verdict is AdmissionVerdict.BOUNCE:
                    assert decision.bounce_reason is not None
                    formal_bounced.setdefault(decision.bounce_reason.value, []).append(ref)
                else:
                    formal_admitted.append(ref)
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
        "iv_by_kind": by_kind,
        "iv_by_kind_counts": {k: len(v) for k, v in by_kind.items()},
        "must_admit": must,
        "must_admit_dropped": [r for r, b in must.items() if b in ("i_bounce", NOT_FOUND)],
        # msg-9914 Obj-2: the rerun, kept apart from what the log says happened (``security``).
        "formal_recheck": {
            "note": "decide_admission rerun on security heads; a formal check, not an outcome",
            "admitted": len(formal_admitted),
            "admitted_msg_ids": formal_admitted,
            "bounced_by_reason": {r: len(v) for r, v in sorted(formal_bounced.items())},
            "bounced_by_reason_msg_ids": dict(sorted(formal_bounced.items())),
        },
    }


#: The two bounce reasons of a held head (msg-9912 B), always reported, zero or not.
HOLD_BOUNCE_REASONS: tuple[str, ...] = (
    BounceReason.SECURITY_HOLD.value,
    BounceReason.SECURITY_HOLD_NO_VERDICT.value,
)
#: Where a hold release came from (msg-9914 Obj-3, msg-9916), in the order they are checked.
RELEASE_ORIGINS: tuple[str, ...] = ("human", "delivery", "unknown")


def _row_time(row: Mapping[str, Any]) -> datetime | None:
    raw = row.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _release_origin(
    hold_id: str,
    row: Mapping[str, Any],
    at: datetime,
    thread_messages: Mapping[str, Sequence[Any] | None],
    human_identity: str,
) -> str:
    """``human`` / ``delivery`` / ``unknown`` for one stamped release (msg-9916 note).

    ``human`` needs both the author (the conductor's own :func:`is_human_identity` rule, the one
    that decides whose ``HOLD-RELEASE:`` counts) and the order (posted at or before the stamped
    row); if either cannot be confirmed it is not ``human``. ``delivery`` needs the thread to
    have been read, so that "no human line" is known rather than assumed.
    """
    messages = thread_messages.get(str(row.get("thread")))
    if messages is None:
        return "unknown"
    for m in messages:
        if (
            m.timestamp <= at
            and is_human_identity(m.author, human_identity)
            and hold_id in hold_release_ids(m.content)
        ):
            return "human"
    if row.get("kind") == LogKind.ESCALATED.value:
        return "delivery"
    return "unknown"


def security_section(
    rows: Sequence[Mapping[str, Any]],
    threads: Sequence[tuple[str, str, Sequence[Any]]],
    *,
    since: datetime,
    until: datetime,
    human_identity: str,
    replay_from: datetime | None = None,
) -> dict[str, Any]:
    """What the decisions log says happened to security heads and holds in ``[since, until)``.

    Pure; counts, hold ids and ``thread#msg_id`` refs only. ``rows`` in log (append) order. A row
    without a readable timezone-aware ``ts`` is replayed but counted nowhere
    (``rows_without_ts``).
    """
    # The log's ``thread`` is the bare thread id; one harvested in two projects is ambiguous.
    by_thread: dict[str, Sequence[Any] | None] = {}
    for _project, thread_id, messages in threads:
        by_thread[thread_id] = None if thread_id in by_thread else messages
    replayed: list[dict[str, Any]] = []
    times: list[datetime | None] = []
    without_ts = 0
    for row in rows:
        at = _row_time(row)
        if at is None:
            without_ts += 1
        elif at >= until or (replay_from is not None and at < replay_from):
            continue  # after the window: never read (Obj-2-2); before replay_from: not replayed
        replayed.append(dict(row))
        times.append(at)

    heads: dict[str, list[Mapping[str, Any]]] = {}
    hold_bounces: dict[str, list[str]] = {r: [] for r in HOLD_BOUNCE_REASONS}
    opened: list[str] = []
    released: dict[str, list[str]] = {o: [] for o in RELEASE_ORIGINS}
    unmatched = 0
    for step, at in zip(replay_hold_rows(replayed), times, strict=True):
        if at is None or at < since:
            continue  # sets up the holds open at the window's start; counted nowhere
        row = step.row
        ref = f"{row.get('thread')}#{row.get('msg_id')}"
        if row.get("label") == SECURITY_LABEL:
            heads.setdefault(ref, []).append(row)
        if row.get("kind") == LogKind.BOUNCED.value and row.get("reason") in hold_bounces:
            hold_bounces[str(row.get("reason"))].append(ref)
        if step.opened is not None:
            opened.append(step.opened)
        unmatched += len(step.released_unknown)
        for hold_id in step.released:
            origin = _release_origin(hold_id, row, at, by_thread, human_identity)
            released[origin].append(hold_id)

    delivered: list[str] = []
    bounced: dict[str, list[str]] = {}
    no_outcome: list[str] = []
    for ref, head_rows in heads.items():
        kinds = [r.get("kind") for r in head_rows]
        if LogKind.ESCALATED.value in kinds:
            delivered.append(ref)  # within the window ESCALATED wins (msg-9914 Obj-2)
        elif LogKind.BOUNCED.value in kinds:
            last = [r for r in head_rows if r.get("kind") == LogKind.BOUNCED.value][-1]
            bounced.setdefault(str(last.get("reason")), []).append(ref)
        else:
            no_outcome.append(ref)
    return {
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "replay_from": replay_from.isoformat() if replay_from is not None else None,
        },
        "heads": {
            "delivered": len(delivered),
            "bounced": sum(len(v) for v in bounced.values()),
            "bounced_by_reason": {r: len(v) for r, v in sorted(bounced.items())},
            "no_outcome": len(no_outcome),
        },
        "heads_msg_ids": {
            "delivered": delivered,
            "bounced_by_reason": dict(sorted(bounced.items())),
            "no_outcome": no_outcome,
        },
        "hold_bounces": {r: len(v) for r, v in hold_bounces.items()},
        "hold_bounces_msg_ids": hold_bounces,
        "holds_opened": len(opened),
        "holds_opened_ids": opened,
        "holds_released": {o: len(v) for o, v in released.items()},
        "holds_released_ids": released,
        "release_stamps_unmatched": unmatched,
        "rows_without_ts": without_ts,
    }


def load_decisions_log(path: Path) -> list[dict[str, Any]] | str:
    """The log's rows, or ``unmeasured: <reason>`` — a missing log is not an empty one."""
    if not path.is_file():
        return f"unmeasured: decisions log not found at {path}"
    try:
        return list(iter_rows(path))
    except (OSError, UnicodeDecodeError) as exc:
        return f"unmeasured: decisions log unreadable ({type(exc).__name__})"


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
    parser.add_argument(
        "--decisions-log", type=Path, default=None, help="default: the daemon's Tier-C log"
    )
    parser.add_argument(
        "--replay-from",
        type=_date,
        default=None,
        help="replay holds from this time (default: the start of the log); counts use --since",
    )
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
    from spirrow_mindwire.config import resolve_tier_c_decisions_log_path

    log_path = args.decisions_log or resolve_tier_c_decisions_log_path(loaded)
    rows = load_decisions_log(log_path)
    result["security"] = (
        rows
        if isinstance(rows, str)
        else security_section(
            rows,
            threads,
            since=args.since,
            until=args.until,
            human_identity=loaded.conductor.human_identity,
            replay_from=args.replay_from,
        )
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
