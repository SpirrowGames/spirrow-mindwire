# spec read-back ledger (ADR-2026-05-29-13)

定義: `./obligations.yaml` — 入口/出口 read-back の規範定式は
`OBL-READBACK-ENTRY` / `OBL-READBACK-EXIT` (ループに毎 turn 注入される)。
本 ledger は同 obligation の **適用ログ** であり、§N.1.5 (段階昇格) と §N.1.6
(ログ+分類) を旧 `CLAUDE.md` から退避したもの。人間の閲覧用 (ループは読まない)。

背景・trigger 定義・入口/出口手順は `./README.md` を参照。

---

## 段階昇格と Hook 化判断 (旧 §N.1.5)

本規約は段階昇格方式で運用する。機械化で意図 (「要約 ≠ spec」の自覚) が形骸化するリスクを避けるため、文書化から始めて必要時に hook 化する順序を採る:

- **(a) 現段階**: spirrow-mindwire (本 repo) に明文化、本プロジェクト内で運用
- **(b) 次段階**: Claude Code global `CLAUDE.md` への昇格 (cross-project 適用)
- **(c) 最終段階**: Claude Code Hook 機能で自動 prompt 化 (必要時のみ、最終手段)

**昇格基準**:

- **(a) → (b) 昇格**: `miss-after-merge` が **直近 5 回連続 trigger 該当 PR で 0 件**、かつ cross-project 適用の必要が確認できた時点
- **(c) Hook 昇格**: `miss-after-merge` 累積、または原因分類で特定カテゴリ累積 (例: `[散文埋め込み要件]` が累積 → spec テンプレート構造化 or hook へ)

**リセット条件**: 5 回達成前に miss-after-merge が出たらカウンタをゼロリセットして再カウント。ledger に「リセット: 累積X→0」を 1 行追加 (履歴は消さない、累積カウンタのみリセット)。

理由: miss が出たのは「まだ習慣化していない」証拠なので振り出し。累積で見ると「4 回 clean → 1 回 miss → あと 1 回 clean で 5 回」のような解釈ブレが起きる。ゼロリセットなら「直近 5 回連続 clean」が一貫した意味を持つ。

---

## spec read-back 適用ログ (旧 §N.1.6)

trigger 該当 PR ごとに以下を記録する。記録の所在を本 repo に固定することで、昇格判断の根拠がコードベース上に残る (F-07「根拠がログに残らない」症状を本規約自身が再生産しないため)。

**集約方針 (cross-repo)**: 本 ledger は **spirrow-mindwire 集約**。cross-repo 適用 (例: spirrow-magickit / spirrow-prismind / spirrow-conclair) のエントリも本表に集約し、`PR # (repo)` 列で repo を識別する。理由: 規範 ADR の確定先が mindwire なので「ADR の確定先 = ledger 集約先」が読み手にとってシンプル。**ただし (b) global CLAUDE.md 昇格時 (miss-after-merge 直近 5 回連続 0 件 + cross-project 必要)** には、global への ledger 移動 or 各 repo 分散配置を再評価する。

