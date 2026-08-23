# S5 判断材料 push — spec (mindwire 側)

判断ページ (magickit 側 `T-decision-page` 増分 3、`spirrow-magickit`
repo の `spec/slices/S5-decision-materials.md`) が用意した material 受け口
に対し、mindwire の composer が生成した材料 (問い / 選択肢 / 得るもの /
失うもの / 推奨 / 未確定事項) を **PUT で運ぶ**。契約 (URL 形式、body の
field 表、`composer_status != "ok"` の扱い、鮮度判定に `head_msg_id` を
使うこと) の SOT は **magickit repo の `spec/slices/S5-decision-materials.md`**。
本 file はそこに書き写さず参照する — 契約は 1 箇所に置き、両側が読む。

本 spec は本スレッド (`spirrow-mindwire / T-decision-material-push`) の
Bohr msg-1443 §3・§4 (Tier-C 承認済の凍結形) と msg-1445 §3・§4・§5・§6
を SOT として書き下ろしたもの。議論のログ (msg-1443 … msg-1446) は決定の
場、この file は仕様の場。以降の実装は本 file の要件で判定する。

## 0. スコープ / 非スコープ

**スコープ (この増分)**

- 稼働中 conductor (`deploy/run-conductor-scheduled.ps1`) から magickit の
  `PUT /v1/decisions/{project}/{thread_id}/material` へ材料を送る配線。
- 送信は **`NEXT: human` 系駐機ごとに 1 回**（同一 signature の再 tick で
  再送しない — DM-3）。
- 送信は**通知の前**に行い、送信の成否に**通知本文を分岐させない** (D-34)。
- Python 側 CLI (`mindwire-compose-decision`) は composer が実際に読んだ
  head msg id を `extras.head_msg_id_read` に**記録するだけ**。読み取り
  失敗時はキーを書かない (I-16 / DM-4)。
- 送信先 URL と Discord 通知本文中のダッシュボードリンクは、**同一の base
  URL と同一の {project}/{thread_id} エンコード**から組む (I-17 / DM-2)。

**非スコープ**

- 材料の受け口実装 (magickit 側 S5''、別 repo・別 PR で完了済)。
- 送信失敗時の再試行キュー、部分状態トラッカ — **明示的に作らない**
  (D-34 / YAGNI)。同一 signature の再試行は DM-3 の gate で 1 回に制限
  される。
- 認証・認可の配線 (D-33: 現時点で PUT 面は未認証。tailnet 限定を防壁と
  する)。
- ダッシュボード側の描画・3 状態切替 (magickit S5'' の責務)。
- 問いの文章を平易にする作業 (`T-decision-request-composer` msg-1439)。

## 1. 凍結された決定 (Tier-C 承認 msg-1443 §3・§4)

以下は本 spec が動く前提であり、本 spec で再設計しない。番号は
msg-1443 のまま (`D-32` は magickit 側 `T-decision-page` に由来、
`D-33` / `D-34` / `I-16` / `I-17` は本スレッドで凍結)。

### D-33 — 無認証は**意図的な選択**である

PUT 面に認証を掛けない。防壁は **tailnet 限定** (`*.taile861db.ts.net`、
公開インターネットには出ていない)。

**失効条件**: `:8443` が tailnet の外に出る変更 (公開 / 別 proxy 経由 /
認証境界の変更) が入った時点で本判断は無効。**そのとき測り直し、この
spec を改訂する。**

**未解決 (§7)**: 本失効条件を「失効させる変更をする人が読む場所」に
置けていない。**infra 側の記述への到達手段が本 repo から無い**ため。
本 spec (= mindwire の spec slice) は infra を変える人が必ずしも読む
場所ではない ∴ 不十分。**この未解決を隠さない** — 本 spec に記載する
ことで、少なくとも「未解決である」ことは記録される。

### D-34 — PUT は通知の前。**通知を PUT に依存させない**

順序は ① 材料を PUT → ② Discord 通知。**PUT が失敗 (4xx / 5xx / timeout
/ 到達不能) しても通知は出す**。通知本文は分岐させない (リンクの契約を
変えない)。

理由: 逆にすると**材料経路の障害が依頼経路の障害になる**。通知は「人が
呼ばれていることを人が知る唯一の手段」であり、それを別サービスの生死に
賭けると**誰も呼ばれないまま人待ちで止まる**。**無価値な画面は、届かない
依頼より良い**。

