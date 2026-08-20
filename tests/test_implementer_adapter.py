"""Tests for T19 ``ImplementerSdkAdapter`` + the SDK-tool classifier.

The classifier (SDK tool call → allow-list :class:`Operation`) is the
safety-critical mapping and is tested for the invariants that still hold after
the 2026-08-19 simplification (T-drop-branch-prediction-from-allowlist §3):

* the surviving Tier-C operation ``git.merge_to_main`` is denied on a name
  match alone from every route (``gh pr merge``, MCP ``merge_pull_request``,
  a wrapped `gh pr merge`), regardless of any base argument or ambient HEAD;

* the ex-Tier-C names ``fs.delete`` / ``drive.write`` are **gone from the enum**
  (this is what pins the "dead scaffolding removed" invariant — an enum
  reference would refuse to compile / import). ``external.publish`` was briefly
  removed and RESTORED (msg-1274) — its enum entry is back, and pinning tests
  below make sure the ``gh release`` / ``gh repo delete|archive`` route to it
  is not silently removed again;

* the classifier stops predicting branches: a bare ``git rebase`` / bare
  ``git push`` / chained ``git checkout main && git ...`` classifies to its
  verb (for provenance) without an ``UNKNOWN`` bounce that used to rest on the
  chain-guard;

* the ``gh`` fall-through is default-deny (msg-1300 naysayer follow-up):
  after the explicit routes for ``gh pr merge`` / ``gh pr create`` /
  ``gh release`` / ``gh repo delete|archive`` / ``gh api`` mutation, an
  unrecognised subcommand returns ``UNKNOWN`` unless it appears in an
  explicit read/metadata-mutation whitelist. This closes the
  ``gh repo edit --default-branch <new>`` bypass (which re-targeted the
  ``~DEFAULT_BRANCH`` server ruleset off ``main``). The whitelist covers
  the reads the loop actually needs; adjacent dangerous mutations
  (``gh secret set`` / ``gh gist create`` / ``gh workflow run`` / any
  ``gh repo`` write) are ``UNKNOWN``;

* the built-in tool exposure + isolation settings survive (T37 #1/#4), the
  guard is wired (nothing auto-approved), and the assembled system prompt still
  carries the cwd grounding + injected obligations + ADR-index block.

The predictor-heavy suite (~50 guard/branch/`_current_branch`/`_push_destination`/
chain-guard tests) was retired with the machinery it pinned; the replacement
lives in :mod:`tests.test_preflight` (P0/P1/P2 as the invariants that now
carry "no push to main"). The adapter lifecycle is exercised with a fake SDK
client that drives the ``can_use_tool`` guard, so the fail-loud
allow-list-violation path is covered without the real CLI.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
)

from spirrow_mindwire.adapters.implementer import (
    _BENIGN_BUILTIN_TOOLS,
    _DEFAULT_IMPLEMENTER_SYSTEM_PROMPT,
    _IMPLEMENTER_BUILTIN_TOOLS,
    ImplementerAllowlistError,
    ImplementerSdkAdapter,
    ImplementerSdkDeliveryError,
    ImplementerSdkSpawnError,
    _AllowlistGuard,
    classify_tool_call,
)
from spirrow_mindwire.allowlist import Operation, default_allowlist
from spirrow_mindwire.obligations import load_manifest
from spirrow_mindwire.ports import RoleAdapter, SpawnContext
from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    Event,
    EventType,
    NewMessagePayload,
    ReplyDraft,
    Role,
    SessionState,
    ThreadRef,
)

_TS = datetime(2026, 5, 23, tzinfo=UTC)

# Loop-readable obligations manifest — required by the implementer adapter now
# that the DECLARE-UNREADABLE clause has been MOVED to it (spec/process/README.md).
# Loaded once at import time; the manifest is immutable.
_OBLIGATIONS = load_manifest()


# --------------------------------------------------------------------------- #
# classifier — non-Bash tools
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,inp,expected",
    [
        ("Write", {"file_path": "a.py"}, Operation.FS_WRITE),
        ("Edit", {"file_path": "a.py"}, Operation.FS_WRITE),
        ("MultiEdit", {"file_path": "a.py"}, Operation.FS_WRITE),
        ("NotebookEdit", {"notebook_path": "a.ipynb"}, Operation.FS_WRITE),
        ("Read", {"file_path": "a.py"}, Operation.FS_READ),
        ("Glob", {"pattern": "**"}, Operation.SEARCH),
        ("Grep", {"pattern": "x"}, Operation.SEARCH),
        # T37 #3: benign built-ins (planning + background-shell mgmt) classify to
        # EXEC_CODE (Tier A allow), not UNKNOWN — else they halt the agent's first
        # planning step. Anything with real fs/git/external effect stays explicit.
        ("TodoWrite", {"todos": []}, Operation.EXEC_CODE),
        ("BashOutput", {"bash_id": "1"}, Operation.EXEC_CODE),
        ("KillShell", {"shell_id": "1"}, Operation.EXEC_CODE),
        ("Frobnicate", {}, Operation.UNKNOWN),
    ],
)
def test_classify_simple_tools(name: str, inp: dict[str, Any], expected: Operation) -> None:
    assert classify_tool_call(name, inp).operation is expected


def test_classify_fs_write_carries_path() -> None:
    assert classify_tool_call("Write", {"file_path": "src/x.py"}).path == "src/x.py"


# --------------------------------------------------------------------------- #
# classifier — bash: what still classifies, and what deliberately no longer does
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cmd,expected",
    [
        # Tier A — unremarkable code / git that used to be classified but no longer
        # needs to be because the invariant moved to the server-side ruleset.
        ("pytest -q", Operation.EXEC_CODE),
        ("uv run pytest", Operation.EXEC_CODE),
        ("git status", Operation.EXEC_CODE),
        ("git add -A", Operation.EXEC_CODE),
        ("git checkout -b feature/x", Operation.EXEC_CODE),
        ("git checkout main", Operation.EXEC_CODE),
        ("git pull", Operation.EXEC_CODE),
        # Still classified for provenance — the operation is what it is, even
        # though the branch predicate that used to bound them is retired.
        ("git commit -m msg", Operation.GIT_COMMIT),
        ("git push origin feature/x", Operation.GIT_PUSH),
        ("git push origin main", Operation.GIT_PUSH),
        ("git push --force origin feature/x", Operation.FORCE_PUSH),
        ("git push --force origin main", Operation.FORCE_PUSH),
        ("git push -f origin feature/x", Operation.FORCE_PUSH),
        ("git push --force-with-lease origin feature/x", Operation.FORCE_PUSH),
        ("git merge feature/x", Operation.GIT_MERGE),
        ("git merge develop", Operation.GIT_MERGE),
        ("git rebase -i HEAD~2", Operation.HISTORY_REWRITE),
        ("git reset --hard HEAD", Operation.HISTORY_REWRITE),
        # UNKNOWN — argument shape not one this classifier reads (rev-list
        # expressions). Default-deny catches them.
        ("git filter-branch --tree-filter true HEAD", Operation.UNKNOWN),
        # `gh pr merge` is the sole Tier-C name match — no base/HEAD read.
        ("gh pr merge 5", Operation.GIT_MERGE_TO_MAIN),
        ("gh pr merge 5 --squash", Operation.GIT_MERGE_TO_MAIN),
        ("gh pr create --base develop", Operation.GITHUB_PR_OPEN),
        ("gh pr view 5", Operation.GITHUB_READ),
        # T23: mutating `gh api` → deny (UNKNOWN); a read (GET / no fields) stays read.
        ("gh api repos/o/r/merges -X PUT", Operation.UNKNOWN),
        ("gh api --method POST repos/o/r/pulls -f title=x", Operation.UNKNOWN),
        ("gh api -f base=main repos/o/r/merges", Operation.UNKNOWN),
        ("gh api repos/o/r/contents/x", Operation.GITHUB_READ),
        ("gh api repos/o/r -X GET", Operation.GITHUB_READ),
        # T23 (main SHOULD): direct `-X` with value concatenated (no space).
        ("gh api repos/o/r/merges -XPUT", Operation.UNKNOWN),
        ("gh api repos/o/r -Xget", Operation.GITHUB_READ),
    ],
)
def test_classify_bash(cmd: str, expected: Operation) -> None:
    assert classify_tool_call("Bash", {"command": cmd}).operation is expected


@pytest.mark.parametrize(
    "cmd",
    [
        # `rm` used to route to FS_DELETE; the operation is retired, so these
        # classify to EXEC_CODE now. The invariant they used to pin
        # (delete → denied at this layer) does not hold any more — the module
        # docstring explains that `exec.code` was always the bypass for
        # `fs.delete`. This pins the removal so a future revert would fail
        # here rather than silently re-enable the theatrical check.
        "rm -rf build",
        "rmdir foo",
        "sudo rm -rf /tmp/x",
        "shred secret.pem",
        "Remove-Item -LiteralPath x",
        'bash -c "rm -rf x"',
    ],
)
def test_removed_operations_now_fall_through_to_exec_code(cmd: str) -> None:
    """T-drop-branch-prediction-from-allowlist §3: FS_DELETE is retired.
    The verbs still run — they just do not classify to a Tier-C operation
    any more.
    """
    action = classify_tool_call("Bash", {"command": cmd})
    assert action.operation is Operation.EXEC_CODE, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # `gh release` (ALL subcommands, including reads) routes to
        # EXTERNAL_PUBLISH. Deliberate over-deny on the safe side; the loop
        # has no reason to `gh release list` and the mistake shape "list first,
        # then create" is exactly what a `list → view → create` slip looks like.
        "gh release create v1",
        "gh release create v1 --title x",
        "gh release list",
        "gh release view v1",
        "gh release download v1",
        "gh release delete v1",
        # `gh repo delete|archive` — POST/DELETE/PATCH on api.github.com; squid
        # allows that host through so the network boundary does not catch these.
        "gh repo delete SpirrowGames/x",
        "gh repo archive SpirrowGames/x",
        # Structural `<pkgmgr> publish|push` verbs — retained for parity with
        # the raw-coarse floor, even though their destination is not
        # api.github.com. Ordinary loop-slip verbs the classifier catches
        # cheaply, msg-1274 restoration.
        "npm publish",
        "yarn publish",
        "pnpm publish",
        "poetry publish",
        "cargo publish",
        "twine upload dist/*",
        "gem push mygem.gem",
        "docker push myimg",
        # Wrapped variants: T23 direct == wrapped. Structural recursion handles
        # tokenizable forms; the raw-coarse floor covers the tokenizer-defeating
        # ones. Both routes must reach EXTERNAL_PUBLISH.
        'bash -c "gh release create v1"',
        "eval 'gh repo delete SpirrowGames/x'",
        'bash -c "npm publish"',
    ],
)
def test_publish_routes_to_external_publish(cmd: str) -> None:
    """msg-1274 restoration: `gh release` / `gh repo delete|archive` / `<pkgmgr>
    publish|push` classify to EXTERNAL_PUBLISH (Tier C).

    msg-1273 §2 discipline: never invoke the forbidden operation to verify —
    call the classifier directly. If a future edit removes the routing, THIS
    test reddens; the loop is never at risk of executing `gh release create`
    to check that it is denied.
    """
    action = classify_tool_call("Bash", {"command": cmd})
    assert action.operation is Operation.EXTERNAL_PUBLISH, cmd


def test_tokenizer_defeating_publish_still_falls_closed() -> None:
    """The raw-coarse floor catches a `gh release` smuggled past shlex.

    ANSI-C `$'...'` quoting produces a byte sequence shlex cannot re-parse into
    tokens, so the structural pass sees nothing to classify. The floor scans
    the RAW string with `_INDIRECTION_RE` as its gate, so a wrapped tokenizer-
    defeat still hits EXTERNAL_PUBLISH — direct == wrapped (T23).
    """
    cmd = "bash -c $'gh release create v1 --title x'"
    action = classify_tool_call("Bash", {"command": cmd})
    assert action.operation is Operation.EXTERNAL_PUBLISH


# --------------------------------------------------------------------------- #
# msg-1300 naysayer follow-up — gh fall-through default-deny + read whitelist
# --------------------------------------------------------------------------- #
#
# Before msg-1300, `_classify_gh` fell through to `GITHUB_READ` for any
# subcommand it did not explicitly recognise. `gh repo edit --default-branch
# <new>` was the specific bypass named — it re-targets the `~DEFAULT_BRANCH`
# GitHub ruleset off `main`, then an unrestricted subsequent `git push
# --force main` succeeds. The fix inverts the fall-through: an explicit
# whitelist of loop-safe reads / metadata mutations returns `GITHUB_READ`;
# anything else returns `UNKNOWN` (default-deny).
#
# msg-1273 §2 discipline: these tests never invoke `gh` — they call the
# classifier directly and assert the resulting `Operation`. A regression
# that re-enabled a bypass reddens this test file, not a live gh call.


@pytest.mark.parametrize(
    "cmd",
    [
        # THE naysayer-named vector — moves the default branch away from
        # `main`, stripping the ~DEFAULT_BRANCH ruleset off it. Now UNKNOWN.
        "gh repo edit --default-branch staging",
        "gh repo edit SpirrowGames/spirrow-mindwire --default-branch staging",
        # Bare `gh repo edit` even without dangerous flags — the classifier
        # cannot inspect flags for repo-level mutations, so all `gh repo edit`
        # invocations are UNKNOWN. If a future task legitimately needs one
        # variant, it must be added to the whitelist explicitly.
        "gh repo edit",
        # Other repo-level mutations that were also silently allowed under
        # the old fall-through — all now UNKNOWN.
        "gh repo rename new-name",
        "gh repo transfer other-owner",
        "gh repo sync",
        "gh repo unarchive SpirrowGames/x",
        "gh repo create new-repo",
        "gh repo fork SpirrowGames/x",
        "gh repo set-default owner/repo",
        # Actions secrets / variables — writing these could wire in a
        # backdoor for subsequent CI runs.
        "gh secret set MY_TOKEN",
        "gh secret remove MY_TOKEN",
        "gh variable set MY_VAR",
        # `gh gist create` publishes text to the internet — a real publish
        # path adjacent to what `EXTERNAL_PUBLISH` covers. Kept as UNKNOWN
        # rather than routed to EXTERNAL_PUBLISH to keep the routing surface
        # small (gist is a rarer vector than gh release).
        "gh gist create secret.txt",
        "gh gist edit abc123",
        "gh gist delete abc123",
        # Workflow trigger — burns CI resources and can ship code by
        # dispatching a release-workflow.
        "gh workflow run release.yml",
        "gh workflow enable deploy.yml",
        "gh workflow disable deploy.yml",
        # Run rerun / cancel — mutations to CI state.
        "gh run rerun 12345",
        "gh run cancel 12345",
        # Label / cache writes.
        "gh label create bug --color ff0000",
        "gh label delete bug",
        "gh cache delete abc",
        # Extension install — arbitrary code load.
        "gh extension install some-untrusted/extension",
        # Codespace create/delete — mutations.
        "gh codespace create --repo owner/repo",
        "gh codespace delete abc",
        # Unrecognised group (future GitHub subcommand) — default-deny.
        "gh some-new-command",
        "gh some-new-command with args",
    ],
)
def test_gh_unrecognised_or_dangerous_mutation_is_unknown(cmd: str) -> None:
    """msg-1300 fix — the default-deny fall-through catches unlisted `gh`
    subcommands. The naysayer-named `gh repo edit --default-branch` vector
    is here; adjacent repo/secret/gist/workflow mutations are also caught.
    """
    action = classify_tool_call("Bash", {"command": cmd})
    assert action.operation is Operation.UNKNOWN, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # The reads the loop legitimately uses — all whitelisted.
        "gh pr view 5",
        "gh pr list",
        "gh pr diff 5",
        "gh pr checks 5",
        "gh pr status",
        # PR-level metadata mutations the loop uses (body-file updates,
        # comments) — safe because the loop already owns the PR via the
        # Tier-A `gh pr create` route.
        "gh pr edit 5 --body-file /tmp/body.md",
        "gh pr comment 5 --body-file /tmp/comment.md",
        "gh pr close 5",
        "gh pr reopen 5",
        "gh pr ready 5",
        # Issue-side symmetric.
        "gh issue view 12",
        "gh issue list",
        "gh issue edit 12 --title x",
        "gh issue comment 12 --body y",
        # Repo READS only.
        "gh repo view SpirrowGames/x",
        "gh repo list SpirrowGames",
        "gh repo clone SpirrowGames/x",
        # CI reads.
        "gh run view 12345",
        "gh run list",
        "gh run watch 12345",
        "gh workflow view deploy.yml",
        "gh workflow list",
        # Auth reads.
        "gh auth status",
        "gh auth token",
        # Search.
        "gh search prs is:open",
        "gh search repos SpirrowGames",
        # Gist reads.
        "gh gist view abc123",
        "gh gist list",
        # Label / config / alias reads.
        "gh label list",
        "gh config get editor",
        "gh alias list",
        "gh secret list",
        "gh variable list",
        # No-sub / help — pass.
        "gh",
        "gh --version",
        "gh --help",
        "gh pr",
        "gh version",
        # gh api reads (mutations already routed to UNKNOWN by the earlier
        # explicit check — that path is covered in test_classify_bash above).
        "gh api repos/o/r",
        "gh api /user",
    ],
)
def test_gh_loop_safe_reads_and_metadata_mutations_pass(cmd: str) -> None:
    """The whitelist covers the reads (and PR-level metadata mutations) the
    loop actually needs. If a future edit narrows the whitelist too far,
    THIS test reddens BEFORE the loop deadlocks on its own workflow.
    """
    action = classify_tool_call("Bash", {"command": cmd})
    assert action.operation is Operation.GITHUB_READ, cmd


def test_gh_repo_edit_default_branch_still_denied_via_gh_api(tmp_path: Path) -> None:
    """The equivalent `gh api` PATCH is caught by the existing mutating-api
    route (already-existing UNKNOWN). This pins that BOTH the friendly
    `gh repo edit --default-branch` vector AND the raw `gh api` vector are
    default-deny — closing the shape symmetrically.
    """
    del tmp_path
    action = classify_tool_call(
        "Bash",
        {
            "command": (
                "gh api repos/SpirrowGames/spirrow-mindwire -X PATCH -f default_branch=staging"
            )
        },
    )
    assert action.operation is Operation.UNKNOWN


def test_operation_enum_no_longer_carries_removed_names() -> None:
    """FS_DELETE / DRIVE_WRITE must not be present on the enum.

    This is the load-time counterpart to the classifier tests above: if the
    enum still had them, a future patch could quietly re-route. Removing them
    from the enum makes any reintroduction a compile-time / import-time break.
    EXTERNAL_PUBLISH is NOT on this list — it was restored by msg-1274; its
    routing is pinned by `test_publish_routes_to_external_publish` above.
    """
    names = {op.name for op in Operation}
    assert "FS_DELETE" not in names
    assert "DRIVE_WRITE" not in names
    # Positive assertion for the restored member — the mirror invariant of the
    # two removals above.
    assert "EXTERNAL_PUBLISH" in names


def test_default_allowlist_forbids_external_publish(tmp_path: Path) -> None:
    """The packaged YAML lists external.publish as forbidden (Tier C).

    Complements the classifier test: the classifier CAN name EXTERNAL_PUBLISH,
    and the YAML denies it. If either half is missing, the guardrail is a no-op.
    Uses `Allowlist.check` on a synthesized ClassifiedAction — never runs the
    forbidden verb (msg-1273 §2).
    """
    from spirrow_mindwire.allowlist import ClassifiedAction

    allowlist = default_allowlist(repo_root=tmp_path)
    action = ClassifiedAction(operation=Operation.EXTERNAL_PUBLISH, detail="gh release create v1")
    decision = allowlist.check(action)
    assert decision.allowed is False
    assert decision.operation is Operation.EXTERNAL_PUBLISH


def test_default_allowlist_forbids_git_merge_to_main(tmp_path: Path) -> None:
    """Parallel positive assertion for the other surviving Tier-C guardrail."""
    from spirrow_mindwire.allowlist import ClassifiedAction

    allowlist = default_allowlist(repo_root=tmp_path)
    action = ClassifiedAction(operation=Operation.GIT_MERGE_TO_MAIN, detail="gh pr merge 5")
    decision = allowlist.check(action)
    assert decision.allowed is False
    assert decision.operation is Operation.GIT_MERGE_TO_MAIN


# --------------------------------------------------------------------------- #
# T27: indirection is unified via recursion (direct == wrapped) — narrowed to
# what still classifies to something after the retirements.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cmd,expected",
    [
        # Wrapping does not change the classification (T27 invariant).
        ("eval \"bash -c 'git push --force origin feature/x'\"", Operation.FORCE_PUSH),
        ("echo $(git push --force origin feature/x)", Operation.FORCE_PUSH),
        # `gh pr merge` wrapped still routes to GIT_MERGE_TO_MAIN. This is the
        # anchor of the surviving Tier-C guarantee under wrapping.
        ('bash -c "gh pr merge 5"', Operation.GIT_MERGE_TO_MAIN),
        ("eval 'gh pr merge 5 --squash'", Operation.GIT_MERGE_TO_MAIN),
        # gh api precision now comes from recursion (single source), not a regex
        # mirror: a wrapped field-flag gh api (gh defaults to POST) still denies.
        ('bash -c "gh api repos/o/r/pulls --field title=x"', Operation.UNKNOWN),
        # legit wrapped commands stay allowed (EXEC_CODE) — not over-denied.
        ('bash -c "pytest -q && echo done"', Operation.EXEC_CODE),
        ("sh -c 'uv run mypy src'", Operation.EXEC_CODE),
        # `bash script.sh` runs a file (not -c) → not inline indirection.
        ("bash deploy.sh", Operation.EXEC_CODE),
        # backticks: closed pair, and an unclosed trailing backtick (deny-safe).
        ("echo `git push --force origin feature/x` done", Operation.FORCE_PUSH),
        ("echo `gh pr merge 5", Operation.GIT_MERGE_TO_MAIN),
    ],
)
def test_classify_bash_indirection_recursed(cmd: str, expected: Operation) -> None:
    assert classify_tool_call("Bash", {"command": cmd}).operation is expected


@pytest.mark.parametrize(
    "inner",
    [
        "git push --force origin feature/x",
        "git reset --hard HEAD~3",
        "gh api repos/o/r/merges -X PUT",
        "gh api repos/o/r/pulls -f title=x",
        "gh pr merge 5",
        "pytest -q",
        "git status",
    ],
)
def test_classify_direct_equals_wrapped(inner: str) -> None:
    # The T27 invariant: wrapping a command in `bash -c "..."` must not change its
    # classification — direct and indirection share one classifier (no drift).
    direct = classify_tool_call("Bash", {"command": inner}).operation
    wrapped = classify_tool_call("Bash", {"command": f'bash -c "{inner}"'}).operation
    assert direct == wrapped


def test_classify_bash_nesting_depth_fails_closed() -> None:
    # Pathologically nested indirection exceeds the recursion bound → fail closed
    # (UNKNOWN → deny) rather than spin.
    cmd = "$(" * 10 + "git status" + ")" * 10
    assert classify_tool_call("Bash", {"command": cmd}).operation is Operation.UNKNOWN


@pytest.mark.parametrize(
    "cmd,expected",
    [
        # #74 naysayer MUST-1 / Copilot: a launcher before `bash -c` must not hide
        # the inner from the structural classifier.
        ('exec bash -c "gh api repos/o/r/merges -f base=main"', Operation.UNKNOWN),
        ('env bash -c "gh api repos/o/r/merges -f base=main"', Operation.UNKNOWN),
        ('timeout 5 bash -c "gh api repos/o/r/merges -f base=main"', Operation.UNKNOWN),
        ('nice -n 10 bash -c "git push --force origin feature/x"', Operation.FORCE_PUSH),
        # value-taking bash OPTIONS hide `-c` (--rcfile F / -O shopt): skip the arg.
        ('bash --rcfile myrc -c "gh pr merge 5"', Operation.GIT_MERGE_TO_MAIN),
        ('bash -O extglob -c "gh pr merge 5"', Operation.GIT_MERGE_TO_MAIN),
        # value-taking option used WITHOUT its argument.
        ('bash -O -c "gh pr merge 5"', Operation.GIT_MERGE_TO_MAIN),
        ('bash --rcfile -c "gh pr merge 5"', Operation.GIT_MERGE_TO_MAIN),
        # control: option WITH its argument still parses as before (no false deny).
        ('bash -O extglob -c "echo hi"', Operation.EXEC_CODE),
        # must NOT over-deny: a launcher/shell name as a mere argument isn't a wrapper.
        ('echo bash -c "hello world"', Operation.EXEC_CODE),
    ],
)
def test_classify_bash_launcher_and_option_wrappers(cmd: str, expected: Operation) -> None:
    assert classify_tool_call("Bash", {"command": cmd}).operation is expected


def test_classify_push_target_and_force_still_populated() -> None:
    # Even though the branch is no longer consulted for enforcement, the
    # classifier still surfaces it in the ClassifiedAction for provenance
    # (denial records / log lines).
    a = classify_tool_call("Bash", {"command": "git push origin main"})
    assert a.operation is Operation.GIT_PUSH
    assert a.branch == "main"
    assert a.force is False

    b = classify_tool_call("Bash", {"command": "git push --force origin feature/x"})
    assert b.operation is Operation.FORCE_PUSH
    assert b.branch == "feature/x"
    assert b.force is True


def test_classify_merge_source_extracted() -> None:
    a = classify_tool_call("Bash", {"command": "git merge feature/x"})
    assert a.operation is Operation.GIT_MERGE
    assert a.source == "feature/x"


def test_classify_env_prefix_force_push() -> None:
    a = classify_tool_call("Bash", {"command": "FOO=bar git push --force origin feature/x"})
    assert a.operation is Operation.FORCE_PUSH


# --------------------------------------------------------------------------- #
# classifier — MCP
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,inp,expected",
    [
        # The sole Tier-C route in MCP too — name match, no base read.
        ("mcp__github__merge_pull_request", {}, Operation.GIT_MERGE_TO_MAIN),
        ("mcp__github__create_pull_request", {"base": "develop"}, Operation.GITHUB_PR_OPEN),
        ("mcp__github__create_or_update_file", {"branch": "feature/x"}, Operation.GIT_PUSH),
        ("mcp__github__push_files", {"branch": "feature/x"}, Operation.GIT_PUSH),
        ("mcp__github__get_file_contents", {}, Operation.GITHUB_READ),
        # T-drop-branch-prediction-from-allowlist §3: `delete_*` MCP routing is
        # gone (fs.delete retired), `smart_*_document` / `drive*` routing is
        # gone (drive.write retired). Any of those now falls to UNKNOWN (default
        # -deny) via the positive read whitelist, which is enough — the loop
        # runs with zero MCP tools anyway.
        ("mcp__github__delete_file", {}, Operation.UNKNOWN),
        ("mcp__github__list_and_delete", {}, Operation.UNKNOWN),
        ("mcp__drive__smart_update_document", {}, Operation.UNKNOWN),
        # unknown read-ish → default-deny (positive whitelist).
        ("mcp__weird__frobnicate", {}, Operation.UNKNOWN),
        ("mcp__github__list_widgets", {}, Operation.UNKNOWN),
    ],
)
def test_classify_mcp(name: str, inp: dict[str, Any], expected: Operation) -> None:
    assert classify_tool_call(name, inp).operation is expected


# --------------------------------------------------------------------------- #
# End-to-end: the sole surviving Tier-C deny (name-match, base-independent)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr merge 5",
        "gh pr merge 5 --squash",
        "gh pr merge 5 --squash --delete-branch",
        'bash -c "gh pr merge 5"',
        "eval 'gh pr merge 5'",
    ],
)
def test_gh_pr_merge_denied_end_to_end(tmp_path: Path, cmd: str) -> None:
    """`gh pr merge` is the sole surviving classifier-executed Tier-C guarantee.

    Denied on a name match alone (no base argument inspection, no HEAD read),
    including under indirection. This is the invariant that stays after the
    2026-08-19 simplification because the GitHub server-side push rejection
    covers pushes, not the merge API, and covers SpirrowGames repos only.
    """
    al = default_allowlist(repo_root=tmp_path)
    action = classify_tool_call("Bash", {"command": cmd})
    assert action.operation is Operation.GIT_MERGE_TO_MAIN, cmd
    d = al.check(action)
    assert d.allowed is False, cmd


def test_mcp_merge_pull_request_denied_end_to_end(tmp_path: Path) -> None:
    al = default_allowlist(repo_root=tmp_path)
    action = classify_tool_call("mcp__github__merge_pull_request", {"pull_number": 5})
    assert action.operation is Operation.GIT_MERGE_TO_MAIN
    assert al.check(action).allowed is False


def test_repo_internal_write_allowed_end_to_end(tmp_path: Path) -> None:
    """fs.write is now unconstrained Tier A (path_glob retired 2026-08-19)."""
    al = default_allowlist(repo_root=tmp_path)
    action = classify_tool_call("Write", {"file_path": str(tmp_path / "src" / "x.py")})
    assert al.check(action).allowed is True


def test_os_temp_write_now_allowed_at_this_layer(tmp_path: Path) -> None:
    """The scratch-write halt moved out of this layer on 2026-08-19.

    fs.write is unconstrained here now. The residual guidance ("don't write to
    OS temp, use <repo>/.git/mindwire-scratch/") lives in the implementer's
    system prompt (the only text the loop reads at runtime — the yaml comment
    at the fs.write rule cross-refs this).
    """
    import tempfile

    al = default_allowlist(repo_root=tmp_path)
    action = classify_tool_call(
        "Write", {"file_path": str(Path(tempfile.gettempdir()) / "pr_body.md")}
    )
    assert al.check(action).allowed is True


# --------------------------------------------------------------------------- #
# guard
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_guard_allows_exec(tmp_path: Path) -> None:
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "pytest"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow)
    assert guard.violations == []


@pytest.mark.anyio
async def test_guard_denies_gh_pr_merge_with_interrupt(tmp_path: Path) -> None:
    """The one Tier-C route the guard still refuses at the loop level."""
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "gh pr merge 5 --squash"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)
    assert res.interrupt is True
    assert len(guard.violations) == 1
    assert guard.violations[0].operation is Operation.GIT_MERGE_TO_MAIN
    assert guard.violation_actions[0].operation is Operation.GIT_MERGE_TO_MAIN


@pytest.mark.anyio
async def test_guard_allows_bare_force_push_now(tmp_path: Path) -> None:
    """force_push is unconstrained Tier A after 2026-08-19.

    The `main` guarantee moved to GitHub's org ruleset + composition-root
    preflight (P0/P1/P2); the classifier no longer answers "which branch".
    So a bare `git push --force origin main` classifies to FORCE_PUSH and the
    allow-list permits it — GitHub then rejects the push server-side.
    """
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git push --force origin main"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow)


@pytest.mark.anyio
async def test_guard_allows_bare_rebase_now(tmp_path: Path) -> None:
    """history_rewrite is unconstrained Tier A after 2026-08-19 (same rationale)."""
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git rebase -i HEAD~3"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow)


@pytest.mark.anyio
async def test_guard_allows_local_checkout_main_and_merge(tmp_path: Path) -> None:
    """The "checkout main + merge" chain used to be lifted to GIT_MERGE_TO_MAIN
    by a chain-guard special case. That special case was removed on 2026-08-19
    (msg-1272 Q1 answer (b)): local merge in a captive clone burns the clone,
    reflog restores, and the GitHub server rejects any push that would export
    the damage. So the local chain is now allow at this layer.
    """
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard(
        "Bash", {"command": "git checkout main && git merge feature/x"}, ToolPermissionContext()
    )
    assert isinstance(res, PermissionResultAllow)


@pytest.mark.anyio
async def test_guard_denies_gh_pr_merge_wrapped(tmp_path: Path) -> None:
    """Same-verdict-under-wrapping still holds for the surviving Tier-C op."""
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": 'bash -c "gh pr merge 5"'}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)
    assert res.interrupt is True


# --------------------------------------------------------------------------- #
# adapter lifecycle
# --------------------------------------------------------------------------- #


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="test", parent_tool_use_id=None)


def _result(is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="t",
        stop_reason="end_turn",
        result="ok",
    )


class _FakeSdkClient:
    """Structural stand-in that can drive the options' can_use_tool guard."""

    def __init__(
        self,
        options: Any,
        *,
        responses: list[Any],
        simulate_tool: tuple[str, dict[str, Any]] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.options = options
        self._can_use_tool = options.can_use_tool
        self._responses = responses
        self._simulate_tool = simulate_tool
        self._fail_on = fail_on
        self.connected = False
        self.disconnected = False
        self.interrupt_count = 0
        self.queries: list[str] = []

    async def connect(self) -> None:
        if self._fail_on == "connect":
            raise RuntimeError("connect boom")
        self.connected = True

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        if self._simulate_tool is not None:
            name, inp = self._simulate_tool
            await self._can_use_tool(name, inp, ToolPermissionContext())

    async def receive_response(self) -> AsyncIterator[Any]:
        for message in self._responses:
            yield message

    async def interrupt(self) -> None:
        self.interrupt_count += 1

    async def disconnect(self) -> None:
        self.disconnected = True


def _factory(
    *,
    responses: list[Any],
    simulate_tool: tuple[str, dict[str, Any]] | None = None,
    fail_on: str | None = None,
    capture: list[_FakeSdkClient] | None = None,
) -> Callable[[Any], _FakeSdkClient]:
    def make(options: Any) -> _FakeSdkClient:
        client = _FakeSdkClient(
            options, responses=responses, simulate_tool=simulate_tool, fail_on=fail_on
        )
        if capture is not None:
            capture.append(client)
        return client

    return make


def _thread_ref() -> ThreadRef:
    return ThreadRef(project_id="spirrow-mindwire", thread_id="01JTHREAD", chatroom_uri="mc://t/1")


def _ctx(captured: list[ReplyDraft]) -> SpawnContext:
    async def on_reply(draft: ReplyDraft) -> None:
        captured.append(draft)

    async def on_event_log(_event: Event) -> None:
        return None

    return SpawnContext(
        on_reply=on_reply,
        on_event_log=on_event_log,
        own_role=Role.IMPLEMENTER,
        own_instance_id="implementer-1",
    )


def _event(
    *, author: str = "human", body: str = "do it", event_type: EventType = EventType.NEW_MESSAGE
) -> ChatroomEvent:
    return ChatroomEvent(
        event_id="01JEVENT",
        event_type=event_type,
        thread_ref=_thread_ref(),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id="m1", author=author, body=body, parent_msg_id=None),
    )


def _init_head(repo_root: Path, branch: str | None) -> None:
    """Initialise a git repo (only needed by the manual smoke tests now)."""
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", str(repo_root)],
        check=True,
        capture_output=True,
    )
    if branch is not None and branch != "main":
        subprocess.run(
            ["git", "-C", str(repo_root), "checkout", "-q", "-b", branch],
            check=True,
            capture_output=True,
        )


