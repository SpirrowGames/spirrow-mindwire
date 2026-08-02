# Spirrow MindWire アーキテクチャ仕様 (T02)

> Phase 0 ベースライン設計 — MindWire コア (AI エージェント間通信ハブ) のディレクトリ構造、 データモデル、 ライフサイクル、 外部統合境界を定義する。

## 1. 設計原則

MindWire は **AI エージェント同士の通信ハブ** に世界観を閉じる。 引き継ぎドキュメントの 7 つの確定事項に、 T02 で確定した追加原則を加えた合計 10 項目が本仕様の根幹。

### 引き継ぎ済み確定事項
1. 独立 MCP サーバとして実装 (Magickit 非依存)
2. ChatRoom を経由しない (責務分離)
3. 双方向通信 (Claude.ai ↔ Claude Code、 どちらからも開始可能)
4. ファイルベース I/O が核
5. レイテンシ秒〜分単位許容
6. 全ログ保存機能搭載
7. Phase 0 = Claude Code 側のみ自動化 (Claude.ai 側は手動コピペ)

### T02 で確定した追加原則
8. **外部システム連携は MindWire コアから完全分離**: コア schema は外部システムを一切知らない (`related.*` / `external_refs.*` / `metadata: {}` のいずれも持たない)。 連携は独立 Connector 層 (MCP サーバレベル) が担う
9. **Lifecycle 通知は pull 専用**: MindWire はイベントを外部に push しない。 Connector / 観測者は polling または file watch で eventual consistency を前提とする
10. **`schema_version` を全データ単位に必須**: meta.yaml / メッセージ frontmatter / イベントログそれぞれに `schema_version` を持つ。 コア schema 変更時の前方互換性確保

経緯: 当初の引き継ぎでは 「双方向参照リンクは OK」 と書かれていたが、 Takahito の追加指摘 (「コネクタ / MCP サーバレベルの結合度に絞りたい」) を受けて refinement。 詳細は ChatRoom thread `T-connector-pattern-decision` および第 6 章を参照。

## 2. ディレクトリ構造

```
~/spirrow-mindwire-data/
├── config/
│   └── mindwire.toml              # サービス設定 (poll間隔、 paths、 claude binary パス等)
├── new/                            # 新規スレッドのドロップゾーン (まだ thread_id 無し)
├── threads/
│   └── <ULID>/                     # 1 thread = 1 ディレクトリ
│       ├── meta.yaml               # スレッドメタ
│       └── messages/               # 1 メッセージ = 1 ファイル (append-only)
│           ├── 001-from-cai.md
│           ├── 002-from-cc.md
│           └── ...
├── archive/                        # resolved 後の thread を退避
│   └── <ULID>/                     # threads/ と同形を保持
└── logs/
    ├── system.jsonl                # サービス起動 / 停止 / グローバルエラー
    └── threads/
        └── <ULID>.jsonl            # スレッド毎のイベントログ
```

### ディレクトリの役割
- `config/`: サービス設定。 ユーザが手で編集する
- `new/`: 操作者が初発話を置く入口。 watcher が検知して `threads/<ULID>/messages/001-from-cai.md` へ移動 + meta.yaml 作成
- `threads/`: 進行中スレッドの全データ
- `archive/`: resolved 後に thread ディレクトリごと移動。 削除はしない (ログ完全性)
- `logs/system.jsonl`: サービス起動・停止・予期しないエラー等
- `logs/threads/<ULID>.jsonl`: 当該スレッドで起きた全イベント

## 3. データモデル

### 3.1 Thread (`threads/<ULID>/meta.yaml`)

```yaml
schema_version: 2                                   # 必須 (Phase 1 era、 F3-A sub-PR 1 で v1→v2 bump)
thread_id: 01ARZ3NDEKTSV4RRFFQ69G5FAV               # ULID (時刻ソート可、 衝突回避)
title: ""                                            # 任意。 人間 / エージェントが付ける1行サマリ
status: active                                       # active | awaiting-cc | awaiting-cai | resolved | archived
participants: [claude.ai, claude-code]              # 会話に参加する AI エージェント識別子
created_at: 2026-05-07T08:43:07Z                    # UTC ISO 8601
updated_at: 2026-05-07T08:45:22Z                    # 最終更新 UTC ISO 8601
tags: []                                             # participants 自身が付ける属性
```

