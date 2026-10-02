# ADR-2026-05-31-14: ガワ方式撤回 — claude.ai web 外部駆動の ToS 衝突と Claude Code への操作対象転換

- **Status**: Draft（ローカル develop。Takahito GO 後に Drive 反映 = ADR-07 §2.5 / Tier C）
- **Date**: 2026-05-31
- **Scope**: spirrow-mindwire（T15 PoC-H / UI 自動化手段選定。identity 規範定義ではないため CLAUDE.md §M 対象外 = UI 自動化手段 ADR）
- **Author**: Heisenberg (implementer, terminal_coding_agent) — chatroom T-T15-poc-h-phase1-kickoff の trilateral decide (Bohr proposer) を反映
- **Relates to**: 2026-05-27-08（旧採番。T15 ガワ方式の §M 参照名。本 ADR が実体化し置換）、ADR-2026-05-31-15（independence-class グラデーション。別経路 naysayer の規範根拠）
- **Supersedes**: T15 ガワ方式（claude.ai web 版を外部から駆動する PoC 方針）。撤回記録として本 ADR が当該方針を閉じる。

---

## 1. Context

T15 PoC-H は「既存 claude.ai persona を外部から操作するガワ」方式（2026-05-27-08 として §M に参照されていたが文書実体は未作成、起点は chatroom decide）で、claude.ai web 版を Playwright 等で駆動し AI をトリガーする構想だった。

Phase 1 段階 A（Step 1.1 retry）の実機検証で、この方式が**技術障壁ではなく規約エンフォースメント**に衝突することが判明した。

### 1.1 段階 A 実機結果（客観事実）

{{HOST_LOOP}} 上で標準 Playwright（Chrome for Testing build）を headed 起動し claude.ai を開いたところ、Cloudflare「セキュリティ検証の実行」画面（Turnstile checkbox）に到達。人手で checkbox をクリックしても pass されず同一 challenge に戻る loop が発生。

| 観測項目 | 値 |
|---|---|
| 最終 URL | `claude.ai/api/challenge_redirect?to=https%3A%2F%2Fclaude.ai%2Fnew` |
| Ray ID | `a042c58d9d458d01` |
| Turnstile | checkbox 表示、人手 click 後も loop（token 不発） |

標準 Playwright の自動化 indicator（`navigator.webdriver` / TLS fingerprint / WebGL / IP reputation）が Cloudflare の background check で reject され、Turnstile token が生成されず backend verification に失敗していた。

### 1.2 ToS 一次確認（規範事実）

段階 A の loop が「技術的に越えられない壁」なのか「意図的な規約エンフォースメント」なのかを切り分けるため、Anthropic 規約原文を一次確認した（Heisenberg, 2026-05-31）。

- **Consumer ToS §3.7（一次確認）**: `anthropic.com/legal/consumer-terms` 原文 —
  > "Except when you are accessing our Services via an Anthropic API Key or where we otherwise explicitly permit it, to access the Services through automated or non-human means, whether through a bot, script, or otherwise."

  加えて §3.4（crawl/scrape/harvest 禁止）、§2.2（credential 共有禁止）。
- **OAuth 宛先制限（2026-02 規定、一次ドキュメント確認）**: `code.claude.com/docs/en/legal-and-compliance` —
  > "OAuth authentication is intended exclusively for purchasers of Claude Free, Pro, Max, Team, and Enterprise subscription plans and is designed to support **ordinary use** of Claude Code and other native Anthropic applications."

  Free/Pro/Max の OAuth トークンを Claude Code / claude.ai 以外の製品・ツールで使うことは ToS 違反（Agent SDK 含む明示）。

**結論**: ガワ方式が当たった Cloudflare challenge は単なる技術障壁ではなく、Anthropic 側の意図的な ToS エンフォースメント機構である。Playwright でも GUI/入力エミュレートでも、claude.ai web 版を外部から駆動する限り §3.7 違反側に倒れる。stealth 化（playwright-stealth / Patchright / Camoufox）や API solver で技術的に越えても、規約違反であることは変わらず、アカウント凍結リスク（実際にサードパーティ自動化ツール利用者の停止例が 2026-01 以降複数）が規約面から現実化する。

