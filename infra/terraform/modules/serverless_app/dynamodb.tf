# アプリ用テーブル（冪等ロック / orderId / サマリ）。§1.5。
# キースキーマは dynamodb_store.py の _item_key と一致:
#   パーティションキー date(S) + ソートキー job_type(S)。
resource "aws_dynamodb_table" "app" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "date"
  range_key    = "job_type"

  attribute {
    name = "date"
    type = "S"
  }

  attribute {
    name = "job_type"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = local.common_tags
}
