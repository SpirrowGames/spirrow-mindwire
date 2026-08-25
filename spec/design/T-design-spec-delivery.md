---
spec_id: SPEC-2026-08-11-design-spec-delivery
thread: T-design-spec-delivery
target_repo: spirrow-mindwire
base_branch: main
status: active
canary: required
supersedes: []
obligations:
  - OBL-SPEC-PIN
  - OBL-SPEC-RECEIPT
  - OBL-SPEC-SCOPE-CLOSURE
items:
  - id: I-1
    title: "OBL-SPEC-PIN / OBL-SPEC-RECEIPT / OBL-SPEC-SCOPE-CLOSURE を追加"
    paths: ["spec/process/obligations.yaml"]
  - id: I-2
    title: "本 manifest を spec/design に設置し spec PR を開く"
    paths: ["spec/design/T-design-spec-delivery.md"]
  - id: I-3
    title: "spec/design/verify.py を追加"
    paths: ["spec/design/verify.py"]
  - id: I-4
    title: ".mindwire/ を gitignore (spirrow-mindwire)"
    paths: [".gitignore"]
  - id: I-5
    title: ".mindwire/ を gitignore (spirrow-voxelworld)"
    status: withdrawn
    withdrawn_reason: "D-22 違反。spec は自らが置かれている repo に対してのみ authoritative であり、pin は repo 境界を越えられない（pin は local object DB を引くため、spirrow-mindwire の commit は spirrow-voxelworld に存在しない）。item 側の target_repo は D-22 により表現不能になったため意図的に削除した（消し忘れではない — §6 を見よ）。voxelworld 側の .gitignore が必要になった時点で、voxelworld 自身の spec として発行すること。"
    paths: [".gitignore"]
---

# spec delivery — 隔離された implementer に仕様を届ける機構

## §0 この文書の読み方

**この文書は自己完結している。** 読む側が thread T-design-spec-delivery の過去メッセージを復元する必要はなく、また復元できることを前提にしてもいけない（実測: watcher は新着 msg 1 本ごとに `body = msg["content"]` の単一イベントを作り、スレッド履歴は渡らない）。本文に無いものは、この仕様の一部ではない。

論拠・検討経緯は thread に残す。本文は**決定項目（id 付き）・スキーマ・受け入れ条件**に限る。

決定 id は本文書内で振り直している。thread 側 id との対応: thread D-1（分量規律）= 本書 D-1、thread D-2（著者と配送）= 本書 D-2（全面改訂）、thread D-4（payload は sha 1 個に縮む）= 本書 D-4。他は本書で新規に振ったもので、thread の同名 id とは対応しない。**`D-25′` の `′` は id の一部である**（本書に `D-25` は無い。スレッド上でこの決定が一度改訂された経緯の表記をそのまま保っており、書き換えると thread 側の全参照が切れる — D-30）。

## §1 前提（実測値。推測で補わないこと）

以下は実測値である（初出は 2026-08-11。以後の再測は各行の出典欄に計測者と日付を記す）。本仕様はこれらに依存する。**行ごとに計測者を明記する** — role が同じでも **embodiment（稼働形態）が異なればツール面が異なる**からである。embodiment は role と直交する独立の層であり（ADR-2026-05-27-09 の identity 4 レイヤーモデル: identity_name / independence_class / role / embodiment）、ADR-2026-05-29-12 により各セッションが自己申告する値である。実測: 同じ proposer role でも、自律ループの `build_proposer` が構成する embodiment では `Read` / `Glob` / `Grep` のみで `Bash` を持たない（E-1）のに対し、`/mindwire-turn` による手動 Claude Code セッションの embodiment では `Bash` を持つ（2026-08-24、proposer 自身が後者の embodiment で `git rev-parse` を実行し本書の base blob を実測。**この行はその実測によって書き換わった**）。∴ 「git に問い合わせる計測は implementer からしか来ない」は**偽**であり、計測者欄が要る理由は role ではなく **embodiment が可変であること**である。なお D-25′ が base 照合を implementer に課す理由は「proposer が測れないから」ではない — **測る対象は implementer の作業ツリーの状態**であり、それは実装する当人にしか見えないからである（この理由は embodiment に依存しない ∴ proposer がどの embodiment で稼働していても成立する）。実装時に事実が違っていた場合、仕様ではなく事実が正しい。停止して報告せよ。

