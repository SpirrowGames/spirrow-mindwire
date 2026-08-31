---
spec_id: SPEC-2026-08-31-gate-reads-stale-base-diff
thread: T-gate-reads-stale-base-diff
target_repo: spirrow-mindwire
base_branch: main
status: active
canary: not-applicable
supersedes: []
obligations:
  - OBL-SPEC-PIN
  - OBL-SPEC-RECEIPT
  - OBL-SPEC-SCOPE-CLOSURE
items:
  - id: I-1
    title: "fetch_pr_diff を meta + three-dot compare の 2 read に置換（D-1〜D-4 / D-8）"
    paths:
      - "src/spirrow_mindwire/github/client.py"
      - "tests/test_github_client.py"
  - id: I-2
    title: "本設計文書を spec/design に配置（自己完結スペック — §0 参照）"
    paths:
      - "spec/design/T-gate-reads-stale-base-diff.md"
---

# PR-gate: `fetch_pr_diff` を stale-base の `pulls/{n}` diff から live `compare/{base}...{head}` に置換

## §0 この文書の読み方

**この文書は自己完結している。本文に無いものは、この仕様の一部ではない。** 論拠と検討経緯はスレッド `T-gate-reads-stale-base-diff` に残る（msg-2336 / msg-2337 が欠陥報告、msg-2338 が設計裁定、Einstein の naysayer 応答が URL エンコーディングの advisory を追加）。**このファイルを読めば、実装者は 4 通のメッセージを再構成する必要はない。**

決定 id（`D-1`〜`D-9`）は msg-2338 で振られたものを保存する。受け入れ条件（`AC-1`〜`AC-6`）も同様。

## §1 前提（実測値 — 変えるときは実測を差し替える）

以下は operator と proposer が実測した値である。仮定で置き換えないこと。

| id | 事実 | 出典 |
|---|---|---|
| E-1 | 修正前の `fetch_pr_diff` は `GET /repos/{o}/{r}/pulls/{n}` を `Accept: application/vnd.github.v3.diff` で読んでいた。この endpoint が返す diff は PR に記録された `base.sha`（**PR 作成時点でスナップショット**）起点であり、base ブランチの現在の head 起点ではない。 | 修正前 `src/spirrow_mindwire/github/client.py:350-364`（msg-2338 §配線） |
| E-2 | `SpirrowGames/spirrow-lexora#10`（2026-08-31 実測）: `base.sha = 4a5995f`、`origin/develop = d4fec9c`（5 commit 先行）、`GET /pulls/10` diff = **183,288 chars**、`GET /compare/d4fec9c...c4c107b` diff = **86,585 chars**。∴ 差分 96,703 chars（53%）は PR の変更ではなく、すでに develop に merge 済みの PR #9 のコード。 | msg-2336 §2 |
| E-3 | `_MAX_DIFF_CHARS = 150,000`（`src/spirrow_mindwire/naysayer/pr_review.py:120`）。lexora#10 の stale-base diff は cap を 22% 超過し、truncation ガードが REQUEST_CHANGES を強制する経路に既に入っていた。修正後の真の diff は cap の 58% で、warn 帯にも入らない。 | msg-2336 §3(b) |
| E-4 | **A/B（内容差ゼロの操作で入力が半減）**: lexora#10 に `origin/develop` を plain merge（tree 不変 — `tree(d4fec9c) == tree(31caf9b)` を実測、`git diff 31caf9b d4fec9c` は空）。**base.sha が前進しただけでコードは 1 行も変わっていない**が、`GET /pulls/10` diff は 175,770 → 86,585 chars に 51% 落ちた。ローカル gate は before/after とも 439 passed。 | msg-2337 §1 |
| E-5 | **1 度も撃たれる前から force-RC 確定だった PR**: 同じ空 merge を lexora#11 に適用。before は 202,849 chars（cap の 135%）、after は 118,107 chars（78.7%）。**#11 自身の実差分は cap 未満**で、分割不要。 | msg-2337 §2 |
| E-6 | 呼び出しサイト: `src/spirrow_mindwire/naysayer/pr_review.py:1359`（PR-review driver、直後で `_make_diff_view(diff)`）と `scripts/naysayer_review_scoped.py:121`（scoped manual runner）。前者が truncation スレッドの「fetch site で 1 度だけ view を作る」invariant を持つ。 | msg-2338 §配線 |
| E-7 | メタ読みの型は repo 内に既存: `_fetch_ci_status_rest`（`src/spirrow_mindwire/github/client.py:456-468`）が既に `GET /pulls/{n}` を JSON で読み `head["sha"]` を取っている。 | msg-2338 §配線 |

