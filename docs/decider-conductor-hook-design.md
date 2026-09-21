# Decider — Conductor 停止判定への判断フック（設計書 v3.4）

版: **3.4** / 2026-09-21 / 起票: Fermi（Cowork セッション, 2026-09-18）/ 設計 SOT: chatroom `spirrow-mindwire/T-decider-conductor-hook` / v3 差分: Bohr msg-3818 / v3.1: Bohr msg-3820 / v3.2: Bohr msg-3822 / v3.3: Bohr msg-3824 / v3.4: Bohr msg-3826 / 独立 naysayer レビュー: Einstein msg-3819 → msg-3821 → msg-3823 → msg-3825 → msg-3827 (APPROVE) / **Tier-C 承認**: Takahito 2026-09-21（"Approve v3.4 for implementation with bounce activation gated by planned evaluation phases"）/ PR #326 round-3 PR-gate ADVISORY structure（sequential mutation overlap + Tier-C threshold config omission）を Takahito 2026-09-21（"Address both structural advisories before merging the design documentation"）で PR #331 にて修正（D21 追加 + `[decider.thresholds]` に Tier-C 閾値追加）/ PR #331 round-3 PR-gate ADVISORY structure（`elif` の連結誤読 + `stop = ...` indentation の視覚的曖昧）を Takahito 2026-09-21（"Fix both advisories within PR #331 before merging (reopen, amend, re-review)"）で本 PR にて修正（D22 追加 + §3.3.a active-mode を `if dv is not None:` 配下に nest + §3.3.b の `elif` を standalone `if` に変更）/ PR #333 round-4 PR-gate ADVISORY docs（§3.3.b inline comment の lifecycle 説明矛盾: `rule_stop_reason の直後` は §3.3.a の説明で §3.3.b は admission-gate の直後）を Takahito 2026-09-21（"Fix advisory to correct the §3.3.b comment to say 'admission-gate の直後' before merging"）で本 PR にて修正 / PR #333 round-5 PR-gate ADVISORY structure（`original_stop` を局所変数として扱っており、実装者が別関数 handler へ分割した場合に scope が壊れる ∴ 変数受け渡し戦略を明示せよ）を Takahito 2026-09-21（"Do not merge PR #333; request amendment to explicitly document the variable passing strategy"）で本 PR にて修正（D23 追加 + §3.6 新設 + code snippet を `turn.original_stop` in-memory 契約に変更 — D17 `turn.gate_result` と parity で Conductor 実装形態と独立に snapshot を運ぶ）

対象リポジトリ: spirrow-mindwire（本 repo — Conductor / adapter / state builder / replay）、spirrow-lexora（`/v1/decide` エンドポイント側、本設計の前提）。

前提: `T-decide-endpoint` (spirrow-lexora)。**本設計の実装ステップは lexora 側 `/v1/decide` 完了に blocked**。state builder / questions 定義 / replay script は先行着手可、adapter と Conductor フックは lexora 側完了後。

---

## 0. 一言で

Operator の「進める / 人に回す / 止める」判断のうち、生成が要らない判断（Tier-C 一次判定 / handoff 妥当性 / made_progress / naysayer レビュー深さ）を Lexora `/v1/decide`（裏は Jev または LLM light emulation）に寄せる。狙いは (a) silent stop の検出、(b) 聞くまでもない問いが人に届く率の低減、(c) naysayer レビューコストの削減。T42（silent stop の汎用 watchdog）とは統合する — watchdog の判定器がこの Decider。

Decider の責務は最終的に **admission-gate（`src/spirrow_mindwire/tier_c_admission_gate.py`）を通った後の grey zone** に狭められる（`ADMIT_UNSURE` / `second_time_force_admit` / label 名指しの真偽の judgement）。Tier-C の正の確定は admission-gate のラベル判定が担う。

---

## 1. 決定事項（Tier-C 承認済み。以後の実装はこれを前提にする）

