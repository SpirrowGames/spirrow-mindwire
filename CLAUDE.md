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
   - ただし spec として identify すべき合意 msg が既に判明している場合 (例: 確定済 ADR の decide msg ID が既知) は、targeted message ID の再読でも入口 read-back を満たす。`mode="full"` 必須は spec msg 位置が不明なときの探索手段であり、本 ADR の趣旨「spec 本文を一次ソースで読む」は targeted re-read でも達成可能 (full mode の token コスト回避目的の最適化として許容)。
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

| PR # (repo) | trigger | 入口 read-back 対象 msg | 出口チェック結果 | miss 原因分類 |
|---|---|---|---|---|
| #75 (spirrow-mindwire) | #3 (ADR 反映実装) | T-implementer-spec-readback-checklist msg-326 | `clean` | — |
| msg-327 着手前調査 (spirrow-mindwire) | #3 (ADR 反映実装、ADR-11/12 着手前) | T-author-partition-key-normalization msg-324 / T-embodiment-self-declared msg-325 | `gap-detected` (入口検出 2 件、`gap-detected-pre-impl` 相当: Cognilens 一本化 no-op + Magickit 先行反映済) | — |
| msg-330 close-decide observation (spirrow-mindwire) | #3 派生 (ADR 反映実装の自己批判) | T-adr-11-12-pre-impl-investigation msg-329 (Bohr proposer 確定) | `self-improvement-note` (出口 miss なし、入口 read-back 対象に「自身の過去セッション完了状態」を追加すべきとの観察) | — |
| T15 msg-342 ack observation (spirrow-mindwire) | #3 派生 (ADR-08 PoC-H 実機検証 着手段階) | T-T15-poc-h-phase1-kickoff msg-293 / msg-302 (sg-tomtebo-01 上に本人常用 Chrome session がある暗黙前提) | `self-improvement-note` (出口 miss なし、入口 read-back 対象に「実装環境前提も Takahito に事前確認」を追加すべきとの観察、msg-338 で前提崩れ判明) | — |
| T15 msg-346 checkpoint observation (spirrow-mindwire) | #3 派生 (ADR 反映実装の前提状態確認) | (GitHub 状態 — msg 外) PR #75 review/merge 状態 (resume 時の再 fetch 未実施) | `self-improvement-note` (出口 miss なし、入口 read-back 対象に「直前 checkpoint 後の GitHub PR review/merge 状況も再 fetch」を追加すべきとの観察、本セッション resume で merged 後判明) | — |
| T15 msg-346 ack observation (spirrow-mindwire) | #3 派生 (ADR-08 PoC-H 実機検証、認証4軸 (d) Vaultwarden 整合評価中) | T-T15-poc-h-phase1-kickoff msg-302 §3 (b) (Vaultwarden bw CLI による memory-only 取り出しの暗黙前提) | `self-improvement-note` (出口 miss なし、bw CLI 2026.5.0 で `BW_CLIENTID`/`BW_CLIENTSECRET` env var が廃止され `bw login --apikey` が interactive prompt only に変更、automation hostility の環境前提変化として記録、入口 read-back 対象に「依存外部ツールの version/仕様前提」を加えるプロセス改善含意) | — |
| Stage3 loop-runner (`mindwire-loop`) PR (spirrow-mindwire) | #3, #4 (ADR-2026-05-21-06 §4/§7 + ADR-07 gating + ADR-15 naysayer 配置の反映 + proposer 確定 msg ある thread) | T-stage3-loop-wiring msg-381 (A〜E operative decide) / msg-385 (topology = Option A) | `gap-detected-pre-impl` (入口 read-back (msg-384) で msg-383 §2「単一 thread turn-taking」が T16 docstring + `orchestrator.py` の既存 topology 決定「1 auto-reply role / thread + orchestrator bridge」と矛盾と検出 → Bohr が msg-385 で §2 撤回・Option A 採用。proposer の decide も spec として read-back 対象になる実証。実装中に registry first-qualified の proposer↔implementer 曖昧性も検出し text-only `Stage3ProposerAdapter` で in-scope 解消。実装後の出口突き合わせは clean: msg-385 §2 In-scope 全項目反映 + §5 不変条件 (1 auto-reply role/thread, core 無改造, allowlist ロード, no auto merge-to-main) 充足) | — |
| T-naysayer-ci-gate 着手前調査 (spirrow-mindwire) | #3 派生 (ADR-2026-06-03-16 起案前の現状調査) | T-naysayer-ci-gate msg-387 §6 (naysayer CI-blind 一次確認) | `gap-detected` (既存 naysayer が CI 状態を一切読まず approve-while-red を出しうる穴を検出 → ADR-16 起案の起点。#82 が CI 赤のまま review に向かい得た live example) | — |
| #85 (T30, spirrow-mindwire) | #3 (ADR-2026-06-03-16 naysayer CI-gate 反映実装) | ADR-16 §2/§4 + T-naysayer-ci-gate decide msg-389 / 訂正 msg-390 (Heisenberg 入口/出口 read-back = T-T30-naysayer-ci-gate-impl msg-393) | `clean` (proposer spec-review PASS = T-T30-…-impl msg-396、実装が ADR-16 L1/L2/L4 と一致、PR #85 merged `9afa37d`) | — |
| #85 rebase/発火 着手前 (Bohr session, spirrow-mindwire) | #3 派生 (PR を回す前の base 鮮度確認漏れ) | (GitHub 状態 — msg 外) origin/main の #82/#83/#84 マージ未 fetch (T-T30-…-impl msg-394) | `self-improvement-note` (出口 miss なし。checkout 中ブランチ作業で origin/main の進みを再 fetch せず stale #85 ブランチから naysayer を手書き起動しかけた。入口 read-back 対象に「作業前に origin/main 再 fetch / PR 起票・review 前に base 鮮度=rebase 確認」を加える候補。implementer 側も「PR 起票前 rebase/base 鮮度確認」を入口に足す候補) | — |
| #85 naysayer 発火試行 (Bohr session, spirrow-mindwire) | #3 派生 (outward アクション実行可否の前提確認) | (host 状態 — msg 外) magickit `:8117` localhost-bind の dev PC 到達性 (T-T30-…-impl msg-397) | `self-improvement-note` (出口 miss なし。outward アクション (naysayer 発火) を宣言してから実行 host の到達性を実測し不可と判明。入口 read-back 対象に「outward 宣言前に実行 host の reachability を実測」を加える候補。関連: #85 は Tier B 独立 naysay 未経由で CI 緑 + Tier C override merge、恒久解 = ADR-2026-06-03-17 / T-naysayer-design-participation) | — |
| #10 (spirrow-magickit) | #3 (ADR-2026-06-04-19 D-5 反映 = human owner-override 補修) + API 挙動変更 | ADR-19 D-5 + 改修依頼 prompt + #9 gate 実装 + close owner-check 現行コード (入口 read-back で **owner-check は Magickit でなく Conclair 在**と判明 → 2 repo 化) | `clean` (PR #10 body に要件×反映 突合表、unit 387 passed、CI 緑 3.11/3.12、実機検証 PASS = T-owner-override-probe msg-428) | — |
| #6 (spirrow-conclair) | #3 (ADR-2026-06-04-19 D-5 反映) + API 挙動変更 (close 不変条件) | close owner-check 実体 = `permissions.assert_owner_can_close` / `integrity.assert_closes_thread_rule` Invariant 3 + ADR-19 D-5 | `clean` (`owner_override` で **ownership 句のみ**緩和 = decide-type / closes_thread==thread_id 不変・identity 判定せず、integration/unit テスト追加、実機検証 PASS = msg-428) | — |
| #91 (spirrow-mindwire) | #3 (ADR-2026-06-04-19 N-2 反映実装 = design-time naysayer への決定論 ADR 索引注入) | T-naysayer-unify-impl msg-434 §① / decide msg-438 (in-repo COMPLETE union manifest) / Tier B msg-442 | `gap-detected` (**三層 review が別種の欠陥を merge 前に捕捉した good case**: ① proposer 相互 review (msg-436) が索引 source=CLAUDE.md §M の subset 不完全 (naysayer/arch ADR 16-19 欠落) を検出 → host-reality (loop host に `_docmap` 無) を経て in-repo union manifest + gen-script へ修正。② 独立 Tier B naysayer (Gemini, msg-442) が **`yaml.YAMLError` 未捕捉クラッシュ (F2、proposer PASS msg-440 が見落とし) + dual-management-by-promise (F1)** を検出 → fail-open + gen-script 同梱で修正。全て merge 前解消・Tier B 再 review APPROVE (msg-448)。proposer 教訓 = docstring の claim を実装の例外捕捉範囲と逐一突合する。非ブロッカーの partial-§M-drift-check は fast-follow) | — |

