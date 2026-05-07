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
schema_version: 1                                   # 必須
thread_id: 01ARZ3NDEKTSV4RRFFQ69G5FAV               # ULID (時刻ソート可、 衝突回避)
title: ""                                            # 任意。 人間 / エージェントが付ける1行サマリ
status: active                                       # active | awaiting-cc | awaiting-cai | resolved | archived
participants: [claude.ai, claude-code]              # 会話に参加する AI エージェント識別子
created_at: 2026-05-07T08:43:07Z                    # UTC ISO 8601
updated_at: 2026-05-07T08:45:22Z                    # 最終更新 UTC ISO 8601
tags: []                                             # participants 自身が付ける属性
```

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
{"schema_version":1,"ts":"2026-05-07T08:43:07Z","type":"thread.created","thread_id":"01ARZ3...","title":""}
{"schema_version":1,"ts":"2026-05-07T08:43:08Z","type":"message.received","thread_id":"01ARZ3...","seq":1,"from":"claude.ai","size_bytes":1234}
{"schema_version":1,"ts":"2026-05-07T08:43:09Z","type":"thread.status.changed","thread_id":"01ARZ3...","from_status":"active","to_status":"awaiting-cc"}
{"schema_version":1,"ts":"2026-05-07T08:43:10Z","type":"claude_code.invoke.start","thread_id":"01ARZ3...","msg_seq":1}
{"schema_version":1,"ts":"2026-05-07T08:43:42Z","type":"claude_code.invoke.end","thread_id":"01ARZ3...","msg_seq":1,"duration_ms":33000,"exit_code":0}
{"schema_version":1,"ts":"2026-05-07T08:43:42Z","type":"message.sent","thread_id":"01ARZ3...","seq":2,"from":"claude-code","size_bytes":4567}
{"schema_version":1,"ts":"2026-05-07T08:43:42Z","type":"thread.status.changed","thread_id":"01ARZ3...","from_status":"awaiting-cc","to_status":"awaiting-cai"}
```

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

### 期待される連携先 (温度感、 T02 時点)

| 連携先 | パターン | 温度 | 備考 |
|---|---|---|---|
| Magickit ChatRoom | bidirectional (将来) | 高 | 今回の発端。 ただし Connector 起点でも当面 read 中心 |
| Prismind | pull (read) | 高 | MindWire 全ログを長期知識として indexing |
| Thirdy | pull (read) | 中〜高 | AI-AI 議論ログから Spec 抽出する入力源 |
| Lexora | --- | 低 | LLM gateway、 連携動機弱い |
| CodePyxis | pull (read) | 低 | コード参照解析の入力 (将来) |

**設計上の含意**: 強い候補は pull/read 中心。 T03 で MindWire の公開 I/F は **read 系を充実、 write 系は最小限** で進める方針。

## 6. 設計判断の経緯

### 6.1 統合 (`messages/`) vs 分離 (`inbox/` `outbox/`) → 統合採用
- 双方向通信での視点反転問題 (cai inbox = cc outbox) を回避
- Magickit ChatRoom と同じ append-only + status 思想で整合
- 視覚的キュー視認性は補助コマンド (将来) で代替

### 6.2 別ファイル vs 単一 thread.md → 別ファイル採用
- アトミック性: tmp+rename パターンが効くのは独立ファイルのみ
- watcher の CREATE イベント検出が直接的 (パターン一致のみ、 差分計算不要)
- 1 メッセージ = 1 イベントの append-only ログ思想と整合

### 6.3 Thread ID: 連番 vs UUID vs ULID → ULID 採用
- ツール経由で常時アクセスする前提では人間可読性は弱い利点
- 並行生成での衝突回避 (採番 race のリスク回避)
- ULID の時刻プレフィックスで ID 単体ソート = 時刻順 (ツール実装が単純化)
- ID 単体で作成時刻判別可能 (forensics で効く)

### 6.4 外部参照 schema 内蔵 vs Connector 化 → Connector 化採用 (T02 確定)
- 当初の引き継ぎ ("双方向参照リンクは OK") を Takahito の追加指摘で refinement
- 「リンクの管理主体をコアの外に追い出す」 ことで 「MindWire は AI-AI 通信に閉じる」 世界観を厳守
- `metadata: {}` 汎用 dict も「世界観侵食を呼ぶ穴」 と判断、 設置せず
- マッピング状態は Connector が外部 KV で保持

詳細議論: `T-connector-pattern-decision` thread (msg-003 〜 msg-007 の decide で resolved)

## 7. Phase 0 / 後続フェーズへの影響

### Phase 0 (= T05) 実装スコープ
- `~/spirrow-mindwire-data/` ディレクトリ初期化 (config / new / threads / archive / logs)
- `new/` ファイル監視 → thread 作成
- `threads/<ULID>/messages/` への from-cai 書込みを検知 → claude-code 起動 (PTY 経由)
- `threads/<ULID>/messages/` への from-cc 書込み (claude-code の応答)
- `meta.yaml` の status 更新
- イベントログ append (`logs/threads/<ULID>.jsonl`)
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

## 8. 未決事項 / 将来論点 (記録のみ、 T02 では decide しない)

### 8.1 Connector 用の identity / 認証境界
- `participants` / message frontmatter `from` は単なる文字列で誰でも名乗れる
- Phase 0 はローカルファイル + 単一ユーザで信頼境界が明確 → 問題化しない
- T02 確定: **Connector は read-only** とすることで write-side identity 問題は当面回避
- Phase 1+ で 「Connector 自身が書込みたい需要」 が出た場合に再議論する

### 8.2 ログのローテーション戦略
- Phase 0 = テキストベースで永久保存
- 将来 GB 級になった場合の rotation / 圧縮 / 古いスレッドのコールドストレージ化を検討

### 8.3 thread_id の衝突 (Phase 1+)
- ULID 採用で衝突リスクは事実上ゼロ
- Phase 1+ で複数プロセスから thread 作成シナリオが出た場合、 採番 race は無いが file system レベルの mkdir 競合の整合性は別途検証必要

### 8.4 Phase 0 実装言語 (T05 で確定予定)
- Python (watchdog + tomllib + pty) を第一候補
- TypeScript (chokidar + node-pty) も候補
- T05 着手時に最終決定
