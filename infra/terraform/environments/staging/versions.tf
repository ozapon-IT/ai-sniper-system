terraform {
  # ルートは予測可能な更新のため上下限を置く（#347 Phase A）。
  # ~> 1.15.7 は >= 1.15.7, < 1.16.0 と同義。brew 直実行の 1.16+ を fail-closed する。
  # 出典: https://developer.hashicorp.com/terraform/language/expressions/version-constraints
  # （参照日 2026-09-05）
  required_version = "~> 1.15.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}
