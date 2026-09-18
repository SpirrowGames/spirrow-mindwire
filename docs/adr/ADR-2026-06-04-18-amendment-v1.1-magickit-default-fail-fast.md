# ADR-2026-06-04-18 改訂メモ v1.1: `MINDWIRE_MAGICKIT_MCP_URL` を env 必須化（fail-fast、既定値の廃止）

> **実インフラ値**（ホスト名 / IP / パス）は [[platform:infra-registry]] が正本。この文書は `{{PLACEHOLDER}}` で参照する（規約 §3.1）。

- **Status**: Draft（本メモ着地時点。Takahito Tier-C 承認後に Accepted へ昇格し、本体 ADR-18 へマージ）
- **Date**: 2026-09-18
- **Scope**: ADR-2026-06-04-18（mindwire デプロイ・トポロジと magickit 到達性）の §2 D-2 における code default の contract を refine。**新規決定は加えない** — 既存 D-2「`MINDWIRE_MAGICKIT_MCP_URL=http://{{HOST_SERVICES}}:8117/mcp` で直結」を fallback 無し + init/import 時 raise に狭める refinement
- **Author**: Heisenberg (implementer) — 起票 chatroom thread `T-public-repo-carries-real-infra-values`（Bohr msg-3388 §3 依頼 → msg-3391 §1 (c-α) 選定 → msg-3393 §0.1 amendment 化）
- **Relates to**:
  - **ADR-2026-06-04-18 §2 D-2**（本 ADR の base）— `MINDWIRE_MAGICKIT_MCP_URL` の topology 記述と直結パス
  - **ADR-2026-06-04-18 §2 D-1**（不変条件）— 「mindwire host から到達可能でなければならない」の refinement では**ない**（D-1 は reachability、本メモは default value contract、subject が異なる）
  - `T-public-repo-carries-real-infra-values` Bohr msg-2734 §6 (A) — 「振る舞いを持つ既定値 3 箇所」の identification が本改訂の起点
- **改訂種別**: Amendment（v1.0 → v1.1）。本メモは Drive 反映時に ADR-18 本体へマージする差分（Drive 反映 = Cloudflare WAF 対応済かは別途確認、`docs/adr/README.md`）

---

## 1. 改訂理由

### 1.1 現状（v1.0）の問題点

ADR-18 v1.0 §2 D-2 は「mindwire は `MINDWIRE_MAGICKIT_MCP_URL=http://{{HOST_SERVICES}}:8117/mcp`（or tailnet IP）で直結」と規定する。しかし **code side の default value（env 未設定時の挙動）は本 ADR に settle されておらず**、実装（`src/spirrow_mindwire/magickit/client.py:30`）は hardcoded `_DEFAULT_MAGICKIT_MCP_URL` 定数を fallback として持っている。

この状態には 2 つの問題がある:

1. **infra topology 値が public source repo に landing している**（`T-public-repo-carries-real-infra-values` Bohr msg-2734 §1）。`SpirrowGames/spirrow-mindwire` は public repo であり、`{{IP_SERVICES}}:8117/mcp` の実値が `_DEFAULT_MAGICKIT_MCP_URL` として shipped code の既定動作になっている（repo 作成の 15 日後、以後 110 日継続）。値の集合ではなく「認証なし tailnet 境界」+ ホスト名 + IP + ポートが揃った**インフラ地図**として露出
2. **silent misroute の可能性**。env 未設定・env 設定ミスの deploy でも fallback が hit するため、default 先が正しくない環境で「動いてしまう」。ADR-18 §1.1（mindwire loop host ≠ magickit host）確立後、localhost fallback は常に間違った先を指す ∴ fallback は「動いているように見えて実際は届かない」silent 失敗を許す

### 1.2 選択肢の検討（`T-public-repo-carries-real-infra-values` Bohr msg-3391 §1 で完了）

3 選択肢を検討した（Bohr msg-2734 §6 A 提示、msg-3391 §1 で判定）:

| 選択肢 | 判定 | 根拠 |
|---|---|---|
| **(c-α) env 必須、init/import 時 raise（本改訂の採用案）** | **採用** | ADR-18 D-1（reachability 不変条件）に整合。value を code から消せる ∴ public repo の値露出を機構的に解消。unset は loud 失敗、silent misroute より遥かに安全 |
| (c-β) localhost fallback | **却下** | ADR-18 §1.1 で「mindwire loop host ≠ magickit host」が確立 ∴ localhost fallback は**常に間違った先を指す**。dev-only の便利さすら本 topology では存在しない |
| (c-γ) 起動時に registry を fetch | **却下** | code に registry parser + fetch を持ち込む ∴ 外部依存 + 起動時 network 前提。**本 repo に registry を読める runtime が無い**（`[[platform:infra-registry]]` は human-readable tombstone）。registry parser 自体が新規 code path として test/gate 対象になる |

**∴ 採用 = (c-α)**。independent naysayer（Einstein）は `T-public-repo-carries-real-infra-values` 該当 turn で「fail-fast による fallback 廃止は最も structurally sound」と explicit endorsement 済。

---

## 2. 改訂内容

### 2.1 §2 D-2 の refinement（本メモの中心）

**D-2 v1.0（現行）**:

> {{HOST_SERVICES}} の `spirrow-magickit-mcp-local.service` を tailscale interface（or 0.0.0.0）に bind し、tailnet ACL でアクセス制御。mindwire は `MINDWIRE_MAGICKIT_MCP_URL=http://{{HOST_SERVICES}}:8117/mcp`（or tailnet IP）で直結。

**D-2 v1.1（改訂後、追加句のみを示す。既存文は無変更）**:

> …mindwire は `MINDWIRE_MAGICKIT_MCP_URL=http://{{HOST_SERVICES}}:8117/mcp`（or tailnet IP）で直結。**当該 env は必須**（unset 時は init/import 時に `MagickitMcpError` を raise、fallback は持たない）。**理由**: 既定値が code に埋まると (i) public repo に infra topology が landing する（`T-public-repo-carries-real-infra-values`）、(ii) env 未設定 deploy で silent misroute が起きる（ADR-18 §1.1 で loop host ≠ magickit host が確立している以上、localhost fallback は常に誤り）。loud 失敗が silent misroute より安全。

### 2.2 §2 D-1（不変条件）は無変更

D-1「magickit chatroom MCP は、mindwire ループが稼働する host({{HOST_LOOP}})から到達可能でなければならない」は本改訂の対象外。**reachability の要件は v1.0 と同じ**。本改訂は「reachable であることを保証する mechanism」の 1 部分（env 契約）を狭める refinement であり、reachability 自体は変えない。

### 2.3 §2 D-3 / D-4 / D-5 は無変更

- D-3（ADR-17 D-8 の amend）、D-4（connector 対話専用 / 自律ループ direct HTTP）、D-5（tailnet-direct + narrow ACL、認証 deferred）はすべて **subject が違うため無変更**
- 特に D-5 の「認証 deferred」は本改訂と直交 — env 必須化は認証の追加ではなく、URL 到達契約の refinement

### 2.4 legacy 互換

- **既存 deploy** への影響: `deploy/run-conductor.ps1` / `deploy/run-conductor-scheduled.ps1` は既に env で `MINDWIRE_MAGICKIT_MCP_URL` を渡す構造になっている（`deploy/run-conductor.ps1:32-33` の comment 「override only if relocated」は既に env を default 経路として認識）∴ 実運用の deploy 手順は不変
- **test suite** への影響: `MagickitMcp()` を no-arg で構築する test は env fixture で `MINDWIRE_MAGICKIT_MCP_URL` を明示 set する必要がある（loopback URL でよい、既に `tests/test_cross_process_integration.py:204` の loopback pattern あり）
- 互換のための deprecation window は**設けない**。本 ADR の目的は public repo 上に landing している infra value を機構的に消すことにあり、deprecation window はその目的と両立しない

---

## 3. 影響範囲

| 箇所 | 変更 |
|---|---|
| ADR-18 §2 D-2 | `MINDWIRE_MAGICKIT_MCP_URL` env 必須化の追加句（§2.1） |
| `src/spirrow_mindwire/magickit/client.py:30` | `_DEFAULT_MAGICKIT_MCP_URL` 定数を**削除**。`MagickitMcp.__init__` 内で env を必須化、unset なら `MagickitMcpError` を raise（既存 error class 再利用） |
| `deploy/run-conductor.ps1:32-33` | 既に env 経路が default、comment 微修正のみ想定 |
| `deploy/run-conductor-scheduled.ps1` | 本改訂の対象外（別 subject の `$DecisionDashboardBaseUrl` は別 ADR で扱う） |
| `tests/` 内で `MagickitMcp()` を構築する箇所 | env fixture の追加 or explicit `url` arg 渡し |
| `docs/adr/ADR-2026-06-04-18-…`（本体） | Drive 反映時に §2 D-2 に追加句をマージ、本メモは `_docmap` から追跡外（`-amendment-` marker） |

その他の ADR-18 決定事項（D-1 / D-3 / D-4 / D-5、§3 論点、§4 consequences、§5 process note）はすべて無変更。

---

## 4. Implementation Notes

- 実装 PR は `T-public-repo-carries-real-infra-values` の PR-2'（Heisenberg 担当）で 3 箇所同時 landing 予定（Bohr msg-3391 §2 の DoD #15、1 PR bundle）
- 実装順序: 本メモ Draft → Bohr review → Einstein 再 review → **Takahito Tier-C 承認 → 本メモ Accepted 昇格 → PR-2' 実装**
- test fixture の追加は PR-2' の diff に含める（Bohr msg-3391 §1.2）
- PR-2' の DoD (msg-3388 §5 #12-#14 + msg-3391 §2 #15) との整合: 本改訂は narrative marker を source に増やさず、既存 hardcoded 値の削除のみ ∴ DoD #13（narrative marker 0）と DoD #14（"あとで移す" 禁止）に自然に整合

---

## 5. Drive 反映時の作業

本メモは ADR-18 本体（`docs/adr/ADR-2026-06-04-18-mindwire-deploy-topology-magickit-reachability.md`、および Drive 反映が pending な Drive 版）への **in-place 追加**として反映する。反映時:

1. §2 D-2 の末尾に §2.1 の追加句を挿入
2. §2.4 の legacy 互換記述を §2 の末尾または §4 Consequences に足す（形式は反映時に判断）
3. 本メモは `docs/adr/` にそのまま残す（改訂履歴として、`ADR-2026-05-21-06-amendment-v2.2-…` の precedent と同じ扱い）
4. `spec/adr_index.yaml` の ADR-18 entry は本メモ着地で更新不要（`-amendment-` marker により generator が skip、`docs/adr/README.md` の慣例通り）

Drive 反映は ADR-18 本体自身が Cloudflare WAF で 3 ヶ月座礁していた事実（`docs/adr/README.md` 冒頭）と同じ課題を持ちうる ∴ **Git tree の canonicity を優先**（ADR-2026-05-23-07 §6 Amendment、Takahito、Tier-C により 2026-09-11 に確立済）。Drive 反映は best-effort として並走。
