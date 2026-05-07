# Spirrow MindWire

AI エージェント間通信ハブ。 Claude.ai と Claude Code 間のチャットのやり取りをファイルベース I/O で自動中継する独立 MCP サーバ。

## ステータス

- **Phase 0 (Claude Code 側 I/O 自動化)** 設計中
- T02 アーキテクチャ設計 完了 → [`docs/architecture.md`](docs/architecture.md)
- 実装言語未確定 (Python 第一候補 / TypeScript 候補、 T05 で確定)

## 命名の由来

Telegraph (電信) を AI 文脈に再構築。 Mind (思考) + Wire (線で繋ぐ) の合成。 Spirrow Platform の命名規則 (spirrow-* シリーズ) に整合。

## 設計原則

MindWire は **AI エージェント同士の通信ハブ** に世界観を閉じる。 外部システム連携は **独立 Connector 層** (= MCP サーバレベル) が担い、 MindWire コアは外部システムの存在を一切知らない。 詳細は [`docs/architecture.md`](docs/architecture.md) 参照。

## ライセンス

未定。 公開タイミングで決定。
