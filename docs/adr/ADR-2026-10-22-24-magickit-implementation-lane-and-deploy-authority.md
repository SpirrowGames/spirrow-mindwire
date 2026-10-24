# ADR-2026-10-22-24 Magickit の実装レーンと本番 deploy 権限（ADR-2026-05-27-09 D-6 amendment）

> **実インフラ値**（ホスト名 / IP / パス）は [[platform:infra-registry]] が正本。この文書は `{{PLACEHOLDER}}` で参照する（規約 §3.1）。決定の原文（Fermi msg-7370）にある Magickit 本番ホストの実名は、この規約に従って `{{HOST_SERVICES}}` に置き換えている（ADR-2026-06-04-18 が Magickit の稼働ホストとして参照しているものと同じプレースホルダ）。

- **Status**: Accepted（Takahito 2026-10-22。Fermi `T-adr09-d6-magickit-lane-amendment` msg-7370 による裁定の転記）
- **Date**: 2026-10-22
- **Amends**: ADR-2026-05-27-09 D-6（D-6 のみ。D-1〜D-5 は変えない）
- **Scope**: Magickit の変更を誰が実装してよいか、および Magickit 本番 deploy を誰の指示で行うか。
- **Author**: Heisenberg (implementer) — 起票 chatroom thread `T-adr09-d6-magickit-lane-amendment`（作業仕様: Bohr msg-7392 / msg-7394 / msg-7396 / msg-7398、設計レビュー: Einstein msg-7393 / msg-7395 / msg-7397 と、その後の go（ADR-12 を D-6' -4 に加える助言 1 件を反映））
- **改訂種別**: 既存 ADR の決定 1 件（ADR-09 D-6）の改訂。新しい番号を持つ独立の ADR として起こす（ファイル名に `-amendment-` を含めないのは、`adr_index_gen` が `-amendment-` を含むファイルを索引の生成から外すため）。

---

## 1. 背景

msg-7370 の背景を写す。

- ADR-2026-05-27-09（`T-T28-author-role-identity` msg-283）D-6 は「Magickit 拡張は Magickit team 案件、claude-code lane では実装不可」と定めている（2026-05 時点の運用）。
- 実態（2026-09 以降）: spirrow-magickit project の三者ループ（Bohr / Heisenberg / Einstein）が magickit の PR を実装している。直近では #109（2e-2b-1、PR-gate APPROVE、main `2ad86bf`）。
- このずれのため、`T-agmsg-transport-lessons-readiness-session-claim-board` msg-7219 で operator が T45 PR2（`session_declare`）を「Magickit team への依頼」として `spirrow-magickit/T-T45-pr2-magickit-session-declare-request` に `NEXT: none` で駐機させ、T45 全体（PR1 → PR2 → PR3）が止まっている。
- Takahito 確認（2026-10-22）: **D-6 の本来の意図は「Magickit の本番 deploy は Takahito の指示以外では行わない」であって、実装主体の制限ではない。**

## 2. 決定

### D-6' -1 実装主体

Magickit の変更も他のプロダクトと同じく、spirrow-magickit project の三者ループで実装してよい。ADR-09 D-6 の「claude-code lane では実装不可」は撤廃する。

### D-6' -2 本番 deploy の権限

Magickit の本番 deploy（`{{HOST_SERVICES}}` の `spirrow-magickit{,-mcp,-mcp-local}.service`）は、Takahito の明示的な指示があるときにだけ行う。ループも operator も、自律では deploy しない。

### D-6' -3 deploy 待ちの表現

本番確認が merge 後の Magickit 本番 deploy に依存するスレッドは、返信の**末尾 2 行**を次のとおりにして駐機する。`STOP:` 行を `NEXT: none` の直上に置き、`NEXT: none` を返信の最終行にする。

```
STOP: blocked-on deploy:<request_id> wake:<persona>
NEXT: none
```

- `<request_id>`: その deploy の request_id。対象 head は deploy request の側に記録する。（msg-7370 の原文は `deploy:<magickit の対象 head>` だが、protocol の trigger 文法は `deploy:<request_id>` なので、意味を変えずに表記を文法に合わせた。Bohr msg-7392）
- `<persona>`: そのスレッドで proposer または naysayer を担う persona。**implementer は指定できない**。原則として proposer にする。wake で起きた proposer が本番での挙動を確認し、次の担当に渡す。
- deploy request がまだない段階では、`<request_id>` が決まらないため駐機できない。その場合は deploy の指示を Takahito に依頼する（`NEXT: human`、TIER-C: irreversible。本番に外部副作用が出るため）。operator は Takahito の指示を受けてから deploy request を作成・実行し、request_id・head・時刻・再起動したサービスをスレッドに残す。スレッドはその request_id を使い、上の 2 行で駐機する。
- 前例: `T-decider-decision-page-jev` msg-1346（この流れを規則にしたのが本項）。

### D-6' -4 変えないもの

- ADR-09 の D-1〜D-5 は変えない。
- ADR-09 に対する既存の改訂・拡張は変えない: ADR-2026-05-29-12（D-5 の拡張・実装）、ADR-2026-05-31-15、ADR-2026-08-25-20、ADR-2026-09-14-21。

## 3. 影響

- `spirrow-magickit/T-T45-pr2-magickit-session-declare-request` は「Magickit team への依頼」ではなく、通常の実装スレッドになる（PR1 の merge 後に着手）。merge 後の報告は proposer が行う（msg-7370「Bohr に依頼すること」）。
- deploy の権限（D-6' -2）は実装レーンと切り離した。Magickit の PR を merge しても、本番に反映されるのは Takahito の指示による deploy の後である。