---

## 2. Decision

### D-1: ガワ方式の撤回

T15 の「操作対象 = claude.ai web 版」を**撤回する**。claude.ai web 版を外部から駆動する全 route（標準 Playwright / stealth 系 / GUI 入力エミュレート / API solver）を nogo とする。撤回根拠は §1.2 の Consumer ToS §3.7 原文一次確認。

### D-2: 新操作対象 = Claude Code セッション + 別経路 naysayer

新しい操作対象を **Claude Code セッション**（terminal_coding_agent）と**別経路の naysayer**に再定義する。Claude Code は Anthropic 公式の自動化用途製品で、ToS が自動化禁止から明示的に除外している（OAuth 許可宛先の一つ）。

- **proposer / implementer = Claude Code セッション**（同系列、協調速度を取る）
- **naysayer = 別訓練分布の外部 API**（独立性最大化。配置の規範根拠は ADR-2026-05-31-15）

### D-3: A-2 bound — 「ordinary individual usage」制約（不変条件）

Claude Code の自動化は「種類としてはクリーン」だが、OAuth/サブスク権限は **"ordinary, individual usage"** に条件づけられている（§1.2 一次確認）。したがって本不変条件を持つ:

- T15 が将来「無人自動トリガー」（人間ブローカリングの置換）に向かう際、**無人駆動の頻度・量を ordinary individual usage の範囲内に制約する**。高頻度・大量の無人連続駆動は権限想定を超えうる。
- **2026-06-15 以降**、`claude -p` / Agent SDK のサブスク利用は interactive とは別建ての monthly Agent SDK credit で計量される。interactive な trilateral 運用は credit 非適用なので現行運用に影響なし。無人自動化に踏み込んだ場合のみ credit + ordinary 制約が効く。

### D-4: Gemini データ統治（不変条件、ADR-15 と共有）

別経路 naysayer に Gemini API を充てる場合のデータ統治を不変条件として持つ。本項は当初 ZDR を必須としていたが、Takahito 決定（2026-06-01）+ trilateral 収束（chatroom `T-zdr-invariant-downgrade` msg-368〜373、Einstein naysayer N-1〜N-5 取り込み）により以下へ改訂した:

- **paid 鍵＝必須不変条件（最上位）**（free 鍵厳禁）。paid は prompt/response をモデル訓練・プロダクト改善に使用しない（一次文面確認: 有料サービスの「Google による使用者のデータの利用方法」）。free は製品改善に使用＋一部 human review で学習利用が確定する。**ZDR を必須から外したため、paid 鍵が最後の防御線**であり ZDR 推奨より上位の不変条件。
  - **構造的保証**: naysayer backend に渡す `GEMINI_API_KEY` は paid 枠（請求有効化済 project）の鍵であることを**鍵管理・起動レイヤーで構造的に保証する**（gate は plain generateContent surface を強制するが、鍵文字列から paid/free を判別できないため——PR spirrow-lexora#1 review B-1 隣接論点）。運用任せにせず、Lexora 起動時チェック等で担保する（具体手段は implementer 裁量、Lexora 側で実装）。
