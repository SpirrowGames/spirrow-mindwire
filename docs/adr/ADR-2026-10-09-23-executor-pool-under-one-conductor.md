# ADR-2026-10-09-23: 中央 1 Conductor ＋ 実行機プール（executor pool under one conductor）

> **実インフラ値**（ホスト名 / IP / パス）は [[platform:infra-registry]] が正本。この文書は `{{PLACEHOLDER}}` で参照する（規約 §3.1）。msg-6739 からの逐語転記のうち、ホスト名だけはこの規約に従って置き換えている。

- **Status**: Accepted（決定 D1〜D17 は takahito 承認済み — Fermi `T-executor-pool-under-one-conductor` msg-6739。設計 v1.2 は Einstein msg-6765 で go、確定版は Bohr msg-6766）
- **Date**: 2026-10-09
- **Scope**: 開発機の拡張（実行機を複数台にする）に伴う、ループの実行トポロジ。中央の Conductor 1 本が、能力を宣言した実行機のプールにターンを振り分ける。本 ADR は決定事項・段階表・未決の数値・単位間の要件を記録する。各単位の詳細 spec は単位ごとのスレッドで確定する（単位 1 = 本スレッド msg-6766）。
- **Author**: Heisenberg (implementer) — 起票 chatroom thread `T-executor-pool-under-one-conductor`
- **Relates to**:
  - `T-agmsg-transport-lessons-readiness-session-claim-board`（T45〜T49: 占有の設計）— D6 で合流させる
  - `T-sweep-starves-deep-candidates` — 単位 5（フェーズ 2）で公平性と合流させる
  - `T-silent-stops-need-a-generic-watchdog-and-a-loud-stand-down` — park 理由の明示（停止理由を黙って消さない）
- **改訂種別**: 新規 ADR

---

## 1. 背景

msg-6739 の背景を、言い換えずに写す。

- 開発機を拡張する。UE 機 2 台（5950X/3090 = VoxelWorld 用、Intel/RX9060XT 16GB = PlayProof 用 兼 CI・nightly）を愛媛に集約し、{{HOST_SERVICES}} は東京へ移設する。
- このまま各機に Conductor を置くと、スレッドの二重駆動と checkpoint の上書きが起きる。

### 1.1 根拠：調査で確認した事実（コード実読、msg-6739）

1. sweep はローカルの sweep.json 候補だけで駆動しており、サーバー側にスレッドの占有ロックはない。
2. checkpoint の保存キーは project×user×author で、スレッドを含まない。
3. Conclair の既読カーソルは project×identity×thread をキーにしている。
4. loop_control の HOLD はサーバー側にあり、全機に共通で効く。

---

## 2. 決定事項（D1〜D17）

msg-6739「決定事項」1〜17 を、条項番号 D1〜D17 として言い換えずに写す。

- **D1. 中央 1 Conductor ＋ 実行機プール。** tomtebo も実行機の 1 台として扱い、特別扱いしない。実行機台帳を置かない状態が、現行の単機構成と完全に同じ動作になること。前例は owner_map の「キーがなければゲート OFF」と同じ流儀。
- **D2. 能力による振り分け。** 実行機は capabilities（例: `ue`）、slots、clones（project→clone パス）、wol を持つ。repo_dir は sweep の候補から外し、実行機側の clones に移す。ターンの要件 `requires` は、候補ごとの静的な宣言を既定とし、handoff で上書きできる枠を最初から用意する（Bohr の reproduce-first のターンでも UE が要る場合があるため）。
- **D3. 実行機の選び方。** 起きていて空きスロットがある → 同じスレッドの前回の実行機（速度のための優先で、必須ではない）→ 空きの多い機械、の順。全員寝ていれば WoL で 1 台だけ起こす。
- **D4. オフライン時の二段構え。** WoL ゲートウェイで起こして実行し、失敗したら park して停止理由に「実行機オフライン」を明示する。
- **D5. ターン途中で実行機を喪失した場合。** 自動では再試行せず、park（「実行機喪失」）。依頼を受け取る前の失敗だけは、別の機械へ回してよい。
- **D6. ターン依頼・占有権・実行機台帳・死活状態・現バージョンは Conclair に置く。** Magickit はオーケストレーションに専念する。
- **D7. git 同期はプログラムで行い、LLM のルールにしない。** オプションで既定は OFF（`[turn.git] sync`）。
  - ターン前: fetch（＋必要なら lfs pull）→ 未コミット変更があれば park → スレッドブランチを fast-forward のみで追従（ローカルが先行していれば park）
  - ターン後: 未コミット変更があれば park → スレッドブランチを push（失敗は park）
  - 自動コミットや stash はしない。main / develop へは構造上 push できないようにする。
