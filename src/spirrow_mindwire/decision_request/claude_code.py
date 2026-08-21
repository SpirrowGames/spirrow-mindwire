"""Claude Code backend for the decision-request composer (slice S3).

SOT for this file: ``spec/slices/S3-claude-code-composer.md``. Every
decision annotated ``D-3x`` below points at that document; when they
disagree, the spec wins and this file gets updated to match (see the
"Conflict rule: file wins" clause in the spec header, restated per
msg-1406 / §20.2).

Design intent (verbatim from the spec, D-35..D-43):

- **D-35** headless subprocess, one process per stop, no daemon.
- **D-36** child gets NO tools (structural, not prompt-level, enforcement
  of I-1 / I-5). `--allowedTools ""` and `--disallowedTools "*"`.
- **D-37** child does not inherit role context: `settings-source` off,
  cwd OUTSIDE the repo, env scrubbed. ``argv_digest`` in extras so an
  operator can retro-check the launch shape.
- **D-38** tail fetched Python-side (in the CLI), NEVER by the child
  (which stays chatroom-mute per D-36). The composer here only renders
  what the CLI already put in ``request.tail`` — it does not fetch.
- **D-39** prompt = one system + one user, no few-shot. ``PROMPT_VERSION``
  is recorded to extras so a retrospective can bind quality to a version.
- **D-41** every failure mode fail-opens through the exception hierarchy;
  I-2 is preserved (the wrapper falls back to the raw ping).
- **D-42** cost / latency / model / turns propagated verbatim from the
  child's structured output. Model name is NEVER hard-coded here.
- **D-43** child stdout is decoded EXPLICITLY as UTF-8 (bytes → utf-8);
  ``subprocess.run(..., text=True)`` is DELIBERATELY not used because
  the platform default (cp932 on the Windows deploy host) would mojibake
  Japanese under the same failure shape §14 caught on the outer
  boundary.

Value-object policy (spec §19.4 冒頭): ports and value objects are NOT
modified in S3. Extras are attached via a duck-typed
:attr:`ClaudeCodeComposer.last_extras` attribute that :func:`compose_once`
reads with ``getattr`` — see ``spec/slices/S3-claude-code-composer.md``
"Extras carrier" for the rationale.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Protocol

from .exceptions import (
    DecisionComposerEmptyError,
    DecisionComposerError,
    DecisionComposerTimeoutError,
)
from .value_objects import (
    DecisionOption,
    DecisionRequestInput,
    DecisionRequestOutput,
)

# --------------------------------------------------------------------------- #
# Constants (spec §D-35, §D-39, §D-42)
# --------------------------------------------------------------------------- #

DEFAULT_CLAUDE_CLI = "claude"
"""Executable name for the Claude Code CLI. Overridable via constructor.

Kept as a plain name (not a path) so that PATH resolution in the child's
scrubbed environment is what wins — the deploy host and CI both put a
resolvable ``claude`` on PATH, and a hard-coded path here would silently
diverge from what an operator sees when they run the same command by hand.
"""

DEFAULT_TIMEOUT_SECONDS = 60
"""Wall-clock ceiling for one child invocation.

Matches the S2 wrapper's ``$DecisionComposerTimeoutSeconds`` (also 60 s).
The number is not duplicated in the wrapper's config — the wrapper passes
its own value in explicitly. This is the fallback for callers who did not
plumb one through (tests, ad-hoc).

**D-45 (Tier-C msg §25.2)**: originally 30 s. Raised to 60 s after A-18
measured the real end-to-end elapsed at **33,812 ms** on a live parked
thread (``spirrow-voxelworld/T-T227-P0-spec-kickoff``, tail 6 msgs /
21,026 chars). 30 s would have tripped the timeout on real inputs while
staying green on stub tests. Tail was **not** trimmed to buy time: §25.1
records the 21 KB input producing a high-quality question (F-1 rubric
satisfied), and trading quality for latency defeats the purpose of case B.
The extra 30 s of ceiling costs at most 60 s of notification delay on
composer failure, negligible against the measured 8-11 h human response
latency (I-2 fallback still fires the raw ping; ceiling only bounds how
long the wrapper waits before falling back).
"""

PROMPT_VERSION = "1"
"""Bumped whenever the system-prompt text below changes.

