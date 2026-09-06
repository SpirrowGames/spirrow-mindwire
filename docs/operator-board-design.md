# Operator Board — 設計書（実装レベル）

版: **0.3.2** / 2026-09-06 / 起草: Claude（Cowork セッション）/ 決定者: Takahito / 設計レビュー: Einstein（msg-2543 → msg-2545 で blocking 解除、msg-2567 → msg-2569 で v0.3.1 blocking 解除）+ PR-review naysayer（PR #224 msg-(gate) → v0.3.2 blocking 解除）/ v0.2 差分の正本: Bohr msg-2544 / v0.3 差分の正本: Bohr msg-2566 / v0.3.1 差分の正本: Bohr msg-2568 / v0.3.2 差分の正本: 本ファイル §5.2A（Heisenberg、PR-review msg-(gate) 受け入れ）
設計 SOT: chatroom `spirrow-mindwire/T-operator-board`。本文はその同期コピー。
対象リポジトリ: spirrow-conclair（状態）・spirrow-mindwire（tick / executor）・spirrow-magickit（UI）
根拠: 2026-09-03〜04 operator セッションの実測（116 判断点）、light ティア判断リプレイ（一致 85%）、3 リポジトリのソース調査（conclair `cb517af` / mindwire `60f52b1` / magickit `6bfa87d`）

---

## 0. 一言で

三者ループの「operator」を、**Conclair 上のカンバン状態機械 + 各 dev PC で走る Python tick** として実装する。カードの列がそのまま operator の記憶であり、遷移はコードで決定し、分類が要る点だけ Lexora light に問う。人は `Ready for human` 列だけを見る。tomtebo-02 が増えても設定が増えるだけで、状態の合成は起きない。

---

## 1. 決定事項（会話で確定したもの。以後の設計はこれを前提にする）

| # | 決定 | 根拠 |
|---|---|---|
| D1 | mindwire から独立させない。operator サブシステムとして mindwire に置く。UI は magickit web | docs/deploy.md が「再武装は operator の仕事」と operator を前提にしている。ボードの半分（sweep / quarantine / pending-decisions / digest）は既に mindwire の state に散在 |
| D2 | ボードの状態は Conclair に集約。executor（tomtebo-0x）から push。Phanthand はドリルダウン専用 | pull 型は node のスリープで盤面が消え、02 追加時に合成問題が出る |
| D3 | 単一スレッドを複数 node で分割しない。**関連しないスレッドのみ並列開発可** | Takahito 裁定 |
| D4 | 関連の定義は Conclair 側に持つ | Takahito 裁定 |
| D5 | Tier-C（main への merge、選択肢のある判断）は人。「返事が無ければ推奨で進める」は NG | Takahito 裁定 |
| D6 | 判断点の LLM は light（Qwen3.8-27B）。frontier 不要 | リプレイ検証: 粗分類 85%、自律局面の human エスカレーション precision 0.89 / recall 0.85 |
| D7 | 一番なくすもの: 静かに止まる／聞くまでもない問い | Takahito |
| D8 | 「宣言して履行しない」は規則でなく仕組みで消す | Takahito。ボード化で構造的に消える（§6） |

---

## 2. 現状の事実（ソース調査。設計はこれに接続する）

**Conclair** は FastAPI + **PostgreSQL**（asyncpg、alembic 0001〜0008）。`threads(project, thread_id)` に `affects_threads JSONB` と `tags JSONB` があるが、`affects_threads` は close 時にしか書かれない。関連テーブルは無い。`project_control` が loop_control の実体（`desired_state / observed_state ∈ run|supervised|hold`、履歴テーブルあり）。リース・判断のテーブルは無い。ルーターの型は `api/control.py` が最も小さく綺麗（ORM 1 本・schema 1 本・UPSERT・GET は 404 しない）。

**mindwire** の operator 相当は `deploy/run-conductor-scheduled.ps1`（**207KB の PowerShell**）。tick の流れ: sync-repo → sweep.json 読み → gate_bootstrap_tick → project ごとに loop_control probe と head probe → `head_skip_decide.py` で launch/defer/skip → 候補ループで `mindwire.toml` を書き換えて `run-conductor.ps1` を起動 → stdout の `conductor stopped:` 行を parse → 非 0 なら quarantine.json → 停止理由が human 系なら composer + Discord。state は `head_skip / evaluated / quarantine / notified / pending-decisions / digest / leases.json`（leases は `lib/Lease.ps1`、resource `editor`、**まだ候補ループを gate していない**）。`run_conductor(settings)` は TOML からしか thread を受け取らない。`StopReason ∈ human | none | no_handoff_to_human | no_progress_to_human | round_cap | empty_thread | hold`。

**magickit web** は FastAPI + Jinja + HTMX。`board.py` に **既にボードがある**（`/dashboard/decisions`、レーン `new / doing / parked`、SQLite `board_lanes` に手動レーンを保存、カードは判断待ち・deploy 承認待ち・止まったループの 3 種）。Conclair へは `ChatroomAdapter`（HTTP）。`ops.py` に `classify()`（running / stalled / held / unmanaged）がある。`decisions.py` に `PUT /v1/decisions/{p}/{t}/material` と `decision_materials` テーブル。

∴ 新規に作るものは思ったより少ない。**足りないのは (a) Conclair のボード状態と関連、(b) PowerShell の tick を Python にして状態を push すること、(c) magickit の既存ボードを operator の列に置き換えること**の 3 つ。

---

## 3. アーキテクチャ

```
 tomtebo-01 ──┐                       sg-ai-server-01
 tomtebo-02 ──┤  push (HTTP, Tailscale)   ┌──────────────────────────────┐
              │  ────────────────────►    │ Conclair (PostgreSQL)         │
  operator tick (Python, mindwire)        │  threads / messages / control │
   observe  : chatroom, gh, git, local    │  + board_cards / board_events │
   reconcile: 列を再計算                   │  + board_leases / board_nodes │
   decide   : 遷移表 (+ light 判断点)       │  + board_judgments            │
   act      : conductor 起動 / gate / 登録  │  + thread_relations           │
   push     : cards / events / heartbeat  └──────────────┬───────────────┘
              │                                          │ HTTP
              │   ◄──── リース取得 / 判断ログ ────         ▼
              │                                 magickit web /dashboard/operator
              │                                  列 × プロジェクトレーン
              │                                  Ready for human = 人の待ち行列
              └── Phanthand :7300 ◄── ドリルダウン（ログ tail）── magickit
```

責務の境界:

- **Conclair**: 状態の正本と関連の正本。判断しない。リースの原子性だけ担保する（UPSERT + 期限）。
- **mindwire `operator/`**: 観測・遷移・行為。node ごとに 1 プロセス、tick 駆動。状態を持たない（ローカルは node 固有キャッシュのみ）。
- **magickit web**: 描画と、人の操作（選択肢のクリック → chatroom への human decide 投稿、hold/run）。
- **Lexora light**: 遷移表で「判断」と印の付いた点だけ。

---

## 4. データモデル（Conclair、alembic `0009_operator_board`）

### 4.1 `board_cards` — カード = スレッド（PR はスレッドに紐づく属性）

