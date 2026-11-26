# Clone slots — turning them on and off (T-ephemeral-impl-clone-slot-per-thread, A6)

`[loop].slots_root` switches the implementer's working tree from one shared clone per repo
(`repo_dir`) to a pool of per-turn clone slots. The code is merged (A1–A5); this page is only the
operating procedure. It changes no code.

**Who decides.** Turning slots on or off on the production loop host is a production change.
ADR-2026-10-22-24 limits production deploys to Takahito's instruction (as quoted in the thread,
msg-10016; the ADR body is not in this repository). So nobody, agent or person, runs the steps below
on the production host without that instruction, recorded in the thread. Being *able* to edit the
config file is not that instruction.

## What the switch does (read from the code)

- **Where it is read.** `deploy/lib/CloneSlot.ps1` `Get-SlotModeConfig` reads `slots_root` and
  `slots_per_repo` from the `[loop]` section of `<data_dir>/config/mindwire.toml`, once per tick. The
  Python config (`src/spirrow_mindwire/config.py`, `Stage3LoopConfig.slots_root`) reads the same keys.
  The sweep logs `slot mode: slots_root=… slots_per_repo=…` on every tick that runs in slot mode.
- **It applies to every repo in the sweep at once.** There is no per-repo switch: every role-lane
  candidate in `sweep.json` borrows a slot named after the leaf of its `repo_dir`. Gate-lane launches
  borrow nothing. `repo_dir` stays in use for the repo name and the `origin` URL, so do not remove it.
  See *Gaps* below about starting with one repo only.
- **Layout.** Slots `<slots_root>/<repo>/slot-NN/` (`NN` = `01` .. `slots_per_repo`, default 1), the
  bare mirror `<slots_root>/<repo>.git`, quarantine `<slots_root>/_quarantine/<repo>/`.
- **State.** Leases are in `<data_dir>/state/slot-leases.json`. Quarantine rows, with their export
  paths, are in `<data_dir>/state/clone-retreats.jsonl`. Each tick also runs `quarantine-gc`.
- **Trust.** `slot-prep` checks, read-only, that the slot's own path is trusted in Claude Code's
  `.claude.json` (`src/spirrow_mindwire/clone_slot_trust.py`; trust is not inherited from a parent
  directory). An untrusted slot parks that thread with `workspace-untrusted` and returns the lease.
  MindWire never writes Claude Code's config. A person marks each slot path trusted.
- **Secret guard and remotes (A8).** On every borrow, `slot-prep` copies the shared clone's
  effective `core.hooksPath` (`git -C <repo_dir> config --get --path core.hooksPath`, read by the
  sweep) into the slot's local config, so preflight P3 finds the guard. An empty, relative, inside-
  `slots_root` or `pre-commit`-less value parks that thread with `provision_failed` before anything
  is cloned. The slot keeps no `mirror` remote, because preflight P2 refuses its local URL: the
  borrow fetches from the mirror's path, and a slot made before A8 loses the remote on its next
  borrow. The only remote left is `origin`.

## The stop window (used by both directions)

Both switching on and switching back run inside the same stop window. No new drain mechanism
exists or is added: the stop is the Task Scheduler task, as in `docs/deploy.md`
→ *In-flight ledger and the hold acknowledgement* → *Clearing*. Switching is therefore only done in
a maintenance window in which this stop holds.

1. **Stop new launches.** Disable the task that runs `deploy/run-conductor-scheduled.ps1`, then wait
   until no wrapper is left. Disabling stops new ticks, not the one already running:

   ```powershell
   $t = Get-ScheduledTask | Where-Object { "$($_.Actions.Arguments)" -match 'run-conductor-scheduled' }
   $t | Disable-ScheduledTask | Out-Null
   while (Get-CimInstance Win32_Process | Where-Object { "$($_.CommandLine)" -match 'run-conductor-scheduled' }) { Start-Sleep 10 }
   ```

   The per-project HOLD (`loop_control_set`, read by the sweep and by the conductor) does not stop a
   turn that is already running and is not the stop here.
