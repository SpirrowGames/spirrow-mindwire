# ADR bodies — in-repo set

ADR 本体の置き場は**移行中**である。このディレクトリはその移行先で、まだ全部は揃っていない。

## いま何がどこにあるか

| ADR | 本体の所在 | 理由 |
|---|---|---|
| ADR-2026-05-21-06 改訂メモ v2.2 | **本ディレクトリ** | Drive 未反映だった |
| ADR-2026-05-24-08 | **本ディレクトリ** | Drive 未反映だった |
| ADR-2026-06-04-18 | **本ディレクトリ** | Drive 反映が Cloudflare WAF に阻まれて座礁していた |
| ADR-2026-08-25-20 | **本ディレクトリ** | Drive 未反映、`_docmap` 未登録だった |
| ADR-2026-05-23-07 / -05-31-14 / -05-31-15 | **本ディレクトリ** | Drive から取り直して移設（下記「Drive から取り直した 3 件」） |
| ADR-06 / 16 / 17 / 19 | Google Drive（folder `1LAENGwj…`。ADR-06 のみ Drive ルート直下 `18Joskr…`） | 未移設。Drive 側が本文の正本で、ローカル develop より進んでいる |
| ADR-09〜13 | `CLAUDE.md` §M | identity 系。もともと §M が SOT |

`spec/adr_index.yaml`（独立 naysayer と implementer に毎 summon 注入される索引）は id + title + `thread` + **`body:` locator** の派生ビューである。本体の所在は索引に載る ∴ 本ディレクトリに本体を足したら、その entry の `body:` を `repo:docs/adr/<file>.md` に更新すること。`docs/adr/` に本体があるのに `body:` が別を指す entry は CI が落とす（`*-amendment-*` は除外 — ADR-06 改訂メモは本体ではないため）。locator は「読み手がバイト列を開ける場所」の主張であって、どのコピーが正本かの主張ではない。

## なぜ移ってきたか

これまで ADR 本体は `DOCS_DEVELOP_LAYOUT_CONVENTION`（本リポジトリ `docs/spec/`、ADR-2026-05-23-07 §2.5 / §5 Q2 由来）に従い、**Drive を正本**とし、ローカルの develop 段リポジトリ（`spirrow-docs`、remote 無し）で編集して Takahito GO で Drive へ反映する運用だった。

この運用には 2 つの穴があった:

1. **Drive に届かなければ、その文書はどこにも残らない。** develop 段リポジトリは remote を持たないので、反映前の本文は 1 台のディスク上の 1 コピーしか存在しない。ADR-18 は三者収束 + Takahito Tier-C GO まで通った Accepted 文書でありながら、WAF に阻まれて 3 ヶ月この状態にあった。
2. **反映漏れが検出されない。** ADR-20 は `_docmap` にすら登録されず、`adr_index.yaml` からも落ちていた。

上の 4 件は Drive に無く、ローカルにしか無かったものである。Git に入れたことでこの穴は塞がった。

## 残っている作業

- **残り 4 件（ADR-06 / 16 / 17 / 19）をここへ移す。** 本文は Drive 側が新しいので、ローカルコピーではなく
  **Drive から取り直して**移す必要がある。7 / 14 / 15 は移設済み。別 PR。
  - ADR-06 は移設時に `tests/test_naysayer_adr_index.py::test_amendment_memo_does_not_force_a_repo_locator_on_adr_06`
    が赤くなる。**それが正しい挙動**で、同テストの失敗メッセージが指示するとおり ADR-06 を `drive` から `repo:` に
    動かし、フィクスチャを新しい現実に合わせて書き直すこと（改訂メモを drift check から外す規則自体は据え置く）。
- **`_docmap.yaml` の去就。** `scripts/gen_adr_index.py --docmap` の入力として今も docs host 上で使われている。ADR 本体が本リポジトリに揃えば、索引はリポジトリ自身から生成でき、`adr_index.py` が「loop host / CI に `_docmap` が無いので commit 済コピーは不可避」と記す制約（ADR-2026-06-04-19 N-2）が消える。生成器の変更を伴うので別途。
- **`docs/spec/DOCS_DEVELOP_LAYOUT_CONVENTION.md` の status。** 「Drive = doc の main」という前提そのものが、Spirrow ドキュメント基盤（Git 正本）に置き換わる方向にある。本 PR では本文を一切変更していないので、Draft のままそこにある。

## 注意

移設した 7 件は**一切改変していない**（md5 一致で確認）。したがって本文中の「ローカル develop」「Drive 反映は Takahito GO 後」といった記述は、書かれた当時の運用を指したままである。上の表と突き合わせて読むこと。

## Drive から取り直した 3 件（2026-09-10）

上の 7 件と違い、**この 3 件は Drive のバイト列そのままではない。** ホスト名 2 箇所を
プレースホルダに置換してある（[[platform:document-conventions]] §3.1 は「以後に書かれるもの、
**および Phase 2 で移行するもの**」に適用されると定めており、この移設はその Phase 2 に当たる）。

| ファイル | Drive fileId | Drive のサイズ / md5 | 置換 |
|---|---|---|---|
| `ADR-2026-05-23-07-stage3-autonomy-gating.md` | `1JPjXtl4IK8bhYclMAcY8YVaVsPzy0N7t` | 8707 / `3936353b8fd4e1fd6ba514b8f1102654` | §2.5 の実ホスト名 1 件 → `{{HOST_SERVICES}}` |
| `ADR-2026-05-31-14-gawa-method-withdrawal.md` | `16JC7hNAQEPioNygqVipNf2sZyCrZ0dDV` | 15559 / `5471883ae8d3d81de4f0a84266565a9e` | §1.1 の実ホスト名 1 件 → `{{HOST_LOOP}}` |
| `ADR-2026-05-31-15-independence-class-gradation.md` | `1ZyElmou_dgREPxp2y9l-BtJFQCGXJT7B` | 11765 / `5ec718575bdce62b1fc60241df471bbd` | なし（実インフラ値を含まない） |

**上の md5 は Drive 原本のもの**で、この repo のファイルのものではない。置換前のバイト列が
Drive のそれと一致していたことの証拠として残してある（サイズは Drive の `fileSize` メタデータと
一致を確認済み）。実値は `platform:infra-registry` にある。

**ローカルの develop 段リポジトリのコピーは使っていない。** 実測すると 3 件とも Drive と md5 が
違い、07 は 368 バイト、14 は 2471 バイト、15 は 1425 バイト短かった。