```
project            TEXT NOT NULL
thread_id          TEXT NOT NULL
column             TEXT NOT NULL CHECK IN ('backlog','proposing','implementing','gate',
                                          'ready_for_human','waiting','post_merge','stalled','done')
column_since       TIMESTAMPTZ NOT NULL
kind               TEXT NOT NULL CHECK IN ('design','pr','ops')        -- ops = sweep 運用系
repo               TEXT NULL                                            -- 'SpirrowGames/spirrow-mindwire'
pr_number          INT NULL
head_sha           TEXT NULL
ci_state           TEXT NULL        -- success | failure | pending | unknown
gate_verdict       TEXT NULL        -- APPROVED | CHANGES_REQUESTED | NONE
gate_head_sha      TEXT NULL        -- verdict が紐づく head（現 head と一致して初めて有効）
merged             BOOLEAN NOT NULL DEFAULT FALSE
next_token         TEXT NULL        -- 最終 msg の NEXT: 逐語（Bohr / Heisenberg / human / none / pr-review …）
last_msg_id        TEXT NULL
last_motion_at     TIMESTAMPTZ NULL -- head / msg / PR のいずれかが最後に動いた時刻
waiting_on         JSONB NOT NULL DEFAULT '{}'  -- {"kind":"msg_approve","thread":"T-x","author":"Einstein"} 等（§5.3）
decision_ref       TEXT NULL        -- decision_materials の signature（Ready for human のとき）
presented_hash     TEXT NULL        -- 人に提示した事実のハッシュ（再提示抑止、§8）
node               TEXT NULL        -- 最後にこのカードを動かした node
facts              JSONB NOT NULL DEFAULT '{}'  -- 判断点に渡す 2〜5 文の事実（tick が生成）
updated_at         TIMESTAMPTZ NOT NULL
updated_by         TEXT NOT NULL    -- node 名 or 'human' or 'magickit-web'
PRIMARY KEY (project, thread_id)
FOREIGN KEY (project, thread_id) REFERENCES threads
INDEX idx_board_cards_column (project, column)
INDEX idx_board_cards_motion (last_motion_at)
```

### 4.2 `board_events` — 遷移の追記ログ（監査と「宣言未履行」の検出に使う）

```
id BIGSERIAL PK, project, thread_id, at TIMESTAMPTZ,
from_column TEXT NULL, to_column TEXT NOT NULL,
actor TEXT NOT NULL,            -- node 名 / human / magickit-web
reason TEXT NOT NULL,           -- 遷移表の規則 ID（例 'R-PUSH-GATE'）または判断 ID
judgment_id BIGINT NULL REFERENCES board_judgments,
details JSONB DEFAULT '{}'
INDEX (project, thread_id, at DESC)
```

### 4.3 `board_leases` — 並列規則の原子性（§7）【v0.2: resource_key に一般化】

```
project, resource_key TEXT NOT NULL,   -- 'thread:<thread_id>' | 'repo:<owner/name>'
node TEXT NOT NULL, kind TEXT CHECK IN ('turn','implementing'),
acquired_at, expires_at TIMESTAMPTZ NOT NULL, renewed_at,
PRIMARY KEY (project, resource_key)
```
- `thread:` と `repo:` を同じ表で持つ。`implementing` に入るには **thread lease と repo lease の両方**が要る。repo lease は fleet-wide に 1 枚（v0.1 の「同 repo の implementing は node 内で 1 枚」を node 境界を跨いで昇格させたもの）。
- 取得は `resource_key` の**辞書順で all-or-nothing**。1 つでも取れなければ取った分を即座に解放し、部分保持で待たない（2 node が 1 枚ずつ握って睨み合う deadlock を消す。残るのは live-lock だけで、それは次 tick の再試行で吸収）。
- 1 レコードの取得は `INSERT ... ON CONFLICT (project, resource_key) DO UPDATE SET node=EXCLUDED.node, ... WHERE board_leases.expires_at < now() OR board_leases.node = EXCLUDED.node`、影響行数 0 なら失敗。node 死は期限で回収。
- lease の一般形（FIFO / 飢餓 / 再入）は `T-exclusive-resource-lease-queue` の成果物を **consume** する。ここで 2 個目の lease 実装を生やさない。依存は両側に書く。
- 既存の `leases.json`（`lib/Lease.ps1`）はこのテーブルに置き換える。

### 4.4 `board_nodes` — executor の生死

```
node TEXT PK, last_tick_at TIMESTAMPTZ, tick_duration_ms INT, version TEXT,
projects JSONB,            -- この node が担当するプロジェクト（設定の写し）
capacity JSONB,            -- {"implementing_max":1,"turns_max":3}
in_flight JSONB            -- 走行中の turn（thread_id, role, started_at）
```

### 4.5 `board_judgments` — 判断点の入出力ログ。**評価データセットがここに溜まる**

```
id BIGSERIAL PK, project, thread_id, at, node,
point TEXT NOT NULL,        -- 'route_rc' | 'escalate' | 'stall_cause' | 'related'
input JSONB NOT NULL,       -- facts + 選択肢
output JSONB NOT NULL,      -- {"label":..., "reason":...}
model TEXT, tier TEXT, latency_ms INT, prompt_version TEXT,
overridden_by TEXT NULL,    -- 人が後で覆した場合（UI から）
overridden_to TEXT NULL
```

### 4.6 `thread_relations` — 関連の正本（D4）

```
project, a TEXT, b TEXT, kind TEXT CHECK IN ('affects','references','declared','same_pr_chain'),
source TEXT NOT NULL,       -- 'threads.affects_threads' | 'messages.references_threads' | 'human' | 'operator'
created_at,
PRIMARY KEY (project, a, b, kind)
CHECK (a < b)               -- 無向。挿入時に正規化
```
投入経路: (1) `open_thread` / `close_thread` / `post_message` の既存 JSONB を書くのと同じトランザクションで派生行を書く（Conclair 内、`services/relations.py`）。(2) 人が UI で「関連」を宣言（`kind='declared'`）。(3) operator が同一 PR チェーン（`T-pr-review-<repo>-<n>` ↔ 設計スレッド）を `same_pr_chain` で結ぶ。
API: `GET /v1/projects/{p}/threads/{t}/related?depth=1` は無向到達集合を返す（**深さ既定 1**、上限 50）。**lease の排他判定は depth-1 のみを消費する**（§7）。depth≥2 の消費者は J-RELATED（light の助言）と UI 表示に限定し、安全経路から外す。P1 終了時に depth≥2 の実消費者がどちらにも無ければパラメータごと落とす【v0.2 C-4】。

### 4.7 `project_control` の拡張 — hold に期限

```
ALTER TABLE project_control ADD COLUMN desired_expires_at TIMESTAMPTZ NULL;
```
`GET /control` は `desired_expires_at < now()` のとき `desired_state` を `run` として返し、履歴に `expired` を追記する（読み側で失効。cron 不要）。`PUT /control` に `ttl_seconds` を受ける。operator lane の hold は既定 4 時間。**戻し忘れが無害になる。**

### 4.8 API（`api/board.py`、prefix `/v1/projects/{project}/board`）

| method | path | 用途 |
|---|---|---|
| GET | `/cards?column=&node=` | 一覧（UI・tick の再同期） |
| PUT | `/cards/{thread_id}` | tick からの upsert（`If-Match: updated_at` で楽観ロック、衝突は 409） |
| POST | `/cards/{thread_id}/transition` | `{to, reason, actor, judgment_id?, details}` → cards 更新 + events 追記を 1 トランザクション |
| POST | `/leases` | `{node, kind, ttl_seconds, resource_keys:[...]}` → 辞書順 all-or-nothing。200 全取得 / 409 いずれか他 node 保持（何も保持しない） |
| DELETE | `/leases` | `{node, resource_keys:[...]}` 解放 |
| PUT | `/nodes/{node}` | heartbeat |
| POST | `/judgments` | 判断ログ追記、id を返す |
| POST | `/judgments/{id}/override` | 人が覆す |
| GET | `/events?since=` | UI のタイムライン |
| GET | `/threads/{t}/related` | §4.6 |

---

## 5. 列と遷移表（状態機械）

### 5.1 列

| 列 | 意味 | 入る条件の要約 |
|---|---|---|
| `backlog` | sweep 候補。未着手 | 旧 sweep.json の候補。順序 = 優先度（`facts.priority`） |
| `proposing` | Bohr のターン中／Bohr 指名 | `NEXT: Bohr`、または gate RC 裁定待ち |
| `implementing` | Heisenberg のターン中／指名 | `NEXT: Heisenberg`（human か naysayer の指名によるもの。guard (i) と整合） |
| `gate` | PR gate 発火中／verdict 待ち | push 検出後、verdict が現 head に付くまで |
| `ready_for_human` | Tier-C | APPROVE が現 head に紐づき CI 緑 / `NEXT: human` / 選択肢あり |
| `waiting` | 条件付き待機 | `NEXT: none` + 着手条件、他スレッドのレビュー待ち、hold、resource 待ち |
| `post_merge` | merged 検出後の後処理中 | sync-repo、台帳 close、次の proposer ターン |
| `stalled` | 止まっている | quarantine、head 不動、rounds=0 human の反復、starvation |
| `done` | 解決 | thread resolved かつ PR merged（または PR 無し） |

