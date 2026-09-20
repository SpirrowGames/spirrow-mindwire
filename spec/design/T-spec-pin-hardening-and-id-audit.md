---
spec_id: SPEC-2026-09-20-pin-hardening-and-id-audit
thread: T-spec-pin-hardening-and-id-audit
target_repo: spirrow-mindwire
base_branch: main
status: active
canary: required
supersedes:
  - SPEC-2026-08-11-design-spec-delivery
obligations:
  - OBL-SPEC-PIN
  - OBL-SPEC-RECEIPT
  - OBL-SPEC-SCOPE-CLOSURE
verify_exempt_ids: ["U-1"]
items:
  - id: I-1
    title: "本 manifest を spec/design に設置し spec PR を開く"
    paths: ["spec/design/T-spec-pin-hardening-and-id-audit.md"]
  - id: I-2
    title: "spec/design/verify.py に BOOTSTRAP / PROHIBITED_FIELD 認識・V-13 の 13 code 化・V-14 新設・_OPTIONAL_MANIFEST_KEYS を実装"
    paths: ["spec/design/verify.py"]
  - id: I-3
    title: "atomic cutover — dispatcher が全 dispatch で pin を書く ＋ OBL-SPEC-PIN / OBL-SPEC-RECEIPT body の cutover を単一 PR で"
    paths:
      - "src/spirrow_mindwire/watcher/dispatcher.py"
      - "src/spirrow_mindwire/dispatcher/core.py"
      - "docs/mindwire-turn.md"
      - "spec/process/obligations.yaml"
---

# pin 硬化 ＋ 内部 id 監査 — 窓外 `ABSENT` を halt に、id 参照を機械検査に

## §0 この文書の読み方

**本 spec は自己完結していない — 差分 spec である。** 旧 spec **SPEC-2026-08-11-design-spec-delivery** の後継であり（`supersedes`）、その全条項を**継承**したうえで以下だけを差し替える。旧 spec ファイルは main 上に immutable として残存する（D-19 / D-20）∴ 読者は両文書を並列に参照すること。

**継承するもの:** 旧 §0（読み方）・§1（実測値 E-*）・§1.1（規範根拠）・§2 の D-1〜D-31（本 spec で override するものを除く — §2.0 参照）・§2.1（検査されていないもの）・§4-3（OBL-SPEC-SCOPE-CLOSURE）・§6（items 継承規則）。

**差し替えるもの:**
- 旧 §3（pin schema と解決手順）に `mode` フィールドと step 3.5、reason code `BOOTSTRAP` / `PROHIBITED_FIELD` を追加（§3 本節）。
- 旧 §4-1（`OBL-SPEC-PIN` body）を**新 body 全文**に置き換え（§4-1 本節、逐語）。
- 旧 §4-2（`OBL-SPEC-RECEIPT` body）を**新 body 全文**に置き換え（§4-2 本節、逐語）。
- 旧 §5（`verify.py` 検査）に V-14 新設、V-13 enumeration を 13 code に拡張、`_OPTIONAL_MANIFEST_KEYS` を追加（§5 本節）。
- 旧 D-* に D-32〜D-39 を追加（§2.1）。
- 旧 A-* に A-30〜A-40 を追加（§7）。

**id namespace は旧 spec と共有する**（D-30 継承 — 一度振った id は再利用も再割当もしない）∴ 本 spec は未使用番号のみを採る（D-32 以降・A-30 以降・V-14）。`D-25′` の `′` は id の一部（旧 §0 継承）— 本 spec 内でも同様。**inherited id の V-14 解決は `supersedes` chain を辿る**（D-37 — 詳細は §5-D）。

**本 spec は分量規律（D-1 継承）に従う** — 論拠は thread `T-spec-pin-hardening-and-id-audit` に残す。

## §1 前提

旧 spec §1 の実測値（E-1〜E-12）および §1.1 の ADR-2026-05-23-07 引用を継承する。本 spec は新規機構を導入せず、既存 pin 機構の硬化 ＋ 診断追加であり、追加の実測値を要しない。

**未検証（前提に使ってはならない項目）: 現在 0 件。**

## §2 決定

### §2.0 継承と override

旧 D-1〜D-31 のうち、本 spec が上書きするものは以下のみ:

- **D-24 override**: 「`ABSENT` は 2 事態の bytewise 同一痕跡」は D-33 の物証導入で無効化される。`NO-PIN` の 2 クラス分割は D-34 で再定義（新: BOOTSTRAP / FAULT）。「新 code は既定 FAULT」規則および分割が §3 の全 code を覆う要件は継続する。
- **D-12 継続と拡張**: 旧 D-12 が語る bootstrap 窓は依然として存在するが、その表現は `NO-PIN(ABSENT)` から `NO-PIN(BOOTSTRAP)` に移る（D-33）。窓の中の receipt は §4-2 の BOOTSTRAP 形で書く。
- **D-31 継続**: body は §3 手順の逐語を含む — 手順が 13 code に拡張された分だけ body が伸びる。
- **D-19 継続**: 本 spec も merge 後 immutable ∴ 訂正は次世代 spec を起こす。

