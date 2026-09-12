# ADR-2026-06-04-18: mindwire デプロイ・トポロジと magickit 到達性 — magickit を mindwire host から到達可能にする(ADR-17 D-8 amendment)

> **実インフラ値**（ホスト名 / IP / パス）は [[platform:infra-registry]] が正本。この文書は `{{PLACEHOLDER}}` で参照する（規約 §3.1）。

- **Status**: **Accepted**（2026-06-05 trilateral 収束: proposer=Bohr / implementer=Heisenberg(msg-418/483) / 独立 naysayer=Einstein **gate APPROVE**(msg-485, principles_v1) / Takahito **Tier-C GO**(scope 判断 msg-484 = 認証は YAGNI で deferred + decide)。`T-mindwire-deploy-topology` を decide-close（msg-486）。**Drive 反映は pending**（Cloudflare WAF が本 markdown を block、retry 要）、_docmap は status=accepted に更新済）
- **Date**: 2026-06-04
- **Scope**: spirrow-mindwire のデプロイ・トポロジ + サービス到達性。**ADR-2026-06-03-17 D-8 を amend**（co-location 暗黙前提を撤回）。
- **Author**: Bohr (proposer, terminal_coding_agent) — chatroom `T-mindwire-deploy-topology`（propose）。**発生源 = Takahito の指摘**:「{{HOST_LOOP}} 上で mindwire が稼働するのに {{HOST_LOOP}} 上でテストできない = 機能的に足りていないのでは」。proposer が D-8 で host トポロジを取り違えていた（mindwire host と magickit host を同一と暗黙仮定）ことを認めて起案。
- **Relates to / amends**:
  - **ADR-2026-06-03-17 D-8**（relay transport は :8117 直結に依存しない / 「:8117 の 0.0.0.0 公開は不採用」）— 本 ADR が **D-8 の「公開不採用」を撤回**し、co-location 前提を是正。
  - ADR-05 §5（independence / connector は judge でない）、Lexora client（:8110 は 0.0.0.0 + no caller auth で既に tailnet 公開）。

---

## 1. Context

### 1.1 確定トポロジ（Takahito 確認 2026-06-04）

- **mindwire ループ = {{HOST_LOOP}}**（Windows、Tailscale `{{IP_LOOP}}`）。proposer/implementer のワークステーション兼、Stage 3 自律ループの稼働 host。
- **Spirrow services = {{HOST_SERVICES}}**（`{{IP_SERVICES}}`、Linux）: `spirrow-magickit-mcp-local.service`（chatroom MCP, `:8117`）+ Lexora（`:8110`）。

### 1.2 欠落（functional gap）

- **magickit `:8117` は {{HOST_SERVICES}} で localhost-bind** → **mindwire host({{HOST_LOOP}})から到達不可**（実測: `{{IP_SERVICES}}:8117` timeout、`:22` も timeout、`:8110` のみ到達）。
- 一方 **Lexora `:8110` は 0.0.0.0 + no caller auth で tailnet 公開済**（lexora client docstring、msg-215）→ 到達可。
- 結果: **headless な mindwire 自律ループ(Stage 3 の本体)は {{HOST_LOOP}} から magickit に届かず、chatroom の gather/post ができない = 自律ループが成立しない**。`scripts/naysayer_review.py` / `scripts/design_review.py` の subprocess も同様に不可（#85 / #87 / #88 で再現）。

### 1.3 connector が欠落を覆い隠していた

対話中の Claude セッション（proposer=Bohr）は **claude.ai connector(SpirrowMagickit)** で magickit に届くため、Tier B review を connector-relay で回せた（#87 msg-410 / #88 msg-415）。だが **connector は claude.ai 仲介の対話セッション専用経路で、headless subprocess には渡せない**。→ connector は「対話中の私」にだけ gap を隠し、**自律運用では穴が開いたまま**。

### 1.4 D-8 の前提誤り

ADR-17 D-8 は「完全自動 orchestrator は **loop host(:8117 localhost)で動かす**」「**:8117 の 0.0.0.0 公開は不採用**」とした。これは **mindwire host = magickit host(co-location)を暗黙前提**にした判断。確定トポロジ(両者別 host)では誤りで、`-local` bind が到達性を壊す主因になっている。

---

## 2. Decision

### D-1: magickit MCP は mindwire host から到達可能にする（要件）

> **不変条件: magickit chatroom MCP は、mindwire ループが稼働する host({{HOST_LOOP}})から到達可能でなければならない。** headless 自律ループは connector に依存できない（対話専用）ため、**直結 HTTP の magickit エンドポイント**が要る。