**Einstein msg-134 §3 の凍結形**:

> **一時的な PUT 失敗は、そのターンについて恒久的な「材料なし」表示を
> 意味する。** 通知済みフラグが立つため PUT は再試行されない ∴ 人は
> リンクを踏んで「判断材料が用意されていません」を見続ける。
> **再試行キューも部分状態トラッカも作らない** (YAGNI)。「材料なし」
> 表示はこの場合の**設計された安全側の挙動**である。人は chatroom を
> 読みに行くことで回復する。

### I-16 — `head_msg_id` は **composer が実際に読んだ head**

材料を組むために読んだスレッドの head を**そのまま**載せる。**PUT の
直前に読み直して上書きしない**。

理由: 読み直すと「材料は古いが head は新しい」を**新鮮だと主張する**
ことになり、受け側の「読めなければ古い扱いに倒す」既定を無効化する。
**受け側はこの違反を検出できない** (composer が何を読んだかを知らない)
∴ **この不変条件は送る側にしか置けず、送る側の test で pin するしか
ない**。

### I-17 — リンクと PUT は**同じ値・同じ base** から組む

通知リンクの `{project}/{thread_id}` と PUT 先の `{project}/{thread_id}`
は**同一の変数から**組む。base URL も 1 箇所に持つ。

## 2. 本 spec で新規に凍結する決定 (DM-1〜DM-6)

`DM-` = decision material push。本スレッドの決定に付与する番号 (mindwire
repo の D 系列 D-35〜D-45 と衝突しないため、Bohr msg-1445 §0 で
`DM-n` を採用)。

### DM-1 — PUT は wrapper (PowerShell) に置く。Python CLI には置かない

**理由**:

1. I-17 の base 一本化は `$DecisionDashboardBaseUrl` が既に wrapper 側に
   ある — CLI に置くと env を 2 箇所で読む (= 分岐の芽)。
2. CLI は module docstring で「a pure stateless converter」と宣言され、
   単体テストが素で叩く ∴ 副作用のあるネットワーク I/O を足すと性質が
   壊れる。
3. cache-hit tick (CLI が走らない) でも PUT の可否を制御したいのは
   wrapper 側 (`Test-NotificationSuppressed` gate に相乗り、DM-3)。

### DM-2 — URL 組み立ては 1 関数に集約する (I-17 の実行形)

`New-DecisionLink -Project -ThreadId` (Discord に載せる人向けリンク) と
`New-MaterialUrl -Project -ThreadId` (magickit PUT の宛先) を新設し、
**両方が同一の `$DecisionDashboardBaseUrl` と同一の `EscapeDataString`
呼び出しから組む**。`Format-DecisionMessage` はリンクを**自前組み立て
せず `New-DecisionLink` を呼ぶ**。「同じ場所に 2 つ目の独立した組み立て
を足さない」を、コメントではなく関数の単一性で担保する。

### DM-3 — PUT の gate は通知の gate と**同一**にする

`Send-NotificationIfChanged` の内部述語を `Test-NotificationSuppressed
-State -Key -Signature` として抽出し、**呼び出し側と
`Send-NotificationIfChanged` の両方が同じ 1 つの述語を読む** (述語の
二重記述を作らない)。

これは D-34 の明文「**通知済みフラグが立つため PUT は再試行されない**」
を**真にする**ための配置である。gate を掛けずに置くと、駐機中スレッド
に対して 5 分ごとに PUT が飛び続け、**設計されていない再試行**
(凍結形が YAGNI として明示的に拒否したもの) が事故として実装される。

### DM-4 — `head_msg_id` は composer が読んだ head。無ければ送らない

**CLI 側** (`src/spirrow_mindwire/decision_request/cli.py`):

- `--tail N` の tail fetch 成功枝で、`fetch_extras["head_msg_id_read"]
  = fetched_tail[-1].msg_id` を書く。ただし `fetched_tail` が空タプル
  なら**キーを書かない** (Einstein msg-1446 §1 の IndexError 防衛)。
- 空タプル / fetch 失敗時は**キーを書かない** — 「読めなかった」を
  「読めた」と偽装するより「送らない」を選ぶ。

**wrapper 側** (`deploy/run-conductor-scheduled.ps1`):

- `Get-ComposerReadHead -Envelope` で `envelope.extras.head_msg_id_read`
  を読む。**`envelope.last_msg_id` へフォールバックしない** (それは
  conductor の停止行であり、composer が読んだものではない)。
