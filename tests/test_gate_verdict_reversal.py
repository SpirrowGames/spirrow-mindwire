"""T-gate-verdict-not-a-function-of-its-input — lookup-state line and same-diff reversal line.

Settled design:
- msg-8810 D-1' — the coverage lookup returns ``CoverageLookup(coverage, read_failures)`` from
  strict reads; ``ok-empty`` is byte-identical, ``failed(<ExcClass>[ status=<n>], k read(s))``
  when nothing was found and reads failed, ``; k read(s) failed — coverage may be partial``
  appended to ``ok(n)``. Labels only, never ``str(exc)`` (msg-8424 Obj-1-2).
- msg-8810 D-2'' / msg-8812 Obj-3-2 — the fingerprint is sha256 over ``view.text`` (the diff
  string handed to ``_build_messages``), stamped as ``reviewed-diff-sha256 v1``; other names and
  versions are not comparable.
- msg-8812 Obj-3-1 — the reversal wording claims only the diff text matched.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

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
    CoverageLookup,
    CrossPrApproveCoverage,
    GitHubClient,
    GitHubHTTPError,
    PrRef,
    ReviewEvent,
    parse_reviewed_diff_sha256_marker,
    render_reviewed_diff_sha256_marker,
    reviewed_diff_sha256,
)
from spirrow_mindwire.naysayer.pr_review import (
    _GATE_NOTICE_SENTINEL,
    _MARKER_G_COVERAGE,
    _MARKER_H_REVERSAL,
    DiffView,
    NaysayerPrReviewDriver,
    decide_verdict,
    render_gate_notice,
)

_DIFF = "diff --git a/x b/x\n+added — non-ascii ✓\n"
_OTHER = PrRef("SpirrowGames", "spirrow-playproof", 95)
_APPROVED_SHA = "86209a2" + "0" * 33
_CANARY = "https://api.example/repos/x?token=ghp_LEAKCANARY"


def _cov(prior: str | None, sha: str = _APPROVED_SHA) -> CrossPrApproveCoverage:
    return CrossPrApproveCoverage(
        sha=sha,
        other_pr=_OTHER,
        approved_at="2026-10-01T10:38:00Z",
        prior_diff_sha256s=(prior,) if prior is not None else (),
    )


async def _run(
    *,
    verdict: str,
    coverage: list[CrossPrApproveCoverage] | None = None,
    exc: Exception | None = None,
    read_failures: tuple[str, ...] = (),
    pr: PrRef | None = None,
) -> tuple[str, ReviewEvent]:
    # An empty objection block keeps an APPROVE fully quiet (no D-divergence), so a notice in
    # these tests is there only because of the lines under test.
    lexora = _FakeLexora(content=f"critique\n\n{_objection_block('[]')}\n\nVERDICT: {verdict}")
    github = _FakeGitHub(diff=_DIFF, coverage=coverage, coverage_exc=exc)
    github.read_failures = read_failures
    posted, post = _capture()
    outcome = await NaysayerPrReviewDriver(lexora=lexora, github=github).review(
        pr or _pr(), post_critique=post
    )
    assert len(github.submitted) == 1
    assert posted[0] == github.submitted[0][2]  # one body, both channels
    return posted[0], outcome.verdict


def _notice(body: str) -> str:
    """The GATE NOTICE block (everything before the first blank line), or "" when absent."""
    return body.split("\n\n", 1)[0] if body.startswith(_GATE_NOTICE_SENTINEL) else ""


_THIS = reviewed_diff_sha256(_DIFF)  # with no ignore rules and a small diff, view.text == diff


# ---------- fingerprint: hashing, stamping, parsing ------------------------------------------- #


def test_reviewed_diff_sha256_hashes_utf8_bytes() -> None:
    assert reviewed_diff_sha256(_DIFF) == hashlib.sha256(_DIFF.encode("utf-8")).hexdigest()


def test_parse_takes_the_last_whole_line_v1_marker() -> None:
    """A marker the model quoted inside the critique sits above the gate's own line."""
    quoted = render_reviewed_diff_sha256_marker("a" * 64)
    own = render_reviewed_diff_sha256_marker("b" * 64)
    body = f"the PR adds `{quoted}` inline\n{quoted}\n\nVERDICT: X\n\n{own}\nADR-INDEX: none"
    assert parse_reviewed_diff_sha256_marker(body) == "b" * 64


