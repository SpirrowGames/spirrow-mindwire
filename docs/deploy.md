# Deploy — Stage 3 unattended conductor daemon (ADR-18)

How to run the NEXT-driven design-loop **conductor** (`mindwire-loop --mode conductor`) as an
unattended daemon. The conductor reads one design thread and serially dispatches the single
`NEXT:`-named role each turn (proposer → implementer → naysayer), driving it to a stop condition
(`NEXT: human` / `NEXT: none` / round cap).

> Scope: this is the *operational* wiring — how the loop is launched, kept fed with work, kept
> affordable, and how it tells a human it is stuck. The loop code (conductor, the three role
> adapters, the allow-list gate) is covered by `docs/architecture.md` / `docs/feature-3-design.md`.

The pieces, and which file owns each:

| File | Role |
|---|---|
| `deploy/run-conductor.ps1` | drives **one** thread to a stop condition and exits. Env + secrets + UTF-8 |
| `deploy/run-conductor-scheduled.ps1` | **what Task Scheduler runs.** Picks the thread, skips threads that have not moved, logs, notifies |
| `scripts/thread_heads.py` | the work detector — one chatroom call, every thread's head message id |
| `deploy/sync-clock-http.ps1` | clock correction, because NTP cannot leave this host |

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

### Host prerequisites (egress chokepoint + clock)

sg-tomtebo-01 runs an **allow-list egress model**: every firewall profile is
`DefaultOutboundAction=Block`, only `squid.exe` has an outbound rule, and `squid.conf`
(`C:\Squid\etc\squid\squid.conf`, outside this repo) decides which domains are reachable. Two
consequences bite the daemon:

- **Anything the daemon talks to must be in `squid.conf`.** Adding a host is
  `acl allowed dstdomain <domain>` + `squid.exe -k parse` + `squid.exe -k reconfigure`. Beyond the
  build/inference domains, the only addition these pieces need is **`.discord.com`** (handoff
  notifications).
  A denied domain fails *quietly* — the client just retries forever and nothing surfaces it. A
  non-daemon example from this host: `.claudeusercontent.com` was missing, so something retried every
  30 s for eight days and logged 23,940 `TCP_DENIED/403` before anyone looked. When a component on
  this host misbehaves inexplicably, grep `access.log` for `TCP_DENIED` **before** debugging the
  component.
- **NTP cannot work.** Nothing grants UDP/123, so `w32time` reaches no time source at all — not
  `time.windows.com`, not the tailnet peer — and reports `Free-running System Clock`. Measured
  2026-08-02: the host was **173 s slow**, which corrupts every log correlation and token lifetime on
  it. `deploy/sync-clock-http.ps1` works around this by taking the time from an HTTPS `Date` header
  through squid (~±1 s) — see *Clock* below. If UDP/123 is ever opened for W32Time, retire that
  script rather than running both.

Outbound requests from the daemon's own scripts pass `-Proxy` explicitly. That is deliberate: `pwsh`
has no outbound firewall permission of its own, so a request that skipped the proxy is **blocked**
rather than quietly bypassing the chokepoint.

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
| `MINDWIRE_NOTIFY_DISCORD_WEBHOOK` | **YES** | Discord webhook the sweep posts human-handoff alerts to (see *Human-handoff notifications*). Read from the **User** env scope by the wrapper, so set it with `[Environment]::SetEnvironmentVariable(...,'User')`, not a session variable. Unset = notifications silently skipped |
| `MINDWIRE_NOTIFY_PROXY` | no | proxy used for the webhook POST (default `http://127.0.0.1:3128`) |

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

### Shadow measurement (size the savings risk-free)

Both levers also have an observe-only mode that measures what they *would* save **without changing
behaviour or coverage** — the full review still runs:

- The conductor always logs `forced_naysayer_saveable` at finish — forced consults on a
  non-explicit-human terminal that `force_naysayer_only_on_explicit_human` would drop. Read it with
  that lever **off** to size Item 1's saving (no flag needed).
- `[naysayer_gating].shadow = true` (with `skip_if_head_unchanged` / `max_review_rounds` set to the
  policy you are evaluating) makes the PR-gate compute and log `would SKIP` / `would CAP` per review,
  and surface `would_skip_head_unchanged` / `would_cap` on the outcome, while still running the full
  review — so Item 2's saving is measured before you enforce it.

