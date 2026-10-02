# per-project deploy rule — `.mindwire-deploy.yaml`

Status: proposed (PR-0)
Thread: `T-per-project-deploy-rule` (spirrow-magickit chatroom)
Scope: spirrow-mindwire / Spirrow-VoxelWorld

---

## 1. 前文 — 本 spec の位置づけ

本 spec は **PR-A（VoxelWorld の宣言）/ PR-B（mindwire の宣言）/ PR-C（強制コードとテスト）の単一 SOT** である。3 つの PR はいずれも本ファイルを読んで作られ、本ファイルと食い違う実装はレビューで落とされる。

スレッド上の議論は**判断の記録**であって仕様ではない。仕様は本ファイルにある。逆に、本ファイルに書かれていない要件を「スレッドにあった」を根拠に実装してはならない。欠けているなら本ファイルを直す PR を先に出す（改訂規律は §11）。

### 1.1 統合してはならない 2 項

**INV-2（境界の定義）と INV-6（その執行手段）を 1 項に統合しないこと。**

- INV-2 は「セキュリティ境界がどこにあるか」という**命題**である
- INV-6 は「その境界を、読み取り ref を固定することで**どう執行するか**」という手段である

手段は将来置き換わりうる（例: API 経路が使えなくなり §5.2 の退避経路に切り替わる）。そのとき統合された 1 項を書き換えると、**境界の定義ごと書き換わる**。定義と執行手段を別項に保つことは、執行手段を差し替えても境界が動かないことを構造的に保証するためのものである。

### 1.2 非目標

- **本ファイルは事故に対する安全宣言であって、敵対的 implementer に対するセキュリティ境界ではない。** 対象リポジトリの宣言は自分で狭めた範囲を自分で天井まで戻せる。境界は MindWire 側の天井ただ 1 つである。ここを曖昧にすると「repo 側に書いたから安全」という誤った安心が生まれる
- **`git.merge` 等への一般化は v1 では行わない。** operation→target の汎用 narrowing map は、対象リポジトリ側ファイルを第二の allowlist に育ててしまう（implementer が書けるファイルの影響面が増える）。v2 候補
- **`docs/branching.md` の散文を置き換えない。** 機械可読宣言は散文の**対**であって代替ではない
- **`.mindwire/` ディレクトリ名前空間への移行は行わない。** 3 本目の `.mindwire-*` が必要になった時点で一括移行する（§3.1）

---

## 2. 適用範囲・用語

### 2.1 適用範囲

MindWire のループが `github.pr.open` を実行しうる全リポジトリ。本 spec 執筆時点で実在するのは 2 つ:

| repo | デプロイ形態 | 帰結 |
|---|---|---|
| Spirrow-VoxelWorld | ゲームプラグイン。CI がビルドし、リリースという事象がある | リリーストレイン（`feature → develop → release → main`）が合う |
| spirrow-mindwire | 常時稼働のデーモン。`main` チェックアウトから継続デプロイ | リリーストレインは害になる（修正が稼働中のループに届くまでリリースを待つことになる） |

∴ **どのブランチへ PR を開いてよいかは MindWire が知っているべきことではなく、対象プロジェクトが宣言すべきことである。**

### 2.2 用語

| 用語 | 定義 |
|---|---|
| **天井 (ceiling)** | MindWire 側 `implementer_allowlist.yaml` が許す操作・対象の集合。リポジトリ非依存 |
| **床 (floor)** | 対象リポジトリの `.mindwire-deploy.yaml` が宣言する、プロジェクト個別の許容集合 |
| **実効ポリシー** | 天井と床から構築される、そのタスクで実際に使われる認可オブジェクト |
| **宣言 (declaration)** | 対象リポジトリのルートに置かれる `.mindwire-deploy.yaml` の内容 |
| **default branch** | 対象リポジトリの GitHub 上の default branch。本 spec 執筆時点で両 repo とも `main`（§8.2 に実測） |
| **subset-assert** | 床の各要素が天井のいずれかにマッチすることをロード時に検証し、違反を hard error とすること |

---

## 3. 宣言ファイル

### 3.1 名前と配置

**ファイル名: `.mindwire-deploy.yaml`。対象リポジトリのルート直下に、厳密名で置く。**

- `.mindwire-` prefix は `.mindwire-gate` で確立済みの名前空間を踏襲する
- **`.yaml` 拡張子は可読性のためではない。** `.mindwire-gate` が「実行されるもの」であるのに対し、本ファイルは「**絶対に実行されないデータ**」であることを名前で分離するためである。権限を決める宣言が実行可能形式であってはならない（implementer が書き込めるファイルの実行 = 任意コード実行）
- パースは safe-load 相当・単一ドキュメント・トップレベル mapping のみ

**命名の差し戻し条件（実装時に判定する）**: 入口 read-back で `.mindwire-gate` が「**パス固定でルートを見に行く**」規約であることを確認できたなら本節のまま。もし mindwire 側 config からの参照で発見される（= ルート flat 名は偶然）と判明したら、**実装者は独断で命名を変えず、本 spec の改訂 PR を先に出す**（§11）。U-2 が未測であることに対応する（§9）。

### 3.2 schema v1

```yaml
# Spirrow-VoxelWorld/.mindwire-deploy.yaml
schema_version: 1
pr_base_allow:
  - develop
```

```yaml
# spirrow-mindwire/.mindwire-deploy.yaml
schema_version: 1
pr_base_allow:
  - main
```

| キー | 型 | 必須 | 意味 |
|---|---|---|---|
| `schema_version` | 整数 | **必須** | v1 は `1` 固定。未知/将来値は fail-closed |
| `pr_base_allow` | 文字列の配列 | **必須** | PR の base として許される branch の **literal** 集合。1 要素以上 |

**トップレベルキーは上記 2 つのみ。closed schema である**（未知キーは error、§3.3-6）。

> `policy_doc` は本 schema に**含まれない**。初期案には存在したが人間 Tier-C 判断により削除済であり、非規範ヒントは YAML 内コメントで足りる。

### 3.3 検証規則（すべて違反は hard error。warning は作らない）

1. ルート直下に厳密名 `.mindwire-deploy.yaml` で存在すること
2. 単一ドキュメント / トップレベル mapping / safe-load 可能であること
3. `schema_version` 必須・値は `1`
4. `pr_base_allow` 必須・1 要素以上・非空 literal 文字列・重複禁止・**wildcard 禁止**
5. `pr_base_allow` の各要素が天井 glob のいずれかにマッチすること（**subset-assert**、§4.2）
6. **未知のトップレベルキーは error**（typo が「宣言したつもり」を作らないため）

**規則 4 の wildcard 禁止の理由**: 天井側は `target_glob` 型である。プロジェクト側に wildcard を許すと `*` 一発で「天井いっぱい」を書けてしまい、宣言の意味が消える。

**規則 6 が closed schema である理由**: 開いていると、キー名を打ち間違えた宣言が「宣言したつもりで無効」という最悪の状態で通過する。これは本件の起点（設定が読まれていないことが隠れる）と同型の失敗である。

**hard error の効果**: 規則 1〜6 のいずれかに違反した宣言は、**宣言全体を拒否する**（天井内・妥当な部分だけを部分採用しない）。拒否の効果は §4.3 の構築表の 3 行目であり、**タスク全体の停止ではない**。warning への格下げはしない。違反した規則と §7.1 の reason の対応は §7.1 の表による。

### 3.4 拡張

