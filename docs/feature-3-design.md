# Feature 3-A 設計 (Phase 1 MCP write API foundation)

**base commit at doc creation**: `de4d040` (= Phase 0 完結後 main、 Phase1-Obs1 fix merged)
**umbrella tracker**: GitHub #41
**trilateral debate final decide**: chatroom `T-feat3-design-overview` msg-117 (resolved 2026-05-14)

> **このドキュメントは skeleton**。 sub-PR 1 (= schema migration v2 skeleton bump) 着手時に §1 overview + §3.1 schema migration の最小章のみで創設。 sub-PR 2 以降で incremental に章を追加する。 起案以前の議論経緯は本 doc に複製せず、 chatroom thread + msg-117 decide msg を一次 reference とする。

## 1. Overview

Feature 3-A = **Phase 1 MCP write API foundation + claude.ai 側 write 開放**。 Phase 0 完結後 dogfooding で観察した「multi-turn 2nd turn 以降の friction」 (= claude.ai 側 awaiting_from 更新機構が不在、 operator manual workflow が現状の唯一 path) を解消する Phase 1 milestone。

### 1.1 Scope (= msg-117 §2 採用 roadmap)

| sub-PR | 内容 | scope | status |
|---|---|---|---|
| 1 | schema migration v2 (skeleton bump + migration script) | F3-A foundation | **着手中** |
| 2 | mindwire-mcp-server 設計 + 実装基盤 (別 process) | F3-A | 未着手 |
| 3 | claude.ai 側 awaiting_from / message write 実装 + race monitoring | F3-A | 未着手 |
| **★ MVP 完了 → Phase 1 dogfooding resume** | | | |
| 4 (deferred) | 2-phase commit re-design | F3-A | dogfooding race observation N+ 件 trigger 後 |
| **★ F3-A 完結** | | | |
| (out of scope) | F3-B (events.jsonl derive engine + operator dashboard / CLI) | deferred | Phase 1 後半 or Phase 2 candidate |

### 1.2 Phase 0 4 assumptions crack (= msg-117 §3)

`docs/feature-2-design.md` §6.0 の 4 assumptions のうち F3-A で crack するもの:

| sub-PR | crack |
|---|---|
| 2 | **in-process MindWire MCP** crack (= 別 process MCP server 立上) |
| 3 | **single writer** crack (= claude.ai 側 MCP 経由 write 開始) |
| 4 (deferred) | (crack ではなく) 2-phase commit semantic 再設計 |

watcher-driven invocation / single watcher process は **維持** (= Phase 1 では crack 不要)。 order は logical dependency で forced、 設計判断的自由度なし。

### 1.3 F3-B deferred (= msg-117 §5)

F3-B (events.jsonl derive engine + operator dashboard / CLI) は本 Feature 3 から外す。 revisit trigger は **以下を全て満たした時点で再評価**:

- F3-A MVP (= sub-PR 1-3) 完了
- Phase 1 dogfooding resume
- multi-turn observation N+ 件蓄積
- operator friction が `yq '.status, .awaiting_from' threads/*/meta.yaml` で扱えない complexity level に到達

→ Phase 1 後半 or Phase 2 candidate。

## 2. 設計選択

(sub-PR 2 以降で incremental 追加予定。 想定 subsection)