### 5.2 遷移表

規則 ID は `board_events.reason` にそのまま入る。「判断」列が空の行はコードで決定する。

| ID | from | 条件（tick が観測） | to | 行為 | 判断 |
|---|---|---|---|---|---|
| R-INTAKE | — | thread active かつカード無し | `backlog` | カード作成、`facts` 生成 | |
| R-NEXT-BOHR | backlog/any | 最終 msg の `NEXT: Bohr`（human/naysayer/実装者どれでも） | `proposing` | リース取得 → conductor 起動（§6.3） | |
| R-NEXT-HEIS | any | `NEXT: Heisenberg` かつ指名者が human か naysayer | `implementing` | リース取得（thread + repo）→ conductor 起動 | |
| R-NEXT-HEIS-GUARD | any | `NEXT: Heisenberg` かつ `routing.guard_proposer_to_implementer(...)` が redirect を返す | `ready_for_human` | **判定はボードが持たない。** conductor と同じ述語（mindwire `routing.py`、PR #222）を呼ぶだけ。「naysayer に回すべきか」は J-ROUTE | J-ROUTE |
| R-PUSH-GATE | implementing | PR の head が `gate_head_sha` と異なり CI が pending でない | `gate` | gate 発火（`naysayer_review.py` 相当を Python から直接） | |
| R-CI-WAIT | implementing | head 更新、CI pending | （据え置き） | 次 tick へ | |
| **E-CI-RED** | gate | `gate_admission` が `ROUTE_IMPLEMENTER` を返した（CI 赤 ∧ 現 head 未 route） | `implementing` | ci-route マーカ付きで relay → conductor を implementer で起動【v0.3.1 §A-5 / §B-3】 | |
| R-GATE-RC | gate | verdict = CHANGES_REQUESTED on 現 head | `proposing` | Bohr を起こす（skill: RC は proposer へ） | J-ROUTE（例外: objection が全て機械的 nit のとき implementer 直行） |
| R-GATE-OK | gate | verdict = APPROVED on 現 head ∧ CI 緑 ∧ !draft ∧ mergeable | `ready_for_human` | decision material 生成（推奨: merge）。**人に「教えて」と言わない** | |
| R-MERGED | ready_for_human / any | gh で `merged=true` | `post_merge` | repo プロファイルの post-merge 手順（mindwire: sync-repo.ps1）→ 台帳 close 可否判定 → `NEXT: Bohr` 相当で proposing へ | |
| R-POSTMERGE-DONE | post_merge | 後処理完了 ∧ thread resolved | `done` | | |
| R-POSTMERGE-NEXT | post_merge | 後処理完了 ∧ thread active | `proposing` | Bohr を起こす | |
| R-HUMAN | any | `NEXT: human`（役の Tier-C 依頼） | `ready_for_human` | composer で material 生成、`presented_hash` 記録 | J-ESCALATE（依頼段落に選択肢が無い／operator 権限で済む内容なら差し戻し） |
| R-NONE-COND | any | `NEXT: none` ∧ 本文に着手条件 | `waiting` | `waiting_on` を構造化して保存 | J-COND（条件の抽出） |
| R-NONE-SETTLED | any | `NEXT: none` ∧ thread resolved | `done` | | |
| R-WAIT-FIRE | waiting | `waiting_on` が成立 | 成立時の指定列 | 指定された行為（例: sweep 登録、Bohr 起こし） | |
| R-HOLD | any | control = hold（期限内） | `waiting` | 何もしない。期限切れで自動復帰 | |
| R-QUAR | any | conductor exit≠0 | `stalled` | quarantine 相当を `facts.failure` に保存、Discord | J-STALL（原因分類: 外部障害 / 資格情報 / スレッド固有） |
| R-SILENT | proposing/implementing/gate/**waiting(resource)** | `NEXT:` が役を指したまま `last_motion_at` から N tick 動かず（既定 3 tick=15 分でリース確認、6 tick で stalled）。**resource 待ちの `waiting` も同じ閾値で検出対象**【v0.2】 | `stalled` | in_flight を確認、死んでいればリース解放して再起動 1 回、再発で `ready_for_human` | |
| R-STARVE | backlog | プロジェクト単位で `evaluated` 以来 24h ターン無し | （列は据え置き、レーンに starvation フラグ） | Discord + UI 強調 | |
| R-HUMAN-SPIN | ready_for_human | `rounds=0 reason=human` が同 head で 3 回 | `stalled` | 原因 (1) 未 attest naysayer / (2) proposer→implementer 指名 を機械判定して facts に書く | |

**遷移の優先順位**（同 tick に複数成立したとき）: R-HOLD > R-QUAR > R-MERGED > R-GATE-* > R-PUSH-GATE > R-NEXT-* > R-SILENT > R-STARVE。

### 5.2A `gate_admission` — pre-gate CI-wait admission【v0.3.1】

`NEXT: pr-review <ref>` を検出したとき、gate を「今このタイミングで呼んでよいか。呼べないなら誰に渡すか」を決める純関数。設計 v0.3.1 §A-1..§A-5 / §B-1..§B-3。現行 conductor と将来の operator tick の両方から同じ関数を呼ぶ（`routing.guard_proposer_to_implementer` と同じく単一 SOT）。実装: `src/spirrow_mindwire/gate_admission.py`。

#### 5.2A.1 不変条件

> **INV-CI-1** — naysayer モデルは `(head_sha, ci_conclusion)` の組につき高々 1 回しか呼ばれない**（self-nomination 経路上で）**。pending の観測はモデル呼び出しを伴わない（DEFER 経路）。R6（`ALREADY_REVIEWED`）が同一 head の同一 conclusion での再判定を dedupe する。**Manual override（R0-OVERRIDE、v0.3.2）は separate に数える** — operator や role が明示的に再指名した場合は「無駄」ではなく「意思を持った再要求」であり、常に許可する。naysayer の L1 CI-gate（`pr_review.py:1613`）が非 SUCCESS CI を model call 抜きに吸収するので、R0-OVERRIDE 経由の manual invoke が pending / red CI に着いても model round は焼かれない。

> **INV-CI-2（改）** — `gate_admission` の入力に `verdict` は存在しない。∴ verdict の内容を読むことが**構造的に不可能**であり、carve-out ② をこの関数が所有することはあり得ない。`verdict_heads` は「その head に verdict が**在るか**」という admission の事実のみで、内容ではない。強制手段は grep でも AST でもなく**入力の不在**そのもの。署名テスト（`tests/test_gate_admission.py::test_inv_ci_2_gate_admission_signature_has_no_verdict_input`）は、その不在が事故で埋められないための早期警告として置く — 担保しているのは署名そのものであってテストではない（#222 で narrow した書き方を踏襲）。

#### 5.2A.2 完了判定と待ち時計の分離（§A-1）

**完了判定は timestamp を一切読まない**。

- `concluded = rollup 非空 ∧ 全 check の status が "completed"`
- `red = concluded ∧ ∃ conclusion ∈ {failure, timed_out, cancelled, action_required, startup_failure}`

`started_at` / `created_at` は完了判定に登場しない。∴ 「CI が終わったか」は queued の null 有無と無関係に決まる。

#### 5.2A.3 待ち時計 `ci_clock_start(rollup, head_committed_date)` を全域関数化（§A-2）

```
ts(c)          = c.started_at ?? c.created_at ?? None        # StatusContext は created_at しか持たない
observed       = { ts(c) for c in rollup if ts(c) is not None }
ci_clock_start = min(observed)            if observed ≠ ∅   → cap = CAP_CHECK   (6h)
                 head_commit.committed_date if observed = ∅  → cap = CAP_NOCLOCK (12h)
```

`head_commit.committed_date` を最後の拠り所にした理由:

- **常に存在する**（head sha があれば必ず引ける）。∴ 全域。
- **head 束縛**。head が動けば時計も入れ替わるので、statelessness（head_skip が自動的に成り立つ性質）が fallback 経路でも壊れない。
- **必ず真の開始時刻より早い**。∴ CAP は早く鳴ることはあっても遅れて鳴ることはない。誤りの向きが「人に早く渡す」側に固定される。

fallback 側を 12h にしたのは、その早鳴りが実害になる唯一のケース（数日前に author した commit を今 push した）を実用上潰すため。それでも鳴ったら、**escalation message に必ず「どちらの時計を使ったか」（`clock=check` / `clock=commit`）を書く**ので、人は 1 行読んで「commit が古かっただけ」と判別できる。黙って早鳴りしない。

`min(observed)` を選んだ理由: 途中で required check が増えれば時計は単調に早い側へ寄るだけ。∴ CAP が遅く鳴ることはない。

#### 5.2A.4 admission 表（§A-3、msg-2568 で crash 経路を除去、v0.3.2 で PR-review msg-(gate) の 2 件を受け入れ）

| # | 条件 | admission | 行き先 |
|---|---|---|---|
| **R0-OVERRIDE** | `not nomination_is_self`（operator / role の手動 handoff） | `INVOKE` | — 【v0.3.2、msg-(gate) BLOCKING-2】 |
| **R1a** | rollup 空 ∧ `now − head_committed_date ≤ CAP_EMPTY_RACE` | `DEFER` | 自己指名、**モデル呼び出しなし**【v0.3.2、msg-(gate) BLOCKING-1】 |
| **R1b** | rollup 空 ∧ CAP_EMPTY_RACE 超過（steady-state「CI 未設定」） | `INVOKE` | — 【v0.3.2】 |
| R2 | `not concluded` ∧ `now − ci_clock_start ≤ cap` | `DEFER` | `NEXT: pr-review <ref>` 自己指名、**モデル呼び出しなし** |
| R3 | `not concluded` ∧ CAP 超過 | `ROUTE_HUMAN` | 「CI stuck: `<check 名/status>`、時計 = `check`\|`commit`、起点 `<t>`」 |
| R4 | `red` ∧ head ∉ `ci_red_routed_heads` | `ROUTE_IMPLEMENTER` | ci-route マーカ付きで implementer 起動（E-CI-RED） |
| R5 | `red` ∧ head ∈ `ci_red_routed_heads`（新規 push なしで 2 度目） | `ROUTE_HUMAN` | 「同一 head で CI 赤 2 回、新規 push 無し」 |
| R6 | `concluded ∧ ¬red` ∧ head ∈ `verdict_heads` | `ALREADY_REVIEWED` | 既存 verdict の `NEXT:` に従う（撃ち直さない）— `and nomination_is_self` は v0.3.2 で削除（R0-OVERRIDE に吸収され unreachable） |
| R7 | `concluded ∧ ¬red`（それ以外） | `INVOKE` | — |

- **CAP_CHECK = 6h**（`min(started_at)` 起点）: #222 の最長観測 2.5h（msg-2562）に headroom を足したサイズ。CAP に当たること自体が異常の信号なので、人に渡すのは正しい。
- **CAP_NOCLOCK = 12h**（commit clock 起点）: fallback 側は真の開始時刻より早い分だけ余裕が必要。
- **CAP_EMPTY_RACE = 5min**（commit clock 起点、v0.3.2 追加）: GitHub Actions の CheckSuite は push 後に一瞬空を返す（数秒〜稀に分単位）ため、空 rollup を即座に「CI 未設定」と決めつけると naysayer の L1 CI-gate が fail-close→implementer→fix loop の pathological な循環に入る。CAP_EMPTY_RACE 以内は DEFER で CheckSuite の populate を待ち、以後は R1b で「genuinely 未設定」と判定する。
- **R0-OVERRIDE**（v0.3.2 追加）は msg-2566 §A-3 R6 note の「operator の手動 `NEXT: pr-review` は常に override として通る」を R6 単独から**全 admission 状態**に一般化したもの。R3（stuck CI）や R5（loop-safety escalation）で summon された operator が再指名しても escalation が re-fire する trap（msg-(gate) BLOCKING-2）を構造的に消す。naysayer の L1 CI-gate（`pr_review.py:1613`）は非 SUCCESS CI で model call 抜きに COMMENT へ短絡するので、R0-OVERRIDE 経由の manual invoke は pending / red CI 上でも model round を焼かない。
- **R6 は self-nomination 経路のみ到達可能**（v0.3.2 で `and nomination_is_self` を落とした）。R0-OVERRIDE が non-self handoff を先に食うため、R6 に到達するのは conductor 自身の DEFER wake-up のみ。振る舞いは不変、rule label が実体を反映するようになった。

#### 5.2A.5 唯一の新規レコード — ci-route マーカ（§A-5）

R5 のループ安全のために `ci_red_routed_heads` が要る。これだけは ledger から derive できない（deferral は message を書かないため）。∴ **R4 で implementer に渡すときだけ** conductor が機械可読マーカ 1 行を relay message に付す:

```
<!-- mindwire:ci-route v1 {"head":"<sha>","conclusion":"failure","checks":["gate"]} -->
```

deferral（頻出）には書かず、CI 赤 routing（稀）にだけ書く。∴ スレッドノイズはほぼゼロ、状態ファイルもゼロ。v0.3.1 で唯一「記録を増やす」判断。

#### 5.2A.6 3 つの辺の対照表（§B-3、concept drift の再発防止）

`gate_admission` は「gate を今呼ぶか」だけを決め、gate の verdict をどう扱うかは決めない。関数がそれぞれ担当する辺を明示する:

| 辺 | 決めるもの | 入力 | 実装 |
|---|---|---|---|
| proposer→implementer | guard (i) | 4 bool（human / naysayer / RUN / attested） | `guard_proposer_to_implementer`（#222 で landing） |
| **gate verdict → 次役**（carve-out ②） | verdict の内容 | verdict | 既存経路。新規にモデル化しない（msg-2546 / msg-2564 の判断を維持） |
| **CI 結論 → gate を呼ぶ / 待つ / implementer**（**E-CI-RED**）| 機械的事実 | rollup + head + 時計 | `gate_admission`（v0.3.1 新規） |

3 行目は guard (i) の carve-out でも carve-out ② でもない**新しい辺**であり、conductor が機械的事実だけで決める（役の判断が入らない）。

#### 5.2A.7 期待効果（#222 で再生した場合）

| | 現行 | v0.3.1 |
|---|---|---|
| gate の CI-gate 短絡呼び出し（`pr_review.py:1613`） | 6 回（うち pending 3） | 3 回（pending 0、自己指名 backoff で吸収） |
| pr-gate-relay の COMMENT 相当ノイズ | 3 回 | 0 回 |
| 人の停止（gate ↔ 人） | 3 回（全部「聞くまでもない問い」） | 0 回 |
| APPROVE 後の人の停止（merge = Tier-C） | 1 回 | 1 回（維持） |

（msg-2568 §A-6 の元表は naysayer モデル呼び出し数を数えていたが、現行 `pr_review.py:1613` の L1 CI-gate 短絡は既に model 呼び出しを塞いでいる。減るのは pr-gate-relay の CI-gate 経路そのものと、対応する COMMENT 中継ノイズと、人の停止 — msg-2568 の意図はそのまま生きているが、数える単位を model call から gate invocation に置き換えた。実装 PR #224 で proposer に flag 済み。）

### 5.3 `waiting_on` の形

```json
{"kind":"msg_from","thread":"T-sweep-intake-and-quarantine-stalls","author":"Einstein","after_msg":"msg-2529",
 "then":{"column":"backlog","action":"sweep_register","args":{"threads":["T-a","T-b"]}}}
{"kind":"pr_merged","repo":"SpirrowGames/spirrow-mindwire","pr":214,"then":{"column":"proposing"}}
{"kind":"control_run","then":{"column":"backlog"}}
{"kind":"time","at":"2026-09-06T00:00:00Z","then":{"column":"proposing"}}
{"kind":"resource","resource":"repo:SpirrowGames/spirrow-conclair","holder":"sg-tomtebo-01","since":"2026-09-05T08:00:00Z","then":{"column":"implementing"}}
```
`kind:"resource"` は lease が取れなかった候補を**不可視にしないため**のもの（v0.2 C-1）。R-SILENT の検出対象に入る。
**operator が「〜したら X します」と言う代わりに、この JSON を書く。** tick は毎回全 `waiting` カードの `kind` を評価する。これが D8 の実体。

---

## 6. Tick アルゴリズム（`spirrow_mindwire/operator/`）

### 6.1 モジュール構成

```
src/spirrow_mindwire/operator/
  __init__.py
  config.py        # operator.toml（§10）
  observe.py       # chatroom / gh / git / local → Observation
  reconcile.py     # Observation × cards → 遷移候補（純関数、テスト対象の中心）
  rules.py         # §5.2 の遷移表（データ + 述語）
  judgments.py     # Lexora light 呼び出し・プロンプト・ログ push
  act.py           # conductor 起動 / gate 発火 / sweep 相当 / post-merge 手順
  leases.py        # リース API クライアント（magickit MCP 経由）
  push.py          # cards / events / nodes / judgments の送信
  tick.py          # 1 tick のオーケストレーション、CLI `mindwire-operator-tick`
  profiles/        # repo プロファイル（mindwire.py / voxelworld.py …）: merge 権限、post-merge 手順
```

### 6.2 1 tick（node ごと、Task Scheduler 5 分。将来は常駐）

```
def tick(cfg, node):
    hb = heartbeat_start(node)
    for project in cfg.projects_for(node):
        control = conclair.get_control(project)              # 期限切れ hold は run で返る
        cards   = conclair.list_cards(project)
        obs     = observe(project, cards, cfg)               # 1) chatroom: active threads + 各 last msg + NEXT
                                                             # 2) gh: open PRs (head, ci via actions/runs, reviews per commit_id, merged)
                                                             # 3) git: ls-remote refs/pull/*/head（gh と 2 経路一致）
                                                             # 4) local: in_flight プロセス、前 tick の conductor 結果
        plan    = reconcile(cards, obs, control, rules)      # 純関数。[(card, rule_id, to, needs_judgment)]
        for step in order_by_priority(plan):
            if step.needs_judgment:
                j = judgments.ask(step)                      # light。失敗時は保守側（human 寄り）に倒す
                step = apply_judgment(step, j)
            if step.requires_lease:
                keys = sorted(step.resource_keys)            # thread:<id> (+ repo:<name> if implementing)
                if not leases.acquire_all(project, keys, node, kind, ttl):   # all-or-nothing → 取れなければ waiting(resource)
                    mark_waiting_resource(step); continue
                if not parallel_allowed(step, obs, cfg):     # §7（cross-repo の関連、depth-1）
                    leases.release_all(project, keys, node); continue
            if not preconditions_ok(step):                   # §6.3 環境事前条件 → 満たさなければ stalled カード
                leases.release_all(project, keys, node); mark_stalled(step, reason=...); continue
            result = act(step)                               # conductor 起動は非同期（in_flight に登録）
            conclair.transition(project, step.thread, step.to, reason=step.rule_id, actor=node, judgment_id=j.id, details=result)
        conclair.upsert_cards(project, refresh_facts(cards, obs))   # 遷移が無くても facts / last_motion_at は更新
    heartbeat_end(node, hb)
