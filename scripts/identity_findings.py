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

  5. Store check (write half, msg-1706 §4). For every classified identity, the record the
     identity store actually holds (``get_identity``) is run through the same two-way guard
     the write path uses (:func:`~spirrow_mindwire.identity.check_store_record`). This is
     what makes a record written by hand with ``upsert_identity``, bypassing
     ``scripts/register_identities.py`` (for example ``machine`` with a role, or a
     participant marked ``machine``), show up. Mindwire's guard only binds mindwire's own
     writes, and Prismind deliberately does not enforce the pairing (msg-1706 §1). Before
     this check existed the script never read the store, so such a record went undetected
     (measured when PR-B started).

  6. Per-identity registration cut (PR-C, msg-5676 §1/§4 + msg-5678 §2 + msg-5680 §2-§3).
     For every author in the scanned corpus, plus every critical-path identity, the store
     record (``get_identity``) supplies the cut: that identity's own ``created_at``. Posts
     are split into before / after the cut, and each registered participant
     (``independence_class != "machine"``) gets exactly one state:

       * ``violated``  — at least one post after the cut has a null role
       * ``evidenced`` — at least one post after the cut, none with a null role
       * ``silent``    — no posts after the cut
       * ``undetermined`` — the cut could not be computed (lookup failed, or a
         ``created_at`` that does not parse). This is not one of the three design states. It
         is the honest value when the measurement itself failed, and it fails start
         condition 1 instead of passing it by omission.

     Unregistered authors get no cut (``cut_reason: "unregistered"``) and no state.
     Phase 2's rule never resolves an identity for them (msg-5678 §1).

     ``phase2_start`` then evaluates the start condition literally:
       1. no participant is ``violated`` (nor ``undetermined``);
       2. every critical-path identity (:data:`_CRITICAL_PATH`, fixed in msg-5680 §2) is
          ``evidenced``;
       3. other participants may be ``silent``.

     The cut is the store's value, not a date this script chooses (msg-5674 §2). So the
     cut block reads EVERY scanned message and ignores ``--since-created-at`` /
     ``--since-msg-id`` (those still scope sections 1-4). For ``human``, the post-cut
     null posts are further split by whether the body carries the delegation record
     (operator posting on Takahito's behalf, msg-5222 / msg-5692).

The script is READ-ONLY. It never posts, never marks read, and never writes the identity
store (``get_identity`` is its only store call). It is the "測る" half of msg-1491 §4's
read/write split. It can always run and does not depend on the readiness lock.

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
      "store": [
        {"identity_name": "pr-gate-relay", "kind": "machine", "status": "found",
         "independence_class": "machine", "allowed_roles": [], "violations": []}
      ],
      "identity_cut": [
        {"identity_name": "human", "store_status": "found", "independence_class": "human",
         "participant": true, "critical_path": true,
         "created_at": "2026-05-29T23:44:46.084011", "cut": "2026-05-29T14:44:46.084011Z",
         "cut_reason": null, "first_post_at": "...", "last_post_at": "...",
         "pre_cut": {"posts": 0, "null_role": 0}, "post_cut": {"posts": 453, "null_role": 418},
         "undated": {"posts": 0, "null_role": 0}, "state": "violated",
         "last_post_cut_null_at": "2026-10-02T00:27:53Z",
         "delegation": {"post_cut_null_role": 418, "delegated": 3, "not_delegated": 415,
                        "post_cut_with_role_delegated": 0}}
      ],
      "phase2_start": {
        "critical_path": ["naysayer-pr-review", "Bohr", "Einstein", "Heisenberg", "human"],
        "condition_1_no_violated": {"pass": false, "violated": ["human"], "undetermined": []},
        "condition_2_critical_path_evidenced": {"pass": false,
          "not_evidenced": [{"identity_name": "human", "state": "violated"}]},
        "pass": false
      },
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
        "collision_groups": 0,
        "store_unregistered": 0,
        "store_violations": 0
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

``store_violations > 0`` means some classified identity's live record disagrees with its
classification, which is tampering or drift. ``store_unregistered > 0`` means
``scripts/register_identities.py --apply`` has not been run, or has not covered that
identity.

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
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

from spirrow_mindwire.identity import (
    MACHINE_INDEPENDENCE_CLASS,
    ClassificationError,
    IdentityCollisionError,
    LegitimateRolesFile,
    check_store_record,
    default_classification_path,
    derive_allowed_and_residual,
    find_collisions,
    load_legitimate_roles,
    normalize_identity_key,
)
from spirrow_mindwire.magickit.client import (
    MagickitMcpError,
    McpToolCaller,
    StreamableHttpChatroomMcp,
)

# PR #153 (`13618e9`, `feat: conductor supplies role...`) merged 2026-08-17 in
# `spirrow-mindwire`. Bohr's msg-1484 §4 pins the scope to "deploy 以降に post した author"
# and this is the deploy in question. Anything older is history — msg-1179 §6 point 2's
# "履歴は null のまま残す" carry-forward.
_DEFAULT_SINCE = "2026-08-17T00:00:00+00:00"
_DEFAULT_PROJECTS = ("spirrow-mindwire", "spirrow-voxelworld")

# Start condition 2's critical path, fixed in the design thread (msg-5678 §2, extended with
# `human` in msg-5680 §2): the identities whose rejection would stop the loop. They carry the
# Tier-B verdict, the proposer, the naysayer, the implementer, and the human's Tier-C decide
# (carve-out ①). The list's source of truth is that thread; this tuple only lets the script
# check it.
_CRITICAL_PATH = ("naysayer-pr-review", "Bohr", "Einstein", "Heisenberg", "human")

# Identities whose post-cut null posts are split by delegation (msg-5680 §2: "for `human`").
_DELEGATION_BREAKDOWN_IDENTITIES = frozenset({"human"})

# The delegation record an operator writes when posting under `human` on Takahito's behalf
# (msg-5222 and msg-5692 both carry `**代行の記録**`). A post that only paraphrases delegation
# without this record is NOT counted as delegated. The breakdown is evidence for the later
# supply-path fix, and a loose match would blur exactly the split it exists to make.
_DELEGATION_MARKERS = ("代行の記録",)

# The identity store returns `created_at` WITHOUT an offset (e.g. "2026-09-30T16:52:37").
# Checked against the thread, those values are JST. `naysayer-pr-review.updated_at`
# 2026-10-01T14:25:03 is the live registration msg-5363 reports, posted at 05:26:49Z. Its
# `created_at` 2026-09-30T16:52 falls inside the implementer turn that msg-4953 says timed
# out at 17:06 JST. Read as UTC, neither value lands on any turn. Reading a JST value as UTC
# would move every cut 9 hours LATE and silently file post-registration nulls as history.
# So the offset is explicit and overridable, never assumed to be UTC.
_DEFAULT_STORE_NAIVE_TZ = "+09:00"


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
        :func:`_parse_cutoff`). Compared against the message's ``created_at``, or,
        when that is absent, its ``timestamp``. The live ``chatroom_get_thread``
        payload carries ``timestamp`` and has no ``created_at`` (measured at the
        start of PR-B on msg-1179). The original ``created_at``-only lookup
        therefore failed open on every live message, and the cutoff was never
        applied. A
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
    parsed = _message_time(msg)
    if parsed is None:
        return True
    return parsed >= cutoff


