# ADR-2026-06-03-17: 独立 naysayer の design-time 参加復元 — 5原則の常時注入 + convergence/relay orchestrator

- **Status**: Accepted（2026-06-03、trilateral 収束 + Takahito GO で確定。proposer=Bohr / implementer=Heisenberg(msg-400) / 独立 naysayer=Einstein(msg-404 dry-run、条件付き賛成→指摘全件反映)。`T-naysayer-design-participation` 収束。Drive 反映 = 本ファイル、ADR-07 §2.5 / Tier C）
- **Date**: 2026-06-03
- **Scope**: spirrow-mindwire。**二層にまたがる**: (a) §M 規範 — naysayer 5原則を canonical / versioned な単一 SOT として定義（従来 README に散在する要約のみだった）。(b) 機構 — 独立モデル naysayer（Einstein）を design-thread の議論に参加させる relay 経路。
- **Author**: Bohr (proposer, terminal_coding_agent) — chatroom `T-naysayer-design-participation`（propose）。Takahito（human）との対話で論点が収束し起案（「議論を見せない方が独立」案を Takahito が棄却 → full context 注入 + 別分布で独立を取る元設計に回帰、参加=ブレーキの価値、原則の常時意識+改訂前提を要件として追加）。
- **Relates to / amends**:
  - **ADR-2026-05-31-15**（2協調1独立 / independence-class グラデーション、§3「naysayer は内部 context を失う trade-off を *review 時の full context 注入* で緩和」）— 本 ADR はこの §3 を design-time にも拡張。
  - **ADR-2026-05-31-14**（ガワ方式撤回 = T15 ピボット、naysayer を 2nd Claude.ai session → 別モデルファミリー Gemini/Lexora へ移行）— **本 ADR が回帰させる「参加機能喪失」の発生源**（§1 参照）。
  - **ADR-2026-06-03-16**（naysayer CI-gate / `NaysayerPrReviewAdapter`）— 本 ADR で **PR-gate は naysayer の末端 confirmation に再配置**（主機能ではなくなる、§2 D-6）。
  - ADR-05（RoleAdapter / `Capability.NAYSAYER_QUALIFIED`）、README「trilateral AI workflow」§248-262（5原則 + 2協調1独立の SOT 記述）。
- **発生源**: PR #82〜#85 の dogfood で「Einstein が chatroom を見れない=独立判断を PR diff の末端ゲートでしか出せない」配線問題が露呈。Takahito が「使えないのではなく Chatroom を見れないだけ」「独立性に固執して手段がおかしくなっている」「むしろ反対の立場で議論参加するのがブレーキとして活きる」と指摘。原則検索で **元設計が既に design-time 参加を規定し実績がある**ことが判明（§1）。

---

## 1. Context

### 1.1 naysayer 5原則（SOT 記述、現状 README のみ）

README「trilateral AI workflow」が naysayer を *independent, adversarial review under 5 principles* と定義:

1. **YAGNI / OverScope** — 不要なものを作る/過剰スコープを撃つ
2. **hybrid & dual-management complexity** — ハイブリッド・二重管理が生む複雑性を撃つ
3. **no opposition for opposition's sake** — 反対のための反対をしない
4. **explicitly endorse what should be endorsed** — 賛成すべきは明示賛成
5. **silence is negligence** — 沈黙は怠慢（言うべき時に黙るのは職務怠慢 = **能動参加の要求**）

原則3+4 は naysayer を「常時反対するノイズ源」にしないための calibration、原則5 は「ゲート専任でなく議論に踏み込め」という能動性の要求。

### 1.2 design-time 参加は元々の正規スタイルで実績がある

README §250「設計判断を 3つの AI role の議論で確定する」「仕様増減を伴う変更は 3役割が convergent に至るまで chatroom thread で議論」。naysayer はゲートでなく**議論の当事者**。実例:

- **feature-2**: 当初の 6 sub-PR plan を naysayer の YAGNI/overscope/二重管理複雑性 critique で**縮小**（「Naysayer 寄り=縮小派を採用」）。
- **feature-3**: design option B を 3軸（YAGNI/overscope/ハイブリッド複雑性）で独立 reject → 起案者 stance 撤回。さらに **frame inversion**（「driver 不在で reject は speculation」→「driver 不在で *追加* が speculation、不追加が YAGNI 整合」）= 枠組みごと作り直す深い参加。

