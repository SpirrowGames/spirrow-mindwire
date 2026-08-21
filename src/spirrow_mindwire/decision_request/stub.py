"""A deterministic composer with no model call.

Purpose: prove I-2 / I-3 without spending a single inference. The
wrapper's ``StubComposer`` mode is what the sweep uses under
:envvar:`MINDWIRE_DECISION_COMPOSER_BACKEND=stub`, and what tests use
for A-2 (composer intentionally failing) and A-3 (composer runs at most
once per signature).

The stub is **not a fake for production use**. It produces a plain
"someone stopped, look at the chatroom" question with no options and no
recommendation — deliberately unhelpful, because its job is to keep the
end-to-end path alive, not to explain the decision. A real composer
lands in slice S3; until then, and in tests, the stub is what fills the
port shape.

Two modes selectable via the constructor let tests exercise the
failure surface without patching:

- ``FailingStubComposer(kind="timeout")`` raises
  :class:`~.exceptions.DecisionComposerTimeoutError`.
- ``FailingStubComposer(kind="empty")`` raises
  :class:`~.exceptions.DecisionComposerEmptyError`.
- ``FailingStubComposer(kind="error")`` raises a generic
  :class:`~.exceptions.DecisionComposerError`.

Because I-2 says a broken composer must not sink the raw ping, the
wrapper's own tests need to be able to *reproduce* a broken composer.
The failing stub is the safest way to do that: it is opt-in per
construction, so the default :class:`StubComposer` cannot silently
regress into raising.
"""

from __future__ import annotations

from typing import Literal

from .exceptions import (
    DecisionComposerEmptyError,
    DecisionComposerError,
    DecisionComposerTimeoutError,
)
from .value_objects import DecisionRequestInput, DecisionRequestOutput

# The stub's persona label. Kept distinct from any role identity in the
# design roster (Bohr / Heisenberg / Einstein), so a test that plugs the
# stub in cannot accidentally satisfy the I-5 role-identity assertion by
# reusing a role name. See DecisionRequestComposer.identity_name for the
# obligation this satisfies.
DEFAULT_STUB_IDENTITY = "composer-stub"


class StubComposer:
    """A composer that returns a deterministic, minimal question.

    Deterministic in the strict sense: given the same input the output
    is byte-equal. That is what lets a test show "same signature => the
    wrapper reused the cached envelope" without needing to freeze a
    clock or seed a randomiser (I-3, A-3).
    """

    def __init__(self, identity_name: str = DEFAULT_STUB_IDENTITY) -> None:
        self.identity_name = identity_name

    def compose(self, request: DecisionRequestInput) -> DecisionRequestOutput:
        # Deliberately terse. The stub is not trying to write a good
        # question — it is proving the pipe works end-to-end.
        title = request.thread_title.strip() or request.thread_id
        question = (
            f"{title} ({request.project}) が停止しました "
            f"(reason={request.stop_reason}, rounds={request.rounds}, "
            f"last={request.last_msg_id})。chatroom を確認して次を指定してください。"
        )
        return DecisionRequestOutput(
            question=question,
            options=(),
            recommendation=None,
            recommendation_reason=None,
            unknowns=(f"tail: {len(request.tail)} of {request.total_messages} messages provided",),
            tail_used=len(request.tail),
        )


FailKind = Literal["timeout", "empty", "error"]


class FailingStubComposer:
    """A composer that always raises. Test / A-2 backstop.

    The reason a *separate* class exists — instead of a kwarg on
    :class:`StubComposer` — is that the raising path is exactly what the
    default class must never do. Splitting it makes accidental
    regression noisier: a test that flips ``StubComposer(raises=True)``
    would silently start failing in the wrapper if the parameter went
    away, whereas removing :class:`FailingStubComposer` breaks the
    import in a way that stops the sweep at the door.
    """

    def __init__(
        self,
        kind: FailKind = "error",
        identity_name: str = DEFAULT_STUB_IDENTITY,
    ) -> None:
        self._kind = kind
        self.identity_name = identity_name

    def compose(self, request: DecisionRequestInput) -> DecisionRequestOutput:
        if self._kind == "timeout":
            raise DecisionComposerTimeoutError("stub configured to time out")
        if self._kind == "empty":
            raise DecisionComposerEmptyError("stub configured to return empty")
        raise DecisionComposerError("stub configured to error")


__all__ = ["DEFAULT_STUB_IDENTITY", "FailKind", "FailingStubComposer", "StubComposer"]
