"""監視アラート通知（Issue #105 / §7.1）。

CloudWatch Alarm → SNS → slack_notifier Lambda → Slack incoming webhook。
スケジューラ発火失敗（InvocationDroppedCount / TargetErrorCount）・Lambda Errors・
DLQ メッセージ滞留を検知して Slack へ通知し、手動スポット確認への依存を解消する。
"""
