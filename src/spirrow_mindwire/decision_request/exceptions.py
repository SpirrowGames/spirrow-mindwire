"""Exception catalog for decision-request composers.

The wrapper catches :class:`DecisionComposerError` broadly and records the
subclass in the envelope's ``composer_status`` (see
:class:`~spirrow_mindwire.decision_request.value_objects.ComposerStatus`).
A backend that surfaces its own subclasses (a timeout inside a Claude Code
adapter, for example) inherits from these bases so a single ``except``
covers every failure mode of the port.

I-2 is enforced at the wrapper layer, not here. What these exceptions
guarantee is that a composer failure is a specific, catchable event —
never a raise that reaches the sweep's ``try/except`` and looks
indistinguishable from a bug in the sweep itself.
"""

from __future__ import annotations


class DecisionComposerError(Exception):
    """Root of the composer exception hierarchy.

    The wrapper catches this broadly; specific subclasses let a caller
    that wants per-outcome handling do so (e.g. a timeout returns a
    dedicated envelope status).
    """


class DecisionComposerTimeoutError(DecisionComposerError):
    """The composer exceeded its deadline.

    Maps to :attr:`~spirrow_mindwire.decision_request.value_objects.ComposerStatus.TIMEOUT`.
    """


class DecisionComposerEmptyError(DecisionComposerError):
    """The composer returned no usable output (e.g. blank question).

    Distinct from a generic error because the sweep can decide to move
    on without a retry — the composer ran, it just had nothing to say —
    whereas an error might warrant a follow-up.

    Maps to :attr:`~spirrow_mindwire.decision_request.value_objects.ComposerStatus.EMPTY`.
    """


__all__ = [
    "DecisionComposerEmptyError",
    "DecisionComposerError",
    "DecisionComposerTimeoutError",
]