**`schema_version` namespace**: meta.yaml / message frontmatter / イベントログ それぞれが **独立** な `schema_version` を持つ (T02 原則 10、 `_common.py` SCHEMA_VERSION docstring 参照)。 同時に bump する必要なし。 2026-05-15 時点で meta.yaml = 2 (F3-A sub-PR 1)、 message frontmatter = 1、 event log = 1。 詳細 / 経緯は `docs/feature-3-design.md` §3.1 (= migration trigger fired) と `docs/feature-2-design.md` §3.2 (= Phase 0 据え置き policy + Phase 1 trigger fire 記録)。

**設計上の禁則** (世界観保護):
- `related.*` フィールドを追加しない (外部システム参照)
- `external_refs.*` フィールドを追加しない
- `metadata: {}` のような汎用 free-form dict を追加しない
- `tags` は participants 自身が付けるもの。 **Connector が書き込むのは禁則**

### 3.2 Message (`threads/<ULID>/messages/NNN-from-{cai|cc}.md`)

ファイル命名:
- `NNN`: ゼロパディング 3 桁 (`001`, `002`, ...) のスレッド内シーケンス。 桁あふれ時は 4 桁に拡張
- `from-cai` / `from-cc`: 発話者識別 (`cai` = claude.ai、 `cc` = claude-code)

ファイル形式 (Markdown + YAML frontmatter):

```markdown
---
schema_version: 1
msg_id: 01ARZ3NDEKTSV4RRFFQ69G5FAV/003               # <thread_id>/<seq>
seq: 3                                                # スレッド内連番
from: claude.ai                                       # claude.ai | claude-code
to: claude-code                                       # claude.ai | claude-code
created_at: 2026-05-07T08:43:07Z                      # UTC
reply_to: 2                                           # 任意。 直前メッセージの seq
---

(本文 Markdown)
```

**書込み手順 (アトミック保証)**:
1. `messages/003-from-cai.md.tmp` に全内容を書く
2. `fsync` (任意、 強耐久性が必要な場合)
3. `rename(003-from-cai.md.tmp → 003-from-cai.md)` で公開
4. watcher は `*.md` のみ監視し、 `.tmp` は無視

### 3.3 イベントログ (`logs/threads/<ULID>.jsonl`)

1 行 1 JSON、 append-only。 ripgrep / jq で横断検索可能。

```jsonl
{"schema_version":1,"event_id":"01ARZ4A...","ts":"2026-05-07T08:43:07Z","type":"thread.created","thread_id":"01ARZ3...","title":""}
{"schema_version":1,"event_id":"01ARZ4B...","ts":"2026-05-07T08:43:08Z","type":"message.received","thread_id":"01ARZ3...","seq":1,"from":"claude.ai","size_bytes":1234}
{"schema_version":1,"event_id":"01ARZ4C...","ts":"2026-05-07T08:43:09Z","type":"thread.status.changed","thread_id":"01ARZ3...","from_status":"active","to_status":"awaiting-cc"}
{"schema_version":1,"event_id":"01ARZ4D...","ts":"2026-05-07T08:43:10Z","type":"claude_code.invoke.start","thread_id":"01ARZ3...","msg_seq":1}
{"schema_version":1,"event_id":"01ARZ4E...","ts":"2026-05-07T08:43:42Z","type":"claude_code.invoke.end","thread_id":"01ARZ3...","msg_seq":1,"duration_ms":33000,"exit_code":0}
{"schema_version":1,"event_id":"01ARZ4F...","ts":"2026-05-07T08:43:42Z","type":"message.sent","thread_id":"01ARZ3...","seq":2,"from":"claude-code","size_bytes":4567}
{"schema_version":1,"event_id":"01ARZ4G...","ts":"2026-05-07T08:43:42Z","type":"thread.status.changed","thread_id":"01ARZ3...","from_status":"awaiting-cc","to_status":"awaiting-cai"}
```

各イベントには **`event_id` (ULID)** を付与する。 dedup / cursor-based pagination / Connector の処理位置記録に利用される (詳細は `docs/mcp-interface.md` §4)。

