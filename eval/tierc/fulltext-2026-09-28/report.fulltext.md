# Tier-C fulltext eval — 500 vs 8,000 chars (T-decider-tierc-fulltext-eval)

rows: 66; bootstrap: 10000 paired resamples, seed 20260928; thresholds genuine_min=0.6 genuine_max=0.4 spurious_min=0.6

## truth = consensus (genuine* 37 / spurious 12)

| measure | value | 95% CI |
|---|---|---|
| AUC orig | 0.705 | [0.532, 0.860] |
| AUC base | 0.705 | [0.532, 0.862] |
| AUC full | 0.730 | [0.579, 0.862] |
| Δ full-orig | +0.025 | [-0.131, +0.176] |
| Δ base-orig (noise) | +0.000 | [-0.047, +0.050] |
| Δ full-base | +0.025 | [-0.138, +0.180] |
| reduction / recall genuine* — orig | 0/12 / 37/37 | |
| reduction / recall genuine* — base | 0/12 / 37/37 | |
| reduction / recall genuine* — full | 0/12 / 37/37 | |

| rows | pair | n | verdict changed | answers moved ≥0.05 | mean abs change |
|---|---|---|---|---|---|
| spurious | orig→base (noise, same input) | 12 | 0/12 | 0.0% | 0.011 |
| spurious | orig→full (500→8000) | 12 | 0/12 | 58.3% | 0.071 |
| spurious | base→full (same session) | 12 | 0/12 | 54.2% | 0.068 |
| genuine* | orig→base (noise, same input) | 37 | 0/37 | 1.8% | 0.012 |
| genuine* | orig→full (500→8000) | 37 | 1/37 | 63.1% | 0.085 |
| genuine* | base→full (same session) | 37 | 1/37 | 63.1% | 0.084 |
| all | orig→base (noise, same input) | 66 | 0/66 | 1.5% | 0.012 |
| all | orig→full (500→8000) | 66 | 2/66 | 60.4% | 0.080 |
| all | base→full (same session) | 66 | 2/66 | 58.8% | 0.078 |

## truth = naysayer-tier (genuine* 50 / spurious 16)

| measure | value | 95% CI |
|---|---|---|
| AUC orig | 0.712 | [0.567, 0.841] |
| AUC base | 0.708 | [0.562, 0.840] |
| AUC full | 0.744 | [0.605, 0.866] |
| Δ full-orig | +0.032 | [-0.089, +0.152] |
| Δ base-orig (noise) | -0.004 | [-0.041, +0.030] |
| Δ full-base | +0.037 | [-0.091, +0.161] |
| reduction / recall genuine* — orig | 0/16 / 50/50 | |
| reduction / recall genuine* — base | 0/16 / 50/50 | |
| reduction / recall genuine* — full | 0/16 / 50/50 | |

| rows | pair | n | verdict changed | answers moved ≥0.05 | mean abs change |
|---|---|---|---|---|---|
| spurious | orig→base (noise, same input) | 16 | 0/16 | 1.0% | 0.012 |
| spurious | orig→full (500→8000) | 16 | 0/16 | 58.3% | 0.070 |
| spurious | base→full (same session) | 16 | 0/16 | 53.1% | 0.068 |
| genuine* | orig→base (noise, same input) | 50 | 0/50 | 1.7% | 0.012 |
| genuine* | orig→full (500→8000) | 50 | 2/50 | 61.0% | 0.083 |
| genuine* | base→full (same session) | 50 | 2/50 | 60.7% | 0.082 |
| all | orig→base (noise, same input) | 66 | 0/66 | 1.5% | 0.012 |
| all | orig→full (500→8000) | 66 | 2/66 | 60.4% | 0.080 |
| all | base→full (same session) | 66 | 2/66 | 58.8% | 0.078 |

## truth = frontier-tier (genuine* 46 / spurious 19)

| measure | value | 95% CI |
|---|---|---|
| AUC orig | 0.546 | [0.390, 0.701] |
| AUC base | 0.555 | [0.401, 0.709] |
| AUC full | 0.569 | [0.421, 0.717] |
| Δ full-orig | +0.023 | [-0.105, +0.150] |
| Δ base-orig (noise) | +0.010 | [-0.021, +0.041] |
| Δ full-base | +0.013 | [-0.119, +0.142] |
| reduction / recall genuine* — orig | 0/19 / 46/46 | |
| reduction / recall genuine* — base | 0/19 / 46/46 | |
| reduction / recall genuine* — full | 0/19 / 46/46 | |

| rows | pair | n | verdict changed | answers moved ≥0.05 | mean abs change |
|---|---|---|---|---|---|
| spurious | orig→base (noise, same input) | 19 | 0/19 | 0.9% | 0.011 |
| spurious | orig→full (500→8000) | 19 | 0/19 | 57.0% | 0.073 |
| spurious | base→full (same session) | 19 | 0/19 | 54.4% | 0.071 |
| genuine* | orig→base (noise, same input) | 46 | 0/46 | 1.8% | 0.012 |
| genuine* | orig→full (500→8000) | 46 | 2/46 | 62.0% | 0.083 |
| genuine* | base→full (same session) | 46 | 2/46 | 61.2% | 0.082 |
| all | orig→base (noise, same input) | 66 | 0/66 | 1.5% | 0.012 |
| all | orig→full (500→8000) | 66 | 2/66 | 60.4% | 0.080 |
| all | base→full (same session) | 66 | 2/66 | 58.8% | 0.078 |

