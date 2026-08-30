# PR-gate: elision（宣言的除去）と評価失敗の可視化

**thread**: `T-gate-silently-suppresses-approve-on-truncated-diff`（spirrow-mindwire chatroom）
**target repo**: `spirrow-mindwire`
**base**: `main`
**status**: active — 設計は msg-2252（本体）/ msg-2259（抽出手続き）で独立 naysayer（Einstein）が clear 済み。**clearance は merge 承認ではない。** protected `main` への merge は Tier-C（Takahito）固定。
**この文書の著者**: Heisenberg（implementer）。msg-2260 §3 の 規律-14 により、記録アクセスを持つ側が抽出手続きを実行した。**設計の著者は Bohr（proposer）であり、本文書は設計を作っていない。**

---

## §0 この文書の読み方

### 0-1 なぜ存在するか

本スレッドは 62 通の chatroom メッセージからなる。**transcript は spec ではない。** transcript が読めるのは、それが積み上がるのを見ていた者だけである（msg-2254 §2）。msg-2253 で implementer は 54 通中 9 通しか読めない状態で実装を求められ、`OBL-DECLARE-UNREADABLE` に基づいて拒否した。その拒否は正しく、欠落は proposer 側の構造的欠陥として記録された（**error type 13**）。

本文書はその欠陥を恒久的に閉じる。**次の実装者は 62 通を再構成せず、このファイルを読む。**

### 0-2 自己完結性

**本文書は自己完結している。本文に無いものは、この仕様の一部ではない。** 論拠と検討経緯は thread に残る。ただし本文書は各項目の provenance（どの msg-id が触れたか）を全件持つので、経緯を追いたい者は §付録 A の被覆台帳から辿れる。

### 0-3 出所の 3 分類（msg-2254 §4）

各項目には出所を付す。

| 分類 | 意味 |
|---|---|
| **QUOTED** | 1 通のメッセージから逐語で取れる。再導出していない。 |
| **CONSOLIDATED** | 複数メッセージにまたがる差分連鎖から導出した。**再導出であり、fidelity review の対象**である。 |
| **UNRECOVERABLE** | 記録が決着させていない。**執行者の判断で埋めていない。** loop に返す（§付録 E）。 |

### 0-4 抽出手続き（msg-2258 §6 が正典）

本文書は以下の 10 手順で機械的に生成した。手続きは msg-2258 §6 が正典であり、msg-2260 §4 はその便宜的な再掲である。**両者が食い違う場合は記録が勝ち、差分を報告する**（§付録 F）。

1. 全メッセージを取得する。**件数は抽出時に読む。仮定しない。**
2. msg-id 昇順で処理する。各メッセージはちょうど 1 本の disposition 行を生む — 項目イベント、または `no normative content`。
3. イベントのタグ: `INTRODUCED(id)` / `MODIFIED(id)` / `SUPERSEDED(old → new)` / `TOMBSTONED(id, reason)`。
4. 累算器は `id → 本体 + provenance 連鎖` を保持し、イベントを順に適用する。
5. **live index = walk 終了時に TOMBSTONED でも SUPERSEDED でもない項目。**
6. **被覆表明**: 全 msg-id がちょうど 1 度 disposition 行として現れる。
7. **系譜表明**: 全 `SUPERSEDED` 辺が両端の msg-id を引用する。prime / 改 の表記だけに依存する辺は `CONSOLIDATED` と印し、fidelity review に回す。
8. **KAT を両方向、artefact を書く前に実行する。**
9. 取得不能なメッセージ → `UNRECOVERABLE`、loop に escalate する。
10. 被覆台帳と墓標付録を artefact と共に出荷する。

**実測件数 = 62。** msg-2256 §4 は「57」、msg-2253 §1 は「54」と書いている。手続き 1 に従い 62 を採り、差を §付録 F に報告する。

### 0-5 KAT の結果（手続き 8）

**陽性集合**（live index に現れなければならない。msg-2256 §5 + msg-2258 §3）:
`D-1` / `D-2″` / `D-3` / `D-3′` / `D-4` / `D-5` / `D-6` / `D-7` / 脅威モデルの受容（msg-2231 §5） / `AC-13″` → **全て surface した。PASS。**

**陰性集合**（live index に現れてはならない。msg-2258 §5）:
`D-8` / `D-8′` / `D-8″` / `D-8‴` / `D-8⁗` / 問い返し（query-back）根拠 / `AC-26改(c)` → **全て SUPERSEDED または TOMBSTONED。live index に無い。PASS。**

**両方向 PASS。** ∴ 手続き 8 の停止条件は発火せず、artefact を書いた。陽性集合の項目はいずれも台帳の walk が独立に surface したものであり、手で足していない（msg-2256 §5 の要求）。

---

## §1 本スレッドが直すもの / 直さないもの

### 1-1 起票（msg-1871、human、全数実測）

`#182` は 12 巡すべて非収束だった。原因は内容ではなく機構である。

```python
_MAX_DIFF_CHARS = 150_000                                    # pr_review.py:111
def _truncate_diff(diff: str) -> str:                        # :338
    return diff[:_MAX_DIFF_CHARS] + "\n\n[diff truncated]"
def _resolve_verdict(critique, *, truncated, finish_reason): # :302
    if truncated or finish_reason == "length":
        return ReviewEvent.REQUEST_CHANGES
    return _parse_verdict(critique)
```

R6（156,927 chars）で上限を跨ぎ、以降このゲートは `#182` に APPROVE を出せなくなった。**R10 と R12 では naysayer 本人が `VERDICT: APPROVE` と書いたのに、記録は `CHANGES_REQUESTED` になった。** そしてそのことはどこにも出なかった — GitHub 本文にも chatroom relay にも「切り詰めた」「PR を分割せよ」の文言が 1 度も無い（全 13 msg / 全 12 review を grep、ヒット 0）。

∴ implementer は「本文は APPROVE、判定は CHANGES_REQUESTED、理由の記載なし」を受け取り、**存在しない指摘を探して小さな修正を積んだ**。diff は R1 の 87,810 から R11 の 211,635 まで単調に増えた。**自己増悪ループである。**

### 1-2 SAFETY-VALVE（1 bit も緩めない）

> **truncation → force-RC は正しい設計である。** 部分しか見ていないレビューで gate を開けるほうが危険である。**争点は挙動ではなく、それが不可視であることだ。**
> — msg-1871 §1、msg-2030 で human が再確認（「ガードを弱める方向は、msg-1871 が safety valve として明示的に残したものなので、そこを崩す提案ではない」）

[QUOTED: msg-1871 §1 / msg-2030]

**本仕様は gate verdict を 1 bit も変えない。** `_decide_verdict` はゼロ変更、24 ケース matrix は bit-for-bit（AC-17）。

### 1-3 第 2 の起票（msg-2030、human、別リポジトリでの実測）

`SpirrowGames/spirrow-verimend#3` に PR-gate を 4 巡発火。4 巡目で naysayer は `VERDICT: APPROVE` を書き "I found no blocking flaws." と明記したが、記録は `REQUEST_CHANGES` になった。

| 項目 | 値 |
|---|---|
| raw diff（`gh pr diff` の実バイト数） | **208,120** |
| `_MAX_DIFF_CHARS` | 150,000 |
| 切り落とされた分 | **58,120** |
| **切り落とし位置以降の `diff --git` ヘッダ** | **0 本** |
| `uv.lock` の変更行 | 797 行（diff 全体の約 80%） |
| 人が書いた 20 ファイルの合計 | 約 41,000 chars（**全て cap の内側**） |

[QUOTED: msg-2030]

**human 自身の訂正**: この発火は `main` より 12 commits 古い checkout（`5970a48`、#186 を含まない）から行われた ∴ **本件は #186（可聴化）の効果について何も言っていない。** 言えるのは **cap の述語そのもの**についてだけである。

### 1-4 診断 — cap の値は壊れていない。cap が量っている対象が違う

`pr_review.py:109-110` のコメントは「a diff too big to see fully must not be approved」と書く。これは**「レビュー可能な表面が全部見えたか」**を言っている。実装が量っているのは**「diff 文字列の長さ」**である。生成物が 1 個入るだけで両者は乖離する（今回: 表面 41k、文字列 208k）。

∴ 直すべきは閾値ではなく、**閾値に食わせる量**である。[QUOTED: msg-2229 §3]

### 1-5 無害だったのは設計ではなく `u` の字のおかげである

git は path のバイト順で並べる。`uv.lock` の `u` (0x75) は `src/` の `s` (0x73)、`tests/` の `t` (0x74) より後ろ ∴ 人が書いた 20 ファイルが先に全部入り、余りを lockfile が食って、そこで切れた。

| lockfile | 先頭バイト | `src/` (`s`) との前後 |
|---|---|---|
| `Cargo.lock` | `C` (0x43) | **前** |
| `Gemfile.lock` | `G` | **前** |
| `go.sum` | `g` | **前** |
| `package-lock.json` | `p` | **前** |
| `poetry.lock` | `p` | **前** |
| `pnpm-lock.yaml` | `p` | **前** |
| `uv.lock` | `u` | 後 ← 今回 |

**verimend が poetry を使っていたら、167k の lockfile が先頭を食い、150,000 の切断は `src/` の内部に落ちていた。** 現行の述語は、被害の有無が path のソート順という無関係な変数に依存している。**無害な標本が 1 件出たことは、機構が安全であることの証拠にならない。** [QUOTED: msg-2229 §2]

### 1-6 非目標（明示）

- **`_MAX_DIFF_CHARS` の値の変更** — しない。150,000 のまま。値の再測定は 残余-1（elision 着地後、post-elision の分布に対して測る）。
- **gate verdict の変更** — 1 bit も動かさない。`_decide_verdict` ゼロ変更。
- **lockfile の意味的要約**（どの package が変わったか） — follow-up。形式ごとの parse が要る ∴ 本 PR に混ぜると OverScope。
- **非除去部分を file 境界で切る案** — 非目標。誰も組成を測っていない状態で `#182` 級の挙動を変えることになる。
- **shape check**（TOML/JSON らしさの判定） — 入れない（§2-10 脅威モデル）。
- **Checks API への書き込み** — 行わない（msg-2242 で受理した scope cut）。
- **`#182` / verimend#3 の処遇**（分割 / 上限引き上げ / merge） — **Takahito の Tier-C。本仕様では決めない。**
- **`T-naysayer-ledger-visibility`（issue #115）の文脈欠如** / **`T-verdict-echo-after-real-verdict` の echo 解析** — 別件。混ぜない。

---

## §2 設計決定（D 系列）

> **本系列は msg-2229 以降の名前空間である。** msg-1872 系（#186 で出荷済み）も `D-1`〜`D-6` を使っているが**別内容である**。両者の分離は §付録 E の `U-4` に UNRECOVERABLE として記録した。本節の `D-n` は msg-2229 系を指す。

### D-1 — elision（宣言的除去）と truncation（沈黙の切断）を別概念にする

| | 定義 | 帰結 |
|---|---|---|
| **truncation** | 文字位置で切る。どこで切れたか不定。モデルは失ったものの正体を知らない | **従来どおり無条件 force-RC** |
| **elision** | ファイル単位で本体だけを外し、**その事実・path・増減行数・バイト数を明示する** | **partial review ではない** |

**根拠（msg-2231 §1-a で差し替え済み。旧根拠は TOMBSTONED）**:

> **宣言された除去が partial review でないのは、モデルが問い返せるからではない。gate の review scope が、その artifact を含まないものとして定義されているからである。** scope は決定的・有界・開示済みであり、scope 内での欠落は従来どおり無条件 force-RC のままである。

∴ safety valve は 1 bit も緩まない。valve が守る対象が「diff 全体の完全性」から「**宣言された scope 内の完全性**」になり、scope 自体はコード内定数 ∴ **scope を広げる変更はそれ自体が gate のレビュー対象になる。**

**撤回された旧根拠（絶対に復活させないこと）**: 「モデルは欠けているものを知る ∴ 問い返せる」。**PR-gate は tool 無しの単一ターン評価器であり、問い返しは存在しない。** この pipeline に無い能力を根拠にしていた。[TOMBSTONED at msg-2231 §1-a、由来 msg-2229 §7]

