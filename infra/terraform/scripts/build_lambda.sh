#!/usr/bin/env bash
# Lambda デプロイバンドルを構築する。
# 出力: infra/terraform/build/pkg（中身ルートに backend/ + config/ + 依存）。
# archive_file（package.tf）がこのディレクトリを zip 化する。
#
# インポートは repo ルート絶対（from backend.src... / from config...）。
# よって backend/ と config/ を zip ルート直下に置き、ハンドラを backend.src.* で解決する。
#
# 依存は Lambda 実行環境（Amazon Linux / x86_64 / py3.11）向けに manylinux wheel を取得。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"           # infra/terraform
REPO_ROOT="$(cd "${TF_DIR}/../.." && pwd)"          # repo ルート
BUILD_DIR="${TF_DIR}/build/pkg"
REQ_FILE="${TF_DIR}/lambda-requirements.txt"
CONSTRAINTS_FILE="${TF_DIR}/lambda-constraints.txt"

PY_VERSION="${LAMBDA_PY_VERSION:-3.11}"
PLATFORM="${LAMBDA_PLATFORM:-manylinux2014_x86_64}"

echo "[build_lambda] repo=${REPO_ROOT}"
echo "[build_lambda] out=${BUILD_DIR} (py${PY_VERSION}/${PLATFORM})"

rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"

# 1) 依存を取得（Lambda ランタイム向けバイナリ wheel）。
#    再現性のため exact pin（requirements）＋ 完全 pin（constraints）。--upgrade はしない。
python3 -m pip install \
  --requirement "${REQ_FILE}" \
  --constraint "${CONSTRAINTS_FILE}" \
  --target "${BUILD_DIR}" \
  --platform "${PLATFORM}" \
  --python-version "${PY_VERSION}" \
  --implementation cp \
  --only-binary=:all:

# 2) アプリコードを同梱（backend/ と config/）。
cp -R "${REPO_ROOT}/backend" "${BUILD_DIR}/backend"
cp -R "${REPO_ROOT}/config" "${BUILD_DIR}/config"

# 3) 不要物を除去（テスト・キャッシュ・dist-info の肥大化対策）。
find "${BUILD_DIR}" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "${BUILD_DIR}" -type d -name "tests" -prune -exec rm -rf {} +
find "${BUILD_DIR}" -type f -name "*.pyc" -delete

echo "[build_lambda] done. size:"
du -sh "${BUILD_DIR}"