| id | 事実 | 出典 / 計測者 |
|---|---|---|
| E-1 | **`build_proposer` が構成する embodiment における** proposer の capability は `{READ_THREAD, POST_REPLY}` の 2 つのみで、`EXECUTE_CODE` を持たない。**この embodiment では書き込み系ツールが無い**（`Write` / `Edit` / `Bash` いずれも不在）∴ ファイルを書けず、commit も PR 作成もできない。**ただし読める**: `_PROPOSER_BUILTIN_TOOLS = ("Read", "Glob", "Grep")` が `cwd=repo_dir` ＋ `can_use_tool=_PathScopeGuard(root=repo_dir)` で渡される。**本行は embodiment の性質であって role の普遍的性質ではない**（ADR-2026-05-27-09: role と embodiment は直交する）— `/mindwire-turn` による手動 Claude Code セッションの embodiment で同じ role を演じる場合は `Bash` を含む完全なツール面を持つ（行 45） | 実査 — `dd07135`, `src/spirrow_mindwire/loop_runner.py` 行 281 `_PROPOSER_BUILTIN_TOOLS` ／ 行 284-295 `build_proposer`（Bohr, 2026-08-24 再確認）。手動セッション embodiment で `Bash` が在ることは同日 proposer が `git rev-parse` の実行成功で実証（Bohr, 2026-08-24）  r3 の `tools=[] の text-only` は 2026-08-11 時点では真だったが `906cb58`（08-18）で偽になった |
| E-2 | proposer から `EXECUTE_CODE` を落としてあるのは意図された設計。registry の `qualified_for` は「first qualified」であり、base adapter（`ClaudeCodeSdkAdapter`）が `EXECUTE_CODE` を宣言している ∴ 両方を registry に入れると IMPLEMENTER スロットが登録順で決まってしまう。`Stage3ProposerAdapter` が `EXECUTE_CODE` を落とすことで PROPOSER にのみ qualify し、IMPLEMENTER スロットが `ImplementerSdkAdapter` に一意に解決する。**r3 の「現に稼働している implementer は allow-list の後ろにある」は 2026-08-20 以降 偽である**（E-12） | 実査 — `9be0c81`, `src/spirrow_mindwire/loop_runner.py` の `Stage3ProposerAdapter` docstring（Bohr, 2026-08-24）。r3 が挙げていた出典 `adapters/claude_code_sdk.py` docstring には現在この記述が無い |
| E-3 | implementer / naysayer のみツールを持つ | 同上 — human |
| E-4 | **implementer と naysayer は CLAUDE.md を読まない**（implementer は `setting_sources=[]`）。proposer のみ読む | ループ設定 — human |
| E-5 | implementer に渡るのは新着 msg 1 本の body のみ。スレッド履歴は渡らない | watcher 実装 — human |
| E-6 | repo 単位の classic branch protection は両 repo とも未設定である（`GET /repos/<owner>/<repo>/branches/main/protection` は **404**。r7 以前の本行が書いていた 403 は誤りである）。**ただし Organization ruleset が両 repo の `main` に現に効いている** — `SpirrowGames` の `ruleset_id=21017016` が `pull_request`（PR 必須・approving review 1 件・team reviewer 指定・`require_extra_approval_for_unattributed_changes: true`）／`non_fast_forward`／`deletion` を強制する ∴ **`main` への直接 push 禁止と force-push 禁止は機構として現に設定されている**。required status check は本 ruleset に含まれず現時点で未設定だが、「設定できない」ことの根拠は無い。**bypass actor の構成は未計測である**（`admin:org` スコープを持たない token では ruleset 詳細が読めない）∴ admin バイパスの可否は本行では主張しない。機構で強制できないのは「merge を実行する主体が human であること」だけである — ruleset は主体の種別を区別しない | 実測 — `gh api repos/SpirrowGames/spirrow-mindwire/rules/branches/main` ＋ 同 `spirrow-voxelworld`（Einstein msg-1569 ／ Heisenberg msg-1581 ／ Bohr 2026-08-24 の 3 者が独立に同一結果）。r3〜r7 の「両 repo で使用不可（403 / GitHub Pro 不採用）」は誤りだが、ruleset がいつ入ったかは本行では特定していない |
| E-7 | `spirrow-mindwire` に `develop` は無い。PR は `feature/*` → `main`。マージは常に人間 | 実測 — human |
| E-8 | `gh pr merge` は base に関わらず Tier C 拒否となり implementer セッションを halt させる | `OBL-MERGE-MECHANISM` / PR #136 — human |
| E-9 | `OBL-DECLARE-UNREADABLE` は `origin.moved_from` を持つ逐語移設 entry で、canary が `len(body) == origin.original_length`（現在 795）を検査している | `spec/process/obligations.yaml` / PR #135 — human |
| E-10 | `.mindwire/pin` は spirrow-voxelworld のクローンにも現れる（implementer は両プロジェクトで同じ prompt 経路を通る） | 実測 — human |
| E-11 | `spec/process/obligations.yaml` の entry スキーマは `{id, role, body, origin?}`。`role` は**単一文字列**で `src/spirrow_mindwire/obligations.py` の `_MANIFEST_ROLES`（`implementer` / `naysayer` の 2 値）に照合される。`applies_to` / `trigger` は**認識されないキー**であり、未知の role や不正な `origin` は `ObligationsError`（composition root で `SystemExit`）になる | 実査 — `origin/main` = `72339ee`, `spec/process/obligations.yaml` ＋ `src/spirrow_mindwire/obligations.py`（Heisenberg, msg-1356）。`9be0c81` で再確認（Bohr, 2026-08-24）。r3 の E-11 は `{id, applies_to, trigger, body}` を主張していたが誤り。実査ラベルを持ちながら sha を欠いていたため、書かれた時点で照合できなかった（D-29 の根拠事例） |
| E-12 | implementer の tool surface は `_IMPLEMENTER_BUILTIN_TOOLS = ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "BashOutput", "KillShell", "Glob", "Grep", "TodoWrite")` であり **`Bash` を含む**。per-call の `can_use_tool` allow-list は 2026-08-20 に撤去された ∴ `git fetch` を拒める gate は存在しない。不変条件は agent の外（GitHub org ruleset `guard-default-branch` ／ `spirrow_mindwire.preflight` の P0-P2 ／ egress proxy の allow-list ／ implementer の clone が使い捨てであること）が担う | 実査 — `9be0c81`, `src/spirrow_mindwire/adapters/implementer.py` の `_IMPLEMENTER_BUILTIN_TOOLS` ＋ module docstring。撤去は `#165` `773ee24` / `#166` `72339ee`、いずれも本ブランチの祖先（Heisenberg, msg-1547。Bohr 再確認 2026-08-24） |

**未検証（前提に使ってはならない項目）: 現在 0 件。**

U-1（implementer の allow-list に `Bash` ないし `git fetch` が含まれるか）は 2026-08-24 に解消した。allow-list そのものが 2026-08-20 に撤去されており、問いの前提が消えている（E-12）。∴ §3 手順 8 の分岐と reason code は変更しない — `NO-PIN(FETCH_UNAVAILABLE)` は `--no-fetch` と実ネットワーク障害という 2 経路で到達可能なままであり、到達不能な死枝にはならない（A-9）。

**E-4 の帰結（規範）: 「CLAUDE.md に書いてあるから implementer が従う」という設計を書いてはならない。** implementer に効かせたい規約は `spec/process/obligations.yaml` か、本 manifest 本文のいずれかに置く。

### §1.1 規範根拠（要検証引用）

本設計は **ADR-2026-05-23-07 — Stage 3 Autonomy Gating + Implementer 安全設計** の要請を実装に落とすものであり、E-1〜E-3 の capability 分離はこの ADR に由来する。

**この引用は proposer が検証していない**（E-1 により ADR を読めない）。出典は Einstein の実査申告（2026-08-11）である。**A-15 で着地時に検証すること。番号・表題・内容のいずれかが実在と食い違う場合、実装者は黙って修正せず、停止して報告せよ** — 誤った参照を静かに正しくすると、誤りがどこで混入したかが失われる。

## §2 決定

- **D-1（分量規律）** manifest には決定項目・スキーマ・受け入れ条件のみを書く。論拠は thread に残す。この規律は本 manifest 自身にも適用される。
- **D-2（著者と、commit する手の分離）** 設計の著者は proposer、ファイルを置くのは implementer である。E-1 により proposer は書き込み行為を一切行わない。配送経路は次の 6 段で固定する:
  1. proposer が thread で設計を詰める。
  2. proposer が **manifest のファイル本文そのもの**を 1 通の msg として投稿する（front-matter を含む、そのまま保存すれば spec ファイルになるバイト列）。これが「proposer が書く」の実体であり、著述行為であって書き込み行為ではない。
  3. human はその msg 本文を**一字も変えずに** implementer へ dispatch する。human はパイプであって著者ではない。
  4. implementer は受け取った本文をそのまま `spec/design/<thread-id>.md` に置き、spec PR を開く。
  5. naysayer は **PR の diff** をレビューする。このとき PR の中身と chatroom の msg 本文を突き合わせられる ∴ human が途中で改変していないことは第三者検証可能である。
  6. human の Tier-C ＝ その spec PR を merge する。merge が承認である。
  human が本文を運ぶのは **spec 1 本につき 1 回**、運ぶのは要約ではなくバイト列である。**手順 3 の byte 一致は本設計が買っている検証可能性の本体であり、他の目的（診断の静音化・体裁の統一等）のために売り渡してはならない。**
