---
spec_id: SPEC-2026-11-19-v14-definition-forms
thread: T-verify-v14-errors-on-main-and-spec-pin-residue
target_repo: spirrow-mindwire
base_branch: main
status: active
canary: not-applicable
supersedes:
  - SPEC-2026-09-20-pin-hardening-and-id-audit
obligations:
  - OBL-SPEC-PIN
  - OBL-SPEC-RECEIPT
  - OBL-SPEC-SCOPE-CLOSURE
items:
  - id: I-1
    title: "V-14 の定義子抽出を見出し形・suffix 形・bold 表セル・CommonMark インデントに拡張し、本 manifest と test を置く"
    paths:
      - "spec/design/T-verify-v14-errors-on-main-and-spec-pin-residue.md"
      - "spec/design/verify.py"
      - "tests/test_spec_design_verify_v14_forms.py"
---

# V-14 定義子抽出の改訂 — main 上の V-14 error 42 件のうち抽出誤り 29 件を解消する

## §0 この文書の読み方

**本 spec は差分 spec である。** **SPEC-2026-09-20-pin-hardening-and-id-audit** の後継（`supersedes`）で、§5-D の「定義子抽出」と A-35 の文言だけを差し替える。それ以外の条項はすべて継承する。先祖 spec のファイルは書き換えない（D-19）。論拠と経緯はスレッド `T-verify-v14-errors-on-main-and-spec-pin-residue` に残る（msg-9139 設計、msg-9140 / msg-9142 naysayer、msg-9141 修正版、2026-11-19 human 承認）。

**id namespace は先祖と共有する**（D-30 継承）。∴ スレッドで D-1〜D-6 と呼んだ決定は、未使用の D-40〜D-45 として採番する（D-1 → D-40 … D-6 → D-45）。受け入れ条件はスレッドの名前のまま AC-0〜AC-5 とする（`AC-` prefix は先祖で未使用）。他 spec の id を本文で引くときは inline code に入れる（V-14 の参照抽出から外すため）。

## §1 実測（AC-0 — 2026-11-19、`origin/main` `d3abf02`）

`python spec/design/verify.py --json` の V-14 error は 42 件。定義行の**書式**で主ラベルを 1 つ付け（優先順位: 見出し > 表セル > 行内 bold > 定義なし > その他）、`′` 以外の suffix の有無を別の列にした。

| manifest | 見出し | 表セル | 行内 bold | 定義なし | その他 | suffix あり |
|---|---|---|---|---|---|---|
| `SPEC-2026-08-11-design-spec-delivery` | 0 | 0 | 0 | 2 | 0 | 0 |
| `SPEC-2026-08-31-gate-reads-stale-base-diff` | 10 | 0 | 0 | 0 | 0 | 0 |
| `SPEC-2026-08-30-gate-silently-suppresses-approve-on-truncated-diff` | 8 | 11 | 0 | 11 | 0 | 12 |
| 計 | 18 | 11 | 0 | 13 | **0** | 12 |

「その他」は 0 件なので、AC-0 の停止条件には当たらない。

## §2 決定