2. **Check that no turn is still running.** On the loop host, no `mindwire-loop` or `claude` process
   of the conductor may be left, for any project. If one is, stop: re-enable the task and try
   another window. This includes a turn that ran past a timeout or was interrupted: do not go on
   until it is gone.
3. **Check the leases.** Every slot in `state/slot-leases.json` must be free. A held slot has a
   `state` field (`launching`, `running` or `quarantine_failed`); a free one has only
   `last_thread_id`.

   Run the check below in a shell that resolves the data directory the way the scheduled task does
   (`deploy/run-conductor-scheduled.ps1`: `MINDWIRE_PATHS__DATA_DIR`, else `~/spirrow-mindwire-data`).
   If the task sets that variable for itself and your shell does not, set
   `$env:MINDWIRE_PATHS__DATA_DIR` to the task's value first. Any error stops the procedure.

   ```powershell
   $ErrorActionPreference = 'Stop'
   $d = if ($env:MINDWIRE_PATHS__DATA_DIR) { $env:MINDWIRE_PATHS__DATA_DIR } else { Join-Path $HOME 'spirrow-mindwire-data' }
   $d = (Resolve-Path -LiteralPath $d).Path        # throws if the directory does not exist
   if (-not (Test-Path -LiteralPath (Join-Path $d 'config\mindwire.toml') -PathType Leaf)) {
       throw "no config\mindwire.toml under ${d}: not the loop's data directory"
   }
   $p = Join-Path $d 'state\slot-leases.json'
   "lease file: $p"
   $held = 0
   if (Test-Path -LiteralPath $p) {
       $o = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json   # throws on unreadable JSON
       if ($o.version -ne 1 -or $null -eq $o.repos) { throw "unexpected lease file shape in $p" }
       foreach ($r in $o.repos.PSObject.Properties) {
           foreach ($s in $r.Value.slots.PSObject.Properties) {
               if ($s.Value.PSObject.Properties.Name -contains 'state') {
                   $held++
                   "HELD $($r.Name)/$($s.Name): $($s.Value.state) thread=$($s.Value.thread_id)"
               }
           }
       }
   } else { 'no lease file (slot mode has not run with this data directory)' }
   if ($held -eq 0) { 'OK' } else { throw "$held held lease(s), listed above" }
   ```

   Go on only when it ends with `OK`. An error, or any `HELD` line, means stop here. When switching
   back, `no lease file` is expected only if slot mode never ran; if it did run, the path is wrong:
   stop and find the data directory the task uses. A `launching` / `running` lease with no process
   left (step 2) is one whose turn end never ran. Return it with
   `mindwire slot-recover --slots-root R --repo r --slot slot-NN --project p --state-dir S --confirm-no-process`
   (see `src/spirrow_mindwire/clone_slot_cli.py`). A `quarantine_failed` lease needs a person:
   leave it held, stop, and report it in the thread. Never edit `slot-leases.json` by hand.
4. **Change the config, then re-enable the task** (`$t | Enable-ScheduledTask`), and check the first
   turn as described under the direction below.

## Switching on

Before the window:

- **Read the effective config.** Read the `[loop]` section of the production
  `<data_dir>/config/mindwire.toml`, plus any `MINDWIRE_LOOP__*` environment variable, and record
  in the thread what `repo_dir`, `slots_root` and `slots_per_repo` actually are. Until that has been
  read, nobody knows whether production is on the shared clone.
- **Choose `slots_root`** on the same volume as the shared clones, outside every repo.
- **List the slot paths** that will need trust: `<slots_root>/<repo>/slot-01` …
  `slot-<slots_per_repo>` for every repo that appears in `sweep.json`.
- **Check every shared clone is armed** (A8): for each `repo_dir` in `sweep.json`,
  `git -C <repo_dir> config --get --path core.hooksPath` prints the spirrow-docs `githooks`
  directory. A slot is armed with exactly this value, so a shared clone without it parks its threads
  with `provision_failed`. Arm it with the secret guard's `install_githooks.sh` (naming the clone,
  not `--all`) before the window; do not set a hooks path on a slot by hand.
