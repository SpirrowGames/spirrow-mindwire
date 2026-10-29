"""The isolation checks run before every launch (D2'' (a), D2⁗).

**Magickit (D2⁗).** The session may not reach magickit. ADR-2026-06-04-18 makes magickit reachable
from this host without authentication, so the barrier is the OS: the session runs as a dedicated
user and an outbound firewall rule blocks that user from the magickit addresses. Whether the
barrier stands is decided ONLY by auditing the rule statically — never by trying to connect, so
magickit being down neither opens the guard (msg-7429) nor stops the lane (msg-7431). The rule
``MindWire-Operator-Block-Magickit`` must be enabled, Outbound, Block, carry the user's SID in its
LocalUser filter, list every configured magickit address (v4 and v6), and apply in every active
profile, whose firewall must itself be on. Anything missing is ``open``: the lane disables itself.

**GitHub (D2'' (a)).** ``main`` must refuse a direct push: its branch rules must require a pull
request or status checks. Read from ``GET /repos/{repo}/rules/branches/main`` (measured
2026-10-29: the classic ``/protection`` endpoint answers 403 to the loop's token; the rules
endpoint answers ``["deletion", "non_fast_forward", "pull_request"]``). A rule set without either
is ``open``; an API failure is ``undetermined`` — this tick does not dispatch, nothing is disabled.
"""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .config import LaneConfig

OK = "ok"
OPEN = "open"
UNDETERMINED = "undetermined"

#: Branch-rule types that keep a direct push off ``main``.
PROTECTING_RULE_TYPES = frozenset({"pull_request", "required_status_checks"})


@dataclass(frozen=True)
class GuardVerdict:
    status: str
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status == OK


# The snapshot the audit reads. Everything is converted to plain strings in PowerShell so the
# Python side never depends on how a CIM enum serialises.
FIREWALL_SNAPSHOT_PS = r"""
$ErrorActionPreference = 'Stop'
$name = $env:MW_RULE_NAME
$user = $env:MW_RUN_AS_USER
$out = [ordered]@{
  sid = $null; sid_error = $null; rules = @(); profiles = @(); active_profiles = @()
}
try {
  $out.sid = ([System.Security.Principal.NTAccount]$user).Translate(
    [System.Security.Principal.SecurityIdentifier]).Value
} catch { $out.sid_error = $_.Exception.Message }
$rules = @(Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue)
if ($rules.Count -eq 0) {
  $rules = @(Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)
}
foreach ($r in $rules) {
  $sec = $r | Get-NetFirewallSecurityFilter
  $addr = $r | Get-NetFirewallAddressFilter
  $out.rules += [ordered]@{
    enabled = [string]$r.Enabled; direction = [string]$r.Direction; action = [string]$r.Action
    profiles = [string]$r.Profile; local_user = [string]$sec.LocalUser
    remote_addresses = @($addr.RemoteAddress | ForEach-Object { [string]$_ })
  }
}
foreach ($p in @(Get-NetFirewallProfile)) {
  $out.profiles += [ordered]@{ name = [string]$p.Name; enabled = [string]$p.Enabled }
}
foreach ($c in @(Get-NetConnectionProfile -ErrorAction SilentlyContinue)) {
  $cat = [string]$c.NetworkCategory
  if ($cat -eq 'DomainAuthenticated') { $cat = 'Domain' }
  $out.active_profiles += $cat
}
$out | ConvertTo-Json -Depth 5 -Compress
"""


def _covers(remote: Sequence[str], address: str) -> bool:
    try:
        target = ipaddress.ip_address(address)
    except ValueError:
        return address in remote
    for entry in remote:
        try:
            if "-" in entry:
                lo, hi = (ipaddress.ip_address(p.strip()) for p in entry.split("-", 1))
                if lo.version == target.version and lo <= target <= hi:  # type: ignore[operator]
                    return True
            else:
                net = ipaddress.ip_network(entry.strip(), strict=False)
                # A rule lists v4 and v6 together; compare like with like explicitly rather than
                # lean on ``__contains__``'s cross-version behaviour (PR #505 gate).
                if net.version == target.version and target in net:
                    return True
        except (ValueError, TypeError):
            continue
    return False


def _sid_allowed(sddl: str, sid: str) -> bool:
    return re.search(rf"\(A;[^)]*;{re.escape(sid)}\)", sddl, re.IGNORECASE) is not None


def _profile_applies(rule_profiles: str, active: str) -> bool:
    names = {p.strip().casefold() for p in rule_profiles.split(",")}
    return "any" in names or active.casefold() in names


def _rule_problems(
    rule: dict[str, Any], sid: str, addresses: Sequence[str], active: Sequence[str]
) -> list[str]:
    problems: list[str] = []
    if str(rule.get("enabled")) != "True":
        problems.append("rule is not enabled")
    if str(rule.get("direction")) != "Outbound":
        problems.append(f"rule direction is {rule.get('direction')!r}, not Outbound")
    if str(rule.get("action")) != "Block":
        problems.append(f"rule action is {rule.get('action')!r}, not Block")
    if not _sid_allowed(str(rule.get("local_user") or ""), sid):
        problems.append(f"rule LocalUser does not name {sid}")
    remote = [str(r) for r in rule.get("remote_addresses") or []]
    missing = [a for a in addresses if not _covers(remote, a)]
    if missing:
        problems.append(f"rule RemoteAddress lacks {', '.join(missing)}")
    for prof in active:
        if not _profile_applies(str(rule.get("profiles") or ""), prof):
            problems.append(f"rule does not apply to the active {prof} profile")
    return problems