- **ZDR（Zero Data Retention）＝推奨（必須ではない）**。承認されると全ユーザーコンテンツ（prompt/response）と識別メタデータが logging 前にクリアされる（申請制）。ZDR の差分は「訓練利用の防止」（それは paid 鍵が担う）ではなく「短期アビューズログ・24h キャッシュすら残さない」上積み部分のみ。**ただし推奨であって不要ではない**: 単発では個人データでない naysay も集積すると SpirrowGames の実態が再構成されうる（Einstein N-5）ため、短期ログ抑止としての価値があり、下記 (i)(ii) 非該当でも有効化が望ましい。
  - **再必須化トリガー（二本立て、N-1/N-3 取り込み）**: 以下の **いずれか** に該当する LLM 経路は ZDR 必須（or 外部 naysayer 不使用で内部処理）とする——
    - **(i) 他人の個人データを LLM 経路に載せる用途**（例: Thirdy の顧客ミーティング処理、ゲーム内 UGC のモデレーション）。
    - **(ii) セキュリティ構成・未公開脆弱性が naysay 対象の中心になる経路**（例: 認証設計・インフラ構成・Vaultwarden/Tailscale/firewall 構成の議論、PR レビューで脆弱性箇所を記述する naysay）。これは「他人の個人データ」ではないが実害度が異なり、かつ **T15 認証4軸議論のように naysayer 経路を実際に通っている**（再帰構造）。Takahito 本人の認証材料・個人開発環境の機密も (ii) で拾う（「他人の」限定が本人機密を除外しないため）。
  - **検知点（N-2 取り込み）**: トリガーは静的条件だけでなく **評価タイミング** とセットで持つ。**新規 service/機能が Lexora 経由で LLM を呼ぶ設計をするとき、その設計 ADR の段階で「この経路に (i)(ii) のいずれかが載るか」を必須チェック項目にする**。「条件は書いたが検知が無主で暗黙運用に落ちる」を防ぐ。
- **素の `generateContent` のみ**。grounding（検索/マップ、30日保持・無効化不可）/ File API / 明示的コンテキストキャッシュ / Live API / Interactions API は呼ばない（adapter 層で gate）。この gate は ZDR の要否と独立した surface 強制であり、ZDR 格下げの影響を受けない（PR spirrow-lexora#1）。

### D-5: D2-1 / D2-2 の新構成への写像

旧ガワ方式の不変条件「D2-1: 書き込み主体明示 / D2-2: ガワは read-only」を新構成へ写像する:

- **D2-1（書き込み主体明示）**: 旧構成では「中の claude.ai が書き込み主体、ガワは経路」だった。新構成では各 Claude Code セッション / naysayer が自身の identity（instance_id）で chatroom に書き込むため、書き込み主体は instance 単位で明示される（ADR-2026-05-24-08 instance-identity モデルの author=instance_id 規約で担保）。
- **D2-2（read-only 不変条件）**: 旧構成の「ガワは claude.ai を read-only 観測」は、新構成では「外部ハーネスは Claude Code を起動・観測するが、AI の出力主体性を奪わない（書き込みは AI 自身の判断）」へ写像。無人トリガーでも D-3 の ordinary usage 範囲内に留める。

---

## 3. Consequences

### Positive

- 規約クリーンな構成に確定（ToS §3.7 / OAuth 宛先制限の両方を一次確認で回避）。アカウント凍結リスクを規約面から排除。
- 段階 A で ToS 衝突を実地に潰したため、新構成に確信を持って倒せた（PoC の本来目的＝失敗の早期発見を達成）。
- Claude Code は公式自動化製品で、scripted/`claude -p` 利用が想定済み。インフラ追加なし（proposer/implementer はサブスク枠のまま）。

### Negative / Cost

- ガワ方式に投じた段階 A の実機検証コストは sunk。ただし「規約衝突の確証」という成果に転化。
- 別経路 naysayer（Gemini）の導入で外部 API 依存・paid 鍵管理・データ統治レイヤーが増える（実装複雑度中、ADR-15 / Lexora adapter で扱う）。
- D-3 の ordinary usage 制約により、無人自動トリガーの頻度・量に上限が生じる（無制限スケールはできない）。

### Neutral

- interactive な trilateral 運用は現状と変わらない（Agent SDK credit 非適用）。

---

## 4. 経緯の追跡性

ガワ方式採択から撤回までの議論は chatroom thread `T-T15-poc-h-phase1-kickoff`（msg-347〜msg-362）に残る。主要 msg:

- msg-356（Bohr）: ToS Q1 調査による根本再評価提起
- msg-357（Einstein）: naysayer 独立検証 + 一次確認 3 条件（A-1/A-2/C-1）
- msg-358（Heisenberg）: A-1/A-2/C-1 一次ソース検証 + 実装影響評価
- msg-359（Heisenberg）: C-1 ブラウザ一次確認 + ZDR + paid 承認で残条件解消
- msg-360（Bohr）: T15 正式 decide（D-1〜D-4）
- msg-362（Bohr）: ADR 採番・執筆方針 decide（本 ADR の起案根拠）

