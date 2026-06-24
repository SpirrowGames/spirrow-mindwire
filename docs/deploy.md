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

## Cost levers (optional, default-off)

Two independent knobs trim redundant naysayer (Gemini) calls — both default-off, so leaving them
unset preserves the prior behavior. See `deploy/mindwire.toml.example` for the inline reference.

- **`[conductor].force_naysayer_only_on_explicit_human`** (env
  `MINDWIRE_CONDUCTOR__FORCE_NAYSAYER_ONLY_ON_EXPLICIT_HUMAN`): forces the Obj2 design-loop consult
  only on an explicit `NEXT: human` (the real Tier-C handoff), not on a guard-(i) redirect or an
  un-routed turn. The independent review at genuine handoffs is unchanged; only incidental forced
  consults are dropped.
- **`[naysayer_gating]`** (env `MINDWIRE_NAYSAYER_GATING__*`): PR-gate re-review debounce —
  `skip_if_head_unchanged` reuses the prior verdict when the head SHA has not moved since the
  naysayer's last review; `max_review_rounds` (0 = off) caps re-reviews per PR and escalates to the
  human (a `COMMENT`) instead of re-billing a full Gemini review.

Enable in a dev run first and compare `forced_naysayer_turns` (logged at conductor finish) and the
per-PR naysayer review counts against the baseline before flipping them on for the standing daemon.

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

## Autonomy modes (design→implement)

Two ways to clear the conductor's Tier-C design→implement gate. Either way, merges to `main` are
never automated (D-5: the human's manual merge is the authoritative guard).

- **Per-step GO (supervised)**: post a Tier-C GO authored as `[conductor].human_identity` (default
  `human`; PR-2b-3 D-1) with `NEXT: <implementer>`. A GO under a relay name (e.g. `Bohr`) does
  **not** fire the carve-out — the flag-1 gap.
- **Standing autonomy — `DELEGATE` (PR-2b-3 D-4)**: include a standalone `DELEGATE` line in a
  human-authored message to grant standing design→implement autonomy on the thread (alongside that
  message's normal `NEXT:`). Then the **independent naysayer's own** proceed-handoff to the
  implementer advances code with no per-step GO — but only the naysayer (never the proposer) can
  advance, each iteration is freshly reviewed (reset-on-implementation), the naysayer's escalation
  (`NEXT: human`) pulls the human back in, and **any** later human message without `DELEGATE`
  revokes it. `max_rounds` bounds a run regardless.

Author trust is the environment trust model (D-3) — the chatroom accepts any author string, so the
carve-outs are best-effort loop-level gating; the authoritative guard is the human's manual merge.

The design-time naysayer reaches Gemini via the SDK + Lexora `naysayer` tier (verified by the
capability test, `T-design-naysayer-gemini-reach`); the only thing that was missing was
`MINDWIRE_NAYSAYER_BASE_URL` — now set by the launcher.
