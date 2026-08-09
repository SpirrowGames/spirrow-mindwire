# spec/process — implementation-process regulations (human-facing)

This directory is the SOT for the **process** an implementer follows around a
loop turn. The loop-facing artefacts and the human-facing artefacts are split
by reader — the pointer table at the top of `CLAUDE.md §N` names both.

| ファイル | 内容 | 読み手 |
|---|---|---|
| `./obligations.yaml` | **注入される制約のみ** (implementer / naysayer) | ループ (prompt 組立時に loader が直読) |
| `./ledger.md` | read-back ledger 表 + 昇格基準・リセット条件・miss 原因分類 (旧 CLAUDE.md §N.1.5 / §N.1.6) | 人 |
| `./README.md` | 背景 (旧 §N.1.1) + trigger 定義 (旧 §N.1.2) + ADR 索引再生成手順 (旧 §N.2) + fail-open 配置規律 (旧 §N.3) + 残余 (R1 / R2 / R3) + 上 2 ファイルへの pointer | 人 / 汎用 Claude Code セッション |
| `CLAUDE.md` §N | **pointer のみ** (3 行) | 人 / 汎用 Claude Code セッション |

`CLAUDE.md §N` was reduced to a pointer intentionally: this repo's Claude Code
sessions run with `setting_sources=[]` and never load `CLAUDE.md`. Regulation
text that lives only there binds humans, not the loop — the loop-facing rules
must live in `./obligations.yaml` where the composition root actually reads
them.

---

## 提案の核 — spec read-back (旧 §N.1.1、ADR-2026-05-29-13)

`resume()` で受け取った context は **要約** であり **spec ではない**。要約は本質的に lossy で、schema の細部・enum 値・preserve semantics のような「具体性が意味を持つ」情報は要約で落ちる。Cognilens 圧縮精度の向上を期待するのではなく、ループ手順として固定する方が信頼できる。

`OBL-READBACK-ENTRY` / `OBL-READBACK-EXIT` (in `./obligations.yaml`) は本節の再定式化であり、ループに毎 turn 注入される — つまり本節の規範性は obligations.yaml で運搬される。本 README は人向けの背景説明。

**旧 §N.1.1 の rationale SOT** は ADR-2026-05-29-13 だが同 ADR は文書実体が未作成の可能性がある。そのため rationale を破棄せず、本 README に退避してある。

## 適用 trigger (旧 §N.1.2、5 件)

以下のいずれかに該当する実装では、`OBL-READBACK-ENTRY` / `OBL-READBACK-EXIT` を必ず適用する:

1. **`#1` schema 変更を含む実装** (DB schema / API スキーマ / 設定スキーマ 等)
2. **`#2` 新規 API 追加**
3. **`#3` ADR (Architecture Decision Record) 反映実装**
4. **`#4` proposer/reviewer の確定 msg がある thread の実装**
5. **`#5` 既存の確定 spec を「維持・踏襲する」と称する実装** (維持の正しさは元 spec の read-back でしか保証できない)

軽い実装 (typo 修正、test 追加のみ、依存バンプ等) は read-back 不要。

trigger id (`#1`〜`#5`) は `./ledger.md` の `trigger` 列で inline label 付きで参照される (`#3 (ADR 反映実装)`)。

## 入口/出口手順の要旨

- **入口** (`OBL-READBACK-ENTRY` = 旧 §N.1.3): `chatroom_get_thread(mode="full")` で関連 thread を全文取得 (spec msg 位置が既知なら targeted re-read でも可)。直近の合意 msg を **spec** として identify し、read-back してから実装着手。TaskCreate で「Read-back: thread T-xxx msg-NNN §K の spec を反映する」と明示。
- **出口** (`OBL-READBACK-EXIT` = 旧 §N.1.4): spec msg を再度開き、本文から要件を順に拾って突き合わせ表を作る (記憶や要約から項目を起こさない)。各要件単位を 1 行に、「反映した / 反映しなかった (理由)」を記入し PR description に含める。「機械的列挙」の意味 = 完全自動化ではなく「実装者の記憶からではなく **spec 本文を一次ソースとして拾う**」。