```

冪等性: 全ての行為は「観測 → 条件 → 行為」で、行為の前に必ず現物を取り直す（skill の「発火前に rollup を取り直す」をコード化）。gate 発火は `gate_head_sha == head_sha` なら二重発火しない。conductor 起動はリースで一意。

### 6.3 conductor の起動

現状 `run_conductor(settings)` は TOML からしか thread を取れない。**変更**: `run_conductor(settings, *, project=None, thread_id=None, repo_dir=None)` を追加し、指定があれば `settings.loop.project / repo_dir` と `settings.conductor.task_thread_id` を上書きした複製を作って走らせる（`Set-TomlValue` による書き換えを廃止）。戻り値 `ConductorOutcome` はそのまま使い、`StopReason` を遷移表に流す（`human` → R-HUMAN、`none` → R-NONE-*、`no_handoff_to_human` / `no_progress_to_human` / `round_cap` → J-STALL 経由で stalled か ready_for_human、`hold` → waiting、`empty_thread` → stalled）。

conductor 自体（`conductor/core.py`）は変えない。役セッションの spawn、guard、PR-gate 発火の内部実装はそのまま。**guard (i) は `src/spirrow_mindwire/routing.py::guard_proposer_to_implementer` に抽出済み（PR #222）**で、conductor とボードは同じ述語を呼ぶ。定義箇所が 1 つであることは AST 走査のテストで検査する。**抽出が landing するまでボードの routing は live にしない**【v0.2 C-3】。

**環境事前条件（act の前、v0.2 C-2）**: `implementing` の act に入る前に tick は次を検査し、満たさなければ**進めずに `stalled` カードを理由付きで立てる**: worktree clean ／ `.git/index.lock` 無し ／ base が origin に追随済み ／ lease 取得済み。壊れた worktree の上を走って途中で死ぬ経路を、静かな停止ではなく可視カードに変換する。破壊的操作（`clean -fdx` 系）は無条件に移植しない。lease を取り直した直後の worktree に限定し、人が見ていない時に未 push の作業を消す経路を作らない。

### 6.4 PowerShell wrapper からの引き継ぎ対応表

| wrapper の機能 | 引き継ぎ先 |
|---|---|
| sweep.json 読み・順序 | `backlog` 列 + `facts.priority`。**sweep.json は移行期間だけ両方読み、P2 で廃止** |
| head_skip_decide（launch/defer/skip、backoff） | `reconcile.py` の R-NEXT-* 前段。backoff は `board_cards.facts.backoff` に持つ。`head_skip.py` の `Record` はそのまま流用 |
| evaluated.json（starvation） | R-STARVE。`board_cards.updated_at` と `board_events` から計算、ファイル廃止 |
| quarantine.json / Clear-Quarantine.ps1 | `stalled` 列 + `facts.failure`。解除は UI の「再投入」（human decide を伴う）か tick の J-STALL 外部障害判定による自動再投入 |
| notified.json（Discord 抑止） | `board_events` の直近同 reason を見て抑止。ファイル廃止 |
| pending-decisions.json + composer | composer はそのまま使う（`mindwire-compose-decision`）。material の PUT 先は現行 magickit `decisions.py` のまま。カードの `decision_ref` に signature |
| leases.json / Lease.ps1 | `board_leases` |
| digest.json / 日次ダイジェスト | 当面 wrapper に残す（依存が薄い）。P3 でボードから生成 |
| gate_bootstrap_tick | 当面 wrapper に残す |
| git 環境の準備・復旧（stale lock、base 追随、untracked 掃除、状態復旧） | **port**（§6.3 の環境事前条件 + 限定的な復旧）。ただし対応する wrapper 関数の確定は inventory gate で機械検証する |

**inventory gate（v0.2 C-2、CI 必須）**: 上の表は手で列挙したもので「列挙漏れが無い」を主張では担保できない。∴ `run-conductor-scheduled.ps1` の関数を列挙し、本表に `port` / `drop(理由)` / `keep-in-wrapper` の owner が付いていない関数が 1 つでもあれば CI を落とすテストを足す（`EPHEMERAL-DEVELOP-PROCEDURE-V1` の sentinel count と同じ手）。P2 のカットオーバー条件に「inventory gate が緑 ＋ 環境事前条件 assert を含む 1 周を人が見ている場で観測」を追加。P2 で止めるのは wrapper の**候補ループ**であって wrapper 全体ではなく、scheduled 実行側の git 準備は P2 時点では残る。

---

## 7. リースと並列規則（D3 / D4）

### 7.1 用語

- **関連集合** `Rel(t)` = `thread_relations` で t から**深さ 1** で到達するスレッド集合 ∪ {t} ∪ 同一 repo の同一 PR チェーン（v0.2 C-4）。
- **占有** `Occ(node)` = node が `turn` または `implementing` リースを持つスレッド集合。

### 7.2 規則

1. **同一スレッドのリースは常に 1 node**（`board_leases` PK）。分割禁止はこれで機械的に成立する。
2. **implementing には thread lease と repo lease の両方**【v0.2 C-1】。repo lease は fleet-wide に 1 枚なので、同一 repo の implementing は node を跨いでも同時 1 枚。排他すべきは filesystem（node ごとに worktree は別）ではなく **push 先**（同一 base への PR base drift、develop 進行の競合、self-merge の連続失敗）であり、repo lease はそれを鍵にする。当日 #209 と #210 の 4 ファイル衝突は 1 node でも起きた。
3. **関連グラフによる排他は cross-repo 対にだけ効く**: node A が t を取ろうとするとき、他 node の `Occ` に `Rel(t)`（**depth-1**）と交わるスレッドがあれば拒否。判定は tick 内で `GET /related` を呼び、リース取得の直後にもう一度確認する（取得→確認の 2 段で TOCTOU を潰す。厳密な直列化が要るなら Conclair 側で `SELECT ... FOR UPDATE`、tick 間隔 5 分で衝突確率は低いので P4 で判断）。
4. **proposing / gate は関連に関わらず並列可**（読みと裁定なので衝突しない）。
5. リース TTL: `turn` 30 分、`implementing` 90 分。conductor が走っている間は 10 分ごとに renew。期限切れは他 node が奪取可能（R-SILENT の再起動経路と同じ）。
6. **関連が未登録なら「並列可」に倒す。ただしこの既定が効くのは repo が異なるスレッド対だけ**（同 repo は規則 2 の repo lease で潰れる）【v0.2 C-1】。P0/P1 の初期並列は conclair / mindwire / magickit の 3 repo 3 スレッドで全部 cross-repo なので、「02 が何も取れない」問題は permissive のまま解ける。J-RELATED は cross-repo で implementing が並ぶときの助言に限定し、`related` なら `thread_relations(kind='operator')` に書いて以後は機械判定にする。
7. **コスト（隠さない）**: 単一 repo プロジェクトでは implementing の cross-node 並列が消える。implementing は分単位なので初期は許容。詰まったときの次の精緻化は「関連グラフを信じる」方向ではなく、`resource_key` を worktree 粒度（`repo:x#worktree:y`）に降ろす方向。

