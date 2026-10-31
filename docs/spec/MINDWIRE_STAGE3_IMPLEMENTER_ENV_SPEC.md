# MindWire Stage 3 — Implementer Environment Spec

> **実インフラ値**（ホスト名 / IP / パス）は [[platform:infra-registry]] が正本。この文書は `{{PLACEHOLDER}}` で参照する（規約 §3.1）。

- **Status**: Draft (local develop). Drive 反映は Takahito GO 後 (Tier C)。
- **Author**: main (claude.ai). Materialized by claude-code from the ADR-07 §5 Open-Q1 resolution.
- **Resolves**: ADR-2026-05-23-07 §2.4 / §5 Q1

## 1. Purpose
implementer は EXECUTE_CODE を全開放するため、blast radius を loop のゲートではなく**環境レベルで物理封じ込め**する。本 spec はその独立 PC の封じ込め設定を規定する。Tier A（EXECUTE_CODE 全開放）の前提条件。

## 2. Tailscale ACL (Layer 1)
- `tag:mindwire-implementer` ノードからの到達先は **{{HOST_SERVICES}}:tcp:8110 (Lexora) のみ許可**。grants 構文にマージ。
- 定義時点で未記載 = default-deny ゆえ Vaultwarden / SSH / 他ノードは到達不可。
- `autogroup:member` は従来どおり全許可（人の運用は維持）。

## 3. Egress firewall (Layer 2)
- Windows Firewall **default-block (outbound)** + 許可4本のみ:
  1. Tailnet 100.64.0.0/10
  2. tailscaled.exe (★必須: 無いと Tailscale が死ぬ)
  3. Squid (allow-list 型 egress proxy)
  4. DNS
- Squid allow-list = **GitHub + package registry のみ**。**api.anthropic.com は明示 DENY**。
- git は HTTPS:443 token 認証に統一、:22 は閉じる。
- dispatch 方向は **pull 既定**（inbound ルール不要）。

## 4. Credentials
- GitHub token = mindwire repo scoped fine-grained PAT（Contents R/W + PR R/W）。
- Vaultwarden アクセスを環境に置かない。長期クレデンシャルを implementer 環境に保持しない。
- 同一機械で owner と turn が同居する構成（ADR-2026-10-09-23 D9）では、上の 2 点は **Windows ユーザーの分離**で担保する。turn は `[turn.isolation] user` で動き、owner の Credential Manager（`mindwire/vaultwarden` / `mindwire/turn-user` / `mindwire/implementer-pat`）を読めない。turn の環境に入るのは S5 で `turn` と宣言された名前だけ（2a では `GH_TOKEN`）。§5 の確認方法はこれに合わせてある。
- **Anthropic API キーは implementer PC に持たせない**。推論は Lexora 経由（Lexora が cloud Claude へルーティング、キーは {{HOST_SERVICES}} のみ保持）。

## 5. Deny smoke test (EXECUTE_CODE 解放の前提)
implementer から以下を実証:
- ✓ Lexora (:8110, tailnet 直) 到達
- ✓ GitHub (Squid proxy 経由) 到達
- ✗ api.anthropic.com (Squid deny)
- ✗ 直 outbound (firewall default-block)
- ✗ SSH:22 (Tailscale ACL 粒度で遮断)
- ✗ Vaultwarden — **turn user として** 次の 2 つが失敗すること（ネットワーク到達性では判定しない）:
  1. turn user による `CredRead("mindwire/vaultwarden")` が失敗する（`ERROR_NOT_FOUND`）。
  2. turn user による `bw unlock` が、資格情報を持たないため失敗する。

**Vaultwarden 行をネットワーク遮断から資格情報の不在に置き換えた理由**（ADR-2026-10-09-23 D9 / T-executor-pool-under-one-conductor msg-7091 S7・S8）:
D9 で実行機は起動時に自分で Vaultwarden から秘密を取得する。turn と owner（tick wrapper / conductor / naysayer）は同じ機械のネットワークスタックを共有するので、Vaultwarden:443 への遮断を turn user だけに効かせることはできない。そこで境界をネットワークから **Windows ユーザー**に移す: turn は専用の低権限ローカルアカウント（`[turn.isolation] user`、例 `mindwire-turn`）で動き、Vaultwarden の bootstrap 鍵と unlock パスワードは owner の Credential Manager（ユーザー単位）にだけ置く。turn user はそれを読めないので `bw unlock` もできない。§4 の意図（implementer 環境に Vaultwarden アクセスや長期クレデンシャルを置かない）は変えず、その実証方法だけを変える。turn user は自分の `CredRead` と `bw unlock` の失敗によって、Vaultwarden に到達できないことが示される。

この 2 つの確認は人手で行う（turn user でログオンする必要があり、CI では実行できない）。自動の起動時確認は `mindwire turn-isolation-check`（S8: turn user の資格情報の有無と、各 clone に対する git / 書き込みプローブ）で、別物である。

## 6. Windows 固有の知見 (implementer 増設時に再利用)
- ★ tailscaled.exe の outbound 許可必須。
- ★ NO_PROXY に tailnet を入れる（さもないと Lexora 行きが Squid に流れ deny → 推論死）。
- ★ curl/git の schannel が OCSP/CRL 失効チェックで CRYPT_E_REVOCATION_OFFLINE → `git config http.schannelCheckRevoke false` / `curl --ssl-no-revoke`。Python agent (OpenSSL) は無関係。
- Lexora は tailnet 到達可能アドレスで listen（localhost 単独 bind 不可）、サーバ firewall で tailscale0+loopback に絞る。

## 7. Token スコープ方針
- bring-up 中: mindwire repo のみ。
- 本格稼働マイルストーン: fine-grained all-repos（Contents R/W + PR R/W のみ）+ org branch protection（全 main に Takahito レビュー必須）に拡大。
