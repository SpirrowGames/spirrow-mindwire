---
spec_id: SPEC-2026-08-11-denial-detail-and-overdeny
thread: T-denial-detail-and-overdeny
target_repo: spirrow-mindwire
base_branch: main
status: active
supersedes: []
---

# 隔離時の denial detail を可視化し、over-deny の疑いを判別可能にする

## §0 この文書の読み方

本文は自己完結している。読む側が thread T-denial-detail-and-overdeny の過去メッセージを復元する必要はなく、また復元できることを前提にしてもいけない（E-1 参照）。**本文に無いものは、この仕様の一部ではない。**

本 spec は **観測 (PR-1)** に閉じており、classifier のロジック（`_RAW_COARSE` / 構造分類器 / allow-list Tier 分類）には**触れない**。それは「測る前に直さない」の適用であり、PR-1 は測定材料を揃えるためだけの変更である。分類のロジックを直すかどうかは、本 PR で得たデータをもとに別 PR（PR-2 以降）で議論する。

論拠と検討経緯は thread に残る。本文は **決定項目（id 付き）・スキーマ・受け入れ条件** に限る。

### §0.1 implementer が読めなかったもの（OBL-DECLARE-UNREADABLE）

- 起票 msg・proposer 検討 msg・naysayer レビュー msg（本 spec を書いた implementer は該当 thread の**最新 human 発話 1 本のみ**を payload として受け取っており、それ以前のスレッド履歴のバイト列を持っていない）。∴ 本 spec は human の最終判断メッセージ（本 spec を含む PR-1 を承認したもの）と、実装コード（`src/spirrow_mindwire/adapters/implementer.py` / `src/spirrow_mindwire/allowlist.py` / `src/spirrow_mindwire/adapters/implementer_allowlist.yaml`）から**再構成された** design である。要件表（§7）は human 最終判断の逐語引用に限って組み立てる。
- ADR の本体テキスト（title のみが implementer に見える）。∴ 本 spec は ADR に「これを要求している」等の断定を書かない。

## §1 前提（実測値。推測で補わないこと）

以下は 2026-08-11 時点の実測である。本仕様はこれらに依存する。実装時に事実が違っていた場合、仕様ではなく事実が正しい。停止して報告せよ。

