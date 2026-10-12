"""Unit tests for the D-2 log-only unregistered-threads predicate.

Every decision the enumerator makes is decided from a constructed thread
listing + registered index. No network. The point of the module is to
name the three filters and to distinguish "measured 0" from "did not
measure" (msg-2531 §2 invariant 2); every branch of those two contracts
is exercised here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spirrow_mindwire.unregistered_threads import (
    LIVE_STATUSES,
    PR_REVIEW_PREFIX,
    EnumerateReport,
    ListedThread,
    ProjectReport,
    RegisteredIndex,
    ThreadTime,
    UnregisteredThreadsError,
    classify_updated_at,
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


def _listed(threads: list[dict[str, object]]) -> list[ListedThread]:
    """Wrap plain dicts as the boundary would, with no ``updated_at`` (``missing``)."""
    return [ListedThread(dict(t), ThreadTime("missing")) for t in threads]


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

    The docstring on :func:`enumerate_project` describes that truthy
    non-string ``thread_id`` values are coerced via :func:`str` and
    surface under ``unregistered``. Tightening this to reject
    non-strings would be a material semantics change, out of scope for
    the docstring fix; this test pins the current behaviour so any such
    tightening lights up here first rather than as a silent regression.
    """
    registered = _index(projects=("p",))
    threads = [{"thread_id": raw_id, "status": "active"}]
    report = enumerate_project("p", _listed(threads), registered)
    assert report.unregistered == (coerced,)
    assert report.unregistered_count == 1
    # Coercion is not a "malformed" event — the item was a dict with an
    # id-shaped field, just of the wrong Python type. The boundary drop
    # counter tracks non-object items only.
    assert report.malformed_count == 0


@pytest.mark.parametrize("raw_id", [0, [], {}, "", None])
def test_falsy_thread_id_is_ignored(raw_id: object) -> None:
    """Pin the falsy branch of the coercion so a refactor cannot silently break it.

    The docstring on :func:`enumerate_project` describes a strict
    divergence: truthy non-strings (``123``, ``["x"]``) coerce and
    surface under ``unregistered``; every falsy ``thread_id`` — the
    falsy non-strings (``0``, ``[]``, ``{}``, ``None``) and the empty
    string ``""`` — coerces to ``""`` via the ``str(x or "")`` idiom
    and is dropped. Both sides of that boundary must be pinned — a
    refactor from ``str(thread.get("thread_id") or "")`` to
    ``str(thread.get("thread_id", ""))`` would push ``0`` through as
    ``"0"``, silently violating the falsy contract described in the
    docstring, and only the presence of this test would catch it.
    ``None`` is covered explicitly as a distinct upstream state from a
    missing key: a JSON payload can send ``"thread_id": null`` and,
    mechanically, that is a dict with the key present and the value
    ``None`` — different from ``dict.get`` returning the default because
    the key is absent. The empty string ``""`` is a string, not a
    non-string, so the test name uses the umbrella term "falsy" rather
    than "non-string" — the property that unites all five parametrised
    values is that they are all falsy under Python truth-testing.
    """
    registered = _index(projects=("p",))
    threads = [{"thread_id": raw_id, "status": "active"}]
    report = enumerate_project("p", _listed(threads), registered)
    assert report.unregistered == ()
    assert report.unregistered_count == 0
    assert report.malformed_count == 0


