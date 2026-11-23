"""ローカルバックテスト（BT シリーズ, docs/ai-dev/backtest-design.md）。

Lambda デプロイ物には含めないローカル専用ツール群。
Public API の GET とローカルキャッシュのみで完結し、発注系コード
（batch / private POST）を import しない（AGENTS.md ガードレール #1 への構造的適合）。
"""
