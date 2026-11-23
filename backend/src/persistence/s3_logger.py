"""S3 実行ログ保存モジュール（§1.5, §5.2）。

S3 パス規約:
  logs/decision/{date}.json     — 判断バッチ実行ログ
  logs/closeout/{date}.json     — 手仕舞いバッチ実行ログ
  notes/{date}.json             — LLM トレードノート
  summary/dashboard.json        — ダッシュボード集約 JSON（最新上書き）

LocalStack 利用: AWS_ENDPOINT_URL=http://localhost:4566
ファイル fallback: LOCAL_PERSIST_DIR=<path> で指定したディレクトリに保存
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

import boto3

from backend.src.alerting.slack_alert import send_slack_alert

logger = logging.getLogger(__name__)

S3_BUCKET = os.environ.get("S3_LOG_BUCKET", "your-log-bucket")
LOCAL_PERSIST_DIR = os.environ.get("LOCAL_PERSIST_DIR", "")


# ---------------------------------------------------------------------------
# S3 パス定数
# ---------------------------------------------------------------------------


def s3_key_decision_log(today: date) -> str:
    """判断バッチ実行ログのキー（logs/decision/）。"""
    return f"logs/decision/{today.isoformat()}.json"


def s3_key_closeout_log(today: date) -> str:
    """手仕舞いバッチ実行ログのキー（logs/closeout/）。"""
    return f"logs/closeout/{today.isoformat()}.json"


def s3_key_trade_note(today: date) -> str:
    return f"notes/{today.isoformat()}.json"


S3_KEY_DASHBOARD = "summary/dashboard.json"


# ---------------------------------------------------------------------------
# 内部ユーティリティ
# ---------------------------------------------------------------------------


def _boto3_client() -> Any:
    kwargs: dict[str, Any] = {
        "region_name": os.environ.get("AWS_REGION", "ap-northeast-1"),
    }
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


def _slack_alert(message: str) -> None:
    """永続化失敗を Slack へ通知する（§1.5, AGENTS.md #9）。

    実装は `alerting.slack_alert.send_slack_alert` に集約（#206 で共用化）。
    テストが `s3_logger._slack_alert` をパッチできるようモジュール属性として残す。
    """
    send_slack_alert(message)


def _write_local(key: str, payload: dict[str, Any]) -> None:
    """LOCAL_PERSIST_DIR 指定時のファイル fallback。"""
    path = Path(LOCAL_PERSIST_DIR) / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    logger.info("local_persist_written", extra={"path": str(path)})


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------


def save_log(payload: dict[str, Any], s3_key: str, *, raise_on_failure: bool = False) -> bool:
    """payload を S3 の s3_key へ書き込む。

    Returns True on success, False on failure.
    失敗時は Slack アラートを送信する（§1.5）。
    LOCAL_PERSIST_DIR が設定されている場合はファイル fallback のみ実行。
    """
    if LOCAL_PERSIST_DIR:
        try:
            _write_local(s3_key, payload)
            return True
        except Exception as exc:
            msg = f"[AI Sniper] ローカル永続化失敗: key={s3_key} error={exc}"
            logger.error("local_persist_failed", extra={"key": s3_key, "error": str(exc)})
            _slack_alert(msg)
            if raise_on_failure:
                raise
            return False

    try:
        s3 = _boto3_client()
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=s3_key,
            Body=json.dumps(payload, ensure_ascii=False),
            ContentType="application/json",
        )
        logger.info("s3_log_saved", extra={"bucket": S3_BUCKET, "key": s3_key})
        return True
    except Exception as exc:
        msg = f"[AI Sniper] S3 書き込み失敗: key={s3_key} error={exc}"
        logger.error("s3_log_save_failed", extra={"key": s3_key, "error": str(exc)})
        _slack_alert(msg)
        if raise_on_failure:
            raise
        return False


def load_log(s3_key: str) -> dict[str, Any] | None:
    """S3 または LOCAL_PERSIST_DIR から JSON ログを読み込む。

    Returns:
        dict: 読み込み成功時。
        None: キー/ファイルが存在しない場合（not-found のみ）。

    Raises:
        RuntimeError: 存在するが読み込み・デコードに失敗した場合。
            呼び出し元が保存をスキップして既存データを破壊しないよう区別する。
    """
    if LOCAL_PERSIST_DIR:
        path = Path(LOCAL_PERSIST_DIR) / s3_key
        if not path.exists():
            return None
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise RuntimeError(f"期待する dict 型ではありません: {s3_key}")
            return raw
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"ローカルファイル読み込み失敗: key={s3_key}") from exc

    try:
        s3 = _boto3_client()
        resp = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
        raw = json.loads(resp["Body"].read().decode("utf-8"))
        if not isinstance(raw, dict):
            raise RuntimeError(f"期待する dict 型ではありません: {s3_key}")
        return raw
    except RuntimeError:
        raise
    except Exception as exc:
        if "NoSuchKey" in type(exc).__name__ or "NoSuchKey" in str(exc):
            return None
        raise RuntimeError(f"S3 読み込み失敗: key={s3_key}") from exc


def load_closeout_log(today: date) -> dict[str, Any] | None:
    """手仕舞いバッチ実行ログを読み込む。"""
    return load_log(s3_key_closeout_log(today))


def load_decision_log(today: date) -> dict[str, Any] | None:
    """判断バッチ実行ログを読み込む。

    手仕舞いバッチが rationale 等を取得するために使用。
    """
    return load_log(s3_key_decision_log(today))


def load_decision_log_if_exists(today: date) -> dict[str, Any] | None:
    """判断ログを読む。存在確認は ListObjectsV2、存在するときだけ GetObject する（#402）。

    存在しないキーへの GetObject は、呼び出し側に ``s3:ListBucket`` が（条件を満たして）
    付いているかで 404 か 403 になり得る。この Lambda の ``s3:ListBucket`` は ``s3:prefix``
    条件付きなので、**404 になることに頼らない**: まず ``Prefix=<正確なキー>`` の
    ListObjectsV2（``s3:prefix`` 条件を満たす）で存在を確かめ、あるときだけ GetObject する。

    Returns:
        dict: 存在して読めたとき。
        None: 存在しないとき。

    Raises:
        RuntimeError: 存在確認・読み込み・デコードのいずれかに失敗したとき
            （``load_log`` / ``list_s3_keys`` の契約どおり。呼び出し元は既存データを
            破壊しない側に倒すこと）。
    """
    key = s3_key_decision_log(today)
    if LOCAL_PERSIST_DIR:
        return load_log(key)  # ローカルファイルは存在しなければ None
    if key not in list_s3_keys(prefix=key):
        return None
    return load_log(key)


def load_dashboard() -> dict[str, Any] | None:
    """ダッシュボード集約 JSON を読み込む（手仕舞いバッチが note 部分を追記するために使用）。"""
    return load_log(S3_KEY_DASHBOARD)


def save_decision_log(
    payload: dict[str, Any], today: date, *, raise_on_failure: bool = False
) -> bool:
    """判断バッチ実行ログを S3（logs/decision/）へ保存する。"""
    return save_log(payload, s3_key_decision_log(today), raise_on_failure=raise_on_failure)


def save_closeout_log(
    payload: dict[str, Any], today: date, *, raise_on_failure: bool = False
) -> bool:
    """手仕舞いバッチ実行ログを S3（logs/closeout/）へ保存する。"""
    return save_log(payload, s3_key_closeout_log(today), raise_on_failure=raise_on_failure)


def save_trade_note(note: dict[str, Any], today: date) -> bool:
    """LLM トレードノートを S3 へ保存する。"""
    return save_log(note, s3_key_trade_note(today))


def save_dashboard(data: dict[str, Any]) -> bool:
    """ダッシュボード集約 JSON を S3 へ保存する（最新上書き）。"""
    return save_log(data, S3_KEY_DASHBOARD)


def s3_key_kill_switch_log(ts: str) -> str:
    """Kill Switch 実行ログの S3 キーを返す（ISO タイムスタンプでユニーク）。"""
    safe_ts = ts.replace(":", "-").replace("+", "Z")
    return f"logs/kill_switch/{safe_ts}.json"


def save_kill_switch_log(payload: dict[str, Any]) -> bool:
    """Kill Switch 実行ログを S3 へ直接保存する（§1.5）。

    LOCAL_PERSIST_DIR fallback を意図的にスキップする。Kill Switch の実行記録は
    Lambda ローカルファイルに落としてはいけない（緊急証跡は S3 のみ）。
    """
    ts = payload.get("timestamp", "unknown")
    key = s3_key_kill_switch_log(str(ts))
    try:
        s3 = _boto3_client()
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=json.dumps(payload, ensure_ascii=False),
            ContentType="application/json",
        )
        logger.info(
            "kill_switch_log_saved",
            extra={
                "bucket": S3_BUCKET,
                "key": key,
                "timestamp": str(ts),
                "llm_tokens": int(payload.get("llm_tokens") or 0),
            },
        )
        return True
    except Exception as exc:
        msg = f"[AI Sniper] Kill Switch S3 ログ書き込み失敗: key={key} error={exc}"
        logger.error(
            "kill_switch_log_save_failed",
            extra={
                "key": key,
                "error": str(exc),
                "timestamp": str(ts),
                "llm_tokens": int(payload.get("llm_tokens") or 0),
            },
        )
        _slack_alert(msg)
        return False


def list_s3_keys(prefix: str, suffix: str = "") -> list[str]:
    """S3 の指定プレフィクス下のキー一覧を返す（履歴エンドポイント用）.

    LOCAL_PERSIST_DIR 設定時はローカルディレクトリをスキャンする。
    """
    if LOCAL_PERSIST_DIR:
        base = Path(LOCAL_PERSIST_DIR) / prefix
        if not base.exists():
            return []
        keys: list[str] = []
        for p in sorted(base.iterdir()):
            if p.is_file() and p.name.endswith(suffix or ""):
                keys.append(prefix + p.name)
        return keys

    try:
        s3 = _boto3_client()
        paginator = s3.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                if not suffix or key.endswith(suffix):
                    keys.append(key)
        return keys
    except Exception as exc:
        logger.error("s3_list_keys_failed", extra={"prefix": prefix, "error": str(exc)})
        raise RuntimeError(f"S3 key listing failed: prefix={prefix}") from exc
