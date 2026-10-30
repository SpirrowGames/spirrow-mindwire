# Operator lane — runbook

The operator lane takes one `NEXT: operator` head at a time and runs it in a fresh, isolated Claude Code
session. It posts the result back into the thread as an `operator` report. The design is the one agreed on
chatroom thread `spirrow-mindwire/T-operator-task-lane` (msg-7408 through msg-7438, summarised for
implementation in msg-7460). Code: `src/spirrow_mindwire/operator_lane/`, `deploy/lib/OperatorLane.ps1`.

The lane is **off** until the manual setup below is finished and `enabled` is set to `true`.

## What the lane does

- **Who starts it.** The `MindWire-Conductor` scheduled task's tick runs an *operator pass* right after its
  head probe. There is no separate scheduled task. The pass dispatches at most one task per tick and never
  touches a held project.
- **What it takes.** The pass takes a head only when all of the following hold:
  - the project is in `projects` (default: `spirrow-mindwire`);
  - the head-skip sweep saw `NEXT: operator` on that exact head;
  - the body is the 3-line form (`OPERATOR-TASK:` / `TIER-C-CHECK: none` / `NEXT: operator`);
  - the body has no `OPERATOR-LANE:` line;
  - the head was not already dispatched.
- **One task, one session.** The pass starts a detached runner (`python -m spirrow_mindwire.operator_lane
  run-session`). Before anything else, the runner re-checks the guard and the head. It then starts
  `claude -p` as the `mw-operator` user, with no MCP servers, inside a Job Object (`KILL_ON_JOB_CLOSE`),
  with a 60-minute limit. The session gets the task and the thread in its prompt, and its only output is
  the report body.
- **The report.** The runner adds a header and the run marker
  `<!-- operator-lane run_id=… kind=report -->`, then checks the report with `validate_report`. It writes
  the report atomically to `state\operator_reports\<run_id>.md`, checks it again, and posts it as
  `operator`. The report ends with `NEXT: <requester>`, `NEXT: human` (Tier-C) or the operator 3-line
  form with `OPERATOR-LANE: needs-hands …`.
- **Failures.** A runner that never started a session is retried: 2 attempts in total. A session that
  started and then failed is **quarantined**, never retried: the lane posts an `OPERATOR-LANE: needs-hands
  quarantined` report that hands the task to a person.

### What is a guarantee and what is not

| Risk | How it is stopped | Strength |
|---|---|---|
| The session reaches magickit (deploy, chatroom) | No MCP servers (`--strict-mcp-config`, `deploy/operator-lane/session-mcp.json`). The session runs as `mw-operator`. Firewall rule `MindWire-Operator-Block-Magickit` blocks that user, and is audited before every launch. | **OS-enforced**, checked every launch |
| A direct push or merge to `main` | GitHub branch rules require a pull request. Checked before every launch via `rules/branches/main`. `mw-operator`'s token has no admin or merge rights. | **Server-enforced**, checked every launch |
| Anything else (deleting data, a forced push to a feature branch, …) | Deny rules in `deploy/operator-lane/session-settings.json`, the keyword pre-screen, and a post-run transcript audit (`OPERATOR-LANE: audit-hit`) | **Detected after the fact only.** Not a guarantee. |

The isolation check is a static audit of the firewall rule. It never tries to connect to magickit, so
magickit being down neither opens the guard nor stops the lane. If the audit finds anything missing,
the lane turns itself off (`enabled: false` plus `disabled_reason`) and sends a Discord notification.

## One-time manual setup (Takahito)

Run these in an elevated PowerShell on tomtebo-01, except where a step says otherwise. Nothing here costs
money. The session uses the same Claude subscription as the conductor's personas. **If step 4 turns out to
need API-key billing, stop: that is a Tier-C `cost` decision.**

1. **Create the user.** Use a long random password and keep it only in step 2. Do not add the user to
   Administrators.

   ```powershell
   $pw = Read-Host -AsSecureString "mw-operator password"
   New-LocalUser -Name mw-operator -Password $pw -PasswordNeverExpires -UserMayNotChangePassword
   ```

2. **Store the password for the runner.** Do this as the account that runs `MindWire-Conductor`, not
   elevated. The value must equal `credential_target` in the config.

   ```powershell
   cmdkey /generic:MindWire-Operator-Lane/mw-operator /user:mw-operator /pass:<password>
   ```

3. **Block magickit for that user.** Replace the placeholders with every address magickit answers on, v4
   and v6 (values: [[platform:infra-registry]]). They must also be listed in `magickit_addresses` (step 6).

   ```powershell
   $sid = ([System.Security.Principal.NTAccount]'mw-operator').Translate([System.Security.Principal.SecurityIdentifier]).Value
   New-NetFirewallRule -Name MindWire-Operator-Block-Magickit -DisplayName MindWire-Operator-Block-Magickit `
     -Direction Outbound -Action Block -Profile Any -LocalUser "D:(A;;CC;;;$sid)" `
     -RemoteAddress {{IP_SERVICES}}, <magickit IPv6>
   ```