Written verbatim to ``envelope.extras["prompt_version"]`` so a future
retrospective can bind output quality to a prompt revision without
archaeology. Version bumps are additive edits to this string — the
number does not need to be semver, just monotonic.
"""

_INTERNAL_TAIL_BODY_HARD_CEILING = 10_000
"""Absolute per-body ceiling applied here regardless of what the caller set.

Belt-and-braces for D-38: the CLI applies the caller's ``--body-cap`` (default
4000) before handing the tail over, but a caller that forgot to cap would
still be bounded by this. Set well above the CLI default so a legitimate
larger cap (a future 8000-char experiment) is not clipped silently.
"""


# --------------------------------------------------------------------------- #
# Subprocess-runner seam (D-37 test surface)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SubprocessResult:
    """What the runner returns; a subset of :class:`subprocess.CompletedProcess`.

    Bytes, not text: D-43 requires an explicit UTF-8 decode step in the
    composer, and returning ``str`` here would obscure whether the runner
    already decoded (and thus whether the cp932 boundary bug reappeared).
    """

    returncode: int
    stdout: bytes
    stderr: bytes


class SubprocessRunner(Protocol):
    """Callable that runs one child process and returns bytes.

    Broken out so tests can inject a fake runner without spawning
    ``claude``. The real implementation is :func:`_default_runner` below.
    """

    def __call__(
        self,
        *,
        argv: list[str],
        input_bytes: bytes,
        cwd: str,
        timeout: float,
        env: dict[str, str],
    ) -> SubprocessResult:  # pragma: no cover
        ...


def _default_runner(
    *,
    argv: list[str],
    input_bytes: bytes,
    cwd: str,
    timeout: float,
    env: dict[str, str],
) -> SubprocessResult:
    """Real subprocess runner.

    D-43 lives here: ``capture_output=True`` returns bytes; we do NOT set
    ``text=True`` / ``encoding=...`` — decode is the composer's job so
    it can be done explicitly as UTF-8.
    """
    completed = subprocess.run(
        # allowlist inside _build_argv; no shell=True, no user string.
        argv,
        input=input_bytes,
        capture_output=True,
        cwd=cwd,
        timeout=timeout,
        env=env,
        check=False,  # non-zero exit is handled by D-41 mapping, not a raise.
    )
    return SubprocessResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


# --------------------------------------------------------------------------- #
# Composer
# --------------------------------------------------------------------------- #


DEFAULT_COMPOSER_IDENTITY = "Composer"
"""Default identity_name for :class:`ClaudeCodeComposer`.