**初期イベントタイプ (Phase 0)**:
- `thread.created` / `thread.status.changed` / `thread.archived` / `thread.resolved`
- `message.received` (cai → MindWire) / `message.sent` (cc → MindWire 経由 cai 行き)
- `claude_code.invoke.start` / `claude_code.invoke.end`
- `error.*` (具体的サブタイプは実装時に追加)

イベントタイプは将来増える前提。 `schema_version` で互換性を管理。

## 4. ライフサイクル / 状態遷移

```
[new ファイル投入] ──→ thread.created (status: active)
                             │
                             ├──→ status: awaiting-cc
                             │       (= claude-code の応答待ち)
                             │
                             ├── claude_code.invoke ──→ message.sent
                             │                              │
                             │                              ▼
                             ├──────────────── status: awaiting-cai
                             │                  (= claude.ai 側の応答待ち、
                             │                   操作者が手動コピペで Phase 0)
                             │
                             ├── 操作者が次の cai 発話投入 ──→ ループ
                             │
                             ├── 操作者 / エージェントが close ──→ status: resolved
                             │
                             └── 一定期間後 (手動 / scheduled) ──→ archive/ へ移動 (status: archived)
```

### 状態定義

| status | 意味 | 次の遷移トリガ |
|---|---|---|
| `active` | 初期状態 (thread 作成直後、 まだ最初の message 配信前) | message.received → `awaiting-cc` |
| `awaiting-cc` | claude-code の応答待ち | claude_code.invoke 完了 + message.sent → `awaiting-cai` |
| `awaiting-cai` | claude.ai 側の応答待ち (Phase 0 は手動コピペ) | 操作者が次の cai 発話投入 → `awaiting-cc` |
| `resolved` | 会話完了 | 一定期間後 → `archived` |
| `archived` | アーカイブ済み (`archive/` に物理移動) | 終端状態 |

### 同時実行 / 並行制御 (Phase 0)
- watcher プロセスは単一。 採番 / 状態更新は逐次処理 → ロック不要
- スレッド間は完全独立。 1 つの thread の処理が他を block しない (将来並列化容易)
- 同一 thread に対する claude_code.invoke は 1 件のみ並行不可 (Phase 0 では自然に逐次)

## 5. Connector パターン (外部統合の境界)

### 概念図

```
[Magickit ChatRoom]   [Prismind]   [Thirdy]   ...
        ↑↓               ↑           ↑
        │                │           │
[各 Connector]       (独立プロセス / 独立 MCP サーバ)
        ↑                ↑           ↑
        │                │           │
        └────────────────┴───────────┘
                         ↓
        [MindWire MCP server / file API]   ← 公開 I/F のみ
```

### Connector の責務と制約

**MindWire コア視点での Connector の扱い**:
- MindWire は Connector の存在を一切知らない
- Connector は MindWire の **公開 I/F** (file API、 将来は MCP tools) のみを使う
- Connector は MindWire データディレクトリ (`~/spirrow-mindwire-data/`) に **書き込まない** (read-only)
- Connector は自身の状態 (マッピング、 同期位置等) を **外部 KV** (sqlite / json file 等) に持つ

**Connector のメッセージ書込み権限 (T02 確定)**:
- **Connector は read-only**。 messages/ への書込みは participants (= AI エージェント) のみ
- 理由: AI-AI 通信ハブという世界観に Connector の声を混ぜないため
- これにより `from` / `participants` は常に AI エージェント識別子に限定される

**Lifecycle 通知の non-coupling**:
- MindWire は外部に push しない (webhook / event hook なし)
- Connector は polling または file watch (`logs/threads/*.jsonl` を tail) で eventual consistency 前提
- 「webhook が欲しい」 という将来の要望はこの原則で防衛 (秒〜分単位レイテンシ許容と整合)

**Filesystem アクセス前提 (Phase 0)**:
- 公開 MCP は read-only。 bidirectional Connector (例: Magickit ChatRoom 連携) が MindWire 側にメッセージを書き戻す経路は **filesystem 直書き** (atomic tmp + rename プロトコル: `messages/<NNN>-from-<src>.md.tmp` → `rename` で公開、 watcher の `write_reply` と同じ手順)
- 結果として **Phase 0 の Connector は MindWire データディレクトリへの filesystem アクセス権を持つ** ことが暗黙の前提
- Phase 1+ で Connector を別ホスト / 別プロセスで独立サーバ化する場合は、 NFS / sync / write API のいずれかでこの結合を再構築する必要がある (将来論点)
- **claude-code 自身は filesystem に直アクセスしない** (T06 で確定): SDK custom tool 経由で watcher にメッセージ書込みを委譲し、 ユーザのコードベース read も Phanthand 経由 (§6 参照)