4. **Give `mw-operator` a Claude Code it can run.** Install it for that user, or put it in a directory that
   user can read. Set `claude_path` to the absolute path. Then log in once interactively, as
   `mw-operator`, with the same subscription account (`runas /user:mw-operator powershell`, then
   `claude`, then `/login`).

5. **Give `mw-operator` GitHub access without merge rights.** Create a fine-grained token limited to the
   repositories in `github_repos`, with *Contents: read/write* and *Pull requests: read/write* only (no
   Administration). Then, as `mw-operator`, run `gh auth login --with-token`. Also create the session's
   working clone, owned by `mw-operator`, and set `session_workdir` to it.

6. **Write the config** at `<data_dir>\config\operator_lane.json` (default data dir
   `%USERPROFILE%\spirrow-mindwire-data`). Leave `enabled` false for now.

   ```json
   {
     "enabled": false,
     "projects": ["spirrow-mindwire"],
     "operator_author": "operator",
     "claude_path": "C:\\Program Files\\ClaudeCode\\claude.exe",
     "run_as_user": "mw-operator",
     "credential_target": "MindWire-Operator-Lane/mw-operator",
     "session_workdir": "C:\\Users\\mw-operator\\work\\spirrow-mindwire",
     "firewall_rule_name": "MindWire-Operator-Block-Magickit",
     "magickit_addresses": ["{{IP_SERVICES}}", "<magickit IPv6>"],
     "github_repos": ["SpirrowGames/spirrow-mindwire"],
     "session_timeout_sec": 3600,
     "max_spawn_attempts": 2
   }
   ```

7. **Acceptance check.** Record the results in the thread.
   - Run the lane's own audit. It must print `"status": "ok"`.

     ```powershell
     uv run python -m spirrow_mindwire.operator_lane guard
     ```

   - As `mw-operator`, try to reach magickit. It must fail. **Record which error you get:** a refused
     connection, a timeout, or `WSAEACCES` / 10013. Then, as your own account, confirm that the same
     connection succeeds, so the failure is the rule and not a magickit outage.

     ```powershell
     runas /user:mw-operator "powershell -NoExit -Command Test-NetConnection {{IP_SERVICES}} -Port 8110"
     Test-NetConnection {{IP_SERVICES}} -Port 8110
     ```

   - The session's environment is built from `mw-operator`'s own logon (`LogonUserW` +
     `CreateEnvironmentBlock`, inheriting nothing), and the runner refuses to start it if any
     `MINDWIRE_*` name or value appears. A refusal shows in the run's report as `session_failed` with
     `session environment carries runner settings: <names>`. `LogonUserW` needs the same
     "Allow log on locally" right that `runas` uses above. If it fails, record the error code.

8. **Turn it on:** set `"enabled": true`. The first enabled tick creates `state\operator_lane.json`.

## The `/mindwire-operator` skill (hand-driven sessions)

The skill lives outside this repository (`~/.claude/skills/mindwire-operator/SKILL.md`), so this PR does
not change it. Add these steps by hand so that a hand-driven session and the lane never work at the same
time:

- **Before the first OPERATOR-TASK:**
  `uv run python -m spirrow_mindwire.operator_lane lock acquire --holder manual [--project P --thread T --head M]`.
  Keep the printed `lease_id`. If the command says a lane run is active, stop and try later.
- **Before each further OPERATOR-TASK:**
  `uv run python -m spirrow_mindwire.operator_lane lock renew --lease-id <id>`. Use `--ttl <minutes>` before
  a step that may take more than an hour. **If `renew` fails, stop at once:** the lease expired and was
  reclaimed, and the lane may already be running.
- **At the end:**
  `uv run python -m spirrow_mindwire.operator_lane lock release --lease-id <id>`.

A lease lasts 60 minutes by default. When it expires, the next conductor tick reclaims it and sends a
notification.

## Operations

- **Files.** Everything is under `<data_dir>`:
  - `state\operator_lane.lock`: who is working.
  - `state\operator_lane.json`: per-thread `heads`.
  - `state\operator_lane.mutex`: never delete this file.
  - `state\operator_reports\`: pending reports. Finished ones move to `done\` and are kept for 7 days.
  - `logs\operator-lane\`: per run, `<run_id>.runner.log`, `.prompt.md`, `.session.jsonl` and
    `.session.err`.
  - `state\operator_lane.lock.stale-*`: copies of reclaimed locks, kept for 7 days.
- **"状態ファイルが読めません".** `operator_lane.json` is missing or corrupt, so dispatch is stopped. A
  dispatched head would be dispatched again after a reset, so first check every `NEXT: operator` head in
  the allow-listed projects. If a head already has a lane report after it, it is done. Then run
  `uv run python -m spirrow_mindwire.operator_lane reset`.
- **"lock が壊れています" or a stuck lock.** First make sure no `run-session` or `claude` process of the
  lane is running. Then run `uv run python -m spirrow_mindwire.operator_lane lock release --force`. A copy
  of the lock is kept.
- **"隔離ガードが開いています".** Fix what the message names. Then run `guard` until it prints `ok`, and set
  `enabled` back to `true` by hand.
- **A quarantine.** Read the facts in the quarantine report and the run's logs, and take the task by hand.
  The head carries `OPERATOR-LANE:`, so the lane will not take it again.
