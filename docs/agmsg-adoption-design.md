---
id: spirrow-mindwire:agmsg-adoption-design
title: agmsg 知見の取り込み設計（トランスポート層の堅牢化）
product: spirrow-mindwire
type: design
status: draft
created: 2026-09-16
last_verified: 2026-09-16
keywords: [agmsg, stall-watchdog, spawn-readiness, identity, transport]
---
# agmsg 知見の取り込み設計（トランスポート層の堅牢化）

- 日付: 2026-09-16
- 起案: Fermi（claude.ai 設計相談、既定で提案扱い）
- 対象: spirrow-mindwire（Conductor / adapters / event_log）、一部 Magickit・Conclair
- 参照: https://github.com/fujibee/agmsg （main @ 2026-09-15、`scripts/watch.sh` `session-start.sh` `spawn.sh` `poke.sh` `peek.sh` `lib/instance-id.sh` `docs/session-resurrect.md` を実読）
- ステータス: 提案（Tier C 判断点は §6 に列挙）

## 0. 結論

agmsg は mindwire の競合ではなく、T15 要素 1（auto-terminal for Claude Code）・要素 3（chatroom 媒介トリガー routing）に相当する **トランスポート層だけ** を切り出した製品。役割・ハンドオフ・停止条件・SOT は意図的に持たない（README FAQ「プロトコルはプロンプトの中に生きている」）。協調プロトコル層は mindwire が先行しているので取り込む物は無い。一方、トランスポート層の細部には mindwire の既知の課題（「静かに止まる」・identity 確定・spawn 直後の取りこぼし・Operator ボードの状態表現・tomtebo-02 追加時の排他）に直撃する実装があり、それらを **仕組みとして** 取り込む。

## 1. agmsg の実態（コードベース）

- ストア: 単一 SQLite（WAL）。`events(seq, type ∈ {message_sent, message_read}, team, from, to, body, …)` + `read_cursors(team, agent, local_position)`。未読 = `seq > cursor` かつ対応する `message_read` 無し、を 1 スナップショット TX で読む。
- monitor モード: Claude Code の SessionStart フック → 「Monitor ツールで `watch.sh` を起動せよ」という **指示文** を出す → `watch.sh` が `sleep` ベース 5 秒ポーリングで新着行を stdout に `printf` → Monitor ツールがそれをセッションのイベント（新ターン）に変換。「ブロッキングストリーム」は README 上の表現で、実態はポーリング。
- both モード: monitor + Stop フック（`check-inbox.sh`、60 秒クールダウン）。ウォッチャー生存を確認して Stop 側を抑止する設計は **意図的に撤去**（#694：「生きているが配信していない」ウォッチャーがあるため）。重複配信は共有 cursor で回避。
- ウォッチャー死活: 別プロセスの watchdog は無く、ループ内蔵。pidfile 置換（cmdline に `watch.sh` を含む pid だけ kill）、instance id = `<session_id>.<pid>` を `kill -0` + `run/cc-instance.<pid>` のトークン一致で判定（PID 再利用ガード、戻り値 0 alive / 1 dead / 2 不明、exit するのは 1 のみ）、scripts/ 更新検知で自己終了。
- stall 検出（#1045）: pending の `message_sent` があるのに cursor が 3 回連続ポーリングで不変 → stdout に `delivery for X is STUCK …` を出し exit 75。閾値 3 は意図的に固定（環境変数化しない）。
- spawn readiness: ウォッチャーが購読解決 + DB オープン後に `run/ready.<team>__<agent>` を書く。spawn 側は 1 秒ポーリングで待ち `status=ready after=Ns`。待てない type / mode では `status=launched-unconfirmed` と **別の語で** 返す。
- identity 解決（SessionStart）: role-session 記録 or この session id が持つ actas ロックから seat を解決。決まらず、かつプロジェクトに複数 seat があれば **指示文を出さず stand down**（他 seat の受信箱を食わないため）。
- send / poke: send = ストアに記録。poke = 相手ペインにキー入力する非常手段（tmux `send-keys -l` → `Right Enter`）。poke 不能は exit 13 で「send に黙ってフォールバックするな、どちらをやったか言え」。SKILL.md は Claude Code ネイティブ SendMessage の使用も禁止（履歴から消えるため）。
- peek: 画面テキストの部分一致で `approval`（"Do you want to proceed"）/ `working`（"esc to interrupt" 等）/ `idle`（残余）/ `read_rc_N`。`idle` は「非活動の証拠ではない」と明記。
- where: `resolved=false`（判定不能、reason 付き）と `resolved=true placement=none`（本物の否定）を分け、前者を「ペイン無し」と報告することを禁止。
- session-resurrect: `actas` 時に `(team, agent) → CLI session id` を `run/role-session.*` に記録し、spawn は既定で `claude --resume <uuid>`。
- 制御メッセージ: `ctrl:despawn` の body 文字列一致が 1 種のみ。graceful despawn = 受信側が自分でロール解放 + 自分のペインを閉じる。
- 自己メッセージ: from==to のフィルタは無い（普通に配信される）。

