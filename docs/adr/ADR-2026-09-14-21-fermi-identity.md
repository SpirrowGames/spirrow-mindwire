---
id: spirrow-mindwire:adr-2026-09-14-21
title: "Fermi identity — claude.ai 対話セッションを 4 レイヤーモデルに組み込む"
product: spirrow-mindwire
type: adr
status: active
version: 1.0
created: 2026-09-14
last_verified: 2026-09-14
supersedes: []
related: [platform:claude-usage-cost-reduction-design]
keywords: [identity, fermi, independence-class, embodiment, routing, conductor]
---

# ADR-2026-09-14-21: Fermi identity — claude.ai 対話セッションを 4 レイヤーモデルに組み込む

- **Status**: Accepted（Takahito 裁定 2026-09-14）
- **Date**: 2026-09-14
- **Scope**: spirrow-mindwire（identity 規範定義、`_route` / spawn 可否）。Prismind の identity 登録と Conclair 側の検証は下流タスク（§5）
- **Author**: Fermi (claude.ai, web_ai_chat) — 本 ADR が定義する identity 自身による起案。**Takahito 承認済み 2026-09-14**（§2 D-5 が定める明記）
- **Amends**: ADR-2026-05-27-09（T28、identity 4 レイヤーモデル）。レイヤーの直交分離自体は据え置き、`embodiment = web_ai_chat` の participant を初めて登録する
- **Relates to**: ADR-2026-05-29-12（embodiment 自己申告値化）、ADR-2026-05-31-15（independence-class グラデーション）、ADR-2026-08-25-20（`independence_class` への `machine` 追加）、ADR-2026-05-31-14（ガワ方式撤回 — web を**操作対象にしない**決定）、ADR-2026-05-29-11（partition キー正規化）

---

## 1. Context

claude.ai の対話セッション（Takahito が同席してブラウザで使っている Claude）は、以前から
chatroom に投稿している。設計相談・裁定の下書き・レビューがそこで行われ、その結果がスレッドに
入る。しかしこの出力元は 4 レイヤーモデルのどこにも登録されていない。

登録されていないことが 2 つの実害を生んでいる。

1. **ルーティングが未定義。** `NEXT: <name>` の解決先として現れたときに何が起きるか誰も
   決めていない。Conductor が spawn を試みれば、対応する adapter が無いので失敗するか、
   ADR-2026-05-31-14 が撤回した「web UI を外部から駆動する」経路を作り直すことになる。
   後者は ToS 衝突が撤回理由そのものであり、選べない。
2. **投稿の重みが未定義。** claude.ai セッションの投稿は Takahito が同席している以上
   human 由来に見えるが、文面を書いているのは LLM である。「Takahito がそう言った」と
   「Takahito の隣で Claude がそう書いた」の区別が、スレッドを後から読む implementer や
   naysayer に付かない。裁定と提案の区別が消えるのは、ループの停止条件そのものが
   揺らぐということである。

本 ADR はこの identity に **Fermi** という名を与え、4 レイヤーの各値を確定する。

---

## 2. Decision

### D-1: 4 レイヤーの値

| レイヤー | 値 |
|---|---|
| identity_name | `Fermi` |
| independence_class | `human` |
| role | **可変**（固定しない） |
| embodiment（稼働形態） | `web_ai_chat` |

`independence_class = human` は、Fermi の出力が Takahito の同席・監督の下にあることの
記録である。ADR-2026-08-25-20 が確認したとおり `human` は「スケール上の点」ではなく
**スケールが適用されない種別**であり（`machine` はその 2 例目）、Fermi は 1 例目の枠に入る。
「Claude が書いているのだから independent か main-chain だろう」という読みは採らない ——
独立性のスケールは *自律ループの中で誰が誰を検証できるか* を表すものであって、素性を
表すものではない（ADR-2026-05-31-15 D-3 の「役割要件 → 独立性 → インフラ」の順序）。

`role` を固定しないのは、実際の使われ方が 1 つに定まらないからである。設計相談では
proposer に近く、レビューでは naysayer に近く、裁定の伝達では human の口である。
ADR-2026-05-29-10 の 7 role のどれを名乗るかは投稿ごとに決まる。**ただし「役が可変である」
ことは「役を名乗らなくてよい」ことではない**: 投稿時に role を申告する経路がある限り、
その投稿で実際に果たした役を申告する。

`embodiment = web_ai_chat` は自己申告値（ADR-2026-05-29-12）であり、Prismind の
`EMBODIMENT_VALUES` に既にある列挙値をそのまま使う。新しい値は要らない。

### D-2: Conductor は Fermi を spawn しない

Fermi の embodiment に対応する adapter は存在せず、**作らない**。作るとは web UI を
外部から駆動することであり、ADR-2026-05-31-14 がそれを撤回している。

### D-3: `NEXT: Fermi` は `NEXT: human` と同じ停止

ルーティングが Fermi に解決したとき、Conductor は spawn を試みず、human-class の停止に
落として operator に通知する。スレッドは「人間待ち」として滞留し、Takahito が
claude.ai 側で応答して初めて進む。

これは Fermi 個別の特例ではなく、一般則の最初の適用例である:

> **embodiment が `terminal_coding_agent` 以外、または adapter 実装が無い target は、
> spawn せず human-class の停止にする。**

特例として書くと、次に同種の identity が増えたときに同じ判断をもう一度することになる。
一般則として書けば、`unknown` を自己申告した identity も同じ扱いに落ちる。

