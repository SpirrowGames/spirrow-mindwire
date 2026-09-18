# ADR-2026-09-18-22: decision-material-push dashboard URL の default behavior contract（env 必須、fail-fast）

> **実インフラ値**（ホスト名 / IP / パス）は [[platform:infra-registry]] が正本。この文書は `{{PLACEHOLDER}}` で参照する（規約 §3.1）。

- **Status**: Draft（本メモ着地時点。Takahito Tier-C 承認後に Accepted へ昇格）
- **Date**: 2026-09-18
- **Scope**: mindwire wrapper（`deploy/run-conductor-scheduled.ps1`）が decision-material-push の dashboard base URL に用いる default value の contract を確立する。**新規 subject** — 既存 ADR は本トピックを持たない（`docs/adr/` 全文 grep 済、msg-current 実測）
- **Author**: Heisenberg (implementer) — 起票 chatroom thread `T-public-repo-carries-real-infra-values`（Bohr msg-3391 §2 選定 → msg-3393 §0.3 β2 分岐 → Einstein 該当 turn endorsement）
- **Relates to**:
  - **ADR-2026-06-04-18**（mindwire デプロイ・トポロジと magickit 到達性）— **subject が異なる**（ADR-18 は magickit MCP `:8117`、本 ADR は decision-material dashboard `:8443`。別 service、別 port、別 operational context）ため fragment ではない。ただし env 必須 + fail-fast の**同型 contract** を採る（symmetry こそが安全性の担保、Bohr msg-3391 §1）
  - `T-decision-material-push`（decision-material-push の設計スレッド、msg-1445 §6 の M-1 wire measurement を含む）— 本 ADR は「default value の contract」を扱うのみで、push mechanism 自体には触れない
- **改訂種別**: 新規 ADR

---

## 1. Context

### 1.1 現状の default 値

`deploy/run-conductor-scheduled.ps1:1975-1977` は decision-material-push の dashboard base URL を以下の形で解決している:

```powershell
$DecisionDashboardBaseUrl = if ($env:MINDWIRE_DECISION_DASHBOARD_URL) {
    $env:MINDWIRE_DECISION_DASHBOARD_URL.TrimEnd('/')
} else { 'https://{{HOST_SERVICES}}.<tailnet MagicDNS domain — registry gap G4>:8443' }
```

`else` 分岐が hardcoded fallback として実 tailnet URL を持っている。この URL は `New-DecisionLink`（Discord 通知に載る human-facing dashboard link）と `New-MaterialUrl`（magickit `/v1/decisions/.../material` への PUT target）の両方を組み立てる SOT である。

### 1.2 なぜ contract を確立する必要があるか

`T-public-repo-carries-real-infra-values` Bohr msg-2734 §5.1 が identify した通り、`SpirrowGames/spirrow-mindwire` は public repo であり、当該 else 分岐に実 tailnet host + tailnet MagicDNS domain + port が landing している。これは registry §5「public リポジトリに書かない散文」に触れる。

さらに 2 点:

1. **tailnet MagicDNS ドメインは registry §1/§2 の値検出パターンに掛からない** — hook を全ファイルに広げても永久に検出されない値クラス（msg-2734 §5.1）
2. **silent misroute の可能性** — env 未設定の deploy でも fallback が hit するため、当該 tailnet 到達不能な環境で「動いているが実際は届かない」silent 失敗が起きる。material push は fail-open（D-34、msg-1443 §3）なので、`Push-DecisionMaterial` の失敗は 1 log line で吸収されるが、**human-facing の Discord dashboard link は誤 URL のまま送出される** ∴ human が click → 404 or dead host、可視化された material が実は届いていないという誤解を招く

---

## 2. Decision

### D-1: `MINDWIRE_DECISION_DASHBOARD_URL` env を必須化、fail-fast

**`$DecisionDashboardBaseUrl` の解決は env 一択とする。env 未設定なら PowerShell `throw` で loud fail、fallback は持たない。**

理由:
- **ADR-2026-06-04-18 v1.1 と同型の contract**（symmetry こそが安全性の担保、Bohr msg-3391 §1）。3 箇所（`_DEFAULT_MAGICKIT_MCP_URL` / `run-conductor.ps1` env 既定 / 本 dashboard URL）で異なる default 挙動を持たせると、どの deploy でどの behavior が生きているかを人が追跡できなくなる
- value を code から消せる ∴ msg-2734 §5.1 の tailnet MagicDNS ドメイン landing を機構的に解消
- silent misroute より loud 失敗の方が安全（human-facing link の誤 URL 送出は fail-open では吸収できない副次的害）

### D-2: value の解決方法（deploy 側）

deploy operator は `MINDWIRE_DECISION_DASHBOARD_URL` を `[[platform:infra-registry]]` から解決した実値で set する。set 方法は既存 secret 管理と同じ pattern（PowerShell の persistent user env var、`Vaultwarden` sourced が推奨、`deploy/run-conductor.ps1:37-39` の `MINDWIRE_NAYSAYER_GITHUB_TOKEN` と同型）。

registry 台帳側での **tailnet MagicDNS FQDN 用 placeholder** の追加は本 ADR の scope 外（`T-public-repo-carries-real-infra-values` msg-2734 §5.1 の G4「tailnet MagicDNS ドメインが台帳にも §5.1 パターンにも無い」問題、別スレッド案件。具体的な placeholder 名は spirrow-docs 側の判断で決まる ∴ 本 ADR で先取り命名しない）。

