"""ORB デモセッションの DynamoDB / ローカル永続化アダプタ。

テーブル: ``orb_demo_sessions``（設計書 §6.1）。夜型の ``your-job-lock-table`` とは別。
状態機械本体（``broker.py``）とは切り離し、I/O のみを担当する。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from backend.src.orb.broker_models import (
    BrokerSession,
    BrokerState,
    DirectionName,
    ExitReason,
    SkipReason,
    SlippageRecord,
    TickerObservation,
)
from backend.src.orb.models import RangeLevels

logger = logging.getLogger(__name__)

ORB_DEMO_SESSIONS_TABLE = os.environ.get("ORB_DEMO_SESSIONS_TABLE", "orb_demo_sessions")
LOCAL_PERSIST_DIR = os.environ.get("LOCAL_PERSIST_DIR", "")

_HALT_KEY = "_halt"


def _local_root() -> Path:
    return Path(LOCAL_PERSIST_DIR) / "orb_demo_sessions"


def _local_path(session_date: str) -> Path:
    return _local_root() / f"{session_date}.json"


def _boto3_client() -> Any:
    import boto3

    kwargs: dict[str, Any] = {
        "region_name": os.environ.get("AWS_REGION", "ap-northeast-1"),
    }
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("dynamodb", **kwargs)


def _slip_to_dict(slip: SlippageRecord | None) -> dict[str, Any] | None:
    if slip is None:
        return None
    return asdict(slip)


def _slip_from_dict(raw: dict[str, Any] | None) -> SlippageRecord | None:
    if raw is None:
        return None
    return SlippageRecord(
        trigger_price=float(raw["trigger_price"]),
        observed_bid=float(raw["observed_bid"]),
        slip_pips=float(raw["slip_pips"]),
        spread_pips=float(raw["spread_pips"]),
        poll_gap_ms=None if raw.get("poll_gap_ms") is None else int(raw["poll_gap_ms"]),
        price_gap=None if raw.get("price_gap") is None else float(raw["price_gap"]),
    )


def _obs_to_dict(obs: TickerObservation | None) -> dict[str, Any] | None:
    if obs is None:
        return None
    return {
        "ts": obs.ts.isoformat(),
        "bid": obs.bid,
        "ask": obs.ask,
        "status": obs.status,
    }


def _obs_from_dict(raw: dict[str, Any] | None) -> TickerObservation | None:
    if raw is None:
        return None
    return TickerObservation(
        ts=datetime.fromisoformat(str(raw["ts"])),
        bid=float(raw["bid"]),
        ask=float(raw["ask"]),
        status=str(raw["status"]),
    )


def session_to_dict(session: BrokerSession) -> dict[str, Any]:
    levels = session.levels
    return {
        "session_date": session.session_date,
        "state": session.state,
        "levels": None
        if levels is None
        else {
            "high": levels.high,
            "low": levels.low,
            "buy_stop": levels.buy_stop,
            "sell_stop": levels.sell_stop,
        },
        "direction": session.direction,
        "entry_price": session.entry_price,
        "entry_ts": None if session.entry_ts is None else session.entry_ts.isoformat(),
        "sl_price": session.sl_price,
        "exit_price": session.exit_price,
        "exit_ts": None if session.exit_ts is None else session.exit_ts.isoformat(),
        "exit_reason": session.exit_reason,
        "skip_reason": session.skip_reason,
        "opposite_cancel_pending": session.opposite_cancel_pending,
        "last_open_obs": _obs_to_dict(session.last_open_obs),
        "entry_slip": _slip_to_dict(session.entry_slip),
        "exit_slip": _slip_to_dict(session.exit_slip),
        "uncertain_reason": session.uncertain_reason,
    }


def session_from_dict(raw: dict[str, Any]) -> BrokerSession:
    levels_raw = raw.get("levels")
    levels = None
    if isinstance(levels_raw, dict):
        levels = RangeLevels(
            high=float(levels_raw["high"]),
            low=float(levels_raw["low"]),
            buy_stop=float(levels_raw["buy_stop"]),
            sell_stop=float(levels_raw["sell_stop"]),
        )
    entry_ts = raw.get("entry_ts")
    exit_ts = raw.get("exit_ts")
    return BrokerSession(
        session_date=str(raw["session_date"]),
        state=cast(BrokerState, raw["state"]),
        levels=levels,
        direction=cast(DirectionName | None, raw.get("direction")),
        entry_price=None if raw.get("entry_price") is None else float(raw["entry_price"]),
        entry_ts=None if entry_ts is None else datetime.fromisoformat(str(entry_ts)),
        sl_price=None if raw.get("sl_price") is None else float(raw["sl_price"]),
        exit_price=None if raw.get("exit_price") is None else float(raw["exit_price"]),
        exit_ts=None if exit_ts is None else datetime.fromisoformat(str(exit_ts)),
        exit_reason=cast(ExitReason | None, raw.get("exit_reason")),
        skip_reason=cast(SkipReason | None, raw.get("skip_reason")),
        opposite_cancel_pending=bool(raw.get("opposite_cancel_pending", False)),
        last_open_obs=_obs_from_dict(cast(dict[str, Any] | None, raw.get("last_open_obs"))),
        entry_slip=_slip_from_dict(cast(dict[str, Any] | None, raw.get("entry_slip"))),
        exit_slip=_slip_from_dict(cast(dict[str, Any] | None, raw.get("exit_slip"))),
        uncertain_reason=None
        if raw.get("uncertain_reason") is None
        else str(raw["uncertain_reason"]),
    )


# 🔴 **終端は sticky**（上書き禁止）。
# 15:55 の poller（300 秒回）と 16:00 の EOD 回は、どちらも「5 分ループの最後に 1 回だけ」
# save する。16:00 回は 15:50 時点の状態を load するため、**終端どうしがぶつかる**:
#   15:55 側がメモリ上で CLOSED(sl) → 16:00 側は古い FILLED から CLOSED(eod) を書く
#   → 後から 15:55 が CLOSED(sl) を書くと EOD が消える（逆順なら SL が消える）
# ランク比較だけでは終端どうし（同ランク）を素通りさせるため、**保存済みが終端なら拒否**する。
#
# 本来は Lambda の予約同時実行で直列化する設計だったが、本アカウントの同時実行上限が 10 で
# 予約できない（apply が InvalidParameterValueException で失敗・2026-08-14 実測）。
TERMINAL_STATES: frozenset[str] = frozenset(
    {"CLOSED", "CANCELLED", "ANOMALY", "UNCERTAIN", "SKIPPED"}
)
_NON_TERMINAL_STATES: tuple[str, ...] = ("IDLE", "ARMED", "FILLED")

# 非終端どうしの巻き戻り（FILLED → ARMED 等）も防ぐ。
_STATE_RANK: dict[str, int] = {"IDLE": 0, "ARMED": 1, "FILLED": 2}

# 🔴 **entry は先着確定**（#304）。poller は `run_seconds`(300s) が起動間隔(300s)と同値で
# 実行時間が 301.3s のため起動どうしが必ず重なる。`orb_poller` 側で遷移を即 save して
# 窓は塞いだが、「load 直後に fill」の数秒窓は原理的に残る。そこを通り抜けた後発の
# 起動は、古い ARMED から **2 度目の fill** を判定して別の entry を書きに来る
# （2026-08-14 実測: 真 fill 159.185/滑り0.6pips を 159.168/滑り2.3pips が上書きし得た）。
#
# 状態ランクでは防げない（FILLED → FILLED は同ランク）。`entry_ts` の一致で判定する:
# **最初の fill だけが entry を確定でき**、別の `entry_ts` を持つ書き込みと、
# 確定済み entry を消す書き込みを拒否する。DynamoDB では ConditionExpression で
# アトミックに評価するため Get→Put の TOCTOU 窓が無い。
#
# 壊れるのが滑り統計（§6.3）＝デモの合否基準そのものなので、終端 sticky と同格で守る。

# save の結果。`REJECTED` は「条件不成立で書かなかった」＝**異常ではない**（先着が正）。
SAVE_WRITTEN = "written"
SAVE_REJECTED = "rejected"
SAVE_FAILED = "failed"


def _entry_ts_str(session: BrokerSession) -> str | None:
    return None if session.entry_ts is None else session.entry_ts.isoformat()


def build_put_condition(entry_ts: str | None) -> dict[str, Any]:
    """`put_item` の条件式を組み立てる（`ConditionExpression` / `Names` / `Values`）。

    ローカル経路（`_local_write_blocked`）と**同じ意味**を DynamoDB 側で表す。
    式を関数に切り出して固定し、テストで直接検証できるようにする。

    - ① 未作成、または保存済みが**非終端**のときだけ書く（＝終端は sticky）
    - ② entry は先着確定。entry を持たない書き込みは確定済み entry を消せない
    """
    values: dict[str, Any] = {f":s{i}": {"S": st} for i, st in enumerate(_NON_TERMINAL_STATES)}
    clauses = ["attribute_not_exists(session_date) OR #st IN (:s0, :s1, :s2)"]
    if entry_ts is None:
        clauses.append("attribute_not_exists(#ets)")
    else:
        clauses.append("attribute_not_exists(#ets) OR #ets = :ets")
        values[":ets"] = {"S": entry_ts}
    return {
        "ConditionExpression": " AND ".join(f"({c})" for c in clauses),
        "ExpressionAttributeNames": {"#st": "state", "#ets": "entry_ts"},
        "ExpressionAttributeValues": values,
    }


def _local_write_blocked(incoming: BrokerSession) -> str | None:
    """ローカル永続化（単一プロセス）での退行判定。拒否理由 or None。"""
    try:
        stored = load_session(incoming.session_date)
    except Exception:  # noqa: BLE001 - 読めないなら書く（可用性優先）
        return None
    if stored is None:
        return None
    if stored.state in TERMINAL_STATES:
        return f"stored_terminal:{stored.state}"
    stored_entry = _entry_ts_str(stored)
    if stored_entry is not None:
        incoming_entry = _entry_ts_str(incoming)
        if incoming_entry is None:
            return f"entry_cleared:{stored_entry}"
        if incoming_entry != stored_entry:
            return f"entry_conflict:{stored_entry}"
    if incoming.state in TERMINAL_STATES:
        # 終端への前進は常に許可する（stored が終端のケースは上で弾き済み）。
        # `_STATE_RANK` は非終端専用なので、終端を `.get(..., 0)` で評価すると
        # FILLED → CLOSED が「退行」に化ける（ローカル経路のみの既存バグ・#304）。
        return None
    if _STATE_RANK.get(incoming.state, 0) < _STATE_RANK.get(stored.state, 0):
        return f"rank_regression:{stored.state}"
    return None


def save_session(session: BrokerSession, *, allow_regression: bool = False) -> bool:
    """セッション状態を保存する。**書けなかった＝異常** のときだけ False。

    条件不成立での不書き込み（終端 sticky・entry 先着）は正常系なので True を返す。
    呼び出し側が「実際に書けたか」を知る必要があるときは `save_session_checked` を使う。
    """
    return save_session_checked(session, allow_regression=allow_regression) != SAVE_FAILED


def save_session_checked(session: BrokerSession, *, allow_regression: bool = False) -> str:
    """セッション状態を保存し、結果を `SAVE_*` で返す。

    🔴 **保存済みが終端なら上書きしない**。遅れて到着した別 invocation の save が、
    確定済みの決済記録（EOD / SL）を消さないようにする。
    🔴 **entry は先着確定**（#304）。別の `entry_ts` を持つ書き込みと、確定済み entry を
    消す書き込みを拒否する。二重 fill 判定が滑り統計（§6.3）を汚染するのを防ぐ。
    DynamoDB では `ConditionExpression` で**アトミックに**判定するため、
    Get→Put の間に相手が書く TOCTOU 窓が無い。

    `allow_regression=True` は意図的な巻き戻し（テスト・手動復旧）でのみ使う。
    """
    payload = session_to_dict(session)
    entry_ts = _entry_ts_str(session)
    if LOCAL_PERSIST_DIR:
        blocked = None if allow_regression else _local_write_blocked(session)
        if blocked is not None:
            logger.warning(
                "orb_session_save_skipped",
                extra={
                    "session_date": session.session_date,
                    "incoming_state": session.state,
                    "reason": blocked,
                },
            )
            return SAVE_REJECTED
        try:
            path = _local_path(session.session_date)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return SAVE_WRITTEN
        except Exception as exc:
            logger.error(
                "orb_session_local_save_failed",
                extra={"session_date": session.session_date, "error": str(exc)},
            )
            return SAVE_FAILED

    try:
        client = _boto3_client()
        item = {
            "session_date": {"S": session.session_date},
            # `state` / `entry_ts` を**トップレベル属性**にも持つ（ConditionExpression で
            # 参照するため。payload は JSON 文字列なので条件式から中身を見られない）。
            "state": {"S": session.state},
            "payload": {"S": json.dumps(payload, ensure_ascii=False)},
        }
        if entry_ts is not None:
            item["entry_ts"] = {"S": entry_ts}
        kwargs: dict[str, Any] = {"TableName": ORB_DEMO_SESSIONS_TABLE, "Item": item}
        if not allow_regression:
            kwargs.update(build_put_condition(entry_ts))
        try:
            client.put_item(**kwargs)
        except client.exceptions.ConditionalCheckFailedException:
            logger.warning(
                "orb_session_save_skipped",
                extra={
                    "session_date": session.session_date,
                    "incoming_state": session.state,
                    "incoming_entry_ts": entry_ts,
                    "reason": _rejection_reason(session.session_date),
                },
            )
            return SAVE_REJECTED
        return SAVE_WRITTEN
    except Exception as exc:
        logger.error(
            "orb_session_dynamo_save_failed",
            extra={"session_date": session.session_date, "error": str(exc)},
        )
        return SAVE_FAILED


def _rejection_reason(session_date: str) -> str:
    """条件不成立の理由を best-effort で特定する（ログ用・失敗しても握り潰す）。

    拒否は 1 日に数回あるかどうかなので、追加の GET を払ってでも
    「終端だったのか entry 衝突だったのか」を運用時に判別できるようにする。
    """
    try:
        stored = load_session(session_date)
    except Exception:  # noqa: BLE001 - ログのための読みで失敗を伝播させない
        return "condition_failed:unknown"
    if stored is None:
        return "condition_failed:unknown"
    if stored.state in TERMINAL_STATES:
        return f"stored_terminal:{stored.state}"
    stored_entry = _entry_ts_str(stored)
    if stored_entry is not None:
        return f"entry_conflict:{stored_entry}"
    return f"condition_failed:{stored.state}"


def load_session(session_date: str) -> BrokerSession | None:
    """セッションを読み込む。無ければ None。"""
    if LOCAL_PERSIST_DIR:
        path = _local_path(session_date)
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"invalid orb session payload: {path}")
        return session_from_dict(cast(dict[str, Any], raw))

    try:
        client = _boto3_client()
        resp = client.get_item(
            TableName=ORB_DEMO_SESSIONS_TABLE,
            Key={"session_date": {"S": session_date}},
        )
        item = resp.get("Item")
        if not item:
            return None
        payload = json.loads(item["payload"]["S"])
        return session_from_dict(cast(dict[str, Any], payload))
    except Exception as exc:
        logger.error(
            "orb_session_dynamo_load_failed",
            extra={"session_date": session_date, "error": str(exc)},
        )
        raise


def get_halt_detail() -> dict[str, Any]:
    """halt の状態を詳細で読む（`halt` / `reason` / `since`）。

    `since` は halt を **False → True** にした時刻（ISO8601）。ORB-6 で halt 中の日次
    Slack を抑止したため、**halt が続いていることは週次サマリでしか分からない**（§9）。
    継続日数の算出に使う。`since` を持たない古いレコードは `None`。
    """
    if LOCAL_PERSIST_DIR:
        path = _local_path(_HALT_KEY)
        if not path.exists():
            return {"halt": False, "reason": "", "since": None}
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {
            "halt": bool(raw.get("halt", False)),
            "reason": str(raw.get("reason", "")),
            "since": raw.get("since"),
        }

    try:
        client = _boto3_client()
        resp = client.get_item(
            TableName=ORB_DEMO_SESSIONS_TABLE,
            Key={"session_date": {"S": _HALT_KEY}},
        )
        item = resp.get("Item")
        if not item:
            return {"halt": False, "reason": "", "since": None}
        since = item.get("since", {}).get("S")
        return {
            "halt": item.get("halt", {}).get("BOOL", False) is True,
            "reason": item.get("reason", {}).get("S", ""),
            "since": since or None,
        }
    except Exception as exc:
        logger.error("orb_halt_load_failed", extra={"error": str(exc)})
        raise


def get_halt() -> bool:
    """グローバル halt フラグを読む（不一致検知でセット・§8.2）。"""
    if LOCAL_PERSIST_DIR:
        path = _local_path(_HALT_KEY)
        if not path.exists():
            return False
        raw = json.loads(path.read_text(encoding="utf-8"))
        return bool(raw.get("halt", False))

    try:
        client = _boto3_client()
        resp = client.get_item(
            TableName=ORB_DEMO_SESSIONS_TABLE,
            Key={"session_date": {"S": _HALT_KEY}},
        )
        item = resp.get("Item")
        if not item:
            return False
        return item.get("halt", {}).get("BOOL", False) is True
    except Exception as exc:
        logger.error("orb_halt_load_failed", extra={"error": str(exc)})
        raise


def set_halt(halt: bool, *, reason: str = "") -> bool:
    """halt フラグを書き込む。解除も本関数（reason をログに残す）。

    `since`（発動時刻）は **False → True の遷移でのみ更新**する。既に halt 中の再セットで
    上書きすると継続日数がリセットされ、週次サマリ（§9）の「解除忘れ検出」が効かなくなる。
    """
    since: str | None = None
    if halt:
        try:
            current = get_halt_detail()
        except Exception:  # noqa: BLE001 - 読めなくても halt のセットは通す
            current = {"halt": False, "since": None}
        since = (
            current.get("since")
            if current.get("halt") and current.get("since")
            else datetime.now(tz=UTC).isoformat()
        )
    logger.info("orb_halt_set", extra={"halt": halt, "reason": reason, "since": since})

    if LOCAL_PERSIST_DIR:
        try:
            path = _local_path(_HALT_KEY)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"halt": halt, "reason": reason, "since": since}, ensure_ascii=False
                ),
                encoding="utf-8",
            )
            return True
        except Exception as exc:
            logger.error("orb_halt_local_save_failed", extra={"error": str(exc)})
            return False

    try:
        client = _boto3_client()
        item: dict[str, Any] = {
            "session_date": {"S": _HALT_KEY},
            "halt": {"BOOL": halt},
            "reason": {"S": reason},
        }
        if since:
            item["since"] = {"S": since}
        client.put_item(TableName=ORB_DEMO_SESSIONS_TABLE, Item=item)
        return True
    except Exception as exc:
        logger.error("orb_halt_dynamo_save_failed", extra={"error": str(exc)})
        return False
