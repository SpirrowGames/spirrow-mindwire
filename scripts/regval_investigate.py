"""Read-only investigation: registry values in pushed history (T-registry-values-in-pushed-history).

Implements the procedure agreed in that thread (Bohr 改訂 3, msg-8986, on top of 改訂 2 msg-8984;
revised by 改訂 5 msg-8994, 改訂 6 msg-8996 and 改訂 7 msg-9000).  It does NOT decide anything:
it emits observations, and Bohr turns them into 確認 / 否定 / 不明.

OUTPUT CONTRACT (§0-b) — the reason this file exists as a reviewed script:
  * Secret values never leave this process.  They are read from the registry blob or the commit
    diff in memory, compared only as SHA-256 digests, and dropped.
  * Values are never passed as arguments or command lines (so they cannot land in a transcript).
  * Every line printed goes through ``emit``: each field must match an allow-listed shape
    (sha / repo / path / line / item name / enum / int / ISO time / uuid).  A field that does not
    match is printed as ``REDACTED`` — never as itself.
  * ``main`` points fd 1 and fd 2 at devnull at the OS level (``os.dup2``) BEFORE argument
    parsing, and replaces ``sys.stdout`` / ``sys.stderr`` with devnull streams (改訂 5 S-2,
    改訂 7 E-2).  ``emit`` alone writes, to a copy of fd 1 taken before the redirection.  So a
    ``print``, ``sys.stderr.write`` or ``os.write(1|2, ...)`` inside the executed guard checker,
    or an argparse diagnostic, reaches devnull.  A parse failure is reported as ``ERR ArgError``.
  * Child-process stderr is captured and discarded; only the exit status is kept, mapped to a
    fixed code.
  * Every exception is caught at the top and reported as ``ERR <step> <ExceptionClassName>``.
FAILURES ARE NOT OBSERVATIONS (改訂 5 S-4): every git / gh call checks its exit status.  A failed
read prints ``ERR <step> <unit> <code>`` and never turns into "no finding" / "other blob" /
"not reproduced".  A step with any such failure ends with ``INCOMPLETE <n>`` and exit status 3;
the failed units are 不明.  ``table`` with a failure does NOT write --state at all.
MUTABLE STATE (改訂 4 §8, 改訂 5 S-1 / S-3):
  * refs, reflogs, config and hooks come ONLY from the §1-1 preserved copy (--preserve).  The
    preserved copy and its re-hash at 07:16Z matched; whether it is a consistent single-instant
    snapshot is UNCONFIRMED, and nothing here relies on it being one.
  * The original clones (and spirrow-docs) are used ONLY as object stores, through ``git_obj``,
    which refuses any revision argument that is not a full 40-hex sha.
  * Every git call runs with all inherited ``GIT_*`` variables removed (so ``GIT_DIR``,
    ``GIT_WORK_TREE``, ``GIT_OBJECT_DIRECTORY`` ... cannot redirect it), with
    ``GIT_NO_REPLACE_OBJECTS=1`` (refs/replace cannot substitute an object), no system / global
    config and no system attributes; ``show`` / ``log`` also get ``--no-ext-diff --no-textconv``.
  * Each store is measured once per step (``STORE``): shallow → its history results are 不明.
  * The guard's checker and registry are loaded from blobs at the preserved spirrow-docs HEAD
    sha, not from the spirrow-docs working tree.
  * Session transcripts (~/.claude/projects) are read as append-only logs (C-2 / C-3a).

Subcommands (run in this order):
  table     §1-3  re-derive candidate P (改訂 4 §7-a: a CANDIDATE list, not the fixed P) from the
                  preserved refs/remotes/origin tips: pushed commits in the window whose added
                  Markdown lines the guard's own scanner flags; value-free rows → --state.
  mirror    §1-2  ``git clone --mirror`` each project into --mirror-dir (state acquisition only).
  expose    §2    A-2 / A-3, and A-1b as a ref COUNT on a verified scratch mirror.  A-1 / A-4 are
                  ``regval_expose_a1.py`` (改訂 5 §9-a, S-5).
  registry  §3    B-1 (hash equality against the registry version(s) in force by history at the
                  author / committer instants) and B-2 HEAD comparison (which registry blob the
                  spirrow-docs HEAD commit held then; the working tree itself is unestablished).
  creation  §4    C-1 candidates from preserved reflogs (create vs import), C-2 candidates from
                  session transcripts (C2CAND, never creation evidence on its own), C-3a guard
                  output in the matched commit tool_results, C-3b preserved-snapshot state.

Environment-specific locations are arguments (--sandbox, --docs-root), not constants (改訂 5 S-7).
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import types
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

WIN_START = datetime(2026, 10, 2, 1, 28, 37, tzinfo=UTC)  # snapshot-2 window (msg-8164)
REGISTRY_REL = "docs/platform/infra-registry.md"
CHECKER_REL = "scripts/check_infra_placeholders.py"
HOOK_REL = "githooks/pre-commit"
# Set from --sandbox / --docs-root / --projects-root by ``configure``.
SANDBOX = ""
DOCS_ROOT = ""
PROJECTS_ROOT = ""

# ---------------------------------------------------------------------------------------------
# Output filter.  Everything printed goes through emit(); nothing else may print.

_SHAPES: dict[str, re.Pattern[str]] = {
    "sha": re.compile(r"^[0-9a-f]{40}$"),
    "repo": re.compile(r"^[A-Za-z0-9_.\-]{1,100}/[A-Za-z0-9_.\-]{1,100}$"),
    "project": re.compile(r"^[a-z0-9\-]{1,40}$"),
    "path": re.compile(r"^[A-Za-z0-9_.\-/]{1,200}$"),
    "int": re.compile(r"^-?\d{1,9}$"),
    "ints": re.compile(r"^\d{1,6}(?:,\d{1,6}){0,99}$"),
    "item": re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,63}$"),
    "time": re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+\-]\d{2}:\d{2}|Z)$"),
    "uuid": re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
    "tag": re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,40}$"),
}

_OUT: TextIO | None = None  # copy of fd 1 taken by _quiet(); None outside main


def safe(kind: str, value: object) -> str:
    """Return ``value`` as text only if it has the allow-listed shape ``kind``; else REDACTED."""
    text = str(value)
    pattern = _SHAPES.get(kind)
    if pattern is None or not pattern.match(text):
        return "REDACTED"
    return text


def emit(*fields: tuple[str, object]) -> None:
    """Print one record.  Each field is (shape, value); shapes are checked by ``safe``."""
    out = _OUT if _OUT is not None else sys.stdout
    out.write(" ".join(safe(k, v) for k, v in fields) + "\n")
    out.flush()


def T(v: object) -> tuple[str, object]:  # noqa: N802 — short field constructors keep calls readable
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


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# Failure accounting (改訂 5 S-4).

_FAILS = 0


def fail(step: str, unit: tuple[str, object], code: str) -> None:
    global _FAILS
    _FAILS += 1
    emit(T("ERR"), T(step), unit, T(code))


class ReadFailedError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------------------------
# Child processes: stdout kept in memory, stderr discarded, status mapped to a code.


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Inherited environment minus every GIT_* variable, plus the fixed read conditions (S-3)."""
    env = {
        k: v for k, v in (os.environ if base is None else base).items() if not k.startswith("GIT_")
    }
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