拡張は `schema_version` の bump ＋ **加算キー**で行う。既存キーの意味を変えてはならない。改訂規律は §11。

---

## 4. 意味論 — 天井・床・積集合

### 4.1 実効権限は積集合であって置換ではない

- **MindWire 側の allowlist が天井**
- **対象リポジトリ側の宣言が、プロジェクトごとの床**
- **実効権限は積集合**

**置換の意味論にしてはならない。** このファイルは対象リポジトリにあり、implementer はそこに書き込める（天井の `fs.write` は `<repo>/**`）。∴ 置換にすると **implementer が自分の権限を自分で広げられる**。積集合にすることで、自己昇格が構造的に不可能になる。

**prompt や規約で「広げるな」と言うのでは足りない。** test で固定できない前提の上に安全性を置かないこと。§4.3 の property test がこの要求に対応する。

### 4.2 積の取り方は strict subset-assert（静かな ∩ ではない）

宣言に天井外の要素が 1 つでもあれば、**ロード時に宣言全体を拒否する**（hard error。天井内の要素だけを部分採用しない）。拒否の効果は §4.3 の構築表のとおり、宣言が支配する操作が実効ポリシーに入らないことである。reason は §7.1 の `out-of-ceiling`。

素朴な積集合との差は、**invalid 入力が黙って縮むか、鳴って落ちるか**だけである:

- 素朴な ∩: 天井 `[develop, main]` ∩ 宣言 `[release]` = `∅` → 静かに何も開けなくなる
- subset-assert: 天井外の要素があれば hard error

妥当な入力に対して両者は観測的に等価（valid なら実効 = 宣言集合）であり、要件（実効 ⊆ 天井 / 自己昇格不能）はどちらでも同一に成り立つ。**採る理由は「宣言したのに効いていない」が不可視にならないこと**である。静かな ∩ は「誰も読まない場所に UNAVAILABLE が出ていた」型の穴を作る。

**代償を明記する**: 天井を狭めた瞬間、消えた branch を名指ししている全プロジェクトが一斉に loud fail する。これは天井を狭めた副作用が静かに効いた事象の裏返しであり、**loud fail 側を正とする**（Tier-C 承認済み）。

### 4.3 積を取る場所 — 評価経路の中ではなく、評価器を作る所

**構築時（手前）で積を取る。評価器は無改造とする。**

- 天井 YAML をロードして implementer の操作認可に使うオブジェクトを作る、その**唯一の構築点**で `.mindwire-deploy.yaml` を読み、検証し、**すでに狭められた実効ポリシー 1 個**を作って渡す
- 「許されるか」に答える主体が 1 個のままなので、**2 経路が食い違う / 片方が project 側チェックを飛ばす**という失敗が構造的に起きない
- 積は純関数 ∴ `∀ 天井, 宣言: 実効 ⊆ 天井` を property test で固定できる

**構築点の出力（規範）**。「宣言が支配する操作」とは v1 では `github.pr.open` のみである（R-3）。

| 入力 | 実効ポリシー |
|---|---|
| 天井が読めない／不正 | **生成されない。** タスクは何も走らない（宣言の有無と無関係） |
| 天井は正常・宣言が妥当（§3.3 の全規則を満たす） | 天井と同じ。ただし宣言が支配する操作の対象は、宣言の `pr_base_allow`（literal 集合）に**狭められる**。⊆ 天井は subset-assert で保証済み |
| 天井は正常・宣言が §7.1 の `absent` / `indeterminate` / `parse` / `schema` / `out-of-ceiling` のいずれか | 天井から生成する。ただし、**宣言が支配する操作を許す rule を 1 本も含めない**。それ以外の操作は天井のまま |

**fail-closed は構築の結果として出る。構築の失敗として出るのではない。**

- 3 行目の場合、実効ポリシーには `github.pr.open` を許す rule が存在しない。∴ 拒否は評価器の既定動作（どの rule にもマッチしない操作は拒否する）として出る。評価器に「宣言が無ければ拒否する」という分岐を足さない。∴ 評価器が「拒否を忘れる」余地は無い
- 拒否理由（§7.1 の reason と、§7.2 のプレースホルダの値）は実効ポリシーに**認可に使わないデータ**として添付する。用途は §7.2 の文言の描画だけである。認可判断はこれを参照しない（INV-7）
- 宣言の不在・不正でタスク全体を止めない理由は §7.3 にある

**実装者の入口 read-back 義務**（いずれかが偽なら、独断で設計を変えずに halt し、本 spec の改訂を求めてスレッドに差し戻す）:

1. **構築点が本当に 1 個か。** 2 個以上あれば、まず 1 個に寄せてから本機能を入れる（増やした側に入れ忘れる形の再発を防ぐ）。`test_single_policy_construction_site` で固定する
2. **評価器が allowlist 方式であるか**、すなわちマッチする rule の無い操作を拒否するか。上の「拒否は評価器の既定動作として出る」はこの性質に全面的に依存する。§10.4 のテストで固定する

### 4.4 実効ポリシーは per-task 構築物であって process-global ではない

sweep のマルチプロジェクト化により、1 プロセスが複数リポジトリを扱う。**天井を起動時に 1 回読んで使い回す形は、本件のバグ（リポジトリ非依存ファイルにリポジトリ依存の事実を書いた）を実行時に再現する。**

∴ 実効ポリシーはタスクごとに構築する。テストで固定する（§10.4 の必要テスト）。

---

## 5. 読み取り

### 5.1 既定経路（API）— PR-C の唯一の実装対象

判定述語は、人間が手で実行した probe と**同一**にする:

```
GET /repos/{owner}/{repo}/contents/.mindwire-deploy.yaml
```

**ref は無指定（= default branch）ではなく、静的定数で明示する**（INV-6、§6.1）。

| 応答 | 判定 | reason |
|---|---|---|
| `200` | 内容を parse して policy 構築 | — |
| `404` | **deny** | definitive absence |
| parse 失敗 / schema 不適合 / 空 | **deny** | present but invalid（404 と**別の**文字列） |
| transport error / 5xx | **deny** | indeterminate（上記 2 つと**別の**文字列） |

API flaky でループが止まる運用コストは認める。**fail-closed 要件に例外は作らない。** reason を 3 分岐させるのは、運用者が原因を即座に判別できるようにするためである（§7）。

### 5.2 退避経路（VCS）— 規範として定義するが、PR-C の実装スコープには含めない

> **適用条件（重要）**: VCS 退避経路は、**API 経路が利用不能と判定された場合にのみ**発動する代替経路である。本 spec はその挙動を規範として定義するが、**PR-C の実装スコープには含めない**（API 経路のみを実装する）。将来この経路を実装する際は、INV-10 を含む本節の規範をそのまま満たさなければならない。

**PR-C における扱い — Out of Scope（実装禁止）**:

- 実装者は VCS 経路を **PR-C に含めてはならない**。API 単一経路のみを実装する
- 理由: API 到達性は実測で真（httpx ＋ loop token で HTTP 200）である ∴ 両経路を同時に実装することは、同じ事実を取得する 2 機構の二重保守であり、かつローカル Git 操作は「PR head を静かに読んでしまう」事故の温床を自分で作ることになる
- **非規範の注記に降格させないのは、INV-10 が安全性の命題だからである。** 参考情報に落とすと、将来の再導出時に「守るべき契約だったのか」が判別できなくなる

**規範（将来この経路を実装する場合に満たすべき要件）**:

- **必須**: `git show <ref>:.mindwire-deploy.yaml` 等、**VCS 経由で対象 ref から抽出**すること。working tree / index / `HEAD` を読んではならない。ローカルワークスペースは PR head に checkout されている ∴ `fs.readFile` は境界を**静かに**破壊する（動作しているように見えるのが最悪である）
- **必須**: 読み取り前に `git fetch origin <ref>` を実行すること。stale なリモート追跡 ref は ① 直近で追加された宣言を見落として deny（fail-closed 方向 ∴ 安全側）だが ② **撤回された権限を stale な宣言のまま許可し続ける**（危険側）
- **必須**: **fetch 失敗は indeterminate として deny**（§5.1 の 3 分岐と同じ扱い、reason 文字列は分ける）
- 読み取り対象 ref は §6.1 の `TARGET_REF` と同一の静的定数を用いる。ローカルのブランチ状態から導出してはならない

---

## 6. 不変条件

### 6.1 INV-1 〜 INV-6（承認済み・逐語）

> 出典: 本スレッドの Tier-C 承認済み dispatch（human が INV-6.1〜6.5 を確定仕様として承認 → 全文化 → naysayer 指摘による INV-6.3 補足の追加と naysayer の明示的支持 → 最終 dispatch）。以下はその節の**逐語転記**であり、改変していない。逐語の変更は §11 の手続きによる（承認済み artifact 側の改訂が先行する）。

**INV-1（宣言の所在）**
deploy 可否・deploy 先の宣言は**対象プロジェクトの repo 内**に置く。mindwire 側に per-project の設定・対応表を持たない。∴ 対象が増えても mindwire 側の変更を要さない。

**INV-2（セキュリティ境界の定義）**
ループは**自分が書き込める場所から自分の権限を読まない**。宣言が「ループが push できる ref」に置かれた状態で読まれるなら、ループは宣言を書き換えて自己に deploy 権限を付与できる ∴ 境界が存在しない。境界は「**読み取り先がループの書き込み可能領域と交わらないこと**」で定義される。
※ INV-2 は**境界の定義**、INV-6 は**その執行手段**である。**両者を 1 項に統合しないこと**（執行手段を差し替えた際に境界の定義まで一緒に動くため）。

**INV-3（fail-closed の既定）**
宣言が存在しない・読めない・壊れている・schema に適合しない場合は **deploy しない**（拒否して報告）。「読めないので既定値で続行」は、宣言を壊せば既定に落ちるという自己付与経路そのもの ∴ 禁止。

**INV-4（closed schema）**
宣言の parse は closed schema。**未知キー・型不一致・必須キー欠落はすべて拒否**（fail-closed）。「未知キーを無視して続行」は宣言側から実行系の挙動を拡張する経路 ∴ 禁止。

**INV-5（宣言はデータであってコードではない）**
宣言は「どこへ deploy するか」の値のみを持ち、実行手段（コマンド文字列・スクリプトパス・シェル片）を持たない。実行手段は mindwire 側のコードに固定する。

**INV-6（読み取り ref の固定 = INV-2 の執行手段）**

- **INV-6.1**: 宣言の読み取りは**明示 ref 指定**で行う。ref を省略して API の既定解決（default branch）に委ねない。
- **INV-6.2**: 読み取り ref は per-project の対応表を持たず、**単一静的定数 `TARGET_REF = "main"`** とする。
  *注記: `main` 以外の default branch を持つ対象が実際に生じた時点で初めて構造化を検討する。* 実測（2026-08-11）:
  ```
  SpirrowGames/spirrow-mindwire:   default=main  literal-main=main
  SpirrowGames/Spirrow-VoxelWorld: default=main  literal-main=main
  ```
  ∴ 現時点で対応表が表現できる情報は定数と等価であり、表を先に作れば「まだ無い差異」のための機構を維持し続けることになる。
- **INV-6.3**: `TARGET_REF` は**実行時入力から決定されない**。ループの引数・環境変数・宣言ファイル自身・chatroom msg・PR 本文など、**ループが影響を及ぼしうるいかなる経路からも上書きできない**。
  - **INV-6.3 補足（適用範囲の明示）**: `TARGET_REF` に対する**テスト専用の override 経路を実装しない**。テストで差し替えてよいのは **client（読み取り手段）であって ref ではない**。テストコードからも `TARGET_REF` は定数のまま読まれること。利便性の名目で override を開けることは、INV-6.4 で塞いだフォールバックを別の入口から復活させることに等しい。
- **INV-6.4（本改訂の要点）**: **fail-closed**。`TARGET_REF` で読めなかった場合に **default branch へフォールバックする経路を実装しない**。読めなければ拒否して報告する。
  *根拠: フォールバック経路は INV-6.3 の抜け穴そのものである。「安全側に倒したつもりのフォールバック」が自己付与経路を復活させる。*
- **INV-6.5**: `TARGET_REF` の変更は mindwire への PR を経る ∴ **読み取り先の変更は必ず人間のレビューを通る**。定数であること自体が統制になっている。

### 6.2 INV-7 〜 INV-10（追番。出自を併記する）

| # | 出自 | 命題 |
|---|---|---|
| **INV-7** | 旧 INV-1 | **非規範フィールドが認可判断に影響しない**（テストで証明する） |
| **INV-8** | 旧 INV-3 | **PR-C マージ前後で、許可される (repo, action) の集合が不変**（活性化時点で挙動不変 = R-11 の同値性） |
| **INV-9** | 旧 INV-4 | **両 repo の実ファイル fixture を持ち、parse → 期待権限集合が得られる**（fixture と実ファイルの一致はレビュー時に照合する） |
| **INV-10** | 旧 INV-5 | **退避（VCS）経路は working tree / `HEAD` を読まず、`fetch` 成功なしに判定しない。** 適用は §5.2 の条件付き — API 経路が利用不能と判定された場合にのみ発動する経路に対する規範であり、**PR-C の実装対象ではない** |

### 6.3 crosswalk（旧ラベル → 確定ラベル）

**本表の目的は、番号の再割り当てによって命題が黙って死ぬのを防ぐことである。** 吸収も記録の対象とする。

| 旧ラベル（旧番号体系） | 確定ラベル | 処理 |
|---|---|---|
| 旧 INV-1（非規範フィールドが認可判断に影響しない） | **INV-7** | 追番して存続 |
| 旧 INV-2（宣言は default branch からのみ読む / PR head から読まない） | **新 INV-2 ＋ INV-6 に吸収** | **独立した命題としては残さない。** 新 INV-6.2 の静的 `main` 固定と、新 INV-6.3 / INV-6.4 の override・fallback 完全禁止により、PR head が読み取り対象となる経路は物理的に消滅している（naysayer 判定、人間承認） |
| 旧 INV-3（PR-C マージ前後で許可集合が不変） | **INV-8** | 追番して存続 |
| 旧 INV-4（両 repo の実ファイル fixture） | **INV-9** | 追番して存続 |
| 旧 INV-5（fallback は working tree/HEAD を読まず、fetch 成功なしに判定しない） | **INV-10** | 追番して存続。**新 INV-6.4 とは別の故障モードである** — 6.4 = API 読み失敗時に default branch へ落ちない／INV-10 = ローカル経路で PR head を静かに読まない・stale な remote ref で判定しない。**片方が他方を含まない** |

**INV 番号は今後、本表の確定ラベルのみを使う。** 旧ラベルは本表の左列以外に現れてはならない。

---