| id | 事実 | 出典 / 計測者 |
|---|---|---|
| E-1 | implementer に渡るのは新着 msg 1 本の body のみ。スレッド履歴は渡らない | watcher 実装 — human (T-design-spec-delivery E-5) |
| E-2 | 本 spec 起票以前、denial 発生時に session.error.message に入るのは Tier C `forbidden.reason` の**文字列だけ**（例: 「ファイル削除は Tier C (不可逆)。」）で、**何のコマンドで denial が起きたのか**は残らない | `src/spirrow_mindwire/adapters/implementer.py` deliver_event 分岐（`session.error = ErrorInfo(code="adapter.allowlist_violation", message=violation.decision.reason, ...)`）— implementer |
| E-3 | `_INDIRECTION_RE` は `\b(?:bash\|sh\|zsh\|dash)\b(?:\s+\S+)*?\s+-\w*c\b\|\beval\b\|\$\(\|`` で、`$(` と backtick は**単独で** coarse floor の gate を開ける | `src/spirrow_mindwire/adapters/implementer.py` の `_INDIRECTION_RE` 定義（逐語）— human |
| E-4 | coarse floor `_RAW_COARSE` の FS_DELETE パターンは `\b(?:rm\|rmdir\|shred\|unlink)\b\|-delete\b\|\bRemove-Item\b` を含む。∴ PowerShell 本文中の `Remove-Item` は raw string scan で FS_DELETE として拾える | 同 file の `_RAW_COARSE` 定義（逐語）— implementer |
| E-5 | 実際に隔離されたセッションのファイル本文 (`tests/Test-SweepQuarantine.ps1`) には `$(` / backtick を含む行が 65、`Remove-Item` を含む行が 5 存在する。∴ 「`$(` を含む PowerShell 本文を 1 つの heredoc で書き込むコマンド」は `_INDIRECTION_RE` と FS_DELETE パターンを**同時に**満たす | halt したセッションが編集していたファイルの実測 — human |
| E-6 | 発行されたコマンドが Bash heredoc だったのか `Write` / `Edit` ツール経由だったのかは、`detail` を記録しない現状の denial 出力からは**判別できない**（`Write`/`Edit` は Bash 分類器を通らず `FS_WRITE` に落ちる ∴ そもそも FS_DELETE 判定にならない — 発生していたなら別欠陥である） | 同 file の `classify_tool_call` 分岐（Bash と Write/Edit で経路が分かれる）— implementer |

**未検証（前提に使ってはならない項目）**

| id | 未検証事項 | 扱い |
|---|---|---|
| U-1 | 過去に発生した T-human-terminal-overuse ほか **5 件** の denial の**実際の tool_name と command**。`session.error.message` に残っていない以上、遡って判別する材料が現状は無い | 本 spec の実装が着地すれば、以後は判別可能になる。過去分は不明のまま残ることを受け入れる（本 spec は前向きの観測を作るだけであって、過去観測を復元しない） |

## §2 決定

- **D-1（PR-1 は観測に閉じる）** 本 spec は denial 発生時の観測を可能にするための変更に閉じる。classifier のロジック（`_RAW_COARSE` / 構造分類器 / Tier 分類 / `_INDIRECTION_RE`）を変更しない。allow-list YAML（`implementer_allowlist.yaml`）を変更しない。`obligations.yaml` に節を追加しない — **回避策を SOT に恒久刻印しない**（直った後も残り、なぜあるか分からないまま次の読み手を縛るため）。

- **D-2（denial reason に detail を含める）** allow-list の deny が発生したとき、`AllowlistDecision.reason` に**構造化された detail** を付与する。detail は少なくとも次を含む:
  1. 元のコマンド文字列（Bash 系ツールの場合。§3 のスキーマに従う）
  2. verdict の由来（`corroborated` フィールド。§3 参照）
  3. `_scan_raw_coarse` が触れたパターンとその match span（該当時のみ）
  4. heredoc 本文の位置一覧（該当時のみ）

- **D-3（heredoc 本文位置の記録）** raw command 中に bash heredoc が含まれる場合、その本文（delimiter 開き〜閉じ delimiter の直前まで）の (start, end) を記録する。ここは detection のみで、**分類判断を変えない**（本文中の Tier C verb を無視する等の変更は本 spec の scope 外）。

- **D-4（corroborated は "unknown" を許容する）** `corroborated` は enum 値であり、bool ではない。値は `"structural_and_coarse"` / `"structural_only"` / `"coarse_only"` / `"unknown"` の 4 値。**PR-1 では fail-closed 写像を導入しない** — `"unknown"` の出現率を測ってから PR-3 で導入判断を行う。

- **D-5（秘匿の最低限）** 既知形状の token（GitHub PAT / OAuth / JWT / AWS access key）を detail の出力に混入させない。列挙は必ず漏れるが、`detail` を丸ごと捨てる現状より厳密に良い。**漏れうることを PR 本文の残余に書く**。

- **D-6（fail-safe on redaction failure）** redaction ロジックが例外を投げた場合、detail は**全体を捨てる**（fail-closed）。redaction が例外で落ちて生の command 文字列が漏れる、という失敗形は許容しない。

- **D-7（後方互換）** `ClassifiedAction` の追加フィールドはすべて default 値付きの optional。既存の呼び出し（テスト・他アダプタ）は変更不要である。

- **D-8（scope 外の明記）** 以下は本 spec の scope **外** である:
  - classifier のロジック変更（`_RAW_COARSE` / `_INDIRECTION_RE` / 構造分類器）
  - allow-list YAML の変更（`implementer_allowlist.yaml`）
  - `obligations.yaml` への節追加
  - 過去の denial の詳細復元
  - `corroborated == "unknown"` の fail-closed 写像

## §3 スキーマ

### §3.1 `ClassifiedAction` の拡張

`src/spirrow_mindwire/allowlist.py` の `ClassifiedAction` に次のフィールドを追加する（すべて default 付き optional）:

| field | type | 意味 |
|---|---|---|
| `raw_command` | `str \| None` | Bash 系ツール由来なら raw command 文字列。他ツールは `None` |
| `heredoc_bodies` | `tuple[tuple[int, int], ...]` | raw_command 中の heredoc 本文の (start, end) 一覧。半開区間 `[start, end)` |
| `match_span` | `tuple[int, int] \| None` | coarse floor が Tier C 判定を出したときの match の span。他は `None` |
| `corroborated` | `str` | §3.2 の 4 値 enum。default は `"unknown"` |

追加はすべて後方互換（default 値あり）。

### §3.2 `corroborated` の値

| value | 意味 |
|---|---|
| `"structural_and_coarse"` | 構造分類器の verdict と `_scan_raw_coarse` の verdict が**同じ Tier C operation** で一致した |
| `"structural_only"` | 構造分類器のみが Tier C を出した（coarse は該当なし、または `_INDIRECTION_RE` gate が開かず coarse が走らなかった） |
| `"coarse_only"` | coarse floor のみが Tier C を出した（構造分類器の verdict は EXEC_CODE 等の非 Tier C） |
| `"unknown"` | Bash 系以外のツール、または Bash 系だが判定が不能／未測定 |

**注**: `corroborated` はあくまで観測ラベルであり、この値によって allow/deny の判定が変わることは PR-1 の範囲では**ない**（D-4）。

### §3.3 `AllowlistDecision.reason` の形式

deny 時、`reason` は次の 2 段構成にする:

```
<既存の Tier C reason 文字列（例: 「ファイル削除は Tier C (不可逆)。」）>
detail: op=<operation> corroborated=<value> [match=<start>..<end>] [heredoc=<h1_start>..<h1_end>,<h2_start>..<h2_end>,...]
command: <redacted raw command, or "(non-bash tool: <tool_name>)">
```

第 2 行目・第 3 行目は本 spec で新設。redaction は §3.4 に従う。

### §3.4 秘匿 (T1)

`detail` に含める前に、少なくとも次のパターンを `<REDACTED:<kind>>` に置換する（Python `re` の raw pattern、逐語）:

| kind | pattern |
|---|---|
| `github_pat` | `ghp_[A-Za-z0-9]{36,}` |
| `github_fine_pat` | `github_pat_[A-Za-z0-9_]{22,}` |
| `slack` | `xox[baprs]-[A-Za-z0-9-]{10,}` |
| `jwt` | `eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+` |
| `aws_access_key` | `AKIA[0-9A-Z]{16}` |

redaction 実行中に例外が発生した場合、detail 全体を落として `command: (redacted; redaction error)` とする（D-6）。

## §4 受け入れ条件

- **AC1（heredoc body 内かが読める）** raw command 中に heredoc が含まれ、かつ Tier C 判定の match が heredoc の本文内に落ちる入力に対して、denial reason 文字列から「match の span が heredoc body の span に含まれるか否か」が**機械的に読み取れる**（`match=` と `heredoc=` の値の突き合わせで判定できる）。
- **AC2（PowerShell fixture）** 「`$(` を複数箇所含む PowerShell 本文を bash heredoc（`cat > file.ps1 << 'EOF' ... Remove-Item ... EOF`）で書き込む」入力に対して:
  - `operation is Operation.FS_DELETE`
  - `match_span` が `Remove-Item` の位置を指す
  - `heredoc_bodies` に本文範囲が 1 件記録される
  - `corroborated` は `"structural_and_coarse"` か `"coarse_only"` のいずれか（下記実測メモ参照）
  - denial reason の `command:` 行に raw command（redaction 適用後）が含まれる

  **実測メモ（本 PR-1 の実装で判明）**: 現在の `_BASH_SEP` は `\n` を含むため、構造分類器は heredoc 本文中の行を**兄弟 fragment として扱い**、`Remove-Item ...` 行を独立に分類する ∴ 本 fixture では `corroborated == "structural_and_coarse"` となる。これは「構造分類器も heredoc 境界を越えている」ことを示す観測であり、二択のうち **branch (a)（heredoc 内 verb を Tier C と誤検出）が構造分類器と coarse floor の両方に存在する**という追加事実である。この事実そのものが PR-1 が測定するために存在する情報であり、修正判断は PR-2 以降で行う（D-1）。
- **AC3（後方互換）** 既存のテスト suite は変更なしで通る（`ClassifiedAction` の追加フィールドは default 値付き optional）。
- **AC4（redaction）** raw command の任意位置に `ghp_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX` が含まれる場合、denial reason の `command:` 行にその文字列が**現れない**（`<REDACTED:github_pat>` に置換される）。
- **AC5（redaction fail-safe）** redaction ロジックの内部で例外が発生する状況を注入したテストで、denial reason の `command:` 行が生の command 文字列を漏らさない。
- **AC6（corroborated=unknown）** Bash 系以外のツール（例: `Write`）由来の denial では `corroborated == "unknown"` である。
- **AC7（reason 拡張の下位互換）** 拡張前の reason 文字列（Tier C `forbidden.reason` の逐語）は拡張後の reason 文字列の**先頭に**含まれる（既存の error handling が第 1 行を読んでも壊れない）。

## §5 非スコープ（PR-1 では触れない）

- classifier のロジック変更（`_RAW_COARSE` / `_INDIRECTION_RE` / 構造分類器）
- allow-list YAML（`implementer_allowlist.yaml`）
- `obligations.yaml` への節追加
- 過去の denial の遡及復元
- `corroborated == "unknown"` に対する fail-closed 写像

## §6 実装物（file と行数の概算）

| file | 変更 |
|---|---|
| `src/spirrow_mindwire/allowlist.py` | `ClassifiedAction` に 4 field 追加（default 付き） |
| `src/spirrow_mindwire/adapters/implementer.py` | heredoc 検出関数 / redaction 関数 / `_classify_bash` の match_span & corroborated 記録 / `AllowlistDecision.reason` 拡張ヘルパ |
| `tests/test_implementer_adapter.py` | AC1〜AC7 のテスト追加 |
| `spec/design/T-denial-detail-and-overdeny.md` | 本 spec |

## §7 出口 read-back（要件表）

本 spec の実装完了時、PR 本文に次の表を含める。行は本 spec §2〜§4 の要件を 1 行 1 要件で挙げ、実装ポインタと状態を書く。

| req | 内容 | 状態 | ポインタ |
|---|---|---|---|
| D-1 | classifier ロジック不変 | | |
| D-2 | reason に detail | | |
| D-3 | heredoc 位置記録 | | |
| D-4 | corroborated 4 値 | | |
| D-5 | 秘匿最低限 | | |
| D-6 | redaction fail-safe | | |
| D-7 | 後方互換 | | |
| AC1 | heredoc body 内か機械可読 | | |
| AC2 | PowerShell fixture | | |
| AC3 | 既存 suite pass | | |
| AC4 | ghp_ redaction | | |
| AC5 | redaction fail-safe test | | |
| AC6 | 非 Bash は unknown | | |
| AC7 | reason 先頭に既存文字列 | | |
