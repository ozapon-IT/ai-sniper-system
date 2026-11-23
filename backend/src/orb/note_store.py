"""セッションノートの S3 / ローカル保存（`orb-demo/notes/YYYY-MM-DD.md`）。

設計書: docs/ai-dev/london-orb-demo-design.md §7.1

**S3 側は日付キー**（`YYYY-MM-DD.md`）で保持する。記録システム §1 の連番（`T001_...`）は
かつては vault へ同期する時点で振っていた（`scripts/sync_orb_demo_notes.py`）。
同期は #362 で運用終了。S3 は日付キーのまま。
S3 に連番を持たせると、途中の日を後から埋めたときに採番がずれて既存ファイルの
リネームが必要になるため。日付キーなら再生成が冪等になる。
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

S3_BUCKET = os.environ.get("S3_LOG_BUCKET", "your-log-bucket")
LOCAL_PERSIST_DIR = os.environ.get("LOCAL_PERSIST_DIR", "")
NOTES_PREFIX = "orb-demo/notes/"


def s3_key_orb_note(session_date: str | date) -> str:
    """セッションノートの S3 キー。"""
    day = session_date if isinstance(session_date, str) else session_date.isoformat()
    return f"{NOTES_PREFIX}{day}.md"


def _local_path(key: str) -> Path:
    return Path(LOCAL_PERSIST_DIR) / key


def _boto3_client() -> Any:
    import boto3

    kwargs: dict[str, Any] = {"region_name": os.environ.get("AWS_REGION", "ap-northeast-1")}
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


def save_note(session_date: str, markdown: str) -> bool:
    """ノートを保存する。`LOCAL_PERSIST_DIR` があればローカル、無ければ S3。"""
    key = s3_key_orb_note(session_date)
    if LOCAL_PERSIST_DIR:
        path = _local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        return True
    try:
        _boto3_client().put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=markdown.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        return True
    except Exception:  # noqa: BLE001 - 保存失敗でバッチを落とさない（アラートは呼び出し側）
        logger.exception("orb note save failed: %s", key)
        return False


def load_note(session_date: str) -> str | None:
    """保存済みノートを読む。無ければ None。"""
    key = s3_key_orb_note(session_date)
    if LOCAL_PERSIST_DIR:
        path = _local_path(key)
        return path.read_text(encoding="utf-8") if path.exists() else None
    try:
        obj = _boto3_client().get_object(Bucket=S3_BUCKET, Key=key)
        body = obj["Body"].read()
        return str(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        logger.info("orb note not found: %s", key)
        return None


def list_note_dates() -> list[str]:
    """保存済みノートのセッション日を昇順で返す（連番採番の入力）。"""
    if LOCAL_PERSIST_DIR:
        root = _local_path(NOTES_PREFIX)
        if not root.exists():
            return []
        return sorted(p.stem for p in root.glob("*.md"))
    try:
        client = _boto3_client()
        paginator = client.get_paginator("list_objects_v2")
        days: list[str] = []
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=NOTES_PREFIX):
            for item in page.get("Contents", []):
                name = str(item["Key"]).removeprefix(NOTES_PREFIX)
                if name.endswith(".md"):
                    days.append(name.removesuffix(".md"))
        return sorted(days)
    except Exception:  # noqa: BLE001
        logger.exception("orb note listing failed")
        return []
