"""Quoted-heredoc masking: one recognised shape, everything else left as shell.

The mask exists because the implementer is told to write commit messages and PR
bodies through a heredoc, those bodies are Markdown, a code span is a backtick,
and a lone backtick opened the coarse floor over the prose. Measured 2026-08-17:
four conductor runs died on messages that merely *mentioned* deleting, one of
them quoting the human's own instruction not to delete a file.

Getting the *parse* right then failed four times. Each Tier B round found a real
defect in the attempt to work out where a heredoc body begins:

1. a fake opener inside a body, its terminator placed after the real one, so the
   masked span swallowed live commands (PR #154 — an outright bypass);
2. several openers on one line, whose bodies follow in opener order, so a cursor
   that jumped past the first landed behind the second and never masked it;
3. a backslash line-continuation, which defers the body past the next physical
   newline — the mask blanked the continued command as if it were data (bypass);
4. a comment hiding a trailing ``&&``, and ``$(`` inside double quotes — two more
   ways for the logical line to keep going (bypasses).

Every fix was correct and every one left another construct. So the question
changed from "where does the body begin?" to "**is this the one shape we are
sure about?**" — a single data-sink invocation, a single quoted heredoc, and
nothing else in the command. Anything else is not masked, which is exactly as
strict as before the feature existed.

All five exploits are kept below as cases: each fails the template on structure
alone. The final test states the promise as a property rather than a case list.
"""

from __future__ import annotations

import pytest

from spirrow_mindwire.adapters.implementer import (
    _mask_quoted_heredoc_payloads,
    classify_tool_call,
)
from spirrow_mindwire.allowlist import Operation

DELETION = "rm -rf /tmp/x"
BS = chr(92)
DQ = chr(34)
BT = chr(96)


def _verdict(command: str) -> Operation:
    return classify_tool_call("Bash", {"command": command}).operation


# --- the shape the mask exists for ---------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        "Einstein asks me to `git rm` the spec file. Not doing it.",
        "Kept per the human's directive (`Step 0 の .md を今 git rm しないこと`).",
        # A thread name is enough: `-delete\b` matches inside T-fs-delete-path-scope.
        "restorability belongs to a separate gate (T-fs-delete-path-scope).",
        "Example in the docs:\n\n    rm -rf build/\n",
        "Do not `git push --force` here.",
        "`Remove-Item` is the PowerShell spelling.",
        "",  # an empty body is still a body
    ],
)
def test_prose_in_a_lone_sink_heredoc_is_not_a_command(body: str) -> None:
    cmd = "git commit -F - <<'A'\n" + body + "\nA"
    assert _verdict(cmd) is Operation.GIT_COMMIT, cmd


@pytest.mark.parametrize(
    "sink",
    [
        "git commit -F -",
        "git tag -a v1 -F -",
        "gh pr create --body-file -",
        "gh pr edit 1 --body-file -",
        "gh issue comment 1 --body-file -",
    ],
)
def test_each_data_sink_is_recognised(sink: str) -> None:
    cmd = sink + " <<'A'\nmentions `rm -rf` in prose\nA"
    assert _verdict(cmd) is not Operation.FS_DELETE, cmd


def test_dash_form_allows_an_indented_terminator() -> None:
    cmd = "git commit -F - <<-'A'\n\tsays git rm\n\tA"
    assert _verdict(cmd) is Operation.GIT_COMMIT


# --- every exploit the four review rounds produced ------------------------ #


