# Deploy — Stage 3 unattended conductor daemon (ADR-18)

> **実インフラ値**（ホスト名 / IP / パス）は [[platform:infra-registry]] が正本。この文書は `{{PLACEHOLDER}}` で参照する（規約 §3.1）。

How to run the NEXT-driven design-loop **conductor** (`mindwire-loop --mode conductor`) as an
unattended daemon. The conductor reads one design thread and serially dispatches the single
`NEXT:`-named role each turn (proposer → implementer → naysayer), driving it to a stop condition
(`NEXT: human` / a settle — `STOP: done`, `BLOCKED-ON:` + `NEXT: <wake>`, or the legacy
`NEXT: none` — / round cap).

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
| magickit chatroom MCP | `MINDWIRE_MAGICKIT_MCP_URL` (default `http://{{IP_SERVICES}}:8117/mcp`) | the thread substrate; reachable from {{HOST_LOOP}} over Tailscale (verified — the voxelworld conductor smoke read/posted through it) |
| Lexora gateway | `http://{{IP_SERVICES}}:8110` | design-time naysayer (`naysayer` tier → Gemini) **and** the Tier B PR-gate driver |
| Claude inference | `https://api.anthropic.com` | the implementer's local subscription on the daemon host |
| GitHub | api.github.com | PR open / diff read / review submit (via the scoped token) |

**{{HOST_LOOP}} is a viable host**: it reaches magickit + Lexora over Tailscale and has a local
Claude subscription for the implementer. (Running co-resident on {{HOST_SERVICES}} is the alternative;
then magickit/Lexora are loopback and the implementer needs its own inference there.)

### Host prerequisites (egress chokepoint + clock)

{{HOST_LOOP}} runs an **allow-list egress model**: every firewall profile is
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

### CPU-heavy manual jobs on the loop host

Role turns (the implementer's local `bash .mindwire-gate` above all) share {{HOST_LOOP}}'s CPU with
whatever an operator runs there by hand. A CPU-bound manual job that takes every core starves the
gate, and the turn runs out of wall clock. Any manual job that uses a lot of CPU on this host must
follow these three rules:

1. **Cap the workers.** The job's worker processes and threads, summed over every process it
   starts, must not exceed the host's logical core count minus 4 (on a 16-core host, 12 at most).
2. **Start it at lowered priority.** On Windows, launch it with `cmd.exe`'s `start` command:
   `start "" /BELOWNORMAL <command>` from a `cmd.exe` prompt, or
   `cmd /c start "" /BELOWNORMAL <command>` from PowerShell. (In PowerShell a bare `start` is the
   `Start-Process` alias, which rejects `/BELOWNORMAL`. The empty `""` is the window title, so a
   quoted command is not mistaken for it.) Launching at `BelowNormal` from the start matters:
   worker processes the job spawns inherit that class, whereas lowering the priority after launch
   leaves workers already started at `Normal`. After launch, check in PowerShell that each process
   really is `BelowNormal`, e.g. `Get-Process -Id <pid> | Select-Object Id, PriorityClass`.
3. **Record when it ran and how big it was.** Write the start time, the end time and the worker
   count to the job's own log or to the operator lane's notes. When a turn later times out, this
   record is how anyone can tell whether a job was running alongside it.

**Why.** When a heavy job that broke these rules overlapped an implementer turn, the local gate took
3–4× its usual time, and the turn hit the 1800 s timeout. With the rules followed, the gate stayed
within about 1.3× its usual time. Only one measurement has confirmed this, so treat it as a working
rule, not a guarantee. The details are in thread `T-local-gate-takes-half-the-turn`.

**When to revisit.**
- If a turn's local gate takes longer than 240 s even though the rules were followed, consider an
  exclusive lease that pauses heavy manual jobs while a role turn runs.
- If an implementer turn times out and the operator still cannot tell, after 30 minutes with the
  transcript and the job logs, whether another job was running at the same time, reconsider
  recording host load in the turn record (D2 in that thread).

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

### Choosing the role sessions' model (`[loop].role_model` / `[loop].role_cli_path`)

Both default to unset, which is the behaviour every deploy had before they existed: the proposer and
the implementer run the CLI vendored inside `claude-agent-sdk`, on that CLI's default model.

`role_model` on its own is valid whenever the model is one the vendored CLI already supports —
naming `haiku` is a cost lever that needs no binary change, and nothing enforces a pairing that would
forbid it. What needs **both** is reaching a model *newer* than the vendored CLI, because there the
refusal comes from the API rather than the SDK: the symptom is a per-turn 400 that names a version,
not a startup error.

```
API Error: 400 Claude Code 2.1.133 does not support this model; version 2.1.280 or newer is required.
```

The SDK prefers its vendored binary over anything on `PATH`, and the newest CLI reachable under this
repo's `claude-agent-sdk>=0.1.0,<0.2` pin is 2.1.139 — so reaching a newer model means pointing
`role_cli_path` at a CLI installed on the host:

```toml
[loop]
role_model = "claude-opus-5-5"
role_cli_path = "C:/Users/<you>/.local/bin/claude.exe"
```

A `role_cli_path` that is not a file, or is not executable, stops the daemon at startup with a named
error rather than failing once per tick. It is resolved to an absolute path there too —
a relative one would be read against the daemon's working directory by that check and against the
session's `cwd` by the SDK, which are not the same directory.

Two things to know before setting them:

- **The CLI version stops being pinned by the lockfile.** Claude Code updates itself in place, so
  the binary a role session runs can change with no deploy and no diff. That is the cost of the
  override; leaving both unset keeps the vendored, locked binary.
- **Neither reaches the design-time naysayer, and neither may.** Its independence *is* its Lexora
  tier (ADR-05 §5), and a newer CLI breaks that route outright — measured 2026-09-23, same host and
  options, only the binary differing: vendored 2.1.133 returns the reply, 2.1.280 returns
  `422 body.messages[1].role: expected 'user' or 'assistant', got 'system'`. The naysayer adapter
  therefore has no `cli_path` parameter at all; see `adapters/_cli_selection.py`.

## Target-repo branch flow (V-4, 2026-08-02)

The dogfooding target (spirrow-voxelworld, renamed from `Spirrow-VoxelWorld` on 2026-09-11) switched
to a release-train flow with dev-speed plan
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
> branch rule recorded only there binds humans and not the loop. spirrow-voxelworld currently has no
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

## Naysayer tier routing (N-3, ADR-14 §7.3 / §7.6 / §10)

**Since ADR-14 §10 (2026-11-10, Takahito) every naysayer request goes to the codex tier.**
`naysayer/n3_routing.py::route_tier` (the only place that decides) always returns `naysayer`, and
Lexora's wrapper behind it is `codex_only`: when codex can't answer, the request is refused (HTTP 502)
instead of being answered by Gemini. Lexora no longer has a Gemini backend or a `naysayer-gemini` tier,
and requests without `X-Mindwire-N3-Route` go to codex too.

| tier | answered by | the attestation accepts |
|---|---|---|
| `naysayer` | codex (ChatGPT Pro, **no ZDR**). No fallback | `codex` |
| `naysayer-gemini` | *retired by §10* (was: Gemini only, paid key, project logging off — ADR-14 §9) | `gemini` (never routed to) |

While codex is closed, the naysayer stops: the PR-gate's pass 1 fails loudly on a Lexora error (a timeout
degrades to a COMMENT for the human), and Lexora's Discord notice says `codex unavailable`. A Lexora
deploy reopens codex itself (Lexora `deploy/RUNBOOK.md` §11).

The rest of this section is the §7.3 **classification**, now `n3_routing.classify_n3`. It still runs, but
only as the reason in the routing log line (`codex tier (N-3 classified, routed to codex per ADR-14 §10:
...)`); read "goes to `naysayer-gemini`" as "is classified N-3".

A request is classified N-3 (formerly: went to `naysayer-gemini`) when **any** of these holds:

- it carries the marker `n3-sensitive`: a PR **label** at the PR-gate, a thread **tag** at design time.
  Labels or tags that couldn't be read count as marked. The conductor passes the tags it got from
  `chatroom_get_thread`; the watcher path doesn't read them, so it always counts as marked;
- a path is outside the repository:
  - at the PR-gate, GitHub's file list is checked **lexically**, with no disk access: an absolute path,
    a drive letter or a `..` segment counts as outside. A deleted file is just a path;
  - at design time, each referenced path is resolved first: `~` is expanded, the path is made absolute
    against the summon directory, and `realpath` follows symlinks. It must exist and lie under the
    `realpath` of `git rev-parse --show-toplevel`;
- the PR-gate's changed-file list may be incomplete: the files endpoint can't be read, its count differs
  from the PR's `changed_files`, or it hit GitHub's 3000-file cap. On a rename **or** copy, both
  `filename` and `previous_filename` are matched;
- it touches `.mindwire-n3.toml`, the per-repo config file at the repository **root**, whatever the
  globs say;
- a changed or referenced file matches a glob. The globs are the union of three sets (ADR-14 §8):
  - the **org-wide defaults**, `N3_DEFAULT_SENSITIVE_PATHS` in `naysayer/principles.py`, applied to every
    repo: deploy, CI workflows, systemd units, env files, keys and credentials, auth code, and host
    security configuration. They live in MindWire's install, so no PR in the judged repo can loosen them;
  - a trusted side, the repo's own `n3_sensitive_paths` list, which is always enforced;
  - a working side, which may only add globs:
    - PR-gate: the PR's **base** ref is trusted; its **head** is the working side;
    - design time: the HOST_REPO's remote **default branch** is trusted, read through the contents API;
      the working side is `.mindwire-n3.toml` at the local repository root, never the current directory.

  A repo needs no `.mindwire-n3.toml` at all: without one (or without the key) the defaults alone decide,
  and the routing log line says `org default globs only`. Add the file only to list paths the defaults
  miss; `n3_sensitive_paths = []` means "nothing beyond the defaults";
- the config can't be resolved: either side unreadable or malformed, or no repository root;
- the prompt is over the codex tier's limit (`CODEX_TIER_PROMPT_CHAR_LIMIT`, §7.5). The value is
  provisional until Lexora pins its `model_catalog_json` / `context_window`. Raising the output
  reserve to 65,536 moved prompts of 145,537-179,072 chars from codex to Gemini. `mindwire
  codex-band-count` counts those requests from the Lexora ledger (`MINDWIRE_LEXORA_URL`) and sums
  their cost. It only sees requests made after this marking was deployed. If it prints
  `truncated=true`, the count is a lower bound: re-run it with a larger `--limit`.

