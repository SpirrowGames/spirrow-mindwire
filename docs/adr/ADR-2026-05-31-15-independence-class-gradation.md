# ADR-2026-05-31-15: independence-class グラデーション化と「2 協調 1 独立」配置

- **Status**: Draft（ローカル develop。Takahito GO 後に Drive 反映 = ADR-07 §2.5 / Tier C）
- **Date**: 2026-05-31
- **Scope**: spirrow-mindwire（role/identity 規範定義 — CLAUDE.md §M 対象）
- **Author**: Heisenberg (implementer, terminal_coding_agent) — chatroom T-T15-poc-h-phase1-kickoff の trilateral decide (Bohr proposer / Einstein naysayer) を反映
- **Amends**: ADR-2026-05-27-09（T28、identity 4 レイヤーモデルの independence_class レイヤー）。本 ADR は独立性を二値からグラデーションに拡張する補強であり、T28 の 4 レイヤー直交分離自体は据え置く。
- **Relates to**: ADR-2026-05-31-14（ガワ方式撤回。本配置の発生源）、ADR-2026-05-24-08（instance-identity §2.4 = 独立性を model_binding 属性へ移す end-state）、ADR-05（RoleAdapter / Capability.NAYSAYER_QUALIFIED）

---

## 1. Context

ADR-09（T28）の identity 4 レイヤーモデルは independence_class を二値（main-chain / independent）で扱ってきた。T15 のガワ方式撤回（ADR-14）に伴う役割×インフラ再配置の検討で、この二値モデルの限界が露呈した。

trilateral（Bohr/Heisenberg/Einstein）が当初すべて Claude tab（同一訓練分布）で動いていた構成では、naysayer の Einstein が「独立検証」していても、独立しているのは context / commitment の分離（別セッション）だけで、**同一訓練分布由来の共通盲点・収束バイアスは naysayer にも同じく効いていた**。つまり「independent」の実体が「別セッション」止まりで「別分布」に達していなかった。

ガワ方式撤回で proposer/implementer を Claude Code に寄せる際、naysayer をどこに置くかを「役割が要求する独立性レベル」から導く必要が生じた。independence を二値で扱う限り、この「役割ごとに必要な独立性の度合いが違う」という事実を表現できない。

---

## 2. Decision

### D-1: independence-class をグラデーションとして扱う

independence_class を二値（main-chain / independent）から**グラデーション**に拡張する。各役に必要な独立性レベルを、素性（どのモデルか）ではなく**役割要件**から導く。

| 役割 | インフラ | 独立性レベル | 配置理由（役割要件） |
|---|---|---|---|
| proposer (Bohr) | Claude Code | 中（対 implementer ~40-60%） | 提案↔実装の協調速度。共有理解で handoff 軽量 |
| implementer (Heisenberg) | Claude Code | 中（対 proposer ~40-60%） | 同上 |
| naysayer (Einstein) | Gemini 3.1 Pro API | 高（別訓練分布） | 共通盲点を外から突く。最大の独立性が要る役 |

### D-2: 「2 協調 1 独立」配置

proposer↔implementer を同系列（Claude Code）に置いて協調速度を取り、naysayer を別訓練分布（Gemini）に置いて最大独立性を取る。

- 同一モデルのセッション分割で除けるのは文脈依存・コミットメント維持バイアスまで。**同じ訓練分布由来の共通盲点・推論の癖・収束バイアスは系列分離では届かない**。
- 一番客観性が要る naysayer を別訓練分布に置くことで、proposer/implementer が共有ゆえに両者とも気づけない領域に手が届く。
- 3 者全独立は協調コストでテンポを失い、3 者全同一は共通盲点を誰も突けない。その中間の最適配置。

### D-3: §0 整合 — 順序は「役割要件 → 独立性 → インフラ」