### 期待される連携先 (温度感、 T02 時点)

| 連携先 | パターン | 温度 | 備考 |
|---|---|---|---|
| Magickit ChatRoom | bidirectional (将来) | 高 | 今回の発端。 ただし Connector 起点でも当面 read 中心 |
| Prismind | pull (read) | 高 | MindWire 全ログを長期知識として indexing |
| Thirdy | pull (read) | 中〜高 | AI-AI 議論ログから Spec 抽出する入力源 |
| Lexora | --- | 低 | LLM gateway、 連携動機弱い |
| CodePyxis | pull (read) | 低 | コード参照解析の入力 (将来) |

**設計上の含意**: 強い候補は pull/read 中心。 T03 で MindWire の公開 I/F は **read 系を充実、 write 系は最小限** で進める方針。

## 6. claude-code 起動モデル (Phase 0)

T06 で確定した watcher → claude-code spawn の構成。 「ファイルベース I/O が核」 (決定 4) と 「世界観の死守」 (決定 8) を両立させる。

### 6.1 起動手段: Claude Agent SDK (Python)

- `claude-agent-sdk` (Python) を採用。 PTY 直叩きはしない
- 採用根拠: claude-code CLI auth 継承、 programmatic な cwd / system_prompt / allowed_tools / mcp_servers 制御、 async 統合容易、 CLI flag 変更への耐性
- watcher は asyncio で書き、 SDK セッションを 1 invoke = 1 セッションで起動・完了させる

### 6.2 Spawn 時の構成

| パラメータ | 値 |
|---|---|
| `cwd` | thread dir (`threads/<ULID>/`) |
| `tools` | `[]` (built-in tools 全切り) |
| `mcp_servers` | in-process MindWire custom + 設定経由の pass-through |
| `allowed_tools` | 上記 in-process custom + 設定で許可された pass-through tools |
| `prompt` (user turn) | thread の全 context を XML 構造化 (§6.4) して 1 度に投入 |
| `system_prompt` | role + protocol 説明 (static、 thread 状態に依らない) |

### 6.3 MindWire 提供 custom tool セット (in-process)

| tool | 役割 |
|---|---|
| `mcp__mindwire__write_reply(content)` | 次メッセージの atomic 書込み (`NNN-from-cc.md.tmp` → rename) |
| `mcp__mindwire__read_file(path)` | Phanthand `/files/read` 経由 |
| `mcp__mindwire__list_dir(path)` | Phanthand `/files/list` 経由 |
| `mcp__mindwire__search(pattern)` | Phanthand `/files/search` 経由 |
| `mcp__mindwire__file_info(path)` | Phanthand `/files/info` 経由 |

claude-code は filesystem に直接アクセスしない:
- MindWire データへの書込みは `write_reply` のみ (連番計算 / atomic rename を watcher 側で完結、 LLM はファイルレイアウトを知らない)
- ユーザのコードベース read は Phanthand 経由 (§6.5)

### 6.4 thread context の prompt 注入

watcher は thread 状態 (meta.yaml + 全 messages) を XML 構造化して `prompt` parameter に積む (full eager push)。 system prompt 側で 「`<mw_thread>` 内の `is_latest="true"` の `<mw_message>` に対して `mcp__mindwire__write_reply` で返信」 と教える。

XML フォーマット例:

```xml
<mw_thread thread_id="01ARZ3..." status="awaiting-cc"
           participants="claude.ai,claude-code">
  <mw_message seq="1" from="claude.ai" to="claude-code"
              created_at="2026-05-07T08:43:07Z">
本文 markdown (XML 特殊文字は &lt; &gt; &amp; にエスケープ)
  </mw_message>
  ...
  <mw_message seq="N" ... is_latest="true">
本文 (これに返信する対象)
  </mw_message>
</mw_thread>
```

prefix `mw_` でユーザ本文中の XML との衝突を回避。 本文は XML escape する。

### 6.5 外部 file access layer: Phanthand 依存

