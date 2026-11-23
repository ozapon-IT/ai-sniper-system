"""ORB デモの S3 / ローカル JSONL イベントログアダプタ。

キー: ``orb-demo/sessions/YYYY-MM-DD.jsonl``（設計書 §7.1）
状態機械は ``BrokerEvent`` を返すだけ。本モジュールが追記する。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

from backend.src.orb.broker_models import BrokerEvent

logger = logging.getLogger(__name__)

S3_BUCKET = os.environ.get("S3_LOG_BUCKET", "your-log-bucket")
LOCAL_PERSIST_DIR = os.environ.get("LOCAL_PERSIST_DIR", "")


def s3_key_orb_session_events(session_date: str | date) -> str:
    """セッションイベント JSONL の S3 キー。"""
    day = session_date if isinstance(session_date, str) else session_date.isoformat()
    return f"orb-demo/sessions/{day}.jsonl"


def _local_path(key: str) -> Path:
    return Path(LOCAL_PERSIST_DIR) / key


def _boto3_client() -> Any:
    import boto3

    kwargs: dict[str, Any] = {
        "region_name": os.environ.get("AWS_REGION", "ap-northeast-1"),
    }
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


def append_events(session_date: str, events: list[BrokerEvent]) -> bool:
    """イベントを JSONL に追記する。空なら no-op で True。"""
    if not events:
        return True
    key = s3_key_orb_session_events(session_date)
    lines = "".join(json.dumps(ev.to_jsonable(), ensure_ascii=False) + "\n" for ev in events)

    if LOCAL_PERSIST_DIR:
        try:
            path = _local_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(lines)
            return True
        except Exception as exc:
            logger.error(
                "orb_event_log_local_append_failed",
                extra={"key": key, "error": str(exc)},
            )
            return False

    try:
        client = _boto3_client()
        existing = ""
        try:
            resp = client.get_object(Bucket=S3_BUCKET, Key=key)
            existing = resp["Body"].read().decode("utf-8")
        except Exception as exc:
            # NoSuchKey は新規作成。それ以外は再送出。
            if "NoSuchKey" not in type(exc).__name__ and "NoSuchKey" not in str(exc):
                raise
        client.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=(existing + lines).encode("utf-8"),
            ContentType="application/x-ndjson",
        )
        return True
    except Exception as exc:
        logger.error(
            "orb_event_log_s3_append_failed",
            extra={"key": key, "error": str(exc)},
        )
        return False


def load_events(session_date: str) -> list[dict[str, Any]]:
    """JSONL を行ごとに dict で返す（テスト・検証用）。"""
    key = s3_key_orb_session_events(session_date)
    text = ""
    if LOCAL_PERSIST_DIR:
        path = _local_path(key)
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
    else:
        try:
            client = _boto3_client()
            resp = client.get_object(Bucket=S3_BUCKET, Key=key)
            text = resp["Body"].read().decode("utf-8")
        except Exception as exc:
            if "NoSuchKey" in type(exc).__name__ or "NoSuchKey" in str(exc):
                return []
            raise

    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        if isinstance(raw, dict):
            out.append(raw)
    return out


def notify_slack_for_events(events: list[BrokerEvent]) -> None:
    """``notify_slack=True`` のイベントを Slack へ送る（失敗は握りつぶす）。"""
    from backend.src.alerting.slack_alert import send_slack_alert

    for ev in events:
        if not ev.notify_slack:
            continue
        msg = (
            f"[ORB demo] anomaly session={ev.detail.get('how', ev.reason)} "
            f"state={ev.state_from}->{ev.state_to} ts={ev.ts.isoformat()}"
        )
        send_slack_alert(msg)
