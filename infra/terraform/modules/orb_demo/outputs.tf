output "function_names" {
  description = "ORB デモの Lambda 関数名。"
  value       = { for k, fn in aws_lambda_function.this : k => fn.function_name }
}

output "sessions_table_name" {
  description = "セッション状態テーブル名。"
  value       = aws_dynamodb_table.orb_sessions.name
}

output "schedule_names" {
  description = "EventBridge Scheduler のスケジュール名。"
  value       = { for k, s in aws_scheduler_schedule.orb : k => s.name }
}

output "schedule_group_name" {
  description = "ORB 専用の EventBridge Scheduler グループ名（夜型 default とは別）。"
  value       = aws_scheduler_schedule_group.orb.name
}

output "schedule_timezone" {
  description = "スケジュールのタイムゾーン（AGENTS.md 規約6 の例外であることを可視化）。"
  value       = "Europe/London"
}