I-5 forbids this from shadowing a design-roster role (Bohr / Heisenberg /
Einstein). "Composer" is deliberately picked to be unmistakable: it names
the role of this piece and cannot be misread as one of the three
designers. The wrapper's operator config is the enforcement surface — a
deploy that overrides ``MINDWIRE_DECISION_COMPOSER_IDENTITY=Bohr`` is
the operator's mistake to see.
"""


class ClaudeCodeComposer:
    """Compose a decision request by shelling out to ``claude``.

    Instances are single-use in the sense that ``last_extras`` is
    overwritten on every ``compose()`` call — do NOT share one instance
    between concurrent stops. The CLI (:mod:`.cli`) constructs one per
    invocation, which is the only production caller.

    The class deliberately does NOT hold any long-lived resources
    (no ``claude`` daemon, no cached process) — D-35: one process per
    stop. Every ``compose()`` call is a fresh subprocess.
    """

    identity_name: str
    """See :class:`DecisionRequestComposer.identity_name`."""

    last_extras: dict[str, str]
    """Per-call telemetry for the envelope's ``extras`` field.

    Populated by :meth:`compose`; read by :func:`.cli.compose_once`
    via ``getattr(composer, "last_extras", {})``. This attribute is the
    "extras carrier" the S3 spec references — it lets us record backend
    telemetry without adding a field to :class:`DecisionRequestOutput`
    (spec §19.4 冒頭: "port も value object も PS 側も形を変えない").
    """

    def __init__(
        self,
        *,
        identity_name: str = DEFAULT_COMPOSER_IDENTITY,
        cli_path: str = DEFAULT_CLAUDE_CLI,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        cwd: str | None = None,
        runner: SubprocessRunner | None = None,
    ) -> None:
        self.identity_name = identity_name
        self._cli_path = cli_path
        self._timeout_seconds = timeout_seconds
        # D-37: cwd OUTSIDE the repo. tempfile.gettempdir() gives an
        # OS-specific temp dir we never write to — the child launches
        # there, walks upward looking for CLAUDE.md, and finds nothing
        # (or finds unrelated files that are ignored). Callers can
        # override for tests.
        self._cwd = cwd if cwd is not None else tempfile.gettempdir()
        self._runner = runner if runner is not None else _default_runner
        self.last_extras = {}

    # ---- public API ------------------------------------------------------- #

    def compose(self, request: DecisionRequestInput) -> DecisionRequestOutput:
        """Run one child process and return the composed output.

        Failure surface (D-41):

        - :class:`DecisionComposerTimeoutError` on
          :class:`subprocess.TimeoutExpired`.
        - :class:`DecisionComposerError` on missing binary, non-zero
          exit, un-parseable CLI JSON, missing model text, un-parseable
          model JSON.
        - :class:`DecisionComposerEmptyError` on empty ``question`` or
          fewer than 2 ``options``.

        Any other exception the runner or the parser raises is
        propagated — the CLI's outer ``except Exception`` (:func:`.cli.compose_once`)
        catches those and packages an ``error`` envelope, so I-2 is
        never bypassed. This method deliberately does not swallow
        unexpected errors; a broken backend that raised the wrong class
        is worth surfacing verbatim in the envelope.
        """
        # Reset extras first, so a re-run on the same instance never
        # accidentally shows the previous call's telemetry.
        self.last_extras = {}

        system_prompt = self._render_system_prompt()
        user_prompt = self._render_user_prompt(request)
        argv = self._build_argv(system_prompt=system_prompt)
        argv_digest = self._digest_argv(argv)
        env = self._make_child_env()

        # Record baseline extras BEFORE spawning the child, so every failure
        # branch below — including subprocess.TimeoutExpired and
        # FileNotFoundError (which SKIP the whole post-runner block) — still
        # surfaces the argv digest and neutral-cwd fingerprint in the envelope.
        # This is what makes A-3 diagnostics ("did that timed-out call actually
        # launch under the neutral setup?") answerable from the envelope alone
        # without re-running the composer. Placing the assignment AFTER the
        # runner call was a bug caught by the pr-gate on PR #169 — the timeout
        # and spawn-failure paths would have lost the fingerprint precisely
        # when it is most needed.
        self.last_extras = {
            "backend": "claude-code",
            "argv_digest": argv_digest,
            "prompt_version": PROMPT_VERSION,
            "cwd": self._cwd,
        }

        try:
            result = self._runner(
                argv=argv,
                input_bytes=user_prompt.encode("utf-8"),
                cwd=self._cwd,
                timeout=self._timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise DecisionComposerTimeoutError(
                f"claude CLI timed out after {self._timeout_seconds}s"
            ) from exc
        except FileNotFoundError as exc:
            raise DecisionComposerError(f"claude CLI not found ({self._cli_path}): {exc}") from exc
        except OSError as exc:
            raise DecisionComposerError(
                f"claude CLI failed to start ({self._cli_path}): {exc}"
            ) from exc

        # D-43: explicit UTF-8 decode. `errors="replace"` chosen over `strict`
        # so a stray malformed byte in stderr does not sink the whole path —
        # the child's ok output is what actually matters, and a replacement
        # character in a diagnostic message is still readable.
        stdout_text = result.stdout.decode("utf-8", errors="replace")
        stderr_text = result.stderr.decode("utf-8", errors="replace")

        if result.returncode != 0:
            stderr_tail = stderr_text.strip().splitlines()[-5:]
            raise DecisionComposerError(
                f"claude CLI exit={result.returncode}; stderr tail: {stderr_tail}"
            )

        # The Claude Code CLI's `--output-format json` shape: one top-level
        # object with `result` (the assistant text), plus `duration_ms`,
        # `total_cost_usd`, `num_turns`, `model`. Keys can vary across CLI
        # versions — we pull known aliases and skip missing keys silently.
        try:
            cli_json = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise DecisionComposerError(
                f"claude CLI stdout is not JSON: {exc}; head: {stdout_text[:200]!r}"
            ) from exc
        if not isinstance(cli_json, dict):
            raise DecisionComposerError(
                f"claude CLI stdout JSON is not an object: type={type(cli_json).__name__}"
            )

        # D-42: fold known telemetry into extras. Whatever the child reports.
        # Model name is NEVER hard-coded here — msg-1370 §2 port neutrality.
        _fold_telemetry(cli_json, self.last_extras)

        result_text = _extract_result_text(cli_json)
        if result_text is None:
            raise DecisionComposerError(
                "claude CLI JSON has no assistant text (looked for keys: result, text, output)"
            )

        try:
            answer = _parse_json_from_result_text(result_text)
        except ValueError as exc:
            raise DecisionComposerError(
                f"claude output is not valid JSON: {exc}; head: {result_text[:200]!r}"
            ) from exc

        return _shape_composer_answer(answer, tail_used=len(request.tail))

    # ---- prompt helpers --------------------------------------------------- #

    def _render_system_prompt(self) -> str:
        """The system prompt. D-39 (a)..(e).

        The text is intentionally boring: this is not where a composer
        gets clever. Every property (does-not-decide, ≥2 options,
        gain/loss/label, one recommendation with a fact-grounded reason,
        unknowns declared, JSON only) is stated as a hard rule so the
        rejection at the parse stage is unambiguous when a rule breaks.
        """
        return _SYSTEM_PROMPT

    def _render_user_prompt(self, request: DecisionRequestInput) -> str:
        """The user prompt. D-38 shape (see spec §D-39 "User-prompt shape").

        Everything the model sees comes from ``request``. There is no
        hidden context; F-1's "what wasn't enough?" report can be
        reconstructed from the tail exactly as it was handed in.
        """
        header_lines = [
            f"project: {request.project}",
            f"thread: {request.thread_title or request.thread_id} ({request.thread_id})",
            (
                f"last message: {request.last_msg_id}, "
                f"stop_reason={request.stop_reason}, rounds={request.rounds}"
            ),
        ]
        header = "\n".join(header_lines)

        if request.tail:
            body_parts: list[str] = []
            for m in request.tail:
                body = m.body
                if len(body) > _INTERNAL_TAIL_BODY_HARD_CEILING:
                    body = body[:_INTERNAL_TAIL_BODY_HARD_CEILING] + "… (省略)"
                body_parts.append(f"--- {m.msg_id} by {m.author} ---\n{body}")
            tail_block = (
                f"tail (last {len(request.tail)} of {request.total_messages} messages):\n\n"
                + "\n\n".join(body_parts)
            )
        else:
            tail_block = (
                f"tail (0 of {request.total_messages} messages): "
                "no tail was provided; ground the recommendation in what is stated "
                "in the header and record unknowns for anything the tail would "
                "normally answer."
            )

        return (
            f"{header}\n\n{tail_block}\n\n"
            "Task: read the tail and produce the decision-request JSON described in the "
            "system prompt. Output JSON only."
        )

    # ---- process helpers -------------------------------------------------- #

    def _build_argv(self, *, system_prompt: str) -> list[str]:
        """Construct the child's argv.

        D-36: no tools. D-37: settings source off. The concrete flag names
        (``--allowedTools`` / ``--disallowedTools`` / ``--setting-sources``)
        are what the current Claude Code CLI uses; if a future version
        renames them, this list is the single place to update.
        """
        return [
            self._cli_path,
            "-p",  # print / headless
            "--output-format",
            "json",
            "--allowedTools",
            "",  # D-36
            "--disallowedTools",
            "*",  # D-36 (belt-and-braces with empty allow)
            "--append-system-prompt",
            system_prompt,  # D-39 property list injected here
            # D-37: refuse to read persona settings from the launch dir.
            # The exact flag has churned across Claude Code versions; the
            # current one is `--setting-sources` with an empty value.
            "--setting-sources",
            "",
        ]

    def _digest_argv(self, argv: list[str]) -> str:
        """First 16 hex chars of ``sha256(" ".join(argv))``.

        Separator is a plain space, matching the spec exactly
        ``spec/slices/S3-claude-code-composer.md`` §D-37: the SOT names
        the join string so an operator can reproduce the digest by hand
        with a shell-level ``echo -n "$argv" | sha256sum | head -c 16``.
        Using a null byte was tempting for argv-boundary unambiguity, but
        that convenience is not the SOT's concern — reproducibility from
        the documented recipe is (naysayer PR-gate on PR #169).

        Recorded to extras so an operator can retro-check "was this launch
        actually the neutral one, or did a persona flag leak in?" without
        having to keep the full command line around. Truncated to 16 chars
        because uniqueness across a handful of launches is enough for the
        diagnostic — this is not a security-grade fingerprint.
        """
        joined = " ".join(argv).encode("utf-8")
        return hashlib.sha256(joined).hexdigest()[:16]

    def _make_child_env(self) -> dict[str, str]:
        """Scrub the environment (D-37).

        Keeps PATH (so ``claude`` is findable) and a small allowlist of
        variables the CLI genuinely needs (HOME on POSIX, USERPROFILE on
        Windows — the CLI writes state under one of those). Explicitly
        drops MINDWIRE_* and any inherited PYTHONIOENCODING (D-43: we
        do not want an env variable to be what makes the encoding work,
        which would silently break under a different caller).

        Case-insensitive on the KEY only: Windows preserves whatever case
        an environment variable was created with (``Path``, ``SystemRoot``,
        ``AppData``, etc.). A case-sensitive membership check would silently
        strip ``Path`` on Windows — the child would then be launched without
        PATH and every ``subprocess.run(argv[0])`` would ``FileNotFoundError``.
        Dropping ``SystemRoot`` breaks CreateProcess's own crypto/networking
        subsystems. The naysayer PR-gate on PR #169 caught this.
        """
        allowed = {
            "PATH",
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "TEMP",
            "TMP",
            "SYSTEMROOT",
            # ANTHROPIC_API_KEY is what the CLI reads to authenticate.
            # If the deploy uses `claude login` instead, this is absent
            # and the CLI reads its own credential file — that is fine.
            "ANTHROPIC_API_KEY",
            # D-44 (Tier-C msg §24): the sg-ai-server-01 deploy host has
            # NO direct egress — the ONLY route to api.anthropic.com is
            # through the squid proxy exported via HTTP_PROXY / HTTPS_PROXY
            # (with NO_PROXY carrying the LAN exceptions). Dropping these
            # makes `claude -p` fail INSIDE the CLI with
            # ``terminal_reason:"api_error"`` and ``duration_api_ms:0`` —
            # the API call never leaves the box. That failure fails-open
            # through I-2 to the raw ping, so nothing screams: composer
            # silently produces no questions in production while CI stays
            # green (the whole class is stub-only). D-37's intent is to
            # scrub ROLE CONTEXT and TOOLS, not the network egress route —
            # a proxy is neither. Case-insensitive membership handles the
            # POSIX lowercase forms (http_proxy / https_proxy / no_proxy)
            # transparently.
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
        }
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        # DELIBERATELY not propagating PYTHONIOENCODING (D-43 principle).
        return env


# --------------------------------------------------------------------------- #
# Free-standing helpers (module-scope so tests can hit them without a composer)
# --------------------------------------------------------------------------- #


# Aliases the Claude Code CLI has used across versions for its telemetry
# fields. Keeping this list here (instead of inline) makes a version change
# a one-line data edit.
_TELEMETRY_ALIASES: dict[str, tuple[str, ...]] = {
    "duration_ms": ("duration_ms", "duration", "elapsed_ms"),
    "total_cost_usd": ("total_cost_usd", "cost_usd", "total_cost"),
    "num_turns": ("num_turns", "turns"),
    "model": ("model", "model_id"),
}


def _fold_telemetry(cli_json: dict[str, Any], extras: dict[str, str]) -> None:
    """Pull known telemetry keys from the CLI JSON into the extras dict.

    Missing keys are simply skipped — a partial CLI output should not
    downgrade the whole envelope, and a wrapper reader must tolerate
    missing telemetry keys (see spec "Extras (envelope)" table).
    """
    for target_key, aliases in _TELEMETRY_ALIASES.items():
        for alias in aliases:
            if alias in cli_json:
                value = cli_json[alias]
                # Everything in extras is a string (envelope shape).
                extras[target_key] = str(value)
                break


_RESULT_KEYS = ("result", "text", "output", "content")


def _extract_result_text(cli_json: dict[str, Any]) -> str | None:
    """Find the assistant's text in the CLI's JSON output.

    Claude Code has used several key names for the assistant text across
    versions. We check them in a stable order; the first non-empty string
    wins. Returns ``None`` if none of the known keys carry usable text —
    caller raises :class:`DecisionComposerError` in that case (D-41).
    """
    for key in _RESULT_KEYS:
        value = cli_json.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _parse_json_from_result_text(text: str) -> dict[str, Any]:
    """Parse the JSON object the model wrote into ``text``.

    The system prompt says "JSON only" — a compliant response IS a JSON
    object. But even compliant models occasionally wrap the JSON in a
    ```json fenced block or add a trailing sentence; we accept those
    forms by finding the first ``{`` and matching to the last ``}`` and
    parsing the slice. If the slice does not parse, raise ``ValueError``.
    """
    stripped = text.strip()
    # Fast path: whole string is JSON.
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            if not isinstance(parsed, dict):
                raise ValueError(f"top-level JSON is not an object: {type(parsed).__name__}")
            return parsed

    # Slow path: substring from first `{` to last `}`.
    open_idx = stripped.find("{")
    close_idx = stripped.rfind("}")
    if open_idx == -1 or close_idx == -1 or close_idx <= open_idx:
        raise ValueError("no JSON object found in text")
    candidate = stripped[open_idx : close_idx + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"substring is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"substring JSON is not an object: {type(parsed).__name__}")
    return parsed


def _shape_composer_answer(
    answer: dict[str, Any],
    *,
    tail_used: int,
) -> DecisionRequestOutput:
    """Convert the model's JSON answer into a validated :class:`DecisionRequestOutput`.

    D-41 mapping:
    - empty question    → :class:`DecisionComposerEmptyError`
    - < 2 options       → :class:`DecisionComposerEmptyError`
    - shape errors from the value-object validators (bad option id,
      recommendation not in options, orphan recommendation_reason) →
      :class:`DecisionComposerError`
    """
    question = answer.get("question", "")
    if not isinstance(question, str) or not question.strip():
        raise DecisionComposerEmptyError("composer produced empty or non-string question")

    raw_options = answer.get("options", [])
    if not isinstance(raw_options, list):
        raise DecisionComposerError(
            f"composer's options is not a list: type={type(raw_options).__name__}"
        )
    if len(raw_options) < 2:
        raise DecisionComposerEmptyError(
            f"composer returned {len(raw_options)} options; need at least 2"
        )

    options: list[DecisionOption] = []
    for i, raw in enumerate(raw_options):
        if not isinstance(raw, dict):
            raise DecisionComposerError(f"options[{i}] is not an object: type={type(raw).__name__}")
        try:
            options.append(
                DecisionOption(
                    id=str(raw.get("id", "")),
                    label=str(raw.get("label", "")),
                    gain=str(raw.get("gain", "")),
                    loss=str(raw.get("loss", "")),
                )
            )
        except (ValueError, TypeError) as exc:
            # Bad option id / blank label → shape error, not "empty".
            raise DecisionComposerError(f"options[{i}]: {exc}") from exc

    recommendation_raw = answer.get("recommendation")
    recommendation: str | None
    if recommendation_raw is None or (
        isinstance(recommendation_raw, str) and not recommendation_raw.strip()
    ):
        recommendation = None
    else:
        recommendation = str(recommendation_raw).strip()

    reason_raw = answer.get("recommendation_reason")
    recommendation_reason: str | None
    if reason_raw is None or (isinstance(reason_raw, str) and not reason_raw.strip()):
        recommendation_reason = None
    else:
        recommendation_reason = str(reason_raw).strip()

    unknowns_raw = answer.get("unknowns", [])
    if not isinstance(unknowns_raw, list):
        raise DecisionComposerError(
            f"composer's unknowns is not a list: type={type(unknowns_raw).__name__}"
        )
    unknowns: tuple[str, ...] = tuple(str(u) for u in unknowns_raw if str(u).strip())

    try:
        return DecisionRequestOutput(
            question=question.strip(),
            options=tuple(options),
            recommendation=recommendation,
            recommendation_reason=recommendation_reason,
            unknowns=unknowns,
            tail_used=tail_used,
        )
    except (ValueError, TypeError) as exc:
        # DecisionRequestOutput.__post_init__ catches duplicates,
        # orphan recommendation_reason, recommendation-not-in-options,
        # etc. Those are shape errors from the model — DecisionComposerError.
        raise DecisionComposerError(f"model output failed shape validation: {exc}") from exc


# --------------------------------------------------------------------------- #
# Prompt text — the D-39 system prompt
# --------------------------------------------------------------------------- #


# The system prompt below is written as a plain string (no template
# variables) so PROMPT_VERSION genuinely bumps when the text changes. If
# a future revision needs to interpolate anything, first bump
# PROMPT_VERSION, then interpolate — the version-bump-first order is what
# makes the extras data usable retrospectively.
_SYSTEM_PROMPT = """\
You are a decision-request composer. You do NOT decide.

Your role is to phrase a question a human operator will answer. The human
is the only decision authority. Do not choose, do not merge, do not act.

You are given:
- the project / thread the operator was working in,
- why they got stopped (reason=human, rounds, and the last-message id),
- a bounded tail of prior messages from the thread and its title.

You must produce exactly one JSON object matching this schema:

{
  "question": "one-line question that names the thread's specific subject",
  "options": [
    {"id": "A", "label": "short label",
     "gain": "what this option gets you", "loss": "what it costs you"},
    {"id": "B", "label": "...", "gain": "...", "loss": "..."}
  ],
  "recommendation": "A" | "B" | null,
  "recommendation_reason": "why, citing a concrete fact from the tail" | null,
  "unknowns": ["thing you did not verify from the tail", "..."]
}

Hard rules:
1. You do not decide. You phrase a question and, at most, recommend.
2. At least 2 options. Each option has id (single uppercase letter A, B, C, ...),
   label (one line), gain (what is gained by choosing it), loss (what is lost).
3. If you set recommendation, its reason MUST cite a concrete fact from the
   tail — a msg-id, a number, a quoted phrase, a specific behaviour. Do not
   write general platitudes ("A is safer overall"). If no fact in the tail
   supports a recommendation, set recommendation and recommendation_reason
   to null.
4. Declare unknowns you did NOT verify. Empty list is only correct when
   the tail is complete for this decision; otherwise list what you did not see.
5. Output JSON ONLY. Do not wrap in fences, do not add commentary, do not
   apologise. The first character of your output is {, the last is }.
"""


__all__ = [
    "DEFAULT_CLAUDE_CLI",
    "DEFAULT_COMPOSER_IDENTITY",
    "DEFAULT_TIMEOUT_SECONDS",
    "PROMPT_VERSION",
    "ClaudeCodeComposer",
    "SubprocessResult",
    "SubprocessRunner",
]
