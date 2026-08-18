"""Quoted-heredoc masking: one recognised shape, everything else left as shell.

The mask exists because the implementer is told to write commit messages and PR
bodies through a heredoc, those bodies are Markdown, a code span is a backtick,
and a lone backtick opened the coarse floor over the prose. Measured 2026-08-17:
four conductor runs died on messages that merely *mentioned* deleting, one of
them quoting the human's own instruction not to delete a file.

Getting the *parse* right then failed round after round, each Tier B review
finding another way to be wrong about where a heredoc body begins. Every one of
those shapes is kept below as a case. Running them under bash — line 1 replaced
by ``touch <marker>``, sink replaced by ``true`` so nothing short-circuits —
splits them into two kinds, and the difference is worth stating plainly because
it says which of these tests are load-bearing:

**Three where bash really runs the line the mask blanked.** These are fail-OPEN
and they are why the parse was abandoned for a template:

1. a fake opener inside a body, its terminator placed after the real one, so the
   masked span swallowed live commands (PR #154);
2. a backslash line-continuation, which defers the body past the next physical
   newline — the mask blanked the continued command as if it were data;
3. a second, fake terminator at the end: bash stops at the *first* one, but a
   pattern anchored to the end of the string could not, so its non-greedy body
   swallowed the real terminator and the deletion behind it.

**The rest, where bash refuses to parse at all** — a trailing ``&&``, ``$(``
left open, a bare backtick. Measured: ``bash`` exits 2 and runs nothing, so
declining them buys no safety over what bash already does. They are declined
because the parse is ambiguous, not because a deletion would otherwise execute.

Every fix was correct and every one left another construct. So the question
changed from "where does the body begin?" to "**is this the one shape we are
sure about?**" — the command's first line is a data-sink invocation carrying a
single quoted heredoc opener and nothing else, and the body runs to the first
line that is exactly the delimiter. Only that body is blanked; anything before
or after it is left as shell, which is exactly as strict as before the feature
existed.

The final test states the promise as a property rather than a case list.
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


def test_a_quoted_flag_on_the_owner_is_ordinary() -> None:
    # `gh pr create --title "..."` is how the agent actually opens a PR. Quotes
    # are safe in the owner because it is handed to shlex, which raises on an
    # unclosed one — the only way a quote could push the line's end past this
    # newline. (Tier B naysayer, PR #156 round 5.)
    cmd = "gh pr create --title " + DQ + "Fix bugs" + DQ + " --body-file - <<'A'\nsays git rm\nA"
    assert _verdict(cmd) is not Operation.FS_DELETE, cmd


def test_a_command_after_the_terminator_does_not_prevent_masking() -> None:
    # Ordinary batching: commit, then push. The body is still data; the trailing
    # command is still shell and still read by the floor.
    cmd = "git commit -F - <<'A'\nsays git rm\nA\ngit push"
    assert _verdict(cmd) is Operation.GIT_PUSH, cmd


def test_a_substitution_in_the_owner_is_declined() -> None:
    # `$` stays out of the owner charset: bash keeps reading an open
    # substitution across the newline, which would move where the body begins.
    cmd = "gh pr create --title " + DQ + "$(rm -rf /)" + DQ + " --body-file - <<'A'\nprose\nA"
    assert _verdict(cmd) is Operation.FS_DELETE, cmd


@pytest.mark.parametrize(
    ("label", "title"),
    [
        # The round-6 critique's own example, then the punctuation it is made of.
        ("conventional-commit-with-issue-ref", "feat(ui): fix layout, update docs (#12)"),
        ("colon", "feat: x"),
        ("comma", "a, b"),
        ("parens", "wrap (ui) x"),
        ("hash-mid-word", "closes (#12)"),
        ("plus-at-bang", "a+b @c !d"),
        ("brackets-braces", "a[b] {c}"),
        ("percent-caret-tilde", "50% ^ ~x"),
        ("star-question", "a* b?"),
        # This loop's commit messages are frequently Japanese.
        ("japanese", "レイアウトの破れを直す"),
    ],
)
def test_ordinary_punctuation_in_a_title_is_still_the_shape(label: str, title: str) -> None:
    # Measured under bash (probe: line 1 = `touch <marker>`): every one of these
    # leaves line 1 as heredoc data, so none of them can move where the body
    # begins and none of them is a reason to decline. Round 6 reported these as
    # broken and it was right.
    cmd = "gh pr create --title " + DQ + title + DQ + " --body-file - <<'A'\nsays git rm\nA"
    assert _verdict(cmd) is not Operation.FS_DELETE, label


@pytest.mark.parametrize(
    ("label", "cmd"),
    [
        # `#` begins a word, so it comments out the rest of the line — the
        # opener included. There is then no heredoc and `rm` is live shell.
        # Measured: `true # <<'A'` runs line 1.
        ("hash-hides-the-opener", "git commit -F - # <<'A'\n" + DELETION + "\nA"),
        ("hash-after-a-flag", "git commit -F - -q # <<'A'\n" + DELETION + "\nA"),
    ],
)
def test_a_word_initial_hash_is_declined(label: str, cmd: str) -> None:
    # The half of the round-6 critique that was wrong: admitting `#`
    # unconditionally would have masked a live deletion.
    assert _verdict(cmd) is Operation.FS_DELETE, label


# --- every shape the review rounds produced ------------------------------- #
#
# MEASURED LIVE: bash runs the blanked line. Load-bearing.


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
        # MEASURED UNPARSEABLE: bash exits 2 and runs nothing, so these are
        # declined for ambiguity, not because a deletion would execute. Kept as
        # cases so a future relaxation has to face them, but labelled honestly.
        # 4a. A comment hides the trailing && that keeps the list open.
        ("comment-hides-and-and", "git commit -F - <<'A' && # c\nrm -rf /\nsafe\nA"),
        # 4b. `$(` left open inside double quotes.
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


def test_the_body_ends_at_the_first_terminator_and_the_rest_stays_shell() -> None:
    # The rule the last bypass broke, stated on its own. bash ends the body at
    # the FIRST delimiter line; a second one further down is not a terminator
    # and cannot stretch the body over `echo hi`. Asserted on the mask itself,
    # since the verdict here is dominated by `git commit`, which hides the point:
    # only `prose` is blanked, and everything from the first `A` on is untouched.
    cmd = "git commit -F - <<'A'\nprose\nA\necho hi\nA"
    assert _mask_quoted_heredoc_payloads(cmd) == "git commit -F - <<'A'\n     \nA\necho hi\nA"


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
