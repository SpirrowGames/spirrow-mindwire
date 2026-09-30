"""The two head-keyed facts :func:`~spirrow_mindwire.gate_admission.gate_admission` reads back.

``gate_admission`` is stateless by design (v0.3.1 §A-2: GitHub is the state store, never a
mindwire-side counter or a ``next_at`` file). Two of its inputs are nevertheless *history* —
"has the gate already produced a verdict on this head?" (``verdict_heads``, R6) and "has the
implementer already been dispatched at this head's red CI?" (``ci_red_routed_heads``, R5) — so
they have to be recovered from somewhere durable. The design's answer is: from the design
thread itself, off the conductor's own ``pr-gate-relay`` messages. This module is that reader,
and the writer for the one record that does not already exist.

Why a module and not two helpers on the conductor
-------------------------------------------------

The write side and the read side of a marker are the same fact stated twice, and the failure
mode when they drift is silent: R5 stops firing, an implementer is re-dispatched at a red head
it already failed to fix, and nothing anywhere reports an error. Keeping
:func:`render_ci_route_marker` and :func:`ci_route_heads` in one file lets one test drive the
round trip (write → parse → the same head), which is the only test that can catch drift.

What is a NEW record here and what is not (design §5.2A.5)
----------------------------------------------------------

Only ONE new record is sanctioned: the ci-route marker, written **on R4 only**. Deferrals — the
frequent case — write nothing at all, which is precisely why R5's input cannot be derived by
counting messages and needs this marker. The byte form below is the design's, verbatim.

``verdict_heads`` adds no record. The verdict relay is a message the conductor already posts on
every gate firing; this only *names the head in its heading*, which is the form
:func:`gate_admission`'s own parameter documentation calls for ("a marker or heading naming the
head is enough"). Before this, the relay named the PR but not the SHA, so the thread recorded
that a verdict existed without recording what it was a verdict *on* — enough to read, not
enough to key R6 by.

Both readers are deliberately restricted to messages authored by the conductor's relay author.
A critique that quotes a marker (this arc's own review turns have quoted markers verbatim) must
not be read as one, and the same restriction is what
:meth:`~spirrow_mindwire.conductor.core.Conductor._attested` applies to attestation stamps for
the same reason. Author filtering is not authentication — the chatroom accepts any author string
(see ``_is_human``'s statement of the same trust model) — it is noise rejection, and the two
facts it feeds only ever make the conductor do *less*: R6 suppresses a re-fire and R5 stops a
re-dispatch. A forged marker cannot make the loop act, only make it stop and ask a human.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from enum import StrEnum

from ..github.client import ReviewEvent
from ..github.reviews import parse_verdict_footer
from ..naysayer.pr_review import ObjectionParse, parse_objections

#: The reserved author under which the conductor's PR-gate verdict relay (and the R3/R4/R5
#: admission posts) are written. Both readers below are restricted to messages authored under
#: this name (noise rejection, not authentication — see module docstring). The constant lives
#: here rather than in a writer module because two writers now exist (``conductor.core`` for the
#: admission posts, ``orchestrator._post_design_relay`` for the verdict) and both readers live
#: here; duplicating the literal is the exact drift that flips ``verdict_heads`` silently empty
#: (T-pr-gate-relay-belongs-to-the-conductor msg-2835 §4 C-2). ``gate_records`` owns the record
#: vocabulary — the leaf module both writers depend on — so one file defines the string.
RELAY_AUTHOR = "pr-gate-relay"

#: The ci-route marker, byte-for-byte from design v0.3.1 §5.2A.5:
#: ``<!-- mindwire:ci-route v1 {"head":"<sha>","conclusion":"failure","checks":["gate"]} -->``
_CI_ROUTE_OPEN = "<!-- mindwire:ci-route v1 "
_CI_ROUTE_CLOSE = " -->"

#: Tolerant on read, exact on write. The JSON payload is matched non-greedily up to the closing
#: comment so a marker that a later version pretty-prints, or that a chatroom renderer has
#: re-wrapped, still parses; anything that is not a JSON object between the two delimiters is
#: skipped rather than raising.
_CI_ROUTE_RE = re.compile(r"<!--\s*mindwire:ci-route\s+v1\s+(\{.*?\})\s*-->", re.DOTALL)

#: The head SHA on a verdict relay's heading line. Anchored to the END of the FIRST line, which
#: is the only place :func:`render_relay_heading` writes it. Scanning the whole body would read a
#: SHA out of a quoted critique — the same mistake ``_attested`` documents for stamps.
_RELAY_HEAD_RE = re.compile(r"@\s*([0-9a-fA-F]{7,40})\s*$")


def normalize_sha(sha: str) -> str:
    """Lower-case a hex SHA so a head from GitHub and one read back from a body compare equal.

    :func:`gate_admission` tests membership with ``in`` against a ``frozenset[str]``, i.e. exact
    string equality. GitHub's API answers lower-case, but a marker or heading travels through
    human hands (a quoted body, a hand-written correction), so both sides are normalised at
    every boundary rather than trusting one producer's casing.
    """
    return sha.strip().lower()


def render_ci_route_marker(*, head: str, conclusion: str, checks: Sequence[str]) -> str:
    """The R4 marker for ``head``, in the design's compact form.

    ``checks`` names the red checks, for a human reading the thread; only ``head`` is read back
    by :func:`ci_route_heads`, so a change to the other fields cannot break R5 — the ``>`` escape
    below is what makes that true. Without it a check name containing ``}`` followed by ``-->``
    (GitHub check-run names are arbitrary strings, written in the target repo's workflow) would
    close this comment early, and the reader's non-greedy capture would truncate the payload.
    """
    payload = json.dumps(
        {"head": normalize_sha(head), "conclusion": conclusion, "checks": list(checks)},
        separators=(",", ":"),
        ensure_ascii=True,
    )
    # ``>`` is never JSON syntax — it can only occur inside a string value — so escaping it here
    # makes ``-->`` structurally impossible inside the payload, and _CI_ROUTE_RE's non-greedy
    # capture can no longer stop short of this marker's own closing brace. json.loads restores
    # the character, so the value a reader gets back is unchanged. Tab/newline/CR were already
    # safe (ensure_ascii turns them into backslash escapes); a plain space was not.
    payload = payload.replace(">", "\\u003e")
    return f"{_CI_ROUTE_OPEN}{payload}{_CI_ROUTE_CLOSE}"


def ci_route_heads(bodies: Iterable[str]) -> frozenset[str]:
    """Every head named by a ci-route marker in ``bodies`` — ``gate_admission``'s R5 input.

    A malformed marker contributes nothing instead of raising: the caller is a scheduled loop
    reading a thread it does not control, and one unparseable comment must not stop the tick.
    The cost of dropping one is bounded and known — R5 does not fire for that head, so a second
    red routes an implementer instead of a human, which is R4's behaviour — strictly more
    conservative than the pre-wiring path, which fired the gate regardless of CI (see
    :class:`~spirrow_mindwire.conductor.core.CheckRollupSource` and the "pre-wiring behaviour"
    log line beside it, both of which name firing the gate as what came before).
    """
    heads: set[str] = set()
    for body in bodies:
        for raw in _CI_ROUTE_RE.findall(body):
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            head = payload.get("head")
            if isinstance(head, str) and head.strip():
                heads.add(normalize_sha(head))
    return frozenset(heads)


def render_relay_heading(pr_ref: str, head: str | None) -> str:
    """The verdict relay's first line, naming the head when one is known.

    ``head`` is ``None`` when the gate could not resolve one (``PrReviewOutcome.head_sha`` is
    optional — a CI read that failed closed still produces a verdict). The heading then reads
    exactly as it did before this wiring, and :func:`verdict_heads` records nothing for it.
    That is the correct outcome, not a gap to paper over: R6 must dedupe a re-review only when
    it can name the head the first verdict was about.
    """
    heading = f"PR-gate (Tier B independent naysayer) — {pr_ref}"
    return f"{heading} @ {normalize_sha(head)}" if head else heading


def verdict_heads(bodies: Iterable[str]) -> frozenset[str]:
    """Every head named by a verdict relay heading in ``bodies`` — ``gate_admission``'s R6 input."""
    heads: set[str] = set()
    for body in bodies:
        first_line = body.strip().splitlines()[0] if body.strip() else ""
        match = _RELAY_HEAD_RE.search(first_line)
        if match:
            heads.add(normalize_sha(match.group(1)))
    return frozenset(heads)


# ---- U3' (T-tier-c-admission-gate, Bohr msg-4774 / msg-4776, Einstein go) -------------------- #
#
# Where a verdict relay sends the loop next. Before this, the rule lived twice — once in
# ``orchestrator._post_design_relay`` (the printed ``NEXT:``) and once in ``conductor.core`` (the
# real stop / dispatch) — and both said "REQUEST_CHANGES → implementer, everything else →
# human". So an APPROVE carrying advisory objections parked at the human, who was then asked
# "fix this nit before merge?" (msg-3630 §1 source 2; this thread's own msg-3817 → msg-3835 →
# PR #325 → msg-3904 spent two human decisions on one merge). The rule now lives ONLY in
# :func:`decide_relay_route`; the relay writer calls it once and both the printed ``NEXT:`` and
# the conductor's branch are read off its answer (Einstein msg-4775 #2).
#
# Merge authority is preserved structurally: no row sends an APPROVE anywhere but the human
# except the FIRST advisory-carrying APPROVE on a PR, and that row only inserts one implementer
# turn before the same merge request (the implementer is told to end with TIER-C:
# merge-protected / NEXT: human). Every misread fails toward the human or toward exactly one
# extra implementer turn — never past the merge hand-off (msg-4774 "パーサー").


class RelayRoute(StrEnum):
    """The one routing answer the verdict relay produces (msg-4776 #2)."""

    IMPLEMENTER = "implementer"
    HUMAN = "human"


#: Appended (above the ``NEXT:`` line) ONLY on the first-advisory → implementer route, so the
#: implementer knows on the spot what to do with an advisory-only APPROVE without its global
#: system prompt growing for a PR-gate edge case (Einstein msg-4775 #1, Bohr msg-4776 #1 — text
#: verbatim from msg-4776). It must never contain a column-zero objections sentinel or a verdict
#: footer: :func:`prior_advisory_approvals` re-parses relay bodies, and the instruction must not
#: change what they parse as.
ADVISORY_SELF_TRIAGE_INSTRUCTION = (
    "Advisory のみの APPROVE。human に「直すか」を聞かずに、自分で裁くこと: 安く直せるなら同じ "
    "PR で直して push する (再ゲートになる)。見送る advisory は `DECIDED: <どれを> — <理由>` を "
    "1 行ずつ書く。見送りだけで終える場合は、最後に `TIER-C: merge-protected` と `NEXT: human` "
    "を書いて merge を依頼する。"
)

#: The label an APPROVE → human relay carries on the line above its ``NEXT: human``: every such
#: hand-off is a merge request, i.e. the ``merge-protected`` Tier-C type (msg-4772 / msg-4774).
MERGE_REQUEST_TIER_C_LINE = "TIER-C: merge-protected"

#: Objection-parse statuses that mean "no advisory to triage". ``MISSING`` is here on purpose:
#: an unreadable block is not evidence of an advisory, and the safe direction is the human
#: (msg-4774 table row 2).
_NO_ADVISORY = frozenset({ObjectionParse.EMPTY, ObjectionParse.MISSING})


def carries_advisory(verdict: ReviewEvent, objections: ObjectionParse) -> bool:
    """True iff this is an APPROVE whose objection block parsed to something other than
    ``EMPTY`` / ``MISSING`` — i.e. an APPROVE with (non-blocking) objections attached."""
    return verdict is ReviewEvent.APPROVE and objections not in _NO_ADVISORY


def prior_advisory_approvals(messages: Iterable[tuple[str, str]], pr_ref: str) -> int:
    """How many earlier verdict relays for ``pr_ref`` were an APPROVE carrying advisories.

    ``messages`` is ``(author, body)`` for the design thread. Counted: messages by
    :data:`RELAY_AUTHOR` whose first line is this PR's relay heading, whose verdict footer says
    ``event=APPROVE``, and whose objection block parses to neither ``EMPTY`` nor ``MISSING``.
    REQUEST_CHANGES relays and clean APPROVEs are NOT counted — counting every relay (the
    ``verdict_heads`` shape) would call the first advisory after a fixed REQUEST_CHANGES a
    "second" one and skip the implementer's triage turn (Einstein msg-4773).

    Same trust model as the rest of this module: author filtering is noise rejection, not
    authentication, and a forged record can only raise this count — which routes to the human,
    i.e. makes the loop do less, never more.
    """
    heading = render_relay_heading(pr_ref, None)
    count = 0
    for author, body in messages:
        if author != RELAY_AUTHOR:
            continue
        stripped = body.strip()
        first_line = stripped.splitlines()[0] if stripped else ""
        if first_line != heading and not first_line.startswith(f"{heading} @"):
            continue
        footer = parse_verdict_footer(body)
        if footer is None or footer[1] is not ReviewEvent.APPROVE:
            continue
        if carries_advisory(ReviewEvent.APPROVE, parse_objections(body).status):
            count += 1
    return count


def decide_relay_route(
    verdict: ReviewEvent,
    objections: ObjectionParse,
    prior_advisory_approvals: int | None,
    implementer: str | None,
) -> RelayRoute:
    """The msg-4774 table, and the ONLY place it is evaluated. Pure.

    ============================  ==========================  ===========
    verdict / objections          prior advisory APPROVEs     route
    ============================  ==========================  ===========
    REQUEST_CHANGES               —                           implementer
    APPROVE, EMPTY or MISSING     —                           human
    APPROVE, anything else        0                           implementer
    APPROVE, anything else        ≥ 1                         human
    COMMENT                       —                           human
    ============================  ==========================  ===========

    ``prior_advisory_approvals is None`` means the history could not be read; that fails to the
    human (the count that would stop an advisory chain is unknown, so do not start one). No
    implementer persona also fails to the human — there is nobody to route to. Relay-post
    failure is the conductor's fail-safe, not a policy row, and stays there (msg-4776 #2).
    """
    if not implementer:
        return RelayRoute.HUMAN
    if verdict is ReviewEvent.REQUEST_CHANGES:
        return RelayRoute.IMPLEMENTER
    if carries_advisory(verdict, objections) and prior_advisory_approvals == 0:
        return RelayRoute.IMPLEMENTER
    return RelayRoute.HUMAN


__all__ = [
    "ADVISORY_SELF_TRIAGE_INSTRUCTION",
    "MERGE_REQUEST_TIER_C_LINE",
    "RELAY_AUTHOR",
    "RelayRoute",
    "carries_advisory",
    "ci_route_heads",
    "decide_relay_route",
    "normalize_sha",
    "prior_advisory_approvals",
    "render_ci_route_marker",
    "render_relay_heading",
    "verdict_heads",
]