### D-3: silent misroute への対策 — human-facing link を fail-open 対象から除外

現状: material PUT の失敗は 1 log line で吸収（D-34、msg-1443 §3）。**dashboard link の URL 誤りは PUT 失敗と直交する** — env が unset のとき PUT も dashboard link も両方誤先を指す。ここで PUT が fail-open だと human-facing link だけが誤 URL のまま送出される。

本 ADR は D-1 により init 時に throw させることでこの class を解消する。sweep の tick 自体は task scheduler が再起動を管理するため、config 修正後に自然に復旧（既存の quarantine 機構は本 class に対して過剰、init 失敗は quarantine 対象外）。

### D-4: material PUT の fail-open は保持（D-34 継続、subject が異なる）

decision-material-push の PUT 失敗時 fail-open（`Push-DecisionMaterial`、D-34、msg-1443 §3）は本 ADR の対象**外**。fail-open の根拠は「material-side outage を notification-side outage に転化しない」（`run-conductor-scheduled.ps1:1995-1999`）で、本 ADR の env 必須化とは subject が異なる ∴ 保持。

### D-5: 既存 wire measurement narrative（M-1）は source から動かさない（future-only pattern）

`run-conductor-scheduled.ps1:1987-2006` に「Wire measurements from {{HOST_LOOP}} (M-1, msg-1445 §6)」（値は [[platform:infra-registry]]）の historical evidence narrative が landing している（Heisenberg audit `T-public-repo-carries-real-infra-values` msg-3390 §1.3 で identify 済）。この narrative は `T-public-repo-carries-real-infra-values` human msg-3354 A binding pattern（narrative は ADR/runbook/commit-message へ）に反するが、Einstein 該当 turn ADVISORY 1 の裁定「future-only、retroactive churn 禁止」に従い、**本 ADR は既存 narrative を移設しない**。

本 ADR の PR-2' 実装 diff が触るのは line 1977 のみ ∴ narrative block は「触っていない line」として future-only scope 外に残る（PR-2' の DoD #13 の narrative marker 0 は「diff で加わる行」に対する要件であり、既存 line の維持は違反ではない）。

---

## 3. 論点（trilateral 叩き台）

- **N-1（implementer / deploy 環境）**: 既存 deploy は `MINDWIRE_DECISION_DASHBOARD_URL` を明示 set していない可能性が高い（コメント `deploy/run-conductor-scheduled.ps1:1970` は env override の意図を示唆するが実運用状況は要確認）。**Heisenberg 実装 turn の前に deploy 側の env 設定状況を audit**。set されていない deploy は PR-2' merge 後に init 失敗するため、事前に env を propagate する運用手順が要る
- **N-2（naysayer / 対称性）**: ADR-18 v1.1 amendment（magickit env 必須化）と本 ADR（dashboard env 必須化）は同型の contract を採るが、**両方が独立に throw** すると deploy operator は 2 段階の失敗 message を受け取る。init flow のどこで throw するか、message で何を hint するかは実装 detail、独立 ADR で扱わない
- **N-3（既存 mini-ADR 慣例）**: 本 ADR は 5 章構成の long-form でなく、subject narrow な mini-ADR として起票した（Bohr msg-3391 §2 の β2 pattern）。既存 ADR がすべて long-form である慣例に対する明示的な逸脱であることを記録。理由は「subject narrow ゆえ context/consequence の枠を保つほど記述量が無い」

---

## 4. Consequences

- **(+)** decision-material-push の dashboard base URL が code から消える ∴ public repo の infra topology landing（msg-2734 §5.1）が機構的に解消
- **(+)** ADR-18 v1.1 の magickit env 必須化と対称の contract により、deploy 挙動が 3 箇所で一貫（Bohr msg-3391 §1 の symmetry rule に整合）
- **(+)** env 未設定 deploy での silent misroute（誤 URL の human-facing link 送出）が排除
- **(−)** 既存 deploy は `MINDWIRE_DECISION_DASHBOARD_URL` を propagate するまで init 失敗（N-1）
- **(−)** ADR の粒度が細かくなる（Einstein の Principle 1 OverScope への正当な advisory 対応として mini-ADR 化した ∴ 本項は既知の cost）

---

## 5. process note

- 本 ADR は `T-public-repo-carries-real-infra-values` msg-3391 §2 Q3 の 1-PR bundle と組み合わさる（3 ファイル同時 landing、DoD #15）。**本 ADR の Accepted 昇格前に PR-2' 実装を進めない**（Bohr msg-3393 §1 の sequence が指示する trilateral 収束を経る）
- 本 ADR の subject narrow 化（mini-ADR 化）は Einstein 該当 turn の explicit endorsement（`T-public-repo-carries-real-infra-values` β2 approach）に依拠。Principle 1（OverScope）と Principle 2（dual-management）の両方に整合すべく、magickit と分離し、dashboard 単独で完結する context のみを持つ
- 既存 wire measurement narrative（D-5）を移設せず放置する判断は、Einstein の future-only 裁定に依拠。**pattern の反例が main に残る事実を本 ADR で明示的に記録**（Bohr msg-3391 §4「忘却すると誤診断を招く」への対応）
