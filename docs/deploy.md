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
| `deploy/run-conductor-scheduled.ps1` | **what Task Scheduler runs.** Deploys, picks the thread, skips threads that have not moved, logs, notifies, **quarantines failures** |
| `deploy/Clear-Quarantine.ps1` | the one legitimate way to release a quarantined thread — records the reason to `state/quarantine-history.json` |
| `deploy/sync-repo.ps1` | fast-forwards this checkout to `origin/main` — merging is not deploying |
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
| `MINDWIRE_NAYSAYER_BASE_URL` | no | design-time naysayer → Lexora `naysayer` tier (Gemini). **Unset, the naysayer adapter refuses to spawn.** Set-but-wrong, it also refuses: since P-2, spawn runs a preflight against *this* URL — one non-streaming probe, then the gateway's own `/stats/costs/recent` row read back — and aborts unless that row attributes the request to the expected backend (`gemini`). Until P-2 only the first half was true, and a non-empty string was taken as proof the string pointed at Gemini. What the preflight attests is the tier→backend resolution **observed at spawn, from this host** — not the routing of each streaming turn that follows (the gateway writes no accounting row for streaming; closing that needs per-request streaming records on the Lexora side, P-5, which is not done) |
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

## Target-repo branch flow (V-4, 2026-08-02)

The dogfooding target (Spirrow-VoxelWorld) switched to a release-train flow with dev-speed plan
batch 1, V-4: **feature → develop → release → main**.