配置の決定順序を規範として固定する。**「Einstein が Gemini だから naysayer」（素性で役割決定）ではなく、「naysayer は最大の独立性が要る役だから、独立性を最も出せる別分布を充てた」（役割要件で独立性を決定）**。proposer/implementer が Claude Code なのも「能力が劣るから」でなく「協調速度が要る役だから同系列で十分」という積極的理由。

これは ADR-10 N-3（§0 をループ外権限の正当化に流用しない）/ ADR-12 N-1（§0 は許容であって演繹元でない）と同じ「順序を正す」規律の適用。

---

## 3. トレードオフの併記（失う面・引き受けるリスク）

別モデル naysayer 化は独立性を上げるが、利点だけでなく代償を規範として明記する（「別分布＝独立性向上」だけを書かない）。

### N-1: 内部文脈の喪失（プロセス上のトレードオフ）

別モデル naysayer は、共通盲点を外から突ける代わりに、**Claude 系の内部文脈（ADR-09 §0 の趣旨 / F-09 の教訓 / partition-key 事故の当事者性 等）を共有しない**。

- 現状の Claude naysayer は「ADR-13 のテーマ（注意力依存を構造に倒す）」を内側から理解して naysay できる（PR #75/#76 で「ledger の定義と実装の食い違い」を見つけられたのはこの内部理解ゆえ）。
- 外部モデルが同精度の指摘を出せるかは **context 注入の質に完全依存**する。
- **対策の実体**: naysayer 呼び出し時に ADR 全文 + 関連 diff + thread 全文を明示 bundle 化して渡す層（context-bundle builder）を設ける。Gemini の 2M context により Cognilens 8192 截ち落としは解消され、bundle を圧縮なしで丸渡しできる。
- **naysay の種類で最適 naysayer が異なりうる**: 構造的見落とし＝別モデルが強い / 文脈整合性＝Claude 系が強い。共謀収束が「共通盲点由来」なら別モデルが、「文脈読み違い由来」なら内部理解のある Claude naysayer が強い。どちらが起きやすいかは実運用で観測する（§4）。

### C-2: 機密の外部移動（データ統治上のリスク）

naysayer の Gemini 化は、SpirrowGames の private な設計情報（ADR / diff）を **Anthropic 境界の外（2 社目ベンダー Google）に開く**決定を含む。これは T15 自身が認証 4 軸で避けようとした「機密の外部移動」と同型。

- **防御線（改訂後）**: **paid 鍵＝必須不変条件（最後の防御線、非訓練）** + ZDR＝推奨。当初 paid+ZDR の二重防御を必須としていたが、Takahito 決定（2026-06-01）+ trilateral 収束（`T-zdr-invariant-downgrade` msg-373）で ZDR を推奨へ格下げ。データ統治の詳細・再必須化トリガー・検知点・paid 鍵の構造的保証は **ADR-14 D-4 が SOT**（本項と共有）。
- ただし「Anthropic 境界の外に出す」事実自体は消えない。paid 鍵は学習利用を防ぐが、境界外移動そのものの解消ではない。
- **N-3（実害度の異なる部分集合）**: naysayer 経路に渡る設計情報は一括りにできない。ゲームプレイ/コンテンツ設計は実害低だが、**セキュリティ構成・未公開脆弱性に触れる naysay（T15 認証4軸・Vaultwarden/firewall 構成議論、PR の脆弱性指摘）は実害度が異なり、かつ naysayer 経路を実際に通っている**（再帰構造）。この部分集合は ADR-14 D-4 の再必須化トリガー (ii) で ZDR 必須側に分岐する。
- adapter 層で「素の `generateContent` 以外を呼ばない」「bundle に含める機密を最小化」を gate として実装（ADR-14 D-4 と共有、ZDR 要否と独立の surface 強制）。

---

## 4. 段階移行（スピード重視で最小化）

完全移行前に Claude naysayer と Gemini naysayer を並走観測するが、遠回り分を取り戻すため最小化する:

- 並走は **次に来る ADR/PR レビュー 2〜3 件**に限定（期間でなく件数で区切る）。
- 観測項目は 1 点に絞る: 「proposer/implementer の共謀収束を、Gemini naysayer が Claude naysayer と同等以上に破れているか（共通盲点由来 vs 文脈読み違い由来、どちらに強いか）」。
- 2〜3 件で実用水準と確認できたら即完全移行。N-1 の文脈喪失が実害として出た場合のみ context-bundle builder を強化して再評価。だらだら両走を続けない。
- 観測結果は ADR-13 ledger に 1 エントリ化（別 PR）。

---

## 5. Consequences

### Positive

- 役割ごとに必要な独立性を表現できる（二値では潰れていた粒度）。
- naysayer が名実ともに独立（別分布）になり、共謀収束を破る力が上がる。
- 協調速度（proposer↔implementer）と外部検証（naysayer）の両立。

### Negative / Cost

- N-1（内部文脈喪失）と C-2（機密外部移動）を恒常的に引き受ける。context-bundle builder とデータ統治 gate の実装・維持コスト。
- Gemini paid 運用コスト（往復込み月 $10〜30 見積り、Takahito 承認済）。

### Neutral

- Phase 1 の `qualified_for` / `NAYSAYER_QUALIFIED` 機構（ADR-05 / ADR-08 §2.4）は当面維持。independence_class のグラデーション値を型に昇格させるのは、ADR-2026-05-24-08 §2.4 の model_binding 型化（Phase 2）と歩調を合わせる。

---

## 6. ADR-09 / ADR-08 / ADR-05 との関係

- 本 ADR は **ADR-09（T28）の independence_class レイヤーを補強**する（二値→グラデーション）。T28 の 4 レイヤー直交分離（identity_name / independence_class / role / 稼働形態）自体は据え置き、independence_class の値域だけを拡張する。T28 本体の §M 参照は維持する。
- **ADR-2026-05-24-08（instance-identity）§2.4** は「naysayer 独立性を model_binding の属性へ移す（別モデルファミリーであることが capability の根拠）」を end-state として既に予告しており、本 ADR の Gemini naysayer はその実体化。独立性グラデーション値の型昇格は §2.4 の model_binding 型化（Phase 2）と対で扱う。
- **ADR-05** の Capability.NAYSAYER_QUALIFIED 判定機構は引き続き Accepted。本 ADR は「別モデルファミリー」を NAYSAYER_QUALIFIED の根拠として明確化する。


---

## 6. Amendment (2026-06-04): C-2 データ統治 gate の無効化 — Takahito 権限・判断

**本改訂は trilateral 議論を経ていない。Takahito（human owner）の権限・判断で決定し、指示により本欄へ明記する。** 決定主体は Takahito 単独で、通常の §M プロセスに対する例外。

§3 C-2 の防御線として記した adapter gate（「素の `generateContent` 以外を呼ばない」surface 強制）を、spirrow-lexora の Gemini (naysayer) backend で **無効化**した。具体的には config トグル `governance_gate_enabled: false`（既定 True）により、`tools` / grounding / cached content / 非テキスト part の拒否を解除している。

- この gate は **ADR-2026-05-31-14 D-4 が SOT**（C-2 はそれを共有）。決定内容・トレーサビリティ・非変更点（paid 鍵不変条件と ZDR 推奨は不変）・再有効化手順の詳細は **ADR-14 §6 Amendment を参照**。
- 実装: spirrow-lexora commit `c9aa914`（`main`、push 済）。gate 無効時はリクエストごとに `gemini_governance_gate_disabled` warning をログ出力。
- C-2 が指摘する「機密の外部移動」リスク本体（Anthropic 境界外への設計情報移動）は本改訂では変わらず引き受けたまま。緩和したのは surface 強制 gate のみで、paid 鍵による学習利用防止は維持。