- **D-3（payload の自己完結）** implementer に渡す payload は、参照ではなく本文でなければならない（E-5）。「§4 を見よ」「前回の合意どおり」の類は payload として無効である。要約ラベルでの代置も無効である。自己完結性の単位は **payload（＝ manifest 1 本）**であって item ではない。
- **D-4（payload の収縮）** 2 ターン目以降の implementer は `.mindwire/pin` と `origin/main` から仕様を読む ∴ 人手のリレーは実装ターン数に比例せず、payload は sha 1 個に縮む。なお pin は repo 境界を越えない（D-22）。他 repo を対象とする item は本機構では扱わず、対象 repo 自身の spec として発行する。
- **D-5（pin は tracked にしない）** `.mindwire/pin` は dispatcher が working tree に書く untracked ファイルであり、commit しない。**本 spec が `.gitignore` に `.mindwire/` を入れるのは `spirrow-mindwire` のみである**（I-4）。E-10 のとおり同じ pin ファイルは spirrow-voxelworld のクローンにも現れるが、**本 spec はその repo の `.gitignore` を変更しない** — spec はそれが置かれている repo に対してのみ authoritative であり（D-22）、それを行う item であった I-5 は同じ理由で withdrawn である（D-23）。∴ voxelworld 側で pin が untracked のまま `git status` に現れることは本 spec の**既知の未解決残渣**であり、隠さずここに記す。閉じるのは voxelworld 自身の spec である（§6）。
- **D-6（fail-closed 解決）** pin の解決は fail-closed である。判定不能はすべて `NO-PIN` に落とす。特に detached HEAD（`git rev-parse --abbrev-ref HEAD` が `HEAD` を返す／失敗する）は例外を投げずに `NO-PIN` とする。これは「例外を握り潰す」のではなく「判定不能 = pin 無し」として明示的に扱う、という意味である。
- **D-7（branch スコープ）** pin は `branch` フィールドを持ち、現在の HEAD ブランチ名との**完全一致**でのみ有効。ワイルドカード・前方一致・正規表現は導入しない。不一致の pin は**存在しないものとして扱う**（`NO-PIN`）。
- **D-8（obligations の置き場）** `OBL-SPEC-*` は `spec/process/obligations.yaml` に置く。Python の文字列リテラルに直書きしない。3 件はいずれも net-new であり、**`origin` ブロックを付けない**（付けると canary ②″ を構造的にすり抜ける）。
- **D-9（OBL-DECLARE-UNREADABLE は改訂しない）** E-9 の逐語移設 entry には触れない。unreadable 宣言義務の trigger 拡張分は `OBL-SPEC-PIN` の body 側に書く（§4-1 末尾）。逐語移設 entry を net-new の都合で動かすと、その entry が守っている不変条件の意味が薄まる。
- **D-10（verify.py は gate ではない）** `verify.py` は診断ツールであり、CI gate にしない。`main` 上での検出は **warning** に降格し、exit code に影響させない。
- **D-11（G-4 は機構ではなく規律）** E-6 のとおり Organization ruleset が `main` への直接 push と force-push を機構として拒否している。**しかし「merge を実行する主体が human であること」だけは ruleset が区別できない** ∴ その一点は規律で担保する。そして本仕様はマージを機構で強制しない — 本仕様は**マージ手順に一切触れない**。implementer に `gh pr merge` を実行させる記述を、本仕様およびその実装から出してはならない（E-8: セッションが halt する）。PR を開くところまでが implementer の仕事であり、merge は human の Tier-C である。
- **D-12（bootstrap は NO-PIN で始まる）** I-1 と I-2 は、pin 機構がまだ存在しない状態で実行される ∴ その 2 ターンは `NO-PIN` であり、payload は D-2 経路の msg 本文そのものである。これは違反ではなく設計どおりの挙動であり、receipt は `NO-PIN` と書くのが正しい。なお pin は repo 境界を越えない（D-22）。他 repo を対象とする item は本機構では扱わず、対象 repo 自身の spec として発行する。
- **D-13（第一号実運用対象）** pin つき dispatch の第一号実運用対象は **`T-pr-gate-adr-index-scope`** とする（`T-loop-readable-obligations` は PR #135 で完了・close 済みのため対象から外す）。
- **D-14（到達性判定と遅延 fetch）** pin の `commit` が `origin/main` から到達可能であることを確認する。この検査は「pin が指す spec は human が merge したものである」を機械的に担保する唯一の経路である。E-6 の ruleset は PR を経由することまでは強制するが**主体が human であることは強制しない** ∴ この検査を外さない。ネットワーク規律は次のとおり:
  - **肯定側は fetch しない。** ローカルの `origin/main` から到達可能なら `RESOLVED` に進む（古い `origin/main` の祖先である commit は、より新しい `origin/main` の祖先でもある ∴ 肯定判定は陳腐化しない。main が force-push で書き換えられないことを前提とする。**この前提は Organization ruleset の `non_fast_forward` が機構として守っている**（E-6）— r7 以前の本行は「E-6 により機構では守れない ∴ D-11 と同じく規律である」と書いていたが、それは過小申告であった）。
  - **否定側でのみ、1 回だけ** `git fetch origin +refs/heads/main:refs/remotes/origin/main` を試み、再判定する。連続再試行はしない。refspec を明示するのは、remote-tracking ref が更新されるか否かを `remote.<name>.fetch` の設定に依存させないためである（設定済みの clone では bare の `git fetch origin main` と挙動が一致することを実測した。2026-08-25）。
  - fetch が失敗・不可の場合は `NO-PIN(FETCH_UNAVAILABLE)`。fetch 後もなお到達不能なら `NO-PIN(COMMIT_UNREACHABLE)`。**この 2 つを同じコードに畳まない** — 前者は「クローンが main を見られない」、後者は「未 merge の commit を pin した」であり、直す相手も直し方も異なる。