- **§2.1** mindwire-mcp-server 設計 (= layer / transport / auth、 sub-PR 2) — **filled**
- **§2.2** claude.ai 側 write protocol (= exposed tools `send_message` / `open_thread` / `resolve_thread`、 sub-PR 2) — **filled**
- **§2.3** claude.ai 側 single writer crack 実発生 (sub-PR 3) — **filled**
- **§2.4** race monitoring instrumentation (sub-PR 3 bundle) — **filled**
- **§2.5** 2-phase commit re-design (sub-PR 4 deferred、 dogfooding race observation N+ 件 trigger)
- **§2.6** Feature 3-C: claude.ai-participant read tools (= 独立 feature、 F3-A umbrella #41 外、 GitHub tracker #48) — **filled**

### 2.1 mindwire-mcp-server 設計 (sub-PR 2)

**Trilateral decide SOT**: chatroom `T-feat3-d2-mcp-server` msg-127 (= integrator decide、 resolved 2026-05-15、 user 最終承認済 for D2-3 = A 採用)。 本節は decide msg を文書化した記録、 design 自体の議論ログは chatroom 一次。

#### 2.1.1 3 layer 分離 (D2-3 = A 採用)

`mindwire` repo は本 sub-PR 完了時点で **3 つの独立な MCP server layer** を持つ:

| layer | entry point / file | scope | 本 sub-PR で touch |
|---|---|---|---|
| **mindwire-mcp** | `mindwire-mcp` CLI / `src/spirrow_mindwire/mcp_server.py` | read-only API stub (= `docs/mcp-interface.md` §3、 Phase 2+ Connector / observer / dashboard 用) | **touch なし** (= status quo preserve) |
| **mindwire-mcp-server** (本 sub-PR で追加) | `mindwire-mcp-server` CLI / `src/spirrow_mindwire/mcp_write_server/` | **write-only HTTP MCP API** (= claude.ai 側からの thread 操作、 別 process) | **新規 entry point + 新 subpackage** |
| **in-process mindwire MCP** | `src/spirrow_mindwire/claude_code/tools/mindwire_server.py` (= `build_mindwire_mcp_server`) | watcher が claude-code 起動時に inject する 5 tool (= `write_reply` / `read_file` / `list_dir` / `search` / `file_info`) | **touch なし** |

**A 採用の rationale** (= msg-127 §2):

| 観点 | A (= 3 layer 分離) merit | B (= 1 server 統合 rebrand) で問題化していた点 |
|---|---|---|
| YAGNI | 既存 read-only stub を driver 出現まで status quo preserve、 observation driven | read consumer (= Connector / observer) Phase 1 不在のまま「同 audience = external」 grouping、 future state anticipation 寄り |
| spec scope | `docs/mcp-interface.md` §3 (= read-only spec) touch なし、 umbrella #41 射程内、 user 再承認不要 | spec 拡張 (= read-only → read+write) + entry point rebrand、 user 再承認必須 |
| 複雑性 | read / write を別 server / 別 api_key で自然分離、 scope-based access control 不要 | 1 server / 1 api_key で read+write 混在、 Phase 2+ で scope-based access control 後付け carry |
| phanthand precedent | 同 pattern (= driver 単位で entry point 分離) | 異なる pattern (= phanthand は read 専用、 write は別 mechanism) |
| 「3 entry point overhead」 counter | 実質 0 cost (= read stub 放置)、 mental model は driver 単位の方が単純 | counter 自体が naysayer §3.3 で reject |

**Naysayer §3 独立検証 trace** (= msg-126 §3、 起案 stance 撤回 trigger): Naysayer pass が「§1 YAGNI / §2 overscope / §3 ハイブリッド複雑性」 の 3 軸で B を独立 reject、 起案者 (claude-code) が integrator step で stance 撤回。 user judgment で A 採用 confirm、 procedural integrity 維持。

#### 2.1.2 Transport / auth / startup (D2-2 + D2-5)

| 項目 | 採用 |
|---|---|
| Transport | **streamable HTTP MCP** (= FastMCP の `streamable_http_app()` を Starlette ASGI として host、 uvicorn で foreground 起動) |
| Auth | **API key bearer token** (= phanthand precedent と同 pattern)、 `Authorization: Bearer <token>` を `ApiKeyMiddleware` で constant-time 検証 (`hmac.compare_digest`) |
| Lifecycle | **operator manual** (= `uv run mindwire-mcp-server`、 Ctrl-C で停止) |
| Config | `[mcp_server]` section in `mindwire.toml`、 fields = `host` (default `127.0.0.1`) / `port` (default `7400`) / `api_key_env` (default `MINDWIRE_MCP_API_KEY`)。 secret 値は env var に置く (= TOML には name のみ) |
| URL path | `/mcp` (= `streamable_http_path`、 module-level `MCP_PATH` constant) |
| Server name | `mindwire-participant` (= MCP handshake で advertise、 in-process `mindwire` server / read stub `mindwire-mcp` と区別)。 **sub-PR 2 では `mindwire-write`、 Feature 3-C (§2.6) で `mindwire-participant` に改名** (audience 軸 reframe、 API key env / port は不変) |

`127.0.0.1` default は phanthand precedent + 「write tool 露出 surface は localhost 既定が安全」 の二軸根拠。 operator が別 host から接続する場合は `[mcp_server].host` 明示 override。

#### 2.1.3 Cross-process invariant (D2-6)

**Invariant**: `mindwire-mcp-server` と watcher dispatcher は **on-disk thread directory 経由でのみ coordinate する** (= shared memory なし、 socket-level RPC なし)。 両 process は同じ race-acceptance contract (= `docs/feature-2-design.md` §3.6 「operator should stop watcher before destructive manual edits」) を共有する。

実装上の trace:

- `src/spirrow_mindwire/mcp_write_server/http.py` module docstring: cross-process invariant を verbatim 記載
- `src/spirrow_mindwire/mcp_write_server/tools_write.py` module docstring: 「watcher と MCP-server racing on same thread within a few ms can produce both processes computing the same next_seq」 と明示、 sub-PR 4 が 2-phase commit re-design 担当である旨も記載
- `src/spirrow_mindwire/awaiting_from_toggle.py` module docstring: cross-process race scope (= AwaitingFromChanged event が 2 件 emit され得る) を独立 record

**Test scope split** (= msg-127 §4 C3 + C4):

| layer | scope | sub-PR |
|---|---|---|
| In-process concurrency (= MCP server 内で同 thread 2 send_message) | `tests/test_mcp_write_server.py::test_send_message_concurrent_writes` で per-thread asyncio.Lock 挙動 baseline | **本 sub-PR** |
| Cross-process integration (= watcher + MCP server 同 thread race) | watcher 駆動 e2e test として sub-PR 3 で実装 | sub-PR 3 carry |

#### 2.1.4 Layer architecture diff

```
新規 / 修正 (= 本 sub-PR 2):

src/spirrow_mindwire/
├── awaiting_from_toggle.py            # 新規。 dispatcher + MCP server 共有 helper (= C1)
├── config.py                          # MCPServerConfig 追加、 MindwireSettings.mcp_server field
├── mcp_server.py                      # touch なし (= stub status quo preserve)
├── mcp_write_server/                  # 新 subpackage
│   ├── __init__.py
│   ├── http.py                        # FastMCP + uvicorn entry、 ApiKeyMiddleware wrap
│   ├── auth.py                        # ApiKeyMiddleware + read_api_key
│   └── tools_write.py                 # WriteTools class (= per-thread lock dict + 3 tool handlers)
└── watcher/dispatcher.py              # 修正 (= self._toggle_awaiting_from 削除、 toggle_awaiting_from import)
```

`mindwire-mcp-server` entry point: `pyproject.toml` `[project.scripts]` に新規追加 (= 既存 `mindwire-mcp` は touch なし)。 `starlette` + `uvicorn` は MCP SDK に transitive 依存だが、 直接 import する以上 `[project.dependencies]` に明示 ([[feedback_decoupling_preference]] 整合)。

### 2.2 claude.ai 側 write protocol (sub-PR 2)

**Decide SOT**: chatroom `T-feat3-d2-mcp-server` msg-127 §1 D2-1 (= 3 tool 最小、 α 採用 + Naysayer Q4 frame inversion 受諾 = 「不追加が YAGNI 整合」)。

#### 2.2.1 Tool surface

| tool | args | return (success) | error 条件 (ToolError verbatim) |
|---|---|---|---|
| `send_message` | `thread_id: str (ULID)`、 `body: str` | `{thread_id, seq, msg_id, awaiting_from="claude-code"}` | invalid ULID / nonexistent thread / status terminal / `awaiting_from != "claude.ai"` (= 順番違反) |
| `open_thread` | `initial_message: str`、 `title?: str`、 `tags?: list[str]` | `{thread_id, msg_id, awaiting_from="claude-code"}` | (operational fault 以外 user-actionable error 不在) |
| `resolve_thread` | `thread_id: str (ULID)` | `{thread_id, status="resolved"}` (+ `noop=True` if already resolved) | invalid ULID / nonexistent thread / 遷移不可 (= e.g. `archived → resolved`) |

#### 2.2.2 4 番目 tool (`update_awaiting_from`) 不採用 rationale

Naysayer Q4 frame inversion (= msg-127 §5): 「driver 不在で reject = speculation」 frame は誤り、 正しい frame は **「driver 不在で追加するのが speculation、 不追加が YAGNI 整合」**。 観察 driver が emerge した時点で incremental に追加できる、 spec scope 拡張ではなく将来拡張余地の record。

具体 anchor: `tools_write.py` module docstring の冒頭 paragraph に「A fourth potential tool `update_awaiting_from` ... is intentionally deferred ... Future drivers can append the tool incrementally without changing this module's contract」 と verbatim 記載済。

#### 2.2.3 Turn-discipline guard for `send_message`

`send_message` は `meta.awaiting_from == "claude.ai"` を要件化する (= turn-discipline guard)。 watcher dispatcher は claude-code reply 完了時に `awaiting_from` を `claude.ai` に toggle するので、 「claude.ai's turn」 = 「dispatcher が toggle を終えた状態」。 caller が turn 違反で送ろうとした場合 (= in-flight invoke と overlap する) は ToolError verbatim。

caller (= claude.ai-side) が poll で `awaiting_from` を check する design は spec 範囲外 (= sub-PR 3 で integrate)、 本 sub-PR は MCP server side guard のみ。

#### 2.2.4 Lifecycle helper reuse (C1 採用 = msg-127 §4)

`send_message` の post-write awaiting_from toggle は **watcher dispatcher と shared helper** (= `awaiting_from_toggle.toggle_awaiting_from`) を使う。 terminal-state guard + idempotent skip + AwaitingFromChanged snapshot semantic を 2 callsite で SOT 一元化。

**#39 carry N2 disposition** (= external caller idempotency for `set_awaiting_from`): shared helper 自体が「pre_meta.awaiting_from == target なら no-op」 idempotent skip を持っているので、 external caller 専用の関数 level guard 追加は不要。 dispatcher と MCP-server caller が同じ short-circuit を共有することで、 関数 level idempotency 議論は本 sub-PR で disposition 完結。 #39 N2 trigger は closed 候補。

### 2.3 claude.ai 側 single writer crack 実発生 (sub-PR 3)

**Trilateral decide SOT**: chatroom `T-feat3-d3-single-writer-crack` msg-131 (= integrator decide、 resolved 2026-05-15、 全 D3-N converge、 D3-1 = option-a で仕様増なし → user 別途承認不要)。 本節は decide msg の文書化記録、 議論ログは chatroom 一次。

#### 2.3.1 single writer crack の位置付け

`docs/feature-2-design.md` §6.0 の 4 assumptions のうち **single writer** が本 sub-PR で crack する。 server-side write (`send_message` / `open_thread` / `resolve_thread`) は sub-PR 2 (PR #45) で実装済 = claude.ai 側が MCP 経由で thread に書き込む path は既に存在。 sub-PR 3 は **その path を実 dogfooding loop に組込み crack を実発生させる + 観察可能にする**。

| 4 assumption | sub-PR 3 後 |
|---|---|
| single writer | **crack** (= watcher dispatcher = claude-code writer に加え、 mindwire-mcp-server 経由 claude.ai writer が稼働) |
| in-process MindWire MCP | crack 済 (= sub-PR 2) |
| watcher-driven invocation | 維持 |
| single watcher process | 維持 |

#### 2.3.2 dogfooding exercise method (D3-3 = option-a 採用、 option-b smoke harness 不採用)

claude.ai web の MCP connector config は **out-of-repo** (= user が claude.ai 側で設定)。 sub-PR 3 は repo 内に simulate 用 caller harness (= option-b) を**作らない**:

- 実 claude.ai が dogfooding で server を叩く = Phase 1 「実機 reality check」 方針整合
- server 内部 (auth / 3 tool / concurrency) は sub-PR 2 `tests/test_mcp_write_server.py` で unit verify 済、 client roundtrip は §2.4 cross-process integration test で実質 cover
- 起案者 D3-3「最も不安」 flag (= server 単体 health 切り分け手段が repo にない) は **既存 primitives + recipe doc 明文化** で解消 (= 新 artifact ゼロ)：`docs/dogfooding.md` §3 に server → connector triage flow (= `curl` で 401-without / 401-bad-token / not-401-good-token の auth smoke) を明文化
- option-b smoke harness 不採用 rationale (= naysayer Q5 4 原則 + review pass 同方向): driver 不在の新 repo artifact、 「smoke 通れば OK」 で実 dogfooding skip する形骸化誘惑

review C2 + naysayer Q5 が独立に同一解 (= 既存 primitives + recipe doc 明文化、 option-b 不要) に到達した収束を採用。

#### 2.3.3 MVP 完了 milestone

sub-PR 3 完了 = **F3-A MVP (sub-PR 1-3) 完了** → 部分 deploy → **Phase 1 dogfooding resume** (= umbrella #41 採用 roadmap milestone)。 resume 前に `docs/dogfooding.md` §2 の phase-switch gate checklist (= migrate dry-run + server smoke + `uv run pytest -m manual`) を 1 回必須実行。

### 2.4 race monitoring instrumentation (sub-PR 3)

#### 2.4.1 D3-1 = option-a 採用 (minimal startup gap metric)

single writer crack 後、 watcher dispatcher (= claude-code writer) と mindwire-mcp-server (= claude.ai writer) が同 thread の `next_seq` に ms 単位で衝突し得る (= msg-127 §1 D2-6 で Phase 1 MVP 受容済の race)。 sub-PR 3 は **観察のみ** を追加 (= 解消は sub-PR 4 = 2PC re-design 担当、 sub-PR 境界厳守):

| 案 | 採否 |
|---|---|
| **option-a (minimal)** | **採用** — `startup_full_scan` に structural race-gap 検出 + per-scan structured summary log (`startup_scan race-gap summary: scanned=.. gap_detected=.. gap_rate=.. anomalies=..`) 追加。 新 event 型なし、 Event schema bump なし |
| option-b (runtime detection) | 不採用 — 検知機構自体が cross-process race の hard problem (= 2PC 設計先食い、 sub-PR 境界侵食)、 race rate 未観察で検知機構 = observation-driven 違反、 Event schema 公開仕様増 |
| option-c (hybrid) | 不採用 — option-b の問題継承 + 2 機構複雑性 |

三者 converge (propose 推奨 / review Q1 十分判定 / naysayer Q4 賛成)。

#### 2.4.2 frame 正確化 (= naysayer Q4 frame refinement 採用)

- **frame**: option-a metric は race frequency の **下限 (lower bound) estimator**、 sub-PR 4 (2PC re-design) trigger 判定に**必要十分**。 「race 全件観察」 は主目的ではない (= user 逆 frame「under-instrumentation で主目的不達」 は primary goal 取り違え、 reject)
- **trigger 判定論理**: 下限 LB が threshold を超える ⇒ 真 frequency > threshold 確実 ⇒ sub-PR 4 trigger 正当。 LB < threshold ⇒ 真値不明だが保守 bias = sub-PR 4 着手遅延寄り = observation 蓄積待ち behavior = observation-driven 核心整合
- option-b/c を本 sub-PR で導入すると「検知機構を作った以上 sub-PR 4 着手は既定路線」 という sunk-cost bias を導入、 これも option-a が回避する副次 merit (= review Q1 指摘)

#### 2.4.3 既知 blind spot の明示記録 (= naysayer Q4 (b) case)

option-a metric が catch するのは **structural trace を残す race のみ**:

| race 結末 | option-a 検知 |
|---|---|
| (a) duplicate seq (= 2 writer が同 next_seq、 異 `from` suffix → 両 file 残存) | ✅ catch |
| (a') seq hole (= lost write で番号欠番) | ✅ catch |
| (b) **silent content overwrite** (= 同 seq **かつ** 同 `from` suffix → `os.replace` later-wins、 file 1 個のみ、 structural trace なし) | ❌ **blind spot** |

(b) は隠さず本節 + `startup_scan._detect_race_gap` docstring に **明示 trade-off** として記録。 これにより:

- **option-b/c switch revisit trigger anchor**: dogfooding 中に「meta は consistent だが message content が想定と違う」 report が出た場合、 (b) silent overwrite の可能性として runtime detection (option-b/c) への switch を再評価する。 「option-a だから見落とした」 後付け批判の防止 + observation-driven escalation path の明示

#### 2.4.4 cross-process integration test (D3-2 = γ 採用 = option-b CI default + option-a manual gate)

D2-6 invariant (= filesystem 経由のみ coordinate) の verify 構成:

| layer | 採用 | 内容 |
|---|---|---|
| **option-b (CI default)** | 採用 | in-process `WriteTools` + `ThreadDispatcher` を独立 instance で並行 invoke (real `tmp_path`)、 deterministic。 `tests/test_cross_process_integration.py` の `test_filesystem_mediated_roundtrip` (= multi-turn round trip、 D2-6 core) + `test_concurrent_same_thread_no_corruption` (= accepted race が silent overwrite 超の corruption を起こさない、 scheduling-independent assertion) |
| **option-a (manual gate)** | 採用 | 実 `mindwire-mcp-server` subprocess 起動 (= 真 process 分離) + health/auth smoke。 `@pytest.mark.manual`、 CI は `addopts -m "not manual"` で skip、 Phase 1 dogfooding resume gate (`uv run pytest -m manual`) で実行 |

**α manual の運用 framing refinement** (= review C1 + naysayer Q6 独立同一収束): option-a manual を「Phase 1 dogfooding resume 前 1 回必須 **gate**」 に紐付け、 「manual / periodic / 任意実行」 ambiguous framing は drop (= 構造的腐敗防止、 trigger = MVP 完了 という single 明確 event)。 `docs/dogfooding.md` §2 に gate checklist 明文化。

#### 2.4.5 race-rate threshold spec の sub-PR 4 carry (= review C3 新規 carry)

option-a metric は **数値出力までで止める**。 「race rate どの値で sub-PR 4 trigger か」 の threshold spec は本 sub-PR で**決めない** — 実 dogfooding observation 前に speculation threshold を打つと判断材料が circular (= endogenous bias)。 「race rate threshold spec」 を **sub-PR 4 propose に carry** (= msg-131 §5 新規 carry)。 downstream log aggregation が summary line を跨 startup で sum して cross-startup rate を出す (= in-process persistence なし、 minimal 維持)。

### 2.6 Feature 3-C: claude.ai-participant read tools

**Trilateral decide SOT**: chatroom `T-feat3-read-overview` msg-136 (= integrator decide、 三者全論点 convergent、 user 最終承認 2026-05-16)。 propose=msg-133 (claude.ai main) / review pass=msg-134 (ClaudeCode) / naysayer pass=msg-135 (claude.ai-naysayer 4 原則) / integrator=msg-136 (ClaudeCode)。 GitHub monument = **Issue #48** (`[tracker] Feature 3-C`、 F3-A #41 と並列の独立 tracker)。 本節は decide msg の文書化記録、 議論ログは chatroom 一次。

#### 2.6.1 位置付け (= 独立 feature、 F3-A umbrella 外)

F3-C は **F3-A umbrella #41 の射程外の独立 feature**。 F3-A MVP (sub-PR 1-3) は `be7940c` で完了 + dogfooding resume gate 通過済 = 「MVP 完了 = dogfooding resume」 milestone は既に fire 済で、 F3-C が遡及して遅延させることは構造的に不能 (review msg-134 §依頼3a)。 sub-PR 5 化すると「F3-A 完結」 が曖昧化する (naming hygiene) ため、 driver / audience / scope が独立に identifiable な本件は独立 feature 化する。 scope = single sub-PR (read 2 tool 実装 + spec §1.1/§3.1/§5 additive + server 改名 + dogfooding runbook 更新)。

driver = dogfooding harvest で局所化した relay friction = 「claude-code 返信 → claude.ai 側 relay」 経路に Claude Desktop の thread read 手段がなく本文目視手貼り。 observation-driven (= speculation でない実観察)。

#### 2.6.2 採用結論 (= 三者 convergent、 divergent ゼロ)

| 論点 | 採用 |
|---|---|
| 1 audience 拡張 | **A** = §3 read API の同一 read surface に `claude.ai-participant` audience を additive 追加 (別建て read tool set は signature SOT 二重化で reject)。 load-bearing = signature SOT 単一 + audience list additive 最小 + Phase 2+ Connector 自然合流 (worldview「participant も AI agent」 は secondary support) |
| 2 scope | **2 tool minimal** = `mindwire_list_threads` + `mindwire_get_thread`。 1 tool minimal は thread_id 手貼り friction 再生で reject、 `get_events`/`status` は participant driver 不在で Phase 2+ Connector/Operator まで spec-only |
| 3 前倒し配置 | **partial 前倒し + F3-C 独立 feature 化** (§2.6.1) |
| 4 server 配置 | **A (mindwire-mcp-server 相乗り) + 改名を atomic decision (不可分)**。 改名なし A は naming mismatch + D2-3 rationale (read/write+api_key 分離) tension の独立 2 path で reject。 改名先 = `mindwire-participant` (user 承認、 §2.1.2 表更新済)。 read stub `mindwire-mcp` は D2-3 status quo preserve で touch なし |
| 5 §3 spec additive | **additive 3 件**: (a) §1.1 audience list に `claude.ai-participant` (b) §3.1 `ThreadSummary.awaiting_from: string\|null` return field (c) §5.5 read consistency model (best-effort snapshot / transactional 非対象)。 `awaiting_from_filter` input filter は client-side filter で同 outcome + driver 不在で **defer**。 schema_version 据え置き (= backward compat) |

#### 2.6.3 仕様増減 override = user 最終承認取得済

公開 MCP API 増 (= 三者合意でも user 最終承認必須、 CLAUDE.md 仕様増減 override rule)。 2026-05-16 user AskUserQuestion で全面承認: (1) 公開 read API 2 tool 追加 (2) spec additive 3 件 (3) server 改名 `mindwire-write`→`mindwire-participant` (user 選択、 `mindwire-cai` 不採用) (4) D2-3 (user 承認済境界) の audience 軸再解釈。

#### 2.6.4 非干渉確認 (= review msg-134 §3b、 実装で verify 済)

- read tool は **file write ゼロ** (`atomic_write_text` / `os.replace` / `toggle_awaiting_from` を呼ばない、 `load_*` は pure read) → sub-PR 3 D3-1 race-gap metric に不変 (gap は seq-file 生成数依存)
- read stub `mcp_server.py` は **touch なし** → D2-3 status quo preserve 維持
- `ApiKeyMiddleware` は app-level blanket gate (BaseHTTPMiddleware、 routing 前に全 request 検証) → read tool を同 FastMCP に register しても per-tool bypass なし、 同 bearer gate を自動継承、 auth 機構不弱化。 唯一の帰結 = 同 api_key が read+write 両方を grant = これは §2.6.2 論点 4 の audience 軸 reframe で正当化済 (read だけ欲しい第三者不在)

#### 2.6.5 実装 trace

| commit 単位 | 内容 |
|---|---|
| c1 | `src/spirrow_mindwire/mcp_write_server/tools_read.py` (`ReadTools` = `list_threads` net-new enumeration + `get_thread` loader 薄 wrap) + `http.py` `_register_read_tools` 登録。 tool 名は §3 SOT 命名 100% 流用 (`mindwire_list_threads` / `mindwire_get_thread`)。 `tests/test_mcp_read_server.py` |
| c2 | `docs/mcp-interface.md` §1.1 / §3.1 / §5.5 additive + §8 経緯 |
| c3 | `SERVER_NAME` `mindwire-write`→`mindwire-participant` 改名 (http.py const + docstring) + 本 §2.1.2 表 / §2.6 + `docs/dogfooding.md` §5 connector 改名 runbook |

## 3. Schema policy

### 3.1 ThreadMeta schema_version 1 → 2 bump (sub-PR 1)

**変更**: `ThreadMeta.schema_version: Literal[1]` → `Literal[2]`、 `SCHEMA_VERSION` 定数も 1 → 2。 field shape は不変 (= Naysayer skeleton narrow per、 chatroom `T-feat3-d1-schema-skeleton` msg-120 §2 D1-1 = A 採用)。

#### 3.1.1 「3 schema 独立」 原則の維持

mindwire core は **3 つの独立 `schema_version`** を持つ (= `docs/architecture.md` T02 原則 10、 `_common.py` SCHEMA_VERSION docstring):

| schema | 用途 | 本 bump 後 |
|---|---|---|
| `ThreadMeta.schema_version` | `threads/<ULID>/meta.yaml` | **2** |
| `Message.schema_version` | `threads/<ULID>/messages/NNN-from-{cai\|cc}.md` frontmatter | 1 (= 据え置き) |
| `_BaseEvent.schema_version` | `logs/threads/<ULID>.jsonl` 各 event | 1 (= 据え置き) |

F3-A の driver (= claude.ai 側 awaiting_from / message write) が直接触るのは ThreadMeta であり、 Message / Event の structural 変更は本 sub-PR に存在しない。 「3 schema 独立」 原則を曲げる driver なし、 不要な bump は cognitive friction を増やすだけ (= sub-PR 2 / 3 で新 field / 新 event 型が emerge した時点で incremental bump)。

> **`schema_version` は per-resource、 era marker ではない** (= Naysayer pass msg-122 §1.3 cognitive friction defuse): meta.yaml が v2 で messages frontmatter が v1 という mixed 状態は **正しい状態**、 「Phase 1 era 全体を v2 で統一」 という連想は誤読。 各 resource は自身の breaking-change cadence で bump され、 cascade しない。 本節 §3.1.1 が文字通り 3 resource を横並びに列挙しているのはこの原則の visualization。

#### 3.1.2 Big-bang 厳格 (= 並走 reader なし)

`ThreadMeta` は v2 only に bump、 v1 を読むと validation fail。 (= msg-117 §4 採用 + msg-120 §4 D1-3 = β、 並走 reader phase は持たない)

operator は **watcher 起動前に必ず一度** `uv run mindwire-migrate-v1-to-v2` を実行する (= documented operation)。 idempotent なので safe re-run 可。

#### 3.1.3 migration script の semantics (msg-120 §3 D1-2)

`uv run mindwire-migrate-v1-to-v2 [--data-dir PATH] [--dry-run]`:

- **対象**: `<data_dir>/threads/<ULID>/meta.yaml` のみ。 staging dir (`.staging-<ULID>/`) / 空 thread dir / messages / events.jsonl は touch せず
- **idempotent**: 既 v2 = no-op skip
- **atomic**: 既存 `atomic_write_text` 流用 (= `*.tmp` → `os.replace`)
- **pre-flight validation**: rewrite 前に `ThreadMeta.model_validate(new_payload)`、 失敗時は file 不変
- **per-thread isolation**: 1 thread failure が run 全体を阻害しない、 `MigrationReport` に集約
- **--dry-run**: scan + plan のみ、 file 不変
- **exit code**: failed thread あれば 1、 なければ 0

#### 3.1.4 Phase 1 dogfooding 5 thread の migration

既存 dogfooding 5 thread (`01KRGW51..` / `01KRHS3Z..` / `01KRHW2N..V9/A/B`) は v1。 sub-PR 1 merge 後の dogfooding resume 前に migration script で v2 に変換する。

### 3.2 後続 sub-PR の schema 変更方針

(sub-PR 2 以降で incremental 追加予定。 想定 subsection)

- **§3.2.1** Message schema_version bump trigger (= sub-PR 2/3 で claude.ai 側 write 用 frontmatter field 追加が emerge した時点)
- **§3.2.2** Event schema_version bump trigger (= sub-PR 3 の race monitoring 用 event 型 / sub-PR 4 の 2-phase commit event 追加時)
- **§3.2.3** schema_version は per-resource (= not era marker) — 1 resource の bump が他 resource に cascade しないこと、 「Phase N era = v_N」 という連想は誤読である旨を明示 (= Naysayer pass msg-122 §1.3 cognitive friction defuse)

## References

- 三者 view source: `T-feat3-design-overview` msg-109 (propose) / msg-112 (review) / msg-115 (naysayer)
- decide: `T-feat3-design-overview` msg-117 (= integrator、 resolved)
- sub-PR 1 design specifics: `T-feat3-d1-schema-skeleton` msg-120 (propose、 active)
- 設計 SOT (継承元): `docs/feature-2-design.md` §3.2 / §6.0 / §6 FI-2 / `docs/architecture.md` §3.1 (= meta.yaml example bumped to v2)
- 関連 GitHub: Issue #41 (= umbrella) / #42 (= sub-PR 1)
