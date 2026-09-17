# ADR-2026-05-21-05: 役割と adapter の抽象化 (ports & adapters)

> **⚠ Cross-Project Supersede Notice (2026-05-21)**
>
> 本 ADR-2026-05-21-05 の **§ "RoleAdapter Protocol"** は、
> spirrow-mindwire project の **ADR-2026-05-21-06 (Accepted: 2026-05-21T23:16:04.978077Z UTC)**
> によって **Phase 1 contract として部分 supersede** されました。
>
> - Phase 1 における `RoleAdapter` の signature 仕様 SOT は
>   spirrow-mindwire ADR-06 §3 に移動
> - 本 ADR-05 の他 § (architecture pattern / Role enum / Capability enum /
>   NAYSAYER_QUALIFIED 判定 logic) は **引き続き Accepted** のまま
> - spirrow-mindwire 側 cross-project link doc (`15HsiZ-lXq1t_l0kw_Q1hL_zroSH8RNQzl9cG2b9_XlQ`)
>   にも対応注釈あり
> - Audit trail: spirrow-mindwire chatroom thread `T-ADR06-interface-contract`
>   decide msg `msg-169`

## Status

Accepted (2026-05-21, user 確定)

## Context

ADR-2026-05-21-04 で mindwire の責務を「chatroom クライアントの自動操縦 layer」に再定義した。 この際、 user から 3 つの設計要件が明示された:

1. **naysayer は claude.ai 固定ではなく、 別モデル (Gemini / local Qwen 等) を担当者にする可能性が前提**
2. **3 役は thread ごとに動的に割り当たる** (claude-code = implementer 固定ではない)
3. **自動操作の手段は不問** (Claude in Chrome / OpenCrow / 専用ブラウザ + selenium / Anthropic Console API / Computer Use 等は実装フェーズで選定)

これらを満たすには、 mindwire の dispatcher 層に **model agnostic + 手段 agnostic + role 動的** な抽象化が必要となる。

## Decision

### 1. アーキテクチャパターン

**Ports & Adapters (hexagonal architecture)** を採用する。

```
[議論層: magickit chatroom (= conclair)]
    ↑ thread / message / decide / role 属性
    │
[dispatcher 抽象 (mindwire core)]
    ↑ Port: 「この thread でこの role を起動せよ」
    │
[adapter (plugin)]
    ├─ ClaudeAi via Browser  (Claude in Chrome / OpenCrow / custom selenium)
    ├─ ClaudeAi via SDK      (Anthropic Console API)
    ├─ ClaudeCode via SDK    (既存)
    ├─ Gemini via Browser
    ├─ Gemini via API
    ├─ LocalQwen via Lexora  ({{HOST_SERVICES}} 経由)
    └─ ...
```

dispatcher は port (interface) のみを知り、 adapter (実装) を実行時に選択する。

### 2. Role の定義 (動的役割)

```python
class Role(StrEnum):
    PROPOSER = "proposer"
    NAYSAYER = "naysayer"
    IMPLEMENTER = "implementer"
    # 将来拡張: ARBITER, REVIEWER, etc.
```

3 役は thread の meta に動的に割り当たる:

```yaml
# thread の meta 例
thread_id: thr_xxx
project: spirrow-mindwire
roles:
  proposer: { adapter: "claude-ai-browser", session: "sess_001" }
  naysayer: { adapter: "gemini-browser",    session: "sess_002" }
  implementer: { adapter: "claude-code-sdk", session: "sess_003" }
```

「claude-code は常に implementer」のような静的紐付けはしない。 同じ thread 内で claude-code が proposer になる場合もある。

### 3. RoleAdapter Port (interface)

