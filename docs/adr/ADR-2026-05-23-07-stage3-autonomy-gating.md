# ADR-2026-05-23-07: Stage 3 Autonomy Gating + Implementer 安全設計

- **Status**: Accepted (2026-05-23, Takahito GO) — Claude Code が Drive へ canonical 反映 + ADR-07 thread decide-close（本 doc 自体が §2.5 のフローに従う）。
- **Date**: 2026-05-23
- **Author**: main (claude.ai)
- **Related**: ADR-2026-05-21-04（mindwire 役割転換の根拠）, ADR-2026-05-21-05（役割と adapter の抽象化, §5 独立性）, ADR-06 v2.1（Phase 2 dogfood）
- **Project**: spirrow-mindwire / Phase 2 dogfood Stage 3

---

## 1. Context

Phase 2 dogfood は Stage 1（watcher baseline, PR #65 merged）と Stage 2（独立 naysayer adapter, PR #66 merged）を完了。三者ループの 2 者目（naysayer = DeepSeek V4-Flash, ADR-05 §5 独立性充足）が実モデルで稼働済み。

Stage 3 は **autonomy gating + implementer** = 正式稼働の最終段。proposer↔naysayer↔**implementer** の三者ループを完成させる。fork2 の「重い部分」である EXECUTE_CODE capability 解放を含むため、**「どこまで自律で実行してよいか（自律度ライン）」の安全設計が本 ADR の主題**。

設計原則は Stage 2 から継承する: **fail-loud no-fallback**、**naysayer の構造的独立性**（main chain と分離）。

---

## 2. Decision

### 2.1 自律度ライン（3 Tier, operation ベース）

