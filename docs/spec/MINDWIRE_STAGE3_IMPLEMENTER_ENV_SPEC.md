# MindWire Stage 3 — Implementer Environment Spec

- **Status**: Draft (local develop). Drive 反映は Takahito GO 後 (Tier C)。
- **Author**: main (claude.ai). Materialized by claude-code from the ADR-07 §5 Open-Q1 resolution.
- **Resolves**: ADR-2026-05-23-07 §2.4 / §5 Q1

## 1. Purpose
implementer は EXECUTE_CODE を全開放するため、blast radius を loop のゲートではなく**環境レベルで物理封じ込め**する。本 spec はその独立 PC の封じ込め設定を規定する。Tier A（EXECUTE_CODE 全開放）の前提条件。

## 2. Tailscale ACL (Layer 1)
- `tag:mindwire-implementer` ノードからの到達先は **sg-ai-server-01:tcp:8110 (Lexora) のみ許可**。grants 構文にマージ。
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
- **Anthropic API キーは implementer PC に持たせない**。推論は Lexora 経由（Lexora が cloud Claude へルーティング、キーは sg-ai-server-01 のみ保持）。

## 5. Deny smoke test (EXECUTE_CODE 解放の前提)
implementer から以下を実証:
- ✓ Lexora (:8110, tailnet 直) 到達
- ✓ GitHub (Squid proxy 経由) 到達
- ✗ api.anthropic.com (Squid deny)
- ✗ 直 outbound (firewall default-block)
- ✗ Vaultwarden:443 / SSH:22 (Tailscale ACL 粒度で遮断)

## 6. Windows 固有の知見 (implementer 増設時に再利用)
- ★ tailscaled.exe の outbound 許可必須。
- ★ NO_PROXY に tailnet を入れる（さもないと Lexora 行きが Squid に流れ deny → 推論死）。
- ★ curl/git の schannel が OCSP/CRL 失効チェックで CRYPT_E_REVOCATION_OFFLINE → `git config http.schannelCheckRevoke false` / `curl --ssl-no-revoke`。Python agent (OpenSSL) は無関係。
- Lexora は tailnet 到達可能アドレスで listen（localhost 単独 bind 不可）、サーバ firewall で tailscale0+loopback に絞る。

## 7. Token スコープ方針
- bring-up 中: mindwire repo のみ。
- 本格稼働マイルストーン: fine-grained all-repos（Contents R/W + PR R/W のみ）+ org branch protection（全 main に Takahito レビュー必須）に拡大。