- **D-15（順序の SOT は items の列挙順）** item 間の依存グラフ機構は持たない。`after` 相当のフィールドを導入せず、`verify.py` に循環検査も置かない。dispatch するのは人間であり 1 ターンに 1 つの item id が渡るだけで、自動シーケンサは存在しない ∴ **front-matter の `items` 列挙順が実行順であり、それが順序の唯一の記述である**。§8 は順序を再宣言せず、item でない段のみを述べる。
- **D-16（obligations は union）** 有効な obligation 集合は **manifest 直下 ∪ item 直下**である。item 側の記述は**追加のみ**であり、削除は仕様上表現できない。item に全列挙を強制しない（自己完結性の単位は payload であって item ではない — D-3）。
- **D-17（外部規範への参照は着地時に検証する）** ADR その他の外部文書を引用するときは、引用者が検証していない場合に**出典と計測者を明記**する。実装者は着地時に実在と内容一致を確認し、食い違う場合は黙って修正せず停止して報告する。
- **D-18（`status` は git 上の所在を表さない）** `status` は文書の編集状態のみを表し、enum は **`active` | `withdrawn`** の 2 値である。`proposed` / `superseded` は持たない。
  - merge 済みか否かは **git が知っている** ∴ ファイルに書かない（二重管理の禁止）。`active` は「この文書は、それが存在する場所において有効である」を意味し、feature ブランチ上では提案、`main` 上では仕様として読まれる。**著者が書いた値のまま変わらない。**
  - ∴「main 上に居るのに `proposed`」という矛盾状態が構造的に発生しない。それを検出する検査も、解消する mutation も不要になる。
- **D-19（manifest は merge 後 immutable）** merge 済み manifest を書き換えない。訂正・改訂は**新しい spec を起こし `supersedes` で繋ぐ**。唯一の例外は `status: withdrawn` への変更であり、その場合も元のバイト列は merge commit（`git show <merge-commit>:<path>`）から復元でき、D-2 手順 5 の突き合わせは失われない。
- **D-20（supersession は派生）** X が superseded であることは「他の manifest Y の `supersedes` に X が載っている」ことと同値である ∴ X 側に状態を書かず、X のファイルを書き換えない。`verify.py` は派生した関係を報告する。
- **D-21（診断は常態で鳴らない）** 定常状態で恒久的に warning を出す診断を設計しない。常に鳴っている警告は警告ではなく、本当に見るべきものを隠す。main 上の常態は **warning 0** である（A-12）。
- **D-22（1 spec = 1 target repo）** spec ファイルは、それが置かれている repo に対してのみ authoritative である。`target_repo` はそのファイルを収める repo と一致しなければならず（`verify.py` の V-11 が照合する）、item 側で上書きできない（V-12）。N repo に跨る設計は N 本の spec になり、共通の `design_id`（スレッド id）と、名前だけの `siblings` で結ぶ。sibling は pin で結ばない（結べない — pin は local object DB を引くため、他 repo の commit はそこに無い）。列挙は人間向けであって解決機構ではない。棄却した代替 3 案（cross-repo だけ `NO-PIN` ＋全文インライン／spec blob の vendor 複製／対象 repo から他 repo の object を fetch）の論拠は thread に残す（D-1）。
- **D-23（I-5 は withdrawn にする）** I-5 は D-22 違反 ∴ item 側 `status: withdrawn` ＋ `withdrawn_reason` とする。削除しないのは、なぜ消えたかが残る方が将来の再提案を防ぐからである。D-18 が enum に `withdrawn` を残したのはこの用途である。
- **D-24（`NO-PIN` を 2 クラスに割る）**
  - **ABSENT** — dispatch に pin が無い。D-12 のブートストラップ窓（spec が `main` に未着）に限り sanctioned であり、message body のみで作業してよい。
  - **FAULT** — pin は発行されたが解決しなかった（`REPO_MISMATCH` / `COMMIT_UNREACHABLE` / `BLOB_UNREADABLE`、§3 の fallback を尽くした後）∴ 停止して報告する。**message body で作業を続行してはならない。**
  - 根拠: 「pin が出ているのに解決しない」は上流の故障であって、劣化運転してよい状態ではない。body で続行するのは本スレッドの起点となった失敗そのものである。
- **D-25′（改訂の運び方 — base-hash ＋ 構造アンカー ＋ NEW のみ）** proposer の改訂は対象ファイルの blob sha（`base`）を名指す。implementer は編集前に照合し、一致しなければ `BASE_MISMATCH` で停止する。これが陳腐化検査であり、逐語 byte を 1 文字も要さない。
  - **照合は 2 段**（U-3）: ① `git status --porcelain -- <path>` が空であること ② `git rev-parse HEAD:<path>` が `base` と一致すること。**作業ツリーのハッシュ化（`git hash-object`）を使わない** ∴ `core.autocrlf` / clean-smudge filter の影響を受けず、偽 `BASE_MISMATCH` が起きない。
  - `git rev-parse HEAD:<path>` が `fatal: path ... does not exist in 'HEAD'` で失敗した場合は、それ自体を「大規模 drift」条件とみなし D-27-b に直行する。anchor ファイル自体が消えている ∴ diff を取る対象が無い。
  - **位置は逐語引用ではなく構造アンカーで指す**（§見出し／item id／decision id／evidence id／行番号）。短い識別子であり、LLM が壊しにくく、文書内で一意である。
  - **逐語なのは NEW だけ。** 新しい規範文はどこかから来るしかない ∴ 不可避。ただし NEW は小さく保ち、全文を再発行しない。
  - 適用後、implementer は**適用後の blob sha を報告する**。それが次の改訂の `base` になる。
  - review 側の義務も同じ形である。「対象ブロックを逐語引用せよ」ではなく「読んだファイルの sha ＋ 位置（§／id／行番号）を述べよ」。レビュアにも打ち直しをさせない。
  - これは逐語 OLD block による置換より強い。OLD block は patch 対象ブロックの一致しか見ないが、base-hash はファイル全体の一致を見る ∴ patch 対象外の場所に起きた drift も捕まる。しかも LLM の打ち直し量はゼロである。
- **D-26（委譲境界）**
  - **変換は委譲可。** ただし規則側が ①決定的な変換規則 ②変換後も意味が保存されること ③保存できない箇所が出たら停止して報告（`TRANSFORM_AMBIGUOUS`）— の 3 点を備えること。
  - **規範テキストの起草は委譲不可。** proposer が NEW block として逐語で出す。implementer が自分の言葉で書き直せば、以後の implementer はその言い換えに従う ∴ その瞬間 implementer は proposer になっている。
  - **human dispatch は「決定 ＋ その決定の対象 byte」を運んでよい。「純粋なデータ転送」は運ばせない。** 承認は、承認対象の文面を伴ってはじめて承認である ∴ 前者まで禁じると Tier-C 承認そのものが不可能になる。
