"""T-gate-verdict-not-a-function-of-its-input — lookup-state line and same-diff reversal line.

Settled design: msg-8422 D-1 (B-(a) coverage lookup state on the GATE NOTICE, ``ok-empty``
byte-identical), msg-8424 D-2' (diff sha256 fingerprint stamped on every content review; a
REVERSAL line when a prior head-bound APPROVE carries the same fingerprint and this gate verdict
is not APPROVE) and Obj-1-2 (the ``failed`` line names the exception class only).
"""

from __future__ import annotations

import httpx
import pytest
from test_pr_review_driver import (
    _capture,
    _FakeGitHub,
    _FakeLexora,
    _make_view,
    _objection_block,
    _pr,
)

from spirrow_mindwire.github.client import (
    CrossPrApproveCoverage,
    GitHubClient,
    GitHubHTTPError,
    PrRef,
    ReviewEvent,
    diff_sha256,
    parse_diff_sha256_marker,
    render_diff_sha256_marker,
)
from spirrow_mindwire.naysayer.pr_review import (
    _GATE_NOTICE_SENTINEL,
    _MARKER_G_COVERAGE,
    _MARKER_H_REVERSAL,
    CoverageLookup,
    NaysayerPrReviewDriver,
    decide_verdict,
    render_gate_notice,
)

_DIFF = "diff --git a/x b/x\n+added — non-ascii ✓\n"
_OTHER = PrRef("SpirrowGames", "spirrow-playproof", 95)
_APPROVED_SHA = "86209a2" + "0" * 33


def _cov(prior: str | None, sha: str = _APPROVED_SHA) -> CrossPrApproveCoverage:
    return CrossPrApproveCoverage(
        sha=sha, other_pr=_OTHER, approved_at="2026-10-01T10:38:00Z", prior_diff_sha256=prior
    )


async def _run(
    *,
    verdict: str,
    coverage: list[CrossPrApproveCoverage] | None = None,
    exc: Exception | None = None,
) -> tuple[str, ReviewEvent]:
    # An empty objection block keeps an APPROVE fully quiet (no D-divergence), so a notice in
    # these tests is there only because of the lines under test.
    lexora = _FakeLexora(content=f"critique\n\n{_objection_block('[]')}\n\nVERDICT: {verdict}")
    github = _FakeGitHub(diff=_DIFF, coverage=coverage, coverage_exc=exc)
    posted, post = _capture()
    outcome = await NaysayerPrReviewDriver(lexora=lexora, github=github).review(
        _pr(), post_critique=post
    )
    assert len(github.submitted) == 1
    assert posted[0] == github.submitted[0][2]  # one body, both channels
    return posted[0], outcome.verdict


def _notice(body: str) -> str:
    """The GATE NOTICE block (everything before the first blank line), or "" when absent."""
    return body.split("\n\n", 1)[0] if body.startswith(_GATE_NOTICE_SENTINEL) else ""


# ---------- fingerprint: hashing, stamping, parsing ------------------------------------------- #


def test_diff_sha256_hashes_utf8_bytes() -> None:
    import hashlib

    assert diff_sha256(_DIFF) == hashlib.sha256(_DIFF.encode("utf-8")).hexdigest()


def test_parse_takes_the_last_whole_line_marker() -> None:
    """A marker the model quoted inside the critique sits above the gate's own line."""
    quoted = render_diff_sha256_marker("a" * 64)
    own = render_diff_sha256_marker("b" * 64)
    body = f"the PR adds `{quoted}` inline\n{quoted}\n\nVERDICT: X\n\n{own}\nADR-INDEX: none"
    assert parse_diff_sha256_marker(body) == "b" * 64


@pytest.mark.parametrize("body", [None, "", "no marker here", "<!-- mindwire:diff-sha256 xyz -->"])
def test_parse_without_a_valid_marker_is_none(body: str | None) -> None:
    assert parse_diff_sha256_marker(body) is None


@pytest.mark.anyio
async def test_every_content_review_carries_the_fingerprint_of_the_untruncated_diff() -> None:
    body, _ = await _run(verdict="APPROVE")
    assert parse_diff_sha256_marker(body) == diff_sha256(_DIFF)
    # Below the critique, above the fixed-position tail (verdict footer, ADR marker last).
    assert body.index(render_diff_sha256_marker(diff_sha256(_DIFF))) > body.index("critique")
    assert body.rstrip().splitlines()[-1].startswith("ADR-INDEX:")


# ---------- D-1: lookup state line ------------------------------------------------------------ #


@pytest.mark.anyio
async def test_ok_empty_prints_no_notice_and_no_coverage_line() -> None:
    """``ok-empty`` is silent: no sentinel, no G marker — the pre-change notice shape."""
    body, _ = await _run(verdict="APPROVE", coverage=[])
    assert _GATE_NOTICE_SENTINEL not in body
    assert _MARKER_G_COVERAGE not in body
    assert "prior-verdict coverage" not in body


