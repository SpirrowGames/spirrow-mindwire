"""Read-only investigation: registry values in pushed history (T-registry-values-in-pushed-history).

Implements the procedure agreed in that thread (Bohr 改訂 3, msg-8986, on top of 改訂 2 msg-8984,
approved by Einstein).  It does NOT decide anything: it emits observations, and Bohr
turns them into 確認 / 否定 / 不明.

OUTPUT CONTRACT (§0-b) — the reason this file exists as a reviewed script:
  * Secret values never leave this process.  They are read from the registry blob or the commit
    diff in memory, compared only as SHA-256 digests, and dropped.
  * Values are never passed as arguments or command lines (so they cannot land in a transcript).
  * Every line printed goes through ``emit``: each field must match an allow-listed shape
    (sha / repo / path / line / item name / enum / int / ISO time / uuid).  A field that does not
    match is printed as ``REDACTED`` — never as itself.
  * Child-process stderr is captured and discarded; only the exit status is kept, mapped to a
    fixed code.  ``gh api`` commit lookups use ``--jq .sha`` so the patch is never produced.
  * Python's own stderr is pointed at devnull and every exception is caught at the top and
    reported as ``ERR <step> <ExceptionClassName>`` only (the message may carry a value).
  * Original clones are only read: no fetch / pull / gc / checkout / ref or config writes.
    ``git`` is run with ``GIT_OPTIONAL_LOCKS=0`` and ``gc.auto=0`` / ``maintenance.auto=false``.
    Network access happens only in ``mirror`` (into a separate scratch mirror) and ``expose``.

Subcommands (run in this order):
  table     §1-3  re-derive P (pushed commits in the window whose added Markdown lines the guard's
                  own scanner flags) as value-free rows, and save them to --state.
  mirror    §1-2  ``git clone --mirror`` each project into --mirror-dir (state acquisition only).
  expose    §2    A-1 / A-1b / A-2 / A-3 / A-4 per row.
  registry  §3    B-1 (hash equality against the registry version(s) in force by history at the
                  author / committer instants) and B-2 evidence (which registry blob the guard's
                  working tree held at those instants, from the preserved spirrow-docs reflog).
  creation  §4    C-1 candidates from preserved reflogs (create vs import), C-2 evidence from
                  session transcripts, C-3a timed guard evidence, C-3b preserved-snapshot state.
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
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WIN_START = datetime(2026, 10, 2, 1, 28, 37, tzinfo=UTC)  # snapshot-2 window (msg-8164)
SANDBOX = "C:/workspace/sandbox"
DOCS_ROOT = "C:/workspace/spirrow/spirrow-docs"
REGISTRY_REL = "docs/platform/infra-registry.md"
CHECKER_REL = "scripts/check_infra_placeholders.py"
HOOK_REL = "githooks/pre-commit"
PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")

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


def safe(kind: str, value: object) -> str:
    """Return ``value`` as text only if it has the allow-listed shape ``kind``; else REDACTED."""
    text = str(value)
    pattern = _SHAPES.get(kind)
    if pattern is None or not pattern.match(text):
        return "REDACTED"
    return text


def emit(*fields: tuple[str, object]) -> None:
    """Print one record.  Each field is (shape, value); shapes are checked by ``safe``."""
    sys.stdout.write(" ".join(safe(k, v) for k, v in fields) + "\n")
    sys.stdout.flush()


def T(v: object) -> tuple[str, object]:  # noqa: N802 — short field constructors keep calls readable
    return ("tag", v)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# Child processes: stdout kept in memory, stderr discarded, status mapped to a code.

_GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
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
            env=_GIT_ENV,
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
    return run(["git", *_GIT_SAFE, "-C", repo, *args])


def gh_api(path: str, jq: str, paginate: bool = False) -> tuple[int, str]:
    argv = ["gh", "api", *(["--paginate"] if paginate else []), path, "--jq", jq]
    return run(argv)


def rc_tag(rc: int) -> str:
    return "ok" if rc == 0 else f"rc{rc}" if 0 < rc < 1000 else "rcX"


# ---------------------------------------------------------------------------------------------
# The guard's own scanner and registry loader (imported, so "flagged" means what the hook means).


def load_guard() -> Any:
    sys.path.insert(0, os.path.join(DOCS_ROOT, "scripts"))
    import check_infra_placeholders as g  # type: ignore[import-not-found]

    return g


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


def remote_repo(clone: str) -> str:
    _, url = git(clone, "config", "--get", "remote.origin.url")
    m = re.search(r"github\.com[/:]([^/\s]+/[^/\s]+?)(?:\.git)?\s*$", url)
    return m.group(1) if m else ""


def findings_of_commit(g: Any, clone: str, sha: str, registry: Any) -> list[tuple[str, int, Any]]:
    """(path, line, Finding) for every flagged added Markdown line of ``sha``.  In memory only."""
    _, diff = git(clone, "show", "--format=", "-U0", "--diff-filter=ACMR", sha, "--", "*.md")
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
    g = load_guard()
    registry = g.load_registry(Path(DOCS_ROOT) / REGISTRY_REL)
    rows: list[dict[str, Any]] = []
    for clone in sorted(glob.glob(f"{SANDBOX}/*-impl")):
        project = os.path.basename(clone)[: -len("-impl")]
        repo = remote_repo(clone)
        rc, log = git(clone, "log", "--remotes=origin", "--no-merges", "--format=%H %aI %cI")
        if rc:
            emit(T("ERR"), T("table-log"), ("project", project), T(rc_tag(rc)))
            continue
        for line in log.splitlines():
            parts = line.split()
            if len(parts) != 3 or datetime.fromisoformat(parts[1]) < WIN_START:
                continue
            sha, ai, ci = parts
            for path, line_no, f in findings_of_commit(g, clone, sha, registry):
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
        T("TOTAL"),
        T("commits"),
        ("int", len({r["sha"] for r in rows})),
        T("rows"),
        ("int", len(rows)),
    )
    Path(a.state).write_text(json.dumps(rows, indent=1), encoding="utf-8", newline="\n")


def load_rows(a: argparse.Namespace) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(Path(a.state).read_text(encoding="utf-8"))
    return rows


# ---------------------------------------------------------------------------------------------
# §1-2 mirror


def cmd_mirror(a: argparse.Namespace) -> None:
    for project in sorted({r["project"] for r in load_rows(a)}):
        clone = f"{SANDBOX}/{project}-impl"
        _, url = git(clone, "config", "--get", "remote.origin.url")
        dest = os.path.join(a.mirror_dir, f"{project}.git")
        if os.path.exists(dest):
            emit(T("MIRROR"), ("project", project), T("exists"))
            continue
        rc, _ = run(["git", *_GIT_SAFE, "clone", "--mirror", "--quiet", url.strip(), dest])
        emit(T("MIRROR"), ("project", project), T(rc_tag(rc)), ("time", now()))


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------------------------
# §2 expose


def commit_fetchable(repo: str, sha: str) -> str:
    """'yes' (API returned this sha) / 'no404' / 'no422' / other fixed code.  Never the patch."""
    rc, out, err = run3(["gh", "api", f"repos/{repo}/commits/{sha}", "--jq", ".sha"])
    if rc == 0:
        return "yes" if out.strip() == sha else "mismatch"
    # gh reports HTTP failures on stderr as "... (HTTP 404)"; only the 3-digit status is kept.
    m = re.search(r"\(HTTP (\d{3})\)", err)
    return f"no{m.group(1)}" if m else rc_tag(rc)


def cmd_expose(a: argparse.Namespace) -> None:
    rows = load_rows(a)
    by_commit: dict[tuple[str, str], list[int]] = {}
    for r in rows:
        by_commit.setdefault((r["repo"], r["sha"]), []).append(r["n"])
    repos = sorted({r["repo"] for r in rows})
    vis: dict[str, str] = {}
    forks: dict[str, list[str] | None] = {}
    for repo in repos:
        rc, out = run(["gh", "repo", "view", repo, "--json", "visibility", "--jq", ".visibility"])
        vis[repo] = out.strip().lower() if rc == 0 else rc_tag(rc)
        emit(T("A2"), ("repo", repo), T(vis[repo]), ("time", now()))
        # A-3: needs org audit log; any failure is reported as a code (→ 不明, operator task).
        org = repo.split("/")[0]
        rc, out = gh_api(
            f"orgs/{org}/audit-log?phrase=repo:{repo}+action:repo.access&per_page=100",
            '.[] | [.action, (.["@timestamp"]|tostring)] | join(" ")',
            paginate=True,
        )
        if rc:
            emit(T("A3"), ("repo", repo), T("unavailable"), T(rc_tag(rc)))
        else:
            n = 0
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].isdigit():
                    n += 1
                    t = datetime.fromtimestamp(int(parts[1]) / 1000, UTC).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                    emit(T("A3"), ("repo", repo), T(parts[0].replace(".", "-")), ("time", t))
            emit(T("A3"), ("repo", repo), T("events"), ("int", n))
        rc, out = gh_api(f"repos/{repo}/forks?per_page=100", ".[].full_name", paginate=True)
        forks[repo] = out.split() if rc == 0 else None
        emit(
            T("A4LIST"),
            ("repo", repo),
            T("ok" if rc == 0 else rc_tag(rc)),
            ("int", len(forks[repo] or [])),
        )
    for (repo, sha), ns in sorted(by_commit.items()):
        rows_tag = ",".join(str(n) for n in ns)
        a1 = commit_fetchable(repo, sha)
        emit(T("A1"), ("repo", repo), ("sha", sha), T(a1), ("ints", rows_tag), ("time", now()))
        project = next(r["project"] for r in rows if r["sha"] == sha)
        mirror = os.path.join(a.mirror_dir, f"{project}.git")
        if os.path.isdir(mirror):
            rc, out = git(
                mirror, "for-each-ref", f"--contains={sha}", "--format=%(refname)", "refs/"
            )
            refs = out.split() if rc == 0 else []
            emit(T("A1b"), ("sha", sha), T(rc_tag(rc)), ("int", len(refs)))
            for ref in refs:
                emit(T("A1bREF"), ("sha", sha), ("path", ref))
        else:
            emit(T("A1b"), ("sha", sha), T("nomirror"))
        for fork in forks.get(repo) or []:
            emit(T("A4"), ("repo", fork), ("sha", sha), T(commit_fetchable(fork, sha)))


# ---------------------------------------------------------------------------------------------
# §3 registry


def registry_history() -> list[tuple[datetime, str]]:
    _, out = git(DOCS_ROOT, "log", "--format=%H %cI", "--", REGISTRY_REL)
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


def cmd_registry(a: argparse.Namespace) -> None:
    g = load_guard()
    current = g.load_registry(Path(DOCS_ROOT) / REGISTRY_REL)
    hist = registry_history()
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
    cache: dict[str, Any] = {}
    for r in load_rows(a):
        clone = f"{SANDBOX}/{r['project']}-impl"
        match = None
        for path, line_no, f in findings_of_commit(g, clone, r["sha"], current):
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
                rc, text = git(DOCS_ROOT, "show", f"{rev}:{REGISTRY_REL}")
                try:
                    cache[rev] = g.load_registry(_MemText(text)) if rc == 0 else None
                except BaseException:  # SystemExit text may name the blob; keep inert
                    cache[rev] = None
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
            # B-2 evidence: which registry blob the hook's working tree (spirrow-docs) held then.
            head = head_at(docs_head, t)
            if head is None:
                emit(T("B2EV"), ("int", r["n"]), T(label), T("no-docs-reflog-before"))
                continue
            _, b_head = git(DOCS_ROOT, "rev-parse", f"{head}:{REGISTRY_REL}")
            _, b_rev = git(DOCS_ROOT, "rev-parse", f"{rev}:{REGISTRY_REL}")
            same = bool(b_head.strip()) and b_head.strip() == b_rev.strip()
            emit(
                T("B2EV"),
                ("int", r["n"]),
                T(label),
                ("sha", head),
                T("same-blob" if same else "other-blob"),
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
_COMMIT_OUT = re.compile(r"^\[[^\]\s]+(?: \([^)]*\))? ([0-9a-f]{7,40})\]", re.M)


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


def cmd_creation(a: argparse.Namespace) -> None:
    rows = load_rows(a)
    shas = sorted({(r["project"], r["sha"]) for r in rows})
    for project, sha in shas:
        base = os.path.join(a.preserve, project)
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
        _, email = git(f"{SANDBOX}/{project}-impl", "show", "-s", "--format=%ae", sha)
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
        # C-2 evidence: a transcript tool_result that is git's own commit output for this sha.
        for pdir, sid, recs in transcripts(project):
            created_at = None
            for rec in recs:
                if rec.get("type") != "user" or not isinstance(rec.get("message"), dict):
                    continue
                for cm in _COMMIT_OUT.finditer(text_of(rec["message"].get("content"))):
                    if sha.startswith(cm.group(1)):
                        created_at = rec.get("timestamp", "")
                        break
                if created_at:
                    break
            if not created_at:
                continue
            emit(
                T("C2EV"),
                ("sha", sha),
                ("path", pdir),
                ("uuid", sid),
                ("time", created_at[:19] + "Z"),
            )
            # C-3a timed evidence: guard output seen earlier in the same session.
            for tag, needle in _GUARD_MARKS:
                n = sum(
                    1
                    for rec in recs
                    if rec.get("timestamp", "") <= created_at
                    and isinstance(rec.get("message"), dict)
                    and needle in text_of(rec["message"].get("content"))
                )
                emit(T("C3aEV"), ("sha", sha), ("uuid", sid), T(tag), ("int", n))
    # C-3b: the preserved snapshot's state (現状 only; never used for 当時).
    for project in sorted({p for p, _ in shas}):
        base = os.path.join(a.preserve, project)
        cfg = (
            Path(base, "config").read_text(encoding="utf-8", errors="replace")
            if Path(base, "config").exists()
            else ""
        )
        hp = re.search(r"^\s*hooksPath\s*=\s*(.+?)\s*$", cfg, re.M)
        points_docs = (
            hp is not None and hp.group(1).replace("\\", "/").rstrip("/") == f"{DOCS_ROOT}/githooks"
        )
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
    for r in rows:
        for rel in (HOOK_REL, CHECKER_REL):
            _, out = git(
                DOCS_ROOT, "log", "-1", "--format=%H", f"--before={r['committer']}", "--", rel
            )
            emit(T("C3GV"), ("int", r["n"]), ("path", rel), ("sha", out.strip()))


# ---------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    ap.add_argument("step", choices=("table", "mirror", "expose", "registry", "creation"))
    ap.add_argument("--state", required=True, help="value-free row file (written by table)")
    ap.add_argument("--mirror-dir", default="")
    ap.add_argument("--preserve", default="", help="preserved git-dir copies (§1-1)")
    a = ap.parse_args(argv)
    sys.stderr = open(os.devnull, "w", newline="\n")  # noqa: SIM115 — nothing may reach stderr, including tracebacks
    try:
        {
            "table": cmd_table,
            "mirror": cmd_mirror,
            "expose": cmd_expose,
            "registry": cmd_registry,
            "creation": cmd_creation,
        }[a.step](a)
    except BaseException as e:  # the message may carry a value; report the class only
        emit(T("ERR"), T(a.step), T(type(e).__name__))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