def test_capabilities_execute_code_not_naysayer() -> None:
    caps = ImplementerSdkAdapter.capabilities
    assert Capability.EXECUTE_CODE in caps
    assert Capability.NAYSAYER_QUALIFIED not in caps


def test_satisfies_roleadapter_protocol(tmp_path: Path) -> None:
    adapter: RoleAdapter = ImplementerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url="http://lx"
    )
    assert adapter.adapter_id == "implementer-sdk"


@pytest.mark.anyio
async def test_spawn_requires_inference_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MINDWIRE_IMPLEMENTER_BASE_URL", raising=False)
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, client_factory=_factory(responses=[])
    )
    with pytest.raises(ImplementerSdkSpawnError):
        await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))


@pytest.mark.anyio
async def test_spawn_routes_inference_via_base_url(tmp_path: Path) -> None:
    cap: list[_FakeSdkClient] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lexora:8110",
        client_factory=_factory(responses=[], capture=cap),
    )
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    opts = cap[0].options
    # never api.anthropic.com directly: ANTHROPIC_BASE_URL pinned to Lexora.
    assert opts.env["ANTHROPIC_BASE_URL"] == "http://lexora:8110"
    assert opts.can_use_tool is not None  # the allow-list guard is wired in
    assert opts.permission_mode == "default"  # NOT bypassPermissions
    assert handle.role is Role.IMPLEMENTER