- **D-27（CAS 敗者の回復）** D-25′ は楽観的並行制御（compare-and-swap）であり、`base` が lock である。同一 spec に 2 つの改訂が並走すれば後着が必ず負ける。負けること自体は正しい（盲目上書きを防ぐ）。implementer は `BASE_MISMATCH` で停止する際、報告に以下を含める:
  1. **実際の sha**（`git rev-parse HEAD:<path>`）。
  2. **anchor ごとの存否と現在行範囲** — proposer が指定した各構造 anchor について `present@L120-L134` ／ `absent` を列挙。
  3. **`git diff <base> <actual>` の出力**（ファイル全文ではない）。**path limiter を付けてはならない** — blob 同士の diff では git が拒否する（blob は path metadata を持たないため）。ヘッダは `a/<base>` `b/<actual>` になり **path が出ない** ∴ **報告本文に path を別記すること**。行番号と文脈は正しく出る。
  4. handoff は proposer 宛て。
  - 全文でなく diff なのは、proposer が必要としているのが「新しいファイル」ではなく「何が変わったか」だからである。diff のサイズは drift に比例し、かつ機械生成物であって LLM が作文しない。anchor が消えた場合も diff に写る ∴ 別機構は不要である。
  - **上限**: diff が局所的でない場合（anchor が総崩れ／変更が spec 全体に及ぶ）は貼らずに D-27-b に回す。
- **D-27-b（統治エスカレーション）** anchor が総崩れになるような drift、または対象ファイル自体が消えている場合、implementer は proposer ではなく **human に Tier-C としてエスカレーション**する。これは「同一の spec ファイルに 2 つの権威が同時に書いた」ことを意味し、D-19 と D-25′ が禁じている状態が実際に起きたということである ∴ テキストを運んで辻褄を合わせる問題ではない。問うのは「この spec の所有権はどちらの改訂系列にあるか」であり、human が運ぶのは決定であってテキストではない。決定後、敗者側の改訂は破棄し、新しい `base` から起案し直す。
- **D-28（obligations エントリは on-disk schema に従う）** エントリは `{id, role, body, origin?}` である。`role` は**単一文字列**で `_MANIFEST_ROLES` allow-list に照合される（E-11）∴
  - 複数 role に係る義務は role ごとに別エントリにする（id を role 接尾で分ける）。`applies_to` は使わない。
  - `trigger` キーは存在しない ∴ 発動条件は `body` の第一文に書く。書式で代替し、キーを捏造しない。**認識されないキーは loader が黙って捨てる** ∴ 書けば情報が消える。
  - `origin` は逐語移設専用の予約ブロックである（`moved_from` ＋ `original_length` が必須で、`len(body) == original_length` を canary が強制する）。net-new のエントリには付けない — 付ければ「移設した」という偽の主張になり、canary の意味を薄める（D-8 / A-1）。
- **D-29（実査は pin されていなければ実査ではない）** `実査` と記された evidence 行は commit sha ＋ path（＋読み取りコマンド）を必ず伴う。伴わない行は evidence ではなく主張として扱う。根拠: E-11 は実査ラベルを持ちながら誤っていた。sha があれば、書いた時点で照合可能だった。実査ラベルは自己検証しない。
- **D-30（決定 id は再利用も再割当もしない）** 一度この文書に書かれた決定 id は、意味を変えない。新しい決定には未使用の id を与える。既存 id を新しい決定に振り直すと、その id を記憶している読み手・thread・レビューが**気づかないまま別の規則を読む** — バイト列としては正しく、参照としても解決する ∴ `BASE_MISMATCH` も `verify.py` も検出できない、唯一検出不能な種類の drift になる。追記位置は末尾であり、番号順と論理順は一致しなくてよい。

### §2.1 検査されていないもの（過大申告しない）

以下は機械検査できない。緩和はあるが、検査ではない。

1. **チャット msg を経由するテキストの転記忠実性。** 該当するのは ①proposer が出す NEW block ②D-27 の recovery diff。期待 hash を誰も先に計算できない ∴ 原理的に不可能である。緩和は 2 つだけ — NEW を小さく保つこと、PR diff を naysayer が message と突き合わせること。後者は人／エージェントによる比較であって機械検査ではない。
2. **ADR 本文。** A-15 は id ＋ title までしか照合できない。naysayer も implementer も ADR 本文を読めない（bodies は Drive にあり、inference 時に不可視）∴ ADR との整合は title 推論に留まる。
3. **spec が世界について述べた主張の真偽。** E-11 / E-2 / E-6 がこれである。D-29 で「書いた時点で照合可能」までは下げられるが、消えない。最終防波堤は §1 の「実装時に事実が違っていた場合、仕様ではなく事実が正しい。停止して報告せよ」＋ implementer の停止であり、これは実際に **3 度**作動した（E-11 は loader が落ちる前に、E-2 は merge 前に、E-6 は naysayer の独立 probe ＋ implementer の停止によって merge 前に捕まった）。

## §3 `.mindwire/pin` schema

**場所**: repo root 直下 `.mindwire/pin`。**形式**: YAML 単一ドキュメント、UTF-8、LF、タブ不可。**tracked にしない**（D-5）。

| field | type | required | 意味 |
|---|---|---|---|
| `schema_version` | int | ✔ | 現在 `1`。未知の値は `NO-PIN` |
| `spec_id` | str | ✔ | 対象 manifest の `spec_id` |
| `thread` | str | ✔ | 対象 thread id |
| `repo` | str | ✔ | `git remote get-url origin` の basename から `.git` を除いたもの |
| `branch` | str | ✔ | この pin が有効なブランチ名（完全一致、D-7） |
| `path` | str | ✔ | repo root 相対の manifest パス |
| `blob_sha` | str | ✔ | manifest 内容の `git hash-object` 値。40 桁小文字 hex |
| `commit` | str | ✔ | その blob を含む commit。`origin/main` から到達可能であること。40 桁小文字 hex |
| `pinned_at` | str | ✔ | RFC3339 UTC（例 `2026-08-11T09:30:00Z`） |
| `pinned_by` | str | ✔ | 自由記述（例 `human`） |

未知フィールドは無視してよい（前方互換）。必須フィールドの欠落は `NO-PIN`。

例:

```yaml
schema_version: 1
spec_id: SPEC-2026-08-11-design-spec-delivery
thread: T-design-spec-delivery
repo: spirrow-mindwire
branch: feature/spec-delivery-i3
path: spec/design/T-design-spec-delivery.md
blob_sha: 4b825dc642cb6eb9a060e54bf8d69288fbee4904
commit: 1f0a3c9e5b7d2a4f6c8e0b1d3f5a7c9e1b3d5f70
pinned_at: 2026-08-11T09:30:00Z
pinned_by: human
```

### 解決手順（fail-closed。すべての分岐は例外ではなく `NO-PIN` + reason に落ちる）

