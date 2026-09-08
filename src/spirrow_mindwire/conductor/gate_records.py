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
    by :func:`ci_route_heads`, so a change to the other fields cannot break R5.
    """
    payload = json.dumps(
        {"head": normalize_sha(head), "conclusion": conclusion, "checks": list(checks)},
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"{_CI_ROUTE_OPEN}{payload}{_CI_ROUTE_CLOSE}"


def ci_route_heads(bodies: Iterable[str]) -> frozenset[str]:
    """Every head named by a ci-route marker in ``bodies`` — ``gate_admission``'s R5 input.

    A malformed marker contributes nothing instead of raising: the caller is a scheduled loop
    reading a thread it does not control, and one unparseable comment must not stop the tick.
    The cost of dropping one is bounded and known — R5 does not fire for that head, so a second
    red routes an implementer instead of a human, which is R4's behaviour, i.e. the behaviour
    before this wiring existed.
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


__all__ = [
    "ci_route_heads",
    "normalize_sha",
    "render_ci_route_marker",
    "render_relay_heading",
    "verdict_heads",
]