- `develop` is the integration branch, synced to `main@15883c1` on 2026-08-02 (voxelworld PR #175).
  Merges **into `develop` may be automated** — the github-mcp local policy already allows non-`main`
  merges.
- PRs with `base=main` are release-only (head `develop` or `release/*`). A CI guard
  (`.github/workflows/main-base-guard.yml`, landed on voxelworld `develop` via #176/#177) turns any
  other `base=main` PR red. The teeth engage once the workflow reaches `main` with the next
  develop→main release PR — `pull_request` triggers read the workflow from the merge ref.
- **Operational consequence for this loop: the implementer's PRs must target `develop`, not
  `main`.** Through #150–#167 (…2026-07-29) they targeted `main` directly; a PR opened that way
  after the guard is live goes red. Where the base branch is decided (implementer adapter / prompt)
  is a code-side follow-up, not covered by this doc.
- D-5 is unchanged: merges to `main` are never automated — the human's manual merge remains the
  authoritative guard, now CI-backed.
- **`develop` is ephemeral: after each develop→main release it is deleted and re-cut from `main`**
  (Takahito, 2026-08-02 — the same rule his global CLAUDE.md already sets for MindWire, confirmed
  here as the target repo's rule too). The current branch matches it: `merge-base(main, develop)`
  is exactly `15883c1`, i.e. develop carries no history of its own from before the V-4 sync.
  Two consequences worth stating, because both look like bugs otherwise:
  - **A back-merge `main` → `develop` is never needed.** Re-cutting from `main` achieves it. Do not
    cherry-pick release commits backwards.
  - **`main` may legitimately sit ahead of `develop` between releases**, and that self-heals at the
    re-cut. It happened on 2026-08-02: bot PR #174 (`0be6a7e`, the nightly baseline refresh) was
    merged straight into `main` — wrong under V-4, and only possible because the guard is not live
    on `main` yet. The next release merge keeps `main`'s newer baseline (develop never touched those
    files after the merge-base, so there is no conflict), and the re-cut then clears the divergence.
    The interim cost is real but bounded: `develop` carries the stale May baseline — the `null`
    `chunk_memory_kb` / `seam_octree_leaf_usage` that #174 filled in — until the next release.

> **Where this rule has to live.** The implementer runs with `setting_sources=[]` (SDK isolation, a
> deliberate credential-surface fix — see the adapter), so it does **not** read any `CLAUDE.md`. A
> branch rule recorded only there binds humans and not the loop. Spirrow-VoxelWorld currently has no
> branch-policy document of its own, so this section is the de-facto record; the SOT belongs in the
> target repo, and moving it there is an open follow-up.

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

### The daemon runs from its OWN checkout, never a working one

**The scheduled task must point at a clone nobody edits** — on sg-tomtebo-01,
`C:\Users\tomtar\spirrow-mindwire-daemon`, deliberately beside the data dir rather than in the dev
workspace, so the location itself says "not a place to work".

The reason is not tidiness. The venv holds an **editable** install, so the working tree *is* the
running module — there is no build or copy step to lag behind an edit. A daemon pointed at the
checkout a human works in therefore executes whatever is on disk at each tick, mid-edit and all.
Measured 2026-08-07, while two PRs were being written against that shared checkout: four ticks ran
work-in-progress code, one of them a version deliberately broken for a negative test, and
`state/heads.json` was rewritten several times in two mutually unreadable schemas. Nothing broke —
but only because every path involved fails open. That is luck plus fail-open design, not isolation.

Setting it up:

```pwsh
git clone https://github.com/SpirrowGames/spirrow-mindwire.git C:\Users\tomtar\spirrow-mindwire-daemon
cd C:\Users\tomtar\spirrow-mindwire-daemon
uv sync                                    # its own venv, editable onto its own source
```

The data root is unaffected — both checkouts default to `~/spirrow-mindwire-data`, so logs, sweep
config and state stay in one place and survive the move.

**Nothing needs to keep the daemon checkout up to date by hand**: `deploy/sync-repo.ps1` fast-forwards
it to `origin/main` at the top of every tick (see *Deploying a merged change*). And if someone does
edit it, that is not silent either — a modified tracked file makes the sync return `blocked`, which
pushes a Discord alert. Detection comes free from the sync step; no extra guard is needed.

To see which commit is actually deployed: `git -C C:\Users\tomtar\spirrow-mindwire-daemon log -1`, or
read the `repo up to date (<sha>)` line the sweep writes on any tick that does something.

Task settings that matter (Windows):

| Setting | Value | Why |
|---|---|---|
| Action | `pwsh -NoProfile -File <daemon-checkout>\deploy\run-conductor-scheduled.ps1` | the wrapper, not the raw launcher — and the **daemon** checkout, not a working one (above) |
| Trigger | at logon **and** a repeating trigger, 5 min, indefinite | the head probe makes a short interval nearly free; see below |
| `MultipleInstancesPolicy` | `IgnoreNew` | a tick that fires while the previous sweep is still working is dropped — this is the whole of the "already running?" handling, no lock file needed |
| `ExecutionTimeLimit` | `PT4H` | a real design round can take a while; `PT2H` was cutting runs off |
| `RestartCount` / `RestartInterval` | `3` / `PT10M` | transient MCP or inference failures retry instead of waiting for the next tick |
| Run as | the user holding `MINDWIRE_NAYSAYER_GITHUB_TOKEN`, the webhook var, and the Claude subscription | the wrapper reads the webhook from the **User** env scope |

### Deploying a merged change

**Merging is not deploying.** The task runs `uv run mindwire-loop` from this checkout — the venv holds
an *editable* install, so the working tree IS the running module — and nothing pulled it. A merged fix
could therefore sit undeployed indefinitely, with GitHub showing it merged and the task history
showing exit 0. The only way to notice was to compare `git log` against `origin/main` by hand.

Every tick now starts by running **`deploy/sync-repo.ps1`**, which fast-forwards the checkout to
`origin/main` and prints one JSON verdict. This needs no separate approval: `main` only advances
through a human's Tier-C merge, so the decision is already made by the time the script can see it.
What it adds is delivery, not authority.

| `status` | What it means | What the sweep does |
|---|---|---|
| `updated` | fast-forwarded to a new `main` | logs, notifies, and **stops this tick** |
| `current` | already at `origin/main` | proceeds (one buffered log line) |
| `skipped` | HEAD is not `main` — someone is working on this checkout | proceeds on that code, notifies once |
| `blocked` | tracked files modified, or the branch diverged | proceeds on current code, notifies once |
| `failed` | fetch / merge errored (network, auth, proxy) | proceeds on current code, notifies once |

Three rules are load-bearing:

- **A tick either updates the code or uses it, never both.** The wrapper was parsed from the old file
  at startup, while `run-conductor.ps1` would be read from disk *after* the pull — a sweep spanning
  two versions is not a thing worth debugging later. So a deploy tick launches nothing; the cost is
  one 5-minute cycle of latency after a merge.
- **Nothing but a pure fast-forward is ever performed.** A dirty tree or a diverged branch means
  someone has work here; resolving that automatically would be the script inventing an answer nobody
  asked for. Untracked files never block — the live host deliberately carries untracked working notes,
  and a fast-forward cannot conflict with them.
- **A sync failure is not a sweep failure.** One unreachable GitHub must not stop the loop; it keeps
  running the code it has, which is known-good and merely possibly old.

> The non-happy statuses report to **Discord**, not just the log. That is the point: a loop quietly
> running week-old code because `git fetch` has been failing is precisely the kind of correct-but-
> unread announcement that `spec/process/README.md` (the fail-open-placement rule) exists to prevent. Alerts are deduped on the reason, so a
> week spent on a feature branch costs one message, not 2016.

### Which thread gets driven

The sweep list lives in **`<data_dir>/config/sweep.json`** (template: `deploy/sweep.json.example`),
not in the script. Each entry is a `(project, thread_id, repo_dir)` triple, and order is priority:

```json
{"candidates": [
  {"project": "spirrow-voxelworld", "thread_id": "T-…", "repo_dir": "C:/workspace/sandbox/voxelworld-impl"},
  {"project": "spirrow-mindwire",   "thread_id": "T-…", "repo_dir": "C:/workspace/sandbox/mindwire-impl"}
]}
```

Two things follow from it being config:

- **The loop is not single-project.** The wrapper rewrites `[loop].project`, `[loop].repo_dir` and
  `[conductor].task_thread_id` per candidate, so MindWire's own threads can be driven alongside the
  target repo's. All three move together — a stale `[loop]` would drive the right thread against the
  wrong repo. The head probe runs once per distinct *project*, not per candidate.
- **`repo_dir` must be the implementer's own clone for that project — never the checkout the daemon
  itself runs from.** Otherwise the implementer edits its own running code.
- A missing `sweep.json` is a **hard error**. There is deliberately no built-in fallback list: a
  silent fallback would hide that the config was never read.

> Originally this list was a literal in the script. That meant adding one thread cost a PR, and — worse
> — a thread that was simply *not listed* was never picked up **at all, silently**. That is how
> `T-pr-gate-adr-index-scope` sat stranded after being opened. State keys are `project/thread_id` for
> the same reason: thread ids are only unique within a project.

The wrapper walks the list head-first and **advances past any candidate the conductor reports no work
for**, so one settled thread cannot park the whole loop. Only a clean `rounds=0` advances: a non-zero
exit or an unparseable run **stops** the sweep, so a genuine breakage is never laundered into
"everything is idle".

`T-pr-review-*` threads are deliberately absent from that list. They resolve to
`NEXT: pr-review <ref>`, which fires the Tier B PR-gate against the paid Lexora/Gemini backend —
driving those from an unattended schedule would spend money on a timer, so it stays a human action.
(The entire `T-pr-review-150`〜`167` family was closed by the 2026-08-02 K-5 triage — all PRs merged
with their verdicts recorded — but the exclusion rule here stays for future `T-pr-review-*`
threads.)

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

**The head alone is not enough to say "same stop".** Two inputs decide where a round lands, and the
cache is keyed on both: the thread's head message *and* the project's loop control state. At an
unchanged head, a naysayer→implementer handoff stops at the human gate under `hold` / `supervised`
but dispatches the implementer under `run` (carve-out ③). So `state/heads.json` stores a pair per
thread:

```json
{"spirrow-voxelworld/T-lod0-sliver-shards": {"head": "msg-2172", "control": "run"}}
```

and a thread is skipped only when **both** match. Anything unknown — an unreadable control probe, a
thread the head probe did not report, a record written before control was tracked — fails open into
a launch.

> Without the control half, releasing a project from `hold` never took effect: every thread kept its
> old head, so every thread was skipped, forever, while the log reported
> `no thread moved (9/9 heads unchanged) — nothing to do` at exit 0. Measured 2026-08-06 — the same
> line that means "healthy and idle" also meant "nothing can ever run", and nothing distinguished
> them. Entries written before the upgrade are bare strings; they read as unknown control and cost
> one launch each, once. `tests/Test-SweepHeadCache.ps1` (run by `.mindwire-gate`) guards the rule.

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

> **2026-08-02 update (K-5 triage)**: the 33-active state above is historical. K-5 closed 22 threads
> (the whole `T-pr-review-150`〜`167` review-record family plus the settled May–June threads),
> leaving **11 active** — matching what the probe was returning at the time. Probe and
> `chatroom_list_threads` should now agree, but the exclusion rule itself is *still* not
> characterised, so the fail-open stance stays.

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
| `<data_dir>/state/quarantine.json` | quarantined threads — one entry per `project/thread_id`; see *Quarantine and daily digest* |
| `<data_dir>/state/quarantine-history.json` | append-only clear log; every `Clear-Quarantine` writes its `-Reason` here |
| `<data_dir>/state/evaluated.json` | `first_seen_at` + `last_evaluated_at` per **live** thread; the starvation metric pivots on the current sweep list and prunes ex-live keys |
| `<data_dir>/state/digest.json` | `last_sent_at` of the daily digest — one send per 24h max |

Deleting `heads.json` costs one full bootstrap sweep; `notified.json` at most one duplicate alert.
Deleting `quarantine.json` **un-quarantines every thread silently** — do not do it as a shortcut for
`Clear-Quarantine`; the history file exists precisely so cleared-with-reason and cleared-without-
context are not confusable later. Deleting `evaluated.json` resets the starvation clock (harmless,
one tick of empty starvation report). Deleting `digest.json` forces the next tick to send a digest.

## Quarantine and daily digest

**Why it exists.** The prior wrapper broke the sweep on any non-zero exit. That fail-safe stopped a
real failure from being laundered into "everything idle" — but the mechanism was itself silent: the
loop just stopped, every candidate behind the broken one starved, and nothing announced the state
anywhere. Measured 2026-08-11, five unattended hours on threads that had work. The direct cause of
that specific starvation is closed (PR #136 / `OBL-MERGE-MECHANISM`); this section describes the
mechanism that keeps the NEXT unknown breakage from dying the same silent way.

**Signal / scheduling split.** A non-zero exit now
1. is **declared** — the candidate is written into `state/quarantine.json` and a Discord alert
   fires,
2. does **not** stop the sweep — the next candidate is tried, so downstream work still progresses.

The old sweep-break fail-safe is retained but re-aimed: only a **failure to write the declaration**
(and a "conductor stopped: … rounds=…" line that never arrived) breaks the sweep. If we cannot
even record what went wrong, we still cannot quietly move on.

**K-budget (2 per sweep).** Two quarantines in one tick suggest a shared cause; a third would spend
another inference on that same cause before stopping. At K=2 the sweep breaks and a "systemic
cause suspected" notification fires. Remaining candidates count as `not-reached` on the starvation
metric — the honesty rule below.

**Escalation ladder.** A quarantine record's derived state is a function of its age, not any
scheduling flag (it stays skipped either way):

| Age | State | Digest wording |
|---|---|---|
| 0–24h | `quarantined` | plain listing |
| 24h–7d | `escalated` | broken out at the top of the digest; state-transition alert fires once |
| ≥7d | `stale` | "fix it or fold the thread"; state-transition alert fires once |

Only a human clear (`Clear-Quarantine`) ever transitions a thread out. There is deliberately no
auto-clear on a `(head, control)` change — see *Fingerprint hint* below.

**Fingerprint hint.** Each quarantine record stores the `(head, control)` pair observed at the
failure. On every daily digest the current probe is compared against it; the difference is shown
as a hint next to the entry:

- current head ≠ stored head → `⚠ 新規メッセージあり (head 変化)`
- current control ≠ stored control → `⚠ control 変更あり`
- neither changed → **no hint** — meaning "no new head, no control change," **not** "not fixed."

The runner does not read fingerprints for scheduling. The two components are the *same* pair the
head-skip cache already computes each tick — no new observation surface, no external state, no
`git rev`, no `config hash`. The general rule this follows: **fingerprint components are limited
to values the runner already has in hand for its own purposes.** Widening the surface always
brings one of coupling by overreach, silent coverage-loss on the metric, or false positives, and
the digest is *the* load-bearing channel — polluting it degrades every entry on it.

**Starvation metric.** `<data_dir>/state/evaluated.json` records two fields per thread:
`first_seen_at` (written the first tick a candidate appears on the sweep list, before any per-candidate
decision) and `last_evaluated_at` (refreshed on every disposition where the sweep actually reached
the candidate — a launched verdict of `worked` / `no-work` / non-zero exit, **and** a `head-skipped`
where the sweep probed the head and proved nothing had moved). `quarantined-skipped` /
`held` / `not-reached` do **not** refresh — the sweep never asked the question for those, and
that is the whole point of the metric. A candidate whose effective age
(`last_evaluated_at`, falling back to `first_seen_at`) is ≥24h shows up as `starved` in the digest
and the log; a never-launched entry carries the `(未評価)` label so an operator's eye lands on it
distinctly from "stuck after real work."

> A head-skip counts even though nothing was launched, because the metric asks "how long since I
> actually reached this candidate?" and a head-skip IS reaching: the sweep probes the chatroom,
> sees the head has not moved (or the control state has not changed), and correctly fast-paths.
> If a head-skip did *not* refresh, every legitimately-idle thread over a weekend would flag as
> starved on Monday, the digest would fill with perfectly healthy inactive threads, and the metric
> would be trained into noise. The distinction that matters is "did the sweep evaluate this?",
> not "did the sweep launch an inference for this?".

The report is **pivoted on the current live sweep list**, not on the state file's accumulated keys.
Two failure modes that pivot closes:

- *False negatives* — a candidate that never enters `evaluated.json` (permanently `not-reached`
  behind a K-budget hit, or held / quarantined on its very first tick) would be silently absent
  from the metric if the report iterated over `evaluated.json`'s keys. That reproduces the exact
  "suppressed dark area" Q4 forbids.
- *False positives* — `evaluated.json` accumulates keys forever; without pruning, a folded thread
  stays in the file, inevitably ages past the threshold, and spams the digest every day.

The wrapper prunes any key not on the current sweep list and records `first_seen_at` for any new
live key before any candidate decision — so the 24h clock ticks from first sight for a candidate
that never launches, and a folded thread simply disappears from the metric.

Quarantined threads are **included** in the starvation count. That is the design's honesty rule —
a suppressed area on the "how long since I actually reached this candidate?" metric is exactly the
silent degradation this whole file exists to end. If it hurts to see the same thread in both
sections, that is the metric working.

**Daily digest.** Sent through the same Discord webhook, at most once per 24h from the last send.
**Sent even when both lists are empty.** The empty-day digest is not decoration: it is a positive
heartbeat that proves the channel is alive. "No alert" without a heartbeat would mean either
"nothing wrong" or "the webhook died" — those two are indistinguishable, which was exactly the
5-hour failure mode. The digest closes that ambiguity; what it cannot close is *the human not
reading the digest*, which is not channel failure but operational abandonment. Machines cannot
detect that; do not try to.

**Where the tunables live.** Failure budget, escalation cutoff, stale cutoff and starvation
cutoff are collected in one block at the top of `deploy/run-conductor-scheduled.ps1` under the
comment "NONE OF THESE FOUR ARE DERIVED VALUES." They are policy calls (K=2, escalated=24h,
stale=7d, starved=24h), not derived from anything, and the comment says so beside each value along
with the observation that would trigger revisiting it. Do not scatter magic numbers away from that
block.

### Clearing a quarantine

```powershell
pwsh deploy/Clear-Quarantine.ps1 -Thread 'spirrow-mindwire/T-foo-bar' -Reason 'root cause fixed in #999'
```

The `-Reason` is required and is appended to `state/quarantine-history.json` — this text is the
first-hand data for whether the quarantine judgement was over-sensitive (`Clear-Quarantine`
frequency is the signal, `-Reason` is the payload). Clearing also drops the `heads.json` entry for
the thread, so the next tick launches instead of head-skipping to the same head that was
quarantined under.

**Running Clear-Quarantine while the sweep is running is safe.** A single tick reads its state
files once at start and holds them in memory for the whole run (measured minutes on a real
AI-driven candidate). Without care, that stale in-memory map would silently overwrite the
operator's disk write at end-of-tick and resurrect the entry — the sort of failure the whole
quarantine story exists to end. So the sweep uses **merge-on-write**: at flush time it re-reads
`quarantine.json` and `heads.json` from disk, and any key the operator removed during the tick
stays removed. New adds from the sweep still land, age-driven state transitions still land; only
operator-cleared entries survive the merge. This narrows the race from "the whole tick" to
"between the re-read and the write" (sub-millisecond). Closing that residual window would take a
file lock, and a multi-minute sweep holding a lock would block the operator for that entire time,
defeating the point of a human-operable clear.

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
- **Standing autonomy — loop control state `run`**: autonomy is a **per-project, latching** state
  read from conclair at the top of every round (`run` / `supervised` / `hold`), not a marker written
  into the thread. The per-thread `DELEGATE` line this used to describe **no longer exists** —
  `conductor/handoff.py` removed it when the control state replaced it. At `run`, the **independent
  naysayer's own** proceed-handoff to the implementer advances code with no per-step GO — but only
  the naysayer (never the proposer) can advance, each iteration is freshly reviewed
  (reset-on-implementation), the naysayer's escalation (`NEXT: human`) pulls the human back in, and
  the state can be flipped away between rounds. A state that cannot be read is `hold`. `max_rounds`
  bounds a run regardless.
  - **Since P-3, that proceed must also carry the harness's `attest:` stamp** — the preflight
    observation P-2 records at spawn. Un-stamped, the carve-out is not taken and the turn falls
    through to the human terminal. Same for the Obj2 forced consult: only an attested naysayer post
    discharges it, so an un-attested segment buys exactly one extra forced consult.

Author trust is the environment trust model (D-3) — the chatroom accepts any author string **and any
body**, so the carve-outs, including the stamp check, are best-effort loop-level gating and
**noise-reduction rather than an authentication boundary**; the authoritative guard is the human's
manual merge. A stamp is forgeable by anyone who can post; what it removes is the case where an
un-attested turn is indistinguishable from an attested one.

The design-time naysayer is configured to reach Gemini via the SDK + Lexora `naysayer` tier, and
since P-2 each spawn *observes* that resolution rather than assuming it (see
`MINDWIRE_NAYSAYER_BASE_URL` above). What is established is per-spawn and non-streaming: no
per-turn proof exists for the streaming turns themselves, because the gateway records no accounting
row for streaming (P-5, upstream, not done). Earlier wording here cited the
`T-design-naysayer-gemini-reach` capability test as verification; that test showed reachability, not
that any given turn was served by Gemini.
