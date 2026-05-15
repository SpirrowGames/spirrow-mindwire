# MindWire dogfooding operator runbook

Operator-facing runbook for running the Phase 1 dogfooding loop. Design
rationale lives in `docs/feature-3-design.md`; this file is the
*procedure*.

Established by Feature 3-A sub-PR 3 (chatroom
`T-feat3-d3-single-writer-crack` msg-131 §3/§4, C1 + C2). Keep this in
sync with the watcher / `mindwire-mcp-server` config defaults.

## 1. Service startup order (dependency direction)

1. **phanthand** — file-access layer the watcher depends on.
   - `cd <spirrow-phanthand repo>` → `uv run phanthand`
   - default listen `http://127.0.0.1:7300`, config `config.yaml`
   - health: `curl http://127.0.0.1:7300/health` → `{"success":true,...}`
2. **MindWire watcher** — dispatch side.
   - `cd <spirrow-mindwire repo>`
   - `PHANTHAND_API_KEY=<value-from-phanthand-config>` set
   - `uv run mindwire-watcher`
   - `ANTHROPIC_API_KEY` not required (bundled claude CLI OAuth carries over)
3. **mindwire-mcp-server** — claude.ai-side write entry point
   (Feature 3-A sub-PR 2). Single-writer crack (sub-PR 3) starts once
   claude.ai writes through this server.
   - `MINDWIRE_MCP_API_KEY=<secret>` set (name fixed by
     `MCPServerConfig.api_key_env`; the value lives only in the env)
   - `uv run mindwire-mcp-server` (foreground; Ctrl-C to stop)
   - default bind `http://127.0.0.1:7400/mcp`

## 2. Phase 1 dogfooding resume pre-checklist (run once at sub-PR 3 completion)

This checklist is a **phase-switch gate**, not a periodic task — it is
run exactly once when MVP (sub-PR 1-3) completes and Phase 1 dogfooding
resumes (msg-131 §3, the deliberate anti-rot framing: a single clear
trigger event, not "manual/periodic"). Do not skip it before resuming.

1. **Schema migration dry-run** — verify existing threads are v2-clean:

   ```
   uv run mindwire-migrate-v1-to-v2 --dry-run
   ```

   Run without `--dry-run` if any thread is still v1 (idempotent;
   safe to re-run).

2. **`mindwire-mcp-server` standalone smoke** — see §3 Step 1.

3. **Cross-process integration test — manual e2e** (D2-6 invariant true
   verify, true process separation):

   ```
   uv run pytest -m manual
   ```

   CI skips this by default (`addopts -m "not manual"`); this gate is
   the only place it is expected to run. It launches a real
   `mindwire-mcp-server` subprocess and smoke-checks health + auth.

## 3. Triage flow — "claude.ai isn't working"

Isolate **server bug** vs **connector misconfig** by checking in this
order (msg-131 §4, C2). The server can be smoked with repo primitives
alone — no extra harness.

### Step 1: server-only health (repo primitives, no claude.ai involved)

```
cd <spirrow-mindwire repo>
export MINDWIRE_MCP_API_KEY="<your-key>"
uv run mindwire-mcp-server &        # standalone start

# auth smoke — no token must be rejected:
curl -s -o /dev/null -w "%{http_code}" \
     -X POST http://127.0.0.1:7400/mcp
# expect: 401

# wrong token must still be rejected:
curl -s -o /dev/null -w "%{http_code}" \
     -X POST -H "Authorization: Bearer wrong" \
     http://127.0.0.1:7400/mcp
# expect: 401

# valid token must NOT be 401 (MCP-level response instead):
curl -s -o /dev/null -w "%{http_code}" \
     -X POST -H "Authorization: Bearer $MINDWIRE_MCP_API_KEY" \
     http://127.0.0.1:7400/mcp
# expect: not 401
```

- **Any expectation fails → server-side bug.** Fix the server before
  touching the claude.ai connector; the two debug tracks must not be
  mixed.
- All pass → server is healthy; proceed to Step 2.

### Step 2: claude.ai connector configuration (only after Step 1 passes)

1. In claude.ai web, configure the MCP connector with the server
   address (`http://<host>:7400/mcp`) and the API key.
2. Issue a claude.ai-side test invoke against a dedicated test thread.
3. Step 2 failure with Step 1 passing ⟹ connector / auth-config issue,
   not a server bug.

Triage order is always **server → connector**.

## 4. Observation paths

- event log: `<data_dir>/logs/threads/<ULID>.jsonl`
- replies: `<data_dir>/threads/<ULID>/messages/NNN-from-{cai|cc}.md`
- meta: `<data_dir>/threads/<ULID>/meta.yaml`
- **race-gap metric** (Feature 3-A sub-PR 3, D3-1): grep the watcher
  log for `startup_scan race-gap summary:` — each watcher startup emits
  one line (`scanned=.. gap_detected=.. gap_rate=.. anomalies=..`).
  Summing `gap_detected`/`scanned` across startups is the quantitative
  input for the sub-PR 4 (2-phase-commit re-design) trigger. The
  race-rate → trigger threshold itself is intentionally **not** fixed
  here — it is decided in the sub-PR 4 propose against real
  observation (msg-131 §5). Known blind spot: a same-filename silent
  overwrite leaves no structural trace; a dogfooding report of "meta
  consistent but message content unexpected" is the signal to revisit
  runtime conflict detection (see `docs/feature-3-design.md` §2.4).