[CONSOLIDATED: msg-2229 §4 D-1 + msg-2231 §1-a（根拠差し替え）]

### D-2″ — scope 判定（allowlist × 予算条件、all-or-nothing、同時評価）

**allowlist（exact basename。glob も拡張子マッチもしない。v1 の集合はこれで全部）**:

```
uv.lock  poetry.lock  Pipfile.lock  package-lock.json  yarn.lock
pnpm-lock.yaml  Cargo.lock  Gemfile.lock  composer.lock  go.sum
```

- **add / modify / delete を区別しない。** basename のみで判定する。
- allowlist はコード内定数 ∴ **集合を広げる変更は、それ自体が gate のレビュー対象になる。黙って育たない。**

**判定規則（fetch site で `DiffView` を組む時点、totals から一度だけ）**:

- `S_other` := allowlist に**属さない**ファイルの diff 文字数合計
- `ΣL` := allowlist に属するファイルの diff 文字数合計

| 条件 | 挙動 |
|---|---|
| `S_other + ΣL <= _MAX_DIFF_CHARS` | **全て保持。** elision を一切行わない。`D-elided` note も出さない |
| そうでないとき | **allowlist 該当ファイルを全て除去。** 部分集合は作らない（all-or-nothing） |

**仕様（実装注意ではない）**: **判定は集合全体に対して同時に (simultaneously) 一度だけ行う。** 「先に total を出す、それから全員の可否を決める」であり、「for 文の中で縮めながら判定する」ではない。逐次評価は結果を反復順（= 実質 path 順）に依存させ、§1-5 で殺した欠陥を裏口から戻す。

**最小部分集合（「cap に収まる分だけ除去」）は採らない。** knapsack であり同点時の tie-break が要る。任意の tie-break は恣意的か順序依存かのどちらかで、後者は順序不変性を壊す。all-or-nothing は決定的・冪等・順序不変で、`D-elided` note に一文で書ける。代償（cap 超過時には 200 文字の lockfile も 200,000 文字のそれと一緒に消える）は許容する — その時点での代替は truncation であり、`D-elided` は消したファイル名と文字数を全て開示する。

**保持 branch が到達可能であることは受入基準（AC-5）。** 到達不能なら「保持」は dead code であり、その場合は正直に「常に除去」と書くべきである。

**撤回された 2 つの narrowing（復活させないこと）**:
- `D-2` の **modification-only**（`new file mode` は除去しない）— out of model の攻撃者への部分的 guard を、in-model の確定デッドロック（初回 lockfile 導入 PR が永久に通らない）と交換していた。[TOMBSTONED at msg-2231 §2-a]
- `D-2′` の **`_ELIDE_MIN_CHARS` = 20,000**（静的な下限）— INV-REMEDY を、それを導入した当のメッセージの中で破っていた。19,000 の lockfile + 135,000 の code = 154,000 で truncation する。[TOMBSTONED at msg-2233 §0]

[CONSOLIDATED: msg-2229 §4 D-2 → msg-2231 §2-a/§4 → msg-2233 §2/§3/§4]

### D-3 — SOT は `DiffView` 側の除去記録。diff 本文中の stub は render 専用で、絶対に読み返さない

モデルに渡す diff には除去箇所を示す stub を差し込む（モデルはレビュー中に知る必要がある）。だが **notice に載る除去リストは `DiffView` の構造化フィールドからのみ生成し、diff テキストからは決して parse しない。** ∴ diff の中身が stub の書式を騙っても、notice の主張は汚染されない。

**冪等性はここから従う** — 我々は自分の出力を parse し直さない ∴ stub ブロックが `uv.lock` という語を含んでいても再除去は起きない（AC-8改）。

[QUOTED: msg-2229 §4 D-3 + msg-2235 §5（冪等性の根拠の書き直し）]

### D-3′ — stub は diff テキストの先頭 1 ブロック（inline をやめる）

D-7 により truncation は stub 注入の**前**に起きる ∴ 「切断点より後ろに位置していた除去ファイルの stub をどこに出すか」という位置問題が発生する。分岐で解くより、**位置を持たせない**。

- truncation との位置相互作用が**存在しなくなる**（分岐ゼロ）。
- モデルは位置ではなく scope を知る必要がある ∴ 先頭 1 ブロックで足りる（D-1 の足場は「モデルの気づき」ではなく「scope 宣言」）。
- diff の途中に gate 由来の行が紛れないので、モデルが stub を diff 本文と取り違える経路も消える。

**manifest ポインタ**（msg-2231 §1-c）はこのブロックの中で行う:
- `DiffView` が持つ diff 内ファイル一覧を見て、**manifest が diff に含まれるときだけ**それを名指しする。
- 含まれないときは「この変更に対応する manifest 変更は本 diff に無い（lockfile 単独の更新）」と**事実として**書く。
- `uv lock --upgrade` のように manifest を触らず lockfile だけが動く変更では、存在しないものを見ろと言うことになる ∴ これは INV-REMEDY の要求である。

[QUOTED: msg-2235 §5 + msg-2231 §1-c]

### D-4 — notice に新 marker `D-elided`

- **文面は除去の軸のことだけを言う。** 例: 「次のファイルは生成物として本体を表示していない: `uv.lock` (+797/-N, 167,xxx chars)。本レビューはそれ以外を全部見ている。」
- **`A-headroom` / `B-diff` の split 指示と衝突する主張を書かない**（round-6 規律）。特に**「∴ 分割は不要」と書かない**。共存しうる。
- critique への方向参照は **"below"**（round-7 規律）。
- **verdict と無関係に、除去があれば常に出す。** 承認したレビューでも「何を見ていないか」は記録に残す。
- **stub / notice は「この本体を見せろ / 見ないと判断できない」を誘発する語を持たない**（INV-REMEDY）。代わりに gate 自身の scope 宣言を書く:
  - ✅ 「本 gate は生成 lockfile の本体をレビュー対象としない。**この artifact の本体を見ることを verdict の条件にしないこと。**」← gate の scope に関する我々自身の宣言 ∴ 定義上真。
  - ❌ 「CI が整合を検証している」← 事実主張 ∴ **測るまで書かない**（補償(a) が未実測）。未測定の保証を prompt に注入することは、本スレッドが罰してきた行為そのものである。

[CONSOLIDATED: msg-2229 §4 D-4 + msg-2231 §1-b/§1-c]

### D-5 — cap と warn は post-elision の値で測り、raw も併記する

- `_MAX_DIFF_CHARS` / `_DIFF_WARN_THRESHOLD` の比較対象は除去後のサイズ。さもないと lockfile 持ちの repo が恒久的に headroom 警告に居座り、警告が無意味になる。
- ただし **raw サイズを消さない**。notice に `raw 208,120 → reviewed 41,xxx (elided 167,xxx)` を出す。「この PR は大きい」という事実自体は隠さない。

[QUOTED: msg-2229 §4 D-5]

### D-6 — 除去してもなお cap を超えるなら、従来どおり切って RC

backstop は現状のまま。**elision は real code を救出しない。**

[QUOTED: msg-2229 §4 D-6]

### D-7 — 予算は in-scope の量に対してのみ測る

`DiffView` 構築時、順に:

1. **scope を決める**（D-2″ の判定）
2. **in-scope テキストだけを測る。** `truncated := len(in_scope_text) > _MAX_DIFF_CHARS`。超過なら in-scope テキストを切る（D-6 不変）。
3. **gate 生成のメタデータ（stub ブロック / notice）は、truncation の判定・実行が終わった後に付ける。長さはいかなる比較にも入らない。**

∴ **系の中で cap と比較される数は 1 つだけになる — in-scope の長さ。** stub 長を予測する必要も、予測と実測を突き合わせる必要も消える。dual management は「両者を一致させる」ではなく「**片方を無くす**」で閉じる。

**採らなかった 2 案**:
- **stub 長を `S_other` に算入する** — 「開示にコストを払わせる」設計であり、非単調性を予算式に固定化する。scope 外の artifact が多いほど in-scope が痩せる ∴ INV-SCOPE-NEUTRAL に正面から反する。加えて描画長の**予測**が要り、予測式と描画コードが別々に育つ。
- **構造的に検証された diff では物理長チェックを迂回する** — **採ってはいけない。** `truncated` が測定値でなくなり**主張**になる。描画結果が実際に上限を超えていたら、`truncated=False` と宣言したうえで部分 view の APPROVE を通す。**本スレッドが殺した沈黙の、符号を反転させた版**である。

**帰属の事実（AC-11 で検証する）**: gate notice は `prepend_gate_notice` で GitHub review body の先頭に付くものであって、**モデルに渡す diff には 1 文字も入らない**。∴ 予算に効くのは **in-diff stub だけ**である。これは msg-2235 §1 時点での読みであり、実装時に一次照合する。

[QUOTED: msg-2235 §4]

### §3-ESCAPE-HATCH — 単一ファイルが cap を超えるとき

**単一の non-allowlist ファイルの diff だけで cap を超える**とき、`B-diff` は素の "Split the PR" を出さない。代わりに:

> 「`<path>` 単体が上限を超えている。**PR の分割ではこれは解消しない** ∴ 人の判断（Tier-C）が要る」

- これは diff-size 軸の中での**事実の精緻化**であって、他軸への主張ではない（round-6 規律を破らない）。
- 判定は **post-elision view の上で**行う。allowlist 単独が cap 超過なら D-2″ で除去され、ここには来ない。
- msg-2235 §2 が発見した blind band（単一ファイルが `(cap − stub 長, cap]` に落ちる帯域で hatch が鳴らないまま不履行可能な指示が出る）は、**D-7 が stub を予算から外したことで消滅した** ∴ 発火条件は `> cap` のままでよい。

[CONSOLIDATED: msg-2231 §3 + msg-2235 §2/§5]

### D-8⁵ — LLM 呼び出しが verdict を返さなかった場合（順序付き 4 規則）

LLM 呼び出し境界で例外が発生したとき、**上から評価する**:

| # | 条件 | 挙動 |
|---|---|---|
| **1** | **size-orthogonal と同定**（§2-9 の (A)、および (B) で実測済かつ route 一致） | `sent` に**関わらず** catch しない。bubble。**Reviews 不呼出。CI red。** |
| **2** | **`sent` が at-risk 帯**（1 に該当しない場合の全て。size-disguising も unclassifiable も含む） | **Reviews 投稿。** gate verdict `REQUEST_CHANGES`、model verdict = **not-evaluated**、marker `E-not-evaluated (payload-suspected)`。文面は AC-28 の 3 条件 |
| **3** | **帯外 かつ size-disguising と同定** | catch しない。bubble。CI red |
| **4** | **帯外 かつ 分類不能** | **Reviews 投稿。** `E-not-evaluated (unknown)`。文面「gate は評価できなかった。**これはあなたのコードに対する指摘ではない**。サイズは原因ではない（sent N chars）。1 回再実行し、繰り返すなら Tier-C」 |

**model verdict = not-evaluated** は既存 3 値（`APPROVE` / `REQUEST_CHANGES` / `UNPARSEABLE`）のどれでもない。`UNPARSEABLE` は「応答があったが読めなかった」であって「応答がなかった」ではない ∴ 流用しない。`ModelVerdict` に 4 値目を足すか `VerdictDecision` 側に持たせるかは実装判断（**AC-15**: #189 が同じ enum を触る予定 ∴ 先に状態を確認する）。

**「gate が評価して反対した」と「gate が評価できなかった」を混同させない。** 前者は critique が読める。後者は critique が存在しない ∴ implementer が「何を直せばいいのか」を critique に探しても何もない。marker が「critique は無い、なぜなら評価が走っていないから」と書けば、探索そのものが起きない。

**marker は 1 本の軸、cause は属性**:
- 軸: 「評価が成立しなかった」
- 値: `(payload-suspected)` / `(unknown)`
- **`(operational)` は存在しない**（Reviews に到達しないから marker を持たない）
- 属性値は `(payload)` ではなく **`(payload-suspected)`**。断定しない文面と marker が食い違ってはならない（round-6 規律）。

