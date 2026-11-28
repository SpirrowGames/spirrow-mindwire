# ADR-2026-11-28-25 role `product_owner` の新設と role `human` の段階的退役（ADR-2026-05-29-10 partial-supersede）

- **Status**: Accepted（三者収束。設計 Bohr msg-10139 / msg-10141 / msg-10143 / msg-10145 / msg-10148 / msg-10150、独立検証 Einstein msg-10140 / msg-10144 / msg-10146 と msg-10150 への go。起点は Takahito 裁定 msg-8784）
- **Date**: 2026-11-28
- **Partially supersedes**: ADR-2026-05-29-10（`T-T29-role-registry` msg-309）の「確定した 7 role 定義」の `human` の行、「closeable_roles 正式定義」、「role 名正規表記固定」。それ以外（他の 6 role の定義、close_reason enum と案 X-1、SOT 分離）は変えない。
- **注記先**: ADR-2026-05-29-12（`T-embodiment-self-declared` msg-325）、ADR-2026-09-14-21 D-1。
- **Scope**: role registry に `product_owner` を足すこと、role `human` を退役させる順序、`human` で人を判定していた reader の判定キー。identity `human` と independence_class `human` は退役させない。
- **Author**: Heisenberg (implementer) — chatroom thread `T-human-role-registry`（brief: Fermi msg-8918 = `T-role-null-must-become-impossible` msg-8802 §1）

---

## 1. 背景

- Takahito 裁定 msg-8784 §2: 「human の role は新設の `product_owner` とする。role registry（ADR-2026-05-29-10）に追加する。ただし既存の `integrator` が『人が判断・統合する役』と同義なら新設せずそれを使う」。§1: Fermi は投稿ごとに実際に果たした役を申告する（`role="human"` は msg-8671 で退けた形）。
- brief（msg-8802 §1）: R を決める。R = `product_owner` なら registry と `closeable_roles` に足す。既存の role `human` は、変更前に使用件数を測り、新規投稿では使わせず、どの identity にも与えず、削除はしない。human の登録は T-H で行う。

## 2. 決定

### D-1: R = `product_owner`

ADR-10 の `integrator` の定義は「複数 stream 合流点の統合・close 権限あり」で、人が判断することも人と結びつくことも書かれていない（msg-10139 §1、msg-10143 §0）。msg-8784 §2 の条件に当たらないので、`integrator` は使わず `product_owner` を新設する。

### D-2: `product_owner` の定義

- msg-8784 §2 の文言のとおり、「human の投稿（判断ページでの選択・Tier-C 裁定・方向性の承認）が実際に果たしている役の名前」とする。
- close 権限を持つ: **closeable_roles = {proposer, implementer, integrator, product_owner}**（`human` は段階 2 で外れる。D-4）。
- ADR-10 の `human` が持っていた「全決定 override 権限」と §2.7（領域分離の論証）は**引き継がない**。根拠となる裁定の文言がないため（msg-10141 Obj-1-4、msg-10143 Obj-1-4）。close_reason の `override` を誰が使えるかは T-H で付与と一緒に決める。
- role は機能で定義し、誰に割り当てるかは `allowed_roles` で決める。定義の中に「人専用」とは書かない（ADR-2026-05-27-09 の層分離）。この ADR では `product_owner` を**どの identity にも付与しない**。
- 正規表記（lowercase 厳密 match）に `product_owner` を加える。`human` は「退役（新規の申告・付与不可、過去の投稿の値としては有効、予約名）」とする。

### D-3: 変更前の実測（2026-11-28、Heisenberg msg-10147。読み取りのみ）

**(iii) `role=human` の投稿**: 21 project・23,661 件中 **183 件**。

| 内訳 | 件数 |
|---|---|
| author `Fermi` | 108（2026-10: 2、2026-11: 106） |
| author `human` | 75（2026-08: 53、2026-09: 9、2026-11: 13） |
| type | report 132 / handoff 17 / propose 16 / decide 11 / answer 7 |
| project | spirrow-mindwire 102（Fermi 60・human 42）、spirrow-lexora 23、spirrow-magickit 22、spirrow-voxelworld 21、他（tutorail / verimend / playproof / prismind / conclair）15 |
| `role=human` の close | 1（spirrow-lexora `T-lexora-gate-and-ci` msg-020、author `human`、2026-08-31） |

2026-11 の author `human` の 13 件は、すべて「Takahito 指示で Fermi セッション（claude.ai）が代行投稿」で始まる代行で、Tier-C 裁定を含む（spirrow-mindwire msg-10042 / msg-10044、spirrow-magickit msg-1770 / msg-1781）。**人の裁定を届ける経路は今も identity `human` の `allowed_roles=["human"]` を使っている。** msg-10143 §0 の「human 名義の投稿は role=null」という前提はこの実測で否定され、D-4 の段階分けになった（msg-10148 ①）。

**(ii) identity の `allowed_roles`**: 投稿に現れる author 24 名に `get_identity` した結果、`human` を持つのは identity `human`（`["human"]`）と `Fermi`（`["proposer","reviewer","human"]`）の 2 件。**これは store 全レコードの列挙ではない**（列挙 API がこの作業環境に無い）。全レコードの列挙・除去・件数の記録は Magickit lane で行い、結果をこの節に追記する（Einstein Obj-3-2、D-6）。