### 7.3 node ↔ プロジェクトの親和性

`operator.toml` の `[[nodes]]` に `projects = ["spirrow-mindwire", ...]` と `prefer = true|false`。別プロダクトを分担するなら重複なしに書く。同じプロダクトを手分けするなら両方に同じ project を書き、規則 2 が効く。

---

## 8. 人の待ち行列（`ready_for_human`）

- カードには composer の material（question / options / recommendation / reason / unknowns）が付く。`presented_hash = sha256(facts の判断に効く部分)`。
- **再提示しない**: `presented_hash` が変わらない限り UI はカードを「変化なし」で表示し、Discord は再通知しない。変わったら「前回提示からの差分」を先頭に出す。
- UI 操作:
  - 選択肢をクリック → magickit `chatroom_writes` で **human 名義の `decide`**（`NEXT: <役>` を本文に素で書く。`next_participant` は送らない — conductor が読まないため）。Tier-C は人のクリックのまま。
  - 「merge した」ボタンは**無い**。R-MERGED が gh で検出する。
  - 「これは operator 権限で進めてよい」ボタン → `judgments/{id}/override` に記録して該当規則で再遷移。**これが J-ESCALATE の教師データになる。**
- 静かに止まる方向の保険: `ready_for_human` に 48h 滞留したカードは Discord に 1 回だけ再通知（内容は同じ、再提示ではなく催促）。

