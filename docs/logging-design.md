# Spirrow MindWire ログ機能設計 (T04)

> Phase 0 のログ機能の詳細仕様。 architecture.md / mcp-interface.md と重複する基本要件は参照で済ませ、 T04 固有の補強事項 (ログレベル、 エラーカタログ、 モデル情報、 system.jsonl、 ローテーション、 検索戦略) を中心に記述する。

## 1. 既に確定済みの基本要件 (再確認)

| 項目 | 確定先 |
|---|---|
| JSONL append-only フォーマット | `architecture.md §3.3` |
| スレッドごとディレクトリ + system 全体ログ (`logs/threads/<ULID>.jsonl` / `logs/system.jsonl`) | `architecture.md §2` |
| Event 共通フィールド (`schema_version`, `event_id`, `ts`, `type`, `thread_id`) | `mcp-interface.md §4.1` |
| type 別の追加フィールド | `mcp-interface.md §4.2` |
| Body は events に含めない (N+1 許容) | `mcp-interface.md §4.3` |
| 永久保存 (Phase 0) | `architecture.md §8.2` |
| ripgrep 横断検索 | JSONL 構造で自然達成 |
| schema_version 互換管理 | `mcp-interface.md §5.4` |

## 2. ログレベル方針

**event type 自体で判別する** (level フィールドを追加しない)。

理由:
- event type が明示的かつ体系的 (`error.*` / `*.invoke.*` / `thread.*` / `message.*`)
- level field を別途持つと冗長になり、 event type と矛盾する設計余地が生まれる
- 検索は type prefix で十分対応可能

検索例:
```bash
# エラー全部
rg '"type":"error\.'

# 業務トレース (claude_code 起動・終了)
rg '"type":"claude_code\.'

# thread ライフサイクル
rg '"type":"thread\.'
```

将来 「INFO/DEBUG レベルで動的にフィルタしたい」 ニーズが出た場合は schema_version up で `level` を追加可能 (Phase 0 では不要)。

## 3. エラーイベント (`error.*`) カタログ

Phase 0 で想定するエラー subtype:

| event type | 発生シナリオ |
|---|---|
| `error.thread.create_failed` | `new/` ファイルから thread 作成失敗 (ULID 衝突 / I/O エラー等) |
| `error.message.write_failed` | `messages/` への書込み失敗 (権限・ディスク等) |
| `error.claude_code.invoke_failed` | claude-code subprocess の起動失敗 (binary not found 等) |
| `error.claude_code.timeout` | claude-code が config 上限時間内に応答せず |
| `error.config.invalid` | `mindwire.toml` 構文エラーまたはスキーマ違反 |
| `error.fs.permission_denied` | データディレクトリへの権限不足 |
| `error.unknown` | 上記カタログに該当しない想定外エラー |

`error.*` event の追加フィールド (`mcp-interface.md §4.2` で定義済み):
- `code: string` (内部コード、 例: `"ENOENT"`)
- `message: string` (人間可読の説明)
- `details: object` (発生コンテキスト: file path, exception class 等)

**運用方針**:
- 新規エラーパターンが見つかったら **`error.unknown` で出してから subtype 追加** で対応 (Phase 0 では unknown 多発を許容)
- `error.unknown` が頻発するようになったら subtype 拡充を検討する

## 4. メッセージ作成モデル情報

`claude_code.invoke.start` event に **`model_id: string?`** を追加する。

```jsonl
{"schema_version":1,"event_id":"01ARZ4D...","ts":"2026-05-07T08:43:10Z","type":"claude_code.invoke.start","thread_id":"01ARZ3...","msg_seq":1,"model_id":"claude-opus-4-7"}
```

理由:
- 「このメッセージはどのモデルが書いたか」 を後から辿れる (forensics / 比較検証)
- MyLanguageModel 構想 (architecture.md 決定 6) の基盤素材として価値
- 実装コスト最小 (起動時に取得した model_id を 1 フィールド追加するだけ)

**設計上の禁則**:
- メッセージ本文の frontmatter (`Message` schema) には **`model_id` を入れない**
- 理由: `Message` schema は世界観保護 (T02 設計判断 8) のため最小に保つ。 モデル情報は event log のみで持つ
- 将来 claude.ai 側 (cai) の自動化が進んだ場合、 同様に `claude_ai.invoke.*` イベントを定義してそこに `model_id` を付ける想定

## 5. system.jsonl の役割

threadごとログ (`logs/threads/<ULID>.jsonl`) が主軸。 system.jsonl は **thread に紐付かないグローバル事象のみ** に絞る。

Phase 0 で記録する event:

| event type | 追加フィールド |
|---|---|
| `service.started` | `version` (semver), `started_at`, `config_hash` (config 内容の md5 / sha 短縮) |
| `service.stopped` | `reason` (`"sigterm"` / `"sigint"` / `"config_reload"` 等) |
| `service.config.reloaded` | `old_hash`, `new_hash` (実装する場合のみ) |
| `error.config.invalid` | `code`, `message`, `details` (thread スコープ無しの config エラー) |
| `error.fs.*` | `code`, `message`, `details` (起動時 / 全体的な filesystem エラー) |
| `error.unknown` | `code`, `message`, `details` (想定外グローバルエラー) |

**Phase 0 スコープ外** (system.jsonl に書かない):
- thread 単位のエラーは threadごとログに書く
- message 受信・送信は threadごとログに書く
- メトリクス系 (cpu / mem / 接続数) は別系統 (将来的に Prometheus 連携等)

