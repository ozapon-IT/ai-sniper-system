# Lambda デプロイパッケージ。build_lambda.sh が用意したディレクトリを zip 化する。
# 中身ルート: backend/ + config/ + 依存（site-packages）。ハンドラは backend.src.* で解決。
data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = var.lambda_build_dir
  output_path = "${path.module}/.build/${local.name}-lambda.zip"
}
