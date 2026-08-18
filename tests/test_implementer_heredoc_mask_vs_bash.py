"""The heredoc mask, checked against real bash rather than against an argument.

Six review rounds argued about where a heredoc body begins, and the arguments
were wrong in both directions: four rounds masked a line bash would run, and two
refused to mask a line bash treats as data. Reasoning about it kept failing, so
this module stops reasoning and measures.

Each generated script is built from line templates. Every ``PROBE`` line becomes
``touch <marker-for-that-line>``, so one bash run says exactly which lines
executed. The same script is put through :func:`_mask_quoted_heredoc_payloads`.
The forbidden square, per line, is:

    bash executed it  AND  the mask blanked it

— the fail-OPEN direction, where a live command is hidden from the coarse floor.
The opposite square (bash treated it as data, the mask left it alone) is a false
positive: the conductor dies on prose. It is reported but not asserted, because
the mask is deliberately conservative about shapes it cannot account for.

The sink is spelled ``true`` on the bash side and ``git commit -F -`` on the
mask side. Both are plain words, so the structure is identical — but ``git
commit`` FAILS in a temp dir, and a failing left operand short-circuits ``&&``,
which makes a live line look like data. That confound produced a wrong reading
once already; using ``true`` removes it.

bash is a hard dependency of this repo's gate (``.mindwire-gate`` is a bash
script, run by CI and by the loop's implementer alike), so there is nothing to
skip on.
"""

from __future__ import annotations

import itertools
import pathlib
import subprocess

import pytest

from spirrow_mindwire.adapters.implementer import _mask_quoted_heredoc_payloads

DQ = chr(34)
BS = chr(92)
PROBE = "\x00PROBE\x00"

PREFIXES = [
    [],
    ["git add ."],
    ["SINK <<'ZZ'"],  # an opener we do not recognise: the scan must stop here
    ["git add . &&"],
    ["echo 'x"],
    # Round 11: a comment cannot reach the next line, so these must be stepped
    # over, not stopped on. Both spellings, plus the ones that look dangerous.
    ["git add . # note"],
    ["# create the PR"],
    ["# don't delete"],
    ["git add --title a#b'c"],  # mid-word `#`: the quote after it is real
    ["git add . " + BS],
    ["("],  # opens a subshell, so `)` becomes reachable in the opener
    ["{"],
    # Round 12: lines that rebind the sink's name. The function definition is
    # the one that demonstrates itself — bash calls the function, which execs
    # `bash`, which inherits stdin and runs the heredoc body.
    ["CMD() {", "bash", "}"],
    ["unfamiliar-tool --go"],
    [PROBE],
]

OPENERS = [
    "SINK <<'A'",
    "SINK --title " + DQ + "feat(ui): x (#12)" + DQ + " <<'A'",
    "SINK <<'A' &&",
    "SINK <<'A' ; eval " + BS,
    "SINK # <<'A'",
    # Round 8, measured: bash closes the subshell at `)`, reads `#<<'A'` as a
    # comment, and runs the next line. A `#` check that only knew about spaces
    # masked it. `)` is a metacharacter, so it begins a word just as space does.
    "SINK )#<<'A'",
    "SINK ) <<'A'",
    "SINK " + DQ + "x" + DQ + "#<<'A'",
    # Round 9: `(` is a metacharacter too, but unreachable in argument position,
    # so treating it as a comment opener only cost the round-6 fix. Both spellings
    # are here so the claim is measured rather than argued.
    "SINK (#<<'A'",
    "SINK --title " + DQ + "docs (#12)" + DQ + " <<'A'",
    "tee out <<'A'",
    "SINK <<A",  # unquoted delimiter: the body is expanded, so it is not inert
]

SUFFIXES = [
    [],
    [PROBE],
    ["A", PROBE],  # a second, fake terminator
    [PROBE, "SINK <<'B'", PROBE, "B", PROBE],  # a second heredoc in the batch
]


def _render(
    template: list[str],
    sink: str,
    markers: list[pathlib.Path],
    separator: str = "\n",
) -> str:
    rendered = []
    for index, line in enumerate(template):
        if PROBE in line:
            # `PROBE` may carry a prefix, e.g. the tab a `<<-` body is indented
            # with, so it is substituted rather than compared.
            rendered.append(line.replace(PROBE, "touch '" + markers[index].as_posix() + "'"))
        else:
            # SINK is the whole invocation; CMD is only its command word, which
            # is what a function definition has to shadow.
            rendered.append(line.replace("SINK", sink).replace("CMD", sink.split()[0]))
    return separator.join(rendered)


