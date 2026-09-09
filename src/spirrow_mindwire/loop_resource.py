"""Predict a checkout's *resource identity* — a normalized ``<host>/<org>/<repo>``.

Used by the sweep wrapper (``deploy/run-conductor-scheduled.ps1``) to add a
resource axis to the HOLD gate so an operator can stop every candidate that
writes to a given GitHub repo, regardless of the chatroom project each of
those candidates belongs to (the driving example: ``spirrow-magickit`` has
candidates whose ``repo_dir`` is ``mindwire-impl`` — a HOLD on
``spirrow-mindwire`` did not stop those, even though they wrote to the same
remote every candidate on ``spirrow-mindwire`` did).

**This module produces a PREDICTED identity, not identity itself.** The name
``predicted_resource`` and the ``Predicted`` prefix on the callers exist to
make that discipline structural (D-KEY-4c(1) in thread
``T-loop-control-keyed-by-project-resource-is-the-repo``): the identity that
FAIL-CLOSED enforcement reads must be derived at the *acting* checkout at the
moment of action, and this file is called at *tick preparation time* over the
sweep's ``repo_dir`` hint — a different checkout in a general case, and
demonstrably a different checkout for at least the manual lane on
``mindwire-relay``. The prediction is:

  - Correct for candidates the wrapper LAUNCHES itself, because
    ``run-conductor-scheduled.ps1`` writes ``$cand.repo_dir`` into
    ``[loop].repo_dir`` immediately before spawning the inner daemon
    (line ~3163). The acting checkout of that spawn *is* the value we
    resolved here, modulo a tick→act window in which the checkout's
    ``origin`` was re-pointed (accepted as a design residual — see
    ``§7`` of the specifying thread).
  - Unmodelable for actions the wrapper did not launch — the manual lane
    and any checkout not listed as ``repo_dir`` in ``sweep.json``. This
    is #244 class and is out of scope for the A'-owner predicate (the
    specifying thread records it as "residual, no owner").

**The value returned here IS NOT identity.** The specifying thread defines
resource identity (D-KEY-4) as "the value derived from the acting checkout,
at the moment of the action". Consumers MUST NOT feed this value to the
fail-closed enforcement layer. The fail-closed enforcement enforcer is A
(schema resource axis on ``loop_control_*``), currently unimplemented — a
mistake landed here would leak into that layer if the boundary is not held
in code, which is why the helper names below carry ``Predicted``.

D-KEY-1 (normalization, v3): the returned key SHAPE is ``<host>/<org>/<repo>``
— host is NOT stripped. Stripping the host would silently assume a single
Git provider, which is a hidden premise the specifying thread's D-KEY-3c
explicitly forbids (no guessing). D-KEY-1b (short-form id, single-host +
single-org assumption) is an ORTHOGONAL choice made elsewhere and is not
this module's responsibility.

Fail-open (D-KEY-3a permits observation to fail; D-KEY-4c(2) requires
failure to fall through to the current predicate = project axis only):
every failure returns ``ResourceResolution(resource=None, reason=...)``
so the wrapper can log a diagnosable line and continue. No exception ever
escapes ``resolve_predicted_resource``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# D-KEY-1 depth cap for transitive local-path resolution. A local-path origin
# (``origin`` pointing at another checkout on disk) is legal and used by
# ``git worktree`` and by hand-linked mirror clones. Following it further than
# needed lets a cycle turn into an unbounded walk; a depth cap plus explicit
# cycle detection covers both. Depth 4 is the specifying thread's value.
_MAX_LOCAL_PATH_DEPTH = 4


class _GitRunner:
    """Minimal interface: return (origin_url_or_None, error_message).

    Defined here (before the resolver functions that reference it) so
    ``from __future__ import annotations`` is not the only thing keeping
    the forward reference valid — the class exists at import time and
    ``ruff`` / ``mypy`` see a normal name resolution.
    """

    def get_origin_url(self, cwd: str) -> tuple[str | None, str | None]:
        raise NotImplementedError


@dataclass(frozen=True)
class ResourceResolution:
    """Result of one predicted-resource resolution.

    Exactly one of ``resource`` / ``reason`` is set:

    - ``resource is not None`` and ``reason is None`` on success. The value
      is the D-KEY-1 v3 normalised comparison key
      (``<host>/<org>/<repo>``).
    - ``resource is None`` and ``reason is not None`` on failure. The
      wrapper prints the reason and falls back to the project-only HOLD
      check (fail-open per D-KEY-4c(2)).

    ``repo_dir`` echoes the input path so batch callers can associate
    results with inputs without depending on ordering.
    """

    repo_dir: str
    resource: str | None
    reason: str | None


def resolve_predicted_resource(
    repo_dir: str,
    *,
    _git: _GitRunner | None = None,
) -> ResourceResolution:
    """Return the predicted resource for ``repo_dir`` — never raises.

    The function is deliberately named ``resolve_PREDICTED_resource`` (per
    D-KEY-4c(1)): callers must not conflate this value with the identity
    that fail-closed enforcement reads. See the module docstring.

    On any failure path — path not absolute, not a git repo, ``origin``
    missing, a cycle in local-path origins, depth budget exceeded, git
    invocation error — the return value is ``ResourceResolution(resource=
    None, reason=...)``. This is fail-open by design (D-KEY-4c(2)).

    The ``_git`` parameter is a test seam. Production callers leave it at
    the default and the module invokes ``git`` via :class:`_SubprocessGit`.
    """

    git = _git if _git is not None else _SubprocessGit()

    if not repo_dir or repo_dir.strip() != repo_dir or not repo_dir.strip():
        return ResourceResolution(
            repo_dir=repo_dir,
            resource=None,
            reason="repo_dir is empty or blank",
        )
    if not Path(repo_dir).is_absolute():
        # Match the wrapper's PowerShell contract: relative or drive-relative
        # paths are rejected upstream by ``IsPathFullyQualified``. If one
        # slips through — a caller other than the wrapper — we still refuse
        # to guess against CWD.
        return ResourceResolution(
            repo_dir=repo_dir,
            resource=None,
            reason=f"repo_dir is not an absolute path: {repo_dir!r}",
        )

    # D-KEY-1 step 1: resolve local-path origins transitively, with a depth
    # cap and cycle detection. Every hop asks ``git -C <p> remote get-url
    # origin`` for exactly one string; the loop only follows the answer when
    # it is itself a local filesystem path.
    seen: list[str] = []
    current = repo_dir
    for _ in range(_MAX_LOCAL_PATH_DEPTH + 1):
        # Resolve to a canonical form BEFORE the cycle check so
        # ``C:/foo/../foo`` and ``C:/foo`` don't defeat detection.
        try:
            canonical = str(Path(current).resolve())
        except OSError as exc:
            return ResourceResolution(
                repo_dir=repo_dir,
                resource=None,
                reason=f"cannot canonicalise path {current!r}: {exc}",
            )
        if canonical in seen:
            return ResourceResolution(
                repo_dir=repo_dir,
                resource=None,
                reason=(
                    "local-path origin cycle detected: " + " -> ".join(seen) + f" -> {canonical}"
                ),
            )
        seen.append(canonical)

        origin, err = git.get_origin_url(canonical)
        if origin is None:
            return ResourceResolution(
                repo_dir=repo_dir,
                resource=None,
                reason=f"git -C {canonical!r} remote get-url origin: {err}",
            )
        origin = origin.strip()
        if not origin:
            return ResourceResolution(
                repo_dir=repo_dir,
                resource=None,
                reason=f"origin URL is empty for {canonical!r}",
            )

        if _looks_like_local_path(origin):
            # Follow the chain — the true origin is at the far end. Absolute
            # or relative-to-caller: normalise before the next hop.
            next_path = (
                origin if Path(origin).is_absolute() else str((Path(canonical) / origin).resolve())
            )
            current = next_path
            continue

        # A network URL — normalize and return.
        try:
            key = _normalise_remote_url(origin)
        except ValueError as exc:
            return ResourceResolution(
                repo_dir=repo_dir,
                resource=None,
                reason=f"cannot parse remote URL {origin!r}: {exc}",
            )
        return ResourceResolution(repo_dir=repo_dir, resource=key, reason=None)

    return ResourceResolution(
        repo_dir=repo_dir,
        resource=None,
        reason=(
            f"local-path origin chain exceeded depth {_MAX_LOCAL_PATH_DEPTH}: " + " -> ".join(seen)
        ),
    )


def resolve_batch(
    repo_dirs: Sequence[str],
    *,
    _git: _GitRunner | None = None,
) -> list[ResourceResolution]:
    """Resolve a batch, one entry per unique ``repo_dir`` in input order.

    The wrapper calls this once per tick over the distinct ``repo_dir`` set
    (7 today). Order and duplicates are preserved so the caller can match
    results back to input positions when it wants to, without imposing a
    cache shape here.
    """

    return [resolve_predicted_resource(rd, _git=_git) for rd in repo_dirs]


# --- normalisation (D-KEY-1 v3) ------------------------------------------


def _is_scp_style(candidate: str) -> bool:
    """True iff ``candidate`` looks like an scp-style SSH URL.

    Git's scp-style syntax is ``[user@]host.xz:path/to/repo.git`` — a
    non-scheme URL with a ``:`` that appears BEFORE any ``/``. Notably:

    - ``user@`` is optional: ``github.com:org/repo`` is a valid scp-style
      URL that Git recognises without a user prefix, and it does not have
      to be ``git@`` — an operator could clone as ``user@github.com:...``
      or ``root@host:...`` (naysayer PR #252 objection 2).
    - A Windows drive letter (``C:``, ``D:``, …) also has a single ``:``
      before any ``/``, but is a LOCAL path — distinguished by the
      one-character alphabetic prefix (drive letters are always exactly
      one letter).

    Distinguishing an ambiguous edge — a single-letter hostname followed
    by ``:path`` — is a losing battle in general, and Git itself
    documents the drive-letter carve-out with the same shape. This
    heuristic follows Git's convention: single alpha before ``:`` is a
    drive letter, anything longer or containing ``.`` / ``@`` is a host.
    """

    if "://" in candidate:
        return False
    first_slash = candidate.find("/")
    first_colon = candidate.find(":")
    if first_colon == -1:
        return False
    if first_slash != -1 and first_slash < first_colon:
        return False
    prefix = candidate[:first_colon]
    # Windows drive letter carve-out — single alpha char before ``:``.
    return not (len(prefix) == 1 and prefix.isalpha())


def _normalise_remote_url(url: str) -> str:
    """Return the D-KEY-1 v3 comparison key: ``<host>/<org>/<repo>``.

    Steps applied in order (see the specifying thread's D-KEY-1):

    1. Trim + lowercase (schemes and hostnames are case-insensitive in
       every Git flavour; the compare key is lowercase anyway, so doing
       this up front makes every following step case-insensitive —
       fixes naysayer PR #252 objection 3, where ``HTTPS://…`` retained
       its prefix through the case-sensitive scheme-match and blew up
       three-segment validation).
    2. If scp-style (:func:`_is_scp_style`), rewrite ``host:path`` to
       ``host/path`` — the ``user@`` prefix (if any) is stripped as
       credentials on the same pass. Handles ``git@`` and any other
       user prefix, and ``host:path`` with no user (naysayer objection
       2).
    3. Otherwise strip a scheme (``https://``, ``ssh://``, ``git://``,
       ``http://``, ``file://``) and any embedded credentials
       (``user[:pass]@``).
    4. Strip any trailing ``.git`` and any trailing ``/`` — in a LOOP so
       ``.git/`` and ``/.git`` both collapse cleanly (fixes naysayer
       objection 1, where a ``.git/`` suffix left ``.git`` in the
       comparison key and made a HOLD miss every checkout that omitted
       the trailing slash).

    The result is asserted to be exactly three ``/``-separated non-empty
    segments (host, org, repo). A URL that does not split into three
    segments raises ``ValueError`` — a genuine parse failure the caller
    must surface, not silently paper over.
    """

    lowered = url.strip().lower()

    if _is_scp_style(lowered):
        # scp-style: [user@]host:path (path is org/repo(.git))
        # Strip an optional ``user@`` prefix on the host side, then swap
        # the ``:`` for ``/``. Credentials in scp-style are never ``user:pass``
        # — the ``:`` before the first ``/`` is the host/path separator —
        # so the only credential form here is a bare ``user@``.
        candidate = lowered
        first_slash = candidate.find("/")  # after :, may be -1
        first_colon = candidate.find(":")
        first_at = candidate.find("@")
        if first_at != -1 and first_at < first_colon:
            candidate = candidate[first_at + 1 :]
            first_colon = candidate.find(":")
            first_slash = candidate.find("/")
        host = candidate[:first_colon]
        path = candidate[first_colon + 1 :]
        candidate = f"{host}/{path}"
    else:
        # scheme://... or bare host/path
        candidate = lowered
        scheme_sep = candidate.find("://")
        if scheme_sep != -1:
            candidate = candidate[scheme_sep + 3 :]
        # Strip credentials on the first '@' before the first '/'.
        first_slash = candidate.find("/")
        first_at = candidate.find("@")
        if first_at != -1 and (first_slash == -1 or first_at < first_slash):
            candidate = candidate[first_at + 1 :]

    # Loop-strip trailing ``.git`` and ``/`` — both may repeat, and either
    # order (``.git/`` vs ``/.git``) must collapse to the bare form.
    while candidate.endswith(".git") or candidate.endswith("/"):
        candidate = candidate[: -len(".git")] if candidate.endswith(".git") else candidate[:-1]

    parts = candidate.split("/")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"expected <host>/<org>/<repo> after normalisation, got {candidate!r}")
    return candidate


def _looks_like_local_path(origin: str) -> bool:
    """Heuristic: is this origin a filesystem path (needs another hop)?

    ``git remote get-url origin`` on a linked clone returns either a URL
    (network form) or a local filesystem path. The former always contains
    a scheme (``://``) or is scp-style (``:`` before any ``/``); the
    latter does not — with the single carve-out for Windows drive
    letters (``C:/…``), which contain a ``:`` but are still local.

    This rule is conservative in the direction that matters: false-
    positive local would loop forever on the same path (caught by the
    cycle check upstream), false-negative would try to normalise a
    filesystem path as a URL (caught by
    ``_normalise_remote_url``'s three-segment check).

    Delegating to :func:`_is_scp_style` — instead of the previous
    hardcoded ``origin.startswith("git@")`` — is the fix for naysayer
    PR #252 objection 2: a checkout cloned with a different SSH user
    (``user@github.com:org/repo``) or no user at all
    (``github.com:org/repo``) was being classified as local and followed
    onto the filesystem, then failing open.
    """

    if "://" in origin:
        return False
    return not _is_scp_style(origin)


# --- git runner (test seam) ----------------------------------------------
# ``_GitRunner`` (the abstract seam) is defined near the top of the module so
# forward references from ``resolve_predicted_resource`` / ``resolve_batch``
# resolve cleanly under ruff/mypy without a stringified annotation.


class _SubprocessGit(_GitRunner):
    """Production runner — invokes ``git`` as a subprocess.

    The wrapper's contract (``run-conductor-scheduled.ps1`` line ~2465)
    guarantees ``repo_dir`` is absolute, so ``git -C`` cannot resolve
    against the caller's CWD and this call is CWD-independent.
    """

    def get_origin_url(self, cwd: str) -> tuple[str | None, str | None]:
        try:
            result = subprocess.run(
                ["git", "-C", cwd, "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except FileNotFoundError:
            return None, "git executable not found on PATH"
        except subprocess.TimeoutExpired:
            return None, f"git subprocess timed out (10s) for {cwd!r}"
        except OSError as exc:
            return None, f"git subprocess OSError for {cwd!r}: {exc}"

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            return None, f"git exit {result.returncode}: {err or '(no output)'}"
        return result.stdout, None