**catch の境界**: 「LLM 呼び出しから verdict を得るまで」の区間のみ。**GitHub API の失敗 / chatroom relay の失敗は射程外** — review を投稿する経路そのものが壊れている場合、review で知らせることは定義上できない（それを要求すると INV-REMEDY 違反になる）。

**Checks API への書き込みは行わない。** validation worker は orchestrator 級の credential も CI context も持たない（msg-2242）。operational 失敗の可視化は 残余-3（書ける層での設計）に落とし、AC-23 を前提条件に持つ。

**pre-flight token counting は第一機構にしない。** 「送る前に、送ったらどうなるかを計算する」は予測であり、tokenizer が backend と一致している保証がない（provider 側が変われば式は**静かに**間違う）。catch-and-synthesize が第一機構であり、pre-flight を使う場合は「実際に render 済みの payload を計測する」形に限り、**catch の代替ではなく上乗せ**とする。

**総縮退（AC-13″ が at-risk 帯を定義できない場合）**: **帯 = 全域とみなす。** ∴ 規則 3 が消え、規則 1 / 2 / 4 が残る。**この状態は正常な設定であって、劣化した設定ではない。** 設計が測定に賭けていないことをこの規則が保証する。

**サイズによる分岐は「予測」ではない**（msg-2245 §9 の対比表）:

| | pre-flight（却下済） | D-8⁵ |
|---|---|---|
| いつ | 呼び出し**前** | 呼び出しが**失敗した後** |
| 何を | 失敗するかを**予測**する | 送った量を**参照**する（既知の事実） |
| 何を決める | 呼ぶかどうか（**挙動**） | どの文面を出すか（**語**のみ） |
| 外すと | 呼ぶべき call を呼ばない | 文面がずれる。**feedback の有無は変わらない** |
| tokenizer 依存 | する | **しない**（chars を測るだけ） |

[CONSOLIDATED: msg-2237 §2（D-8）→ msg-2239 §3（D-8′）→ msg-2241 §4（D-8″）→ msg-2243 §4（D-8‴）→ msg-2245 §5（D-8⁗）→ msg-2247 §8（D-8⁵）+ msg-2249 §6（本体無変更の確認）]

### §2-9 例外の分類 — 軸は「直交性」ではなく「証明の所在」

「暗号的または物理的に直交」は*主張*であって*証明*ではない ∴ 未実測のまま「直交だ」と言えてしまう。軸を、主張できない形に変える。

| クラス | 定義 | 分類 |
|---|---|---|
| **(A) 送信前失敗** | 名前解決、TCP connect、TLS handshake、**body の 1 バイトも書く前に失敗したもの** | **size-orthogonal。** サーバ側の実測は不要 — サイズ非依存は**我々の側のクライアント例外型から証明できる**（サーバが body を見ていない ∴ サイズを判定しようがない） |
| **(B) サーバが返した応答** | 401 / 403 / 400 / 5xx | サーバは body を（一部でも）見ている ∴ **どれもサイズ判定でありうる。** AC-26改(b) の実測が要る。**未実測なら size-disguising** |
| **(C) body 送信中の切断** | reset / broken pipe | **(A) ではない。** WAF が過大 body を途中で切る挙動と区別できない ∴ **size-disguising 固定** |
| — | クライアントライブラリが (A) と (C) を区別できない場合 | **size-disguising**（INV-SILENCE-PROVEN） |

**総縮退**: 何も証明できないなら size-orthogonal = ∅。規則 1 が空になり、規則 2 / 3 / 4 だけが残る。

**(B) の測定は route / backend に束ねる。** 測定記録に測定時の route を**リテラル文字列**で併記し、**build 時のテスト**で route 定数との一致を assert する（**AC-31**）。runtime の provenance 照合は作らない — fail-safe な既定へ黙って倒れる機構は、自分の故障について沈黙する（規律-10）。

[CONSOLIDATED: msg-2247 §4（直交性軸）→ msg-2249 §4（証明所在軸に差し替え）+ msg-2251 §0/§4]

### §2-10 脅威モデルの受容（明示的な受容であって、見落としではない）

**shape check（TOML / JSON らしさの判定）は入れない。**

- その guard が守る相手は、msg-2229 §7 で **out of model と宣言した攻撃者**（push 権限を持ち能動的に隠す作者）である。宣言したまま設計に反映しないほうが不誠実である。
- 形式ごとの parse は「lockfile の意味的要約」follow-up と同じ作業であり、本 PR に入れれば OverScope。

∴ **コード内コメントに脅威モデルを明記する**: 「本 allowlist は path 申告を信頼する。想定する相手はループ自身の過失であって、能動的に隠す作者ではない。」

**受け入れではなく、明示的な受容として記録する。** allowlist は diff が申告する path で一致させる ∴ 「敵対的な作者が payload を lockfile 名に隠す」は原理的に可能である。

**併せて記録する穴（msg-2229 §7）**: lockfile の変更は hash / URL の差し替えという意味で**レビュー価値がある**。本設計はそれをモデルの視界から外す。緩和は「stub に path と増減を出す」だけであって、意味的な検査ではない。→ 補償(a)（CI が lockfile↔manifest 整合を検証しているか実測）。**本設計が作った穴の所在なので、記録しないことは許さない。**

[QUOTED: msg-2231 §5 + msg-2229 §7]

---

## §3 不変条件（INV 系列）

### INV-REMEDY

> gate が出すあらゆる**指示 (directive)** は、**pipeline 自身の挙動を前提として implementer が実行できる行動**を名指ししなければならない。pipeline が構造的に withhold するものを要求させてはならない。

**射程の確定（msg-2243 §1 で受理）**: **crash は指示ではない。指示の不在である。** ∴ INV-REMEDY は crash に対して何も言わない。

**背景**: 「gate が出した指示に対して、implementer が実行できる行動が存在しない」は `#182` の病そのものである — msg-1871 §3 の implementer は「存在しない指摘を探して小さな修正を積んだ」。症状は同じ（永久に閉じない周回）で、原因が「不可視」から「不履行可能」に移っただけである。

[CONSOLIDATED: msg-2231 §0（導入）+ msg-2243 §1（射程確定）]

### INV-BUDGET

> elision の narrowing は、**予算（`_MAX_DIFF_CHARS` との関係）の関数としてのみ**表現してよい。ファイル単体の静的性質（サイズ閾値、変更種別、拡張子など）を narrowing の述語に使ってはならない。**allowlist は narrowing ではなく scope の定義なので、この禁止の対象外。**

[QUOTED: msg-2233 §7]

### INV-SCOPE-NEUTRAL

> scope 外と宣言した artifact の存在は、同じ PR からそれを取り除いた場合と比べて gate の verdict を変えてはならない。**除去の開示もまた scope 外であり、予算を消費してはならない。**

[QUOTED: msg-2235 §3]

### INV-ESCAPE′

> 同一の障害が持続する**呼び出し系列**において、implementer が gate の remedy に従った結果として Tier-C 経路が失われてはならない。∴ ある remedy に従うと gate が沈黙帯へ遷移するなら、**その remedy をその文面で出してはならない。**

**旧 INV-ESCAPE は単発の述語だった**（「positively-classified-operational-outside-band を除く全ての失敗は Tier-C 経路を置く」）ので、系列上のトラップを 1 回ずつ検査すると両方とも「合格」してしまった。不変条件を書く軸が間違っていた。[SUPERSEDED: msg-2245 §5 → msg-2247 §3]

[QUOTED: msg-2247 §3]

### INV-SILENCE-PROVEN

> **沈黙に至る分岐に入ってよいのは、証明のある積極的同定だけである。** 未実測・分類不能・ライブラリが区別できない、はすべて**発話側**に倒す。「安全側に倒す」の安全側とは、**沈黙側ではなく発話側**を指す。

**根拠（後付けでない）**: 沈黙を許してよい帯は「失敗が PR 固有でない」帯である。トークン失効も名前解決失敗も、その PR だけでなく**全 PR を落とす** ∴ 障害として別経路（全件赤）で人に届く。逆に、サイズ依存の失敗は**その PR にだけ起きる** ∴ 沈黙すると誰にも届かない。∴ 「サイズ依存でないと証明できたときだけ黙ってよい」は、可視性の構造そのものから出てくる。

[QUOTED: msg-2249 §2]

### 沈黙帯の会計（AC-25改' が pin する）

| 帯 | 条件 | 可否 |
|---|---|---|
| **(i)** | **(A) 送信前失敗**、および **(B) で実測済かつ route 一致**のもの（帯の内外を問わず） | **可。** PR 固有でない ∴ 全件赤として別経路で人に届く。破壊的作業を誘発しない。ledger は zero review で fail-closed |
| **(ii)** | **帯外 × size-disguising** | **条件付きで可。** 帯内の呼び出しで既に Tier-C を配達済みの場合に限る。**初回から帯外で起きた場合は経路が無い → 残余-5** |

**未実測 (B) は、どちらの沈黙帯にも入らない。**

[CONSOLIDATED: msg-2247 §9 → msg-2249 §7]

---

## §4 受け入れ条件（AC 系列 — 完全な定義。差分ではない）

**blocking** の印がある AC は、実装完了の必要条件である。

### 実装前に測るもの（着手時、実装より先）

| AC | blocking | 定義 |
|---|---|---|
| **AC-0** | ○ | verimend#3 の `uv.lock` が **addition か modification か**を実測し、PR 本文に記録する。addition だった場合、「旧 D-2（modification-only）は動機事例で no-op だった」ことを明記する。**測る前に実装を始めない。** [QUOTED: msg-2231 §2-b] |
| **AC-3** | ○ | `#182` の **R5 (142,054) / R10 (201,829) をリプレイし、その組成を測って PR 本文に記録する。** 「#182 は real code だから従来どおり RC のはず」は**推測であって実測ではない** — 測らずに書かないこと。もし #182 も lockfile 支配だったなら、それは本 PR の中で黙って挙動を変えてよい話ではなく、**報告すべき発見**である。[QUOTED: msg-2229 §6-3] |
| **AC-13″** | ○ | backend の**実 input limit** を、in-scope（最大 cap）+ stub ブロック + prompt の合計に対して測り、**at-risk 帯を定義する**。`_MAX_DIFF_CHARS` は我々の方針定数であって物理限界ではない ∴ 別に測る必要がある。**測れない場合は D-8⁵ の総縮退規則（帯 = 全域）を適用したことを記録する。閾値を根拠なく置かない**（`_ELIDE_MIN_CHARS` の前例）。[CONSOLIDATED: msg-2235 §7（AC-13）→ msg-2237 §7（AC-13′、非 blocking 化）→ msg-2245 §10（AC-13″、blocking へ昇格）] |
| **AC-15** | ○ | **#189**（`VERDICT: COMMENT` の平坦化）の状態を確認し、`ModelVerdict` / `VerdictDecision` の変更が衝突しないことを実装スレッドで先に決める。[QUOTED: msg-2237 §7] |
| **AC-22** | ○ | **`ADR-2026-06-03-16` の本文**を実装時に確認し、「CI 赤 → APPROVE ではありえない」が D-8⁵ の前提として実際に成立することを記録する（この前提は Einstein の要約に基づく読みであり、本文は誰も読んでいない）。[QUOTED: msg-2241 §9] |
| **AC-26改(b)** | ○ | size-orthogonal への分類は**証明を要件とする**。**(A)** 送信前失敗はクライアント例外型で証明。**(B)** サーバ応答は route に対する実測で証明 — 当該 route に `_MAX_DIFF_CHARS` 超の payload を実際に送って返るコードを測定し、**401 / 403 が過大 payload で出ないことを確認してから** size-orthogonal に分類する。出るなら size-disguising 側へ移す。**証明が無いものはすべて size-disguising。** **(C)** 送信中切断は size-disguising 固定。ライブラリが (A)/(C) を区別できないなら size-disguising。[CONSOLIDATED: msg-2247 §12 → msg-2249 §10] |

### elision（D-1〜D-7）の受入条件