- キー無し / 空値なら **PUT を行わない** — ページは J-absent。「読んで
  いないのに head を主張する」より安全側であり、D-34 が既に「設計され
  た安全側の挙動」と認めた表示である。**新しい UI 状態も再試行も作らない**。

### DM-5 — timeout 10 秒。失敗は必ず 1 行残す。通知は PUT の結果を一切参照しない

- `Invoke-MaterialPut` は **`-TimeoutSec 10`**。根拠: M-1 実測 RTT
  186〜215ms (§6)、通知 30s / composer 60s / tick 5min に対し十分小さい。
  **2000ms を超える計測が出たら止めて報告** (上限の前提が誤り)。
- 呼び出し側 (`Push-DecisionMaterial`) は **PUT の戻り値で分岐しない**。
- `Push-DecisionMaterial` は**例外を外に出さない** (内部で catch)。
  `Invoke-MaterialPut` も `catch` で全例外を `@{ ok=$false; ... }` に
  変換する — 二重の belt-and-braces (D-34 の fail-open は物理的に一つの
  try-catch で保証される)。
- 通知本文は**分岐させない** (リンクの契約を変えない)。

**ログ文言 (逐語で固定する)**:

| 状態 | 逐語 |
|---|---|
| 成功 | `material push: {key} head={head} replaced={true\|false} ({ms} ms) url={materialUrl} link={dashboardLink}` |
| HTTP 失敗 | `material push FAILED (non-fatal): {key} head={head} — HTTP {status} {body の先頭 120 文字} — 通知は継続` |
| 例外 | `material push FAILED (non-fatal): {key} head={head} — {型}: {message} — 通知は継続` |
| head 不明 | `material push skipped: {key} — composer が読んだ head が不明 (extras.head_msg_id_read 無し) ∴ 材料を送らない (ページは J-absent)` |
| 非 ok | `material push skipped: {key} — composer_status={status} ∴ 材料なし` |
| output 無し (DM-6 second half) | `material push skipped: {key} — envelope に output が無い (composer_status=ok だが output=null) ∴ 材料を送らない` |

**失敗枝でも `Confirm-LogWorthKeeping` を明示的に呼ぶ**。呼ばないと、
バッファされたまま捨てられる tick があり得る (= 沈黙)。本ループが
過去繰り返し踏んだ「exit 0 でログも書かず静かに壊れている」型を、
ここで再輸入しない。

成功行に **`link=` (= `New-DecisionLink` の出力そのもの) を載せる**のは
A-19 の実行可能性のためである (§5)。**secret は 1 つも載らない**
(webhook はこの経路に渡さない)。

### DM-6 — `composer_status != "ok"` / `output` 無しには PUT しない

magickit S5 §1.3 により受け側は 400 で弾き部分保存もしない ∴ 送っても
保存されない。送らないのは受け側の検査を弱めるためではなく (**受け側
の検査は残す — それが §1.3 の趣旨**)、無意味な 400 を毎回作らないため。

## 3. 実装配置

| 何 | どこ |
|---|---|
| Python CLI: `extras.head_msg_id_read` を書く | `src/spirrow_mindwire/decision_request/cli.py` の tail fetch 成功枝 (`_run_tail_fetch` 直後) |
| wrapper: `New-DecisionLink` / `New-MaterialUrl` / `Get-ComposerReadHead` / `Invoke-MaterialPut` / `Push-DecisionMaterial` | `deploy/run-conductor-scheduled.ps1`、`$DecisionDashboardBaseUrl` 定義の直下 |
| wrapper: `Test-NotificationSuppressed` (通知 gate の述語を抽出) | 同 file、`Send-NotificationIfChanged` の直前 |
| wrapper: `Send-HumanParkAlert` (§DM-1〜§DM-6 の順序と fail-open を保持する関数) | 同 file、`Format-DecisionMessage` の直後 |
| sweep tick 内の呼び出し | 同 file、`if ($verdict.reason -and $needsHuman.ContainsKey($verdict.reason))` 節を `Send-HumanParkAlert` の 1 呼び出しに置換 |
| Python テスト (I-16 pin) | `tests/test_decision_request.py::TestCliClaudeCodeBackend::test_head_msg_id_read_*` |
| PowerShell テスト (順序 / fail-open / gate / 非 ok / I-17 の pin) | `tests/Test-DecisionComposerWiring.ps1` の `--- T-decision-material-push ---` セクション以下 |

