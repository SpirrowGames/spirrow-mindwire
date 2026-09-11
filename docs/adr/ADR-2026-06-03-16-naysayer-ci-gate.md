# ADR-2026-06-03-16: naysayer CI-gate — APPROVE は CI 緑を含意する（approve-while-red 防止）

- **Status**: Accepted（2026-06-03、PR #85 merge = Takahito Tier C GO で確定。merge commit `9afa37d`。Drive 反映=本ステータス変更を canonical doc へ要反映 = ADR-07 §2.5 / Tier C）
- **Date**: 2026-06-03
- **Scope**: spirrow-mindwire（naysayer のレビュー品質ゲート機構。§M role/identity 規範の再定義ではない）
- **Author**: Bohr (proposer, terminal_coding_agent) — chatroom `T-naysayer-ci-gate`（msg-387 propose → msg-389 decide-close）。Heisenberg (implementer) が msg-388 で実装可否 + 論点γを先回り回答。**独立 naysayer (Einstein) の formal review は既知 deferral**（§3 N-3）。
- **Relates to**: ADR-2026-05-23-07（Stage 3 autonomy gating — Tier B naysayer gate / Tier C merge）、ADR-05（RoleAdapter / Capability.NAYSAYER_QUALIFIED）、ADR-2026-05-31-15（2協調1独立 — naysayer=Gemini/Lexora）。実装系: T20（NaysayerPrReviewAdapter）/ T22（naysayer GitHub identity 分離 = spirrowgames-ops）/ T30（本 ADR の実装タスク）。
- **発生源**: `T-stage3-loop-wiring` の runner PR #82 の CI 赤を Takahito が指摘 → 「review 時に CI を検知できるか / 赤なのに APPROVE が出るのは問題」という process gap。
- **Amendment (2026-06-03、同日)**: D-4 / D-6 を訂正。**fine-grained PAT には `Checks` 権限が存在しない**（GitHub がエッジケースで無効化、Checks API は GitHub App 専用）ため、当初の msg-389 decide が採った GraphQL `statusCheckRollup`（CheckRun を読むのに `Checks` を要する）も REST `/commits/{sha}/check-runs` も、review 側 fine-grained PAT では読めない。→ 本 repo の CI は GitHub Actions なので、**Actions API（`GET /actions/runs?head_sha=`、権限=`Actions: Read-only`、fine-grained で付与可）**に切り替える。Takahito の「PAT に Checks 項目が無い」報告 + GitHub Docs 一次確認で判明（§2 D-4/D-6 を改訂）。

---

## 1. Context

Stage 3 自律ループの Tier B ゲートは「naysayer が develop→main PR をレビューし、APPROVE が Takahito merge GO（Tier C）の必要条件になる」（ADR-07）。しかし実コード確認で、この naysayer が **CI 状態を一切見ていない**ことが判明した。

一次確認した3つの事実:

1. **naysayer は CI-blind**: `src/spirrow_mindwire/github/client.py` は `fetch_pr_diff`（`GET /pulls/{n}` を diff 取得）と `submit_review`（`POST /pulls/{n}/reviews`）の2操作のみ。`adapters/naysayer_pr_review.py` を含め check-runs / combined-status / mergeable / conclusion を読む経路がゼロ。naysayer は diff の中身だけを Lexora（Gemini）で批評し、**CI 色と無関係に APPROVE を出せる**。
2. **token が CI 状態 API を弾く**: 実測で `GET /commits/{sha}/check-runs` と `/commits/{sha}/status` が `403 "Resource not accessible by personal access token"`。fine-grained PAT に `Checks: Read-only` + `Commit statuses: Read-only` が無い。
3. **GitHub ネイティブ backstop が無い**: free plan ゆえ branch protection の required status checks が使えない（loop-level merge guard を自作した理由そのもの）。「CI 緑でないと merge 不可」を GitHub 側に委譲できない。

→ 現状 approve-while-red を止めているのは **Takahito の手動 merge 判断のみ**で、仕組みとしてのゲートは不在。PR #82（自律ループ最初の実 PR）が `ruff format --check` で CI 赤のまま naysayer review に向かい得たのが live example で、dogfood が穴を初回1周で炙り出した。

---

## 2. Decision

### D-1: 不変条件

> **naysayer の APPROVE は「レビューした PR head SHA で CI が success」を含意する。CI が failure / pending / 取得不能（403 含む）のいずれでも APPROVE と merge を塞ぐ（fail-closed）。**

### D-2: L1 — CI-aware naysayer（LLM 経路の belt）

`NaysayerPrReviewAdapter.deliver_event` の parse_pr_ref + review-request 判定の後、**Lexora 批評の前**に head SHA の CI を照会し分岐する:

| CI 状態 | 動作 |
|---|---|
| SUCCESS | 従来どおり内容（Lexora）レビューへ |
| FAILURE | Lexora を呼ばず短絡し `REQUEST_CHANGES`（落ちた check 名を body 引用）— model 呼び出し節約 + approve-while-red を構造的に不可能化 |
| PENDING / EXPECTED | `COMMENT` で保留（APPROVE しない） |
| UNKNOWN（403 / network / parse 失敗） | fail-closed: 緑扱いせず APPROVE しない |

### D-3: L2 — merge 側の独立ゲート（決定論の本丸）

「内容 APPROVE」と「CI 緑」を**独立した2必要条件**にする。merge-GO 前に決定論コードで CI state（Actions API、D-4）== SUCCESS を確認する。**L2 が決定論の本丸、L1 は確率的 LLM 経路の belt** であり、L1 単体をゲートにしない。GitHub ネイティブの「approve ≠ checks pass、両方必須」を、branch protection が使えない free plan 環境で loop 内に再構成したもの。

### D-4: API — Actions API（`/actions/runs?head_sha=`）を採用【改訂】

> **改訂理由（2026-06-03）**: 当初は GraphQL `statusCheckRollup` を採った（#82 で `gh`＝広権限トークンでは live 検証できた）。しかし **review 側の fine-grained PAT には `Checks` 権限を付与できない**（GitHub がエッジケースで無効化、Checks API は GitHub App 専用 — GitHub Docs / community #129512 で確認）。`statusCheckRollup` の CheckRun コンテキストも REST `/commits/{sha}/check-runs` も `Checks` を要するため、fine-grained PAT では読めない。

- **採用: Actions API**。本 repo の CI は **GitHub Actions の単一 workflow（`CI`）**なので、`GET /repos/{owner}/{repo}/actions/runs?head_sha={head_sha}` で head SHA の workflow run 群を取り、各 run の `status`（queued/in_progress/completed）+ `conclusion`（success/failure/…）から CI 状態を導く。この endpoint の fine-grained 権限は **`Actions: Read-only`**（付与可）。
- **REST Combined Status `/commits/{sha}/status`（権限=`Commit statuses`）は単体では不十分**: 本 repo の CI は check-runs であって commit status ではないため、`/status` は空を返し**偽の緑**になる罠。`Commit statuses: Read` を付けても Actions CI の緑/赤は判定できない（将来サードパーティの status-context 型チェックを足した時のみ意味を持つ）。
- **state マッピング**: 全 run が `completed` かつ全 `conclusion==success` → SUCCESS / いずれか failure・cancelled・timed_out → FAILURE / queued・in_progress を含む → PENDING / 取得不能（403/network/parse）→ UNKNOWN。`head_sha`（L4 用）は run の `head_sha` フィールドから取得。
- **限界（将来トリガー）**: Actions API は **GitHub Actions の run しか見ない**。非 Actions のチェック（サードパーティ CI App 等）を導入したら Actions API では見えないので、その時点で **GitHub App 化（Checks API / statusCheckRollup が使える）** へ移行する。現状は Actions 単一ゆえ Actions API で完全カバー。

実装面: `github/client.py` に `async def fetch_ci_status(pr) -> CiStatus`（value object `{state, head_sha, failing: list[str]}`）を追加（Actions API REST 呼び出し、既存 httpx client にそのまま載る）。Protocol `GitHubReviewClient` とテスト fake も拡張。fail-closed: 403 / network / parse 失敗 → `state=UNKNOWN`。

### D-5: naysayer は one-shot 維持、pending の再 fire は orchestration 層

naysayer adapter に**内部ポーリングを持たせない**（単一障害点・stall 回避・監査容易）。pending は「保留で返す」だけ。**CI 完了後の再レビュー発火は orchestration 層の責務**とする（webhook は Tailscale egress 方針で不可、`PrReviewOrchestrator` は現状 watch を張るのみで CI 非参照）。MVP では pending → 保留 → 人間 or loop が CI 完了を観測して再投入する。

### D-6: L3 — token 権限拡張（Takahito、L1 の hard 前提）【改訂】

review 側 token `MINDWIRE_NAYSAYER_GITHUB_TOKEN`（spirrowgames-ops, T22、fine-grained PAT）に **`Actions: Read-only`** を付与する。

> **改訂（2026-06-03）**: 当初「`Checks: Read-only` + `Commit statuses: Read-only`」としたが、**fine-grained PAT に `Checks` 権限は存在しない**（GitHub UI に項目が無い ＝ Takahito 報告 と一致、GitHub Docs でも未掲載）。D-4 を Actions API に切り替えたため、必要権限は **`Actions: Read-only` のみ**（fine-grained で付与可）。`Commit statuses: Read-only` は Actions CI には効かない（D-4）ので必須ではない（将来 status-context 型チェックを足す時のみ追加）。

