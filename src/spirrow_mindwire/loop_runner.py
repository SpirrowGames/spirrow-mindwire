"""Stage 3 autonomous-loop daemon (``mindwire-loop``) — ADR-2026-05-21-06 §4/§7.

The production console-script that assembles the Stage 3 RoleAdapter loop into a
running daemon: it wires the three production adapters into the **existing**
``Dispatcher`` + ``ChatroomWatcher`` (T13/T14) over the magickit chatroom and
runs the poll loop. This is the "last piece" the loop was missing — every
component it composes (watcher, dispatcher, registry, adapters, gateway,
orchestrator) already exists; this module only *wires and runs* them.

Two intake modes share that registry→dispatcher core (selected by ``mindwire-loop --mode``):

- ``watcher`` (default): the Phase 1 ``ChatroomWatcher`` auto-reply loop described below (msg-385
  Option A, PR #82).
- ``conductor``: the NEXT-driven single-thread design conductor (``T-cross-thread-relay-conductor``
  Tier-C decide msg-523) — :func:`build_conductor` / :func:`run_conductor` wire the PR-1
  :class:`~spirrow_mindwire.conductor.core.Conductor` (core logic, already merged) onto this same
  composition root. On the conductor path there is **no** standing auto-reply watcher (Obj1): the
  conductor reads the one task thread and serially dispatches the single ``NEXT:``-named
  participant. The PR-gate stays a synchronous ``orchestrator.fire_pr_review`` driver call (ADR-19
  N-1, already watcher-independent), so no concurrent ``_seen`` watcher engine runs alongside it.

Decided in chatroom ``T-stage3-loop-wiring`` (Bohr, msg-381 + msg-385,
embodiment terminal_coding_agent):

- **Topology (msg-385 §1, Option A)**: the canonical Phase 1 topology is **one
  auto-replying role per thread, bridged by an orchestrator** — *not* a single
  thread with three roles taking turns (msg-383 §2, retracted). The
  ``ChatroomWatcher`` dedups per ``(thread, msg_id)`` and the SDK adapters reply
  to every message, so two auto-reply roles on one thread would steal each
  other's messages / ping-pong (``tests/test_phase1_e2e_smoke.py`` docstring).
  Each :class:`~spirrow_mindwire.config.LoopWatchConfig` must therefore name a
  single auto-reply role per thread; the naysayer's PR-review runs on its own
  ``T-pr-review-<repo>-<n>`` thread, opened by
  :class:`~spirrow_mindwire.orchestrator.PrReviewOrchestrator`.
- **Scope (msg-385 §2)**: this PR is *runner only* — it does not modify any
  core (watcher ``_seen`` / dispatcher routing / adapter reply path). The
  cross-thread relay + automatic thread-kickoff + convergence detection are a
  follow-up (msg-385 §4); in this MVP a human (Takahito / main) plays the
  relay/orchestrator role across threads (the role the T16 e2e harness stands
  in for), while the PR→naysayer-review→merge-GO leg is already mechanised via
  ``PrReviewOrchestrator``.
- **Invariants (msg-385 §5)**: (a) exactly one auto-reply role per thread;
  (b) no core modification; (c) the implementer adapter loads the allow-list and
  the daemon never auto-fires merge-to-main (Tier C stays unreachable from the
  loop — ADR-2026-05-23-07).

Adapter→role routing (a Stage 3 wrinkle): the registry's Phase 1 policy is
"first qualified". ``ClaudeCodeSdkAdapter`` declares ``EXECUTE_CODE`` (the T16
dual-use case where one adapter fills both proposer and implementer), so it
would also win the IMPLEMENTER slot and shadow the allow-list-gated
``ImplementerSdkAdapter``. We therefore run the proposer as a
:class:`Stage3ProposerAdapter` that drops ``EXECUTE_CODE`` — which is also the
correct Stage 3 model (the proposer only proposes, and may read to check what it
proposes against; the implementer executes and writes, gated).
:func:`build_registry` then asserts the resolution fail-loud.

Secrets / inference endpoints are resolved from the environment by the adapters
at spawn (``MINDWIRE_IMPLEMENTER_BASE_URL`` / ``MINDWIRE_LEXORA_URL`` /
``MINDWIRE_NAYSAYER_GITHUB_TOKEN``), never from TOML — see
:class:`~spirrow_mindwire.config.Stage3LoopConfig`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .adapters._sdk_result import emit_sdk_error_marker, find_sdk_error_signal
from .adapters.claude_code_sdk import ClaudeCodeSdkAdapter, _PathScopeGuard
from .adapters.implementer import ImplementerSdkAdapter
from .adapters.naysayer_sdk import NaysayerSdkAdapter
from .conductor import Conductor, ConductorOutcome, LoopControlReader
from .config import MindwireSettings, NaysayerGatingConfig, Stage3LoopConfig, load_settings
from .dispatcher.core import Dispatcher
from .dispatcher.event_log import (
    EVENT_FIELD_AUTHOR,
    EVENT_FIELD_ERROR,
    EVENT_KIND_DELIVERY_FAILED,
)
from .dispatcher.registry import InMemoryAdapterRegistry
from .github.client import (
    CheckRollup,
    EnvironmentTerminalError,
    GitHubClient,
    PrRef,
    naysayer_github_token,
)
from .magickit.client import McpToolCaller, StreamableHttpChatroomMcp
from .magickit.gateway import MagickitChatroomGateway
from .magickit.watcher import ChatroomWatcher, WatchSpec
from .naysayer.pr_review import NaysayerPrReviewDriver
from .obligations import ObligationsError, ObligationsManifest, load_manifest
from .orchestrator import PrReviewOrchestrator
from .ports import RoleAdapter
from .preflight import PreflightError, preflight_gate
from .spec_pin import SpecPinWriter
from .value_objects import Capability, Event, Role, ThreadRef

logger = logging.getLogger(__name__)


class Stage3ProposerAdapter(ClaudeCodeSdkAdapter):
    """Read-only proposer for the Stage 3 loop (same model as ``main``).

    Identical to :class:`~spirrow_mindwire.adapters.claude_code_sdk.ClaudeCodeSdkAdapter`
    at runtime, but advertises only ``{READ_THREAD, POST_REPLY}`` — it
    deliberately **drops** ``EXECUTE_CODE``.

    It was text-only (``tools=[]``) until it turned out that a proposer which
    cannot open a file cannot check a claim either; see
    :data:`_BOHR_BUILTIN_TOOLS` for what the persona currently playing this
    role may read, and the block comment above that constant for why widening
    that constant does not by itself widen the role-level invariant reasoned
    about below (the role invariant is enforced on other axes — capabilities,
    the ``can_use_tool`` guard, and the persona-tool tests).

    Why: the registry's Phase 1 ``qualified_for`` is "first qualified", and the
    base adapter declares ``EXECUTE_CODE`` (T16's dual-use, one adapter filling
    both proposer and implementer). With both it and
    :class:`~spirrow_mindwire.adapters.implementer.ImplementerSdkAdapter`
    registered, the IMPLEMENTER slot would resolve to whichever was registered
    first — risking the allow-list-gated implementer being shadowed by an
    un-gated adapter. Dropping ``EXECUTE_CODE`` here makes this adapter qualify
    for PROPOSER only, so the IMPLEMENTER slot resolves unambiguously to the
    gated adapter. It is also the correct Stage 3 model: the proposer proposes;
    only the implementer executes code or changes the tree, behind the
    allow-list.

    Independence is unaffected: like the base adapter this omits
    ``NAYSAYER_QUALIFIED`` (same model family as ``main``), so it can never fill
    the naysayer slot (ADR-05 §5).
    """

    adapter_id: str = "stage3-proposer"
    capabilities: frozenset[Capability] = frozenset({Capability.READ_THREAD, Capability.POST_REPLY})


@dataclass
class Stage3Loop:
    """The assembled, ready-to-run Stage 3 loop (composition root output).

    The daemon runs :attr:`watcher` (the standing role watches). The ``orchestrator``
    drives the develop→main PR-review directly via its ``NaysayerPrReviewDriver``
    (ADR-19 driver-化 unify — no naysayer watch / no watcher round-trip); firing
    ``orchestrator.fire_pr_review`` is a ``scripts/naysayer_review.py`` run's / a
    follow-up PR-event trigger's job.
    """

    mcp: McpToolCaller
    registry: InMemoryAdapterRegistry
    dispatcher: Dispatcher
    watcher: ChatroomWatcher
    orchestrator: PrReviewOrchestrator
    watches: tuple[WatchSpec, ...]

    async def aclose(self) -> None:
        """Tear the loop down cleanly: stop the watcher + close the PR-review driver's clients.

        The PR-review driver is orchestrator-held (not in the registry, so it is not swept by an
        adapter teardown), so its HTTP pools are closed here (Tier B #93 round-4) — symmetric with
        ``watcher.stop()``.
        """
        await self.watcher.stop()
        await self.orchestrator.aclose()


@dataclass
class Stage3Conductor:
    """The assembled, ready-to-run NEXT-driven conductor (conductor-mode composition root output).

    Holds the same registry→dispatcher core as :class:`Stage3Loop` but with the serial
    :class:`~spirrow_mindwire.conductor.core.Conductor` as the intake instead of the
    ``ChatroomWatcher`` auto-reply path (Obj1, msg-523): there is no standing watcher on this path.
    :meth:`run` drives the one task thread to a stop condition and returns the outcome;
    :meth:`aclose` halts the spawned adapter sessions on shutdown (symmetric with
    ``ChatroomWatcher.stop()``).
    """

    mcp: McpToolCaller
    registry: InMemoryAdapterRegistry
    dispatcher: Dispatcher
    conductor: Conductor

    async def run(self) -> ConductorOutcome:
        """Drive the configured task thread turn-by-turn until a stop condition (D-4)."""
        return await self.conductor.run()

    async def aclose(self) -> None:
        """Halt the adapter sessions the conductor spawned (clean daemon teardown).

        The conductor spawns sessions directly through the dispatcher (no watcher records the
        handles), so the dispatcher's own ``aclose`` is what tears the SDK subprocess sessions down.
        """
        await self.dispatcher.aclose()


def _thread_ref(project: str, thread_id: str) -> ThreadRef:
    """Build a :class:`ThreadRef` for a magickit chatroom thread.

    The ``chatroom_uri`` mirrors
    :meth:`~spirrow_mindwire.orchestrator.PrReviewOrchestrator.fire_pr_review`
    so threads opened by the orchestrator and threads watched from config share
    one URI shape. Only ``project_id`` / ``thread_id`` are used for MCP I/O.
    """
    return ThreadRef(
        project_id=project,
        thread_id=thread_id,
        chatroom_uri=f"magickit://chatroom/thread/{thread_id}",
    )


def build_registry(
    *,
    proposer: RoleAdapter,
    implementer: RoleAdapter,
    naysayer: RoleAdapter,
) -> InMemoryAdapterRegistry:
    """Register the three Stage 3 adapters and assert role resolution fail-loud.

    Registration order matters only for the PROPOSER slot: its required
    capabilities ``{READ_THREAD, POST_REPLY}`` are a subset of every adapter's,
    so all three "qualify" and first-qualified resolves by order — the proposer
    is registered first so it wins. IMPLEMENTER (``EXECUTE_CODE``) and NAYSAYER
    (``NAYSAYER_QUALIFIED``) are each satisfied by exactly one adapter, so they
    resolve unambiguously. The post-registration assertion turns any future
    capability/ordering drift into a loud startup failure rather than a silent
    mis-route.
    """
    registry = InMemoryAdapterRegistry()
    registry.register(proposer)
    registry.register(implementer)
    registry.register(naysayer)
    _assert_role_resolution(registry, proposer=proposer, implementer=implementer, naysayer=naysayer)
    return registry


def _assert_role_resolution(
    registry: InMemoryAdapterRegistry,
    *,
    proposer: RoleAdapter,
    implementer: RoleAdapter,
    naysayer: RoleAdapter,
) -> None:
    proposer_candidates = registry.qualified_for(Role.PROPOSER)
    if not proposer_candidates or proposer_candidates[0] is not proposer:
        raise RuntimeError(
            "PROPOSER slot did not resolve to the read-only proposer adapter "
            f"(got {[a.adapter_id for a in proposer_candidates]!r}); register the "
            "proposer first so first-qualified picks it"
        )
    implementer_candidates = registry.qualified_for(Role.IMPLEMENTER)
    if not implementer_candidates or implementer_candidates[0] is not implementer:
        raise RuntimeError(
            "IMPLEMENTER slot did not resolve to the allow-list-gated "
            f"ImplementerSdkAdapter (got {[a.adapter_id for a in implementer_candidates]!r}); "
            "the proposer must not advertise EXECUTE_CODE (use Stage3ProposerAdapter)"
        )
    naysayer_candidates = registry.qualified_for(Role.NAYSAYER)
    if naysayer_candidates != [naysayer]:
        raise RuntimeError(
            "NAYSAYER slot must resolve to exactly the independent naysayer adapter "
            f"(got {[a.adapter_id for a in naysayer_candidates]!r}); ADR-05 §5 "
            "independence requires the naysayer to be the sole NAYSAYER_QUALIFIED adapter"
        )


# The Bohr persona (the daemon-hosted persona that currently plays the proposer
# role) designs against this repository, so it has to be able to READ it. It
# could not: the adapter passed ``tools=[]``, which disables every built-in,
# and Bohr's own turn on T-fs-delete-path-scope (msg-1197) stopped with
# "read 系 tool の実行権限が下りず、一次照合を一切行えていない" — it declined to
# design rather than guess, which is right, and then nothing moved. The Einstein
# review of that turn endorsed the refusal. So the gap is here, not there.
#
# The list is named for the persona whose tools it happens to enumerate, not
# for the role. Takahito msg-3507 ("Bohr であることと PROPOSER であることは必ずし
# も一致しない") and ADR-2026-05-27-09 (identity 4 layers) draw ``identity_name``
# (persona) and ``role`` on orthogonal axes, and the name reflects that: what
# lives here is Bohr's built-in tool面 for its current embodiment, not a
# statement about what the proposer role may carry in general.
#
# The rename is honest about naming; it is not a structural decoupling.
# :func:`build_proposer` still hardcodes this constant into the proposer role's
# adapter, so today every proposer built by this composition root gets exactly
# these tools. A future embodiment that put a different persona in the proposer
# slot would need :func:`build_proposer` to accept a persona identifier and
# look up a per-persona tool tuple — that plumbing does not yet exist.
#
# What the role invariant "a proposer role adapter cannot change the tree"
# does have is enforcement on axes independent of this constant, and those
# axes are still what stops a widening of this list from silently widening the
# role: (a) :class:`Stage3ProposerAdapter` drops ``EXECUTE_CODE`` from
# ``capabilities`` so PROPOSER is the only registry slot it qualifies for and
# the IMPLEMENTER slot resolves unambiguously to the allow-list-gated adapter;
# (b) the ``can_use_tool`` guard injected at :func:`build_proposer` (currently
# :class:`_PathScopeGuard`, whose ``scopeable_tools`` frozenset admits only
# ``Read`` / ``Glob`` / ``Grep``) refuses anything it cannot bound; (c) the
# tests at :file:`tests/test_loop_runner.py` that assert Bohr's built-in tool
# 面 today does not include write/execute tools. Widening this list to add a
# new tool therefore does *not* by itself widen the role — the guard still has
# to admit the tool for it to actually run through the adapter.
#
# Read / Glob / Grep is the current persona 面. They are auto-approved because
# a call that is not auto-approved goes to an interactive permission prompt,
# and nobody is there to answer it — which is exactly the "permission denied"
# Bohr reported. That is a property of running headless, not of whether a
# guard exists: the ``can_use_tool`` guard injected below still runs on every
# call, and is where the bound lives.
_BOHR_BUILTIN_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")


def build_proposer(
    repo_dir: Path,
    *,
    model: str | None = None,
    cli_path: Path | None = None,
) -> Stage3ProposerAdapter:
    """Proposer with read-only access to ``repo_dir`` (same model family as ``main``).

    The tool list wired here is :data:`_BOHR_BUILTIN_TOOLS` — named for the
    persona whose tools it happens to enumerate today. This function does not
    yet take a persona parameter, so it hardcodes that constant; the naming is
    honest labeling of what the constant is, not a claim that this composition
    root already dispatches on persona.

    The role-level "proposer cannot change the tree" invariant lives on other
    axes (see the block comment above the constant). The path-scope guard
    installed here is the seam that enforces the intersection — the adapter
    can only actually invoke a tool the guard admits.

    ``model`` / ``cli_path`` come from ``[loop].role_model`` /
    ``[loop].role_cli_path`` and default to None = the SDK's own choice. They
    are passed as a pair because naming a model the vendored CLI is too old for
    fails at the API, not here; ``adapters/_cli_selection`` carries the measured
    versions, and the reason the naysayer is not offered the same pair.
    """
    return Stage3ProposerAdapter(
        cwd=repo_dir,
        builtin_tools=_BOHR_BUILTIN_TOOLS,
        allowed_tools=list(_BOHR_BUILTIN_TOOLS),
        model=model,
        cli_path=cli_path,
        # The scope is decided here, where the role is known — the adapter has no
        # way to tell a filesystem path from an MCP tool's URI-shaped ``path``,
        # so it is not asked to guess. `allowed_tools` auto-approves, which is
        # what makes a guard the only place a bound can live.
        can_use_tool=_PathScopeGuard(root=repo_dir),
    )


def build_implementer(
    repo_dir: Path,
    *,
    obligations: ObligationsManifest,
    model: str | None = None,
    cli_path: Path | None = None,
) -> ImplementerSdkAdapter:
    """Allow-list-gated implementer; inference base URL + allow-list from env/defaults.

    ``inference_base_url`` is left to the adapter's
    ``MINDWIRE_IMPLEMENTER_BASE_URL`` resolution; the adapter **refuses to
    spawn** if it is unset (no silent fallback to ``api.anthropic.com`` — ADR-07
    §2.4), which surfaces a misconfigured daemon loudly at first watch.

    ``obligations`` is the loop-readable obligations manifest loaded at the
    composition root and passed in by injection — the adapter never reaches for a
    module-global path itself. See :func:`_load_obligations_or_exit`.

    v12 (T-auto-backgrounded-command-hangs-conductor-4h): installs the SDK
    Job Object hook exactly once here. The install is a no-op on POSIX, and
    idempotent under re-invocation — this is safe even if the composition
    root is re-entered from tests or a hot-reload. Placement here (rather
    than at module import) means a docs-only checkout that never builds an
    implementer never patches the SDK's transport module.

    ``model`` / ``cli_path`` are the same pair :func:`build_proposer` takes, from
    the same two config keys — the two roles that route to Anthropic move
    together, so a host cannot end up designing on one model and implementing on
    another without saying so.
    """
    from .adapters import _sdk_job_hook

    _sdk_job_hook.install_hook()
    return ImplementerSdkAdapter(
        cwd=repo_dir,
        obligations=obligations,
        model=model,
        cli_path=cli_path,
    )


def build_naysayer(repo_dir: Path, *, obligations: ObligationsManifest) -> NaysayerSdkAdapter:
    """The single registry naysayer = the design-time agent (ADR-19 D-1 / driver-化 unify).

    The PR-review gate is no longer a registry RoleAdapter (it is the driver built by
    :func:`build_pr_review_driver`); the sole ``NAYSAYER_QUALIFIED`` adapter is this design-time
    agent (independence by distribution: ``MINDWIRE_NAYSAYER_BASE_URL`` → Lexora Gemini, resolved
    at spawn). It participates by summon, so the daemon registers it (the single-NAYSAYER
    invariant) without a standing watch.

    ``obligations`` is the loop-readable obligations manifest loaded at the
    composition root and passed in by injection.
    """
    return NaysayerSdkAdapter(cwd=repo_dir, obligations=obligations)


def _resolve_role_cli_path_or_exit(configured: Path | None) -> Path | None:
    """Check ``[loop].role_cli_path`` points at a real file — fail-closed with ``SystemExit``.

    Unset is the normal case and returns ``None`` (the SDK uses its vendored
    CLI). Set-but-wrong is checked here, at daemon startup, rather than left to
    the first spawn: the sweep runs the daemon every five minutes, so a typo'd
    path would otherwise surface as a per-tick spawn failure — the shape an
    operator reads as "the loop is broken" rather than "one setting is wrong".

    The path is made **absolute** before it goes anywhere, because a relative
    one is read against two different base directories by the two things that
    consume it: this check runs in the daemon's working directory, while the SDK
    hands the string to the OS with the session's ``cwd`` (``[loop].repo_dir``)
    in play. A relative value could therefore pass here and fail at every spawn,
    or — worse — resolve to a different binary than the one that was checked.
    Resolving once, at the point the operator's string enters the program,
    removes the question rather than answering it per consumer. It also keeps
    the implementer's leftover belt on a real absolute path, which is the form
    it matches most precisely (``adapters/_sdk_job_hook`` falls back to a
    basename comparison for anything else).

    Existence and the executable bit are all that is checked. Whether the binary
    is new enough for ``role_model`` is not knowable from here without running
    it, and the API says so precisely when it is not
    (``adapters/_cli_selection``).
    """
    if configured is None:
        return None
    path = Path(configured).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(
            f"loop.role_cli_path does not point at a file: {path} — set "
            "[loop].role_cli_path (or MINDWIRE_LOOP__ROLE_CLI_PATH) to the Claude Code "
            "executable the proposer/implementer sessions should run, or leave it unset "
            "to use the CLI vendored in claude-agent-sdk"
        )
    # A file that exists but cannot be executed fails at spawn, once per sweep
    # tick — the shape this guard exists to convert into one startup error. The
    # check is a no-op on Windows (``os.access`` reports X_OK for any readable
    # file there), so it buys nothing on the current loop host and everything on
    # a POSIX one; it is here because the cost of being wrong is asymmetric.
    if not os.access(path, os.X_OK):
        raise SystemExit(
            f"loop.role_cli_path is not executable: {path} — the daemon would spawn it "
            "once per tick and fail each time; fix the file's permissions, point "
            "[loop].role_cli_path at the real Claude Code executable, or leave it unset "
            "to use the CLI vendored in claude-agent-sdk"
        )
    return path


def _load_obligations_or_exit() -> ObligationsManifest:
    """Load the loop-readable obligations manifest — fail-closed with ``SystemExit``.

    The manifest (``spec/process/obligations.yaml``) holds the prompt clauses
    that bind agent behaviour at runtime; a missing or malformed manifest would
    silently degrade every session's prompt if the adapters loaded it themselves.
    The composition root loads it once at daemon startup and converts any
    :class:`~spirrow_mindwire.obligations.ObligationsError` into ``SystemExit`` so
    the daemon refuses to start rather than serving weakened prompts. This is the
    "loader 欠損時 fail-closed" invariant (Tier-C GO msg-737).
    """
    try:
        return load_manifest()
    except ObligationsError as exc:
        # No path-override mechanism is exposed here on purpose: the composition
        # root loads the in-repo manifest at its default location. Suggesting a
        # non-existent override would misdirect the operator (naysayer round-3
        # finding on this PR — "phantom path override"). Remediation is fixing
        # the manifest file, or invoking the daemon from the repo root.
        raise SystemExit(
            f"obligations manifest failed to load — daemon cannot start with a degraded "
            f"prompt set (this is fail-closed by design; fix spec/process/obligations.yaml "
            f"and retry from the repo root): {exc}"
        ) from exc


def build_pr_review_driver(
    gating: NaysayerGatingConfig | None = None,
) -> NaysayerPrReviewDriver:
    """The Tier B develop→main PR-review driver (Lexora one-shot + GitHub T22, env-resolved).

    ``gating`` (default-off when ``None``) supplies the PR-review debounce knobs
    (``[naysayer_gating]``): skip a re-review of an unchanged head, and cap the re-review rounds.
    """
    g = gating or NaysayerGatingConfig()
    return NaysayerPrReviewDriver(
        skip_if_head_unchanged=g.skip_if_head_unchanged,
        max_review_rounds=g.max_review_rounds,
        review_login=g.review_login,
        shadow=g.shadow,
    )


def build_watches(cfg: Stage3LoopConfig) -> tuple[WatchSpec, ...]:
    """Turn the config's watch list into :class:`WatchSpec`\\s.

    A blank ``instance_id`` is left blank so ``WatchSpec`` mints the Phase 1
    default (``mint_instance_id(role)`` = ``"{role}-1"``).
    """
    return tuple(
        WatchSpec(
            thread_ref=_thread_ref(cfg.project, w.thread_id),
            role=w.role,
            instance_id=w.instance_id,
        )
        for w in cfg.watches
    )


async def _log_event_sink(event: Event) -> None:
    """Observational event-log sink (I7): log reply.sent / delivery.failed.

    A denial record, when present, is rendered onto the same warning line. That
    placement is the point: the sweep's quarantine record captures the *wrapper log
    tail*, so a field that never reaches this line is a field nobody reads when a
    session halts (``spec/design/T-denial-detail-and-overdeny.md``).
    """
    author = event.fields.get(EVENT_FIELD_AUTHOR, "?")
    if event.kind == EVENT_KIND_DELIVERY_FAILED:
        logger.warning(
            "loop event %s author=%s error=%s",
            event.kind,
            author,
            event.fields.get(EVENT_FIELD_ERROR),
        )
    else:
        logger.info("loop event %s author=%s", event.kind, author)


def _build_dispatcher(
    settings: MindwireSettings,
    *,
    mcp: McpToolCaller | None,
    proposer: RoleAdapter | None,
    implementer: RoleAdapter | None,
    naysayer: RoleAdapter | None,
) -> tuple[McpToolCaller, InMemoryAdapterRegistry, Dispatcher]:
    """Shared composition root for the watcher loop and the conductor.

    Resolves the MCP transport + the three production adapters (proposer / implementer / naysayer)
    from ``settings`` / the environment unless injected (tests pass fakes), then assembles the
    registry + gateway + dispatcher. Both :func:`build_loop` (watcher mode) and
    :func:`build_conductor` (conductor mode) build the *same* registry→dispatcher→gateway core; only
    the intake differs (a ``ChatroomWatcher`` vs the serial
    :class:`~spirrow_mindwire.conductor.core.Conductor`). Raises ``SystemExit`` if ``loop.repo_dir``
    is unset and an SDK adapter must be built from config.
    """
    cfg = settings.loop
    if mcp is None:
        mcp = StreamableHttpChatroomMcp()  # MINDWIRE_MAGICKIT_MCP_URL or default

    # proposer / implementer / naysayer are all SDK adapters that operate in the repo, so any of
    # them being unset requires loop.repo_dir.
    if proposer is None or implementer is None or naysayer is None:
        if cfg.repo_dir is None:
            raise SystemExit(
                "loop.repo_dir is not configured: set [loop].repo_dir (the repo the "
                "proposer/implementer/naysayer operate in) in mindwire.toml or "
                "MINDWIRE_LOOP__REPO_DIR"
            )
        repo_dir = Path(cfg.repo_dir)
        # Load the loop-readable obligations manifest once, at the composition root, and
        # pass it into every adapter that needs it. Fail-closed (SystemExit) on missing
        # or malformed manifest — a silent degradation of the loop's actual instructions
        # would be exactly the "correct-but-invisible fail-open" that
        # spec/process/README.md warns against.
        obligations = _load_obligations_or_exit()
        role_cli_path = _resolve_role_cli_path_or_exit(cfg.role_cli_path)
        if proposer is None:
            proposer = build_proposer(repo_dir, model=cfg.role_model, cli_path=role_cli_path)
        if implementer is None:
            implementer = build_implementer(
                repo_dir,
                obligations=obligations,
                model=cfg.role_model,
                cli_path=role_cli_path,
            )
        if naysayer is None:
            # No model / cli_path here, by design: the naysayer's independence is
            # its Lexora tier, and a newer CLI breaks that route (422). See
            # ``adapters/_cli_selection``.
            naysayer = build_naysayer(repo_dir, obligations=obligations)

    registry = build_registry(proposer=proposer, implementer=implementer, naysayer=naysayer)
    gateway = MagickitChatroomGateway(mcp)
    # SPEC-2026-09-20-pin-hardening-and-id-audit §2.1 D-32 / D-39: the
    # dispatcher writes `.mindwire/pin` before every implementer / naysayer
    # dispatch. The pin lives at ``<repo_root>/.mindwire/pin``, so the writer
    # is built from ``loop.repo_dir`` — the same working tree the implementer
    # / naysayer SDK adapters operate in. Phase 1 has no production
    # ``SpecPinMapping``, so the default empty mapping applies and every
    # dispatch writes a bootstrap pin (§3-B).
    #
    # When ``cfg.repo_dir`` is None the adapters above were all pre-built by
    # the caller (tests), in which case the composition root does not know a
    # repo root and pin writing is left off — production paths always set
    # ``cfg.repo_dir`` because the ``if proposer is None or ...`` block above
    # would already have raised ``SystemExit``.
    pin_writer: SpecPinWriter | None = None
    if cfg.repo_dir is not None:
        # In the Stage 3 loop the SDK invoke's cwd IS the git checkout, so
        # pin_target_dir and spec_source_root are the same path. The Phase
        # 0/1 ThreadDispatcher, by contrast, uses layout.thread_dir for
        # pin_target_dir (per-thread scratch) and leaves spec_source_root
        # unset (PR-review #335 round-2: the two must not be conflated).
        repo = Path(cfg.repo_dir)
        pin_writer = SpecPinWriter(pin_target_dir=repo, spec_source_root=repo)
    dispatcher = Dispatcher(
        registry=registry,
        gateway=gateway,
        event_sink=_log_event_sink,
        spec_pin_writer=pin_writer,
    )
    return mcp, registry, dispatcher


def build_loop(
    settings: MindwireSettings,
    *,
    mcp: McpToolCaller | None = None,
    proposer: RoleAdapter | None = None,
    implementer: RoleAdapter | None = None,
    naysayer: RoleAdapter | None = None,
    pr_review_driver: NaysayerPrReviewDriver | None = None,
) -> Stage3Loop:
    """Assemble the Stage 3 watcher loop from settings (composition root).

    Components may be injected (tests pass fakes); anything left ``None`` is
    built from ``settings`` / the environment. Raises ``SystemExit`` if
    ``loop.repo_dir`` is unset and an SDK adapter must be built from config.
    The ``naysayer`` is the design-time SDK agent (the sole registry NAYSAYER);
    the PR-review gate is the ``pr_review_driver`` wired into the orchestrator
    (ADR-19 driver-化 unify), not a registered adapter.
    """
    cfg = settings.loop
    mcp, registry, dispatcher = _build_dispatcher(
        settings, mcp=mcp, proposer=proposer, implementer=implementer, naysayer=naysayer
    )
    if pr_review_driver is None:
        pr_review_driver = build_pr_review_driver(settings.naysayer_gating)

    watches = build_watches(cfg)
    # Constructor watches=() — each watch is added via add_watch() in run_loop so
    # its per-watch baseline is honoured (watcher.start() applies one flag to all).
    watcher = ChatroomWatcher(mcp, dispatcher, [])
    orchestrator = PrReviewOrchestrator(mcp, driver=pr_review_driver)
    return Stage3Loop(
        mcp=mcp,
        registry=registry,
        dispatcher=dispatcher,
        watcher=watcher,
        orchestrator=orchestrator,
        watches=watches,
    )


async def run_loop(settings: MindwireSettings) -> None:
    """Build the loop, register its watches, and poll until cancelled.

    Runs the composition-root preflight (:func:`_preflight`) before the loop
    is built — a P0/P1/P2 failure raises :class:`~spirrow_mindwire.preflight.PreflightError`
    (``SystemExit``), which the daemon launcher surfaces. The premise the loop
    relies on (server-side protection of ``main``, a captive clone separate
    from the daemon's own checkout, remotes under SpirrowGames) has to hold
    before any adapter is spawned, so preflight comes first.

    Each configured watch is added with its own ``baseline`` (so a freshly
    opened task thread can be acted on with ``baseline=False``, while an ongoing
    thread uses the production-safe ``baseline=True``). ``loop.aclose()`` runs in
    ``finally`` so the SDK subprocess sessions disconnect (watcher.stop) AND the
    PR-review driver's HTTP clients close cleanly on shutdown (watcher docstring /
    msg-381 §E-1; Tier B #93 round-4 for the driver close).
    """
    cfg = settings.loop
    _preflight(cfg)
    loop = build_loop(settings)

    if not cfg.watches:
        logger.warning(
            "loop.watches is empty — the daemon will idle (no thread is being watched). "
            "Add [loop].watches entries (one auto-reply role per thread) to put it to work."
        )

    # add_watch (not start()) so each watch's baseline is applied individually.
    for spec, watch_cfg in zip(loop.watches, cfg.watches, strict=True):
        await loop.watcher.add_watch(spec, baseline=watch_cfg.baseline)

    logger.info(
        "stage3 loop started: project=%s watches=%d poll=%.1fs",
        cfg.project,
        len(loop.watches),
        cfg.poll_interval_seconds,
    )
    try:
        await loop.watcher.run(poll_interval_seconds=cfg.poll_interval_seconds)
    finally:
        await loop.aclose()


class _PerCallCheckRollupSource:
    """``CheckRollupSource`` backed by a short-lived :class:`GitHubClient`, one per admission.

    Per-call rather than a shared long-lived client, matching what the PR-review driver already
    does with its own client: the conductor has no teardown hook to close a pooled client on, and
    inventing one for a read that happens at most once per tick would buy a connection pool at
    the cost of a lifecycle that can leak.

    **Token.** The review-side identity (:func:`naysayer_github_token`), not the author one, so
    admission and the gate it admits are looking at the same repo through the same credential —
    an admission that could see a rollup the gate cannot (or the reverse) would decide on a
    world the gate does not live in. Measured 2026-09-09 on ``SpirrowGames/spirrow-mindwire``:
    the GraphQL ``statusCheckRollup.contexts`` read returns 200 with full per-check rows under
    this token.

    Never raises: :meth:`GitHubClient.fetch_check_rollup` is fail-soft to ``None`` on every error
    path, and ``None`` is the conductor's "keep the pre-wiring behaviour" signal. A scheduled
    loop must not die because GitHub was briefly unreachable.
    """

    def __init__(self, token: str | None = None) -> None:
        self._token = token

    async def fetch_check_rollup(self, pr: PrRef) -> CheckRollup | None:
        token = self._token if self._token is not None else naysayer_github_token()
        async with GitHubClient(token) as client:
            return await client.fetch_check_rollup(pr)


def build_conductor(
    settings: MindwireSettings,
    *,
    mcp: McpToolCaller | None = None,
    proposer: RoleAdapter | None = None,
    implementer: RoleAdapter | None = None,
    naysayer: RoleAdapter | None = None,
    pr_review_driver: NaysayerPrReviewDriver | None = None,
) -> Stage3Conductor:
    """Assemble the NEXT-driven conductor from settings (conductor-mode composition root).

    Reuses :func:`_build_dispatcher` (same registry→dispatcher core as the watcher loop) and wires a
    :class:`~spirrow_mindwire.conductor.core.Conductor` over the single
    ``[conductor].task_thread_id`` thread. The conductor's ``project`` / ``repo_dir`` / adapters
    come from ``[loop]``; the conductor-specific ``task_thread_id`` / ``roster`` /
    ``naysayer_identity`` / ``max_rounds`` come from ``[conductor]``.

    Raises ``SystemExit`` (daemon-startup config error, like ``build_loop``'s ``repo_dir`` guard)
    if a required ``[conductor]`` field is unset, or if ``naysayer_identity`` does not map to the
    naysayer role in ``roster`` (the :class:`Conductor` ctor's fail-loud invariant, Tier B msg-529,
    surfaced here as a friendly startup error rather than a raw ``ValueError``).
    """
    loop_cfg = settings.loop
    cond_cfg = settings.conductor
    if not cond_cfg.task_thread_id.strip():
        raise SystemExit(
            "conductor.task_thread_id is not configured: set [conductor].task_thread_id (the "
            "single design thread the conductor drives) in mindwire.toml or "
            "MINDWIRE_CONDUCTOR__TASK_THREAD_ID"
        )
    if not cond_cfg.roster:
        raise SystemExit(
            "conductor.roster is empty: set [conductor].roster (the chatroom identity→role map, "
            'e.g. Bohr = "proposer") so the conductor can resolve each NEXT: participant'
        )
    if not cond_cfg.naysayer_identity.strip():
        raise SystemExit(
            "conductor.naysayer_identity is not configured: set [conductor].naysayer_identity (the "
            "roster persona that fills the independent naysayer slot) so Obj2 forced consultation "
            "can recognise a naysayer turn"
        )

    mcp, registry, dispatcher = _build_dispatcher(
        settings, mcp=mcp, proposer=proposer, implementer=implementer, naysayer=naysayer
    )
    if pr_review_driver is None:
        pr_review_driver = build_pr_review_driver(settings.naysayer_gating)
    # PR-gate (PR-2b-2): the conductor fires the Tier B independent naysayer review synchronously on
    # a ``NEXT: pr-review <ref>`` via this orchestrator (the same one build_loop wires for the
    # watcher path) — driver-化 unify, ADR-19 N-1; no parallel watcher is added.
    orchestrator = PrReviewOrchestrator(mcp, driver=pr_review_driver)
    thread_ref = _thread_ref(loop_cfg.project, cond_cfg.task_thread_id)
    try:
        conductor = Conductor(
            mcp=mcp,
            dispatcher=dispatcher,
            thread_ref=thread_ref,
            roster=dict(cond_cfg.roster),
            naysayer_identity=cond_cfg.naysayer_identity,
            human_identity=cond_cfg.human_identity,
            max_rounds=cond_cfg.max_rounds,
            force_naysayer_only_on_explicit_human=cond_cfg.force_naysayer_only_on_explicit_human,
            # ADR-2026-09-14-21: merged over the shipped default inside the Conductor, so an
            # empty config block is the ADR's behaviour rather than "spawn anything named".
            identity_embodiment=dict(cond_cfg.identity_embodiment),
            orchestrator=orchestrator,
            # Per-project loop control (Part C). Keyed on ``[loop].project`` — the same key the
            # sweep rewrites per candidate — so one daemon invocation controls exactly the project
            # it was pointed at. Wired unconditionally and with no disable knob: a switch that
            # turned the stop control off would be a way to make the loop unstoppable from the
            # dashboard while still looking configured.
            control=LoopControlReader(mcp, project=loop_cfg.project),
            # Pre-gate CI-wait admission (design v0.3.1 §5.2A, residual RES-WIRING). Wired
            # unconditionally, for the same reason ``control`` is: a knob that turned admission
            # off would restore exactly the behaviour the residual exists to end, and there is
            # already a structural off-switch that does not need a flag — an unreadable rollup
            # degrades to the pre-wiring path (fire the gate) inside ``Conductor._admit``.
            rollup_source=_PerCallCheckRollupSource(),
        )
    except ValueError as exc:
        raise SystemExit(f"conductor misconfigured ([conductor] in mindwire.toml): {exc}") from exc
    return Stage3Conductor(mcp=mcp, registry=registry, dispatcher=dispatcher, conductor=conductor)


async def run_conductor(settings: MindwireSettings) -> ConductorOutcome:
    """Build the conductor, drive the task thread once to a stop condition, and tear it down.

    Like :func:`run_loop`, runs the composition-root preflight
    (:func:`_preflight`) before the conductor is built — the P0/P1/P2 premise
    has to hold before any adapter session opens.

    The conductor's :meth:`~spirrow_mindwire.conductor.core.Conductor.run` is itself the serial poll
    loop (it re-reads the thread each turn); it returns when a D-4 stop condition is reached
    (``NEXT: human`` Tier-C decision point / ``NEXT: none`` settled / a malformed handoff or
    no-progress human fallback / the round cap). This entry therefore drives one design thread to
    its stop and exits — re-arming after the human responds is an operator / follow-up concern. The
    spawned adapter sessions are closed in ``finally`` so SDK subprocesses don't leak on shutdown.
    """
    _preflight(settings.loop)
    cond = build_conductor(settings)
    logger.info(
        "conductor started: project=%s thread=%s roster=%d max_rounds=%d",
        settings.loop.project,
        settings.conductor.task_thread_id,
        len(settings.conductor.roster),
        settings.conductor.max_rounds,
    )
    try:
        outcome = await cond.run()
        logger.info(
            "conductor finished: stop_reason=%s rounds=%d forced_naysayer=%d "
            "forced_naysayer_saveable=%d last_msg=%s",
            outcome.stop_reason.value,
            outcome.rounds,
            outcome.forced_naysayer_turns,
            outcome.forced_naysayer_turns_saveable,
            outcome.last_msg_id,
        )
        return outcome
    finally:
        await cond.aclose()


def _preflight(cfg: Stage3LoopConfig) -> None:
    """Run the P0/P1/P2 composition-root preflight — fail-closed on any failure.

    Called from :func:`run_loop` / :func:`run_conductor` before any adapter is
    built. The invariants it enforces (see :mod:`spirrow_mindwire.preflight` for
    the design write-up):

    * **P0** — ``repo_dir`` must not be the daemon's own checkout.
    * **P2** — every remote URL in ``repo_dir`` must be under
      ``https://github.com/SpirrowGames/``.
    * **P1** — the default branch of each such repo must have the three
      server-side rules (``deletion`` / ``non_fast_forward`` / ``pull_request``)
      AND ``current_user_can_bypass == 'never'`` for the loop's identity.

    Deliberately NOT called from :func:`build_loop` / :func:`build_conductor`,
    so tests can compose the loop without a live GitHub API — the preflight
    fires only on the ``run_*`` production path. Configuration errors that
    ``build_*`` already raises as ``SystemExit`` (``loop.repo_dir`` missing,
    conductor knobs missing) come first because they precede any preflight
    concern; a repo_dir the operator did not set has nothing for P0/P2 to
    check against.
    """
    if cfg.repo_dir is None:
        # `_build_dispatcher` raises SystemExit for this case with a fuller
        # message; here we defer to it so the operator sees one preflight
        # failure per issue rather than two overlapping ones.
        return
    try:
        preflight_gate(Path(cfg.repo_dir))
    except PreflightError:
        raise
    except Exception as exc:
        # Any unexpected exception at this layer is itself a fail-closed event —
        # the preflight either passes cleanly or halts the daemon.
        raise PreflightError(
            f"preflight raised an unexpected exception (fail-closed by design): {exc}"
        ) from exc


def _ensure_utf8_runtime() -> None:
    """Make the daemon UTF-8-safe (T39).

    The implementer / naysayer adapters already force UTF-8 in their CLI subprocesses (T37 #2), but
    the daemon *parent* process's own stdout / stderr default to the OS code page — cp932 on JP
    Windows — which raises ``UnicodeEncodeError`` on em-dash / 日本語 in logged reply content. We
    reconfigure the parent streams to UTF-8 and export ``PYTHONUTF8`` / ``PYTHONIOENCODING`` so any
    process the daemon spawns inherits UTF-8 too.

    The interpreter's own UTF-8 *mode* can only be set before startup, so the production service
    launches the daemon with ``PYTHONUTF8=1`` (ADR-18 deploy); this is the in-code defensive floor
    so a bare ``mindwire-loop`` invocation does not crash on non-ASCII before that wrapper exists.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with suppress(Exception):  # a redirected / non-text stream may not support it
                reconfigure(encoding="utf-8")
    # Propagate to children (setdefault: respect an explicit operator override).
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


# The stdout sentinel the PS wrapper looks for to locate the environment-terminal
# payload row. Kept as a module constant so tests can assert on the exact bytes and
# the PS parser can be pinned against the same string (T-gate-review-submit-failure-
# handling DESIGN v3 §3, msg-1988). Any change here must land alongside the matching
# regex update in :file:`deploy/run-conductor-scheduled.ps1` (PR-C).
_ENV_TERMINAL_PAYLOAD_PREFIX = "MINDWIRE_ENV_TERMINAL_PAYLOAD "


def _emit_environment_terminal_payload(exc: EnvironmentTerminalError) -> None:
    """Print the JSON payload row the PS wrapper parses for the alert dedup key.

    Shape: one line, prefix ``MINDWIRE_ENV_TERMINAL_PAYLOAD `` followed by a JSON
    object with ``scope`` / ``status_code`` / ``owner`` / ``repo`` / ``pr``. The PS
    parser is permitted to consume ONLY this row for its dedup key; the control
    flow decision (do-not-quarantine) rides on the exit code, never on this row
    (DESIGN v3 §3, msg-1987). If parsing fails on the PS side, the wrapper falls
    back to the global ``__github_credential__`` key and still fires the alert —
    a malformed row never swallows a critical environment notification (Einstein
    v3 condition 1, msg-1988).
    """
    import json  # local to avoid pulling json into hot-path imports of this module

    payload = {
        "scope": exc.scope.value,
        "status_code": exc.status_code,
        "owner": exc.pr.owner,
        "repo": exc.pr.repo,
        "pr": exc.pr.number,
    }
    sys.stdout.write(f"{_ENV_TERMINAL_PAYLOAD_PREFIX}{json.dumps(payload)}\n")
    sys.stdout.flush()


def main() -> None:
    """Entry point for the ``mindwire-loop`` console script.

    ``--mode watcher`` (default) runs the Phase 1 ``ChatroomWatcher`` auto-reply loop (msg-385
    Option A, PR #82). ``--mode conductor`` runs the NEXT-driven single-thread design conductor
    (Tier-C decide msg-523): the autonomous design path with no auto-reply watcher on it (Obj1). The
    default is left at ``watcher`` for backward compatibility; flipping the default / retiring the
    watcher auto-reply mode entirely is the remaining Obj1 decision (deferred — see PR body).
    """
    _ensure_utf8_runtime()
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        prog="mindwire-loop",
        description="Spirrow MindWire Stage 3 autonomous-loop daemon.",
    )
    parser.add_argument(
        "--mode",
        choices=("watcher", "conductor"),
        default="watcher",
        help=(
            "watcher: Phase 1 auto-reply loop (default); "
            "conductor: NEXT-driven single-thread design conductor (msg-523)"
        ),
    )
    args = parser.parse_args()
    settings = load_settings()
    try:
        if args.mode == "conductor":
            asyncio.run(run_conductor(settings))
        else:
            asyncio.run(run_loop(settings))
    except KeyboardInterrupt:
        logger.info("stage3 loop interrupted; shut down cleanly")
    except EnvironmentTerminalError as env_exc:
        # T-gate-review-submit-failure-handling DESIGN v3 §3: an environment-scoped
        # terminal (a dead PAT, or a repo whose access this credential does not have
        # while the same credential answers ``GET /user`` fine) is NOT a fault of
        # this thread. Signalling it as exit code 2 tells the PowerShell wrapper NOT
        # to quarantine the thread — instead the wrapper alerts on the fault-class
        # key (``__github_credential__`` / ``__github_permission__/<owner>/<repo>``)
        # and continues the sweep. PR-C teaches the PS wrapper to distinguish
        # exit=2 from exit=1; until PR-C lands, exit=2 falls into the same
        # ``$code -ne 0`` branch exit=1 uses (verified by Bohr msg-3276 against
        # ``deploy/run-conductor-scheduled.ps1``: line 3681 uses ``-ne 0``, not
        # ``-eq 1``, so this PR-A is an ops-behaviour no-op).
        #
        # The payload line is JSON-shaped and printed to stdout on its OWN line,
        # prefixed with a fixed sentinel so the PS parser can locate it in a mixed
        # log stream. The PS side parses this only to build the dedup key; the
        # control-flow decision (do-not-quarantine) rides on the exit CODE, not on
        # the parse. If parsing fails, PS falls back to the global
        # ``__github_credential__`` key and still fires the notification — a
        # malformed payload never suppresses a critical environment alert
        # (Einstein v3 condition 1, msg-1988).
        _emit_environment_terminal_payload(env_exc)
        logger.warning(
            "environment-terminal exit=2: pr=%s scope=%s status_code=%s",
            env_exc.pr.slug,
            env_exc.scope.value,
            env_exc.status_code,
        )
        sys.exit(2)
    except BaseException as exc:
        # Exit-time SDK-error marker (T-sdk-is-error-loses-the-reason S-6,
        # second copy). Sequenced carefully because Python's default
        # ``sys.excepthook`` writes the traceback to stderr AFTER the except
        # block returns, and the wrapper's capture (``deploy/run-conductor-scheduled.ps1``:
        # ``$output = (& $inner *>&1) | …``) merges stderr into the same stream
        # it feeds into ``session_log_tail``. A plain ``raise`` here would let a
        # multi-frame async traceback (an ``SdkIsErrorSignal`` wrapped in
        # ``ClaudeCodeSdkDeliveryError`` inside ``asyncio.run`` easily spans
        # 30+ lines — the very shape observed in ``quarantine.json`` today)
        # push our marker out of the 50-line tail window, exactly the
        # regression the second copy exists to prevent. (PR #181 naysayer
        # review; the raise-site copy is the "belt", this is the "suspenders".)
        #
        # Fix: for the ``SdkIsErrorSignal`` case, PRINT the traceback ourselves
        # to stdout (same stream the marker uses, so their in-stream order is
        # deterministic — writes to two different pipes race for merge slots,
        # writes to the same pipe do not), THEN emit the marker, THEN exit
        # non-zero via ``sys.exit`` — whose ``SystemExit`` the default
        # excepthook special-cases NOT to print. So the marker is provably the
        # last thing on stdout, and the traceback is preserved above it.
        #
        # For any OTHER exception the block is a no-op — the plain ``raise``
        # keeps existing behaviour unchanged.
        sig = find_sdk_error_signal(exc)
        if sig is None:
            raise
        import traceback

        traceback.print_exception(exc, file=sys.stdout)
        sys.stdout.flush()
        emit_sdk_error_marker(sig.detail)
        sys.stdout.flush()
        # SystemExit with an int argument bypasses the default excepthook's
        # traceback print, so nothing more lands on stderr after the marker.
        # Exit code stays 1 to match the pre-change behaviour the wrapper's
        # ``if ($code -ne 0)`` branch relies on.
        sys.exit(1)


__all__ = [
    "PreflightError",
    "Stage3Conductor",
    "Stage3Loop",
    "Stage3ProposerAdapter",
    "build_conductor",
    "build_implementer",
    "build_loop",
    "build_naysayer",
    "build_pr_review_driver",
    "build_proposer",
    "build_registry",
    "build_watches",
    "main",
    "run_conductor",
    "run_loop",
]
