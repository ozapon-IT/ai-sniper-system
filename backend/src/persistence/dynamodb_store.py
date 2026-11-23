"""DynamoDB 永続化モジュール（§1.5）。

テーブル: DYNAMODB_TABLE（デフォルト: your-job-lock-table）
キースキーマ:
  PK: date (S)  例: "2025-06-28"
  SK: job_type (S)  例: "decision" | "closeout" | "decision_concluded"

LocalStack 利用: AWS_ENDPOINT_URL=http://localhost:4566
ファイル fallback: LOCAL_PERSIST_DIR=<path> で指定したディレクトリに保存
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import boto3
import botocore.exceptions

logger = logging.getLogger(__name__)

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "your-job-lock-table")
LOCAL_PERSIST_DIR = os.environ.get("LOCAL_PERSIST_DIR", "")

JOB_DECISION = "decision"
JOB_CLOSEOUT = "closeout"
# 判断バッチが当日に「結論」（見送りを含む）を出したことのマーカー（#404）。
# 当日ロック（JOB_DECISION）とは別のレコード・別の意味: ロックは発注の二重実行を防ぎ、
# このマーカーは「見送りと判断した日に、同じ日の再起動で判断をやり直さない」ために使う。
# 手動で判断をやり直したいときは、このレコードだけを人間が削除する（Runbook §2.5）。
JOB_DECISION_CONCLUDED = "decision_concluded"


class LockReleaseOutcome(StrEnum):
    """ロック解放の結果（#406）。条件不成立と障害を区別する。

    - RELEASED: 解放した。
    - REFUSED: 条件不成立でロックを残した（ロック不在・実 orderId 記録済み）。設計どおりの拒否で、
      障害ではない。
    - FAILED: 障害（AccessDenied・DynamoDB / ローカル I/O のエラー）。ロックは残る。
      運用者が「その日はやり直せない」ことに気づけるよう、呼び出し側で通知する。
    """

    RELEASED = "released"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True)
class LockAcquireOutcome:
    """冪等ロック獲得の結果。acquired=False のとき lock_exists で競合と障害を区別する。"""

    acquired: bool
    lock_exists: bool = False


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
    return boto3.client("dynamodb", **kwargs)


def _item_key(today: date, job_type: str) -> dict[str, Any]:
    return {
        "date": {"S": today.isoformat()},
        "job_type": {"S": job_type},
    }


def _local_path(today: date, job_type: str) -> Path:
    return Path(LOCAL_PERSIST_DIR) / "dynamodb" / f"{today.isoformat()}-{job_type}.json"


def _local_read(today: date, job_type: str) -> dict[str, Any] | None:
    p = _local_path(today, job_type)
    if p.exists():
        raw: Any = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"invalid local DynamoDB fallback payload: {p}")
        return cast(dict[str, Any], raw)
    return None


def _is_conditional_check_failed(exc: Exception) -> bool:
    """DynamoDB の条件不成立を boto3 公式推奨の error code で判定する（3 関数で共通利用）。

    クラス名の文字列一致は動的生成例外・モックに依存して脆いため、ClientError の
    response["Error"]["Code"] を直接比較する。
    https://boto3.amazonaws.com/v1/documentation/api/latest/guide/error-handling.html（参照 2026-07-02）
    """
    if isinstance(exc, botocore.exceptions.ClientError):
        return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    return False


def _local_write(today: date, job_type: str, item: dict[str, Any]) -> None:
    p = _local_path(today, job_type)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------


def acquire_lock_outcome(
    today: date, job_type: str = JOB_DECISION, order_id: str | None = None
) -> LockAcquireOutcome:
    """冪等ロックを原子的に獲得し、競合とバックエンド障害を区別した結果を返す。

    既に同日・同 job_type のレコードが存在する場合は lock_exists=True。
    DynamoDB / ローカル I/O 障害は lock_exists=False（dynamodb_lock_acquire_failed を記録）。
    LOCAL_PERSIST_DIR が設定されている場合は O_EXCL でファイルを原子的に作成。
    """
    if LOCAL_PERSIST_DIR:
        p = _local_path(today, job_type)
        p.parent.mkdir(parents=True, exist_ok=True)
        item = {
            "date": today.isoformat(),
            "job_type": job_type,
            "order_id": order_id or "pending",
            "created_at": datetime.now(tz=UTC).isoformat(),
        }
        try:
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            logger.info(
                "local_lock_already_exists",
                extra={"date": today.isoformat(), "job_type": job_type},
            )
            return LockAcquireOutcome(acquired=False, lock_exists=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(item, f, ensure_ascii=False)
        return LockAcquireOutcome(acquired=True)

    try:
        ddb = _boto3_client()
        ddb.put_item(
            TableName=DYNAMODB_TABLE,
            Item={
                **_item_key(today, job_type),
                "order_id": {"S": order_id or "pending"},
                "created_at": {"S": datetime.now(tz=UTC).isoformat()},
            },
            ConditionExpression="attribute_not_exists(#d)",
            ExpressionAttributeNames={"#d": "date"},
        )
        logger.info(
            "dynamodb_lock_acquired",
            extra={"date": today.isoformat(), "job_type": job_type},
        )
        return LockAcquireOutcome(acquired=True)
    except Exception as exc:
        if _is_conditional_check_failed(exc):
            logger.info(
                "dynamodb_lock_already_exists",
                extra={"date": today.isoformat(), "job_type": job_type},
            )
            return LockAcquireOutcome(acquired=False, lock_exists=True)
        logger.error(
            "dynamodb_lock_acquire_failed",
            extra={"error": str(exc), "date": today.isoformat(), "job_type": job_type},
        )
        return LockAcquireOutcome(acquired=False, lock_exists=False)


def mark_decision_concluded(today: date, *, status: str, reason: str | None = None) -> bool:
    """当日の判断に結論が出たことを記録する（#404）。既に記録済みでも True（冪等）。

    DynamoDB は ``attribute_not_exists`` の条件付き Put。条件不成立（記録済み）は成功扱い。
    バックエンド障害は False（呼び出し側は error ログを残すだけで判断結果は変えない。
    記録できなければ、従来どおり同じ日の再起動で判断をやり直し得る）。
    """
    if LOCAL_PERSIST_DIR:
        p = _local_path(today, JOB_DECISION_CONCLUDED)
        p.parent.mkdir(parents=True, exist_ok=True)
        item: dict[str, Any] = {
            "date": today.isoformat(),
            "job_type": JOB_DECISION_CONCLUDED,
            "status": status,
            "created_at": datetime.now(tz=UTC).isoformat(),
        }
        if reason is not None:
            item["reason"] = reason
        try:
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            return True
        except OSError as exc:
            logger.error(
                "local_decision_concluded_write_failed",
                extra={"error": str(exc), "date": today.isoformat()},
            )
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(item, f, ensure_ascii=False)
        return True

    try:
        ddb = _boto3_client()
        ddb_item: dict[str, Any] = {
            **_item_key(today, JOB_DECISION_CONCLUDED),
            "status": {"S": status},
            "created_at": {"S": datetime.now(tz=UTC).isoformat()},
        }
        if reason is not None:
            ddb_item["reason"] = {"S": reason}
        ddb.put_item(
            TableName=DYNAMODB_TABLE,
            Item=ddb_item,
            ConditionExpression="attribute_not_exists(#d)",
            ExpressionAttributeNames={"#d": "date"},
        )
        logger.info(
            "dynamodb_decision_concluded_marked",
            extra={"date": today.isoformat(), "status": status, "reason": reason},
        )
        return True
    except Exception as exc:
        if _is_conditional_check_failed(exc):
            return True  # 既に記録済み（冪等）
        logger.error(
            "dynamodb_decision_concluded_write_failed",
            extra={"error": str(exc), "date": today.isoformat()},
        )
        return False


def is_decision_concluded(today: date) -> bool:
    """当日の判断に結論が出ている（マーカーがある）か。読み取り失敗は False（fail-open）。

    fail-open の理由: 発注の経路は別途 当日ロックの獲得（DynamoDB）を要するため、DynamoDB が
    読めないときに判断をやり直しても、ロックが取れなければ発注には進めない。ここで止めると
    DynamoDB の一時障害で見送り以外の日まで判断できなくなる。
    ``ConsistentRead=True``: GetItem は既定で結果整合の読み取りで、直前に書いたマーカーを見落とし
    得る。強い整合性の読み取りは「常に最後に更新された値を返す」。
    出典: boto3 同梱の botocore 1.43.78 の DynamoDB API モデル
    （``botocore/data/dynamodb/2012-08-10/service-2.json.gz`` の GetItem / ConsistentRead・
    参照日 2026-11-22）。
    """
    if LOCAL_PERSIST_DIR:
        try:
            return _local_read(today, JOB_DECISION_CONCLUDED) is not None
        except (ValueError, OSError) as exc:
            logger.error(
                "local_decision_concluded_read_failed",
                extra={"error": str(exc), "date": today.isoformat()},
            )
            return False

    try:
        ddb = _boto3_client()
        resp = ddb.get_item(
            TableName=DYNAMODB_TABLE,
            Key=_item_key(today, JOB_DECISION_CONCLUDED),
            ConsistentRead=True,
        )
        return bool(resp.get("Item"))
    except Exception as exc:
        logger.error(
            "dynamodb_decision_concluded_read_failed",
            extra={"error": str(exc), "date": today.isoformat()},
        )
        return False


def acquire_lock(today: date, job_type: str = JOB_DECISION, order_id: str | None = None) -> bool:
    """冪等ロックを原子的に獲得する（後方互換ラッパー）。"""
    return acquire_lock_outcome(today, job_type=job_type, order_id=order_id).acquired


def release_lock_outcome(today: date, job_type: str = JOB_DECISION) -> LockReleaseOutcome:
    """未約定が確実な発注失敗時に冪等ロックを解放し、結果を区別して返す（#138, §6.1, #406）。

    解放は条件付きの削除で、結果を RELEASED / REFUSED / FAILED に区別する（bool 版は無い。
    呼び出し側は FAILED だけを通知する）。

    order_id が "pending" のままのレコードのみ削除する。実 orderId が書き戻された
    ロックは発注成功の証跡であり、削除すると二重発注を許すため条件で拒否する。

    🔴 DynamoDB の削除は IAM の `dynamodb:DeleteItem` が要る（`iam.tf`・#406）。権限が無いと
    AccessDenied で FAILED になる（条件不成立の REFUSED とは別）。
    """
    if LOCAL_PERSIST_DIR:
        try:
            existing = _local_read(today, job_type)
        except ValueError as exc:
            # 破損ペイロードでも例外を伝播させず安全側（ロック保持）に倒す
            logger.error(
                "local_lock_release_invalid_payload",
                extra={"error": str(exc), "date": today.isoformat(), "job_type": job_type},
            )
            return LockReleaseOutcome.FAILED
        except OSError as exc:
            # exists() 後の read_text() で起きる I/O 例外（レース・権限）も安全側に倒す
            logger.warning(
                "local_lock_release_read_failed",
                extra={"error": str(exc), "date": today.isoformat(), "job_type": job_type},
            )
            return LockReleaseOutcome.FAILED
        if existing is None:
            logger.warning(
                "local_lock_release_not_found",
                extra={"date": today.isoformat(), "job_type": job_type},
            )
            return LockReleaseOutcome.REFUSED
        if existing.get("order_id") != "pending":
            logger.error(
                "local_lock_release_refused_order_id_present",
                extra={"date": today.isoformat(), "job_type": job_type},
            )
            return LockReleaseOutcome.REFUSED
        try:
            _local_path(today, job_type).unlink()
        except OSError as exc:
            # レース（FileNotFoundError）・権限エラー等 — 例外を伝播させず安全側（ロック保持扱い）
            logger.warning(
                "local_lock_release_unlink_failed",
                extra={"error": str(exc), "date": today.isoformat(), "job_type": job_type},
            )
            return LockReleaseOutcome.FAILED
        logger.info(
            "local_lock_released",
            extra={"date": today.isoformat(), "job_type": job_type},
        )
        return LockReleaseOutcome.RELEASED

    try:
        ddb = _boto3_client()
        # ConditionExpression 不成立時は削除されず ConditionalCheckFailedException。
        # https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_DeleteItem.html（参照 2026-07-02）
        ddb.delete_item(
            TableName=DYNAMODB_TABLE,
            Key=_item_key(today, job_type),
            ConditionExpression="attribute_exists(#d) AND order_id = :pending",
            ExpressionAttributeNames={"#d": "date"},
            ExpressionAttributeValues={":pending": {"S": "pending"}},
        )
    except Exception as exc:
        if _is_conditional_check_failed(exc):
            logger.error(
                "dynamodb_lock_release_refused",
                extra={"error": str(exc), "date": today.isoformat(), "job_type": job_type},
            )
            return LockReleaseOutcome.REFUSED
        logger.error(
            "dynamodb_lock_release_failed",
            extra={"error": str(exc), "date": today.isoformat(), "job_type": job_type},
        )
        return LockReleaseOutcome.FAILED
    # 削除成功の確定後にログ・return（ログ例外で成功を False に化けさせない）
    logger.info(
        "dynamodb_lock_released",
        extra={"date": today.isoformat(), "job_type": job_type},
    )
    return LockReleaseOutcome.RELEASED


def write_order_id(today: date, order_id: str, job_type: str = JOB_DECISION) -> bool:
    """発注成功後に実 orderId を書き戻す（§1.5, §5.2）。

    acquire_lock で作成済みのレコードのみ更新する。ロック未取得時は False を返す。
    """
    if LOCAL_PERSIST_DIR:
        existing = _local_read(today, job_type)
        if existing is None:
            logger.error(
                "local_order_id_write_without_lock",
                extra={"date": today.isoformat(), "job_type": job_type},
            )
            return False
        existing["order_id"] = order_id
        existing["order_written_at"] = datetime.now(tz=UTC).isoformat()
        _local_write(today, job_type, existing)
        logger.info(
            "local_order_id_written",
            extra={"date": today.isoformat(), "job_type": job_type, "order_id": order_id},
        )
        return True

    try:
        ddb = _boto3_client()
        ddb.update_item(
            TableName=DYNAMODB_TABLE,
            Key=_item_key(today, job_type),
            UpdateExpression="SET order_id = :oid, order_written_at = :ts",
            ConditionExpression="attribute_exists(#d) AND attribute_exists(#j)",
            ExpressionAttributeNames={"#d": "date", "#j": "job_type"},
            ExpressionAttributeValues={
                ":oid": {"S": order_id},
                ":ts": {"S": datetime.now(tz=UTC).isoformat()},
            },
        )
        logger.info(
            "dynamodb_order_id_written",
            extra={"date": today.isoformat(), "job_type": job_type, "order_id": order_id},
        )
        return True
    except Exception as exc:
        if _is_conditional_check_failed(exc):
            logger.error(
                "dynamodb_order_id_write_no_lock",
                extra={"error": str(exc), "date": today.isoformat(), "order_id": order_id},
            )
        else:
            logger.error(
                "dynamodb_order_id_write_failed",
                extra={"error": str(exc), "date": today.isoformat(), "order_id": order_id},
            )
        return False


def get_order_id(today: date, job_type: str = JOB_DECISION) -> str | None:
    """保存済みの orderId を取得する（監査・冪等用。pending は None）。"""
    if LOCAL_PERSIST_DIR:
        item = _local_read(today, job_type)
        if item is None:
            return None
        oid = item.get("order_id", "pending")
        return oid if oid != "pending" else None

    try:
        ddb = _boto3_client()
        resp = ddb.get_item(
            TableName=DYNAMODB_TABLE,
            Key=_item_key(today, job_type),
        )
        item = resp.get("Item", {})
        if not item:
            return None
        oid = item.get("order_id", {}).get("S", "pending")
        return oid if oid != "pending" else None
    except Exception as exc:
        logger.error(
            "dynamodb_get_order_id_failed",
            extra={"error": str(exc), "date": today.isoformat(), "job_type": job_type},
        )
        return None


def update_summary(today: date, summary: dict[str, Any], job_type: str = JOB_DECISION) -> bool:
    """実行サマリ（status, pnl 等）を DynamoDB レコードに追記する。"""
    if LOCAL_PERSIST_DIR:
        existing = _local_read(today, job_type)
        item: dict[str, Any] = existing or {
            "date": today.isoformat(),
            "job_type": job_type,
            "created_at": datetime.now(tz=UTC).isoformat(),
        }
        item.update(summary)
        _local_write(today, job_type, item)
        return True

    try:
        ddb = _boto3_client()
        update_expr_parts = []
        attr_values: dict[str, Any] = {}
        attr_names: dict[str, str] = {}
        for k, v in summary.items():
            placeholder = f"#k{k}"
            val_placeholder = f":v{k}"
            attr_names[placeholder] = k
            attr_values[val_placeholder] = {"S": str(v)}
            update_expr_parts.append(f"{placeholder} = {val_placeholder}")

        if not update_expr_parts:
            return True

        ddb.update_item(
            TableName=DYNAMODB_TABLE,
            Key=_item_key(today, job_type),
            UpdateExpression="SET " + ", ".join(update_expr_parts),
            ExpressionAttributeValues=attr_values,
            ExpressionAttributeNames=attr_names,
        )
        return True
    except Exception as exc:
        logger.error(
            "dynamodb_update_summary_failed",
            extra={"error": str(exc), "date": today.isoformat()},
        )
        return False