1. `.mindwire/pin` が無い → `NO-PIN(ABSENT)`
2. YAML パース失敗 → `NO-PIN(PARSE_ERROR)`
3. `schema_version != 1` → `NO-PIN(SCHEMA_VERSION)`
4. 必須フィールド欠落／型不一致／`blob_sha`・`commit` が 40 桁小文字 hex でない → `NO-PIN(MISSING_FIELD)`
5. `git rev-parse --abbrev-ref HEAD` が `HEAD` を返す／非 0 終了／空 → `NO-PIN(DETACHED_HEAD)`
6. 現在ブランチ名 ≠ `branch` → `NO-PIN(BRANCH_MISMATCH)`
7. 現在 repo 名 ≠ `repo` → `NO-PIN(REPO_MISMATCH)`
8. 到達性（D-14）:
   - `git merge-base --is-ancestor <commit> origin/main` が真 → 次へ（**fetch しない**）
   - 偽、または `origin/main` ref が無い → `git fetch origin +refs/heads/main:refs/remotes/origin/main` を**1 回だけ**試みる
     - fetch 失敗／実行不可 → `NO-PIN(FETCH_UNAVAILABLE)`
     - fetch 成功、再判定して真 → 次へ
     - fetch 成功、再判定して偽 → `NO-PIN(COMMIT_UNREACHABLE)`
9. `git show <commit>:<path>` が失敗 → `NO-PIN(BLOB_UNREADABLE)`
10. 取得内容の `git hash-object` ≠ `blob_sha` → `NO-PIN(SHA_MISMATCH)`
11. すべて通過 → `RESOLVED`。取得した内容がそのターンの仕様である。

reason code は上記 11 種で閉じる（`ABSENT` / `PARSE_ERROR` / `SCHEMA_VERSION` / `MISSING_FIELD` / `DETACHED_HEAD` / `BRANCH_MISMATCH` / `REPO_MISMATCH` / `FETCH_UNAVAILABLE` / `COMMIT_UNREACHABLE` / `BLOB_UNREADABLE` / `SHA_MISMATCH`）。receipt（§4-2）はこの code をそのまま書く。

**pin 解決は他の manifest を読まない。** 対象 spec が withdrawn / superseded であるかは解決経路に含めない（他ファイルの走査を hot path に持ち込まないため）。その検出は `verify.py` の V-10 が warning として担う。

## §4 obligation 本文

`spec/process/obligations.yaml` に **3 件を net-new として追加**する（D-8: `origin` ブロックを付けない）。キーは on-disk スキーマ `{id, role, body}` に一致している（E-11 / D-28）。`applies_to` / `trigger` は loader が**認識しないキー**であり、書いても黙って捨てられる ∴ 使わない。

### §4-1 `OBL-SPEC-PIN`

```yaml
- id: OBL-SPEC-PIN
  role: implementer
  body: |
    Before you do anything else on a turn, look for `.mindwire/pin` at the
    repository root and resolve it exactly as the spec delivery manifest
    specifies. Resolution is fail-closed: an absent file, a parse error, an
    unknown schema_version, a missing or malformed required field, a branch
    that does not equal `git rev-parse --abbrev-ref HEAD` (a detached HEAD
    reports `HEAD` or fails, and is therefore never a match), a repo name that
    does not match, a pinned commit you cannot confirm is reachable from
    `origin/main`, a blob you cannot read, or a content hash that does not
    equal `blob_sha` — every one of these resolves to NO-PIN with the
    corresponding reason code. Do not raise, do not retry with a guess, and do
    not repair the pin.

    Reachability has one network rule. If the pinned commit is already an
    ancestor of your local `origin/main`, accept it and fetch nothing. Only if
    it is not — or if you have no `origin/main` ref at all — run `git fetch
    origin +refs/heads/main:refs/remotes/origin/main` exactly once and judge
    again. If that fetch fails or is unavailable to you, the verdict is
    NO-PIN/FETCH_UNAVAILABLE: you could not determine the answer. If the fetch
    succeeds and the commit is still not reachable, the verdict is
    NO-PIN/COMMIT_UNREACHABLE: the pin names a commit that is not on `main`,
    which usually means the specification was never merged. Report whichever
    code you got; they have different causes and different fixes, and
    collapsing them costs the reader the diagnosis.

    NO-PIN is a state to report, not an obstacle to route around. Say NO-PIN in
    your reply with its reason code, and treat the message body you were given
    as the only specification you have for this turn. Do not reconstruct,
    infer, or recall specification content you cannot read in this turn: a
    remembered spec and a read spec are indistinguishable in your own output
    and distinguishable to no one else. If the message body alone does not
    contain enough to act on, stop and say what is missing.

    Never delete `.mindwire/pin`. Do not delete, rename, move, truncate,
    rewrite, or gitignore it, and do not include it in any cleanup, tidying, or
    formatting change. If it looks stale, wrong, or inconsistent with the work
    you were asked to do, report that and stop; the pin is written by the
    dispatcher and is not yours to correct.

    When the pin resolves, the pinned document is the specification for the
    turn. The message body may narrow what you are asked to do within that
    document, but it may not silently contradict it. If it does, stop and
    report the contradiction, naming both sides; do not choose one and proceed.

    If you cannot read the pinned document — reasons BLOB_UNREADABLE,
    COMMIT_UNREACHABLE, FETCH_UNAVAILABLE, or SHA_MISMATCH — declare it
    unreadable in the same form OBL-DECLARE-UNREADABLE requires for the sources
    it names, and stop there. Reading that entry for the declaration form is
    part of this obligation.
```

本 body の `Never delete ...` の段落は、本設計 §1 の負の制約の**逐語**である。実装時に改稿してはならない。

末尾段落は D-9 の帰結であり、`OBL-DECLARE-UNREADABLE` の body には**一切触れない**ことでその entry の `origin.original_length`（E-9）を保つ。

### §4-2 `OBL-SPEC-RECEIPT`

```yaml
- id: OBL-SPEC-RECEIPT
  role: implementer
  body: |
    Open every reply in which you performed, or attempted, implementation work
    with a receipt naming what you actually read this turn, on one line:

      SPEC <spec_id> <blob_sha first 12> <path> (pin: RESOLVED)

    or, when the pin did not resolve:

      SPEC (pin: NO-PIN/<reason code>) — worked from message body only

    Follow it with the item ids from the specification you acted on. The
    receipt reports what you read, not what you believe to be true: if you
    worked from the message body alone, it says NO-PIN and names no sha, and
    that is a correct receipt, not a confession. A reply that does work without
    a receipt is incomplete. A receipt naming a sha you did not read in this
    turn is a false statement about your own execution, and is worse than no
    receipt at all — it is the one claim in your output that no reviewer can
    check against the diff, so it is the one claim you must not get wrong.
```

### §4-3 `OBL-SPEC-SCOPE-CLOSURE`

```yaml
- id: OBL-SPEC-SCOPE-CLOSURE
  role: implementer
  body: |
    On any turn performed under a resolved specification, the specification's
    `items` are the whole of your mandate for the turn. Do not change files
    outside the paths an item declares, do not act in a repository no item
    names, and do not add work that no item declares, however obviously
    necessary it looks. If the declared scope cannot be completed without work
    outside it, stop and report the gap — the item id, the work you believe is
    missing, and why — and let the proposer amend the specification. An
    amendment costs one turn; an undeclared change costs the reviewer their
    ability to review.

    Close the scope explicitly before you finish. State, per item id, done or
    not-done with the reason, and confirm that the diff touches nothing outside
    the declared paths. Silent expansion and silent omission are the same
    failure: in both cases the reviewer's picture of what happened is wrong,
    and in this loop the reviewer is the only defence there is.
```

