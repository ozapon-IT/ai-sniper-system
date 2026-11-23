# tfstate remote backend（aws-bootstrap.md §2 で手動作成済み・pre-TF）。
# state 暗号化必須（Secret 値を含み得ないが方針として encrypt=true。AGENTS.md #8）。
# dual-lock: use_lockfile と dynamodb_table を同時指定（#347 Phase B）。
# 出典: https://developer.hashicorp.com/terraform/language/backend/s3
# （Issue 参照日 2026-09-03 / 再確認 2026-09-05）
# 公開版: bucket / dynamodb_table（your-*）は自分の環境のリソース名に置き換えてから init する。
terraform {
  backend "s3" {
    bucket         = "your-tfstate-bucket"
    key            = "staging/terraform.tfstate"
    region         = "ap-northeast-1"
    dynamodb_table = "your-tflock-table"
    encrypt        = true
    use_lockfile   = true
  }
}
