"""tests/infra/test_no_public_endpoints.py — 公開エンドポイント検出ガードのテスト（§1.6.1・#177）。

🔴 このテストが守る契約:

`read-api` / `kill-switch` は **アプリ層の認証を持たない**（Ver 3.7 で Bearer 廃止）。
認証境界は IAM のみであり、**「HTTP 公開しないこと」自体が唯一の防御**になっている。

したがって `check_no_public_endpoints.py` が壊れる＝防御が静かに無効化される。
「検出できること」だけでなく「**正常系で誤検知しないこと**」も固定する。誤検知で
CI が常に赤いと、いずれ無効化されるため。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_no_public_endpoints import find_violations

_FORBIDDEN_SNIPPETS = [
    ('aws_lambda_function_url', 'resource "aws_lambda_function_url" "ks" {\n  authorization_type = "NONE"\n}\n'),
    ('aws_apigatewayv2_api', 'resource "aws_apigatewayv2_api" "http" {\n  protocol_type = "HTTP"\n}\n'),
    ('aws_api_gateway_rest_api', 'resource "aws_api_gateway_rest_api" "rest" {\n  name = "x"\n}\n'),
    ('aws_cloudfront_distribution', 'resource "aws_cloudfront_distribution" "cdn" {\n  enabled = true\n}\n'),
    (
        'aws_s3_bucket_website_configuration',
        'resource "aws_s3_bucket_website_configuration" "site" {\n  bucket = "b"\n}\n',
    ),
]


@pytest.mark.parametrize("label,snippet", _FORBIDDEN_SNIPPETS, ids=[s[0] for s in _FORBIDDEN_SNIPPETS])
def test_detects_forbidden_resource(tmp_path: Path, label: str, snippet: str) -> None:
    """禁止リソースが 1 件でも入れば検出すること。"""
    (tmp_path / "main.tf").write_text(snippet)
    violations = find_violations([tmp_path])
    assert len(violations) == 1
    assert violations[0][2] == label


def test_clean_terraform_passes(tmp_path: Path) -> None:
    """通常のリソースは誤検知しないこと（誤検知は CI の無効化を招く）。"""
    (tmp_path / "main.tf").write_text(
        'resource "aws_lambda_function" "batch" {\n  function_name = "x"\n}\n'
        'resource "aws_s3_bucket" "logs" {\n  bucket = "y"\n}\n'
        'resource "aws_scheduler_schedule" "decision" {\n  name = "z"\n}\n'
        # 名前に api_gateway を含むが型が違うもの（前方一致の誤爆チェック）
        'resource "aws_iam_role" "aws_api_gateway_like" {\n  name = "w"\n}\n'
    )
    assert find_violations([tmp_path]) == []


def test_skips_build_artifacts(tmp_path: Path) -> None:
    """ビルド成果物（.terraform / build）配下は対象外。

    `infra/terraform/build/pkg/` には Lambda パッケージのコピーが入り、
    過去の定義が残ることがある。ここを拾うと恒久的な誤検知になる。
    """
    for skip_dir in (".terraform", "build", ".build"):
        d = tmp_path / skip_dir
        d.mkdir()
        (d / "main.tf").write_text('resource "aws_lambda_function_url" "x" {}\n')
    assert find_violations([tmp_path]) == []


def test_real_terraform_tree_is_clean() -> None:
    """🔴 実際の infra/terraform に公開リソースが無いこと（本番の契約そのもの）。"""
    root = Path(__file__).resolve().parents[2] / "infra" / "terraform"
    assert root.exists(), f"terraform ディレクトリが見つからない: {root}"
    violations = find_violations([root])
    assert violations == [], (
        "HTTP 公開リソースが追加されている。read-api / kill-switch は無認証であり、"
        f"公開すると全建玉を成行決済できる（§1.6.1）: {violations}"
    )


def test_missing_root_does_not_crash(tmp_path: Path) -> None:
    """存在しないパスを渡しても落ちない（CI の実行位置差で落とさない）。"""
    assert find_violations([tmp_path / "nope"]) == []