## §5 `spec/design/verify.py`

**位置**: `spec/design/verify.py`。**起動**: `python spec/design/verify.py [--repo-root PATH] [--json] [--pin-only] [--no-fetch]`。

**ネットワーク**: §3 手順 8 の否定側で `git fetch origin +refs/heads/main:refs/remotes/origin/main` を最大 1 回だけ実行する。それ以外のネットワークアクセスは無い。`--no-fetch` を付けた場合は fetch を試みず、否定側は `NO-PIN(FETCH_UNAVAILABLE)` として報告する（決定的な診断実行用）。

### 入力
- repo root（既定: `git rev-parse --show-toplevel`）
- `spec/design/*.md` の全 manifest
- `.mindwire/pin`（存在すれば）
- `spec/process/obligations.yaml`
- git: 現在ブランチ、`origin/main`

### 検出ロジック

| id | 検査 | level |
|---|---|---|
| V-1 | front-matter が存在し YAML としてパースでき、必須キー（`spec_id` `thread` `target_repo` `base_branch` `status` `canary` `supersedes` `obligations` `items`）が型どおり揃う | error |
| V-2 | ファイル名が `<thread>.md` と一致。`spec_id` が全 manifest 間で一意 | error |
| V-3 | `status` ∈ {`active`, `withdrawn`}、`canary` ∈ {`required`, `not-applicable`}（D-18: `proposed` / `superseded` は不正値） | error |
| V-4 | `supersedes` の各要素が実在の `spec_id` を指し、自分自身を指していない（D-20 により相手側 `status` は検査しない） | error |
| V-5 | `items`: `id` が manifest 内で一意、`id`/`title`/`paths` が存在する。item 側 `status` があれば `active` \| `withdrawn` のいずれかであり、`withdrawn` なら非空の `withdrawn_reason` を伴う（依存グラフ・循環の検査は行わない — D-15） | error |
| V-6 | 継承解決（§6）後、各 item が `target_repo`/`base_branch`/`canary` を持つ。暗黙の大域既定値は無く、解決できない欠落は error | error |
| V-7 | 各 item の有効 obligation 集合を **root ∪ item** として解決し、出力に含める（D-16。item 側は追加のみ ∴ 欠落検査は不要） | info |
| V-8 | 解決された obligation id が `spec/process/obligations.yaml` に実在する | error |
| V-9 | pin 解決（§3 手順 1〜11）を実行し、`RESOLVED` または `NO-PIN(<reason>)` を報告する。`NO-PIN` 自体は error ではない | info |
| V-10 | 現在ブランチが `main` のとき、`status: withdrawn` の manifest、または他 manifest の `supersedes` に載っている（＝派生 superseded、D-20）manifest を報告する。**新たな pin の対象にしてはならない文書である**ことの注意喚起であり、gate ではない | **warning**（D-10 / D-21。exit code に影響させない） |
| V-11 | manifest 直下の `target_repo` が `git remote get-url origin` の basename（末尾 `.git` を除く）と一致する（D-22）。`origin` remote が存在しない作業コピーでは本検査を行わず info を 1 件出す（診断ツールは環境で落ちない — D-10） | error |
| V-12 | 上書き不可フィールド（`spec_id` / `thread` / `supersedes` / `target_repo`）が item 側に現れない（§6） | error |

### 出力
- 既定: 1 行 1 件、`LEVEL CHECK TARGET: message`（例: `ERROR V-8 SPEC-2026-08-11-design-spec-delivery I-3: unknown obligation OBL-SPEC-TYPO`）
- `--json`: 単一 JSON オブジェクト
  `{"errors": [...], "warnings": [...], "items": [{"id": ..., "obligations": [...resolved union...]}], "pin": {"state": "RESOLVED"|"NO-PIN", "reason": <code|null>, "spec_id": ..., "blob_sha": ...}}`
- `--pin-only`: V-9 のみ実行

### exit code
- `0` — error 0 件（warning の有無は問わない）
- `1` — error 1 件以上
- `2` — 使用法／環境エラー（git repo でない等）

## §6 `items` 継承規則

- **継承される（item 側で省略可）**: `target_repo` / `base_branch` / `canary`。省略時は manifest 直下の値をそのまま採る。
- **item 必須（継承されない）**: `id` / `title` / `paths`。
- **上書き可**: `base_branch` / `canary`。item 側に書けばその item にのみ適用される。**`target_repo` は上書きできない**（D-22: spec はそれが置かれている repo に対してのみ authoritative であり、pin は repo 境界を越えられない）。他 repo を対象とする item は本 manifest では表現せず、対象 repo 自身の spec として発行する。
- **上書き不可（manifest 直下のみ・item 側に書けば error — V-12）**: `spec_id` / `thread` / `supersedes` / `target_repo`。
- **item 側 `status`（任意）**: manifest 直下の `status` の上書きではなく、**item 固有の独立フィールド**である。enum は文書側と同じ `active` / `withdrawn` で、既定は `active`。`withdrawn` の item は mandate から外れ、dispatch 対象にならず、実行順（D-15）から飛ばされる。`withdrawn` の item は `withdrawn_reason`（非空文字列）を必須とする（V-5）。
- **`obligations` は union**（D-16）: item の有効集合 ＝ **manifest 直下 ∪ item 直下**。item 側の記述は追加のみで、削除は表現できない。item 側の全列挙は要求しない。解決後の集合は `verify.py --json` の `items[].obligations` に出る ∴ 有効集合を知るのに手作業の再構成は要らない。
- **順序**: `items` の列挙順が実行順である（D-15）。依存フィールドは持たない。
- **暗黙の大域既定値を持たない。** manifest 直下にも item 側にも無い継承対象フィールドは error であり、実装が「妥当そうな値」で埋めてはならない（D-6 と同じ fail-closed 方針）。

## §7 受け入れ条件