### D-2: 採用 = magickit を tailnet 公開（ADR-17 D-8「公開不採用」を撤回）

- {{HOST_SERVICES}} の `spirrow-magickit-mcp-local.service` を **tailscale interface（or 0.0.0.0）に bind** し、**tailnet ACL でアクセス制御**。mindwire は `MINDWIRE_MAGICKIT_MCP_URL=http://{{HOST_SERVICES}}:8117/mcp`（or tailnet IP）で直結。
- **セキュリティ posture の整合**: 本デプロイは **既に tailnet を信頼境界としている**（Lexora が 0.0.0.0 + no-auth で稼働）。magickit の localhost-bind はこの posture から外れた outlier であり、tailnet 公開(ACL-gated)への変更は **この環境で実質的なセキュリティ低下を生まない**（control は tailnet ACL）。`-local` の語は「localhost-local」から「tailnet-local」へ意味を更新（or リネーム）。

### D-3: ADR-17 D-8 の amend

- D-8「:8117 を 0.0.0.0 公開は不採用」→ **撤回。tailnet-scoped 公開を採用**（D-2）。
- D-8「完全自動 orchestrator は loop host で動かす(co-location)」→ **co-location は不要**。orchestrator/driver は `MINDWIRE_MAGICKIT_MCP_URL` で magickit に届けばどこでも動く（本番は {{HOST_LOOP}}）。
- D-8 の「2具象 transport（StreamableHttp / connector）」自体は維持。ただし **自律ループは StreamableHttp(直結)一択**（connector は対話セッション専用、D-4）。

### D-4: connector は対話専用、自律ループは direct HTTP

connector-relay は **対話中の Claude セッション(proposer/naysayer relay)** の有効経路として残す（off-host でも動く利点）。だが **Stage 3 自律ループは connector に依存してはならない**（headless に connector は無い）。自律経路 = tailnet 直結 magickit。

### D-5: magickit 到達の認証スコープ（ADR-19 N-5 reconcile）= 現スコープ tailnet-direct + narrow ACL、認証は deferred scope（Tier-C 判断）

ADR-19 議論中に Heisenberg（msg-419）が **remote OAuth magickit（`magickit.spirrowgames.dev/mcp`）+ Squid egress** 方向を共有 → ADR-18 の tailnet-direct（:8117 公開、D-2）と要 reconcile（ADR-19 N-5）。

**現状の認証地図**: OAuth（authorization-code）は対話的ゆえ headless 自律ループは実行不可。対話 client（claude.ai connector）= remote-OAuth `magickit.spirrowgames.dev/mcp`（既に認証付き、不変）/ 自律ループ = tailnet-direct :8117（D-2、非対話）。

**【shaping naysayer msg-481 = REQUEST_CHANGES】**（記録）: 初版 D-5 が (A)無認証tailnet + (B)OAuth を「2 permanent surface」として両立させたのを、(i) **dual-management（原則2）**、(ii) 無認証 tailnet は **caller identity を transport で検証できない**（author 自己申告 = spoof 可、mutation-capable な magickit で agent/role 帰属不能 = ADR-09 / 原則5）と却下。「2 surface 永続」framing の撤回は妥当ゆえ ACCEPT。

**【Tier-C スコープ判断（Takahito）= 認証は現スコープ外（YAGNI）】**:
- naysayer の懸念（認証無し → identity 偽造可能）は valid だが、その**実害は「未信頼 caller が tailnet 上に居る」前提**で生じる。**現環境 = single-user・Takahito 信頼デバイスのみの tailnet** ゆえ未信頼 caller は不在。加えて host-compromise シナリオでは token も同 host に同居しほぼ無力。→ **per-caller 認証を今構築するのは YAGNI**。
- **境界整理（process note §5）**: 「認証サブシステムを target に追加」は **仕様の増減 = Tier-C 承認事項**。proposer/naysayer が「permanent target = 認証」と先取り確定したのは **越権**（naysayer は advisory ＝ スコープ増減を進言はできるが、採否は Tier-C）。よって **permanent-target 確定表現は撤回**、スコープ採否は Takahito に戻す。

**現スコープ（採用）= (A) tailnet-direct + narrow ACL**:
- magickit を tailscale-IP bind（D-2）+ **ACL は {{HOST_LOOP}} のみに narrow grant**（tailnet-wide にしない = 安価な blast-radius 制限。認証でなく hygiene）。
- **既知の制約として明記（隠さず割り切る）**: この経路は **無認証**で、magickit は caller identity を transport で検証しない（author は app 自己申告、同 tailnet 上では原理的に偽造可能）。**現脅威モデル（信頼 tailnet のみ）で受容**。

