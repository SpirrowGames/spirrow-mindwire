# Spirrow MindWire

AI エージェント間通信ハブ。 Claude.ai と Claude Code 間のチャットのやり取りをファイルベース I/O で自動中継する独立 MCP サーバ。

## ステータス

- **Phase 0 (Claude Code 側 I/O 自動化)** 実装中
- 設計フェーズ完了:
  - T02 アーキテクチャ → [`docs/architecture.md`](docs/architecture.md)
  - T03 MCP インターフェース → [`docs/mcp-interface.md`](docs/mcp-interface.md)
  - T04 ログ機能 → [`docs/logging-design.md`](docs/logging-design.md)
- 実装言語: **Python 3.11+** (uv ベース)
- 進行中: T05 (scaffolding) → T06 (watcher) / T07 (MCP server) → T08 (E2E test)

## Development

### 必要環境
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (パッケージ管理)

### セットアップ

```bash
# 依存解決 + venv 作成
uv sync --extra dev

# テスト
uv run pytest

# Lint
uv run ruff check
uv run ruff format --check

# 型チェック
uv run mypy src tests
```

### CLI エントリポイント (実装中)
- `mindwire-watcher`: ファイル監視 daemon (T06 で実装)
- `mindwire-mcp`: read-only MCP server (T07 で実装)
- `mindwire`: 運用 CLI (将来)

## 命名の由来

Telegraph (電信) を AI 文脈に再構築。 Mind (思考) + Wire (線で繋ぐ) の合成。 Spirrow Platform の命名規則 (spirrow-* シリーズ) に整合。

## 設計原則

MindWire は **AI エージェント同士の通信ハブ** に世界観を閉じる。 外部システム連携は **独立 Connector 層** (= MCP サーバレベル) が担い、 MindWire コアは外部システムの存在を一切知らない。 詳細は [`docs/architecture.md`](docs/architecture.md) 参照。

## ライセンス

未定。 公開タイミングで決定。