- **A-1** `spec/process/obligations.yaml` に `OBL-SPEC-PIN` / `OBL-SPEC-RECEIPT` / `OBL-SPEC-SCOPE-CLOSURE` の 3 件が存在し、いずれも `origin` ブロックを持たない。
- **A-2** `OBL-SPEC-PIN` の body に §4-1 の `Never delete ...` 段落が逐語で含まれる。
- **A-3** `OBL-DECLARE-UNREADABLE` entry が、本 spec 由来の変更で 1 バイトも変わっていない（`origin.original_length` の検査が緑のまま）。
- **A-4** `.mindwire/pin` が無い状態で `verify.py` を実行すると `pin: NO-PIN(ABSENT)` を報告し、exit code は 0（他に error が無い場合）。
- **A-5** detached HEAD（`git checkout <sha>`）の状態で有効な pin を置いて `verify.py --pin-only` を実行すると、例外を送出せず `NO-PIN(DETACHED_HEAD)` を報告する。
- **A-6** pin の `branch` を現在ブランチと異なる値にすると `NO-PIN(BRANCH_MISMATCH)` を報告する。pin の内容が他の点で正しくてもよい。
- **A-7** pin の `blob_sha` を 1 文字変えると `NO-PIN(SHA_MISMATCH)` を報告する。
- **A-8** すべて正しい pin では `RESOLVED` を報告し、`spec_id` と `blob_sha` を出力に含む。到達可能な commit に対して fetch を実行しない（肯定側はネットワーク不要 — D-14）。
- **A-9** `origin/main` が古い状態で正しい pin を置くと、1 回の fetch を経て `RESOLVED` になる。`--no-fetch` を付けた同じ状況では `NO-PIN(FETCH_UNAVAILABLE)` を報告する（U-1 は E-12 で解消済 ∴ 観測義務は消費された。`FETCH_UNAVAILABLE` は `--no-fetch` と実ネットワーク障害の 2 経路で到達する）。
- **A-10** `main` に merge されていない commit を指す pin では、fetch 後も `NO-PIN(COMMIT_UNREACHABLE)` を報告する。A-9 の `FETCH_UNAVAILABLE` と同一コードに畳まれていない。
- **A-11** item に obligation を追加した manifest に対し、`verify.py --json` の `items[].obligations` が root ∪ item を出力する。item 側から root の obligation を削除する手段がスキーマ上存在しない。
- **A-12** **I-2 が `main` に merge された直後の `main` 上で `verify.py` を実行すると、error 0・warning 0 である**（D-21: 恒久 warning を出さない）。
- **A-13** `spirrow-mindwire` の `.gitignore` に `.mindwire/` が入っており、`.mindwire/pin` を置いた状態で `git status --porcelain` にそれが現れない（I-4）。**本条件は本 repo に閉じる** — 他 repo での成立を本仕様の受け入れ条件にしない（D-5 / D-22。それを担っていた I-5 は withdrawn — D-23）。
- **A-14** 本 manifest が `spec/design/T-design-spec-delivery.md` として存在し、`verify.py` の V-1〜V-8 および V-11 / V-12 を error 0 で通過する（自己適用）。
- **A-15** §1.1 の ADR 参照（番号・表題・内容）が実在と一致することを確認し、結果を PR 本文に書く。**一致しない場合は修正せず停止して報告する**（D-17）。
- **A-16** `after` に相当する依存フィールドが front-matter スキーマにも `verify.py` にも存在しない（D-15）。
- **A-17** 本仕様の実装差分のどこにも `gh pr merge` を implementer に実行させる記述・コード・手順が無い（D-11 / E-8）。
- **A-18** 実装 PR で canary が緑である（`canary: required`）。**`canary` の値が持つ効果は本条件の適用可否だけである** — `not-applicable` の item は本条件の対象外になり、それ以外にこの値を読む consumer は本仕様に無い。実体の canary は obligations loader が `origin` ブロックを持つ entry に対して常に強制するものであり（E-9）、front-matter の値では止まらないし始まらない。
- **A-19** `status: withdrawn` の manifest、または他 manifest から `supersedes` されている manifest を `main` 上に置くと V-10 が warning を出し、exit code は 0 のままである。
- **A-20** front-matter に `status: proposed` または `status: superseded` を書くと V-3 が error を出す（D-18 の回帰防止）。
- **A-21** `main` に merge 済みの manifest の本文が、chatroom に投稿された msg 本文と byte 単位で一致する（D-2 手順 3・D-19）。
- **A-22** item に `status: withdrawn` ＋ `withdrawn_reason` を書いた manifest が V-1〜V-8 を error 0 で通過し、`verify.py --json` の当該 item がその状態を出力する。`withdrawn_reason` を落とすと V-5 が error を出す。
- **A-23** `target_repo` を実在の remote basename と異なる値にすると V-11 が error を出す。`origin` remote を持たない作業コピーでは V-11 が error を出さず info を 1 件出し、exit code は 0 のままである。
- **A-24** item 側に `target_repo` を書くと V-12 が error を出す（D-22 の回帰防止）。
- **A-25** 本 manifest の**本文（決定・スキーマ・受け入れ条件）および front-matter（`items` の `id` / `title` / `paths` / `withdrawn_reason` を含む全フィールド）**のいずれにも、`spirrow-mindwire` 以外の repo に対して作業・状態変更を**要求する**記述が無い（D-22）。**front-matter を明示的に含めるのは、dispatch されるのが item だからである** — cross-repo mandate を機械が実際に運べる唯一の面がそこであり、I-5 が現にそうだった（I-4 と I-5 は `paths` が同一で、両者を分けていたのは `title` の散文だけである）。V-11 / V-12 は manifest 直下の `target_repo` しか見ず、item の散文は素通りする。実測の記述（E-6 / E-10）と、withdrawn item がなぜ消えたかの記録（I-5 / D-23）は要求ではない ∴ 本条件に反しない。散文でこの規律が破れることは機械では検出できない — 本条件はその穴を人手の検査で塞ぐものであり、A-15 / A-17 と同じ種類の受け入れ条件である。

## §8 運用（順序の SOT は `items` の列挙順 — D-15）

本節は item でない段のみを述べる。

- **I-1 と I-2 は `NO-PIN` ターンである**（D-12）。pin 機構がまだ存在しないため、payload は D-2 経路で運ばれた本文書の本文そのものであり、receipt は `NO-PIN` と書くのが正しい。
- **I-2 の merge が Tier-C である。** human は本文を一字も変えずに merge する（D-2 手順 3）。merge した時点で本 manifest は `origin/main` 上に存在し、pin から参照可能になる。**merge 前後で `status` を書き換えない**（D-18: merge 済みか否かは git が知っている）。
- **merge 後、本 manifest は immutable である**（D-19）。訂正・改訂は新しい spec を起こし `supersedes` で繋ぐ。古い側のファイルは書き換えない（D-20）。
- **I-3 以降は pin つき dispatch で実行する。** ここから receipt は `RESOLVED` になる。
- 各 item の PR は `feature/*` → 当該 item の `base_branch`（E-7）。**merge は常に human（Tier-C）**であり、E-6 の ruleset は PR を経由することまでしか強制しない（主体が human であることは強制しない）∴ 規律である（D-11）。
- pin つき dispatch の第一号実運用対象は **`T-pr-gate-adr-index-scope`**（D-13）。
