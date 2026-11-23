"""Terraform lambda.tf の S3 write prefix 回帰（#225）。"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_LAMBDA_TF = _ROOT / "infra" / "terraform" / "modules" / "serverless_app" / "lambda.tf"


def test_decision_batch_writes_logs_decision_prefix() -> None:
    text = _LAMBDA_TF.read_text(encoding="utf-8")
    assert 's3_write_prefixes = ["logs/decision/"]' in text
    assert 's3_write_prefixes = ["logs/morning/"]' not in text
    assert "DECISION_MODEL = var.decision_model" in text


def test_closeout_batch_writes_logs_closeout_prefix() -> None:
    text = _LAMBDA_TF.read_text(encoding="utf-8")
    assert 's3_write_prefixes = ["logs/closeout/", "notes/", "summary/"]' in text
    assert "logs/evening/" not in text
    assert 'handler           = "backend.src.batch.closeout_batch.lambda_handler"' in text
    assert 'name_suffix       = "closeout-batch"' in text


def test_staging_dynamodb_table_name_is_job_lock() -> None:
    staging = (
        _ROOT / "infra" / "terraform" / "environments" / "staging" / "main.tf"
    ).read_text(encoding="utf-8")
    assert 'dynamodb_table_name = "your-job-lock-table"' in staging


def test_run_local_simulation_uses_closeout_naming() -> None:
    """#232: run_local_simulation.py の内部識別子は evening ではなく closeout 表記であること。"""
    text = (_ROOT / "scripts" / "run_local_simulation.py").read_text(encoding="utf-8")
    assert "_closeout_mode" in text
    assert "closeout_notes_generated" in text
    assert "_evening_mode" not in text
    assert "evening_notes_generated" not in text


def _function_block(text: str, name: str) -> str:
    start = text.index(f"    {name} = {{")
    nxt = text.find("\n    }\n", start)
    return text[start:nxt]


def test_decision_batch_can_read_logs_decision_prefix() -> None:
    """#402: 同じ日の 2 回目の起動が 1 回目の判断ログを上書きしないよう、保存前に既存ログを読む。

    読み取り権限が無いと、本番では読み込みが AccessDenied になり保護が効かない
    （ローカルのファイル保存のテストは IAM を通らないので検知できない）。
    GetObject と、s3:prefix 条件付きの ListBucket（存在確認の ListObjectsV2）が付く。
    """
    block = _function_block(_LAMBDA_TF.read_text(encoding="utf-8"), "decision_batch")
    assert re.search(r's3_read_prefixes\s*=\s*\["logs/decision/"\]', block)
    assert re.search(r's3_write_prefixes\s*=\s*\["logs/decision/"\]', block)
    # 読み取りは判断ログのプレフィックスに限る（広げない）
    assert not re.search(r's3_read_prefixes\s*=\s*\["logs/"', block)