## 7. fail-closed のエラー要件

### 7.1 要件

- **宣言が無いときは fail-closed。** 既定値へのフォールバックにしてはならない。フォールバックは「設定が読まれていない」ことを隠す
- **エラーには「誰が・どこに・何を置けばよいか」を出す。** 行動可能な文言であること。宣言に起因する拒否はループ自身では解消できない（§7.3）。∴ 宣言に起因する拒否の文言は**人間の運用者**に作業を指示し、**ループ**には解消しない再試行をさせない
- **拒否理由は次の 6 種であり、すべて別文言・別 reason 文字列にする。** 「読めなかった」と「読めたが許していない」が同じ文言だと、まさに設定が読まれていないことが隠れる

| reason | 段階 | 原因 | §3.3 規則 | §5.1 分岐 | ループの再試行 |
|---|---|---|---|---|---|
| `absent` | 宣言のロード | `404` | 1 | definitive absence | **不可**。報告して halt |
| `indeterminate` | 宣言のロード | transport error / 5xx | — | indeterminate | **タスク内では不可**。報告して halt（次のタスクの構築時に読み直される） |
| `parse` | 宣言のロード | YAML として解釈不能・複数ドキュメント・トップレベルが mapping でない・空 | 2 | present but invalid | **不可**。報告して halt |
| `schema` | 宣言のロード | `schema_version` 不正・`pr_base_allow` 不正・未知キー | 3, 4, 6 | present but invalid | **不可**。報告して halt |
| `out-of-ceiling` | 宣言のロード | `pr_base_allow` に天井外の要素がある | 5 | present but invalid | **不可**。報告して halt |
| `base-not-allowed` | 操作の評価 | 宣言は妥当だが、要求された base が `pr_base_allow` に無い | — | — | **可**。`pr_base_allow` 内の base で開き直す（宣言は変更しない） |

- 上 5 種（ロード段階）の効果は §4.3 の構築表の 3 行目である。最後の 1 種は、妥当に構築された実効ポリシーを評価器が拒否したものである
- §5.1 の「present but invalid」は本表の `parse` / `schema` / `out-of-ceiling` に細分される。§5.1 の要件（404・invalid・indeterminate を別の文字列にする）は、本表によって満たされたうえで、さらに細かくなる
- **`indeterminate` でもタスク内の再試行を許さない理由**: 実効ポリシーはタスク開始時に 1 回だけ構築される（§4.4）。同じタスク内で `github.pr.open` を再試行しても、同じポリシーが同じ答えを返すだけである。自動リトライの機構は v1 では作らない

### 7.2 文言（文言も spec の一部である）

**以下はテンプレートである。** `{…}` 以外の文字列は規範であり、逐語で出す。`{…}` は実行時の値で埋める。リポジトリ名・branch 名・天井の範囲を**固定文字列で書いてはならない**（1 プロセスが複数 repo を扱う。§4.4）。

| プレースホルダ | 値の出所 |
|---|---|
| `{repo}` | そのタスクの対象リポジトリ（`owner/name`） |
| `{target_ref}` | 定数 `TARGET_REF`（INV-6.2）。文言のためだけに別のリテラルを持たない |
| `{ceiling}` | **ロード済みの天井**のうち、`github.pr.open` の `target_glob` を記載どおりカンマ区切りで並べたもの |
| `{declared}` | 宣言の `pr_base_allow` を記載順にカンマ区切りで並べたもの |
| `{offending}` | `pr_base_allow` のうち天井にマッチしない要素を、カンマ区切りで並べたもの |
| `{requested_base}` | 拒否された `github.pr.open` 要求の base |
| `{detail}` | parser または validator が出した最初の違反。1 行。`schema` の場合は違反した §3.3 の規則番号を含める |
| `{error}` | transport error の種別、または HTTP status |

1 行目の `reason=…` は §7.1 の reason 文字列であり、機械判別用の正本である。

**absent:**

```
BLOCKED: github.pr.open [{repo}] reason=absent
{repo} の {target_ref} にデプロイ規則の宣言（.mindwire-deploy.yaml）がありません。

この拒否はループ（implementer）自身では解消できません。人間の操作が必要です。

[人間の運用者へ]
MindWire は、各対象リポジトリが「どの branch 宛に PR を開いてよいか」を
自分で宣言することを要求します。{repo} の {target_ref} ブランチのルートに
以下を置いてください（{target_ref} への直接コミット、またはそのリポジトリ自身の
リリース手順で {target_ref} に到達させる）:

  .mindwire-deploy.yaml
  ---
  schema_version: 1
  pr_base_allow:
    - <branch>        # 現在 MindWire が許す範囲: {ceiling}

宣言は {target_ref} からのみ読まれます。feature branch・PR・作業ツリーに置いても
読まれません。
宣言できるのは上記範囲の部分集合のみです。範囲外の branch は宣言できません
（リポジトリ側のファイルは MindWire の天井を狭めることはできても、広げられません）。

[ループ（implementer）へ]
作業ツリーにこのファイルを作成して再試行しないでください。宣言は {target_ref} から
しか読まれないため、拒否は解消しません。このエラーをスレッドに報告して
halt してください。

既定値にフォールバックしない理由: thread T-per-project-deploy-rule
```

**indeterminate:**

```
BLOCKED: github.pr.open [{repo}] reason=indeterminate
{repo} の {target_ref} からデプロイ規則の宣言を読めませんでした（{error}）。
宣言が有るか無いかを判定できていません。これは「宣言が無い」とは別の状態です。

[人間の運用者へ]
GitHub API への到達性・token・rate limit を確認してください。宣言ファイル自体の
変更は不要な可能性があります。MindWire は読めない場合に既定値で続行しません。

[ループ（implementer）へ]
このタスク内で github.pr.open を再試行しないでください。権限はタスク開始時に
1 回だけ決まるため、同じタスク内の再試行は結果を変えません。宣言を作成・変更
しないでください。このエラーをスレッドに報告して halt してください。宣言は
次のタスクの開始時に読み直されます。

既定値にフォールバックしない理由: thread T-per-project-deploy-rule
```

**parse:**

```
BLOCKED: github.pr.open [{repo}] reason=parse
{repo} の {target_ref} にある .mindwire-deploy.yaml は存在しますが、解釈できません:
{detail}
（単一ドキュメントで、トップレベルが mapping の YAML である必要があります。
空のファイルもこの拒否に含まれます。）

この拒否はループ（implementer）自身では解消できません。人間の操作が必要です。

[人間の運用者へ]
{repo} の {target_ref} 上の .mindwire-deploy.yaml を修正してください
（{target_ref} への直接コミット、またはそのリポジトリ自身のリリース手順で到達させる）。
宣言が修正されるまで、このリポジトリでは PR を開けません。

[ループ（implementer）へ]
作業ツリーでこのファイルを修正して再試行しないでください。宣言は {target_ref} から
しか読まれないため、拒否は解消しません。このエラーをスレッドに報告して
halt してください。

既定値にフォールバックしない理由: thread T-per-project-deploy-rule
```

**schema:**

