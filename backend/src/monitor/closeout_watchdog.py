"""手仕舞い欠測ウォッチドッグ（Issue #295）。

火〜土 5:00 JST（closeout の 30 分後）に起動し、**その朝の手仕舞いログが S3 に
書かれているか**を確認する。無ければ Slack に強アラートを出す。

🔴 **なぜ必要か**

現行の監視（§5.4 / #105）は「**起動したが失敗した**」系しか見ていない。
Scheduler が `DISABLED` なら何も発火しないため、`InvocationDroppedCount` も
Lambda `Errors` も DLQ も**すべて沈黙する**。2026-08-13 に、`terraform apply` の
`-var` 付け忘れで両 cron が `DISABLED` になり得る状態を発見した（#294 で解消）。

手仕舞いが止まると、OCO（TP/SL）は GMO 側で生きているので即座に無防備にはならないが、
**時間切れ手仕舞いが機能せず保有窓 8 時間の前提が崩れる**。気づくのは翌朝の手動確認時で、
最大 24 時間気づけない。

🔴 **この Lambda の EventBridge Scheduler は `var.schedule_state` を参照しない**
（`infra/terraform/modules/serverless_app/scheduler.tf`。独立変数 `watchdog_schedule_state`、
モジュール既定 `ENABLED`。**既定を `ENABLED` 以外にしないこと**）。監視が trading cron と運命を共有すると、**事故の再現で監視ごと
沈黙する**。これが本ウォッチドッグの設計上いちばん重要な性質である。
`DISABLED` にしてよいのは計画停止で建玉・注文が 0 のときだけ。契約の正は
`docs/system_requirements.md` §1.6.2-1（#371 / #372）。

判定方針:
  - 見るのは **S3 の成果物**（`logs/closeout/<取引日>.json`）。Lambda の `Invocations`
    メトリクスだけだと「起動したがログを書けなかった」を取りこぼす。
  - **`run_date` が当日と一致すること**まで確認する（下記 `_is_fresh` の設計根拠）。
  - **`note_pending` が残っていないこと**も見る（#313）。#311 で証跡を LLM より先に
    保存するようにしたため、LLM がハングしても closeout ログは残る。存在チェックだけだと
    「ログはあるがノートが無い」を素通りする。
  - **その朝のログの JSON bool ``executions_fetched`` が ``false`` のときだけ**、
    別の ALERT を出す（#245）。キー欠損・null・文字列・数値は通知しない。
    欠測・古い ``run_date``・S3 例外には、この通知を足さない。
  - 誤発火は許容する（沈黙より軽い）。ただし休場日は**スキップしない**（`_should_run` 参照）。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from backend.src.alerting.slack_alert import send_slack_alert
from backend.src.persistence import s3_logger
from config.market_calendar import is_market_holiday

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_JST_OFFSET = timedelta(hours=9)

#: 手仕舞い cron が動く曜日（火〜土 = weekday 1..5）。`cron(30 4 ? * TUE-SAT *)` と対応。
_CLOSEOUT_RUN_WEEKDAYS: frozenset[int] = frozenset({1, 2, 3, 4, 5})

#: Step3 のあとでも建玉が無防備に残り得る status。通知本文には生文字列を出さない。
_STEP3_OPEN_RISK: frozenset[str] = frozenset(
    {"step3_failed", "step3_unverified", "step3_residual"}
)
#: 約定取得へ進む前に return する status。false は書かない。
_FETCH_NOT_ATTEMPTED: frozenset[str] = frozenset(
    {"market_closed", "step1_failed", "step2_failed"}
)


def _today_jst() -> date:
    """現在日付を JST で返す（§6 時刻は常に JST）。"""
    return (datetime.now(tz=UTC) + _JST_OFFSET).date()


def resolve_prev_trade_date(today: date) -> date:
    """直近の確定取引日（土日・祝日を逆順に遡る）。

    ⚠️ `closeout_batch._resolve_prev_trade_date` と**同一の規則**であること。
    ずれると別の取引日のログを見に行き、欠測を見逃す / 誤検知する。
    """
    prev = today - timedelta(days=1)
    while prev.weekday() >= 5 or is_market_holiday(prev):
        prev -= timedelta(days=1)
    return prev


def _is_fresh(log: dict[str, Any], today: date) -> bool:
    """ログが**その朝の実行**で書かれたものかを判定する。

    🔴 存在チェックだけでは不十分。`resolve_prev_trade_date` は土日・祝日を遡るため、
    月曜が休場だと火曜 4:30 の実行が**金曜**に解決する。金曜のログは土曜 4:30 の実行が
    既に書いているので、火曜の実行が失敗していても「ログはある」で素通りしてしまう。

    そこで `run_date`（バッチ実行日・closeout ペイロードの既存フィールド）が
    当日と一致することまで見る。
    """
    return log.get("run_date") == today.isoformat()


def _executions_fetched_flag(log: dict[str, Any]) -> bool | None:
    """JSON bool だけを返す。欠損・null・文字列・数値は None。

    ``True`` / ``False`` は ``int`` のサブクラスなので、数値 0 と取り違えないよう
    ``bool`` を先に見る。``false`` 以外では約定未取得アラートを出さない。
    """
    if "executions_fetched" not in log:
        return None
    value = log["executions_fetched"]
    if isinstance(value, bool):
        return value
    return None


def _executions_alert_kind(log: dict[str, Any]) -> tuple[bool | None, str | None]:
    """fresh ログの通知種別。明示 false を先にし、未着手と重複送信しない。

    戻り値は ``(flag, executions_alert)``。``flag`` は JSON bool か None。
    ``executions_alert`` は ``explicit_false`` / ``not_attempted`` / None。
    """
    flag = _executions_fetched_flag(log)
    if flag is False:
        return flag, "explicit_false"
    status = log.get("status")
    if flag is not True and status in _FETCH_NOT_ATTEMPTED:
        return flag, "not_attempted"
    return flag, None


def _position_clause(closeout_status: Any) -> str:
    """建玉の案内。status の生文字列とログ本文は出さない。"""
    if closeout_status in _STEP3_OPEN_RISK:
        return (
            "建玉の対応は、4:30 の Step3 強通知と緊急決済ラダーを優先する。"
            "この取得通知はそれを止めない。\n"
        )
    if closeout_status == "ok":
        return "この通知は建玉の決済手順ではない。緊急決済ラダーへは誘導しない。\n"
    if closeout_status in {"step1_failed", "step2_failed"}:
        return (
            "建玉の対応は 4:30 の失敗通知を優先する。"
            "損切り（SL）が消えたとは断言しない。\n"
        )
    if closeout_status == "market_closed":
        return "閉場時は次回 cron で再試行する既存の契約のままである。明細回収のための再 invoke はしない。\n"
    return ""


def _recovery_limits() -> str:
    return (
        "GET /private/v1/latestExecutions は直近1日分で、その境界は未確認である。"
        "できるだけ早く読む。\n"
        "この通知を理由に明細を回収するために、再 invoke しない。"
        "ロックを削除しない。既存 S3 を上書きしない。"
        "cancel-first も発注 POST も打たない。\n"
        "結果は既存の logs/closeout と DynamoDB の外へ保存する。"
        "専用の回収コマンドも、既存 S3 へ安全に書き戻す手段も無い。\n"
    )


def _alert_executions_unfetched(
    *,
    today: date,
    prev_trade_date: date,
    closeout_status: Any,
    kind: str,
) -> None:
    """取得失敗または未着手を ALERT する。診断用 status は extra だけ。"""
    trade_day = prev_trade_date.isoformat()
    run_day = today.isoformat()
    logger.error(
        "closeout_watchdog_executions_unfetched",
        extra={
            "date": run_day,
            "run_date": run_day,
            "prev_trade_date": trade_day,
            "closeout_status": closeout_status,
            "executions_alert": kind,
            "llm_tokens": 0,
        },
    )
    if kind == "explicit_false":
        headline = (
            "[AI Sniper][ALERT] 約定明細を取得できていません"
            f"（executions_fetched=false、対象取引日={trade_day}、当朝 run_date={run_day}）\n"
        )
    else:
        headline = (
            "[AI Sniper][ALERT] 約定の取得に進んでいない"
            f"（対象取引日={trade_day}、当朝 run_date={run_day}）\n"
            "executions_fetched が false だと断言しない。\n"
        )
    send_slack_alert(headline + _position_clause(closeout_status) + _recovery_limits())


def _should_run(today: date) -> tuple[bool, str]:
    """本日チェックすべきか。(判定, 理由) を返す。

    🔴 **休場日でもスキップしない。** `closeout_batch` には休場の早期 return が無く、
    EventBridge は休場を知らないので**火〜土は休場でも起動してログを書く**
    （市場が閉じていれば Step0 で failure パスに入るが、そこでも `run_date` 付きで
    S3 に保存される）。ここでスキップすると「休場の朝に trading cron だけ止まっていても
    検知しない」盲点になる。closeout がログを書く以上、watchdog も期待し続けるのが一貫する。

    日曜・月曜は closeout の cron（`TUE-SAT`）自体が無いのでスキップする。
    """
    if today.weekday() not in _CLOSEOUT_RUN_WEEKDAYS:
        return False, "closeout_not_scheduled_today"
    return True, ""


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    """手仕舞いログの有無を確認し、欠測なら Slack へ強アラートを出す。

    Returns:
        ``status``: ``ok``（ログあり）/ ``missing``（欠測・アラート済み）/
        ``skipped``（本日は対象外）/ ``error``（S3 読み取り失敗・アラート済み）。
    """
    today = _today_jst()
    run, skip_reason = _should_run(today)
    if not run:
        logger.info(
            "closeout_watchdog_skipped",
            extra={"date": today.isoformat(), "reason": skip_reason, "llm_tokens": 0},
        )
        return {
            "status": "skipped",
            "reason": skip_reason,
            "date": today.isoformat(),
            "executions_fetched": None,
            "executions_alert": None,
        }

    prev_trade_date = resolve_prev_trade_date(today)

    try:
        log = s3_logger.load_closeout_log(prev_trade_date)
    except Exception as exc:
        # 🔴 読めない＝「無い」ではないが、**確認できない**時点で沈黙してはいけない。
        # 手仕舞いが動いていない可能性を潰せないため、fail-closed でアラートする。
        logger.error(
            "closeout_watchdog_read_failed",
            extra={
                "date": today.isoformat(),
                "prev_trade_date": prev_trade_date.isoformat(),
                "error": str(exc),
                "llm_tokens": 0,
            },
        )
        send_slack_alert(
            f"[AI Sniper][ALERT] 手仕舞いログを確認できません"
            f"（対象取引日={prev_trade_date.isoformat()}）\n"
            f"S3 読み取りエラー: {exc}\n"
            f"手仕舞いが実行されたか手動で確認してください。"
        )
        return {
            "status": "error",
            "reason": "s3_read_failed",
            "prev_trade_date": prev_trade_date.isoformat(),
            "executions_fetched": None,
            "executions_alert": None,
        }

    if log is not None and _is_fresh(log, today):
        # 🔴 ログがあっても「ノートまで到達した」とは限らない（#313）。
        # #311 で証跡を LLM より先に保存するようにしたため、LLM がハングして
        # Lambda が落ちても closeout ログは残る。従来の存在チェックだけだと
        # ここで "ok" を返して沈黙し、ノートが無いことに誰も気づかない。
        #
        # note_pending は closeout_batch が LLM 呼び出し前の書き込みに立てる印で、
        # 確定版の上書きで消える。残っている ＝ 2 回目に到達しなかった。
        #
        # SDK の明示タイムアウト（#311）は「LLM 開始時点で残り時間が read timeout
        # 以上ある」ときだけ先に発火する。init・cancel-first・約定取得リトライで
        # 30 秒超を使うと Lambda が先に死ぬため、この経路が残る。
        executions_fetched, executions_alert = _executions_alert_kind(log)
        closeout_status = log.get("status")
        if executions_alert is not None:
            _alert_executions_unfetched(
                today=today,
                prev_trade_date=prev_trade_date,
                closeout_status=closeout_status,
                kind=executions_alert,
            )

        if log.get("note_pending"):
            logger.warning(
                "closeout_watchdog_note_pending",
                extra={
                    "date": today.isoformat(),
                    "prev_trade_date": prev_trade_date.isoformat(),
                    "closeout_status": closeout_status,
                    "llm_tokens": 0,
                },
            )
            # 証跡は無事なので ALERT ではなく WARN。緊急対応を誘発しない文面にする。
            # JSON false、または Step3 の未解消だけ、完了断定とノート再生成案内を外す。
            trade_day = prev_trade_date.isoformat()
            if executions_alert == "explicit_false":
                send_slack_alert(
                    f"[AI Sniper][WARN] トレードノートが生成されていません"
                    f"（対象取引日={trade_day}）\n"
                    "約定明細が closeout ログに保存済みかは未確認です"
                    "（executions_fetched=false）。\n"
                    "先に、同じ朝の取得失敗 ALERT と Runbook の"
                    "「約定明細を取得できていません」に従ってください。\n"
                    + _position_clause(closeout_status)
                    + "この通知を理由に明細を回収するために、再 invoke しない。"
                    "ロックを削除しない。既存 S3 を上書きしない。"
                    "cancel-first も発注 POST も打たない。\n"
                    "明細の取得を確認する前に、ノートを再生成しない。"
                )
            elif executions_alert == "not_attempted":
                send_slack_alert(
                    f"[AI Sniper][WARN] トレードノートが生成されていません"
                    f"（対象取引日={trade_day}）\n"
                    "約定の取得に進んでいない。"
                    "executions_fetched が false だと断言しない。\n"
                    "保存済み、手仕舞い完了、建玉への影響なしとは断言しない。\n"
                    + _position_clause(closeout_status)
                    + "この通知を理由に明細を回収するために、再 invoke しない。"
                    "ロックを削除しない。既存 S3 を上書きしない。"
                    "cancel-first も発注 POST も打たない。\n"
                    "明細の取得を確認する前に、ノートを再生成しない。"
                )
            elif closeout_status in _STEP3_OPEN_RISK:
                send_slack_alert(
                    f"[AI Sniper][WARN] トレードノートが生成されていません"
                    f"（対象取引日={trade_day}）\n"
                    "建玉の対応は、4:30 の Step3 強通知と緊急決済ラダーを優先する。"
                    "手仕舞い完了でも、建玉への影響なしでもない。\n"
                    "ノートの再生成より先に、そちらの通知に従う。"
                )
            else:
                send_slack_alert(
                    f"[AI Sniper][WARN] トレードノートが生成されていません"
                    f"（対象取引日={trade_day}）\n"
                    f"手仕舞い自体は完了し、約定明細・損益・exit_reasons は "
                    f"closeout ログに保存済みです（note_pending が残存）。\n"
                    f"LLM 呼び出し中に Lambda がタイムアウトした可能性があります。"
                    f"建玉・注文への影響はありません。ノート本文のみ再生成してください。"
                )
        else:
            logger.info(
                "closeout_watchdog_ok",
                extra={
                    "date": today.isoformat(),
                    "prev_trade_date": prev_trade_date.isoformat(),
                    "closeout_status": log.get("status"),
                    "llm_tokens": 0,
                },
            )
        # 手仕舞い自体は実行されているので、この watchdog の主契約としては "ok"。
        # ノート欠落は別シグナルとして note_pending で返す（status を汚さない）。
        return {
            "status": "ok",
            "prev_trade_date": prev_trade_date.isoformat(),
            "closeout_status": log.get("status"),
            "note_pending": bool(log.get("note_pending")),
            "executions_fetched": executions_fetched,
            "executions_alert": executions_alert,
        }

    # ログが無い、または当日の実行で書かれていない（古いログが残っているだけ）。
    reason = "log_missing" if log is None else "log_stale"
    stale_run_date = None if log is None else log.get("run_date")
    logger.error(
        "closeout_watchdog_missing",
        extra={
            "date": today.isoformat(),
            "prev_trade_date": prev_trade_date.isoformat(),
            "reason": reason,
            "stale_run_date": stale_run_date,
            "llm_tokens": 0,
        },
    )
    detail = (
        "ログが存在しません。"
        if log is None
        else f"ログはありますが実行日が古いです（run_date={stale_run_date}）。"
    )
    send_slack_alert(
        f"[AI Sniper][ALERT] 🚨 手仕舞いが実行されていない可能性があります"
        f"（対象取引日={prev_trade_date.isoformat()}）\n"
        f"{detail}\n"
        f"確認: ①EventBridge Scheduler が ENABLED か ②closeout Lambda のログ "
        f"③建玉が残っていないか。\n"
        f"建玉が残っている場合は緊急決済ラダー（docs/runbook/README.md）で決済してください。"
    )
    return {
        "status": "missing",
        "reason": reason,
        "prev_trade_date": prev_trade_date.isoformat(),
        "executions_fetched": None,
        "executions_alert": None,
    }
