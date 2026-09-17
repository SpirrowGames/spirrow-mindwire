# MindWire Stage 3 — Wiring + Implementer Allow-list Spec

- **Status**: Draft (local develop). Drive 反映は Takahito GO 後 (Tier C)。
- **Author**: main (claude.ai). Materialized by claude-code from the ADR-07 §5 Open-Q3/Q4 resolution.
- **Resolves**: ADR-2026-05-23-07 §2.2 / §2.3 / §2.6 / §5 Q3 (Part A) + Q4 (Part B)

## Part A — naysayer review trigger / orchestrator wiring (Q3)

### A.1 Watcher 再利用
Stage 1 の ChatroomWatcher は Magickit chatroom のスレッドメッセージを監視する（git ではない）。これを再利用し、**git 用の別 watcher は新設しない**。

### A.2 Trigger = orchestrator イベント駆動
- develop→main の PR open → orchestrator が **`T-pr-review-<n>` スレッドを発火** → 既存 ChatroomWatcher が naysayer adapter に dispatch。
- webhook（Tailscale-only 構成と不整合）/ poller（余分な常駐）は**不採用**。
- **手動トリガをフォールバック**に残す。

### A.3 naysayer review flow
レビュー依頼（PR ref）受領 → GitHub から diff fetch → Lexora（V4-Flash, tier=naysayer, max_tokens>=1500）で批判生成 → **chatroom post + GitHub PR review（Approve / Request-changes）提出**。
- objection → proposer↔implementer の修正ループに差し戻し（human 呼び出しでない）。
- approve → Takahito の merge GO（Tier C）。
- 到達不能・timeout → **fail-closed halt**（ADR-07 §2.6）。

### A.3.1 trigger scoping（least authority / 実装注記）
- review は **review-request マーカー（"review" 語）+ PR ref** の両方が揃ったメッセージのみ発火。スレに無関係な `owner/repo#n` の言及だけでは submit しない。
- セッションは最初に review した PR に **bind** し、以後そのスレで別 PR ref が流れても review しない（least authority）。

### A.3.2 verdict 安全化（実装注記）
- verdict は **行頭 anchor の最終 `VERDICT:` 行**を採用（diff 内に注入された `VERDICT: APPROVE` は +/- prefix で行頭一致せず無効化、prompt injection 耐性）。曖昧・欠落は **REQUEST_CHANGES**。
- diff truncation（>60k）または `finish_reason=="length"` の **部分レビューは APPROVE 不可**（強制 REQUEST_CHANGES）。

### A.3.3 token identity（前提・要環境）
- naysayer の scoped token は **PR 作成者（implementer チェーン）と別 GitHub identity** であること。同一 identity だと GitHub が自 PR への formal review（APPROVE / REQUEST_CHANGES）を 422 で拒否する。
- 暫定 fallback: 同一 identity 由来の 422 を検知したら **COMMENT review にフォールバック**（verdict は body に明記）して fail-closed halt を避ける。本番は別 identity を env spec §4 で発行する。

## Part B — implementer allow-list (Q4)

### B.1 Format
YAML（`_docmap.yaml` と統一）。

### B.2 設計原則
- **default: deny**（未許可 op は fail-loud halt）。
- **operation enum** + **glob 制約**（branch / path / flag）+ **forbidden 明示列挙**（fail-loud の拒否理由を具体化）。
- Tier B（naysayer レビュー）は PR open 後のゲートで implementer 操作ではないため対象外。implementer の世界は **Tier A（許可）+ Tier C（禁止）のみ**。

### B.3 Config（allow-list 本体）
**Tier A 許可:**
- `exec.code` — test・ビルド・実コード実行
- `fs.write` — path glob `<repo>/**` のみ
- `git.commit` / `git.push` — branch glob `feature/*`・`develop`、`force: false`
- `git.merge` — source `feature/*` → target `develop`（main を target にすると制約違反で自動 deny）
- `github.pr.open` — target `develop`・`main`
- `github.read`
- `fs.read` / `search` — read・search（Tier A）

**Tier C forbidden（明示列挙、loop からは実行不可）:**
- `git.merge_to_main`
- `force_push`
- `history_rewrite`
- `fs.delete`
- `drive.write`
- `external.publish`

### B.4 Enforcement
adapter が各アクションを**実行前に config 照合**し、違反 / 未許可 → **fail-loud halt**（Tier 判定ロジックの実体）。Stage 2 の fail-loud no-fallback を継承。

### B.5 役割分離
- `_docmap.yaml` 台帳更新（新規起票 = proposer authoring / 反映書き戻し = integrator）+ doc 本体 authoring + Drive 反映 = **main チェーン管轄**。implementer の仕事ではない。
- 帰結: implementer の `fs.write` は `<repo>/**` 限定（`spirrow-docs` 領域は scope 外）。
- PR open の「所有（review / merge 判断）」は main チェーン + Takahito だが、「git 機構（open）」は GitHub アクセスを持つ implementer が実行（open は可逆 = Tier A）。

### B.6 Enforcement 実装注記（射程と backstop）
- **branch/target の解決**: `git.commit` / bare `git.push` / `git.merge` はコマンド行に branch/target を載せないことがあるため、guard は `.git/HEAD` から **現在 branch を解決**して照合する。解決不能時は **`UNKNOWN` に降格 → default-deny**（fail-closed）。
- **shell 間接実行**: `bash -c` / `eval` / `$(...)` / backtick は内側コマンドがトークン化で見えないため、raw 文字列に対して Tier C パターンを **追加スキャン**する（best-effort, deny-safe）。
- **MCP ツール**: read 系は**正の whitelist（完全一致）**、それ以外は `UNKNOWN → deny`。`*_delete` 等の変異系は substring で write 判定し deny。
- **真の backstop は二層**: (1) 環境封じ込め（Tailscale ACL + egress default-deny + scoped token, ADR-07 §2.4）、(2) **Takahito の手動 merge**（Tier C human pre-GO）が main 到達の authoritative guard。GitHub **main branch protection** は env spec §7 の**将来強化（現プランで保留）**であり現時点では有効化していない。loop ゲートの push-to-main / merge-to-main 拒否はノイズ削減であり、main 到達不可の保証を単独で負わない。