---

## 5. Implementation Notes

- 本 ADR は UI 自動化手段選定 ADR であり、CLAUDE.md §M（role/identity 規範定義）対象外。§M の注記行「2026-05-27-08（T15 ガワ方式）」を本 ADR の採番（05-31-14）に更新する。
- 別経路 naysayer の独立性配置の規範根拠は ADR-2026-05-31-15。Gemini adapter 実装（Lexora naysayer ルートの backend 差し替え）は別タスク。
- D-3 の ordinary usage 制約と 2026-06-15 Agent SDK credit 計量は、無人自動トリガー設計（Phase 2 以降）の着手前に再確認すること。


---

## 6. Amendment (2026-06-04): データ統治 gate の無効化 — Takahito 権限・判断

**本改訂は trilateral 議論（proposer/implementer/naysayer 収束）を経ていない。Takahito（human owner）の権限・判断で決定し、指示により本欄へ明記する。** 通常の §M / Tier C プロセス（trilateral + Takahito 承認）に対する例外で、決定主体は Takahito 単独である。

### 決定内容

D-4 の surface 強制不変条件「素の `generateContent` のみ（tools/grounding/File API/cached content/非テキスト part を adapter 層で gate）」を、spirrow-lexora の Gemini (naysayer) backend で **無効化**した。

- 実装: backend に config トグル `governance_gate_enabled`（既定 `True` = fail-closed）を新設し、本番 naysayer backend の `config/lexora_config.yaml` で `governance_gate_enabled: false` に設定。
- 効果: `tools` / `tool_choice` / `functions` / `function_call` / `parallel_tool_calls` / `cached_content` / 非テキスト content part が **拒否されなくなる**（`/v1/messages` naysayer surface）。
- gate 無効時はリクエストごとに `gemini_governance_gate_disabled` warning をログ出力し、緩和状態を可視化する。

### 影響範囲と非変更点

- 緩和されるのは D-4 の **surface 強制 gate のみ**（ADR-2026-05-31-15 C-2 と共有する同一 gate）。
- **paid 鍵＝必須不変条件（D-4 最上位・最後の防御線）と ZDR 推奨は本改訂で一切変更しない。** 学習利用防止の防御線は維持される。
- 補足: 現状 Lexora の翻訳層（`_to_gemini_request`）は `tools` を Gemini へ転送しないため、本無効化は「gate が拒否しない」状態であり、実際の tool use を機能させるには別途翻訳実装が要る。

### トレーサビリティ

- spirrow-lexora commit `c9aa914`（`main`、push 済）。変更ファイル: `config.py` / `factory.py` / `backends/gemini.py` / `config/lexora_config.yaml`。
- 関連: ADR-2026-06-03-17（Gemini の tool-less/one-shot 制約による design-time 参加 regression を relay orchestrator で回帰させる決定）。本改訂は gate 自体を config で外す別レバーであり、17 の orchestrator 方式とは独立。
- **再有効化**: 本番 config を `governance_gate_enabled: true` に戻す（または key 削除で既定 True）だけで surface 強制が復活する。


---

## 7. Amendment (2026-10-03): OpenAI（Codex CLI、ChatGPT Pro サブスク認証）を naysayer の 3 社目として認める — D-4 の改訂

- **Status**: Proposed。収束は chatroom `T-D8-codex-backend-adr14-15-amendment`（Bohr proposer の msg-6059、msg-6061 / Einstein naysayer の承認）。Takahito の判断は msg-5940（Fermi が代筆、選択肢 (a)）。**この節が main に merge されることを、Takahito による承認とする**（merge-protected）。
- **SOT**: Codex に関するデータ統治は、この節が SOT です。ADR-15 C-2 と ADR-19 D-3 は、この節を参照します。
- **変更しないこと**: Gemini の経路にかかる D-4（paid 鍵の不変条件、ZDR の推奨、再必須化トリガー (i)(ii)、検知点）と §6 は、そのまま有効です。

