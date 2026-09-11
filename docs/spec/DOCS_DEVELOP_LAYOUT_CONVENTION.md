# Docs-Develop Layout Convention (cross-project)

> **実インフラ値**（ホスト名 / IP / パス）は [[platform:infra-registry]] が正本。この文書は `{{PLACEHOLDER}}` で参照する（規約 §3.1）。

- **Status**: Draft (local develop). Drive 反映は Takahito GO 後 (Tier C)。
- **Author**: main (claude.ai). Materialized by claude-code from the ADR-07 §5 Open-Q2 resolution.
- **Resolves**: ADR-2026-05-23-07 §2.5 / §5 Q2

## 1. Purpose
git の develop/main モデルを doc に一般化する（**Drive = doc の main**）。ローカルの「develop ブランチ役」doc フォルダで自由に編集（Tier A）し、Takahito GO で Drive 反映（Tier C）。cross-project 共通規約。

## 2. Root
- 専用 root に **単一 git repo `spirrow-docs`**（各 code repo とは分離 = Magickit project が必ずしも repo を持たないため）。
- 規約上の理想配置は `/srv/spirrow-docs`（{{HOST_SERVICES}}）。Windows ホスト ({{HOST_LOOP}}) では bring-up 中 `C:\workspace\spirrow-docs` に instantiate。

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
