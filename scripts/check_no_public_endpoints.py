#!/usr/bin/env python3
"""Lambda の HTTP 公開リソースが terraform に追加されていないか検査する（§1.6.1・#177）.

🔴 **なぜ CI で見張るのか**

Ver 3.7 で read-api / kill-switch のアプリ層 Bearer 認証を廃止した。認証境界は IAM
（``lambda:InvokeFunction``）であり、**HTTP 公開しないこと自体が唯一の防御**になっている。

したがって Function URL / API Gateway / CloudFront を誰かが追加した瞬間、
**無認証で全建玉を成行決済できるエンドポイントが公開される**。人間のレビューだけに
頼らず機械的に落とす。

検出対象（terraform のリソース種別）:
  - aws_lambda_function_url
  - aws_api_gateway_*      （REST API）
  - aws_apigatewayv2_*     （HTTP API / WebSocket）
  - aws_cloudfront_distribution
  - aws_s3_bucket_website_configuration

意図的に公開する場合は §1.6.1 / §1.6.3 の再検討トリガーに従って**先に仕様を改訂**し、
本スクリプトの ``_ALLOWED_EXCEPTIONS`` に理由付きで追加すること。

⚠️ **検出範囲の限界（認識した上で許容している）**

本スクリプトが見るのは **terraform に書かれた公開リソース定義だけ**である。以下は検出しない:

- コンソール / CLI で**手動作成**した Function URL・API Gateway（IaC 外の変更）
- ``aws_lambda_permission`` の ``principal = "*"`` 等による広すぎる invoke 許可
- 既存リソースの属性変更（例: ``authorization_type`` を ``AWS_IAM`` から ``NONE`` へ）

現在の脅威モデル（単一利用者・IaC 経由の変更が主）では十分と判断している。
IaC 外の変更まで塞ぐなら AWS Config / CloudTrail 側の統制が要る（§1.6.3 の
再検討トリガーに該当したときに検討する）。

使い方:
    python scripts/check_no_public_endpoints.py            # 既定の infra/terraform を検査
    python scripts/check_no_public_endpoints.py <path> ...
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 検出する terraform リソース種別（前方一致）
_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "aws_lambda_function_url",
    "aws_api_gateway_",
    "aws_apigatewayv2_",
    "aws_cloudfront_distribution",
    "aws_s3_bucket_website_configuration",
)

#: 例外的に許可するリソース名 ``(type, name)``。追加には仕様改訂が必要（§1.6.3）。
_ALLOWED_EXCEPTIONS: frozenset[tuple[str, str]] = frozenset()

#: terraform のビルド成果物などスキャン対象外にするディレクトリ
_SKIP_DIRS: frozenset[str] = frozenset({".terraform", "build", ".build"})

_RESOURCE_RE = re.compile(r'^\s*resource\s+"([A-Za-z0-9_]+)"\s+"([A-Za-z0-9_-]+)"')


def find_violations(roots: list[Path]) -> list[tuple[Path, int, str, str]]:
    """公開リソース定義を探す。戻り値は (file, lineno, type, name) のリスト。"""
    violations: list[tuple[Path, int, str, str]] = []
    for root in roots:
        if not root.exists():
            continue
        for tf in sorted(root.rglob("*.tf")):
            if _SKIP_DIRS & set(tf.parts):
                continue
            for lineno, line in enumerate(tf.read_text().splitlines(), start=1):
                m = _RESOURCE_RE.match(line)
                if not m:
                    continue
                res_type, res_name = m.group(1), m.group(2)
                if not res_type.startswith(_FORBIDDEN_PREFIXES):
                    continue
                if (res_type, res_name) in _ALLOWED_EXCEPTIONS:
                    continue
                violations.append((tf, lineno, res_type, res_name))
    return violations


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv[1:]] or [Path("infra/terraform")]
    violations = find_violations(roots)
    if not violations:
        print("✅ HTTP 公開リソースは見つかりませんでした（§1.6.1）")
        return 0

    print("🔴 HTTP 公開リソースが追加されています（§1.6.1 違反）\n")
    for path, lineno, res_type, res_name in violations:
        print(f"  {path}:{lineno}  resource \"{res_type}\" \"{res_name}\"")
    print(
        "\nread-api / kill-switch はアプリ層の認証を持たない（Ver 3.7 / #177）。"
        "\nHTTP 公開すると**無認証で全建玉を成行決済できる**状態になります。"
        "\n意図的に公開するなら、まず §1.6.1 / §1.6.3 に従って仕様を改訂し、"
        "\n本スクリプトの _ALLOWED_EXCEPTIONS に理由付きで追加してください。"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