| Tier | 範囲 | ゲート | 根拠 |
|---|---|---|---|
| **A 自律** | feature/* での開発・commit / feature→develop の **main 役 self-review & マージ** / EXECUTE_CODE（test・ビルド・実コード実行）/ ローカル develop doc フォルダの編集 / read・search | なし | 可逆・低ブラスト。feature / develop / ローカル doc は非 canonical |
| **B naysayer-gate** | PR（develop→main）の独立レビュー | naysayer **明示 approve** 必須（objection 時は §2.2 へ） | 集約 diff に対する独立検証 |
| **C human pre-GO** | main への merge / delete・force-push・history rewrite / 外部副作用（publish・post・send）/ Drive canonical doc 反映 | **Takahito 事前承認** | 不可逆・高ブラスト |

判定軸は **operation ベース**（doc_type / path / git operation の種別）。develop 上では速度を最大化し、canonical（main / Drive）への昇格点にのみゲートを集約する。

### 2.2 三者ループ完成形

```
proposer (claude-code / main chain)
  └─ 提案
implementer (Stage 3 新規 / EXECUTE_CODE 付き)
  └─ feature/* で開発・commit【Tier A】
  └─ feature/* → PR → develop を open
main 役 (reviewer/integrator / claude.ai chain)
  └─ self-review してマージ【Tier A: 自律、naysayer 不要】
  └─ 区切りで develop → main の PR を open
naysayer (DeepSeek V4-Flash / 独立)
  └─ develop→main の集約 diff を独立レビュー【Tier B】
       ├─ objection → proposer↔implementer の修正ループに差し戻し（自律再試行。human 呼び出しではない）
       └─ approve → Takahito の merge 判断へ
Takahito
  └─ main merge GO【Tier C】
```

- **feature→develop は main 役の self-review & マージ（自律, Tier A）**。proposer と reviewer/integrator は同一 main チェーン（共有 context）なので独立検証ではないが、整合性・品質ゲートとして機能する。**現行の claude.ai↔claude-code レビュー運用（claude.ai が claude-code 実装をレビュー）で self-review の実効性は実証済み**。
- **独立検証は develop→main に集約**。naysayer は per-commit のノイズではなく **集約 diff** に当たることでレビュー面の質を上げ、独立性（ADR-05 §5）も main 昇格点のみに保持される。
- naysayer の objection は「人間を呼ぶ」シグナルではなく **「ループ内で直せ」** のシグナル。最終 merge 判断のみ Takahito。

### 2.3 implementer adapter

- `ClaudeCodeSdkAdapter` をベースに **EXECUTE_CODE capability** を付与。
- 許可境界（allow-list, operation ベース）:
  - **許可**: feature/* への commit、feature→develop の self-review マージ、ローカルでの任意コード実行、ローカル develop doc フォルダ書込
  - **禁止（loop からは実行不可）**: main merge、force-push、delete、history rewrite、外部副作用、Drive canonical 書込 → すべて Tier C で Takahito 経由
- Stage 2 の **fail-loud no-fallback** を継承（不明 tier → 502, fallback しない）。

### 2.4 環境セキュリティ（独立 PC）

EXECUTE_CODE を全開放するため、blast radius は **loop のゲートではなく環境レベルで物理的に封じる**。

- **Tailscale ACL を絞る**: implementer ノードからの到達先を **Lexora（naysayer 用エンドポイント）と magickit 等の一部許可ツールに限定**。Vaultwarden / SSH / その他内部ホスト・ポートには到達不可。
- **egress default-deny**: package registry・GitHub API・許可済み内部エンドポイント以外への外向き通信を遮断。
- **scoped credentials**: GitHub token は対象 repo の最小権限。Vaultwarden アクセスは環境に置かない。長期クレデンシャルを implementer 環境に保持しない。

→ Tier A（EXECUTE_CODE 全開放）が成立する前提条件として、**内部網への横移動と外部への exfiltration を環境レベルで不可能化**しておくことを必須とする。

### 2.5 ドキュメント取り扱い

git の develop/main モデルを doc にも一般化する（**Drive = doc の main**）。

- プロジェクト毎に {{HOST_SERVICES}} ローカルへ **「develop ブランチ役」の doc フォルダ**を設置。そこの編集は **自由（Tier A）**。
- 最終的に **Takahito 承認を得て Drive へ反映（Tier C）**。canonical な ADR / spec doc は Drive 反映時にのみゲート。
- **Deferred（Stage 3 スコープ外, 別機会）**: magickit を拡張し、read 時にローカル develop doc / Drive canonical のどちらを読むか指定できるツール化。面白いが本 ADR では扱わない。

### 2.6 Fail-mode

- naysayer 到達不能・timeout 時は **fail-closed で halt**（human 待ち）。fallback で先に進めない。Stage 2 の fail-loud 原則と整合。

---

## 3. 独立性制約との整合（ADR-05 §5）

naysayer は main chain と context 分離された独立検証専用であり、**irreversible 操作の最終権限を構造的に持てない**（main 経由の承認系に入れない）。したがって **Tier C のバックストップは構造的に Takahito 一択**。これは制約ではなく設計の要請であり、naysayer approve は「merge の必要条件」、Takahito GO は「十分条件」として二段で機能する。

---

## 4. Consequences

### Positive
- develop / ローカル doc 上で速度を最大化しつつ、canonical 昇格点（main / Drive）にゲートを集約 → 安全と速度の両立。
- naysayer が集約 diff をレビュー → 独立検証の質が per-commit より向上。
- 環境レベルの封じ込めにより、EXECUTE_CODE 全開放と安全性が両立。

### Negative / Risks
- feature→develop は self-review（同一 main チェーン）のため独立検証ではない（緩和: 独立性は develop→main の naysayer に集約。現行 claude.ai↔claude-code レビュー運用で self-review の実効性は実証済み）。
- 環境セキュリティが Tailscale ACL / egress 設定の正しさに依存する（緩和: ACL を最小許可で構成、設定自体を Tier C 変更扱いにする）。
- naysayer halt 固定により、naysayer 不安定時はループが止まる（受容: 安全側の意図的選択）。

### Deferred
- magickit read-source 切り替えツール化（§2.5）。

---

## 5. Open Questions / Follow-ups

1. 実装独立 PC の現行ネットワーク構成確定 → Tailscale ACL の具体ルール記述。
2. ローカル develop doc フォルダのレイアウト規約（プロジェクト毎ディレクトリ命名）。
3. PR（develop→main）の naysayer レビュー trigger（PR open hook? 手動? watcher 連動?）。
4. implementer adapter の allow-list を設定ファイル化する形式（operation enum + path glob）。

---

> **Provenance**: canonical reflection of `ADR-2026-05-23-07-stage3-autonomy-gating.md` (source author: main / claude.ai). Reflected to Drive by claude-code per §2.5 (Tier C, Takahito pre-GO obtained). Recorded in chatroom thread `T-phase2-stage3-autonomy-gating`.