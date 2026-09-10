# ADR-2026-05-21-04: 根本要求の再確認と tomtar 廃案、mindwire の責務再定義

## Status

Accepted (2026-05-21, user 確定)

## Context

`spirrow-tomtar` の設計検討中、mindwire の README を改めて読んだことで重大な認識ズレが判明し、user との対話で根本要求まで遡って整理した結果、設計全体を更新する必要があった。

### 認識ズレの経緯

過去の design phase で「tomtar = REACTIVE dispatcher / 観測→判断→信号」と命名された時点から、設計議論は「tomtar をどう作るか」に集中していた。この間、根本要求(= 何を解決したいのか)への参照が薄れ、結果として:

- `tomtar / mindwire / conclair` の 3 層モデルを誤って構築
- mindwire を「control plane」、 conclair (chatroom) を「data plane」とする ADR を 3 件作成 (ADR-2026-05-21, -02, -03)
- Phase 1 derived tasks T02-T08 を「mindwire webhook server + tomtar daemon」として派生
- 一方で mindwire の README には「Claude.ai と Claude Code を filesystem 越しに対話させる中継 hub」と明記されており、 我々が想定していた「control plane」とは無関係

これは典型的な solution-first thinking の drift。設計者(本セッションの Claude)は「先回りして詳細化する癖」を自覚し、根本要求に立ち返るタイミングを逸した。

## Decision

### 1. 根本要求の確認

**根本要求**: 「AI 三者合議開発において、 三者間通信を人間がやらなきゃいけない状態を解消して、 自律的にやり取りが出来るようにしたい」

具体: 人間は「最初の問い」と「最終承認」だけを行う。 chatroom thread での議論・naysayer 起動・proposer への依頼・claude-code への実装依頼などは AI 群が自律的に回す。

### 2. 議論の場 = magickit chatroom (= conclair)

AI の議論は magickit chatroom 上で行われる。 議論は project に紐付き、 magickit が project 管理を兼ねているので、 chatroom がそこに統合されているのは合理的。 chatroom = conclair の本質的実装関係は ADR-2026-05-21 で確認済み。

### 3. mindwire の責務 (再定義)

mindwire は **chatroom クライアントの自動操縦 layer**。 過去 ADR で記述した「control plane (signaling)」は誤り。 mindwire が解決すべきは以下のような **人間による橋渡し作業**:

- 「naysayer に thread `thr_xxx` を確認してくれと頼む」
- 「proposer に新規 thread を立てて議題 X を提起してくれと頼む」
- 「proposer にレスが来てるから対応してくれと頼む」

これらを人間が AI 群に毎回伝えていた状態を、 mindwire が自動で行うようにする。 これが mindwire (Mind + Wire = 思考を線で繋ぐ) の命名意図。

filesystem 上の thread directory / 専用ブラウザ / 専用ターミナル などは、 この「自動操縦」を実現するための**実装手段**であって、 設計の本質ではない。

### 4. tomtar は廃案

新しい構造では mindwire と chatroom (= conclair) の 2 service で完結する。 tomtar に予定していた責務:

- 観測 → mindwire が自分で chatroom を見れば良い
- 判断 → mindwire の dispatcher 内で済む
- signal 発火 → mindwire 内呼び出しで済む
- instance 起動 → mindwire の責務
- 暴走防止の番人 → mindwire の watcher 内に組み込む

全て mindwire 内に吸収可能であり、 tomtar を独立 service にする実装上の正当化理由が無い。

過去の ADR で書いた「tomtar = REACTIVE dispatcher / AI 推論しない / 暴走防止の番人」などの設計概念は、 mindwire の責務に統合される。 概念ごと放棄するわけではないが、 別 daemon にする必然性は失われた。

### 5. claude-code も chatroom の対等な participant

claude-code は「実装担当」固定ではない。 状況に応じて自分で thread を立て、 proposer や naysayer の意見を求めることもある。 したがって 3 役 (proposer / naysayer / implementer) は thread ごとに動的に割り当たり、 「claude-code は常に implementer」のような静的紐付けは設計に入れない。

### 6. naysayer の独立性は要件

naysayer は claude.ai 固定ではなく、 別モデル (Gemini / local Qwen など) を担当者とする可能性が前提。 これは trilateral debate の本質である「同じモデルの blind spot を共有しない independent verification」を担保するため。

→ 設計含意: mindwire の dispatcher は **model agnostic な抽象化レイヤー**を必須とする (ADR-2026-05-21-05 で詳述)。

### 7. 自動操作の手段は不問

claude.ai を自動操作する手段 (Claude in Chrome / OpenCrow / 専用ブラウザ + selenium / Anthropic Console API / Computer Use 等) は実装フェーズで選定する。 設計の本質ではない。 dispatcher の port (interface) を切り、 adapter を後から差し替え可能にする。

## Consequences

### Positive
- 根本要求と設計が直結する状態に復帰
- mindwire 1 service で完結し、 maintenance 負荷が軽い
- chatroom (magickit 既存) + mindwire (役割転換後) で全ての三者合議が回る
- naysayer 独立性が architecture level で担保される
- 自動操作の手段を実装フェーズに先送りでき、 候補 (Claude in Chrome / OpenCrow など) を実物で比較できる

### Negative
- 既存 ADR 3 件 (-01 chatroom=conclair / -02 入力経路 / -03 signal channel) は **superseded**: 廃案ではないが、 適用範囲が変わる (詳細は各 ADR の status 欄を後日更新)
- 既存 Phase 1 task T02-T08 は廃止対象
- mindwire 自身も現状実装 (2 ペルソナ専用) から N ペルソナ・abstract adapter モデルへの拡張が必要 = mindwire の Phase 2 相当の作業量

### Neutral
- spirrow-tomtar Magickit project は archive せず保留状態とする。 議論履歴は知的資産として残す。 将来「上位 supervisor として再生する」可能性は完全には捨てない (Phase 3+ 検討対象)

## Implementation Notes

- 旧 ADR 3 件 (-01, -02, -03) は本 ADR-04 によって superseded
- Phase 1 task T02-T08 は priority=low + status=cancelled 相当に格下げ (Magickit に「キャンセル」状態が無い場合は description 先頭に [CANCELLED by ADR-04] と注記)
- 新規 task T02'-T08' (mindwire 役割転換) を Phase 1 に追加
- 設計者 (Claude) の reflection: solution-first thinking に陥ったら根本要求を再確認するという内省を Phase 1 全体で繰り返し意識する

## Related
- ADR-2026-05-21 (chatroom = conclair) → superseded scope changed
- ADR-2026-05-21-02 (入力経路 webhook) → superseded
- ADR-2026-05-21-03 (signal channel 設計) → superseded
- ADR-2026-05-21-05 (役割と adapter の抽象化) → 本 ADR の続編
