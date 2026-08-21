"""Port contract for a decision-request composer.

Single-method Protocol. The port names neither a model nor a transport
(msg-1370 §2): the backend is one adapter behind this shape, swappable in
the same way the RoleAdapter's model+transport bundle is (ADR-05 SEAM).
Phase 1 ships :class:`~spirrow_mindwire.decision_request.stub.StubComposer`
for tests / A-2 / A-3 and the CLI's ``--backend stub`` mode; the Claude
Code adapter lands with slice S3 (see the task's plan in msg-1387 §12.5).

The port is intentionally synchronous. Every production caller — the
CLI, itself invoked by a PowerShell wrapper — is a one-shot invocation
per stop; an ``async`` port would push the caller into an event-loop
just to enter the adapter, which is friction the shape does not need
here. An adapter whose backend is async (a Claude Code SDK session,
for instance) owns its own ``asyncio.run`` boundary; the port stays sync.
"""

from __future__ import annotations

from typing import Protocol

from .value_objects import DecisionRequestInput, DecisionRequestOutput


class DecisionRequestComposer(Protocol):
    """Compose a human-facing decision request for one parked-thread stop.

    A production caller invokes :meth:`compose` **at most once per
    ``reason:last_msg`` signature per stop** (I-3, enforced by the CLI
    wrapping this port). The composer itself does not enforce that; it
    is not asked to remember prior calls.

    Failure modes raise from :mod:`.exceptions`:

    - :class:`~spirrow_mindwire.decision_request.exceptions.DecisionComposerTimeoutError`
      if the composer exceeded a deadline the caller passed to the
      backend (deadline plumbing is the CLI's concern; the port carries
      no timeout parameter to keep the shape narrow).
    - :class:`~spirrow_mindwire.decision_request.exceptions.DecisionComposerEmptyError`
      if the composer returned no usable output (a blank question, no
      options and no free-text — none of which the wrapper can render).
    - :class:`~spirrow_mindwire.decision_request.exceptions.DecisionComposerError`
      for any other backend failure.

    A caller MUST NOT let an ``AdapterError``-style bare ``Exception``
    escape — the wrapper's contract (I-2: raw ping still fires on
    composer failure) depends on catching this hierarchy specifically.
    Bare ``Exception`` from a broken backend is caught by the CLI at
    the outermost level and reported as :class:`DecisionComposerError`.
    """

    identity_name: str
    """The persona label the composer runs under (I-5).

    Must not be one of the three roles in a design thread — Bohr /
    Heisenberg / Einstein or whatever the local roster names them —
    because neutrality is the whole reason this piece exists (msg-1370
    §0 Tier-C: "3 role から独立した専用の composer が判断依頼を書く").
    Recorded verbatim into the envelope so an operator can see which
    identity phrased the question and challenge it if it drifted into
    one of the role personas by accident.
    """

    def compose(self, request: DecisionRequestInput) -> DecisionRequestOutput:
        """Compose a structured decision request from ``request``.

        Contract:

        - The composer MUST NOT read outside ``request`` (I-4): the
          whole thread and the chatroom are off-limits from here. The
          caller has already decided how much tail to hand over.
        - The composer MUST NOT decide (I-1): it phrases the question
          and may recommend, but nothing in the returned value is a
          Tier-C authorization. The dashboard and the wrapper enforce
          the same rule downstream (recommendation is displayed, never
          pre-selected).
        - The output's ``tail_used`` MUST NOT exceed ``len(request.tail)``.
        """
        ...


__all__ = ["DecisionRequestComposer"]