規範定式はループが読む `./obligations.yaml` を SOT とする (本節はそれを人向けに再説明したもの)。

---

## 派生アーティファクト再生成手順 — naysayer ADR index manifest (旧 §N.2、ADR-2026-06-04-19 N-2)

`spec/adr_index.yaml` は独立 naysayer の system prompt に毎 summon 注入される **全 ADR 索引の派生ビュー** (id + title のみ、ADR 本体は Drive)。canonical な ADR 集合は分散 (`CLAUDE.md §M` 参照 ∪ spirrow-docs `_docmap`) しており、loop host / CI には `_docmap` が無いため runtime union も **full** drift-check も不可 → **in-repo の commit 済コピーは不可避** (host-reality finding, T-naysayer-unify-impl msg-438/443)。

手書き二重管理を避けるため、本ファイルは **生成物**として扱う:

- **再生成手順 (proposer)**: ADR を追加/Accepted した時、`_docmap` がある docs host で `python scripts/gen_adr_index.py --docmap <spirrow-docs/_docmap.yaml>` を実行し `spec/adr_index.yaml` を再生成・commit する (手編集しない)。
- **CI の役割**: `_docmap` が CI に無いので **full** drift-check (docs-only の architecture ADR まで照合) は不可。ただし CLAUDE.md は CI に在るので CI は (a) commit 済 manifest が **parse でき well-formed** (`test_real_in_repo_manifest_loads_and_is_well_formed`) と (b) **partial drift-check** = §M 参照 ADR が manifest の部分集合であること (`test_section_m_adrs_are_a_subset_of_the_manifest`、identity ADR を §M に足して再生成を忘れたケースを捕捉、Tier B msg-448) を検証する。
- `_docmap` schema は spirrow-docs 側が SOT で本 host から不可視のため、gen-script の `_docmap` reader は schema-tolerant (初回実行時に実 `_docmap` と突き合わせ確認)。

---

## fail-open の宣言先を先に決める (旧 §N.3、2026-08-02)

fail-open を設計するとき、**「degradation を宣言する」ことで足りたと判断しない。宣言の置き場所を先に決める。**

