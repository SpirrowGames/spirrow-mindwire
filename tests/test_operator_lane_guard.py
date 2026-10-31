"""T-operator-task-lane PR2: the static firewall audit (D2⁗) and the branch-rule check."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

import pytest

from spirrow_mindwire.operator_lane import guard
from spirrow_mindwire.operator_lane.config import LaneConfig

SID = "S-1-5-21-1-2-3-1009"
CONFIG = LaneConfig(
    enabled=True,
    magickit_addresses=("192.0.2.10", "2001:db8::1"),
    github_repos=("SpirrowGames/spirrow-mindwire",),
)
GOOD: dict[str, Any] = {
    "sid": SID,
    "rules": [
        {
            "enabled": "True",
            "direction": "Outbound",
            "action": "Block",
            "profiles": "Any",
            "local_user": f"O:LSD:(A;;CC;;;{SID})",
            "remote_addresses": ["192.0.2.10", "2001:db8::/48"],
        }
    ],
    "profiles": [
        {"name": "Domain", "enabled": "True"},
        {"name": "Private", "enabled": "True"},
        {"name": "Public", "enabled": "True"},
    ],
    "active_profiles": ["Private"],
}


def mutate(path: str, value: Any) -> dict[str, Any]:
    snap = copy.deepcopy(GOOD)
    if path.startswith("rule."):
        snap["rules"][0][path[5:]] = value
    else:
        snap[path] = value
    return snap


def test_complete_rule_passes() -> None:
    assert guard.audit_firewall(GOOD, CONFIG) == guard.GuardVerdict(guard.OK)


@pytest.mark.parametrize(
    ("snapshot", "fragment"),
    [
        (mutate("rules", []), "does not exist"),
        (mutate("rule.enabled", "False"), "not enabled"),
        (mutate("rule.direction", "Inbound"), "not Outbound"),
        (mutate("rule.action", "Allow"), "not Block"),
        (mutate("rule.local_user", "O:LSD:(A;;CC;;;S-1-5-21-9-9-9-1)"), "LocalUser"),
        (mutate("rule.remote_addresses", ["192.0.2.10"]), "2001:db8::1"),
        (mutate("rule.remote_addresses", ["2001:db8::/48"]), "192.0.2.10"),
        (mutate("rule.profiles", "Public"), "Private profile"),
        (
            mutate("profiles", [{"name": "Private", "enabled": "False"}]),
            "firewall is off in the active Private",
        ),
        (mutate("sid", None), "cannot resolve the SID"),
    ],
)
def test_each_missing_piece_opens_the_guard(snapshot: dict[str, Any], fragment: str) -> None:
    verdict = guard.audit_firewall(snapshot, CONFIG)
    assert verdict.status == guard.OPEN
    assert any(fragment in p for p in verdict.problems), verdict.problems


def test_no_configured_addresses_is_open() -> None:
    verdict = guard.audit_firewall(GOOD, LaneConfig(enabled=True))
    assert verdict.status == guard.OPEN


def test_address_ranges_are_understood() -> None:
    snap = mutate("rule.remote_addresses", ["192.0.2.0-192.0.2.255", "2001:db8::1"])
    assert guard.audit_firewall(snap, CONFIG).ok


class FakeRun:
    """Records every command; only the firewall snapshot and ``gh api`` are answered."""

    def __init__(self, gh: tuple[int, str, str], snapshot: dict[str, Any] = GOOD) -> None:
        self.gh = gh
        self.snapshot = snapshot
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], env: dict[str, str] | None) -> tuple[int, str, str]:
        import json

        self.calls.append(list(argv))
        if argv[0] == "powershell":
            return 0, json.dumps(self.snapshot), ""
        if argv[0] == "gh":
            return self.gh
        raise AssertionError(f"unexpected command {argv}")


def test_guard_never_connects_to_magickit() -> None:
    """D2⁗: magickit down or up does not matter — the only commands are the audit and gh."""
    run = FakeRun((0, '["deletion","non_fast_forward","pull_request"]', ""))
    assert guard.check_all(CONFIG, run).ok
    assert {c[0] for c in run.calls} == {"powershell", "gh"}
    assert all("192.0.2.10" not in " ".join(c) for c in run.calls)


def test_main_without_pull_request_rule_opens_the_guard() -> None:
    run = FakeRun((0, '["deletion","non_fast_forward"]', ""))
    verdict = guard.check_all(CONFIG, run)
    assert verdict.status == guard.OPEN
    assert "pull_request" in verdict.problems[0]


def test_unreadable_branch_rules_is_undetermined_not_open() -> None:
    run = FakeRun((1, "", "HTTP 502"))
    assert guard.check_all(CONFIG, run).status == guard.UNDETERMINED


def test_an_audit_that_cannot_run_is_open() -> None:
    def broken(argv: Sequence[str], env: dict[str, str] | None) -> tuple[int, str, str]:
        return 1, "", "Get-NetFirewallRule: access denied"

    assert guard.check_firewall(CONFIG, broken).status == guard.OPEN


@pytest.mark.parametrize(
    "remote",
    [
        ["192.0.2.10/32", "2001:db8::/64"],
        ["2001:db8::/64", "192.0.2.10/32"],
        ["192.0.2.10", "2001:db8::1"],
        ["2001:db8::1", "192.0.2.10"],
        ["192.0.2.0/24", "2001:db8::1-2001:db8::ff"],
    ],
)
def test_mixed_v4_and_v6_entries_never_crash_the_audit(remote: list[str]) -> None:
    """PR #505 gate: the runbook's rule lists v4 and v6 together, so every v4 target meets a v6
    entry and vice versa. Each must be skipped, not raise, in either order."""
    from spirrow_mindwire.operator_lane.guard import _covers

    assert _covers(remote, "192.0.2.10")
    assert not _covers(remote, "198.51.100.1")
    assert not _covers(["2001:db8::/64"], "192.0.2.10")
    assert not _covers(["192.0.2.0/24"], "2001:db8::1")


def test_duplicate_rules_report_every_rules_gaps() -> None:
    """PR #505 gate advisory: two same-named rules, both incomplete — the verdict names the gaps
    of each, not only the first one's; one complete rule among them still passes."""
    snap = copy.deepcopy(GOOD)
    first = dict(snap["rules"][0], enabled="False")
    second = dict(snap["rules"][0], action="Allow")
    snap["rules"] = [first, second]
    verdict = guard.audit_firewall(snap, CONFIG)
    assert verdict.status == guard.OPEN
    text = "\n".join(verdict.problems)
    assert "rule 1 of 2" in text and "rule 2 of 2" in text
    assert len(verdict.problems) >= 2
    snap["rules"] = [first, copy.deepcopy(GOOD["rules"][0])]
    assert guard.audit_firewall(snap, CONFIG).ok