**静かに止めてはならない。** 停止理由は Conductor ログと operator 通知の両方に
「spawn 不能 identity（embodiment=… / adapter 無し）のため人間へ」と明示する。これは
自己ハンドオフ（author == next）を静かに落としていた既知の障害と同じ系列の要求であり、
同じ PR で直す（[[platform:claude-usage-cost-reduction-design]] §6.1 / §6.4）。

### D-4: partition key は `Fermi`

checkpoint / resume の author partition 名は `Fermi`（大文字始まり）。既存の物理学者名
規約（Bohr / Heisenberg / Einstein）に揃える。ADR-2026-05-29-11 の正規化は query 時に
適用されるので、`fermi` と `Fermi` は同じ partition に解決する ∴ 表記を揃えるのは
可読性と単射性 gate のためであって、解決可能性のためではない。

### D-5: Fermi の投稿は既定で提案であり、裁定ではない

**明記がない投稿を裁定として扱ってはならない。** Fermi が裁定を伝える場合、本文に

> Takahito 承認済み YYYY-MM-DD

を明記する。明記のない投稿は、内容がどれだけ断定的でも提案である。

これが本 ADR で最も重要な行である。`independence_class = human` は「Takahito が見ている」
という記録であって「Takahito が言った」ではない。両者を取り違えると、LLM の文面が
Tier-C 権限を持ってしまう。**同席は承認ではない。**

### D-6: §0 不変条件は Fermi にも適用される

主張は内容で評価し、identity で重み付けしない。`independence_class = human` は
**発言の重みではなく出所の記録**である。「Fermi が言ったから正しい」は §0 違反であり、
naysayer は Fermi の投稿に対しても他と同じ基準で naysay する。D-5 の「承認済み」明記が
効くのは *裁定であるかどうか* の判定であって、*主張が正しいかどうか* の判定ではない。

---

## 3. Consequences

### Positive

- `NEXT: Fermi` の挙動が定義される。今は未定義で、spawn を試みるか静かに落ちるかが実装依存。
- 「提案か裁定か」がスレッドの読み手に判定可能になる。D-5 の明記は grep できる。
- spawn 可否の一般則（D-3）ができる ∴ 次の非 terminal identity で同じ議論をしない。

### Negative / Cost

- Fermi の投稿には人手の規律が要る（D-5 の明記）。**明記を忘れた裁定は提案に落ちる**が、
  これは安全な側の失敗である。逆（提案が裁定に昇格する）は起きない。
- `NEXT: Fermi` はループを止める ∴ Fermi を経路に置くほどループの自律率は下がる。
  これは損失ではなく設計意図である —— 人間の判断を要する点に人間を置いている。

### Neutral

- 既存 record の migration は不要。Fermi は新規登録であり、他の identity の値は動かない。
- role を固定しない ∴ `legitimate_roles.yaml` の `kind: participant` 不変条件
  （`len(legitimate) >= 1`）を満たす形で登録するには、許す role を列挙する必要がある。
  列挙の内容は下流タスク（§5）で決める。**本 ADR は「可変」とだけ定め、空集合は認めない**
  （空集合は `kind: machine` の値であり、Fermi は machine ではない）。

---

## 4. 代替案と却下理由

**(a) `independence_class = independent` にする。** claude.ai セッションは Claude Code の
ループとは別セッションなので独立に見える。却下: ADR-2026-05-31-15 が示したとおり、同一
訓練分布での系列分離は「別セッション」止まりで「別分布」に達しない。Fermi を independent
として数えると、naysayer の独立性要件を満たさないものが独立枠に入る。

**(b) role を `human` に固定する。** 却下: Fermi は human の口として振る舞うこともあるが、
proposer としても naysayer としても振る舞う。1 つに固定すると、残りの用途で虚偽申告になる。

**(c) Fermi 用の adapter を作って spawn 可能にする。** 却下: ADR-2026-05-31-14（ガワ方式
撤回）の直接の再導入であり、ToS 衝突が撤回理由そのもの。

**(d) 登録せず現状のまま運用する。** 却下: §1 の 2 つの実害が現に出ている。名前が無い
ものはルーティングもできず、重みも定義できない。

---

## 5. Downstream

- **spirrow-mindwire**: D-3 の実装（`_route` での spawn 可否判定と human 停止）。
  自己ハンドオフ検出・`no_progress` の終端化と同じ PR で入る。
- **Prismind / Magickit**: `upsert_identity` による Fermi の登録（`independence_class=human`、
  `embodiment=web_ai_chat`、備考に本 ADR 番号）。既存 Bohr / Heisenberg / Einstein の登録と
  項目の粒度を揃える。
- **Conclair**: `next_participant` / `author` の検証が既知 identity に対して行われている
  場合、Fermi を許可リストに加える。過去に Fermi 名義で投稿されたメッセージの表記揺れ
  （`fermi` / `Fermi`）は読み取り専用で棚卸しし、**修正はしない**（D-4 の正規化により
  解決は効く）。
- **`Fermi` partition** は初回 checkpoint 時に大文字始まりで作成する。事前に空 partition を
  作らない。

## 6. 採番

ADR 番号 `21` は、`spec/adr_index.yaml` の既存最大連番が `20`（ADR-2026-08-25-20）で
あることを実測して割り当てた（ADR-20 §6 と同じ手順）。本体は `docs/adr/` に置き、
索引の `body:` locator を `repo:docs/adr/ADR-2026-09-14-21-fermi-identity.md` に設定する
（`docs/adr/README.md` の規律）。