**trigger 列の記法**: `#3 (ADR 反映実装)` のように id と label を inline する (README.md の trigger 一覧を dangling 参照にしないため)。複数 trigger 該当時は両方を列挙し、最も支配的なものを先頭に書く (例: `#3, #5`)。

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
| #93 (spirrow-mindwire) | #3, #4 (ADR-2026-06-04-19 N-1 反映実装 = naysayer driver-化 unify + proposer 確定 msg ある thread) | T-naysayer-unify-impl msg-434 §② / msg-429-430 (driver-化 / middle=transport≠judge 設計) / Tier C decide msg-459 | `gap-detected` (**Tier B 独立 naysayer が 5 round で逓減的に周辺欠陥を摘出し、proposer 実コード検証と双方向で収束した good case**: r1 eager-open の空 thread leak → lazy / r2 lazy の `_next_number` TOCTOU race (r1 と相反 = naysayer の run 間非決定性が設計トレードオフを炙り出す) → **Takahito Tier C 裁定で決定論 PR-id (`T-pr-review-<pr.number>`) を採用し leak/race を同時解消** / r3 driver `health()` dead-code (valid) + **`build_loop` indentation = false-positive (proposer が実コード照合で捕捉 = 独立 gate も誤りうる実証)** / r4 driver が daemon teardown で未 close (HTTP pool 漏れ、valid) → `aclose` plumbing / r5 **APPROVE**。core (driver 化 / middle / partial-§M-drift-check) は毎回 endorse。proposer 教訓 = naysayer の指摘も鵜呑みにせず実コードで**双方向検証**する。非ブロッカー (script-aclose / driver cap 引き上げ) は fast-follow) | — |
| #95 (spirrow-mindwire) | #3 (ADR-2026-06-04-19 N-4 反映 = 旧 ADR-17 relay/bundle 撤去) | T-naysayer-unify-impl msg-434 §③ / ADR-19 N-4 / msg-429 sequencing | `clean`(impl)+ `self-improvement-note`(gate)(撤去・re-wiring 完全、proposer PASS + README:254 の stale `design_review.py` 参照を本 PR で修正)。**Tier B 独立 naysayer = 誤前提 RC**: context_bundle 削除を「naysayer 盲目化 / N-5 監査破壊」と objection したが、**loop naysayer は summon path (`build_naysayer_system_prompt` = preamble + role + N-2 完全 ADR 索引) で `context_bundle` 未使用** = #95 は dead-code 撤去 (CI green が裏付け)。proposer が実コード検証で誤前提を捕捉 → **Takahito Tier C override-merge** (msg-477/478/479)。**観察 = v1 PR-gate は diff-only ゆえ削除 PR の安全性 (unchanged caller 依存) を誤モデル化**しうる (#93 r3 indentation false-positive に続く gate 誤検出 2 例目) → 安全弁 = proposer full-repo 検証 + Tier C override、恒久解候補 = richer-context gate。naysayer の核 (summon naysayer が doc 本文不可視) は実在ゆえ **T33 起票** | — |
| #98 (spirrow-mindwire) | #4 (proposer 確定 spec ある thread = T34 被験タスク) | T-T34-naysayer-timeout-reconcile msg-503 (Bohr propose、要件 M1-M4 + defer + open question Q) | `clean` (M1-M4 全要件を出口突合表で反映確認 = PR #98 body。M1 `LexoraTimeoutError(LexoraHTTPError)` + `httpx.TimeoutException` wrap (chat_completion/health、subclass ゆえ RequestError より前に catch + 既存 `except LexoraHTTPError` 後方互換) + `__all__` export / M2 `review()` で `LexoraTimeoutError` 捕捉 → truncated 同様 post_critique+submit_review path で force REQUEST_CHANGES、非 timeout LexoraHTTPError は fail-loud 維持、`PrReviewOutcome.timed_out` 追加 / M3 `_LEXORA_BACKEND_TIMEOUT_SECONDS=900` + margin で client default=960 (tie/race 解消) + 誤コメント訂正 / M4 driver+client 両 test。**defer ((c) single-knob / `_resolve_verdict` 一般化) と open question Q (RC vs COMMENT-hold) は仕様どおり未着手** (Q は proposed default の REQUEST_CHANGES を実装し判断は naysayer/Tier-C へ持ち越し)。43+ test green、full 725 passed (既存 flaky `test_invoke_absolute_timeout_fires` は origin/main `0dfb971` でも fail = 無関係)、ruff/mypy clean。naysayer summon せず (Bohr 別 hop)、main 未マージ (Tier-C 固定)) | — |
| #99 (spirrow-mindwire) | #4 (proposer 確定 spec ある thread = T35 被験タスク) | T-T35-lexora-client-default-margin msg-513 (Bohr propose、要件 M1-M3 + defer なし) | `clean` (M1-M3 全要件を出口突合表で反映確認 = PR #99 body。M1 `lexora/client.py` に `_CLIENT_DEFAULT_MARGIN_SECONDS = 30.0` 導入 + `_DEFAULT_TIMEOUT_SECONDS = LEXORA_BACKEND_TIMEOUT_SECONDS + margin` (=930) + `:58-61` コメントを backwards-by-default→safe-by-default に書換 (client が backend を小 margin で上回り ad-hoc 利用者も明示 margin 不要で race から保護、30s 根拠 = 900s gateway timeout 発火/応答の最小余裕、driver の review 専用 60s は ad-hoc default に課さない) / M2 driver (`naysayer/pr_review.py`) の `_CLIENT_TIMEOUT_MARGIN_SECONDS=60` と `+60` default は別概念 (review headroom) ゆえ未変更 (diff に `pr_review.py` 不含で検証、client margin(30)/driver margin(60) 分離維持・統合せず) / M3 `tests/test_lexora_client.py` で `_DEFAULT_TIMEOUT_SECONDS > LEXORA_BACKEND_TIMEOUT_SECONDS` + `backend + margin` 等式を assert (既存に等値前提 assert は無し = 既存 timeout 系 test は typed `LexoraTimeoutError` であって default 値ではない)。**defer なし** (T35 単機能、margin の env 化等 over-eng 回避)。新 T35 test green、full 726 passed (既存 flaky `test_invoke_absolute_timeout_fires` のみ fail = Windows-local timing、CI ubuntu/origin/main で green ゆえ無関係)、ruff/mypy clean。naysayer summon せず (Bohr 別 hop)、main 未マージ (Tier-C 固定)) | — |
| #100 (spirrow-mindwire) | #3, #4 (cross-thread relay conductor 確定設計の反映 = PR-1 純ロジック + proposer 確定 msg ある thread) | T-cross-thread-relay-conductor msg-520 (propose) / msg-522 (disposition) / msg-523 (Tier-C decide = 確定 spec) | `clean` (PR-1 = `conductor/handoff.py` + `core.py`、D-1 serial NEXT dispatch / ① Obj2 諮問必須・非 veto / ③ Obj3 ABSENT→human / D-4 停止 / D-5 不変条件を実装、突合は PR #100 body。proposer spec-review PASS = msg-525。**Tier B 独立 naysayer が同分布見落とし実バグ2件を摘出**: session-keying (Role-key→identity-key、msg-526) / ctor invariant 欠落→無限 force loop + read-after-write 仮定 (msg-529) → fix (identity-key / ctor 検証 + test / docstring precondition 化、F2 transport-hardening は defer) → re-review APPROVE msg-533。3 ゲート通過 → Takahito Tier-C squash merge `6188cad`。22 conductor tests + full 746 passed (既知 Windows-local flaky 除く)、ruff/mypy clean) | — |
| #101 (spirrow-mindwire) | #3, #4 (確定設計 msg-523 の daemon 配線反映 = PR-2a + proposer 確定 msg ある thread) | T-cross-thread-relay-conductor msg-523 (Tier-C decide = 確定 spec) / msg-534 (PR-2 handoff) | `clean` (in-scope 全要件を出口突合表で反映確認 = PR #101 body。**Q4** `mindwire-loop --mode conductor` + `ConductorConfig` (task_thread_id/roster/naysayer_identity/max_rounds、startup validate) / **③** 既存 #82 composition root を `_build_dispatcher` に抽出し watcher/conductor 両モードで再利用 / **① Obj2** naysayer_identity config 化 + ctor invariant を SystemExit 化 / **② Obj1** conductor path は watcher 非構築 (design loop auto-reply 撤去達成、PR-gate は ADR-19 N-1 同期 driver call のまま) / **D-4/D-5** PR-1 から継承、max_rounds 注入 / daemon teardown = `Dispatcher.aclose()` (`ChatroomWatcher.stop()` 対称)。**defer** (gate/Tier-C 判断): adapter の末尾 `NEXT:` emission = PR-2b (未実装ゆえ実 loop は ABSENT→human fallback degrade) / `--mode` 既定 flip・watcher mode 完全廃止の是非 (msg-523 ②「design loop 撤去は確定、完全廃止 vs 残置は実装時 read-back」を gate に委譲、proposer 単独で拡張しない) / ABSENT 経路の Obj2 穴 (msg-525 flag) ・author-suffix fragility (msg-533 weakest point) = PR-2b 候補。+18 tests、ruff/mypy clean、CI green、唯一 fail は既知 Windows-local flaky `test_invoke_absolute_timeout_fires` (本変更無関係)。main 未マージ (Tier-C 固定)) | — |

