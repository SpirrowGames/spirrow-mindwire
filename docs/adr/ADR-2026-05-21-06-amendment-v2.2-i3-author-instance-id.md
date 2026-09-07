# ADR-2026-05-21-06 改訂メモ v2.2: I3 author = instance_id

- **Status**: Draft（ローカル develop。Takahito GO 後に Drive 反映 = ADR-07 §2.5 / Tier C）
- **Date**: 2026-05-24
- **Scope**: ADR-2026-05-21-06（Interface Contract / Ports）の §4 I3 および §2.2 / §2.4 関連箇所の改訂
- **Author**: main (claude.ai)
- **Relates to**: ADR-2026-05-24-08（Instance を一次概念とする識別モデル）
- **改訂種別**: Amendment（v2.1 → v2.2）。本メモは Drive 反映時に ADR-06 本体へマージする差分。

---

## 1. 改訂理由

ADR-2026-05-24-08 で識別の一次概念を Role から Instance に引き上げる決定をした。これに伴い、chatroom 上の reply author を **bare role 文字列から `instance_id` に変更**する必要がある。これは ADR-06 §4 の不変条件 I3、および §2.2（`SessionHandle`）/ §2.4（`ReplyDraft` の author 補完規約）に触れるため、ADR-06 本体の改訂として記録する。

現状（v2.1）の問題点：`dispatcher._handle_reply` が `author = session.role`（`ReplyDraft` 完成時に dispatcher が `author = session.role` を補完）しているため、複数インスタンスを立てても chatroom 上は全て同じ role 名（例 `implementer`）で post され、個体識別ができない。共通認識3の「mindwire はインスタンスを個体識別する必要がある」と正面から矛盾する。

---

## 2. 改訂内容

### 2.1 §2.2 SessionHandle（フィールド追加）

`SessionHandle` に `instance_id: str`（個体の安定ラベル）を追加する。詳細・根拠は ADR-2026-05-24-08 §2.1。`session_id`（per-spawn ULID）との区別：

- `session_id`: spawn 毎に変わる ULID。再 spawn で変化。
- `instance_id`: 個体の安定 ID。再 spawn を跨いで同一個体を指す。author の SOT。

`eq=False`（I2: identity equality）は維持。フィールド追加は equality セマンティクスに影響しない。

### 2.2 §4 I3 改訂（author 規約）

**I3（v2.1）**: reply の author は session の role 文字列。

**I3（v2.2, 改訂後）**: reply の author は session の `instance_id`。dispatcher は `ReplyDraft` 完成時に `author = session.instance_id` を補完する（従来 `author = session.role` だった箇所）。

- 1ロール1インスタンス運用（Phase 1 近期スコープ）では `instance_id = "{role}-1"`（例 `implementer-1`）が author になる。
- 表示規約: chatroom 上の表記は `instance_id` をそのまま用いる。`#` 区切り表記（`implementer#1`）を使う場合は表示層の変換とし、SOT は `instance_id`（`implementer-1`）に一本化する。実装は `instance_id` 文字列をそのまま author とする（表示変換は chatroom 側の責務、mindwire は無加工）。

### 2.3 §2.4 ReplyDraft（author 補完元の変更）

`ReplyDraft` 自体は引き続き author を持たない（adapter は chatroom を知らない、I1 知識境界）。dispatcher が補完する値の源泉が `session.role` → `session.instance_id` に変わる。`ReplyDraft` の型・フィールドは無変更。

### 2.4 legacy 互換

bare role の既存 post（v2.1 以前に投稿済みのもの）は**残置**する（既存規約どおり、historical として扱う）。改訂は新規 post の author 補完にのみ適用。

---

## 3. 影響範囲

| 箇所 | 変更 |
|---|---|
| §2.2 SessionHandle | `instance_id` フィールド追加（ADR-08 と対） |
| §2.4 ReplyDraft | 型無変更。dispatcher の補完元が role → instance_id |
| §4 I3 | author = instance_id に改訂 |
| `dispatcher/core.py` `_handle_reply` | `author=handle.role` → `author=handle.instance_id` |
| `dispatcher/gateway.py` / `magickit/gateway.py` | `post_reply(author: Role)` → `author: str`（`author` は instance_id 文字列） |
| `dispatcher/event_log.py` | `reply.sent` / `delivery.failed` の `author` field を `handle.role.value` → `handle.instance_id`（anchor #6 統一を instance_id 側で維持） |
| 各 adapter `deliver_event` self-filter (Gap-2 (b)) | `payload.author == own_role.value` → `== handle.instance_id`（author 改名に追随、self-reply backstop を維持） |

> **実装知見（PR #69 / #70）**: 当初「`_handle_reply` 実質1行」と見積もったが、`author` は `Role` 型で gateway Protocol / magickit gateway / event_log / self-filter / test fake まで貫通していた。実体は (a) chatroom post 経路（T25, #69）と (b) event_log の identity SOT 統一（T26, #70）の2段で、上表の範囲に波及する。**event_log は当初本改訂のスコープ外だったが、anchor #6（chatroom-author == event-log-author）を instance_id 側で揃えるため T26 で取り込んだ**（main PR #69 review 決定）。

その他の不変条件（I1 / I2 / I4 / I5 / I7 / I8 / I9）は無変更。特に I2（identity equality）は `instance_id` 追加後も維持される。self-filter は I3 改名に追随して instance_id 基準に更新（挙動同等、むしろ Phase 2 で sibling instance を誤除去しない方向に正確化）。

---

## 4. Implementation Notes

- `dispatcher/core.py`: `_handle_reply` 内の author 補完を `handle.instance_id` に変更。これが本改訂の実体（1行）。
- `SessionHandle.instance_id` の追加自体は ADR-2026-05-24-08 の T-task で実施（本改訂はそれに依存）。
- 反映順序: ADR-08（型 + 配線）→ 本改訂（author 補完の切替）。author 切替は `SessionHandle.instance_id` が存在して初めて機能するため、ADR-08 の型変更が先行する。
- テスト: 1ロール1インスタンスで author が `implementer-1` 等になることを確認。複数インスタンスの author 分離テストは Phase 2（並列実装と同時）。

---

## 5. Drive 反映時の作業

本メモは ADR-06 本体（Drive doc ID は `_docmap.yaml` 参照）への **in-place 改訂**として反映する。Drive doc は markdown 管理のため `smart_update_document`（full replace）で版を更新可能（Prismind PR#2 f4c85ac 稼働中、mimeType 分岐対応済）。反映時：

- ADR-06 本体の §2.2 / §2.4 / §4 I3 を上記改訂内容で更新。
- Metadata の版表記を v2.1 → v2.2 に。改訂履歴に本メモの要約（I3 author = instance_id, ADR-08 と対）を追記。
- `_docmap.yaml` の該当エントリの `last_reflected` を更新。