- **Make sure the daemon checkout has A8.** Without it, a slot is never armed and every role turn
  is refused with `check=P3` (msg-10086), and after that `check=P2`.

In the window: steps 1–3 above, then add to `[loop]`:

```toml
slots_root = "C:/workspace/slots"
# slots_per_repo = 1
```

then step 4.

**Trust.** The first role turn of each repo creates the mirror and the slot, and then parks that
thread with `workspace-untrusted` unless its path is already trusted. When that happens: run
`claude` once interactively in that slot directory, accept the trust dialog, and clear that thread's
quarantine with `deploy/Clear-Quarantine.ps1` (`docs/deploy.md` → *Clearing a quarantine*). Whether
a path can be trusted before the slot directory exists was not measured; do not rely on it.

**Check after switching on.**

- The tick log (`<data_dir>/logs/conductor-YYYY-MM-DD.log`) has `slot mode: slots_root=…`, and the
  first role turn has `slot-borrowed <key>: <slots_root>\<repo>\slot-01 …` followed by
  `slot-finish <key>: <slots_root>\<repo>\slot-01 outcome=…`.
- `git -C <slot> status --porcelain` is empty after that turn, and `slot-leases.json` shows the slot
  free again (step 3 ends with `OK` and prints no `HELD` lines).

## Switching back

In the window: steps 1–3, then remove (or comment out) `slots_root` in `[loop]`, then step 4.

- Leave `<slots_root>` as it is: the slots, the mirrors, `_quarantine/` and `slot-leases.json`. They
  are not read once `slots_root` is unset, and the quarantine exports in `state/` refer to them.
- Before step 4, check that each repo's shared clone (`repo_dir`) is clean and on its default branch
  (`git -C <repo_dir> status --porcelain` empty, `git -C <repo_dir> rev-parse --abbrev-ref HEAD`
  prints the default branch). It has not been used while slots were on, so it may be behind; the
  sweep's own sync brings it up to date.
- **Check after switching back:** the tick log no longer has `slot mode:`, and the first role turn
  runs in `repo_dir` (no `slot-borrowed` line).
- A slot lease that is still held at switch-back stays in `slot-leases.json`. It does not block the
  shared clone, but resolve it (step 3) before switching on again.

## First day: what to read

Read these for the first day after switching on, and report them in the thread before slots are
kept on or widened:

1. **Quarantine reasons (D-7).** Rows of `state/clone-retreats.jsonl` with `"kind": "slot_quarantine"`,
   counted by `quarantine_reason`.
2. **Whole launch time against the 120 s launch budget.** The source is the sweep's per-tick line
   (`docs/deploy.md` → *Launch fairness*):

   ```text
   tick elapsed=…s decide=…s loop=…s budget=gate 60/launch 120s (…)
   ```

   plus any `WARN tick exceeded launch budget`. The budget bounds when a launch may *start*, so the
   question is whether the role turn after `slot-borrowed` still starts before `tickElapsed` reaches
   the budget (no `time-budget` stop that the shared clone would not have had).
3. **Provisioning seconds.** `clone_seconds` and `slot_bytes` are written to `clone-retreats.jsonl`
   only on a quarantine-and-rebuild row. A slot's *first* provisioning prints them in the
   `MINDWIRE_SLOT_PREP` json, which the sweep does not log. For the first provisioning, read the
   time between the `slot mode:` line and the `slot-borrowed` line of that tick (that span also
   includes `quarantine-gc`). If no provisioning happened in the day (the slot already existed),
   say "provisioning not measured". Do not delete a slot to force one.

If a number is missing, report it as not measured, and do not widen anything on it.

## Gaps (not solved by this page)

- **No per-repo rollout.** `slots_root` applies to every repo in the sweep. "Start with mindwire
  only" cannot be done with configuration alone; it needs either a code change (a per-repo
  selection) or switching on for all repos at once.
- **First provisioning is not recorded durably** (see *First day*, point 3).
