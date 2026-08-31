"""D-2 close-failure visibility for the gate-bootstrap tick.

Design source: chatroom thread
``T-gate-bootstrap-close-refused-and-tick-crash`` (msg-2291 D-2 → msg-2293
D-2' → msg-2295 D-2'' → msg-2297 D-2''' → msg-2301 D-2''''). Read that thread top
to bottom before touching this module: the design was rewritten four times
under naysayer objections and every constraint here is load-bearing.

What this module does — and does not do
---------------------------------------

The problem it exists to close (F-1 in msg-2290): the gate-bootstrap tick's
``close_alert`` call has been failing on every tick, for every project, for
weeks — and the failure only surfaced in a log line no one reads. The
sweep wrapper is fail-open (a broken tick must not stop the sweep), which
in this case turned into fail-silent.

What this module gives it: when :func:`close_alert` refuses, the sweeper
posts a bounded failure report into the alert thread itself, so the same
face a human already visits ("why is this alert still open?") also carries
the reason it is stuck.

**Scope is close-only** — this mechanism does NOT run for ``open_alert``
failures (msg-2301 D-2''''). Three reasons in cascade, of which the second
is decisive:

1. **Physical target**: the failure report is posted into the alert thread.
   If ``open_alert`` failed, that thread does not exist — the report has
   nowhere to land.
2. **Rule inversion (decisive)**: :meth:`_clear_if_thread_not_open`'s
   asymmetric clear ("alert is not open → episode is over") treats
   "alert-not-open" as the goal. On the close path this is correct — the
   sweeper wanted the thread closed. On the open path the WORLD-STATE is
   inverted: "alert-not-open" means the failure is *actively persisting*,
   and applying the same rule would clear the state every tick, defeating
   the rate limiter. The result is not merely wrong; it is wrong in the
   direction that reintroduces the 5-minute-spam vector this whole module
   exists to close.
3. **Key shape**: :attr:`FailureEpisode.thread_id` is part of the episode
   key; a failed thread creation produces no thread_id to key on.

The judgement test — codified in :meth:`on_close_failure` and pinned by
``test_visibility_is_close_only`` — is a physical invariant:

    the failure itself must guarantee the existence of the reporting target.

If it does not, the failure must fall back to the existing log-only fail-open
path. A global visibility surface for target-less failures (a system digest)
is a separate design item and out of scope for this PR (msg-2301 §4).

State machine (D-2''' rules 1-3, D-2'' 2-field split)
--------------------------------------------------

Per-project state carries FOUR fields, in TWO logically-independent records:

- The **episode record**, keyed by ``(project, thread_id, signature)``:
    * ``signature`` — a stable short identifier for the failure kind
      (:class:`GateBootstrapCloseError` at present; the ``error_type`` field
      of the underlying envelope if we grow richer failure taxonomy).
    * ``first_seen_at`` — when this episode began.
    * ``reported_at`` — set ONLY on a successful post; used for dedup so
      "one episode → one report" holds even across restarts.
- The **rate-limit floor record**, keyed by ``(project,)`` ONLY:
    * ``last_attempt_at`` — written **write-ahead**, before any post is
      attempted, regardless of whether that post will succeed or fail. This
      is the entire basis of the 24-hour floor (Einstein msg-2294 objection:
      you cannot rate-limit *attempts* by recording only *successes*).

The two records are stored side-by-side in one file but the code reads and
writes them separately so a future edit cannot accidentally clear the floor
by clearing an episode (D-2''' Rule 2 — a rate limiter must never be reset by
the condition it is rate-limiting).

Evaluation order, in this order and no other, so the 24h upper bound holds
even if the dedup key drifts:

1. **Floor check** (project-only key): if ``now - last_attempt_at < 24h``,
   do nothing. Signature-independent so a drifting signature cannot open a
   backdoor around the limit.
2. **Dedup check** (episode key): if the same ``(thread_id, signature)`` is
   in the episode record with ``reported_at`` set, do nothing.
3. **Write-ahead the attempt**: persist ``last_attempt_at = now`` FIRST.
   If that persist fails, do NOT post — fail-closed. A post that cannot
   be rate-limited is a post we must not make.
4. **Attempt the post** once. Retries are not permitted within a tick
   (msg-2291 D-2 invariant: at most one attempted write per episode per
   tick, and never raise).
5. **On post success**, write ``reported_at = now``.
6. **On post failure**, do nothing further this tick. The floor now
   blocks any retry for 24 hours (msg-2295 D-2'' acceptance of the
   maximum-24h reporting-delay cost).

Episode clear (D-2''' Rule 1 — positive observation only):

- Close succeeded (was_open true OR false) → alert is not open → episode
  cleared. This is the normal path: the world state the sweeper wanted has
  arrived; the episode is over. Includes the "human closed manually" case
  (Einstein msg-2296 objection): ``was_open=False`` from a close call means
  the same thing as ``was_open=True`` for episode purposes.
- Close failed → the episode is NOT cleared. The failure state persists
  precisely because we could not verify the world state we wanted.
- Tick could not run at all → episode is NOT cleared (default to keeping
  state on unobserved ticks; the wrong direction is repeated reports, not
  silent stalls).

The clear NEVER touches the floor record (D-2''' Rule 2). Flapping
(failure → no-failure → failure) leaves the floor intact so a 5-minute
oscillation cannot restart the spam.

Invariants this module MUST NOT violate (raise on any):
- **The visibility path never raises to its caller.** Every failure inside
  this module (state persist, post refusal, network drop) is captured into
  a :class:`VisibilityReport` and returned. The tick loop treats visibility
  as a best-effort side channel; a broken visibility must not break the
  tick.
- **At most one post attempt per tick per project.** Retries are not
  permitted.
- **At most one post attempt per 24 hours per project** (write-ahead floor).
- **A read failure must never trigger a save.** Silently returning
  "empty state" on a read error and then persisting it would erase every
  OTHER project's floor and episode records — the "swallowed OSError
  makes on_close_failure's fail-closed guard dead code" defect the PR-gate
  on PR #209 caught (blocking #2). Only ``FileNotFoundError`` (the
  fresh-install case) is allowed to produce empty state; every other
  read or parse failure is propagated so the caller can decline to
  post AND decline to save.

The module is deliberately transport-shaped: it talks to the same
``McpToolCaller`` abstraction the rest of the codebase uses, so a fake for
tests is exactly the same shape as production. State I/O is behind a
:class:`FailureStateStore` protocol so tests do not touch the real
filesystem.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .config import DEFAULT_DATA_DIR
from .magickit.client import McpToolCaller

# The rate-limit floor — the ONE constant the whole objection cycle turns on.
# Named here (not scattered as a magic number) because msg-2295 D-2'' pinned
# it as a testable claim: "1 project につき 24 時間に 1 回を超えて post を
# 試みない". The test at ``test_floor_holds_across_failing_posts`` computes
# 24h / 5min = 288 ticks from this value and asserts exactly one attempt.
FAILURE_REPORT_FLOOR = timedelta(hours=24)

# The magickit post-message tool the failure report is sent through. Named
# separately from the ``report`` msg_type so an mcp caller substitution in
# tests remains string-comparable at a single point.
_POST_MESSAGE_TOOL = "chatroom_post_message"
_POST_MESSAGE_TYPE = "report"


def visibility_state_path() -> Path:
    """Resolve the state file path from ``MINDWIRE_PATHS__DATA_DIR`` or default.

    The state file sits under ``<data_dir>/state/`` next to leases.json and
    the other sweeper-owned state (matching deploy/lib/Lease.ps1's convention
    — see its header ``<data_dir>/state/leases.json``). One file per install,
    keyed internally by project; the whole gate-bootstrap failure state is
    at most a few hundred bytes per project.

    Kept as a module-level function (not a Config field) because this is
    sweeper-owned mechanical state, not a user-tunable knob. Adding it to
    ``config.py`` would invite the misreading that operators can move it
    independently of the other state files under ``<data_dir>/state/`` —
    the D-2 fail-closed invariant depends on the state actually being
    persisted, and a caller who split the paths could produce a
    silently-broken installation.
    """
    env_data_dir = os.environ.get("MINDWIRE_PATHS__DATA_DIR")
    data_dir = Path(env_data_dir).expanduser() if env_data_dir else DEFAULT_DATA_DIR
    return data_dir / "state" / "gate_bootstrap_failure.json"


@dataclass(frozen=True)
class FailureEpisode:
    """One episode of "close_alert failed for this project" — dedup record.

    Keyed by ``(project, thread_id, signature)`` per D-2''' Rule 3. The
    ``thread_id`` component matters when a human manually resolves and later
    re-opens an alert thread (with the same fixed id): the state must not
    permanently suppress the fresh episode's report. Because the current
    ``thread_id`` scheme is deterministic (``T-gate-bootstrap-<project>``)
    the thread_id contribution is a no-op in normal running, but including
    it costs nothing and future-proofs against non-deterministic ids.

    ``signature`` is intentionally kept short and structural (the exception
    class name, or the envelope's ``error_type`` if we surface it). The
    naysayer flagged its stability as unverified (msg-2295); the design's
    answer is that the **floor** is signature-INDEPENDENT (project-only key)
    so a drifting signature cannot break the 24h upper bound. This dedup
    record is the finer-grained inner layer, and its worst failure mode is
    "one extra report per unique signature per 24h" — bounded by the floor.
    """

    project: str
    thread_id: str
    signature: str
    first_seen_at: str  # ISO-8601 UTC
    reported_at: str | None = None  # ISO-8601 UTC, set on post-success only


@dataclass(frozen=True)
class RateLimitFloor:
    """The rate-limit floor — project-only key, signature-independent.

    Split from :class:`FailureEpisode` in D-2''' Rule 2. The two records
    share a file but are otherwise independent: an episode clear does NOT
    touch the floor (or a flap would restart the spam), and a floor entry
    can be present with no episode (an old failure whose thread has been
    closed manually — the floor still holds until 24h have elapsed).

    ``last_attempt_at`` is the only field; it is written **write-ahead**
    (before any post is attempted) so a mid-tick crash does not lose the
    attempt evidence and open the door to a next-tick retry.
    """

    project: str
    last_attempt_at: str  # ISO-8601 UTC


@dataclass(frozen=True)
class VisibilityReport:
    """The result of one :meth:`CloseFailureVisibility.on_close_failure` call.

    Never raised — the visibility mechanism cannot raise to its caller
    (see module docstring). Instead the outcome is captured here and made
    available on the tick's JSON output for the sweep wrapper's log line.

    Fields:
      * ``action`` — one of ``"posted"``, ``"floor_blocked"``,
        ``"dedup_blocked"``, ``"state_read_failed"``,
        ``"state_write_failed"``, ``"post_failed"``.
      * ``reason`` — human-readable summary. Machine consumers key on
        ``action``; ``reason`` is for the log reader.
      * ``episode`` — the episode record after the call, if any. Included
        so tests can inspect the post-condition without opening the state
        file.
    """

    action: str
    reason: str
    episode: FailureEpisode | None = None

    def as_dict(self) -> dict[str, Any]:
        """Machine-friendly form for the tick's JSON output object."""
        return {
            "action": self.action,
            "reason": self.reason,
            "episode": asdict(self.episode) if self.episode is not None else None,
        }