claude-code がユーザの project ファイルを参照する場合、 watcher が Phanthand HTTP API (`SpirrowGames/spirrow-phanthand`) を呼ぶ。

- 各 PC で Phanthand を独立に起動 + whitelist 設定 → **MindWire schema に local path が一切入らない** ため multi-PC portability が完全達成される
- Phanthand は read-only API のみ。 Phase 0 では write/exec 系操作 (役割 R3) は **サポートしない** (議論内で実装が必要な場合は §6.6 の handoff で委譲)

### 6.6 Generic MCP pass-through (Connector パターン at SDK layer)

`mindwire.toml` の `[claude_code.extra_mcp_servers.*]` で SDK セッションに追加 expose する MCP server を declarative に列挙する。 MindWire コアは pass-through 対象を一切知らない (世界観保護)。

設定例 (Magickit ChatRoom 連携):

```toml
[claude_code.extra_mcp_servers.magickit]
type = "stdio"
command = "uv"
args = ["run", "magickit-mcp"]
allowed_tools = [
  "mcp__magickit__chatroom_open_thread",
  "mcp__magickit__chatroom_post_message",
  "mcp__magickit__chatroom_close_thread",
  "mcp__magickit__chatroom_get_thread",
  "mcp__magickit__chatroom_list_threads",
]
```

ユースケース: thread 議論中に 「実装してほしい」 「テストを走らせてほしい」 等、 Phase 0 の R2 (read-only) を超える要件が出た場合、 claude-code は ChatRoom thread を開いて人間 / 通常の対話 claude-code に handoff する。

### 6.7 `mindwire.toml` schema 抜粋

```toml
[phanthand]
endpoint = "http://localhost:7300"
api_key_env = "PHANTHAND_API_KEY"

[claude_code]
allowed_tool_profile = "readonly"   # "minimal" | "readonly"
                                     # "full" は Phase 0 ではサポート外

[claude_code.extra_mcp_servers.magickit]
# 上記参照
```

### 6.8 役割整理 (Phase 0)

| Role | tools | Phase 0 での扱い |
|---|---|---|
| R1 (minimal) | `write_reply` のみ | ⚪ `allowed_tool_profile = "minimal"` で選択可能 |
| R2 (readonly) | R1 + Phanthand 5 tool + ChatRoom (opt-in) | ⚪ **default 推奨** (`"readonly"`) |
| R3 (full = write/exec) | Phanthand 範囲外 | ❌ Phase 0 スコープ外、 必要なら ChatRoom 経由で handoff |

### 6.9 設計判断の経緯 (T06)

- **Q1 起動手段**: Claude Agent SDK 採用 (PTY 直叩き案を退けた、 auth 継承と programmatic 制御を優先)
- **Q2a ファイルプロトコル教育**: in-process custom tool で完全隠蔽 (LLM がファイルレイアウトを知らない構成)
- **Q2b thread context 注入**: full eager push via `prompt` + XML 構造化 (`<mw_thread>` / `<mw_message>`)
- **Q2c ファイル参照と handoff**: Phanthand 経由 read + generic MCP pass-through (ChatRoom 連携は設定例)
- 詳細議論: T06 進行セッション

## 7. 設計判断の経緯

### 7.1 統合 (`messages/`) vs 分離 (`inbox/` `outbox/`) → 統合採用
- 双方向通信での視点反転問題 (cai inbox = cc outbox) を回避
- Magickit ChatRoom と同じ append-only + status 思想で整合
- 視覚的キュー視認性は補助コマンド (将来) で代替

### 7.2 別ファイル vs 単一 thread.md → 別ファイル採用
- アトミック性: tmp+rename パターンが効くのは独立ファイルのみ
- watcher の CREATE イベント検出が直接的 (パターン一致のみ、 差分計算不要)
- 1 メッセージ = 1 イベントの append-only ログ思想と整合

### 7.3 Thread ID: 連番 vs UUID vs ULID → ULID 採用
- ツール経由で常時アクセスする前提では人間可読性は弱い利点
- 並行生成での衝突回避 (採番 race のリスク回避)
- ULID の時刻プレフィックスで ID 単体ソート = 時刻順 (ツール実装が単純化)
- ID 単体で作成時刻判別可能 (forensics で効く)