def _executed_and_blanked(
    template: list[str],
    tmp_path: pathlib.Path,
    separator: str = "\n",
) -> list[tuple[int, bool, bool]]:
    """Run the script under bash, mask it, and report both per PROBE line."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    markers = [tmp_path / f"m{i}" for i in range(len(template))]

    bash_text = _render(template, "true", markers, separator)
    script = tmp_path / "s.sh"
    script.write_bytes((bash_text + separator).encode("utf-8"))
    subprocess.run(
        ["bash", script.as_posix()],
        capture_output=True,
        text=True,
        cwd=tmp_path.as_posix(),
        check=False,
    )

    mask_text = _render(template, "git commit -F -", markers, separator)
    original = mask_text.split("\n")
    masked = _mask_quoted_heredoc_payloads(mask_text).split("\n")

    return [
        (
            index,
            markers[index].exists(),
            masked[index] != original[index] and masked[index].strip() == "",
        )
        for index, line in enumerate(template)
        if PROBE in line
    ]


@pytest.mark.parametrize(
    ("prefix", "opener", "suffix"),
    list(itertools.product(PREFIXES, OPENERS, SUFFIXES)),
    ids=lambda value: "|".join(value).replace(PROBE, "P")[:40] if isinstance(value, list) else None,
)
def test_the_mask_never_blanks_a_line_bash_would_run(
    prefix: list[str],
    opener: str,
    suffix: list[str],
    tmp_path: pathlib.Path,
) -> None:
    template = [*prefix, opener, PROBE, "A", *suffix]
    for index, executed, blanked in _executed_and_blanked(template, tmp_path):
        assert not (executed and blanked), (
            f"line {index} of {template!r} is live shell under bash, "
            f"but the mask blanked it — the coarse floor would never see it"
        )


# --- CRLF ------------------------------------------------------------------ #
#
# Round 10's one note. Splitting on `\n` leaves a `\r` on every line, which no
# pattern here admits, so the scan stopped at line 0 and masked nothing. It fails
# closed — but "closed" means the prose reaches the coarse floor, which is the
# conductor death this function exists to prevent, arriving silently and only for
# Windows-authored payloads. Measured on git-bash: a CRLF script parses and the
# body is data exactly as with LF, so the masking was simply missing.


@pytest.mark.parametrize(
    "template",
    [
        ["SINK <<'A'", PROBE, "A"],
        ["git add .", "SINK <<'A'", PROBE, "A", "git push"],
        ["SINK --title " + DQ + "feat(ui): x (#12)" + DQ + " <<'A'", PROBE, "A"],
        ["SINK <<'A'", PROBE, "A", PROBE],
        ["SINK <<-'A'", "\t" + PROBE, "\tA"],
        # And the shapes that must stay refused with CRLF too.
        ["SINK <<'ZZ'", "SINK <<'A'", "ZZ", PROBE, "A"],
        ["(", "SINK )#<<'A'", PROBE, "A"],
        ["SINK <<'A' ; eval " + BS, PROBE, "safe", "A"],
    ],
    ids=[
        "lone-heredoc",
        "batch",
        "issue-ref-title",
        "trailing-command",
        "dash-form",
        "unrecognised-outer-heredoc",
        "paren-comment",
        "continuation",
    ],
)
@pytest.mark.parametrize("separator", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_crlf_is_read_the_same_way_as_lf(
    template: list[str], separator: str, tmp_path: pathlib.Path
) -> None:
    for index, executed, blanked in _executed_and_blanked(template, tmp_path, separator):
        assert not (executed and blanked), (
            f"line {index} of {template!r} with {separator!r} is live shell "
            f"under bash, but the mask blanked it"
        )


@pytest.mark.parametrize(
    ("label", "template"),
    [
        ("lone-heredoc", ["SINK <<'A'", PROBE, "A"]),
        ("batch", ["git add .", "SINK <<'A'", PROBE, "A", "git push"]),
        ("dash-form", ["SINK <<-'A'", "\t" + PROBE, "\tA"]),
    ],
)
def test_a_crlf_body_is_masked_just_as_an_lf_one_is(
    label: str, template: list[str], tmp_path: pathlib.Path
) -> None:
    # The other half: not merely "never fails open" but "still does its job".
    # Without the `\r` handling every one of these masked nothing at all.
    lf = _executed_and_blanked(template, tmp_path / "lf", "\n")
    crlf = _executed_and_blanked(template, tmp_path / "crlf", "\r\n")
    assert [blanked for _, _, blanked in crlf] == [blanked for _, _, blanked in lf], label
    assert any(blanked for _, _, blanked in crlf), f"{label}: nothing was masked"