他の旧 D-* はすべて継承する（D-1〜D-11 / D-13〜D-23 / D-25′ / D-26〜D-31）。

### §2.1 新規決定（D-32〜D-39）

- **D-32（`.mindwire/pin` は dispatcher が常に書く）** dispatcher（コード側の `src/spirrow_mindwire/…` と `/mindwire-turn` による手動ディスパッチの双方）は、**すべての** implementer / naysayer dispatch の直前に `.mindwire/pin` を書く。対象 thread がその dispatch 時点で有効な spec-pin を持たない場合は §3-B の bootstrap 形を書く。書かない選択肢は存在しない — 「書き忘れ」だけが `ABSENT` を作る唯一経路になる。
- **D-33（bootstrap 形は物証を持つ）** bootstrap 形の pin は untracked ファイルとして作業ツリーに存在し、その content（`mode: bootstrap`）が「spec が原理的に載っていない turn である」ことを明示的に宣言する。∴ agent は「pin ファイルが在って中身が bootstrap を宣言している」ことを作業ツリーの内側から検証できる — 旧 D-24 の根「untracked ファイルの不在は反証不能」は content の記載を反証対象にすることで迂回される。
- **D-34（`NO-PIN` 分割の再定義 — D-24 override）** `NO-PIN` の 2 クラスは以下:
  - **BOOTSTRAP** — 1 code (`BOOTSTRAP`)。pin present ＋ `mode: bootstrap`。**message body 続行が sanctioned な唯一の code。**
  - **FAULT** — 12 code (`ABSENT` / `PARSE_ERROR` / `SCHEMA_VERSION` / `MISSING_FIELD` / `PROHIBITED_FIELD` / `DETACHED_HEAD` / `BRANCH_MISMATCH` / `REPO_MISMATCH` / `FETCH_UNAVAILABLE` / `COMMIT_UNREACHABLE` / `BLOB_UNREADABLE` / `SHA_MISMATCH`)。すべて halt。`ABSENT` は本 spec で FAULT 側に移った。
  - 分割は §3 の全 13 code を覆う。§3 に新 code が追加された場合、既定は FAULT に属する（旧 D-24 継承）。BOOTSTRAP に新 code を足すには本決定を改訂しなければならない（fail-closed — D-6 継承）。
- **D-35（pin schema は `schema_version: 1` を維持し `mode` を追加、BOOTSTRAP 判定は schema_version 検査の後）** `mode: {resolved, bootstrap}`、省略時 default = `resolved`（旧 pin と後方互換）。`mode: resolved` は旧 §3 の全必須フィールドを要求（現行通り）。`mode: bootstrap` は §3-A の限定 field 集合のみ許す。**BOOTSTRAP 判定は `schema_version` 検査を通過した後にのみ行う** — 未来 v2 pin を旧 agent が bootstrap 経路で素通しさせないため。`schema_version` を上げない理由は、旧 verify.py が bootstrap 形を旧 step 4 で `MISSING_FIELD` として halt する fail-closed 挙動を保つため（正しい fail-closed）。
- **D-36（`verify.py` は本 spec でも gate ではない — 旧 D-10 継承）** V-14 の新設と pin BOOTSTRAP / PROHIBITED_FIELD 認識は診断であり、CI gate にしない。main 上の常態は warning 0 / error 0（旧 A-12 継承、本 spec A-34 で自己適用を追加）。
- **D-37（V-14 は「明示定義」を要求し、`supersedes` chain を辿る）** V-14 は id 参照側と定義子側を次のとおり扱う:
  - **id パターン（境界あり）**: `(?<![A-Z0-9-])[A-Z]+-\d+′?(?![A-Z0-9-])`。直前 / 直後が `[A-Z0-9-]` の場合はマッチしない（`SPEC-2026-08-11-design-spec-delivery` や `ADR-2026-05-23-07` からの部分抽出を防ぐ）。末尾 prime を id の一部として保つ（`D-25′` を切らない — D-30 継承）。
  - **定義子（明示 3 形）**: `**X-nn**` bold および `**X-nn（…` 形（decision list 見出し）、表行頭 `| X-nn |`、front-matter `items[].id`。
  - **`supersedes` chain walking**: current manifest の定義子集合と `verify_exempt_ids` は、`supersedes` に列挙された各 `SPEC-*` id を `spec/design/*.md` 中で front-matter `spec_id` 一致検索により file 解決し、その本文から定義子を再帰的に抽出、front-matter の `verify_exempt_ids` があれば current の集合に union する。先祖 file を発見できない / 開けない場合は V-14 error として報告（診断のみ — exit code は D-36 により 0 のまま）。
  - PIN reason code（`ABSENT` 等の全大文字語）は id パターンにマッチしない ∴ V-14 対象外（V-13 が別に照合）。
