"""Per-turn ``--model`` choice for the ``claude --bg`` spawn (T-spawn-via-claude-bg PR-A).

Spec: msg-6788 (policy: Opus by default, Sonnet only on a closed allow-list), msg-6810 §3 (the pure
function), msg-6812 (reading rule), msg-6814 (C6), msg-6826 / msg-6828 (C4a / C4b), msg-6830 §2 and
msg-6858 (scope). :func:`choose_model` returns ``(model, reasons)``; it is pure and does no I/O —
the caller gathers the thread flags and the worktree state and records the reasons (PR-B / PR-C).

Reading rule (msg-6812 §1, stated here as the spec requires):

    Free text is read only by fixed-substring matching, and that match only ever pushes the turn
    to Opus. The only material that can push a turn to Sonnet is the structured fields.

Concretely, the structured fields are lines of the handoff that *start at column 0* with
``TURN-KIND:``, ``FILES:`` or ``ACCEPTANCE:`` (:func:`parse_handoff_fields`). A field written
anywhere else — indented, mid-sentence — does not exist. The only free-text read is C5
(:data:`ModelPolicyConfig.revision_markers <spirrow_mindwire.config.ModelPolicyConfig>`), and a hit
there can only fail C5.

Sonnet is chosen only when every condition passes; anything missing, malformed or unknown fails
its condition, so every doubt resolves to Opus:

* **C1** ``turn_kind`` is ``mechanical`` / ``docs-config`` / ``local-fix``. ``docs-config`` needs a
  non-empty ``FILES:`` all inside the docs / config / test globs; ``local-fix`` needs a non-empty
  ``FILES:`` and at least one ``ACCEPTANCE:`` line. Missing or malformed fields → ``no-turn-kind``.
* **C2** ``repo_kind`` is known and is not ``ue-cpp``.
* **C3** no ``FILES:`` entry matches ``sonnet_forbidden_paths``.
* **C4a** the caller found no REQUEST_CHANGES / no-progress / malformed handoff in the thread's
  last ``recent_window`` messages.
* **C4b** the thread has no ``sonnet_exclusion`` record (``sonnet-excluded:<trigger>``).
* **C5** ``design_text`` carries none of the revision markers.
* **C6** ``git status --porcelain`` was empty at spawn (``dirty-worktree``).

Every condition is evaluated and reported, pass or fail, so the recorded reasons say exactly why
a turn ran on the model it did (msg-6788 "測定").
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, NamedTuple

from ..config import ModelPolicyConfig

TurnKind = Literal["mechanical", "docs-config", "local-fix", "design-impl"]
TURN_KINDS: tuple[TurnKind, ...] = ("mechanical", "docs-config", "local-fix", "design-impl")
SONNET_TURN_KINDS: frozenset[str] = frozenset({"mechanical", "docs-config", "local-fix"})
UE_CPP_REPO_KIND = "ue-cpp"

_FIELD_LINE = re.compile(r"^(TURN-KIND|FILES|ACCEPTANCE):(.*)$")


@dataclass(frozen=True)
class HandoffFields:
    """The structured fields of one handoff, as :func:`parse_handoff_fields` read them.

    ``files`` is ``None`` when there is no ``FILES:`` line; ``problems`` lists every reason the
    fields are unusable (non-empty → C1 fails with ``no-turn-kind``).
    """

    turn_kind: TurnKind | None
    files: tuple[str, ...] | None
    acceptance: tuple[str, ...]
    problems: tuple[str, ...]

    @property
    def usable(self) -> bool:
        return self.turn_kind is not None and self.files is not None and not self.problems


def _normalise_path(raw: str) -> str | None:
    """Repo-relative ``/`` path, or ``None`` when the entry cannot be trusted as one."""
    path = raw.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if not path or re.search(r"\s|`", path):
        return None
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return None
    if any(seg in ("", "..", ".") for seg in path.rstrip("/").split("/")):
        return None
    return path


def parse_handoff_fields(text: str) -> HandoffFields:
    """Read the column-0 ``TURN-KIND:`` / ``FILES:`` / ``ACCEPTANCE:`` lines of a handoff.

    Strict by design: a repeated ``TURN-KIND:`` or ``FILES:`` line, an unknown turn kind, or a
    ``FILES:`` entry that is not a plain repo-relative path is a problem, and any problem sends
    the turn to Opus. ``ACCEPTANCE:`` may repeat; a blank one does not count.
    """
    turn_kinds: list[str] = []
    files_lines: list[str] = []
    acceptance: list[str] = []
    for line in text.splitlines():
        m = _FIELD_LINE.match(line)
        if m is None:
            continue
        name, value = m.group(1), m.group(2).strip()
        if name == "TURN-KIND":
            turn_kinds.append(value)
        elif name == "FILES":
            files_lines.append(value)
        elif value:
            acceptance.append(value)

    problems: list[str] = []
    turn_kind: TurnKind | None = None
    if not turn_kinds:
        problems.append("turn-kind-missing")
    elif len(turn_kinds) > 1:
        problems.append("turn-kind-repeated")
    else:
        for kind in TURN_KINDS:
            if turn_kinds[0] == kind:
                turn_kind = kind
        if turn_kind is None:
            problems.append(f"turn-kind-invalid={turn_kinds[0]!r}")

    files: tuple[str, ...] | None = None
    if not files_lines:
        problems.append("files-missing")
    elif len(files_lines) > 1:
        problems.append("files-repeated")
    elif files_lines[0]:
        parsed: list[str] = []
        for raw in files_lines[0].split(","):
            path = _normalise_path(raw)
            if path is None:
                problems.append(f"files-entry-invalid={raw.strip()!r}")
            else:
                parsed.append(path)
        files = tuple(parsed)
    else:
        files = ()

    if problems:
        turn_kind = None
    return HandoffFields(
        turn_kind=turn_kind, files=files, acceptance=tuple(acceptance), problems=tuple(problems)
    )


@lru_cache(maxsize=256)
def _glob_regex(pattern: str, ignore_case: bool) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    pat = pattern.replace("\\", "/")
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("".join(out) + r"\Z", re.IGNORECASE if ignore_case else 0)


def glob_match(path: str, patterns: Iterable[str], *, ignore_case: bool) -> bool:
    """``True`` when ``path`` matches any pattern (``**`` spans directories, ``*`` does not)."""
    return any(_glob_regex(p, ignore_case).match(path) for p in patterns)


@dataclass(frozen=True)
class ThreadFlags:
    """Per-thread inputs the caller gathers before the spawn.

    ``recent_trouble``: the C4a kinds (REQUEST_CHANGES / no-progress / malformed handoff) found in
    the thread's last ``recent_window`` messages; empty means none. ``sonnet_excluded`` /
    ``exclusion_trigger``: whether the thread's permanent ``sonnet_exclusion`` record exists, and
    its ``trigger`` (C4b).
    """

    recent_trouble: tuple[str, ...] = ()
    sonnet_excluded: bool = False
    exclusion_trigger: str | None = None


class ModelChoice(NamedTuple):
    model: str
    reasons: tuple[str, ...]


def _verdict(cond: str, ok: bool, detail: str) -> tuple[bool, str]:
    return ok, f"{cond}:{'pass' if ok else 'fail'}:{detail}"


def _c1(fields: HandoffFields, policy: ModelPolicyConfig) -> tuple[bool, str]:
    if not fields.usable or fields.turn_kind is None or fields.files is None:
        return _verdict("C1", False, "no-turn-kind(" + ",".join(fields.problems) + ")")
    kind = fields.turn_kind
    if kind not in SONNET_TURN_KINDS:
        return _verdict("C1", False, f"turn-kind={kind}")
    if kind == "docs-config":
        if not fields.files:
            return _verdict("C1", False, "docs-config-no-files")
        outside = [
            f
            for f in fields.files
            if not glob_match(f, policy.docs_config_globs, ignore_case=False)
        ]
        if outside:
            return _verdict("C1", False, "docs-config-outside-globs=" + ",".join(outside))
    if kind == "local-fix":
        if not fields.files:
            return _verdict("C1", False, "local-fix-no-files")
        if not fields.acceptance:
            return _verdict("C1", False, "local-fix-no-acceptance")
    return _verdict("C1", True, f"turn-kind={kind}")


def _c2(repo_kind: str | None) -> tuple[bool, str]:
    if not repo_kind or not repo_kind.strip():
        return _verdict("C2", False, "repo-kind-unknown")
    return _verdict("C2", repo_kind.strip() != UE_CPP_REPO_KIND, f"repo-kind={repo_kind.strip()}")


def _c3(fields: HandoffFields, policy: ModelPolicyConfig) -> tuple[bool, str]:
    if fields.files is None or fields.problems:
        return _verdict("C3", False, "files-unknown")
    # Case-insensitive on purpose: a forbidden-path hit can only move the turn to Opus.
    hits = [
        f for f in fields.files if glob_match(f, policy.sonnet_forbidden_paths, ignore_case=True)
    ]
    if hits:
        return _verdict("C3", False, "forbidden-path=" + ",".join(hits))
    return _verdict("C3", True, "no-forbidden-path")


def _c4a(flags: ThreadFlags) -> tuple[bool, str]:
    if flags.recent_trouble:
        return _verdict("C4a", False, "recent-trouble=" + ",".join(flags.recent_trouble))
    return _verdict("C4a", True, "recent-clean")


def _c4b(flags: ThreadFlags) -> tuple[bool, str]:
    if flags.sonnet_excluded:
        return _verdict("C4b", False, f"sonnet-excluded:{flags.exclusion_trigger or 'unknown'}")
    return _verdict("C4b", True, "not-excluded")


def find_revision_marker(design_text: str, markers: Iterable[str]) -> str | None:
    """The first configured marker found in ``design_text`` (fixed substring, case-folded)."""
    folded = design_text.casefold()
    for marker in markers:
        if marker.casefold() in folded:
            return marker
    return None


def _c5(design_text: str, policy: ModelPolicyConfig) -> tuple[bool, str]:
    marker = find_revision_marker(design_text, policy.revision_markers)
    if marker is not None:
        return _verdict("C5", False, f"revision-marker={marker}")
    return _verdict("C5", True, "no-revision-marker")


def _c6(worktree_clean: bool | None) -> tuple[bool, str]:
    if worktree_clean is None:
        return _verdict("C6", False, "worktree-unknown")
    return _verdict("C6", worktree_clean, "clean-worktree" if worktree_clean else "dirty-worktree")


def choose_model(
    *,
    handoff_text: str,
    repo_kind: str | None,
    thread_flags: ThreadFlags,
    worktree_clean: bool | None,
    policy: ModelPolicyConfig,
    design_text: str | None = None,
) -> ModelChoice:
    """Pick the model for one turn: Sonnet only when C1-C6 all pass, otherwise Opus.

    ``handoff_text`` is the handoff carrying the structured fields; C5 scans it together with the
    optional ``design_text`` (the design message the handoff points at). ``worktree_clean`` is
    whether ``git status --porcelain`` was empty at spawn (``None`` = undetermined, fails C6).
    """
    fields = parse_handoff_fields(handoff_text)
    verdicts = (
        _c1(fields, policy),
        _c2(repo_kind),
        _c3(fields, policy),
        _c4a(thread_flags),
        _c4b(thread_flags),
        _c5(f"{handoff_text}\n{design_text or ''}", policy),
        _c6(worktree_clean),
    )
    reasons = tuple(reason for _, reason in verdicts)
    if all(ok for ok, _ in verdicts):
        return ModelChoice(policy.sonnet_model, reasons)
    return ModelChoice(policy.opus_model, reasons)
