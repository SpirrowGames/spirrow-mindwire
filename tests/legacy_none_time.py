"""The posting time the tests of the legacy ``NEXT: none`` reading read it at.

Boundary 5-B (T-done-disposition-auto-close, Bohr msg-9235 §3-4) set
``handoff.NONE_MALFORMED_AFTER``. From then on a ``NEXT: none`` whose posting time is unknown is
refused, so a test that pins how a legacy head WAS read has to say when that head was posted, the
way every production reader does (``posted_at_of(message)``).

:data:`PRE_CUTOFF` is that time: the instant of the last ``NEXT: none`` any writer posted before
the switch (spirrow-voxelworld msg-6652, measured in msg-9235 §1). It is a fixed instant, not
"the cutoff minus something", so a test that passes it keeps reading history even if the cutoff
is moved forward before the merge. ``test_none_malformed_after.py`` pins that it is before the
shipped cutoff.

:data:`PRE_CUTOFF_STAMP` is the same instant as the ``timestamp`` string a chatroom message
carries, for the fakes that build messages.
"""

from __future__ import annotations

from datetime import UTC, datetime

PRE_CUTOFF = datetime(2026, 11, 11, 21, 32, 33, tzinfo=UTC)
PRE_CUTOFF_STAMP = "2026-11-11T21:32:33Z"
