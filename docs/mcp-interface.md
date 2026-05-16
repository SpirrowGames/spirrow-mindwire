# Spirrow MindWire MCP Interface 仕様 (T03)

> Phase 0 公開 read-only MCP サーバが提供する 4 tool の signature と運用ポリシーを定義する。 詳細議論は ChatRoom thread `T-mcp-interface-review` 参照。

## 1. 設計原則

### 1.1 公開スコープ (T02 / T03-Q1 確定、 F3-C で audience 追加)
公開 MCP の **read API** は read-only に閉じる。 audience は:
- **Connector**: 外部システム連携 (Magickit ChatRoom / Prismind / Thirdy 等) を担う独立プロセス
- **Operator**: サービス監視・運用 CLI
- **claude.ai-participant** (Feature 3-C 追加): claude.ai 側 participant が自分宛て thread を一覧 / 取得し、 claude-code の返信を relay するための audience。 dogfooding harvest で観測した relay friction (= Claude Desktop に thread read 手段がなく本文目視手貼り) の解消が driver。 §3.1 `mindwire_list_threads` + §3.2 `mindwire_get_thread` の 2 tool のみがこの audience 向けに実装活性化される (§3.3 `mindwire_get_events` / §3.4 `mindwire_status` は Connector/Operator driver 出現まで spec-only のまま)

claude-code 自身は MindWire MCP を使わず、 ファイルプロトコル (Write tool で `messages/<NNN>-from-cc.md` を直接書く) で応答する。 これにより 「ファイルベース I/O が核」 (architecture.md 決定 4) と整合。

> **claude.ai-participant audience の server 配置** (Feature 3-C、 chatroom `T-feat3-read-overview` msg-136 論点 4): この audience 向け read tool は read-only stub (`mindwire-mcp`) ではなく、 同 audience (claude.ai-participant) が write tool も正当に要するため **`mindwire-participant` server** (旧 `mindwire-write`、 F3-C で改名) に相乗りする。 api-key 境界が contract 軸 (write) から audience 軸 (participant) に reframe され、 「read だけ欲しい第三者が存在しない」 ことで D2-3 の scope-based access control 不要性が回復する。 read stub (`mindwire-mcp`) は D2-3 status quo preserve のまま touch しない。 詳細は `docs/feature-3-design.md` §2.1。

### 1.2 同期 / 非同期方針 (T03-Q4 確定)
すべての tool は **同期 return**。 「変化を待つ」 ニーズは Connector 側の polling で実現 (秒〜分レイテンシ許容と整合)。 long-poll / webhook は実装しない (T02 「lifecycle 通知は pull 専用」 と整合)。

### 1.3 schema_version policy
return data の `schema_version` で互換性を管理。 tool 名にバージョン suffix を付けない。 互換性を保つ変更 (フィールド追加 等) は version 据え置き。 破壊変更が必要な場合は新 tool (`_v2` 等) で並行運用。

## 2. Tool 一覧

| tool | 用途 | 主要 consumer |
|---|---|---|
| `mindwire_list_threads` | thread 一覧 (filter / pagination) | Connector / Operator |
| `mindwire_get_thread` | thread meta + messages 一括取得 | Connector / Operator |
| `mindwire_get_events` | event log 構造化クエリ (Connector の polling 主用途) | 主に Connector |
| `mindwire_status` | サービス稼働確認・運用メトリクス | Connector / Operator |

**Defer (Phase 1+)**:
- `mindwire_get_message` (単一 message 取得): `get_thread` の `message_seq_from/to` で代替可
- `mindwire_search` (全文検索): Phase 1+ で `_search_threads` / `_search_messages` の **別 tool として** 導入予定
- write 系全部: α スコープ外

## 3. Tool signature

### 3.1 `mindwire_list_threads`

```yaml
inputs:
  status_filter: array<string>?       # OR; 例 ["active", "awaiting-cc"]; 省略時 archived 除く全 status
  tag_filter: array<string>?          # OR
  participant_filter: array<string>?  # OR
  id_filter: array<string>?           # OR (IN 句意味論); 特定 thread 群の更新確認用
  created_after: string?              # UTC ISO 8601, inclusive (≥)
  created_before: string?             # UTC ISO 8601, inclusive (≤)
  include_archived: bool = false
  limit: int = 100 (max 1000)
  offset: int = 0

returns:
  schema_version: int
  items: array<ThreadSummary>
  total: int
  limit: int
  offset: int

ThreadSummary:
  thread_id: string (ULID)
  title: string
  status: string
  awaiting_from: string | null     # F3-C additive; 次に応答すべき participant、 terminal 状態で null
  participants: array<string>
  created_at: string (UTC ISO 8601)
  updated_at: string
  tags: array<string>
  message_count: int
```

