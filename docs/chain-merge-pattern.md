# Chain-merge pattern — sub-PR 間 contract integration checklist

> **一般メタ運用**: sub-PR 構造を持つ任意の chain merge (Feature 2 / Feature 3 / Feature 4 …) に適用される contract integration プロセス。 元は `feature-2-design.md` §5.2.1 として Feature 2 robustness 開発中に確立され、 Phase 0 完結時点で本独立 doc に extract (Issue #30)。 Feature 3+ が chain merge pattern を採用する際、 Feature 2 専用 doc を読まずに本 doc を参照できる。

`feedback_chain_pr_merge.md` (= 上流 squash で下流 PR が auto-CLOSE、 rebase --onto + 新規 PR で進める運用) と整合。

---

## 目的

大型 feature を `develop/feat-*` 統合ブランチ配下の連鎖 sub-PR で実装する際、 **各 sub-PR 着手前に、 直前 sub-PR で merged された contract を verify する事前 phase** を運用化する。 既存 CI test では catch されない contract drift を、 着手時の design-level 整合性確認で先回り検出するのが狙い。

## 確立の経緯 (motivating case: Feature 2 PR #27)

PR #27 (Feature 2 sub-PR 2 timeout) の spirrowgames-ops review M-3 由来の meta-process improvement (Issue #28)。 PR #27 で C1 / C2 の 2 must bug が review で発見された:

- **C1**: `InvokeTimeoutError(asyncio.TimeoutError)` (`claude_code/session.py`) の inheritance と Python 3.11+ `asyncio.TimeoutError = TimeoutError` alias の subtle interaction (except 順序 / isinstance subclass match) が抑え切れていなかった
- **C2**: sub-PR 1 で merged された `REQUEUE_STATES` (`lifecycle/transitions.py`、 `startup_full_scan` 由来) と `_ALLOWED_TRANSITIONS` (`lifecycle/transitions.py`) の contract に対し、 sub-PR 2 dispatcher の timeout handler が unaware だった

共通の根本原因: sub-PR 2 着手時に sub-PR 1 で merged された contract を chain integration verify する事前 step が存在しなかったこと。 既存 CI test では catch されず、 review で初めて検出。 → 本 checklist 運用を確立。

## checklist

各 sub-PR 着手時、 直前 sub-PR で merged された以下 contract を verify する。 下記 symbol list は **Feature 2 を origin とする canonical 育成リスト** であり、 新たな contract symbol が sub-PR で導入された場合は本 checklist にも追加すること (= checklist 自体も sub-PR ごとに育てる)。 他 feature が本 pattern を採用する際は、 当該 feature 固有の contract symbol を同形で追補する。

- [ ] `_ALLOWED_TRANSITIONS` (`lifecycle/transitions.py`) — 新規 invoke path で発生しうる全 status 遷移が allowed か
- [ ] `REQUEUE_STATES` (`lifecycle/transitions.py`、 `startup_full_scan` で参照) — 新規 path が requeue される thread state を honor するか
- [ ] `TERMINAL_STATES` (`lifecycle/transitions.py`、 `dispatcher._run_thread` で short-circuit) — terminal state skip が新規 path でも維持されるか
- [ ] `transition_state` invariants (`awaiting_from` / `terminated_reason` / `terminated_at` / `retry_count`) — meta.yaml status 遷移を伴う書込は本 entry point 経由か、 rule 違反していないか
- [ ] `bump_retry_count` (`lifecycle/transitions.py`) — meta.yaml status 不変で `retry_count` だけ advance する path が caller side で本 entry point 経由か (sub-PR 2 C2 由来、 `retrying → retrying` 自己遷移禁止と integration)
- [ ] `DedupCache` semantic (`watcher/dedup.py`) — 新規 path が dedup と整合するか (sub-PR 1 review O-3、 sub-PR 2 で carry 確認済)
- [ ] Python 3.11+ language alias (`asyncio.TimeoutError = TimeoutError` 等) との interaction — `except` 順序、 `isinstance` の subclass match (sub-PR 2 C1 由来)
- [ ] `_TRANSIENT_ERROR_TYPES` / `_is_transient` (`watcher/dispatcher.py`) — allowlist transient classification、 新規 exception class 導入時に allowlist 拡張が必要か判定 (sub-PR 3 由来)
- [ ] `_handle_transient_failure` / `_handle_permanent_failure` / `_recover_retrying_to_active` / `_compute_backoff` (`watcher/dispatcher.py` private methods) — retry / permanent / recovery / backoff 各 path 拡張時の symmetry 維持、 新規 path が responsibility separation framework に従うか (sub-PR 3 由来、 PR #34 review S-1 helper extract 結果)
- [ ] `RetryBackoffStarted` event type (`schema/event.py`、 occurrence event) — 新規 retry-adjacent event を追加する時、 occurrence vs snapshot semantic との整合 (sub-PR 3 由来)
- [ ] `ThreadStatusChanged.retry_count` field (`schema/event.py`、 snapshot event) — 新規 ThreadStatusChanged emit 箇所で `retry_count=current_meta.retry_count` を populate しているか (sub-PR 3 由来、 snapshot mirror 維持)
- [ ] **O-2 carry: `_handle_transient_failure` direct unit test** (`tests/lifecycle/test_error_classification.py` への extract) — sub-PR 3 PR #34 review O-2 carry、 sub-PR 4 内では実装せず carry note のみ、 future Phase 1+ で `lifecycle/error_classification.py` extract と並行 migrate (= module extract が trigger)

## sub-PR 着手時の流れ

1. **本 checklist を 1 項目ずつ verify** (= 各 contract を新規 path で confirm、 design level の整合性確認)
2. branch 切り (`feat/<feature>-<name>` from `develop/feat-<feature>`)
3. 結果を sub-PR PR description に明記 ("Chain integration checklist verified" + 各項目の備考)
4. 実装着手

## PR description 記載 form

各項目に **判定 (✅/❌)** + **理由 (1〜2 行)** の統一 form を採用、 reviewer (Copilot / claude.ai / Takahito) が見落としを catch しやすい。

**example** (Feature 2 sub-PR 3 retry の場合):

````markdown
## Chain integration checklist verified

- ✅ `_ALLOWED_TRANSITIONS`: 新規 path `retrying → active` (retry 後 re-invoke 時の dispatcher transition) は `_ALLOWED_TRANSITIONS["retrying"] = {"active", "terminated"}` に含まれる、 整合
- ✅ `REQUEUE_STATES`: 新規 path は requeue 対象 thread state を honor、 `startup_full_scan` 経由の retrying thread → retry path の chain を verify
- ✅ `TERMINAL_STATES`: 新規 path で terminal state thread が再 invoke されない、 dispatcher の terminal short-circuit が retry 経路でも維持
- ✅ `transition_state` invariants: retry 後の `active` 復帰時に `awaiting_from` を preserve、 `retry_count` も合わせて update
- ✅ `bump_retry_count`: retry 経路では status 不変で retry_count advance のみ、 sub-PR 2 で確立した API を踏襲
- ✅ `DedupCache` semantic: retry 経路の re-invoke は新規 event 由来 (= 別 seq) のため dedup と衝突しない
- ✅ Python 3.11+ language alias: sub-PR 3 で `asyncio.TimeoutError` 派生例外を新規追加しない、 既存の `InvokeTimeoutError` (sub-PR 2 由来) のみ取扱い
````

## 関連

- 元 location: `docs/feature-2-design.md` §5.2.1 (extract 後は本 doc を SoT とする link stub)
- 確立 review: PR #27 spirrowgames-ops review M-3 (`r3215170437`)
- meta tracker: Issue #28 (※ issue body は提案時の symbol 名で記述、 現 code との差異あり、 本 doc では現 code symbol を SoT 採用) / extract tracker: Issue #30
- 整合運用: `feedback_chain_pr_merge.md` (chain PR squash merge は rebase + 新規 PR)
- 直近適用先: Feature 2 sub-PR 3 (#20、 retry) 着手前