---

## 出口チェック結果 (4 値)

- `clean`: 全要件項目が反映済
- `gap-detected`: 突き合わせで差分発見、修正してマージ (入口段階で検出した場合は `gap-detected-pre-impl` と注釈)
- `miss-after-merge`: マージ後に逸脱発覚
- `self-improvement-note`: 出口 miss はないが、**入口 read-back の対象 / 出口表項目の網羅性 / trigger 判定 / 実装環境前提** 等について将来的に改善すべき観察を記録。累積 → 規律 / spec テンプレートの構造化判断 / 入口 read-back 対象の拡張 等の打ち手に繋げる。`miss-after-merge` ではないので昇格カウンタ (直近 5 回連続 clean) のリセット条件には該当しない。

## miss 原因分類 (miss-after-merge 発生時のみ、1 語添える)

- `[散文埋め込み要件]`: spec 本文の散文に埋め込まれた要件を目視走査で拾い漏れ
- `[trigger 判定漏れ]`: 該当 trigger に気づかず read-back 自体をスキップ
- `[出口表の項目欠落]`: 出口突き合わせ表の作成時に項目を漏らした
- `[spec 解釈差異]`: 要件の解釈が proposer と implementer で食い違った (implementer 側の read-back 精度の問題)
- `[spec 欠陥]`: spec 本文に要件が欠落/矛盾していた、implementer 側の read-back は正しかった (proposer 側の spec 起案品質の問題)

`[spec 解釈差異]` と `[spec 欠陥]` は **責任の所在**を切り分ける軸: 前者は implementer 規律の問題、後者は proposer の spec 起案品質の問題。この分類で「特定カテゴリ累積」判定を、implementer 規律改善 (前者累積) と spec テンプレート構造化 (後者累積) を異なる打ち手に振り分けられる。