@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        "no marker here",
        "<!-- mindwire:reviewed-diff-sha256 v1 xyz -->",
        # The #556-first-revision raw-diff marker: another hash domain, never comparable.
        f"<!-- mindwire:diff-sha256 {'a' * 64} -->",
        # A version this code does not know.
        f"<!-- mindwire:reviewed-diff-sha256 v2 {'a' * 64} -->",
    ],
)
def test_parse_without_a_v1_marker_is_none(body: str | None) -> None:
    assert parse_reviewed_diff_sha256_marker(body) is None


@pytest.mark.anyio
async def test_every_content_review_carries_the_v1_fingerprint() -> None:
    body, _ = await _run(verdict="APPROVE")
    assert parse_reviewed_diff_sha256_marker(body) == _THIS
    # Below the critique, above the fixed-position tail (verdict footer, ADR marker last).
    assert body.index(render_reviewed_diff_sha256_marker(_THIS)) > body.index("critique")
    assert body.rstrip().splitlines()[-1].startswith("ADR-INDEX:")


@pytest.mark.anyio
async def test_fingerprint_is_of_view_text_not_the_raw_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-2'': the hash covers what the model read (after ignore filtering / truncation)."""
    filtered = "diff --git a/kept b/kept\n+kept\n"

    async def _view(self: object, pr: object, diff: str, rules: object, **_: object) -> DiffView:
        return DiffView(text=filtered, original_chars=len(diff), limit=10**6, warn_threshold=10**6)

    monkeypatch.setattr(NaysayerPrReviewDriver, "_ignore_view", _view)
    body, _ = await _run(verdict="APPROVE")
    assert parse_reviewed_diff_sha256_marker(body) == reviewed_diff_sha256(filtered)
    assert reviewed_diff_sha256(filtered) != reviewed_diff_sha256(_DIFF)


# ---------- D-1': lookup state line ----------------------------------------------------------- #


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
        "SpirrowGames/spirrow-playproof#95 — marker was in the prompt\n"
    ) in notice + "\n"


@pytest.mark.anyio
async def test_ok_n_with_failed_reads_says_coverage_may_be_partial() -> None:
    body, _ = await _run(
        verdict="APPROVE", coverage=[_cov(None)], read_failures=("GitHubHTTPError status=502",)
    )
    assert (
        "> prior-verdict coverage: 1 commit(s) already head-bound APPROVED on "
        "SpirrowGames/spirrow-playproof#95 — marker was in the prompt"
        "; 1 read(s) failed — coverage may be partial"
    ) in _notice(body)


@pytest.mark.anyio
async def test_failed_from_client_read_failures() -> None:
    body, verdict = await _run(
        verdict="APPROVE", read_failures=("GitHubHTTPError status=502", "TimeoutError")
    )
    assert (
        "> prior-verdict coverage lookup FAILED (GitHubHTTPError status=502, 2 read(s)) — "
        "reviewed WITHOUT the marker"
    ) in _notice(body)
    assert verdict is ReviewEvent.APPROVE  # fail-open unchanged: the lookup never gates


@pytest.mark.anyio
async def test_failed_from_driver_belt_prints_class_and_status_never_the_message() -> None:
    body, verdict = await _run(verdict="APPROVE", exc=GitHubHTTPError(_CANARY, status_code=503))
    assert (
        "> prior-verdict coverage lookup FAILED (GitHubHTTPError status=503, 1 read(s)) — "
        "reviewed WITHOUT the marker"
    ) in _notice(body)
    assert "LEAKCANARY" not in body
    assert verdict is ReviewEvent.APPROVE


# ---------- D-1': the client records failed strict reads -------------------------------------- #

_ME = PrRef("SpirrowGames", "spirrow-playproof", 96)
_UNMARKED = "ok\n\nVERDICT: APPROVE"


def _marked(hexd: str) -> str:
    return f"ok\n\nVERDICT: APPROVE\n\n{render_reviewed_diff_sha256_marker(hexd)}"


def _client_handler(
    *,
    commits: int | None = None,
    pulls: int | None = None,
    reviews: int | None = None,
    commits_body: object = None,
    pulls_body: object = None,
    approve_bodies: tuple[str, ...] | None = None,
    reviews_raw: bytes | None = None,
) -> httpx.MockTransport:
    """Two commits, both in PR #95; #95 carries a head-bound APPROVE on sha2 only.

    An int argument makes that endpoint answer with that status (and a canary in its body);
    for ``pulls`` / ``reviews`` only the FIRST call fails, so a partial result is observable.
    ``reviews_raw`` makes EVERY reviews call answer 200 with that raw JSON body.
    """
    seen = {"pulls": 0, "reviews": 0}

    def fail(status: int) -> httpx.Response:
        return httpx.Response(status, json={"message": _CANARY})

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/96/commits"):
            if commits is not None:
                return fail(commits)
            if commits_body is not None:
                return httpx.Response(200, json=commits_body)
            return httpx.Response(200, json=[{"sha": "sha1"}, {"sha": "sha2"}])
        if "/commits/" in path and path.endswith("/pulls"):
            seen["pulls"] += 1
            if pulls is not None and seen["pulls"] == 1:
                return fail(pulls)
            if pulls_body is not None and seen["pulls"] == 1:
                return httpx.Response(200, json=pulls_body)
            row = {
                "number": 95,
                "base": {"repo": {"name": "spirrow-playproof", "owner": {"login": "SpirrowGames"}}},
            }
            return httpx.Response(200, json=[row])
        if path.endswith("/pulls/95/reviews"):
            seen["reviews"] += 1
            if reviews_raw is not None:
                return httpx.Response(
                    200, content=reviews_raw, headers={"content-type": "application/json"}
                )
            if reviews is not None and seen["reviews"] == 1:
                return fail(reviews)
            bodies = approve_bodies if approve_bodies is not None else (_marked("d" * 64),)
            return httpx.Response(
                200,
                json=[
                    {
                        "user": {"login": "spirrowgames-ops"},
                        "state": "APPROVED",
                        "commit_id": "sha2",
                        "submitted_at": f"2026-10-01T10:3{i}:00Z",
                        "body": b,
                    }
                    for i, b in enumerate(bodies)
                ],
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def _lookup(transport: httpx.MockTransport) -> CoverageLookup:
    async with GitHubClient(token="t", transport=transport, base_url="https://api.test") as c:
        return await c.find_cross_pr_head_bound_approves(_ME, reviewer_login="spirrowgames-ops")


@pytest.mark.anyio
async def test_client_commits_read_502_is_failed() -> None:
    result = await _lookup(_client_handler(commits=502))
    assert result.state == "failed"
    assert result.coverage == []
    assert result.read_failures == ("GitHubHTTPError status=502",)


@pytest.mark.anyio
async def test_client_commit_pulls_read_502_is_recorded_and_skipped() -> None:
    result = await _lookup(_client_handler(pulls=502))
    # sha1's pulls read failed; sha2 still found its APPROVE → ok, partial.
    assert result.state == "ok"
    assert [c.sha for c in result.coverage] == ["sha2"]
    assert result.read_failures == ("GitHubHTTPError status=502",)


@pytest.mark.anyio
async def test_client_reviews_read_502_is_recorded_and_retried() -> None:
    result = await _lookup(_client_handler(reviews=502))
    # The failed reviews read is not cached; sha2 retries and finds the APPROVE.
    assert [c.sha for c in result.coverage] == ["sha2"]
    assert result.read_failures == ("GitHubHTTPError status=502",)


@pytest.mark.anyio
@pytest.mark.parametrize("which", ["commits", "pulls", "reviews"])
async def test_client_read_failures_never_carry_the_message(which: str) -> None:
    result = await _lookup(
        _client_handler(
            commits=502 if which == "commits" else None,
            pulls=502 if which == "pulls" else None,
            reviews=502 if which == "reviews" else None,
        )
    )
    assert result.read_failures
    assert all("LEAKCANARY" not in f and "/" not in f for f in result.read_failures)


@pytest.mark.anyio
async def test_client_fills_prior_diff_sha256_from_the_review_body() -> None:
    result = await _lookup(_client_handler())
    assert result.state == "ok"
    assert result.read_failures == ()
    assert [c.prior_diff_sha256s for c in result.coverage] == [("d" * 64,)]


# PR-gate #556 round 1 obj 1: a 2xx body that is not a JSON list is a failed read, not ok-empty.
@pytest.mark.anyio
@pytest.mark.parametrize("bad", [{}, {"message": _CANARY}, "x", 0])
async def test_client_commits_non_list_200_is_a_failed_read(bad: object) -> None:
    result = await _lookup(_client_handler(commits_body=bad))
    assert result.state == "failed"
    assert result.coverage == []
    assert result.read_failures == ("GitHubHTTPError",)
    assert all("LEAKCANARY" not in f for f in result.read_failures)


@pytest.mark.anyio
async def test_client_commits_null_200_is_a_failed_read() -> None:
    """``null`` on its own: the handler's ``None`` default means "use the normal rows"."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/96/commits"):
            return httpx.Response(
                200, content=b"null", headers={"content-type": "application/json"}
            )
        return httpx.Response(404)

    result = await _lookup(httpx.MockTransport(handler))
    assert result.state == "failed"
    assert result.read_failures == ("GitHubHTTPError",)


