# ADR-2026-05-24-08: Instance を一次概念とする識別モデル

- **Status**: Draft（ローカル develop。Takahito GO 後に Drive 反映 = ADR-07 §2.5 / Tier C）
- **Date**: 2026-05-24
- **Scope**: spirrow-mindwire（Phase 1 dispatcher / adapters / value objects / ports）
- **Author**: main (claude.ai)
- **Relates to**: ADR-2026-05-21-06 (Interface Contract / Ports, v2.1)、ADR-05 (RoleAdapter Protocol, partial superseded)
- **Supersedes (partial)**: なし（ADR-06 §2.2 / §3 を拡張する。詳細は §6）

---

## 1. Context

mindwire 自律開発の最終形について Takahito ↔ main 間で合意した共通認識6点のうち、識別に関わる以下が現 main 実装（#67, commit `22cfb4f`）と乖離している。

- **共通認識3**: ロールは経路ではなくインスタンス個体に紐づく。mindwire はインスタンスを個体識別する必要がある（implementer#1 / implementer#2 …）。
- **共通認識4**: 推論主体は差し替え可能で、mindwire はその中身に無関知。
- **共通認識2**: mindwire は推論しない中継ハブに徹する。

現実装は識別の一次キーが一貫して **Role** になっており、共通認識3が求める「インスタンス個体が一次キー、ロールはその属性」になっていない。具体的な乖離は以下の4点（#67 ギャップ分析より）。

1. **`WatchSpec(thread_ref, role)`**（`magickit/watcher.py`）— watch 登録単位が `(thread, role)`。`_handles: dict[WatchSpec, SessionHandle]` のキーが衝突するため、同一 thread に同ロール複数インスタンスを登録できない（`add_watch` が no-op 化する）。
2. **`spawn_role(thread_ref, role)`**（`dispatcher/core.py`）— 入口が role 固定。`qualified_for(role)` の戻りから `candidates[0]` を一意に選ぶため、role → adapter が実質 1:1。
3. **author = role**（`dispatcher._handle_reply` の `author=handle.role`）— chatroom 上の author が bare role 文字列。仮に複数インスタンスを立てても個体識別（`implementer#1` / `#2`）ができない。
4. **経路 × ロール × モデルの結合** — adapter が `(経路, ロール, モデル)` を一体で抱えている。`ports.py` の `RoleAdapter` docstring は既に "A model+transport adapter that runs one role on one thread" と記述しており、概念としては束を意図していたが、型に昇格していない。

dispatcher 下流の `_sessions` は既に `SessionHandle`（`eq=False` の identity equality, I2）をキーに個体を保持できる構造になっている。つまり下流に個体識別の土台は存在し、欠けているのは**入口（watcher / spawn）と出口（author 規約）が role 固定である**点に集約される。

### 近期スコープ（Takahito 確認済み）

当面は **1ロール1インスタンス**で運用上の問題はない。したがって本 ADR は「end-state の識別モデルを型・signature に反映し、並列インスタンスのための seam を空ける」までを Phase 1 範囲とし、**並列インスタンスの実体実装（同ロール複数の同時稼働）は Phase 2 送り**とする。end-state 設計は並列を見込んだ形だが、Phase 1 では `instance_id` は `{role}-1` 固定で払い出す。

---

## 2. Decision

識別の一次概念を **Role から Instance に引き上げる**。Role・Model・経路（transport）は Instance の属性とする。

Instance を「`instance_id`（安定ラベル）で個体識別され、ひとつの Role 属性・ひとつの Model 束・ひとつの経路を持ち、ひとつの thread 上で動く中継対象」と定義する。`session_id`（spawn 毎に変わる ULID）とは別の、個体の安定 ID として `instance_id` を導入する。

### 2.1 型の差分（`value_objects.py`）

**`SessionHandle` に `instance_id` を追加する。**

