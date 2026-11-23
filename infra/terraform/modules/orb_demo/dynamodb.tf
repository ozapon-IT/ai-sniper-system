# ORB デモのセッション状態テーブル。
# 🔴 夜型の当日ロック（`your-job-lock-table`）とは**別テーブル**（設計書 §6.1）。
# 既存ロック機構を流用すると、ORB の書き込みが夜型の冪等性判定に干渉しうる。

resource "aws_dynamodb_table" "orb_sessions" {
  name         = var.orb_sessions_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_date"

  attribute {
    name = "session_date"
    type = "S"
  }

  point_in_time_recovery {
    # デモの記録は再生成可能（S3 のイベント JSONL が一次記録）だが、
    # halt 状態と継続日数は復元できないので PITR を有効にしておく。
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = local.common_tags
}
