"""``is_human_identity`` — the one "who is the human" rule (DECIDED 2d-14, msg-5582)."""

from __future__ import annotations

import pytest

from spirrow_mindwire.conductor.human_identity import is_human_identity


@pytest.mark.parametrize(
    ("author", "identity", "expected"),
    [
        ("human", "human", True),
        ("Takahito", "takahito", True),
        ("TAKAHITO", "Takahito", True),
        ("operator", "Takahito", False),
        ("human", "Takahito", False),
        ("Takahito", "", False),
        ("", "", False),
        # Not ADR-11 normalised (DECIDED on the msg-5583 advisory): separators and surrounding
        # whitespace are not folded, so carve-out ① is not widened.
        ("taka-hito", "taka hito", False),
        ("Takahito ", "Takahito", False),
    ],
)
def test_is_human_identity(author: str, identity: str, expected: bool) -> None:
    assert is_human_identity(author, identity) is expected