## Run

```powershell
# one-shot (foreground) — drives the thread to a stop condition, then exits
pwsh -File deploy/run-conductor.ps1
```

`run_conductor` drives **one** thread to a stop and exits. It reads whichever thread happens to be in
`[conductor].task_thread_id`, so on its own it is a one-shot: point it, fire it, done.

## Standing daemon — the scheduled sweep

For unattended operation, register **`deploy/run-conductor-scheduled.ps1`** (not `run-conductor.ps1`)
with Task Scheduler. The wrapper picks the thread, keeps a log, and decides when launching the
conductor is worth it at all.

> Do not register `run-conductor.ps1` directly. It was the original wiring and it fails silently: it
> drives whatever thread was left in the config, and if that thread has settled (`NEXT: none`) every
> fire is a 0-round no-op that still exits 0 and still shows green in the task history. Observed for
> an unknown number of days before the wrapper made it visible.

Task settings that matter (Windows):

| Setting | Value | Why |
|---|---|---|
| Action | `pwsh -NoProfile -File <repo>\deploy\run-conductor-scheduled.ps1` | the wrapper, not the raw launcher |
| Trigger | at logon **and** a repeating trigger, 5 min, indefinite | the head probe makes a short interval nearly free; see below |
| `MultipleInstancesPolicy` | `IgnoreNew` | a tick that fires while the previous sweep is still working is dropped — this is the whole of the "already running?" handling, no lock file needed |
| `ExecutionTimeLimit` | `PT4H` | a real design round can take a while; `PT2H` was cutting runs off |
| `RestartCount` / `RestartInterval` | `3` / `PT10M` | transient MCP or inference failures retry instead of waiting for the next tick |
| Run as | the user holding `MINDWIRE_NAYSAYER_GITHUB_TOKEN`, the webhook var, and the Claude subscription | the wrapper reads the webhook from the **User** env scope |

### Which thread gets driven

The wrapper walks `$ThreadPriority` (edited at the top of the script) head-first and **advances past
any candidate the conductor reports no work for**, so one settled thread cannot park the whole loop.
Only a clean `rounds=0` advances: a non-zero exit or an unparseable run **stops** the sweep, so a
genuine breakage is never laundered into "everything is idle".

`T-pr-review-*` threads are deliberately absent from that list. They resolve to
`NEXT: pr-review <ref>`, which fires the Tier B PR-gate against the paid Lexora/Gemini backend —
driving those from an unattended schedule would spend money on a timer, so it stays a human action.

### Why the sweep is cheap enough to run every 5 minutes

Launching the conductor is not uniformly cheap:

- a **settled or human-parked** thread costs one MCP read and **no inference** — the conductor reads
  the head message, resolves `NEXT: none` / `NEXT: human`, and stops;
- a thread whose `NEXT:` names a **role** costs a real role dispatch. When that role then posts
  nothing (`no_progress_to_human`), the tick has bought nothing and billed for it.

So the wrapper does not launch blindly. **`scripts/thread_heads.py`** answers "did anything change?"
from data: one `chatroom_my_unread` call returns every thread's `latest_msg_id` without fetching a
single message body (~1 s for all threads at once). If a thread's head equals the `last_msg` the
conductor reported last time, the conductor would resolve the same handoff and reach the same stop —
so it is not launched at all.

Measured on a 6-candidate list:

| | conductor launches | elapsed | log written |
|---|---|---|---|
| bootstrap (no state yet) | 3 | 18 s | full detail |
| second pass | 3 | 11 s | full detail |
| **steady state** | **0** | **3 s** | **182 bytes** |

An earlier design used a cooldown timer instead. It was dropped: a timer guesses at when re-running
might be worthwhile, the head id knows.

**Everything unknown fails open.** A probe failure, a thread missing from the probe's result, or a
thread with no recorded head all launch the conductor anyway. The probe's exclusion rule is not fully
characterised — it reported 11 threads where `chatroom_list_threads` showed 33 active, omitting the
`T-pr-review-*` family — so a gap must cost one cheap run rather than silently parking a live thread
forever.

> **The probe identity must never post and never mark read.** `chatroom_my_unread` is an inbox: it
> lists threads with unread messages, so an identity whose read cursor has advanced under-reports
> *silently*. Measured: `Heisenberg` returned 5 of 11 threads and omitted two live candidates, while
> the dedicated `conductor-probe` identity returned all 11. Nothing in this repo calls
> `chatroom_mark_read`; if anything ever does for that identity, the probe goes blind and the sweep
> quietly degrades to "launch everything".