def audit_firewall(snapshot: dict[str, Any], config: LaneConfig) -> GuardVerdict:
    """Pure: judge a firewall snapshot against the configuration (D2⁗)."""
    problems: list[str] = []
    if not config.magickit_addresses:
        problems.append("config lists no magickit_addresses to audit")
    sid = snapshot.get("sid")
    if not isinstance(sid, str) or not sid:
        problems.append(
            f"cannot resolve the SID of {config.run_as_user}: {snapshot.get('sid_error')}"
        )
        return GuardVerdict(OPEN, tuple(problems))
    active = [str(a) for a in snapshot.get("active_profiles") or []]
    if not active:
        problems.append("no active network profile reported")
    enabled = {
        str(p.get("name")).casefold(): str(p.get("enabled")) == "True"
        for p in snapshot.get("profiles") or []
    }
    for prof in active:
        if not enabled.get(prof.casefold(), False):
            problems.append(f"the firewall is off in the active {prof} profile")
    rules = [r for r in snapshot.get("rules") or [] if isinstance(r, dict)]
    if not rules:
        problems.append(f"firewall rule {config.firewall_rule_name!r} does not exist")
    else:
        per_rule = [_rule_problems(r, sid, config.magickit_addresses, active) for r in rules]
        if all(per_rule):
            problems.extend(per_rule[0])
    return GuardVerdict(OPEN if problems else OK, tuple(problems))


Runner = Callable[[Sequence[str], dict[str, str] | None], tuple[int, str, str]]


def run_command(argv: Sequence[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    import os

    full_env = {**os.environ, **(env or {})}
    proc = subprocess.run(
        list(argv), capture_output=True, text=True, encoding="utf-8", env=full_env, timeout=60
    )
    return proc.returncode, proc.stdout, proc.stderr


def firewall_snapshot(config: LaneConfig, run: Runner = run_command) -> dict[str, Any]:
    code, out, err = run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", FIREWALL_SNAPSHOT_PS],
        {"MW_RULE_NAME": config.firewall_rule_name, "MW_RUN_AS_USER": config.run_as_user},
    )
    if code != 0:
        raise RuntimeError(f"firewall snapshot exited {code}: {err.strip()[:300]}")
    data = json.loads(out.strip().splitlines()[-1])
    if not isinstance(data, dict):
        raise RuntimeError("firewall snapshot is not a JSON object")
    return data


def check_firewall(config: LaneConfig, run: Runner = run_command) -> GuardVerdict:
    try:
        snapshot = firewall_snapshot(config, run)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        # The audit itself could not run: the rule was NOT shown to be in place. That is open,
        # not undetermined — it depends on nothing outside this host.
        return GuardVerdict(OPEN, (f"firewall audit could not run: {exc}",))
    return audit_firewall(snapshot, config)


def judge_branch_rules(repo: str, rule_types: Sequence[str]) -> GuardVerdict:
    if PROTECTING_RULE_TYPES.intersection(rule_types):
        return GuardVerdict(OK)
    return GuardVerdict(
        OPEN,
        (
            f"{repo}: main has no pull_request / required_status_checks rule "
            f"(rules: {', '.join(rule_types) or 'none'})",
        ),
    )


def check_github(config: LaneConfig, run: Runner = run_command) -> GuardVerdict:
    if not config.github_repos:
        return GuardVerdict(OPEN, ("config lists no github_repos to check",))
    for repo in config.github_repos:
        try:
            code, out, err = run(
                ["gh", "api", f"repos/{repo}/rules/branches/main", "--jq", "[.[].type]"], None
            )
            types = json.loads(out) if code == 0 else None
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return GuardVerdict(UNDETERMINED, (f"{repo}: branch rules unreadable: {exc}",))
        if code != 0 or not isinstance(types, list):
            return GuardVerdict(
                UNDETERMINED,
                (f"{repo}: branch rules unreadable (exit {code}): {err.strip()[:200]}",),
            )
        verdict = judge_branch_rules(repo, [str(t) for t in types])
        if not verdict.ok:
            return verdict
    return GuardVerdict(OK)


def check_all(config: LaneConfig, run: Runner = run_command) -> GuardVerdict:
    """Firewall first (an ``open`` there disables the lane), then GitHub."""
    firewall = check_firewall(config, run)
    if not firewall.ok:
        return firewall
    return check_github(config, run)


__all__ = [
    "OK",
    "OPEN",
    "UNDETERMINED",
    "GuardVerdict",
    "audit_firewall",
    "check_all",
    "check_firewall",
    "check_github",
    "judge_branch_rules",
]