def test_enumerate_carries_upstream_malformed_count_through_unchanged() -> None:
    """Filtering lives at the boundary, not here.

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
    report = enumerate_project("p", _listed(threads), registered, malformed_count=5)
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
    report = enumerate_project("p", _listed(threads), registered)
    assert report.unregistered_count == 1
    assert report.unregistered == ("T-b",)
    assert report.malformed_count == 0


def test_enumerate_preserves_thread_order() -> None:
    """Digest wiring depends on stable output — pin the ordering explicitly."""
    registered = _index(projects=("p",))
    threads = [_thread(thread_id=f"T-{c}") for c in "abcde"]
    report = enumerate_project("p", _listed(threads), registered)
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
                "threads": [],
                "error": None,
                "malformed_count": 2,
            },
            {
                "project": "q",
                "unregistered_count": None,
                "unregistered": [],
                "threads": [],
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


def test_powershell_pr_review_prefix_matches_python() -> None:
    """The sweep's local PR-review refusal must use the same prefix as the enumerator.

    ``deploy/lib/UnregisteredIntake.ps1`` keeps its own copy of the prefix as a second check
    against sweeping a paid PR-gate thread (PR #448 gate advisory, class=structure). A copy that
    drifted would stop protecting anything without a sound, so the two are pinned equal here.
    """
    import re

    from spirrow_mindwire.unregistered_threads import PR_REVIEW_PREFIX

    lib = Path(__file__).resolve().parent.parent / "deploy" / "lib" / "UnregisteredIntake.ps1"
    found = re.findall(
        r"^\$script:UnregisteredPrReviewPrefix\s*=\s*'([^']*)'\s*$",
        lib.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    assert found == [PR_REVIEW_PREFIX]


# ----------------------- T-sweep-intake-discovery-by-default (msg-6776 / msg-6778)


@pytest.mark.parametrize(
    "raw, state",
    [
        (None, "missing"),
        ("", "missing"),
        ("   ", "missing"),
        ("2026-10-01T00:00:00Z", "ok"),
        ("2026-10-01T09:00:00+09:00", "ok"),
        ("2026-10-01T00:00:00.123456+00:00", "ok"),
        ("2026-10-01T00:00:00", "unparseable"),  # no offset: ambiguous, not comparable
        ("yesterday", "unparseable"),
        (1727740800, "unparseable"),
        ({"at": "x"}, "unparseable"),
    ],
)
def test_classify_updated_at_has_three_unmixed_states(raw: object, state: str) -> None:
    """msg-6770 §1: 時刻あり / 時刻なし / 解釈不能 are reported as three distinct states."""
    t = classify_updated_at(raw)
    assert t.state == state
    assert (t.at is not None) == (state == "ok")


def test_classify_updated_at_normalises_to_utc() -> None:
    t = classify_updated_at("2026-10-01T09:00:00+09:00")
    assert t.at == datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def test_enumerate_reports_idle_days_and_keeps_unknown_times() -> None:
    """Classifying is not dropping (msg-6778): a thread with no usable time is still listed."""
    now = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
    registered = _index(projects=("p",))
    threads = [
        ListedThread(_thread(thread_id="T-ok"), classify_updated_at("2026-10-01T00:00:00Z")),
        ListedThread(_thread(thread_id="T-none"), classify_updated_at(None)),
        ListedThread(_thread(thread_id="T-bad"), classify_updated_at("garbage")),
    ]
    report = enumerate_project("p", threads, registered, now=now)
    assert report.unregistered == ("T-ok", "T-none", "T-bad")
    assert [t.as_json() for t in report.threads] == [
        {
            "thread_id": "T-ok",
            "time_state": "ok",
            "updated_at": "2026-10-01T00:00:00+00:00",
            "idle_days": 10.0,
        },
        {"thread_id": "T-none", "time_state": "missing", "updated_at": None, "idle_days": None},
        {"thread_id": "T-bad", "time_state": "unparseable", "updated_at": None, "idle_days": None},
    ]


@pytest.mark.parametrize("raw", [None, "garbage", "2020-01-01T00:00:00Z"])
def test_an_explicit_candidate_never_reaches_the_report_whatever_its_time(raw: object) -> None:
    """msg-6778 regression: missing / unparseable / over-limit time cannot touch a listed thread.

    The listed pair is removed by filter 3 before any time is looked at, so
    the wrapper's time and idle rules never see it.
    """
    registered = _index(("p", "T-listed"))
    threads = [ListedThread(_thread(thread_id="T-listed"), classify_updated_at(raw))]
    report = enumerate_project("p", threads, registered)
    assert report.unregistered == ()
    assert report.threads == ()


def test_parse_registered_scans_the_union_of_candidates_clones_and_projects() -> None:
    """穴 3: clones keys and the projects key widen the scan; candidates come first, deduped."""
    payload = {
        "candidates": [{"project": "p1", "thread_id": "T-a", "repo_dir": "/x"}],
        "clones": {"p2": "/c2", "p1": "/c1"},
        "projects": ["p3", "p2"],
    }
    index = parse_registered(payload)
    assert index.projects == ("p1", "p2", "p3")
    assert index.pairs == frozenset({("p1", "T-a")})


def test_parse_registered_accepts_a_config_without_candidates() -> None:
    index = parse_registered({"clones": {"p": "/c"}})
    assert index.projects == ("p",)
    assert index.pairs == frozenset()


@pytest.mark.parametrize(
    "clones",
    [["p"], "p", {"p": ""}, {"p": "  "}, {"p": 3}, {"  ": "/c"}],
)
def test_parse_registered_rejects_malformed_clones(clones: object) -> None:
    """A shape error in clones is a config error: loud, like the wrapper's throw."""
    with pytest.raises(UnregisteredThreadsError, match="clones"):
        parse_registered({"candidates": [], "clones": clones})


@pytest.mark.parametrize("projects", ["p", [""], [3], {"p": 1}])
def test_parse_registered_ignores_a_malformed_projects_key(projects: object) -> None:
    """A malformed projects key widens nothing; the park-wake tick names it in the digest."""
    index = parse_registered({"candidates": [], "projects": projects})
    assert index.projects == ()


def test_parse_registered_ignores_intake_and_exclude() -> None:
    """intake / exclude are the wrapper's to read; the enumerator lists, it does not decide."""
    index = parse_registered(
        {"clones": {"p": "/c"}, "intake": {"mode": "on"}, "exclude": ["p/T-a"]}
    )
    assert index.projects == ("p",)