### 1.3 独立性の源は「分布差」であって「情報遮断」ではない

ADR-2026-05-31-15 §3 / README §260:「single reviewer の blind spot を構造的に補う」「同一分布の reviewer なら共有する共通盲点を摘出」「naysayer が内部 context を失う trade-off は **review 時に full context を注入して緩和**」。= 独立性は**別モデルファミリー**で取り、context は**むしろ full で注入**する設計。「議論を見せない方が独立」は本設計への逆行。

### 1.4 現 diff-only ゲートは T15 副作用の regression（本 ADR の問題）

元々 naysayer は 2nd 隔離 Claude.ai session で chatroom に peer 参加できた。T15 ピボット（ADR-14）で**真の独立性のため別モデル（Gemini/Lexora）へ移行**したが、Gemini 経路は **tool-less / one-shot**（データガバナンスゲート: no tools/grounding）で chatroom を自分で駆動できず、結果「bundle 投入 → verdict 返却」=**PR diff の末端ゲートの形にしか嵌らなくなった**。

> **認定: 現 Stage 3 の diff-only naysayer ゲートは、独立性アップグレードの副作用で design-time 参加機能（5原則の最高価値=設計段階の YAGNI/overscope ブレーキ）を落とした regression。意図的なゲート専任化ではない。**

---

## 2. Decision

### D-1: 5原則を canonical / versioned な単一 SOT 化し、Einstein に常時注入する【本 ADR の中核要件】

Takahito 要件「原則は Einstein が常に意識すべき & 見直す事もある → 念頭に置く設計」を満たすため:

- **単一 SOT**: 5原則を構造化した canonical artifact（`spec/NAYSAYER_PRINCIPLES.md`、`version:` 付き）を置く。README はそれを**参照のみ**（規範定義を README に転載しない = §M 境界、F-04/F-07 分散防止）。現状の README 一行要約は「参照 + 要約」に格下げ。
- **常時注入（常に意識）**: Einstein を起動する全経路（design-thread 参加 / PR-gate review の両方）で、orchestrator/adapter が原則 artifact を **preamble に verbatim 注入**する。原則を prompt 文字列に散在ハードコードしない（一箇所改訂で全注入に伝播）。
- **改訂前提（見直し）**: 原則は §M role 規範 = ADR が SOT。改訂は **trilateral 議論 + Takahito 承認**を経て artifact の `version` を bump する meta-process（原則改訂それ自体が trilateral に乗る）。
- **traceability**: naysayer の各出力 metadata に適用 `principles_version` を記録。改訂後に「どの review がどの版で判断したか」を遡れる（「見直す事もある」を運用上クリーンにする）。

### D-2: 独立 naysayer を design-time 議論に参加させる（ゲート専任を解除）

仕様増減を伴う design decision の thread で、Einstein は **decision point ごとに 5原則 review を出す**（原則5「沈黙は怠慢」）。これが naysayer の**主機能**で、§1.2 の元スタイルへの回帰。

### D-3: 機構 = convergence/relay orchestrator（msg-385 §4 follow-up の実体化）

Gemini が tool-less/one-shot である制約と、参加性を両立させる:

- orchestrator が design-thread を監視し、decision point で **full context（議論本文 + 関連 ADR/spec）+ 5原則 preamble** を Gemini に注入（§1.3 / ADR-15 §3 を design-time に拡張）。
- Gemini の contrarian 出力を **Einstein の独立 identity で thread に relay-post**（gather-context → independent model judges → relay verbatim、ADR-05 §5 の起動側=gatherer/relay 規律を踏襲。起動セッションは judge しない）。
- 形は「常駐 peer」でも「PR 末端ゲート」でもなく **「decision ごとに full-context で召喚され、賛否を thread に記録する独立 reviewer」**。one-shot 性と参加性を orchestrator 層で接合。

### D-4: full context 注入（情報遮断しない）