- **D-40（見出し形の定義子）** ATX 見出し（行頭の空白 0〜3、`#` 1〜6 個、空白）の**最初の token** が id なら、それを定義子とする。id の直後には D-41 の suffix を許し、その後に空白・`—`・`（`・`(`・`:`・行末のいずれかを要求する。見出しの途中にある id は定義子にしない。
- **D-41（suffix は capture の外で許す。正規化はしない）** 定義子の capture は参照側と同じ `X-nn′?` とする。∴ `D-3` と `D-3′` は別の id のまま。参照側がすでに切り捨てている suffix（`″` `‴` `⁗`、上付き数字、`改`、ASCII `'`、英小文字 1 字の `(a)`）は、capture の後・区切りの前でだけ許す。こうすると `AC-14‴` の定義は `AC-14` になり、参照側の抽出結果と一致する。区切りの要求は緩めない（`**D-8 は…**` のような区切りの無い強調文は定義子にしない）。
- **D-42（表セル）** 行頭の空白 0〜3 の後の表行で、先頭セルが `X-nn` だけのもの、または対応する `**` で囲まれた `**X-nn**` だけのものを定義子とする（いずれも D-41 の suffix を許す）。開きだけ・閉じだけの `**` は受け付けない。行内 bold 形も、区切りが `**` の場合は開きと閉じが揃っている必要がある（既存の形どおり）。行頭の空白が 4 以上（または先頭 4 桁以内の tab）の行は CommonMark のインデントされたコードブロックなので、見出し形・表セル形・行内 bold 形のどれを試す前にも読み飛ばす（行頭に固定されない行内 bold 形から定義子が拾われないようにするため）。
- **D-43（残る error は exempt にしない）** 抽出を直した後に残る V-14 error は、`verify_exempt_ids` にも external override にも入れず、未解決のまま §4 の一覧に載せる。
- **D-44（missing-ancestor の重複）** main では再現しない。測った事実を §5 に記録して閉じる。コードは書かない。
- **D-45（変えないもの）** `verify.py` の exit code は 0 のまま（D-36）。CI gate にしない。参照パターン（A-42 の bound）は変えない。

## §3 受け入れ条件

- **AC-0（実装前の測定）** §1 の表。「その他」が 1 件でもあれば実装せずに止める。→ 0 件。
- **AC-1（通常実行と override 実行を分けて報告する）** (a) 通常実行の `verify.py --json` での V-14 error は、§4 の一覧と同じ 13 件。(b) override 実行は **A-35′** に従う。新しく解決した参照は §4 の解決表に、ファイル・行・suffix 付きの定義でしか解決しないかの印付きで列挙する。
- **AC-2（fixture）** 陽性: 見出し形、suffix 形（`″` `‴` `⁗` `⁵` `改` `(a)`）、bold の表セル、3 スペースのインデント。陰性: 見出しの途中の id、区切りの無い強調文中の id、4 スペースのインデント、開きだけ・閉じだけの bold セル。併存: `D-3′` の定義しか無いときの `D-3` 参照は error。`D-3` と `D-3′` の両方がある場合は、それぞれ別に解決する。`### D-8⁵` しか無い場合、`D-8` 参照は解決する。
- **AC-3** 本当に未定義な参照は、従来どおり V-14 error になる。
- **AC-4（親と後継を分ける）** (a) 親 A-34/A-38 の対象 `SPEC-2026-09-20-pin-hardening-and-id-audit` の通常実行の V-14 error は 0 件。(b) 本 manifest 自身の V-14 error は 0 件。親の達成は (a) だけから記録し、(b) からは導かない。
- **AC-5** `verify.py` の exit code は 0 のまま（A-41 の回帰防止）。
- **A-35′（A-35 の置き換え）** `SPEC-2026-08-11-design-spec-delivery` に external override `verify_exempt_ids: ["U-1"]` を与えて V-14 を実行すると、error は `G-4` の 1 件だけ残る。A-35 の「error 0」は数え違いだった（`G-4` は同 spec の `D-11` の見出しで参照されるだけで、定義行が無い）。

## §4 解決表と未解決一覧（AC-1）

**新しく解決した参照（29 件）。** 行番号はファイル先頭からの行。「suffix のみ」の印は、bare な参照が `′` 以外の suffix 付きの定義でしか解決しないものを示す。1 件ずつ確かめた結果、いずれも同じ項目の改訂系列（台帳 付録 B の `X → X″` の記録と一致）。