> **`awaiting_from` return field** (Feature 3-C additive、 chatroom `T-feat3-read-overview` msg-136 論点 5): list 段階で turn 判定可 = participant が `get_thread` を都度叩かずに自分の turn の thread を絞り込める (N+1 削減)。 型は `string | null` (`Participant | None`、 terminal 状態で `null`)。 schema_version 据え置き (= additive、 §5.4 policy 整合)。
>
> **`awaiting_from_filter` input filter は意図的に defer** (msg-136 論点 5): server-side の `awaiting_from_filter` 入力は追加しない。 dogfooding scale では participant の thread 数が僅少で、 返却された `awaiting_from` の client-side filter で同 outcome が得られる = server-side filter は driver 不在の speculative surface。 driver 出現時に additive 追加可能 (schema_version 据え置き)。

**エラー**:
- `invalid_argument`: filter 引数の型不正、 ULID format 不正 (id_filter 内)、 created_after > created_before 等

**使用例** (Connector が複数 thread の更新検知):
```
mindwire_list_threads(id_filter=["01ARZ3...", "01ARZ4...", "01ARZ5..."])
→ 各 thread の updated_at が一括取得できる
```

### 3.2 `mindwire_get_thread`

```yaml
inputs:
  thread_id: string (ULID)              # required
  include_messages: bool = true
  message_seq_from: int?                # 両端 inclusive
  message_seq_to: int?                  # 両端 inclusive

returns:
  schema_version: int
  thread: ThreadDetail                  # meta.yaml フル相当
  messages: array<Message> | null       # include_messages=false なら null

ThreadDetail:
  schema_version, thread_id, title, status, participants[],
  created_at, updated_at, tags[]

Message:
  schema_version: int
  msg_id: string                        # <thread_id>/<seq>
  seq: int
  from: string                          # claude.ai | claude-code
  to: string
  created_at: string (UTC)
  reply_to: int | null
  body: string                          # Markdown 本文
```

**エラー**:
- `thread_not_found`: 該当 thread_id が `threads/` にも `archive/` にも無い
- `invalid_argument`: ULID format 不正、 `message_seq_from > message_seq_to` 等

**使用例**:
```
# Thread 全体取得
mindwire_get_thread(thread_id="01ARZ3...", include_messages=true)

# 差分取得 (前回処理済 seq=5 まで)
mindwire_get_thread(thread_id="01ARZ3...", message_seq_from=6)

# meta のみ (軽量)
mindwire_get_thread(thread_id="01ARZ3...", include_messages=false)
```

### 3.3 `mindwire_get_events`

```yaml
inputs:
  thread_id: string?                    # 省略時は全 thread + system 結合
  since_ts: string?                     # UTC ISO 8601, inclusive
  until_ts: string?                     # inclusive
  type_filter: array<string>?           # OR; 例 ["message.received", "message.sent"]
  limit: int = 1000 (max 10000)
  offset: int = 0

returns:
  schema_version: int
  events: array<Event>
  total: int
  limit: int
  offset: int
  has_more: bool                        # offset + len(events) < total
```

**条件付き必須ルール**:
- `thread_id` 省略時は `since_ts` 必須 (全結合スキャン暴走の防止)
- 違反時のエラー: `invalid_argument: "When thread_id is omitted, since_ts is required to bound the result."`

Event 型は **§4 参照**。

**使用例** (Connector の polling 主パターン):
```
mindwire_get_events(since_ts="2026-05-07T10:00:00Z", limit=1000)
→ 最終同期時刻以降の全 thread + system イベントが ts 順に返る
→ 受け取った最新 ts を保存して次回の since_ts に渡す
```

特定 thread の差分取得:
```
mindwire_get_events(thread_id="01ARZ3...", type_filter=["message.received"])
```

### 3.4 `mindwire_status`

```yaml
inputs: (none)

returns:
  schema_version: int
  service:
    status: "healthy" | "degraded" | "stopped"
    version: string                     # semver, 例 "0.1.0"
    started_at: string (UTC)
    uptime_seconds: int
  threads:
    active: int
    awaiting_cc: int
    awaiting_cai: int
    resolved: int
    archived: int
    total: int
  recent_activity:
    messages_last_hour: int
    threads_created_today: int
    last_event_at: string?              # 最終イベントの ts; polling 最適化用
  paths:
    data_dir: string
    config_file: string
```