@pytest.mark.parametrize(
    ("label", "cmd"),
    [
        # 1. A fake opener inside the body, terminated after the real one.
        (
            "fake-opener-in-body",
            "git commit -F - <<'ZZ'\n; gh pr create <<'echo'\nZZ\nrm -rf /\necho",
        ),
        # 1b. Its mirror: a sink-shaped opener inside a body that is real script.
        (
            "sink-opener-in-script-body",
            "bash <<'ZZ'\n; git commit -F - <<'X'\nZZ\nrm -rf /tmp/a\nX",
        ),
        # 3. A backslash line-continuation defers the body past the next newline.
        (
            "backslash-continuation",
            "git commit -F - <<'A' ; eval " + BS + "\nrm -rf /\nsafe\nA",
        ),
        # 4a. A comment hides the trailing && that keeps the list open.
        ("comment-hides-and-and", "git commit -F - <<'A' && # c\nrm -rf /\nsafe\nA"),
        # 4b. `$(` inside double quotes — bash keeps reading the substitution.
        (
            "substitution-in-double-quotes",
            "git commit -F - <<'A' ; echo " + DQ + "$(" + DQ + "\n" + DELETION + "\nsafe\nA",
        ),
        ("trailing-and-and", "git commit -F - <<'A' &&\n" + DELETION + "\nsafe\nA"),
        (
            "backtick-spans-the-line",
            "git commit -F - <<'A' ; echo " + BT + "\n" + DELETION + "\n" + BT + "\nsafe\nA",
        ),
        # 5. A second, fake terminator at the very end. bash stops at the FIRST
        #    one and runs what follows; a pattern anchored to the end of the
        #    string could not stop there, so its non-greedy body swallowed the
        #    real terminator and the deletion behind it.
        (
            "second-terminator-at-the-end",
            "git commit -F - <<'A'\nharmless prose\nA\n" + DELETION + "\nA",
        ),
    ],
)
def test_every_exploit_stays_visible(label: str, cmd: str) -> None:
    assert _verdict(cmd) is Operation.FS_DELETE, label


def test_a_command_after_the_first_terminator_means_the_shape_is_not_recognised() -> None:
    # The rule the last bypass broke: bash ends the body at the FIRST delimiter
    # line, so what follows is a command — and a command being there is exactly
    # what disqualifies the shape. Asserted on the mask itself because the
    # verdict here is dominated by `git commit`, which would hide the point.
    cmd = "git commit -F - <<'A'\nprose\nA\necho hi\nA"
    assert _mask_quoted_heredoc_payloads(cmd) == cmd


# --- deletions that are plainly shell ------------------------------------- #


@pytest.mark.parametrize(
    ("label", "cmd"),
    [
        ("after-the-body", "git commit -F - <<'A'\nprose\nA\n" + DELETION),
        ("before-the-sink", DELETION + "\ngit commit -F - <<'A'\nprose\nA"),
        ("bare", DELETION),
        ("interpreter-heredoc", "bash <<'A'\n" + DELETION + "\nA"),
        # An unquoted delimiter is expanded by the shell, so it is not inert.
        ("unquoted-delimiter", "git commit -F - <<A\n" + DELETION + "\nA"),
        ("owner-is-not-a-sink", "tee out <<'A'\n" + DELETION + "\nA"),
        ("no-terminator", "git commit -F - <<'A'\n" + DELETION),
    ],
)
def test_shell_deletions_are_still_denied(label: str, cmd: str) -> None:
    assert _verdict(cmd) is Operation.FS_DELETE, label


# --- the promise, as a property ------------------------------------------- #


@pytest.mark.parametrize("owner", ["git commit -F -", "tee out"], ids=["sink", "nonsink"])
@pytest.mark.parametrize("where", ["body", "before", "after"])
@pytest.mark.parametrize(
    ("extra_on_line", "extra_tail"),
    [
        ("", ""),
        (" ; echo hi", ""),
        (" && echo hi", ""),
        (" # note", ""),
        (" ; gh pr create --body-file - <<'B'", "\nsecond body\nB"),
    ],
    ids=["plain", "semicolon", "andand", "comment", "second-heredoc"],
)
def test_a_deletion_is_hidden_only_by_the_recognised_shape(
    owner: str, where: str, extra_on_line: str, extra_tail: str
) -> None:
    """Hidden if and only if all three hold: the owner is a data sink, the
    command carries no structure beyond its single heredoc, and the deletion sits
    in the body.

    Each of the five defects is one point in this grid. Three of them are an
    ``extra_on_line`` that an earlier parser tried to interpret instead of
    declining, and interpreting it wrongly is what hid a live command.
    """
    body = DELETION if where == "body" else "prose"
    cmd = owner + extra_on_line + " <<'A'\n" + body + "\nA" + extra_tail
    if where == "before":
        cmd = DELETION + "\n" + cmd
    elif where == "after":
        cmd = cmd + "\n" + DELETION

    hidden = owner == "git commit -F -" and extra_on_line == "" and where == "body"
    verdict = _verdict(cmd)
    if hidden:
        assert verdict is not Operation.FS_DELETE, cmd
    else:
        assert verdict is Operation.FS_DELETE, cmd