### 7.1 何を認めるか

- **3 社目のベンダーとして OpenAI を認めます。** 経路は Lexora の codex backend です。Lexora が `codex exec` を naysayer ティアの裏で動かします。
- 対象は、design-time の naysayer と PR-gate の naysayer の両方です。
- 認証は個人の ChatGPT Pro サブスクで、Takahito の個人契約です。API 鍵（API org）は使いません。
- 費用は Pro の定額だけです。API の従量課金は発生しません。

### 7.2 最後の防御線（D-4 の「paid 鍵」に当たるもの）

サブスク認証には、paid 鍵に当たるものがありません。そこで、最後の防御線を次の 3 つで置き換えます。

1. **データ設定の記録（TTL つき）**
   - ChatGPT のデータ設定で「モデルの改善に使う」をオフにします。人がこれを確かめた日時を、Lexora の `_check_data_controls()` が読む記録に残します。
   - TTL が切れたら、ゲートが閉じます。
   - 期限が近づいたら、codex backend が自分で予告を出します。予告には、どの backend からも呼べる共通の notifier を使います。
2. **`verification_stale`**
   - CLI のバージョンが変わったら、ゲートが閉じます。
   - `verify_codex` をやり直すまで、ゲートは開きません。
3. **D-D（ツールを無効にすること）**
   - 中身は 7.4 のとおりです。

### 7.3 受け入れるリスク（paid 鍵との違い）

**data controls は、黙って変わりうる**
- paid 鍵の性質が変わるのは、契約の変更としてです。その場合は予告があります。
- 一方、data controls のトグルは、予告なしに切り替わりえます。たとえば規約の更新、アカウントの操作、プランの変更などです。
- 学習オプトアウトの状態を、CLI や API から機械的に読む手段は確認されていません。
- そのため、TTL が残っている間は、トグルが変わっても気づけません。
- **このリスクは受け入れます。** Takahito の判断（msg-5940）によるもので、既存の方針「学習されても困らない」にもとづいています。

**ZDR は満たしていません**
- 個人向けの Pro では、ZDR を満たせません。
- msg-5940 で、ZDR は codex の通常経路の要件から外されました。

**N-3 の部分集合（再必須化トリガー (ii)）は Gemini 固定とします**
- ADR-15 C-2 の「この部分集合は ZDR 必須」は外しません。
- そのため、naysayer ティアを決める関数は、この部分集合では codex を返しません。
- これを mindwire 側の実装で強制します。名前がずれたことを検知する構造テストも置きます。
- この部分集合を codex に広げるかどうかは、goal の判断です。広げる場合は、別に改訂します。

**個人のサブスクを headless で使うことの運用上のもろさ**
- レート制限やセッションの失効が起きると、codex は失敗します。その場合は gemini-fallback に切り替わります（7.4）。レビューは止まりませんが、その間は定額の利点がありません。
- どれくらいの頻度でフォールバックしたかは、shadow 比較と primary に上げたあとの観測項目にします。フォールバックが常態になるなら、構成を見直します（認証方式の変更は cost / goal の判断です）。

**HOST_REPO が private な場合も、その内容が OpenAI に送られます**
- public なリポジトリを除外する条件（4b）は、撤回しました（msg-5940）。
- 根拠は公式ドキュメントにあります。「Do not use this workflow for public or open-source repositories」が禁じているのは、CI の runner に `auth.json` を置く手順です。コードの内容を送ることを禁じた文ではありません。
- また、`auth.json` は、外部の PR がコードを動かせるマシンには置きません。

### 7.4 D-D：ツールを無効にすること（4a。PR-gate と design-time の両方で、codex を有効にする条件）

**不変条件**
- モデルに渡るツールの一覧に、次のものを 1 つも含めません。
  - ファイルを読めるもの
  - コードやコマンドを実行できるもの
  - ネットワークに出られるもの