_GIT_SAFE = ["-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "core.fsmonitor=false"]


def run(argv: list[str]) -> tuple[int, str]:
    rc, out, _ = run3(argv)
    return rc, out


def run3(argv: list[str]) -> tuple[int, str, str]:
    """(status, stdout, stderr).  stderr is returned only so a caller can pull a fixed-shape
    token (an HTTP status) out of it with a regex; it is never printed."""
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
    dec = p.stdout.decode("utf-8", errors="replace"), p.stderr.decode("utf-8", errors="replace")
    return p.returncode, dec[0], dec[1]


def git(repo: str, *args: str) -> tuple[int, str]:
    """Plain git; used only on the scratch mirror (never on an original clone)."""
    return run(["git", *_GIT_SAFE, "-C", repo, *args])


_OBJ_SUBCOMMANDS = ("log", "show", "rev-parse")
_OBJ_REV = re.compile(r"^[0-9a-f]{40}(?::[A-Za-z0-9_.\-/]+)?$")
_NO_CONVERT = ["--no-ext-diff", "--no-textconv"]


def git_obj(store: str, sub: str, *args: str) -> tuple[int, str]:
    """git on an original clone used purely as an object store (改訂 4 §8).

    Every revision must be a full 40-hex sha (optionally ``sha:path``).  A ref name, ``HEAD``,
    ``--all`` / ``--remotes`` or a reflog selector would read mutable state, so it is refused
    with ValueError before git runs.  Arguments after ``--`` are pathspecs.  ``show`` / ``log``
    always run with ``--no-ext-diff --no-textconv``.
    """
    if sub not in _OBJ_SUBCOMMANDS:
        raise ValueError("subcommand")
    seen_dashdash = False
    for arg in args:
        if seen_dashdash:
            continue
        if arg == "--":
            seen_dashdash = True
        elif arg.startswith("-"):
            if arg.startswith(
                (
                    "--all",
                    "--remotes",
                    "--branches",
                    "--tags",
                    "--glob",
                    "--reflog",
                    "-g",
                    "--walk-reflogs",
                )
            ):
                raise ValueError("ref-selecting option")
            if arg.startswith("--disambiguate") and not re.fullmatch(
                r"--disambiguate=[0-9a-f]{7,40}", arg
            ):
                raise ValueError("disambiguate prefix")
        elif not _OBJ_REV.match(arg):
            raise ValueError("revision is not a full sha")
    extra = _NO_CONVERT if sub in ("show", "log") else []
    return run(["git", *_GIT_SAFE, "-C", store, sub, *extra, *args])


def store_check(step: str, label: str, store: str) -> bool:
    """Measure a store once: shallow → its history is 不明 (False).  Prints STORE."""
    rc, out = git_obj(store, "rev-parse", "--is-shallow-repository")
    if rc:
        fail(step, ("project", label), f"store-{rc_tag(rc)}")
        return False
    rc2, refs = run(["git", *_GIT_SAFE, "-C", store, "for-each-ref", "--format=x", "refs/replace/"])
    replace_n = len(refs.split()) if rc2 == 0 else -1
    shallow = out.strip() != "false"
    emit(
        T("STORE"),
        ("project", label),
        T("shallow" if shallow else "full"),
        T("replace"),
        ("int", replace_n),
    )
    if shallow:
        fail(step, ("project", label), "shallow")
    return not shallow


def rc_tag(rc: int) -> str:
    return "ok" if rc == 0 else f"rc{rc}" if 0 < rc < 1000 else "rcX"


# ---------------------------------------------------------------------------------------------
# The guard's own scanner and registry loader (imported, so "flagged" means what the hook means).


def docs_rev(preserve: str) -> str:
    """spirrow-docs HEAD at preservation time: the last reflog entry of the preserved copy."""
    entries = reflog_entries(os.path.join(preserve, "spirrow-docs", "logs", "HEAD"))
    if not entries:
        raise ValueError("no preserved spirrow-docs HEAD")
    return entries[-1][1]


def load_guard(rev: str) -> Any:
    """The guard's checker module, executed from its blob at ``rev`` (not the working tree).

    Runs inside main's ``_quiet``: anything the checker writes to fd 1 / fd 2 reaches devnull.
    """
    rc, src = git_obj(DOCS_ROOT, "show", f"{rev}:{CHECKER_REL}")
    if rc:
        raise ValueError("checker blob unreadable")
    name = "check_infra_placeholders_pinned"
    mod = types.ModuleType(name)
    sys.modules[name] = mod  # dataclasses resolve their module through sys.modules
    exec(
        compile(src, "<checker-blob>", "exec"), mod.__dict__
    )  # the guard's own code, at a pinned sha
    return mod


def registry_at(g: Any, rev: str) -> Any:
    """Registry parsed from its blob at ``rev``, in memory; None if unparsable.

    An unreadable blob raises ReadFailedError (it is not the same thing as an unparsable one)."""
    rc, text = git_obj(DOCS_ROOT, "show", f"{rev}:{REGISTRY_REL}")
    if rc:
        raise ReadFailedError(rc_tag(rc))
    try:
        return g.load_registry(_MemText(text))
    except BaseException:  # SystemExit text may name the blob; keep inert
        return None


class _MemText:
    """Stands in for a Path so load_registry can parse a historical blob held only in memory."""

    def __init__(self, text: str) -> None:
        self._text = text

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._text

    def __str__(self) -> str:  # appears in load_registry's SystemExit message; keep it inert
        return "<registry-blob>"


def item_of(kind: str) -> tuple[str, str]:
    """('value', NAME) for a registry-value finding, ('pattern', kind) for a pattern finding."""
    m = re.match(r"^registered as \{\{([A-Z][A-Z0-9_]*)\}\}$", kind)
    return ("value", m.group(1)) if m else ("pattern", kind)


def preserved_url(base: str) -> str:
    """remote.origin.url from the preserved config copy."""
    cfg = Path(base, "config").read_text(encoding="utf-8", errors="replace")
    m = re.search(r'^\[remote "origin"\][^\[]*?^\s*url\s*=\s*(\S+)\s*$', cfg, re.M | re.S)
    return m.group(1) if m else ""


def repo_of_url(url: str) -> str:
    m = re.search(r"github\.com[/:]([^/\s]+/[^/\s]+?)(?:\.git)?\s*$", url)
    return m.group(1) if m else ""


def preserved_tips(base: str) -> list[str]:
    """Full shas of refs/remotes/origin/* in the preserved copy, resolved by ref NAME the way git
    does: packed-refs first, then a loose file of the same name overrides it."""
    by_name: dict[str, str] = {}
    packed = Path(base, "packed-refs")
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split(" ", 1)
            if (
                len(parts) == 2
                and parts[1].startswith("refs/remotes/origin/")
                and re.fullmatch(r"[0-9a-f]{40}", parts[0])
            ):
                by_name[parts[1].strip()] = parts[0]
    root = Path(base, "refs", "remotes", "origin")
    for f in root.rglob("*") if root.is_dir() else []:
        if f.is_file():
            v = f.read_text(encoding="utf-8", errors="replace").strip()
            if re.fullmatch(r"[0-9a-f]{40}", v):
                name = "refs/" + f.relative_to(Path(base, "refs")).as_posix()
                by_name[name] = v
    return sorted(set(by_name.values()))


def preserved_projects(preserve: str) -> list[str]:
    return sorted(
        d
        for d in os.listdir(preserve)
        if d != "spirrow-docs" and os.path.isdir(os.path.join(preserve, d))
    )


def store_of(project: str) -> str:
    return f"{SANDBOX}/{project}-impl"


def findings_of_commit(g: Any, project: str, sha: str, registry: Any) -> list[tuple[str, int, Any]]:
    """(path, line, Finding) for every flagged added Markdown line of ``sha``.  In memory only.

    A failed diff read raises ReadFailedError — it never becomes "no findings"."""
    rc, diff = git_obj(
        store_of(project), "show", "--format=", "-U0", "--diff-filter=ACMR", sha, "--", "*.md"
    )
    if rc:
        raise ReadFailedError(rc_tag(rc))
    out: list[tuple[str, int, Any]] = []
    for chunk in re.split(r"^diff --git ", diff, flags=re.M)[1:]:
        m = re.search(r"^\+\+\+ b/(.+)$", chunk, re.M)
        if not m:
            continue
        name = m.group(1).strip()
        for line_no, text in g.added_lines(chunk):
            for f in g.scan_text(text, Path(name), registry):
                out.append((name, line_no, f))
    return out


# ---------------------------------------------------------------------------------------------
# §1-3 table


def cmd_table(a: argparse.Namespace) -> None:
    rev = docs_rev(a.preserve)
    if not store_check("table", "spirrow-docs", DOCS_ROOT):
        return
    g = load_guard(rev)
    registry = registry_at(g, rev)
    if registry is None:
        raise ValueError("registry blob unparsable")
    emit(T("DOCSREV"), ("sha", rev))
    rows: list[dict[str, Any]] = []
    for project in preserved_projects(a.preserve):
        base = os.path.join(a.preserve, project)
        repo = repo_of_url(preserved_url(base))
        tips = preserved_tips(base)
        emit(T("TIPS"), ("project", project), ("int", len(tips)))
        if not tips:
            continue
        if not store_check("table", project, store_of(project)):
            continue
        rc, log = git_obj(store_of(project), "log", "--no-merges", "--format=%H %aI %cI", *tips)
        if rc:
            fail("table", ("project", project), f"log-{rc_tag(rc)}")
            continue
        for line in log.splitlines():
            parts = line.split()
            if len(parts) != 3 or datetime.fromisoformat(parts[1]) < WIN_START:
                continue
            sha, ai, ci = parts
            try:
                found = findings_of_commit(g, project, sha, registry)
            except ReadFailedError as e:
                fail("table", ("sha", sha), f"show-{e.code}")
                continue
            for path, line_no, f in found:
                cls, item = item_of(f.kind)
                rows.append(
                    {
                        "project": project,
                        "repo": repo,
                        "sha": sha,
                        "path": path,
                        "line": line_no,
                        "cls": cls,
                        "item": item,
                        "author": ai,
                        "committer": ci,
                    }
                )
    rows.sort(key=lambda r: (r["committer"], r["sha"], r["path"], r["line"]))
    for i, r in enumerate(rows, 1):
        r["n"] = i
        emit(
            T("ROW"),
            ("int", i),
            ("project", r["project"]),
            ("repo", r["repo"]),
            ("sha", r["sha"]),
            ("path", r["path"]),
            ("int", r["line"]),
            T(r["cls"]),
            ("item", r["item"]),
            ("time", r["author"]),
            ("time", r["committer"]),
        )
    emit(
        T("CANDIDATE_TOTAL"),
        T("commits"),
        ("int", len({r["sha"] for r in rows})),
        T("rows"),
        ("int", len(rows)),
    )
    if _FAILS:
        # An incomplete list must not look like a complete one: nothing is written.
        emit(T("TABLE"), T("not-written"))
        return
    Path(a.state).write_text(json.dumps(rows, indent=1), encoding="utf-8", newline="\n")


def load_rows(a: argparse.Namespace) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(Path(a.state).read_text(encoding="utf-8"))
    return rows


# ---------------------------------------------------------------------------------------------
# §1-2 mirror


def cmd_mirror(a: argparse.Namespace) -> None:
    for project in sorted({r["project"] for r in load_rows(a)}):
        url = preserved_url(os.path.join(a.preserve, project))
        dest = os.path.join(a.mirror_dir, f"{project}.git")
        if os.path.exists(dest):
            emit(T("MIRROR"), ("project", project), T("exists"))
            continue
        rc, _ = run(["git", *_GIT_SAFE, "clone", "--mirror", "--quiet", url.strip(), dest])
        if rc:
            fail("mirror", ("project", project), rc_tag(rc))
            continue
        emit(T("MIRROR"), ("project", project), T("ok"), ("time", now()))


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------------------------
# §2 expose — A-2 / A-3 / A-1b.  A-1 / A-4 live in regval_expose_a1.py (改訂 5 §9-a, S-5).


def commit_fetchable(repo: str, sha: str) -> str:
    """'yes' (API returned this sha) / 'no404' / 'no422' / other fixed code.  Never the patch.

    Kept in parity with regval_expose_a1.commit_fetchable (tests/test_regval_parity.py)."""
    rc, out, err = run3(["gh", "api", f"repos/{repo}/commits/{sha}", "--jq", ".sha"])
    if rc == 0:
        return "yes" if out.strip() == sha else "mismatch"
    # gh reports HTTP failures on stderr as "... (HTTP 404)"; only the 3-digit status is kept.
    m = re.search(r"\(HTTP (\d{3})\)", err)
    return f"no{m.group(1)}" if m else rc_tag(rc)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).replace("\\", "/").rstrip("/")