```
BLOCKED: github.pr.open [{repo}] reason=schema
{repo} の {target_ref} にある .mindwire-deploy.yaml は YAML として読めましたが、
schema に適合しません: {detail}
（許されるトップレベルキーは schema_version と pr_base_allow のみ。schema_version は 1、
pr_base_allow は重複・wildcard を含まない 1 個以上の branch 名です。）

この拒否はループ（implementer）自身では解消できません。人間の操作が必要です。

[人間の運用者へ]
{repo} の {target_ref} 上の .mindwire-deploy.yaml を修正してください
（{target_ref} への直接コミット、またはそのリポジトリ自身のリリース手順で到達させる）。
未知のキーは無視されず拒否されます。宣言が修正されるまで、このリポジトリでは
PR を開けません。

[ループ（implementer）へ]
作業ツリーでこのファイルを修正して再試行しないでください。宣言は {target_ref} から
しか読まれないため、拒否は解消しません。このエラーをスレッドに報告して
halt してください。

既定値にフォールバックしない理由: thread T-per-project-deploy-rule
```

**out-of-ceiling:**

```
BLOCKED: github.pr.open [{repo}] reason=out-of-ceiling
{repo} の {target_ref} にある .mindwire-deploy.yaml は、MindWire が許す範囲外の
branch を宣言しています: {offending}
現在 MindWire が許す範囲: {ceiling}
範囲外の要素が 1 つでもあると、宣言全体が拒否されます（範囲内の要素だけを
採用することはしません）。

この拒否はループ（implementer）自身では解消できません。人間の操作が必要です。

[人間の運用者へ]
次のいずれかを行ってください:
  - {repo} の {target_ref} 上の宣言から範囲外の branch を除く
  - その branch が本当に必要なら、MindWire 側の天井を広げる変更を
    spirrow-mindwire への PR として出す（人間のレビューを経る）
リポジトリ側の宣言だけで MindWire の天井を広げることはできません。

[ループ（implementer）へ]
宣言を変更して再試行しないでください。このエラーをスレッドに報告して
halt してください。

既定値にフォールバックしない理由: thread T-per-project-deploy-rule
```

**base-not-allowed:**

```
BLOCKED: github.pr.open base={requested_base} [{repo}] reason=base-not-allowed
{repo}/.mindwire-deploy.yaml（{target_ref}）は pr_base_allow: [{declared}] を宣言しています。
{requested_base} はこれに含まれません。

[ループ（implementer）へ]
{declared} のいずれかを base にして開き直してください。宣言は変更しないでください。

[人間の運用者へ]
base の許容範囲を変える必要がある場合は、{repo} の宣言の変更が必要です。
その変更は人間のレビューを経て {target_ref} に到達した時点で有効になります
（ループの作業ツリーや feature branch での変更は効きません）。
```

### 7.3 fail-closed の及ぶ範囲 — ループは不在・不正の宣言を自力で直せない

**宣言に起因する拒否（§7.1 の `absent` / `indeterminate` / `parse` / `schema` / `out-of-ceiling`）で落ちるのは、宣言が支配する操作（v1 では `github.pr.open`）だけであり、タスク全体ではない。** これは §4.3 の構築表の 3 行目の帰結である。実効ポリシーは生成され、天井が許す他の操作（`fs.write`、テスト実行、ローカル commit 等）はそのまま走る。タスク全体が走らないのは、天井自体が読めない場合（構築表の 1 行目）だけである。

**ただし、ループは不在・不正の宣言を自力で用意・修正して拒否を解消することはできない。**

- 実行時は宣言を `TARGET_REF = "main"` からのみ読む（INV-6.2 / §5.1）。作業ツリー・`HEAD`・PR head は読まない（INV-6.4 / INV-10）
- ∴ implementer が作業ツリーや feature branch で宣言を作成・修正しても、`main` 上の判定は変わらない
- 宣言を含む PR を開くこと自体が、拒否されている `github.pr.open` である
- ループが自分で `main` の宣言を置き換えられるなら、それは INV-2 が禁じる自己付与経路そのものである（U-8）。**∴ この不能は欠陥ではなく、境界が働いていることの帰結である**

**宣言に起因する拒否を受けたときの implementer の振る舞い（規範）**: 宣言ファイルを作業ツリーで作成・修正して `github.pr.open` を再試行してはならない。同じタスク内で再試行してもならない（`indeterminate` を含む。§7.1）。§7.2 の拒否文言をそのままスレッドに報告して halt する。復旧は §7.4 の人間の手順による。`base-not-allowed` は宣言に起因する拒否ではなく、ループの要求の誤りである。∴ 宣言を変えずに許された base で開き直してよい（§7.1）。

**帰結 — 新規プロジェクトの導入（PR-C 活性化後）は、人間が対象 repo の `main` に宣言を置くことから始まる。** 代償として、導入ごとに人間の操作が 1 回要る。これは「宣言が人間の操作を経ずに有効にならない」ことと同じ事実の表裏であり、受け入れる。

**今回のロールアウトには影響しない。** PR-A / PR-B（§10.5 手順 4）は、PR-C が強制を有効にする**前に** open される。∴ 宣言に起因する deny は発生しない。本節を「PR-A / PR-B が開けない」と読んではならない。

### 7.4 lockout からの復旧（常に人間 1 手）

将来どこかで宣言ファイルが消えた／新 repo が scope に入った場合、ループは deny され PR を出せない。

**復旧手順はコード変更でもループ経由でもなく、人間が対象 repo の `main` に宣言ファイルを 1 コミット置くことである**（branch protection 不在 ∴ 人間は `main` に直接 push できる、§8.3）。

これを PR-C の PR body / README に明記する。「後で有効化する約束」ではなく「今すぐ実行可能な手順」ゆえ腐らない。**この復旧可能性が、猶予期間なしの無条件 fail-closed（R-8）を採れる根拠である。**

---

## 8. 根拠

### 8.1 INV-6 は単独では機能しない

読み取り ref を固定する INV-6 は、**読み取り経路が 1 本であることと組で初めて意味を持つ**。override 経路や暗黙のフォールバックが 1 本でも残れば、固定した ref は迂回される。∴ INV-6.3 / INV-6.4 の「override 禁止・fallback 禁止」は装飾ではなく、INV-6.2 の成立条件である。

これは §1.1（INV-2 と INV-6 を統合しない）と対になる: **境界の定義は 1 つ、執行手段は複数の下位条件から成り、下位条件が 1 つ欠けると執行が成立しない。**

**INV-6 は「どこを読むか」を固定するだけであり、「そこにループが書けないこと」は保証しない。** 後者は INV-2 の成立に必要なもう一方の条件であり、branch protection が使えない以上（§8.3）、天井の `git.push` が担う（§9 U-8）。

### 8.2 default branch の実測

両 repo の default branch は `main` である（実測済、INV-6.2 に記録）。

**この一致は記録するが SOT にしない。** 「default branch から読む」（旧 INV-2 の内包）と「静的に `main` を読む」（新 INV-6.2 の内包）は、今日の外延が一致するだけで内包は異なる。default branch は GitHub の設定であって、我々の管理下の定数ではない。∴ 実装は**静的定数 `TARGET_REF` を持ち、API 呼び出しに明示的に渡す**。「ref 無指定 = default branch」の暗黙依存を作らない。

### 8.3 branch protection は使用不可（両 repo 403）

∴ 以下はいずれも設定不能である:

- required status check
- CODEOWNERS
- admin bypass 禁止
- `main` 直 push 禁止

**帰結 1: CI の赤はマージを妨げない。** 「PR-C を開く前に両 repo の `main` に宣言が到達していること」は CI では担保できない ∴ **PR-C を開かないことで担保する**（§10.2 の §0-gate）。

**帰結 2: 人間は `main` に直接 push できる** ∴ §7.4 の復旧手順が成立する。