- **D8. ブランチ名は機械的に決める。** `loop/<thread_id>`（小文字に正規化、長さ上限を超えたら末尾をハッシュ化）。merge 済みなら `-2`, `-3` と連番で切り直す。進行中の既存スレッドは上書き指定で現行のブランチ名を維持する。
- **D9. 秘密情報は各実行機が起動時に Vaultwarden（bw CLI）から取得する。** bootstrap 用の鍵は Windows 資格情報マネージャーに置く。取得できなければ、はっきり止まる（前回値のキャッシュで動き続けない）。
- **D10. バージョンは git の不変タグを正とする。** セマンティックバージョンで、タグは動かさず新規作成していく（`executor/vX.Y.Z`、注釈付きタグ）。ロールバックは「古いコミットに新しいタグを作る」前進操作。`executor/*` は GitHub ルールで削除・更新を禁止し、作成は Magickit のデプロイ経路のみに限定する。メジャーは Conductor⇔実行機のプロトコル非互換を表すが、当面は完全一致で照合する。
- **D11. 実行機は tick 冒頭で最新のタグと自分のバージョンを比べ、遅れていれば sync-repo してから依頼を受け付ける**（pull 型の追従）。実行機の処理は Conductor と同じく tick ごとに起動して終わる単発型にし、自己更新の問題を回避する。依頼にはタグ名を載せ、不一致なら拒否して park（「実行機バージョン不一致: 手元 X / 依頼 Y」）。
- **D12. 切り替え時刻は mindwire（実行機）が記録主体となり、Magickit の deploy_history に「どの機械がいつ切り替わったか」を報告する。** Conclair は現バージョンだけを持ち、履歴は持たない。
- **D13. 利用予約。** 人が UE 機を使うときは、Claude のセッションから予約できる MCP ツール（予約・延長・解除）を Magickit に用意する。予約中は新しい依頼を振らず、実行中のターンは最後まで終わらせる。人は解除を忘れる前提なので、予約には必ず期限を付けて自動で失効させる。
- **D14. CI との共存。** 最終形は CI の仕事も実行機の依頼として流す（b）。段階的に、まずは self-hosted runner のジョブの開始・終了に合わせて受付停止を切り替える（a）。
- **D15. 移設からフェーズ 1 完成までは、ループは tomtebo の Conductor 1 本で回す。** UE 機は人の手作業と CI だけに使う。この順序なら、暫定の修正（host 付き sweep.json、checkpoint の改修）はどちらも不要になる。
- **D16. 単一スレッドの分割禁止（9/5 裁定）と、「1 プロジェクトにつき同時に動く Heisenberg は 1 本」を、中央スケジューラの制約として実装する。**
- **D17. 投稿のメタデータに実行機名を載せる。**

---

## 3. 不変条件

- **INV-0（設定がなければ現行と同じ動作）。** どの単位も、その単位の設定がなければ現行と同じ動作をする。単位ごとに、設定 OFF 時の動作を固定するテストを必須とする（owner_map のゲート OFF と同じ流儀、D1）。

---

## 4. 段階表（実装単位と依存関係）

各単位は独立にリリースでき、未設定なら現行と同じ動作になる（INV-0）。

| 単位 | 内容 | 決定 | 前提 | 実装先 |
|---|---|---|---|---|
| 1 | git 同期フック | D7・D8 | なし（単一機でも価値がある） | spirrow-mindwire |
| 2 | Vaultwarden bootstrap | D9 | なし。**期限 2026-11-26**（implementer PAT の失効日） | spirrow-mindwire |
| 3 | Conclair: 実行機台帳・死活状態・現バージョン・ターン依頼と占有権・予約 | D6・D13 | なし | spirrow-conclair（別スレッドに分割） |
| 4 | フェーズ 1: リモート Implementer の実装と振り分け ＋ Magickit 側（deploy_history への報告受付、タグ作成経路、予約ツール） | D1〜D5・D10〜D13・D17 | 単位 3、および移設の完了（D15） | spirrow-mindwire ＋ spirrow-magickit |
| 5 | フェーズ 2: 並列化と全体の同時実行数上限 | D16 | 単位 4。`T-sweep-starves-deep-candidates` の公平性と合流 | spirrow-mindwire |
| 6 | CI の依頼化 | D14 (b) | 単位 4 | — |

- フェーズ 1（単位 4）では並列化しない。同時に進むのは全体で 1 スレッドで、リモートのターンは完了を待つ。

---

## 5. 未決の数値（計測してから決める）

以下は本 ADR では**未決**とする。値を決めるときは、計測結果を添えて本 ADR を改訂する。

- 全体の同時実行数の上限と slots の値（3 台時の Claude Max 20x の消費を計測してから）
- 死活確認の間隔と WoL の起動待ち時間（UE 機の起動時間を実測してから）
- 予約の既定の期限

---

## 6. 単位 1 の既知の制約

- ターン後に clone を汚したまま終わると（未コミット変更、途中の git 操作、HEAD の移動）、汚したスレッドは `git.dirty_after_turn` / `git.head_moved` で park されて通知される。その clone は、オペレーターが片付けるまで dispatch を受け付けない（clone guard の exit 8）。
- 自動で片付ける（stash / reset / clean）ことは D7 と clone guard の方針「検出して拒否、片付けない」で禁じられている。
- この影響範囲は、単機運用の現在と同じである。プール環境での影響範囲の縮小は、次節の単位 4 への要件で扱う。
- ターン前に clone が汚れていた場合は、clone guard（exit 8、隔離なし）に任せ、これから dispatch されるスレッドは park しない。前のターンが残した汚れは、そのスレッドの責任ではないためである。

---

## 7. 単位 4 への要件

- **(a) clone 単位で外す。** Conclair の死活状態に `clone_dirty` を持たせる。スケジューラ（D3）は、汚れを検出した「実行機 × clone」の単位で候補から外し、他の実行機に振る。実行機全体は外さない。
- **(b) スレッドごとの worktree（再提案の候補）。** スレッドごとに git worktree を分けて汚れを隔離する案は、単位 4〜5 で再提案する候補として記録する。D2 の「clones = project→clone パス」を変更することになるので、採用するときは改めて提案する。

---

## 8. 関連スレッド

- `T-agmsg-transport-lessons-readiness-session-claim-board`（T45〜T49）— 占有の設計。D6 で合流させる。
- `T-sweep-starves-deep-candidates` — 単位 5（フェーズ 2）。
- `T-silent-stops-need-a-generic-watchdog-and-a-loud-stand-down` — park 理由の書式（`git.<code>: <thread_id> <branch> <要約>`）を、停止理由を黙って消さない既存の経路に載せる。
