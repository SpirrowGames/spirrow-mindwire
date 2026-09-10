# ADR-2026-05-31-14: ガワ方式撤回 — claude.ai web 外部駆動の ToS 衝突と Claude Code への操作対象転換

- **Status**: Draft（ローカル develop。Takahito GO 後に Drive 反映 = ADR-07 §2.5 / Tier C）
- **Date**: 2026-05-31
- **Scope**: spirrow-mindwire（T15 PoC-H / UI 自動化手段選定。identity 規範定義ではないため CLAUDE.md §M 対象外 = UI 自動化手段 ADR）
- **Author**: Heisenberg (implementer, terminal_coding_agent) — chatroom T-T15-poc-h-phase1-kickoff の trilateral decide (Bohr proposer) を反映
- **Relates to**: ADR-2026-05-27-08（旧採番。T15 ガワ方式の §M 参照名。本 ADR が実体化し置換）、ADR-2026-05-31-15（independence-class グラデーション。別経路 naysayer の規範根拠）
- **Supersedes**: T15 ガワ方式（claude.ai web 版を外部から駆動する PoC 方針）。撤回記録として本 ADR が当該方針を閉じる。

---

## 1. Context

T15 PoC-H は「既存 claude.ai persona を外部から操作するガワ」方式（ADR-2026-05-27-08 として §M に参照されていたが文書実体は未作成、起点は chatroom decide）で、claude.ai web 版を Playwright 等で駆動し AI をトリガーする構想だった。

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
    - **(ii) セキュリティ構成・未公開脆弱性が naysay 対象の中心になる経路**（例: 認証設計・インフラ構成・Vaultwarden/Tailscale/firewall 構成の議論、PR レビューで脆弱性筧所を記述する naysay）。これは「他人の個人データ」ではないが実害度が異なり、かつ **T15 認証4軸議論のように naysayer 経路を実際に通っている**（再帰構造）。Takahito 本人の認証材料・個人開発環境の機密も (ii) で拾う（「他人の」限定が本人機密を除外しないため）。
  - **検知点（N-2 取り込み）**: トリガーは静的条件だけでなく **評価タイミング** とセットで持つ。**新規 service/機能が Lexora 経由で LLM を呼ぶ設計をするとき、その設計 ADR の段階で「この経路に (i)(ii) のいずれかが載るか」を必須チェック項目にする**。「条件は書いたが検知が無主で暗黙運用に落ちる」を防ぐ。
- **素の `generateContent` のみ**。grounding（検索/マップ、30日保持・無効化不可）/ File API / 明示的コンテキストキャッシュ / Live API / Interactions API は呼ばない（adapter 層で gate）。この gate は ZDR の要否と独立した surface 強制であり、ZDR 格下げの影響を受けない（PR spirrow-lexora#1）。

### D-5: D2-1 / D2-2 の新構成への写像

旧ガワ方式の不変条件「D2-1: 書き込み主体明示 / D2-2: ガワは read-only」を新構成へ写像する:

- **D2-1（書き込み主体明示）**: 旧構成では「中の claude.ai が書き込み主体、ガワは経路」だった。新構成では各 Claude Code セッション / naysayer が自身の identity（instance_id）で chatroom に書き込むため、書き込み主体は instance 単位で明示される（ADR-2026-05-24-08 instance-identity モデルの author=instance_id 規約で担保）。
- **D2-2（read-only 不変条件）**: 旧構成の「ガワは claude.ai を read-only 観測」は、新構成では「外部ハーネスは Claude Code を起動・観測するが、AR の出力主体性を奪わない（書き込みは AI 自身の判断）」へ写像。無人トリガーでも D-3 の ordinary usage 範囲内に留める。

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

- 本 ADR は UI 自動化手段選定 ADR であり、CLAUDE.md §M（role/identity 規範定義）対象外。§M の注記行「ADR-2026-05-27-08（T15 ガワ方式）」を本 ADR の採番（05-31-14）に更新する。
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