独立性は**分布差（Gemini）**で取り、context は full で渡す（D-3）。ただし「文脈」ではなく **adversarial mandate（5原則を明示的 review 次元として）** で渡す — context-as-reassurance による迎合を防ぐ（context を受容材料でなく攻撃対象として提示）。

### D-5: advisory であって veto ではない

naysayer は**うるさく異議を可視化するが決めない**。convergent に至らない場合の最終決定は user（Takahito、Tier C）。power が responsibility を超えないこと。

**【msg-404 B】bootstrap / dry-run でも同じ**: dry-run で naysayer が条件付き賛成／反対を出した場合の**最終判定者も Takahito（Tier C）**。テスト段階で proposer + implementer だけで強行突破しない（= 独立性の破綻防止、Einstein 指摘B）。本 ADR の msg-404 条件付き賛成に対する取り込み判定も Takahito が下す。

### D-6: PR-gate を末端 confirmation に再配置

ADR-16 の CI-gate / `NaysayerPrReviewAdapter`（diff-only review）は**廃止せず**、design-time 参加の**末端確認**として残す（「合意設計に忠実に実装されたか」+ CI 緑の機械確認）。役割が縮むだけで矛盾しない。原則4（明示賛成）で各 decision の賛否が記録されるため、末端ゲートが「自作自演で自分の設計を裁く」問題は生じない（design-time の判断が一次、PR-gate は confirmation）。

### D-7: 独立性会計

design-time 参加で縦方向（longitudinal）の独立性が摩耗するという懸念は、独立性が**分布差**由来である本 topology では成立しない（Gemini は何ラウンド回っても別分布）。各召喚を fresh に保ち（過去の自発言を commitment として抱えさせない relay 設計）、原則3で「反対のための反対」を抑制する。

### D-8: relay の chatroom transport は :8117 直結に依存しない【msg-400 + Takahito 確認 2026-06-03】

relay の chatroom post 経路を `spirrow-magickit-mcp-local.service`（:8117、localhost bind）直結に固定しない。magickit backend へは2経路で到達できる: (i) :8117 直結（loop host 上のみ）、(ii) claude.ai connector（同一 backend への別玄関、off-host から到達可）。

- **完全自動 orchestrator**: loop host 上で動かす → :8117 が localhost で使える（コード変更不要、:8117 の bind 変更も不要）。
- **Claude-session 駆動 relay（dry-run / 手動発火）**: connector 経由で post（off-host から動く）。注意: standalone subprocess は connector を使えない（in-process tool 面ゆえ）= session が回す時限定。
- driver は **2具象経路のみ実装**（① configurable URL の StreamableHttp = loop-host `:8117` / 到達可能な HTTP magickit、② session 駆動 connector relay）。**汎用 Transport 抽象は作らない**（Einstein msg-404 指摘4 = YAGNI 受諾・自己反転）。要件は **:8117 を URL ハードコードしないこと**のみ（現 `naysayer_review.py` の :8117 ハードコードが #85 を dev PC から回せなかった根因）。第3経路が必要になった時に初めて refactor。
- **不採用: :8117 を 0.0.0.0 公開**（`-local` のセキュリティ意図を弱める。connector / loop-host-local で足りるため不要）。

transport は judge ではない（ADR-05 §5）ので本決定は独立性に影響しない。

---

## 3. 論点と暫定解（trilateral、msg-400 Heisenberg + Takahito 確認を反映）