**出口チェック結果** (4 値):

- `clean`: 全要件項目が反映済
- `gap-detected`: 突き合わせで差分発見、修正してマージ (入口段階で検出した場合は `gap-detected-pre-impl` と注釈)
- `miss-after-merge`: マージ後に逸脱発覚
- `self-improvement-note`: 出口 miss はないが、**入口 read-back の対象 / 出口表項目の網羅性 / trigger 判定 / 実装環境前提** 等について将来的に改善すべき観察を記録。累積 → 規律 / spec テンプレートの構造化判断 / 入口 read-back 対象の拡張 等の打ち手に繋げる。`miss-after-merge` ではないので昇格カウンタ (直近 5 回連続 clean) のリセット条件には該当しない。

**miss 原因分類** (miss-after-merge 発生時のみ、1 語添える):

- `[散文埋め込み要件]`: spec 本文の散文に埋め込まれた要件を目視走査で拾い漏れ
- `[trigger 判定漏れ]`: 該当 trigger に気づかず read-back 自体をスキップ
- `[出口表の項目欠落]`: 出口突き合わせ表の作成時に項目を漏らした
- `[spec 解釈差異]`: 要件の解釈が proposer と implementer で食い違った (implementer 側の read-back 精度の問題)
- `[spec 欠陥]`: spec 本文に要件が欠落/矛盾していた、implementer 側の read-back は正しかった (proposer 側の spec 起案品質の問題)