### 7.4 外部参照 schema 内蔵 vs Connector 化 → Connector 化採用 (T02 確定)
- 当初の引き継ぎ ("双方向参照リンクは OK") を Takahito の追加指摘で refinement
- 「リンクの管理主体をコアの外に追い出す」 ことで 「MindWire は AI-AI 通信に閉じる」 世界観を厳守
- `metadata: {}` 汎用 dict も「世界観侵食を呼ぶ穴」 と判断、 設置せず
- マッピング状態は Connector が外部 KV で保持

詳細議論: `T-connector-pattern-decision` thread (msg-003 〜 msg-007 の decide で resolved)

## 8. Phase 0 / 後続フェーズへの影響

### Phase 0 (= T05+T06) 実装スコープ
- `~/spirrow-mindwire-data/` ディレクトリ初期化 (config / new / threads / archive / logs)
- `new/` ファイル監視 → thread 作成
- `threads/<ULID>/messages/` への from-cai 書込みを検知 → **claude-code 起動 (Claude Agent SDK 経由、 custom tools + Phanthand)** (詳細 §6)
- `threads/<ULID>/messages/` への from-cc 書込み (`mcp__mindwire__write_reply` 経由、 watcher が atomic 書込み)
- `meta.yaml` の status 更新
- イベントログ append (`logs/threads/<ULID>.jsonl`)
- Phanthand HTTP client (read 系 custom tool の内部実装)
- generic MCP pass-through 機構 (`mindwire.toml` driven)
- `mindwire status` 等の最低限の運用 CLI (オプショナル)

### Phase 0 スコープ外
- Connector 実装 (Magickit 連携を含む)
- Claude.ai 側の自動化 (DOM 操作 / 専用ブラウザ)
- 高度な検索機能
- イベント subscribe API / webhook

### Phase 1+ (将来)
- Magickit-MindWire Connector の別プロジェクト化
- MCP tools 公開 (T03 で設計)
- Prismind による全ログ indexing
- Thirdy 連携 (Spec 抽出)
- Connector 用の安定 I/F 仕様 (`schema_version` 約束を含む)
- 大型 feature の sub-PR chain merge 運用は [`chain-merge-pattern.md`](chain-merge-pattern.md) (= contract integration checklist meta-process、 Feature 2 origin) を参照

## 8bis. Stage 3 — 第二の基盤 (magickit chatroom) とその上の層 (2026-08-02 追記)

> **本章より前の §2〜§7 は Phase 0 = filesystem 基盤の仕様であり、 Stage 3 には適用されない。** 両者は同じリポジトリに同居する **別の基盤**である。 この章はその境界を明示するために足した。 §2〜§7 を書き換えてはいない (Phase 0 の記述として今も正しい)。

### 8bis.1 基盤が 2 つある

| | Phase 0 基盤 | Stage 3 基盤 |
|---|---|---|
| daemon | `mindwire-watcher` | `mindwire-loop` |
| thread の実体 | `~/spirrow-mindwire-data/threads/<ULID>/messages/*.md` (§2/§3) | **magickit chatroom** (MCP 越し、 `chatroom_get_thread` / `chatroom_post_message`) |
| 参加者 | Claude.ai ↔ Claude Code の 2 者 | Bohr (proposer) / Heisenberg (implementer) / Einstein (naysayer) / human の 4 者 |
| 記述箇所 | 本書 §2〜§7 | 本章 + [`deploy.md`](deploy.md) |

実装上の裏付け: `conductor/` と `magickit/` は `threads/` にも `meta.yaml` にも触れない。 `ChatroomWatcher` の名前が示すとおり、 Stage 3 の watcher モードも監視対象は chatroom であって filesystem ではない。

∴ **§3 のデータモデル (`meta.yaml` / `NNN-from-{cai|cc}.md` / `schema_version`) は Stage 3 には無関係。** Stage 3 の thread schema は magickit 側が SOT である。

### 8bis.2 層とその境界

Stage 3 は 3 層で、 **層ごとに設定ファイルと責務が分かれる**。