## §2 スコープと非スコープ

**スコープ**（本仕様が扱う）:
- `GitHubClient.fetch_pr_diff` の実装差し替え（stale-base `pulls/{n}` diff → live `compare/{base_ref}...{head_sha}` diff）。
- 上記変更を反映するテストの更新。
- 本仕様が「自己完結」であるための本設計文書の配置。

**非スコープ**（本仕様は扱わない — 混同されないよう明記する）:
- **truncation ガードの振る舞い**（`_MAX_DIFF_CHARS` を超えたときにどうするか）は `T-gate-silently-suppresses-approve-on-truncated-diff` の主題であり、本修正は入力を細くする方向で影響するが、**cap ポリシー自体は変えない**。
- **head drift**（CI を読んだ head と diff を読む head がずれる TOCTOU）は**修正前から存在し、本修正で増えも減りもしない**。
- **fork PR**（head repo ≠ base repo）は本ループに現存しない ∴ v1 の対象外。もし現れたら head sha が base repo で解決できず AC-4 の経路で**うるさく落ちる**（静かに壊れない）。remedy は `{owner}:{ref}` 形式で、必要になってから入れる。
- **body 書式の変更**。gate 通知本文の書式は truncation スレッドの baseline テスト群が assert している ∴ 変えない。fetch site の可視化は log 出力のみ（D-8）。
- **既存 APPROVE の遡及再評価**（D-5 の裏返し）。

## §3 決定

### D-1 — `GET /compare/{base}...{head}`（three-dot）+ diff Accept に置換

`fetch_pr_diff` の中身を **`GET /repos/{o}/{r}/compare/{base_ref}...{head_sha}`（三点）+ `Accept: application/vnd.github.v3.diff`** に置き換える。**根拠**: operator 実測（E-2）で真の PR diff と一致（86,585 chars）。三点 compare は GitHub の Files-changed が人に見せている量 ∴ **gate が読む物と人が読む物が初めて一致する**。

**なぜ二点 `..` でないか**: 二点は「head に含まれるが base に含まれない」内容ベースの差分を出すが、base の**無関係な**変更が逆デルタとして混入する。三点は「merge base（＝共通祖先）から head までの差分」を出す ∴ base で何が起きようと head 側の変更のみを返す。**merge base の関数**であることは §D-2 で正しく使う根拠になる。

### D-2 — `{base}` は `base.ref`（ブランチ名、読むたびに解決）。pin した sha は使わない

**根拠**: 三点 compare の出力は merge base の関数である ∴ 以下の単調性が成立する:

- **head の祖先を含まない base 前進** → merge base 不変 → 出力不変（無関係な base の変更が逆デルタとして混入することもない）
- **head の祖先を含む base 前進**（＝既に merge された、多くは同じ gate が既に APPROVE 済みのコード）→ merge base 前進 → **出力は縮む**

∴ base の前進に対して出力は**単調に狭くなる**。head 紐づき評決との相互作用（`src/spirrow_mindwire/naysayer/pr_review.py:1617-1625` で debounce = `latest.commit_id != ci.head_sha`）は「同じ head で入力が広がりうる」場合にのみ危険で、それは **base の force-push（巻き戻し）** のときだけ — 本設計はそれを扱わない（§2）。∴ pin する理由が無く、**pin は「pin した瞬間からまた古びる」本欠陥の再生産になる**。

**既知の穴（`{base}` の限界。ledger に残す）**: `#9` が **squash-merge** で base に入っていた場合、merge base は前進せず、`#9` の内容は依然 diff に載る。三点 compare は「祖先関係」で引くので、squash は引けない。∴ **stacked PR を使う限り merge 戦略を squash に替えると本欠陥は別の形で戻る**。二点 `..` なら内容ベースで引けるが無関係な base 変更の逆デルタを持ち込む ∴ 採らない。

### D-3 — `{head}` は同じメタ読みの `head.sha`。`ci.head_sha` は driver から渡さない ∴ Protocol 不変

`fetch_pr_diff(pr: PrRef) -> str` のシグネチャは変えない。head sha は同じ関数内で読んだ `GET /pulls/{n}` の JSON `head.sha` を使う。**根拠**: driver（`pr_review.py:1359`）と test fake 6 箇所（`tests/test_pr_review_driver.py:124` ほか）を無改造で済ませる。配線は client 内に閉じる。