| AC | blocking | 定義 |
|---|---|---|
| **AC-4** | ○ | Einstein の反例そのもの。allowlist 19,000 + 非 allowlist 135,000 = 154,000 → 全除去 → post-elision 135,000 ≤ cap → **truncation なし、truncation 由来の force-RC なし**、`D-elided` note にファイル名と 19,000 が出る。**この literal な数値で回帰テストを書く。** [QUOTED: msg-2233 §6] |
| **AC-5** | ○ | **保持 branch の到達可能性。** 19,000 + 100,000 = 119,000 → **除去なし、`D-elided` note なし**、lockfile は model に渡る diff の中にある。[QUOTED: msg-2233 §6] |
| **AC-6** | ○ | **順序不変性（「`u` の字」回帰）。** 同一のファイル集合を、allowlist ファイルが `src/` より**前**に整列する名前（`Cargo.lock`）と**後**に整列する名前（`uv.lock`）の 2 通りで与え、**除去判定と post-elision サイズが完全一致**すること。[QUOTED: msg-2233 §6] |
| **AC-7** | ○ | **all-or-nothing。** allowlist 2 ファイルを含み cap 超過のとき、**両方**除去される（片方だけ残る出力が存在しない）。[QUOTED: msg-2233 §6] |
| **AC-8改** | ○ | **冪等。** post-elision の `DiffView` を規則に再投入して、追加除去が 0 であること。**根拠は「規則の不動点性」ではなく、D-3 により自身の出力を parse しないこと。** [CONSOLIDATED: msg-2233 §6 → msg-2235 §7] |
| **AC-9** | ○ | **境界そのもの。** `S_other` = `_MAX_DIFF_CHARS − 10` = **149,990**、allowlist ファイル 50,000 → 除去 → **`truncated=False`**、truncation 由来の force-RC なし。[QUOTED: msg-2235 §7] |
| **AC-10** | ○ | **INV-SCOPE-NEUTRAL / 単調性。** 同一の `S_other` について「allowlist ファイル有り / 無し」の 2 本を流し、**`truncated`・gate verdict・切断の有無が完全一致**すること。`S_other` は cap 直下（**149,990**）と cap 直上（**150,010**）の両方で行う。[QUOTED: msg-2235 §7] |
| **AC-11** | ○ | **帰属の検証。** gate notice が**モデルに渡る diff 文字列に含まれない**こと、stub ブロックが `truncated` の算出に**一切寄与しない**こと（`truncated` が in-scope 長のみの関数であることをテストで固定）。[QUOTED: msg-2235 §7] |
| **AC-12** | ○ | **blind band 回帰。** 単一 non-allowlist ファイル 149,950 + allowlist ファイル → **`truncated=False`**。150,001 単独 → **§3-ESCAPE-HATCH の Tier-C 文面**。[QUOTED: msg-2235 §7] |
| **AC-17** | ○ | **`test_decide_verdict_matrix_axes_and_oracle` が無編集で通ること。** D-8⁵ は `_decide_verdict` に入らない — verdict 決定の前段（呼び出しが返らなかった経路）にある ∴ matrix の 24 ケースはどれも通らない経路である。**これが崩れるなら D-8⁵ の配置が間違っている。** [QUOTED: msg-2237 §7] |

### 評価失敗（D-8⁵）の受入条件

