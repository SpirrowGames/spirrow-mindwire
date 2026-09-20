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
# (Not dual-management with an SDK auto-default: ``ImplementerSdkAdapter.spawn`` refuses to spawn
# without a set env var, so this line is what satisfies that requirement, not a redundant mirror.)
if (-not $env:MINDWIRE_IMPLEMENTER_BASE_URL) { $env:MINDWIRE_IMPLEMENTER_BASE_URL = "https://api.anthropic.com" }
#
# INTERNAL INFRA endpoints — the operator resolves values from [[platform:infra-registry]] and
# sets them as persistent user env vars (sourced from Vaultwarden, mirroring the token below).
# Validation lives in Python — the wrapper does NOT pre-flight (single source of truth,
# PR #296 pr-gate advisory msg-3484 / msg-3516 / PR #300 pr-gate advisory msg-3591 /
# T-public-repo-carries-real-infra-values):
#
#   Variable                       | Python owner
#   -------------------------------|-------------
#   MINDWIRE_MAGICKIT_MCP_URL      | spirrow_mindwire.magickit.client (magickit_mcp_url)
#   MINDWIRE_NAYSAYER_BASE_URL     | spirrow_mindwire.adapters.naysayer_sdk (NaysayerSdkAdapter)
#   MINDWIRE_LEXORA_URL            | spirrow_mindwire.lexora.client (lexora_url)
#
# The Python owner listed above is the ONE place that raises on a missing / empty value, records
# whether the variable is required or optional, when it is checked (startup vs. first use vs. lazy
# default), the ADR rationale, and the process-exit story (per --mode where it diverges). See
# that module for the fail-loud contract and why there is no in-code fallback. Do not paraphrase
# those facts here — that is the dual-management drift the advisory called out.

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
