# ドキュメント地図

AI Sniper System のドキュメントの入口です。仕様の唯一の正は [system_requirements.md](system_requirements.md)（Ver.3.7）です。

> 公開版では、運用手順（`runbook/`）・開発環境メモ（`ai-dev/`）・API 実測記録（`spikes/`）・税務記録（`tax/`）を除いています。
> 各ドキュメント内に残るこれらへのリンクは非公開側を指しています。理由はトップの [README](../README.md#公開版について) を参照してください。

## 仕様・設計

| ドキュメント | 内容 |
|---|---|
| [system_requirements.md](system_requirements.md) | **仕様の唯一の正**（Ver.3.7）。実装判断は該当 § を参照する |
| [architecture.md](architecture.md) | 完全サーバーレス構成の俯瞰図（Mermaid）・スケジュール |
| [features.md](features.md) | 実装済み機能のカテゴリ別カタログ |
| [spikes/README.md](spikes/README.md) | API スパイク記録（付録B）の概要 |

## ルール・ガードレール

| ドキュメント | 内容 |
|---|---|
| [../AGENTS.md](../AGENTS.md) | プロジェクト最上位ルール・安全ガードレール（ツール非依存） |
| [../CLAUDE.md](../CLAUDE.md) | Claude Code 用メモリ（AGENTS.md を取り込む） |
| [../.claude/rules/gmo-api.md](../.claude/rules/gmo-api.md) | GMO FX API 実装ルール（署名パス非対称・レートリミット・エンドポイント） |
| [../.claude/rules/trading-safety.md](../.claude/rules/trading-safety.md) | トレード安全ルール（two-key・発注前バリデーション・キャンセルファースト） |

## インフラ

| ドキュメント | 内容 |
|---|---|
| [../infra/terraform/README.md](../infra/terraform/README.md) | Terraform IaC（Lambda・EventBridge Scheduler・S3・DynamoDB・Secrets） |