## 6. ローテーション / アーカイブ戦略

**Phase 0 では実装しない** (永久保存)。 方針のみ記録:

### Phase 1+ 候補

- **ファイル肥大化検知**: `mindwire_status` に `disk_usage_bytes` 追加 (`mcp-interface.md §7.5` と整合)
- **アーカイブ時の圧縮**:
  - 個別 JSONL を残す案: `logs/threads/<ULID>.jsonl.gz` (ripgrep `-z` で透過読込み可能)
  - tar 集約案: `archive/<ULID>.tar.gz` にまとめる (検索性は落ちるがコンパクト)
  - Phase 1+ で実測して決定
- **ディスク容量監視**: 運用層 (T05 / 運用ドキュメント) で別途

### Phase 0 で気をつけること

- 単一 thread の JSONL が極端に肥大化する設計上の理由はない (1 message あたり数百バイト〜数 KB の event を発生)
- system.jsonl は startup/shutdown + 例外のみなので低頻度
- 一般的な運用では Phase 0 期間中にローテーション必須レベルに達することは想定しにくい

## 7. 検索戦略

### 7.1 Primary: ripgrep ベース (Phase 0)

`logs/` 配下の JSONL は 1 行 1 イベントで grep フレンドリー。 ripgrep のパターン例:

```bash
# 直近 1 日のエラーイベント
rg '"type":"error\.' logs/ | rg '"ts":"2026-05-07'

# 特定 thread の message.received だけ
rg '"thread_id":"01ARZ3...","type":"message\.received"' logs/

# 60 秒超かかった claude_code.invoke
rg '"type":"claude_code\.invoke\.end".*"duration_ms":[6-9][0-9]{4,}' logs/

# 特定 model が書いたメッセージ
rg '"type":"claude_code\.invoke\.start".*"model_id":"claude-opus-4-7"' logs/
```

`rg --json` で JSON 出力すれば後段の `jq` / Python でパース可能。

### 7.2 Secondary: `mindwire_get_events` MCP tool

Connector / Operator が構造化アクセスする標準経路 (`mcp-interface.md §3.3`)。 `type_filter` / `since_ts` / `thread_id` でクエリ。

### 7.3 Phase 1+: Prismind 連携

Prismind が `mindwire_get_events` を polling で全件吸い上げ → 長期インデックス化。 全文検索 / 構造化クエリは Prismind 側で提供 (`mcp-interface.md §7.2` と整合)。

## 8. メタデータ拡張方針 (T04 task notes 対応)

T04 task notes が言及する 「関連プロジェクト・モデル情報」 への対応:

| メタデータ | 対応方針 |
|---|---|
| **モデル情報** | `claude_code.invoke.start` event の `model_id` (§4) で記録 |
| **関連プロジェクト** | meta.yaml の `tags[]` で緩やか表現 (人間 / エージェントが付ける)。 厳密な参照は Connector が外部 KV で管理 (T02 確定: コアに `related.*` を持たない) |
| **その他のドメインメタデータ** | thread の `tags[]` を最低限の表現として、 構造化が必要になったら schema_version up で対応 |

「ドメイン特有のメタデータ」 をコアに足したくなった時の判断基準:
- AI エージェント間通信に内在するか? (= 例: タグ・タイトル・参加者)
- それとも外部システム連携に紐付くか? (= 例: 「Magickit プロジェクト ID」 「Lexora モデル ID」)

前者ならコア候補、 後者なら Connector 層で持つ (T02 設計判断 8 と整合)。

## 9. ログ生成タイミング (実装方針メモ)

T05 (Phase 0 実装) で詳細化するが、 設計レベルで決めておく:

- **immediate write**: イベント発生時に即座に write (バッファリングしない)
- **append-only**: ファイル末尾に追記、 既存行は変更しない
- **アトミック保証は緩い**: 1 行 = 1 JSON object なので部分書込みのリスクは小 (POSIX の `write(2)` の atomic 保証で 4KB 未満は基本安全)
- **`fsync` は不要 (Phase 0)**: 性能優先、 OS クラッシュで直近イベントが失われる可能性は許容
- **複数 thread が同時に書く可能性**: あるが、 各 thread が別ファイル (`logs/threads/<ULID>.jsonl`) なので衝突なし
- **system.jsonl への並行書込み**: Phase 0 では単一 watcher プロセスから書くので衝突なし

## 10. 設計判断の経緯

T02 / T03 で定めた基本構造を T04 で補強:

- **A** ログレベル: event type で判別 (level field 追加せず) — 体系的・冗長性回避
- **B** error subtype カタログ: 7 種を初期セット、 `error.unknown` 多発時に拡充
- **C** `model_id` を `claude_code.invoke.start` に追加 — MyLanguageModel 素材として価値、 Message schema は変更しない
- **D** system.jsonl は thread スコープ外のグローバル事象のみ
- **E** ローテーション Phase 0 では未実装、 Phase 1+ 候補として記録
- **F** ripgrep + MCP の二段構え検索 (Phase 1+ で Prismind 追加)
- **G** メタデータ拡張は world-view 軸 (内在 / 外部) で判断

T04 は T02 / T03 で大筋方針が固まっていたため、 補強的な詳細詰めに留まる。 Claude.ai レビューは経ず、 Claude Code 単独で確定。
