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
    ["git add . # note"],
    ["git add . " + BS],
    [PROBE],
]

OPENERS = [
    "SINK <<'A'",
    "SINK --title " + DQ + "feat(ui): x (#12)" + DQ + " <<'A'",
    "SINK <<'A' &&",
    "SINK <<'A' ; eval " + BS,
    "SINK # <<'A'",
    "tee out <<'A'",
    "SINK <<A",  # unquoted delimiter: the body is expanded, so it is not inert
]

SUFFIXES = [
    [],
    [PROBE],
    ["A", PROBE],  # a second, fake terminator
    [PROBE, "SINK <<'B'", PROBE, "B", PROBE],  # a second heredoc in the batch
]


def _render(template: list[str], sink: str, markers: list[pathlib.Path]) -> str:
    rendered = []
    for index, line in enumerate(template):
        if line == PROBE:
            rendered.append("touch '" + markers[index].as_posix() + "'")
        else:
            rendered.append(line.replace("SINK", sink))
    return "\n".join(rendered)


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
    markers = [tmp_path / f"m{i}" for i in range(len(template))]

    bash_text = _render(template, "true", markers)
    script = tmp_path / "s.sh"
    script.write_text(bash_text + "\n", encoding="utf-8", newline="\n")
    subprocess.run(
        ["bash", script.as_posix()],
        capture_output=True,
        text=True,
        cwd=tmp_path.as_posix(),
        check=False,
    )

    mask_text = _render(template, "git commit -F -", markers)
    original = mask_text.split("\n")
    masked = _mask_quoted_heredoc_payloads(mask_text).split("\n")

    for index, line in enumerate(template):
        if line != PROBE:
            continue
        executed = markers[index].exists()
        blanked = masked[index] != original[index] and masked[index].strip() == ""
        assert not (executed and blanked), (
            f"line {index} of {template!r} is live shell under bash, "
            f"but the mask blanked it — the coarse floor would never see it"
        )
