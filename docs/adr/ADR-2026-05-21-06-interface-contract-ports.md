# ADR-2026-05-21-06: mindwire Interface Contract (Ports)

## Metadata
- **Status**: Accepted
- **Date**: 2026-05-21
- **Accepted Date**: 2026-05-21T23:16:04.978077Z UTC
- **Decided By**: msg-169 (chatroom thread T-ADR06-interface-contract decide msg)
- **Author**: main
- **Supersedes**: 旧 `src/spirrow_mindwire/` の Phase 0 実装 (schema / claude_code / lifecycle / mcp_server / watcher / filesystem)
- **Supersedes (partial)**: ADR-2026-05-21-05 § "RoleAdapter Protocol" — Phase 1 contract として signature を本 ADR §3 に置き換える。ADR-2026-05-21-05 の他 § (architecture pattern / Role enum / Capability enum / NAYSAYER_QUALIFIED 判定 logic) は引き続き Accepted。
- **Depends on**:
  - [ADR-2026-05-21-04](https://docs.google.com/document/d/1MWxqhh55dxWLaoL5pXKTZVyxcc2Ojbr7WPW1XG8IkSI/edit) — mindwire 役割転換
  - [ADR-2026-05-21-05](https://docs.google.com/document/d/15HsiZ-lXq1t_l0kw_Q1hL_zroSH8RNQzl9cG2b9_XlQ/edit) — Ports & Adapters 抽象化
- **Cross-project audit pre-condition**: T11 (ClaudeCodeSdkAdapter) 着手の前に以下 2 件 doc update 完了が必須:
  - (i) spirrow-tomtar 側 ADR-2026-05-21-05 doc 注釈追記「§ "RoleAdapter Protocol" は spirrow-mindwire ADR-06 で部分 supersede、Phase 1 contract」
  - (ii) mindwire 側 cross-project link doc (ADR-05) 注釈追記「signature SOT は ADR-06 §3」
  - 両 doc 注釈完了が T11 spawn の絶対 pre-condition。
- **Project**: spirrow-mindwire

## 1. Scope

Phase 1 mindwire の Port 群と値オブジェクトを定義する。実装 (adapter, dispatcher) の具体は本 ADR 範囲外。

ADR-04/05 で「mindwire = N者 chatroom クライアント自動操縦 layer」「Ports & Adapters」と決まったが、Port の signature が未定義であるため、本 ADR で contract を確定させる。

設計方針: **旧資産は手段に過ぎないので必要に応じて捨てる**。新世界で再利用したくなる規約だけ §6 で継承宣言する。

## 検討した選択肢

本 ADR で採用した (α) Pure Hexagonal に至るまでに比較検討した代替設計、および採用後に fresh naysayer audit で flag された Gap-2 alternative (c) について audit trail として記録する。

### (β) Mixed: adapter が ChatRoom を部分的に知る
- post 経路は dispatcher 経由 / read 経路は adapter 直接
- 棄却理由: 5 補完 field (author / thread_ref / posted_at / session_id / adapter_id) の責任が dispatcher / adapter に分散、I1 Knowledge boundary 二重定義

### (γ) Adapter-aware: dispatcher は session 管理のみ
- ChatroomGateway を adapter に inject
- 棄却理由: dispatcher 抽象の存在意義減 + ADR-05 の naysayer 独立性 architecture-level 強制 (NAYSAYER_QUALIFIED capability 判定) が adapter 側に分散する

### Gap-2 alternative (c): dispatcher 側完全 role-aware filter
- (c) では dispatcher が session→role mapping を持ち、event.author == session.role の post を filter out する。adapter は他 role 概念を知らずに済む
- **I1 純度は (c) > (b) > (a)** — (c) は採用した (b) を上回る
- 採らなかった理由 (close call rationale):
  1. **Phase 1 dispatcher routing simplicity** (moderate weight): (c) は dispatcher が session-role mapping + per-event filter logic を持つ、(b) は dispatcher が全 event を pass-through で adapter 側 self-filter
  2. **Adapter defensive programming** (strong だが decisive でない): (b) は own 投稿が echo back されても self-filter で吸収、(c) では adapter が「own 投稿は届かない」 silent assumption を持ち、dispatcher bug で own 投稿が届くと self-reply 無限 loop の risk
- ADR §3 変更量: (b) は SpawnContext field 1 件追加、(c) は dispatcher state 1 件明示で **roughly 同等**、(b) が最小ではない
- **本選定は close call**、dominant reason は defensive programming のみ
- Phase 2 で 2 つ目 adapter 登場時、上記 2 理由 weight を再評価して (c) migration を検討する余地を残す

## 2. Value Objects

### 2.1 ThreadRef

ChatRoom 上の thread を一意に指す値オブジェクト。

```python
@dataclass(frozen=True)
class ThreadRef:
    project_id: str
    thread_id: str       # ULID
    chatroom_uri: str    # magickit chatroom 上の resource URI
```

旧 `schema/ThreadMeta` (filesystem-shaped, awaiting_from / retry_count 等を含む superset) は discard。

### 2.2 SessionHandle

dispatcher が adapter の session を参照するための **不透明トークン**。adapter 固有のリソース (pid / browser tab id / HTTP session token 等) は dispatcher に漏らさない。

```python
@dataclass(frozen=True)
class SessionHandle:
    session_id: str      # ULID
    adapter_id: str
    thread_ref: ThreadRef
    role: Role
    started_at: datetime # iso_z
```

### 2.3 ChatroomEvent

dispatcher → adapter に流す event。`event_type` で discriminated union。

```python
class EventType(StrEnum):
    NEW_MESSAGE = "new_message"
    THREAD_CLOSED = "thread_closed"
    ROLE_REASSIGNED = "role_reassigned"  # Phase 2+
    PEER_HALTED = "peer_halted"

@dataclass(frozen=True)
class ChatroomEvent:
    event_id: str        # ULID, dedupe key
    event_type: EventType
    thread_ref: ThreadRef
    occurred_at: datetime # iso_z
    payload: EventPayload # union by event_type

@dataclass(frozen=True)
class NewMessagePayload:
    msg_id: str          # chatroom 側 message ID
    author: str          # "human" / role 文字列
    body: str
    parent_msg_id: str | None
```

### 2.4 ReplyDraft

adapter → dispatcher のリプライ。**adapter は ChatRoom を知らない**ので post API param は含まない。

```python
@dataclass(frozen=True)
class ReplyDraft:
    body: str            # markdown
    reply_to_msg_id: str | None
    adapter_metadata: dict[str, Any]  # adapter 固有 trace (model_id, tokens 等)
```

**dispatcher 側で補完するフィールド** (adapter には書かせない):
- `author = session.role`
- `thread_ref = session.thread_ref`
- `posted_at = now()`
- `session_id = handle.session_id`
- `adapter_id = handle.adapter_id`

### 2.5 HealthStatus / ErrorInfo

```python
@dataclass(frozen=True)
class HealthStatus:
    state: SessionState
    last_active_at: datetime
    error: ErrorInfo | None
    details: dict[str, Any]  # adapter-specific (pid, tab_id, http_session_status 等)

@dataclass(frozen=True)
class ErrorInfo:
    code: str            # "adapter.timeout" / "adapter.auth_failed" 等カタログ化
    message: str
    raised_at: datetime
```

### 2.6 Enums

```python
class Role(StrEnum):
    PROPOSER = "proposer"
    NAYSAYER = "naysayer"
    IMPLEMENTER = "implementer"

class Capability(StrEnum):
    READ_THREAD = "read_thread"
    POST_REPLY = "post_reply"
    EXECUTE_CODE = "execute_code"
    NAYSAYER_QUALIFIED = "naysayer_qualified"

class SessionState(StrEnum):
    IDLE = "idle"        # spawned, awaiting events
    PROCESSING = "processing"
    HALTING = "halting"
    HALTED = "halted"
    FAILED = "failed"
```

## 3. Ports

### 3.1 RoleAdapter

```python
class RoleAdapter(Protocol):
    adapter_id: str
    capabilities: frozenset[Capability]

    async def spawn(
        self,
        thread_ref: ThreadRef,
        role: Role,
        ctx: SpawnContext,
    ) -> SessionHandle: ...

    async def deliver_event(
        self,
        handle: SessionHandle,
        event: ChatroomEvent,
    ) -> None: ...

    async def halt(
        self,
        handle: SessionHandle,
        *,
        grace: timedelta = timedelta(seconds=5),
    ) -> None: ...

    async def health(
        self,
        handle: SessionHandle,
    ) -> HealthStatus: ...


@dataclass
class SpawnContext:
    """dispatcher が adapter に渡す『出口』。adapter は ChatRoom を知らない。"""
    on_reply: Callable[[ReplyDraft], Awaitable[None]]
    on_event_log: Callable[[Event], Awaitable[None]]
    own_role: Role  # NEW (Gap-2 (b) 案要件: adapter の self-filter 用)
```

### 3.2 AdapterRegistry

```python
class AdapterRegistry(Protocol):
    def register(self, adapter: RoleAdapter) -> None: ...
    def get(self, adapter_id: str) -> RoleAdapter: ...
    def qualified_for(self, role: Role) -> list[RoleAdapter]: ...
```

Phase 1 実装は dict-backed の trivial 実装。Phase 2 で `qualified_for` の policy 化が見込まれる。

### 3.3 ChatroomGateway (dispatcher 内部)

dispatcher と magickit chatroom MCP の橋渡し。**Port にはしない** (Phase 1 では内部実装として固定)。Phase 2 で他 ChatRoom 実装が登場したら Port 化を検討。

### 3.4 Port-level Exception Contract

各 Port method の失敗時 raise 例外を以下 4 base class で規定する。adapter / dispatcher の例外責務分担を ADR レベルで固定し、Phase 1 dogfood 中に新規例外 type が出現しても base class hierarchy で吸収する。

| Method | Failure raise class | 典型 failure |
|---|---|---|
| `spawn()` | `AdapterSpawnError` | subprocess 起動失敗 / gateway 接続失敗 / model 不在 |
| `halt()` | `AdapterHaltError` | graceful timeout / force kill 失敗 |
| `health()` | `AdapterHealthError` | adapter 応答なし / internal state 不整合 |
| `deliver_event()` | `AdapterDeliveryError` | session closed / payload validation 失敗 |

- 4 base class は mindwire core が `src/spirrow_mindwire/exceptions.py` (想定 path) で定義
- adapter 側は subclass で具体化可能 (例: `ClaudeCodeSdkSpawnError`)
- dispatcher は base class で catch、subclass の詳細は `HealthStatus.details` 経由で observability に流す (I2 整合)
- exception code は §1 規約継承の `error.*` カタログと統合し、**`HealthStatus.error.code` (§2.5 `ErrorInfo.code`) を single SOT として cross-reference** する。dispatcher は base class catch 後、subclass 詳細を `HealthStatus.error` (code/message/raised_at) に格納する。`HealthStatus.details` には exception code を **重複保持しない** (§3 軸 二重管理回避)。
  - ※ Minor clarification 経緯: decide msg msg-169 §3 では Option (ii) (`details["error_code"]` 経由) で記録されたが、本 doc rewrite 時に §2.5 `error: ErrorInfo | None` の既存が判明し、二重経路回避のため **Option (i) 相当 (`HealthStatus.error.code` 経由) に訂正** (audit trail: thread `T-ADR06-drive-rewrite-from-cc`)。

## 4. Invariants

**I1. Knowledge boundary**: adapter は ChatRoom を知らない。ChatroomEvent と SpawnContext.on_reply 経由でのみ chatroom と相互作用する。

**I2. SessionHandle opacity** (v2.1):
- dispatcher は `SessionHandle` の内部構造を参照しない (== / hash / dict key 用途のみ)
- SessionHandle equality は **identity equality (Python `is` 等価)**。value equality は禁止 (adapter 実装で `__eq__` / `__hash__` override 不可、dataclass default の value equality を使う場合は `eq=False` 必須)。2 spawn() の返値は internal field が偶然一致しても異なる session として区別される
- adapter 固有資源 (pid / tab_id / subprocess handle 等) は `HealthStatus.details` 経由でのみ運用観測可能
- dispatcher の **意思決定分岐 (session lifecycle 遷移判定 / event routing / dedup 判定 を含むがこれに限られない)** に `HealthStatus.details` 値を使うことを禁止
- `HealthStatus.details` 値の用途は **observability / 運用 log 出力 / 運用 dashboard 表示** に限定

**I3. Author = Role with Migration boundary** (v2.1):
- chatroom post の `author` field は role 名 (`proposer` / `naysayer` / `implementer`) または `human` のみ許容 (Phase 1 新規 post)
- Phase 1 では adapter / model identity は Event log (フラット JSONL) の唯一 SOT。ChatRoom message metadata への二重持ちは行わない (二重管理 §3 軸違反回避)
- **Migration cut-over**: ADR-06 Accepted msg = thread `T-ADR06-interface-contract` の decide msg `msg-169` (timestamp **2026-05-21T23:16:04.978077Z UTC**) を以て、以降の新規 post に本 invariant 適用。既存 `claude.ai` / `claude-code` author 表記は legacy として残置、rewrite 禁止 (audit trail 保全)
- 既存 legacy author の identity 確認は当該時期の Event log を参照
- **Phase 2 で再選定する trigger 条件** (両条件 AND):
  - (a) ChatRoom (conclair) が公式 metadata field を提供し、
  - (b) adapter / model identity 表記の cross-search / audit 用途で Event log 単独参照のコストが運用上問題化した時
  - 上記 (a) (b) **いずれも満たすまでは** Phase 1 invariant を継続。再選定議論は別 ADR で扱い、ADR-06 自体は影響しない

**I4. Event dedup** (v2.1):
- dispatcher は ULID 起点の bounded set による dedup を行う
- bounded set のサイズは observation 結果に基づき調整可能とし、**本 ADR では固定しない**。実装側 `DEFAULT_DEDUP_SET_SIZE` constant として T13 dispatcher core module に配置
- 取りこぼし率 observation 設計は §5 Out of Scope (Phase 1 dogfood 中に観察、別 ADR で最終決定)
- **event_id 解釈注記** (T14 watcher, audit root chatroom thread `T-phase1-impl-t11-t13` msg-197/198): `event_id` は **ULID 型である必要はなく、安定識別子であれば I4 を満たす**。dedup の目的 (restart-safe な重複排除) を達成する安定 id であればよく、ULID 型自体は invariant の目的ではない。T14 ChatroomWatcher は chatroom 由来の安定 id `f"{thread_id}:{msg_id}"` を event_id に用いる (fresh ULID だと watcher 再起動時に全件再 dispatch され dedup 目的を裏切るため)。本注記は I4 の dedup 機構を変えない解釈明確化 (仕様増減ではない)。

**I5. idempotency_key** (v2.1):
- `idempotency_key = f"{session_id}:{reply_seq}"`
- `reply_seq` は session 単位 monotonic counter であり、**dispatcher が払出** (adapter は ReplyDraft 構築時に knowledge しない)。ReplyDraft が adapter から dispatcher に到達した時点で dispatcher が seq を付与し、idempotency_key を生成する

**I6. 規約継承**: ULID, iso_z timestamps, フラット JSONL Event log は旧資産から継承 (§6 参照)。

**I7. on_event_log observational channel** (新規):
- `on_event_log` は observational log channel に限定
- control flow request (adapter → dispatcher 方向の作業依頼) の semantics は持たない
- control flow 追加が必要な場合は callback 追加 (= ADR 改訂) を強制
- `on_event_log` callback raise 時、dispatcher は **例外を catch して内部 log (例: dispatcher の error log channel) に記録し、main flow には propagate しない**。observational channel の失敗が main flow を break することは禁止 (= 「observational」 semantics の boundary 強制)

**I8. SessionState transitions** (新規):
- `idle → processing`: spawn 完了後 deliver_event 受領時
- `processing → idle`: reply emit 後、次 deliver_event 受領可能状態
- `idle | processing → halting`: `halt()` 呼び出し
- `halting → halted`: graceful 停止完了
- `* → failed`: 任意状態から致命例外 (terminal)
- `halted | failed → *`: **遷移不可** (terminal、SessionHandle 廃棄、recovery は新規 spawn 要)
- `halt()` を **terminal state (halted / failed) または既に halting state** の session に呼んだ場合は **idempotent no-op**。例外を投げず、SessionState 遷移も発生させず、即座に return する (shutdown path retry idempotency 保証)

**I9. deliver_event ordering** (新規):
- 同一 SessionHandle に対する `deliver_event` 呼び出し順は dispatcher の `occurred_at` 時系列順 (FIFO) を保証
- `occurred_at` は ULID 由来 monotonic timestamp
- adapter は受領順 = 発生順として処理して良い
- 異 SessionHandle 間の event 順序は **ChatRoom post 順 (thread-level msg-id 単調増加)** から inherit。dispatcher は同一 SessionHandle 内 FIFO 保証のみ責務、cross-session ordering は ChatRoom 規約に委ねる
- I9 monotonicity guarantee は **単一 dispatcher process 内で成立**。multi-dispatcher 構成 (例: HA / region 分散) は本 ADR で対象外、Phase 2 で multi-dispatcher driver が出る場合は別 ADR で順序保証 protocol を再設計する

## 5. Out of Scope (Phase 2+)

1. callback 群拡張 (driver 観察後検討、例: progress streaming / adapter からの能動 chatroom read)
2. `HealthStatus.details` 共通 key set 整備 (2 つ目 adapter 登場時)
3. Event dedup bounded set サイズ最終決定 + Phase 1 dogfood 中の取りこぼし率 observation 設計
4. `adapter_metadata` 共通 key set 整備 (2 つ目 adapter 登場時)
5. External MCP API — **Phase 2 開始時点の意思決定、Phase 1 中の試作は invariant 違反**

## 6. Migration / Discard

**Discard**:
- `src/spirrow_mindwire/schema/` (filesystem-shaped, 新 ChatRoom 世界と整合せず)
- `src/spirrow_mindwire/claude_code/` の subprocess + custom MCP tool 構成 (T11 で新規実装、知見のみ参照)
- `src/spirrow_mindwire/lifecycle/` (新 SessionState で再定義)
- `src/spirrow_mindwire/mcp_server.py` (Phase 1 不要)
- `src/spirrow_mindwire/watcher/` のファイル監視ロジック (chatroom 監視に再構成、T14)
- `src/spirrow_mindwire/filesystem/` (SOT が chatroom に移るので不要)

**継承する規約** (実装ではなく規約):
- ULID 採番
- iso_z (Z-suffix) timestamps
- フラット JSONL Event log 設計 (`event_id` ULID 必須, body 不含, N+1 許容)
- `error.*` カタログ運用 (HealthStatus.error.code として継続)
- Pydantic v2 `ValidationError` catch 注意 (`ValueError` subclass ではない、3a バグ知見)

### Discard モジュール復活 path

Phase 2 以降で discard 済みモジュール (例: `mcp_server.py`) の機能を復活させる必要が生じた場合、git revert による code 復元は **不可** (依存していた旧 schema も同時 discard 済のため import 失敗 + name collision)。復活は「新 value objects / 新規約に整合する **新規実装**」として行い、旧実装は git log を参照する知見資料として扱う。復活 cost は新規実装の概ね 50〜70% と見積もる (設計判断は再利用、code は流用不可)。

## 7. 関連タスク

| Task | 概要 |
|---|---|
| T10 | 本 ADR §2 値オブジェクト + §3 Port Protocol 定義 (実装なし、型のみ) |
| T11 | ClaudeCodeSdkAdapter 実装 (新規、旧 claude_code/ は参考のみ) |
| T12 | ChatroomGateway 実装 (dispatcher 内部) |
| T13 | Dispatcher core + lifecycle 状態遷移 + idempotency dedup |
| T14 | ChatroomWatcher (polling) |
| T15 | 自動操作手段比較研究 (Phase 1 末) |
| T16 | Phase 1 smoke test |

## 8. Smoke Test 合格基準

> 1 thread に対し、proposer 役として ClaudeCodeSdkAdapter を spawn、human が 1 メッセージを post すると、dispatcher → adapter → dispatcher → ChatRoom の経路で `author=proposer` の応答が 1 件 post される。

- 1 adapter, 1 role assignment, 1 round-trip
- 失敗時 (timeout / 例外) は `FAILED` イベントが Event log に残る
- halt で session が消えてリソースも開放される

## 9. オープン論点 (naysayer focus 推奨領域)

> 本 § は Proposed 時点の論点。各項目は v2.1 Accepted で下記の通り解決済 (historical 保全)。

A. **(γ) を捨てて (α) Pure Hexagonal を選んだ判断の妥当性**: ADR-05 の `extra_mcp_servers` 経由の柔軟性を失っていないか、特に多 adapter 化時の implementation cost。
- **[Resolved]** 「検討した選択肢」で (β)(γ) 棄却理由明文化、(α) 採用根拠は naysayer pass #8 (msg-142) で converge。

B. **`SpawnContext` の callback 設計**: `on_reply` 単独で足りるか、`on_progress` / `on_request_chatroom_read` 等が将来必要にならないか。
- **[Resolved]** I7 (`on_event_log` observational 限定) + §5-1 callback 拡張 defer。

C. **`SessionHandle` の不透明化と debuggability の両立**: dispatcher が「あの session の subprocess pid を知りたい」状況が運用で発生しないか (HealthStatus.details で十分か)。
- **[Resolved]** I2 (`HealthStatus.details` escape hatch 正規化 + identity equality)。

D. **Author = Role の migration boundary**: 既存 chatroom thread 内に `claude.ai` / `claude-code` post が並んで存在する状態は audit / 後読み時に混乱を招かないか。post metadata に `adapter_id` を持たせる磁石が ChatRoom 側未対応の場合、I3 の effective 半減リスクは?
- **[Resolved]** I3 (Event log 唯一 SOT + cut-over timestamp 焼き付け)。

E. **Event dedup の bounded set サイズ 1024 の妥当性**: 高頻度な thread (例: dogfooding session で複数往復) で取りこぼしの可能性は?
- **[Resolved]** I4 (bounded set 数値を本文から削除) + §5-3 observation 設計 defer。

F. **`mcp_server.py` を Phase 1 で完全 discard することの可逆性**: Phase 2 で復活させる場合の Cost (新規実装相当か、git history から再生可能か)。
- **[Resolved]** §6 Discard モジュール復活 path (git revert 不可、新規実装 50-70%)。

## Implementation Notes

### naming hygiene anchor #6 (累積到達)

過去 5 case (sub-PR 1 author 名揺れ / sub-PR 3 thread tag 揺れ / sub-PR 4 race threshold defer / F3-C 改名 / F3-C naming hygiene) に続く **6 件目** として「chatroom author = role 名統一 + Event log key 名統一」 を accumulate。本累積到達を ADR-level anchor として正規化し、enforcement は T13 dispatcher 単体 test で carry。

### T13 dispatcher 単体 test enforce 一覧
- Event log への author / model_id 書込み key 名統一
- I7 `on_event_log` への control 性 event 流入を assertion fail
- I7 callback raise が dispatcher 内 log に隔離 (main flow propagate なし)
- I2 SessionHandle identity equality 検証 (異 spawn 結果が偶然同 field 値でも `!=`)

### §8 Phase 1 smoke test integration 検証一覧
- I8 SessionState 遷移 (halt() / failed 経路含む)
- I8 halt() idempotency on terminal/halting state
- I9 同一 SessionHandle 内連続 NEW_MESSAGE 流入で FIFO 維持

### Port exception base class 配置

mindwire core (`src/spirrow_mindwire/exceptions.py` 想定) で 4 base class (AdapterSpawnError / AdapterHaltError / AdapterHealthError / AdapterDeliveryError) を定義、adapter は import して subclass。T11 ClaudeCodeSdkAdapter は subclass 例として ClaudeCodeSdkSpawnError 等を実装。