# CLAUDE.md — spirrow-mindwire

このファイルは Claude Code が spirrow-mindwire プロジェクトで作業する際に読み込まれる context です。

**§N はプロセス規約 (本書 SOT)、§M は規範参照 (ADR が SOT)** — この境界を CLAUDE.md 内で再分散させないこと。プロセス規約と規範定義を無分類で混ぜると F-04 / F-07 系の分散症状を CLAUDE.md 内部で再発させる。

---

## §N. メタプロセス (実装プロセス規約 — 本書 SOT)

### §N.1 spec read-back チェックリスト (ADR-2026-05-29-13)

実装着手前と完了時の二点で、resume() / begin_task() の要約に頼らず spec msg 本文を一次ソースとして読み返すことを構造的に要求する。F-09 (spirrow-magickit、resume→実装着手→spec 逸脱) を再発させないための、注意力依存→構造化の措置。

#### §N.1.1 提案の核

resume() で受け取った context は **要約** であり **spec ではない**。要約は本質的に lossy で、schema の細部・enum 値・preserve semantics のような「具体性が意味を持つ」情報は要約で落ちる。Cognilens 圧縮精度の向上を期待するのではなく、ループ手順として固定する方が信頼できる。

#### §N.1.2 適用 trigger (5 件)

以下のいずれかに該当する実装では、本チェックリストを必ず適用する:

1. **schema 変更を含む実装** (DB schema / API スキーマ / 設定スキーマ 等)
2. **新規 API 追加**
3. **ADR (Architecture Decision Record) 反映実装**
4. **proposer/reviewer の確定 msg がある thread の実装**
5. **既存の確定 spec を「維持・踏襲する」と称する実装** (維持の正しさは元 spec の read-back でしか保証できない)

軽い実装 (typo 修正、test 追加のみ、依存バンプ等) は read-back 不要。

#### §N.1.3 入口チェック (実装着手前)

1. `chatroom_get_thread(mode="full")` で関連 thread を全文取得
2. 直近の合意 msg (proposer の確定 msg、decide msg 等) を **spec** として identify
3. その spec を read-back してから実装着手
4. TaskCreate で「Read-back: thread T-xxx msg-NNN §K の spec を反映する」と明示

#### §N.1.4 出口チェック (実装完了時)

**spec msg を再度開き、本文から要件を順に拾って突き合わせ表を作る (記憶や要約から項目を起こさない)**。

手順:

1. spec msg の見出し / 箇条書き / 表 / コードブロックを **目視走査**
2. 各「要件単位」(例: schema 1 フィールド、API 1 endpoint、enum 1 値) を 1 行に対応
3. 各行に「反映した / 反映しなかった (理由)」を記入
4. 突き合わせ表を **PR description または complete_task notes に含める**
5. 突き合わせ表が無い場合は完了扱いしない

**「機械的列挙」の意味**: 完全自動化 (parser) を要求するものではない。「実装者の記憶からではなく **spec 本文を一次ソースとして拾う**」という意味での機械性。将来 spec msg 構造化 (template 化) で自動列挙化する余地はあるが、本 ADR 段階では人手手順として固定。

#### §N.1.5 段階昇格と Hook 化判断

本規約は段階昇格方式で運用する。機械化で意図 (「要約 ≠ spec」の自覚) が形骸化するリスクを避けるため、文書化から始めて必要時に hook 化する順序を採る:

- **(a) 現段階**: spirrow-mindwire `CLAUDE.md` (本書) に明文化、本プロジェクト内で運用
- **(b) 次段階**: Claude Code global `CLAUDE.md` への昇格 (cross-project 適用)
- **(c) 最終段階**: Claude Code Hook 機能で自動 prompt 化 (必要時のみ、最終手段)

#### §N.1.6 spec read-back 適用ログ (ledger)

trigger 該当 PR ごとに以下を記録する。記録の所在を CLAUDE.md 末尾に固定することで、昇格判断の根拠がコードベース上に残る (F-07「根拠がログに残らない」症状を本規約自身が再生産しないため)。