---

## 9. 判断点（Lexora light）

| ID | どこで | 入力 | 出力 | 外したときの被害 | 保守側 |
|---|---|---|---|---|---|
| J-ROUTE | R-GATE-RC / R-NEXT-HEIS-GUARD | facts（RC の objection class 一覧、NEXT、直前の裁定有無） | `proposer` / `implementer` / `human` | 1 ターン無駄 | proposer |
| J-ESCALATE | R-HUMAN | 役の Tier-C 依頼段落 + operator 権限リスト | `human` / `operator_can_do:<action>` | 聞くまでもない問いが届く／必要な判断が届かない | human |
| J-COND | R-NONE-COND | `NEXT: none` 本文 | `waiting_on` JSON か `none` | waiting が発火しない → 48h で R-SILENT が拾う | none（waiting にしない） |
| J-STALL | R-QUAR / no_progress | session_log_tail、直近の遷移 | `external` / `credential` / `thread_specific` | 再投入の要否を誤る | thread_specific（人へ） |
| J-RELATED | §7.2-6 | 2 スレッドの title + 冒頭 + 触るパス | `related` / `unrelated` | 衝突 PR が 1 本増える | related |

共通契約: system prompt は遷移表の抜粋 + **operator 権限リスト（肯定形）** + `wait` 制約（「必要な役の slot が塞がり NEXT: が明示されているときだけ」）。出力は JSON 1 行。temperature 0。`prompt_version` を判断ログに残す。**リプレイ評価（`operator_eval.py`）を CI に入れ、`board_judgments` から週次で正解候補を吐き、人が override した行を正解として蓄積する。**

権限リスト（コードで持つ。プロンプトにも同文を入れる）: gate の発火・再発火／sweep 掲載済みスレッドへのターン起こし／Bohr にスレッドを立てさせる／計測・調査／PR の close（branch 保持）／gate review の dismiss（理由付き）／backlog・stalled・waiting・control の編集／develop 再作成と base 付け替え／daemon 再デプロイ。**役が設定した停止規則は operator の権限を縮めない。**

---

## 10. 設定 `operator.toml`（`<data_dir>/config/`、`mindwire.toml` と並置）

```toml
schema_version = 1

[operator]
node = "sg-tomtebo-01"
magickit_mcp_url = "http://100.79.84.62:8117/mcp"   # Conclair 直叩きはしない（§13.1 決定）。board_* は magickit MCP ツール経由
tick_seconds = 300
lexora_url = "http://100.79.84.62:8110"
judgment_tier = "light"

[[projects]]
project  = "spirrow-mindwire"
repo     = "SpirrowGames/spirrow-mindwire"
repo_dir = "C:/workspace/sandbox/mindwire-impl"
profile  = "mindwire"          # profiles/mindwire.py: main 直行・Tier-C・post-merge = sync-repo
implementing_max = 1

[[projects]]
project  = "spirrow-voxelworld"
repo     = "SpirrowGames/Spirrow-VoxelWorld"
repo_dir = "C:/workspace/sandbox/voxelworld-impl"
profile  = "ephemeral-develop"
```

`mindwire.toml` の `[loop] / [conductor]` は conductor 単体の既定値として残す（operator が上書きして呼ぶ）。

---

## 11. ダッシュボード（magickit web）

- 既存 `board.py`（`/dashboard/decisions`、new/doing/parked）を **`/dashboard/operator` に置き換える**。手動レーン（`board_lanes`）は廃止し、列は Conclair の `board_cards.column` を正とする。`board_seen` は不要になる。
- 画面: 横 = 列（§5.1 の 9 列、`done` は折り畳み）、縦 = プロジェクトのスイムレーン。上部に node ストリップ（`board_nodes.last_tick_at` から 稼働 / 遅延 / オフライン）。`stalled` は赤、`ready_for_human` は黄、`column_since` からの経過を各カードに表示。starvation はレーン見出しに表示。
- カードのドロワー: facts、最終 msg へのリンク（`/ui/projects/{p}/threads/{t}`）、PR リンク、gate verdict と head、`board_events` のタイムライン、判断ログ（J-* の入出力）と override ボタン。
- HTMX 5 秒ポーリング（既存 `_board` パターン）。データ取得は `ChatroomAdapter` に `list_board_cards / list_board_events / list_nodes / transition / override_judgment` を追加。
- ドリルダウン: `stalled` カードの「ログ全文」は Phanthand `smart_read(mode="raw")` で当該 node の `session_log_path` を読む。node がオフラインなら `session_log_tail` を表示。
- `ops.py` の `classify()` は残すが、running/stalled の判定源をボードに切り替える。

