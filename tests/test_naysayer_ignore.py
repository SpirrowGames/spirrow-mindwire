"""``.naysayer-ignore`` (T-diff-cap-has-no-escape-for-indivisible-data-artifacts, msg-8470 §8).

One test (or group) per bullet of the final design's test list, plus the parser/splitter pins
the bullets rest on. Gate-side tests drive :class:`NaysayerPrReviewDriver` with a fake that adds
the three ignore-file reads to the driver suite's ``_FakeGitHub``.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from test_pr_review_driver import _capture, _FakeGitHub, _FakeLexora, _pr

from spirrow_mindwire.github.client import GitHubHTTPError, PrRef
from spirrow_mindwire.naysayer import naysayer_ignore as ni
from spirrow_mindwire.naysayer import pr_diff_size
from spirrow_mindwire.naysayer.pr_review import (
    _MAX_DIFF_CHARS,
    NaysayerPrReviewDriver,
    _make_diff_view,
)

_HEAD = "sha-default"


def _section(
    path: str, body: str = "+x\n", *, old: str | None = None, deleted: bool = False
) -> str:
    a = old if old is not None else path
    if deleted:
        return (
            f"diff --git a/{a} b/{a}\ndeleted file mode 100644\nindex 1111111..0000000\n"
            f"--- a/{a}\n+++ /dev/null\n@@ -1 +0,0 @@\n{body}"
        )
    if old is not None:
        return (
            f"diff --git a/{old} b/{path}\nsimilarity index 90%\nrename from {old}\n"
            f"rename to {path}\n--- a/{old}\n+++ b/{path}\n@@ -1 +1 @@\n{body}"
        )
    return (
        f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n"
        f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}"
    )


class _IgnoreGitHub(_FakeGitHub):
    """``_FakeGitHub`` plus the ignore-file / compare-files / blob reads."""

    def __init__(
        self,
        *,
        head_ignore: str | None = None,
        base_ignore: str | None = None,
        base_ignore_exc: Exception | None = None,
        blobs: dict[str, str] | None = None,
        blob_bytes: dict[str, bytes] | None = None,
        blob_exc: Exception | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._ignore = {_HEAD: head_ignore, self._base_ref: base_ignore}
        self._base_ignore_exc = base_ignore_exc
        self._blobs = dict(blobs or {})
        self._blob_bytes = dict(blob_bytes or {})
        self._blob_exc = blob_exc
        self.ignore_reads: list[str] = []

    async def fetch_file_bytes_at(
        self, owner: str, repo: str, *, path: str, ref: str
    ) -> bytes | None:
        assert path == ni.IGNORE_FILE
        self.ignore_reads.append(ref)
        if ref == self._base_ref and self._base_ignore_exc is not None:
            raise self._base_ignore_exc
        text = self._ignore.get(ref)
        return None if text is None else text.encode("utf-8")

    async def fetch_compare_blob_shas(
        self, owner: str, repo: str, base_ref: str, head_sha: str
    ) -> dict[str, str]:
        return dict(self._blobs)

    async def fetch_blob_bytes(self, owner: str, repo: str, sha: str) -> bytes:
        if self._blob_exc is not None:
            raise self._blob_exc
        return self._blob_bytes[sha]


async def _review(github: _IgnoreGitHub) -> tuple[str, str]:
    """Run one gate review; return ``(posted body, pass-1 user prompt)``."""
    lexora = _FakeLexora(content="fine\n\nVERDICT: APPROVE")
    posted, post = _capture()
    await NaysayerPrReviewDriver(lexora=lexora, github=github).review(_pr(), post_critique=post)
    user = next(m.content for m in lexora.calls[0][1] if m.role == "user")
    return posted[0], user


# ---- parser / glob ------------------------------------------------------------------------------


def test_glob_star_stays_in_one_segment_and_double_star_crosses() -> None:
    rules = ni.parse_ignore_file("data/*.jsonl  # unchecked\nev/**/*.ndjson  # sha pin\n")
    assert rules.match("data/a.jsonl") is not None
    assert rules.match("data/sub/a.jsonl") is None
    assert rules.match("ev/a.ndjson") is not None
    assert rules.match("ev/x/y/a.ndjson") is not None


def test_line_without_guarantor_is_invalid_and_named_in_the_body() -> None:
    rules = ni.parse_ignore_file(
        "a/*.csv\nb/*.csv  #   \n!c/*.csv  # x\n# comment\n\nd/*.csv # ok\n"
    )
    assert [(i.line_no, i.reason.split(" ")[0]) for i in rules.invalid] == [
        (1, "no"),
        (2, "empty"),
        (3, "negation"),
    ]
    assert [r.line_no for r in rules.rules] == [6]
    diff = _section("a/x.csv")
    view = _make_diff_view(diff, rules=rules)
    assert view.excluded == ()
    rendered = view.ignore_report.render()
    assert "L1 `a/*.csv`" in rendered and "L3 `!c/*.csv  # x`" in rendered


@pytest.mark.anyio
async def test_invalid_line_reaches_the_posted_body() -> None:
    github = _IgnoreGitHub(head_ignore="data/*.csv\n", diff=_section("data/a.csv"))
    body, _ = await _review(github)
    assert "Invalid `.naysayer-ignore` lines (not applied)" in body
    assert "- L1 `data/*.csv`: no guarantor" in body


# ---- never excluded -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [".naysayer-ignore", ".gitattributes", ".github/workflows/ci.json", "Tools/x.py", "a.ps1"],
)
def test_never_ignorable_files_are_sent_in_full_and_named(path: str) -> None:
    rules = ni.parse_ignore_file("**  # everything, on purpose\n")
    diff = _section(path)
    view = _make_diff_view(diff, rules=rules)
    assert view.text == diff
    assert view.excluded == ()
    assert [r.path for r in view.ignore_report.refused] == [path]
    assert "Matched a rule but sent in full" in view.ignore_report.render()


def test_rename_with_only_one_side_matching_is_not_excluded() -> None:
    rules = ni.parse_ignore_file("ev/*.ndjson  # sha pin\n")
    diff = _section("ev/a.ndjson", old="src/a.txt")
    view = _make_diff_view(diff, rules=rules)
    assert view.excluded == () and view.text == diff
    both = _make_diff_view(_section("ev/b.ndjson", old="ev/a.ndjson"), rules=rules)
    assert [f.path for f in both.excluded] == ["ev/b.ndjson"]


def test_mp4_rule_excludes() -> None:
    rules = ni.parse_ignore_file("media/*.mp4  # unchecked: record only\n")
    keep = _section("src/app.py")
    diff = keep + _section("media/clip.mp4", "+" + "z" * 5000 + "\n")
    view = _make_diff_view(diff, rules=rules)
    assert [f.path for f in view.excluded] == ["media/clip.mp4"]
    assert view.text.startswith(keep)
    assert "[excluded by .naysayer-ignore:L1] media/clip.mp4" in view.text
    assert "z" * 100 not in view.text
    assert view.pre_exclusion_chars == len(diff) and view.original_chars == len(view.text)


def test_split_round_trips_the_diff() -> None:
    diff = "preamble\n" + _section("a.csv") + _section("b.py") + _section("c.csv", deleted=True)
    sections = ni.split_diff_by_file(diff)
    assert "".join(s.text for s in sections) == diff
    assert [s.head_path for s in sections] == ["", "a.csv", "b.py", "c.csv"]
    assert sections[3].deleted


# ---- head rules, NEW RULE -----------------------------------------------------------------------


@pytest.mark.anyio
async def test_rule_added_on_head_applies_and_is_named_new() -> None:
    data = _section("ev/a.ndjson", "+" + "d" * 4000 + "\n")
    github = _IgnoreGitHub(
        head_ignore="ev/*.ndjson  # sha256+len pinned by tests/test_x.py\n",
        base_ignore=None,  # 404 on base: the rule is this PR's
        diff=_section("src/a.py") + data,
        blobs={"ev/a.ndjson": "a" * 40},
        blob_bytes={"a" * 40: b"payload"},
    )
    body, prompt = await _review(github)
    assert github.ignore_reads == [_HEAD, "main"]
    line = "NEW RULE (this PR): L1 ev/*.ndjson → 1 files excluded"
    assert line in body and line in prompt
    assert "d" * 100 not in prompt


@pytest.mark.anyio
async def test_rule_already_on_base_is_not_new_and_unread_base_makes_all_new() -> None:
    ignore = "ev/*.ndjson  # sha pin\n"
    diff = _section("ev/a.ndjson")
    same = _IgnoreGitHub(head_ignore=ignore, base_ignore=ignore, diff=diff)
    body, _ = await _review(same)
    assert "NEW RULE" not in body and "Excluded from this review" in body
    unread = _IgnoreGitHub(
        head_ignore=ignore, base_ignore_exc=GitHubHTTPError("boom", status_code=502), diff=diff
    )
    body, _ = await _review(unread)
    assert "NEW RULE (this PR): L1 ev/*.ndjson → 1 files excluded" in body


@pytest.mark.anyio
async def test_no_ignore_file_leaves_the_review_unchanged() -> None:
    github = _IgnoreGitHub(diff=_section("ev/a.ndjson"))
    body, prompt = await _review(github)
    assert ".naysayer-ignore" not in body and ".naysayer-ignore" not in prompt
    assert github.ignore_reads == [_HEAD]


# ---- sha256 / blob ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sha256_is_the_head_blobs() -> None:
    payload = b'{"tick": 1}\n{"tick": 2}\n'
    blob = "b" * 40
    github = _IgnoreGitHub(
        head_ignore="ev/*.ndjson  # sha pin\n",
        diff=_section("ev/a.ndjson"),
        blobs={"ev/a.ndjson": blob},
        blob_bytes={blob: payload},
    )
    body, prompt = await _review(github)
    expected = (
        f"[excluded by .naysayer-ignore:L1] ev/a.ndjson  bytes={len(payload)}  "
        f"sha256={hashlib.sha256(payload).hexdigest()}  blob={blob}  "
    )
    assert expected in body and expected in prompt
    assert "HASH UNAVAILABLE" not in body


@pytest.mark.anyio
async def test_blob_failure_still_excludes_and_says_fetch_failed() -> None:
    blob = "c" * 40
    data = _section("ev/a.ndjson", "+" + "q" * 3000 + "\n")
    github = _IgnoreGitHub(
        head_ignore="ev/*.ndjson  # sha pin\n",
        diff=data,
        blobs={"ev/a.ndjson": blob},
        blob_exc=GitHubHTTPError("timeout"),
    )
    body, prompt = await _review(github)
    assert "q" * 100 not in prompt
    assert f"bytes=unknown  sha256=fetch_failed  blob={blob}" in body
    assert "HASH UNAVAILABLE: ev/a.ndjson" in body


def test_deleted_file_line() -> None:
    rules = ni.parse_ignore_file("ev/*.ndjson  # sha pin\n")
    view = _make_diff_view(_section("ev/a.ndjson", deleted=True), rules=rules)
    assert "bytes=0  sha256=deleted" in view.excluded[0].summary_line()


# ---- gate and pr-diff-size agree ----------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("blob_fails", [False, True])
async def test_gate_and_pr_diff_size_measure_the_same_original_chars(blob_fails: bool) -> None:
    blob = "d" * 40
    diff = _section("src/a.py") + _section("ev/a.ndjson", "+" + "w" * 20_000 + "\n")
    github = _IgnoreGitHub(
        head_ignore="ev/*.ndjson  # sha pin\n",
        diff=diff,
        blobs={"ev/a.ndjson": blob},
        blob_bytes={blob: b"w"},
        blob_exc=GitHubHTTPError("x") if blob_fails else None,
    )
    driver = NaysayerPrReviewDriver(lexora=_FakeLexora(), github=github)
    rules = await ni.read_head_rules(github, "o", "r", _HEAD)
    view = await driver._ignore_view(_pr(), diff, rules, base_ref="main", head_sha=_HEAD)
    size = await pr_diff_size.measure(github, PrRef("o", "r", 1), _HEAD)
    assert size.original_chars == view.original_chars
    assert size.excluded_chars == len(diff) - view.original_chars > 20_000
    assert f"excluded_chars={size.excluded_chars}" in size.render()


# ---- the cap after exclusion --------------------------------------------------------------------


@pytest.mark.anyio
async def test_over_cap_after_exclusion_names_never_sent_and_partial() -> None:
    first = _section("src/a.py", "+" + "a" * (_MAX_DIFF_CHARS - 1000) + "\n")
    cut = _section("src/b.py", "+" + "b" * 5000 + "\n")
    dropped = _section("src/c.py")
    github = _IgnoreGitHub(
        head_ignore="ev/*.ndjson  # sha pin\n",
        diff=first + cut + dropped + _section("ev/a.ndjson", "+" + "e" * 50_000 + "\n"),
    )
    body, _ = await _review(github)
    assert "> PARTIAL: src/b.py" in body
    assert "> NEVER SENT: src/c.py" in body
    assert "NEVER SENT: src/a.py" not in body and "ev/a.ndjson" in body