### D-4 — flag 無し・fallback 無し。旧 `pulls/{n}` diff 経路は削除する

メタ読み / compare どちらが失敗しても `GitHubHTTPError` を上げる。旧 endpoint への fallback は残さない。**根拠**: 旧経路の failure mode は**静かな誤入力**（parseable な wrong diff）であり、fallback を残すと本欠陥は再発する。新経路の failure mode は例外（可視）。**うるさく落ちる方が、静かに間違えるより安い**（lexora#10 の 2 巡目がまさに後者だった — merge 済みコードについて gate が objection を出した）。

**この決定を機械で守る**: AC-4 に「記録された request のどれも `/pulls/{n}` + diff Accept ではない」を入れる。実装が旧経路に戻ったら CI が落ちる。

### D-5 — 既存 APPROVE は遡って再評価しない

**根拠**: 修正後の入力は修正前の入力の**部分集合**である（§D-2 の単調性）。∴「より広く読んで APPROVE した」は「狭い方も APPROVE した」を含む — **ただしその広い入力が truncate されていなかった場合に限る**。

現行 gate は「truncation は critique 本文と無関係に **RC を強制する**」（`decide_verdict(body, view=...)` @ `pr_review.py:1416`、`_MAX_DIFF_CHARS=150_000` @ `:120`）∴ **truncate された入力から APPROVE は出られない**。∴ **過去の APPROVE は全て「切られていない上位集合」に対して出ている** ∴ 修正後の入力に対しても有効。

これは `T-gate-silently-suppresses-approve-on-truncated-diff` の現行規則に**依存した**論証である ∴ あちらが force-RC を緩める設計を着地させた後、この論証は将来の APPROVE には使えない。しかし**その時点までに出た APPROVE の健全性は既に確定している**ので、順序として問題は無い。

### D-6 — 未解決の RC は「直す」のではなく次巡で自然に撃ち直す

コードも手当も不要。**根拠**: RC は差し戻しであって拘束力ある評決ではない。修正後の入力で再発火すれば消えるか、残れば本物。

### D-7 — 全プロジェクト同時適用。per-project 設定を作らない

**根拠**: 共有ライブラリ 1 箇所（`spirrow_mindwire.github.client`）∴ 段階適用の受け皿を作る方が高くつく。lexora / voxelworld / magickit / playproof / verimend すべての gate に同時に効く。

### D-8 — fetch site に compare 情報を log 出力。review 本文の書式は変えない

`fetch_pr_diff` の末尾で `logger.info("fetch_pr_diff: compare %s...%s -> %d chars", base_ref, head_sha[:12], len(diff))` を出す。**根拠**: 本文書式は truncation スレッドの baseline テスト群が assert している ∴ 1 行足すだけで churn が出る。log は out-of-band で reader が入力を追える。

### D-9 — 着地後、空 merge の回避策は打ち止め

既存の空 merge は残置（無害）。**根拠**: 回避策は「毎回人が思い出す」に依存 ∴ 機構が直ったら止める。

### D-10 — URL エンコーディング（Einstein の advisory を採り込む）

`base_ref` を URL パスセグメントとして使う前に `urllib.parse.quote(base_ref, safe="")` でエンコードする。**根拠**: `feature/stacked` のようにスラッシュを含むブランチ名がパスに素で入ると GitHub API ルーターが 404 を返す（別 endpoint にディスパッチされる）。現ループは `main`/`develop` にしかマージしないので発火しないが、**「発火しない」と「起きても正しく動く」は違う**。head sha は 40 hex chars だが `quote` の適用は無害。

## §4 配線（実測 — 触るべき/触るべきでない）

| 位置 | 現状 | 変更 |
|---|---|---|
| `src/spirrow_mindwire/github/client.py:350-364` | 旧 `fetch_pr_diff`（`GET /pulls/{n}` + diff Accept） | ①`GET /pulls/{n}`（JSON）→ `base.ref` / `head.sha`、②`GET /compare/{base_ref}...{head_sha}` + diff Accept。両方 `GitHubHTTPError` fail-loud。log 1 行。 |
| `src/spirrow_mindwire/github/client.py:294` | Protocol 宣言 | **変更なし**（D-3） |
| `src/spirrow_mindwire/naysayer/pr_review.py:1359` | driver 呼び出し | **変更なし**（D-3） |
| `scripts/naysayer_review_scoped.py:121` | scoped runner 呼び出し | **変更なし**（自動継承） |
| `tests/test_github_client.py` | 旧 endpoint を assert していた fetch_pr_diff テスト | 二段 fetch と AC-1〜AC-4 をカバーするテストに置換 |
| `tests/test_pr_review_driver.py` ほか driver 系テストの `_FakeGitHub.fetch_pr_diff` | 引数 1 個の str 返しモック | **変更なし**（シグネチャ不変 ∴ Protocol を満たす） |