| # | 決定 | 出典 msg |
|---|---|---|
| D1 | 生成の要らない判断を `/v1/decide` に寄せる。Decider は生成を行わず、noul / choice の 4+3+... の question 群に答える構造 | msg-3404 §0 |
| D2 | 単調性原則: Decider は既存規則判定の `stop` を **覆さない**。active でも「停止理由を足す」方向にしか動かない | msg-3404 §0 |
| D3 | 唯一の例外は §4 Tier-C 一次判定の bounce（annotate → bounce の移行は Takahito 承認事項） | msg-3404 §0 |
| D4 | env 切替: `MINDWIRE_DECIDER_BACKEND = off (default) \| lexora`（`MINDWIRE_DECISION_COMPOSER_BACKEND` と同じ流儀）。設定 `[decider].mode = off \| shadow \| active` | msg-3404 §1, §2 |
| D5 | 問いはコードで持ちバージョン付与（ログに残す） | msg-3404 §1 |
| D6 | ログ join キー: `thread_id + round + decision_id`。turn ログに `decision_id` を残す | msg-3404 §2, §6 |
| D7 | `typesafe-ai` skill をこの repo に置かない、`/v1/decide` のみを呼ぶ（依存を増やさない側に倒す） | msg-3404 §5 / v3 |
| D8 | 問いセット v3.4: **genuine 3 + spurious 3 + computed feature 0**。合成規則: genuine=和 / spurious=max | msg-3822, msg-3824 |
| D9 | computed feature は全廃。`routing_artifact` は Conductor `rule_stop_reason` が既に捕捉するため二重管理禁止（Principle 2）。`protected_merge` は admission-gate の `merge-protected` ラベルが担うため Decider に持たない | msg-3820, msg-3822 |
| D10 | bounce の応答は **3 択**: (a) self-resolve `NEXT: <role>` / (b) stand_down `NEXT: none` / (c) reassert `NEXT: human`。**構文的引用検査は入れない**（意味的幻覚を止められず、副作用のみ大きいため） | msg-3822, msg-3824, msg-3826 |
| D11 | safety story は「構造 + 経験 + 観測」の 3 層。invariant「構造的に不可能」は撤回する | msg-3824 |
| D12 | 実装順は shadow 先行デプロイ（`stop is None` ガード下で log-only、Takahito 承認不要 — 実動作を変えないため）。オフライン replay と live shadow JSONL 蓄積は並行 | msg-3820 |
| D13 | 評価は **A-pre / A-post / live shadow** の 3 面で出す。A-post 母数不足なら shadow が JSONL を貯め続ける | msg-3818 §6, msg-3820 |
| D14 | `TIER_C_LABELS` は `src/spirrow_mindwire/tier_c_admission_gate.py` の `ADMIT_LABELS` を import して同一定数を参照する（文字列二重管理禁止） | msg-3818 §4 |
| D15 | bounce 活性化は evaluation phase gate 制。annotate は「genuine 見逃し 0 件」を満たしたときのみ、bounce は Takahito 追加承認 | 本 spec §6-C / Takahito 2026-09-21 |
| D16 | Decider は Conductor から **2 点** で呼ばれ、両フックは strict disjoint に運用する（entry condition + logging + evaluation の全てを排他化）。(§3.3.a) 一般フックは **`original_stop is None` のときだけ** evaluate / log / route する（shadow でも `original_stop is None` ガード内に閉じる）— これは Track B の意味論（stop が無いターンで新たに stop を足すか）とも整合する。(§3.3.b) Tier-C フックは `original_stop == HUMAN` のときだけ走る。両フックが同一 turn で発火することは無い ∴ `decision_id` は上書きされず join key として機能する。**disjoint 性は D21 で導入した `original_stop = rule_stop_reason(turn)` snapshot に基づく entry-guard から構造的に従う**（`stop` を直接見ると §3.3.a active-mode Track B の mutation で §3.3.b の entry-guard が付随的に真になる — sequential mutation overlap）。D2 単調性は snapshot 排他性から従う | 本 spec §3.3 / PR #326 PR-gate BLOCKING correctness (round 1) + round-2 correctness #3 + round-3 ADVISORY structure (sequential mutation, D21 で修正) |
| D17 | admission-gate 結果の Decider への受け渡しは **in-memory `turn.gate_result` 契約**。JSONL からの live join は禁止（live / replay の distribution shift 回避、dual-management 回避）。replay driver は fixture として同じ shape の `AdmissionGateResult` を構築し `turn` に載せてから `state_builder` に渡す | 本 spec §3.5 / PR #326 PR-gate ADVISORY structure |
| D18 | Tier-C フック (§3.3.b) は **grey zone gating** で運用: `gate_result.kind ∈ {ADMIT_UNSURE, second_time_force_admit}` のときにのみ Decider に問いを渡し、`ADMIT` (with a valid label) / それ以外は問い掛けしない。理由: D9 で `merge-protected` などの label を Decider の genuine 語彙から外している ∴ もし ADMIT を Decider が再評価すれば genuine sum = 0 で誤 bounce する。Decider の権限は admission-gate が判別できなかった grey zone に限定する | 本 spec §3.3.b / PR #326 round-2 PR-gate BLOCKING correctness #1 |
| D19 | Tier-C フックの annotation は **annotate mode と bounce mode の両方で発火**。bounce mode は annotate の superset — LIKELY_NOT の注釈は bounce 資格に関わらず（`answerable_from_thread` 以外の根拠、既 bounced、2 回目、いずれの状態でも）人へ届く。bounce 節は annotate 節の後に加算的に走り、eligible な場合のみ `stop = None` を書き込む | 本 spec §3.3.b / PR #326 round-2 PR-gate BLOCKING regression |
| D20 | 両フックは **backend=off と mode!=off の設定不整合に対して fail-open**: `evaluate_general` / `evaluate_tierc` が `None` を返した場合、annotation / bounce / route の全てをスキップし escalation を人へそのまま届ける（§3.3.a は `if dv is not None:`、§3.3.b は `if tv is not None:`）。設定と env の不整合は Conductor 起動時 preflight が捕捉すべき事象で、hook 内で crash / silent drop させない — Tier-C escalation を落とすリスクの方が env 不整合を素通しするリスクより高い。加えて §3.3.a の active-mode route は `dv.is_actionable` を要求（continue verdict で `from_verdict` を呼ばない） | 本 spec §3.3 / PR #326 round-3 PR-gate BLOCKING correctness (tv None crash) + ADVISORY structure (`and dv:` truthiness) |
| D21 | 両フックの **entry-guard は `rule_stop_reason(turn)` の snapshot (`original_stop`) に基づく**。`stop` 変数は §3.3.a の active-mode Track B が mutate しうるため、`stop` を直接 entry-guard に使うと Track B が `escalate` を発火した瞬間に `stop == HUMAN` となり §3.3.b の entry-guard も真になる（sequential mutation で entry-guards が overlap）。これは D16 の「両フックの入場条件は disjoint」を prose の主張だけに留めていた（実態は `is_grey_zone` の内部 state に依存して bug を回避していた）欠陥である。snapshot `original_stop = rule_stop_reason(turn)` を 1 度だけ取り、`original_stop is None` / `original_stop is StopReason.HUMAN` で両フックを排他化する（両者は standalone `if` として書く — D22）。これで disjoint 性が entry-guard の shape 自体から従い、内部 state（`gate_result` の有無）に依存せずに保証される。log_decision の二重発火リスク（D16 で扱った correctness 事象）も snapshot 排他により構造的に不可能になる | 本 spec §3.3 / PR #326 round-3 PR-gate ADVISORY structure (sequential mutation overlap) |
| D22 | §3.3.b Tier-C フックは **standalone `if` として書く**（`elif` にしない）。§3.3.a と §3.3.b は Conductor の別 lifecycle 点で呼ばれ、間に admission-gate が `turn.gate_result` を populate するタイミングを挟む ∴ 物理的に連結された `if / elif` ブロックではない。`elif` で書くと (a) 2 フックが同一ブロックに連結されているという誤読を招き、実装者が admission-gate 実行を挟まず 1 箇所で書いてしまう ∴ `turn.gate_result` が未設定のまま §3.3.b に入る、または (b) `elif` 相当の意味論的排他が entry-guard の shape でなく `if / elif` の連結に依存しているように見える。実際の disjoint 性は D21 の snapshot によって保証されており、`if turn.original_stop is StopReason.HUMAN:` は §3.3.a の `if turn.original_stop is None:` と排他条件で shape 上 disjoint。加えて §3.3.a active-mode 側の `stop` 書き換えは `if dv is not None:` 配下に nest することで `if cfg.decider.mode == "active" and dv.is_actionable:` の block indentation を視覚的に明確化する（context lines で `stop = ...` が `if` の block 外に見える誤読の余地を排除） | 本 spec §3.3 / PR #331 round-3 PR-gate ADVISORY structure (elif contiguity + indentation legibility) |
| D23 | `original_stop` snapshot の受け渡しは **`turn.original_stop` in-memory 契約**（D17 `turn.gate_result` と parity）。§3.3.a と §3.3.b は Conductor の別 lifecycle 点で呼ばれ、間に admission-gate 実行を挟む（D22）∴ 局所変数 (Python の `original_stop = stop`) では 2 phase を跨いで snapshot を共有できない。実装者が Conductor を単一 loop で書く選択と `execute_general_decider` / `execute_tierc_decider` の別 handler 関数へ分割する選択の両方を許すためには、snapshot を `turn` オブジェクトに attach する必要がある。§3.3.a が `turn.original_stop = rule_stop_reason(turn)` で書き、§3.3.b が `turn.original_stop` として読む。これで (a) 別関数実装でも局所変数の scope を跨がず契約が成立、(b) 局所変数保持のために 2 phase を artificial に単一関数へ束縛する誘因が消える。契約詳細は §3.6 | 本 spec §3.3, §3.6 / PR #333 round-4 PR-gate ADVISORY structure (implicit variable scope across lifecycle phases) |