**帰結 3: GitHub はループの token による `main` への直 push も止めない。** ∴ INV-2 の境界（読み取り先 `main` がループの書き込み可能領域と交わらないこと）は、**天井 allowlist の `git.push` が `main` を拒否していることに全面的に依存する**。これが偽なら、ループは宣言を書き換えて `main` に直 push し、自己昇格できる。前提として §9 U-8 に置き、PR-C の open 前に §10.2 で検査する。

### 8.4 `git.merge_to_main` は Tier C

`main` へのマージは常に人間が行う。**本 spec が許すのは PR を*開く*ことのみである**（Tier A、可逆）。mindwire が `pr_base_allow: [main]` を宣言することと、`main` への自動マージは別事象である。この不変は本 spec のいかなる条項によっても緩められない。

---

## 9. 未検証前提

**推測で埋めてはならない。** 実装者は着手前に実測し、結果をスレッドに報告する。

| # | 前提 | 状態 | 偽だった場合 |
|---|---|---|---|
| **U-1** | ループが Spirrow-VoxelWorld に対し `github.pr.open` を保持している | **未測** | **PR-A は人間が出す**（退避経路確定済） |
| **U-2** | `.mindwire-gate` の実装（存在検査のみか / どの ref を見るか） | **未測** | §3.1 の命名差し戻し条件が発火しうる。実装者は独断で命名を変えず spec 改訂 PR を出す |
| **U-3** | 実行時から GitHub contents API へ到達可能・token scope あり | **実測で真**（httpx ＋ loop token で HTTP 200） | — （偽なら §5.2 の退避経路へ。現時点では発動しない） |
| **U-4** | 各ターンが `/mindwire-turn` 経由で起動され chatroom を ground-truth として読んでいるか | **未測** | relay 実装ではなく起動手順に欠陥がある、と結論が変わる |
| **U-5** | 実行時が宣言を読む ref が `main` HEAD であること（INV-6 の実装可能性確認） | **未測** | INV-6 の実装方法が変わる。R-9（§5.1）の正しさに必要 |
| **U-6 / U-7** | （default branch・branch protection の実測） | **実測済**（§8.2 / §8.3） | — |
| **U-8** | 天井 allowlist の `git.push`（および `main` への ref 更新を伴う全操作）の target_glob の**いずれも** `main` に**マッチしない**こと。literal `main` を含まないことだけでなく、`*` 等の glob が `main` に一致しないことを確認する。測定対象は**稼働中ループが実際にロードする**天井ファイル（スケジュールタスクが実行する `main` チェックアウト上のもの）であり、開発用 checkout の写しではない | **未測**（2026-10-01、開発用 checkout 上では天井ファイルを発見できず） | **INV-2 不成立。PR-C を halt する。** 先に天井から `main` 宛 push を除く mindwire への PR（人間レビュー）を出し、その merge 後に U-8 を再測する。U-8 が真になるまで PR-C を open してはならない |

---

## 10. 手順と PR スコープ

### 10.1 PR スコープ

| # | repo | 触るファイル | 開く人 | base | 前提 |
|---|---|---|---|---|---|
| **PR-0** | spirrow-mindwire | 本 spec（`spec/deploy/per-project-deploy-rule.md`）**のみ** | Heisenberg | `main` | §11.5（`【供給待ち】` 残存なし） |
| **PR-A** | Spirrow-VoxelWorld | `.mindwire-deploy.yaml`（新規）**のみ** | Heisenberg（U-1 が偽なら人間） | **`develop`** | schema 凍結済（§3.2）。PR-0 マージ後 |
| **PR-B** | spirrow-mindwire | `.mindwire-deploy.yaml`（新規）**のみ** | Heisenberg | **`main`** | 同上。PR-A と並行可 |
| **PR-C** | spirrow-mindwire | 強制コード / テスト（INV-1〜10、`test_single_policy_construction_site`）/ **両 repo の fixture**。**宣言ファイルを含まない** | Heisenberg | **`main`** | **両 repo の `main` に宣言が到達済み**（mindwire は PR-B の merge、VoxelWorld は PR-A の `develop` merge の後に §10.5 手順 6 で `main` へ到達）、かつ **U-8 が真**であることを §10.2 で確認後 |

**base の非対称は意図的である。** VoxelWorld はリリーストレインを敷いている ∴ `develop`。mindwire は `main` から継続デプロイされ `develop` を持たない ∴ `main`。**この非対称こそが本 spec の存在理由である**（§2.1）。

**非対称の帰結 — PR-A の merge は VoxelWorld の `main` への到達を意味しない。** 実行時の読み取り先は `TARGET_REF = "main"`（INV-6.2）であり、`develop` 上の宣言は読まれない。∴ VoxelWorld については「PR-A の merge」と「`main` への到達」が別の事象であり、後者には人間の操作が要る（§10.5 手順 6）。**PR-A の merge をもって §0-gate の充足とみなしてはならない。**

**マージは全て人間**（§8.4）。

### 10.2 §0-gate（PR-C を開く前の read-only 確認。**両 repo ＋ 天井**）

PR-C を開く前に、以下を read-only で確認し、**いずれか欠けていれば halt する**:

- Spirrow-VoxelWorld の `main` に `.mindwire-deploy.yaml` が存在すること
- **spirrow-mindwire の `main` に `.mindwire-deploy.yaml` が存在すること**
- **U-8 が真であること**（稼働中ループがロードする天井で、`main` に到達しうる push 系 target_glob が 1 つも無いこと）

**mindwire 側を落とさないこと。** システム自身の境界に関する証明が抜け落ちる。

宣言の存在に関する判定述語は「**scope 内の全 repo の default branch に宣言が存在する**」という単一かつ均一な述語であり、§5.1 の実行時述語および人間が手で実行した probe と**完全に一致**する。

結果は PR-C の body に記載する。

### 10.3 fixture 規則（**両 repo**）

- base fixture は**両 repo の `main` 上の実宣言から各 1 本**を 1:1 転写する（ネットワーク不要）
- 負の fixture は base の**決定的 1 箇所破壊**により生成する
- fixture と実ファイルの一致はレビュー時に照合する（INV-9）
- **fixture の供給は読み取り client の差し替えによってのみ行う。** INV-6.3 補足に従い、テストは `TARGET_REF` を override してはならない（monkeypatch・引数・環境変数・fixture 用分岐のいずれによっても）。テストからも `TARGET_REF` は定数のまま読まれ、差し替えた client は**その `TARGET_REF` を受け取った上で** fixture を返す

### 10.4 必要テスト（最低限）

