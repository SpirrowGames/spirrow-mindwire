"""Mindwire operator CLI.

Only ``mindwire init-config tierc-rules`` exists today (T-decider-tierc-v2-all-escalations,
Bohr msg-4382 Objection 3): it places the Tier-C rules file (五ヶ条) at its canonical path,
``[decider.tierc].rules_path`` or ``<data_dir>/config/tierc_rules.toml``, by copying the template
shipped in the package. An existing file is **never** overwritten — it is Takahito's edited copy.
The status / debug commands are still deferred to a later phase.

``mindwire pr-diff-size --repo o/r --pr N [--head SHA]`` (T-fix-now-vs-followup-is-mechanical)
measures a PR's diff exactly as the naysayer gate does and prints ``decision=fix-now`` (exit 0)
or ``decision=split`` (exit 3); see :mod:`spirrow_mindwire.naysayer.pr_diff_size`.

``mindwire clone-check --repo-dir <dir>`` (T-clone-guard-pin-ignored-only-in-mindwire, design
v2.1 D-3c, Bohr msg-6162 / msg-6164) re-judges a shared clone the sweep wrapper has parked after a
dirty-clone exit 8. It runs exactly what the dispatcher runs before a dispatch —
:func:`~spirrow_mindwire.pin_ignore.ensure_pin_ignored` then
:meth:`~spirrow_mindwire.clone_guard.CloneGuard.check` — and nothing else (no model session, no
MCP). Exit 0 = clean, prints nothing. Exit 8 = refused, prints the one
``MINDWIRE_DIRTY_CLONE_PAYLOAD <json>`` row through the same
:func:`~spirrow_mindwire.clone_guard.emit_dirty_clone_payload` the daemon uses. Exit 1 = the
clone's state could not be judged (any other exception): a one-line summary on stderr, no
traceback, so "could not tell" is never confused with "dirty".

``mindwire clone-recover --repo-dir <dir>`` (T-dirty-clone-wip-retreat-and-silent-stall-alerts,
PR-0) switches a ``wrong_head`` clone back to its default branch when every commit on its branch is
already on the remote; see :mod:`spirrow_mindwire.clone_recover`. Exit 0 recovered, 3 not
eligible (nothing changed), 1 failed. The wrapper calls it before ``clone-check``, which still
makes the release decision.

``mindwire clone-retreat --repo-dir <dir> --thread-id <id> --head-msg <msg> --stop-reason <r>
--state-dir <dir>`` (same thread, PR-1a) runs after a launch: WIP to a ``wip/`` branch, or export a
patch to ``--state-dir`` and discard; see :mod:`spirrow_mindwire.clone_retreat`. Exit 0 clean /
retreated / discarded, 4 discard incomplete (still parked), 1 failed.

``mindwire slot-prep --slots-root <dir> --repo <name> --thread-id <id> --thread-branch <b>
--origin-url <url> --state-dir <dir>`` (T-ephemeral-impl-clone-slot-per-thread, PR-A) runs the
turn-pre procedure for one thread's clone slot; see :mod:`spirrow_mindwire.clone_slot_cli`. Exit 0
launch, 3 skip, 4 park (one payload row each), 1 failed.

``mindwire continue-report [--days 7] [--config PATH]`` (T-implementer-self-continuation PR-2,
D-3) prints the weekly view of implementer self-continuations, read from the continuation ledger
(``<data_dir>/state/continuation_ledger.jsonl``) and nothing else: per thread, the admitted
continuations, the highest ``n``, the cap hits and the clone refusals. Exit 0.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import load_settings, resolve_tierc_rules_path
from .decider.questions import tierc_rules_template_path


def init_tierc_rules(dest: Path, *, template: Path | None = None) -> bool:
    """Copy the packaged template to ``dest`` unless ``dest`` exists. ``True`` = written."""
    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template if template is not None else tierc_rules_template_path(), dest)
    return True


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mindwire", description="Mindwire operator CLI.")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-config", help="place a config file from its packaged template")
    init.add_argument("target", choices=["tierc-rules"], help="which config file to place")
    init.add_argument(
        "--config",
        type=Path,
        default=None,
        help="mindwire.toml to resolve the destination from (default: the standard location)",
    )
    size = sub.add_parser(
        "pr-diff-size",
        help="measure a PR's diff as the naysayer gate does; decide fix-now (exit 0) or split (3)",
    )
    size.add_argument("--repo", required=True, help="owner/repo")
    size.add_argument("--pr", required=True, type=int, help="pull request number")
    size.add_argument(
        "--head",
        default=None,
        help="head commit SHA to measure (default: local `git rev-parse HEAD`; must be pushed)",
    )
    band = sub.add_parser(
        "codex-band-count",
        help="count requests the Lexora ledger marks as in the codex prompt-limit band",
    )
    band.add_argument("--limit", type=int, default=100_000, help="newest ledger rows to read")
    check = sub.add_parser(
        "clone-check",
        help="re-judge a parked shared clone: exit 0 clean, 8 refused (payload row), 1 unknown",
    )
    check.add_argument("--repo-dir", required=True, type=Path, help="the shared clone to judge")
    recover = sub.add_parser(
        "clone-recover",
        help="switch a wrong_head clone back to its default branch if that loses no commit",
    )
    recover.add_argument("--repo-dir", required=True, type=Path, help="the shared clone")
    retreat = sub.add_parser(
        "clone-retreat",
        help="after a launch: move a dirty clone's WIP to a wip/ branch, or export and discard it",
    )
    retreat.add_argument("--repo-dir", required=True, type=Path, help="the shared clone")
    retreat.add_argument("--thread-id", required=True, help="the thread the launch ran")
    retreat.add_argument("--head-msg", default="-", help="the head msg the launch ran on")
    retreat.add_argument("--stop-reason", default="-", help="how the launch ended")
    retreat.add_argument(
        "--state-dir", required=True, type=Path, help="where discard patches are written"
    )
    retreat.add_argument(
        "--slot-mode",
        action="store_true",
        help="[loop].slots_root is set: never discard; leave the WIP for the turn-pre procedure",
    )
    slot = sub.add_parser(
        "slot-prep",
        help="turn-pre procedure for one thread's clone slot: exit 0 launch, 3 skip, 4 park",
    )
    slot.add_argument("--slots-root", required=True, type=Path, help="[loop].slots_root")
    slot.add_argument("--repo", required=True, help="repo name (the slot / mirror directory)")
    slot.add_argument("--thread-id", required=True, help="the thread the turn is for")
    slot.add_argument("--thread-branch", required=True, help="the thread's loop/... branch")
    slot.add_argument("--origin-url", required=True, help="GitHub remote for the slot's origin")
    slot.add_argument("--default-branch", default="main", help="the repo's default branch")
    slot.add_argument(
        "--state-dir", required=True, type=Path, help="where preserved artefacts and rows go"
    )
    slot.add_argument(
        "--refresh-mirror",
        action="store_true",
        help="fetch the repo's bare mirror first (the wrapper's once-per-tick-per-repo call)",
    )
    cont = sub.add_parser(
        "continue-report",
        help="weekly view of implementer self-continuations, from the continuation ledger",
    )
    cont.add_argument("--days", type=int, default=7, help="window length in days (default 7)")
    cont.add_argument(
        "--config",
        type=Path,
        default=None,
        help="mindwire.toml to resolve the ledger path from (default: the standard location)",
    )
    iso = sub.add_parser(
        "turn-isolation-check",
        help="S8: check the turn user's credentials and clones; exit 0 ok/off, 10 stop",
    )
    iso.add_argument(
        "--clone", action="append", default=[], type=Path, help="a clone to probe (repeatable)"
    )
    iso.add_argument(
        "--config",
        type=Path,
        default=None,
        help="mindwire.toml to read [turn.isolation] from (default: the standard location)",
    )
    return parser


def clone_check(repo_dir: Path) -> int:
    """``mindwire clone-check``: ensure → guard, mapped to exit 0 / 8 / 1 (see module docstring)."""
    from .clone_guard import (
        DIRTY_CLONE_EXIT_CODE,
        CloneGuard,
        DirtyCloneError,
        emit_dirty_clone_payload,
    )
    from .pin_ignore import ensure_pin_ignored

    try:
        ensure_pin_ignored(repo_dir)
        CloneGuard(repo_dir).check()
    except DirtyCloneError as exc:
        emit_dirty_clone_payload(exc)
        return DIRTY_CLONE_EXIT_CODE
    except Exception as exc:
        summary = " ".join(str(exc).split()) or "-"
        name = type(exc).__name__
        print(f"clone-check: cannot judge {repo_dir}: {name}: {summary}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point for the `mindwire` operator CLI."""
    args = _build_parser().parse_args(argv)
    if args.command == "init-config" and args.target == "tierc-rules":
        dest = resolve_tierc_rules_path(load_settings(args.config))
        if init_tierc_rules(dest):
            print(f"wrote {dest} (edit it, then restart the conductor)")
        else:
            print(f"{dest} already exists; left unchanged", file=sys.stderr)
        return
    if args.command == "pr-diff-size":
        from .naysayer.pr_diff_size import run

        raise SystemExit(run(args.repo, args.pr, args.head))
    if args.command == "codex-band-count":
        from .naysayer.codex_band_count import run as run_band_count

        raise SystemExit(run_band_count(args.limit))
    if args.command == "clone-check":
        raise SystemExit(clone_check(args.repo_dir))
    if args.command == "clone-recover":
        from .clone_recover import run as run_clone_recover

        raise SystemExit(run_clone_recover(args.repo_dir))
    if args.command == "clone-retreat":
        from .clone_retreat import run as run_clone_retreat

        raise SystemExit(
            run_clone_retreat(
                args.repo_dir,
                thread_id=args.thread_id,
                head_msg=args.head_msg,
                stop_reason=args.stop_reason,
                state_dir=args.state_dir,
                slot_mode=args.slot_mode,
            )
        )
    if args.command == "slot-prep":
        from .clone_slot_cli import claude_sessions_under
        from .clone_slot_cli import run as run_slot_prep
        from .clone_slot_prep import SlotPrep
        from .clone_slot_trust import trust_preparer

        raise SystemExit(
            run_slot_prep(
                SlotPrep(
                    slots_root=args.slots_root,
                    repo=args.repo,
                    thread_id=args.thread_id,
                    origin_url=args.origin_url,
                    thread_branch=args.thread_branch,
                    default_branch=args.default_branch,
                    state_dir=args.state_dir,
                    sessions_under=claude_sessions_under(),
                    prepare_slot=trust_preparer(),
                ),
                refresh=args.refresh_mirror,
            )
        )
    if args.command == "continue-report":
        from .conductor.continuation_ledger import report_text
        from .config import resolve_continuation_ledger_path

        if args.days < 1:
            print("continue-report: --days must be >= 1", file=sys.stderr)
            raise SystemExit(2)
        path = resolve_continuation_ledger_path(load_settings(args.config))
        print(report_text(path, days=args.days))
        raise SystemExit(0)
    if args.command == "turn-isolation-check":
        from .turn_isolation.check import run as run_isolation_check

        user = load_settings(args.config).turn.isolation.user
        raise SystemExit(run_isolation_check(user, args.clone))
    raise SystemExit(2)  # unreachable: argparse rejects anything else


if __name__ == "__main__":  # the sweep wrapper runs `python -m spirrow_mindwire.cli clone-check`
    main()
