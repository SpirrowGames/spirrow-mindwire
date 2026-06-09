#!/usr/bin/env pwsh
# deploy/run-conductor.ps1 — launch the Stage 3 unattended NEXT-driven conductor daemon (ADR-18).
#
# Runs `mindwire-loop --mode conductor` with the full environment the role adapters resolve at
# spawn. Two rules:
#   1. Secrets are NEVER baked in — the GitHub token must already be in the environment.
#   2. Non-secret internal infra addresses default to the SpirrowGames Tailscale endpoints and are
#      overridable per host (they are environment-dependent, mirroring the in-code defaults).
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

# --- inference / gateway endpoints (non-secret internal infra; override per host) ---------------
# implementer inference: this host's local Claude subscription (NOT routed via Lexora).
if (-not $env:MINDWIRE_IMPLEMENTER_BASE_URL) { $env:MINDWIRE_IMPLEMENTER_BASE_URL = "https://api.anthropic.com" }
# design-time naysayer: Lexora 'naysayer' tier -> Gemini (the SDK reaches it at :8110). Independence
# (ADR-05 §5) holds because the tier is a different model family; same tier the PR-gate uses.
if (-not $env:MINDWIRE_NAYSAYER_BASE_URL)    { $env:MINDWIRE_NAYSAYER_BASE_URL = "http://100.79.84.62:8110" }
# Tier B PR-gate driver: Lexora gateway.
if (-not $env:MINDWIRE_LEXORA_URL)           { $env:MINDWIRE_LEXORA_URL = "http://100.79.84.62:8110" }
# magickit chatroom MCP defaults to http://100.79.84.62:8117/mcp in-code; override only if relocated:
# if (-not $env:MINDWIRE_MAGICKIT_MCP_URL)    { $env:MINDWIRE_MAGICKIT_MCP_URL = "http://100.79.84.62:8117/mcp" }

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