---

## 12. 移行計画と受け入れ基準

**P0 は「実装完了に依存」ではなく「契約凍結に依存」で並列化する**【v0.3.1 §B-1】。P1 と magickit は P0-D0（`contracts/board/v1/openapi.yaml` + vectors）の merge sha に着手条件を紐づけ、実装は fake に対して並行で書く。数日空転を消す。

| Phase | 内容 | 受け入れ基準 | wrapper |
|---|---|---|---|
| **P0-D0** | **契約凍結**: `contracts/board/v1/openapi.yaml` + `contracts/board/v1/vectors/*.json`（各エンドポイントの request/response、**エラー shape 含む**）を conclair repo に merge | vectors が全 API を覆う。P1 / magickit の起票文面に本 merge sha が「着手条件」として書かれている | — |
| P0 | Conclair: 0009 migration、`api/board.py`、`thread_relations` 派生書き込み、control TTL。magickit に `board_*` MCP ツール | alembic up/down 往復、既存テストが緑、`GET /related` が affects_threads / references から到達集合を返す | 変更なし |
| P1 | mindwire `operator/`: observe + reconcile + push のみ（**行為なし**）。wrapper と並走。magickit `/dashboard/operator` 読み取り専用（**advisory only — conductor is authoritative** 常時表示、v0.3.1 §B-6 CON-P1-ADVISORY） | 当日の transcript の状況を再現した fixture で、reconcile が 116 判断点のうち決定的な 74 点（continue 系）と同じ列遷移を出す。盤面に 6 プロジェクトが並び、starvation と stalled が見える | 変更なし（tick が state ファイルも読む） |
| P2 | 決定的遷移の引き継ぎ: R-MERGED、R-PUSH-GATE、R-GATE-*、R-NEXT-*（リース付き）、R-HOLD（TTL）、R-QUAR。sweep.json → backlog、quarantine → stalled | 「マージした」と人が言う必要が消える（R-MERGED が 5 分以内に post_merge へ動かす）。wrapper の候補ループを止めても 1 日のスループットが落ちない（PR land 数で比較）。**inventory gate 緑 ＋ 環境事前条件 assert を含む 1 周を人が見ている場で観測** | 候補ループ停止、digest / gate_bootstrap / git 準備は残す |
| P3 | 判断点 J-ROUTE / J-ESCALATE / J-COND / J-STALL を light で。`waiting_on` 稼働。UI の override | 聞くまでもない問い 0 / 週。J-ESCALATE の override 率 < 10%。`operator_eval.py` が CI で粗分類 ≥ 0.85 | digest 移管、wrapper 退役 |
| P4 | tomtebo-02: `[[nodes]]` 追加、§7 の並列規則、J-RELATED | 同一プロダクトで 2 node が implementing を並走し、関連スレッドが同時に取られない（fixture で検証） | — |

**v0.3.1 の landing 順**: 現行 conductor 版 `gate_admission`（§5.2A、mindwire PR #224）→ 3 スレッドの起票（§16）→ P0-D0 → 並列 P0 / P1-with-fake / magickit-with-fake → 統合ターン → P2 カットオーバー。`gate_admission` は P0 より先に landing するので、**A landing 〜 board 稼働までの期間だけ、残った 1 回の人の停止（merge=Tier-C）に集約可視性が無い**期間が発生する（v0.3.1 §C の残余）。設計変更はしない。この期間はスレッドが唯一の可視面。

対話型 operator（Claude Code）は P2 以降「盤面を見て人と話す」役になる。skill の `/mindwire-operator` は P3 で「ボードの読み方」に書き換える（skill 群は人の資産、書き換えは Tier-C）。

---

## 13. 未決事項（設計レビューで決める）

1. **【決定】Conclair へは magickit MCP 経由**（Einstein endorse、msg-2543）。magickit に `board_*` MCP ツールを足す。Conclair の「他サービスを呼ばない leaf」を守る。根拠の出所として **ADR-2026-06-04-18（mindwire デプロイ・トポロジと magickit 到達性）** が挙がったが未読。P0 着手前の read-back に本 ADR を項目として足し、実体があれば引用、無ければ不在を所見として記録する。どちらでも決定は変わらない【v0.2 #7】。**v0.3.1 §D 補足**: ADR-2026-06-04-18 の実体が無い場合、conclair の schema と API 契約はこれに依存しないので **P0 は着手可**。ブロックされるのは magickit スレッドの deployment 節（MCP ツールの契約と UI は影響なし）のみ。topology の決定は新規 ADR が要る Tier-C(scope) として別に切り出す — P0/P1 を人待ちで止めない。CLAUDE.md §M が「ADR-10〜13 は参照名のみで実体未作成」と明記している以上、欠番はあり得る前提で分岐を先に置く。
2. **【決定】`/related` の既定 depth=1、lease 経路は depth-1 固定**（v0.2 C-4）。depth 2 は J-RELATED / UI 限定、P1 末で消費者が無ければ削除。
3. **【決定】guard (i) は単一述語に抽出（PR #222）**。ボードは呼ぶだけ。`T-human-terminal-overuse` の A 案が guard を変えるなら変更は 1 箇所。両側に依存を書く（v0.2 C-3）。
4. **【決定】judgment 失敗時は保守側で進める**（Einstein endorse）。判断ログに `fallback=true`。
5. **メモリの記述との差異**: 手元の記録では Conclair は「SQLite + WAL + FTS5」だが、ソースは PostgreSQL。設計はソースに従う。
6. **【未決】P2 カットオーバー条件の観測手順の具体化**（inventory gate の実装が先）。
7. **【決定・v0.3.1】pre-gate CI-wait は `gate_admission`（§5.2A）で単一 SOT。INV-CI-2 (改) の強制は署名の入力不在**（msg-2568 §B-2、Einstein endorse msg-2569）。carve-out ② と E-CI-RED は別の辺で、`gate_admission` は carve-out ② を所有しない。
8. **【決定・v0.3.1】completion 判定と待ち時計の分離**（msg-2568 §A-1）。`ci_clock_start` は `started_at ?? created_at ?? committed_date` で全域関数化、CAP_NOCLOCK=12h で fallback、escalation 文字列に必ず clock 名を含める（Einstein endorse msg-2569）。

---

## 14. レビュー履歴