- **N-1（実装分解）= 合流確定**: 既存 relay 骨格（`naysayer_review.py` / `PrReviewOrchestrator` / `NaysayerPrReviewAdapter`）の **入力差し替え**（raw PR diff → thread context bundle）で MVP **4部品**: ① `NAYSAYER_PRINCIPLES.md`（version:1, SOT）② preamble builder（verbatim 注入 + `principles_version` 記録）③ `ContextBundle`（`(thread_id, decision_msg)` の**決定論 gather**）④ relay driver `design_review`（naysayer_review.py の双子）。msg-385 §4 の bundle builder と本 ADR がここで合流。impl task は収束後起票。
- **N-2（召喚タイミング）= Phase1 明示トリガ + プロセス規律【msg-404 で改訂・自己反転】**: 全 propose 自動は却下（ノイズ + 原則3違反 + Lexora コスト）。**Phase1=明示トリガを正とする**。当初 msg-403 で「Phase2=収束点 auto 検知を本命に」としたが、Einstein（msg-404 指摘1）が「非構造テキストから decide 候補を自動検知するのは LLM 不確実性をシステム中核に持ち込む YAGNI／OverScope」と強く反対 → **proposer 受諾・自己反転**。発火忘れ（proposer 過信時）は機構でなく **§N.1 readback checklist に『decide 点で design_review を発火』を追加**するプロセス規律 + Tier C レビューでカバー。**Phase2 auto は YAGNI で無期限保留**（発火忘れの痛みが実観測された時に再評価）。
- **N-3（bootstrap）= 自己 dry-run（実施済）**: Takahito GO（2026-06-03）→ Heisenberg が **Lexora :8110 + connector relay**（:8117 不要、D-8）で ADR-17 自身を独立 Gemini にレビューさせる手動 dry-run を発火。**→ 実施済（msg-404）**: judge=`gemini-3.1-pro-preview`、finish_reason=stop（非 truncation）、**条件付き賛成**。relay 機構の**初 e2e 実証**（Lexora :8110 + connector、:8117 非依存、N-5 決定論 gather を手動体現）。指摘（1〜4 / A / B）は本 §3 N-2/N-5/N-6 + §2 D-5/D-8 に反映済。naysayer が proposer の over-engineering 2点（Phase2 本命化・transport 抽象化）を撃った = 本機構の価値の実証。
- **N-4（独立モデル SOT）= Gemini 確定**: Takahito 確認（2026-06-03）。**SOT = Gemini**。lexora client docstring の "DeepSeek V4-Flash" は stale → impl PR で docstring 訂正 + モデル値を `NAYSAYER_PRINCIPLES.md` 隣の config に1箇所固定。README（Gemini）が正。
- **N-5（tool surface）= scoped + 決定論 gather が生命線**: write=thread post +（D-6 用）PR review submit、広域 read は与えず full context 注入で代替。**独立性が保てる条件 = bundle gather が起動者の恣意でなく `(thread_id, decision)` の決定論関数であること**（Heisenberg caveat）。PR-review 独立性契約「author は context を hand-pick しない」を design-time に適用。
  - **【msg-404 で精緻化】抽出の決定論性の穴（Einstein 指摘2）**: 「参照 ADR を誰が特定するか」が proposer 都合の選択だと見かけだけの独立性。→ **参照解決は正規表現等で thread 内の ADR/spec 参照を機械的・完全抽出（人手選択ゼロ）** + さらに **全 ADR の索引（title のみ、低コスト）を同梱**し、naysayer が「考慮漏れの ADR」も指摘できるようにする。
  - **【msg-404 指摘3】manifest 監査をプロセスに組込む**: bundle manifest を残すだけでなく **Tier C decide の必須確認項目**にする（ログを残すだけで誰も見ない=「沈黙は怠慢」を回避）。
- **N-6（thread 肥大化時の context 戦略）【msg-404 A 新設】**: focused 抜粋規律はファイル同梱の話で、**設計 thread 自体の肥大化**（長期議論で context 巨大化 → Gemini context window / timeout 衝突）が未考慮（Einstein 指摘A）。bundle builder に thread の truncation / 要約戦略を定義する（例: 最新 N msg full + 旧 msg は decide/handoff のみ、or 機械要約）。**決定論性（N-5）を壊さない要約規律**が条件。impl 設計事項。

---

## 4. Consequences

- **(+)** naysayer の最高価値（設計段階の YAGNI/overscope ブレーキ）が復活。手戻り前にコスト未発生の段階で撃てる。
- **(+)** 原則が単一 SOT + 常時注入 + versioned で、Einstein が常に同じ規範で動き、改訂が一箇所で伝播・追跡可能。
- **(+)** 独立性は分布差で担保され、full context 注入と両立（元設計の意図に整合）。
- **(−)** orchestrator 層の複雑性が増える（relay / context 組み立て / 召喚タイミング）。N-1/N-2 で最小実装を設計。
- **(−)** design-time に独立 review を挟む分、収束までの latency が増えうる（原則3で過剰反対を抑え、advisory で進行を止めない設計で緩和）。