def _message_time(msg: dict[str, Any]) -> datetime | None:
    """The message's ``created_at`` (or live ``timestamp``) as an aware datetime, else None.

    Chatroom timestamps carry a ``Z`` suffix. A naive one is read as UTC, as before.
    """
    created_at = msg.get("created_at") or msg.get("timestamp")
    if not isinstance(created_at, str) or not created_at:
        return None
    try:
        # chatroom timestamps are ISO 8601 with a `Z` suffix; ``fromisoformat`` accepts
        # `+00:00` but not `Z` on Python <3.11, so normalise first.
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_tz_offset(offset: str) -> timezone:
    """Parse ``+HH:MM`` / ``-HH:MM`` / ``Z`` into a :class:`timezone`. Raises ValueError."""
    if not isinstance(offset, str) or not offset:
        raise ValueError(f"offset must be a non-empty str, got {offset!r}")
    probe = datetime.fromisoformat("2000-01-01T00:00:00" + offset.replace("Z", "+00:00"))
    delta = probe.utcoffset()
    if delta is None:
        raise ValueError(f"not a UTC offset: {offset!r}")
    return timezone(delta)


def _parse_store_time(raw: Any, naive_tz: timezone) -> datetime | None:
    """Parse a store ``created_at``. A naive value is read in ``naive_tz``. Junk gives None."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_tz)
    return parsed


def _iso_z(when: datetime | None) -> str | None:
    if when is None:
        return None
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _is_delegated(msg: dict[str, Any]) -> bool:
    body = msg.get("content")
    if not isinstance(body, str):
        body = msg.get("body")
    return isinstance(body, str) and any(marker in body for marker in _DELEGATION_MARKERS)


# One observed post, as the cut block needs it: (time or None, role or None, delegated).
_Post = tuple[datetime | None, str | None, bool]


def _counts(group: list[_Post]) -> dict[str, int]:
    return {"posts": len(group), "null_role": sum(1 for _, r, _ in group if r is None)}


def _cut_row(
    name: str,
    lookup: dict[str, Any] | None,
    posts: list[_Post],
    naive_tz: timezone,
) -> dict[str, Any]:
    """Fold one author's posts and store record into a PR-C cut row.

    ``lookup`` is the raw ``get_identity`` response, or None when the call itself failed.
    """
    status = "lookup_failed" if lookup is None else str(lookup.get("status") or "unknown")
    record = lookup.get("identity") if lookup is not None else None
    dated = sorted(t for t, _, _ in posts if t is not None)
    row: dict[str, Any] = {
        "identity_name": name,
        "store_status": status,
        "independence_class": None,
        "participant": None,
        "critical_path": name in _CRITICAL_PATH,
        "created_at": None,
        "cut": None,
        "cut_reason": None,
        "first_post_at": _iso_z(dated[0]) if dated else None,
        "last_post_at": _iso_z(dated[-1]) if dated else None,
    }

    cut: datetime | None = None
    if status == "found" and isinstance(record, dict):
        ic = record.get("independence_class")
        row["independence_class"] = ic
        # The Phase 2 rule verbatim (msg-5678 §1): participant iff class != "machine".
        row["participant"] = ic != MACHINE_INDEPENDENCE_CLASS
        row["created_at"] = record.get("created_at")
        cut = _parse_store_time(record.get("created_at"), naive_tz)
        if cut is None:
            row["cut_reason"] = "created_at_unparseable"
    elif status == "not_found":
        row["cut_reason"] = "unregistered"
    else:
        row["cut_reason"] = f"store_status:{status}"
    row["cut"] = _iso_z(cut)

    if cut is None:
        row["all_posts"] = _counts(posts)
        # Unregistered: Phase 2 never resolves an identity, so there is no state. Anything
        # else means the measurement failed (we cannot tell whether this is a participant,
        # or a participant has no usable cut), and that must not read as "not violated".
        row["state"] = None if row["cut_reason"] == "unregistered" else "undetermined"
        return row

    pre = [p for p in posts if p[0] is not None and p[0] < cut]
    post = [p for p in posts if p[0] is not None and p[0] >= cut]
    undated = [p for p in posts if p[0] is None]
    row["pre_cut"] = _counts(pre)
    row["post_cut"] = _counts(post)
    # An undated post cannot be shown to predate registration, so its null counts against
    # the identity (fail-closed). It cannot count as evidence either.
    row["undated"] = _counts(undated)
    # Diagnostic only, never an input to `state`: when did a post-cut null last happen?
    # An identity registered long before its role supply was fixed is "violated" by old
    # posts, and this field is how a reader tells that apart from a live supply break.
    post_nulls = sorted(t for t, r, _ in post if r is None and t is not None)
    row["last_post_cut_null_at"] = _iso_z(post_nulls[-1]) if post_nulls else None

    if not row["participant"]:
        row["state"] = None
    elif row["post_cut"]["null_role"] or row["undated"]["null_role"]:
        row["state"] = "violated"
    elif row["post_cut"]["posts"]:
        row["state"] = "evidenced"
    else:
        row["state"] = "silent"

    if name in _DELEGATION_BREAKDOWN_IDENTITIES:
        post_null = [p for p in post if p[1] is None]
        delegated = sum(1 for p in post_null if p[2])
        row["delegation"] = {
            "post_cut_null_role": len(post_null),
            "delegated": delegated,
            "not_delegated": len(post_null) - delegated,
            "post_cut_with_role_delegated": sum(1 for p in post if p[1] is not None and p[2]),
        }
    return row


def _phase2_start(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate the Phase 2 start condition (msg-5678 §2 as revised by msg-5680 §2).

    ``rows`` must hold a row for every critical-path identity. :func:`_identity_cut`
    guarantees that (it iterates ``authors | _CRITICAL_PATH`` and emits an
    ``undetermined`` row when a lookup fails), so a missing row is a caller bug,
    not a measurement outcome: raise :class:`ValueError` rather than invent a state.
    """
    by_name = {r["identity_name"]: r for r in rows}
    missing = [name for name in _CRITICAL_PATH if name not in by_name]
    if missing:
        raise ValueError(f"no cut row for critical-path identities: {missing}")
    violated = sorted(r["identity_name"] for r in rows if r["state"] == "violated")
    undetermined = sorted(r["identity_name"] for r in rows if r["state"] == "undetermined")
    not_evidenced = [
        {"identity_name": name, "state": by_name[name]["state"]}
        for name in _CRITICAL_PATH
        if by_name[name]["state"] != "evidenced"
    ]
    cond1 = not violated and not undetermined
    cond2 = not not_evidenced
    return {
        "critical_path": list(_CRITICAL_PATH),
        "condition_1_no_violated": {
            "pass": cond1,
            "violated": violated,
            "undetermined": undetermined,
        },
        "condition_2_critical_path_evidenced": {"pass": cond2, "not_evidenced": not_evidenced},
        "pass": cond1 and cond2,
    }


