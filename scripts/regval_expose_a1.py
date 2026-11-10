"""expose-a1: value-free reachability probe (T-registry-values-in-pushed-history, 改訂 5 §9-a).

Input is ONLY a list of ``owner/repo <full sha>`` lines (改訂 5 §9-b: transcribed, value-free,
from the original P table).  This program never reads a registry, a checker, a diff, a blob, a
ref name or a transcript, and never starts git, so it handles no secret value at all.

Order (§9-a, 改訂 7 E-1): A-1 for every sha → A-4 (all fork pages).  A-1b (scratch-mirror refs)
was removed from this program: it would start git and read free-text ref names.  It lives in
``regval_investigate.py expose`` as a count on a verified mirror.
The FIRST positive observation ends the run: it is saved to --state, ``EARLY ...`` is printed
and the exit status is 10.  A-2 / A-3 and anything left unprobed belong to a later step.

Output contract (same rules as the main script, restated here so this file reviews alone):
  * every printed field passes ``safe`` (allow-listed shape, else ``REDACTED``);
  * ``main`` points fd 1 / fd 2 at devnull (``os.dup2``) and ``sys.stdout`` / ``sys.stderr`` at
    devnull streams BEFORE argument parsing; only ``emit`` writes, to a copy of fd 1.  A parse
    failure (unknown / missing argument, ``-h``) is ``ERR ArgError`` with exit 2 — argparse's own
    text, which may echo an argument, is discarded (改訂 7 E-2);
  * child stderr is never printed; only a 3-digit HTTP status is extracted from it;
  * commit lookups use ``--jq .sha`` (the API's patch never reaches this process's output);
  * an exception is reported as ``ERR <ClassName>`` only.
Kept in parity with regval_investigate.py (``safe`` shapes, ``commit_fetchable``, ``child_env``,
``_quiet``) by tests/test_regval_parity.py.

Exit status: 0 = finished, nothing reachable observed; 10 = EARLY (a reachability was
observed); 3 = INCOMPLETE (some probe failed — those units are 不明); 2 = ERR (bad input or
crash, nothing concluded).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

_SHAPES: dict[str, re.Pattern[str]] = {
    "sha": re.compile(r"^[0-9a-f]{40}$"),
    "repo": re.compile(r"^[A-Za-z0-9_.\-]{1,100}/[A-Za-z0-9_.\-]{1,100}$"),
    "int": re.compile(r"^-?\d{1,9}$"),
    "time": re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"),
    "tag": re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,40}$"),
}

_OUT: TextIO | None = None  # copy of fd 1 taken by _quiet(); None outside main


def safe(kind: str, value: object) -> str:
    text = str(value)
    pattern = _SHAPES.get(kind)
    return text if pattern is not None and pattern.match(text) else "REDACTED"


def emit(*fields: tuple[str, object]) -> None:
    out = _OUT if _OUT is not None else sys.stdout
    out.write(" ".join(safe(k, v) for k, v in fields) + "\n")
    out.flush()


def T(v: object) -> tuple[str, object]:  # noqa: N802 — short field constructor
    return ("tag", v)


@contextlib.contextmanager
def _quiet() -> Iterator[None]:
    """fd 1 / fd 2 → devnull at the OS level; ``emit`` keeps a private copy of fd 1."""
    global _OUT
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    saved1, saved2 = os.dup(1), os.dup(2)
    out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    null_fd = os.open(os.devnull, os.O_WRONLY)
    sink = open(os.devnull, "w", encoding="utf-8", newline="\n")  # noqa: SIM115 — closed in finally
    old = (sys.stdout, sys.stderr, _OUT)
    try:
        os.dup2(null_fd, 1)
        os.dup2(null_fd, 2)
        sys.stdout = sys.stderr = sink
        _OUT = out
        yield
    finally:
        sys.stdout, sys.stderr, _OUT = old
        os.dup2(saved1, 1)
        os.dup2(saved2, 2)
        for fd in (saved1, saved2, null_fd):
            os.close(fd)
        sink.close()
        out.close()


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Inherited environment minus every GIT_* variable, plus fixed conditions (parity)."""
    src = os.environ if base is None else base
    env = {k: v for k, v in src.items() if not k.startswith("GIT_")}
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GH_PROMPT_DISABLED": "1",
        }
    )
    return env


def run3(argv: list[str]) -> tuple[int, str, str]:
    """(status, stdout, stderr).  stderr is only regex-scanned for an HTTP status; never printed."""
    try:
        p = subprocess.run(
            argv,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=child_env(),
            check=False,
            timeout=300,
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
    return "ok" if rc == 0 else f"rc{rc}" if 0 < rc < 1000 else "rcX"


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

    def record(self, kind: str, repo: str, sha: str, result: str) -> None:
        self.obs.append({"kind": kind, "repo": repo, "sha": sha, "result": result, "at": now()})
        Path(self.state).write_text(json.dumps(self.obs, indent=1), encoding="utf-8", newline="\n")
        emit(T(kind), ("repo", repo), ("sha", sha), T(result), ("time", self.obs[-1]["at"]))

    def early(self, kind: str, repo: str, sha: str) -> int:
        emit(T("EARLY"), T(kind), ("repo", repo), ("sha", sha))
        return 10


def probe(a: argparse.Namespace) -> int:
    targets = read_targets(a.shas)
    r = Run(a.state)
    # A-1: the parent repo, every sha first.
    for repo, sha in targets:
        res = commit_fetchable(repo, sha)
        r.record("A1", repo, sha, res)
        if res == "yes":
            return r.early("A1", repo, sha)
        if res not in _NEGATIVE:
            r.incomplete += 1
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


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--shas", required=True, help="lines: owner/repo <full sha>")
    ap.add_argument("--state", required=True, help="observations JSON (rewritten after each)")
    return ap


def main(argv: list[str] | None = None) -> int:
    with _quiet():
        try:
            a = _parser().parse_args(argv)
        except BaseException:  # argparse text may echo an argument; fixed record only
            emit(T("ERR"), T("ArgError"))
            return 2
        try:
            return probe(a)
        except BaseException as e:  # message could carry anything; report the class only
            emit(T("ERR"), T(type(e).__name__))
            return 2


if __name__ == "__main__":
    sys.exit(main())
