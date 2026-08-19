"""The `gh` half of the raw-coarse floor: catch publishes, not the word "release".

Tier B on PR #163 found that `\\bgh\\b.*\\brelease\\b` matched the word wherever it
appeared, so `bash -c "gh pr create --title 'Fix release pipeline'"` classified as
`external.publish` — a Tier C denial, which is fail-loud, which kills the session.
Reproduced before the fix; these pin both halves so it cannot come back.

The floor only has to be the *indirection* backstop: the structural pass already
reads `gh release create` correctly when the command tokenizes. That is why the
gap between `gh` and its subcommand can be narrowed to "does not cross a quote"
without losing coverage — a quoted argument is text, not a subcommand.
"""

from __future__ import annotations

import pytest

from spirrow_mindwire.adapters.implementer import classify_tool_call
from spirrow_mindwire.allowlist import Operation

# Shapes that MUST reach Tier C. The indirection forms are the reason the floor
# exists at all: `bash -c "..."` hides the command from ordinary tokenization.
PUBLISHES = [
    "gh release create v1",
    'bash -c "gh release create v1"',
    "sh -c 'gh release delete v1'",
    "echo $(gh release create v1)",
    "gh repo delete foo",
    'bash -c "gh repo archive foo"',
    # Global flags before the subcommand: the structural pass reads `rest[0]` as
    # the group and so misses these; the floor is what covers them.
    'bash -c "gh -R owner/repo release create v1"',
    'bash -c "gh --repo owner/repo repo delete foo"',
    # Tier B round 2: a quoted argument BEFORE the subcommand. Narrowing the
    # gap to non-quote characters let these escape the floor completely — the
    # fix for the false positive had created a false negative.
    "bash -c \"gh --repo 'SpirrowGames/core' release create v1\"",
    "bash -c \"gh --repo 'SpirrowGames/core' repo delete foo\"",
    "eval \"gh --repo 'SpirrowGames/core' release create v1\"",
]

# Shapes that must NOT be read as a publish. Every one of these carries the
# trigger word inside a quoted argument, which is exactly what `.*` could not
# tell apart from a subcommand.
NOT_PUBLISHES = [
    "gh pr create --title 'Fix release pipeline'",
    "bash -c \"gh pr create --title 'Fix release pipeline'\"",
    "bash -c \"gh issue close 123 -c 'repo archive strategy'\"",
    'gh pr comment 1 --body "we should archive this repo"',
    'gh pr edit 5 --body "delete the repo later"',
    'git commit -m "prepare release"',
]


@pytest.mark.parametrize("command", PUBLISHES)
def test_publish_shapes_reach_tier_c(command: str) -> None:
    assert classify_tool_call("Bash", {"command": command}).operation is Operation.EXTERNAL_PUBLISH


@pytest.mark.parametrize("command", NOT_PUBLISHES)
def test_the_word_in_an_argument_is_not_a_publish(command: str) -> None:
    """A quoted argument is data. Denying on it kills the session for a PR title."""
    operation = classify_tool_call("Bash", {"command": command}).operation
    assert operation is not Operation.EXTERNAL_PUBLISH
    # UNKNOWN is default-deny, so it is the same halt by another name.
    assert operation is not Operation.UNKNOWN


def test_gh_pr_checkout_is_a_read() -> None:
    """`gh pr checkout` only moves local git state, like `git fetch` (EXEC_CODE).

    It was missing from the `gh pr` whitelist, so the new default-deny sent it to
    UNKNOWN — narrower than `main`, where `gh` had no whitelist at all. Tier B, #163.
    """
    assert (
        classify_tool_call("Bash", {"command": "gh pr checkout 5"}).operation
        is not Operation.UNKNOWN
    )
