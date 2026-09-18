#!/usr/bin/env pwsh
# deploy/run-conductor.ps1 — launch the Stage 3 unattended NEXT-driven conductor daemon (ADR-18).
#
# Runs `mindwire-loop --mode conductor` with the full environment the role adapters resolve at
# spawn. Two rules:
#   1. Secrets are NEVER baked in — the GitHub token must already be in the environment.
#   2. Non-secret internal infra addresses are REQUIRED in the environment. The operator resolves
#      each value from [[platform:infra-registry]] and sets it before launching; there are no
#      hard-coded fallbacks — a missing env is a loud `throw`, not a silent misroute (ADR-18 v1.1
#      §2 D-2, ADR-22 D-1; T-public-repo-carries-real-infra-values Bohr msg-2734 §1).
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
# INTERNAL INFRA endpoints — env-required, fail-fast. The hard-coded fallbacks that used to live
# here landed local no-auth topology (tailnet host / IP / port) into the public source repository
# as the shipped default behaviour (T-public-repo-carries-real-infra-values Bohr msg-2734 §6:
# "振る舞いを持つ既定値 3 箇所"), and silent misroute after a config change was itself a failure
# mode. Operators resolve the actual values from [[platform:infra-registry]] and set the env vars
# before launching (persistent user env var sourced from Vaultwarden, mirroring the
# MINDWIRE_NAYSAYER_GITHUB_TOKEN secret handling below). See ADR-2026-06-04-18 v1.1 §2 D-2 for
# the magickit contract; the sibling NAYSAYER_BASE_URL / LEXORA_URL adopt the same fail-fast
# shape for symmetry (Bohr msg-3391 §1 "symmetry こそが安全性の担保").
if (-not $env:MINDWIRE_NAYSAYER_BASE_URL) {
    throw "MINDWIRE_NAYSAYER_BASE_URL is not set. Resolve the Lexora 'naysayer' tier URL " +
          "(Gemini via Lexora, independence per ADR-05 §5; same tier as the PR-gate) from " +
          "[[platform:infra-registry]] and set it in the environment before launching. There is " +
          "no in-code default (T-public-repo-carries-real-infra-values Bohr msg-2734 §6; the " +
          "fallback was removed to stop landing tailnet topology in the public source repo)."
}
if (-not $env:MINDWIRE_LEXORA_URL) {
    throw "MINDWIRE_LEXORA_URL is not set. Resolve the Lexora gateway URL (Tier B PR-gate " +
          "driver) from [[platform:infra-registry]] and set it in the environment before " +
          "launching. There is no in-code default (T-public-repo-carries-real-infra-values Bohr " +
          "msg-2734 §6; the fallback was removed to stop landing tailnet topology in the public " +
          "source repo)."
}
if (-not $env:MINDWIRE_MAGICKIT_MCP_URL) {
    throw "MINDWIRE_MAGICKIT_MCP_URL is not set. Resolve the magickit MCP URL (local no-auth " +
          "chatroom MCP endpoint) from [[platform:infra-registry]] and set it in the environment " +
          "before launching. There is no in-code default (ADR-2026-06-04-18 v1.1 §2 D-2 — the " +
          "in-code fallback was removed to prevent silent misroute after ADR-18 §1.1 established " +
          "that the mindwire loop host is not the magickit host)."
}

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
