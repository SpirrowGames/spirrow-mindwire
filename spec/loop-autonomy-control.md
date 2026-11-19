# ループ自律制御 (HOLD / RESUME) — 実装仕様

- **status**: draft (未レビュー。naysayer / Tier-C 未通過)
- **起案**: 2026-08-04
- **発端**: 設計 (Bohr) → 実装 (Heisenberg) の連鎖が既定で閉じており、開けるには per-thread の
  `DELEGATE` マーカーを human が毎回書き直す必要がある。これを反転し、**既定で開き、プロジェクト単位の
  停止指示で閉じる**形にする。停止操作は UI から 1 アクションで行えること。

---

## 0. 実装ホストの分担

| Part | 対象 | 実装ホスト |
|---|---|---|
| **A** | `spirrow-conclair` — 状態の永続化 + API + UI | **{{HOST_SERVICES}}** |
| **B** | `spirrow-magickit` — MCP tool として公開 | **{{HOST_SERVICES}}** |
| **C** | `spirrow-mindwire` — conductor / sweep が状態を読む | {{HOST_LOOP}} (別担当) |

**A と B は同一ホストなので 1 セッションで通して実装できる。** C は別ホストで動くループ本体側で、
本書ではその**契約 (§7)** だけを凍結する。A/B を実装する側は C を実装しないこと。

A/B のみを先にマージしても既存動作は変わらない (誰も読まないため no-op)。これは意図した順序であり、
§8 で固定する。

---

## 1. 決め打ちした前提

以下は本書が**選択した**もので、議論の余地がある。変えるならここを直せば残りは follow する。

**P-1 状態は 3 値**: `run` / `supervised` / `hold`。
2 値 (自律 / 停止) にすると **現行の挙動 (設計ループは回るが実装には進まない) が失われる**ため 3 値を採る。

| 値 | 意味 |
|---|---|
| `run` | 完全自律。独立 naysayer の proceed で implementer まで進む (現行の `DELEGATE` 有効時と同じ) |
| `supervised` | 設計ループは回るが implementer には進まない。human の decide か PR-gate の REQUEST_CHANGES だけがコードに到達する (**現行の既定と同じ**) |
| `hold` | そのプロジェクトのループを動かさない。sweep は起動せず、実行中の conductor は次のラウンド境界で停止する |

**P-2 既定値は `run`**。状態が未設定のプロジェクトは `run` として扱う。
これは本件の目的そのもの (既定で自律)。**「未設定」と「読み取り失敗」は別物**で、後者は INV-2 により `hold` 扱いになる。

**P-3 認証は行わない。** tailnet に到達できる者は SpirrowGames の開発者であり同等権限を持つ
(Takahito 裁定 2026-08-04)。これは既存の environment trust model
(`spirrow-mindwire/src/spirrow_mindwire/conductor/core.py` の `_is_human` docstring —
author 文字列は認証されず、authoritative guard は human の手動マージ) と同じ立場である。
∴ **本件で認証を実装しない**。actor は記録であって認証ではない、と UI とドキュメントに明記する。

**P-4 状態の所在は conclair の専用テーブル**。予約スレッドにメッセージとして書く案も検討したが、
INV-4 (observed の書き戻し) がラウンドごとのメッセージ投稿になりスレッドを汚すため採らない。

---

## 2. 不変条件 (実装が守るべきこと)

**INV-1 latch する。** 状態は保存値であり、時間経過・無関係な発言・プロセス再起動では変化しない。
**明示的な設定操作でのみ**変わる。「マーカーを書き忘れたら自律に戻る」形にしてはならない。

**INV-2 読めなかったら止まる。** 状態の取得に失敗したとき (DB 不達 / API エラー / 未知の値) は
`hold` として扱う。自律側に倒れてはならない。**未設定 (行が無い) は失敗ではなく `run`** (P-2)。
この 2 つをコード上でも別経路にすること。