## 2. 取り込み項目（設計）

各項目: 現状（mindwire）→ agmsg の実装 → mindwire への適用 → 受け入れ条件。

### A. 汎用 stall watchdog（「静かに止まる」の構造的排除）【最優先】

- 現状: 2026-09-14 実測で Bohr→Bohr 自己ハンドオフ時に `deliver_event` の自己フィルタが無言 return し、head_skip が毎時リトライして「静かに止まる」バグを確認。個別修正プロンプト（自己ハンドオフ検出・no_progress 終端化・spawn 不能 identity の human 停止）は作成済。
- agmsg: pending があるのに進捗が無い状態を N=3 回観測したら **大声で死ぬ**（stdout + 非 0 exit）。個別原因を潰すのではなく「進捗の無さ」自体を検出。
- 適用: Conductor のデリバリループに汎用 watchdog を入れる。観測量 = 「未配信イベント数 > 0」かつ「配信 cursor（または最後に配信成功した event id）が不変」。N 回連続で成立したら (1) event_log に `delivery.stalled`（thread, pending 件数, 最終進捗 event id, 連続回数）、(2) chatroom の当該スレッドに STALLED を投稿（NEXT: human）、(3) Conductor を非 0 exit で終了。N は定数（3）で環境変数化しない。個別バグ修正（自己ハンドオフ検出）はこの watchdog の **上** に置き、watchdog は最後の砦。
- 注意: 自己フィルタ・head_skip 等、意図的に配信しない経路は「無言 return」ではなく **必ず理由付き event を残す**（skip 理由列挙型）。watchdog はこの skip event を「進捗」とは数えない。
- 受け入れ条件: 9/14 の再現条件（Bohr→Bohr 自己ハンドオフ）で、個別修正を外した状態でも 3 poll 以内に STALLED が chatroom に出て Conductor が非 0 で終了する統合テスト。

### B. spawn readiness の二値化（launched ≠ listening）

- 現状: Conductor は adapter を spawn したらハンドオフを投げる。起動直後に取りこぼしても気付く仕組みが無い。
- agmsg: 受信側が「購読解決 + ストア開放」の時点で sentinel を書き、spawn 側はそれを待つ。待てない場合は `launched-unconfirmed` と別の語で返す。
- 適用: RoleAdapter に readiness 通知を追加（`SessionHandle` に `ready_at` / `readiness ∈ {ready, launched_unconfirmed, timeout}`）。ClaudeCodeSdkAdapter は SDK の初回イベント受領（または最初の tool 可用確認）で ready を報告。Conductor は ready まで最初の deliver を保留、timeout（既定 90 秒）で `spawn.timeout` を event_log に残し再 spawn 1 回 → 失敗なら NEXT: human。「起動した」と「聞いている」を event_log でも区別。
- 受け入れ条件: spawn 直後 0 秒で投げた handoff が取りこぼされない e2e smoke。timeout 経路のテスト。

### C. fail-closed な identity / thread 解決（stand down は必ず声を出す）