def mirror_verified(mirror: str) -> bool:
    """True only if git, run in the scrubbed environment, reads exactly ``mirror`` (改訂 7 E-1)."""
    rc, out = git(mirror, "rev-parse", "--absolute-git-dir")
    return rc == 0 and _norm(out.strip()) == _norm(mirror)


def cmd_expose(a: argparse.Namespace) -> None:
    rows = load_rows(a)
    repos = sorted({r["repo"] for r in rows})
    for repo in repos:
        rc, out = run(["gh", "repo", "view", repo, "--json", "visibility", "--jq", ".visibility"])
        if rc:
            fail("expose", ("repo", repo), f"A2-{rc_tag(rc)}")
        else:
            emit(T("A2"), ("repo", repo), T(out.strip().lower()), ("time", now()))
        # A-3: needs org audit log; a failure is 不明 (operator task), never "no events".
        org = repo.split("/")[0]
        rc, out = run(
            [
                "gh",
                "api",
                "--paginate",
                f"orgs/{org}/audit-log?phrase=repo:{repo}+action:repo.access&per_page=100",
                "--jq",
                '.[] | [.action, (.["@timestamp"]|tostring)] | join(" ")',
            ]
        )
        if rc:
            fail("expose", ("repo", repo), f"A3-{rc_tag(rc)}")
            continue
        n = 0
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                n += 1
                t = datetime.fromtimestamp(int(parts[1]) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                emit(T("A3"), ("repo", repo), T(parts[0].replace(".", "-")), ("time", t))
        emit(T("A3"), ("repo", repo), T("events"), ("int", n))
    # A-1b: a COUNT of refs in the verified scratch mirror containing the sha.  Ref names are
    # never printed (a ref name is free text).  An empty count is not a negative.
    for project, sha in sorted({(r["project"], r["sha"]) for r in rows}):
        mirror = os.path.join(a.mirror_dir, f"{project}.git")
        if not os.path.isdir(mirror):
            emit(T("A1b"), ("sha", sha), T("nomirror"))
            continue
        if not mirror_verified(mirror):
            fail("expose", ("sha", sha), "A1b-mirror-unverified")
            continue
        rc, out = git(mirror, "for-each-ref", f"--contains={sha}", "--format=x", "refs/")
        if rc:
            fail("expose", ("sha", sha), f"A1b-{rc_tag(rc)}")
            continue
        emit(T("A1b"), ("sha", sha), T("refs"), ("int", len(out.split())))


# ---------------------------------------------------------------------------------------------
# §3 registry


def registry_history(drev: str) -> list[tuple[datetime, str]]:
    rc, out = git_obj(DOCS_ROOT, "log", "--format=%H %cI", drev, "--", REGISTRY_REL)
    if rc:
        raise ReadFailedError(rc_tag(rc))
    hist = []
    for line in out.splitlines():
        sha, ci = line.split()
        hist.append((datetime.fromisoformat(ci), sha))
    return sorted(hist)


def rev_at(hist: list[tuple[datetime, str]], t: datetime) -> str | None:
    cur = None
    for ct, sha in hist:
        if ct <= t:
            cur = sha
    return cur


def reflog_entries(logfile: str) -> list[tuple[datetime, str, str]]:
    """(time, new sha, op) from a raw reflog file; the message after the op is never kept."""
    out = []
    try:
        lines = Path(logfile).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        head, _, msg = line.partition("\t")
        parts = head.split(" ")
        if len(parts) < 4 or not parts[-2].isdigit():
            continue
        op = msg.split(":", 1)[0].strip()
        out.append((datetime.fromtimestamp(int(parts[-2]), UTC), parts[1], op))
    return out


def head_at(entries: list[tuple[datetime, str, str]], t: datetime) -> str | None:
    cur = None
    for et, new, _ in entries:
        if et <= t:
            cur = new
    return cur


def blob_of(rev: str) -> str | None:
    rc, out = git_obj(DOCS_ROOT, "rev-parse", f"{rev}:{REGISTRY_REL}")
    v = out.strip()
    return v if rc == 0 and re.fullmatch(r"[0-9a-f]{40}", v) else None


def cmd_registry(a: argparse.Namespace) -> None:
    drev = docs_rev(a.preserve)
    if not store_check("registry", "spirrow-docs", DOCS_ROOT):
        return
    g = load_guard(drev)
    current = registry_at(g, drev)
    if current is None:
        raise ValueError("registry blob unparsable")
    emit(T("DOCSREV"), ("sha", drev))
    try:
        hist = registry_history(drev)
    except ReadFailedError as e:
        fail("registry", ("sha", drev), f"history-{e.code}")
        return
    emit(
        T("B0"),
        T("registry-versions"),
        ("int", len(hist)),
        T("first"),
        ("time", iso(hist[0][0]) if hist else ""),
    )
    docs_head = reflog_entries(os.path.join(a.preserve, "spirrow-docs", "logs", "HEAD"))
    emit(
        T("B0"),
        T("docs-reflog-entries"),
        ("int", len(docs_head)),
        T("first"),
        ("time", iso(docs_head[0][0]) if docs_head else ""),
    )
    # B-2 is a comparison of COMMITTED blobs at the spirrow-docs HEAD of that instant.  What the
    # hook actually read was the working tree, which may have held uncommitted edits: that
    # content is not established by anything here.
    emit(T("B2WT"), T("unestablished"))
    cache: dict[str, Any] = {}
    checked: set[str] = set()
    for r in load_rows(a):
        if r["project"] not in checked:
            checked.add(r["project"])
            store_check("registry", r["project"], store_of(r["project"]))
        match = None
        try:
            found = findings_of_commit(g, r["project"], r["sha"], current)
        except ReadFailedError as e:
            fail("registry", ("int", r["n"]), f"show-{e.code}")
            continue
        for path, line_no, f in found:
            if path == r["path"] and line_no == r["line"] and item_of(f.kind)[1] == r["item"]:
                match = digest(f.match)
                break
        if match is None:
            emit(T("B1"), ("int", r["n"]), T("row-not-reproduced"))
            continue
        for label in ("author", "committer"):
            t = datetime.fromisoformat(r[label])
            rev = rev_at(hist, t)
            if rev is None:
                emit(T("B1"), ("int", r["n"]), T(label), T("no-version-before"))
                continue
            if rev not in cache:
                try:
                    cache[rev] = registry_at(g, rev)
                except ReadFailedError as e:
                    fail("registry", ("sha", rev), f"blob-{e.code}")
                    continue
            reg = cache[rev]
            if reg is None:
                verdict = "unparsable"
            elif r["cls"] == "value":
                v = reg.values.get(r["item"])
                verdict = "absent" if v is None else ("match" if digest(v) == match else "differ")
            else:
                verdict = (
                    "pattern-in-values"
                    if any(digest(v) == match for v in reg.values.values())
                    else "pattern-only"
                )
            emit(T("B1"), ("int", r["n"]), T(label), ("sha", rev), T(verdict))
            head = head_at(docs_head, t)
            if head is None:
                emit(T("B2HEAD"), ("int", r["n"]), T(label), T("no-docs-reflog-before"))
                continue
            b_head, b_rev = blob_of(head), blob_of(rev)
            if b_head is None or b_rev is None:
                emit(T("B2HEAD"), ("int", r["n"]), T(label), ("sha", head), T("unknown"))
                fail("registry", ("int", r["n"]), "B2-blob-unreadable")
                continue
            emit(
                T("B2HEAD"),
                ("int", r["n"]),
                T(label),
                ("sha", head),
                T("head-same-blob" if b_head == b_rev else "head-other-blob"),
            )


def iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------------------------
# §4 creation

_CREATE_OPS = (
    "commit",
    "commit (amend)",
    "commit (initial)",
    "commit (merge)",
    "cherry-pick",
    "rebase",
)
_IMPORT_OPS = ("fetch", "pull", "checkout", "reset", "clone", "merge", "update by push", "branch")
_GUARD_MARKS = (
    ("commit_stopped", "Commit stopped by conventions"),
    ("checker_missing", "pre-commit: cannot find the checker"),
    ("no_python", "pre-commit: no working python found"),
)
_COMMIT_OUT = re.compile(r"^\[[^\]\s]+(?: \([^)]*\))? ([0-9a-f]{7,40})\]")
_COMMIT_OUT_ANY = re.compile(r"\[[^\]\s]+(?: \([^)]*\))? ([0-9a-f]{7,40})\]")
# 改訂 6 S-6 condition 1: the WHOLE command is one simple git commit / cherry-pick.
_SIMPLE_GIT = re.compile(
    r"^git(?: -C ([^\s;&|<>`$()\n#'\"]+))? (?:commit|cherry-pick)(?: [^;&|<>`$()\n#]*)?$"
)
_GIT_COMMITISH = re.compile(r"^git\b[^\n]*\b(?:commit|cherry-pick)\b")
C2_WINDOW = timedelta(minutes=10)


def op_class(op: str) -> str:
    if op.startswith(_CREATE_OPS):
        return "create"
    if op.startswith(_IMPORT_OPS):
        return "import"
    return "other"


def text_of(c: object) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(
            text_of(x.get("text", x.get("content", ""))) if isinstance(x, dict) else "" for x in c
        )
    return ""


def transcripts(project: str) -> Iterable[tuple[str, str, list[dict[str, Any]]]]:
    for pdir in sorted(
        glob.glob(os.path.join(PROJECTS_ROOT, f"C--workspace-sandbox-{project}-impl*"))
    ):
        for f in sorted(glob.glob(os.path.join(pdir, "*.jsonl"))):
            recs = []
            try:
                lines = Path(f).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                with contextlib.suppress(ValueError):
                    recs.append(json.loads(line))
            yield os.path.basename(pdir), os.path.basename(f)[:-6], recs


def _blocks(rec: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == kind]


def tool_pairs(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bash tool_use ↔ tool_result pairs joined on tool_use_id.  Text outside these blocks
    (user prose, assistant explanation) is never part of a pair."""
    uses: dict[str, dict[str, Any]] = {}
    for rec in recs:
        if rec.get("type") != "assistant":
            continue
        for b in _blocks(rec, "tool_use"):
            inp = b.get("input")
            if b.get("name") == "Bash" and isinstance(inp, dict) and isinstance(b.get("id"), str):
                uses[b["id"]] = {
                    "command": str(inp.get("command", "")),
                    "cwd": str(rec.get("cwd", "")),
                }
    pairs = []
    for rec in recs:
        if rec.get("type") != "user":
            continue
        for b in _blocks(rec, "tool_result"):
            use = uses.get(str(b.get("tool_use_id")))
            if use is None:
                continue
            pairs.append(
                {
                    **use,
                    "is_error": bool(b.get("is_error")),
                    "text": text_of(b.get("content")),
                    "timestamp": str(rec.get("timestamp", "")),
                }
            )
    return pairs


def _ts(s: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else None


def target_dirs(project: str, base: str) -> set[str]:
    """The clone's work tree and every worktree recorded in the preserved copy."""
    dirs = {_norm(store_of(project))}
    for gd in glob.glob(os.path.join(base, "worktrees", "*", "gitdir")):
        with contextlib.suppress(OSError):
            dirs.add(_norm(os.path.dirname(Path(gd).read_text(encoding="utf-8").strip())))
    return dirs


def c2_candidate(
    pair: dict[str, Any], sha: str, committer: datetime, dirs: set[str], store: str
) -> str:
    """'cand' if 改訂 6 S-6 conditions 1-5 all hold, 'mention' if the sha appears otherwise,
    'none' if not at all, 'fail' if the uniqueness check could not run."""
    first = pair["text"].split("\n", 1)[0].strip()
    m3 = _COMMIT_OUT.match(first)
    mentioned = any(sha.startswith(m.group(1)) for m in _COMMIT_OUT_ANY.finditer(pair["text"]))
    if not mentioned:
        return "none"
    cmd = pair["command"].strip()
    m1 = _SIMPLE_GIT.match(cmd)
    if not m1 or re.search(r"\bcd\b", cmd):
        return "mention"  # condition 1
    where = m1.group(1) or pair["cwd"]
    if m1.group(1) and not os.path.isabs(where):
        where = os.path.join(pair["cwd"], where)
    if _norm(where) not in dirs:
        return "mention"  # condition 2
    if pair["is_error"] or not m3 or not sha.startswith(m3.group(1)):
        return "mention"  # condition 3
    t = _ts(pair["timestamp"])
    if t is None or not (committer < t <= committer + C2_WINDOW):
        return "mention"  # condition 5
    rc, out = git_obj(store, "rev-parse", f"--disambiguate={m3.group(1)}")
    if rc:
        return "fail"
    if out.split() != [sha]:
        return "mention"  # condition 4
    return "cand"


def cmd_creation(a: argparse.Namespace) -> None:
    rows = load_rows(a)
    committer = {r["sha"]: datetime.fromisoformat(r["committer"]) for r in rows}
    shas = sorted({(r["project"], r["sha"]) for r in rows})
    checked: set[str] = set()
    for project, sha in shas:
        base = os.path.join(a.preserve, project)
        store = store_of(project)
        if project not in checked:
            checked.add(project)
            store_check("creation", project, store)
        # C-1: reflog records naming this sha as the new value, split create / import.
        logfiles = glob.glob(os.path.join(base, "logs", "**", "*"), recursive=True)
        logfiles += glob.glob(
            os.path.join(base, "worktrees", "*", "logs", "**", "*"), recursive=True
        )
        hits = 0
        for lf in sorted(x for x in logfiles if os.path.isfile(x)):
            for t, new, op in reflog_entries(lf):
                if new == sha:
                    hits += 1
                    rel = os.path.relpath(lf, base).replace("\\", "/")
                    emit(
                        T("C1"),
                        ("sha", sha),
                        ("path", rel),
                        ("time", iso(t)),
                        T(op_class(op)),
                        T(op.split(" ")[0].replace("-", "_") if op else "none"),
                    )
        emit(T("C1N"), ("sha", sha), ("int", hits))
        # C-1 identity candidate: author email vs the preserved clone config (bool only).
        rc, email = git_obj(store, "show", "-s", "--format=%ae", sha)
        if rc:
            fail("creation", ("sha", sha), f"C1ID-{rc_tag(rc)}")
        else:
            cfg = (
                Path(base, "config").read_text(encoding="utf-8", errors="replace")
                if Path(base, "config").exists()
                else ""
            )
            m = re.search(r"^\s*email\s*=\s*(\S+)\s*$", cfg, re.M)
            emit(
                T("C1ID"),
                ("sha", sha),
                T("clone-email-set" if m else "clone-email-unset"),
                T("match" if m and m.group(1) == email.strip() else "nomatch"),
            )
        # C-2: candidates only (Obj-6-1 cut).  The transcript does not record the environment
        # (GIT_DIR ...), so even a record meeting all five 改訂 6 conditions does not establish
        # which repository git wrote to: it is C2CAND, never creation evidence on its own.
        dirs = target_dirs(project, base)
        for pdir, sid, recs in transcripts(project):
            pairs = tool_pairs(recs)
            mentions = 0
            cand_at: str | None = None
            for p in pairs:
                verdict = c2_candidate(p, sha, committer[sha], dirs, store)
                if verdict == "fail":
                    fail("creation", ("sha", sha), "C2-disambiguate")
                elif verdict == "mention":
                    mentions += 1
                elif verdict == "cand" and cand_at is None:
                    cand_at = p["timestamp"]
            if mentions:
                emit(T("C2MENTION"), ("sha", sha), ("uuid", sid), ("int", mentions))
            if cand_at is None:
                continue
            emit(
                T("C2CAND"),
                ("sha", sha),
                ("path", pdir),
                ("uuid", sid),
                ("time", cand_at[:19] + "Z"),
            )
            # C-3a: guard messages in the OUTPUT of git commit / cherry-pick calls of this
            # session up to the candidate — never in prose or quoted instructions.
            for tag, needle in _GUARD_MARKS:
                n = sum(
                    1
                    for p in pairs
                    if p["timestamp"] <= cand_at
                    and _GIT_COMMITISH.match(p["command"].strip())
                    and needle in p["text"]
                )
                emit(T("C3aCAND"), ("sha", sha), ("uuid", sid), T(tag), ("int", n))
    # C-3b: the preserved snapshot's state (現状 only; never used for 当時).
    for project in sorted({p for p, _ in shas}):
        base = os.path.join(a.preserve, project)
        cfg = (
            Path(base, "config").read_text(encoding="utf-8", errors="replace")
            if Path(base, "config").exists()
            else ""
        )
        hp = re.search(r"^\s*hooksPath\s*=\s*(.+?)\s*$", cfg, re.M)
        points_docs = hp is not None and _norm(hp.group(1)) == _norm(f"{DOCS_ROOT}/githooks")
        own = os.path.isfile(os.path.join(base, "hooks", "pre-commit"))
        acq = (
            Path(base, "ACQUIRED_AT").read_text().strip()
            if Path(base, "ACQUIRED_AT").exists()
            else ""
        )
        emit(
            T("C3b"),
            ("project", project),
            T("hooksPath-docs" if points_docs else "hooksPath-other" if hp else "hooksPath-unset"),
            T("own-pre-commit" if own else "no-own-pre-commit"),
            ("time", acq),
        )
    # Guard version by history (candidate only): last change of hook / checker before each commit.
    drev = docs_rev(a.preserve)
    for r in rows:
        for rel in (HOOK_REL, CHECKER_REL):
            rc, out = git_obj(
                DOCS_ROOT, "log", "-1", "--format=%H", f"--before={r['committer']}", drev, "--", rel
            )
            if rc:
                fail("creation", ("int", r["n"]), f"C3GV-{rc_tag(rc)}")
                continue
            v = out.strip()
            emit(T("C3GV"), ("int", r["n"]), ("path", rel), ("sha", v) if v else T("none"))


# ---------------------------------------------------------------------------------------------

STEPS = ("table", "mirror", "expose", "registry", "creation")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("step", choices=STEPS)
    ap.add_argument("--state", required=True, help="value-free row file (written by table)")
    ap.add_argument("--sandbox", required=True, help="directory holding the <project>-impl clones")
    ap.add_argument("--docs-root", required=True, help="spirrow-docs clone (object store only)")
    ap.add_argument("--projects-root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--mirror-dir", default="")
    ap.add_argument("--preserve", default="", help="preserved git-dir copies (§1-1)")
    return ap


def configure(a: argparse.Namespace) -> None:
    global SANDBOX, DOCS_ROOT, PROJECTS_ROOT
    SANDBOX = a.sandbox.rstrip("/\\")
    DOCS_ROOT = a.docs_root.rstrip("/\\")
    PROJECTS_ROOT = a.projects_root


def main(argv: list[str] | None = None) -> int:
    """0 = complete; 3 = INCOMPLETE (failed units are 不明); 2 = ERR (nothing concluded)."""
    global _FAILS
    _FAILS = 0
    with _quiet():
        try:
            a = _parser().parse_args(argv)
        except BaseException:  # argparse diagnostics may echo an argument; fixed text only
            emit(T("ERR"), T("ArgError"))
            return 2
        try:
            configure(a)
            globals()[f"cmd_{a.step}"](a)
        except BaseException as e:  # the message may carry a value; report the class only
            emit(T("ERR"), T(a.step), T(type(e).__name__))
            return 2
        if _FAILS:
            emit(T("INCOMPLETE"), ("int", _FAILS))
            return 3
        return 0


if __name__ == "__main__":
    sys.exit(main())