**使用例** (Connector の軽量 polling パターン):
```
status = mindwire_status()
if status.recent_activity.last_event_at > my_last_sync_ts:
    events = mindwire_get_events(since_ts=my_last_sync_ts)
    process(events)
else:
    sleep(poll_interval)  # 重い API を呼ばない
```

## 4. Event 型仕様

### 4.1 共通フィールド (全 Event 必須)

| field | type | 説明 |
|---|---|---|
| `schema_version` | int | スキーマバージョン |
| `event_id` | string | ULID。 dedup / cursor-based pagination 用 |
| `ts` | string | UTC ISO 8601 |
| `type` | string | イベント種別 (`thread.*` / `message.*` / `claude_code.*` / `error.*`) |
| `thread_id` | string\|null | 対象 thread。 system 全体イベントは null |

### 4.2 type 別の追加フィールド (Phase 0)

| type | 追加フィールド |
|---|---|
| `thread.created` | `title` (string) |
| `thread.status.changed` | `from_status` (string), `to_status` (string) |
| `thread.resolved` | (なし) |
| `thread.archived` | (なし) |
| `message.received` | `seq` (int), `from` (string), `size_bytes` (int) |
| `message.sent` | `seq` (int), `from` (string), `size_bytes` (int) |
| `claude_code.invoke.start` | `msg_seq` (int) |
| `claude_code.invoke.end` | `msg_seq` (int), `duration_ms` (int), `exit_code` (int) |
| `error.*` | `code` (string), `message` (string), `details` (object) |

### 4.3 Body の扱い
Event は **メッセージ body を含まない**。 必要な場合は `mindwire_get_thread` で別途取得する (Phase 0 では N+1 を許容)。 これにより event log は append-only かつ軽量に保たれる。

## 5. 共通仕様

### 5.1 エラー構造

```yaml
error:
  code: string      # machine-readable
  message: string   # human-readable
  details: object?  # optional context
```

主要 code:
- `thread_not_found`: 該当 thread が無い (404 相当)
- `invalid_argument`: 引数不正 (400 相当)
- `service_unavailable`: watcher 停止中等 (503 相当)
- `internal_error`: 想定外の内部エラー (500 相当)

### 5.2 Filter semantics

| field | 意味論 |
|---|---|
| `status_filter` / `tag_filter` / `participant_filter` / `id_filter` / `type_filter` | **OR** (要素いずれか含む) |
| `created_after` / `created_before` | **inclusive** (≥ / ≤) |
| `message_seq_from` / `message_seq_to` | **両端 inclusive** |
| `since_ts` / `until_ts` | **inclusive** |

将来 AND 意味論が必要になった場合は `*_filter_mode: "or" \| "and"` を後付け (schema_version up)。

### 5.3 Pagination

`offset` + `limit` を採用。 Phase 0 ではデータ量小なので問題にならない。 cursor-based に置き換える場合は schema_version up で互換管理。

### 5.4 schema_version policy
- 全 return data に `schema_version: int` を含める
- 互換性を保つ変更 (フィールド追加・新 enum 値追加) は version 据え置き
- 破壊変更は新 tool (`_v2` 等) で並行運用、 旧 tool は次のマイナー版まで維持

### 5.5 Read consistency model (Feature 3-C 追記)

```
# read is best-effort snapshot; transactional reads are out of scope
```

read tool (`mindwire_list_threads` / `mindwire_get_thread`) は **best-effort snapshot** を返す。 watcher / write tool が同 thread に書き込んでいる最中の read は、 stale-but-complete な状態を観測し得る (= `atomic_write_text` の `*.tmp` → `os.replace` 保証により torn file は観測されない、 loader contract)。 次回 call で convergent (= eventual consistency)。

transactional read (= multi-thread / multi-message を 1 つの一貫した snapshot として atomic に読む) は **scope 外**。 participant の relay 用途 (= 最新 message を取得して貼る) には best-effort snapshot で十分。 cross-process write race contract は `docs/feature-2-design.md` §3.6 / `docs/feature-3-design.md` §2.1.3 (D2-6 invariant) と同じ = read tool は file write を一切行わないため sub-PR 3 race-gap metric に影響しない。

## 6. Connector polling パターン (典型例)

