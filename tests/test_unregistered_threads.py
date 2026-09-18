"""Unit tests for the D-2 log-only unregistered-threads predicate.

Every decision the enumerator makes is decided from a constructed thread
listing + registered index. No network. The point of the module is to
name the three filters and to distinguish "measured 0" from "did not
measure" (msg-2531 §2 invariant 2); every branch of those two contracts
is exercised here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spirrow_mindwire.unregistered_threads import (
    LIVE_STATUSES,
    PR_REVIEW_PREFIX,
    EnumerateReport,
    ProjectReport,
    RegisteredIndex,
    UnregisteredThreadsError,
    enumerate_project,
    is_unregistered_live,
    load_registered,
    parse_registered,
    project_error_report,
)


def _thread(**kw: object) -> dict[str, object]:
    kw.setdefault("thread_id", "T-x")
    kw.setdefault("status", "active")
    return dict(kw)


def _index(*pairs: tuple[str, str], projects: tuple[str, ...] | None = None) -> RegisteredIndex:
    if projects is None:
        seen: list[str] = []
        for project, _ in pairs:
            if project not in seen:
                seen.append(project)
        projects = tuple(seen)
    return RegisteredIndex(pairs=frozenset(pairs), projects=projects)


# --------------------------------------------------------------------- LIVE_STATUSES contract


def test_live_statuses_matches_the_gate_bootstrap_definition() -> None:
    """The 'live' set is decided in one place (Bohr msg-2460 §4); D-2 mirrors it."""
    from spirrow_mindwire.gate_bootstrap import _OPEN_ALERT_TARGET_STATUSES

    assert LIVE_STATUSES == _OPEN_ALERT_TARGET_STATUSES


def test_pr_review_prefix_ends_with_trailing_hyphen() -> None:
    """Guard against the ``T-pr-review-foo`` vs ``T-pr-review-foobar`` ambiguity.

    The pr_review_sweep loader documents the same rule for its
    per-project variant (msg-2162): the trailing hyphen makes sure a
    project named ``spirrow-mindwire`` cannot swallow a sibling named
    ``spirrow-mindwire-legacy``.
    """
    assert PR_REVIEW_PREFIX.endswith("-")


# --------------------------------------------------------------------- filter (1): status


@pytest.mark.parametrize("status", sorted(LIVE_STATUSES))
def test_a_thread_is_unregistered_when_it_is_live_and_missing(status: str) -> None:
    registered = _index(projects=("p",))
    assert is_unregistered_live("p", _thread(status=status, thread_id="T-a"), registered)


@pytest.mark.parametrize("status", ["resolved", "superseded", "parked", "", "unknown"])
def test_a_non_live_status_is_never_unregistered(status: str) -> None:
    """Every status outside LIVE_STATUSES is out of scope by construction."""
    registered = _index(projects=("p",))
    assert not is_unregistered_live("p", _thread(status=status, thread_id="T-a"), registered)


# --------------------------------------------------------------------- filter (2): PR-review


def test_a_pr_review_thread_is_never_unregistered() -> None:
    """Even a live, missing pr-review thread does not count — they are excluded by contract."""
    registered = _index(projects=("p",))
    thread = _thread(thread_id=f"{PR_REVIEW_PREFIX}spirrow-mindwire-42", status="active")
    assert not is_unregistered_live("p", thread, registered)


def test_a_thread_id_that_only_contains_pr_review_as_a_suffix_still_counts() -> None:
    """Filter (2) matches the prefix, not a substring — otherwise a T-not-pr-review-* would leak."""
    registered = _index(projects=("p",))
    thread = _thread(thread_id="T-not-pr-review-lookalike", status="active")
    assert is_unregistered_live("p", thread, registered)


# --------------------------------------------------------------------- filter (3): registered set


def test_a_registered_thread_is_not_unregistered() -> None:
    registered = _index(("p", "T-a"))
    assert not is_unregistered_live("p", _thread(thread_id="T-a", status="active"), registered)


def test_project_scoping_matters() -> None:
    """Thread ids are unique per-project (sweep state key uses ``project/thread_id``).

    Same id in a different project is a different candidate — this test
    pins that the predicate does not silently collapse them.
    """
    registered = _index(("p1", "T-a"))
    assert not is_unregistered_live("p1", _thread(thread_id="T-a", status="active"), registered)
    assert is_unregistered_live("p2", _thread(thread_id="T-a", status="active"), registered)


# --------------------------------------------------------------------- shape tolerance


def test_a_thread_without_id_is_ignored() -> None:
    """No id, no way to match against sweep.json — the safe answer is 'not unregistered'."""
    registered = _index(projects=("p",))
    assert not is_unregistered_live("p", {"status": "active"}, registered)


@pytest.mark.parametrize(
    "raw_id, coerced",
    [
        (123, "123"),
        (["x"], "['x']"),
        ({"a": 1}, "{'a': 1}"),
    ],
)
def test_non_string_truthy_thread_id_is_coerced_not_ignored(raw_id: object, coerced: str) -> None:
    """Pin the current coercion behaviour so a future strict-check is a conscious break.

    The docstring on :func:`enumerate_project` was updated to accurately
    describe that truthy non-string ``thread_id`` values are coerced via
    :func:`str` and surface under ``unregistered`` (msg-3325 PR-gate
    ADVISORY on PR #287). Tightening this to reject non-strings would be
    a material semantics change, out of scope for the docstring fix; this
    test pins the current behaviour so any such tightening lights up
    here first rather than as a silent regression.
    """
    registered = _index(projects=("p",))
    threads = [{"thread_id": raw_id, "status": "active"}]
    report = enumerate_project("p", threads, registered)
    assert report.unregistered == (coerced,)
    assert report.unregistered_count == 1
    # Coercion is not a "malformed" event — the item was a dict with an
    # id-shaped field, just of the wrong Python type. The boundary drop
    # counter tracks non-object items only.
    assert report.malformed_count == 0


@pytest.mark.parametrize("raw_id", [0, [], {}, ""])
def test_falsy_non_string_thread_id_is_ignored(raw_id: object) -> None:
    """Pin the falsy branch of the coercion so a refactor cannot silently break it.

    The docstring on :func:`enumerate_project` describes a strict
    divergence: truthy non-strings (``123``, ``["x"]``) coerce and
    surface under ``unregistered``; falsy non-strings (``0``, ``[]``,
    ``{}``) and the empty string coerce to ``""`` via the
    ``str(x or "")`` idiom and are dropped. Both sides of that boundary
    must be pinned — a refactor from ``str(thread.get("thread_id") or "")``
    to ``str(thread.get("thread_id", ""))`` would push ``0`` through as
    ``"0"``, silently violating the falsy contract described in the
    docstring, and only the presence of this test would catch it
    (PR-gate ADVISORY on PR #298, class=``untested``).
    """
    registered = _index(projects=("p",))
    threads = [{"thread_id": raw_id, "status": "active"}]
    report = enumerate_project("p", threads, registered)
    assert report.unregistered == ()
    assert report.unregistered_count == 0
    assert report.malformed_count == 0


def test_enumerate_carries_upstream_malformed_count_through_unchanged() -> None:
    """msg-3225 PR-gate ADVISORY on PR #282: filtering lives at the boundary, not here.

    The CLI's ``_list_live_threads`` filters non-dicts at the wire and
    passes the drop count in via ``malformed_count``. This function
    trusts its own type signature and does no re-filtering — the earlier
    "add locally-dropped items to the upstream tally" branch was
    dual-management (production runtime logic added purely to support
    tests that violated the signature) and was removed.

    Contract pinned here: the ``malformed_count`` passed in flows onto
    ``ProjectReport.malformed_count`` unchanged, and is **never**
    merged into ``unregistered_count``.
    """
    registered = _index(projects=("p",))
    threads = [_thread(thread_id="T-a"), _thread(thread_id="T-b")]
    report = enumerate_project("p", threads, registered, malformed_count=5)
    assert report.unregistered_count == 2
    assert report.unregistered == ("T-a", "T-b")
    assert report.malformed_count == 5


def test_enumerate_default_malformed_count_is_zero_when_boundary_saw_nothing_bad() -> None:
    """A shape-clean listing at the wire produces ``malformed_count == 0``.

    The default of ``0`` is the "measured, and dropped nothing" signal;
    it is deliberately distinct from ``None`` (which
    :func:`project_error_report` sets to mean "did not measure").
    """
    registered = _index(("p", "T-a"))
    threads = [_thread(thread_id="T-a"), _thread(thread_id="T-b")]
    report = enumerate_project("p", threads, registered)
    assert report.unregistered_count == 1
    assert report.unregistered == ("T-b",)
    assert report.malformed_count == 0


def test_enumerate_preserves_thread_order() -> None:
    """Digest wiring depends on stable output — pin the ordering explicitly."""
    registered = _index(projects=("p",))
    threads = [_thread(thread_id=f"T-{c}") for c in "abcde"]
    report = enumerate_project("p", threads, registered)
    assert report.unregistered == ("T-a", "T-b", "T-c", "T-d", "T-e")


# ------------------------------------- ProjectReport / EnumerateReport contract


def test_measured_zero_is_distinct_from_unmeasured() -> None:
    """msg-2531 §2 invariant 2: 0 件 と 測れなかった を同じ表示にしない."""
    measured_zero = ProjectReport(project="p", unregistered_count=0)
    unmeasured = project_error_report("q", "chatroom_list_threads failed: boom")

    assert measured_zero.unregistered_count == 0
    assert unmeasured.unregistered_count is None
    assert measured_zero.error is None
    assert unmeasured.error is not None

    # And on the aggregate:
    report = EnumerateReport(projects=(measured_zero, unmeasured))
    assert report.unregistered_count_total == 0
    assert report.any_unmeasured is True
    assert report.unmeasured_projects == ("q",)


def test_totals_sum_only_measured_projects() -> None:
    """An unmeasured project must not silently zero out a real count."""
    report = EnumerateReport(
        projects=(
            ProjectReport(project="p", unregistered_count=3, unregistered=("a", "b", "c")),
            project_error_report("q", "network"),
            ProjectReport(project="r", unregistered_count=2, unregistered=("d", "e")),
        )
    )
    assert report.unregistered_count_total == 5
    assert report.unmeasured_projects == ("q",)


def test_project_error_report_sets_malformed_count_to_none() -> None:
    """msg-2648 §3: when the whole listing failed, malformed_count is unknown too.

    Setting it to 0 would silently claim the population was cleanly
    enumerated when in fact nothing was — the same fail-silent shape
    that the third state is meant to defeat.
    """
    report = project_error_report("q", "chatroom_list_threads failed: boom")
    assert report.unregistered_count is None
    assert report.malformed_count is None


def test_malformed_count_total_sums_only_measured_projects() -> None:
    """An unmeasured project must not silently zero out a real malformed count."""
    report = EnumerateReport(
        projects=(
            ProjectReport(project="p", unregistered_count=3, malformed_count=2),
            project_error_report("q", "network"),
            ProjectReport(project="r", unregistered_count=1, malformed_count=4),
        )
    )
    assert report.malformed_count_total == 6
    assert report.any_malformed is True


def test_any_malformed_is_false_when_only_measured_zero_and_unmeasured() -> None:
    """``any_malformed`` fires only on a **positive** measured drop count.

    An unmeasured project (``malformed_count is None``) is not evidence
    of drops — it is evidence that we could not tell. A day where every
    project either measured 0 drops or failed entirely reports
    ``any_malformed=False`` so the wrapper does not tell the operator
    "data was dropped" when what actually happened is "we could not
    check".
    """
    report = EnumerateReport(
        projects=(
            ProjectReport(project="p", unregistered_count=1, malformed_count=0),
            project_error_report("q", "network"),
        )
    )
    assert report.any_malformed is False


def test_enumerate_report_as_json_is_stable() -> None:
    """Snapshot the JSON shape the wrapper reads — a breaking change is caught here, not in prod."""
    report = EnumerateReport(
        projects=(
            ProjectReport(
                project="p",
                unregistered_count=1,
                unregistered=("T-a",),
                error=None,
                malformed_count=2,
            ),
            project_error_report("q", "network"),
        )
    )
    payload = report.as_json()
    assert payload == {
        "projects": [
            {
                "project": "p",
                "unregistered_count": 1,
                "unregistered": ["T-a"],
                "error": None,
                "malformed_count": 2,
            },
            {
                "project": "q",
                "unregistered_count": None,
                "unregistered": [],
                "error": "network",
                "malformed_count": None,
            },
        ],
        "unregistered_count_total": 1,
        "unmeasured_projects": ["q"],
        "any_unmeasured": True,
        "malformed_count_total": 2,
        "any_malformed": True,
    }


# ---------------------------------------------- parse_registered / load_registered


def test_parse_registered_extracts_pairs_and_project_order() -> None:
    payload = {
        "candidates": [
            {"project": "p1", "thread_id": "T-a", "repo_dir": "/x"},
            {"project": "p2", "thread_id": "T-b", "repo_dir": "/y"},
            {"project": "p1", "thread_id": "T-c", "repo_dir": "/z"},
        ]
    }
    index = parse_registered(payload)
    assert index.pairs == {("p1", "T-a"), ("p2", "T-b"), ("p1", "T-c")}
    assert index.projects == ("p1", "p2")  # order preserved, duplicates dropped


def test_parse_registered_rejects_non_object_root() -> None:
    with pytest.raises(UnregisteredThreadsError):
        parse_registered([])


def test_parse_registered_rejects_non_array_candidates() -> None:
    with pytest.raises(UnregisteredThreadsError):
        parse_registered({"candidates": "nope"})


def test_parse_registered_rejects_blank_project() -> None:
    with pytest.raises(UnregisteredThreadsError):
        parse_registered({"candidates": [{"project": "  ", "thread_id": "T-a"}]})


def test_parse_registered_rejects_blank_thread_id() -> None:
    with pytest.raises(UnregisteredThreadsError):
        parse_registered({"candidates": [{"project": "p", "thread_id": ""}]})


def test_parse_registered_rejects_non_object_candidate() -> None:
    with pytest.raises(UnregisteredThreadsError):
        parse_registered({"candidates": ["not-a-dict"]})


def test_parse_registered_accepts_empty_candidates() -> None:
    """An empty list is a valid file: it produces no projects to enumerate.

    The CLI decides what to do about that (report an empty run and
    exit 0); the loader itself only enforces shape.
    """
    index = parse_registered({"candidates": []})
    assert index.pairs == frozenset()
    assert index.projects == ()


def test_parse_registered_trims_whitespace_uniformly() -> None:
    """Whitespace-padded fields become the trimmed value; sweep.json is edited by humans."""
    index = parse_registered({"candidates": [{"project": " p ", "thread_id": " T-a "}]})
    assert index.pairs == {("p", "T-a")}
    assert index.projects == ("p",)


def test_load_registered_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(UnregisteredThreadsError):
        load_registered(tmp_path / "nope.json")


def test_load_registered_reports_bad_json(tmp_path: Path) -> None:
    path = tmp_path / "sweep.json"
    path.write_text("{ not json ", encoding="utf-8")
    with pytest.raises(UnregisteredThreadsError):
        load_registered(path)


def test_load_registered_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "sweep.json"
    path.write_text(
        json.dumps(
            {
                "candidates": [
                    {"project": "p", "thread_id": "T-a", "repo_dir": "/x"},
                ]
            }
        ),
        encoding="utf-8",
    )
    index = load_registered(path)
    assert index.contains("p", "T-a")
    assert not index.contains("p", "T-b")
