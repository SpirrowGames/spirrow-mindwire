"""Print the predicted resource identity for one or more sweep ``repo_dir`` values.

Used by the sweep wrapper (``deploy/run-conductor-scheduled.ps1``) to add a
resource axis to the HOLD gate — see the specifying thread
``T-loop-control-keyed-by-project-resource-is-the-repo`` §5(a) v2 and
``src/spirrow_mindwire/loop_resource.py`` for the design.

The wrapper is PowerShell; this script is the single reference implementation
of the D-KEY-1 normalization in Python. Dual-management in a second language
is exactly what E-53 in that thread ruled out — the wrapper shells out to
this CLI for every distinct ``repo_dir`` in the sweep (currently 7) once per
tick and treats the result as a predicted (not fail-closed) value.

Input:
    --repo-dir <PATH>         (repeatable)
Or:
    --stdin-json              (reads {"repo_dirs": ["...", "..."]} from stdin)

Output (stdout, always exactly one JSON object, ASCII-only):
    {"resolutions": [
        {"repo_dir": "...", "resource": "<host>/<org>/<repo>", "reason": null},
        {"repo_dir": "...", "resource": null,                  "reason": "..."}
    ]}

Every input entry gets exactly one output entry, in input order. Exit code is
always 0 on well-formed input — resolution failures are represented in the
``reason`` field, not as a non-zero exit. This mirrors ``loop_control.py``'s
fail-open posture: the wrapper is the OPTIMISATION layer; its job is to
suppress avoidable launches, not to be the enforcement authority. A blanket
non-zero exit here would take EVERY candidate on the tick out (the caller
falls back to project-only judgment), which is worse than surfacing
per-candidate reasons the wrapper can log individually.

An exit code of 2 means genuinely malformed CLI input (bad arguments, JSON
that will not parse). The caller should treat that as a wiring bug, not a
resolution failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from spirrow_mindwire.loop_resource import ResourceResolution, resolve_batch

# Windows stdout defaults to a legacy codepage; the machine-read JSON on
# stdout uses ``ensure_ascii=True`` and stays 7-bit clean. Only stderr may
# carry native error text — matches thread_heads.py / loop_control.py.
_reconfigure_err = getattr(sys.stderr, "reconfigure", None)
if _reconfigure_err is not None:
    _reconfigure_err(errors="backslashreplace")


def _read_repo_dirs(args: argparse.Namespace) -> list[str]:
    if args.stdin_json:
        try:
            payload = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(f"resolve_resource: cannot parse stdin JSON: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        if not isinstance(payload, dict):
            print("resolve_resource: stdin JSON must be an object", file=sys.stderr)
            raise SystemExit(2)
        rds = payload.get("repo_dirs")
        if not isinstance(rds, list) or not all(isinstance(x, str) for x in rds):
            print(
                "resolve_resource: stdin JSON must have 'repo_dirs': [str, ...]",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return rds
    if not args.repo_dir:
        print(
            "resolve_resource: at least one --repo-dir is required (or --stdin-json)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return list(args.repo_dir)


def _to_json_row(res: ResourceResolution) -> dict[str, object]:
    row = asdict(res)
    # asdict already yields the fields we want; keep the mapping explicit for
    # the machine reader's contract.
    return {
        "repo_dir": row["repo_dir"],
        "resource": row["resource"],
        "reason": row["reason"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-dir",
        action="append",
        default=None,
        help="One repo_dir to resolve; repeat for a batch",
    )
    parser.add_argument(
        "--stdin-json",
        action="store_true",
        help="Read {'repo_dirs': [str, ...]} from stdin instead of --repo-dir",
    )
    args = parser.parse_args()

    repo_dirs = _read_repo_dirs(args)
    resolutions = resolve_batch(repo_dirs)

    print(
        json.dumps(
            {"resolutions": [_to_json_row(r) for r in resolutions]},
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