@pytest.mark.anyio
async def test_make_options_exposes_builtins_and_isolates(tmp_path: Path) -> None:
    # T37: regression guard for the four wiring fixes. Pin the exposure +
    # isolation + UTF-8 + guard wiring against silent drift.
    cap: list[_FakeSdkClient] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(responses=[], capture=cap),
    )
    await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    opts = cap[0].options
    # #1 built-ins exposed (NOT tools=[]): the core code/fs/exec/search tools.
    assert isinstance(opts.tools, list)
    assert opts.tools, "tools must not be empty (else no built-ins)"
    for tool in ("Read", "Write", "Edit", "Bash", "Glob", "Grep", "TodoWrite"):
        assert tool in opts.tools
    # the guard is still the single enforcement point — nothing auto-approved.
    assert opts.allowed_tools == []
    assert opts.can_use_tool is not None
    assert opts.permission_mode == "default"
    # #4 isolation: no host settings inherited; only explicit MCP servers honored.
    assert opts.setting_sources == []
    assert opts.strict_mcp_config is True
    # #2 UTF-8 forced for the subprocess + any python the agent spawns.
    assert opts.env["PYTHONUTF8"] == "1"
    assert opts.env["PYTHONIOENCODING"] == "utf-8"
    # T40: the cwd grounding reaches the SDK system prompt.
    assert str(tmp_path) in opts.system_prompt