```python
class RoleAdapter(Protocol):
    """thread に対して 1 役を担当する instance を起動・操作する抽象"""

    name: str                        # "claude-ai-browser", "gemini-api", ...
    capabilities: set[Capability]    # 何ができる adapter か

    async def spawn(
        self,
        thread_ref: ThreadRef,
        role: Role,
    ) -> SessionHandle:
        """thread に対して role を担う instance を起動。
        thread context は instance が必要に応じて chatroom から取りに行く前提。"""

    async def halt(
        self,
        session: SessionHandle,
        reason: HaltReason,
    ) -> None:
        """instance を停止"""

    async def health(
        self,
        session: SessionHandle,
    ) -> SessionStatus:  # running / waiting / dead / completed
        """生存確認"""
```

最小 interface はこの 3 つ。 必要に応じて拡張 (e.g. `send_nudge` for 「対応してくれ」 と再依頼するケース)。

### 4. Capability 宣言

```python
class Capability(StrEnum):
    READ_THREAD = "read_thread"
    POST_REPLY = "post_reply"
    EXECUTE_CODE = "execute_code"           # claude-code 系のみ
    NAYSAYER_QUALIFIED = "naysayer_qualified"  # main と独立モデルである
```

dispatcher は thread の role 割り当て時に **capabilities が一致する adapter のみを候補にする**:

- `naysayer` slot → `NAYSAYER_QUALIFIED` を持つ adapter のみ (claude.ai main session と同じモデルの session は候補から除外)
- `implementer` slot → `EXECUTE_CODE` を持つ adapter のみ

これにより、 naysayer 独立性 が架構レベルで担保される (= 設定ミスで naysayer に main と同じモデルを当ててしまう事故が型レベルで防げる)。

### 5. 独立性の判定基準 (NAYSAYER_QUALIFIED の意味論)

「main と独立」とは何か。 厳密化すると複雑なので、 Phase 0 では以下のヒューリスティクスで判定:

- **異なる model family**: claude.ai (main) vs Gemini / Qwen (naysayer) → ✅
- **異なる account / session**: 同じ claude.ai でも完全に別 conversation で context を共有しない → △ (独立性弱、 Phase 0 では fallback として許可)
- **同じ model + 同じ session**: → ✗ (NAYSAYER_QUALIFIED を付けない)

ヒューリスティクスは Phase 2 以降で精緻化。 重要なのは 「強制力を architecture 層で持つ」 こと。

### 6. 実装の優先順位

Phase 0 で実装する adapter は 1 つに絞る (最も実現しやすいもの):

- 候補 1: ClaudeCode via SDK (既存)
- 候補 2: ClaudeAi via Browser (Claude in Chrome 公式が出てるので障壁低)
- 候補 3: その他

dispatcher / port / Role / Capability の **抽象側** は Phase 0 で完成させ、 adapter は 1 つだけ動かして smoke test まで通す。 Phase 1 以降で 2 つ目・3 つ目の adapter を追加する形が安全。

## Consequences

### Positive
- naysayer 独立性が型レベル / アーキテクチャレベルで強制される
- 新しいモデル / 操作手段の追加が dispatcher コア無変更で可能
- role の動的割り当てにより、 同じ adapter (例: claude-code) を異なる role で再利用できる
- Phase 0 で 1 adapter のみ実装すれば smoke test が回り、 段階的拡張が容易

### Negative
- 抽象化レイヤーを最初から切るので、 1 adapter しかない Phase 0 時点では over-engineering に見える
- adapter 実装ごとに認証 / セッション管理 / cost tracking が異なるため、 後発 adapter 追加時に Port 拡張が発生する可能性 (Port は実装者が変更しないルールで運用、 必要なら版数管理)
- `NAYSAYER_QUALIFIED` の判定基準が Phase 0 時点では弱い (heuristic ベース)

### Neutral
- 既存 mindwire 実装の `claude_code/` パッケージは ClaudeCode adapter として再構成可能
- 既存 `lifecycle/` パッケージは dispatcher 内部の state machine として再利用可能

## Related
- ADR-2026-05-21-04 (根本要求再確認と mindwire 責務再定義) — 本 ADR の前提
- 旧 ADR-2026-05-21-03 (signal channel 設計) — superseded、 ただし adapter 間 signal の語彙設計は本 ADR から派生して再検討する余地あり
