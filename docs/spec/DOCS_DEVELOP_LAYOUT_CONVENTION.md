# Docs-Develop Layout Convention (cross-project) — SUPERSEDED

> **実インフラ値**（ホスト名 / IP / パス）は [[platform:infra-registry]] が正本。この文書は `{{PLACEHOLDER}}` で参照する（規約 §3.1）。

- **Status**: **Superseded (2026-09-11)**。本規約が定めた「Drive = doc の main」は
  ADR-2026-05-23-07 §6 Amendment（Takahito 権限・判断、Tier-C）で撤回された。**この文書に従って
  新しい運用を組んではならない。** 履歴として残す。
- **置き換えたもの**: 正本は各リポジトリの Git ツリー（`docs/adr/` と `docs/spec/`）。機構は
  [[platform:docs-infrastructure-design]] §6.3.1 の二層構成（working tier = 下書き / canonical tier = Git clone）。
- **Author**: main (claude.ai). Materialized by claude-code from the ADR-07 §5 Open-Q2 resolution.
- **Resolves**: ADR-2026-05-23-07 §2.5 / §5 Q2（§2.5 は同 §6 で撤回済み）

## 0. 何がどう置き換わったか（2026-09-11）

| 本規約の節 | 当時の定め | 現在 |
|---|---|---|
| §1 Purpose | Drive = doc の main | **Git が正本。** Drive は正本でない |
| §2 Root | 専用 root に単一 repo `spirrow-docs`（remote 無し）を置く | **各 code repo の `docs/` に置く。** 下書きは `{{PATH_DOCS_WORK}}` の working tier |
| §3 Layout | `<project>/<category>/<doc>.md` | **`docs/adr/` と `docs/spec/`**（リポジトリごと） |
| §4 Doc body | front-matter を入れない | **front-matter の `type` が分類の正本**（[[platform:document-conventions]]） |
| §5 `_docmap.yaml` | ローカル専用台帳 | 去就は未決。`scripts/gen_adr_index.py --docmap` が今も入力に取る |
| §6 反映ワークフロー | Tier A 編集 → Tier C で Drive 反映 | **working tier へ書き、明示的な promote（PR）で canonical へ。** 滞留と `diverged` は同期時に検出 |
| §7 将来流用 | 切替ツールの台帳に流用可 | Deferred のまま（二層構成は「呼び手は層を知らない」と定めたので設計を作り直す必要がある） |

**なぜ撤回されたかは ADR-2026-05-23-07 §6 に書いてある**（穴が 2 つあり、どちらも実際に発火した:
反映前の本文が 1 台のディスクにしか無い / 反映漏れが検出されない）。ここでは繰り返さない。

**`{{HOST_LOOP}}` のローカル tree は削除されていない。** 全 13 ファイルを git へ照合・取り込みした後、
書き込み停止にしてある。移行が持ち込んだ文字化けを照合できる唯一のコピーであるため。

---

以下は撤回前の本文である。**現行の規約として読んではならない。**

## 1. Purpose
git の develop/main モデルを doc に一般化する（**Drive = doc の main**）。ローカルの「develop ブランチ役」doc フォルダで自由に編集（Tier A）し、Takahito GO で Drive 反映（Tier C）。cross-project 共通規約。

## 2. Root
- 専用 root に **単一 git repo `spirrow-docs`**（各 code repo とは分離 = Magickit project が必ずしも repo を持たないため）。
- 規約上の理想配置は `{{PATH_DOCS_DEVELOP}}`（{{HOST_SERVICES}}）。Windows ホスト ({{HOST_LOOP}}) では bring-up 中 `C:\workspace\spirrow-docs` に instantiate。

## 3. Layout
- `<project>/<category>/<doc>.md`、category ∈ {adr, spec}。
- 例: `spirrow-mindwire/adr/`, `spirrow-mindwire/spec/`。

## 4. Doc body
- 本体は**クリーン（front-matter を入れない）**。ID / 状態は manifest に分離。

## 5. `_docmap.yaml` manifest (local-only)
- repo ルートに置く。**Drive には反映しない**（ローカル専用台帳）。
- local path ↔ `drive_doc_id` / `prismind_id` / `status` / `last_reflected` を管理。

## 6. 反映ワークフロー
- **Tier A**: ローカル自由編集・commit。
- **Tier C (Takahito GO)**: Claude Code が manifest を見て `smart_create_document`（初回）or `smart_update_document`（full replace）+ Prismind catalog 登録 + status / last_reflected 更新。

## 7. 将来流用
- 本 manifest は Deferred の magickit read-source 切替ツール（ローカル develop / Drive canonical のどちらを読むか指定）の台帳に流用可。