- 現状: `mindwire.toml` の `task_thread_id` と `[loop].project` が不一致だと thread が見つからず one-shot で終了する（2026-08-28 手順確認）。終了理由は chatroom に残らない。Fermi 追加（2026-09-14）で identity の種類が増える。
- agmsg: seat が確定しないなら指示文を出さず stand down。他 seat の受信箱を食わない。
- 適用: Conductor 起動時の解決フェーズを明示化。identity（4 層モデル）・project・thread・repo_dir の 4 点を解決し、1 つでも確定しなければ spawn せずに (1) event_log `conductor.stand_down`（未解決項目と理由）、(2) 可能なら chatroom（project が解決できていればそのプロジェクトの運用スレッド）に投稿、(3) 非 0 exit。spawn 不能 identity（Fermi = human 停止）もこの経路で扱う（9/14 修正プロンプトと整合）。
- 受け入れ条件: project 不一致で起動したときに「静かに exit」せず理由が event_log + chatroom に残る。

### D. Operator ボードの状態モデル（unknown ≠ negative、approval を最優先）

- 現状: Operator ボードは Conclair に集約し executor（tomtebo-0x）から push、UI は magickit web（2026-09-05 裁定）。状態の語彙は未定義。
- agmsg: `resolved=false`（不明）と `placement=none`（否定）を分ける。peek は `approval / working / idle / read_rc_N` で、`idle` は残余状態と明記。
- 適用: ボードのセッション状態を **観測可否 × 状態** の 2 軸で定義する。観測 = `observed / unobserved(reason)`、状態 = `awaiting_approval / working / idle(残余) / stalled(A の watchdog 由来) / stand_down(C 由来)`。`unobserved` を `idle` と同色で描かない。`awaiting_approval`（「進めますか？」系の質問が人に来ている状態）はボード最上段 + 通知対象。これは 2026-09-05 の「一番なくしたいのは静かに止まることと、聞くまでもない問いが人に来ること」に直結。
- 受け入れ条件: 状態語彙が Conclair の schema として定義され、A/B/C の event が対応する状態に写像される。

### E. identity 排他ロック + PID 再利用ガード（tomtebo-02 前提）

- 現状: instance_id（T24）で発話主体は識別できるが、同一 identity（例 Heisenberg）を 2 台が同時に名乗ることを防ぐ機構は無い。2026-09-05 裁定「関連しないスレッドのみ並列開発可」を守る道具が要る。
- agmsg: `actas` ロック = `<session_id>.<pid>`、生存確認は `kill -0` + トークンファイル一致（PID 再利用ガード）、判定不能（2）では解放しない。
- 適用: identity claim を Magickit（identity の enforcement point、T29 と整合）に置く。claim = (identity, executor host, instance_id, heartbeat)。heartbeat 途絶で「不明」扱い、明示 release か人の介入でのみ解放（自動解放しない = fail-closed）。thread 単位の並列可否は Conclair のスレッド関連付けで判定。
- 受け入れ条件: 2 executor が同一 identity で spawn しようとしたとき後発が拒否され、その事実が chatroom に残る。

### F. role → session id の自己申告記録（Operator 管理画面の結合問題）

- 現状: 2026-09-04〜05 実測で「外部からの /clear 経路」と「Remote Control ref ↔ transcript UUID の結合」が未解決。
- agmsg: 外から結び付けず、`actas` 時（セッション内）に `(team, agent) → session id` を記録。SessionStart フックは session_id を受け取る。
- 適用: Claude Code 側に SessionStart / UserPromptSubmit フックを入れ、identity・thread・session_id・transcript path をセッション自身が Conclair に push する（ガワは read-only、書くのは中の Claude が MCP 経由という T15 D2-2 原則と整合）。管理画面はこの自己申告を join key にする。
- 受け入れ条件: 管理画面で identity → 現在の session_id → transcript が辿れる。

### G. Monitor ツール常駐セッション PoC（T15 要素 1 の agmsg 方式）【Tier C 判断】