**deferred = 信頼境界が変わった時に Tier-C が spec 増減として再判断**:
- 引き金 = tailnet に他者/未信頼デバイスが入る・より広く公開する・host-compromise を脅威に含める 等。
- その時の実装は **magickit の既存 `/auth/login`（username/password → JWT bearer、Heisenberg msg-483）を再利用**でき**新規プロトコル不要**（chatroom MCP を JWT auth 配下に出す + service user 発行 + mindwire 側 Bearer 取得の配線が work）＝「必要になってから」でも研究リスク無し。形にするなら naysayer P2/P5 を満たす **単一 unified 認証 entry**（OAuth + service token を 1 entry）方向だが、**着手は将来の Tier-C スコープ判断後**。

→ **本 ADR の採用**: 稼働を **(A) tailnet-direct + narrow ACL** で即 unblock（Takahito host action = N-1 (a)）。認証 = deferred、再判断トリガと実装可能性のみ記録。

---

## 3. 論点（trilateral 叩き台）

- **N-1（implementer / 環境）**: `spirrow-magickit-mcp-local.service` の bind 変更（localhost → tailscale interface or 0.0.0.0 + tailnet ACL）+ {{HOST_LOOP}} から `curl http://{{HOST_SERVICES}}:8117/mcp` で smoke test。`MINDWIRE_MAGICKIT_MCP_URL` を mindwire host env に設定。:22 を開けるかは別途（SSH 運用したいなら）。
- **N-2（naysayer 注目点 / security）**: **magickit MCP は mutation-capable**（chatroom post / project state 変更）で、LLM 推論のみの Lexora より **blast radius が大きい**。tailnet 公開 = tailnet メンバ全員が無認証で magickit を叩ける。tailnet メンバが自分のデバイスのみ(信頼可)なら許容、そうでなければ **scoped token 等の per-caller auth** を magickit+Lexora 横断で別途検討（本 ADR スコープ外、N として明示）。
- **N-3（他サービス）**: mindwire が将来必要とする他 Spirrow services(Prismind/Cognilens/Conclair 等)に同じ localhost-vs-tailnet 非対称が無いか棚卸し。
- **N-4（bootstrap）**: 本 ADR 自身の独立 naysayer review は、修正前ゆえ **connector-relay(対話 me)** で回す（headless 経路はまだ直っていない chicken-and-egg）。修正後は {{HOST_LOOP}} から design_review.py / 自律ループが直接動く。

---

## 4. Consequences

- **(+)** Stage 3 自律ループが**意図した host({{HOST_LOOP}})で実機稼働可能**になる（現状の根本欠落の解消）。`design_review.py` / `naysayer_review.py` subprocess も {{HOST_LOOP}} から動く。
- **(+)** 自律運用が connector(対話専用)非依存になり、トポロジが一貫（services=tailnet 公開で統一）。
- **(−)** tailnet を信頼境界とする posture を**明示的な決定として引き受ける**（mutation-capable な magickit を tailnet に晒す、N-2）。tailnet メンバの信頼前提が崩れる場合は per-caller auth が follow-up。
- **(−)** {{HOST_SERVICES}} の service 設定変更（bind + ACL）が要る（運用作業）。

---

## 5. process note（§N.1 ledger 候補）

proposer(私)が ADR-17 D-8 で **デプロイ・トポロジを未検証のまま「co-location」を暗黙前提**にし、「:8117 公開不採用」を決めた。実トポロジ(mindwire と services が別 host)を Takahito 指摘で初めて突き合わせ、判断が覆った。**入口 read-back 対象に「到達性/配線を伴う決定の前に実デプロイ・トポロジ(どの host で何が動くか)を一次確認」を加える**候補。

**【proposer spec-scope 越権、2026-06-05・self-improvement-note 候補】** shaping naysayer の P5(無認証 tailnet は caller identity 検証不可)を受け、proposer(私)は「**認証付き unified-entry を permanent target にする**」という**スコープ増減を確定済みのように D-5 に書き、Takahito には sequencing(今作るか後か)だけ**を諮った。だが「認証サブシステムを target に追加 = 仕様の増減」は **Tier-C(human)承認事項**で、採否を proposer が先取りしたのは越権(Takahito msg-484 指摘)。naysayer は advisory(スコープ増減を**進言**は可、採否は Tier-C)。是正 = Takahito が YAGNI で認証を **現スコープ外(deferred)** と判断、proposer が D-5 を rewrite。**教訓 = naysayer の懸念がスコープ増減を含意する時、proposer は「sequencing」でなく「スコープ要否そのもの」を Tier-C に上げる**(naysayer 懸念 → 即 design 確定、の短絡を踏まない)。
