# ADR-2026-05-21-03: signal channel 設計 (語彙・transport・delivery)

## Status
Accepted (2026-05-21, user 確定)

## Context

`spirrow-tomtar` の Phase 0 設計において、mindwire / tomtar / 3 instance 間で流れる signal の仕様 (語彙、スキーマ、transport、delivery 保証) を確定する必要がある。

ADR-2026-05-21-02 で tomtar 入力経路 = mindwire webhook push が確定したが、その上に乗る信号レイヤーの仕様は未定義だった。

## Decision

### 1. 信号の語彙 (7 種)

| signal_type | direction | 目的 |
|---|---|---|
| `instance.spawn_request` | tomtar → mindwire | 3 instance のどれかを起動したい |
| `instance.spawned` | mindwire → tomtar | 起動完了通知 (session_id 含む) |
| `instance.halt_request` | tomtar → mindwire | 番人検知による停止指示 |
| `instance.halted` | mindwire → tomtar | 停止完了通知 |
| `instance.completed` | mindwire → tomtar | instance が正常終了した |
| `instance.died` | mindwire → tomtar | instance が異常終了した (crash/oom 等) |
| `conclair.thread_event` | conclair → mindwire → tomtar | thread open/close/decide/message_rate spike 等 |

spawn_failed / halt_failed は明示的な signal type にはせず、対応する spawned / halted signal の payload に `status: "failed"` フィールドで表現する。

### 2. 共通スキーマ (JSON)

```json
{
  "signal_id": "sig_2026-05-21T15:30:00.123Z_a8f3",
  "signal_type": "instance.spawn_request",
  "timestamp": "2026-05-21T15:30:00.123Z",
  "source": "tomtar",
  "target": "mindwire",
  "correlation_id": "thr_xxxx",
  "payload": { ... }
}
```

`correlation_id` は、tomtar の判断ログ・mindwire の動作ログ・conclair の audit log を串刺しできるように必須とする。

### 3. `instance.spawn_request` の payload

```json
{
  "role": "integrator",
  "thread_uri": "magickit://chatroom/thread/thr_xxxx",
  "project": "spirrow-mindwire",
  "trigger_reason": "decide_emitted_awaiting_integration",
  "constraints": {
    "max_runtime_sec": 1800,
    "max_messages": 50,
    "max_cost_usd": 5.0
  },
  "context_hint": {
    "last_message_id": "msg_yyyy",
    "ancestor_thread_uris": []
  }
}
```

- `constraints` は spawn 時に必須。mindwire は超過時に halt 相当の処理を行う義務を負う。
- `trigger_reason` は enum (ad-hoc 文字列禁止)。enum 候補は別途定義。
- `context_hint` は instance への hint のみ。コンテキスト本体は渡さない (tomtar は AI 推論しない原則)。instance 起動後は instance 自身が `begin_task` 等で復元する。

### 4. `instance.halt_request` の payload

```json
{
  "session_id": "sess_zzzz",
  "reason": "thread_overheating",
  "evidence": {
    "metric": "message_rate",
    "value": 12,
    "threshold": 5,
    "window_sec": 60
  },
  "grace_sec": 10
}
```

`evidence` は構造化必須。Phase 2 以降に Naysayer で halt 判断ルールを事後レビューできるよう、metric / value / threshold / window_sec の 4 field を最低限備える。

### 5. 信号の意味論 (受信側の義務)

| signal | 受信側 | 義務 |
|---|---|---|
| `instance.spawn_request` | mindwire | 60秒以内に `instance.spawned` を返す (status=success\|failed) |
| `instance.halt_request` | mindwire | `grace_sec` 以内に halt を試み、必ず `instance.halted` を返す |
| `conclair.thread_event` | tomtar | best-effort、ack 不要 |

request 系は ack 必須、event 系は fire-and-forget。これでハング instance を tomtar が timeout で検知できる。

### 6. transport

**HTTP webhook + retry** を採用。

- mindwire / tomtar / conclair は同一マシン上または Tailscale VPN 内で動作するため、localhost / VPN HTTP で十分。
- retry は指数バックオフ (1s → 2s → 4s → 8s)、最大 N 回 (Phase 0 では N=3)。
- N 回失敗 → user に escalate (Magickit chatroom に message 投稿 + 後続 signal の発火停止)。

採用しなかった案:
- Unix socket: local 限定では十分だがマシン分離時に書き直しが必要、HTTP の方が運用 (curl/log) しやすい。
- NATS / Redis Streams: Phase 0 では over-engineering、依存サービスが増える。

### 7. delivery 保証

| signal 種別 | guarantee | 実装 |
|---|---|---|
| `spawn_request` / `halt_request` | at-least-once + 冪等 | mindwire 側で `signal_id` のデダップ table (TTL=10 分程度)、過去受信なら ack のみ返却 |
| `spawned` / `halted` / `completed` / `died` | at-least-once + 冪等 | tomtar 側で `signal_id` のデダップ |
| `conclair.thread_event` | at-most-once | retry なし、欠落許容 |

## Consequences

### Positive
- `signal_id` ベースの冪等化により、重複起動・重複停止が防げる。
- `evidence` 構造化により、Phase 2 以降の halt 判断ルール tuning が可能になる。
- `correlation_id` で全 service のログを串刺し可能、デバッグしやすい。
- HTTP transport で標準的なツール (curl, httpie, log) でデバッグ可能。

### Negative
- mindwire 側でデダップ table の実装が必要 (memory または小型 DB)。
- HTTP は WebSocket より overhead が大きいが、Phase 0 の low throughput 想定では問題なし。
- thread_event を at-most-once にすると欠落リスクがあるが、tomtar の動作は idempotent rule 評価なので欠落しても次の event で巻き返せる設計を強制する。

### Neutral
- signal 語彙 7 個 は Phase 0 の最小セット。Phase 1 以降で増やす余地は残す。

## Implementation Notes

- `signal_id` フォーマット: `sig_<ISO8601 timestamp>_<random 4 hex>` 推奨。time-ordering と uniqueness を両立。
- mindwire のデダップ table は in-memory dict (TTL=10 分) で MVP 開始、必要に応じて永続化検討。
- `trigger_reason` の enum は Phase 1 derived task の早い段階で確定させる必要あり。

## Related
- ADR-2026-05-21: Magickit chatroom = spirrow-conclair
- ADR-2026-05-21-02: tomtar 入力経路 = mindwire webhook push (案 B)
- 既存 ADR: sg-tomtar = REACTIVE dispatcher / AI 推論しない / 暴走防止の番人