- 許すツールは、許可リストで持ちます。許可リストに入るのは、次の 2 つだけです。
  - `clock`
  - `send_user_message_async`
- **`apply_patch` は許しません。** 書き込みが read-only の sandbox で失敗する場合でも、文脈を確かめるために対象ファイルを読むからです。
- 許可リストに無いツールが 1 つでも見つかったら、fail-closed にします。

**消し方は、計測して決めます**
1. **(i)** `model_catalog_json` で、使うモデル（`gpt-6.1-sol`）のエントリを書き換えたカタログを渡します。`tool_mode: direct`、`apply_patch_tool_type: null` などにし、`-c` のフラグも組み合わせます。
2. **(i')** 同じ書き換えを `gpt-5.5` に当てます。
3. **どちらでも許可リストに収まらなければ、codex は本番で有効にしません。** design-time と PR-gate のどちらでもです。

**モデルの選択は、Tier-C ではありません**
- 費用も goal も変わらないからです。
- 品質は、primary に上げる前の shadow 比較で測ります。

**強制は Lexora のコードで行います（config では変えられません）**
- カタログ、`-c` のフラグ、`--model` はコードに固定します。カタログは、中身のハッシュも照合します。
- `TOOL_DISABLE_OVERRIDE_KEYS` は、ツールを無効にするために `codex exec` に `-c` で渡すキーと値の一覧です。**これは無効にする側の一覧であって、無効化を外すための仕組みではありません。** コードの定数として固定し、config から足すことも外すことも、値を変えることもできません（上の「config では変えられません」の一部です）。
- `verify_codex` で `codex exec --json` を実行し、実際に渡るツールの一覧を許可リストと照合します。
- 実行時の多層防御として、ツールの実行を 1 回でも検知したら止める latch（D-1c）を残します。

**同時実行は 1 つまでです**
- 1 つの `auth.json` を、トークンの更新で競合させないためです。
- codex backend の同時実行数は、コードで 1 を上限にします。config で 2 以上が指定されたら、起動時に拒否します。
- **上限に達したときは、まず待ち合わせます（直列化）。上限に達したこと自体は、フォールバックの契機にしません。** 上限に達した瞬間に codex の失敗として扱うと、重なったリクエストが gemini-fallback（従量課金）へ、競合のたびに黙って流れるからです。
- **待ち合わせの上限時間を超えたら、gemini-fallback に切り替えます。呼び出し元を失敗させることはしません。** PR-gate やレビューが、ほかのリクエストと重なっただけで落ちることは許しません。コストを抑えることよりも、レビューが確実に終わることを優先します。
  - この切り替えは黙って行いません。共通の notifier で知らせ、attestation にも `gemini-fallback` として残します。
  - 上限時間の値は Lexora の PR で決めます。ふつうの直列化ではまず超えない長さにします。
  - **1 リクエストにかける時間の合計（待ち合わせ＋codex の実行、または待ち合わせ＋gemini-fallback の実行）は、mindwire が前提にしている backend の時間の上限（`lexora/client.py` の `LEXORA_BACKEND_TIMEOUT_SECONDS`）に収めます。** mindwire のクライアントは、その値に余裕を足した時間だけ待ちます（`naysayer/pr_review.py` の `_DEFAULT_TIMEOUT_SECONDS`）。そのため、Lexora が上限内に返す限り、クライアントが先に接続を切ることはありません。予算の配分は次のとおりにします。
    - codex の 1 回の実行と、gemini-fallback の 1 回の実行には、それぞれ Lexora のコードで上限時間を設けます。上限を超えた実行は打ち切ります。codex の実行を打ち切った場合は、codex 側の失敗として扱います。
    - 最も長くかかるのは、待ち合わせの後に codex を上限まで実行し、それが失敗して gemini-fallback を上限まで実行する経路です。そのため、**待ち合わせの上限時間 ≦ 予算 − (codex の実行の上限時間 ＋ gemini-fallback の実行の上限時間)** とします。どの経路をたどっても、合計が予算に収まります。
    - 待ち合わせの上限を超えて直接 gemini-fallback に切り替える経路は、待ち合わせの上限時間＋gemini-fallback の実行の上限時間なので、上の式を満たせば予算に収まります。
    - この不等式は、Lexora の起動時に確かめます。満たさない値が設定されていたら、起動を拒否します。
  - それでもクライアント側で時間切れになった場合は、いまの PR-gate の扱い（`_degrade_on_timeout`：COMMENT で保留し、人に知らせる）に従います。黙って失敗することはありません。
- そのほかに fallback wrapper が gemini-fallback に切り替えるのは、codex 側が失敗したとき（ゲートが閉じている、実行がエラーになった、レート制限やセッションの失効など）です。

**残るリスク**
- `auth.json` は、sandbox の中から読める位置にあります。読まれないことは、ツールが無いことだけで保証しています。
- design-time でツールを許す案は、今回は採りません。採る場合は、先に sandbox の中から `auth.json` を隠せるかを実測し、別の提案として出します。

### 7.5 プロンプトの構成（ツールの代わりに、mindwire が決まった規則で組み立てます）

何を読むかは、モデルに選ばせません。

**PR-gate に入れるもの**
- diff
- 変更されたファイルの、head 時点の全文
- パスの規則だけで決まる、対になるテストファイル（例: `src/x/y.py` ↔ `tests/**/test_y.py`）
- import をたどってファイルを集めることはしません。shadow 比較で文脈が足りないと確かめられてから、別に提案します。

**design-time に入れるもの**
- スレッドの本文
- principles
- スレッドが参照している ADR やファイルの全文

**どちらにも共通の規則**
- プロンプトには文字数の上限を設けます。上限を超えたら、次の順に落とし、落としたファイルの名前をプロンプトの冒頭に書きます。
  - PR-gate：(1) 対になるテストファイル → (2) 変更されたファイルの全文（大きいものから）。
  - design-time：(1) 参照されたファイルの全文 → (2) 参照された ADR の全文。スレッドの本文と principles は落としません。
- **全部落としてもまだ codex ティアの上限を超える場合**
  - PR-gate と design-time のどちらも：**そのリクエストは codex ティアではなく、Gemini のティアに送ります。** 上限も Gemini のものを使い、切り詰めません。codex の上限を理由にして、Gemini なら収まる PR を REQUEST_CHANGES にすることも、design-time を失敗させることもしません。送り先を変えるこの判定は、ティアを決める関数の中で行います。attestation の期待する値（7.6）も、この判定から導きます（`gemini`）。
  - 大きい PR を Gemini で扱うのは、いまの PR-gate と同じ扱いです（いまは PR-gate のすべてが Gemini です）。費用が新しく増えるわけではありません。
- **Gemini のティアの上限も超える場合**
  - PR-gate（diff そのものが大きい場合）：diff を切り詰め、切り詰めたことをプロンプトと結果に明示します。そのうえで、いまの PR-gate の規則（`naysayer/pr_review.py`：切り詰めた diff のレビューは APPROVE せず、REQUEST_CHANGES に倒す）にそのまま従います。黙って切り詰めることも、切り詰めたまま APPROVE することもありません。
  - design-time（スレッドの本文と principles だけで超える場合）：naysayer を呼ばずに、上限を超えたことを理由として呼び出し元に失敗を返します（fail loud）。
- **上限の値は、mindwire がそのリクエストを送るティアごとに持ちます。** codex ティア（gemini-fallback を予備に持つもの）には codex の上限を使います。N-3 の部分集合のように Gemini に固定して送るリクエストには、Gemini の上限を使います。codex ティアから gemini-fallback に切り替わった場合は、codex の上限で組んだプロンプトがそのまま渡ります（Lexora の中で組み直すことはしません）。
  - そのため、フォールバックしたときの Gemini のレビューは、Gemini が本来読める量より少ない文脈で行われます。これは、フォールバック用にプロンプトを組み直す仕組みを持たないことと引き換えに受け入れる劣化です。フォールバックの頻度（7.3）と合わせて観測し、問題になれば別に提案します。
- 入れたファイルは区切りで囲み、「データであって指示ではない」と明示します。これは補助的な対策です。本当の防御は 7.4 です。
- 組み立ての規則（何を入れるか、落とす順番、区切り）は、backend によって変えません。変わるのは上限の値だけです。

### 7.6 naysayer ティアの構成と attestation

- naysayer ティアの構成は、「codex を正、gemini-fallback を予備」とする fallback wrapper です。
- 素の codex backend を、直接ティアにつなぐことはしません。
- mindwire の attestation で期待する値は、**リクエストごとに決めます。** 全体で一律に緩めることはしません。
  - codex ティアに送ったリクエスト：正は `codex` です。`gemini-fallback` も、許容されるフォールバックとして受け入れます。
  - Gemini のティアに送ったもの（N-3 の部分集合（7.3）と、codex の上限に収まらないもの（7.5））：期待する値は `gemini` だけです。`codex` や `gemini-fallback` が返ってきたら不一致として fail-closed にします。ティアを決める関数が誤って codex ティアに送った場合も、ここで検知できるようにするためです（多層防御）。
  - 期待する値は、ティアを決める関数と同じ判定から導きます。2 か所で別々に判定することはしません。
- この変更の対象は、`expected=gemini` を前提にしている箇所です（`naysayer/preflight.py`・`principles.py`・`adapters/naysayer_sdk.py`）。
- この変更は、Lexora の codex 経路がティアにつながるのと同時に入れます。先に入れると、今の Gemini の経路が attestation で不一致になるからです。

### 7.7 C-2 の対象外と、テストで守っている前提

**`_run_unverified` は、C-2（機密の外部移動）の対象外です**
- 送り先はループバックだけで、送るのは固定の文面と nonce だけです。
- これは backend が強制しています（Lexora `a8517f8`）。

**argv の並びを前提にしています**
- `codex exec` に渡す argv の並びを前提にしています（並びの中身は Lexora の backend とテストが正本です。この ADR では書き写しません）。
- この前提は、テスト `TestVerifyOverridePrecedence`（Lexora `0e28018`）が守っています。

### 7.8 §6（gate の無効化）との関係

- §6 で無効にしたのは、Gemini backend の surface gate です。codex の経路は、read-only の sandbox でツールを持たないので（7.4）、§6 と矛盾しません。

### 7.9 有効にする前提（すべて満たすまで、本番では有効にしません）

1. Lexora の PR で、7.4 の強制、同時実行数 1、共通の notifier、fallback wrapper の構成を入れていること。そのうえで、(i)/(i') の計測結果がスレッドに報告されていること。
2. Lexora のホストで `verify_codex` が成功していること。
3. 本番の B-2 migration（`answered_by` の列）を確かめ、無ければ当てていること。
4. mindwire 側に、attestation（7.6）、N-3 の除外（7.3）、プロンプトの構成（7.5）が入っていること。
   - あわせて、naysayer の principles の SOT（`spec/NAYSAYER_PRINCIPLES.md`）の frontmatter `independent_model`（いまは `gemini-3.1-pro-preview`）を、codex を正・gemini-fallback を予備とする構成に合わせて更新していること。そのままだと、codex に渡す preamble が「Gemini である」と誤って述べることになります。
   - **この更新は principles の改訂として扱い、同じ変更で frontmatter の `version:` を上げていること**（あわせて、`naysayer/principles.py` の `EXPECTED_PRINCIPLES_VERSION` も同じ値に上げます。片方だけ上げると起動時にエラーになります）。principles の SOT は、改訂のたびに `version:` を上げることを定めています。naysayer の出力はそれぞれ、判定に使った `principles_version` を記録します。version を上げずに `independent_model` だけを書き換えると、codex のレビューと Gemini のレビューが同じ `principles_version` で記録され、どちらの構成で判定したかを後からたどれなくなります。principles の改訂なので、SOT が定める手続き（proposer / implementer / naysayer の協議と Takahito の承認）にも従います。
5. shadow 比較のあとで primary に上げること。