```python
@dataclass(frozen=True, eq=False)
class SessionHandle:
    session_id: str       # ULID, per-spawn（既存）
    instance_id: str      # NEW: 個体の安定ラベル "implementer-1" 等
    adapter_id: str
    thread_ref: ThreadRef
    role: Role            # 属性として残す（Instance の一プロパティ）
    started_at: datetime  # iso_z
```

- `session_id` は従来どおり spawn 毎の ULID（再 spawn で変わる）。
- `instance_id` は個体の安定 ID。再 spawn を跨いで同一個体を指す。chatroom 上の author（I3 v2.2, ADR-06 改訂側で規定）に使われる。
- `eq=False`（I2: identity equality）は**維持する**。`instance_id` の追加は equality セマンティクスを変えない（依然として Python `is` による identity equality）。
- 1ロール1インスタンスの Phase 1 では `instance_id = f"{role}-1"` を払い出す。並列時に `-2` 以降を払い出す seam のみ空ける。

**`Role` enum は不変。** 削除も改名もしない。Instance の属性に格下げするだけなので型自体は据え置き、既存コードの破壊を最小化する。

### 2.2 watcher / spawn の instance 配線

**`WatchSpec` に `instance_id` を追加し、`_handles` のキーを instance ベースにする。**

```python
@dataclass(frozen=True)
class WatchSpec:
    thread_ref: ThreadRef
    role: Role
    instance_id: str   # NEW
```

`watcher._handles` のキーを `instance_id`（または `WatchSpec` 全体だが instance_id を含む形）にすることで、同一 thread・同ロールでも個体が分離され、dict キー衝突の構造的欠陥が型レベルで解消される。1ロール1インスタンスでも instance_id を必ず通す。

**`spawn_role` を `spawn_instance` に拡張する。**

```python
# Before
async def spawn_role(self, thread_ref: ThreadRef, role: Role) -> SessionHandle: ...
# After
async def spawn_instance(
    self, thread_ref: ThreadRef, role: Role, instance_id: str
) -> SessionHandle: ...
```

`role` 引数は残す（Instance の属性として adapter 選定に使う）。`instance_id` を引数で受け取り `SessionHandle` に載せる。dispatcher 下流の `_sessions` は既に handle キーなので、配線変更は入口に閉じる。

### 2.3 経路（transport）/ ロール・モデルの直交化 — Phase 2 seam

`RoleAdapter` Protocol（`ports.py` §3.1）の **signature は Phase 1 では据え置く**。直交化の本体（`TransportAdapter`（terminal / browser の土管）× `InstanceSpec`（role + model_binding + capabilities）への分解）は、2つ目の transport（browser）が登場して初めて実利が出る。今分解するのは YAGNI。

Phase 1 では以下に留める。

- `RoleAdapter` docstring の "model+transport adapter" の意図を、本 ADR §2.3 への参照コメントとして明示し、将来の分解点（seam）であることを記録する。
- 共通認識3の「経路は属性」は、`instance_id` による個体識別を先に効かせることで部分的に満たす。経路の型分離は Phase 2。

### 2.4 独立性（共通認識6 / ADR-05 §5）担保箇所

naysayer 独立性の enforcement は現在 `Capability.NAYSAYER_QUALIFIED` を adapter クラスが持つ形で実現されている。end-state ではこれを Instance の `model_binding` の属性に移す（別モデルファミリーであることが capability の根拠）。ただし `model_binding` の型化は §2.3 の直交化と同じく Phase 2。**Phase 1 では `qualified_for` ゲートと `NAYSAYER_QUALIFIED` capability の機構をそのまま維持し、独立性の保証を一切弱めない。**

---

## 3. Consequences

### Positive

- 共通認識3（インスタンス個体識別）を型レベルで満たす土台ができる。
- watcher の dict キー衝突という構造的欠陥が解消され、並列インスタンスへの seam が型で表現される。
- dispatcher 下流（`_sessions` の handle キー）の既存設計をそのまま活かせる。
- 変更が局所的（型1フィールド追加 + author 1行 + watcher/spawn の配線）で、リスクが小さい。

### Negative / Cost