- 現状: Conductor は one-shot 設計（docs/deploy.md「再武装は operator の仕事」、max_rounds=6）。
- agmsg: 常駐 Claude Code セッションに SessionStart フック → Monitor ツール → ポーリングスクリプト stdout で新着を流し込む。公式フック + Monitor ツールなので ADR-14（ToS §3.7）に抵触しない。
- 適用案: Heisenberg（implementer）のみ常駐化する PoC。watch スクリプトは `chatroom_my_unread` 相当を N 秒ポーリングし、1 行 1 イベントで stdout に出す。Conductor は routing に専念。**トレードオフ**: 常駐はコンテキストが溜まる（agmsg も `/compact` 後の SessionStart 再発火で二重起動しない処理を持つ）。F の session 記録と D のボードが無いと常駐の健康状態が見えないため、依存を置く。
- 判断点: one-shot 設計との併存方針（A/B 切替か、role 別か）。これは設計・目標に影響するため Takahito の承認が要る。PoC は「計測して報告」までとし、採用判断は別途。
- 受け入れ条件（PoC）: 常駐 Heisenberg が 1 スレッドを人手ゼロで 3 ラウンド回し、コンテキスト増加量・取りこぼし・stall 検出の挙動を数値で報告。

### H. 裏経路禁止の invariant 化（send ≠ poke、SOT 一元化）

- 現状: chatroom が SOT（既存原則）。ただし Claude Code ネイティブの SendMessage / ListAgents や、auto-terminal が chatroom を経由せずに agent 間通信する経路を明示的に禁止した文書は無い。
- agmsg: SKILL.md で「ネイティブ SendMessage を使うな、履歴から消える」「poke 不能を send に黙って置き換えるな」と明記。
- 適用: NAYSAYER_PRINCIPLES / ADR に「agent 間の意思疎通は chatroom 経由のみ。非常手段（ペインへの直接入力等）を使った場合はその事実を chatroom に残す」を invariant として追記。implementer allowlist で SendMessage 系ツールを deny に。
- 受け入れ条件: allowlist テスト + ADR 注記。

## 3. 取り込まない項目

- 単一マシン前提の SQLite 共有と後付けの remote sync（Stage-1 ポーリングエンジン、quarantine/conflict テーブル、e2ee 鍵管理）: Magickit chatroom がサーバーとして既に存在する。
- 9 種の CLI 対応、tmux/herdr ターミナルドライバ抽象、Windows Git Bash 対応: 構成に不要。
- `ctrl:despawn` の body 文字列一致: mindwire の `NEXT:` ハンドオフのほうが構造化されている。
- ターン制御・ラウンド上限・停止条件: agmsg 側に無い（プロンプト任せ）。mindwire が先行。
- 5 秒ポーリングそのもの: レイテンシ要件は秒〜分で許容済（T02）なので現行 ChatroomWatcher のポーリングで足りる。

## 4. タスク分解と依存

| 項 | タスク | 優先 | 依存 |
|---|---|---|---|
| A | 汎用 stall watchdog + skip 理由の event 化 | high | — |
| B | spawn readiness 二値化 | medium | — |
| C | fail-closed 解決フェーズ + stand_down event | high | — |
| D | [Design] Operator ボード状態モデル（Conclair schema） | medium | A, C（event 語彙） |
| E | identity 排他 claim（Magickit 側） | medium | T29（role registry） |
| F | session id 自己申告フック | medium | — |
| G | [Research/PoC] 常駐 Heisenberg（Monitor ツール方式） | medium | A, B, F、Takahito GO |
| H | 裏経路禁止 invariant | low | — |

三者ループ駆動: proposer Bohr / implementer Heisenberg / naysayer Einstein。A・C は既存の 9/14 修正プロンプトと同一スレッドで扱ってよい（個別修正 → 汎用 watchdog の順）。

## 5. リスク・注意

- A の watchdog が意図的な待機（human 待ち = NEXT: human）を stall と誤検出しないよう、「pending」は **配信対象が AI identity の未配信イベント** に限定する。
- B の readiness を SDK 側でどう検出するかは実装で確認が要る（初回イベント受領で十分か）。
- G は one-shot 設計と思想が異なる。PoC 結果を見てから ADR を起こす。先に ADR を書かない。

## 6. Takahito 判断点（Tier C）

1. G（常駐 Heisenberg PoC）に着手してよいか。one-shot 設計の再考を含むため。
2. E の claim を Magickit に置く（identity enforcement point を一本化）方針でよいか。T29 の role registry ADR と同時に扱うか、先行するか。
3. D の状態語彙（`awaiting_approval / working / idle / stalled / stand_down` × `observed / unobserved`）の採否。