@pytest.mark.anyio
async def test_client_commit_pulls_non_list_200_is_recorded_and_skipped() -> None:
    result = await _lookup(_client_handler(pulls_body={}))
    assert result.state == "ok"
    assert [c.sha for c in result.coverage] == ["sha2"]
    assert result.read_failures == ("GitHubHTTPError",)


# PR-gate #556 round 2 obj 1: a 2xx reviews body that is not a JSON list is a failed read too.
@pytest.mark.anyio
@pytest.mark.parametrize(
    "raw", [b"{}", b"null", b'"x"', b"0", b'{"message": "' + _CANARY.encode() + b'"}']
)
async def test_client_reviews_non_list_200_is_a_failed_read_and_fail_open(raw: bytes) -> None:
    # commits and commit→pulls succeed; only the reviews endpoint answers a non-list 200.
    # Must not raise (fail-open), and must NOT come back as ok-empty.
    result = await _lookup(_client_handler(reviews_raw=raw))
    assert result.state == "failed"
    assert result.coverage == []
    # Not cached on failure, so each of the two commits retried the read and recorded it.
    assert result.read_failures == ("GitHubHTTPError", "GitHubHTTPError")
    assert all("LEAKCANARY" not in f for f in result.read_failures)


@pytest.mark.anyio
async def test_reviews_non_list_200_reaches_the_notice_as_failed_and_stays_fail_open() -> None:
    async with GitHubClient(
        token="t", transport=_client_handler(reviews_raw=b"{}"), base_url="https://api.test"
    ) as c:
        lookup = await c.find_cross_pr_head_bound_approves(_ME, reviewer_login="spirrowgames-ops")
    body, verdict = await _run(verdict="APPROVE", read_failures=lookup.read_failures)
    assert (
        "> prior-verdict coverage lookup FAILED (GitHubHTTPError, 2 read(s)) — "
        "reviewed WITHOUT the marker"
    ) in _notice(body)
    assert verdict is ReviewEvent.APPROVE


