"""Stage 3 autonomous-loop daemon (``mindwire-loop``) — ADR-2026-05-21-06 §4/§7.

The production console-script that assembles the Stage 3 RoleAdapter loop into a
running daemon: it wires the three production adapters into the **existing**
``Dispatcher`` + ``ChatroomWatcher`` (T13/T14) over the magickit chatroom and
runs the poll loop. This is the "last piece" the loop was missing — every
component it composes (watcher, dispatcher, registry, adapters, gateway,
orchestrator) already exists; this module only *wires and runs* them.

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

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from .adapters.claude_code_sdk import ClaudeCodeSdkAdapter
from .adapters.implementer import ImplementerSdkAdapter
from .adapters.naysayer_sdk import NaysayerSdkAdapter
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


def build_loop(
    settings: MindwireSettings,
    *,
    mcp: McpToolCaller | None = None,
    proposer: RoleAdapter | None = None,
    implementer: RoleAdapter | None = None,
    naysayer: RoleAdapter | None = None,
    pr_review_driver: NaysayerPrReviewDriver | None = None,
) -> Stage3Loop:
    """Assemble the Stage 3 loop from settings (composition root).

    Components may be injected (tests pass fakes); anything left ``None`` is
    built from ``settings`` / the environment. Raises ``SystemExit`` if
    ``loop.repo_dir`` is unset and an SDK adapter must be built from config.
    The ``naysayer`` is the design-time SDK agent (the sole registry NAYSAYER);
    the PR-review gate is the ``pr_review_driver`` wired into the orchestrator
    (ADR-19 driver-化 unify), not a registered adapter.
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
    if pr_review_driver is None:
        pr_review_driver = build_pr_review_driver()

    registry = build_registry(proposer=proposer, implementer=implementer, naysayer=naysayer)
    gateway = MagickitChatroomGateway(mcp)
    dispatcher = Dispatcher(registry=registry, gateway=gateway, event_sink=_log_event_sink)
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
    thread uses the production-safe ``baseline=True``). ``watcher.stop()`` runs
    in ``finally`` so the SDK subprocess sessions disconnect cleanly on shutdown
    (watcher docstring / msg-381 §E-1).
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
        await loop.watcher.stop()


def main() -> None:
    """Entry point for the ``mindwire-loop`` console script."""
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    try:
        asyncio.run(run_loop(settings))
    except KeyboardInterrupt:
        logger.info("stage3 loop interrupted; shut down cleanly")


__all__ = [
    "Stage3Loop",
    "Stage3ProposerAdapter",
    "build_implementer",
    "build_loop",
    "build_naysayer",
    "build_pr_review_driver",
    "build_proposer",
    "build_registry",
    "build_watches",
    "main",
    "run_loop",
]