根拠 (実測): 2026-08-02、naysayer の ADR 索引ローダは設計どおり正しく動いていた — クラッシュせず `ADR index — UNAVAILABLE` を明示していた。それでも **5 週間気づかれなかった**。宣言が review artifact 末尾の散文という、誰も grep しない場所に落ちていたため。設計は正しく、**置き場所だけで沈黙の失敗になった** (PR #120)。同日、同じ形が 4 件出た: exit 0 のまま 0 ラウンドで空振りする conductor / 未宣言 workflow / スクリプト直書きの sweep リスト / prompt にしか存在しない verdict 制約。

**残すべき区別は狭い。** 「一度の人手確認ではなく毎 run 機構で確認する」は別種のより自明な洞察で、ループは自力で到達する (Bohr が本規約なしで `T-ci-scheduled-workflows-chronic-red` msg-2095 で同じ結論を出した)。**無料で手に入らないのは「正しく実装され正しく宣言している fail-open でも不可視でありうる」の方**。

適用: 「これを誰が、いつ読むか」を問う。真実を含むだけの artifact より、**読み手のいる経路** (通知 / CI failure / 人が開くファイル) を選ぶ。報告が文書なら限界は**冒頭**に置く (脚注ではなく) — `T-slope-extension-dead-mode` msg-2111 が監査報告の不完全性を冒頭要件にしたのはこの理由。

**`spec/NAYSAYER_PRINCIPLES.md` には意図的に足していない**: あの SOT は全 naysayer 呼び出しに逐語注入され、短いことで機能する。既知 5 件は個別に機構で塞がれており、ループは隣接する推論に独立到達できることが実測されている ∴ 原則リストを薄めるコストに見合わない。

**併せて置き場所の注意**: `CLAUDE.md` に書いた規約が縛るのは **人間だけ**である。implementer は `setting_sources=[]` (SDK 隔離、credential 面の対策) で走り `CLAUDE.md` を読まない。naysayer の system prompt も preamble + role + ADR 索引 + handoff で本書を含まない。**ループに効かせたい規約は、ループが実際に読む場所 (`./obligations.yaml`) に置くこと** — 本規律を再形式化したのが `OBL-READBACK-*` であり、そのために本 README の pointer から `./obligations.yaml` が SOT である旨を明示している。

---

## ループ可読な obligation は `./obligations.yaml` に置け (旧 §N.4、2026-08-09)

ループのエージェント (implementer / naysayer) に効かせる prompt 節は、**必ず `./obligations.yaml` に置くこと**。Python ソースの文字列リテラルに直書きしない。既存の直書きを見つけたら本 manifest に move (copy ではない — ソースから削除し、注入経路で描画する) して、`origin.moved_from` と `origin.original_length` を記録すること。

理由: 上節 (旧 §N.3) の教訓 (「正しく実装され正しく宣言している fail-open でも不可視でありうる」) は obligation にも当てはまる。同じ意味の節が adapter 側に散らばると、単一の PR ではもう全体を審査できず、レビュー時に見えていたはずの規約が知らぬ間にドリフトする (2026-08-09、voxelworld PR #182 で ADR-2026-05-29-13 の read-back 義務が「read できない ADR に対して何をするか」の未定義分岐で沈黙。義務は書かれていたが、`OBL-READBACK-*` として名前がついておらず抽出できなかった)。

manifest の書式・読込 API・不変条件 (verbatim 長さ保持 = canary ②″) は `src/spirrow_mindwire/obligations.py` の docstring と `./obligations.yaml` の冒頭コメントに定義がある。composition root (`loop_runner._build_dispatcher`) が startup で 1 度だけ読み込み、失敗時は `SystemExit` で fail-closed。canary は `tests/test_obligations.py` に 3 本 (① id 網羅 / ②′ 描画注入 / ②″ 長さ保持) + 本 README への pointer 存在 grep — いずれも skip 条件なし。

`moved_from` は Python literal (`path::LITERAL_NAME`) と doc section (`path::§HEADING`) の両方を受ける opaque string。`OBL-DECLARE-UNREADABLE` / `OBL-VERDICT-CONSTRAINT` は前者の例、`OBL-READBACK-ENTRY` / `OBL-READBACK-EXIT` は後者の例 (旧 `CLAUDE.md §N.1.3 / §N.1.4` からの移設)。

---

## 残余 (v1 に載せなかった義務・洞察) — R1 / R2 / R3

Tier-C GO (msg-737 / msg-739) が v1 の scope を「4 obligations の manifest 化」に絞ったため、周辺で議論された 3 項目は本 PR に載せていない。**黙って落としたのではない**ことを記名する場所として本節を用意する。

### R1 — 「fail-open の宣言先を先に決める」を obligation 化する
- **性質**: 設計時の義務。宛先は degradation を設計する者 = ループ内では実質 proposer。
- **v1 非搭載の理由**: proposer に obligation を配送する脚 (D-7) が現時点で無い。器 (`obligations.yaml`) は role = implementer / naysayer 二値のみを受け付ける (`_MANIFEST_ROLES`)。配送脚を持たないうちに義務だけを増やしても、それ自体が「宣言が読まれない fail-open」になる。
- **follow-up**: proposer 向けの注入経路が敷かれた時点で `OBL-*` として named 化する。

### R2 — 「限界は冒頭に置く (脚注ではなく)」を配置規律として明文化
- **性質**: 配置規律。implementer にも適用可能 (「読めなかった msg があります」は reply の最終段落ではなく冒頭に置く)。
- **v1 非搭載の理由**: 現時点でどの prompt にも実在しない文言ゆえ、載せると v1 が「移設 only」でなくなり scope 逸脱になる。**意図的な defer**。
- **follow-up task**: `T-r2-limits-at-head` (立てる時に obligation body を新規起草 = net-new formulation で `origin` を持たない)。

### R3 — 「正しく実装され正しく宣言された fail-open でも不可視でありうる」
- **性質**: 洞察であって義務ではない。
- **v1 非搭載の理由**: `obligations.yaml` は **義務を運ぶ器**であって根拠を運ぶ器ではない。洞察は本 README の旧 §N.3 節で説明されており、それ自体が正しい配置。**非搭載が正しい設計**。