- 宣言なし → 実効ポリシーは生成され、`github.pr.open` を許す rule を含まない。評価器は `github.pr.open` を拒否する（INV-3 / §4.3 構築表の 3 行目）
- **宣言なし・不正（`absent` / `indeterminate` / `parse` / `schema` / `out-of-ceiling` の各 1 件）→ `github.pr.open` 以外の操作の許可集合が天井と同一である**（§7.3。タスク全体を止めないことの固定）
- 天井が読めない → 実効ポリシーが生成されない（§4.3 構築表の 1 行目）
- **評価器は、マッチする rule の無い操作を拒否する**（§4.3 read-back 2 の固定）
- property: `∀ 天井, 宣言（妥当・不在・不正を含む）: 実効 ⊆ 天井` かつ `実効 \ 支配操作 = 天井 \ 支配操作`（§4.1 / §4.3）
- 天井外 branch の宣言 → 宣言全体が拒否される（天井内の要素も採用されない。§4.2）
- 未知トップレベルキー → 宣言全体が拒否される（INV-4 / §3.3-6）
- **§7.1 の 6 種の reason がそれぞれ別の文字列として得られ、§7.2 のそれぞれの文言に描画される**（6 種すべてについて各 1 件）
- **描画された文言に、そのタスクの `{repo}` が現れ、もう一方の repo の名前が現れない。`{ceiling}` はロード済み天井から描画される**（テスト内で天井 fixture の値を変えて、描画結果が追随することを確認する）
- 拒否理由として添付したデータを書き換えても、認可判断が変わらない（INV-7）
- 非規範フィールドが認可判断に影響しない（INV-7）
- PR-C マージ前後で許可 (repo, action) 集合が不変（INV-8）
- 両 repo fixture の parse → 期待権限集合（INV-9）
- **1 プロセス内で 2 プロジェクトを扱い、互いの宣言が混線しない**（§4.4。= 本件バグの一般形をテストに落としたもの）
- `test_single_policy_construction_site`（§4.3 read-back 1）
- **読み取り client に渡される ref が常に `TARGET_REF`（= `"main"`）であることを、差し替えた client 側で記録して assert する**（INV-6.1 / INV-6.3。上記すべてのテストは §10.3 末項のとおり client 差し替えで fixture を供給し、`TARGET_REF` の override 経路を一切使わない）

### 10.5 実行順

1. **Heisenberg**: PR-0（本 spec）を open、base=`main`
2. **人間**: PR-0 を merge（Tier-C）
3. **Heisenberg**: U-1 / U-2 / U-4 / U-5 / **U-8** を実測 → スレッドに報告（U-8 が偽なら §9 の手順で天井修正 PR を先行させる）
4. **Heisenberg**: PR-A（base=`develop`）/ PR-B（base=`main`）を open（並行可、各々宣言 1 ファイルのみ、活性化時点で挙動不変）
5. **人間**: PR-A を `develop` に、PR-B を `main` に merge
6. **人間**: **VoxelWorld の宣言を `main` に到達させる。** 既定の経路は VoxelWorld 自身のリリーストレイン（`develop → release → main`）による通常のリリースである。§7.4 の `main` への直接 push も技術的には可能だが、VoxelWorld のブランチ方針を迂回する ∴ 選ぶかどうかは人間の判断とし、選んだ場合はその旨を PR-C body に記録する。**この手順はループの操作ではない**（`main` への到達は D-5 系の不変 = §8.4 により常に人間）。完了するまで手順 7 に進まない
7. **Heisenberg**: §10.2 の §0-gate（**両 repo ＋ U-8**）を実行 → 結果を PR body に記載して PR-C を open。**VoxelWorld 側が欠けていれば halt し、手順 6 の未了としてスレッドに報告する**（自分で `main` に到達させようとしない）
8. **Einstein**: PR-C レビュー（INV-1〜10 / fixture 一致 / 挙動不変性 / `TARGET_REF` 非 override）
9. **人間**: Tier-C merge
10. **Heisenberg**: `spec/process/ledger.md` に miss 1 件記録（「fail-closed 依存の導入で、依存先データの存在を実測せず設計が通過した。人間の probe で露見」＋ 参照）

**待ちの代償を明記する**: PR-C は VoxelWorld の次のリリースまで open できない。これは VoxelWorld のリリーストレインを尊重した結果であり、遅延を短縮したい場合は人間が宣言のみを載せたリリースを切ればよい（判断は人間）。

### 10.6 活性化時点で挙動不変

両 repo の宣言内容は**現行 de-facto 実効権限を保存**する（活性化を no-op にする）。締め付けは別 PR とする。

実装者は現行の単一 policy 構築点から実効権限集合を導出し PR body に列挙する。同値性は naysayer が検証する（INV-8）。

### 10.7 PR-C body 必須記載

1. §10.2 の §0-gate 結果（**両 repo ＋ U-8**。U-8 は測定した天井ファイルのパスと commit、該当する target_glob の全列挙を含める）
2. 活性化前後の実効権限集合（不変であること）
3. lockout 復旧手順（§7.4）
4. schema が PR-C のレビュー中に動いた場合の扱い（§11.2）
5. VoxelWorld の宣言が `main` に到達した経路（リリース / 直接 push）と、到達を確認した `main` の commit

---

## 11. 改訂規律

### 11.1 本 spec が SOT である

本 spec と実装が食い違った場合、**本 spec が正である**。実装を直すか、本 spec を直す PR を先に出すかのいずれかであり、「スレッドにこう書いてあった」を根拠に実装を通してはならない。

### 11.2 schema 凍結

**PR-A / PR-B は PR-C の schema に適合するファイルを載せる** ∴ **PR-A を開く前に schema を凍結する**（凍結点 = 本 spec §3.2、PR-0 マージ時点）。

PR-C のレビューで schema が動いた場合、**scope 内の全 repo に宣言更新 PR を入れ、それが merge されるまで PR-C を merge しない。** これを PR-C のマージ前提として PR body に書く（§10.7-4）。

### 11.3 一般規則としての順序規律

branch protection が使えない以上、強制面は **(i) `spec/process/obligations.yaml`（ループに効く）** と **(ii) 人間の merge 判断** の 2 つのみである。

**今回限りの手順を manifest に書かないこと**（腐る）。今回一回限りの具体（PR-A/B/C、誰がいつ）は本 spec と PR body に置く。manifest にも Python の文字列リテラルにも置かない。

**一般規則としての manifest 追加（`OBL-FAILCLOSED-DATA-FIRST`）は行わない**（人間 Tier-C 裁定 = A）。代わりに `spec/process/ledger.md` に miss 原因分類として 1 件記録する（§10.5-10）。毎ターンの注入コストは 0 であり、同種の miss が再発した場合には ledger の記録が manifest への昇格根拠になる。

### 11.4 番号ラベルの規律

- **INV 番号は §6 の確定ラベル（INV-1〜10）のみを使う。** 旧ラベルは §6.3 crosswalk の左列以外に現れてはならない
- **決定ラベルは §12 の `R-n` のみを使う。** 旧 `D-n` は §12 crosswalk の左列以外に現れてはならない

### 11.5 R-15 — 自己完結性の要件（本 spec は参照で命題を外部化しない）

**本 spec は、規範命題の本体を本ファイルの外に置いてはならない。**

- 不変条件・schema・エラー文言・手順のいずれについても、「正本は別の場所にある」という書き方をしない。**本ファイルを読むだけで実装とレビューが完結すること**が要件である
- 承認済み artifact の逐語は**本 spec 内に転記する**。転記は改変ではない ∴ 「承認済みだから触らない」は参照に留める理由にならない
- **要約による代替を禁止する。** 逐語が手元に無い場合は、要約で埋めず `【供給待ち】` として明示し、供給を待つ。要約を逐語と称して載せると、承認済み命題が起草者の再構成に静かに置き換わる（＝偽の単一 SOT。二重管理より有害である）

**PR-0 の open 条件**: 本 spec 内に `【供給待ち】` が 1 箇所でも残っている状態で **PR-0 を open してはならない**。実装者は open 前に本ファイルを grep し、残存を確認したら halt して本スレッドに差し戻すこと。

> 由来: 本規律は、§6.1 が INV-1〜6 を参照で外部化し §1 の単一 SOT 宣言と矛盾していたことを naysayer が検出したことによる（本 spec v1 草案に対する指摘）。**同種の外部化を今後の改訂でも構造的に禁止する。**

