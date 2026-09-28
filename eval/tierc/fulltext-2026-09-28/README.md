# Tier-C fulltext eval — 500 vs 8,000 chars (2026-09-28)

Thread: `T-decider-tierc-fulltext-eval`. Design: Bohr msg-4311 / msg-4313 / msg-4318, amended by msg-4327 / msg-4329 (runs) and msg-4332 / msg-4334 (headline, persistence). Every file's sha256, generating commit, seed and exact command is in `manifest.json`; the numbers are in `report.fulltext.md`.

## Headline (quote these three lines together, never one alone — msg-4332)

> **条件付きで続行（ただし全文にした効果は無く、固定した閾値での削減は 0/12）**
> - Jev の判別力は 500 文字の時点からありました（一致した正解で AUC 0.705、下限 0.532）。全文にしても上積みは有意ではありません（ΔAUC +0.025 [−0.131, +0.176]）。
> - 固定した閾値 0.60 / 0.40 / 0.60 では、3 回とも spurious を 1 件も減らせていません。判別力はあっても、今の閾値ではそれを使えていません。
> - 正解を frontier 単独にすると、どの側でも AUC の区間が 0.5 を含みます。判別力があるかどうか自体が、正解の付け方で変わります。

Conditions for continuing (msg-4332 §1): (1) collect more real escalations — billing, Takahito's call; (2) re-fit the thresholds on new data — the heaviest condition, since reduction was 0/12; (3) any proposal to widen the live `turn_from_messages` input must not cite this result — no fulltext effect was seen; live stays at 500 chars.

| truth (genuine* / spurious) | AUC 500 orig | AUC 500 re-run | AUC 8,000 | ΔAUC 8,000−orig | ΔAUC re-run−orig (noise) | reduction | recall genuine* |
|---|---|---|---|---|---|---|---|
| consensus (37 / 12) | 0.705 [0.532, 0.860] | 0.705 | 0.730 [0.579, 0.862] | +0.025 [−0.131, +0.176] | +0.000 [−0.047, +0.050] | 0/12 ×3 | 37/37 ×3 |
| naysayer-tier (50 / 16) | 0.712 | 0.708 | 0.744 [0.605, 0.866] | +0.032 [−0.089, +0.152] | −0.004 | 0/16 ×3 | 50/50 ×3 |
| frontier-tier (46 / 19) | 0.546 | 0.555 | 0.569 [0.421, 0.717] | +0.023 [−0.105, +0.150] | +0.010 | 0/19 ×3 | 46/46 ×3 |

## Limits

- **The pre-fixed criterion was flawed (msg-4332, Bohr's own design issue).** msg-4327 §2 yields "conditional continue" from discrimination that already existed at 500 chars, independently of the question this eval asked (does fulltext make Jev discriminate?). The answer to that question is no.
- **The change-rate measures are exploratory**, not fixed in advance: verdict change at the fixed thresholds, share of answers moved by ≥ 0.05, mean |Δ| (defined by the implementer after the run).
- **The noise interval is narrow**: ΔAUC re-run−orig is +0.000 [−0.047, +0.050] and same-input answers move 0.011 on average, while 500→8,000 moves 0.071 on spurious rows (58% of answers ≥ 0.05). Jev visibly reads the full text; its discrimination does not change.
- **Cache check**: no response cache was concluded from 3 same-input re-runs whose answers differed by 0.01–0.02 with a new `decision_id` each time. The client code has no cache and Lexora's `/v1/decide` OpenAPI has no cache field; Lexora's server code was **not** read (it is not in this repository).
- **The 3 cache-probe rows** (listed in `manifest.json`) were not sent back to back with their full side; every other row was, in the seeded order in `plan.json`.
- **22 of the 66 rows exceed 8,000 chars** and were cut from the head, as the labellers read them; every consensus-spurious row is ≤ 8,000 chars (2,306–7,370), so the reduction side is unaffected by the cut. No control group (≤ 500 chars) exists among the 66, hence the 500-char re-run as the noise baseline.
- **Not read by the implementer**: msg-4309 / msg-4310 (cut from the context; msg-4313 consolidates them) and the body of ADR-2026-05-29-13.
