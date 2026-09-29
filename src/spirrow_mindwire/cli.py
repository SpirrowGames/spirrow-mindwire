"""Mindwire operator CLI.

Only ``mindwire init-config tierc-rules`` exists today (T-decider-tierc-v2-all-escalations,
Bohr msg-4382 Objection 3): it places the Tier-C rules file (五ヶ条) at its canonical path,
``[decider.tierc].rules_path`` or ``<data_dir>/config/tierc_rules.toml``, by copying the template
shipped in the package. An existing file is **never** overwritten — it is Takahito's edited copy.
The status / debug commands are still deferred to a later phase.
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
    return parser


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
    raise SystemExit(2)  # unreachable: argparse rejects anything else
