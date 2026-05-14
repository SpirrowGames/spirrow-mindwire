# Feature 3-A 設計 (Phase 1 MCP write API foundation)

**base commit at doc creation**: `de4d040` (= Phase 0 完結後 main、 Phase1-Obs1 fix merged)
**umbrella tracker**: GitHub #41
**trilateral debate final decide**: chatroom `T-feat3-design-overview` msg-117 (resolved 2026-05-14)

> **このドキュメントは skeleton**。 sub-PR 1 (= schema migration v2 skeleton bump) 着手時に §1 overview + §3.1 schema migration の最小章のみで創設。 sub-PR 2 以降で incremental に章を追加する。 起案以前の議論経緯は本 doc に複製せず、 chatroom thread + msg-117 decide msg を一次 reference とする。

## 1. Overview

Feature 3-A = **Phase 1 MCP write API foundation + claude.ai 側 write 開放**。 Phase 0 完結後 dogfooding で観察した「multi-turn 2nd turn 以降の friction」 (= claude.ai 側 awaiting_from 更新機構が不在、 operator manual workflow が現状の唯一 path) を解消する Phase 1 milestone。

### 1.1 Scope (= msg-117 §2 採用 roadmap)

| sub-PR | 内容 | scope | status |
|---|---|---|---|
| 1 | schema migration v2 (skeleton bump + migration script) | F3-A foundation | **着手中** |
| 2 | mindwire-mcp-server 設計 + 実装基盤 (別 process) | F3-A | 未着手 |
| 3 | claude.ai 側 awaiting_from / message write 実装 + race monitoring | F3-A | 未着手 |
| **★ MVP 完了 → Phase 1 dogfooding resume** | | | |
| 4 (deferred) | 2-phase commit re-design | F3-A | dogfooding race observation N+ 件 trigger 後 |
| **★ F3-A 完結** | | | |
| (out of scope) | F3-B (events.jsonl derive engine + operator dashboard / CLI) | deferred | Phase 1 後半 or Phase 2 candidate |

### 1.2 Phase 0 4 assumptions crack (= msg-117 §3)

`docs/feature-2-design.md` §6.0 の 4 assumptions のうち F3-A で crack するもの:

| sub-PR | crack |
|---|---|
| 2 | **in-process MindWire MCP** crack (= 別 process MCP server 立上) |
| 3 | **single writer** crack (= claude.ai 側 MCP 経由 write 開始) |
| 4 (deferred) | (crack ではなく) 2-phase commit semantic 再設計 |

watcher-driven invocation / single watcher process は **維持** (= Phase 1 では crack 不要)。 order は logical dependency で forced、 設計判断的自由度なし。

### 1.3 F3-B deferred (= msg-117 §5)

F3-B (events.jsonl derive engine + operator dashboard / CLI) は本 Feature 3 から外す。 revisit trigger は **以下を全て満たした時点で再評価**:

- F3-A MVP (= sub-PR 1-3) 完了
- Phase 1 dogfooding resume
- multi-turn observation N+ 件蓄積
- operator friction が `yq '.status, .awaiting_from' threads/*/meta.yaml` で扱えない complexity level に到達

→ Phase 1 後半 or Phase 2 candidate。

## 2. 設計選択

(sub-PR 2 以降で incremental 追加予定)

## 3. Schema policy

### 3.1 ThreadMeta schema_version 1 → 2 bump (sub-PR 1)

**変更**: `ThreadMeta.schema_version: Literal[1]` → `Literal[2]`、 `SCHEMA_VERSION` 定数も 1 → 2。 field shape は不変 (= Naysayer skeleton narrow per、 chatroom `T-feat3-d1-schema-skeleton` msg-120 §2 D1-1 = A 採用)。

#### 3.1.1 「3 schema 独立」 原則の維持

mindwire core は **3 つの独立 `schema_version`** を持つ (= `docs/architecture.md` T02 原則 10、 `_common.py` SCHEMA_VERSION docstring):

| schema | 用途 | 本 bump 後 |
|---|---|---|
| `ThreadMeta.schema_version` | `threads/<ULID>/meta.yaml` | **2** |
| `Message.schema_version` | `threads/<ULID>/messages/NNN-from-{cai\|cc}.md` frontmatter | 1 (= 据え置き) |
| `_BaseEvent.schema_version` | `logs/threads/<ULID>.jsonl` 各 event | 1 (= 据え置き) |

F3-A の driver (= claude.ai 側 awaiting_from / message write) が直接触るのは ThreadMeta であり、 Message / Event の structural 変更は本 sub-PR に存在しない。 「3 schema 独立」 原則を曲げる driver なし、 不要な bump は cognitive friction を増やすだけ (= sub-PR 2 / 3 で新 field / 新 event 型が emerge した時点で incremental bump)。

#### 3.1.2 Big-bang 厳格 (= 並走 reader なし)

`ThreadMeta` は v2 only に bump、 v1 を読むと validation fail。 (= msg-117 §4 採用 + msg-120 §4 D1-3 = β、 並走 reader phase は持たない)

operator は **watcher 起動前に必ず一度** `uv run mindwire-migrate-v1-to-v2` を実行する (= documented operation)。 idempotent なので safe re-run 可。

#### 3.1.3 migration script の semantics (msg-120 §3 D1-2)

`uv run mindwire-migrate-v1-to-v2 [--data-dir PATH] [--dry-run]`:

- **対象**: `<data_dir>/threads/<ULID>/meta.yaml` のみ。 staging dir (`.staging-<ULID>/`) / 空 thread dir / messages / events.jsonl は touch せず
- **idempotent**: 既 v2 = no-op skip
- **atomic**: 既存 `atomic_write_text` 流用 (= `*.tmp` → `os.replace`)
- **pre-flight validation**: rewrite 前に `ThreadMeta.model_validate(new_payload)`、 失敗時は file 不変
- **per-thread isolation**: 1 thread failure が run 全体を阻害しない、 `MigrationReport` に集約
- **--dry-run**: scan + plan のみ、 file 不変
- **exit code**: failed thread あれば 1、 なければ 0

#### 3.1.4 Phase 1 dogfooding 5 thread の migration

既存 dogfooding 5 thread (`01KRGW51..` / `01KRHS3Z..` / `01KRHW2N..V9/A/B`) は v1。 sub-PR 1 merge 後の dogfooding resume 前に migration script で v2 に変換する。

### 3.2 後続 sub-PR の schema 変更方針

(sub-PR 2 以降で incremental 追加予定)

## References

- 三者 view source: `T-feat3-design-overview` msg-109 (propose) / msg-112 (review) / msg-115 (naysayer)
- decide: `T-feat3-design-overview` msg-117 (= integrator、 resolved)
- sub-PR 1 design specifics: `T-feat3-d1-schema-skeleton` msg-120 (propose、 active)
- 設計 SOT (継承元): `docs/feature-2-design.md` §3.2 / §6.0 / §6 FI-2 / `docs/architecture.md` §3.1 (= meta.yaml example bumped to v2)
- 関連 GitHub: Issue #41 (= umbrella) / #42 (= sub-PR 1)