`[spec 解釈差異]` と `[spec 欠陥]` は **責任の所在**を切り分ける軸: 前者は implementer 規律の問題、後者は proposer の spec 起案品質の問題。この分類で §N.1.5 (c) Hook 昇格基準の「特定カテゴリ累積」判定を、implementer 規律改善 (前者累積) と spec テンプレート構造化 (後者累積) を異なる打ち手に振り分けられる。

**昇格基準**:

- **(a) → (b) 昇格**: `miss-after-merge` が **直近 5 回連続 trigger 該当 PR で 0 件**、かつ cross-project 適用の必要が確認できた時点
- **(c) Hook 昇格**: `miss-after-merge` 累積、または原因分類で特定カテゴリ累積 (例: `[散文埋め込み要件]` が累積 → spec テンプレート構造化 or hook へ)

**リセット条件**: 5 回達成前に miss-after-merge が出たらカウンタをゼロリセットして再カウント。ledger に「リセット: 累積X→0」を 1 行追加 (履歴は消さない、累積カウンタのみリセット)。

理由: miss が出たのは「まだ習慣化していない」証拠なので振り出し。累積で見ると「4 回 clean → 1 回 miss → あと 1 回 clean で 5 回」のような解釈ブレが起きる。ゼロリセットなら「直近 5 回連続 clean」が一貫した意味を持つ。

**複数 trigger 該当時の記載方針**: 複数 trigger に該当する場合は両方を `trigger` 欄に列挙し、最も支配的な (= 当該実装の意味付けに直接対応する) trigger を先頭に書く。例: ADR 反映実装が「既存 spec を踏襲する」形を取る場合は `#3, #5` のように記載。trigger 解釈の境界 (`#3` = ADR の新規反映 / `#5` = 既存 spec 上の派生実装) は厳密には演繹できないので、PR 起案時点で implementer の判断で記載し、出口チェックの際に振り返って判定し直してよい (ledger に修正履歴は残す)。

**集約方針 (cross-repo)**: 本 ledger は **spirrow-mindwire 集約** (§N と同じ場所)。cross-repo 適用 (例: spirrow-magickit / spirrow-prismind / spirrow-conclair) のエントリも本表に集約し、`PR # (repo)` 列で repo を識別する。理由: §N の SOT は本書と決めたので (ADR-2026-05-29-13 D-4)、従属メタデータの ledger も同じ場所に置くのが境界整合。cross-repo 適用の起点は常に mindwire 確定 ADR なので「ADR の確定先 = ledger 集約先」が読み手にとってシンプル。**ただし (b) global CLAUDE.md 昇格時 (miss-after-merge 直近 5 回連続 0 件 + cross-project 必要)** には、global への ledger 移動 or 各 repo 分散配置を再評価する。

### §N.2 派生アーティファクト再生成手順 — naysayer ADR index manifest (ADR-2026-06-04-19 N-2)

