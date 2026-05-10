# Spirrow MindWire Feature 2 設計仕様 (Robustness)

> Feature 1 (Phase 0 happy-path watcher + claude-code session) 完成後の robustness 拡張 — lifecycle state machine、 timeout / retry、 crash recovery、 termination semantics の設計を確定する。 Phase 0 base commit `17b4908` (PR #16 squash merge to main) を起点とする。

設計議論は ChatRoom 5 thread (`T-feat2-design-overview` + `T-feat2-d3a1-awaiting-from` / `T-feat2-d3a2-schema-version` / `T-feat2-d3b1-transition-table` / `T-feat2-d3b2-terminated-fields`) で 3 view (claude-code / claude.ai / claude.ai-naysayer) を経て決着。 本書は **decide 8 件 (msg 9 件: msg-028, msg-030, msg-053〜057, msg-059, msg-060)** の集約 SOT。

## 1. Base 方針 (Decide #1)

Feature 2 の全体方針として **Naysayer 寄り (縮小派)** を採用。 当初提案の 6 sub-PR plan に対し、 YAGNI / overscope / ハイブリッド複雑性の観点で縮小した結果。

### 1.1 採用 base 方針

| 項目 | 採用 |
|---|---|
| sub-PR 数 | **4** (元 6 から縮小): orphan-cleanup / timeout / retry / terminate |
| state 数 | **5** (`active / retrying / terminated / resolved / archived`) |
| dead letter ↔ rejection | **統合** (1 terminal `terminated` + `terminated_reason` field で区別、 命名は §3.4 / Decide #3b-2 で確定) |
| state 表現方法 | **`meta.yaml.status` field 単独** (directory rename しない) |
| graceful shutdown (元 sub-PR e) | **削除**、 即落とし運用、 crash recovery で吸収 |
| f 後半 (thread_locks cleanup) | **削除**、 観測後別 issue 化 |
| failure-injection test infra (新規論点 10) | **却下**、 各 sub-PR 内で local 整備 |
| observability strategy (新規論点 11) | **却下**、 各 sub-PR 内で `logger.info` で足りる |
| token budget (残課題 iv) | **却下**、 観測後別 phase |

### 1.2 採用された (D) 4 盲点

PR #1〜#16 review 経験から claude.ai が指摘し、 全 view 一致で組込決定:

- **D-1** `Participant` Literal の 2-party 前提 → test fixture 設計時に意識 (→ §5.1 sub-PR 全般)
- **D-2** EventLogWriter concurrent writers が Feature 2 で増 (timeout/retry/shutdown handler) → per-thread lock hold 時間考慮 (→ §5.1 sub-PR 3)
- **D-3** (高 priority) `next_seq` retry 挙動 = **commit semantics** (write_reply は rollback せず commit、 next_seq は最大 seq+1 で deterministic 計算) → §5.1 sub-PR 3 (retry) で decide
- **D-4** (高 priority) claude-code SDK subprocess の zombie / cleanup 責任分界 → §5.1 sub-PR 2 (timeout) で decide

### 1.3 Monument hybrid

議論結果の monument は 4 layer 分担 (3 view 一致):

| Layer | 対象 | 役割 |
|---|---|---|
| `docs/feature-2-design.md` (本書) | SOT | 全 decide 結果 |
| GitHub Issue | tracker | 各 sub-PR 進捗、 各論点が 「どの PR で解決」 された trace |
| ChatRoom thread | process | 議論 trail |
| 各 sub-PR description | per-PR context | 該当する decide への link |

## 2. Crash Recovery Semantics (Decide #2)

`(b) State-based recovery (status field-driven)` を採用。 startup 時に thread directory を scan し、 `meta.yaml.status` に基づき復帰挙動を分岐 (`status` field の定義は §3 参照)。

### 2.1 復帰ロジック

| status | 復帰挙動 |
|---|---|
| `active` | re-queue、 観測者ループに戻す |
| `retrying` | re-queue、 retry counter は `meta.yaml.retry_count` から維持 |
| `terminated` | skip (operator manual で復帰させたければ status 書換、 §3.6) |
| `resolved` | skip |
| `archived` | skip |

「invoke 中だった thread」 の検出 (heartbeat / pid file) は **入れない**。 idempotency (D-3 commit semantics: `next_seq` deterministic) で safety net。

### 2.2 `retry_count` の永続化

`meta.yaml.retry_count: int = 0` field を採用。 各 retry 開始時に rewrite (= meta.yaml の atomic_write_text 1 回追加)。 operator が `meta.yaml` を見るだけで現状 retry 回数が判明する visibility を優先。

`events.jsonl` 再計算は不採用 (計算 logic + load コスト増、 operator visibility 低下)。

### 2.3 `.tmp` orphan cleanup の age threshold

startup 時の即時削除は別 process の write_reply 中 (= staging 中の `.tmp`) を巻き込む risk があるため、 一定時間経過したもののみ削除。 config 値: `orphan_tmp_cleanup_age_seconds=300.0` (= `idle_timeout_seconds` と同 orderfo の **暫定値**、 staging 中の write_reply 完了想定時間 (本来は秒〜十数秒) とは異なる semantic、 dogfooding 後 FI-3 で再 audit、 §4 参照)。

### 2.4 Test 戦略

**重厚寄り**。 integration test で以下のシナリオを実機再現:

- `.tmp` cleanup の age threshold 動作 (新しい `.tmp` は残る、 古い `.tmp` は削除)
- `retrying` thread の crash 後 startup → retry counter 維持の確認
- `active` thread の invoke 中 kill → 再起動 → next_seq deterministic で二重応答しないことの確認 (D-3 と integration)
- `terminated` / `resolved` / `archived` thread が startup 時に skip される確認

D-3 commit semantics の単体 test で済ませず、 crash recovery 全体パスを e2e で検証。 sub-PR 1 / 3 で実装。

## 3. Lifecycle State Machine (Decide #3)

5 状態 (`active / retrying / terminated / resolved / archived`) の表現、 transition 表、 atomicity、 enforcement を確定。

> **Namespace disclaimer**: 本 §の `ThreadStatus` は MindWire watcher の filesystem-level thread 状態を指す。 ChatRoom (chatroom-magickit) の thread state (`active / awaiting_reply / resolved / superseded / parked`) とは **別 namespace**。 `active` / `resolved` の名前重複は意図的でない (混同しないこと)。

### 3.1 Status enum と `awaiting_from` field (Decide #3a-1)

既存 `awaiting-cc` / `awaiting-cai` を ThreadStatus enum から削除し、 「誰の応答待ちか」 は **直交 field** で表現。

```python
# src/spirrow_mindwire/schema/_common.py (Feature 2 後)
ThreadStatus = Literal["active", "retrying", "terminated", "resolved", "archived"]

class ThreadMeta(StrictModel):
    status: ThreadStatus
    awaiting_from: Participant | None = None  # 次に応答すべき participant (None: terminal state)
```

**`awaiting_from` の semantic**: 「次に応答すべき participant」 (「現在 invoke 中」 ではない、 watcher in-memory state とは責任分離)。

**`awaiting_from` の初期値**: 新 thread 開設時 (`new/` から `threads/<ULID>/` への移動 / propose msg 書込時)、 **最初に thread 開設した participant の opposite** (= 次に応答すべき相手) を設定。 例: claude.ai が initial msg を `new/` に置いて開設すれば `awaiting_from="claude-code"`、 逆もまた然り。

```
status="active", awaiting_from="claude-code"
  → 次に claude-code が応答すべき thread

status="retrying", awaiting_from="claude-code"
  → 次に claude-code が応答すべきだが、 直近の invoke 試行は失敗、 watcher が retry 中

status="terminated", awaiting_from=None
  → 誰の応答も待たない (terminal)

status="active", awaiting_from="claude-code" (invoke 開始前 / event 検出待ち)
  → §3.1 表現上は 1 つ目と同じ、 区別は §6 FI-1 で別途検討 (Phase 0 では区別不要、
    watcher in-memory state + per-thread lock 状態で trace)
```

**multi-participant 拡張** (Phase 1+): `Participant` Literal 拡張で対応、 別 enum 設置せず。

### 3.2 schema_version 据え置き (Decide #3a-2)

`schema_version: Literal[1]` のまま、 enum 変更 (`awaiting-cc`/`awaiting-cai` 削除 + `retrying`/`terminated` 追加) と field 追加 (`awaiting_from` / `retry_count` / `terminated_reason` / `terminated_at`) を **同 version で実施**。

理由: Phase 0 段階で production thread ゼロ、 schema bump コスト > 価値。 Phase 1 MCP write API 移行が初の real migration trigger (external consumer 出現で breaking 制約発生)、 そこで bump + migration infrastructure を整備。

**semantic 固定**: 「on-disk schema の互換性」 のみを表現。 「lifecycle state machine の version」 は別概念。

**namespace docstring 補強** (Naysayer §3 → Claude.ai 採用):

```python
SCHEMA_VERSION = 1
"""Schema version for ThreadMeta on-disk YAML format.

NOTE: This version is INDEPENDENT from event log schema version
(_BaseEvent.schema_version). The numeric value happening to be 1 in
both cases is coincidental; bumping one does not require bumping the
other. See architecture.md §3 for the snapshot vs audit log boundary.
"""

class _BaseEvent(StrictModel):
    schema_version: Literal[1]
    """Schema version for individual event log entries.

    Independent from ThreadMeta.schema_version. See SCHEMA_VERSION
    docstring in this module.
    """
```

**git bisect mitigation**: schema 変更を跨ぐ git bisect で古い test fixture (`status: "awaiting-cc"` 等) が新 schema で ValidationError 起こす risk → schema 変更 commit に test fixture 全再生成を pair commit。 sub-PR 1 で生成する fixture は **Feature 2 後の最終 schema** を想定 (§5.1 sub-PR 1 参照、 sub-PR 2〜4 は schema additive 変更を伴わない前提)。

### 3.3 Transition 表 (Decide #3b-1)

5 状態間の遷移は **8 transition** に限定:

```
active ─→ retrying          (invoke 失敗 = transient error)
active ─→ terminated        (validation failed without retry)
active ─→ resolved          (operator manual)
retrying ─→ active          (retry 成功)
retrying ─→ terminated      (retry exhausted)
terminated ─→ resolved      (operator manual = re-investigate)
terminated ─→ archived      (operator manual)
resolved ─→ archived        (operator manual)
```

| transition | trigger | reason field 設定 |
|---|---|---|
| active → retrying | invoke 失敗 (transient) | — |
| active → terminated | validation 失敗 | `terminated_reason="validation-failed"` |
| active → resolved | **operator manual (Phase 0)、 claude.ai resolve は Phase 1+ MCP write API 経由** | — |
| retrying → active | retry 成功 | — |
| retrying → terminated | retry 上限突破 | `terminated_reason="retry-exhausted"` |
| terminated → resolved | **operator manual (Phase 0)、 claude.ai resolve は Phase 1+ MCP write API 経由** | — |
| terminated → archived | **operator manual (Phase 0)、 claude.ai resolve は Phase 1+ MCP write API 経由** | — |
| resolved → archived | **operator manual (Phase 0)、 claude.ai resolve は Phase 1+ MCP write API 経由** | — |

**禁止 transition**:
- `terminated → active` 自動: 不可。 operator は status 書換で `terminated → resolved` に戻し、 必要なら新規 thread を起こす
- `archived → 任意`: archived は terminal の terminal、 immutable
- `resolved → retrying / terminated`: 不可。 resolved 後に問題が再発したら新規 thread

**`archived` immutable の semantic 範囲**: 「software-level immutable」 (= watcher は archived thread に対する transition を実行しない) を指す。 operator は filesystem level で削除 (`rm -rf threads/<ULID>/`) 可能。

**`active → terminated` direct trigger binding**: schema 起因 error (`pydantic.ValidationError` / `_FRONTMATTER_RE` parsing 失敗 / `msg_id` mismatch 等)。 これらは retry しても直らないため direct terminated。 詳細 error 分類 (どの SDK error / Phanthand error を transient と見なすか) は §5.1 sub-PR 3 (retry) 内 decide。

`awaiting_from` field の transition は **status と直交**。 invoke 完了で都度更新、 SOT は `write_reply` 成功完了時 (§3.5)。

### 3.4 Terminated fields (Decide #3b-2)

`terminated` state に入った時の追加情報を **両方独立 field** として持つ。

```python
class ThreadMeta(StrictModel):
    ...
    terminated_reason: Literal["retry-exhausted", "validation-failed"] | None = None
    terminated_at: UTCDatetime | None = None
```

**redundancy 許容**: `terminated_at` は `events.jsonl` の `ThreadStatusChanged` event timestamp と重複するが、 「snapshot vs audit log」 責任分離で同じ情報が両方に出るのは natural。 `transition_state` 唯一 entry point (§3.5) で inconsistency 構造的防止。

**`terminated_reason` Literal 拡張性**: 拡張時 schema_version bump で値追加 (§3.2 整合)。 命名 kebab-case 統一。

**非対称 OK**: `resolved_at` / `archived_at` は持たない (= operator manual 遷移、 自分が遷移させた時刻を覚えている / events.jsonl で確認可能)。 Phase 0 では terminated のみ独立、 Phase 1+ で必要になったら additive 追加。

**`updated_at` docstring 補強** (Naysayer §3 → Claude.ai 採用):

```python
class ThreadMeta(StrictModel):
    ...
    updated_at: UTCDatetime
    """Last write time of this meta.yaml, regardless of trigger.

    Updated whenever ThreadMeta is persisted, including:
    - status transition (transition_state function)
    - awaiting_from update without status change
    - any other meta.yaml write

    Distinct from event log timestamps (events.jsonl), which record
    when each event was appended.
    """
```

**Terminal-out transition 時の field 保持**: `terminated_reason` / `terminated_at` は **保持** (`terminated → resolved`、 `terminated → archived`、 `resolved → archived (terminated 経由)` で audit trail として残す)。 §3.5 sketch の `model_copy(update={...})` で caller が明示的に reset しない限り保持される実装と整合。

### 3.5 Atomicity と enforcement

**`transition_state` 関数を唯一 entry point** として 1 箇所集約。 status / awaiting_from / terminated_reason / terminated_at / updated_at を 1 atomic_write_text で同時更新。 Pydantic `model_validator` 追加せず (二重管理回避、 partial update バグは code 構造で防止)。

```python
def transition_state(
    layout: ThreadDirLayout,
    new_status: ThreadStatus,
    *,
    awaiting_from: Participant | None,
    terminated_reason: TerminatedReason | None = None,
    terminated_at: datetime | None = None,
    ...
) -> None:
    old_meta = load_thread_meta(layout)
    _validate_transition(old_meta.status, new_status)  # code-level enforce
    new_meta = old_meta.model_copy(update={...})
    atomic_write_text(layout.meta_path, yaml.safe_dump(new_meta.model_dump()))
    # meta.yaml と events.jsonl の write 順序 / 失敗 detection は §6 FI-2 で sub-PR 3 着手時に formal decide
    # (Phase 0 暫定: events.jsonl への ThreadStatusChanged append は本関数末尾で実施想定)
```

**禁止 transition enforcement**: `_ALLOWED_TRANSITIONS: dict[ThreadStatus, set[ThreadStatus]]` table-driven 設計 + `pytest.parametrize` 網羅 test。 schema validator level での enforce はせず (= schema は instance 単独 validity の責任のみ)。

```python
_ALLOWED_TRANSITIONS: dict[ThreadStatus, set[ThreadStatus]] = {
    "active":     {"retrying", "terminated", "resolved"},
    "retrying":   {"active", "terminated"},
    "terminated": {"resolved", "archived"},
    "resolved":   {"archived"},
    "archived":   set(),  # immutable terminal
}

def _validate_transition(old: ThreadStatus, new: ThreadStatus) -> None:
    if new not in _ALLOWED_TRANSITIONS[old]:
        raise InvalidTransitionError(...)
```

**`awaiting_from` 更新タイミング**: `write_reply` 成功完了時を SOT (§3.1 semantic と整合)。 invoke 開始 / 終了時には更新しない。 retry 中 (= retrying state) の `awaiting_from` は **失敗した invoke の actor を指したまま**。

### 3.6 Operator manual transition (Phase 0)

```
operator manual transition の前提 (Phase 0):
- meta.yaml 直接編集 (yq / 手動) のみ許容、 専用 CLI なし
- watcher は次回 _run_thread 開始時に meta.yaml を再読込、 race は許容
  (= operator 編集と watcher 読み込みのどちらが勝つかは undefined、
   ただし transition rule は code-level _validate_transition でチェックされるので
   invalid state にはならない)
- operator は編集前に watcher を止める (pkill / systemctl stop) ことを推奨運用
- 推奨運用に違反した場合の保証: 「invalid state にはならない」 のみ、
  watcher が古い状態を上書きする可能性は許容
```

**reload 戦略**: **(i) per-iteration reload** を採用 (sub-PR 1 着手時 decide、 commit `0960f21` 後に決定)。 watcher は `_run_thread` 開始時に毎回 `load_thread_meta(layout)` で meta.yaml を read する (= 現 `dispatcher.py:_run_thread` 実装と整合)。 (ii) startup 1 回 + invoke 開始時 cache 代替案は不採用 — cache 同期コスト > read cost、 また §3.6 の operator manual race acceptance (= 「次回 _run_thread 開始時に再読込」) との整合性が (i) のみで自然に成立する。

## 4. WatcherConfig defaults (Decide #4)

Naysayer §5-3 提案 (各値が 「観測 / 実測 / experience 由来か」 1 行 audit) に従う。

### 4.1 Audit 結果

| field | 現 default | 由来 | 結論 |
|---|---|---|---|
| `dedup_ttl_seconds` | `5.0` | filesystem event 重複観測 window、 PR #5 議論で確立 | **keep** |
| `max_concurrent_threads` | `4` | ローカル dogfooding 想定、 LLM API rate limit 考慮 | **keep** |
| `polling_mode` | `False` | filesystem watcher native event 優先 | **keep** |
| `idle_timeout_seconds` | `300.0` | LLM 応答 30s〜数分、 5 分は安全側上限 (実測なし、 暫定) | **keep (要観測)** |
| `absolute_timeout_seconds` | `3600.0` | 1 時間絶対上限、 異常 long invoke 検知 (実測なし、 暫定) | **keep (要観測)** |
| `retry_backoff_seconds` | `(5.0, 30.0, 120.0)` | exponential-ish、 LLM API rate limit 標準的値 (実測なし、 暫定) | **keep (要観測)** |
| `retry_jitter` | `0.2` | 20% jitter、 一般的妥当値 (実測なし、 暫定) | **keep (要観測)** |
| `max_retries` | `3` | 一般的妥当値 (実測なし、 暫定) | **keep (要観測)** |
| `shutdown_grace_seconds` | `60.0` | T06 hard-code、 graceful shutdown 用 | **削除** (§1.1 graceful shutdown 削除整合) |

### 4.2 追加

| field | default | 由来 |
|---|---|---|
| `orphan_tmp_cleanup_age_seconds` | `300.0` | §2.3 暫定値、 dogfooding 後 FI-3 で再 audit |

### 4.3 Feature 2 後の WatcherConfig

```python
class WatcherConfig(_StrictModel):
    """Watcher runtime tuning. Defaults are the T06+Feature2-confirmed values."""

    dedup_ttl_seconds: float = Field(default=5.0, gt=0)
    max_concurrent_threads: int = Field(default=4, ge=1)
    polling_mode: bool = False
    idle_timeout_seconds: float = Field(default=300.0, gt=0)
    absolute_timeout_seconds: float = Field(default=3600.0, gt=0)
    retry_backoff_seconds: tuple[float, ...] = (5.0, 30.0, 120.0)
    retry_jitter: float = Field(default=0.2, ge=0, le=1)
    max_retries: int = Field(default=3, ge=0)
    orphan_tmp_cleanup_age_seconds: float = Field(default=300.0, gt=0)
    # shutdown_grace_seconds 削除 (Decide #1)
```

**Flag (sub-PR 3 で対応)**: `retry_backoff_seconds: tuple[float, ...]` に min length / monotonic increase の field validator が無く、 例えば `(120, 30, 5)` のような decreasing 値も pass する。 sub-PR 3 (retry) 着手時に field validator 追加検討。

## 5. 4 sub-PR の境界 + 着手順序 (Decide #5)

### 5.1 各 sub-PR の中身

#### sub-PR 1: schema + orphan-cleanup

最大 PR、 schema 変更を全集約。

> **size warning**: sub-PR 1 は schema 変更を全集約するため **800〜1500 行規模** になる見込み (Phase 0 参考: PR #2 ~600 / PR #3 ~700 / PR #16 ~1000)。 review 効率のため、 PR description で 10 項目を明確に sectioning + Copilot review focus を `schema/`、 `storage/transition_state.py`、 `config/watcher.py` の 3 subsystem に分けて指定推奨。

- ThreadStatus enum 変更 (`awaiting-cc`/`awaiting-cai` 削除 + `retrying`/`terminated` 追加) → §3.1
- ThreadMeta 新規 4 field: `awaiting_from`, `retry_count`, `terminated_reason`, `terminated_at` → §3.1, §3.4
- docstring 補強 (`updated_at` / `SCHEMA_VERSION` namespace) → §3.2, §3.4
- `transition_state` 関数 (1 entry point) → §3.5
- `_ALLOWED_TRANSITIONS` table + `pytest.parametrize` 網羅 test → §3.5
- §3.6 operator manual transition 1 段落明文化 (docstring or 本書参照) → §3.6
- `_run_thread` 開始時の meta.yaml reload 戦略を decide + §3.6 末尾に追記
- WatcherConfig: `shutdown_grace_seconds` 削除 + `orphan_tmp_cleanup_age_seconds` 追加 → §4
- startup `.tmp` cleanup with age threshold → §2.3
- state-based status scan 枠組み → §2.1
- test fixture 全再生成 (sub-PR 2〜4 は schema additive 変更を伴わない前提) → §3.2

#### sub-PR 2: timeout

- `idle_timeout_seconds` + `absolute_timeout_seconds` 監視ロジック
- `active → retrying` 遷移 (transient timeout error)
- timeout simulation fixture (PR 内 local 整備)
- **sub-PR 内追加 decide**: D-4 SDK subprocess cleanup 責任分界 (claude-code SDK の `query()` cancel 時の zombie 処理、 候補 (a) SDK 依拠 / (b) watcher が subprocess tree track + kill / (c) zombie 許容)

#### sub-PR 3: retry

- retry loop (`retry_backoff_seconds` + `retry_jitter` + `max_retries`)
- `meta.yaml.retry_count` 永続化 → §2.2
- `retrying` state recovery integration test → §2.4
- per-thread lock 範囲明確化 (Decide #3b-1 後送り、 lock hold 時間が長くなる影響)
- `retry_backoff_seconds` field validator (min length / monotonic increase、 §4.3 flag)
- **sub-PR 内追加 decide**:
  - D-3 next_seq commit semantics (write_reply は rollback せず commit、 next_seq は最大 seq+1 で deterministic)
  - error 分類 (transient: filesystem IO / Phanthand transient HTTP / SDK rate limit → retry。 permanent: schema 起因 → direct terminated)
  - FI-2 `transition_state` の 2-phase commit semantics (meta.yaml ↔ events.jsonl の write 順序 / 失敗 detection)

#### sub-PR 4: terminate

- `active → terminated` direct trigger (schema 起因 error binding) → §3.3
- `retrying → terminated` (retry 上限突破)
- `terminated_reason` + `terminated_at` 設定 → §3.4
- `terminated`/`resolved`/`archived` thread の startup skip 統合 test → §2.1
- terminal state 関連 integration test

### 5.2 着手順序 + chain merge pattern

**順序**: 1 → 2 → 3 → 4 (依存順、 並行不可)。

依存関係:
- 1 が schema + transition_state を提供、 2/3/4 はその上で実装
- 2 (timeout) が `active → retrying` 遷移を実装 → 3 (retry) が retrying state での backoff を実装 → 4 (terminate) が `retrying → terminated` を実装

**chain merge pattern** (`feedback_chain_pr_merge.md` 整合):

- `develop/feat-robustness` 統合 branch を main `17b4908` から切る
- 各 sub-PR は base = 直前 sub-PR の head branch (chain)
- squash merge 後に下流 sub-PR を rebase + 新規 PR 作成:
  ```
  git checkout feat/<next>
  git rebase --onto develop/feat-robustness <prev-tip> feat/<next>
  git push --force-with-lease
  gh pr create --base develop/feat-robustness --head feat/<next>
  ```
- 全 4 sub-PR squash 後に develop を main へ squash merge
- 各 push 前に local で `uv run ruff format --check` + `mypy src/` 確認 (CI fail 防止)
- spirrowgames-ops APPROVE は新規 PR で引き継がれない (元 PR 引用で対応、 PR #4 と同 pattern)

### 5.3 論点 4/5/6 の処遇

| 当初提案論点 | 処遇 |
|---|---|
| 論点 4 (timeout 動作 semantics) | sub-PR 2 内 decide で消化 (D-4 含む) |
| 論点 5 (retry 範囲) | sub-PR 3 内 decide で消化 (D-3、 error 分類、 FI-2 含む) |
| 論点 6 (graceful shutdown ↔ retry interaction) | 削除確定 (§1.1 で graceful shutdown 削除済) |

**sub-PR 内 decide における Naysayer pass の運用方針**: ChatRoom 派生 thread を立てるか、 PR description / review comment で済ますか、 sub-PR 着手時に決定 (Decide #1〜#5 と本 docs review で機能した multi-pass pattern を sub-PR 内でも継続するかは個別判断)。

## 6. Future Issues (FI)

設計議論で flag されたが本書 SOT には含めない future 課題。 Phase 1+ で取り扱う。

### FI-1: 「invoke 中 vs 未開始」 の表現を別途持つ必要性

(Naysayer #3a-1 §3 flag、 GitHub Issue 化予定)

`(active, awaiting_from=claude-code)` の状態は:
- claude-code が invoke 中
- claude-code が invoke 未開始 (event 検出待ち)

の 2 ケースを同表現する。 Phase 0 では per-thread lock + asyncio task 状態で trace 可能なため meta.yaml 範疇外、 ただし Phase 1+ の **observability / metric 出力時** (= 「invoke 中 thread 数」 を export したい時) に問題化する可能性。

取り扱い時期: dogfooding 開始後の observability 議論。

### FI-2: `transition_state` の 2-phase commit semantics

(Naysayer #3b-2 §4 flag、 GitHub Issue 化予定)

`atomic_write_text` (meta.yaml) と `EventLogWriter.append` (events.jsonl) は 2 つの file 操作。 1 つ目成功 + 2 つ目失敗 (disk full / permission error) で inconsistency 可能性。

decide が必要な点:
- meta.yaml 先 → events.jsonl 後 (meta.yaml が SOT、 events.jsonl は補完)
- events.jsonl 先 → meta.yaml 後 (meta.yaml が「event log に書かれた事実」 のみ表現)
- 失敗時の rollback / detection 戦略

取り扱い時期: sub-PR 3 (retry) 着手時。 Phase 0 では現状の atomic_write_text 2 段で OK、 Phase 1+ で MCP write API 導入時に「transactional write」 が必要になったら SQLite or filesystem-level transaction 検討。

### FI-3: dogfooding 後の WatcherConfig 再 audit

(Decide #4、 GitHub Issue 化予定)

現在の defaults は「実測なし、 経験的暫定値」 中心 (特に `idle_timeout_seconds` / `absolute_timeout_seconds` / `retry_backoff_seconds` / `retry_jitter` / `max_retries` / `orphan_tmp_cleanup_age_seconds`)。 dogfooding 開始後の Phase 1 着手前に実測値で再 audit。

取り扱い時期: Phase 1 設計 phase 着手時。

## 7. References

### 7.1 ChatRoom thread

- `T-feat2-design-overview` (parent、 全議論の roof) — Decide #1, #2, #4, #5
- `T-feat2-d3a1-awaiting-from` (resolved) — Decide #3a-1
- `T-feat2-d3a2-schema-version` (resolved) — Decide #3a-2
- `T-feat2-d3b1-transition-table` (resolved) — Decide #3b-1
- `T-feat2-d3b2-terminated-fields` (resolved) — Decide #3b-2

### 7.2 Decide msg

| Decide | resolved by | 内容 |
|---|---|---|
| #1 | msg-028 | base 方針 = Naysayer 寄り |
| #2 | msg-030 | crash recovery = state-based |
| #3a-1 | msg-053 / msg-056 (close) | `awaiting_from` field |
| #3a-2 | msg-054 (close) | schema_version 据え置き |
| #3b-1 | msg-055 (close) | 8 transition 表 |
| #3b-2 | msg-057 (close) | terminated fields 両方独立 |
| #4 | msg-059 | WatcherConfig audit |
| #5 | msg-060 | sub-PR 境界 + 順序 |

### 7.3 関連ドキュメント

- `docs/architecture.md` §3 — Phase 0 baseline (snapshot vs audit log boundary)
- `docs/logging-design.md` — logging 設計
- `docs/mcp-interface.md` — MCP interface 仕様

### 7.4 関連 feedback memory

設計議論で参照された Takahito の persistent feedback:

- `feedback_design_review_format` — Pros/Cons + 推奨 + 確認質問の format
- `feedback_long_term_stepping_stone` — 短期最適 + 長期 optionality 両軸
- `feedback_decoupling_preference` — Connector / MCP サーバレベルの分離志向
- `feedback_config_defaults_first` — defaults を production-ready に、 手動編集前提にしない
- `feedback_git_workflow` — feature slice + develop branch + squash merge
- `feedback_chain_pr_merge` — chained PR の rebase + 新規 PR pattern
- `feedback_pr_resolution_summary` — PR レビュー × ChatRoom 議論の GitHub-first 運用
- `feedback_future_issues_monument` — Future Issues は GitHub Issue/Discussion 等の参照可能な場所に
- `reference_chatroom_handoff` — ChatRoom が AI 間 handoff の現行レイヤ