**INV-3 desired と observed を分離する。** 設定値 (desired) と、ループが実際にそれを読んで従った値
(observed) を別に保持し、UI は**両方**を表示する。ボタンを押しても効くのはループが次にそれを読んだ
時であり、実行中のターン (実装なら数分) は完了する。**即時停止を UI で約束しないこと。**

**INV-4 observed を書けるのはループだけ、desired を書けるのは操作者だけ。** API / MCP tool の
レベルで分離する。ループが desired を書けると「止めたのに勝手に再開していた」が起こりうる。

**INV-5 状態は 1 つ。** UI からの操作と MCP からの操作は同じレコードを更新する。
二系統の状態を作らない。

**INV-6 履歴が残る。** 誰がいつどの値に変えたかを追える。P-3 より、これは監査記録であって認証ではない。

---

## 3. 全体構成

```
  [操作面 1] conclair dashboard  ──PUT──┐
  (tailnet, 出先からスマホで可)          │
                                        ├──> conclair: project_control テーブル
  [操作面 2] claude.ai + magickit MCP ──┘         │
  (tailnet 不要、UI が落ちていても可)              │ GET
                                                  │
                       mindwire conductor / sweep ─┘  (Part C, 別ホスト)
                                  │
                                  └── POST observed (書き戻し)
```

---

## 4. Part A — `spirrow-conclair`

### 4.1 データモデル

`src/spirrow_conclair/models/project_control.py` を新設。既存の `models/thread.py` の書式に合わせる
(`Text` / `TIMESTAMP(timezone=True)` / `CheckConstraint` で enum を縛る)。

```python
CONTROL_STATES = ("run", "supervised", "hold")

class ProjectControl(Base):
    __tablename__ = "project_control"

    project: Mapped[str]        = mapped_column(Text, primary_key=True)
    desired_state: Mapped[str]  = mapped_column(Text, nullable=False)
    desired_actor: Mapped[str]  = mapped_column(Text, nullable=False)
    desired_at: Mapped[datetime]= mapped_column(TIMESTAMP(timezone=True), nullable=False)
    observed_state: Mapped[str | None]   = mapped_column(Text, nullable=True)
    observed_actor: Mapped[str | None]   = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(f"desired_state IN {CONTROL_STATES}", name="project_control_desired_check"),
        CheckConstraint(
            f"observed_state IS NULL OR observed_state IN {CONTROL_STATES}",
            name="project_control_observed_check",
        ),
    )
```

履歴用に append-only の `project_control_history` を併設する (INV-6):

```python
class ProjectControlHistory(Base):
    __tablename__ = "project_control_history"

    id: Mapped[int]             = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project: Mapped[str]        = mapped_column(Text, nullable=False)
    state: Mapped[str]          = mapped_column(Text, nullable=False)
    actor: Mapped[str]          = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime]= mapped_column(TIMESTAMP(timezone=True), nullable=False)
    note: Mapped[str | None]    = mapped_column(Text, nullable=True)

    __table_args__ = (Index("idx_control_history_project", "project", "changed_at"),)
```

> **`chatroom_events` に相乗りしない理由**: あちらの `action` は conclair 側が enum で縛っており、
> magickit の `chatroom_list_events` tool schema にも同じ enum が写っている。新しい action を
> 足すと 2 リポジトリの変更が必要になり、A/B を独立にマージできなくなる。
> 監査ログの一本化が望ましいなら後続で移せばよい (その判断は本書の範囲外)。

**migration**: `alembic/versions/` に 1 本追加。既存は `0001_initial.py` のみ ∴ `0002_project_control.py`。
**既存行のバックフィルは行わない** (未設定 = `run` が既定であり、行の不在に意味がある)。

### 4.2 API

`src/spirrow_conclair/api/control.py` を新設。既存 (`api/threads.py`) の書式に合わせ、
`APIRouter(prefix="/v1/projects/{project}/control", tags=["control"])`。`main.py` で include。