| AC | blocking | 定義 |
|---|---|---|
| **AC-14‴** | ○ | payload 例外 → **Reviews に post され**、body に marker `E-not-evaluated (payload-suspected)` と縮小/分割 remedy が含まれる。**exit code ではなく post 内容を assert する。** [CONSOLIDATED: msg-2243 §6 + msg-2247 §12（marker 属性追随）] |
| **AC-16⁗** | ○ | **marker 共存規律。** `E (payload-suspected)` と `E (unknown)` は**排他**。`E` は `B-len` / `C-suppressed` と**共存しない**（verdict を得ていない ∴ 抑制も length 終了もあり得ない）。`E` は `D-elided` / `B-diff` と**共存しうる**（elide しても backend の input limit を超えた場合）。共存時、両者は同一軸内で矛盾しないこと（round-6 規律）。[CONSOLIDATED: msg-2237 §7 → msg-2243 §6 → msg-2245 §10 → msg-2247 §12] |
| **AC-18⁗** | ○ | AC-25改' の帯（沈黙帯 (i)）に限り、**Reviews API が 1 度も呼ばれない**こと。**呼び出し自体を assert する**（verdict 内容ではない）。[CONSOLIDATED: msg-2243 §6 → msg-2245 §10] |
| **AC-19⁗** | ○ | **unclassifiable + at-risk 帯外** → **Reviews に post される**。本文が (i)「**コードに対する指摘ではない**」を含み、(ii) **縮小/分割 directive を含まず**、(iii) **Tier-C 経路を名指しする**。marker は `E-not-evaluated (unknown)`。[CONSOLIDATED: msg-2245 §10（AC-19改 を置換）+ msg-2247 §12] |
| **AC-20改''** | ○ | **Tier-C prose を `E (payload-suspected)` と `E (unknown)` の両方に要求する。** [CONSOLIDATED: msg-2243 §6（AC-20改'）→ msg-2245 §10（AC-20改''）→ msg-2247 §12] |
| **AC-21⁗** | ○ | **bubble する経路で `VerdictDecision` を合成しない**こと（catch されない ∴ 経路自体が無いことの pin。except 節が広すぎないことの pin）。[CONSOLIDATED: msg-2241 §9 → msg-2243 §6 → msg-2245 §10] |
| **AC-24** | ○ | **unclassifiable + at-risk 帯内** → `E (payload-suspected)`、縮小/分割 + Tier-C。[CONSOLIDATED: msg-2245 §10 + msg-2247 §12] |
| **AC-25改'** | ○ | **沈黙帯が §3 の (i)(ii) の 2 つだけ**であることを pin する。**未実測 (B) が沈黙帯に入らないことを直接 pin する回帰**を含む。[CONSOLIDATED: msg-2245 §10 → msg-2247 §12 → msg-2249 §10] |
| **AC-26改(a)** | ○ | **size-orthogonal は `sent` に関わらず bubble**、**size-disguising / unclassifiable は帯に従う**。（帯内の 5xx は payload として扱われる = **サイズ判定が taxonomy 判定に優先する**ことの回帰テスト。ただし (a) の分類自体は AC-26改(b) の証明を要件とする） [CONSOLIDATED: msg-2245 §10（AC-26）→ msg-2247 §12（AC-26改(a)）] |
| **AC-27改** | ○ | **INV-ESCAPE′ の直接回帰を、単発ではなく系列で書く。** 例外を N 種注入し、**AC-25改' の帯以外の全ケースで Tier-C 経路を名指しする文が Reviews 本文に存在する**こと。[CONSOLIDATED: msg-2245 §10 → msg-2247 §12] |
| **AC-28** | ○ | **帯内 notice は** (i) **サイズを確定原因として断定しない**（error code がサイズを確認していない限り「サイズが要因の**可能性がある**（sent N chars）」まで）、(ii) **remedy 順序が 再実行 → 分割検討 → Tier-C**（安い → 破壊的）、(iii) **Tier-C 経路をその notice 自身に含む**（後続 notice に委ねない）。[QUOTED: msg-2247 §12] |
| **AC-29** | ○ | **cross-invocation anti-trap 回帰。** 同一の持続的 size-disguising エラーを **帯内 → 帯外**の 2 回で再生し、(a) 1 回目が Tier-C 経路を含むこと、(b) 2 回目が沈黙帯に入ること を pin。**(b) は既知の受容であり、テストに 残余-5 への参照コメントを埋める。** [QUOTED: msg-2247 §12] |
| **AC-30** | ○ | **INV-SILENCE-PROVEN の直接回帰。** 「分類不能」「未実測コード」「区別不能な接続エラー」の 3 系統を注入し、**いずれも Reviews が呼ばれ、Tier-C 経路を含む本文が出る**ことを assert。[QUOTED: msg-2249 §10] |
| **AC-31** | ○ | **(B) の測定記録に、測定時の route をリテラル文字列で併記する。** **build 時のテスト**で route 定数との一致を assert し、不一致ならビルドを落とす。runtime に分岐を追加しない。dynamic routing を前提にしない。**故障が観測される**（規律-10）。**リテラルであることを明記する** — 両辺が同一定数を読むと vacuous test になる ∴ 記録側はハードコードした文字列でなければならない。[QUOTED: msg-2251 §4。Einstein が msg-2252 で明示採択] |

### 後続 issue の前提（本 PR は blocking ではない）

| AC | blocking | 定義 |
|---|---|---|
| **AC-23** | ✗ | 後続 issue（残余-2 / 3 / 4 を束ねたもの）の着手前提として、**`ADR-2026-06-03-17` の worker/orchestrator 境界**と、**orchestrator の Checks 書き込み能力**を実測する。ここが否定されたら後続 issue の設計をやり直す。**本 PR はこの命題に一切依存しない。** [QUOTED: msg-2243 §6] |

### 24 ケース matrix は転記しない（msg-2254 §5）

`test_decide_verdict_matrix_axes_and_oracle` の 24 行を散文に写さない。**SOT はテストファイルである**（`tests/test_pr_review_driver.py` の `test_decide_verdict_matrix_axes_and_oracle` + `_oracle_gate_verdict`）。転記すればオラクルから乖離しうる第 2 の複製ができ、**乖離した複製は複製が無いより悪い** — テストが否定しない「権威ありげな誤答」になる。**本仕様はテストを名前で引用し、不変条件（無編集で通る）を述べる。それが転記より強く item を満たす。**

引用の一次照合（本 turn で実施、名前が実在することの確認のみ。AC の測定ではない）: `test_decide_verdict_matrix_axes_and_oracle` / `_oracle_gate_verdict` / `_MAX_DIFF_CHARS` / `_DIFF_WARN_RATIO` / `_MARKER_A_HEADROOM` / `_MARKER_B_DIFF` / `_MARKER_B_LEN` / `_MARKER_C_SUPPRESSED` / `prepend_gate_notice` / `_make_diff_view` / `DiffView` / `VerdictDecision` / `ModelVerdict` / `decide_verdict` — **全て `src/spirrow_mindwire/naysayer/pr_review.py` および `tests/test_pr_review_driver.py` に実在する**（base `b8b6a64`）。

---

## §5 起票する残余と補償（着手時）

| # | 内容 | 状態 |
|---|---|---|
| **残余-1** | **`_MAX_DIFF_CHARS` の値そのものの再測定。** elision 着地後、**post-elision の分布に対して**行う。いま raw を測っても捨てる数字になる ∴ 順序が固定されている | 未起票 |
| **残余-2** | **operational 失敗の反復が PR 上に蓄積されない。** gate が繰り返し停止していることを検知する観測点は、現状 Checks の履歴か sweep 側にしかない。必要なら Reviews ではない場所（chatroom relay / sweep）に置く | 未起票（残余-3 と同一 issue に束ねる） |
| **残余-3** | **operational 失敗時、赤い CI が理由を述べない。** 書ける層（orchestrator）での可視化設計。**AC-23 を前提条件に持つ** | 未起票（残余-2 と束ねる） |
| **残余-4** | **反復回数の自動カウントは本 PR に無い。** escalation は文面による人への指示であって機械的な counter ではない。counter が要るなら残余-3 と同じ issue に含める | 未起票 |
| **残余-5** | **持続的 operational 障害が、帯外の初回呼び出しで起きた場合、Tier-C 経路が一度も配達されない。** 赤い CI は出るが理由を言わない。総縮退状態（size-orthogonal ≈ ∅）では**露出が (B) クラス全体に広がる**。真の解は反復カウンタ（残余-4）か writable layer（残余-3） | 未起票。**AC-29(b) がテストで pin し、参照コメントを埋める** |
| **補償(a)** | **CI が lockfile↔manifest 整合（`uv lock --check` 等）を検証しているか実測。** 無ければ、本設計は **lockfile を誰も見ない状態**を作ったことになる。**本 PR の非目標だが、本設計が作った穴の所在なので記録しないことは許さない** | 未起票 |

**残余-6 は成立しない。** msg-2251 §5 は「AC-31 を採らないなら」を条件に立てたが、msg-2252 が AC-31 を明示採択した ∴ 条件不成立。記録は「残余-6 は消える」と明示していない ∴ 本文書は TOMBSTONED を **CONSOLIDATED** として付し、fidelity review に回す（§付録 B / F）。

**未起票のまま残っている旧スレッド由来の項目**: `T-unparseable-verdict-is-silent`（切り詰めも length cap も無く、単に `_parse_verdict` が verdict を取れなかった場合の沈黙。msg-1874 で名前だけ残された。**本スレッドの記録上、以後一度も触れられていない**）。

---

## §6 規律（loop の手続き規範）

本スレッドが生成した規律。番号は **6 から始まる** — 規律-1〜5 は本スレッドの corpus に存在しない（§付録 E `U-3`）。

| # | 内容 | 出所 |
|---|---|---|
| **規律-6** | 「未実測の命題を支柱に使わない」は、支柱を**差し替える**より、**支柱が要らない形に設計を切る**ことで満たすほうが上位である | msg-2243 §2 |
| **規律-7** | 設計要素を削るときは、その要素を**根拠に挙げている過去の判断を逆引きし、全て再導出する。** 削除の影響は削除箇所には現れない。**それを根拠にしていた場所に現れる** | msg-2245 §12 |
| **規律-8** | gate が出す文面を設計するときは、「implementer がその文面に従った後の世界で、同じ gate がどう振る舞うか」を必ず一巡させる。**文面は注記ではなく作用素である** | msg-2247 §11 |
| **規律-9** | ある保護（AC / invariant）を論証に使うときは、**「その保護が実行されるコードパスに、いま議論している分岐が到達するか」を先に照合する。到達しない保護を数えない** | msg-2249 §9 |
| **規律-10** | **fail-safe な既定へ倒れる機構は、非既定方向を直接 pin するテストを伴うときにのみ作ってよい。** 伴わないなら作らない。「既定が安全だから壊れても無害」は、その機構を作ってよい理由にならない | msg-2251 §3 |
| **規律-11** | **閉じた設計は記録からのみ lift してよく、著者の記憶から lift してはならない。** 著者の記憶は、それが再現すると主張する当のものと照合できない唯一の出所である。「これは正しく覚えている」は誰にも検証できない性質 ∴ 免除にならない | msg-2254 §3 |
| **規律-12** | **lift の manifest は記録から導出する。記録を読めない当事者の要求からは導出しない。** 要求は need を定義する。scope は定義しない | msg-2256 §3 |
| **規律-13** | **抽出・変換の手続きは、採用前に、自身の corpus の中で最も難しい既知ケースに対して dry-run する。** ここでのそれは D-8 連鎖であり、`union` を 1 パスで反証する | msg-2258 §1 |
| **規律-14** | **記録由来の手続きの執行者は、著者ではなく記録アクセスで選ぶ。誰が実行するかに正しさが依存する手続きは、そもそも機械的ではなかった** | msg-2260 §3 |

**番号の無い規律（本設計に対して同じ拘束力を持つ）**:

| 内容 | 出所 |
|---|---|
| **cross-axis 禁止（round-6 規律）**: 各不変条件・各注記は、**自分の軸のことしか主張してはならない**。他軸の注記の不在や原因を述べた瞬間、それは不変条件ではなく偶然の同時分布の記述になる | msg-1876 §O-3、msg-1895 で文面にも適用 |
| **directional prose（round-7 規律）**: notice は prepend される ∴ critique への方向参照は **"below"**。ブロック内の兄弟 marker への参照だけが "above" | msg-1920 |
| **AC は境界に置く**: 境界条件を主張する AC は、境界から離れた代表値ではなく**境界そのもの**（cap、cap±1、cap−ε）に置く。余裕のある代表値で通る AC は、境界の主張について何も証明していない | msg-2235 §8 |
| **AC は implementer が実際に読む観測点で書く**: exit code / ログ / 内部状態ではなく、**implementer の目に触れる出力**（GitHub review body、chatroom message）を assert する。「起きたこと」ではなく「**届いたこと**」を測る | msg-2237 §8 |
| **単純化は gate の内部構造に対して行ってよい。implementer に届く文面に対して行ってはならない**。内部の分岐を減らすことと、implementer が受け取る区別を減らすことは別の操作であり、後者は常に情報の削除である | msg-2239 §10 |
| **相手の反論が自分の過去の主張と矛盾して見えたとき、まず「自分が相手の区別を潰していないか」を疑う** | msg-2241 §10 |
| **実装は別スレッドで立てる**（設計レビューが完結した段でスレッドを分ける。起票時に開く） | msg-2229 §8、msg-2251 §9 |
| **scope を狭めたうえで残余を起票する判断は Tier-C ではない**。scope を**広げる**判断でも残余を**捨てる**判断でもないため、proposer の権限内 | msg-2243 §8 |

---

## §7 土台 — #186 で既に出荷済みのもの（Phase A）

本設計はゼロからではなく、**PR #186（merge commit `5c00404b`、head `8ac2b47`、APPROVE は msg-1922、merge は Takahito が Tier-C として 2026-08-27T20:07:48Z に実施）** の上に建つ。以下は**既に main に存在する**。壊さないこと。

- **`DiffView(text, original_chars, limit, truncated)`** — `original_chars` は切り詰め**前**に測る。fetch site で 1 度だけ構築される（`_make_diff_view`）。
- **`ModelVerdict`**（3 値: `APPROVE` / `REQUEST_CHANGES` / `UNPARSEABLE`）と **`VerdictDecision(model_verdict, gate_verdict, suppressed, reasons, view)`**。`decide_verdict(critique, view, finish_reason)` が唯一の解決点。
- **`suppressed := (model_verdict == APPROVE) and (gate_verdict == REQUEST_CHANGES)`** — 厳密定義。`RC × RC` / `UNPARSEABLE × RC` は suppression ではない。
- **`truncated := original_chars > limit`** — `limit` ちょうどは切り詰めではない。
- **marker 集合**: `<!-- mindwire:gate-notice v1 -->` sentinel + `A-headroom` / `B-diff` / `B-len` / `C-suppressed`。**不変条件は散文の語ではなく marker に対して書く**（"split" 部分文字列で書くと A と B-diff が衝突する）。
- **軸の直交性**: 入力量（`original_chars` → A / B-diff）/ 出力量（`finish_reason` → B-len）/ 判定（→ C-suppressed）。**A と B-diff だけが排他**（同一スカラーの帯域分割）。それ以外は全て共存しうる。
- **verdict の 2 行はブロックのヘッダが所有する** — どの注記も所有しない ∴ 共存しても重複しない。
- **notice は prepend**（append ではない）。**GitHub 本文と relay 本文は同一レンダラ出力**。relay `tags` は入れない（dual management）。
- **`_DIFF_WARN_RATIO = 0.8`** — 絶対値ではなく比率。
- **`OBL-NO-POLLUTING-PR-HEADER`**（`spec/process/obligations.yaml`、1 行 + テスト参照）。機械的な執行は「平常時は sentinel 不在」テストが担う。
- **`test_decide_verdict_matrix_axes_and_oracle`** + **`_oracle_gate_verdict`** — 24 ケース全数で gate verdict が変更前実装と一致することを固定する。

**未消化の follow-up（本設計の対象外だが、AC-15 が参照する）**:
- **issue #189** — `VERDICT: COMMENT` が notice header で `REQUEST_CHANGES` と報告される（判定は正しく、報告が嘘）。`ModelVerdict` は三値のまま、逐語トークンを display-only の別フィールドで持つ案。**injection 対策（長さ clamp、marker 偽造・引用ブロック脱出・改行注入の不能化）が必須。**
- **issue #190** — PR-gate の naysayer が #186 のコードをいつ読み始めるか（デプロイ経路の確認）。第一 SOT は `ADR-2026-06-04-18`、二次は `ADR-2026-06-03-17`。**「ADR にこう書いてあった」で閉じてはならない** — ADR は意図したトポロジを記述し、issue が答えるべきは実状である。

---

## §8 route（この artefact の次）

1. **本 artefact → Einstein の fidelity review。** 検査対象は「lift が msg-2252 / msg-2259 が clear したものと一致しているか」「CONSOLIDATED 項目がその差分連鎖から逸脱していないか」であり、設計の再オープンではない（msg-2255 §7.1 で settled）。
2. **UNRECOVERABLE 項目（§付録 E）は live な設計問題として loop に返る。** 執行者の判断で埋めていない（msg-2255 §7.2 で settled）。
3. その後に code。**着手時の測定（AC-0 / AC-3 / AC-13″ / AC-15 / AC-22 / AC-26改(b)）は fidelity review の後に行う。AC-0 は D-2″ に対して blocking のまま。**
4. **merge は Tier-C（Takahito）。loop は merge しない。** Einstein の clearance は「code work may proceed」であって merge 承認ではない（msg-2252 / msg-2259 が自ら明記）。

---

# 付録 A — 被覆台帳（手続き 6）

**全 62 msg-id がちょうど 1 度ずつ現れる。** 「no normative content」は明示的な disposition であって省略ではない。

| # | msg-id | author | disposition |
|---|---|---|---|
| 1 | msg-1871 | human | INTRODUCED 起票要件 §6-1/2/3、SAFETY-VALVE（§1）、NONGOAL(`_MAX_DIFF_CHARS` の値 / `#182` の処遇 / #115 / echo 解析）、自己増悪ループの実測（R1 87,810 → R11 211,635） |
| 2 | msg-1872 | Bohr | INTRODUCED A/D-1〜A/D-6、不変条件 v1（テスト 1〜8）、実装サイズ上限 400 行、一次照合義務 3 項目、却下案 3 件、争点 Q1〜Q5 |
| 3 | msg-1873 | Einstein | 自身の項目イベントなし。O-1（`suppressed` 厳密化）/ O-2（relay `tags` 撤回）を提起し、msg-1874 で MODIFIED / TOMBSTONED として着地。`OBL-NO-POLLUTING-PR-HEADER` の名称を提案 |
| 4 | msg-1874 | Bohr | MODIFIED A/D-2（`suppressed` を厳密定義）、TOMBSTONED A/D-5 の relay `tags`、SUPERSEDED(A/B → A/B-diff + A/B-len)、INTRODUCED `OBL-NO-POLLUTING-PR-HEADER`、INTRODUCED `T-unparseable-verdict-is-silent`（scope 外、未起票）、SUPERSEDED(不変条件 v1 → v2) |
| 5 | msg-1875 | Einstein | 自身の項目イベントなし。不変条件 4 の非直交性を指摘し、msg-1876 で SUPERSEDED として着地 |
| 6 | msg-1876 | Bohr | SUPERSEDED(不変条件 v2 → v3、marker ベース)、INTRODUCED A/MARKERS（sentinel + 4 marker）、INTRODUCED cross-axis 禁止（round-6 規律の原型）、INTRODUCED `truncated` の境界定義、INTRODUCED 24 ケース matrix |
| 7 | msg-1877 | Einstein | no normative content（設計 clear） |
| 8 | msg-1878 | Heisenberg | no normative content（実装報告 / PR #186 R1）。宣言された逸脱: diff 651+/13− vs 予算 400 行 |
| 9 | msg-1881 | pr-gate-relay | no normative content（R2 指摘: `_resolve_verdict` の dual management） |
| 10 | msg-1883 | Heisenberg | no normative content（R2 修正報告 `7e70b30`） |
| 11 | msg-1885 | pr-gate-relay | no normative content（R3 指摘: 三重 truncation と嘘のコメント） |
| 12 | msg-1886 | Heisenberg | no normative content（R3 修正報告 `27c1e94`） |
| 13 | msg-1888 | pr-gate-relay | no normative content（R4 指摘: scoped script が notice を付けない） |
| 14 | msg-1889 | Heisenberg | no normative content（R4 修正報告 `1dd8717`） |
| 15 | msg-1891 | pr-gate-relay | no normative content（R5 指摘: `_parse_verdict` が dead wrapper） |
| 16 | msg-1892 | Heisenberg | no normative content（R5 修正報告 `570d6f4`） |
| 17 | msg-1894 | pr-gate-relay | no normative content（R6 指摘: B-len の cross-axis 矛盾） |
| 18 | msg-1895 | Heisenberg | MODIFIED A/MARKERS（B-len から cross-axis 句を削除）。**cross-axis 禁止を文面に適用（round-6 規律の確立）** |
| 19 | msg-1919 | pr-gate-relay | no normative content（R7 指摘: prepend 後も "above" と書いている） |
| 20 | msg-1920 | Heisenberg | MODIFIED A/MARKERS（方向参照を "below" に）。**directional prose 規律（round-7）の確立** |
| 21 | msg-1922 | pr-gate-relay | no normative content（APPROVE。weakest point として `COMMENT` 平坦化を名指し） |
| 22 | msg-1925 | human | **Tier-C 決定 B**: #186 を現状のまま merge し、`COMMENT` 平坦化は follow-up として記録する |
| 23 | msg-1936 | Bohr | INTRODUCED merge 3 条件（head `8ac2b47` / 両 check green / protected `main` なら拒否して報告）、INTRODUCED issue #1 草案（`COMMENT` 平坦化）、INTRODUCED issue #2 草案（デプロイ経路）、INTRODUCED 残余（`_MAX_DIFF_CHARS` 再測定、起票しない） |
| 24 | msg-1937 | Einstein | 自身の項目イベントなし。issue #2 の framing に反対し、msg-1938 で SUPERSEDED として着地 |
| 25 | msg-1938 | Bohr | SUPERSEDED(issue #2 草案 → 差し替え版。`ADR-2026-06-04-18` を第一 SOT に必須化しつつ「ADR で終わらせない」を明記) |
| 26 | msg-1939 | Einstein | no normative content（clear。merge 権限を持たないことを明記） |
| 27 | msg-1940 | Heisenberg | 履行報告: merge は Takahito が実施済（head 一致、両 check green）、issue **#189** / **#190** 起票済。コード差分ゼロ |
| 28 | msg-1955 | human | **Tier-C 決定 A**: msg-1940 を完了として受理しスレッドを閉じる。`_MAX_DIFF_CHARS` は未起票のまま、`#182` は本スレッド外 |
| 29 | msg-1957 | Heisenberg | no normative content（決着報告。`NEXT: none`） |
| 30 | msg-2030 | human | **スレッド再開。** INTRODUCED verimend#3 の実測（raw 208,120 / cap 150,000 / 切り落とし 58,120 / 以降の `diff --git` 0 本 / `uv.lock` 797 行 ≈80% / 人が書いた 20 ファイル ≈41,000 chars）、INTRODUCED cap 述語の欠陥、SAFETY-VALVE を再確認、自己訂正（`5970a48` からの発火 ∴ #186 の可聴化については何も言えない） |
| 31 | msg-2229 | Bohr | INTRODUCED D-1〜D-6、ソート順の発見（`u` の字）、受入条件 §6-1〜9、query-back 根拠、非目標集合、実装スレッド分離規律 |
| 32 | msg-2230 | Einstein | 自身の項目イベントなし。2 つの deadlock（query-back の不履行可能性 / 初回 lockfile 追加）を提起し、msg-2231 で TOMBSTONED として着地 |
| 33 | msg-2231 | Bohr | INTRODUCED **INV-REMEDY**、TOMBSTONED query-back 根拠（D-1 の足場を scope 宣言へ SUPERSEDED）、TOMBSTONED D-2 の modification-only、TOMBSTONED §6-7（新規 lockfile は除去されない）、INTRODUCED D-2′（`_ELIDE_MIN_CHARS`）、INTRODUCED **AC-0**、MODIFIED AC-2 → AC-2改、INTRODUCED §3-ESCAPE-HATCH、INTRODUCED **脅威モデルの受容**、INTRODUCED 補償(a) 素案、MODIFIED D-4/stub（manifest ポインタ） |
| 34 | msg-2232 | Einstein | 自身の項目イベントなし。`_ELIDE_MIN_CHARS` の 19,000+135,000 反例を提起し、msg-2233 で TOMBSTONED として着地 |
| 35 | msg-2233 | Bohr | TOMBSTONED D-2′、SUPERSEDED(D-2 → **D-2″**)、INTRODUCED 同時評価の仕様化、INTRODUCED all-or-nothing の根拠、INTRODUCED **INV-BUDGET**、INTRODUCED **AC-4 / AC-5 / AC-6 / AC-7 / AC-8**（新採番） |
| 36 | msg-2234 | Einstein | 自身の項目イベントなし。stub 長が物理予算を跨ぐ反例を提起し、msg-2235 で D-7 として着地 |
| 37 | msg-2235 | Bohr | INTRODUCED **INV-SCOPE-NEUTRAL**、INTRODUCED **D-7**、INTRODUCED **D-3′**、INTRODUCED **AC-9 / AC-10 / AC-11 / AC-12 / AC-13**、MODIFIED AC-8 → AC-8改、INTRODUCED「AC は境界に置く」、§3 の blind band を発見 |
| 38 | msg-2236 | Einstein | 自身の項目イベントなし。AC-13 の「error にする」が crash = 沈黙になることを提起し、msg-2237 で D-8 として着地 |
| 39 | msg-2237 | Bohr | SUPERSEDED(AC-13 → AC-13′)、INTRODUCED **D-8**、INTRODUCED **AC-14 / AC-15 / AC-16 / AC-17**、INTRODUCED「AC は implementer が読む観測点で書く」 |
| 40 | msg-2238 | Einstein | 自身の項目イベントなし。transient を RC にするのは INV-REMEDY 違反と提起し、msg-2239 で D-8′ として着地 |
| 41 | msg-2239 | Bohr | SUPERSEDED(D-8 → **D-8′**)、MODIFIED AC-14 → AC-14改、INTRODUCED **AC-18 / AC-19 / AC-20**、INTRODUCED「単純化は文面に対して行わない」 |
| 42 | msg-2240 | Einstein | 自身の項目イベントなし。ledger は zero review で fail-closed（§4(a) は事実誤り）+ channel category error を提起し、msg-2241 で着地 |
| 43 | msg-2241 | Bohr | TOMBSTONED msg-2239 §4(a) の fail-open 論拠、SUPERSEDED(D-8′ → **D-8″**)、SUPERSEDED(AC-14改 → AC-14″)、SUPERSEDED(AC-18 → AC-18″)、MODIFIED AC-19、MODIFIED AC-20 → AC-20改、INTRODUCED **AC-21 / AC-22**、INTRODUCED **残余-2**、INTRODUCED「未実測の印を支柱にしない」「相手の区別を潰していないか疑う」 |
| 44 | msg-2242 | Einstein | 自身の項目イベントなし。worker は Checks に到達できない（OverScope）と提起し、msg-2243 で scope cut として着地 |
| 45 | msg-2243 | Bohr | MODIFIED **INV-REMEDY**（directive のみに射程確定）、SUPERSEDED(D-8″ → **D-8‴**)、TOMBSTONED AC-14″/AC-18″ の operational 半分、TOMBSTONED AC-20改、INTRODUCED AC-14‴ / AC-18‴ / AC-19改 / AC-20改' / AC-21改 / AC-16改 / **AC-23**、INTRODUCED **規律-6**、INTRODUCED **残余-1 / 残余-3 / 補償(a) の採番**、INTRODUCED「scope 縮小は Tier-C ではない」 |
| 46 | msg-2244 | Einstein | 自身の項目イベントなし。escape hatch を削ったまま既定を据え置いた deadlock を提起し、msg-2245 で D-8⁗ として着地 |
| 47 | msg-2245 | Bohr | SUPERSEDED(D-8‴ → **D-8⁗**)、INTRODUCED **INV-ESCAPE**、SUPERSEDED(AC-13′ → **AC-13″**、blocking へ昇格)、SUPERSEDED(AC-19改 → AC-19⁗)、INTRODUCED **AC-24 / AC-25 / AC-26 / AC-27**、MODIFIED AC-18‴→AC-18⁗ / AC-20改'→AC-20改'' / AC-21改→AC-21⁗ / AC-16改→AC-16⁗、INTRODUCED **規律-7**、INTRODUCED **残余-4** |
| 48 | msg-2246 | Einstein | 自身の項目イベントなし。401 @149,000 の cross-invocation トラップを提起し、msg-2247 で D-8⁵ として着地 |
| 49 | msg-2247 | Bohr | SUPERSEDED(D-8⁗ → **D-8⁵**)、SUPERSEDED(INV-ESCAPE → **INV-ESCAPE′**)、INTRODUCED size-orthogonal / size-disguising の分割（直交性軸）、MODIFIED AC-26 → AC-26改(a)(b)、MODIFIED AC-25 → AC-25改、MODIFIED AC-27 → AC-27改、INTRODUCED **AC-28 / AC-29**、MODIFIED marker 属性 `(payload)` → `(payload-suspected)`（AC-16⁗ / 18⁗ / 19⁗ / 20改'' / 21⁗ / 24 に追随）、INTRODUCED **規律-8**、INTRODUCED **残余-5** |
| 50 | msg-2248 | Einstein | 自身の項目イベントなし。未実測コードを size-orthogonal に倒すと規則 1 が AC-28 を迂回すると提起し、msg-2249 で着地 |
| 51 | msg-2249 | Bohr | INTRODUCED **INV-SILENCE-PROVEN**、MODIFIED AC-26改(b)（証明を要件化）、INTRODUCED AC-26改(c)、SUPERSEDED(直交性軸 → **(A)/(B)/(C) 証明所在軸**)、MODIFIED AC-25改 → AC-25改'、INTRODUCED **AC-30**、INTRODUCED **規律-9**、MODIFIED 残余-5（露出が (B) クラス全体に拡大） |
| 52 | msg-2250 | Einstein | 自身の項目イベントなし。AC-26改(c) は OverScope かつ非既定方向が未 pin と提起し、msg-2251 で TOMBSTONED として着地。§12.2 / §12.3 を settled にする |
| 53 | msg-2251 | Bohr | **TOMBSTONED AC-26改(c)**、INTRODUCED **規律-10**、INTRODUCED **AC-31**（分離可能）、INTRODUCED 残余-6（AC-31 却下時のみ成立する条件付き）、SETTLED §12.2（(A)/(C) の粒度を測らないことは許容）/ §12.3（規則 1 を残す） |
| 54 | msg-2252 | Einstein | **設計 clear（本体）。AC-31 を明示採択** ∴ (CONSOLIDATED) TOMBSTONED 残余-6（条件不成立）。merge 承認ではないことを明記 |
| 55 | msg-2253 | Heisenberg | 設計項目イベントなし。`OBL-DECLARE-UNREADABLE` に基づく拒否（54 通中 45 通が elided）。INTRODUCED artefact 要求 §6（**暫定 manifest**。msg-2256 §6 で暫定と格付けされる） |
| 56 | msg-2254 | Bohr | INTRODUCED **規律-11**、INTRODUCED error type 13、INTRODUCED artefact 仕様（path / QUOTED・CONSOLIDATED・UNRECOVERABLE / 24 ケース matrix は転記しない / コード無しの第 1 commit）、INTRODUCED route（artefact → Einstein fidelity → Heisenberg） |
| 57 | msg-2255 | Einstein | SETTLED §7.1（fidelity review は naysayer の scope）/ §7.2（UNRECOVERABLE は loop へ返す）。§6 が D-1〜D-7 と脅威モデルを落としていると提起し、msg-2256 で MODIFIED として着地 |
| 58 | msg-2256 | Bohr | INTRODUCED **規律-12**、INTRODUCED error type 14、SUPERSEDED(要求由来の manifest → **記録由来の manifest + 被覆台帳**)、INTRODUCED **KAT 陽性集合** |
| 59 | msg-2257 | Einstein | 自身の項目イベントなし。`union` は死んだ節を蘇生させると提起し、msg-2258 で fold として着地。**KAT 陰性集合**を供給 |
| 60 | msg-2258 | Bohr | INTRODUCED **規律-13**、INTRODUCED error type 15、SUPERSEDED(union → **時系列 fold**)、INTRODUCED **4 タグ集合**、INTRODUCED **系譜表明**、INTRODUCED **墓標付録**（分離可能）、INTRODUCED AC-13″ の anti-KAT、INTRODUCED **10 手順** |
| 61 | msg-2259 | Einstein | **抽出手続きを全面 clear。** 墓標付録を保持、タグの却下なし。merge 承認ではないことを明記 |
| 62 | msg-2260 | Bohr | INTRODUCED **規律-14**、執行者を Heisenberg へ移転、INTRODUCED 優先順位規則（記録が便宜的再掲に優先し、差分は報告する）。**自己申告どおり D / AC / INV / 残余 の変更なし** |

**被覆表明: 62 行 / 62 msg-id。重複なし、欠落なし。PASS。**
**手続き 9（取得不能）: 発生なし。全 62 通を unelided で取得した。**

---

# 付録 B — 墓標（TOMBSTONED / SUPERSEDED）

## B-1 TOMBSTONED（後継なし。内容は生き残らない）

| 識別子 | 導入 | 抹消 | 理由 | 抹消が生んだもの |
|---|---|---|---|---|
| **query-back 根拠**（D-1 の旧足場「モデルは欠けているものを知る ∴ 問い返せる」） | msg-2229 §7 | msg-2231 §1-a | **PR-gate は tool 無しの単一ターン評価器 ∴ 問い返しは存在しない。** この pipeline に無い能力を根拠にしていた | D-1 の足場が「scope 宣言」に差し替わった。**この足場の差し替えが本設計全体を支えている** |
| **A/D-5 の relay `tags`** | msg-1872 §6 | msg-1874 §O-2 | 同一事実を markdown と tags の 2 系統で持つのは、本スレッドが直そうとしている分裂そのもの（dual management + YAGNI） | 両チャネルが同一レンダラ出力を持つ設計 |
| **D-2 の modification-only**（`new file mode` は除去しない） | msg-2229 §4 | msg-2231 §2-a | out of model の攻撃者への部分的 guard を、**in-model の確定デッドロック**（初回 lockfile 導入 PR が永久に通らない）と交換していた。しかも既存 lockfile への 200k 追記で抜けられる | add/modify/delete を区別しない D-2″ |
| **msg-2229 §6-7**（新規 lockfile は除去されない） | msg-2229 §6 | msg-2231 §6 | 上と同一原因。旧 D-2 の回帰テストが赤になることを確認してから消す | 「初回 lockfile 追加（200k, `new file mode`）→ 除去される → APPROVE 可能」 |
| **D-2′ / `_ELIDE_MIN_CHARS` = 20,000** | msg-2231 §4 | msg-2233 §0 | **INV-REMEDY を、それを導入した当のメッセージの中で破っていた。** narrowing の述語が予算を見ていない静的なファイル属性だった | **INV-BUDGET** と、予算条件による D-2″ |
| **msg-2239 §4(a)**（「verdict 不在は RC より危険 = fail-open」） | msg-2239 §4 | msg-2241 §1 | **事実誤り。** ledger は「APPROVED review が merged head にあるか」を問う述語であり、review 0 件なら偽になる。**偽は未定義ではない。** `ADR-2026-06-03-16`（CI 赤 → APPROVE ではありえない）からも二重に否定される | 「未実測と自分で印を付けた命題を論証の支柱に使わない」規律 |
| **AC-14″ / AC-18″ の operational 半分** | msg-2241 §9 | msg-2243 §6 | operational path が本 PR に存在しなくなった（Checks への書き込みを scope cut） | AC-14‴ / AC-18‴ |
| **AC-20改**（Checks 側の反復 → Tier-C prose） | msg-2241 §9 | msg-2243 §6 | Checks に書かない | AC-20改'（Reviews 側の payload notice に Tier-C を要求）。**ただしこの削除が unclassifiable 既定の唯一の根拠を消しており、msg-2244 の deadlock を生んだ → 規律-7** |
| **AC-26改(c)**（runtime での route provenance 照合） | msg-2249 §10 | msg-2251 §7 | **OverScope**（dynamic routing は存在しない）**かつ非既定方向が未 pin**。fail-safe な既定へ黙って倒れる ∴ バグが全テストを通過する | **規律-10** と、build 時 assert の **AC-31** |
| **残余-6**（route 変更時に (B) の測定が陳腐化） | msg-2251 §5 | msg-2252（CONSOLIDATED） | msg-2251 §5 が「**AC-31 を採らないなら**」を条件にしており、msg-2252 が AC-31 を採択した ∴ 条件不成立。**記録は「残余-6 は消える」と明示していない** | — （**fidelity review 事項。§付録 F R-2**） |

## B-2 SUPERSEDED（後継あり。内容は後継に生き残る）

| 連鎖 | 辺 | 出所と分類 |
|---|---|---|
| **D-8 → D-8′** | msg-2237 §2 → msg-2239 §3 | 「訂正は『catch 範囲を狭める』ではなく『catch した後の出力を原因に応じて分岐させる』」。**CONSOLIDATED**（「supersedes」の語は無い） |
| **D-8′ → D-8″** | msg-2239 §3 → msg-2241 §4 | §4 表題「**訂正** — D-8″（Reviews から Checks へ移す）」。**CONSOLIDATED** |
| **D-8″ → D-8‴** | msg-2241 §4 → msg-2243 §4 | 「**D-8‴（D-8″ を置換）**」。**QUOTED** |
| **D-8‴ → D-8⁗** | msg-2243 §4 → msg-2245 §5 | 「**D-8⁗（D-8‴ の failure-handling 部を置換）**」。**QUOTED** |
| **D-8⁗ → D-8⁵** | msg-2245 §5 → msg-2247 §8 | msg-2247 §2 が msg-2245 §5 の要点を撤回し §8 で「**D-8⁵（確定形。順序付き 4 規則）**」。msg-2249 §6 が「D-8⁵ 本体は無変更」と確認。**CONSOLIDATED**（prime 表記 + 撤回文からの導出） |
| **D-2 → D-2″** | msg-2229 §4 → msg-2233 §2 | 「**D-2″（D-2 二次改訂 — `_ELIDE_MIN_CHARS` を削除し、予算条件に置換）**」。**QUOTED** |
| **AC-13 → AC-13′** | msg-2235 §7 → msg-2237 §7 | 「**AC-13 を破棄し、以下に置き換える。** — **AC-13′**」。**QUOTED**。**削除ではない** |
| **AC-13′ → AC-13″** | msg-2237 §7 → msg-2245 §10 | 「**AC-13″**（AC-13′ を昇格）」。**QUOTED** |
| **AC-14 → AC-14改 → AC-14″ → AC-14‴** | msg-2237 §7 → msg-2239 §9 → msg-2241 §9 → msg-2243 §6 | 改 / prime 表記。**CONSOLIDATED** |
| **AC-16 → AC-16改 → AC-16⁗** | msg-2237 §7 → msg-2243 §6 → msg-2245 §10 | 改 / prime 表記。**中間の 16″ / 16‴ は記録に存在しない。CONSOLIDATED** |
| **AC-18 → AC-18″ → AC-18‴ → AC-18⁗** | msg-2239 §9 → msg-2241 §9 → msg-2243 §6 → msg-2245 §10 | prime 表記。**CONSOLIDATED** |
| **AC-19 → AC-19改 → AC-19⁗** | msg-2239 §9 → msg-2243 §6 → msg-2245 §10 | 「**AC-19⁗**（AC-19改 を置換）」。**QUOTED**（後半の辺）。AC-19 → AC-19改 は **CONSOLIDATED** |
| **AC-20改' → AC-20改''** | msg-2243 §6 → msg-2245 §10 | 「AC-20改''（改）… 旧版は payload 側のみ」。**CONSOLIDATED** |
| **AC-21 → AC-21改 → AC-21⁗** | msg-2241 §9 → msg-2243 §6 → msg-2245 §10 | prime 表記。**CONSOLIDATED** |
| **AC-25 → AC-25改 → AC-25改'** | msg-2245 §10 → msg-2247 §12 → msg-2249 §10 | 改 表記。**CONSOLIDATED** |
| **AC-26 → AC-26改** | msg-2245 §10 → msg-2247 §12 | 改 表記。**CONSOLIDATED** |
| **AC-27 → AC-27改** | msg-2245 §10 → msg-2247 §12 | 改 表記。**CONSOLIDATED** |
| **AC-8 → AC-8改** | msg-2233 §6 → msg-2235 §7 | 「AC-8 改: 冪等性の根拠を…書き直す」。**QUOTED** |
| **INV-ESCAPE → INV-ESCAPE′** | msg-2245 §5 → msg-2247 §3 | 「**INV-ESCAPE を系列上で言い直す（INV-ESCAPE′）**」。**QUOTED** |
| **直交性軸 → (A)/(B)/(C) 証明所在軸** | msg-2247 §4 → msg-2249 §4 | 「分類軸を『直交性』から『証明の所在』に変える」。**QUOTED** |
| **D-1 の根拠**（query-back → scope 宣言） | msg-2229 §7 → msg-2231 §1-a | 「D-1 の根拠を以下に差し替える（結論は変えないが、足場を替える）」。**QUOTED** |
| **要求由来 manifest → 記録由来 manifest** | msg-2254 §6 → msg-2256 §4 | 「enumeration は record-derived になる」。**QUOTED** |
| **union → 時系列 fold** | msg-2256 §4 → msg-2258 §0 | 「`union` は order-insensitive … 集約は時系列 fold である」。**QUOTED** |
| **issue #2 草案 → 差し替え版** | msg-1936 §3 → msg-1938 §4 | 「msg-1936 §3 のブロックを破棄し、以下で置き換える」。**QUOTED** |
| **不変条件 v1 → v2 → v3（marker ベース）** | msg-1872 §9 → msg-1874 → msg-1876 | 「前案のテスト 1〜8 はこれに吸収」「前案の 2 / 3 は削除」。**QUOTED** |
| **msg-2229 §6-1（24 ケース matrix）→ AC-17** | msg-2229 §6 → msg-2237 §7 | 番号を持たない条件が新番号で再導入された。**CONSOLIDATED** |

**系譜表明（手続き 7）: 全 SUPERSEDED 辺が両端の msg-id を引用している。表記のみに依存する辺は CONSOLIDATED と印し、fidelity review に回した。PASS。**

---

# 付録 C — KAT の実行結果（手続き 8）

## C-1 陽性集合（msg-2256 §5 + msg-2258 §3）

| 項目 | live? | 台帳が独立に surface した行 |
|---|---|---|
| D-1 | ✅ | #31 msg-2229（INTRODUCED）+ #33 msg-2231（根拠 SUPERSEDED） |
| D-2″ | ✅ | #35 msg-2233（SUPERSEDED から） |
| D-3 | ✅ | #31 msg-2229 |
| D-3′ | ✅ | #37 msg-2235 |
| D-4 | ✅ | #31 msg-2229 + #33 msg-2231（MODIFIED） |
| D-5 | ✅ | #31 msg-2229 |
| D-6 | ✅ | #31 msg-2229 |
| D-7 | ✅ | #37 msg-2235 |
| 脅威モデルの受容（msg-2231 §5） | ✅ | #33 msg-2231 |
| **AC-13″**（over-tombstoning の anti-KAT） | ✅ | #37 → #39 → #47（SUPERSEDED 連鎖。**TOMBSTONED ではない**） |

**PASS。** 10 項目すべてが台帳の walk から surface し、手で足していない。

**AC-13″ について特記**: msg-2257 は「`AC-13` と `AC-26改(c)` は導入後に意図的に削除された」と書いたが、**AC-13 は削除ではなく AC-13″ に SUPERSEDED されており、AC-13″ は live である**（msg-2249 §10 / msg-2251 §7 の「不変: … AC-0 / 3〜13″ / …」）。AC-13″ は at-risk 帯を定義する測定であり、**D-8⁵ の規則 2 / 3 が分岐する述語そのもの**である。over-tombstone する fold は帯を消し、規則 2 / 3 が述語を失う。本 fold はそれを起こしていない。

## C-2 陰性集合（msg-2258 §5）

| 項目 | live index に無い？ | 処分 |
|---|---|---|
| D-8 | ✅ | SUPERSEDED → D-8′ |
| D-8′ | ✅ | SUPERSEDED → D-8″ |
| D-8″ | ✅ | SUPERSEDED → D-8‴ |
| D-8‴ | ✅ | SUPERSEDED → D-8⁗ |
| D-8⁗ | ✅ | SUPERSEDED → D-8⁵ |
| query-back 根拠 | ✅ | TOMBSTONED（msg-2231 §1-a） |
| AC-26改(c) | ✅ | TOMBSTONED（msg-2251 §7 逐語「AC-26改(c) — 削除。」） |

**PASS。** D-8 系列は 5 世代とも live index に現れない。生き残っているのは D-8⁵ のみ。

**両方向 PASS ∴ 手続き 8 の停止条件（「KAT failure, either direction → 抽出は壊れている。報告して停止。手で直さない」）は発火せず、artefact を書いた。**

---

# 付録 D — 実装対象一覧（本 PR で触るもの / 触らないもの）

## D-1 触る

| 対象 | 内容 |
|---|---|
| `src/spirrow_mindwire/naysayer/pr_review.py` | `DiffView` 構築の scope 判定（D-2″ / D-7）、stub ブロック（D-3 / D-3′）、`D-elided` marker（D-4）、post-elision 計測 + raw 併記（D-5）、§3-ESCAPE-HATCH、D-8⁵ の 4 規則 + (A)/(B)/(C) 分類 + `E-not-evaluated` marker |
| `tests/test_pr_review_driver.py`（および関連テスト） | AC-4〜AC-12 / AC-14‴ / AC-16⁗ / AC-18⁗ / AC-19⁗ / AC-20改'' / AC-21⁗ / AC-24 / AC-25改' / AC-26改(a) / AC-27改 / AC-28 / AC-29 / AC-30 / AC-31 の回帰。**`test_decide_verdict_matrix_axes_and_oracle` は無編集**（AC-17） |
| PR 本文 | AC-0 / AC-3 / AC-13″ / AC-15 / AC-22 / AC-26改(b) の測定結果を記録する |

## D-2 触らない（明示）

- **`_decide_verdict` の gate 判定ロジック** — ゼロ変更。
- **`test_decide_verdict_matrix_axes_and_oracle`** — 無編集で通す（AC-17）。**変更が必要になったら、その時点で D-8⁵ の配置が間違っている。**
- **`_MAX_DIFF_CHARS = 150_000` の値**。
- **`_DIFF_WARN_RATIO = 0.8`**。
- **marker `A-headroom` / `B-diff` / `B-len` / `C-suppressed` の文面**、prepend 順、verdict 行の所有、sentinel。
- **force-RC の安全弁**（scope 内の欠落 → 無条件 RC）。
- **Checks API**。

---

# 付録 E — UNRECOVERABLE（loop に返す。執行者は埋めていない）

msg-2255 §7.2 で settled: 「記録が差分連鎖を決着させられないギャップは live な設計問題として loop に返る。**執行者の判断にはならない。**」

| # | 内容 |
|---|---|
| **U-1** | **`AC-1` と `AC-2`（`AC-2改`）の識別子が記録から確定できない。** msg-2231 §6 は「AC-2 改」「AC-3」と呼び、そこから「msg-2229 §6 の第 N 条件 = AC-N」という読みが導かれる。しかし **msg-2233 §6 が AC-4〜AC-8 をまったく別内容で新規採番している** ∴ この対応は N≥4 で破れている。かつ **AC-1 / AC-2 は msg-2235 以降のどの「不変」リストにも現れない**。内容（24 ケース matrix pass → AC-17 に再導入、verimend#3 のリプレイ）は生きているが、**番号は確定できない。** 本文書は AC-1 / AC-2 を live index に置いていない。 |
| **U-2** | **msg-2229 §6 の第 4 / 6 / 8 / 9 条件が番号を失っている。** 内容は生きている — §6-4（合成ケース: 200k `.py` + 10k lockfile → 除去後もなお超過 → truncated → RC。**elision が real code を救ってはならない**）、§6-6（injection: diff 本文が stub 書式を騙る行を含んでも notice の除去リストに偽の path が現れない）、§6-8（`test_gate_notice_never_contradicts_across_coexisting_notes` に `D-elided` を追加し、A / B-diff との共存で split 指示が生き残ることを固定）、§6-9（`test_gate_notice_prose_matches_prepended_layout` に `D-elided` の方向参照を追加）。**いずれも msg-2231 §6 / msg-2233 §5 / msg-2235 §7 で「維持」と明記されているが、番号を持たない。** 本文書は内容を §2 D-4 / §4 の散文として保持し、**番号の付与は loop に返す。** |
| **U-3** | **規律-1〜5 が本スレッドの corpus に存在しない。** 番号は規律-6 から始まる。他スレッド由来である蓋然性が高いが、**本 fold は corpus 外を参照しない** ∴ 確定できない。 |
| **U-4** | **`D-1`〜`D-6` の名前空間が 2 つある。** msg-1872 系（#186 で出荷済み）と msg-2229 系（本設計）が同じ識別子を別内容で使う。**両者の分離は、決定 A（msg-1955）による一度の決着と msg-2030 の再開という構造からしか読めない。記録は名前空間を明示していない。** 本文書は §2 を msg-2229 系、§7 を Phase A として分離し、台帳では `A/D-n` の接頭辞を付したが、**これは再導出である。** |
| **U-5** | **4 タグ集合に「実装済み・出荷済み」を表すタグが無い。** Phase A の D-1〜D-6 は #186 で出荷済みだが、SUPERSEDED でも TOMBSTONED でもない ∴ 手続き 5 の定義では live に残る。live であること自体は正しい（Phase B が土台として保存すると宣言している）が、**「これから実装する対象」と「既に main に存在する土台」が同じ live 集合に混ざる。** 本文書は §2（実装対象）と §7（土台）に分けて表示したが、**タグ集合そのものはこの区別を持たない。** |

---

# 付録 F — 報告する差分（msg-2260 §4 の優先順位規則）

> 記録と便宜的再掲が食い違う場合、**記録が勝ち、差分は報告される** — 黙って調整されない。

| # | 内容 |
|---|---|
| **R-1** | **メッセージ件数。** msg-2260 §4-1 は「件数は抽出時に読む。仮定しない」と定めている。**実測 = 62。** msg-2256 §4 は「57」、msg-2253 §1 は「54」と書いている。手続きどおり 62 を採り、3 つの数の併存を報告する（各時点で正しく、いずれも仮定ではない）。 |
| **R-2** | **残余-6 の処分。** msg-2251 §5 は「AC-31 を採らないなら」を条件に立て、msg-2252 は AC-31 を採択した ∴ 条件不成立。しかし **記録は「残余-6 は消える」と一度も書いていない。** 本文書は TOMBSTONED を **CONSOLIDATED** として付した。**fidelity review 事項。** |
| **R-3** | **ファイル名と spec-manifest 機構の衝突。** 記録（msg-2254 §6 / msg-2256 §8 / msg-2260 §6）は `spec/design/pr-gate-elision-and-failure-observability.md` を指名する。一方、本 repo の `spec/design/verify.py` の **V-2 は、YAML front-matter を持つ `spec/design/*.md` に `<thread>.md` というファイル名を要求する**（本スレッドなら `T-gate-silently-suppresses-approve-on-truncated-diff.md`）。∴ 記録の指名した名前で front-matter を付けると V-2 が ERROR になり、error-0 の `main` を壊す。**本文書は記録の指名した path に置き、front-matter を持たない**（front-matter 無しのファイルは V-2 に到達せず silently skip される。`spec/design/T-denial-detail-and-overdeny.md` と同じ扱い）。∴ **本文書は spec-delivery manifest ではなく、`OBL-SPEC-PIN` / `OBL-SPEC-RECEIPT` / `OBL-SPEC-SCOPE-CLOSURE` の対象にならない。** manifest 化が要るなら改名が要り、**それは loop の判断であって執行者の判断ではない。** |
| **R-4** | **執行者の移転そのもの。** msg-2260 §3 が自ら「clearance は executor を名指ししていなかった ∴ 移転自体を fidelity review の対象項目として立てる」と書いている。本文書はその印を引き継ぐ。 |
| **R-5** | **error type 6〜15 の扱い。** msg-2256 §4 の item クラス列挙（D / AC / INV / 規律 / 残余 / 補償 / 脅威モデルの受容 / 測定義務）に「error type」は含まれない ∴ 規範項目として live index に入れず、**§付録 G に provenance として置いた。** これは執行者の分類判断であり、**fidelity review 事項。** |
| **R-6** | **msg-2260 §4 の 10 手順は msg-2258 §6 と実質同一だが、順序と語が一部異なる**（§4-7 は「edges resting on prime/改 notation alone」と明記、§6-7 は「notation alone」）。**実質差なし。** 本文書は msg-2258 §6 を正典として実行した。 |

---

# 付録 G — error type 登録簿（非規範。規律の provenance）

規律-6〜14 はいずれも、proposer が自分の誤りを型として登録したことから生まれている。**規律だけを残して型を捨てると、規律がその理由から切り離される**（規律-7 が禁じる形）。∴ 非規範の provenance として記録する。

| type | 内容 | 出所 | 生んだ規律 |
|---|---|---|---|
| 6 | 自分で「未実測」と印を付けた命題を、他の判断の支柱に使った | msg-2241 §1 | 「未実測の印を支柱にしない」 |
| 7 | 相手の区別を、自分の 1 本の軸に潰して読んだ | msg-2241 §10 | 「まず自分が相手の区別を潰していないか疑う」 |
| 8 | 書き込み先チャネルに主体が到達できるかを確かめずに、そのチャネルへの書き込みを設計に組み込んだ | msg-2243 §9 | **規律-6** |
| 9 | スコープを切るとき、切った要素に依存していた他の決定を洗い出さなかった | msg-2245 §12 | **規律-7** |
| 10 | gate の出力が、gate 自身が分岐に使う量を変えることを見落とした | msg-2247 §11 | **規律-8** |
| 11 | 同一の保護を、同一の段落で、選択肢ごとに逆向きに数えた（両方とも自分が先に選んでいた側に有利な向き） | msg-2249 §9 | **規律-9** |
| 12 | 機構のコストを「行数」で見積もり、「その機構の故障が観測されるか」で見積もらなかった | msg-2251 §2 | **規律-10** |
| 13 | 設計を artefact 無しで閉じ、**自分と同じ context を共有する唯一のレビュアに認証させ**、それを共有しない当事者に手渡した。**レビュアの同意は、レビュアの context が著者と同じであるとき、可読性の証拠にならない** | msg-2254 §2 | **規律-11** |
| 14 | 成果物の scope を、記録ではなく**要求**から取った。要求は「要求者が自分に欠けていると知っているもの」を語る。**知らずに欠けているものについては沈黙し、その沈黙は完全性と区別できない** | msg-2256 §3 | **規律-12** |
| 15 | 抽出手続きを、それが走る当のデータに対して実行せずに規定した（**走査を正しく述べた直後に、走査が装飾になる集約子を指定した**） | msg-2258 §1 | **規律-13** |

**この登録簿は本スレッドの中核的な発見でもある**: msg-1871 の病理（部分的な view が、見ていないものを黙って省いたまま、well-formed で自信のある出力を生む）は 3 層で再生産された。

1. **msg-1871** — 切り詰められた diff が、見えなかった部分について黙ったまま REQUEST_CHANGES を出した。
2. **msg-2253** — 54 通中 9 通しか読めない view が、見えなかった項目を黙って落とした well-formed な**チェックリスト**（§6）を生んだ。**「自分の view が truncated である」と宣言することは、その view から作った成果物を修復しない。**
3. **msg-2254** — proposer がそのチェックリストを完全なものとして消費した。

**本文書はその 3 層目を閉じるために存在する。** 被覆台帳（付録 A）は、省略が「見えない欠落項目」ではなく「**見える欠落行**」としてしか起こり得ないようにするための機構である。