- **D-38（narrative 定義は front-matter で明示的に免除する）** `**X-nn**` bold でも表行頭でも定義子を持たない id は、front-matter の `verify_exempt_ids` に列挙する。V-14 はこのリストに載る id を照合対象から外す。免除する id にはその根拠を manifest 本文で示すことを規律とする（機械検査ではない — 旧 A-15 と同種の人手条件）。免除経路を持たないと旧 D-21 により V-14 は出荷できない（旧 `T-design-spec-delivery.md` の `U-1` は定義子を持たない ∴ 無差別に効かせれば main が恒久 error になる — 旧 A-9 で `U-1` は narrative に解消と説明されているため）。narrative-only inherited id（parent の `U-1` のように chain walking でも解決しないもの）は current の `verify_exempt_ids` に明示すること。
- **D-39（cutover は atomic である）** dispatcher の pin 常時書きと obligation body の `ABSENT`-halt cutover は**同一 PR で land する**（I-3 が両者を運ぶ）。理由: 逐次 merge はいずれの順でも loop 中間状態を破断する — dispatcher land 先行 → old body が bootstrap 形 pin を `MISSING_FIELD` として halt、body land 先行 → 未 pin 状態を `ABSENT` として halt。中間状態そのものを消すのが唯一の fail-safe な形であり、間に soft-cutover 版 body を挟む案は正本の版数を増やす代償を払う（Principle 2 に触れる）— 本 spec は payload の cutover コストを atomic PR の広さに集約する。順序制御機構（依存グラフ・DAG）は導入しない（旧 D-15 継承）。

## §3 `.mindwire/pin` schema（差分）

旧 §3 の schema 表・解決手順・reason code enumeration を継承したうえで、以下を追加する。

### §3-A schema フィールド追加

旧 §3 の表に 1 行追加:

| field | type | required | 意味 |
|---|---|---|---|
| `mode` | str | ✗ | `resolved` \| `bootstrap`。省略時 default = `resolved` |

`mode: resolved` は旧 §3 の全必須フィールド（`schema_version` / `spec_id` / `thread` / `repo` / `branch` / `path` / `blob_sha` / `commit` / `pinned_at` / `pinned_by`）を要求する（現行と同じ）。

**`mode: bootstrap` の制約:**
- 必須: `schema_version` (=1), `mode` (=`bootstrap`), `pinned_at`, `pinned_by`
- 任意: `reason` (str, 自由記述)
- **禁止（7 field）: `spec_id` / `thread` / `repo` / `branch` / `path` / `blob_sha` / `commit`** — いずれかが present なら `NO-PIN(PROHIBITED_FIELD)`

未知フィールドは無視してよい（旧 §3 継承の前方互換）。

### §3-B 解決手順（step 順の差分）

旧 §3 の 11 step を継承。step 3（`schema_version != 1` → `SCHEMA_VERSION`）と step 4（必須フィールド／型／hex40）の**間**に新 step 3.5 を挿入する:

- **step 3.5（新）**: `pin.get("mode") == "bootstrap"` の場合:
  - `pinned_at` / `pinned_by` の存在と型（非空 str）を確認 — 欠落は `NO-PIN(MISSING_FIELD)`
  - §3-A の 7 個の禁止 field が pin dict に**在らない**ことを確認 — いずれかが present なら `NO-PIN(PROHIBITED_FIELD)`
  - すべて OK → `NO-PIN(BOOTSTRAP)`（sanctioned proceed on message body）
- `mode` が省略か `resolved` の場合、step 4 以降を実行（旧 §3 のまま）。

**BOOTSTRAP 判定は step 3（`schema_version`）を通過した後にのみ行う** — 未来 v2 の bootstrap 形 pin を旧 v1 agent が素通しさせない（D-35）。

### §3-C 例

**resolved 形**（現行と同じ、`mode: resolved` は任意で書ける）:

```yaml
schema_version: 1
mode: resolved  # optional; default
spec_id: SPEC-2026-08-11-design-spec-delivery
thread: T-design-spec-delivery
repo: spirrow-mindwire
branch: feature/spec-delivery-i3
path: spec/design/T-design-spec-delivery.md
blob_sha: 4b825dc642cb6eb9a060e54bf8d69288fbee4904
commit: 1f0a3c9e5b7d2a4f6c8e0b1d3f5a7c9e1b3d5f70
pinned_at: 2026-08-11T09:30:00Z
pinned_by: human
```

**bootstrap 形**:

```yaml
schema_version: 1
mode: bootstrap
pinned_at: 2026-09-20T00:00:00Z
pinned_by: dispatcher
reason: "no spec-pin mapping for thread T-spec-pin-hardening-and-id-audit at dispatch time"
```

### §3-D reason code 総表

**13 code、2 クラス:**

- **BOOTSTRAP** (1): `BOOTSTRAP` — sanctioned proceed on message body、唯一の proceed side code。
- **FAULT** (12): `ABSENT`, `PARSE_ERROR`, `SCHEMA_VERSION`, `MISSING_FIELD`, `PROHIBITED_FIELD`, `DETACHED_HEAD`, `BRANCH_MISMATCH`, `REPO_MISMATCH`, `FETCH_UNAVAILABLE`, `COMMIT_UNREACHABLE`, `BLOB_UNREADABLE`, `SHA_MISMATCH` — すべて halt。

receipt（§4-2）はこの code をそのまま書く。§3 に新しい reason code が追加された場合、それは既定で FAULT に属する（D-34）。

**pin 解決は他の manifest を読まない**（旧 §3 継承）。対象 spec が withdrawn / superseded であるかは解決経路に含めない。その検出は `verify.py` の V-10 が warning として担う。

## §4 obligation 本文（差分）

`spec/process/obligations.yaml` の以下 2 entry の body を**新 body 全文に置き換える**（`origin` ブロックは無いまま、in-place edit）。§4-3（OBL-SPEC-SCOPE-CLOSURE）は変更しない。

### §4-1 `OBL-SPEC-PIN`（新 body — 全文、逐語）

```yaml
- id: OBL-SPEC-PIN
  role: implementer
  body: |
    Before you do anything else on a turn, look for `.mindwire/pin` at the
    repository root and resolve it exactly as the spec delivery manifest
    specifies. That manifest is SPEC-2026-09-20-pin-hardening-and-id-audit,
    on thread T-spec-pin-hardening-and-id-audit, and where the repository
    you are working in carries it you may read the full procedure there.
    You are not required to: this obligation on its own decides what you
    do with every outcome, and a repository that does not carry that
    document changes none of your duties here. Resolution is fail-closed,
    and every way it can end has one reason code, spelled exactly as
    written here.

    No `.mindwire/pin` file at all is ABSENT. That is an upstream fault:
    the dispatcher is required to write a pin on every dispatch, either
    resolved or bootstrap, so a missing file means the dispatcher did not
    do its part. Stop and report ABSENT; do not proceed on the message
    body. A YAML parse failure is PARSE_ERROR. A schema_version other
    than 1 is SCHEMA_VERSION. A required field that is missing or
    malformed is MISSING_FIELD. A pin whose `mode` field is `bootstrap`
    is BOOTSTRAP: the dispatcher has explicitly declared that no
    specification is in force for this turn, and this is the only code
    under which you may proceed on the message body. A pin that declares
    `mode: bootstrap` yet also carries any of `spec_id`, `thread`,
    `repo`, `branch`, `path`, `blob_sha`, or `commit` is
    PROHIBITED_FIELD: a bootstrap pin must carry none of the
    resolved-form fields, and a mixed pin is neither one nor the other.
    Stop and report; inferring which side the dispatcher meant is the
    class of guess this obligation forbids. A `git rev-parse
    --abbrev-ref HEAD` that answers `HEAD`, fails, or comes back empty
    is DETACHED_HEAD. A current branch that does not equal the pin's
    `branch` is BRANCH_MISMATCH. A repo name that does not match is
    REPO_MISMATCH. A fetch you could not run is FETCH_UNAVAILABLE. A
    pinned commit you cannot confirm is reachable from `origin/main` is
    COMMIT_UNREACHABLE. A blob you cannot read is BLOB_UNREADABLE. A
    blob whose sha is not `blob_sha` is SHA_MISMATCH. Those thirteen are
    the whole list. Report the code with that spelling; do not invent
    one, do not abbreviate one, and do not translate one. Do not raise,
    do not retry with a guess, and do not repair the pin.

    Reachability has one network rule. If the pinned commit is already
    an ancestor of your local `origin/main`, accept it and fetch
    nothing. Only if it is not — or if you have no `origin/main` ref at
    all — run `git fetch origin +refs/heads/main:refs/remotes/origin/main`
    exactly once and judge again. If that fetch fails or is unavailable
    to you, the verdict is NO-PIN/FETCH_UNAVAILABLE: you could not
    determine the answer. If the fetch succeeds and the commit is still
    not reachable, the verdict is NO-PIN/COMMIT_UNREACHABLE: the pin
    names a commit that is not on `main`, which usually means the
    specification was never merged. Report whichever code you got; they
    have different causes and different fixes, and collapsing them costs
    the reader the diagnosis.

    NO-PIN is a state to report, not an obstacle to route around. Say
    NO-PIN in your reply with its reason code. What you may do after
    that depends on the code, and there are exactly two classes.
    BOOTSTRAP is the only code under which you may proceed on the
    message body: the dispatcher wrote a pin whose sole content is "no
    specification applies to this turn", so no authorised specification
    was ever named for you to lose. Every other code — including ABSENT
    — means either a pin was issued and did not resolve, or none was
    issued when one was required. Either is an upstream fault, not a
    degraded mode you may run in: stop, report the code, and do not
    carry out the turn's work from the message body. Do not reconstruct,
    infer, or recall specification content you cannot read in this turn:
    a remembered spec and a read spec are indistinguishable in your own
    output and distinguishable to no one else. When you are proceeding
    under BOOTSTRAP and the message body alone does not contain enough
    to act on, stop and say what is missing.

    Never delete `.mindwire/pin`. Do not delete, rename, move, truncate,
    or rewrite it, and do not include it in any cleanup, tidying, or
    formatting change. Keeping `.mindwire/` out of version control is
    not covered by that prohibition: when an item of the governing
    specification declares the change, adding `.mindwire/` to
    `.gitignore` is required work, and it leaves the pin file itself
    untouched on disk. What this paragraph forbids is making the pin go
    away or changing what it says — not making it untracked. If it
    looks stale, wrong, or inconsistent with the work you were asked to
    do, report that and stop; the pin is written by the dispatcher and
    is not yours to correct.

    When the pin resolves, the pinned document is the specification for
    the turn. The message body may narrow what you are asked to do
    within that document, but it may not silently contradict it. If it
    does, stop and report the contradiction, naming both sides; do not
    choose one and proceed.

    Four faults mean you could not read the pinned document at all —
    BLOB_UNREADABLE, COMMIT_UNREACHABLE, FETCH_UNAVAILABLE, and
    SHA_MISMATCH. For those, your report must also declare the document
    unreadable in the same form OBL-DECLARE-UNREADABLE requires for the
    sources it names, and reading that entry for the declaration form is
    part of this obligation. This is the list of codes that need the
    extra declaration. It is not the list of codes that make you stop:
    you stop on every code except BOOTSTRAP.
```