---

## 2. 現状の事実（実測。実装はこれに接続する）

- Conductor 停止判定は `src/spirrow_mindwire/conductor/core.py` の `StopReason` enum（`HUMAN / SETTLED / NO_HANDOFF / NO_PROGRESS / SELF_HANDOFF / ROUND_CAP / EMPTY / HOLD / CI_WAIT`）で捕捉される。self-handoff / round cap / empty / hold / CI pending / 不明ハンドオフ / no progress はすべて `rule_stop_reason` 側で `stop` に載る ∴ Decider 経路には到達しても適用されない。
- Admission gate は `src/spirrow_mindwire/tier_c_admission_gate.py` に実装済み。`ADMIT_LABELS = frozenset({"goal", "cost", "irreversible", "merge-protected"})`、`UNSURE_LABEL = "unsure:goal?"`。`LogKind` enum に `ADMIT` / `BOUNCE` / `LABEL_MIGRATION` 等の記録種別あり。
- `spirrow-lexora/T-decide-endpoint` の `/v1/decide` 完成が本設計の前提。**未完成の間は Decider adapter は書けない**（stub / mock ですらこの前提には触らない — 本物の endpoint spec が固まるまで adapter 実装は着手しない）。
- state builder は既存の `scripts/thread_heads.py` / `scripts/head_skip_decide.py` の抽出ロジックを再利用可能（同じ thread head 情報を DecisionState に注入する）。
- `naysayer_lexora` adapter (`src/spirrow_mindwire/adapters/naysayer_lexora.py`) と同じパターンで書ける（stateless HTTP、`ChatMessage` の代わりに Decider payload、shared `LexoraClient`、fail-loud）。

---

## 3. 構成（既存パターン踏襲）

### 3.1 モジュール配置

```
src/spirrow_mindwire/
  decider/
    __init__.py
    state.py         # DecisionState + state_builder(turn) -> DecisionState
    questions.py     # v1 の question 定義（version 付き、コードで保持）
    verdict.py       # 答え → Verdict の純関数
  adapters/
    decider_lexora.py  # /v1/decide を叩く adapter。env: MINDWIRE_LEXORA_URL, MINDWIRE_DECIDER_BACKEND
scripts/
  decider_replay.py  # --track=tierc / --track=handoff の replay driver
```

### 3.2 DecisionState

`decider/state.py` の `DecisionState` フィールド:

- `thread_id: str`
- `round: int`
- `roster: Mapping[str, Role]`（identity → role）
- `head_summary: str`（head 要約、最大 M 文字）
- `recent_events: list[EventSummary]`（直近 N 件、author / `NEXT:` / 本文先頭 M 文字）
- `parsed_next: str | None`（head の `NEXT:` パース結果）
- `prev_next: str | None`（1 つ前のターンの `NEXT:`）
- `diff_stat: DiffStat | None`（implementer/PR ターンのみ）
- `gate_result: AdmissionGateResult | None`（admission-gate が turn 上に置く in-memory 値。`kind ∈ ADMIT / ADMIT_UNSURE / BOUNCED / RETRY_ADMIT.reason / second_time_force_admit`。**JSONL からの join はしない** — live と replay の入力分布シフトを避けるため、admission-gate が `turn` オブジェクト経由で in-memory に受け渡すこと。replay driver は同じ `turn` 形で構築した fixture を Decider に流す。詳細は §3.5）

**Constants**: `TIER_C_LABELS` は `src/spirrow_mindwire/tier_c_admission_gate.py::ADMIT_LABELS` を import して同一定数を参照する（文字列二重管理禁止 — D14）。

### 3.3 Conductor フック（2 箇所）

Decider は Conductor から **2 点で呼ばれる**。両者は目的も入場条件も異なる。

#### 3.3.a 一般フック（shadow / active — Track B: `handoff_valid` / `made_progress`）

`StopReason` 規則判定の直後、**`turn.original_stop is None` のときにのみ**呼ぶ（§3.3.a 自身が snapshot を書き、その値で分岐する — §3.6 in-memory 契約）。silent-stop 検出などの「stop が無かったところに stop を足す」用途:

```python
stop = rule_stop_reason(turn)              # 既存
turn.original_stop = stop                  # snapshot on turn: 両フックが lifecycle 越しに読む in-memory 契約 (D23, parity with D17 turn.gate_result)。
                                           # stop は §3.3.a で mutate されうるが turn.original_stop は不変。
                                           # Conductor を単一 loop で書いても、§3.3.a と §3.3.b を別 handler 関数に分割しても、
                                           # 両フックが共通の source (turn) から snapshot を読める ∴ 局所変数を跨いで共有する必要は無い。

if turn.original_stop is None:             # §3.3.b と structurally disjoint (D16 + D21)
    dv = decider.evaluate_general(state_builder(turn))  # Track B。None if MINDWIRE_DECIDER_BACKEND=off
    if dv is not None:                                  # backend off (D20 fail-open) なら以降を skip
        log_decision(turn, stop, dv, hook="general")   # turn.decision_ids に append
        if cfg.decider.mode == "active" and dv.is_actionable:
            stop = StopReason.from_verdict(dv)         # active モード時 & 実際に stop を追加する verdict のみ経路が変わる
```

- **entry-guard は `turn.original_stop is None`**（D16 + D21 + D23）。`stop` を直接見ると、Track B が `escalate` を発火して `stop = StopReason.from_verdict(dv)` で `HUMAN` に書き換えた瞬間、§3.3.b の entry-guard（`stop == HUMAN`）も付随的に真になる（sequential mutation で entry-guards が overlap）。`rule_stop_reason` の返り値を `turn.original_stop` として 1 度 snapshot し、両フックの entry-guard を snapshot 基準にすることで disjoint 性が entry-guard の shape 自体から従う（内部 state `is_grey_zone` に依存しない）。
- **snapshot の受け渡しは `turn.original_stop` in-memory 契約（D23）**: §3.3.a と §3.3.b は admission-gate 実行を挟んで別 lifecycle 点で呼ばれる（D22）∴ 局所変数を跨いで共有する経路は無い。§3.3.a は snapshot を `turn` に載せ、§3.3.b は `turn.original_stop` として読む。§3.6 参照。Conductor を単一 loop で書く実装と、`execute_general_decider` / `execute_tierc_decider` の別関数に分割する実装のいずれでも contract は同一。turn 属性を強制することで、実装者が snapshot 変数の scope を保つためだけに 2 phase を単一関数へ artificial に束縛することを避ける。
- `turn.original_stop == HUMAN` のターンで §3.3.a が走ると `log_decision` が §3.3.b と 2 回発火し、`decision_id` を join key として扱えなくなる ∴ snapshot による排他で構造的に不可能にする。Track B は「stop が無かったのに新たに stop を足すべきか」を問うもので、`NEXT: human` で既に停止が確定しているターンには問いとして意味を持たない。
- `turn.original_stop is None` ガードは D12 shadow の要件でもある: shadow mode は log-only、active mode でのみ `stop` に書き込む。
- **`dv.is_actionable` の意味**: `evaluate_general` は Track B の 2 問（`handoff_valid` / `made_progress`）を評価するが、両方が閾値を超えて「続行してよい」と判断した場合の Verdict は truthy な object だが `stop` を追加すべきではない。`dv.is_actionable = True` は「`from_verdict(dv)` が `StopReason` の実値を返す」ことを意味し、continue verdict では `False` になる ∴ `stop = None` の shape が保たれる。単に `if dv:` にすると continue verdict でも `from_verdict` が呼ばれ、enum factory が非 actionable な値に対して何を返すかで挙動が不定になる（PR #326 round-3 PR-gate ADVISORY structure）。
- Track B の一般フックは `turn.original_stop == HUMAN` のターンでは走らない（D21 snapshot 排他）∴ Tier-C bounce は §3.3.b の別フックで実行される。

