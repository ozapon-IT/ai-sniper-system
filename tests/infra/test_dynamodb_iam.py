"""Lambda 実行ロールの DynamoDB 権限の回帰（#406）。

`dynamodb_store.release_lock_outcome` は条件付きの `delete_item` を呼ぶ（#138 / #208: 未約定確実な発注失敗・
閉場でのロック解放）。`dynamodb:DeleteItem` が無いと本番では AccessDenied になり、ロックの解放だけが
静かに効かない（モックの単体テストは IAM を通らず検知できない）。権限は必要な 4 操作に固定し、
それより広い操作（Scan / Query / BatchWriteItem / DeleteTable など）と、DynamoDB を使わない
関数への付与を拒否する。
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MODULE = _ROOT / "infra" / "terraform" / "modules" / "serverless_app"
_IAM = (_MODULE / "iam.tf").read_text(encoding="utf-8")
_LAMBDA = (_MODULE / "lambda.tf").read_text(encoding="utf-8")

_ALLOWED = {
    "dynamodb:PutItem",
    "dynamodb:GetItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
}


def _dynamodb_statement() -> str:
    start = _IAM.index('sid       = "DynamoDB"')
    begin = _IAM.rindex('dynamic "statement"', 0, start)
    end = _IAM.index("\n  }\n", start)
    return _IAM[begin:end]


def _actions(block: str) -> set[str]:
    m = re.search(r"actions\s*=\s*\[(.*?)\]", block, re.S)
    assert m, "DynamoDB statement に actions が無い"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def test_dynamodb_actions_are_exactly_the_four_needed() -> None:
    assert _actions(_dynamodb_statement()) == _ALLOWED


def test_release_lock_needs_delete_item() -> None:
    """release_lock_outcome（条件付き delete_item）が使う操作が許可されている。"""
    assert "dynamodb:DeleteItem" in _actions(_dynamodb_statement())


def test_no_broader_dynamodb_actions() -> None:
    actions = _actions(_dynamodb_statement())
    assert not any(a.endswith(":*") or a == "dynamodb:*" for a in actions)
    for forbidden in (
        "dynamodb:Scan",
        "dynamodb:Query",
        "dynamodb:BatchWriteItem",
        "dynamodb:BatchGetItem",
        "dynamodb:DeleteTable",
        "dynamodb:UpdateTable",
    ):
        assert forbidden not in actions


def test_dynamodb_statement_is_table_level_only() -> None:
    block = _dynamodb_statement()
    assert re.search(r"resources\s*=\s*\[aws_dynamodb_table\.app\.arn\]", block)
    assert "/index/" not in block
    assert '"*"' not in block


def test_only_one_statement_grants_dynamodb_actions() -> None:
    """別の statement で DynamoDB の権限を足していない（レビューで見落とさない）。"""
    assert len(re.findall(r"dynamodb:", _IAM)) == len(_ALLOWED) + _comment_mentions()


def _comment_mentions() -> int:
    """コメント内の `dynamodb:` の言及（権限ではない）。"""
    return sum(
        len(re.findall(r"dynamodb:", line)) for line in _IAM.splitlines() if line.lstrip().startswith("#")
    )


def test_statement_is_only_for_functions_with_dynamodb_flag() -> None:
    assert re.search(r"for_each\s*=\s*each\.value\.dynamodb\s*\?\s*\[1\]\s*:\s*\[\]", _dynamodb_statement())


def _flag(function: str) -> str:
    start = _LAMBDA.index(f"    {function} = {{")
    end = _LAMBDA.index("\n    }\n", start)
    m = re.search(r"dynamodb\s*=\s*(true|false)", _LAMBDA[start:end])
    assert m, f"{function} に dynamodb フラグが無い"
    return m.group(1)


def test_only_decision_and_closeout_batches_get_dynamodb() -> None:
    """判断・手仕舞いだけが DynamoDB を使う（ロックの獲得・解放）。ほかの関数には付かない。"""
    assert _flag("decision_batch") == "true"
    assert _flag("closeout_batch") == "true"
    for function in ("read_api", "kill_switch", "slack_notifier", "closeout_watchdog"):
        assert _flag(function) == "false", function