本 body の `Never delete ...` 段落は、本 spec が定める負の制約そのものである。**§4-1 の本文が唯一の正本であり**、本 spec の他節に原文があるわけではない。実装時に改稿してはならず、`obligations.yaml` には逐語で載せること（A-2 継承、A-32 の body 一意判定条件がこの逐語一致に依存する）。末尾段落は D-9 継承の帰結であり、`OBL-DECLARE-UNREADABLE` の body には**一切触れない**ことでその entry の `origin.original_length`（E-9）を保つ。

### §4-2 `OBL-SPEC-RECEIPT`（新 body — 全文、逐語）

```yaml
- id: OBL-SPEC-RECEIPT
  role: implementer
  body: |
    Open every reply in which you performed, or attempted, implementation
    work with a receipt naming what you actually read this turn, on one
    line. A turn you stopped on because the pin did not resolve is an
    attempted turn and needs one too:

      SPEC <spec_id> <blob_sha first 12> <path> (pin: RESOLVED)

    or, when the pin resolved as BOOTSTRAP and OBL-SPEC-PIN let you go on:

      SPEC (pin: NO-PIN/BOOTSTRAP) — worked from message body only

    or, when the pin did not resolve and OBL-SPEC-PIN made you stop:

      SPEC (pin: NO-PIN/<reason code>) — halted, no work from message body

    OBL-SPEC-PIN decides which of those you are in, and this obligation
    asks only that the line you print match what you actually did. The
    worked-from-message-body form is only for BOOTSTRAP; ABSENT is not
    BOOTSTRAP, it is a fault, and every fault code takes the halted
    form. Never print the worked-from-message-body form after stopping,
    and never print it under ABSENT or any other fault code: it would
    claim you did the one thing that obligation forbade, and a receipt
    confessing a violation you did not commit is as false as one hiding
    a violation you did.

    Follow a receipt you worked under with the item ids from the
    specification you acted on; after a halt there are none, so name
    what you were asked to do and which code stopped you instead. The
    receipt reports what you read, not what you believe to be true:
    naming no sha is a correct receipt, not a confession. A reply that
    does work without a receipt is incomplete. A receipt naming a sha
    you did not read in this turn is a false statement about your own
    execution, and is worse than no receipt at all — it is the one claim
    in your output that no reviewer can check against the diff, so it is
    the one claim you must not get wrong.
```

### §4-3 `OBL-SPEC-SCOPE-CLOSURE`

**変更なし**（旧 spec §4-3 の body をそのまま継承）。

## §5 `spec/design/verify.py`（差分）

旧 §5 の V-1〜V-13 と入出力仕様を継承したうえで、以下を実装する。

