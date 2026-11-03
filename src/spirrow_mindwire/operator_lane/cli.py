"""``python -m spirrow_mindwire.operator_lane <command>``.

* ``pass`` — the conductor's operator pass (called by ``deploy/run-conductor-scheduled.ps1``).
  Reads the sweep's candidates from ``--candidates-file``; prints ONE JSON line
  ``{"action": ..., "events": [...]}`` for the wrapper to log and notify.
* ``run-session --run-id`` — the detached runner (started by the pass, never by hand).
* ``lock acquire --holder manual`` / ``lock renew`` / ``lock release`` — the hand-driven
  ``/mindwire-operator`` session's lease (D3⁵). ``lock release --force`` is the operator's escape
  hatch: check first that nothing is running.
* ``guard`` — run the pre-launch isolation checks once and print the verdict (setup acceptance).
* ``reset`` — re-create an empty ``operator_lane.json`` after it went missing or corrupt. Check
  each thread's head first: a head that was already dispatched would be dispatched again.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path

from .chat import McpChat
from .config import load_config
from .guard import check_all
from .lane_pass import PassDeps, run_pass
from .runner import RunnerDeps, run_session
from .session import DetachedRunnerSpawner, JobSessionLauncher
from .state import (
    DEFAULT_LEASE,
    HeadsUnavailableError,
    LanePaths,
    LockHeldError,
    PsutilProbe,
    force_release,
    hold_mutex,
    initialise,
    manual_acquire,
    manual_release,
    manual_renew,
    utcnow,
)


def _data_dir(value: str | None) -> Path:
    if value:
        return Path(value)
    env = os.environ.get("MINDWIRE_PATHS__DATA_DIR")
    return Path(env) if env else Path.home() / "spirrow-mindwire-data"


def _cmd_pass(args: argparse.Namespace) -> int:
    paths = LanePaths(_data_dir(args.data_dir))
    candidates = json.loads(Path(args.candidates_file).read_text(encoding="utf-8-sig"))
    config = load_config(paths.config) if paths.config.exists() else None
    deps = PassDeps(
        paths=paths,
        chat=McpChat(config.operator_author if config else "operator"),
        spawner=DetachedRunnerSpawner(),
        probe=PsutilProbe(),
        guard=check_all,
        clock=utcnow,
        wrapper_pid=int(args.wrapper_pid),
        head_skip_path=Path(args.head_skip_path)
        if args.head_skip_path
        else paths.state_dir / "head_skip.json",
    )
    print(run_pass(list(candidates or []), deps).to_json())
    return 0


def _cmd_run_session(args: argparse.Namespace) -> int:
    paths = LanePaths(_data_dir(args.data_dir))
    config = load_config(paths.config)
    deps = RunnerDeps(
        paths=paths,
        config=config,
        chat=McpChat(config.operator_author),
        launcher=JobSessionLauncher(config.run_as_user, config.credential_target),
        guard=check_all,
        probe=PsutilProbe(),
        clock=utcnow,
    )
    return run_session(args.run_id, deps)


def _cmd_lock(args: argparse.Namespace) -> int:
    paths = LanePaths(_data_dir(args.data_dir))
    now = utcnow()
    ttl = timedelta(minutes=args.ttl) if args.ttl else DEFAULT_LEASE
    try:
        with hold_mutex(paths.mutex):
            if args.lock_cmd == "acquire":
                target = {
                    k: v
                    for k, v in (
                        ("project", args.project),
                        ("thread_id", args.thread),
                        ("head_msg_id", args.head),
                    )
                    if v
                }
                lock = manual_acquire(
                    paths, lease_id=uuid.uuid4().hex[:16], ttl=ttl, now=now, target=target
                )
                print(json.dumps({"lease_id": lock["lease_id"], "expires_at": lock["expires_at"]}))
            elif args.lock_cmd == "renew":
                expires = manual_renew(paths, lease_id=args.lease_id, ttl=ttl, now=now)
                print(json.dumps({"lease_id": args.lease_id, "expires_at": expires.isoformat()}))
            elif args.force:
                kept = force_release(paths, now)
                print(f"released (copy kept at {kept})" if kept else "no lock to release")
            else:
                manual_release(paths, lease_id=args.lease_id)
                print("released")
    except LockHeldError as exc:
        print(f"operator lane lock: {exc}", file=sys.stderr)
        return 2
    return 0


def _cmd_guard(args: argparse.Namespace) -> int:
    paths = LanePaths(_data_dir(args.data_dir))
    verdict = check_all(load_config(paths.config))
    print(json.dumps({"status": verdict.status, "problems": list(verdict.problems)}))
    return 0 if verdict.ok else 1


def _cmd_reset(args: argparse.Namespace) -> int:
    paths = LanePaths(_data_dir(args.data_dir))
    with hold_mutex(paths.mutex):
        initialise(paths)
    print(f"reset: {paths.heads} now has an empty heads map")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m spirrow_mindwire.operator_lane")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pass")
    p.add_argument("--candidates-file", required=True)
    p.add_argument("--wrapper-pid", required=True, type=int)
    p.add_argument("--head-skip-path", default=None)
    p.add_argument("--data-dir", default=None)
    p.set_defaults(func=_cmd_pass)

    r = sub.add_parser("run-session")
    r.add_argument("--run-id", required=True)
    r.add_argument("--data-dir", default=None)
    r.set_defaults(func=_cmd_run_session)

    lk = sub.add_parser("lock")
    lk.add_argument("lock_cmd", choices=("acquire", "renew", "release"))
    lk.add_argument("--holder", choices=("manual",), default="manual")
    lk.add_argument("--lease-id", default=None)
    lk.add_argument("--ttl", type=int, default=None, help="minutes (default 60)")
    lk.add_argument("--project", default=None)
    lk.add_argument("--thread", default=None)
    lk.add_argument("--head", default=None)
    lk.add_argument("--force", action="store_true")
    lk.add_argument("--data-dir", default=None)
    lk.set_defaults(func=_cmd_lock)

    g = sub.add_parser("guard")
    g.add_argument("--data-dir", default=None)
    g.set_defaults(func=_cmd_guard)

    rs = sub.add_parser("reset")
    rs.add_argument("--data-dir", default=None)
    rs.set_defaults(func=_cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (
        args.cmd == "lock"
        and args.lock_cmd in ("renew", "release")
        and not (args.lease_id or args.force)
    ):
        print("--lease-id is required (or --force for release)", file=sys.stderr)
        return 2
    try:
        return int(args.func(args))
    except HeadsUnavailableError as exc:
        print(f"operator lane: {exc}", file=sys.stderr)
        return 2


__all__ = ["build_parser", "main"]
