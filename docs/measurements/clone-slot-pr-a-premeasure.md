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

## このコミット時点で未測定（PR-A の中で追記する）
- trust の登録先と親ディレクトリの trust が子に効くか。
- slot 内で `.mindwire-gate` / ローカル gate が動くか。
- voxelworld の作り直し 1 回あたりのビルド時間。
