# ADR-2026-08-25-20: `independence_class` に `machine` を追加

- **Status**: Draft（ローカル develop。Takahito GO 後に反映 = ADR-07 §2.5 / Tier C）
- **Date**: 2026-08-25
- **Scope**: spirrow-prismind の `INDEPENDENCE_CLASS_VALUES` 列挙。spirrow-mindwire T-role-null-must-become-impossible の write half prereq。
- **Author**: Heisenberg (implementer) — T-role-null-must-become-impossible chatroom msg-1704 / msg-1706 の trilateral (Bohr proposer / Einstein naysayer / Heisenberg implementer) 収束を反映
- **Amends**: ADR-2026-05-27-09（T28、identity 4 レイヤー）／ ADR-2026-05-31-15（independence-class グラデーション）。本 ADR は `independence_class` 列挙値の追加であり、上記の直交分離・グラデーション拡張自体は据え置く。
- **Relates to**: ADR-2026-05-29-10（role registry — mindwire 側に role 語彙が属することの根拠）、ADR-2026-05-29-11（partition key normalization）
- **Implemented by**: SpirrowGames/spirrow-prismind#10（`feature/independence-class-machine`）

## 1. Context

spirrow-mindwire T-role-null-must-become-impossible の read half（PR #174 / #177, merged 2026-08-25）で 4 identity を分類した結果、3 identity（`orchestrator` / `pr-gate-relay` / `conductor-probe`）が **machine**（harness code that carries an utterance rather than producing judgment）と判定された。write half は各 identity を `upsert_identity` で登録することを DoD としているが、その実測（msg-1703 §2）により以下が判明した:

- `INDEPENDENCE_CLASS_VALUES = ("main-chain", "independent", "human")`。null / 空文字列は `success=False` で拒否される。
- read half の doc（`docs/identity-classification.md`）は machine 3 件を `independence_class=null` で登録するよう指示していたが、これは **live API で実行不能**。

msg-1706 §2 の設計判断: `human` は既に「スケール上の点」ではなく「スケール非適用の種別」として enum に入っている（この scale が適用されない actor を種別で名乗る先例）。machine はその 2 例目にすぎず、新規逸脱ではなく既存 pattern の完成である。

## 2. Decision

`INDEPENDENCE_CLASS_VALUES` に **`"machine"`** を追加する。

### D-1: 値の追加のみ、意味論の強制は追加しない

Prismind は role 語彙を所有していない（`allowed_roles: list[str]` は任意文字列、ADR-2026-05-29-10 が SOT の 7 role registry は mindwire 側）。∴ 「machine ⟹ allowed_roles==[]」型の biconditional を Prismind の validation gate に置くと、**自分が定義していない名前空間との関係を共有サービスに強制させる**ことになる。これは msg-1706 §1 で明示的に却下されている。

∴ Prismind は enum メンバの検査のみを行う。意味論の pairing（machine と空 allowed_roles の対応）は mindwire 側の guard に置かれる。

### D-2: 既存 record の migration は不要

追加は列挙への appendix であり、既存 record の値（`main-chain` / `independent` / `human`）はどれも影響を受けない。migration script も deprecation window も要らない。

### D-3: 名前は `machine`（`not-applicable` 系ではなく）

mindwire 出荷済 YAML の `kind: machine | participant` と同語にする。mindwire 側 guard の検査式が `kind == "machine" ⟺ independence_class == "machine"` という**字面で読める**形になる。

## 3. Consequences

### Positive
- machine actor が enum-legal な値で登録できる。「role=null は必ず defect」の不変条件が enable される（msg-1179 §4）。
- 既存 record 無変更、migration 無し、gate の validation ロジック無変更 ∴ regression 面積 0。
- `human` が既に立てていた「非スケール種別」pattern を完成させるだけ ∴ 概念的な新規追加は無い。

### Negative / Cost
- enum が 4 値になる ∴ MCP tool schema の consumer は enum を再検証する必要がある。値の追加のみで削除・rename は無いので後方互換は保たれる。

### Neutral
- 意味論の enforcement は mindwire 側にある ∴ 別 client が `machine + allowed_roles=["operator"]` を書く自由は残る。それは client 側規約であり Prismind が polices する事項ではない（D-1）。

## 4. Implementation

- `src/spirrow_prismind/integrations/memory_client.py`: `INDEPENDENCE_CLASS_VALUES` に `"machine"` を append。docstring 更新。
- `src/spirrow_prismind/server.py`: `upsert_identity` inputSchema の `independence_class.enum` に `"machine"` を append。description に「非スケール種別、意味論は client 側」を明記。
- `tests/test_server_dispatch.py`: 列挙 pin を更新。
- `tests/test_session_tools.py`: `test_machine_is_a_valid_independence_class` を追加（gate 通過 + role 有り machine も accept される = enforcement は Prismind 側に無いことを pin）。

## 5. Downstream

spirrow-mindwire の write half（T-role-null-must-become-impossible）が本 ADR を prereq とする:

- 分類 YAML `spec/identity/legitimate_roles.yaml` の `kind: machine` に対応する `independence_class` 値は本 ADR が定義する `"machine"`。
- mindwire-side guard: `kind == "machine" ⟺ (allowed_roles == [] AND independence_class == "machine")` / `kind == "participant" ⟺ (allowed_roles ≠ ∅ AND independence_class ≠ "machine")` — Prismind ではなくここに置く（D-1 の帰結）。

spirrow-magickit の docstring（`src/magickit/adapters/prismind.py:1010` に `"main-chain" | "independent" | "human"` の記述あり）は本 ADR に追随して 4 値に更新するのが望ましいが、blocking ではない — enum の実体は Prismind の tuple 定数が SOT。

## 6. 採番と land の記録

- ADR 番号 `20` は、`spirrow-mindwire/spec/adr_index.yaml` の既存最大連番が `19`（ADR-2026-06-04-19）であることを実測して割り当てた（msg-1708 の「Use whatever ADR id the docs repo assigns next」に従う）。
- 本ファイルは 2026-08-26 に `spirrow-docs` へ land した。同 repo は remote 未設定のローカル専用であり、PR は存在しない ∴ land = local commit。
- `_docmap.yaml` への entry 追加は**本コミットに含めていない**。同ファイルには 2026-06 以来の未コミット変更（ADR-16〜19 の status/reflect 更新）が滞留しており、entry を足して commit すると無関係な変更を巻き込むため。docmap 登録と Drive 反映は後続作業。