`spec/adr_index.yaml` は独立 naysayer の system prompt に毎 summon 注入される **全 ADR 索引の派生ビュー** (id + title のみ、ADR 本体は Drive)。canonical な ADR 集合は分散 (CLAUDE.md §M 参照 ∪ spirrow-docs `_docmap`) しており、loop host / CI には `_docmap` が無いため runtime union も CI drift-check も不可 → **in-repo の commit 済コピーは不可避** (host-reality finding, T-naysayer-unify-impl msg-438/443)。

手書き二重管理を避けるため、本ファイルは **生成物**として扱う:

- **再生成手順 (proposer)**: ADR を追加/Accepted した時、`_docmap` がある docs host で `python scripts/gen_adr_index.py --docmap <spirrow-docs/_docmap.yaml>` を実行し `spec/adr_index.yaml` を再生成・commit する (手編集しない)。
- **CI の役割**: `_docmap` が CI に無いので drift-check は不可。CI は commit 済 manifest が **parse でき well-formed** であることのみ検証する (`test_real_in_repo_manifest_loads_and_is_well_formed`)。
- `_docmap` schema は spirrow-docs 側が SOT で本 host から不可視のため、gen-script の `_docmap` reader は schema-tolerant (初回実行時に実 `_docmap` と突き合わせ確認)。

---

## §M. role / identity の規範定義 (ADR 参照のみ — ADR が SOT)

本セクションは規範定義の **参照** のみ。実体の SOT は ADR にあり、CLAUDE.md には規範定義そのものを書かない (T29 SOT 分離原則)。

| ADR | 内容 | thread |
|---|---|---|
| ADR-2026-05-27-09 (T28) | identity 4 レイヤーモデル (identity_name / independence_class / role / 稼働形態 (embodiment) の直交分離)。independence_class レイヤーは ADR-2026-05-31-15 で二値→グラデーション補強。 | T-T28-author-role-identity |
| ADR-2026-05-29-10 (T29) | role registry (proposer / reviewer / implementer / integrator / dogfooder / naysayer / human の 7 role 定義、closeable_roles、close_reason enum) | T-T29-role-registry |
| ADR-2026-05-29-11 | author/identity partition キー正規化 (lowercase + 区切り正規化 + 単射性 gate + strict-by-default) | T-author-partition-key-normalization |
| ADR-2026-05-29-12 | embodiment 自己申告値化 (ADR-09 D-5 拡張・実装、5 API optional 受け口、enum、状態遷移 msg 必須化、human 例外、response-side omit) | T-embodiment-self-declared |
| ADR-2026-05-29-13 | 実装着手前 spec read-back チェックリスト (本ファイル §N.1 の SOT) | T-implementer-spec-readback-checklist |
| ADR-2026-05-31-15 | independence-class グラデーション化 + 「2 協調 1 独立」配置 (ADR-09/T28 の independence_class レイヤーを二値→グラデーションに補強、別訓練分布 naysayer の規範根拠、N-1/C-2 トレードオフ併記、§0 順序) | T-T15-poc-h-phase1-kickoff |

注: ADR-2026-05-31-14 (T15 ガワ方式撤回。旧 §M 参照番号 ADR-2026-05-27-08 を実体化・置換) は identity 規範定義ではないため §M 対象外 (UI 自動化手段選定 ADR)。develop repo に実体あり。
注: 上表 ADR-09 (T28) / ADR-10〜13 は §M 参照名のみで develop repo に文書実体が未作成のものを含む (実体化は別タスク = §M 棚卸し PR、ADR-2026-05-31-15 と ADR-2026-05-31-14 の 2 本のみ本バッチで実体化済)。

ADR 本体は spirrow-docs リポジトリで管理。本 CLAUDE.md からは参照のみで、ADR の規範定義を本ファイルに転載しないこと (F-04 / F-07 系の分散症状を回避)。

---

## 境界の明示

- **§N (メタプロセス / 実装プロセス規約)**: 本書が SOT
- **§M (role / identity の規範定義)**: ADR が SOT、本書は参照のみ

CLAUDE.md 内で §N と §M を無分類で混ぜない。プロセス規約と規範定義は別レイヤーで管理する。後続の追記は §N または §M のいずれかに必ず位置づけ、無分類の散文を CLAUDE.md ルートに置かないこと。

なお規律の趣旨は **root 直書きの散文を禁止する** ことであり、§N/§M の二択を絶対視するものではない。将来 §N (プロセス規約) でも §M (規範参照) でもない第三カテゴリ (例: ビルド手順、テスト規約) が必要になった場合は §O 以降の新セクションを立ててよい。手段 (セクション分類) と目的 (root 直書き禁止 = 分散症状防止) を混同しないこと。