- msg-2543 Einstein: C-1 未登録関連の cross-node contention（blocking）、C-2 wrapper 撤去による git 環境の退行（blocking）、C-3 human routing の二重管理、C-4 depth=2 は YAGNI。§13.1 と §13.4 は endorse。
- msg-2544 Bohr: C-1 accept（排他対象は filesystem ではなく push 先。repo lease を fleet-wide 1 枚、辞書順 all-or-nothing）、C-2 accept（inventory gate + 環境事前条件 assert + 破壊的操作の限定 + カットオーバー条件）、C-3 accept（単一述語に抽出）、C-4 partial accept（lease 経路は depth-1）。ADR-2026-06-04-18 は未読のため引用せず read-back 項目に。
- msg-2545 Einstein: 両 blocking 解除。「deadlock は all-or-nothing で閉じ、live-lock は tick 再試行で吸収」を確認。construction 可。
- msg-2546 Heisenberg: v0.2 item 5（guard (i) 抽出）を PR #222 として実装。item 1/2/3/6 は P0 Conclair 依存、item 4/7 は follow-up。
- **msg-2566 Bohr（v0.3）**: A pre-gate CI-wait（R-CI-WAIT 現行 conductor 版）、B 3 スレッドの契約境界（P0-D0 契約凍結で並列化）、C #222 advisory の同梱、D ADR-2026-06-04-18 欠番時の分岐。
- **msg-2567 Einstein**: BLOCKING edge-case（`min(startedAt)` が queued で crash）、BLOCKING correctness（R4 = carve-out ② は factually false）。他 6 点 endorse。
- **msg-2568 Bohr（v0.3.1）**: 両 blocking 全面受け入れ（押し戻し 0）。完了判定と時計を分離、`ci_clock_start` を `committed_date` fallback で全域化、CAP_NOCLOCK=12h、`route_pr_gate_outcome` → `gate_admission` へ改名、INV-CI-2 (改) を「入力の不在による構造的強制」に書き換え、3 辺の対照表と E-CI-RED を新設。
- **msg-2569 Einstein**: 両 blocking 解除、v0.3.1 承認（"code work may proceed"）。時計と status の分離、署名レベル境界強制、E-CI-RED の別辺明示化を endorse。
- **msg-（PR #224）Heisenberg（v0.3.1 実装）**: v0.3.1 §A（`gate_admission` 純関数 + 36 tests）と §C（16 行 truth table）を mindwire PR #224 として実装。ci-route マーカ書込は conductor wiring follow-up に切り出し、本 docs PR で明示。§A-6 期待効果表の「naysayer モデル呼び出し数」については `naysayer/pr_review.py:1613` の L1 CI-gate 短絡が既に model 呼び出しを塞いでいる事実を確認、数える単位を「gate invocation + relay noise + 人の停止」に置き換え（PR #224 記述、本 docs §5.2A.7 反映）。
- **msg-(gate) PR-review naysayer（v0.3.2 blocking 2 件）**: BLOCKING-1（correctness — 空 rollup が GitHub Actions startup latency の race window で INVOKE を返し、naysayer が UNKNOWN 短絡→implementer→fix loop に入る）、BLOCKING-2（edge-case — `nomination_is_self` が R6 でしか consult されず、R3/R5 で summon された operator が re-nomination で escalation loop に閉じ込められる）。The bar is on how the design misreads reality (BLOCKING-1) と how the design leaves the operator's only escape valve inaccessible (BLOCKING-2), 両方とも構造の欠陥で in-body guard では塞げない類。
- **msg-（PR #224 fix）Heisenberg（v0.3.2 受け入れ）**: 押し戻しゼロ。BLOCKING-1 → R1 を R1a（fresh commit + 空 → DEFER）/ R1b（過ぎたら INVOKE、CAP_EMPTY_RACE=5min で判別）に分割。BLOCKING-2 → `nomination_is_self=False` を先頭の R0-OVERRIDE として promote、下流の全 admission 状態を bypass、R6 の redundant guard を削除。§5.2A.4 表に 3 行追加、INV-CI-1 に manual-override carve-out を明記、Heisenberg fix commit で 16 追加 tests。R0-OVERRIDE の precedence は `test_r0_override_wins_over_every_downstream_rule` の 7 行 parametrised meta-test で pin。

## 15. 開発の進め方（2026-09-05 Takahito 承認）

設計スレッドは spirrow-mindwire の chatroom に立て、Einstein の設計レビューを通す。実装は **3 リポジトリで 3 スレッド**（conclair: P0 / mindwire: P1–P3 / magickit: P1 UI）に分け、依存は両側に書く（規約 3）。順序は conclair → mindwire と magickit 並行。P2 の「wrapper の候補ループ停止」は Tier-C。

ループに載せる根拠: P0 と P1 は仕様が閉じていて、当日の自律レーンが #218〜#221 を完走した粒度に近い。載せない根拠: 3 repo 跨ぎの依存と、ループ自身の実行部を作り替える再帰性。→ **P0/P1/UI はループ、P2 のカットオーバーだけ operator lane（人が見ている場で）**。設計書の正本は本ファイル（repo）と Prismind、chatroom `spirrow-mindwire/T-operator-board` が設計レビューと裁定の SOT。

---

## 16. 3 スレッドの契約境界と起票文面【v0.3.1 §B】

### 16.1 契約 vector の SOT と vendoring 機械チェック（§B-2）

vector の SOT は **conclair repo 一箇所**。他 2 repo は vendor し、`contracts/board/v1/SOURCE` に取得元 commit sha を記録、**vendor 済みファイルの hash が記録 sha のものと一致しなければ CI が落ちる**テストを置く（CLAUDE.md の `EPHEMERAL-DEVELOP-PROCEDURE-V1` sentinel と同じ「SOT 1 + コピーに機械検査」の型）。コピーを黙って腐らせない。

### 16.2 3 スレッドの起票文面に必ず入れる 4 行（§B-3）

どのスレッドも、これが無いと D7（静かに止まる）を自分で踏む:

1. **着手条件**（例: 「P0-D0 が merge されていること」）
2. **環境事前条件**（repo_dir の実在パス、CI の有無）
3. **依存の裏側**（自分が止まると誰が止まるか）
4. **fail-loud 規約**: 着手条件・環境条件が満たされないと分かった時点で、黙って idle にせず `parked` + 理由を 1 本書く

### 16.3 conclair `repo_dir` 未確認への設計制約（§B-4）

> **CON-P0-ENV**: repo_dir が無い状態の P0 スレッドを sweep に載せてはならない。載せるなら第 1 ターンは環境 probe で、結果（有/無）を必ず message にする。

「掲載したが動かない」は D7 が最優先で消すと決めた故障そのもの。∴ probe → 無ければ `parked`（理由付き）→ Takahito の clone 後に `backlog` へ、が正しい順。probe 前に載せるのは不可。

### 16.4 各スレッドのスコープ（§12 / §15 の確定、§B-5）

| thread | repo | 内容 | 着手条件 |
|---|---|---|---|
| `T-operator-board-p0` | conclair | **D0 契約凍結** → alembic 0009（board_cards / board_events / board_leases(project, resource_key) / board_nodes / board_judgments / thread_relations）→ `api/board.py` → relations 派生書き込み（`/related` は **default depth=1**、v0.2 item 6）→ `project_control.desired_expires_at` | CON-P0-ENV の probe 成功 |
| `T-operator-board-mcp-and-ui` | magickit | `board_*` MCP ツール（**Conclair 直叩きはしない**、§13.1 確定）／`/dashboard/operator` 読み取り専用、既存 `board.py` 置換、`presented_hash` 再提示抑止 | P0-D0 |
| `T-operator-board-p1` | mindwire | `operator/` observe + reconcile（純関数）+ push、**act 無し**／`run_conductor(settings, project=, thread_id=, repo_dir=)` と `Set-TomlValue` 廃止／**inventory gate**（v0.2 item 4）／`gate_admission` を `HandoffKind.PR_REVIEW` の前に呼ぶ conductor wiring（本 v0.3.1 §5.2A の実装 follow-up。fetch_check_rollup の追加、verdict_heads / ci_red_routed_heads の thread からの derive、ci-route マーカ書込を含む） | P0-D0 |

**inventory gate は proposer（Bohr）の仕事として P1 スレッドの初回ターンで着手する。** §6.4 が in-repo で読めるようになったので msg-2546 の deferral 理由は消えた。受け入れ条件:

> **ACC-INV-1**: `run-conductor-scheduled.ps1` の全関数が分類表に**ちょうど 1 回**現れ、owner が `port` / `drop` / `keep-in-wrapper` のいずれかである。`unclassified` が 1 件でも残る間は P2 カットオーバーを実行しない。

### 16.5 P1 期間中の二重権威（§B-6、CON-P1-ADVISORY）

P1 は push-only なので board は行為しないが、**board を人が読んで動くと conductor と board が二重に「次の役」を決める**期間が生まれる。∴:

> **CON-P1-ADVISORY**: P1 の `/dashboard/operator` は「advisory only — conductor is authoritative」を常時表示し、カードの `next` は派生値であることを明示する。P2 カットオーバーまで board 由来の指示で人が動く導線（ボタン・コピー可能なコマンド）を置かない。