### §5-A pin BOOTSTRAP / PROHIBITED_FIELD 認識（V-9 拡張）

`_resolve_pin` に §3-B の step 3.5 を実装する。V-9 の info 出力に `NO-PIN(BOOTSTRAP)` と `NO-PIN(PROHIBITED_FIELD)` を新規に含める。exit code に影響しない（既存 `NO-PIN` と同扱い、D-36）。

### §5-B V-13 の enumeration 拡張

`PIN_REASON_CODES` タプルを **13 要素**にする（旧 11 ＋ `BOOTSTRAP` ＋ `PROHIBITED_FIELD`）。V-13 は 13 code すべての逐語出現を照合する（A-31）。

### §5-C V-1 に optional key を追加

`_OPTIONAL_MANIFEST_KEYS: dict[str, type] = {"verify_exempt_ids": list}` を新設する。V-1 の検査は次を行う:

- (a) 必須 key（`_REQUIRED_MANIFEST_KEYS`）の存在と型（現行どおり）
- (b) optional key が**存在する場合**、その型が期待どおり（新規）
- (c) それ以外の未知 key は**現行どおり silent に無視**（前方互換継承 — 未知 key を error 化すると本 spec 自身が supersede される将来の spec を破壊する）

`verify_exempt_ids` の要素は `list[str]` として型検査する。個々の str が id パターンに合致するかの検査は V-14 の内側で行う。**各要素が実際に本文に参照として現れるかは検査しない**（未使用 exempt 宣言を error にすると future revision に厳しすぎる — silent OK）。

### §5-D V-14 新設 — 内部 id の未定義参照検査

| id | 検査 | level |
|---|---|---|
| V-14 | manifest 本文中の内部 id 参照（境界付き id パターン）を全走査し、各参照について定義子（本 manifest ＋ `supersedes` chain 全体の bold / 表 / `items[].id`）の存在を照合する。`verify_exempt_ids` に列挙された id（本 manifest ＋ chain の union）は照合対象外。定義を持たず exempt list にも無い参照 → error。 | error |

