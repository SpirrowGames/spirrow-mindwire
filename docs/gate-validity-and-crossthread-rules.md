# Gate validity and cross-thread rules

Rules that downstream detectors — the `gate_fired` tool, the orphan-review
anomaly alarm, playproof-family checks — apply when reading the boundary
between the chatroom ledger and the GitHub PR review surface. Written for the
reader who is about to decide "did the naysayer gate fire on this PR?" from
observable state alone; keep this document narrow to that question.

## F-1: scoped-driver 経路の naysayer review の判定

### 事実

`scripts/naysayer_review_scoped.py` は gate 経路（`fire_pr_review` /
`naysayer.pr_review.PrReviewOrchestrator`）とは独立した実行経路である。この
script は chatroom に post する transport を持たない ∴ 実行しても
`T-pr-review-<repo>-<n>` は生成されない。GitHub PR に `spirrowgames-ops`
名義の review だけが残る。

### 判定 rule

判定器（`gate_fired` を返す tool / orphan-review anomaly alarm / playproof
系）は以下を採る:

1. **scoped 経路は `gate_fired = False` として扱う。** 「gate を撃った」の
   定義は `fire_pr_review` の発火であり、scoped script はこれを呼ばない。
   `gate_fired` の型は bool のまま — trinary state を導入しない。
2. **orphan review anomaly alarm は、review body が以下の marker で始まる
   場合 (`body.startswith(SCOPED_REVIEW_BODY_MARKER)`)、当該 review を
   アラーム対象から除外する:**

   ```
   <!-- naysayer:scoped-driver -->
   ```

   この marker は `spirrow_mindwire.naysayer.pr_review` の module-level 定数
   `SCOPED_REVIEW_BODY_MARKER` として単一定義されており、
   `scripts/naysayer_review_scoped.py` はこの canonical path から import
   し、`posted_body` の先頭に無条件に stamp する（`prepend_gate_notice` の
   prepend より前）∴ scoped 経路経由の review は必ず先頭一致する。

   **detector は literal 文字列を duplicate せず、`SCOPED_REVIEW_BODY_MARKER`
   を以下の canonical path から import すること**（drift 防止）:

   ```python
   from spirrow_mindwire.naysayer.pr_review import SCOPED_REVIEW_BODY_MARKER
   ```

   本 constant は `spirrow_mindwire.naysayer.pr_review` module に単一定義され、
   scoped script (`scripts/naysayer_review_scoped.py`) も同じ path から import
   して stamp する。`scripts/` 配下は package ではないため、そこから import
   しようとすると `ModuleNotFoundError` になる。

   会話的表現（"the scope" / "adjudicated scope" 等）による fuzzy match は
   用いない（LLM 出力に依存するため確定性が無く、false positive / false
   negative の両方を生む）。
3. **上記 marker で始まらない orphan review** は本当の台帳同期失敗である
   可能性があり、通常の anomaly として扱う（gate 経路の post-hoc thread
   deletion などが該当）。

### 判定不能経路の存在（qualitative unknown）

scoped script には以下 4 経路の zero-trace 実行がある。GitHub にも chatroom
にも痕跡が残らないため、判定器はこれらの発生を検知できない。operator
playbook 側の想定に含めておくこと:

- `--no-submit` フラグ（stdout のみ）
- CI-red 早期 return（`scripts/naysayer_review_scoped.py` L114-119 相当）
- Lexora timeout（`sys.exit(3)`）
- Empty reply（`sys.exit(4)`）

### 遡及的な注意（2026-09-11 orphan について）

本 rule が commit される前に生じた orphan review（`SpirrowGames/spirrow-mindwire#263`
の 3 件、2026-09-11T15:02-15:07Z）は body marker を持たない ∴ 本 rule では
「scoped 経路と判定できない」= 通常 anomaly 扱いになる。これらは
`T-scoped-driver-verdict-never-reaches-chatroom` msg-3372 §2 で「文体
シグネチャ」（`=== BINDING SCOPE FOR THIS REVIEW ===` を含む scoped script
プロンプト template 由来の会話的言及）により scoped 起源と手動確認済みで
あり、alarm が発火した場合は operator が個別に既知例外として扱うこと。
marker が commit された後の新規 scoped run は自動的に除外される。

### 参照

- 起票: `T-scoped-driver-verdict-never-reaches-chatroom` msg-2775 (F-1)
- 測定 report: 同スレッド msg-3372 (2026-08-28 導入以降 3 週間で orphan 3
  件、全て mindwire#263 に集中、signature match 済み)
- decide: 同スレッド msg-3373 (option (c) 現状維持 + 明示 を採用)
- naysayer 反復: msg-3374 / msg-3376 / msg-3378 / msg-3371 (Einstein の
  objection 群を各 turn で採用し、fuzzy match → literal signature → module
  constant による programmatic marker、と反復的に堅牢化)