```python
# 擬似コード
state = load_state()  # last_event_ts を保存

while running:
    # 軽量チェック: 変化があるかだけ確認
    status = mindwire_status()
    if not status.recent_activity.last_event_at > state.last_event_ts:
        sleep(poll_interval)
        continue

    # 変化あり: 重いクエリで詳細取得
    events = mindwire_get_events(since_ts=state.last_event_ts, limit=1000)
    for event in events.events:
        process(event)  # 必要なら mindwire_get_thread で詳細取得

    if events.events:
        state.last_event_ts = max(e.ts for e in events.events)
        save_state(state)

    if events.has_more:
        continue  # ページング、 sleep なし
    sleep(poll_interval)
```

このパターンでは:
- 静止期間 (no-op) は `mindwire_status` のみで済む = サーバ負荷小
- 動きがある時のみ `get_events` を呼ぶ
- `last_event_ts` は Connector の **外部 KV** (sqlite / json file 等) に保存。 MindWire コアは Connector の cursor を知らない (T02 設計原則)

## 7. Future Work (Phase 1+)

### 7.1 Thread topology の表現
将来 thread の split / merge / supersede を表現する必要が出た場合、 `ThreadDetail` に以下のフィールド追加を検討:
- `affects_threads: array<string>` (この thread が影響する他 thread)
- `superseded_by: string?` (この thread を置き換える後継 thread)
- `references_threads: array<string>` (参照する thread)

schema_version up で互換管理可。 Magickit ChatRoom 連携で需要が出る可能性が高い。

### 7.2 Search API
Phase 1+ で導入する場合、 戻り値型が thread か message かで API 形が大きく異なるため、 **別 tool に分離する**:
- `mindwire_search_threads`: 戻り値 ThreadSummary 配列 (タイトル・タグ・participants 等で検索)
- `mindwire_search_messages`: 戻り値 Message 配列 + thread コンテキスト (本文検索)

1 tool 統合は型推論が破綻するので避ける。 内部実装は ripgrep ラッパ or Prismind 連携。

### 7.3 Streaming / Subscription
T02 で 「lifecycle 通知は pull 専用」 を確定済みのため、 Phase 0〜2 までは sync polling のみ。 Phase 3+ で必要になった場合は新 tool として additive に追加 (`mindwire_subscribe_events` 等)。 既存 read tool は壊さない。

### 7.4 メッセージ単位の属性付け
Phase 0 では `Message` に `tags` を持たない (thread 単位の `tags` のみ)。 メッセージ単位の属性付けが必要になった場合は schema_version up で対応。

### 7.5 status enum 増加時の互換性
`mindwire_status.threads` の status 別カウントはハードコード (`active`, `awaiting_cc`, `awaiting_cai`, `resolved`, `archived`)。 将来 status enum が増えたら互換破壊するので、 必要時に `threads_by_status: map<string, int>` 形式に移行 (schema_version up)。

## 8. 設計判断の経緯

- **T03-Q1** (audience スコープ): α (read-only) 採用。 ファイルベース I/O が核という決定 4 と整合
- **T03-Q2** (tool セット): 4 tool (3 必須 + status) で確定。 search / get_message / write 系は defer
- **T03-Q3** (signature 詳細): 個別 signature を確定 (§3 参照)
- **T03-Q4** (同期 / エラー / バージョニング): sync only + 構造化エラー + return data の schema_version で確定
- **T03-Q5** (Claude.ai レビュー): 6 観点中 #1 (Event 構造) を blocker として解決、 #2-5 を Phase 0 に取り込み (`last_event_at`, `id_filter`, filesystem 明記、 OR/AND/inclusive 明示)、 #6 を Future Work として記録
- **F3-C** (claude.ai-participant audience 追加、 chatroom `T-feat3-read-overview` msg-136 = integrator decide、 三者 convergent + user 最終承認 2026-05-16、 GitHub tracker #48): §1.1 に audience `claude.ai-participant` 追加、 `mindwire_list_threads` + `mindwire_get_thread` の 2 tool を `mindwire-participant` server (旧 `mindwire-write`、 同 PR で改名) に相乗り実装活性化。 spec additive 3 件 = (a) §1.1 audience list (b) §3.1 `ThreadSummary.awaiting_from: string\|null` return field (c) §5.5 read consistency model。 `awaiting_from_filter` input filter / §3.3 `mindwire_get_events` / §3.4 `mindwire_status` は driver 不在で defer (spec SOT は維持、 additive 活性化余地)。 signature/命名は §3 SOT を 100% 流用、 schema_version 据え置き

詳細議論: ChatRoom thread `T-mcp-interface-review` (msg-008 〜 msg-012)、 F3-C は `T-feat3-read-overview` (msg-133 〜 msg-136)。