- ADR-06 §2.2（`SessionHandle`）と §4 I3（author 規約）の改訂が必要（I3 は別 ADR = ADR-06 v2.2 改訂で扱う。§6 参照）。
- 経路直交化と model_binding 型化を Phase 2 に残すため、Phase 1 時点では `RoleAdapter` が依然 `(経路, ロール, モデル)` を一体で抱える状態が続く（seam コメントで意図は明示）。

### Neutral

- 1ロール1インスタンス運用では実挙動はほぼ変わらない（author 表示が `implementer` → `implementer-1` に変わる程度）。並列の実利は Phase 2 で顕在化する。

---

## 4. Phase 1 / Phase 2 境界

| 項目 | Phase 1（本 ADR） | Phase 2 |
|---|---|---|
| `SessionHandle.instance_id` | 追加・配線する | — |
| `WatchSpec.instance_id` / `_handles` キー | instance ベースに変更 | — |
| `spawn_instance` signature | role + instance_id | — |
| author = instance_id | ADR-06 I3 v2.2 で規定 | — |
| 同ロール並列インスタンスの実体稼働 | seam のみ（instance_id は `{role}-1` 固定） | 実装 |
| 経路 transport の型分離（TransportAdapter） | seam コメントのみ | 実装（browser 追加時） |
| `InstanceSpec`（role+model+capabilities 束） | — | 実装 |
| `NAYSAYER_QUALIFIED` の model_binding への移設 | 現機構維持 | 実装 |

---

## 5. Implementation Notes（T-task 着手用）

- `value_objects.py`: `SessionHandle` に `instance_id: str` を追加。docstring に「`session_id`=per-spawn ULID / `instance_id`=stable per-instance label」の区別を明記。`eq=False` は load-bearing なので変更しないこと（I2）。
- `magickit/watcher.py`: `WatchSpec` に `instance_id` 追加、`_handles` キーを instance ベースへ。`add_watch` の no-op 化条件を instance_id 込みで再確認。
- `dispatcher/core.py`: `spawn_role` → `spawn_instance`。`instance_id` を `SessionHandle` に載せる。Phase 1 は `instance_id = f"{role}-1"` を払い出すヘルパ `mint_instance_id(role)` を用意（並列時の払い出しロジックの seam）。**配置は `value_objects.py`（`Role` の隣）**に変更した（当初本 §5 は `dispatcher/core.py` を指定していたが、`WatchSpec` デフォルトと dispatcher が単一ソースを共有でき、value_objects → dispatcher の逆 import を避けられるため。PR #69 naysayer MINOR-4 / main 合意）。なお `instance_id` は `instance_id = ctx.own_instance_id` の形で adapter が handle に刻む（`SessionHandle` は I2 identity-key なので dispatcher が spawn 後に差し替え不可）。
- `ports.py`: `RoleAdapter` の signature は据え置き。docstring に本 ADR §2.3 への seam 参照コメントを追加。
- author 規約変更（`_handle_reply`）は ADR-06 v2.2 改訂側の Implementation Notes で扱う（本 ADR では型と配線まで）。
- 独立性: `qualified_for` / `NAYSAYER_QUALIFIED` は無変更。

---

## 6. ADR-06 / ADR-05 との関係

- 本 ADR は ADR-06 §2.2（`SessionHandle`）を**拡張**する（`instance_id` フィールド追加）。ADR-06 §2.2 本文の改訂は ADR-06 v2.2 側で行い、本 ADR はその設計根拠を保持する。
- author 規約（ADR-06 §4 I3）の改訂は **ADR-06 v2.2** で扱う（本 ADR と対で develop に起こす）。
- ADR-05 の RoleAdapter Protocol は Phase 1 で既に ADR-06 §3 に部分 supersede 済み。本 ADR は §2.3 の seam で将来の TransportAdapter 分解を予告するが、ADR-05 の architecture pattern / Role enum / Capability enum / NAYSAYER_QUALIFIED 判定は引き続き Accepted。