無いと L1 は永久 fail-closed（= APPROVE しない。安全だが loop が進まない）。`Actions: Read-only` を付けても**書き込み権限は増えない**ので、author≠approver 分離（T22）や最小権限の原則とも整合。

### D-7: L4 — head-SHA ピン留め（採用）

Actions API の run が `head_sha` を返すので、review metadata に head_sha を記録する。新 push で HEAD が進めば APPROVE を stale 化できる（branch protection の dismiss-stale-reviews 相当を自前で）。低コストゆえ L1 と同時投入。

---

## 3. トレードオフの併記（失う面・引き受けるリスク）

### N-1: fail-closed による loop 停止リスク

L1 を fail-closed にすると、L3 token が未付与の間は naysayer が永久に APPROVE しない（安全だが loop が進まない）。→ L3（Takahito の token 権限付与）を L1 投入の hard 前提として明記し、token 解決まで L1 は「APPROVE しない＝人間 merge に倒れる」状態で安全側に振れる。

### N-2: pending の再 fire 未自動化

D-5 で pending の再 fire を orchestration 層に置くが、その orchestration（CI 完了観測 → 再投入）は本 ADR スコープ外（未実装）。MVP では人間が再投入する。auto 化は `T-stage3-loop-wiring` msg-385 §4 の convergence/relay orchestrator follow-up に併置。adapter にポーリングを持たせる誘惑を退け、stall と単一障害点を回避することを優先した。

### N-3: 独立 naysayer (Einstein) の formal review を経ていない（既知 deferral）

本 ADR は proposer (Bohr) + implementer (Heisenberg) で収束し、**独立 naysayer (Einstein) の formal independent review を経ていない**。Einstein の design-thread 自律参加は `T-stage3-loop-wiring` msg-385 §4 の convergence/relay follow-up（ループが design-thread を agentize する）まで来ないため。論点γ（fail-closed の単一障害点性 / pending stall / 403→not-green の妥当性）は Heisenberg が msg-388 で実質カバーし、proposer が decide で引き取った（§ D-3 が L2 を権威にすることで L1 の単一障害点性を解消）。**ループ agentize 後、本 ADR を独立 naysay の再レビュー対象にしてよい**。これは「naysayer 設計を独立 naysayer 抜きで決めた」構造的皮肉を記録に残すための明示項。
【追記 2026-06-03: 恒久解の設計は ADR-2026-06-03-17（独立 naysayer の design-time 参加復元）/ `T-naysayer-design-participation` で議論中。実際の #85 merge も独立 naysay 未経由（CI 緑 + Tier C）で行われ、本 deferral を裏書きした、】

---

## 4. 段階移行（rollout）

1. **L3（前提）**: Takahito が token に `Actions: Read-only` を付与（D-6）。
2. **L1 + L4**: `github/client.py` に Actions API `fetch_ci_status`（fail-closed）→ `NaysayerPrReviewAdapter` 入口に gate 挿入 + head_sha 記録。
3. **L2**: orchestration / merge-GO 経路に決定論の CI state==SUCCESS チェック。
4. 実装は **T30**（naysayer CI-gate 実装 + token 権限拡張）が追跡。**→ 完了: PR #85 merged（2026-06-03、`9afa37d`）。**

---

## 5. Consequences

### Positive

- approve-while-red を**構造的に不可能化**（L1 belt + L2 決定論ゲート）。free plan で branch protection が無くても「CI 緑 ∧ 内容 APPROVE」の二条件 merge を loop 内で再現。
- L1 の短絡（failure 時に Lexora を呼ばない）で model 呼び出しを節約。

### Negative / Cost

- L3 token 付与が完了するまで L1 は loop を進めない（N-1）。
- pending 再 fire の自動化が別途必要（N-2）。
- 独立 naysay を経ていない設計を一旦受け入れる（N-3）。

### Neutral

- naysayer は one-shot のまま（D-5）。CI 完了待ちの状態を adapter に持ち込まないことで、retry/stall の複雑性を orchestration 層に局在化。

---

## 6. ADR-07 / ADR-05 / ADR-15 との関係

- 本 ADR は **ADR-07（Stage 3 autonomy gating）の Tier B naysayer gate を補強**する。ADR-07 は「naysayer APPROVE が Tier C merge GO の必要条件」を立てたが、その APPROVE が CI 状態に対して無防備だった穴を本 ADR が塞ぐ（APPROVE に CI 緑を含意させ、merge 経路にも独立の CI 緑チェックを置く）。
- **ADR-05** の `Capability.NAYSAYER_QUALIFIED` 機構・naysayer の独立性要件は据え置き。本 ADR は naysayer の**振る舞い（CI を見るか）**を規定するもので、独立性区画（ADR-15 の 2協調1独立）には触れない。
- §M（role/identity 規範）の rewrite は不要と判断（レビュー品質ゲート機構であり役割規範の再定義ではない、ADR-07 と同類）。必要なら §M に参照リンクのみ後付け。