| manifest | 参照 | 解決した定義行 | 形 | suffix のみ |
|---|---|---|---|---|
| gate-reads-stale-base-diff | `D-1`〜`D-10` | L63 / L69 / L80 / L84 / L90 / L98 / L102 / L106 / L110 / L114 | 見出し | — |
| truncated-diff | `D-1` | L231 `### D-1 —` | 見出し | — |
| truncated-diff | `D-2` | L248 `### D-2″ —` | 見出し | ○ |
| truncated-diff | `AC-8` | L520 `**AC-8改**` | 表セル | ○ |
| truncated-diff | `AC-13` | L507 `**AC-13″**` | 表セル | ○ |
| truncated-diff | `AC-14` | L535 `**AC-14‴**` | 表セル | ○ |
| truncated-diff | `AC-16` / `AC-18` / `AC-19` / `AC-21` | L536 / L537 / L538 / L540（`⁗`） | 表セル | ○ |
| truncated-diff | `AC-20` | L539 `**AC-20改''**` | 表セル | ○ |
| truncated-diff | `AC-25` | L542 `**AC-25改'**` | 表セル | ○ |
| truncated-diff | `AC-26` | L510 `**AC-26改(b)**` | 表セル | ○ |
| truncated-diff | `AC-27` | L544 `**AC-27改**` | 表セル | ○ |
| truncated-diff | `B-1`〜`B-4`、`C-1`、`C-2` | L758 / L774 / L810 / L824 / L844 / L876 | 見出し（付録の節） | — |

**観察（抽出の誤りではない）:** truncated-diff の 付録 D は節見出しに `## D-1 触る` / `## D-2 触らない`（L901 / L909）を使っており、決定 `D-1` / `D-2″` と同じ id になっている。原文書の namespace の衝突で、本 spec の抽出規則はそれを持ち込んでも隠してもいない（`D-1` / `D-2` は L231 / L248 だけでも解決する）。

**未解決のまま残す V-14 error（13 件 — D-43）。**

| manifest | 参照 | 主な出現行 | 定義が無い理由 |
|---|---|---|---|
| design-spec-delivery | `G-4` | L93 | `D-11` の見出しで参照されるだけで、定義行が無い |
| design-spec-delivery | `U-1` | L64 | 叙述で解消した問い。同 spec には exempt が無い（exempt は後継の `verify_exempt_ids` にだけある） |
| truncated-diff | `AC-1`、`AC-2` | L748 / L772 | 退役した識別子（L772「識別子は永久に retire」） |
| truncated-diff | `D-2′` | L278 / L766 | TOMBSTONED。L766 の表セル `**D-2′ / …**` は定義形ではない |
| truncated-diff | `O-1`、`O-2`、`O-3` | L686 / L763 / L616 | 別スレッドの objection 番号で、本文書には定義が無い |
| truncated-diff | `V-1`、`V-2`、`V-3`、`V-5`、`V-10` | L60〜L66 / L672 | `verify.py` の検査 id。定義は別 spec にあるが、本文書の `supersedes` は空 |

## §5 missing-ancestor の重複（D-44 の記録）

- main（本 spec の着地前の `d3abf02`、および本 PR の branch）では、`supersedes` の先祖が見つからないことによる V-14 error は 0 件で、重複も 0 件。
- 合成した fixture（A → B → 存在しない X）では、`SPEC-B` を target とする同じ文言の error が 2 回出る（B 自身の検査で 1 回、A の chain walk で 1 回）。main では起きない形なので、D-44 に従ってコードは書かない。親スレッドの msg-3966 の本文は確認できておらず、これが同じ事象かどうかは確かめていない。

## §6 運用

- implementer は AC-0 の表と一緒に、D-40〜D-45 と AC-0〜AC-5 を read-back してから実装に入る（ADR-2026-05-29-13）。
- 本 manifest は先祖を `supersedes` するので、main 上では V-10 が `SPEC-2026-09-20-pin-hardening-and-id-audit` を superseded として warning する（D-19 で後継を立てる以上、構造上避けられない）。

## §7 検査されていないもの

- §4 の「suffix のみ」の解決が意味的に正しいかは、人が 1 件ずつ見た結果で、機械では検査していない。
- 将来の spec が D-41 の集合に無い revision mark を使った場合、その定義は従来どおり拾われず、V-14 error として見える（隠れる方向には倒れない）。
