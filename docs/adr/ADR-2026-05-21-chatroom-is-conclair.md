# ADR: Magickit chatroom = spirrow-conclair (wrapper / impl 関係)

## Status
Accepted (2026-05-21, user 確認)

## Context

`spirrow-tomtar` Phase 1 derived tasks の派生にあたり、tomtar が監視する対象 service を明確化する必要があった。

これまでの ADR では `Magickit chatroom = data plane (deliberation)` と記述されていたが、`spirrow-conclair` という独立 service 名がメモリ上の認識にあるものの、ADR としては「conclair = chatroom 本体」という関係性が明文化されていなかった。

これは tomtar の責務設計に直接影響する論点である:
- tomtar が監視するのは Magickit の chatroom API か、conclair の API 直か?
- 「decide が出た」「新 thread が立った」などの event 発火元は誰か?
- 認証・rate limit・データ整合性責任は Magickit と conclair でどう分担するか?

## Decision

**Magickit の chatroom 機能は、Claude.ai や Claude Code などのクライアントが MCP 経由でアクセスするための「ラッパー」である。本質的な実装(thread/message/decide のストレージ、状態遷移、整合性保証)は独立 service `spirrow-conclair` が担う。**

したがって、概念的には以下の等式が成立する:

```
Magickit.chatroom_*  ==  spirrow-conclair (の MCP 露出面)
```

つまり、ADR で「Magickit chatroom = data plane」と書かれている箇所は、実装上は「spirrow-conclair = data plane」と等価である。

## Consequences

### Positive
- tomtar の監視対象が明確化される: 状態 (thread の open/active/closed、message 流量、decide 発火) は conclair 側から取得するのが本筋。Magickit chatroom API はクライアント向けラッパーなので、tomtar のような内部 daemon が叩くのは ergonomic ではあるが、より直接的には conclair に対して監視 API を生やすのが正道。
- Magickit が肥大化しない (Magickit は orchestrator + ラッパー集約に専念)。
- conclair を独立に scale / replace できる。
- 認証・rate limit が 2 層になる: Magickit 層 (クライアント認証) と conclair 層 (内部 service 認証)。

### Negative
- 同じ機能に対して "Magickit chatroom" と "conclair" の 2 つの呼称が併存し、ドキュメント上で混乱を招く可能性がある。本 ADR 以降は「データ層の議論では conclair、クライアント向け API の議論では Magickit chatroom」と呼び分ける慣習を導入する。
- tomtar が conclair を直接叩く設計にすると、Magickit を経由しないアクセスパスが増え、認証・監査の経路が複雑化する。Phase 0 では tomtar も Magickit chatroom API 経由で監視し、Phase 2 以降で性能要件が出てきたら conclair 直結を検討する (後続論点)。

### Neutral
- 既存の ADR / knowledge で「Magickit chatroom」と記載されている箇所は、conclair と読み替えても等価である。書き直しは行わない。

## Implementation Notes

- Phase 1 derived tasks では tomtar の入力データ source として Magickit chatroom API を採用する (conclair 直結は Phase 2 以降の最適化論点)。
- "暴走防止の番人" 責務における thread 過熱検知 (message 流量、無限ループ) は conclair の責務範囲 (storage 側で carrying signal を生成可能なため) と tomtar の責務範囲 (外形監視) の両方が成立しうる。これは別 ADR で確定させる。

## Related
- `Magickit chatroom = data plane (deliberation)` 既存 ADR
- `sg-tomtar = REACTIVE dispatcher (観測→判断→信号)` 既存 ADR
- `sg-tomtar daemon は別 repo (spirrow-tomtar)` 既存 ADR