#### 3.3.b Tier-C フック（annotate / bounce — parsed_next == "human" のみ）

`turn.original_stop == StopReason.HUMAN` のとき（§3.3.a が turn に snapshot した値、D21 + D23）、human に届ける **直前**に走る。annotate は escalation を人へ通す前に注釈を付け、bounce は escalation を呼び出し元へ差し戻す:

```python
# admission-gate の直後（§3.3.a が turn.original_stop に snapshot 済み、D23）、
# human への手渡し（forced-naysayer や escalation 通知）の直前。
# 独立フックであり §3.3.a とは admission-gate の実行を挟んで別 lifecycle 点で呼ばれる ∴ standalone `if`
# （`elif` にしない — 物理的に §3.3.a のブロックに連結されるという誤読を避け、また 2 フックの間に admission-gate
# が turn.gate_result を populate するタイミングを尊重するため。disjoint 性は D21 snapshot が担う — D22）。
# snapshot 値は turn.original_stop 経由で読む — 別 handler 関数として実装しても、Conductor loop 内 inline でも、
# 局所変数を跨いで渡す必要は無い（D23 in-memory 契約、parity with turn.gate_result）:
if turn.original_stop is StopReason.HUMAN and cfg.decider.tierc.mode != "off":  # §3.3.a と structurally disjoint (D21, D22)
    gr = turn.gate_result   # §3.5 in-memory 契約
    # Decider は admission-gate を通った後の grey zone のみを裁く（D9, D18）。
    # ADMIT with valid label (goal / cost / irreversible / merge-protected) は既に人へ確定 ∴ 問い掛けしない。
    is_grey_zone = (
        gr is not None
        and gr.kind in {LogKind.ADMIT_UNSURE, LogKind.second_time_force_admit}
    )
    if is_grey_zone:
        tv = decider.evaluate_tierc(state_builder(turn))  # 3+3+0 の問いへ回答。None if MINDWIRE_DECIDER_BACKEND=off
        if tv is not None:                                # §3.3.a と parity: backend off なら以降を skip し人へそのまま届ける
            log_decision(turn, stop, tv, hook="tierc")    # turn.decision_ids に append

            # Annotation は annotate / bounce mode の両方で発火する superset (D19):
            # bounce mode に上げても、bounce eligible でない LIKELY_NOT の注釈が人へ届く既存挙動は保たれる。
            if cfg.decider.tierc.mode in {"annotate", "bounce"} and tv.verdict == "LIKELY_NOT":
                escalation.annotation = f"Jev: likely not Tier-C (p={tv.confidence:.2f}) — {tv.reason}"
                # stop は HUMAN のまま。人には届く。annotate は情報の付与のみ

            # Bounce は annotate に加算される: mode=bounce + LIKELY_NOT + 根拠=answerable_from_thread + 未 bounce のみ発火
            if (cfg.decider.tierc.mode == "bounce"
                    and tv.verdict == "LIKELY_NOT"
                    and tv.reason == "answerable_from_thread"
                    and not bounce_ledger.already_bounced(turn)):
                bounce_ledger.mark(turn)
                stop = None                                # human 経路をキャンセルし、
                reroute_to_bounce_prompt(turn, tv)         # 呼び出し元 agent へ §4.5 の 3 択プロンプトを差し戻す
            # 2 回目は already_bounced(turn) == True で bounce 節を通過せず、そのまま stop = HUMAN として人へ届く。
    # gr が None または grey zone でない: Decider は問い掛けせず、admission-gate の判定をそのまま人へ届ける。
    # tv is None (backend=off with mode!=off の設定ミス): fail-open で人へそのまま届ける。設定と env の不整合は Conductor の起動時 preflight で捉えるべきで、hook 内では動作停止させない。
```

- 入場条件: `turn.original_stop == StopReason.HUMAN`（§3.3.a が turn に snapshot 済み、D21 + D23）かつ `[decider.tierc].mode != "off"` かつ `gate_result.kind ∈ {ADMIT_UNSURE, second_time_force_admit}`（D18 grey zone gating）。`turn.original_stop` を使うことで、§3.3.a active-mode の Track B escalate（`stop` を `HUMAN` に書き換える）が誤って §3.3.b を発火させることは無い。snapshot が `turn` 属性として運ばれるため、§3.3.a と §3.3.b を別 handler 関数として実装しても局所変数の scope 問題は生じない（§3.6 in-memory 契約）。
- **本フックは standalone `if` として書く（`elif` にしない — D22）**: §3.3.a と §3.3.b は Conductor の別 lifecycle 点で呼ばれ、間に admission-gate が `turn.gate_result` を populate するタイミングを挟む ∴ 物理的に連結された `if / elif` ブロックではない。`elif` で書くと (a) 2 フックが同一ブロックに連結されているという誤読を招き、実装者が admission-gate 実行を挟まず 1 箇所で書いてしまう ∴ `turn.gate_result` が未設定のまま §3.3.b に入るリスク、(b) `elif` 相当の意味論的排他が entry-guard の shape でなく `if / elif` の連結に依存しているように見える。実際の disjoint 性は D21 の snapshot によって保証されており、`if turn.original_stop is StopReason.HUMAN:` は §3.3.a の `if turn.original_stop is None:` と排他条件で shape 上 disjoint。
- **admission-gate ADMIT (with any valid label) → Decider は問い掛けしない**: D9 で `merge-protected` を Decider から削除した ∴ Decider の問いセットには merge-protected を genuine と認識する語彙が無い。もし ADMIT を Decider が再評価すれば、genuine sum = 0 で spurious のいずれかが発火した瞬間に valid な merge-protected escalation が LIKELY_NOT と判定される ∴ 誤 bounce。これは D9 が admission-gate に委譲した責務を Decider が上書きするパターンで、D2 単調性・Principle 2 の二重管理禁止の両方に反する。Decider の権限は grey zone (ADMIT_UNSURE / second_time_force_admit) に限定する。
- annotate は `stop` を書き換えない（人には届く。文言注釈のみ）。
- bounce は 1 回に限り `stop = None` にして呼び出し元 agent へ差し戻す。2 回目の `NEXT: human` は `bounce_ledger.already_bounced(turn) == True` により bounce 節を通過せず、そのまま `stop = HUMAN` として人へ届く。annotation は 2 回目でも発火するので人は Decider の判定を注釈として見える。
- `routing_artifact` を Decider が計算する経路は存在しない（`rule_stop_reason` が既に落としている — D9, msg-3820）。
- **backend=off / mode!=off の fail-open**: `evaluate_tierc` は `MINDWIRE_DECIDER_BACKEND=off` のとき `None` を返す。§3.3.a が `if dv is not None:` で処理をスキップするのと parity で、§3.3.b も `if tv is not None:` で annotation / bounce 節を全部スキップし、escalation は無編集で人へ届く。設定と env の不整合（`[decider.tierc].mode = "annotate"` に上げたが env は off のまま）は Conductor 起動時の preflight が捕捉すべき事象で、hook 内では動作停止・crash させない — Tier-C escalation を silent drop するリスクの方が env 不整合を騒ぎ立てないリスクより高い ∴ hook は fail-open（D20）。

