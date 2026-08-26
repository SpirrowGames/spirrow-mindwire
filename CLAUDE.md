# CLAUDE.md — spirrow-mindwire

このファイルは Claude Code が spirrow-mindwire プロジェクトで作業する際に読み込まれる context です。

**§N はプロセス規約 pointer (SOT = `spec/process/`)、§M は規範参照 (ADR が SOT)** — この境界を CLAUDE.md 内で再分散させないこと。プロセス規約本文と規範定義を無分類で本書に混ぜると F-04 / F-07 系の分散症状を CLAUDE.md 内部で再発させる。

---

## §N. メタプロセス (実装プロセス規約 — pointer only)

実装プロセス規約 (spec read-back / obligation 配置 / fail-open 配置 / read-back ledger / ADR 索引再生成) の本文は **`spec/process/` へ移した**。ここは pointer だけを置け (§N は分散再発の入口になりやすいので、本文を戻さないこと)。

- **`spec/process/obligations.yaml`** — ループに注入される規約 (implementer / naysayer)。**SOT はここ**。手を入れる時は必ずここに置け。
- **`spec/process/README.md`** — 上記の背景、trigger 定義、ADR 索引再生成手順、fail-open 配置規律、v1 非搭載 (R1/R2/R3) の残余。人向け。
- **`spec/process/ledger.md`** — read-back 適用ログ + 段階昇格・リセット・miss 原因分類。人向け。

---

## §M. role / identity の規範定義 (ADR 参照のみ — ADR が SOT)

本セクションは規範定義の **参照** のみ。実体の SOT は ADR にあり、CLAUDE.md には規範定義そのものを書かない (T29 SOT 分離原則)。

| ADR | 内容 | thread |
|---|---|---|
| ADR-2026-05-27-09 (T28) | identity 4 レイヤーモデル (identity_name / independence_class / role / 稼働形態 (embodiment) の直交分離)。independence_class レイヤーは ADR-2026-05-31-15 で二値→グラデーション補強。 | T-T28-author-role-identity |
| ADR-2026-05-29-10 (T29) | role registry (proposer / reviewer / implementer / integrator / dogfooder / naysayer / human の 7 role 定義、closeable_roles、close_reason enum) | T-T29-role-registry |
| ADR-2026-05-29-11 | author/identity partition キー正規化 (lowercase + 区切り正規化 + 単射性 gate + strict-by-default) | T-author-partition-key-normalization |
| ADR-2026-05-29-12 | embodiment 自己申告値化 (ADR-09 D-5 拡張・実装、5 API optional 受け口、enum、状態遷移 msg 必須化、human 例外、response-side omit) | T-embodiment-self-declared |
| ADR-2026-05-29-13 | 実装着手前 spec read-back チェックリスト (実装 = `spec/process/obligations.yaml` の `OBL-READBACK-ENTRY` / `OBL-READBACK-EXIT`、背景 = `spec/process/README.md`) | T-implementer-spec-readback-checklist |
| ADR-2026-05-31-15 | independence-class グラデーション化 + 「2 協調 1 独立」配置 (ADR-09/T28 の independence_class レイヤーを二値→グラデーションに補強、別訓練分布 naysayer の規範根拠、N-1/C-2 トレードオフ併記、§0 順序) | T-T15-poc-h-phase1-kickoff |

注: ADR-2026-05-31-14 (T15 ガワ方式撤回。旧 §M 参照番号 2026-05-27-08 [この番号の ADR は実体化されず欠番、置換後の ADR-2026-05-31-14 に統合] を置換) は identity 規範定義ではないため §M 対象外 (UI 自動化手段選定 ADR)。develop repo に実体あり。
注: 上表 ADR-09 (T28) / ADR-10〜13 は §M 参照名のみで develop repo に文書実体が未作成のものを含む (実体化は別タスク = §M 棚卸し PR、ADR-2026-05-31-15 と ADR-2026-05-31-14 の 2 本のみ本バッチで実体化済)。

ADR 本体は spirrow-docs リポジトリで管理。本 CLAUDE.md からは参照のみで、ADR の規範定義を本ファイルに転載しないこと (F-04 / F-07 系の分散症状を回避)。

---

## 境界の明示

- **§N (メタプロセス / 実装プロセス規約)**: 本書は **pointer only**。SOT は `spec/process/` (obligations.yaml が SOT for the loop、README.md / ledger.md が SOT for humans)
- **§M (role / identity の規範定義)**: ADR が SOT、本書は参照のみ

CLAUDE.md 内で §N と §M を無分類で混ぜない。プロセス規約本文と規範定義は本書に書かない (プロセス規約は `spec/process/` へ、規範定義は ADR へ)。後続の追記は §N pointer 追加 または §M のいずれかに必ず位置づけ、無分類の散文を CLAUDE.md ルートに置かないこと。

なお規律の趣旨は **root 直書きの散文を禁止する** ことであり、§N/§M の二択を絶対視するものではない。将来 §N (プロセス規約 pointer) でも §M (規範参照) でもない第三カテゴリ (例: ビルド手順、テスト規約) が必要になった場合は §O 以降の新セクションを立ててよい。手段 (セクション分類) と目的 (root 直書き禁止 = 分散症状防止) を混同しないこと。
