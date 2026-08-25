"""Read-only findings for the T-role-null-must-become-impossible read half.

Answers the four measurements Bohr's DoD (msg-1487 §8, msg-1491 §6, msg-1493 §6) requires
before the write half can supply values to ``upsert_identity``:

  1. Author enumeration — every author that has posted in the given projects since the
     cutoff (default: PR #153's merge commit, per msg-1484 §4).
  2. Per-author observed role set — the set of ``role`` values the identity has actually
     supplied on messages in scope.
  3. ADR-11 normalisation collisions — raw author spellings that collapse to one canonical
     key. Reported, not resolved (msg-1487 §6 point 1: "**衝突は登録せず報告**").
  4. Derived ``(allowed_roles, residual, unused)`` per classified identity, computed by
     :func:`~spirrow_mindwire.identity.derive_allowed_and_residual` — the "by construction"
     derivation Einstein required in msg-1492, in the msg-1585 §3 corrected form
     (``allowed_roles = legitimate``; the observation feeds ``residual`` / ``unused`` only).

The script is READ-ONLY. It never posts, never marks read, never touches the identity
store. It is the "測る" half of msg-1491 §4's read/write split — always executable, does
not depend on the readiness lock.

Output shape (stdout JSON):

    {
      "scope": {
        "projects": ["spirrow-mindwire", "spirrow-voxelworld"],
        "since_created_at": "2026-08-17T00:00:00Z",
        "since_msg_id": null,
        "classification_path": "spec/identity/legitimate_roles.yaml"
      },
      "authors": [
        {
          "raw_name": "naysayer-pr-review",
          "normalized_key": "naysayer-pr-review",
          "post_count": 12,
          "observed_roles": ["naysayer"],
          "role_counts": [{"role": "naysayer", "count": 11}, {"role": null, "count": 1}],
          "classification": {
            "known": true,
            "kind": "participant",
            "legitimate": ["naysayer"],
            "primary_source": "src/spirrow_mindwire/orchestrator.py::..."
          },
          "derivation": {
            "allowed_roles": ["naysayer"],
            "residual": [],
            "unused": []
          }
        },
        ...
      ],
      "unclassified_authors": ["some-new-author"],
      "collisions": {"foo-bar": ["foo-bar", "Foo_Bar"]},
      "errors": [],
      "totals": {
        "threads_scanned": 341,
        "messages_scanned": 6879,
        "messages_in_scope": 512,
        "classified_authors": 4,
        "unclassified_authors": 1,
        "authors_with_residual": 0,
        "authors_with_unused": 0,
        "collision_groups": 0
      }
    }

``observed_roles`` lists only the roles the identity actually CLAIMED: a ``null`` role is
the absence of a claim, not a role named "null", so it is excluded there and counted in
``role_counts`` instead (where ``"role": null`` is JSON's own null, never a sentinel
string — a sentinel would collide with a literal ``"<null>"`` role in the corpus and
silently merge two counts).

Exit codes:

  0 — findings produced (the JSON above is on stdout, even if there are unclassified
      authors or non-empty residuals — those are outcomes, not errors)
  1 — the script itself failed (transport dead, classification file unreadable, etc.)

Non-empty ``unclassified_authors`` or ``collisions`` or ``authors_with_residual > 0`` is
the SIGNAL that the write half MUST NOT proceed until each is resolved (``authors_with_unused``
is NOT such a signal — see :func:`_summarise`) — msg-1493 §5:
"登録済み and 理由付き保留 together cover the scope, and 無説明残余 = 0". This script
does not enforce that; it produces the evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spirrow_mindwire.identity import (
    ClassificationError,
    IdentityCollisionError,
    LegitimateRolesFile,
    default_classification_path,
    derive_allowed_and_residual,
    find_collisions,
    load_legitimate_roles,
    normalize_identity_key,
)
from spirrow_mindwire.magickit.client import MagickitMcpError, StreamableHttpChatroomMcp

# PR #153 (`13618e9`, `feat: conductor supplies role...`) merged 2026-08-17 in
# `spirrow-mindwire`. Bohr's msg-1484 §4 pins the scope to "deploy 以降に post した author"
# and this is the deploy in question. Anything older is history — msg-1179 §6 point 2's
# "履歴は null のまま残す" carry-forward.
_DEFAULT_SINCE = "2026-08-17T00:00:00+00:00"
_DEFAULT_PROJECTS = ("spirrow-mindwire", "spirrow-voxelworld")


async def _list_threads(mcp: StreamableHttpChatroomMcp, project: str) -> list[dict[str, Any]]:
    """Enumerate every thread in ``project``, paging through ``chatroom_list_threads``.

    Same paging shape as ``scripts/gen_next_line_corpus.py`` — a stopping condition based
    on both an empty page and the ``total`` field, whichever comes first, so a transient
    off-by-one in one field does not silently truncate.
    """
    threads: list[dict[str, Any]] = []
    offset = 0
    while True:
        page: Any = await mcp.call_tool(
            "chatroom_list_threads", {"project": project, "limit": 200, "offset": offset}
        )
        if not isinstance(page, dict):
            break
        items = page.get("items") or []
        if not isinstance(items, list) or not items:
            break
        threads.extend(item for item in items if isinstance(item, dict))
        offset += len(items)
        total = page.get("total")
        if isinstance(total, int) and offset >= total:
            break
    return threads


async def _fetch_thread_messages(
    mcp: StreamableHttpChatroomMcp, project: str, thread_id: str
) -> list[dict[str, Any]]:
    """Return the full message list for ``thread_id``. Raises :class:`MagickitMcpError`."""
    body: Any = await mcp.call_tool(
        "chatroom_get_thread",
        {"project": project, "thread_id": thread_id, "mode": "full"},
    )
    if not isinstance(body, dict):
        return []
    messages = body.get("messages")
    if not isinstance(messages, list):
        return []
    return [m for m in messages if isinstance(m, dict)]


def _parse_cutoff(since_iso: str) -> datetime:
    """Parse the caller-supplied cutoff string into an aware :class:`datetime`.

    Raises :class:`ValueError` with a caller-facing message on failure — including
    the ``None`` / empty / non-string cases (argparse's ``default=_DEFAULT_SINCE``
    means ``args.since_created_at`` is always a non-empty str via the CLI, but a
    programmatic caller could still pass ``None``; converting that to a
    ``ValueError`` keeps the failure class consistent with the docstring contract
    instead of leaking an ``AttributeError`` from ``.replace()``).

    Meant to be called once, in ``main()`` — a typo in ``--since-created-at`` is
    a caller bug that must fail fast BEFORE any network I/O, not silently in the
    per-message hot loop (a per-message fallback there would emit no error and
    scan the entire project history under a typo like ``--since-created-at=2026/08/17``).
    """
    if not isinstance(since_iso, str) or not since_iso:
        raise ValueError(f"since_iso must be a non-empty str, got {since_iso!r}")
    # chatroom timestamps are ISO 8601 with a `Z` suffix; ``fromisoformat`` accepts
    # `+00:00` but not `Z` on Python <3.11, so normalise first.
    parsed = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _in_scope(msg: dict[str, Any], cutoff: datetime, since_msg_id: str | None) -> bool:
    """Whether ``msg`` is at or after the caller-supplied cutoff.

    Two cutoff modes, tried in order:

      * ``since_msg_id`` (chatroom-native): a lex compare on ``msg-NNNN`` strings works
        because the padding is fixed and the ids are monotonic per chatroom project. When
        the caller supplies this AND the message id compares less than the cutoff, drop.
      * ``cutoff``: an aware :class:`datetime` (parsed once in ``main()`` — see
        :func:`_parse_cutoff`). Compared against the message's ``created_at``. A
        message whose ``created_at`` is missing or unparseable is treated as in-scope
        (fail-open on the read side — we would rather over-include than silently drop
        an unreadable message that the write half then never sees a finding about).
        The CALLER's cutoff, however, must have been parsed already; a caller-bug
        typo is not permitted to reach this function.

    Both filters are AND-ed when both are supplied.
    """
    if since_msg_id:
        this_id = str(msg.get("msg_id") or "")
        if this_id and this_id < since_msg_id:
            return False
    created_at = msg.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        return True
    try:
        # chatroom timestamps are ISO 8601 with a `Z` suffix; ``fromisoformat`` accepts
        # `+00:00` but not `Z` on Python <3.11, so normalise first.
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed >= cutoff


def _summarise(
    author_role_counts: dict[str, dict[str | None, int]],
    classification: LegitimateRolesFile,
) -> tuple[list[dict[str, Any]], list[str], int, int]:
    """Fold the observation into the per-author output shape + list unclassified names.

    Returns ``(authors_entries, unclassified_raw_names, authors_with_residual_count,
    authors_with_unused_count)``.

    ``role_counts`` is emitted as a LIST of ``{"role": <str|null>, "count": n}`` objects,
    not a dict. A dict needs a string key, and any sentinel chosen for the null role
    (``"<null>"`` or otherwise) can also occur as a literal role string in the corpus —
    the two would collapse onto one key and one count would silently overwrite the other,
    leaving ``post_count != sum(role_counts)`` in the JSON with no error raised. JSON has a
    real null; use it and the collision class disappears.

    ``observed_roles`` (str-only) is deliberately NOT the same filter: a null role is the
    absence of a claim, not a role, so it must not reach the derivation. A literal
    ``"<null>"`` string role IS a claim, stays in ``observed_roles``, and therefore shows up
    in ``residual`` — the fabrication evidence surfaces either way.
    """
    entries: list[dict[str, Any]] = []
    unclassified: list[str] = []
    residual_count = 0
    unused_count = 0
    for raw_name in sorted(author_role_counts):
        role_counts = author_role_counts[raw_name]
        key = normalize_identity_key(raw_name)
        classification_entry = classification.by_key(key)
        observed_roles: list[str] = sorted(r for r in role_counts if isinstance(r, str))
        post_count = sum(role_counts.values())
        entry: dict[str, Any] = {
            "raw_name": raw_name,
            "normalized_key": key,
            "post_count": post_count,
            "observed_roles": observed_roles,
            "role_counts": [
                {"role": r, "count": n}
                for r, n in sorted(role_counts.items(), key=lambda kv: (kv[0] is None, kv[0] or ""))
            ],
        }
        if classification_entry is None:
            unclassified.append(raw_name)
            entry["classification"] = {"known": False}
        else:
            derivation = derive_allowed_and_residual(
                observed_roles, classification_entry.legitimate
            )
            if derivation.residual:
                residual_count += 1
            if derivation.unused:
                unused_count += 1
            entry["classification"] = {
                "known": True,
                "kind": classification_entry.kind,
                "legitimate": sorted(classification_entry.legitimate),
                "primary_source": classification_entry.primary_source,
            }
            entry["derivation"] = {
                "allowed_roles": sorted(derivation.allowed_roles),
                "residual": sorted(derivation.residual),
                "unused": sorted(derivation.unused),
            }
        entries.append(entry)
    return entries, unclassified, residual_count, unused_count


async def _measure(
    projects: Iterable[str],
    since_iso: str,
    since_cutoff: datetime,
    since_msg_id: str | None,
    url: str | None,
    classification: LegitimateRolesFile,
    classification_path: Path,
) -> dict[str, Any]:
    """Measure the corpus and emit the findings JSON.

    ``since_iso`` is the ORIGINAL caller-supplied string (used only for the ``scope``
    block so the artifact records what the caller actually typed); ``since_cutoff`` is
    the parsed :class:`datetime` :func:`_in_scope` compares against. Both come from
    ``main()`` — do not re-parse ``since_iso`` here or in the hot loop.

    ``classification_path`` is the path ``main()`` actually resolved and loaded
    ``classification`` from — NOT re-derived here. Re-deriving it would make the
    artifact name the default tree path even on a ``--classification`` override run,
    i.e. the JSON would misreport the input its own numbers came from and the finding
    would not be reproducible from what it claims to have read.
    """
    mcp = StreamableHttpChatroomMcp(url)
    author_role_counts: dict[str, dict[str | None, int]] = defaultdict(lambda: defaultdict(int))
    threads_scanned = 0
    messages_scanned = 0
    messages_in_scope = 0
    errors: list[dict[str, str]] = []

    for project in projects:
        threads = await _list_threads(mcp, project)
        threads_scanned += len(threads)
        for thread in threads:
            thread_id = str(thread.get("thread_id") or "")
            if not thread_id:
                continue
            try:
                messages = await _fetch_thread_messages(mcp, project, thread_id)
            except MagickitMcpError as exc:
                errors.append(
                    {
                        "project": project,
                        "thread_id": thread_id,
                        "reason": f"chatroom_get_thread failed: {exc}",
                    }
                )
                continue
            for message in messages:
                messages_scanned += 1
                if not _in_scope(message, since_cutoff, since_msg_id):
                    continue
                messages_in_scope += 1
                author = message.get("author")
                if not isinstance(author, str) or not author.strip():
                    continue
                role_raw = message.get("role")
                role_key: str | None = role_raw if isinstance(role_raw, str) and role_raw else None
                author_role_counts[author][role_key] += 1

    plain_counts = {a: dict(rc) for a, rc in author_role_counts.items()}
    entries, unclassified, residual_count, unused_count = _summarise(plain_counts, classification)
    collisions = find_collisions(plain_counts.keys())

    return {
        "scope": {
            "projects": list(projects),
            "since_created_at": since_iso,
            "since_msg_id": since_msg_id,
            "classification_path": str(classification_path),
        },
        "authors": entries,
        "unclassified_authors": unclassified,
        "collisions": collisions,
        "errors": errors,
        "totals": {
            "threads_scanned": threads_scanned,
            "messages_scanned": messages_scanned,
            "messages_in_scope": messages_in_scope,
            "classified_authors": len(entries) - len(unclassified),
            "unclassified_authors": len(unclassified),
            "authors_with_residual": residual_count,
            # NOT a lock, despite reading as the twin of `authors_with_residual`.
            # `residual != ∅` means the identity claimed a role it may not claim, and
            # registering it starts REJECTING those posts — live blast radius, inside
            # the write set. `unused != ∅` means it simply has not exercised a right it
            # holds; allowing a role nobody uses rejects nothing, so the blast radius is
            # zero and this number never gates the write half (msg-1585 §3).
            "authors_with_unused": unused_count,
            "collision_groups": len(collisions),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        action="append",
        default=None,
        help=(
            "project id (repeatable). Defaults to spirrow-mindwire + spirrow-voxelworld "
            "(the live gate projects at deploy time)."
        ),
    )
    parser.add_argument(
        "--since-created-at",
        default=_DEFAULT_SINCE,
        help=(
            "ISO-8601 cutoff; only messages with created_at >= this are counted. "
            f"Default: {_DEFAULT_SINCE} (PR #153 merge)."
        ),
    )
    parser.add_argument(
        "--since-msg-id",
        default=None,
        help=(
            "Optional msg-id cutoff. When set, messages with msg_id < this are dropped "
            "(applied in addition to --since-created-at)."
        ),
    )
    parser.add_argument(
        "--classification",
        type=Path,
        default=None,
        help=(
            "Path to legitimate_roles.yaml. Default: spec/identity/legitimate_roles.yaml "
            "at the repo root."
        ),
    )
    parser.add_argument(
        "--url", default=None, help="magickit MCP URL (default: in-code/env default)"
    )
    args = parser.parse_args()

    classification_path = args.classification or default_classification_path()
    try:
        classification = load_legitimate_roles(classification_path)
    except (ClassificationError, IdentityCollisionError, OSError) as exc:
        print(f"identity_findings: classification unreadable: {exc}", file=sys.stderr)
        return 1

    # Parse the cutoff ONCE, fail-fast before any network I/O. A typo in this CLI
    # argument (e.g. ``--since-created-at=2026/08/17``) must abort, not silently
    # promote the run to "no cutoff" and scan the whole project history — the read
    # half's fail-open policy is per-message (unreadable ``created_at`` in the
    # corpus), not for the caller's own arguments.
    try:
        since_cutoff = _parse_cutoff(args.since_created_at)
    except ValueError as exc:
        print(
            f"identity_findings: --since-created-at is not ISO 8601: "
            f"{args.since_created_at!r} ({exc})",
            file=sys.stderr,
        )
        return 2

    projects = tuple(args.project) if args.project else _DEFAULT_PROJECTS
    try:
        result = asyncio.run(
            _measure(
                projects=projects,
                since_iso=args.since_created_at,
                since_cutoff=since_cutoff,
                since_msg_id=args.since_msg_id,
                url=args.url,
                classification=classification,
                classification_path=classification_path,
            )
        )
    except Exception as exc:
        # Whole-run failure: transport dead, asyncio setup broke, etc. Different from a
        # per-thread fetch failure (which is recorded in `errors` and does not exit).
        print(f"identity_findings: measurement failed: {exc}", file=sys.stderr)
        return 1

    # ensure_ascii=True so a Japanese exception message that got into `errors[].reason`
    # cannot break a Windows cp932 stdout — same rule as parked_humans.py D-33 note.
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