### State and logs

| Path | Contents |
|---|---|
| `<data_dir>/logs/conductor-YYYY-MM-DD.log` | sweep log. Detail is buffered and only committed when a tick actually does something — an idle tick collapses to one line, which is what keeps a 5-minute cadence readable |
| `<data_dir>/logs/clock-YYYY-MM-DD.log` | clock-sync log |
| `<data_dir>/state/heads.json` | last head message id observed per thread — the skip decision above |
| `<data_dir>/state/notified.json` | last alert fired per thread, for de-duplication |

Deleting either state file is safe: `heads.json` costs one full bootstrap sweep, `notified.json`
costs at most one duplicate alert.

## Human-handoff notifications

When the loop parks on a human it has, by construction, nothing left to do until someone acts — so
the sweep pushes an alert to a Discord webhook.

**Why Discord and not Claude Code's `PushNotification`:** that tool only exists inside a live Claude
Code session with Remote Control connected. The conductor is an unattended scheduled task driving
headless SDK roles — there is no session there to notify from. This is a process-model gap, not a
configuration one, so no amount of tool-permission wiring closes it.

The webhook URL is a bearer secret (anyone holding it can post to the channel). It is read from the
environment, never committed, and **scrubbed out of the exception text before anything is logged**.
A notification failure is non-fatal — the conductor's work already happened by then.

Alerts fire for every human-terminal `StopReason`, not just `human`:

| `reason` | Alert |
|---|---|
| `human` | yes — the Tier-C decision point |
| `no_handoff_to_human` | yes — `NEXT:` unparseable, routed to the human |
| `no_progress_to_human` | yes — the dispatched role posted nothing |
| `round_cap` | yes — runaway backstop tripped |
| `empty_thread` | yes — almost always a typo in the priority list |
| `none` | **no** — a settled thread is the normal end; the sweep just moves on |

Do not narrow this to `reason -eq 'human'`. The first live sweep after this was wired stopped with
`no_progress_to_human`, which a narrower check drops silently.

Alerts are keyed on `(reason, last_msg)`, so a thread parked on a human for days alerts **once**
rather than on every tick. A thread that changes *how* it is stuck re-alerts.

## Clock

`deploy/sync-clock-http.ps1`, registered as its own scheduled task (`MindWire-ClockSync`), every
15 min, **as SYSTEM** — `Set-Date` needs `SeSystemtimePrivilege`.

It takes the time from an HTTPS `Date` response header through squid and attributes the reading to
the midpoint of the round trip, so the RTT does not land in the measurement as one-sided error. It
corrects only when drift exceeds a 5 s dead band, so the clock is not nudged by the sub-second noise
of a one-second-resolution header.

Measured 2026-08-02: **173.6 s → 0.4 s**, cross-checked against a second host (0.5 s).

This is **not** a substitute for NTP: `w32time` still reports unsynchronised, and header granularity
caps accuracy at roughly ±1 s. It exists only because UDP/123 is blocked (see *Host prerequisites*).

## Preflight

```powershell
# magickit + Lexora reachable from this host?
Test-NetConnection 100.79.84.62 -Port 8117   # magickit MCP
Test-NetConnection 100.79.84.62 -Port 8110   # Lexora
# secrets present? (the webhook lives in the User scope, NOT the session)
if (-not $env:MINDWIRE_NAYSAYER_GITHUB_TOKEN) { "MISSING github token" }
if (-not [Environment]::GetEnvironmentVariable('MINDWIRE_NOTIFY_DISCORD_WEBHOOK','User')) { "MISSING notify webhook" }

# head probe works? (prints {"heads": {...}, "count": n} — count 0 means the sweep will launch everything)
uv run python scripts/thread_heads.py --project <project>

# egress: is the domain actually allowed? a denied host shows TCP_DENIED/403 in squid's access.log
Get-Content C:\Squid\var\log\squid\access.log -Tail 200 | Select-String 'TCP_DENIED'

# clock sane? (measure only — the large threshold suppresses the correction)
pwsh -File deploy/sync-clock-http.ps1 -ThresholdSeconds 99999
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