## 4. wire measurements (M-1〜M-3、msg-1445 §6)

**M-1 (実装前・sg-tomtebo-01 上・pwsh から実測)**:

| 手段 | 結果 | 所要 |
|---|---|---|
| 直 (`Invoke-WebRequest` に `-Proxy` 無し / `-SkipCertificateCheck` 無し) | **status=404** (`{"error_type":"MaterialNotStored", ...}`) | 186〜215 ms (3 回計測) |
| 経由 (`-Proxy http://127.0.0.1:3128`) | HTTP 403 (squid が tunnel を拒否) | 165 ms |

∴ **PUT は squid を経由させない**。tailnet 直接の TLS で通る (証明書は
Tailscale が発行する tailnet-issued cert ∴ 検証は default で通る)。

DM-5 の `-TimeoutSec 10` は実測 RTT の **~50 倍**の余裕。

**M-2**: `deploy/sync-repo.ps1` は `HEAD == 'main'` のときに
`origin/main` を fast-forward する ∴ **稼働 checkout は `main`**。
本増分の land 先も `main` (別ブランチに merge しても稼働は取りに行かない)。

**M-3**: sg-tomtebo-01 上で
`[Environment]::GetEnvironmentVariable('MINDWIRE_DECISION_COMPOSER_BACKEND', 'User')`
は `claude-code`。稼働ログ (`~/spirrow-mindwire-data/logs/conductor-2026-08-23.log`)
にも `composer fire: ... status=ok` の行が複数存在 ∴ **稼働 backend は
`claude-code`**、`stub` ではない。A-19 の実測は stub の固定文字列では
なく本物の composer 出力を測ることになる。

## 5. 受入基準 A-19〜A-22 — **人のタップを 1 回も要求しない**

実タップは増やせない資源 ∴ 全項目がループ側で実行可能に設計する。人の
タップは `T-decision-page` の A-14〜A-18 に温存する。

| 受入 | 実行者 | 手順 |
|---|---|---|
| **A-20 (稼働側で出たこと)** | ループ | land + sync 後、**スケジュールタスク自身**が駐機スレッドに対して PUT するのを待つ。手動 sweep で作らない。証拠 = 当該 tick の `material push:` ログ行 (timestamp が tick のもの) + `pending-decisions.json` の row |
| **A-19 (readback)** | ループ | A-20 のログ行から `link=` の値 (= `New-DecisionLink` の出力そのもの、人が Discord で踏むリンクと**同一構築**の文字列、DM-2) を取り、そこから `{project}` / `{thread_id}` を**取り出して** `GET /v1/decisions/{project}/{thread_id}/material` する。**手で組んだ curl で `{project}` を打ち込まない**。返った body を `pending-decisions.json` の `envelope.output` と **field 単位で比較** (`question` / `options[].{id,label,gain,loss}` / `recommendation` / `recommendation_reason` / `unknowns` / `head_msg_id == extras.head_msg_id_read`)。**200 では不合格、一致で合格** |
| **A-22 (「古い材料」表示の live 確認、curl 1 回)** | ループ | A-19 の後、本スレッドが 1 通進んだ状態で `GET /dashboard/decisions/{project}/{thread_id}` を取得。**警告 1 行が出ること** ∧ 投入した `question` / 各 `label` / `gain` / `loss` の文字列が HTML に **1 文字も無いこと** (A-19 で読み戻した実文字列で検索する) |
| **A-21 (劣化経路の実測)** | ループ | **稼働設定・稼働コードを書き換えない**。単発の pwsh セッションで `Send-HumanParkAlert` を lift し、**`$DecisionDashboardBaseUrl` をその呼び出しにだけ `http://127.0.0.1:9` (誰も listen していない) に上書き**して 1 回実行する。state は**その場のハッシュテーブル** (`notified.json` / `pending-decisions.json` を触らない ∴ 実スレッドの通知抑止を汚染しない)。実 `Invoke-MaterialPut` と実 `Send-Notification` を使う。合格 = **PUT が実際に失敗し、Discord 通知が飛ぶ** (`material push FAILED` と `notification sent` が同一実行のログに並ぶ)。飛んだ 1 通は A-21 の成果物である旨を本スレッドに記す |

