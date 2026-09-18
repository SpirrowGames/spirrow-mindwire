#!/usr/bin/env pwsh
# deploy/run-conductor.ps1 — launch the Stage 3 unattended NEXT-driven conductor daemon (ADR-18).
#
# Runs `mindwire-loop --mode conductor` with the full environment the role adapters resolve at
# spawn. Two rules:
#   1. Secrets are NEVER baked in — the GitHub token must already be in the environment.
#   2. Non-secret internal infra addresses (magickit MCP URL, naysayer inference base URL) are
#      required by the Python runtime and validated there — this wrapper does NOT duplicate that
#      validation. The single source of truth is the Python client / adapter constructor that
#      consumes the value (see ADR-2026-06-04-18 v1.1 §2 D-2, ADR-2026-05-21-05 §5); duplicating
#      the check in PowerShell created a dual-management drift risk (PR #296 pr-gate advisory,
#      T-public-repo-carries-real-infra-values), so it lives in exactly one place — Python.
#
# Register this with Task Scheduler (Windows) / a systemd unit (Linux) for unattended runs. See
# docs/deploy.md for the full runbook (host choice, secrets, config, service registration).
#
# Config: `mindwire-loop` reads <data_dir>/config/mindwire.toml. Set MINDWIRE_PATHS__DATA_DIR to the
# data root that holds config/mindwire.toml (template: deploy/mindwire.toml.example).

$ErrorActionPreference = "Stop"

# --- UTF-8 (T39 deploy half) -------------------------------------------------------------------
# The parent interpreter's UTF-8 *mode* can only be set before startup, so the service sets it here
# (the in-code _ensure_utf8_runtime() is the floor; this guarantees the mode for the whole process).
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# --- inference / gateway endpoints ---------------------------------------------------------------
# implementer inference: the local Claude subscription reaches Anthropic directly (NOT via Lexora).
# The default target is Anthropic's public API URL; only override if you have your own reason
# (proxy, test double, etc.) — this is not an infra-value leak, so a public default is fine.
if (-not $env:MINDWIRE_IMPLEMENTER_BASE_URL) { $env:MINDWIRE_IMPLEMENTER_BASE_URL = "https://api.anthropic.com" }
# INTERNAL INFRA endpoints — resolution + validation both live in Python. The validation is
# lazy (each variable is checked at the moment its consumer is constructed / a session is
# spawned), NOT at wrapper start. The wrapper does NOT pre-flight these because a wrapper-side
# check would either duplicate the Python message (drift risk — PR #296 pr-gate advisory,
# msg-3484) or diverge from it (two different rationales for the same rule):
#
#   * MINDWIRE_MAGICKIT_MCP_URL  → validated in ``spirrow_mindwire.magickit.client.magickit_mcp_url``.
#                                  Unset raises ``MagickitMcpError`` from
#                                  ``StreamableHttpChatroomMcp.__init__`` — the MCP client is
#                                  constructed inside the composition root
#                                  (``spirrow_mindwire.loop_runner._build_dispatcher``), so this
#                                  fails as part of daemon startup for BOTH ``run_loop`` and
#                                  ``run_conductor``, before either enters its event loop.
#                                  No in-code fallback per ADR-2026-06-04-18 v1.1 §2 D-2.
#   * MINDWIRE_NAYSAYER_BASE_URL → read at ``NaysayerSdkAdapter.__init__`` (composition-root time)
#                                  but only VALIDATED at ``NaysayerSdkAdapter.spawn``, when a
#                                  naysayer session is actually summoned. An empty value raises
#                                  ``NaysayerSdkSpawnError`` at that point. If a daemon is
#                                  configured without any naysayer-triggering watches and sits
#                                  idle, this variable is not checked until the first summon.
#                                  Independence rationale per ADR-2026-05-21-05 §5.
#   * MINDWIRE_LEXORA_URL        → optional in ``spirrow_mindwire.lexora.client.lexora_url``.
#                                  Unset falls back to the safe-by-design loopback (Lexora binds
#                                  0.0.0.0 + no auth, so loopback is the only default that does
#                                  not widen the unauthenticated surface). Operators on a host
#                                  where Lexora is NOT co-resident set the env from
#                                  [[platform:infra-registry]].
#
# The operator resolves the required values from [[platform:infra-registry]] and sets them
# (persistent user env var sourced from Vaultwarden, mirroring the MINDWIRE_NAYSAYER_GITHUB_TOKEN
# secret handling below). A misconfigured daemon that reaches the naysayer summon path will
# exit non-zero at that call; a misconfigured daemon whose magickit URL is missing will not
# even complete startup.

# --- secret precondition (fail loud, never hardcode) -------------------------------------------
if (-not $env:MINDWIRE_NAYSAYER_GITHUB_TOKEN) {
    throw "MINDWIRE_NAYSAYER_GITHUB_TOKEN is not set. Provide the spirrowgames-ops PAT via the " +
          "environment (e.g. a persistent user env var sourced from Vaultwarden) before launching " +
          "— it must never be committed or hardcoded. See docs/deploy.md (Secrets)."
}

# --- config root (must hold config/mindwire.toml with [loop] + [conductor]) --------------------
if (-not $env:MINDWIRE_PATHS__DATA_DIR) {
    Write-Warning ("MINDWIRE_PATHS__DATA_DIR is not set; mindwire-loop will read " +
        "~/spirrow-mindwire-data/config/mindwire.toml. Set it to your data root if config lives elsewhere.")
}

# --- run from the repo root so `uv run` resolves the project venv ------------------------------
$repoRoot = Split-Path -Parent $PSScriptRoot   # deploy/.. == repo root
Set-Location $repoRoot
Write-Host "[adr18] starting Stage 3 conductor daemon (project + thread from mindwire.toml [conductor])"
uv run mindwire-loop --mode conductor