**実装規則:**
- **id パターン**: `(?<![A-Z0-9-])[A-Z]+-\d+′?(?![A-Z0-9-])`。末尾 prime を含む（`D-25′` を切らない）。前後の負の lookaround により `SPEC-2026-08-11-design-spec-delivery` や `ADR-2026-05-23-07` からの substring 抽出を防ぐ（境界に来る空白・句読点・全角文字は class 外）。
- **参照抽出範囲**: front-matter を除く本文全体（`---` の 2 本目以降）。**コードブロック（```` ``` ```` fence 内）およびインラインコード（single-backtick 対の内側）は除外**する（例文中の id 誤検出を避けるため — D-21 上の false-positive 化を防ぐ最小手当）。
- **定義子抽出**:
  - bold: 行内で最初に現れる `\*\*[A-Z]+-\d+′?[（(]`（decision list item の title 直前区切り）あるいは `\*\*[A-Z]+-\d+′?\*\*` を定義子とみなす。
  - 表形式: 行頭 `\| [A-Z]+-\d+′? \|` パターンで表内定義を拾う（旧 spec の `E-N` / `V-N` 用）。
  - front-matter の `items[].id` は自明に定義子（`I-N` 用）。
- **`supersedes` chain の解決**: 現 manifest の front-matter `supersedes` に列挙された各 `SPEC-*` id について、`spec/design/*.md` を走査し、front-matter の `spec_id` が一致する file を発見して読み込む。読み込んだ file から再帰的に定義子を抽出し、`verify_exempt_ids` があれば current の集合と union する。先祖 file が発見できない / 読み込めない場合は V-14 error（診断のみ — D-36 により exit code 0 のまま）。
- PIN reason code（`ABSENT` 等の全大文字語）は id パターンにマッチしない ∴ V-14 対象外（V-13 が別に照合）。

### §5-E `--json` 出力への影響

- pin state に `BOOTSTRAP` と `PROHIBITED_FIELD` を追加（json schema は前方互換 — 未知値を捨てる consumer は無い）。
- V-14 の error は既存 `errors` 配列に流れる。

## §6 items 継承規則

旧 spec §6 をそのまま継承（`target_repo` / `base_branch` / `canary` の継承、`items` の順序 = 実行順、依存フィールド無し、暗黙の大域既定値無し）。

## §7 受け入れ条件（新規 A-30〜A-40）

旧 A-1〜A-29 を継承したうえで、以下を追加する。

- **A-30** `spec/design/verify.py` は bootstrap 形の pin（`mode: bootstrap`）を、valid case で `NO-PIN(BOOTSTRAP)`、`pinned_at` / `pinned_by` 欠落で `NO-PIN(MISSING_FIELD)`、`spec_id` 等 7 field いずれかの混在で `NO-PIN(PROHIBITED_FIELD)` として報告する。
- **A-31** `PIN_REASON_CODES` は **13 要素**である（旧 11 ＋ `BOOTSTRAP` ＋ `PROHIBITED_FIELD`）。V-13 は `OBL-SPEC-PIN` の body に 13 code すべての逐語出現を要求する。
- **A-32**（旧 A-26 の後継 — proceed 側の唯一 code は BOOTSTRAP に潰される） `OBL-SPEC-PIN` の body だけを読んで、13 code すべてについて halt / proceed が一意に決まる。proceed してよいのは `BOOTSTRAP` の 1 code のみ。`ABSENT` は halt である。**回帰防止**: 旧 A-26 は 11 code を対象にし、`ABSENT` を proceed 側に置いていた。
- **A-33**（旧 A-28 の後継 — 停止ターンに worked 形を書かせない） `OBL-SPEC-RECEIPT` の body だけを読んで、`NO-PIN/BOOTSTRAP` で worked-from-message-body 形を書き、他 12 code（`ABSENT` を含む）で halted 形を書く、が一意に決まる。停止したターンに worked 形を書かせる読みが成立しないこと。**回帰防止**: 旧 A-28 は 11 code の下で ABSENT を worked 側の唯一 code としていた。
- **A-34** 本 manifest 自身が V-1〜V-14 を error 0 で通過する（自己適用）。`verify_exempt_ids: ["U-1"]` を宣言する — 本 manifest §2.1 D-38 の議論で親 spec の `U-1` を narrative reference として引用しており、`U-1` は親 spec でも narrative-only（bold / 表定義を持たない）∴ chain walking でも解決しない。他の inherited id（`D-*` / `A-*` / `V-*` / `E-*`）は D-37 の `supersedes` chain walking により親 spec 本文の bold / 表定義から解決される。
- **A-35** 旧 `spec/design/T-design-spec-delivery.md` に対して V-14 を実行すると、外部から `verify_exempt_ids: ["U-1"]` を与えた条件下で error 0（他の全参照が定義子を持つ）。**旧 spec ファイル自体は書き換えない**（D-19 / D-20 継承 — supersede されているが immutable）∴ I-2 の test 入力として旧 spec を扱い、`verify_exempt_ids: ["U-1"]` を external override で与える code path で error 0 を確認する形とする。
- **A-36** I-3 実装後、dispatcher は spec-pin 未対応 thread に対して bootstrap 形の pin を書く。`.mindwire/pin` が dispatch 後に必ず present であることを test で示す。
- **A-37** 統合 item I-3 の PR は `spec/process/obligations.yaml` と、`paths` に列挙された dispatcher 系 file の少なくとも 1 つの**両方**に diff を持つ。片方だけの PR は、`OBL-SPEC-SCOPE-CLOSURE` の per-path done/not-done readback で "not-done" が現れる ∴ 実装者は停止して amendment を求める。人手が readback を信じて片側 merge した場合の fail-safe: (a) obligations だけ更新なら次 dispatch で `NO-PIN(ABSENT)` halt、(b) dispatcher だけ更新なら次 dispatch で旧 body が bootstrap 形 pin を `NO-PIN(MISSING_FIELD)` として halt。両者とも loop の生死で観測される — silent breakage ではない。ただし **A-37 は事後観測ではなく事前 gate** として書かれる（loop halt そのものを予防するため）。
- **A-38** V-14 が本 `spec/design/T-spec-pin-hardening-and-id-audit.md` に対し error 0 で通過することが、I-2 実装 PR の test で確認できる（D-37 の `supersedes` chain walking と `verify_exempt_ids: ["U-1"]` を前提とする）。
- **A-39** 本 manifest は `SPEC-2026-08-11-design-spec-delivery` を `supersedes` に持つ。V-4 が緑（実在 spec_id を指し、self-reference でない）。V-10 は `main` 上で旧 spec を「superseded」の warning として報告するようになる（旧 V-10 挙動）— exit code は 0 のまま（D-36）。
- **A-40** `mode: bootstrap` ＋ §3-A 禁止 7 field のいずれかを含む pin を置くと `verify.py` が `NO-PIN(PROHIBITED_FIELD)` を報告する（PROHIBITED_FIELD の到達経路が唯一 bootstrap 用 field 混在であることを test で示す — 回帰防止）。

## §8 運用（順序 = items 列挙順 — D-15 継承）

- **I-1 は bootstrap ABSENT ターンである**（旧 D-12 と同型）。**旧** §4-1 body の下で `ABSENT` は sanctioned proceed である ∴ message body から作業する。receipt は旧 `SPEC (pin: NO-PIN/ABSENT) — worked from message body only`（旧 A-28 の下では正当）。**本 spec の I-3 が land した瞬間から**、`ABSENT` は halt に転じる（新 A-32 / A-33）。
- **I-2 と I-3 の間に順序拘束は無い**（`verify.py` は診断のみで runtime loop の判定機構ではない — D-36 継承 = 旧 D-10 の帰結）。I-2 が先か I-3 が先かは loop の生死に影響しない。診断出力の drift は起きうるが、agent の pin 解決は body 側の enumeration に従うため halt しない。
- **I-3 は atomic cutover である**（D-39）。単一 PR / 単一 merge で dispatcher と body が同時に変わる。merge の瞬間以前は「dispatcher 未変更 ＋ 旧 body」（現行と同じで loop 生存）、merge の瞬間以後は「dispatcher 変更後 ＋ 新 body」（BOOTSTRAP 経路で loop 生存）。**中間状態は git の merge 原子性により存在しない**。以降、pin 機構は「常に検出し、常に阻止する」形に硬化する — 旧 §4-2 の annunciator は補償統制から**主機構**に格上げされる。
- **daemon 再読込に関する運用注記（非規範）**: obligations manifest が daemon 起動時 cache されている場合、I-3 merge 後に daemon 再起動が要る可能性がある。これは deployment 手順であり本 spec の規範ではない ∴ human の運用判断で処理する（機構で強制できないものは規律で担う — 旧 D-11 と同型）。
- **本 spec 自身が第一号実運用対象** — 旧 D-13 の `T-pr-gate-adr-index-scope` より本 spec の適用が先になる（本 spec が supersede する側であり cutover を含むため）。

## §9 明示的に採らないもの（再提案されないための記録）

- **message body に bootstrap-override フラグを置く案**（msg-1620 で却下、msg-1621 で記録）: 信頼の錨を、本機構が不信としている当の経路（message body）に打つことになり、フラグは何とも照合できない（unverifiable）∴ D-33 の物証と同費用でより弱い保証しか買えない。
- **`schema_version` を 2 に上げる案**: 旧 verify.py が新形式を PARSE_ERROR / SCHEMA_VERSION で halt する（fail-closed に正しく halt する）が、既存 resolved pin との相互運用を無意味に破る。`mode` フィールド追加で同じ効果が得られる（旧 verify.py は `mode: bootstrap` の pin を MISSING_FIELD で halt する — これも正しい fail-closed）。∴ 版上げは不要。
- **`ABSENT` を warning 化する soft-cutover 案**: 「1 度だけ warning、次から error」等は Principle 2（二重管理）を招く ∴ 採らない。cutover は I-3 の atomic merge で行う。
- **soft-cutover 案（interim body が `ABSENT` と `BOOTSTRAP` を両方 sanctioned とする）**: body の版数が 3 版になり、interim 版は本 spec の目的を一時的に自己否定する文言を含む ∴ Principle 2 の重管理コストが atomic PR の diff 幅より大きい。
- **V-14 で narrative 定義を自動検出する案**: pattern matching に依存する ∴ 偽陽性 / 偽陰性の resistance が弱く、規則が読み手に見えない。front-matter の明示 `verify_exempt_ids` にすると、免除の意思決定が manifest に localize され、レビュー可能になる（D-38）。
- **inherited id を V-14 の exempt list に全数列挙する案**: `supersedes` chain walking より脆い（親が改訂されると exempt list の追随が要り、Principle 2 に触れる）∴ chain walking で解決し、chain walking でも解決しない narrative-only id（`U-1` のみ）だけを明示 exempt する（D-37 / D-38）。
- **id パターンから境界を落とす案（r3 で採っていた形）**: `SPEC-2026-...` / `ADR-2026-...` から `SPEC-2026` / `ADR-2026` が false-positive に抽出される ∴ V-14 が main 上で恒久 error を出す（D-21 違反）。負の lookaround で境界を持たせる（D-37）。
- **`OBL-SPEC-PIN` body に「pin schema の詳細」を追加する案**: 手順の正本は §3 であり、body は結果に対する挙動を決めるだけである（旧 D-31 継承）。BOOTSTRAP / PROHIBITED_FIELD は「結果」であり、body に code の綴りと behavior を書けば足る。
- **items 依存グラフ／DAG／`verify.py` に順序検査を持たせる案**: 旧 D-15 継承 — 順序の SOT は `items` の列挙順である。本 spec も同規則に従う。

## §10 検査されていないもの（過大申告しない — 旧 §2.1 継承）

旧 §2.1 の 4 項目（チャット msg のテキスト転記忠実性 ／ ADR 本文 ／ spec が世界について述べた主張の真偽 ／ ブートストラップ窓の内外区別）を継承する。ブートストラップ窓の内外区別は本 spec で D-33 の物証によって**閉じる** — 4 項目のうち 1 項目が消える。残り 3 項目は継続する。
