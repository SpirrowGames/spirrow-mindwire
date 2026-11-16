# Clone slot PR-A — 着手前の実測 (T-ephemeral-impl-clone-slot-per-thread)

確定仕様 msg-8061「着手前の実測（PR-A の最初のコミットで記録する）」の記録。
測定日 2026-11-07、ホスト = conductor の実行機（Windows 11, NTFS, Git for Windows）。

## 方法
- mirror は各 repo の既存のローカル impl clone から `git clone --bare` で作成した
  （本番の mirror は GitHub から作る。ここで測るのは「mirror → slot」のローカル clone の部分）。
- slot は `git clone <mirror> <slot>`（ローカル path なので objects はハードリンクされる想定）。
- 時間は Python `time.time()` で `git clone` 1 回を計測。

## 結果

| repo | mirror → slot の clone 秒 | slot サイズ (全体 / .git) | objects のハードリンク | LFS | 通常ターンの増分 (`git status --porcelain`) |
|---|---|---|---|---|---|
| mindwire | 0.44 s | 38 MB / 16 MB | 224/224 ファイル | なし | 0.16 s |
| magickit | 0.41 s | 13 MB / 9 MB | 953/953 ファイル | なし | 0.06 s |
| lexora | 0.31 s | 8 MB / 5 MB | 539/539 ファイル | なし | 0.03 s |
| voxelworld | 2.32 s | 26 MB / 8 MB | 0/3 (下記) | なし | 0.03 s |

- voxelworld の測定元 clone は shallow（`rev-parse --is-shallow-repository` = true）で、
  そこから作った bare は単一 pack になり、ハードリンクは効かなかった。GitHub から作る
  本番 mirror では値が変わる。仕様どおり voxelworld は `slots_root` 未設定のままにする。
- **launch budget に対する通常ターンの増分**: slot 方式で通常ターンに増えるのは、tick ごと・repo ごと
  1 回の mirror fetch（ネットワーク、タイムアウト 40 s、失敗しても park しない）と、slot の
  `fetch` + 検査（数百 ms）。作り直しは隔離時だけで、clone 自体は 4 repo とも 2.5 s 未満。

## slot プール（人の裁定 msg-9385 案 A、設計 msg-9386〜9395）の実装前確認 — 2026-11-17

Heisenberg が実装の前に行った確認（msg-9385 条件 1、msg-9386「実装の前に確かめること」、
msg-9391 submodule、msg-9395 Obj-35-1 の 3）。

1. **path に結び付いた、ターンをまたぐ状態（条件 1）:** `deploy/**/*.ps1` と `src/` を
   `--resume` / `--continue` / session id / `.claude/projects` / `.claude.json` / `CLAUDE_CONFIG_DIR`
   で検索した。
   - `claude --bg` の起動（`adapters/claude_bg.py` `_launch`）は毎ターン新しいセッションを
     `--name <thread>-r<round>-<nonce>` で起動し、resume / continue は使わない。
   - `.ps1` 側の該当は Summon の `--resume-notice-*`（mindwire 自身の CLI 引数で、Claude の
     session resume ではない）だけ。`~/.claude/projects/<path>` を読む箇所は無い。
   - **path を使うのは 1 か所:** `ClaudeBgAdapter.reap_orphans` が、孤児セッションの掃除対象を
     `cwd == repo_dir` で絞っている。これは前のターンの状態を次のターンへ引き継ぐものではなく、
     掃除の範囲の絞り込み。slot 方式では範囲を「その repo の slot 全部」に広げる必要がある
     （A2/A3 で対応）。
2. **trust のキーの書き方:** loop host の実ファイル `~/.claude.json` の `projects` のキーを読み取り
   専用で確認した。すべて「ドライブレター大文字 + `/` 区切り」（例 `C:/workspace/spirrow/spirrow-mindwire`）。
   祖先の `C:/workspace` が trusted でも、その下の git root（`C:/workspace/sandbox/mindwire-impl`、
   および新しく作った git root）で `claude --bg` は `Workspace not trusted` で終了した
   （Claude Code 2.1.325）。小文字ドライブのキーは、確かめるには書き込みが要るので未測定。
   `is_trusted` は測定済みの形（大文字ドライブ）だけを受け付ける。
3. **kill-unconfirmed の終了確認:** `run-conductor-scheduled.ps1` / `ConductorBudget.ps1` の
   kill-unconfirmed（exit 7）は `Kill(entireProcessTree)` 後のプロセスの生存だけを見ており、
   `claude --bg` のセッション一覧は見ていない。
4. **Job Object（msg-9395 Obj-35-1 の 3）:**
   - PowerShell から名前付き Job を作り（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`、BREAKAWAY 不許可）、
     自分自身を入れること: **成功**（`SetInformationJobObject` / `AssignProcessToJobObject` とも True）。
   - `claude --bg` のセッション本体と子が同じ Job に入るか、wrapper 異常終了で孫まで終了するか:
     **確かめられなかった**。この実行機で trusted な path は本番 clone だけで、試験用の path は
     trust が無く `claude --bg` が起動前に終了した。trust を付けるには `.claude.json` への書き込みが
     要り、それは本設計で MindWire がしないこと。また `claude_bg.py` の記述どおり `--bg` は呼び出し元
     から detach する。
   - **結論:** msg-9395 の 3 により、どれか 1 つでも成り立たなければ 2 だけを実装する。
     wrapper 異常終了後の自動回収は実装せず、貸し出しを残して `job_containment_unverified` の
     notice を出す（A2/A3）。
5. **submodule・埋め込み repo（msg-9391 / msg-9393 Obj-34-2 の 4）:** GitHub の既定ブランチの
   tree を確認した。SpirrowGames/spirrow-mindwire・spirrow-magickit・spirrow-lexora・
   spirrow-voxelworld の 4 つとも `.gitmodules` が無く、mode 160000 の gitlink は 0（tree は
   truncated でない）。全 gitlink を確かめる判定は、それでも A2 に入れる。

## このコミット時点で未測定（PR-A の中で追記する）
- trust の登録先と親ディレクトリの trust が子に効くか。
- slot 内で `.mindwire-gate` / ローカル gate が動くか。
- voxelworld の作り直し 1 回あたりのビルド時間。
