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
  ``T-pr-review-<n>`` thread, opened by
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
``ImplementerSdkAdapter``. We therefore run the proposer as a **text-only**
:class:`Stage3ProposerAdapter` that drops ``EXECUTE_CODE`` — which is also the
correct Stage 3 model (the proposer only proposes; the implementer executes,
gated). :func:`build_registry` then asserts the resolution fail-loud.

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

from .adapters.claude_code_sdk import ClaudeCodeSdkAdapter
from .adapters.implementer import ImplementerSdkAdapter
from .adapters.naysayer_sdk import NaysayerSdkAdapter
from .conductor import Conductor, ConductorOutcome
from .config import MindwireSettings, Stage3LoopConfig, load_settings
from .dispatcher.core import Dispatcher
from .dispatcher.event_log import (
    EVENT_FIELD_AUTHOR,
    EVENT_FIELD_ERROR,
    EVENT_KIND_DELIVERY_FAILED,
)
from .dispatcher.registry import InMemoryAdapterRegistry
from .magickit.client import McpToolCaller, StreamableHttpChatroomMcp
from .magickit.gateway import MagickitChatroomGateway
from .magickit.watcher import ChatroomWatcher, WatchSpec
from .naysayer.pr_review import NaysayerPrReviewDriver
from .orchestrator import PrReviewOrchestrator
from .ports import RoleAdapter
from .value_objects import Capability, Event, Role, ThreadRef

logger = logging.getLogger(__name__)


class Stage3ProposerAdapter(ClaudeCodeSdkAdapter):
    """Text-only proposer for the Stage 3 loop (same model as ``main``).

    Identical to :class:`~spirrow_mindwire.adapters.claude_code_sdk.ClaudeCodeSdkAdapter`
    at runtime (``tools=[]``, text-only), but advertises only
    ``{READ_THREAD, POST_REPLY}`` — it deliberately **drops** ``EXECUTE_CODE``.

    Why: the registry's Phase 1 ``qualified_for`` is "first qualified", and the
    base adapter declares ``EXECUTE_CODE`` (T16's dual-use, one adapter filling
    both proposer and implementer). With both it and
    :class:`~spirrow_mindwire.adapters.implementer.ImplementerSdkAdapter`
    registered, the IMPLEMENTER slot would resolve to whichever was registered
    first — risking the allow-list-gated implementer being shadowed by an
    un-gated adapter. Dropping ``EXECUTE_CODE`` here makes this adapter qualify
    for PROPOSER only, so the IMPLEMENTER slot resolves unambiguously to the
    gated adapter. It is also the correct Stage 3 model: the proposer proposes
    (text); only the implementer executes code, behind the allow-list.

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
            "PROPOSER slot did not resolve to the text-only proposer adapter "
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


def build_proposer(repo_dir: Path) -> Stage3ProposerAdapter:
    """Text-only proposer (same model family as ``main``)."""
    return Stage3ProposerAdapter(cwd=repo_dir)


def build_implementer(repo_dir: Path) -> ImplementerSdkAdapter:
    """Allow-list-gated implementer; inference base URL + allow-list from env/defaults.

    ``inference_base_url`` is left to the adapter's
    ``MINDWIRE_IMPLEMENTER_BASE_URL`` resolution; the adapter **refuses to
    spawn** if it is unset (no silent fallback to ``api.anthropic.com`` — ADR-07
    §2.4), which surfaces a misconfigured daemon loudly at first watch.
    """
    return ImplementerSdkAdapter(cwd=repo_dir)


def build_naysayer(repo_dir: Path) -> NaysayerSdkAdapter:
    """The single registry naysayer = the design-time agent (ADR-19 D-1 / driver-化 unify).

    The PR-review gate is no longer a registry RoleAdapter (it is the driver built by
    :func:`build_pr_review_driver`); the sole ``NAYSAYER_QUALIFIED`` adapter is this design-time
    agent (independence by distribution: ``MINDWIRE_NAYSAYER_BASE_URL`` → Lexora Gemini, resolved
    at spawn). It participates by summon, so the daemon registers it (the single-NAYSAYER
    invariant) without a standing watch.
    """
    return NaysayerSdkAdapter(cwd=repo_dir)


def build_pr_review_driver() -> NaysayerPrReviewDriver:
    """The Tier B develop→main PR-review driver (Lexora one-shot + GitHub T22, env-resolved)."""
    return NaysayerPrReviewDriver()


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
    """Observational event-log sink (I7): log reply.sent / delivery.failed."""
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
        if proposer is None:
            proposer = build_proposer(repo_dir)
        if implementer is None:
            implementer = build_implementer(repo_dir)
        if naysayer is None:
            naysayer = build_naysayer(repo_dir)

    registry = build_registry(proposer=proposer, implementer=implementer, naysayer=naysayer)
    gateway = MagickitChatroomGateway(mcp)
    dispatcher = Dispatcher(registry=registry, gateway=gateway, event_sink=_log_event_sink)
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
        pr_review_driver = build_pr_review_driver()

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

    Each configured watch is added with its own ``baseline`` (so a freshly
    opened task thread can be acted on with ``baseline=False``, while an ongoing
    thread uses the production-safe ``baseline=True``). ``loop.aclose()`` runs in
    ``finally`` so the SDK subprocess sessions disconnect (watcher.stop) AND the
    PR-review driver's HTTP clients close cleanly on shutdown (watcher docstring /
    msg-381 §E-1; Tier B #93 round-4 for the driver close).
    """
    cfg = settings.loop
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
        pr_review_driver = build_pr_review_driver()
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
            max_rounds=cond_cfg.max_rounds,
            orchestrator=orchestrator,
        )
    except ValueError as exc:
        raise SystemExit(f"conductor misconfigured ([conductor] in mindwire.toml): {exc}") from exc
    return Stage3Conductor(mcp=mcp, registry=registry, dispatcher=dispatcher, conductor=conductor)


async def run_conductor(settings: MindwireSettings) -> ConductorOutcome:
    """Build the conductor, drive the task thread once to a stop condition, and tear it down.

    The conductor's :meth:`~spirrow_mindwire.conductor.core.Conductor.run` is itself the serial poll
    loop (it re-reads the thread each turn); it returns when a D-4 stop condition is reached
    (``NEXT: human`` Tier-C decision point / ``NEXT: none`` settled / a malformed handoff or
    no-progress human fallback / the round cap). This entry therefore drives one design thread to
    its stop and exits — re-arming after the human responds is an operator / follow-up concern. The
    spawned adapter sessions are closed in ``finally`` so SDK subprocesses don't leak on shutdown.
    """
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
            "conductor finished: stop_reason=%s rounds=%d forced_naysayer=%d last_msg=%s",
            outcome.stop_reason.value,
            outcome.rounds,
            outcome.forced_naysayer_turns,
            outcome.last_msg_id,
        )
        return outcome
    finally:
        await cond.aclose()


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


__all__ = [
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