@pytest.mark.parametrize("digest", [None, "e" * 64])
def test_ok_empty_notice_is_byte_identical_to_no_lookup(digest: str | None) -> None:
    """Both quiet and loud notices: an ``ok-empty`` lookup adds not one byte."""
    from dataclasses import replace

    for critique in ("x\n\n" + _objection_block("[]") + "\n\nVERDICT: APPROVE", "x\n\nVERDICT: X"):
        base = decide_verdict(critique, view=_make_view(10), finish_reason="stop")
        with_lookup = replace(base, coverage_lookup=CoverageLookup(), diff_sha256=digest)
        assert render_gate_notice(with_lookup) == render_gate_notice(base)


@pytest.mark.anyio
async def test_ok_n_prints_the_coverage_line() -> None:
    covs = [_cov(None), _cov(None, sha="c" * 40)]
    body, _ = await _run(verdict="APPROVE", coverage=covs)
    notice = _notice(body)
    assert _MARKER_G_COVERAGE in notice
    assert (
        "> prior-verdict coverage: 2 commit(s) already head-bound APPROVED on "
        "SpirrowGames/spirrow-playproof#95 — marker was in the prompt"
    ) in notice


@pytest.mark.anyio
async def test_failed_prints_class_name_only_never_the_message() -> None:
    secret = "https://api.example/repos/x?token=ghp_LEAKCANARY"
    body, verdict = await _run(verdict="APPROVE", exc=GitHubHTTPError(secret, status_code=503))
    notice = _notice(body)
    assert (
        "> prior-verdict coverage lookup FAILED (GitHubHTTPError) — reviewed WITHOUT the marker"
    ) in notice
    assert "LEAKCANARY" not in body
    assert verdict is ReviewEvent.APPROVE  # fail-open unchanged: the lookup never gates


# ---------- D-2': reversal line --------------------------------------------------------------- #


@pytest.mark.anyio
async def test_reversal_fires_on_same_fingerprint_and_non_approve() -> None:
    body, verdict = await _run(verdict="REQUEST_CHANGES", coverage=[_cov(diff_sha256(_DIFF))])
    notice = _notice(body)
    assert verdict is ReviewEvent.REQUEST_CHANGES  # the verdict is not rewritten
    assert _MARKER_H_REVERSAL in notice
    assert (
        f"> **Verdict reversal on identical input.** this diff's sha256 "
        f"{diff_sha256(_DIFF)[:12]} == the diff APPROVED in SpirrowGames/spirrow-playproof#95 "
        f"at {_APPROVED_SHA[:12]}."
    ) in notice
    assert "neither supersedes the other by recency" in notice


@pytest.mark.anyio
async def test_reversal_silent_when_this_verdict_is_approve() -> None:
    body, _ = await _run(verdict="APPROVE", coverage=[_cov(diff_sha256(_DIFF))])
    assert _MARKER_H_REVERSAL not in body
    assert _MARKER_G_COVERAGE in _notice(body)  # the coverage line still reports


@pytest.mark.anyio
async def test_reversal_silent_when_fingerprint_differs() -> None:
    """Different bytes (e.g. same head tree over another base) is a different input."""
    body, _ = await _run(verdict="REQUEST_CHANGES", coverage=[_cov("f" * 64)])
    assert _MARKER_H_REVERSAL not in body


@pytest.mark.anyio
async def test_reversal_silent_when_prior_review_has_no_marker() -> None:
    """A review written before the marker existed: cannot tell, so say nothing."""
    body, _ = await _run(verdict="REQUEST_CHANGES", coverage=[_cov(None)])
    assert _MARKER_H_REVERSAL not in body


# ---------- client: the prior fingerprint is read from the review body ------------------------ #


@pytest.mark.anyio
async def test_client_fills_prior_diff_sha256_from_the_review_body() -> None:
    digest = "d" * 64
    me = PrRef("SpirrowGames", "spirrow-playproof", 96)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/96/commits"):
            return httpx.Response(200, json=[{"sha": "sha1"}])
        if path.endswith("/commits/sha1/pulls"):
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 95,
                        "base": {
                            "repo": {
                                "name": "spirrow-playproof",
                                "owner": {"login": "SpirrowGames"},
                            }
                        },
                    }
                ],
            )
        if path.endswith("/pulls/95/reviews"):
            return httpx.Response(
                200,
                json=[
                    {
                        "user": {"login": "spirrowgames-ops"},
                        "state": "APPROVED",
                        "commit_id": "sha1",
                        "submitted_at": "2026-10-01T10:38:00Z",
                        "body": f"ok\n\nVERDICT: APPROVE\n\n{render_diff_sha256_marker(digest)}",
                    }
                ],
            )
        return httpx.Response(404)

    async with GitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.test"
    ) as client:
        result = await client.find_cross_pr_head_bound_approves(
            me, reviewer_login="spirrowgames-ops"
        )
    assert [c.prior_diff_sha256 for c in result] == [digest]