Paths and globs are normalised (`\` → `/`, case-folded) before both the config-file match and the
glob match, and globs are matched with `fnmatch`, so `*` also crosses `/`. When the trusted side can't
be resolved, the router logs a WARNING containing `n3-routing-unresolved` and calls the injected
notifier, if one is set. Since §10 that only means the classification couldn't be made; the review
still runs on codex.
The scheduled sweep counts that token in each conductor run's output and the daily digest carries
one line per repo ("routed to Gemini (fail-safe)"); the counts reset after a full digest delivery.

**Which files design time routes on.** The design-time prompt is the thread, the principles SOT
(`spec/NAYSAYER_PRINCIPLES.md`) and the ADR manifest (`spec/adr_index.yaml`). The two files are
MindWire's own *system assets*, read from MindWire's install, not the host: they must resolve under
MindWire's root to a path in `PROMPT_ASSETS`, or the request goes to Gemini (a principles override
pointing elsewhere included). They are not matched against the host's globs, except when the host
is mindwire itself. *Host files* (none today) go through the boundary check and the host's globs.
A builder change that starts carrying another file must declare it; a structure test fails if the
files the builder reads and the files it declares differ.

**Residual risk, authorised by Takahito as a TIER-C goal decision on thread
T-D8-codex-backend-adr14-15-amendment:** suppose the review itself discovers a vulnerability, on a path
no glob lists and with no marker set. That request can't be classified in advance, and it reaches the
codex tier.

**Merge order (§7.6 / §10):** this routing and the per-tier attestation take effect together with Lexora's
codex path. Since §10 MindWire sends nothing to `naysayer-gemini`, so MindWire goes first and Lexora can
then drop that tier; a Lexora still in `fallback` mode answering with `gemini-fallback` fails the
attestation closed.

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
> drives whatever thread was left in the config, and if that thread has settled (`STOP: done` / `NEXT: none`) every
> fire is a 0-round no-op that still exits 0 and still shows green in the task history. Observed for
> an unknown number of days before the wrapper made it visible.

### The daemon runs from its OWN checkout, never a working one

**The scheduled task must point at a clone nobody edits** — on {{HOST_LOOP}},
`C:\Users\tomtar\spirrow-mindwire-daemon`, deliberately beside the data dir rather than in the dev
workspace, so the location itself says "not a place to work".

The reason is not tidiness. The venv holds an **editable** install, so the working tree *is* the
running module — there is no build or copy step to lag behind an edit. A daemon pointed at the
checkout a human works in therefore executes whatever is on disk at each tick, mid-edit and all.
Measured 2026-08-07, while two PRs were being written against that shared checkout: four ticks ran
work-in-progress code, one of them a version deliberately broken for a negative test, and the
sweep's per-thread state file was rewritten several times in two mutually unreadable schemas.
Nothing broke — but only because every path involved fails open. That is luck plus fail-open
design, not isolation.

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
| Trigger | at logon **and** a repeating trigger, 1 min, indefinite | the head probe makes a short interval nearly free; see below |
| `MultipleInstancesPolicy` | `IgnoreNew` | a tick that fires while the previous sweep is still working is dropped — this is the whole of the "already running?" handling, no lock file needed |
| `ExecutionTimeLimit` | `PT4H` | a real design round can take a while; `PT2H` was cutting runs off |
| Run as | the user holding `MINDWIRE_NAYSAYER_GITHUB_TOKEN`, the webhook var, and the Claude subscription | the wrapper reads the webhook from the **User** env scope |

There is deliberately **no `RestartCount` / `RestartInterval`**: the repeating trigger already runs
the next tick sooner than any restart interval worth setting, so a failed tick is simply retried by
the next one. If the task still carries them from an older setup, remove them.

### Deploying a merged change

**Merging is not deploying.** The task runs the loop (`uv run --no-sync python -m spirrow_mindwire.loop_runner`) from this checkout — the venv holds
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
  one tick of latency after a merge.
- **Nothing but a pure fast-forward is ever performed.** A dirty tree or a diverged branch means
  someone has work here; resolving that automatically would be the script inventing an answer nobody
  asked for. Untracked files never block — the live host deliberately carries untracked working notes,
  and a fast-forward cannot conflict with them.
- **The venv is synced by `sync-repo.ps1` and nowhere else** (T-composer-entrypoint-missing-drops-
  decision-cards D-2'). The loop and the decision composer both launch as
  `uv run --no-sync python -m <module>`, so nothing syncs mid-tick, and no `.venv\Scripts\mindwire-*.exe`
  is either needed or held open. `sync-repo.ps1` runs `uv sync --locked` whenever the SHA-256 over
  `pyproject.toml` + `uv.lock` differs from the one recorded by the last *successful* sync
  (`<data_dir>/state/venv-sync.json`), and counts a sync as successful only if the two launched
  modules then import. A failed sync records nothing, so every tick retries it. It reports
  `status=failed` with `deps_hash`, and the alert is deduplicated on that hash. `--locked` never
  rewrites `uv.lock`: a stale lock fails loudly and is fixed by committing a regenerated lock.
- **A sync failure is not a sweep failure.** One unreachable GitHub must not stop the loop; it keeps
  running the code it has, which is known-good and merely possibly old.

> The non-happy statuses report to **Discord**, not just the log. That is the point: a loop quietly
> running week-old code because `git fetch` has been failing is precisely the kind of correct-but-
> unread announcement that `spec/process/README.md` (the fail-open-placement rule) exists to prevent. Alerts are deduped on the reason, so a
> week spent on a feature branch costs one message, not 2016.

### Migration boundary — lease-state substrate change (T-exclusive-resource-lease-queue PR 4)

This subsection is the runbook that MUST be followed when a `main` merge lands PR 4 of
`T-exclusive-resource-lease-queue`. Before that merge, the "who holds the editor" contract lived
as a sentence in a Tier-C decision message (msg-923's `暫定運用: T-materializechunk-zone-relocation-
crash (C2) が editor/PIE を保持`). After the merge, the same contract lives in
`<data_dir>/state/leases.json`, and the wrapper's `Read-LeasesStateForTick` phase reads that file
at the start of every tick.

**The substrate change is what this runbook exists for.** The wrapper cannot look at the disk and
tell "day-1 of a fresh daemon, no physical holder anywhere" apart from "day-1 of a daemon replacing
a chatroom-recorded manual hold" — the two cases look identical from the file system (no
`leases.json` present). The distinction has to be an operator declaration. So:

- **File present on disk = migration complete.** The wrapper treats the state map as authoritative.
- **File absent on disk = MIGRATION NOT COMPLETE.** The wrapper defers every lease-requiring
  candidate (disposition `lease-state-unreadable`) and REFUSES to create the file itself — creating
  it would silently declare migration complete and open the "physical editor still held by C2 while
  the wrapper granted it to someone else" collision this whole feature exists to prevent.

The behaviour is a "fail-closed on missing" policy (P4-3 v4.1 in the design thread). Deploy-time is
the only moment where "missing" is expected; from that moment on, a missing file means an operator
mistake, not a normal state.

STEP 1 — pre-deploy observation (operator).

Observe the CURRENT owner of the editor lease. Sources, in priority order:

  (a) the most recent Tier-C decision text that names an editor holder. msg-923 § 暫定運用 named
      `spirrow-voxelworld/T-materializechunk-zone-relocation-crash` (C2); VERIFY this is still
      current when you are actually deploying — a Tier-C decision from months back MAY have been
      superseded by a later message. Ask the human to re-declare "who currently holds the editor"
      out loud before proceeding;

  (b) direct observation of the physical editor process on the host — any active PIE session? any
      editor PID? A held file lock on `<repo>/<pie-lockfile>`?

Result of STEP 1: either an owner key K (e.g. `spirrow-voxelworld/T-materializechunk-zone-relocation-crash`),
or "no active manual holder".

STEP 2 — daemon pause (operator).

Disable the Task Scheduler task that runs `deploy/run-conductor-scheduled.ps1` (see § Task
Scheduler above for the task name). Verify no active daemon tick is in flight: watch the current
log file (`<data_dir>/logs/conductor-YYYY-MM-DD.log`) until the current tick's line lands, or
until the buffered tick completes silently.

STEP 3 — code deploy (implementer half).

Run `deploy/sync-repo.ps1` in this checkout to fast-forward to `origin/main`. Confirm the sync
returns `updated` (or `current` if you already synced) and no `blocked`/`failed` verdicts.

STEP 4 — seed migration state (operator).

Based on the STEP 1 result:

- **If owner key K was found:** run

  ```powershell
  pwsh deploy/Grant-Lease.ps1 -Resource editor -Holder <K> `
      -Reason "human-migration: seeded from <source msg-id or Tier-C ref>"
  ```

  The `human-migration:` prefix distinguishes this deploy-time seed from an ad-hoc `human-grant`
  or `human-clear` in the audit trail (permanent `reclaimed_reason` field, per msg-1900). The
  `human-*` family names the `human` role from ADR-2026-05-29-10.

- **If no active manual holder:** create an empty `leases.json`:

  ```powershell
  Set-Content -Path <data_dir>/state/leases.json -Value '{}'
  ```

  An empty JSON object is the file-based migration marker: it establishes "migration completed, no
  holder anywhere". The wrapper's `Read-LeasesStateForTick` treats this as `verdict='valid'` with an
  empty state map, and the acquire path opens for the next lease-requiring candidate that runs.

STEP 5 — verify (operator).

Confirm the seed landed:

```powershell
Test-Path <data_dir>/state/leases.json           # MUST return $true
Get-Content <data_dir>/state/leases.json | ConvertFrom-Json   # MUST parse (no ConvertFrom-Json exception)
```

If STEP 4 seeded a holder, `Get-Content ... | ConvertFrom-Json | ConvertTo-Json` should render
the expected holder key. If STEP 4 seeded an empty state, the object should parse to a
zero-property PSCustomObject.

STEP 6 — resume (operator).

Re-enable the Task Scheduler task.

STEP 7 — watchpoint (operator).

Watch `<data_dir>/logs/conductor-YYYY-MM-DD.log` for the first 3 ticks after resume. Any
`lease-state-unreadable` disposition line means the migration was incomplete or the seed was
corrupted (parse-error on the file). See *Recovery* below.

Recovery (STEP 7 shows a failure).

Re-run the runbook: pause the daemon (STEP 2), inspect `<data_dir>/state/leases.json` on disk
(present? valid JSON object? holder key spelled correctly?), re-run STEP 4 as appropriate, and
resume (STEP 6). **Do NOT manually delete `leases.json` to "reset"** — a delete flips the wrapper
to the "missing" branch, indistinguishable from unmigrated, and if a physical holder is still
running the next tick's automated acquire silently double-allocates the resource. Same hazard
applies to accidental truncation: if `leases.json` becomes blank, whitespace-only, or a JSON
array (`[]`), the wrapper treats it as `verdict='unreadable'` (NOT as valid-with-no-holder) —
this is fail-closed by design and prevents silent-double-allocation, so **do NOT `> leases.json`
or write blank lines to it** as a shortcut. Any of the fail-closed cases (root array, root
scalar, JSON parse error, blank/whitespace, `[]`) **must be inspected and repaired directly**:
`Read-LeasesStateForTick` returns `verdict='unreadable'` and the T-5 flush is skipped, so the
file stays in place as your forensic evidence. Do NOT expect a `.bad-<utc>` companion file:
the P4-3 v4.1 fail-closed policy skips the flush path that would invoke
`Save-CorruptedStateBackup`, so no rename happens under this branch.

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

The wrapper **advances past any candidate the conductor reports no work for**, so one settled thread
cannot park the whole loop. List order is only the tie-break for launches: see *Launch fairness* below. Only a clean `rounds=0` advances: a non-zero
exit or an unparseable run **stops** the sweep, so a genuine breakage is never laundered into
"everything is idle".

`T-pr-review-*` threads are deliberately absent from that list. They resolve to
`NEXT: pr-review <ref>`, which fires the Tier B PR-gate against the paid Lexora/Gemini backend —
driving those from an unattended schedule would spend money on a timer, so it stays a human action.
(The entire `T-pr-review-150`〜`167` family was closed by the 2026-08-02 K-5 triage — all PRs merged
with their verdicts recorded — but the exclusion rule here stays for future `T-pr-review-*`
threads.)

### Launch fairness — dispatch order, gate lane, budgets (T-sweep-starves-deep-candidates)

The sweep used to walk `sweep.json` head-first and stop at the first thread that did work. While
the top of the list kept working, nothing below it ever ran (2026-10-02: every tick was "worked 1 /
not-reached 96〜100", and #392 / #361 / #359 waited 21〜31h). The old starvation metric did not see
it: W-2c counts a LAUNCH verdict as an evaluation even when the sweep never reaches the candidate.
The code is in `deploy/lib/SweepFairness.ps1`, and the tests are in `tests/Test-SweepFairness.ps1`.

- **Order.** `evaluated.json` carries `launch_wait_since` per thread. It is set when the verdict is
  LAUNCH and the thread was not launched, and it is never overwritten. It is cleared when the thread
  is actually launched (after commit-launch) or when the verdict is SKIP. It is kept through DEFER,
  held, and quarantined. LAUNCH candidates run in order of `launch_wait_since`, oldest first, so a
  thread that just ran goes to the back. `sweep.json` order only breaks ties. Reordering the file
  no longer moves a thread ahead of one that has waited longer.
- **Gate lane.** A LAUNCH whose `NEXT:` is `pr-review` runs first. Up to **3 launches** per tick,
  and a gate that did work does not stop the sweep. After the gate lane, role turns run as before:
  the first one that did work ends the sweep. Gate failures count toward the same K-budget, and
  reaching K stops both lanes.
- **Gate-only slice.** A gate-lane launch passes `--gate-only` to the conductor (role-lane launches
  and hand runs do not). When the gate returns REQUEST_CHANGES, or CI is red on the head (R4), the
  conductor posts the relay (or the ci-route) and stops on `slice_end` instead of starting the
  implementer. The post stays the head, so the next tick decides it LAUNCH in the role lane and the
  gate resume restarts the implementer from it, after checking GitHub. The trade-off is deliberate:
  a CI-red fix or a REQUEST_CHANGES fix no longer jumps ahead of other threads. It waits its turn in
  the role queue, because only the cheap part (firing the gate) gets priority. In the §0 measurement
  (msg-6312), 88% of gate-lane time was the implementer's turn riding the priority lane. The slice
  is skipped when the conductor has no rollup or review source wired
  (`conductor.gate_only.ignored reason=resume_unavailable`). It is also skipped for a relay the
  resume would not pick up (`reason=not_resumable`, for example the first advisory APPROVE). Both of
  those run inline as before. `slice_end` is not terminal and sends no notification.
- **Budgets — two clocks, checked before every launch.**

  | Admission of | Condition |
  |---|---|
  | the first gate candidate | `tickElapsed < LaunchBudgetSeconds` (it uses up this exception whether or not it launches) |
  | each later gate | the above **and** `loopElapsed < GateBudgetSeconds` **and** fewer than 3 gates launched |
  | a role turn | `tickElapsed < LaunchBudgetSeconds` |

  `tickElapsed` counts from the start of the tick (sync, probes, and decide included).
  `loopElapsed` counts from the start of the launch loop (decide excluded). Once the launch budget
  is spent, the sweep stops with `time-budget`. Everything not launched keeps its
  `launch_wait_since`, so it comes first next tick. The budget limits when a launch may **start**,
  not how long it runs, so a tick can still run past it. When that happens the log says
  `WARN tick exceeded launch budget`.

**Setting the budgets.** The order of precedence is the script argument (`-LaunchBudgetSeconds` /
`-GateBudgetSeconds`), then the environment variable (`MINDWIRE_SWEEP_LAUNCH_BUDGET_SEC` /
`MINDWIRE_SWEEP_GATE_BUDGET_SEC`), then the default (**120 / 60**). `GateBudgetSeconds` must be
less than `LaunchBudgetSeconds`. A tick with any other configuration aborts with a configuration
error, and so does a tick with a value that is not a positive integer.

| Task setting | Value today | Budget it interacts with |
|---|---|---|
| repeating trigger | **1 min** | none directly. Under `IgnoreNew`, the first trigger after a tick ends starts the next one, so a shorter launch budget means the next tick (and its gate lane) comes sooner |
| `MultipleInstancesPolicy` | `IgnoreNew` | ticks cannot overlap, whatever the budget. The budget bounds how long one tick holds the loop |
| `LaunchBudgetSeconds` / `GateBudgetSeconds` | 120 / 60 (defaults) | **if you change the trigger or the policy, revisit these here** |

The design that chose 120 / 60 (msg-5588) assumed a 5-minute interval and set the budget to keep
ticks from overlapping. Measured on the host on 2026-10-02, the trigger is PT1M with `IgnoreNew`,
so overlap is already impossible. The pre-loop phase (sync, probes, decide) took about 22s. Tune
against the per-tick line:

```text
tick elapsed=…s decide=…s loop=…s budget=gate 60/launch 120s (gate launched n/3 of m candidate(s)[, gate lane closed: gate-cap|gate-budget])
```

**Two starvation metrics, both in the log and in the digest.**
- `飢餓` / `starved threads`: not *evaluated* for 24h. Unchanged.
- `起動待ち飢餓` / `launch-wait starved threads`: LAUNCH for **6h or more** without a launch. This
  is the failure the old metric could not see. W-2c's `last_evaluated_at` refresh does not reset it.

Other WARN lines: `decide overhead exceeds launch budget` (or `pre-loop overhead …`) means nothing
can launch this tick, so fix the budget or the slow phase. `gate admission invariant violated`
means a role turn was admitted while gate candidates existed and none of them had been attempted.
That should be impossible, and seeing it means the admission code has a bug.

**Pre-loop overhead (T-sweep-preloop-overhead-exceeds-launch-budget).** By 2026-11-04 the pre-loop
phase had grown from about 22s to a median of 123s, every tick went over the 120s budget, and nothing
launched for 2.5h without any alert. Two things were added (`deploy/lib/PreLoopBudget.ps1`):

- every tick logs which phase took the time (per-project phases are named `control[p]`, `1b[p]`,
  `parkwake[p]`, `head[p]`):

  ```text
  pre-loop phases: sync=…s intake=…s gatebootstrap=…s control[p]=…s 1b[p]=…s parkwake[p]=…s head[p]=…s autogate=…s declaredparkwake=…s operator=…s resource=…s decide=…s (pre-loop=…s launch budget=…s)
  ```

- a notification (key `sweep:preloop-budget`) when ticks keep going over budget. Each tick's level
  is `ok` (under 80% of the launch budget), `near` (80–100%) or `over` (100% or more, nothing
  launches). Three `over` ticks in a row send the **over** alert. Six `near`-or-`over` ticks in a
  row, with nothing sent yet, send a **near** warning. The first non-`over` tick after an over alert
  sends **recovered**. An `ok` tick resets everything. At most one alert per tick, and no near warning
  ever follows an over alert. State: `state\preloop-budget.json`. The daily digest gets one line:
  the last 24h tick count, the over-budget ticks, and the median and max pre-loop time.
- the per-project order is now control → head probe → 1b → park-wake. 1b and park-wake get the
  heads (`--heads-json`) and a tail cache per project (`state\tail-cache\<project>.json`, keyed by
  each thread's head msg id, no TTL). A thread whose head has not moved is not read again. A cache
  file that is missing, broken or on another schema version means a full read, as before. 1b and
  park-wake return `posted_heads`, which the sweep writes over its heads before the next step, so
  their own posts still show as moved heads in the same tick. A post whose msg id is unknown removes
  the thread from the heads, so it is treated as unknown and launched. If a step returns nothing
  usable (timeout, crash), it may have posted somewhere, so the heads are read once more.

### Why the sweep is cheap enough to run on a short interval

Launching the conductor is not uniformly cheap:

- a **settled or human-parked** thread costs one MCP read and **no inference** — the conductor reads
  the head message, resolves a settle (`STOP: done` / `BLOCKED-ON:` / legacy `NEXT: none`) or
  `NEXT: human`, and stops;
- a thread whose `NEXT:` names a **role** costs a real role dispatch. When that role then posts
  nothing (`no_progress_to_human`), the tick has bought nothing and billed for it.

So the wrapper does not launch blindly. **`scripts/thread_heads.py`** answers "did anything change?"
from data: one `chatroom_list_threads` call returns every open thread's `last_msg_id` without fetching a
single message body (~1 s for all threads at once). If a thread's head equals the `last_msg` the
conductor reported last time, the conductor would resolve the same handoff and reach the same stop —
so it is not launched at all.

**The head alone is not enough to say "same stop".** Two inputs decide where a round lands, and an
earlier iteration of this predicate keyed a skip cache on both: the thread's head message *and*
the project's loop control state (`state/heads.json`, entry per thread). At an unchanged head, a
naysayer→implementer handoff stops at the human gate under `hold` / `supervised` but dispatches
the implementer under `run` (carve-out ③ — since the D-4' guardrails, only when the naysayer's
proceed carries `TIER-C-CHECK: none` on the line above its `NEXT:`, no `TIER-C:` line has been
posted since the human last spoke, and the Tier-C Decider has not vetoed the proceed — G3 is a
veto, so with the Decider off (`backend=off`), undecided or failing, G1 and G2 alone decide).

That head-equality predicate has been retired (T-sweep-intake-and-quarantine-stalls, Bohr
msg-1428〜msg-1432). Measured 2026-08-11, it burned inferences on a `T-track-b-seam-octree-
retirement`-shaped spin — head kept moving, nomination and control were unchanged, the equality
check said "not the same head" and launched every tick with nothing to show for it. Worse, the
starved candidates behind the spin were structurally invisible from the log. Its replacement is
**a nomination-target predicate** implemented as `scripts/head_skip_decide.py` + the module
`src/spirrow_mindwire/conductor/head_skip.py`:

- **Stage 1 (stop tokens).** `token ∈ {none, human}` → SKIP. The set is closed at import time
  and cannot silently expand.
- **Stage 2 (time judgment).** Otherwise LAUNCH or DEFER (never SKIP). Exponential backoff on
  no-progress LAUNCHes, capped at CAP=60 min.
- **Head msg-id is audit-only.** A head that moves under an unchanged nomination + control does
  NOT count as progress — the exact spec-degeneracy that motivated the rewrite.
- **Fail-open on the fetch path.** A body-fetch failure yields UNRESOLVED which LAUNCHes; the
  synthetic empty observation is not cached (round-2 anti-poisoning rule inside the module).

The wrapper's role is now purely a caller: one `--mode decide` batch per project at the top of
the tick, then `--mode commit-launch` per candidate the wrapper actually spawns, BEFORE the
spawn. The CLI owns `<data_dir>/state/head_skip.json` (single writer, atomic replace). Anywhere
the wrapper cannot reach the CLI — interpreter absent, decide crashed, output not parseable,
non-zero exit — the tick aborts with `exit 1` (fail-closed): if `decide` is broken then
`commit-launch` is broken too, and spawning conductors that will not be recorded would bypass
the backoff CAP entirely. `tests/Test-SweepHeadCache.ps1` (run by `.mindwire-gate`) guards the
wiring; the two-stage judgment itself is guarded by `tests/test_head_skip.py`.

The old `state/heads.json` is gone with its predicate. Its readers, writer, and merge-on-write
were removed in the same commit as the wire (Bohr msg-1432 §W-2 explicit: no conditions, no
back-up copy). An operator upgrading past the change can safely delete any leftover
`state/heads.json` on disk — it is no longer read.

Measured on a 6-candidate list:

| | conductor launches | elapsed | log written |
|---|---|---|---|
| bootstrap (no state yet) | 3 | 18 s | full detail |
| second pass | 3 | 11 s | full detail |
| **steady state** | **0** | **3 s** | **182 bytes** |

An earlier design used a cooldown timer instead. It was dropped: a timer guesses at when re-running
might be worthwhile, the head id knows.

**Everything unknown fails open.** A probe failure, a thread missing from the probe's result, or a
thread with no recorded head all launch the conductor anyway: a gap must cost one cheap run rather
than silently parking a live thread forever.

**A head with no `NEXT:` line and no handoff field is launched once, then parked**
(T-no-field-no-next-handoff-silent-park). The conductor ends that run on `no_handoff_to_human`.
Because that stop is decided by the head alone, `no_handoff_to_human` is one of the
`TERMINAL_STOP_REASONS` in `head_skip.py`. As long as the head stays the same, the log shows
`terminal-stop:no_handoff_to_human` (SKIP) every tick instead of a new launch, and the Discord
alert fires once. Adding a `NEXT:` line by **editing** the parked message works, but only when the
cached parse ages out (`HEAD_CACHE_TTL`, up to 60 min). Until then the edit is not seen. To resume
right away, post a **new** comment with a `NEXT:` line. A new message moves the head, so the next
tick launches it. The same edit rule applies to every terminal stop reason.

> **2026-09-30 (T-unread-correlated-count-scale)**: the probe used to call `chatroom_my_unread` as a
> dedicated never-reads identity (`conductor-probe`), so that every thread stayed unread and hence
> listed. That inbox evaluates a per-thread unread count for every thread in conclair — a cursorless
> identity is its worst case — and the probe only ever read the head id. At 100× today's data the
> inbox call measured 657 ms against 15 ms for the listing on the same rows (conclair perf harness,
> CI run 35128633398). The inbox's exclusion rule was also never fully characterised (it once reported
> 11 threads where `chatroom_list_threads` showed 33 active). The probe now calls
> `chatroom_list_threads` with every status except `resolved` — the inbox's `include_resolved=false`
> set — and reads `last_msg_id`. Before the switch the two were compared live on all six projects:
> identical thread sets, identical head ids (216/216). No identity is involved any more, so there is
> no read cursor that can blind the probe.

### State and logs

| Path | Contents |
|---|---|
| `<data_dir>/logs/conductor-YYYY-MM-DD.log` | sweep log. Detail is buffered and only committed when a tick actually does something — an idle tick collapses to one line, which is what keeps a minute-scale cadence readable |
| `<data_dir>/logs/clock-YYYY-MM-DD.log` | clock-sync log |
| `<data_dir>/state/head_skip.json` | per-thread head-skip predicate record (nomination-target, launch attempts, cached observation) — the skip decision above. Single writer: `scripts/head_skip_decide.py`; atomic replace |
| `<data_dir>/state/notified.json` | last alert fired per thread, for de-duplication |
| `<data_dir>/state/quarantine.json` | quarantined threads — one entry per `project/thread_id`; see *Quarantine and daily digest* |
| `<data_dir>/state/quarantine-history.json` | append-only clear log; every `Clear-Quarantine` writes its `-Reason` here |
| `<data_dir>/state/retry-pending.json` | `{ pending, events }` — threads whose first failure is waiting for its one automatic retry, plus the retry events the next digest counts; see *Retry once before quarantine*. Single writer: the sweep |
| `<data_dir>/state/evaluated.json` | `first_seen_at` + `last_evaluated_at` per **live** thread; the starvation metric pivots on the current sweep list and prunes ex-live keys |
| `<data_dir>/state/digest.json` | `last_sent_at` of the daily digest — one send per 24h max |
| `<data_dir>/state/conductor-inflight.json` | in-flight launch ledger: one row per launch whose exit is not yet confirmed, plus an optional `*` sentinel. Gates the sweep's hold acknowledgement for a parked clone; see *In-flight ledger and the hold acknowledgement* below. Single writer: the sweep; atomic replace. **Do not delete the file** — remove rows instead |
| `<data_dir>/state/leases.json` | exclusive-resource lease map — one entry per resource name (v1: `editor`), each with holder / acquired_at / queue / audit fields. **Only shape `{...}` (JSON object) is treated as a valid migration marker** (see § Migration boundary). A missing file is treated as UNMIGRATED, NOT bootstrap: lease-requiring candidates are deferred and the wrapper refuses to create the file automatically. If a subsequent tick reads the file and finds any non-object root (root array, root scalar, JSON parse error, blank/whitespace, `[]`), the P4-3 v4.1 policy fails closed: `verdict='unreadable'`, T-5 flush skipped, corrupt file preserved in place as forensic evidence. `Save-CorruptedStateBackup` (the `.bad-<utc>` rename) is NOT invoked on that path. Do NOT delete or truncate without following the recovery steps in § Migration boundary |

Deleting `head_skip.json` costs one full bootstrap sweep (every thread launches once, no
backoff); `notified.json` at most one duplicate alert.
Deleting `retry-pending.json` forgets every pending first failure, so each of those threads gets
one more retry than it would have; it never quarantines anything by itself.
Deleting `quarantine.json` **un-quarantines every thread silently** — do not do it as a shortcut for
`Clear-Quarantine`; the history file exists precisely so cleared-with-reason and cleared-without-
context are not confusable later. Deleting `evaluated.json` resets the starvation clock (harmless,
one tick of empty starvation report). Deleting `digest.json` forces the next tick to send a digest.
Deleting `leases.json` **flips the wrapper to the UNMIGRATED branch** — the next tick reads the
file as missing and defers every lease-requiring candidate; more dangerously, if a physical
holder is still running when the operator re-runs the runbook, the operator's manual re-seed
followed by the next tick's automated acquire can silently double-allocate. Never delete it as a
shortcut. Truncating it (blank / whitespace / `[]`) does NOT bypass this: the wrapper treats those
shapes as `verdict='unreadable'` fail-closed, not as valid-with-no-holder, so the next tick defers
rather than granting — but repairing then requires following the same Recovery path. Follow §
Migration boundary Recovery in either case.

### In-flight ledger and the hold acknowledgement

A HOLD is acknowledged (`observed_state=hold`) by a launched conductor. An acknowledgement counts
only if it is not older than the request: `observed_at >= desired_at`, compared as instants; when
a timestamp is missing, `observed_state` alone decides. An `observed: hold` left over from an
earlier hold is stale, so the project is probed and launched again like an unacknowledged one. A
project that launches nothing this tick (its clone is parked, or it simply has nothing to launch)
used to stay `HOLD NOT ACKNOWLEDGED` indefinitely. The sweep now writes that acknowledgement itself
(`scripts/loop_control.py --report-observed-hold --why <why>`, actor `sweep:dirty-clone-parked`
for a parked clone, `sweep:no-launch` otherwise), once per tick per project, but only when
**both** hold:

- no candidate of that project launched this tick, and
- `state/conductor-inflight.json` exists, parses, and has neither a row for that project nor a
  `*` row.

This covers a project with **no candidate at all** too: a project listed only under `clones` or
sweep.json `projects` has its control read by the 1c / declared-projects scans, and a hold there
that is not freshly acknowledged is logged as `HOLD NOT ACKNOWLEDGED [<project>]: no candidate …`
and acknowledged with `sweep:no-launch`. A held project the sweep config does not mention anywhere
is never read, so it is not acknowledged.

The ledger is how the sweep knows no orphaned conductor may still be running the project:

- **Before each launch** the sweep adds a row `{ "<GUID>": { project, key, started_at } }`. If that
  write fails, nothing launches and the tick stops launching (`inflight-mark-unwritable` in the log).
- **After the launch**, on any exit code but 7, the sweep removes that launch's own row. On exit 7
  (kill-unconfirmed: an orphan may be alive) the row stays; if the removal fails, the row stays too.
- **A file that does not parse** is copied to `conductor-inflight.json.corrupt-<ts>` (the original
  stays in place until replaced) and replaced by a file holding the `*` sentinel, `reason: corrupt`.
- **A missing file** is created holding the `*` sentinel, `reason: absent` (log line
  `inflight-file-created (absent; sentinel set)`).

A row left by exit 7, a sweep crash, or a failed delete is never cleared automatically, and neither
is a `*` sentinel (`corrupt` / `absent`). Launches are not affected by either; only the sweep's own
hold acknowledgement is withheld, and that project's acknowledgement lands as before, at the first
launch after the clone is unparked. The log says which: `hold-ack via park <proj>: skipped (<reason>)`.

**Clearing (operator).** The sweep writes this file on every launch, so the operator must never
edit it while a sweep can run: a read-modify-write that straddles a sweep's new launch row would
overwrite that row, and if that conductor then exits 7 nothing would hold its project back from a
wrong acknowledgement. Clear rows only inside this stop → check → edit → resume window:

1. **Stop the sweep.** Disable the Task Scheduler task that runs
   `deploy/run-conductor-scheduled.ps1` (disabling stops new ticks, not the one already running),
   then wait until no wrapper process is left:

   ```powershell
   $t = Get-ScheduledTask | Where-Object { "$($_.Actions.Arguments)" -match 'run-conductor-scheduled' }
   $t | Disable-ScheduledTask | Out-Null
   while (Get-CimInstance Win32_Process | Where-Object { "$($_.CommandLine)" -match 'run-conductor-scheduled' }) { Start-Sleep 10 }
   ```

   Do not go on while that loop is still waiting.
2. **Check the processes.** On the loop host, check that no `mindwire-loop` / `claude` process of
   the conductor is left for that project (for the `*` sentinel: for any project). If one is, stop
   here: re-enable the task (step 4) and leave the row in place.
3. **Edit.** Remove **only** the row(s) concerned — the project's GUID rows, or the `*` row — and
   leave the file in place:

   ```powershell
   $p = Join-Path $env:MINDWIRE_PATHS__DATA_DIR 'state\conductor-inflight.json'   # or ~/spirrow-mindwire-data
   $o = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json
   $o.PSObject.Properties.Remove('*')            # or the GUID key of the row to clear
   $o | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $p -Encoding utf8
   ```
4. **Resume.** Re-enable the task: `$t | Enable-ScheduledTask | Out-Null`.

Do not delete the file while a project might have an orphaned conductor: a missing file is
re-created with the `*` sentinel, so deleting it clears nothing and only discards the rows.

**One-time step after deploying this change.** Every tick starts by creating a missing file with the
`*` (`absent`) sentinel, before it looks at any candidate — so the first tick after the deploy
creates it even when every candidate is parked (log line `inflight: inflight-file-created (absent;
sentinel set)`). An orphan from before the deploy cannot be ruled out, so the sentinel withholds the
sweep's acknowledgement until an operator clears it with the stop → check → edit → resume procedure
above.

## Stall ledger tick (D-16c)

The stall ledger (`src/spirrow_mindwire/stall_ledger/`, T-stalled-pr-has-no-detector) runs as its
**own** Task Scheduler task, separate from the conductor sweep. Each run is one heartbeat: it
evaluates open PRs, chatroom threads and `quarantine.json`, updates
`<data_dir>/state/stall-ledger.json`, and appends JSON lines to
`<data_dir>/logs/stall-ledger-YYYY-MM-DD.jsonl` (stderr goes to `…-YYYY-MM-DD.err.log`).
It is **log-only**: it sends no Discord message and posts nothing anywhere. Alerts come with D-16d.

Timing (`src/spirrow_mindwire/stall_ledger/timing.py`; a test fails if these stop holding):

| Value | Setting | Why |
|---|---|---|
| `HEARTBEAT_INTERVAL` = 15 min | task `RepetitionInterval` | one tick per interval |
| `T_TICK_MAX` = 10 min | `--t-tick-max-seconds` default | a tick past this saves nothing |
| `T_LOCK_STALE` = 14 min | task `ExecutionTimeLimit` | the scheduler kills a hung tick, and the OS frees its lock, before the next tick fires |
| `FETCH_TIMEOUT` = 30 s | `--fetch-timeout-seconds` default | per request |
| `FETCH_MARGIN` = 2 min | — | body fetches stop starting so the tick can still save |

Register it once, on the loop host, from the daemon checkout. This is an operator step: nothing in
the loop registers it.

```pwsh
cd C:\Users\tomtar\spirrow-mindwire-daemon
# check what will be registered
pwsh -NoProfile -File deploy\Register-StallLedgerTask.ps1 -Checkout (Get-Location) `
    -Repo SpirrowGames/spirrow-mindwire -Project spirrow-mindwire -DryRun
# register
pwsh -NoProfile -File deploy\Register-StallLedgerTask.ps1 -Checkout (Get-Location) `
    -Repo SpirrowGames/spirrow-mindwire -Project spirrow-mindwire
```

Run it as the same user as the sweep task, so the tick sees the same data dir. The task's action is
the **absolute** path of `pwsh` (resolved at registration and shown by `-DryRun`): Task Scheduler
does not resolve a bare `pwsh`, and a Store-installed one failed every launch with `0x80070002`
without writing a line. Registration stops if no absolute path can be resolved.

That pwsh is started by `deploy\headless_launch.py` running under `pythonw.exe`. The task's
`Execute` is `pythonw.exe`, and `Arguments` is
`-I "<checkout>\deploy\headless_launch.py" --limit-seconds <N> "<pwsh>" -NoProfile -File ...`.

- **No window.** Launched directly, pwsh opened a console window on every tick and took keyboard
  focus from whatever the operator was typing into. pythonw has no window, and it starts pwsh with
  `CREATE_NO_WINDOW`.
- **The limit still stops a hung tick.** `conhost.exe --headless` also hides the window, but then
  the task's `ExecutionTimeLimit` stops only conhost and leaves pwsh running. So the launcher stops
  pwsh itself after `<N>` seconds: the task limit minus 30 s, which keeps the task limit as an
  outer backstop. A tick stopped this way reports `LastTaskResult` 121. Otherwise the task
  reports pwsh's own exit code.
- **Which pythonw.** By default it is the one next to `uv python find --system`; pass `-Pythonw`
  to choose another. A venv's `pythonw.exe` is refused, because it is only a stub that starts the
  real interpreter as a second process. Registration stops if no pythonw or launcher is found.

After changing the registered action, run `pwsh -NoProfile -File tests\Test-StallLedgerTaskHeadless.ps1`
on the loop host. It registers throwaway tasks from the script's own `-DryRun` output, and checks
that the wrapper runs with no visible window, that a hung wrapper is stopped at the limit, and that
the exit code comes through.

Re-running it replaces an existing `mindwire-stall-ledger` task with this script's definition
(`-Force`). Success is decided by reading the task back, not by whether `Register-ScheduledTask`
threw: the script compares Execute / Arguments / RepetitionInterval / ExecutionTimeLimit of the
registered task with what it meant to register, prints `verified mindwire-stall-ledger ...` only when
all four match, and throws on a missing task or any mismatch. If the registration itself failed but
the existing task already matches, the `verified` line says so and nothing was replaced.

**GitHub token.** Set `MINDWIRE_STALL_LEDGER_GITHUB_TOKEN` in that user's **User** environment to
the implementer PAT (`takahito-spirrowgames`; the ledger only reads). Never use the naysayer token.
`run-stall-ledger-tick.ps1` hands it to the tick process only, as `MINDWIRE_GITHUB_TOKEN`, and drops
any inherited `MINDWIRE_GITHUB_TOKEN` / `GITHUB_TOKEN`. It is a separate variable so that it does
not also reach the conductor. Do not put the token in the task definition or its arguments. If it
is unset or blank, the github source makes no request and reports `fetch_outcome: auth_missing`
(heartbeat `ingest_failure`, plus a line in the `.err.log`). Without a token the tick would
otherwise read unauthenticated and run into GitHub's 60/hour per-IP limit.
`-Repo` / `-Project` repeat as comma lists. Check that it is working: after one interval, the
newest line of the day's `stall-ledger-*.jsonl` is a `heartbeat`. In that line,
`budget_exhausted: true` together with a `pending_markers` count that keeps growing means body
fetches are falling behind (see `deferred_fetches`).

**Thread heads (D-16d-1).** The tick reads a chatroom thread's head message only when the
thread's `(last_msg_id, last_activity_at)` in the listing differs from the cached read. The
cache is the store's top-level `thread_heads` object. Each read is one `chatroom_get_thread`
call, taken from the same fetch budget as marker fetches, so a tick reads at most the threads
that had a new post since the last read. The head is sorted into a group, and the group sets
the thread's `N`:

| Group | Head | `N` |
|---|---|---|
| A | `NEXT: <persona>` / `NEXT: pr-review`, and the `next_participant` field (if set) agrees | 6h |
| B | `STOP: done` | never opens; an open record closes |
| C | anything else: `NEXT: none` without `STOP:`, no `NEXT:`, a refused form, field ≠ body | 6h |
| D | `BLOCKED-ON:` (any trigger, never evaluated), `NEXT: human`, a well-formed `NEXT: operator` | 72h for now; D-16d-2 sets `T_WAIT_AUTO` / `T_WAIT_MANUAL` |

The hand-off to the PR-gate uses only the body line `NEXT: pr-review <owner/repo#n>`. The
`next_participant` field cannot carry it. Magickit's writer accepts only names of registered
identities and has no gate sentinel, by the owner's decision (msg-072 §2). It therefore refuses
`pr-review <ref>` and bare `pr-review`. The reader's field branch for the sentinel is only a
fail-safe.

A head the tick could not read yet is treated as C with the old 72h. It is counted in the
heartbeat's `pending_heads` and read on a later tick; `head_fetches` is the number of reads this
tick. Only a failed read is retried: a thread read in full whose head message is absent or has no
text body is cached as C (`head_missing` / `head_malformed`) under the same key, so it is not
fetched again until a new post changes the key. A cache entry is dropped when the listing shows the thread `resolved`, or when the thread is
missing from a listing that was read in full on a tick with no failing source. A failed or partial
listing drops nothing. A legacy `STOP: blocked-on … wake:<x>` line above `NEXT: none` needs the
roster to check its wake, which the tick does not have, so it reads as C.

**`--report` (D-16d-1).** `uv run python scripts/stall_ledger_tick.py --data-dir <dir> --report`
prints one JSON line built from the store alone. It makes no chatroom or GitHub call, takes no
lock, uses no fetch budget and writes nothing, so it is safe to run while the task is live. It
shows `heads_by_group` (cached heads per group A–D; B, `STOP: done`, is only counted here),
`pending_heads` (the heads the last tick that wrote the store could not read; the tick keeps
them in the store's `thread_heads_pending`), `next_human` (the D heads that are `NEXT: human`,
count and list — they still open at 72h like the rest of D) and `open_records_by_group` (every
open record under its thread's group; an unread thread is under C with `head_read: false`, a
PR record under `not_thread`, and every `quarantined_records` entry under `not_thread` with
`quarantined: true` and its `reason`). `--repo` / `--project` are not needed for `--report`. The report does not tell a `T-pr-review-*` thread
whose PR is merged or closed apart from others: neither the store nor the tick has PR state.

Known limit: the chatroom offers no way to edit a message (no edit tool on the MCP surface, and no
edit field on a message, checked 2026-11-09). If one is added and an edit does not move
`last_activity_at`, the tick will not see the edit. To correct a head, post a new message.

To stop it, disable the `mindwire-stall-ledger` task. The store survives a stop; quarantined
ledger records are listed and cleared with `scripts/stall_ledger_tick.py --list-quarantined` /
`--clear-quarantined`.

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

Since 2026-10-01 (1) applies only to a failure that happens **twice in a row**. The first failure is
retried once automatically; see *Retry once before quarantine* below.

The old sweep-break fail-safe is retained but re-aimed: only a **failure to write the declaration**
(and a "conductor stopped: … rounds=…" line that never arrived) breaks the sweep. If we cannot
even record what went wrong, we still cannot quietly move on.

**K-budget (2 per sweep).** Two failures in one tick suggest a shared cause; a third would spend
another inference on that same cause before stopping. At K=2 the sweep breaks and a "systemic
cause suspected" notification fires. Remaining candidates count as `not-reached` on the starvation
metric — the honesty rule below. K counts **failures**: a first failure (now retried, not
quarantined) counts the same as a failed retry. A K hit does **not** promote pending retries to
quarantine; once a systemic wave clears, the next tick's retries recover them.

**Retry once before quarantine** (T-retry-once-before-quarantine; decided msg-5424, design msg-5434
/ msg-5441 / msg-5449). On 2026-10-01, 8 of 9 quarantined threads had failed exactly once on a
transient SDK error and waited up to a day for a manual clear. The rule now:

| Event | What the sweep does | Log prefix |
|---|---|---|
| exit ∉ {0, 2}, nothing pending | write `retry-pending.json`; **no** quarantine, **no** Discord alert | `retry-scheduled` |
| a pending thread gets a LAUNCH verdict | launch it — this is its one retry; `--retry-of <error_code>@<first_failure_at>` is added only when the head is the one the first failure saw | `retry-launch` |
| a pending thread is no longer on the sweep list | discard the pending record (not quarantined; the same prune `evaluated.json` gets) | `retry-dropped` |
| the retry exits 0 | clear the pending record | `retry-recovered` |
| the retry exits 2 | leave the pending record as is (environment fault, not the thread's) | — |
| the retry fails again | quarantine with `consecutive_failures=2`, the first failure's `first_failure_at` (the 24h escalation counts from it) and `first_attempt`; alert "再試行でも失敗したため隔離" | `retry-failed→quarantined` |

- **When.** Never inside the same sweep. A pending thread is not excluded from the head-skip decide
  batch; it is retried on the next LAUNCH verdict, so head_skip's backoff and T42's
  `launches_same_head` (STALLED at 3) count and bound the retry like any other launch. DEFER /
  SKIP carry the pending record over.
- **Count reset.** Only an exit 0 clears it, not a head change. A thread that posts and then fails
  in SDK teardown on every run moves its head each time, and would never be quarantined otherwise.
- **Same-head re-fire.** The conductor attaches a fixed "check the working tree / branch / existing
  PR before acting; do not repeat a push, PR or comment" notice to the retry's first prompt. It does
  this only for roles in `RETRY_NOTICE_ROLES` (`src/spirrow_mindwire/conductor/retry_notice.py`;
  today, the implementer only). Every other role's prompt is unchanged.
  The sweep passes `--retry-of` only when the head being launched equals the head recorded at the
  first failure; on a moved head the notice would claim a failed attempt at a message nobody has
  attempted yet, so it is left out (the launch still counts as the retry).
- **Digest.** The summary line carries `再試行 N / 回復 M / 再試行後隔離 Q` since the last full
  digest. A `再試行待ち [retry-pending]` section lists pending threads, and those pending ≥24h are
  marked `24h+` and listed first. A pending record is never timed out into quarantine: a long
  exit-2 outage is the environment's fault and already has its own alert.
- **Q3 scope.** spec/msg-814 Q3 ("解除は人手のみ") now applies to quarantined threads only. The
  automatic retry of a first failure is the one exception, and `Clear-Quarantine` is unchanged.

**Escalation ladder.** A quarantine record's derived state is a function of its age, not any
scheduling flag (it stays skipped either way):

| Age | State | Digest wording |
|---|---|---|
| 0–24h | `quarantined` | plain listing |
| 24h–7d | `escalated` | broken out at the top of the digest; state-transition alert fires once |
| ≥7d | `stale` | "fix it or fold the thread"; state-transition alert fires once |

Only a human clear (`Clear-Quarantine`) ever transitions a quarantined thread out (Q3 — the
one-retry rule above acts before quarantine, never after). There is deliberately no
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
frequency is the signal, `-Reason` is the payload). Clearing does not touch `state/head_skip.json`:
the new predicate times a cleared thread by backoff (capped at 60 min), not head equality, so the
"silent re-skip" failure the old `heads.json` drop guarded against is no longer reachable.

**Running Clear-Quarantine while the sweep is running is safe.** A single tick reads its state
files once at start and holds them in memory for the whole run (measured minutes on a real
AI-driven candidate). Without care, that stale in-memory map would silently overwrite the
operator's disk write at end-of-tick and resurrect the entry — the sort of failure the whole
quarantine story exists to end. So the sweep uses **merge-on-write on `quarantine.json`**: at flush
time it re-reads the file from disk, and any key the operator removed during the tick stays
removed. New adds from the sweep still land, age-driven state transitions still land; only
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
| `self_handoff_to_human` | yes — the head hands to its own author; nobody was spawned |
| `round_cap` | yes — runaway backstop tripped |
| `empty_thread` | yes — almost always a typo in the priority list |
| `none` | **no** — a settled thread is the normal end; the sweep just moves on |
| `hold` | **no** — the operator asked for the stop |
| `ci_wait` | **no** — pre-gate CI wait; bounded by the admission caps |

Do not narrow this to `reason -eq 'human'`. The first live sweep after this was wired stopped with
`no_progress_to_human`, which a narrower check drops silently.

### Two of these stops PARK the thread (2026-09-14, design §6.2)

`no_progress_to_human` and `self_handoff_to_human` are not "try again in a while" — they are
"running this exact head again achieves exactly the same nothing". After such a run the sweep
calls `head_skip_decide.py --mode commit-terminal`, which records the reason and the head it
stopped on; `head_skip.decide`'s Stage 1b then **SKIP**s the thread until its head msg id
changes. Any other stop reason clears that state, so the call is unconditional.

Before this, those threads fell to Stage 2's backoff, which is a floor on the launch *rate* and
by design never terminates: two threads sat at one launch per hour for days
(`spirrow-magickit/T-human-outage-degrade-close-only` and
`spirrow-mindwire/T-scoped-driver-verdict-never-reaches-chatroom`, 72 retries each, no reply and
no record on any of them).

**A parked thread does not un-park itself.** The head has to move — a person posts, or edits the
head's `NEXT:`. `launch_attempts` is preserved through the park, so the log still shows how long
the spin ran before it was stopped.

### Spawn-unavailable targets stop at the human (ADR-2026-09-14-21)

A `NEXT:` naming an identity whose 稼働形態 is not `terminal_coding_agent` — `Fermi`
(`web_ai_chat`) is the shipped case — is not spawned. The conductor stops on `human`, the same
stop `NEXT: human` produces, and posts the reason into the thread. The table lives in
`[conductor.identity_embodiment]`; the ADR's own entry ships as a default, so an empty block is
the ADR's behaviour rather than "spawn anything named".

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
Test-NetConnection {{IP_SERVICES}} -Port 8117   # magickit MCP
Test-NetConnection {{IP_SERVICES}} -Port 8110   # Lexora
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
  - **G1 latch and recording an out-of-chat decision** (T-guard-i-latches-on-discharged-tierc-line).
    A `TIER-C: <label>` line anywhere since the human's last post keeps the proceed closed (G1),
    and before this change only a human post lifted it. A decision the human gives outside the
    chatroom (relayed by the operator or Fermi) now lifts it only if the recorder writes, **in the
    same post that records the decision**, one line per declaration it discharges:

    ```
    TIER-C-RESOLVED: msg-754 decided-by=<[conductor].human_identity> on=2026-10-21
    ```

    The line must match the whole grammar (one msg id per line, `decided-by` = the human identity,
    a real date), and its author must be listed in `[conductor].tier_c_recorders` (default empty =
    no discharge path). **A recorder counts only in a thread where it holds none of proposer /
    implementer / naysayer in that thread's `[conductor].roster`**; otherwise its line is ignored
    and reported as `recorder-holds-loop-role` (the daemon still starts, and logs a WARNING listing
    the overlap). It discharges only the
    declaration it names, and only if it was posted after that declaration; a later `TIER-C:` line
    closes G1 again. When guard (i) does redirect, its relay now names every closed condition with
    its msg ids (G1 lists every open declaration, plus any `unmatched` / malformed / non-recorder
    resolution lines), and the conductor log carries a `guard_i_redirect ... closed_by=...` INFO line.
    `scripts/guard_i_latch_census.py` lists the threads that are latched now.

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