@pytest.mark.anyio
async def test_shared_fetch_pr_reviews_strict_contract_is_unchanged() -> None:
    """The coverage-only read is separate; the shared strict read still reads {} as no rows."""
    async with GitHubClient(
        token="t", transport=_client_handler(reviews_raw=b"{}"), base_url="https://api.test"
    ) as c:
        assert await c.fetch_pr_reviews_strict(_OTHER) == []


# PR-gate #556 round 1 obj 2: every qualifying APPROVE's fingerprint is kept.
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("bodies", "expected"),
    [
        ((_UNMARKED, _marked("d" * 64)), ("d" * 64,)),
        ((_marked("f" * 64), _marked("d" * 64)), ("f" * 64, "d" * 64)),
    ],
    ids=["unmarked-then-marked", "differing-then-marked"],
)
async def test_client_keeps_fingerprints_of_all_approvals_one_row_per_pair(
    bodies: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    result = await _lookup(_client_handler(approve_bodies=bodies))
    assert len(result.coverage) == 1  # coverage reporting stays one row per (sha, PR)
    row = result.coverage[0]
    assert row.sha == "sha2"
    assert row.approved_at == "2026-10-01T10:30:00Z"  # the first approval, as before
    assert row.prior_diff_sha256s == expected


# ---------- D-2'': reversal line -------------------------------------------------------------- #


@pytest.mark.anyio
async def test_reversal_fires_on_same_fingerprint_and_non_approve() -> None:
    body, verdict = await _run(verdict="REQUEST_CHANGES", coverage=[_cov(_THIS)])
    notice = _notice(body)
    assert verdict is ReviewEvent.REQUEST_CHANGES  # the verdict is not rewritten
    assert _MARKER_H_REVERSAL in notice
    assert (
        f"> **Identical reviewed diff, differing verdicts.** The diff text this review read "
        f"hashes to {_THIS[:12]}, the same as the diff text read by the review that APPROVED "
        f"SpirrowGames/spirrow-playproof#95 at {_APPROVED_SHA[:12]}. Only the diff text is "
        f"shown to match; other prompt context (PR slug, prior-verdict marker) may differ.\n"
        f"> This notice records the disagreement; it does not decide which verdict stands."
    ) in notice


@pytest.mark.anyio
async def test_reversal_wording_with_different_slug_and_coverage_claims_only_the_diff() -> None:
    """Same ``view.text``, different PR slug and a B-(a) marker in the prompt (msg-8812)."""
    other_pr = PrRef("someone", "elsewhere", 7)
    body, _ = await _run(
        verdict="REQUEST_CHANGES", coverage=[_cov(_THIS), _cov(None, sha="c" * 40)], pr=other_pr
    )
    assert "Identical reviewed diff, differing verdicts." in body
    assert "other prompt context (PR slug, prior-verdict marker) may differ" in body
    assert "identical input" not in body.lower()
    assert "supersedes" not in body


@pytest.mark.anyio
async def test_reversal_silent_when_this_verdict_is_approve() -> None:
    body, _ = await _run(verdict="APPROVE", coverage=[_cov(_THIS)])
    assert _MARKER_H_REVERSAL not in body
    assert _MARKER_G_COVERAGE in _notice(body)  # the coverage line still reports


@pytest.mark.anyio
async def test_reversal_silent_when_fingerprint_differs() -> None:
    """Different diff text (e.g. same head tree over another base) is a different input."""
    body, _ = await _run(verdict="REQUEST_CHANGES", coverage=[_cov("f" * 64)])
    assert _MARKER_H_REVERSAL not in body


@pytest.mark.anyio
@pytest.mark.parametrize(
    "first", [_UNMARKED, _marked("f" * 64)], ids=["unmarked-then-match", "differing-then-match"]
)
async def test_reversal_fires_when_a_later_approval_matches(first: str) -> None:
    """End to end: client lookup, then the driver notice; the match is on the SECOND approval."""
    lookup = await _lookup(_client_handler(approve_bodies=(first, _marked(_THIS))))
    assert len(lookup.coverage) == 1
    body, verdict = await _run(verdict="REQUEST_CHANGES", coverage=lookup.coverage)
    assert verdict is ReviewEvent.REQUEST_CHANGES
    assert _MARKER_H_REVERSAL in _notice(body)


@pytest.mark.anyio
async def test_reversal_silent_when_prior_review_has_no_marker() -> None:
    """A review written before the marker existed: cannot compare, so say nothing."""
    body, _ = await _run(verdict="REQUEST_CHANGES", coverage=[_cov(None)])
    assert _MARKER_H_REVERSAL not in body


@pytest.mark.anyio
@pytest.mark.parametrize(
    "prior_marker",
    [
        f"<!-- mindwire:diff-sha256 {_THIS} -->",  # old raw-diff marker, same hex
        f"<!-- mindwire:reviewed-diff-sha256 v2 {_THIS} -->",  # unknown version, same hex
    ],
)
async def test_reversal_silent_for_old_or_unknown_marker_even_with_matching_hex(
    prior_marker: str,
) -> None:
    """The coverage row is built by the same parser the client uses, so nothing is faked."""
    prior = parse_reviewed_diff_sha256_marker(f"ok\n\nVERDICT: APPROVE\n\n{prior_marker}")
    body, _ = await _run(verdict="REQUEST_CHANGES", coverage=[_cov(prior)])
    assert _MARKER_H_REVERSAL not in body