| 層 | 実体 | 責務 | 設定 |
|---|---|---|---|
| sweep | `deploy/run-conductor-scheduled.ps1` | **どのスレッドを、 いつ回すか**。 優先リストを head 順に歩き、 変化していないスレッドは起動しない | `<data_dir>/config/sweep.json` |
| conductor | `mindwire-loop --mode conductor` | **1 スレッドを停止条件まで駆動する**。 末尾の `NEXT:` が指す 1 role だけを serial に dispatch | `mindwire.toml` の `[loop]` / `[conductor]` |
| role adapter | `adapters/` の 3 実装 | 各 role の推論を回す。 implementer のみ allowlist gate 下 | 同上 + `implementer_allowlist.yaml` |

**conductor は 1 スレッドを読んで終了する** (§8bis.3 の停止条件のいずれかで exit)。 複数スレッド・複数プロジェクトを跨ぐのは sweep 層であって conductor ではない。 README の CLI 表が `mindwire-loop` を 「reads one design thread」 と書いているのは、 この意味で正確である。

**設定を 2 つに分けている理由**: sweep 対象の一覧は「運用上の判断」であって daemon の設定ではない。 かつ `MindwireSettings` は strict model ∴ `mindwire.toml` に置くと deployment 一覧のためにパッケージ変更が要る。 加えて sweep 層は PowerShell 実装で、 TOML reader を持たない。

### 8bis.3 conductor の停止条件 (`StopReason`)

`NEXT:` の解決結果で決まる。 **`none` (= 決着) 以外はすべて人間に戻る**。

| reason | 意味 |
|---|---|
| `human` | `NEXT: human` — Tier-C 判断点 |
| `none` | `NEXT: none` — スレッド決着。 sweep は次候補へ進む |
| `no_handoff_to_human` | `NEXT:` が読めない / 名簿に無い → human へ fallback (Obj3) |
| `no_progress_to_human` | dispatch した role が何も投稿しなかった |
| `round_cap` | `max_rounds` 到達 (暴走バックストップ) |
| `empty_thread` | スレッドにメッセージが無い |

design→implement の handoff は構造的に human へ redirect される (Tier-C ゲート、 ADR-2026-06-03-17)。 `main` へのマージは**いかなる経路でも自動化されない** (D-5)。

### 8bis.4 naysayer は 2 面あり、 worldview が異なる

同じ 5 原則 SOT (`spec/NAYSAYER_PRINCIPLES.md`、 両者に**逐語**注入) を共有するが、 それ以外は別物である。

| | design-time naysayer | Tier B PR-gate |
|---|---|---|
| 実体 | `adapters/naysayer_sdk.py` (summon、 Agent SDK) | `naysayer/pr_review.py` (one-shot `chat_completion`) |
| 判断対象 | スレッド上の設計 | PR の静的 diff |
| 出力の性質 | スレッド内の助言 | CI 上の blocking verdict |
| ADR 索引 (N-2) | **注入する** | **注入しない** (2026-08-03 時点。 スコープ判断は `T-pr-gate-adr-index-scope` で係属中) |

`transport != judge`: PR-gate が Agent ループでなく one-shot なのは、 静的 diff を読むのに SDK ループを立てるのが YAGNI だからで、 判断の中核 (5 原則) は共通化されている。