@dataclass
class _State:
    """The whole on-disk state as loaded — episodes + floors, one file per install.

    Loaded atomically at the start of each call and rewritten atomically at
    each mutation point. The all-in-one file is intentional: the number of
    projects is small (single-digit today, dozens as a ceiling) and the
    consistency guarantee is stronger with one file than with per-project
    files that can drift.
    """

    episodes: dict[str, FailureEpisode] = field(default_factory=dict)  # keyed by project
    floors: dict[str, RateLimitFloor] = field(default_factory=dict)  # keyed by project


class FailureStateStore(Protocol):
    """The persistence seam for the visibility mechanism.

    Two operations only: read the whole state, atomically write the whole
    state. Kept whole-file because the record set is small and the atomicity
    of a single-file rewrite is easier to reason about than a per-key store.
    """

    def load(self) -> _State: ...

    def save(self, state: _State) -> None: ...


class FileFailureStateStore:
    """Production :class:`FailureStateStore` — one JSON file under ``<data_dir>/state/``.

    Atomic rewrite via ``write text → os.replace``: the state either was the
    old version or the new version, never a half-written mix.

    **Load semantics (PR #209 gate-feedback fix — blocking #2):** ONLY
    ``FileNotFoundError`` is treated as "start empty" (the fresh-install
    case). Every other read or parse failure is PROPAGATED to the caller
    (:meth:`CloseFailureVisibility.on_close_failure`) so it can honour the
    fail-closed rule. Swallowing ``OSError`` here would make the
    ``except OSError`` guard in ``on_close_failure`` dead code, and — worse
    — a swallowed read failure returning an empty ``_State()`` would then
    be re-saved as an empty file, silently erasing every OTHER project's
    floor and episode records. Do not restore the previous silent
    fallback.

    ``json.JSONDecodeError`` and "top-level not a dict" are also
    propagated (as errors) for the same reason: a corrupt file must not
    be treated as "no file" during write-back, because the write-back
    would then paint over the corruption with a partial view.

    **Save atomicity (PR #209 gate-feedback fix — advisory #3):**
    Written to a unique per-process temp file via
    :func:`tempfile.NamedTemporaryFile` in the same directory, then
    ``os.replace``-d. A hard-coded ``.tmp`` sibling would race any
    concurrent tick that happens to run against the same install
    (the tick is currently invoked once per project by the sweep
    wrapper, which is sequential today — but the atomic-write contract
    should not assume that).
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> _State:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # The ONLY safe empty-state case: no file has ever been
            # written for this install. Every other read failure means
            # "there is state on disk that I cannot see", which is not
            # the same thing.
            return _State()
        # Any other OSError (PermissionError, transient FS glitch, etc.)
        # propagates. See class docstring.
        data = json.loads(raw)
        if not isinstance(data, dict):
            # Corrupt top-level shape → treat as a read failure, not as
            # empty state (see class docstring). Raising JSONDecodeError
            # lines up with what ``on_close_failure`` already catches.
            raise json.JSONDecodeError(
                "gate_bootstrap_failure.json top level is not a JSON object",
                raw,
                0,
            )
        return _decode_state(data)

    def save(self, state: _State) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Unique per-write temp file in the same directory, then
        # os.replace to become the target atomically. A statically
        # named ``.tmp`` sibling would race a concurrent tick and
        # produce a corrupt file (PR-gate advisory #3 on PR #209).
        # ``delete=False`` because we hand ownership to ``os.replace``.
        # ``dir=`` keeps the temp on the same filesystem so
        # ``os.replace`` is a rename, not a cross-filesystem copy.
        encoded = _encode_state(state)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=str(self._path.parent),
            prefix=self._path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as fd:
            tmp_path = Path(fd.name)
            fd.write(encoded)
        try:
            os.replace(tmp_path, self._path)
        except Exception:
            # Clean up the orphaned temp file on any failure between
            # write and replace, so a repeated write failure does not
            # accumulate stale tmp files forever.
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise


def _decode_state(data: Mapping[str, Any]) -> _State:
    episodes: dict[str, FailureEpisode] = {}
    for project, entry in (data.get("episodes") or {}).items():
        if not isinstance(project, str) or not isinstance(entry, Mapping):
            continue
        with contextlib.suppress(KeyError, TypeError):
            episodes[project] = FailureEpisode(
                project=project,
                thread_id=str(entry["thread_id"]),
                signature=str(entry["signature"]),
                first_seen_at=str(entry["first_seen_at"]),
                reported_at=(
                    str(entry["reported_at"]) if entry.get("reported_at") is not None else None
                ),
            )
    floors: dict[str, RateLimitFloor] = {}
    for project, entry in (data.get("floors") or {}).items():
        if not isinstance(project, str) or not isinstance(entry, Mapping):
            continue
        with contextlib.suppress(KeyError, TypeError):
            floors[project] = RateLimitFloor(
                project=project,
                last_attempt_at=str(entry["last_attempt_at"]),
            )
    return _State(episodes=episodes, floors=floors)


def _encode_state(state: _State) -> str:
    return json.dumps(
        {
            "episodes": {
                project: {
                    "thread_id": ep.thread_id,
                    "signature": ep.signature,
                    "first_seen_at": ep.first_seen_at,
                    "reported_at": ep.reported_at,
                }
                for project, ep in state.episodes.items()
            },
            "floors": {
                project: {"last_attempt_at": floor.last_attempt_at}
                for project, floor in state.floors.items()
            },
        },
        indent=2,
        sort_keys=True,
    )


class CloseFailureVisibility:
    """D-2 visibility mechanism — turn silent close refusals into one bounded post.

    See the module docstring for the full state machine. This class is one
    logical operation surface:

      * :meth:`on_close_failure` — called after a ``close_alert`` refusal.
        Consults the floor and the episode record, may post exactly one
        report, and returns a :class:`VisibilityReport`.
      * :meth:`on_close_success` — called after a positive observation
        that the alert thread is not open (successful close OR
        already-closed envelope). Clears any episode for this
        ``(project, thread_id)``; does NOT touch the floor (Rule 2).

    Both methods are total and non-raising — the tick's error path must
    not be complicated by the visibility mechanism's own failures.
    """

    def __init__(
        self,
        store: FailureStateStore,
        *,
        floor: timedelta = FAILURE_REPORT_FLOOR,
        now: Any = None,
    ) -> None:
        self._store = store
        self._floor = floor
        # ``now`` is a callable returning a UTC datetime, injectable for
        # deterministic tests. Production uses ``datetime.now(UTC)``.
        self._now = now or (lambda: datetime.now(UTC))

    def on_close_success(self, *, project: str, thread_id: str) -> None:
        """D-2''' Rule 1: positive observation that the alert is not open → clear episode.

        Does NOT touch the floor (Rule 2). The absence of a raise here is
        deliberate — a state failure at this point simply means the
        (now-obsolete) episode entry lingers, and the next tick will
        overwrite it. Compared to raising, that is the strictly less-bad
        failure mode.

        Also catches ``json.JSONDecodeError`` (widened after PR #209 gate
        blocking #2 — ``FileFailureStateStore.load`` no longer swallows
        it, so this call site has to). Same reasoning as ``on_close_failure``:
        a corrupt state file is a state we cannot reason about, and the
        safe response is to do nothing, not to overwrite it.
        """
        try:
            state = self._store.load()
            episode = state.episodes.get(project)
            if episode is None or episode.thread_id != thread_id:
                return
            del state.episodes[project]
            self._store.save(state)
        except (OSError, json.JSONDecodeError):
            return

    async def on_close_failure(
        self,
        mcp: McpToolCaller,
        *,
        project: str,
        thread_id: str,
        owner: str,
        exc: BaseException,
    ) -> VisibilityReport:
        """Consult the floor, consult dedup, at most post once — never raise.

        The order of the checks is fixed (module docstring §Evaluation
        order). Deviations from that order can invert the floor's guarantee.

        Physical-invariant guard (msg-2301 D-2''''): this method assumes the
        alert thread ``thread_id`` exists in ``project`` — it must be
        called only from the close path. The tick wires this correctly
        (see the comment on the open branch in ``gate_bootstrap_tick``);
        the guard here is a defence in depth. A caller that mis-wires it
        will still not corrupt state — the post itself would fail, and the
        floor would block further attempts for 24h.
        """
        signature = _failure_signature(exc)
        now = self._now()
        now_iso = now.isoformat()

        try:
            state = self._store.load()
        except (OSError, json.JSONDecodeError) as read_exc:
            # A read failure means we cannot see the floor — the
            # fail-closed rule (module docstring §Evaluation order
            # step 3) applies: do NOT post if we cannot rate-limit,
            # AND do NOT save (a save from empty state would silently
            # erase every OTHER project's floor and episode records —
            # PR-gate blocking #2 on PR #209).
            #
            # This ``except`` is now REACHABLE (previously
            # ``FileFailureStateStore.load`` swallowed OSError and
            # returned empty ``_State()``, making this block dead
            # code). ``JSONDecodeError`` is included because the load
            # path now propagates a corrupt file as a read failure for
            # the same reason: "corrupt file" and "no file" must not
            # collapse into the same code path, or the save would
            # paint over the corruption with a partial view.
            return VisibilityReport(
                action="state_read_failed",
                reason=(
                    f"state read failed ({type(read_exc).__name__}: {read_exc}); "
                    "refusing to post without a rate-limit floor and refusing "
                    "to save (would erase other projects' state)"
                ),
            )

        # 1. Floor (project-only key, signature-independent). See module
        #    docstring for why this is checked FIRST.
        floor_entry = state.floors.get(project)
        if floor_entry is not None:
            last_attempt = _parse_iso(floor_entry.last_attempt_at)
            if last_attempt is not None and now - last_attempt < self._floor:
                return VisibilityReport(
                    action="floor_blocked",
                    reason=(
                        f"rate-limit floor active: last attempt at {floor_entry.last_attempt_at}, "
                        f"floor {self._floor}"
                    ),
                    episode=state.episodes.get(project),
                )

        # 2. Dedup (episode key). If a matching episode has already reported,
        #    do not report again.
        existing = state.episodes.get(project)
        if (
            existing is not None
            and existing.thread_id == thread_id
            and existing.signature == signature
            and existing.reported_at is not None
        ):
            return VisibilityReport(
                action="dedup_blocked",
                reason=(
                    f"episode already reported at {existing.reported_at} (signature={signature!r})"
                ),
                episode=existing,
            )

        # 3. Write-ahead the attempt. Persist the floor entry (and, if this
        #    is a new episode, the episode record) BEFORE attempting the
        #    post. This is what makes the "≤ 1 attempt / 24h" claim
        #    actually enforceable — writing the floor only on post-success
        #    would let a persistently failing post loop 288 times per 24h
        #    (Einstein msg-2294 objection).
        state.floors[project] = RateLimitFloor(project=project, last_attempt_at=now_iso)
        if existing is None or existing.thread_id != thread_id or existing.signature != signature:
            # New episode — write it now (without reported_at).
            episode = FailureEpisode(
                project=project,
                thread_id=thread_id,
                signature=signature,
                first_seen_at=now_iso,
                reported_at=None,
            )
            state.episodes[project] = episode
        else:
            episode = existing
        try:
            self._store.save(state)
        except OSError as save_exc:
            # Fail-closed: cannot persist the write-ahead attempt marker →
            # cannot enforce the floor → do not post. Log-only fallback via
            # the tick's outer error path.
            return VisibilityReport(
                action="state_write_failed",
                reason=(f"state write failed ({type(save_exc).__name__}); refusing to post"),
            )

        # 4. Attempt the post exactly once. Any exception is captured, not
        #    raised — the visibility mechanism never raises to the tick.
        body = _format_visibility_report(project=project, thread_id=thread_id, exc=exc)
        arguments: dict[str, Any] = {
            "project": project,
            "thread_id": thread_id,
            "msg_type": _POST_MESSAGE_TYPE,
            "author": owner,
            "content": body,
        }
        try:
            await mcp.call_tool(_POST_MESSAGE_TOOL, arguments)
        except Exception as post_exc:
            # Post failed: the write-ahead floor entry already persisted at
            # step 3, so the next 24 hours are blocked. Episode is retained
            # WITHOUT ``reported_at``, so a signature drift (a different
            # failure kind next episode) will get its own report — the floor
            # is still the outer bound. ``except Exception`` is intentional
            # and pinned by the module docstring: this method never raises
            # to its caller. A BaseException (KeyboardInterrupt, SystemExit)
            # still escapes, which is what we want — the operator's Ctrl-C
            # should not be silently converted to "post_failed".
            return VisibilityReport(
                action="post_failed",
                reason=(
                    f"chatroom_post_message failed "
                    f"({type(post_exc).__name__}: {post_exc}); "
                    "floor holds; no retry this tick"
                ),
                episode=episode,
            )

        # 5. Post succeeded → mark reported_at.
        reported_episode = FailureEpisode(
            project=episode.project,
            thread_id=episode.thread_id,
            signature=episode.signature,
            first_seen_at=episode.first_seen_at,
            reported_at=now_iso,
        )
        state.episodes[project] = reported_episode
        # Loss of the reported_at mark is a benign failure: the floor entry
        # already blocks the next 24h of attempts, so no spam results.
        # Worst case: one extra report after the 24h window if the write
        # eventually recovers but this specific mark did not persist.
        # Acceptable per the module docstring's stated trade-offs.
        with contextlib.suppress(OSError):
            self._store.save(state)
        return VisibilityReport(
            action="posted",
            reason=f"failure report posted to {thread_id} for signature {signature!r}",
            episode=reported_episode,
        )


def _failure_signature(exc: BaseException) -> str:
    """A stable short identifier for the failure kind.

    The design (msg-2293 D-2') prefers a structural signature over the
    exception's free-form message, which drifts. For now the class name is
    stable enough — :class:`GateBootstrapCloseError` for a role-check
    refusal, :class:`MagickitMcpError` for transport, etc. If magickit ever
    surfaces the ``error_type`` field on a wrapped envelope, this function
    is the one place to extend.

    The **floor** is signature-INDEPENDENT (see module docstring), so a
    drift in this signature does not break the 24h bound — it only affects
    the finer-grained per-signature dedup.
    """
    return type(exc).__name__


def _format_visibility_report(*, project: str, thread_id: str, exc: BaseException) -> str:
    """The body of the failure report posted into the alert thread.

    Kept short, no LLM call, and explicit about being a *machine notice*
    rather than a design proposal — the alert thread already contains the
    system-alert semantics; this report piggybacks on that framing without
    re-negotiating it.

    The exception message is truncated to a bounded length; the same
    bounded-by-construction rule ``magickit/client.py`` uses for its own
    elevation messages applies here.
    """
    message = str(exc)
    if len(message) > 2000:
        message = message[:2000] + "…[truncated]"
    return (
        "[system-notice · gate-bootstrap · close-refused]\n\n"
        f"The sweeper attempted to close this alert thread "
        f"({thread_id}, project {project}) after `.mindwire-gate` was "
        "observed to be declared, and the `chatroom_close_thread` call "
        "was refused.\n\n"
        "This is a machine notice, not a design proposal. Details:\n\n"
        f"    {message}\n\n"
        "**What is and is not rate-limited (read this carefully — the "
        "prior version of this notice got it wrong; see PR #209 gate "
        "feedback):**\n\n"
        "- **The close attempt itself is NOT rate-limited.** The sweeper "
        "continues to call `close_alert` on THIS thread on every "
        "5-minute tick. The moment the underlying cause is fixed "
        "upstream, the next tick will close this thread and no further "
        "action is required — do NOT wait 24 hours.\n"
        "- **Only THIS report is rate-limited.** The visibility "
        "mechanism will not re-post the same failure notice into this "
        "thread for 24 hours, even if the same close_alert refusal "
        "recurs on every intervening tick. So a stale-looking absence "
        'of new reports here means "nothing NEW to say", not '
        '"nothing to try".\n\n'
        "If the underlying cause is a policy refusal (role registry, "
        "closeable_roles), fix it upstream and the next tick will "
        "succeed and clear the failure state. "
        "Design ref: chatroom thread "
        "`T-gate-bootstrap-close-refused-and-tick-crash`."
    )


def _parse_iso(text: str) -> datetime | None:
    """Parse a UTC ISO-8601 timestamp, tolerating both ``Z`` and offset forms.

    Returns ``None`` on malformed input rather than raising — the caller
    treats a missing floor timestamp as "no floor", which is the same as
    the fresh-install case.
    """
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


__all__ = [
    "FAILURE_REPORT_FLOOR",
    "CloseFailureVisibility",
    "FailureEpisode",
    "FailureStateStore",
    "FileFailureStateStore",
    "RateLimitFloor",
    "VisibilityReport",
    "visibility_state_path",
]