**turn ログの `decision_ids`**: `log_decision(turn, stop, tv, hook=...)` は turn の `decision_ids: list[DecisionRecord]` に append する（scalar でなく list）。D21 の snapshot 排他により §3.3.a / §3.3.b は同一 turn で両方走ることが構造的に無いため、`decision_id` の join-key 上書き問題は entry-guard 段階で防がれる。list shape を採るのは将来 hook を足したときに壊れないための保険。join key は各 record の `decision_id` を使う。

### 3.4 設定（両フック共通）

```toml
[decider]
mode = "shadow"                # off | shadow | active
active_questions = ["handoff_valid", "made_progress"]

[decider.thresholds]
# Track B (§3.3.a 一般フック, §4.7)
handoff_valid_min = 0.30       # 未満 → escalate
made_progress_min = 0.25       # 未満 → stand_down
min_confidence   = 0.60        # choice 系はこれ未満なら無視

# Tier-C (§3.3.b Tier-C フック, §4.4 合成規則)
genuine_min      = 0.60        # sum(genuine) >= genuine_min → CONFIRMED
spurious_min     = 0.55        # max(spurious) >= spurious_min かつ genuine < genuine_max → LIKELY_NOT
genuine_max      = 0.40        # LIKELY_NOT 判定に必要な genuine 上限（これ以上は CONFIRMED 側寄り ∴ LIKELY_NOT にしない）

[decider.tierc]
mode = "off"                   # off | annotate | bounce
skip_naysayer_when_confirmed = false
```

`naysayer_gating` の shadow（compute + LOG, don't act）と同じ意味論。

**閾値の初期値**は暫定であり、§10 Open questions の通り shadow データで較正する（Track B は 1〜2 週、Tier-C は §6.2 A-post + live shadow の n が数十件貯まった時点）。genuine 系は sum、spurious 系は max で合成されるため（§4.4）、初期値は「genuine 3 問中 2 問が中程度の positive」「spurious 3 問中 1 問が強く positive」を境界に置く暫定値。

### 3.5 admission-gate 結果の受け渡し（in-memory 契約、JSONL join 禁止）

`DecisionState.gate_result` は **live 実行時に admission-gate が `turn` オブジェクト経由で in-memory に渡す**。Decider が JSONL ファイルを直接読むことは無い。理由:

- **分布シフト回避**: replay で JSONL を join し、live で `None` のままにすると、Decider backend が受け取る入力が live / replay で構造的に異なる ∴ 較正済み閾値が live で無効化される（PR-gate advisory: docs/decider-conductor-hook-design.md structure 指摘）。
- **dual-management 回避**: live で JSONL をパースすると、admission-gate の state が in-memory と on-disk の 2 箇所に載る ∴ Principle 2 違反。

**受け渡しの流れ**:

1. `tier_c_admission_gate.decide_admission(...)` は verdict と `log_entries` を返す。live の Conductor はこれを既に受け取っている（`src/spirrow_mindwire/tier_c_admission_gate.py` 現行 API）。
2. Conductor 側でこの verdict を `turn.gate_result: AdmissionGateResult` として保持する（型は `AdmissionVerdict + BounceReason | RetryAdmitReason + kind: LogKind` の compact な dataclass、詳細は実装 PR で確定）。
3. `state_builder(turn)` はこの `turn.gate_result` をそのまま `DecisionState.gate_result` にコピーする。JSONL は触らない。
4. **replay driver** (`scripts/decider_replay.py`) は同じ `AdmissionGateResult` 型を fixture として構築し、`state_builder` に渡す前の `turn` に載せる。fixture の source は過去の JSONL でよいが、それは **replay driver の入力構築フェーズ**の話で、Decider から見た state の shape は live と replay で同一になる。
5. `gate_result` が `None` になるのは admission-gate を通らないターン（`stop != HUMAN` の全ターン + admission-gate mode が off のとき）のみ。この場合の Decider の振る舞いは Track B 問いのみに縮退する（Tier-C 問いは呼ばれない）。

これにより live / replay の入力分布は同一の shape になり、`gate_jsonl_kind` を offline join で埋めていた v3.4 初稿の distribution shift は解消される。

### 3.6 `original_stop` snapshot の受け渡し（in-memory 契約、局所変数依存禁止）

`turn.original_stop: StopReason | None` は **`rule_stop_reason(turn)` の返り値 snapshot を Conductor loop の 2 lifecycle 点間で共有する in-memory 契約**（D23、D17 `turn.gate_result` と parity）。§3.3.a と §3.3.b は admission-gate 実行を挟んで別 lifecycle 点で呼ばれる（D22）∴ Python の局所変数 (`original_stop = stop`) では 2 phase を跨いで snapshot を共有できず、実装形態を単一 loop に artificial に束縛してしまう。契約 shape:

- **書き込み**: §3.3.a の直前で `turn.original_stop = rule_stop_reason(turn)` を実行。Conductor loop の共通経路に置く（`stop is None` / `stop is HUMAN` の分岐前に必ず走る位置）。
- **読み出し**: §3.3.a / §3.3.b の両フックが `turn.original_stop` を entry-guard として参照する（`is None` / `is StopReason.HUMAN`）。§3.3.a の active-mode が `stop` を mutate しても `turn.original_stop` は不変。
- **不変性**: 1 turn 内で `turn.original_stop` は一度書かれた後書き換えない。writing は Conductor loop 側の責務で、Decider は read-only。
- **`None` の意味**: `turn.original_stop is None` は「`rule_stop_reason` が stop を出さなかった turn」を意味する。Track B (§3.3.a) が「新たに stop を足すか」を問う対象の turn。
- **実装形態を選ばない**:
  - **単一 loop 実装**: `turn.original_stop = rule_stop_reason(turn)` を loop 内で 1 度書き、両フックが同じ turn オブジェクトから読む。
  - **別関数 handler 実装**（例: `execute_general_decider(turn, cfg)` / `execute_tierc_decider(turn, cfg)`）: どちらの関数も `turn` を引数に取り、`turn.original_stop` を参照する。関数間で局所変数を渡す必要は無い。
