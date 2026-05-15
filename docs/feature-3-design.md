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
- **§2.3** claude.ai 側 awaiting_from 更新 + message write 実装 (sub-PR 3)
- **§2.4** race monitoring instrumentation (sub-PR 3 bundle)
- **§2.5** 2-phase commit re-design (sub-PR 4 deferred、 dogfooding race observation N+ 件 trigger)

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
| Server name | `mindwire-write` (= MCP handshake で advertise、 in-process `mindwire` server と区別) |

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