**(iv) role `human` を前提にした処理（この repo）**: `settled_close/runner.py`（owner が role `human` を持つと自動 close しない）と `park_wake/runner.py`（author が role `human` を持つと bare `NEXT: none` を `human_close` に数える）。D-5 で直す。

**(i) Magickit の `CLOSEABLE_ROLES`** と **(v) ADR-12 の embodiment 例外の判定キー**: Magickit の server コードはこの作業環境から読めず、**未確認**。Magickit lane の結果をここに追記する（D-6）。

### D-4: 退役は段階に分ける（msg-10148 ①、msg-10150 Obj-3-1）

| 段階 | 内容 | 場所 |
|---|---|---|
| 1（本 ADR の thread） | `product_owner` を registry と `closeable_roles` に追加（誰にも付与しない）。identity `human` 以外への `human` の付与と新規申告を拒否。repo の loader / guard が `human` を拒否（`identity/classification.py::RETIRED_ROLES`）。 | Magickit lane / この repo |
| 1 の順序 | ① Magickit `_settled_identity_refusal` と repo `settled_close` の保護キーを D-5 に揃えて merge → ② 両方が稼働環境で有効なことを確認（repo は deploy request の成功と稼働 commit、Magickit は lane が稼働版を確認。本番 deploy は ADR-2026-10-22-24 のとおり Takahito の指示のみ）→ ③ Fermi の `allowed_roles` から `human` を外す → ④ Fermi が owner のスレッドが `OwnerKind.HUMAN` のままであることを稼働環境で読み取り確認。 | |
| 2（T-H） | identity `human` の `["human"]` を `["product_owner"]` に置き換え、同じ 1 つの変更で identity `human` の `human` 申告と close も拒否する。人の裁定の経路が途切れる期間を作らない。「どの identity にも `human` を割り当てない」の完了は T-H の出口条件とする。 | T-H |

案 A（T-H を前に出す）は採らない。T-H には「サーバーが `author=human` から `product_owner` を導出するか」の実測（msg-8784 §3）が残っており、付与が先に進むため（msg-10148 ①）。msg-8784 は退役の時期を裁定していないので、この順序は `goal` に当たらない（Einstein msg-10149）。

### D-5: reader の判定キーは向きによって変える（msg-10148 ②、msg-10150 Obj-3-3）

- **保護（fail-closed）— `settled_close` の「人が owner のスレッドを自動 close しない」**: キーを広く取る。`is_human_name(owner)`、または `independence_class == "human"`、または `allowed_roles` に `product_owner` か `human` を含む。Fermi は `human` を外された後も保護される（`settled_close/classify.py::HUMAN_OWNER_ROLES` / `HUMAN_INDEPENDENCE_CLASS`）。
- **集計の分類 — `park_wake` の `human_close`**: close・遷移・wake を起こさない分類なので、キーを狭く取る。identity `human`（名前）か、`allowed_roles` に `product_owner` を含む author。independence_class は使わない。Fermi の bare `NEXT: none` は `unclassified`（事故候補）として報告される側に移る（`park_wake/runner.py::HUMAN_CLOSE_ROLE`）。
  - 変更前後の digest 件数（2026-11-28、全 21 project の open スレッドの head）: 変更前 `human_close` 0 / `unclassified` 4、変更後 `human_close` 0 / `unclassified` 4。分類が動いた head は 0 件。
- **例外 — ADR-12 の embodiment 申告必須の例外**: キーを狭く取る。サーバーが認証・解決した identity `human` のときだけ。呼び出し側が渡す role・independence_class・自己申告の identity 名は使わない。independence_class `human` は例外の根拠にならない（Fermi は ADR-12 §3 の「magickit を稼働主体として叩かない」に当たらない）。今の実装が role か independence_class で判定しているなら identity 判定に直す（後者なら直す前に Fermi の embodiment なし handoff／ack／decide の件数を記録する）。msg-10145、Einstein msg-10146。

### D-6: Magickit lane へ渡す作業（spirrow-magickit、仮称 `T-magickit-product-owner-role`）

(i) / (v) の確認、`product_owner` の registry と `CLOSEABLE_ROLES` への追加、store 全レコードの列挙と identity `human` 以外からの `human` の除去（件数を D-3 に追記）、upsert での `human` 付与の拒否（identity `human` は T-H まで今のまま）、`_settled_identity_refusal` を D-5 に揃える、テスト（`product_owner` の close が通る／`human` の新規申告・付与が拒否される／過去の投稿を読み出せる／identity `human` の decide が embodiment なしで通る／Fermi の embodiment なし handoff・ack・decide が拒否される／呼び出し側の role・independence_class の細工で例外を得られない）。

## 3. 他の ADR への注記

- **ADR-2026-05-29-12**: 「human registration unblock」の `upsert_identity(..., allowed_roles=["human"])` は、この ADR と T-H の付与（`["product_owner"]`）で置き換える。§2 の層分離と §3（human の embodiment は absent）は変えない。§4 の例外の対象は「identity `human`」と読む（D-5）。
- **ADR-2026-09-14-21 D-1**: 「ADR-2026-05-29-10 の 7 role」は、退役していない registry の role と読む。Fermi は `human` を名乗らず、その投稿で実際に果たした役を申告する（msg-8784 §1）。

## 4. 範囲外

- identity `human` への `product_owner` の付与と、close_reason `override` を使える role の決定（T-H）。
- Fermi がどの role を選ぶのが正しいか（`T-fermi-declares-role`）。