---

## 12. 決定 crosswalk（旧 `D-n` → 確定 `R-n`）

**スレッド上に `D-1`〜`D-7` が 2 系統（別命題集合）存在した** ∴ 本 spec では `R-n` に採番し直し、出自を併記する。**INV と同型の事故を D にも起こさないための処理である。**

| 確定 | 出自 | 命題 | 本 spec 内の所在 |
|---|---|---|---|
| **R-1** | 旧 A-D-1 | 宣言主体と所在 — 利用リポジトリがルートに宣言を置く。MindWire は既定値を持たない | §3.1 / §4.1 |
| **R-2** | 旧 A-D-2 | ファイル名 `.mindwire-deploy.yaml`（実行されないデータであることを名前で分離）＋ 差し戻し条件 | §3.1 |
| **R-3** | 旧 A-D-3 | v1 で宣言できるのは `pr_base_allow` のみ | §3.2 / §1.2 |
| **R-4** | 旧 A-D-4 | `schema_version` 必須・整数・v1 は `1`・未知値は fail-closed | §3.2 / §3.3-3 |
| **R-5** | 旧 A-D-5 | 天井との照合は glob×literal（プロジェクト側 wildcard 禁止） | §3.3-4 / §3.3-5 |
| **R-6** | 旧 A-D-6 | 実効ポリシーは per-task 構築物であって process-global ではない | §4.4 |
| **R-7** | 旧 B-D-1 | 順序: 宣言ファイルが先、コードは後（逆順は deadlock）。**条件分岐条項は削除済** | §10.1 / §10.5 |
| **R-8** | 旧 B-D-2 | 過渡期に猶予期間を置かない。初日から fail-closed（根拠 = lockout が常に人間 1 手で復旧可能） | §7.4 |
| **R-9** | 旧 B-D-3 | 存在検査は対象 ref を 1 回読む。PR head は読まない。404 / invalid / error の 3 分岐すべて deny、reason は別文字列 | §5.1 / §7.1 |
| **R-10** | 旧 B-D-5 | schema 凍結と改訂規律 | §11.2 |
| **R-11** | 旧 B-D-6 | 活性化時点で挙動不変 | §10.6 |
| **R-12** | 旧 A-D-7（作業ツリー読み＋ハッシュ記録） | **不採用。** 読み取り ref の固定（INV-6）と衝突する。working tree を読まないことは INV-10 でも要求される | — |
| **R-13** | 旧 B-D-4（`OBL-FAILCLOSED-DATA-FIRST` の manifest 追加） | **不採用**（人間 Tier-C 裁定 = A）。ledger に miss 1 件記録で代替 | §11.3 |
| **R-14** | 旧 B-D-7（spec ファイル化のスコープ増） | **採用。本 spec の存在そのものがこれである** | §1 |
| **R-15** | 新設（旧ラベル無し。v1 草案への naysayer 指摘による） | 自己完結性 — 規範命題を参照で外部化しない／要約による代替禁止／`【供給待ち】` 残存時は PR-0 を open しない | §11.5 |

> 凡例: 「旧 A-D-n」= 初期 spec 起案（`.mindwire-deploy.yaml` schema 系）の `D-n`、「旧 B-D-n」= 確定版設計仕様 v2（ロールアウト系）の `D-n`。両者は**別命題集合**であり、同じラベルが別のことを指していた。

---

## 13. 変更履歴

| 版 | 内容 |
|---|---|
| v1（PR-0） | 初版。スレッド `T-per-project-deploy-rule` の確定事項を単一 SOT に集約。INV 番号衝突を crosswalk で解消（§6.3）、D 番号衝突を `R-n` 再採番で解消（§12）、`policy_doc` を schema から除去、VCS 退避経路を条件付き規範として記述し PR-C 実装スコープからは除外（§5.2） |
| v1.1（PR-0 前） | naysayer 指摘により §6.1 の参照方式を破棄し、INV-1〜6 の逐語直接記載に変更（供給待ち）。自己完結性の要件を `R-15`（§11.5）として明文化し、`【供給待ち】` 残存時の PR-0 open を禁止 |
| v1.2（PR-0 前） | §6.1 に INV-1〜6（INV-6.1〜6.5 ／ INV-6.3 補足を含む）の承認済み逐語を転記。`【供給待ち】` は 0 箇所。§12 に R-15 行を追加 |
| v1.3（PR-0 前） | naysayer 裁定（security / BLOCKING）により U-8（天井の push 系 target が `main` に到達しないこと）を §9 に追加し、§8.1 / §8.3 帰結 3 / §10.1 / §10.2 / §10.5 / §10.7 で PR-C の open 条件に組み込んだ。INV-6.3 補足に基づき、client 差し替えによる fixture 供給と `TARGET_REF` 非 override を §10.3 / §10.4 に明記した |
| v1.4（PR-0 前） | naysayer 指摘（correctness / BLOCKING: PR-A base=`develop` と PR-C 前提「`main` にマージ済み」の矛盾による deadlock）を修正。PR-C の前提を「両 repo の `main` に宣言が到達済み」に改め（§10.1 / §8.3 帰結 1）、VoxelWorld の宣言を `main` へ到達させる人間の手順を §10.5 手順 6 として新設（既定はリリーストレイン、直接 push は人間判断で記録）。§10.7 に到達経路の記載を追加、手順番号の繰り下げに伴い §11.3 の参照を更新 |
| v1.5（PR-0 レビュー中） | PR-gate 指摘（#426、correctness / BLOCKING: §7.3 の「ループが宣言を自作して通る」bootstrap 経路は INV-6.2 / INV-6.4 / INV-10 と両立せず、PR-C 活性化後は 404 deny のまま進めない）を修正。§7.3 を「fail-closed の及ぶ範囲」に改め、ループは不在の宣言を自力で用意できないこと・不在時は再試行せず報告して halt すること・新規導入は人間が `main` に宣言を置くことから始まること・今回の PR-A / PR-B には影響しないことを明記。§7.2 の不在時文言を人間の運用者とループの双方に宛てて書き直し（`main` からのみ読まれることを明示）、天井外文言に「宣言の変更は `main` 到達時に有効」を追記。§7.1 を文言の宛先に合わせて更新 |
| v1.6（PR-0 レビュー中） | PR-gate 指摘（#426 @ `10514d4`）を修正。(1) correctness / BLOCKING: §4.3「ポリシーオブジェクトが生成されない」と §7.3「`github.pr.open` だけを落とす」の矛盾を解消。§4.3 に構築点の出力表を新設し、宣言の不在・不正では実効ポリシーを生成したうえで支配操作の rule を含めないこと、生成しないのは天井が読めない場合だけであることを定義した。評価器が allowlist 方式であることを read-back 義務とテストに追加し、§3.3・§4.2 の「hard error」の効果を §4.3 に結び付けた。(2) correctness / BLOCKING: §7.2 の文言を `{repo}` 等のプレースホルダを持つテンプレートに改め、repo 名・branch 名・天井の範囲のリテラルを除去した。(3) edge-case: 拒否理由を 6 種（`absent` / `indeterminate` / `parse` / `schema` / `out-of-ceiling` / `base-not-allowed`）に確定して §7.1 に表として置き、§3.3 の規則と §5.1 の分岐との対応、およびループの再試行可否を規定した。§7.2 で 6 種すべての文言を規定した。§10.4 にテストを追加した |