- **replay driver の責務**: `scripts/decider_replay.py` は fixture の `turn` オブジェクトを構築する際に `turn.original_stop` を必ず埋める（historical JSONL から `rule_stop_reason` 出力を再現するか、fixture が Tier-C を対象とする場合は `StopReason.HUMAN`）。`turn.gate_result` と同じく、live / replay で state builder に渡す `turn` の shape を同一化する。
- **禁止事項**: 局所変数 `original_stop = ...` を Decider 側で参照しない（scope が壊れる）。`turn.original_stop` を Decider 側で書き換えない（read-only）。JSONL から `stop` 履歴を join しない（in-memory 契約に閉じる、§3.5 と同じ distribution shift 論法）。

これにより D21 の disjoint 性が、Conductor の実装形態（単一 loop / 別関数 handler）とは独立に、`turn.original_stop` の shape 上排他だけで構造的に保証される。

---

## 4. 問いセット v3.4

**2 つの問いセットが並行して定義される**:

- **Tier-C 問いセット（3 + 3 + 0、§4.1〜§4.4）** — §3.3.b Tier-C フックが呼ぶ。`turn.original_stop == HUMAN` のときにのみ回答される（D21 + D23 snapshot 基準）。genuine / spurious の合成規則と bounce 定義は本節の主題。
- **Track B 問いセット（§4.7）** — §3.3.a 一般フックが呼ぶ。毎ターン回答される（`stop is None` ガードにより shadow / active mode でのみ経路に効く）。目的は silent-stop 検出と handoff 妥当性チェック。

### 4.1 Tier-C genuine 側（noul, 3 問）

- **`changes_goal_or_spec`** — このハンドオフが承認を要する変更（goal / spec の書き換え）を含むか
- **`incurs_cost`** — このハンドオフが cost を発生させる決定を含むか
- **`irreversible`** — このハンドオフが取り消せない操作（データ削除・公開リリース）を含むか

### 4.2 Tier-C spurious 側（noul, 3 問）

- **`answerable_from_thread`** — スレッド内の既存情報から答えが導けるか
- **`is_permission_seeking`** — 権限を求めているだけで、判断そのものは著者ができる状態か
- **`is_review_disposition`** — レビュー結果への disposition（「異論なし、進めてよいか」型）で、実質は自律進行が正解か

### 4.3 computed feature（DecisionState 上の事実, 0 個）

**削除済み**。v3.4 では computed feature を持たない。理由:

- `routing_artifact`: `rule_stop_reason` が self-handoff / round cap / embodiment != terminal_coding_agent / CI pending を既に落としている（D9, msg-3820）
- `protected_merge`: admission-gate の `merge-protected` ラベルが担う（D9, msg-3822）

### 4.4 合成規則

- **genuine 合成**: `sum(genuine) >= genuine_min → CONFIRMED`。3 種は概念的に直交する ∴ 和で合成。
- **spurious 合成**: `max(spurious) >= spurious_min かつ genuine < genuine_max → LIKELY_NOT`。`answerable_from_thread` と `is_review_disposition` は外延が相関する ∴ 和 / 平均だと二重計上され閾値が問い数依存になる（msg-3818 §2 Q1, msg-3818 §4）。
- **UNSURE**: いずれの条件も満たさない場合。

### 4.5 bounce 定義（LIKELY_NOT かつ根拠 = `answerable_from_thread` のとき 1 回だけ）

Conductor は human に上げる前に呼び出し元 agent へ **1 回だけ**差し戻す。プロンプト:

> このハンドオフは Decider により「スレッド内の情報から答えが導ける可能性が高い」と判定されました（confidence p, 根拠 = `answerable_from_thread`）。以下いずれかで応答してください:
>
> **(a) self-resolve** — スレッド内の情報から答えを組み立て、次の作業担当を指す `NEXT: <role>` を出す（正常系）。差し戻し理由と結論は本文に短く残してください。
>
> **(b) stand_down** — 判断保留が不要になったと認める場合、応答本文にその旨を記し、末尾を `NEXT: none` で締める。JSONL には `BOUNCE_ACCEPTED` として記録される。スレッドに未処理の別作業が残っており自分がそれを引き取る場合は (a) を選ぶこと。
>
> **(c) reassert NEXT: human** — Tier-C 判断が実際に必要な場合、Tier-C 該当理由（goal / cost / irreversible / merge-protected のどれか）を本文に明記して再度 `NEXT: human`。**2 回目ルールにより Decider を経由せず必ず human に届く**。

**構文的引用検査は入れない**（v3.1 で提案した msg_id 実在性チェックは v3.3 で廃止）。理由は Einstein v3.1 の指摘通り: 構文検査は意味的幻覚を止められず、「引用しろ」の圧力で無関係な msg_id を貼らせる副作用の方が大きい。

### 4.6 Safety story（構造 + 経験 + 観測）

「silent drop が構造的に不可能」という invariant は撤回する。代わりに 3 層で守る（D11, msg-3824）:

1. **構造層（不変）**: 2 回目の `NEXT: human` は Decider を通さず無条件で human に届く。1 回だけの bounce は既存機構で保証されており、無限ループ不可。genuine Tier-C の agent が引き下がらない限り必ず人に届く。
2. **経験層（bounce active の入場条件）**: bounce は annotate 実運用後、評価セット (A-pre / A-post / live shadow) で **genuine 見逃し 0 件**を満たしたときにしか active にしない（§6-C 制約 1）。この条件は Takahito 追加承認事項でもある（D15）。
3. **観測層（事後レビュー）**: bounce の (a) self-resolve / (b) stand_down / (c) reassert のすべてを JSONL に kind 付きで記録し、evaluation report に msg_id 全件列挙する。genuine を self-resolve / stand_down で silent drop していないか、人が事後に走査できる。

### 4.7 Track B 問いセット（一般フック用、noul, 2 問）

§3.3.a 一般フックが呼ぶ。silent-stop の検出（`stand_down`）と、動けない handoff の検出（`escalate`）に使う。msg-3404 §3 の v1 定義を踏襲:

- **`handoff_valid`** (noul) — この handoff の `NEXT:` 先が実際に動ける identity で、停滞する自己ハンドオフではないか。停滞判定は「認識可能な identity か」「embodiment が terminal_coding_agent か（動けない web_ai_chat 系ではないか）」「author == next の自己ハンドオフではないか」の意味的判断を含む。**ただしこれらのうち規則で確定するもの（self-handoff, embodiment 不整合など）は `rule_stop_reason` 側で先に落ちる ∴ Decider が答える対象は「規則ではまだ落ちていないが人間の目には停滞して見える」grey zone のみ**（D9, msg-3820）。
  - 閾値: `handoff_valid_min = 0.30` 未満 → `escalate`（人に上げる）。
- **`made_progress`** (noul) — このラウンドは具体的な変更・決定・新情報を生んだか（過去の言い換え・同じ主張の反復ではないか）。実装フェーズでは diff_stat の実質、設計フェーズでは新たな決定 / データの導入 / 反論への disposition の有無を評価する。
  - 閾値: `made_progress_min = 0.25` 未満 → `stand_down`（続行しても価値が出ない）。

**合成規則**: 2 問は独立に評価し、各々の閾値で個別に verdict を出す。両方が同時に発火した場合、優先度は `escalate > stand_down`（人の目を優先）。`choice` 型の `next_action` / `tier` は shadow のみで残す（msg-3404 §3、active には昇格しない）。

**Track B 回帰ケース**: §6.5 参照。`rule_stop_reason` で落ちるケース（msg-3656 の author==NEXT、msg-3712 / 3797 の spawn 不能 embodiment）は含めない。Decider の replay ではなく Conductor rule 側テストに置く。

---

## 5. Tier-C 一次判定（parsed_next == "human" のときのみ追加）

`NEXT: human` が出たとき、本当に Tier-C たり得るかを一次判定する。**admission-gate が入った後の世界では、Decider の役割は「著者が名指したラベルは本当か」に変わる**（msg-3818 §1）。

段階（Tier-C mode: `off → annotate → bounce`）:

- **入場条件（両モード共通）**: `turn.original_stop == HUMAN`（D21 + D23 snapshot 基準、§3.3.a が turn に書き、§3.3.b が turn から読む）かつ `gate_result.kind ∈ {ADMIT_UNSURE, second_time_force_admit}` (D18)。admission-gate ADMIT (with a valid label) の場合、Decider は問いを渡さず（4.1 が `merge-protected` の genuine 語彙を持たないため誤 bounce の risk）、admission-gate の判定をそのまま人へ届ける。
- **annotate**: escalation は従来通り人へ届く。通知に `Jev: likely not Tier-C (p) — <根拠>` を 1 行付けるだけ。実力未知数の間はここまで。
  - 入場条件: 見逃し 0 件制約（§6-C 制約 1）を live shadow で満たしたとき。
  - annotate 文言の `likely not Tier-C (p)` は confidence が較正済みレンジのときだけ出し、それ以外は無表示。
- **bounce**: bounce は annotate の superset（D19）— annotation は annotate mode と bounce mode の両方で LIKELY_NOT に対して発火する。bounce 節は annotation の後に加算的に走り、LIKELY_NOT かつ根拠が `answerable_from_thread` かつ 1 回目のときに限り呼び出し元へ差し戻す（§4.5）。**2 回目の `NEXT: human` は bounce 節を通過せず必ず人へ（annotation は継続）**。無限ループ不可。
  - 入場条件: Takahito 追加承認 + §6-C 制約 1 継続。
- **`skip_naysayer_when_confirmed`**: CONFIRMED なら `force_naysayer_only_on_explicit_human` の Gemini 相談を省略、UNSURE のみ従来通り相談（cost lever）。

正解ラベルは自然に貯まる: 人が escalation を「決めた」か「呼び出し元に返した」かが chatroom に残る = genuine / spurious の正解。

---

## 6. 較正・評価

### 6.1 replay script

`scripts/decider_replay.py`:

- `--track=tierc` → A-pre / A-post 両方を吐く
- `--track=handoff` → handoff_valid / made_progress の replay（B track）

state builder は Conductor 側と同一実装を再利用（D12 と同じ builder を使う — 分布シフト防止）。

### 6.2 トラック A — 2 スライス（A-pre / A-post）

**A-pre**（歴史的分布, 179 件そのまま）
- 用途: ルール単体の効果確認および v8 gate 導入前後の分布比較。
- **Jev の go/no-go には使わない**（gate 前分布は gate 後には存在しない — msg-3818 §1）。

**A-post**（v8 gate 適用後に admit された残りを Decider に流す）
- 179 件 + 遡及正例に、admission-gate v8 のラベル必須化と §2.3 の relay routing を先に適用し、`ADMIT` / `ADMIT_UNSURE` / `second_time_force_admit` として通ったものだけを対象。
- 予測母数: MERGE ≈ 17 + GOAL 2 + α ∴ **数十件を下回る可能性が高い**。その場合は結果ヘッダに「replay 単独で bounce 可否を確定できない」と書き、live shadow JSONL の蓄積待ちに切り替える。
- 比較相手: 「ルール vs Jev vs LLM light」ではなく **「全件 escalate の現状 vs LLM light vs Jev」**（gate 後の残りではルールは定義上何も言わない — msg-3818 §1）。
- state builder は **`DecisionState.gate_result`（in-memory の `AdmissionGateResult`、`kind ∈ ADMIT / ADMIT_UNSURE / BOUNCED / RETRY_ADMIT.reason / second_time_force_admit`）を feature として持つ**。`ADMIT_UNSURE` と `second_time_force_admit` は「ルールが判別に失敗した」集合 ∴ Decider annotate が最初に価値を出す場所。**JSONL の live-time join は禁止**（§3.5 in-memory 契約 — live と replay で同一 shape、distribution shift 回避）。replay driver は過去 JSONL を fixture 構築の入力にしてよいが、Decider へ渡す時点では in-memory `AdmissionGateResult` に正規化されている。

### 6.3 判定規則の事前固定（P 値ではなく規則として）

1. **制約（必須）**: genuine の見逃しが比較相手以下、絶対数で **0 件**。1 件でも見逃したら bounce 投入不可、msg_id を全件列挙。
2. **効果**: A-post 対象で、Jev の正解 LIKELY_NOT 件数 − LLM light の正解 LIKELY_NOT 件数を **対応のある比較**（McNemar / 符号検定）で。不一致対が 10 未満なら「差は測れない」と結論に明記。
3. **同等なら安い方**: 差が測れない場合は Jev を入れず、LLM light か annotate 止まり。TypeSafe への依存を増やさない側に倒す（D7）。

### 6.4 §6a — 見逃し / bounce の msg_id 全件列挙

evaluation report に msg_id 単位で列挙する（集計値だけで通さない）:
- genuine 見逃し
- `BOUNCE_ACCEPTED` (b: stand_down)
- `BOUNCE_SELF_RESOLVED` (a: self-resolve) — self-resolve 後の `NEXT:` 先と本文冒頭 M 文字を付記。特に元 handoff が「goal / cost / irreversible / merge-protected のいずれかに読める言い回しを含んでいた」ケースをフラグ立てして人がスキャンできるようにする（heuristic の実装は後段、まず msg_id 列挙だけ）。