| PR | trigger | 入口 read-back 対象 msg | 出口チェック結果 | miss 原因分類 |
|---|---|---|---|---|
| _(初期化、エントリは PR 完了ごとに追記)_ | — | — | — | — |

**出口チェック結果** (3 値):

- `clean`: 全要件項目が反映済
- `gap-detected`: 突き合わせで差分発見、修正してマージ
- `miss-after-merge`: マージ後に逸脱発覚

**miss 原因分類** (miss-after-merge 発生時のみ、1 語添える):

- `[散文埋め込み要件]`: spec 本文の散文に埋め込まれた要件を目視走査で拾い漏れ
- `[trigger 判定漏れ]`: 該当 trigger に気づかず read-back 自体をスキップ
- `[出口表の項目欠落]`: 出口突き合わせ表の作成時に項目を漏らした
- `[spec 解釈差異]`: 要件の解釈が proposer と implementer で食い違った

**昇格基準**:

- **(a) → (b) 昇格**: `miss-after-merge` が **直近 5 回連続 trigger 該当 PR で 0 件**、かつ cross-project 適用の必要が確認できた時点
- **(c) Hook 昇格**: `miss-after-merge` 累積、または原因分類で特定カテゴリ累積 (例: `[散文埋め込み要件]` が累積 → spec テンプレート構造化 or hook へ)

**リセット条件**: 5 回達成前に miss-after-merge が出たらカウンタをゼロリセットして再カウント。ledger に「リセット: 累積X→0」を 1 行追加 (履歴は消さない、累積カウンタのみリセット)。

理由: miss が出たのは「まだ習慣化していない」証拠なので振り出し。累積で見ると「4 回 clean → 1 回 miss → あと 1 回 clean で 5 回」のような解釈ブレが起きる。ゼロリセットなら「直近 5 回連続 clean」が一貫した意味を持つ。

---

## §M. role / identity の規範定義 (ADR 参照のみ — ADR が SOT)

本セクションは規範定義の **参照** のみ。実体の SOT は ADR にあり、CLAUDE.md には規範定義そのものを書かない (T29 SOT 分離原則)。

| ADR | 内容 | thread |
|---|---|---|
| ADR-2026-05-27-09 (T28) | identity 4 レイヤーモデル (identity_name / independence_class / role / 稼働形態 (embodiment) の直交分離) | T-T28-author-role-identity |
| ADR-2026-05-29-10 (T29) | role registry (proposer / reviewer / implementer / integrator / dogfooder / naysayer / human の 7 role 定義、closeable_roles、close_reason enum) | T-T29-role-registry |
| ADR-2026-05-29-11 | author/identity partition キー正規化 (lowercase + 区切り正規化 + 単射性 gate + strict-by-default) | T-author-partition-key-normalization |
| ADR-2026-05-29-12 | embodiment 自己申告値化 (ADR-09 D-5 拡張・実装、5 API optional 受け口、enum、状態遷移 msg 必須化、human 例外、response-side omit) | T-embodiment-self-declared |
| ADR-2026-05-29-13 | 実装着手前 spec read-back チェックリスト (本ファイル §N.1 の SOT) | T-implementer-spec-readback-checklist |

注: ADR-2026-05-27-08 (T15 ガワ方式) は identity 規範定義ではないため §M 対象外 (UI 自動化手段選定 ADR)。

ADR 本体は spirrow-docs リポジトリで管理。本 CLAUDE.md からは参照のみで、ADR の規範定義を本ファイルに転載しないこと (F-04 / F-07 系の分散症状を回避)。

---

## 境界の明示

- **§N (メタプロセス / 実装プロセス規約)**: 本書が SOT
- **§M (role / identity の規範定義)**: ADR が SOT、本書は参照のみ

CLAUDE.md 内で §N と §M を無分類で混ぜない。プロセス規約と規範定義は別レイヤーで管理する。後続の追記は §N または §M のいずれかに必ず位置づけ、無分類の散文を CLAUDE.md ルートに置かないこと。
