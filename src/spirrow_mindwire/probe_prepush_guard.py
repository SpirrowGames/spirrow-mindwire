"""Probe that a clone's *effective* pre-push hook is the force-push guard and that it fires.

T-force-push-silently-overwrites-with-no-fail-loud-boundary, design msg-9133 §4″-3
(revision 2, approved msg-9134). Preflight P3 checks the hook by its file: regular
file, marker line, runnable. This probe checks behaviour, per clone, through the
clone's own ``core.hooksPath`` resolution: one real fast-forward push and one real
non-fast-forward push, both to a throwaway bare repository.

Steps (all must hold; any deviation is ``incomplete`` — there is no fallback):

0. **transport** — every probe git command runs with ``GIT_ALLOW_PROTOCOL=file``, so
   git refuses any transport other than the local file one before it starts: a
   ``url.*.insteadOf`` / ``pushInsteadOf`` rule that rewrites the temp path to
   ``ssh://`` or ``https://`` fails closed instead of connecting. On top of that the
   probe refuses to run at all when the clone's effective config (every scope, plus
   config injected through the environment, read with the same env the pushes use)
   carries any URL rewrite rule, because a rewrite to another *local* path would pass
   the protocol check yet push somewhere other than the probe repo. The clone's config
   is neither altered nor overridden, so the effective ``core.hooksPath`` is the one
   probed (PR #587 gate round 1, Obj-1).
1. **probe remote** — ``git clone --bare`` the clone into a temp dir (local path: no
   network, no checkout, no LFS smudge), then ``update-ref refs/heads/probe <HEAD>``
   and confirm it reads back. No push is used to create the ref, so a failing first
   push cannot turn the later non-FF push into a new-ref push (Obj-3-1).
2. **canary** — with the recording ``git-lfs`` stub first on PATH, ``git lfs version``
   in the clone must print :data:`STUB_TOKEN`. If it does not, the real git-lfs would
   run during the pushes (and could upload to an external ``lfs.url``), so the probe
   stops before any push (Obj-3-2). The stub's call count is read *after* the canary
   and used as the baseline (msg-9134).
3. **commit C** — ``commit-tree HEAD^{tree} -p HEAD``: a child of HEAD that touches
   neither the work tree nor any ref (one unreferenced object, collected by gc).
4. **FF** — push ``C`` to ``refs/heads/probe``: must succeed, move the ref to C, and
   call the stub exactly once with exactly one stdin line naming C and HEAD.
5. **non-FF** — ``push --force HEAD:refs/heads/probe`` (C → HEAD; works for a root
   commit too): must be refused, print ``mindwire-guard`` on stderr, leave the ref at
   C, and not call the stub.
6. verdict; 7. the temp dir is removed.

Side effects: the push target is only the local bare repo (step 0: file transport
only, no URL rewrite rule in force), and LFS is the stub
(confirmed by the canary before the first push), so nothing leaves the host whatever
``lfs.url`` / ``.lfsconfig`` say. Real LFS hand-off is covered by spirrow-docs
``scripts/test_pre_push_guard.py`` (j)-(m), not here.

CLI: ``uv run python -m spirrow_mindwire.probe_prepush_guard <clone> [<clone> ...]``
prints, per clone, the P3 verdict (§4″-2) and the probe verdict (§4″-3) as markdown
table rows; exit 0 only when every clone passes both.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

STUB_TOKEN = "mindwire-lfs-stub"
GUARD_STDERR_TOKEN = "mindwire-guard"
PROBE_REF = "refs/heads/probe"
URL_REWRITE_KEY_RE = r"^url\..*\.(push)?insteadof$"
"""``git config --get-regexp`` pattern matching every URL rewrite rule (keys lowercased)."""
STEPS = ("transport", "probe-remote", "canary", "commit", "ff", "non-ff")

GitRunner = Callable[[Sequence[str], dict[str, str]], "subprocess.CompletedProcess[bytes]"]
"""``(argv after "git", env) -> CompletedProcess``. Injectable for tests."""

StubText = Callable[[Path], str]
"""``record_dir -> text of the git-lfs stub``. Injectable for tests."""


def _run_git(args: Sequence[str], env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], env=env, capture_output=True, check=False)


def stub_text(record_dir: Path) -> str:
    """The recording ``git-lfs`` stub: answers ``version`` with :data:`STUB_TOKEN`;
    any other call is counted and its argv / stdin are saved, then it exits with
    ``MINDWIRE_LFS_STUB_EXIT`` (default 0).

    ``record_dir`` is shell-quoted once into ``R`` and only ever used as ``"$R"``, so a
    temp parent containing ``$``, backticks or quotes is taken literally, never
    expanded or executed (PR #587 gate round 1, Obj-2)."""
    rec = shlex.quote(record_dir.as_posix())
    return (
        "#!/bin/sh\n"
        f"R={rec}\n"
        f'if [ "$1" = version ]; then echo "{STUB_TOKEN}"; exit 0; fi\n'
        'n=$(cat "$R/count" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$R/count"\n'
        'printf "%s\\n" "$@" > "$R/argv.$n"\n'
        'cat > "$R/stdin.$n"\n'
        'exit "${MINDWIRE_LFS_STUB_EXIT:-0}"\n'
    )


@dataclass
class ProbeResult:
    clone: Path
    steps: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        # Exactly the checked steps, all ok: stopping early is never a pass.
        names = [name for name, _ok, _detail in self.steps]
        return names == list(STEPS) and all(ok for _n, ok, _d in self.steps)

    def summary(self) -> str:
        if self.passed:
            return "pass"
        failed = next(((n, d) for n, ok, d in self.steps if not ok), None)
        if failed is None:
            return "incomplete: stopped before all steps ran"
        return f"incomplete at {failed[0]}: {failed[1]}"


class _StopError(Exception):
    pass


def _out(proc: subprocess.CompletedProcess[bytes]) -> str:
    return proc.stdout.decode("utf-8", "replace").strip()


def _err(proc: subprocess.CompletedProcess[bytes]) -> str:
    return proc.stderr.decode("utf-8", "replace").strip()


def _calls(record_dir: Path) -> int:
    f = record_dir / "count"
    return int(f.read_text(encoding="utf-8").strip()) if f.exists() else 0


def _rmtree(path: Path) -> None:
    # Packs written by git are read-only on Windows; make everything writable first.
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            p = os.path.join(root, name)
            with contextlib.suppress(OSError):
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    shutil.rmtree(path, ignore_errors=True)


def probe(
    clone: Path,
    *,
    run_git: GitRunner = _run_git,
    make_stub: StubText = stub_text,
    tmp_parent: Path | None = None,
) -> ProbeResult:
    """Run steps 1-7 against ``clone``. Never raises for a probe failure: it is recorded."""
    result = ProbeResult(clone=clone)
    tmp = Path(tempfile.mkdtemp(prefix="mindwire-probe-", dir=tmp_parent))
    try:
        _probe(clone, tmp, result, run_git, make_stub)
    except _StopError:
        pass
    finally:
        _rmtree(tmp)
    return result


def _probe(
    clone: Path,
    tmp: Path,
    result: ProbeResult,
    run_git: GitRunner,
    make_stub: StubText,
) -> None:
    def step(name: str, ok: bool, detail: str) -> None:
        result.steps.append((name, ok, detail))
        if not ok:
            raise _StopError

    stub_dir = tmp / "stub"
    rec = tmp / "rec"
    stub_dir.mkdir()
    rec.mkdir()
    stub = stub_dir / "git-lfs"
    stub.write_text(make_stub(rec), encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")
    env.pop("MINDWIRE_PUSH_EXPECT", None)  # the probe must see the guard's default verdict
    env.pop("MINDWIRE_LFS_STUB_EXIT", None)  # the stub passes unless make_stub says otherwise
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Step 0 backstop: only the local file transport may start, for every command.
    env["GIT_ALLOW_PROTOCOL"] = "file"
    for k, v in (
        ("GIT_AUTHOR_NAME", "mindwire-probe"),
        ("GIT_AUTHOR_EMAIL", "probe@example.invalid"),
        ("GIT_COMMITTER_NAME", "mindwire-probe"),
        ("GIT_COMMITTER_EMAIL", "probe@example.invalid"),
    ):
        env[k] = v
    c = str(clone)
    probe_git = tmp / "probe.git"
    pg = str(probe_git)
    # Pushes never recurse into submodules or carry tags: the probe repo only.
    push = ["-C", c, "-c", "push.recurseSubmodules=no", "-c", "push.followTags=false", "push"]

    # 0. transport: no URL rewrite rule may be in force (GIT_ALLOW_PROTOCOL is the backstop)
    rw = run_git(["-C", c, "config", "--get-regexp", URL_REWRITE_KEY_RE], env)
    rules = _out(rw)
    if rw.returncode not in (0, 1):
        step("transport", False, f"config unreadable: {_err(rw) or 'rc=' + str(rw.returncode)}")
    step(
        "transport",
        rw.returncode == 1 and not rules,
        "no URL rewrite rule"
        if not rules
        else "URL rewrite rules in force: " + "; ".join(rules.splitlines()),
    )

    # 1. probe remote, without a push
    head_p = run_git(["-C", c, "rev-parse", "--verify", "HEAD^{commit}"], env)
    head = _out(head_p)
    if head_p.returncode != 0 or not head:
        step("probe-remote", False, f"clone HEAD unreadable: {_err(head_p)}")
    cl = run_git(["clone", "--bare", "--quiet", "--", c, pg], env)
    if cl.returncode != 0:
        step("probe-remote", False, f"clone --bare failed: {_err(cl)}")
    ur = run_git(["--git-dir", pg, "update-ref", PROBE_REF, head], env)
    if ur.returncode != 0:
        step("probe-remote", False, f"update-ref failed: {_err(ur)}")
    got = _out(run_git(["--git-dir", pg, "rev-parse", "--verify", "-q", PROBE_REF], env))
    step("probe-remote", got == head, f"{PROBE_REF} = {got or 'missing'} (expected {head})")

    # 2. canary: the stub, not the real git-lfs, answers in this clone with this env
    cv = run_git(["-C", c, "lfs", "version"], env)
    step(
        "canary",
        cv.returncode == 0 and STUB_TOKEN in _out(cv),
        f"`git lfs version` -> {_out(cv) or _err(cv) or 'nothing'}",
    )
    base = _calls(rec)

    # 3. commit C, a child of HEAD
    ct = run_git(["-C", c, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "probe"], env)
    commit = _out(ct)
    step("commit", ct.returncode == 0 and bool(commit), commit or _err(ct))

    # 4. fast-forward HEAD -> C
    ff = run_git([*push, pg, f"{commit}:{PROBE_REF}"], env)
    got = _out(run_git(["--git-dir", pg, "rev-parse", "--verify", "-q", PROBE_REF], env))
    calls = _calls(rec) - base
    stdin_text = ""
    if calls == 1:
        stdin_text = (rec / f"stdin.{base + 1}").read_text(encoding="utf-8", errors="replace")
    lines = stdin_text.splitlines()
    ok = (
        ff.returncode == 0
        and got == commit
        and calls == 1
        and len(lines) == 1
        and commit in lines[0]
        and head in lines[0]
        and PROBE_REF in lines[0]
    )
    step(
        "ff",
        ok,
        f"rc={ff.returncode} ref={got or 'missing'} lfs_calls={calls} stdin_lines={len(lines)}"
        + ("" if ff.returncode == 0 else f" stderr={_err(ff)!r}"),
    )

    # 5. non-fast-forward C -> HEAD must be refused by the guard
    before = _calls(rec)
    nf = run_git([*push, "--force", pg, f"HEAD:{PROBE_REF}"], env)
    got = _out(run_git(["--git-dir", pg, "rev-parse", "--verify", "-q", PROBE_REF], env))
    calls = _calls(rec) - before
    stderr = _err(nf)
    ok = nf.returncode != 0 and GUARD_STDERR_TOKEN in stderr and got == commit and calls == 0
    step("non-ff", ok, f"rc={nf.returncode} ref={got or 'missing'} lfs_calls={calls}")


def _p3_verdict(clone: Path) -> tuple[bool, str]:
    from spirrow_mindwire import preflight

    try:
        preflight._p3_secret_guard_armed(
            clone, preflight._default_hooks_path_reader, preflight._default_git_toplevel
        )
    except preflight.PreflightError as exc:
        return False, str(exc)
    return True, "ok"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m spirrow_mindwire.probe_prepush_guard <clone> [<clone> ...]")
        return 2
    all_ok = True
    print("| clone | P3 (§4″-2) | probe (§4″-3) |")
    print("|---|---|---|")
    for a in args:
        clone = Path(a)
        p3_ok, p3_msg = _p3_verdict(clone)
        res = probe(clone)
        all_ok = all_ok and p3_ok and res.passed
        print(f"| `{clone}` | {'pass' if p3_ok else 'FAIL: ' + p3_msg} | {res.summary()} |")
        for name, ok, detail in res.steps:
            print(f"<!-- {clone.name} {name}: {'ok' if ok else 'NG'} — {detail} -->")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
