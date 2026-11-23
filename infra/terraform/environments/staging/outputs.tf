output "lambda_function_names" {
  value = module.app.lambda_function_names
}

output "dynamodb_table_name" {
  value = module.app.dynamodb_table_name
}

output "s3_log_bucket_name" {
  value = module.app.s3_log_bucket_name
}

output "schedule_names" {
  value = module.app.schedule_names
}

output "orb_function_names" {
  value = module.orb_demo.function_names
}

output "orb_sessions_table_name" {
  value = module.orb_demo.sessions_table_name
}

output "orb_schedule_names" {
  value = module.orb_demo.schedule_names
}

output "orb_schedule_group_name" {
  value = module.orb_demo.schedule_group_name
}