**ADR 索引の参照先は MindWire 自身の `spec/adr_index.yaml` であって、 レビュー対象リポジトリではない。** ここを取り違えると索引が恒常的に空になる (実際に発生した。 PR #120)。

### 8bis.5 利用プロジェクト側に置く規約ファイル

Stage 3 は **対象リポジトリのルートに `.mindwire-*` を置き、 MindWire がそれを読む**という規約を持つ。 現状 1 本:

- `.mindwire-gate` — その repo の CI ゲートを実行するためのエントリ

2 本目 (デプロイ / ブランチ方針の宣言) は `T-per-project-deploy-rule` で設計中。 **背景**: 「どのブランチへ PR を開いてよいか」はプロジェクトごとに異なる (voxelworld = リリーストレイン / MindWire 自身 = main から継続デプロイ) ∴ MindWire の repo 非依存な allowlist に書くべきものではない。

### 8bis.6 §1 の設計原則との関係

原則 8 (外部システム連携はコアから完全分離) は **Stage 3 では成立していない**。 chatroom は magickit の MCP であり、 `magickit/` は Connector 層ではなくコアに同居する。 これは Phase 0 の原則を破ったというより、 **Stage 3 が原則 8 の想定しなかった軸 (AI 同士の多者ループ) に伸びた**結果である。 原則 8 を Stage 3 に遡及適用するか否かは未決 (§9 参照)。

原則 9 (pull 専用、 push しない) は Stage 3 でも保たれている — sweep は polling であり、 chatroom からの push は無い。 ただし **sweep 層から人間への push は行う** (human 待ちで停止したとき Discord へ通知)。 これは 「MindWire がイベントを外部システムに push しない」 とは別の話であり、 原則 9 に抵触しない。

## 9. 未決事項 / 将来論点 (記録のみ、 T02 では decide しない)

### 9.1 Connector 用の identity / 認証境界
- `participants` / message frontmatter `from` は単なる文字列で誰でも名乗れる
- Phase 0 はローカルファイル + 単一ユーザで信頼境界が明確 → 問題化しない
- T02 確定: **Connector は read-only** とすることで write-side identity 問題は当面回避
- Phase 1+ で 「Connector 自身が書込みたい需要」 が出た場合に再議論する

### 9.2 ログのローテーション戦略
- Phase 0 = テキストベースで永久保存
- 将来 GB 級になった場合の rotation / 圧縮 / 古いスレッドのコールドストレージ化を検討

### 9.3 thread_id の衝突 (Phase 1+)
- ULID 採用で衝突リスクは事実上ゼロ
- Phase 1+ で複数プロセスから thread 作成シナリオが出た場合、 採番 race は無いが file system レベルの mkdir 競合の整合性は別途検証必要

### 9.4 ~~Phase 0 実装言語~~ (T05 で Python 確定)
- **Python** (`watchdog` + `tomllib` + `claude-agent-sdk`) を採用
- 採用根拠: Claude Agent SDK の Python 版が最も成熟、 PTY 直叩き不要、 auth 継承

### 9.6 原則 8 を Stage 3 に遡及適用するか (2026-08-02 追記)
§8bis.6 のとおり、 原則 8 (外部システム連携はコアから完全分離、 Connector 層が担う) は Stage 3 では成立していない — magickit chatroom への依存が `magickit/` としてコアに同居している。 選択肢は 3 つあり、 いずれも decide していない:
- **原則 8 を Phase 0 基盤に限定された原則として明示する** (Stage 3 は別基盤 ∴ 別原則を立てる)
- **Stage 3 の magickit 依存を Connector 層へ押し出す** (大きい。 動機が現状無い)
- **原則 8 自体を改訂する** (「AI 同士の多者ループ」という軸を T02 は想定していなかった)
本書は現状を記述したのみで、 どれも選んでいない。

### 9.7 Phase 0 基盤の現況 (2026-08-02 追記)
`mindwire-watcher` (§2〜§7 の filesystem 基盤) と Stage 3 が併存しているが、 **実際の開発ループは Stage 3 側でのみ回っている**。 Phase 0 基盤を維持するか、 縮退させるか、 Stage 3 に統合するかは未決。 §2〜§7 を消していないのは、 それが今も `mindwire-watcher` の正しい仕様だからであって、 現役であることを含意しない。

### 9.5 watcher の進化方向 (Phase 1+)
Phase 0 では単一 watcher プロセス + recursive `watchdog.Observer` (W-A) を採用。 将来の進化方向として以下が想定される:
- **MCP write API への移行**: 現状の filesystem 直書きプロトコルから、 公開 MCP サーバの write tool 経由に再構築 (architecture.md §5 参照)。 これが実現すると watcher の filesystem 監視中心性が低下し、 W-A/W-B の選択は再評価対象外になる
- **multi-instance / sharding**: 流量増 or HA 動機が出た場合、 thread 集合を複数 watcher で分担。 W-A 起点ではフィルタ層を挟む形で対応
- **subscribe 型イベント API**: Connector が pull-polling から push 通知に進化したい場合、 watcher の event log を subscriber に転送する層を追加
- **OS-level 制約への対応**: 多 thread 化時に Linux inotify per-user watch limit (default 8192〜65536) や Windows ReadDirectoryChangesW recursive buffer overflow が顕在化する可能性。 Phase 1+ で thread 数が 1000 を超える運用が見えた時点で kernel param 拡張 / W-B 系への再設計 / polling fallback 強制 等を再評価
- これらはいずれも Phase 0 では YAGNI、 Phase 1+ で動機が顕在化した時点で再設計する