| メソッド | パス | 用途 | 誰が呼ぶ |
|---|---|---|---|
| `GET` | `/v1/projects/{project}/control` | 現在値の取得 | 操作面 / ループ |
| `PUT` | `/v1/projects/{project}/control` | **desired** の設定 | 操作面のみ |
| `POST` | `/v1/projects/{project}/control/observed` | **observed** の報告 | ループのみ |
| `GET` | `/v1/projects/{project}/control/history` | 履歴 (直近 N 件、既定 20) | UI |

**GET のレスポンス** (行が無い場合も 200 を返し、既定を示す):

```json
{
  "project": "spirrow-voxelworld",
  "desired_state": "run",
  "desired_actor": "human",
  "desired_at": "2026-08-04T02:11:00Z",
  "observed_state": "supervised",
  "observed_actor": "mindwire-conductor",
  "observed_at": "2026-08-04T02:08:31Z",
  "configured": true
}
```

`configured: false` のときは `desired_state: "run"` / `desired_actor: null` / `desired_at: null` を返す。
**404 を返さないこと** — 呼び出し側が「未設定」と「取得失敗」を区別できなくなり INV-2 を壊す。

**PUT のリクエスト**: `{"state": "hold", "actor": "human", "note": "optional"}`。
`state` が enum 外なら 422。成功時は GET と同じ形を返し、**`project_control_history` に 1 行追加**する。
同じ値への PUT も履歴に残す (押した事実は事実)。

**POST /observed のリクエスト**: `{"state": "hold", "actor": "mindwire-conductor"}`。
**`desired_*` を一切変更しないこと** (INV-4)。履歴には残さない (ラウンドごとに来るため)。

### 4.3 UI

conclair の `/ui/projects/{project}/threads` ページ**上部**に制御ウィジェットを置く。
理由: プロジェクト単位のスコープが既にそのルートにあり、Takahito がスレッドを読む画面と同じ場所であること。

表示要件:

- 3 値のセグメント (`RUN` / `SUPERVISED` / `HOLD`)。現在の **desired** が選択状態
- その下に **observed** を別行で表示する (INV-3)。例:
  `設定: HOLD (human, 2 分前) / ループが最後に見た値: RUN (3 分前)`
- 両者が食い違っている間は**「反映待ち」であることを明示**する。
  実行中のターンは完了するので即時ではない旨も 1 行添える
- observed が一定時間 (例: 15 分) 更新されていない場合は「ループが動いていない可能性」と表示する。
  **これは推測であって断定ではない**旨がわかる文言にすること
- `hold` を選択中はウィジェット全体の色を変える等、ひと目でわかるようにする
- 直近の履歴 3 件 (誰がいつ何に変えたか) を折りたたみで表示
- **「認証ではなく記録である」旨の注記**を 1 行入れる (P-3)

実装:

- 既存の htmx パターンに従う。`POST /ui/projects/{project}/control` (form field: `state`) →
  ウィジェットの partial を返す。既存の `web/routes.py` の `messages_post` と `partials/` の書式に合わせる
- actor は navbar の `author-input` (localStorage、`static/js/conclair.js` が htmx リクエストに注入する)
  をそのまま使う。空なら 422 にして「author を入れてください」を flash で出す
- observed を更新するため、ウィジェットは既存のスレッド一覧と同様 `hx-trigger="every 7s"` で
  自前の partial を再取得する

**ダッシュボード側 (`:8443/dashboard`) との統合**は本書では規定しない。conclair の `/ui` を
そのまま開ければ足りる。埋め込みたい場合は上記 partial のエンドポイントを再利用すること。

### 4.4 テスト (conclair)

- 行が無いプロジェクトの GET が `configured: false` + `desired_state: "run"` を返す (404 でない)
- PUT が enum 外を 422 で弾く
- PUT が履歴に 1 行足す。同値 PUT でも足す
- **POST /observed が `desired_*` を変更しない** (INV-4 の回帰テスト)
- migration の up / down が通る

---

## 5. Part B — `spirrow-magickit`

