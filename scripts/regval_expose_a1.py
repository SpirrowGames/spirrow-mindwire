"""expose-a1: value-free reachability probe (T-registry-values-in-pushed-history, 改訂 5 §9-a).

Input is ONLY a list of ``owner/repo <full sha>`` lines (改訂 5 §9-b: transcribed, value-free,
from the original P table).  This program never reads a registry, a checker, a diff, a blob or
a transcript, so it handles no secret value at all.

Order (§9-a): A-1 for every sha → A-1b (scratch mirrors, optional) → A-4 (all fork pages).
The FIRST positive observation ends the run: it is saved to --state, ``EARLY ...`` is printed
and the exit status is 10.  A-2 / A-3 and anything left unprobed belong to a later step.

Output contract (same rules as the main script, restated here so this file reviews alone):
  * every printed field passes ``safe`` (allow-listed shape, else ``REDACTED``);
  * child stderr is never printed; only a 3-digit HTTP status is extracted from it;
  * commit lookups use ``--jq .sha`` (the API's patch never reaches this process's output);
  * Python stderr goes to devnull; an exception is reported as ``ERR <ClassName>`` only.

Exit status: 0 = finished, nothing reachable observed; 10 = EARLY (a reachability was
observed); 3 = INCOMPLETE (some probe failed — those units are 不明); 2 = ERR (bad input or
crash, nothing concluded).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SHAPES: dict[str, re.Pattern[str]] = {
    "sha": re.compile(r"^[0-9a-f]{40}$"),
    "repo": re.compile(r"^[A-Za-z0-9_.\-]{1,100}/[A-Za-z0-9_.\-]{1,100}$"),
    "path": re.compile(r"^[A-Za-z0-9_.\-/]{1,200}$"),
    "int": re.compile(r"^-?\d{1,9}$"),
    "time": re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"),
    "tag": re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,40}$"),
}


def safe(kind: str, value: object) -> str:
    text = str(value)
    pattern = _SHAPES.get(kind)
    return text if pattern is not None and pattern.match(text) else "REDACTED"


def emit(*fields: tuple[str, object]) -> None:
    sys.stdout.write(" ".join(safe(k, v) for k, v in fields) + "\n")
    sys.stdout.flush()


def T(v: object) -> tuple[str, object]:  # noqa: N802 — short field constructor
    return ("tag", v)


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GH_PROMPT_DISABLED": "1",
}


def run3(argv: list[str]) -> tuple[int, str, str]:
    """(status, stdout, stderr).  stderr is only regex-scanned for an HTTP status; never printed."""
    try:
        p = subprocess.run(
            argv, capture_output=True, stdin=subprocess.DEVNULL, env=_ENV, check=False, timeout=300
        )
    except subprocess.TimeoutExpired:
        return 124, "", ""
    except OSError:
        return 127, "", ""
    return (
        p.returncode,
        p.stdout.decode("utf-8", errors="replace"),
        p.stderr.decode("utf-8", errors="replace"),
    )


def rc_tag(rc: int) -> str:
    return f"rc{rc}" if 0 < rc < 1000 else "rcX"


def commit_fetchable(repo: str, sha: str) -> str:
    """'yes' / 'no404' / 'no422' / 'mismatch' / 'rcN'.  Only the sha field is ever requested."""
    rc, out, err = run3(["gh", "api", f"repos/{repo}/commits/{sha}", "--jq", ".sha"])
    if rc == 0:
        return "yes" if out.strip() == sha else "mismatch"
    m = re.search(r"\(HTTP (\d{3})\)", err)
    return f"no{m.group(1)}" if m else rc_tag(rc)


_NEGATIVE = ("no404", "no422")  # an answer from GitHub, not a failure


def read_targets(path: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if (
            len(parts) != 2
            or safe("repo", parts[0]) == "REDACTED"
            or safe("sha", parts[1]) == "REDACTED"
        ):
            raise ValueError("input line is not 'owner/repo <full sha>'")
        out.append((parts[0], parts[1]))
    if not out:
        raise ValueError("empty input")
    return out


class Run:
    def __init__(self, state: str) -> None:
        self.state = state
        self.obs: list[dict[str, Any]] = []
        self.incomplete = 0

    def record(self, kind: str, repo: str, sha: str, result: str, extra: int | None = None) -> None:
        self.obs.append(
            {"kind": kind, "repo": repo, "sha": sha, "result": result, "n": extra, "at": now()}
        )
        Path(self.state).write_text(json.dumps(self.obs, indent=1), encoding="utf-8", newline="\n")
        fields: list[tuple[str, object]] = [T(kind), ("repo", repo), ("sha", sha), T(result)]
        if extra is not None:
            fields.append(("int", extra))
        emit(*fields, ("time", self.obs[-1]["at"]))

    def early(self, kind: str, repo: str, sha: str) -> int:
        emit(T("EARLY"), T(kind), ("repo", repo), ("sha", sha))
        return 10


def probe(a: argparse.Namespace) -> int:
    targets = read_targets(a.shas)
    mirrors: dict[str, str] = {}
    for spec in a.mirror or []:
        repo, _, path = spec.partition("=")
        mirrors[repo] = path
    r = Run(a.state)
    # A-1: the parent repo, every sha first.
    for repo, sha in targets:
        res = commit_fetchable(repo, sha)
        r.record("A1", repo, sha, res)
        if res == "yes":
            return r.early("A1", repo, sha)
        if res not in _NEGATIVE:
            r.incomplete += 1
    # A-1b: refs of an existing scratch mirror (empty is NOT a negative).
    for repo, sha in targets:
        mirror = mirrors.get(repo)
        if not mirror:
            r.record("A1b", repo, sha, "nomirror")
            continue
        rc, out, _ = run3(
            [
                "git",
                "-C",
                mirror,
                "for-each-ref",
                f"--contains={sha}",
                "--format=%(refname)",
                "refs/",
            ]
        )
        if rc:
            r.record("A1b", repo, sha, rc_tag(rc))
            r.incomplete += 1
            continue
        refs = out.split()
        r.record("A1b", repo, sha, "refs", len(refs))
        for ref in refs:
            emit(T("A1bREF"), ("sha", sha), ("path", ref))
        if refs:
            return r.early("A1b", repo, sha)
    # A-4: every fork page, then each sha through each fork's URL.
    for repo in sorted({t[0] for t in targets}):
        rc, out, _ = run3(
            ["gh", "api", "--paginate", f"repos/{repo}/forks?per_page=100", "--jq", ".[].full_name"]
        )
        if rc:
            emit(T("A4LIST"), ("repo", repo), T(rc_tag(rc)))
            r.incomplete += 1
            continue
        forks = out.split()
        emit(T("A4LIST"), ("repo", repo), T("ok"), ("int", len(forks)))
        for fork in forks:
            for _, sha in (t for t in targets if t[0] == repo):
                res = commit_fetchable(fork, sha)
                r.record("A4", fork, sha, res)
                if res == "yes":
                    return r.early("A4", fork, sha)
                if res not in _NEGATIVE:
                    r.incomplete += 1
    if r.incomplete:
        emit(T("INCOMPLETE"), ("int", r.incomplete))
        return 3
    emit(T("DONE"), T("nothing-reachable-observed"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="value-free A-1 / A-1b / A-4 probe")
    ap.add_argument("--shas", required=True, help="lines: owner/repo <full sha>")
    ap.add_argument("--state", required=True, help="observations JSON (rewritten after each)")
    ap.add_argument("--mirror", action="append", help="owner/repo=<scratch mirror path>")
    a = ap.parse_args(argv)
    sys.stderr = open(os.devnull, "w", newline="\n")  # noqa: SIM115 — nothing may reach stderr
    try:
        return probe(a)
    except BaseException as e:  # message could carry anything; report the class only
        emit(T("ERR"), T(type(e).__name__))
        return 2


if __name__ == "__main__":
    sys.exit(main())