### 6.5 トラック B — handoff_valid / made_progress

**回帰ケースには本スレッド自身の一部を含める**が、以下は Decider の replay には載せず Conductor の rule 側テストに置く（D9, msg-3820）:
- msg-3656 の `author == NEXT` 自己ハンドオフ → `rule_stop_reason` の `SELF_HANDOFF` で落ちる
- msg-3712 / 3797 の spawn 不能 embodiment → `rule_stop_reason` 側で落ちる

トラック B の回帰ケースは「rule で落ちない、`handoff_valid` が真に問いとして働くもの」だけに絞る。

---

## 7. naysayer レビュー深さ判定（段階 3、別 PR 可）

implementer の push ごとに走る Gemini フルレビュー（PR 平均 2.4 回・最大 13 回）の手前で、diff + 前回レビューを state に:

- `only_addresses_prior_findings` (noul)
- `introduces_new_design_decision` (noul)
- `touches_risky_area` (noul: 認証・削除・スキーマ・デプロイ)

→ 対応のみなら差分レビュー、新規判断ありならフル。`max_review_rounds` の粗い cap より賢い lever。

併せて verdict 一貫性チェック: REQUEST_CHANGES 本文の指摘が diff の実在箇所を指しているか (noul)。truncated-diff 系事故の検出（TypeSafe citation-check cookbook 相当）。

---

## 8. 実装順

1. **state builder**（`gate_result` を in-memory feature 化、§3.5）**+ questions v3.4**（Tier-C 3 + 3 + Track B 2、§4.7）**+ replay script**（`--track=tierc` は A-pre / A-post 両方吐く、`--track=handoff` は Track B）。lexora `/v1/decide` に blocked されないため先行着手可。
2. **adapter + verdict + Conductor フック、mode=shadow で先行デプロイ**（`stop is None` ガード下、log-only。Takahito 承認不要 — 実動作を変えないため）。lexora `/v1/decide` 完了後。
3. **`T-tier-c-admission-gate` PR #322 マージ後（既済）、並行**で:
   - (3a) A-pre / A-post のオフライン replay を回す（既存 179 件 + 遡及正例）
   - (3b) shadow から live JSONL を蓄積する（`ADMIT / ADMIT_UNSURE / BOUNCED / RETRY_ADMIT.reason / second_time_force_admit` の実分布）
4. **トラック A 評価（A-pre / A-post / live shadow の 3 面）→ Takahito が bounce 投入可否 & backend 選択を決める**（最初の Tier-C 判定）。live shadow の n が数十に達した時点で評価を締める。
5. `handoff_valid` / `made_progress` のみ active（トラック B 通過後）。
6. **Tier-C annotate**（入場条件は §6-C 制約 1 のみ — 見逃し 0 件。3 者比較の勝敗は annotate には無関係で、bounce と backend 選択に効く）。annotate 文言の `likely not Tier-C (p)` は confidence が較正済みレンジのときだけ出す。
7. naysayer 深さ判定（shadow → active）。
8. **Tier-C bounce**（Takahito 追加承認後、§4.5 の 3 択プロンプト込み）。

### 遡及正例の収集規則（msg-3818 §2 Q3, msg-3820）

- **母集団**: 8 project ではなく全 project の `type=decide, author=human` を遡る。「A: 進める」型は genuine ではない ∴ 方向・仕様・費用を実際に変えた decide だけを Bohr が全文から拾う。
- **本当の見逃し例**: agent が黙って決め、後で human がひっくり返したもの。gate の `DECIDED:` ログが入れば今後はそこから取れる。過去分は human の「それは違う」型 decide を探す。
- **合成ケース / red-team は別トラック**にし、実データと **絶対に合算しない**。同系統のモデルが書いた合成例は LLM emulation にとって in-distribution で、3 者比較を歪める。用途は「見逃しのストレステスト」だけ。
- **20 の意味を過大に読まない**: 見逃し 0 / 20 でも 95% 上限は約 14%（rule of three）∴ 安全性は見逃し率の数字ではなく §4.6 の safety story（構造 + 経験 + 観測 の 3 層）で担保する。

---

## 9. 非変更事項（v3.4 で不変）

- 単調性原則（D2、既存 stop を覆さない）
- `/v1/decide` のみを呼ぶ制約（D7）
- annotate → bounce の移行は Takahito 承認事項（D15）
- ログ join キー: `thread_id + round + decision_id`（D6）
- `TIER_C_LABELS` import による文字列二重管理禁止（D14）
- shadow 先行デプロイ（D12）
- 問いセット 3 + 3 + 0（D8）
- 合成規則: genuine=和 / spurious=max（D8）
- bounce 3 択、構文検査なし（D10）
- 3 層 safety story（D11）
- 評価 3 面（A-pre / A-post / live shadow — D13）

---

## 10. Open questions / 実装時に決めること

- Lexora `/v1/decide` の request / response schema（本設計は「4 種類の question type: noul / choice / free-form」までは決めたが、実際のワイヤ形式は lexora 側 `T-decide-endpoint` の spec に従う）
- `[decider.thresholds]` の初期値は shadow データを 1〜2 週貯めてから較正する（現在は暫定値: Track B `handoff_valid_min = 0.30 / made_progress_min = 0.25 / min_confidence = 0.60`、Tier-C `genuine_min = 0.60 / spurious_min = 0.55 / genuine_max = 0.40`）。Tier-C 閾値は §6.2 A-post replay と live shadow の n が数十件に達した時点で McNemar / 符号検定と併せて較正する（§6.3）。
- `DecisionState.recent_events` の N と `head_summary` の M（暫定 N=5, M=500 chars）

---

## 11. 参照

- `docs/operator-board-design.md` §17（Cowork operator セッションの 116 判断点、light ティア判断リプレイ 85%）
- `src/spirrow_mindwire/tier_c_admission_gate.py`（admission gate v8 実装、`ADMIT_LABELS`）
- `src/spirrow_mindwire/conductor/core.py`（`StopReason`, `rule_stop_reason` 相当）
- `src/spirrow_mindwire/adapters/naysayer_lexora.py`（adapter パターンの参照実装）
- `src/spirrow_mindwire/lexora/client.py`（Lexora HTTP client）
- ADR-2026-05-23-07（Stage 3 Autonomy Gating）
- ADR-2026-06-03-16（naysayer CI-gate）
- ADR-2026-06-03-17（naysayer design participation）
- chatroom `spirrow-mindwire/T-decider-conductor-hook`（設計 SOT）
- chatroom `spirrow-mindwire/T-tier-c-admission-gate`（前提: v8 gate）
- chatroom `spirrow-lexora/T-decide-endpoint`（前提: `/v1/decide` エンドポイント）
