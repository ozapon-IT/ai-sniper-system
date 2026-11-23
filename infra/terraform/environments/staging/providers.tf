# 認証は IAM Identity Center（SSO）。AWS_PROFILE=your-sso-profile を環境で指定（aws-bootstrap.md §1）。
# 長期アクセスキーは使わない。
provider "aws" {
  region = var.region
}
