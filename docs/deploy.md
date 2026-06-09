# Deploy — Stage 3 unattended conductor daemon (ADR-18)

How to run the NEXT-driven design-loop **conductor** (`mindwire-loop --mode conductor`) as an
unattended daemon. The conductor reads one design thread and serially dispatches the single
`NEXT:`-named role each turn (proposer → implementer → naysayer), driving it to a stop condition
(`NEXT: human` / `NEXT: none` / round cap).

> Scope: this is the *operational* wiring. The loop code (conductor, the three role adapters, the
> allow-list gate) is covered by `docs/architecture.md` / `docs/feature-3-design.md`.

## Host

The daemon needs to reach four things from wherever it runs:

| Dependency | Endpoint | Notes |
|---|---|---|
| magickit chatroom MCP | `MINDWIRE_MAGICKIT_MCP_URL` (default `http://100.79.84.62:8117/mcp`) | the thread substrate; reachable from sg-tomtebo-01 over Tailscale (verified — the voxelworld conductor smoke read/posted through it) |
| Lexora gateway | `http://100.79.84.62:8110` | design-time naysayer (`naysayer` tier → Gemini) **and** the Tier B PR-gate driver |
| Claude inference | `https://api.anthropic.com` | the implementer's local subscription on the daemon host |
| GitHub | api.github.com | PR open / diff read / review submit (via the scoped token) |

**sg-tomtebo-01 is a viable host**: it reaches magickit + Lexora over Tailscale and has a local
Claude subscription for the implementer. (Running co-resident on sg-ai-server-01 is the alternative;
then magickit/Lexora are loopback and the implementer needs its own inference there.)

## Environment

Set by `deploy/run-conductor.ps1`; secrets must be supplied externally.

| Var | Secret? | Purpose |
|---|---|---|
| `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8` | no | parent-process UTF-8 (cp932 safety on Windows; T39). Must be set **before** launch — the launcher does this |
| `MINDWIRE_IMPLEMENTER_BASE_URL` | no | implementer inference (`https://api.anthropic.com`) |
| `MINDWIRE_NAYSAYER_BASE_URL` | no | design-time naysayer → Lexora `naysayer` tier (Gemini). **Without it the naysayer adapter refuses to spawn** — this is the env that, when unset, silently disables the independent-review leg |
| `MINDWIRE_LEXORA_URL` | no | Tier B PR-gate driver → Lexora |
| `MINDWIRE_MAGICKIT_MCP_URL` | no | chatroom MCP (defaults to the in-code Tailscale endpoint) |
| `MINDWIRE_NAYSAYER_GITHUB_TOKEN` | **YES** | spirrowgames-ops PAT (PR diff read + review submit). Provide via a persistent user env var sourced from Vaultwarden; never commit it |
| `MINDWIRE_PATHS__DATA_DIR` | no | data root holding `config/mindwire.toml` |

## Config

Copy `deploy/mindwire.toml.example` to `<data_dir>/config/mindwire.toml` and fill in
`[conductor].task_thread_id` (the design thread to drive) and `[loop].repo_dir` (the implementer's
own **clone** — not a linked worktree). `[conductor].roster` maps chatroom personas to roles and
`naysayer_identity` must map to the `naysayer` role.

## Run

```powershell
# one-shot (foreground) — drives the thread to a stop condition, then exits
pwsh -File deploy/run-conductor.ps1
```

`run_conductor` drives one thread to a stop and exits (re-arming after the human responds is an
operator concern). For a standing daemon, register the launcher with **Task Scheduler** (Windows):
trigger *At log on* / *At startup*, action `pwsh -File <repo>\deploy\run-conductor.ps1`, run as the
user whose environment holds `MINDWIRE_NAYSAYER_GITHUB_TOKEN` and the local Claude subscription.

## Preflight

```powershell
# magickit + Lexora reachable from this host?
Test-NetConnection 100.79.84.62 -Port 8117   # magickit MCP
Test-NetConnection 100.79.84.62 -Port 8110   # Lexora
# secret present?
if (-not $env:MINDWIRE_NAYSAYER_GITHUB_TOKEN) { "MISSING github token" }
```

## Known limits (not yet wired — see the chatroom threads)

- **Fully-autonomous design→implement** still needs a human GO: the conductor's Tier-C guard
  redirects a proposer→implementer handoff to the human unless the GO is authored by the configured
  `human_identity` (default `human`). Until that is operationalized (flag-1 / PR-2b-3), drive
  design→implement by posting the GO as `author="human"` with `NEXT: <implementer>`.
- The design-time naysayer reaches Gemini via the SDK + Lexora `naysayer` tier (verified by the
  capability test, `T-design-naysayer-gemini-reach`); the only thing that was missing was
  `MINDWIRE_NAYSAYER_BASE_URL` — now set by the launcher.
