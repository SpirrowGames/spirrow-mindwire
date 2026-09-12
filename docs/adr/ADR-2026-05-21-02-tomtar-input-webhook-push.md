# ADR-2026-05-21-02: tomtar 入力経路を mindwire webhook push (案 B) に確定

## Status
Accepted (2026-05-21, user 確定)

## Context

`spirrow-tomtar` の入力ソース設計について、3 案 (A=conclair polling / B=mindwire webhook push / C=hybrid) を比較検討した。

これまでの ADR では tomtar の入力経路が未確定で、`Phase 0 = sg-tomtar MVP + Integrator のみ自律化、業務 = spirrow-mindwire dogfooding 自律化` というスコープ範囲も polling 寄りの暗黙前提だった。

## Decision

**tomtar の入力経路は、Phase 0 段階から mindwire webhook push (案 B) 一本で設計する。**

これに伴い、**Phase 0 のスコープに mindwire webhook 基盤の先行実装が含まれる**ことを明示する。元 ADR (Phase 0 = sg-tomtar MVP + Integrator のみ自律化) からのスコープ拡大に該当する。

採用しなかった案:
- 案 A (conclair polling): polling 遅延と conclair 負荷の懸念。Phase 2 への移行時に設計やり直しになる。
- 案 C (hybrid): 実装コスト 2 倍、Phase 0 で過剰設計。

## Consequences

### Positive
- low latency (event 駆動なので polling 間隔ぶんの遅延が無い)
- conclair に観測用 API を生やす必要が無い (conclair は thread_event を mindwire に push するだけで良い)
- tomtar は passive で動作 (cron / polling loop 不要、idle 時の CPU 消費ゼロ)
- Phase 2 以降の信号経路と一貫しているので、後で書き直しが発生しない

### Negative
- **Phase 0 のスコープが拡大する**: mindwire 側に webhook サーバー + signal dispatch ロジックを先に実装する必要がある。元の「Phase 0 = sg-tomtar MVP」の範囲を超える。
- 単体テストが難しい: tomtar 単独でテストするには mock webhook を立てる必要がある。
- conclair → mindwire の webhook 配線が新たに必要 (元の ADR では未定義の経路)。

### Neutral
- tomtar の出力経路 (3 instance への dispatch) も mindwire 経由のため、入力・出力ともに mindwire 依存となる。これは「mindwire = control plane」の役割を最大化する方向性として一貫している。

## Implementation Notes

- Phase 1 derived tasks に「mindwire webhook server MVP」を含める必要がある。
- conclair → mindwire の thread_event push 経路は本 ADR の範囲外 (別 ADR で確定)。Phase 0 では mock event でも可。
- transport は HTTP webhook + retry (ADR-2026-05-21-03 参照)。

## Related
- ADR-2026-05-21: Magickit chatroom = spirrow-conclair (wrapper/impl)
- 既存 ADR: Phase 0 = sg-tomtar MVP + Integrator のみ自律化 (本 ADR でスコープ拡大)
- 既存 ADR: sg-tomtar = REACTIVE dispatcher (観測→判断→信号)