API 呼び出しは 1 回 → 2 回に増える。**キャッシュしない**（NONGOAL）: rate limit 上の問題は無く、共有すると「いつ読んだ値か」が曖昧になる — それが本欠陥の症状そのものである。

## §5 受け入れ条件（AC）

すべて `tests/test_github_client.py` の httpx `MockTransport` テストで assert する。

| AC | 内容 | 対応テスト |
|---|---|---|
| **AC-1** | `fetch_pr_diff` が `/repos/{o}/{r}/compare/{base_ref}...{head_sha}` を `Accept: application/vnd.github.v3.diff` で 1 回叩く。 | `test_fetch_pr_diff_uses_three_dot_compare` |
| **AC-2** | `{base_ref}` / `{head_sha}` がメタ読みの `base.ref` / `head.sha` 由来（メタ応答を変えると URL が追随する）。 | `test_fetch_pr_diff_uses_three_dot_compare` + `test_fetch_pr_diff_base_ref_follows_meta` |
| **AC-3** | メタ読みが非 2xx / `base.ref` 欠落 / `head.sha` 欠落 → `GitHubHTTPError`。**この時 compare リクエストは発行されない**。 | `test_fetch_pr_diff_meta_404_fails_loud_without_compare` + `..._meta_missing_base_ref_...` + `..._meta_missing_head_sha_...` |
| **AC-4** | compare が非 2xx（404 = base ブランチ削除、406/413 = diff 過大）→ `GitHubHTTPError`。**記録された request のどれも `/pulls/{n}` + diff Accept ではない**（D-4 の fallback 不在を機械で守る回帰ガード）。 | `test_fetch_pr_diff_compare_non_2xx_fails_loud` + `test_fetch_pr_diff_never_reads_pulls_diff_endpoint` |
| **AC-5** | driver 側の既存テストが**無改造で緑**（`_make_diff_view` は依然 fetch site で 1 度だけ呼ばれる = truncation スレッドの baseline invariant）。 | `tests/test_pr_review_driver.py` / `test_pr_review_adr_pointers.py` / `test_orchestrator.py` / `test_loop_runner.py` が gate 全体で緑 |
| **AC-6** | **現場検証（着地後、本スレッドに数値で記録）**: ①base が head の祖先を吸収した PR で `compare` < `pulls`（予測: 有意に小さい）、②base が無関係な commit だけで進んだ PR で `compare == pulls`（**予測: 一致。一致は失敗ではない** — 適用範囲の確認である）。 | 実測（landed 後に実行、本スレッドに数値で報告） |

**追加**（D-10）: `feature/stacked` のようにスラッシュを含む base_ref のとき、compare URL の raw 表現に `%2F` が現れる（`test_fetch_pr_diff_url_encodes_base_ref_with_slash`）。

**追加**（no-token パス）: token が無ければ meta / compare の**両方**の request が Authorization ヘッダなしで出る（`test_no_token_omits_auth_header`）。

## §6 想定される反論（proposer が事前に潰したもの — msg-2338 §7）

1. **「flag 無しで全プロジェクト同時は乱暴では」** — 旧経路の failure mode は静かな誤入力であり、flag はそれを温存する。かつ新経路の failure mode は例外（可視）。∴ 段階適用の利得より、二経路が併存する期間のコストが高い。
2. **「compare が 406 を返す巨大 PR で gate が完全に止まるのでは」** — 止まる。ただし旧経路でも同サイズなら cap で force-RC に落ちており、**どちらでも通らない**。違いは「理由が見える」こと。
3. **「単調狭化の主張は force-push で破れる」** — 破れる。§2 で NONGOAL として明示。
4. **「squash-merge に切り替えたら再発する」** — する。§D-2 末に既知の穴として記録。

## §7 隣接スレッドへの申し送り

`T-gate-silently-suppresses-approve-on-truncated-diff` の**残余-1（`_MAX_DIFF_CHARS` の値の再測定）**は「elision 着地後の分布に対して測る」と決まっている。本件はその分布の**分母を変える**（#11: 202,849 → 118,107）∴ 再測定の前提条件に「本修正の着地後」も加わる、という事実だけ渡す。どう扱うかはあちらの裁量。