conclair の上記 API を MCP tool として公開する。既存の chatroom adapter と同じ書式に合わせること。

| tool | 引数 | 用途 |
|---|---|---|
| `loop_control_get` | `project` | 現在値の取得 |
| `loop_control_set` | `project`, `state`, `actor`, `note?` | **desired** の設定 (操作面) |
| `loop_control_report_observed` | `project`, `state`, `actor` | **observed** の報告 (ループ専用) |

**`set` と `report_observed` を 1 つの tool にまとめないこと** (INV-4)。
tool 境界で分離しておくことが、後で「ループにこの tool を渡さない」を実現する前提になる。

`loop_control_set` は claude.ai から呼ばれる想定 — これが「Tailscale を繋がなくてもスマホから
止められる」経路になる (UI が落ちていても効く冗長化)。

### テスト (magickit)

- 3 tool が conclair の対応エンドポイントを正しく叩く
- conclair が 5xx / 到達不能のとき、tool が**エラーを返す** (既定値を捏造しない)。
  INV-2 の fail-closed は呼び出し側 (mindwire) が担うが、そのためには**失敗が失敗として伝わる**必要がある

---

## 6. Part C — `spirrow-mindwire` (別ホスト。契約のみ凍結)

**A/B の実装者はここを実装しない。** 以下は A/B が満たすべき契約の確認用。

- conductor は毎ラウンド `loop_control_get` を読み、
  `_delegation_active()` (`conductor/core.py`) を状態参照に置き換える
  - `run` → 独立 naysayer の implementer への proceed を許可 (現行の carve-out ③)
  - `supervised` → 現行の既定と同じ (human decide と PR-gate RC のみコードに到達)
  - `hold` → ラウンド境界で停止
- 取得失敗は `hold` 扱い (INV-2)
- 状態が前回と変わったときだけ `loop_control_report_observed` を呼ぶ (毎ラウンド書かない)
- sweep (`deploy/run-conductor-scheduled.ps1`) は `hold` のプロジェクトを起動しない
- **per-thread の `DELEGATE` マーカーは撤去する** (INV-5 — 二系統の状態を作らない)。
  `conductor/handoff.py` の `DELEGATE_MARKER` / `has_delegate_marker` と関連テストが対象
- ループのロール (Bohr / Heisenberg / Einstein) が `loop_control_set` を呼べないことを**確認**する。
  proposer は `tools=[]` ∴ 構造的に不可。**implementer の allowlist 経由の可否は未照合**

---

## 7. 実装順序

1. **Part A** (conclair: model + migration + API + UI + tests) — 単体で完結。誰も読まないので no-op
2. **Part B** (magickit: 3 tools + tests) — A に依存
3. **Part C** (mindwire) — 別ホスト・別 PR。ここで初めて挙動が変わる

**A/B を先に入れておくこと**が重要。逆順だと mindwire が存在しない API を読みに行き、INV-2 により
全プロジェクトが `hold` になる。

---

## 8. non-goals

- 認証 (P-3)
- `main` への自動マージ (不変。本件はエッジの**既定値**を変えるだけで、**誰が通れるか**は変えない)
- Bohr → Heisenberg 直行の解禁 (carve-out は独立 naysayer 限定のまま)
- `chatroom_events` への監査ログ一本化 (§4.1 の注記)
- `max_rounds` の値 (別件。ただし現行 6 のままだと本件の効果は 6 ターンで頭打ちになる)

---

## 9. 未確認事項 (実装者が入口で確認すること)

- **ダッシュボード (`:8443/dashboard`) の実装形**。{{HOST_LOOP}} から到達できないため未確認。
  conclair の `/ui` をどう包んでいるかによって §4.3 の統合方法が変わる
- **implementer の allowlist が magickit MCP write を許すか** (§6 の最終項目)
- **magickit の adapter が conclair API をどこまで写しているか**。§5 のコスト見積りはこれに依存
- 本書は naysayer レビューも Tier-C 決裁も経ていない。実装前に通すこと