async def _identity_cut(
    mcp: McpToolCaller, author_posts: dict[str, list[_Post]], naive_tz: timezone
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Look every corpus author (plus the critical path) up in the store and build cut rows."""
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for name in sorted(set(author_posts) | set(_CRITICAL_PATH)):
        lookup: dict[str, Any] | None
        try:
            got: Any = await mcp.call_tool("get_identity", {"identity_name": name})
            lookup = got if isinstance(got, dict) else {"status": "malformed_response"}
        except MagickitMcpError as exc:
            errors.append({"identity_name": name, "reason": f"get_identity failed: {exc}"})
            lookup = None
        rows.append(_cut_row(name, lookup, author_posts.get(name, []), naive_tz))
    return rows, errors


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


async def _check_store(
    mcp: McpToolCaller, classification: LegitimateRolesFile
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Read each classified identity's store record and run the write-path guard on it.

    Returns ``(rows, errors)``. A transport failure on one identity is recorded in ``errors``
    and the identity is reported as ``lookup_failed``. It is never folded into "consistent".
    """
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for entry in classification.entries:
        try:
            got: Any = await mcp.call_tool("get_identity", {"identity_name": entry.name})
        except MagickitMcpError as exc:
            errors.append({"identity_name": entry.name, "reason": f"get_identity failed: {exc}"})
            rows.append(
                {
                    "identity_name": entry.name,
                    "kind": entry.kind,
                    "status": "lookup_failed",
                    "violations": [],
                }
            )
            continue
        rows.append(check_store_record(entry, got if isinstance(got, dict) else {}))
    return rows, errors


async def _measure(
    projects: Iterable[str],
    since_iso: str,
    since_cutoff: datetime,
    since_msg_id: str | None,
    url: str | None,
    classification: LegitimateRolesFile,
    classification_path: Path,
    store_naive_tz: str = _DEFAULT_STORE_NAIVE_TZ,
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

    ``store_naive_tz`` is the offset a naive store ``created_at`` is read in (see
    :data:`_DEFAULT_STORE_NAIVE_TZ`). ``main()`` validates it before any network I/O.
    """
    naive_tz = _parse_tz_offset(store_naive_tz)
    mcp = StreamableHttpChatroomMcp(url)
    author_role_counts: dict[str, dict[str | None, int]] = defaultdict(lambda: defaultdict(int))
    # Every scanned post per author, regardless of --since-*: the cut block's own scope.
    author_posts: dict[str, list[_Post]] = defaultdict(list)
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
                author = message.get("author")
                has_author = isinstance(author, str) and bool(author.strip())
                role_raw = message.get("role")
                role_key: str | None = role_raw if isinstance(role_raw, str) and role_raw else None
                if has_author:
                    assert isinstance(author, str)
                    author_posts[author].append(
                        (_message_time(message), role_key, _is_delegated(message))
                    )
                if not _in_scope(message, since_cutoff, since_msg_id):
                    continue
                messages_in_scope += 1
                if not has_author:
                    continue
                assert isinstance(author, str)
                author_role_counts[author][role_key] += 1

    store_rows, store_errors = await _check_store(mcp, classification)
    errors.extend(store_errors)
    cut_rows, cut_errors = await _identity_cut(mcp, dict(author_posts), naive_tz)
    errors.extend(cut_errors)
    phase2 = _phase2_start(cut_rows)

    plain_counts = {a: dict(rc) for a, rc in author_role_counts.items()}
    entries, unclassified, residual_count, unused_count = _summarise(plain_counts, classification)
    collisions = find_collisions(plain_counts.keys())

    return {
        "scope": {
            "projects": list(projects),
            "since_created_at": since_iso,
            "since_msg_id": since_msg_id,
            "classification_path": str(classification_path),
            "store_naive_tz": store_naive_tz,
            "cut_scope": "all scanned messages (ignores --since-created-at / --since-msg-id)",
        },
        "authors": entries,
        "unclassified_authors": unclassified,
        "store": store_rows,
        "identity_cut": cut_rows,
        "phase2_start": phase2,
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
            "store_unregistered": sum(1 for r in store_rows if r["status"] != "found"),
            "store_violations": sum(1 for r in store_rows if r["violations"]),
            "phase2_start_pass": phase2["pass"],
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
        "--store-naive-tz",
        default=_DEFAULT_STORE_NAIVE_TZ,
        help=(
            "UTC offset a naive identity-store created_at is read in, for the per-identity "
            f"cut. Default: {_DEFAULT_STORE_NAIVE_TZ} (measured; see _DEFAULT_STORE_NAIVE_TZ)."
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

    try:
        _parse_tz_offset(args.store_naive_tz)
    except ValueError as exc:
        print(
            f"identity_findings: --store-naive-tz is not a UTC offset: "
            f"{args.store_naive_tz!r} ({exc})",
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
                store_naive_tz=args.store_naive_tz,
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