**A-21 の弱点 (Bohr msg-1445 §6 末尾 self-report)**: これは「sweep 全体」
ではなく「切り出した関数」の実行である。sweep 全体でやるなら temp な
`MINDWIRE_PATHS__DATA_DIR` に config 一式を用意する必要があり、**用意
した config が稼働と違えば測っているものが違う**。「実コード経路 + 実
HTTP + 実 webhook + state 汚染ゼロ」を優先して関数実行を選んだ。

## 6. 開発者テスト (自動化・ゲート内で走る)

- **I-16 (CLI 側)** — `tests/test_decision_request.py::TestCliClaudeCodeBackend`:
  - `test_head_msg_id_read_is_the_last_fetched_msg_id` — 成功枝で末尾 msg_id が
    `extras.head_msg_id_read` に入ること (payload の `last_msg_id` とは
    独立)。
  - `test_head_msg_id_read_absent_when_fetch_fails` — fetch 例外時に
    キーが**存在しない**こと (present-but-empty ではなく absent)。
  - `test_head_msg_id_read_absent_when_fetch_returns_empty_tuple` —
    Einstein msg-1446 §1 の IndexError 防衛：空タプルでも CLI が
    クラッシュしないこと、かつキーは書かないこと。

- **順序 / fail-open / gate 同一 / 非 ok / I-17 (wrapper 側)** —
  `tests/Test-DecisionComposerWiring.ps1` の
  `--- T-decision-material-push ---` セクション:
  - (a) 順序 — `Push-DecisionMaterial` の呼び出しが `Send-Notification`
    より**前**であることを call log で確認。
  - (b) fail-open — `Invoke-MaterialPut` が例外 / HTTP 500 を返しても、
    通知本文が **PUT 成功時と 1 文字も違わない**こと。
  - (c) gate 同一 — 同一 signature の 2 回目で **PUT も通知も発火しない**
    こと (DM-3 = D-34 の「再試行されない」を真にする)。
  - (d) 非 ok — `composer_status != "ok"` で PUT が呼ばれず、`raw ping`
    が飛ぶこと (DM-6)。
  - (e) I-17 — PUT 先 URL と通知本文中リンクの `{project}` / `{thread_id}`
    が**同一文字列**であること (fake が受け取った URL と本文を突き合わ
    せる)。

- **`New-DecisionLink` / `New-MaterialUrl` の等価性 (DM-2 / I-17)** —
  同 file、同じ base URL と percent-encoding を使うこと。

- **`Get-ComposerReadHead` の duck-typing** — hashtable / PSCustomObject
  の両形でキー抽出が動くこと、キー無しで `$null`。

## 7. 未解決 (この spec で片付けない)

1. **D-33 の失効条件を「失効させる人が読む場所」に置く手段** —
   **解けていない**。infra 側の記述への到達手段が本 repo から無い。
   ここに書くのは記録であって解決ではない。infra を変える人がこの spec
   を読む導線が作れた時点で解ける。それまでは、本項目が「知られた
   未解決」として存在する。

2. **PUT 失敗の digest 反映** — 凍結形に無い ∴ **スコープ外**
   (ログのみ)。足すなら別増分。

3. **A-21 の弱点** — §5 記載。sweep 全体を「実 config で / 稼働 state
   を汚染せず」測る手段があれば、そちらのほうが良い。現時点では関数
   実行が最良の trade-off であり、この trade-off 自体は naysayer
   (Einstein msg-1446 §3) が endorse 済。

## 8. 教訓 — 一般則

1. **受け側が検出できない不変条件は、送る側の test で pin する** (I-16)。
   受け口の検査を弱めるためではなく、送り側の裁量を制限するため。
2. **fail-open は物理的な barrier で保証する** (D-34 → DM-5)。
   「呼び出し側は分岐しない」を規約で言うだけでは、後任者の一行の
   `if` が破る。関数を分けて try-catch を置く。
3. **測る前に塞がない** (D-33)。認証・認可・稀な失敗経路。実際に叩いて
   壊れることを見てから対処を設計する。同時に、**測ってから塞ぐ人が
   いつ現れるかを制御できない前提**を書き残す (§7-1)。
4. **失効条件を書けない未解決を「解けた」ふりをしない** (§7-1)。書けない
   ことをそのまま書くのは、後日 spec を読む人に「ここには手が付いて
   いない」を正しく伝える唯一の方法。
