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
    check = sub.add_parser(
        "clone-check",
        help="re-judge a parked shared clone: exit 0 clean, 8 refused (payload row), 1 unknown",
    )
    check.add_argument("--repo-dir", required=True, type=Path, help="the shared clone to judge")
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
    if args.command == "clone-check":
        raise SystemExit(clone_check(args.repo_dir))
    raise SystemExit(2)  # unreachable: argparse rejects anything else


if __name__ == "__main__":  # the sweep wrapper runs `python -m spirrow_mindwire.cli clone-check`
    main()
