# ADR-2026-06-04-19: 独立 naysayer のエージェント化 + summon ルール(ADR-17 の relay/bundle を supersede)

- **Status**: **Accepted**（2026-06-04 trilateral 収束: proposer=Bohr / implementer=Heisenberg(msg-429) / 独立 naysayer=Einstein **gate APPROVE**(msg-432, principles_v1) / Takahito **Tier-C GO**(msg-431)。`T-naysayer-agentization` decide=msg-433。**Drive 反映 + _docmap 更新は follow-up** = ADR-07 §2.5 / Tier C）
- **Date**: 2026-06-04
- **Scope**: spirrow-mindwire。naysayer role の **runtime 機構**（relay → loop エージェント）+ **design-time 召喚(summon)ルール**。§M 規範のうち 5原則 SOT(ADR-17 D-1)は **carry over**(変更しない)。
- **Author**: Bohr (proposer, terminal_coding_agent) — chatroom `T-naysayer-agentization`（propose）。発生源 = Takahito が naysayer の Gemini surface の **tool-less 制約を解除**(Heisenberg msg-419 で共有、PR #89)。これにより ADR-17 が前提にした「Gemini は tool-less/one-shot ゆえ relay/bundle が必須」が崩れた。
- **Relates to / supersedes**:
  - **ADR-2026-06-03-17（naysayer design-time 参加）を partial supersede**: relay/convergence orchestrator(D-3)・`design_review.py`・`context_bundle.py` を撤回。D-1(5原則 SOT)/ D-4(full context・分布独立)/ D-5(advisory)/ D-6(PR-gate 末端)/ D-7(独立性会計)は **carry over**。
  - 実装: PR #89(`NaysayerSdkAdapter`)/ #87(撤回対象 relay)/ #88(PR-gate principles 注入 = carry over)。
  - ADR-05 §5(独立性 = 別分布)、ADR-2026-05-31-15(2協調1独立)、ADR-2026-06-04-18(loop の magickit 到達性 = loop 共通配線、本 ADR の前提)。

---

## 1. Context

### 1.1 前提の変化(pivot の発生源)
ADR-17 は「naysayer の Gemini surface が **tool-less / one-shot**(データガバナンスゲート)ゆえ chatroom を駆動できない → 専用の **relay + context bundle**(`design_review.py` / `context_bundle.py`)が要る」を中核前提にしていた。**Takahito がこの tool-less 制約を解除**(msg-419)→ 前提が崩れた。

### 1.2 帰結
naysayer を **proposer/implementer と同じ Claude Agent SDK エージェント**にできる。model backend だけ Gemini(Lexora の Anthropic 互換 tier、`ANTHROPIC_BASE_URL`)。chatroom I/O は通常ループ(watcher→dispatcher→adapter→gateway)が担うので、**bespoke relay / context bundle / `design_review.py` は不要**。独立性は「別分布(Gemini)」のまま維持(ADR-05 §5)。実装 = PR #89 `NaysayerSdkAdapter`。

### 1.3 コスト/ToS の再検証(本 ADR の backend / 参加形態判断の根拠)
- **コスト**(verify 済): 外部従量課金は naysayer(Gemini)のみ(light/heavy=自前 Qwen3-32B)。Gemini 3.1 Pro = $2/$12 per 1M(reasoning は output 課金)。relay 実測 = 決定1件 ~$0.03–0.15。**SDK エージェントを standing(毎ターン応答)にすると永続セッションで context 累積 → 約 7×(thread 長で二次増)**。→ 参加形態が cost を支配する。
- **ToS**(verify 済): flat-rate Gemini CLI(Antigravity CLI、Code Assist ライセンス)は「**第三者ソフトが CLI の OAuth で backend に直アクセス = 違反**(公式 CLI headless のみ可)」。Workspace Standard は CLI quota 対象外。**従量 Gemini API(#89/Lexora 経路)は API 規約でプログラム利用がクリーン**。

---

## 2. Decision

### D-1: naysayer = loop エージェント(`NaysayerSdkAdapter`, PR #89)
独立 naysayer を Claude Agent SDK エージェントとして通常ループに載せる:
- inference は `ANTHROPIC_BASE_URL` = Lexora の Gemini tier(`MINDWIRE_NAYSAYER_BASE_URL`)、**未設定なら spawn が fail-closed**(api.anthropic.com に落ちない、独立性 ADR-05 §5)。
- caps = {READ_THREAD, POST_REPLY, NAYSAYER_QUALIFIED}、**EXECUTE_CODE なし**(advisory、repo 非変更)。
- **D-1(ADR-17)維持**: 5原則 SOT を `build_preamble()` で system prompt に verbatim 注入(`principles_version` 記録)。

### D-2: ADR-17 の relay/bundle を撤回(carry over との切り分け)
- **撤回**: relay/convergence orchestrator(ADR-17 D-3)、`scripts/design_review.py`、`src/.../naysayer/context_bundle.py`。Heisenberg が follow-up PR で撤去。
- **carry over(変更しない)**: 5原則 SOT(`NAYSAYER_PRINCIPLES.md` / `build_preamble` / `principles_version`、ADR-17 D-1)、full context + 分布独立(D-4)、advisory not veto(D-5、本 ADR D-4 で強化)、PR-gate 末端 confirmation(`NaysayerPrReviewAdapter` + #88 principles 注入、D-6)、独立性会計(D-7)。
- **要注意の carry-over 課題**: context_bundle が担っていた「**参照 ADR の決定論的・無恣意な提示 + 全 ADR 索引で見落とし指摘**」(ADR-17 N-5 / Einstein 指摘2)は、エージェント化で自動消滅する。エージェントが ADR/spec context をどう得るか(agentic 自己 fetch vs 決定論注入)で、**恣意 curation を避ける不変条件を保つ**こと(§3 N-2)。

### D-3: backend = Lexora-Gemini 従量 API を維持。flat-CLI は park
- **採用**: #89 の Lexora-Gemini(従量 API)。ToS クリーン + 構築済 + モデル pin 可 + summon 運用なら低コスト。
- **park(将来 scale path)**: flat-rate Gemini/Antigravity CLI(Code Assist Standard/Enterprise)。採用条件 = (a) **公式 CLI を headless 起動**(OAuth 流用は ToS 違反)、(b) 業務ライセンス(GCP 規約)、(c) 損益分岐 ≈ naysayer review 5–15 件/日 を超える volume。standing 化 or 複数プロジェクト同時で再評価。Workspace Standard は不可(CLI quota 対象外)。

### D-4: 参加形態 = summon(standing でない)+ summon ルール
コスト・noise(ADR-17 N-2 / Einstein)・縦の独立性の3つが summon を支持。standing(毎ターン応答)は不採用。**summon = decision point ごとに fresh な ephemeral セッションで naysayer を起動**(永続 watcher にしない = 累積コスト/アンカリング回避、独立性会計 D-7 整合)。

**summon ルール(本 ADR の中核)**:
- **対象**: open 時タグ/フラグ付きの **binding-design thread**(spec/ADR 増減を伴う)。ops/bookkeeping/impl 完了 close は **対象外**。
- **タイミング(weight 連動)**:
  | 決定の重さ | shaping(提案時、早期ブレーキ) | gate(decide 前、安全網) |
  |---|---|---|
  | ADR 級(新規/amend/scope 増減) | **必須** | **必須**(最終形) |
  | minor design decision | 推奨 | **必須** |
  | ops / bookkeeping / impl 完了 | — | **免除** |
- **freshness / 再召喚**: gate の naysayer review は **最終形を見ていること**。review msg より後に proposer/implementer の substantive msg(propose/answer/report 等、ack 等除く)が入ったら stale → **再召喚必須**。
- **verdict 別の挙動**(advisory but loud):
  - **存在は必須**、verdict は advisory(veto でない、D-5 carry over)。
  - APPROVE/endorse → proposer が decide(Tier C は従来どおり)。
  - **REQUEST_CHANGES(未充足)のまま decide → ブロック。human(Tier C/Takahito)の明示 override がある時のみ許可**(override 理由を decide に記録、human identity のみ)。proposer の黙殺禁止。
  - naysayer review が存在しない → ブロック(human override 可、記録)。
  - deadlock(数ラウンド REQUEST_CHANGES)→ Tier C tiebreak(override 経路)。
- **トリガ**: `chatroom_close_thread`(decide)という **構造化アクション**。prose から decide 候補を LLM 検知する案(ADR-17 N-2 で Einstein 却下)は採らない。

### D-5: enforcement = 暫定(規律)→ 本命(magickit close-precondition gate)
- **暫定(即運用)**: §N.1 readback checklist に「ADR 級 decide 前に shaping + gate の naysayer 召喚」を追加 + Tier C(Takahito)が merge/accept 時に fresh review 存在を必読確認。
- **本命**: magickit `chatroom_close_thread` に close-precondition gate(タグ駆動・fresh review・verdict・human override)を実装。magickit が role/enforcement の単一 point ゆえここに置く。{{HOST_SERVICES}} の Claude Code に実装依頼(本 ADR と同時にプロンプト発行済)。mindwire 側は「タグ付与 + review msg に構造化 verdict」を揃える(§3 N-3)。
- **【実装・deploy 状況 + owner-override 補修、2026-06-04】** 本命 gate は **deploy 済 & active 実機検証完了**(magickit #9、全4経路: review無→block / non-human override→block / fresh Einstein APPROVE→close 成功 / owner-only。検証 msg-426)。検証中の dogfood で gap 発見: **agent が owner の `gate:naysayer` スレは human override が owner-check に阻まれ届かない**(close は owner-only、gate override は human-only = 権限主体が排他)→ Tier C が agent 所有スレを force-close 不能。補修 = 2 repo: **conclair #6**(`owner_override` flag を honor、**ownership 句のみ**緩和 = `decide`-type / `closes_thread==thread_id` 不変・identity 判定せず・監査 event 記録)+ **magickit #10**(human 限定で `owner_override=is_human` を Conclair へ forward + decide 本文に `[owner-override-by-human]` 注記)。**owner bypass ≠ gate bypass**(gate を先に評価)。deploy 済 & 実機検証 **PASS**(msg-428: human が Bohr 所有 `gate:naysayer` スレを force-close 成功、decide に `[naysayer-gate-override]`+`[owner-override-by-human]` 両注記)。注: gated force-close は `naysayer_override_reason` 流用、非 gated force-close は `owner_override_reason` 必須。

### D-6: registry 共存(design-time エージェント + PR-gate)
`NaysayerSdkAdapter`(design-time)と `NaysayerPrReviewAdapter`(PR-gate / develop→main、D-6 carry over)は**両方 `NAYSAYER_QUALIFIED`**。registry の「NAYSAYER は1つに解決」と衝突する。両者は **異なる surface**(design thread 参加 vs PR diff 末端ゲート)を担うので、共存の機構を確定する必要がある(§3 N-1、implementer 裁量で最小実装)。**不変条件**: summon は fresh ephemeral 起動(永続 watch にしない)/ PR-gate は orchestrator 経由(`T-pr-review` thread)。
- **【msg-429/430 更新 — 共存要件 撤回】** 共存機構は作らない。Heisenberg N-1(msg-429)+ proposer(msg-430)で **driver 化 unify** を採用: judging-behavior を単一 SOT 化し、PR-gate は registry RoleAdapter をやめて **driver**(決定論 CI-gate=ADR-16 / verdict 安全 / T22 GitHub 提出を LLM でなく driver に退避)。これで registry の `NAYSAYER_QUALIFIED` は唯一に戻る(`_assert_role_resolution` 無改修、WatchSpec の adapter_id pin も不要)。**よって D-6 の『両 `NAYSAYER_QUALIFIED` 共存』要件は撤回**。transport は surface 別で可(design-time=Agent SDK ループ / PR-gate=Lexora one-shot)。proposer 推奨 = `transport≠judge`(ADR-05 §5 / ADR-17 D-8)+ YAGNI ゆえ **middle 形**(behavior core 単一・PR judge は one-shot 維持)。strict/middle の最終確認は Einstein(gate 再召喚時)。

### D-7: governance note(tool-less 解除は Tier C の明示判断)
naysayer の tool-less 制約解除 = 「別分布(=信頼境界外でありうる)モデルに agentic/tool アクセスを与えない」というデータガバナンス posture の **意図的変更**(Takahito/Tier C 決定)。エージェントの tool surface は scoped(READ_THREAD/POST_REPLY、EXECUTE_CODE 無し)で blast radius を絞る。

---

## 3. 論点 / 既知 deferral(trilateral 叩き台)

- **N-1(registry 共存の機構)**(implementer): design-time `NaysayerSdkAdapter` と PR-gate `NaysayerPrReviewAdapter` の両 NAYSAYER_QUALIFIED をどう解決するか(thread-type/watch context で選択 / role slot 分離 / PR-gate を role 解決でなく orchestrator 直呼び 等)。最小実装を Heisenberg が選定。
  - **【実装確定 — PR #93 merged (origin/main 96f246c)】** 列挙候補のうち **「PR-gate を role 解決でなく orchestrator 直呼び」を採用**: PR-gate を RoleAdapter から **`NaysayerPrReviewDriver`(driver)化**(D-6 撤回 = driver-unify)。driver は registry 非登録ゆえ NAYSAYER_QUALIFIED でなくなり、**registry NAYSAYER = `NaysayerSdkAdapter` 唯一**に回復(`_assert_role_resolution` 無改修で `== [naysayer]` 成立)。judging-behavior は両 surface で `build_preamble()` 共有 = 単一 SOT、transport は surface 別最適(PR judge は Lexora one-shot 維持 = **middle**、`transport≠judge`)。決定論ガード(CI-gate ADR-16 / verdict 安全 / T22 提出)は driver(code)保持。`PrReviewOrchestrator` が CI-gate→Lexora→post-critique→GitHub submit を直駆動(watcher/dispatcher round-trip 撤去)、thread id は決定論 PR 由来(`T-pr-review-<pr.number>`、Tier C decide)、driver lifecycle は loop teardown で `aclose`。Tier B 独立 review 5 round 収束(leak/race/lifecycle 修正・false-positive は proposer 双方向検証で捕捉)。§N.1.6 ledger #93。
  - **【msg-421 Einstein shaping review Obj-1 反映】** Einstein は「2 adapter 共存 = 二重管理複雑性(原則2)、naysayer の identity/behavior は1つであるべき、PR-gate は別 identity でなく別 trigger/context」として **共存機構を作らず unify(`NaysayerPrReviewAdapter` 廃止 + 同一 `NaysayerSdkAdapter` を PR diff を初期 context に召喚)** を要求。**proposer 希望順位 = unify(単一 SOT)> 共存**。Heisenberg への N-1 判断: (a) PR-gate(develop→main、orchestrator `T-pr-review`、diff 末端)を「同一 adapter を PR diff context で召喚」に畳めるか、(b) #88(PR-gate principles 注入、carry over・merged)を活かしたまま adapter 1本化できるか、を最小実装で決定。**unify が成立するなら D-6 の『両 NAYSAYER_QUALIFIED 共存』要件は撤回**。
  - **【msg-429 Heisenberg N-1 回答 + msg-430 proposer】= 解決(driver 化 unify)**。実体差確定: design-time(`NaysayerSdkAdapter`=Agent SDK ループ・thread 自読・advisory)と PR-gate(`NaysayerPrReviewAdapter`=Lexora one-shot・PR diff・GitHub review 提出+CI-gate/ADR-16)は identity 同一だが plumbing が本質的に別。literal な「adapter 削除して agent に diff+GitHub-write」は **ADR-16(CI-gate fail-closed)と D-7(blast radius)を割る**。→ 採用 = **judging core を1つ(唯一の naysayer agent/SOT)+ PR-gate を driver 化**(driver が決定論 CI-gate→緑なら唯一 judging を diff 初期 context で呼ぶ→verdict 解析+T22 GitHub 提出)。registry `NAYSAYER_QUALIFIED` は唯一に戻り D-6 共存撤回。**proposer 追加判断**: 単一 SOT は **behavior 層**(principles+prompt+verdict 意味論)で満たすべきで transport ではない(`transport≠judge`)→ PR-gate の transport は one-shot 維持(agent ループ化は YAGNI)を推奨(**middle**)。これにより Heisenberg が懸念した「単発 judge を agent ループで回す妥当性検証」も不要化。Einstein が gate 再召喚時に「behavior-core 単一で Obj-1 充足か」を確認。
- **N-2(エージェントの context 取得と無恣意性)= 不変条件確定(msg-421 Einstein Obj-2 ACCEPT)**: context_bundle 撤回で「参照 ADR の決定論的提示 + 見落とし指摘」が消える。**agentic 自己 fetch 単独依存は不採用**(「LLM は存在を知らない ADR を検索できない」= proposer の初期 framing に naysayer の視野が縛られ独立性が崩れる。Einstein 原則1+5)。**不変条件: 全 ADR title の決定論索引(context_bundle の index 部を salvage)を毎 summon の payload / preamble builder に注入する**(naysayer が「考慮漏れ ADR」を独立に特定 → 必要分のみ自己 fetch、の二段構え)。本召喚(msg-421)はこの索引注入を connector-relay で実演済(全 ADR 索引 9 件を機械同梱、proposer hand-pick ゼロ)。これで ADR-17 N-5(起動者が context を hand-pick しない)を満たす。読み range 規約 / 決定論 fetch helper は実装事項。
  - **【実装確定 — PR #91 merged (origin/main 556cca5)】** N-2 不変条件は **in-repo manifest `spec/adr_index.yaml`** として実装(`build_naysayer_system_prompt(repo_root)` が `build_adr_index_block()` を毎 summon 注入)。source は当初案の「`context_bundle` の §M index 部 salvage」を **不採用**: §M は identity/role の curated subset で naysayer/arch ADR(06/07/08/14/16-19)を欠き不完全(proposer 相互 + Tier B review で確定)。代わりに **CLAUDE.md §M ∪ spirrow-docs `_docmap` の union(14 ADR)** を `scripts/gen_adr_index.py`(logic = `naysayer/adr_index_gen.py`)で生成する **派生ビュー**(手書きでなく gen-script で再生成、proposer が docs host で ADR-add/accept 時に実行)。manifest 不在/malformed(`yaml.YAMLError` 含む)は **fail-open → explicit `UNAVAILABLE`**(誤 subset も誤 complete も出さない)。out-of-repo path(`MINDWIRE_DOCS_ROOT`)は loop host に docs 無 + ADR-18 未決ゆえ不採用(in-repo = host 非依存・決定論)。CI は manifest の parse+well-formed を検証(full drift-check は `_docmap` が CI 不在で不可 / **partial §M-subset check は feasible = fast-follow**)。`context_bundle` の §M `parse_adr_index` は ③(N-4 撤去)まで残置。`gap-detected` good case として §N.1.6 ledger #91 に記録。
- **N-3(mindwire 側の対応)**: naysayer adapter/driver が (a) binding-design thread を gate タグ付きで open、(b) review msg に構造化 verdict(`naysayer_verdict`)を載せる。magickit gate(D-5)が読む側なので mindwire が書く側を揃える。
- **N-4(relay 資産撤去 PR)= sequencing 制約明記(msg-421 Einstein Obj-3 ACCEPT)**: `design_review.py` / `context_bundle.py` / relay orchestrator の撤去(Heisenberg follow-up)。**`context_bundle.py` の決定論 ADR 索引生成ロジックを `NaysayerSdkAdapter` の summon/preamble builder へ port 完了するまで、`context_bundle.py` 撤去 PR は merge しない**(N-2 の不変条件を満たす実装を、それを消す前に移植する。Einstein「必要な決定論 gather を消すな」)。relay orchestrator / `design_review.py` の撤去は索引 port と独立に先行可。
  - **【msg-429 Heisenberg port 設計】** port 対象 = `context_bundle.py` の **§M 全 ADR タイトル索引パーサ `_ADR_INDEX_ROW_RE`** を `build_naysayer_system_prompt()` / summon builder へ移し **毎 summon 注入**(完全索引は決定論コードでしか渡せない = Obj-2 核)。thread 内参照 ADR/doc 抽出(`_ADR_RE` / `_LOCAL_DOC_RE`)は agent が thread を自読するので **agent 指示**(参照列挙 → 注入済み完全索引と突合 → 未参照だが関連を点検)に落とす。import グラフ実測で dangling なし・`principles.py` は KEEP。順序 = ①索引 port → ②driver 化 unify(N-1)→ ③撤去 PR(`design_review.py` 先行、`context_bundle.py` は①後)。実装着手は ADR-19 decide 後。
  - **【実装確定 — PR #95 merged (origin/main 2633d07), Step ③】** N-4 撤去完了: `scripts/design_review.py`(relay orchestrator)/ `naysayer/context_bundle.py` / その test を削除。precondition(§M ADR 索引 parser の port)は #91 で充足済(`parse_adr_index` を `naysayer/adr_index.py` へ移植、`adr_index_gen` + §M-subset drift-check が使用)。`principles.py` KEEP。**loop の design-time naysayer(`NaysayerSdkAdapter`)は summon path = `build_naysayer_system_prompt`(preamble + role + N-2 完全 ADR 索引)で `context_bundle` 未使用**ゆえ撤去は dead-code(CI green 裏付け)。Tier B 独立 naysayer は「context_bundle 削除で naysayer 盲目化 / N-5 監査破壊」と **誤前提 RC**(loop が bundle を使うと誤モデル化 = v1 PR-gate の diff-only 盲点)→ proposer 実コード検証で捕捉 → **Takahito Tier C override-merge**(§N.1.6 ledger #95 = `self-improvement-note`)。**未解決の実論点(→ T33)**: msg-429 port 設計は「§M 全 ADR **索引**(id+title)」の port のみ指示し、`context_bundle` の **doc 本文 inlining(`extract_references`/`_read_local_doc`)は未 port**。summon naysayer は text-only(`allowed_tools=[]`)ゆえ参照 spec/doc 本文を見られない(N-2「二段構え」の fetch 段未充足)→ T33 で **port / 限定 read-tool / 運用引用** を検討。
- **N-5(ADR-18 reconcile)**: loop の magickit 到達性は msg-419 で remote OAuth magickit(`magickit.spirrowgames.dev/mcp`)+ Squid egress 方向。ADR-18(:8117 tailnet 公開)はこれと要 reconcile(naysayer 固有でなく loop 共通配線)。
- **N-6(bootstrap)**: 本 ADR 自身は binding-design ゆえ decide 前に naysayer 召喚が要る(自己適用)。新機構(loop エージェント)は magickit 到達性/配線未完ゆえ、当面は **connector-relay(対話 me)** で summon(ADR-17 N-3 と同じ暫定)。

---

## 4. Consequences

- **(+)** naysayer が通常ループの一級エージェントになり、bespoke relay/bundle が消えて配線が単純化。design-time 参加が「特別経路」でなくループ標準で実現。
- **(+)** 5原則 SOT / advisory / PR-gate / 独立性(分布)は維持。#88(principles 注入)は両経路で生き、#87 も概念実証として役割を果たした(relay 配線のみ置換 = whipsaw でなく環境変化由来の健全な反復)。
- **(+)** summon ルール + magickit gate で「naysayer の黙殺」を構造的に防止。コストも summon で bounded。
- **(−)** registry 共存・context 無恣意性(N-1/N-2)の設計が要る。relay 資産撤去の手戻り。
- **(−)** tool-less 解除のガバナンス posture 変更を引き受ける(D-7、scoped tool で緩和)。

---

## 5. process note(§N.1 ledger 候補)

- ADR-17 の relay は #87/#88 で実装・マージした翌日に本 ADR で partial supersede。原因は **外部前提(tool-less 制約)の変更**であって spec 欠陥ではない(`miss-after-merge` ではない)。教訓 = 「外部運用前提(model surface の制約等)は短期で変わりうる。ADR の中核前提が外部条件に依存する場合、その条件の可変性を明示し、変わった時の supersede 経路を最初から想定する」。
- **【bootstrap summon 実施ログ、N-6】** 2026-06-04、本 ADR を connector-relay(Lexora :8110 / judge=`gemini-3.1-pro-preview` / principles_version=1 / finish_reason=stop)で **shaping summon**(msg-421、Bohr verbatim relay)。verdict = **REQUEST_CHANGES**。Obj-2(決定論 ADR 索引の毎注入)/ Obj-3(撤去 sequencing)は ACCEPT し N-2/N-4 へ反映。Obj-1(adapter unify)は identity 一本化に同意・機構は Heisenberg N-1 へ委譲。本 disposition(proposer substantive msg)で当該 review は stale 化 → 改訂 + N-1 回答後、decide 前に **fresh gate summon** を再発火する(D-4 freshness の初実証)。dogfood 所見: summon ルールが proposer の over-scope(self-fetch 単独依存・adapter 二重化)を**設計段階で**撃った = 機構の価値の実証(ADR-17 §3 N-3 と同種)。
- **【owner-override gap → 2-repo 改修、N-6 dogfood 副産物】** #9 gate の deploy 検証 probe 中に「agent owner の `gate:naysayer` スレは human override が owner-check(**Conclair**)に阻まれる」gap を発見(owner-only × override-human-only の権限主体排他)→ 実装依頼 prompt 作成 → {{HOST_SERVICES}} Claude Code が入口 read-back で owner-check の所在(Magickit でなく Conclair)を特定し 2 repo(conclair #6 + magickit #10)で補修・deploy・実機検証 PASS(msg-428)。教訓 = (1) enforcement の所在を入口 read-back で一次確認してから改修範囲を確定する(依頼 prompt は Magickit 単独想定だったが実体は Conclair)。(2) gate の owner-check と override-path の **権限主体**(owner-only × human-only)の排他を設計時に突き合わせる。→ §N.1 ledger に #10(magickit)/ #6(conclair)エントリ追記。

---

## 6. 実装確定サマリ(ADR-19 全 step 完了 — 2026-06-05)

ADR-19 の実装 3 step がすべて merge され、本 ADR の Decision は実装に落ちた:

- **N-1 driver-化 unify** = PR #93(origin/main 96f246c)。PR-gate を `NaysayerPrReviewDriver`(driver, orchestrator 直呼び)に、registry NAYSAYER = `NaysayerSdkAdapter` 唯一。詳細は §3 N-1 実装確定 note。
- **N-2 決定論 ADR 索引注入** = PR #91(556cca5)。in-repo manifest `spec/adr_index.yaml`(§M ∪ _docmap union)+ gen-script + fail-open。詳細は §3 N-2 実装確定 note。
- **N-4 relay/bundle 撤去** = PR #95(2633d07)。`design_review.py` / `context_bundle.py` 削除、`principles.py` KEEP。Tier B 誤前提 RC を Tier C override。詳細は §3 N-4 実装確定 note。

**未了 follow-up**: **T33**(summon naysayer の doc 本文 context-provisioning = N-2 fetch 段)/ **N-3**(mindwire 側 verdict-tag 書き)/ **N-5 = ADR-18 reconcile**(loop magickit 到達性、MVP 稼働の本丸)/ fast-follow(naysayer PR-gate driver の input/output cap 引き上げ・script-aclose 整合)。