def test_system_prompt_grounds_cwd(tmp_path: Path) -> None:
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url="http://lx"
    )
    sp = adapter._system_prompt
    assert str(tmp_path) in sp
    assert "WORKING DIRECTORY" in sp
    assert "relative path" in sp
    # grounding is appended, not a replacement — the role handoff guidance is preserved.
    assert "Conductor handoff protocol" in sp


def test_system_prompt_forbids_reconstructing_unreadable_documents(tmp_path: Path) -> None:
    sp = ImplementerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url="http://lx"
    )._system_prompt
    assert "DOCUMENTS YOU CANNOT READ" in sp
    assert "cannot read" in sp
    assert "do NOT reconstruct" in sp
    assert "[OBL-DECLARE-UNREADABLE]" in sp


def test_system_prompt_carries_the_adr_index_as_titles_only(tmp_path: Path) -> None:
    sp = ImplementerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url="http://lx"
    )._system_prompt
    assert "ADR INDEX" in sp
    assert "ADR-2026-05-29-13" in sp
    assert "TITLES ONLY" in sp
    assert "NOT the ADRs" in sp


def test_adr_index_block_says_so_when_the_manifest_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing manifest is announced, never shipped as a silent gap."""
    import spirrow_mindwire.adapters.implementer as impl

    monkeypatch.setattr(impl, "load_adr_index", lambda *a, **k: ())
    block = impl._adr_index_block()
    assert "UNAVAILABLE" in block
    assert "do not guess" in block


@pytest.mark.anyio
async def test_deliver_emits_reply_when_allowed(tmp_path: Path) -> None:
    captured: list[ReplyDraft] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(
            responses=[_assistant("done"), _result()],
            simulate_tool=("Bash", {"command": "pytest -q"}),
        ),
    )
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx(captured))
    await adapter.deliver_event(handle, _event(author="human"))
    assert len(captured) == 1
    assert captured[0].body == "done"
    assert (await adapter.health(handle)).state is SessionState.IDLE


@pytest.mark.anyio
async def test_deliver_allowlist_violation_fails_loud(tmp_path: Path) -> None:
    """`gh pr merge` is the tool call that trips the sole surviving Tier-C rule."""
    captured: list[ReplyDraft] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(
            responses=[_assistant("ignored"), _result()],
            simulate_tool=("Bash", {"command": "gh pr merge 5"}),
        ),
    )
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx(captured))
    with pytest.raises(ImplementerAllowlistError):
        await adapter.deliver_event(handle, _event(author="human"))
    assert captured == []  # fail-loud: no reply posted
    hs = await adapter.health(handle)
    assert hs.state is SessionState.FAILED
    assert hs.error is not None
    assert hs.error.code == "adapter.allowlist_violation"


@pytest.mark.anyio
async def test_own_role_self_filter(tmp_path: Path) -> None:
    captured: list[ReplyDraft] = []
    cap: list[_FakeSdkClient] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(responses=[_assistant("x"), _result()], capture=cap),
    )
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx(captured))
    # I3 v2.2: the self-filter keys on instance_id ("implementer-1"), not the bare role.
    await adapter.deliver_event(handle, _event(author="implementer-1"))
    assert captured == []
    assert cap[0].queries == []


@pytest.mark.anyio
async def test_non_new_message_is_noop(tmp_path: Path) -> None:
    captured: list[ReplyDraft] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(responses=[_assistant("x"), _result()]),
    )
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx(captured))
    await adapter.deliver_event(handle, _event(event_type=EventType.THREAD_CLOSED))
    assert captured == []


@pytest.mark.anyio
async def test_halt_disconnects_and_is_idempotent(tmp_path: Path) -> None:
    cap: list[_FakeSdkClient] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(responses=[], capture=cap),
    )
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    await adapter.halt(handle)
    assert cap[0].disconnected is True
    assert (await adapter.health(handle)).state is SessionState.HALTED
    await adapter.halt(handle)  # idempotent no-op
    assert cap[0].interrupt_count == 1


@pytest.mark.anyio
async def test_deliver_on_halted_session_raises(tmp_path: Path) -> None:
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(responses=[]),
    )
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    await adapter.halt(handle)
    with pytest.raises(ImplementerSdkDeliveryError):
        await adapter.deliver_event(handle, _event())


def test_builtin_tool_set_is_self_consistent() -> None:
    # T37: the benign-whitelist set must be a subset of the exposed tools, and
    # the core code/fs tools the implementer's job needs must actually be exposed.
    exposed = set(_IMPLEMENTER_BUILTIN_TOOLS)
    assert exposed >= _BENIGN_BUILTIN_TOOLS
    assert exposed >= {"Read", "Write", "Edit", "Bash", "Glob", "Grep"}
    for tool in exposed:
        assert classify_tool_call(tool, {}).operation is not Operation.UNKNOWN


# --------------------------------------------------------------------------- #
# manual SDK smoke (B3): verify the REAL SDK routes tool calls through the guard
# --------------------------------------------------------------------------- #


@pytest.mark.manual
@pytest.mark.anyio
async def test_manual_sdk_routes_tool_calls_through_guard(tmp_path: Path) -> None:
    """The whole gate rests on the real SDK calling can_use_tool for every tool.

    Now that fs.delete is retired, the smoke test uses `gh pr merge` — the sole
    surviving Tier-C route the guard denies. Run with a working Lexora endpoint:

        MINDWIRE_IMPLEMENTER_BASE_URL=... uv run pytest -m manual -k manual_sdk

    Asks the model to run the forbidden command and asserts the guard denied it.
    """
    base = os.environ.get("MINDWIRE_IMPLEMENTER_BASE_URL")
    if not base:
        pytest.skip("set MINDWIRE_IMPLEMENTER_BASE_URL (Lexora Anthropic-compat) to run")
    _init_head(tmp_path, "feature/manual-smoke")
    adapter = ImplementerSdkAdapter(cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url=base)
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    try:
        with pytest.raises(ImplementerAllowlistError):
            await adapter.deliver_event(
                handle,
                _event(body="Run exactly this shell command and nothing else: gh pr merge 999999"),
            )
        assert (await adapter.health(handle)).state is SessionState.FAILED
    finally:
        await adapter.halt(handle)


@pytest.mark.manual
@pytest.mark.anyio
async def test_manual_sdk_executes_allowed_tool_through_guard(tmp_path: Path) -> None:
    """The other half of the gate: an ALLOWED tool must actually EXECUTE (T37 #1)."""
    base = os.environ.get("MINDWIRE_IMPLEMENTER_BASE_URL")
    if not base:
        pytest.skip("set MINDWIRE_IMPLEMENTER_BASE_URL (Lexora Anthropic-compat) to run")
    _init_head(tmp_path, "feature/manual-exec")
    captured: list[ReplyDraft] = []
    adapter = ImplementerSdkAdapter(cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url=base)
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx(captured))
    marker = tmp_path / "t37_exec_proof.txt"
    try:
        await adapter.deliver_event(
            handle,
            _event(
                body=(
                    "Create a file named t37_exec_proof.txt in the current directory "
                    "containing exactly the text OK, using the Write tool. Then reply done."
                ),
            ),
        )
        assert marker.is_file(), "the allowed Write tool did not execute (tools likely still empty)"
        assert (await adapter.health(handle)).state is SessionState.IDLE
        assert captured, "expected a reply after the tool executed"
    finally:
        await adapter.halt(handle)


def test_default_system_prompt_includes_implementer_handoff_protocol() -> None:
    # PR-2b-1: the implementer ends every reply with a NEXT: line.
    assert "Conductor handoff protocol" in _DEFAULT_IMPLEMENTER_SYSTEM_PROMPT
    assert "NEXT:" in _DEFAULT_IMPLEMENTER_SYSTEM_PROMPT
