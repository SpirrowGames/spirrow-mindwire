"""Quoted-heredoc masking: the parse must follow bash, not a convenient subset.

The mask exists because the implementer is told to write commit messages and PR
bodies through heredocs, those bodies are Markdown, and the classifier was
reading their prose as shell (PR #154). Getting the *parse* right then took two
rounds of independent review, and each round found a real defect:

* **#154** scanned with ``finditer`` over the whole command, so an opener-shaped
  string *inside* a body was treated as a real opener. With its terminator placed
  after the real one, the masked span swallowed live commands — a full bypass of
  the classifier.
* **#156** fixed that with a cursor but assumed one opener per line. bash allows
  several on a line, with the bodies following in opener order, so the cursor
  jumped past body A and never saw the ``<<'B'`` opener behind it.

Both are pinned below as cases. The last test states the invariant they each
broke *once*, rather than case by case: a Tier C verb in a shell position is
always seen, and one that sits only inside a masked sink body never is.
"""

from __future__ import annotations

import pytest

from spirrow_mindwire.adapters.implementer import classify_tool_call
from spirrow_mindwire.allowlist import Operation

DELETION = "rm -rf /tmp/x"


def _verdict(command: str) -> Operation:
    return classify_tool_call("Bash", {"command": command}).operation


# --- the two exploits, kept verbatim ------------------------------------- #


def test_fake_opener_inside_a_body_cannot_blank_the_commands_after_it() -> None:
    # PR #154, from the Tier B naysayer. bash ends the heredoc at the delimiter
    # and then runs the deletion; the old scan read the literal `<<'echo'` on
    # line 2 as an opener, found a terminator line `echo` at the very end, and
    # blanked everything between — the deletion included.
    cmd = "git commit -F - <<'ZZ'\n; gh pr create <<'echo'\nZZ\nrm -rf /\necho"
    assert _verdict(cmd) is Operation.FS_DELETE


def test_fake_sink_opener_inside_a_non_sink_body_is_not_an_opener() -> None:
    # The mirror image: the outer body is script (bash is no data sink), so a
    # sink-shaped opener inside it must not be honoured either.
    cmd = "bash <<'ZZ'\n; git commit -F - <<'X'\nZZ\nrm -rf /tmp/a\nX"
    assert _verdict(cmd) is Operation.FS_DELETE


def test_two_openers_on_one_line_both_get_masked() -> None:
    # PR #156. Both bodies are prose for data sinks, so neither is shell.
    cmd = (
        "git commit -F - <<'A' ; gh pr create --body-file - <<'B'\n"
        "commit body says rm -rf\nA\npr body says git rm\nB"
    )
    assert _verdict(cmd) is Operation.GIT_COMMIT


def test_second_opener_on_the_line_is_judged_by_its_own_owner() -> None:
    # `tee` is not a data sink, so B's body stays shell even though A's is masked.
    cmd = "git commit -F - <<'A' ; tee out <<'B'\nprose\nA\nrm -rf /tmp/x\nB"
    assert _verdict(cmd) is Operation.FS_DELETE


# --- the parse's own edges ------------------------------------------------ #


def test_unterminated_body_stops_the_scan_rather_than_guessing() -> None:
    # Without a terminator we cannot say where the body ends, so we cannot say
    # where the shell resumes either. Everything from there stays unmasked.
    cmd = "git commit -F - <<'A'\nprose\nA\ngit commit -F - <<'B'\nrm -rf /tmp/x"
    assert _verdict(cmd) is Operation.FS_DELETE


def test_a_body_cannot_supply_the_next_openers_owner_name() -> None:
    # The owner is the text between the previous terminator and this opener. Were
    # it the whole prefix, the `git commit -F -` written inside A's body would
    # name a sink and license masking B, which `tee` does not own.
    cmd = "bash <<'A'\ngit commit -F -\nA\ntee out <<'B'\nrm -rf /tmp/x\nB"
    assert _verdict(cmd) is Operation.FS_DELETE


def test_dash_form_allows_an_indented_terminator() -> None:
    cmd = "git commit -F - <<-'A'\n\tsays git rm\n\tA"
    assert _verdict(cmd) is Operation.GIT_COMMIT


def test_unquoted_delimiter_is_never_masked() -> None:
    # The shell expands an unquoted heredoc body, so it is not inert.
    cmd = "git commit -F - <<A\nrm -rf /tmp/x\nA"
    assert _verdict(cmd) is Operation.FS_DELETE


# --- the invariant both defects broke ------------------------------------- #


@pytest.mark.parametrize("same_line", [True, False], ids=["one-line", "two-lines"])
@pytest.mark.parametrize("second_is_sink", [True, False], ids=["sink2", "nonsink2"])
@pytest.mark.parametrize("where", ["shell-before", "shell-after", "body-1", "body-2"])
def test_a_deletion_disappears_only_from_inside_a_masked_body(
    same_line: bool, second_is_sink: bool, where: str
) -> None:
    """Build a two-heredoc command whose parts are known by construction, drop a
    deletion into one of them, and assert the only way it stops being visible is
    by sitting inside a body that a data sink owns.

    This is the property, not a case list: #154 hid a shell-position deletion and
    #156 failed to hide a sink-body one, and both are single points in this grid.
    """
    second = "gh pr create --body-file -" if second_is_sink else "tee out"
    body1 = DELETION if where == "body-1" else "prose one"
    body2 = DELETION if where == "body-2" else "prose two"
    opener1, opener2 = "git commit -F - <<'A'", second + " <<'B'"
    if same_line:
        cmd = opener1 + " ; " + opener2 + "\n" + body1 + "\nA\n" + body2 + "\nB"
    else:
        cmd = opener1 + "\n" + body1 + "\nA\n" + opener2 + "\n" + body2 + "\nB"
    if where == "shell-before":
        cmd = DELETION + "\n" + cmd
    elif where == "shell-after":
        cmd = cmd + "\n" + DELETION

    hidden = where == "body-1" or (where == "body-2" and second_is_sink)
    verdict = _verdict(cmd)
    if hidden:
        assert verdict is not Operation.FS_DELETE, cmd
    else:
        assert verdict is Operation.FS_DELETE, cmd
