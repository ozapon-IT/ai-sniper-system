"""手仕舞いバッチ Lambda ハンドラー（§6.2）。

翌 AM 4:30 JST（火〜土・単一 cron）に起動し、
キャンセル・ファーストシーケンス → 約定取得 → トレードノート生成 → 永続化 →
ダッシュボードスナップショット保存 を実行する（E13→E14→E15 結線）。

CLI エントリポイント:
  python -m ai_sniper.batches closeout --mode weekday
  python -m ai_sniper.batches closeout --mode friday
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from backend.src.alerting.slack_alert import send_slack_alert
from backend.src.batch.cancel_first import (
    CancelFirstResult,
    list_open_position_ids,
    run_cancel_first,
)
from backend.src.batch.lock_acquire import (
    LockFailureKind,
    classify_lock_failure,
    notify_lock_release_failed,
    notify_lock_unavailable,
)
from backend.src.clients.gmo_client import GmoFxClient
from backend.src.lambda_auth import get_gmo_credentials, reset_token_cache
from backend.src.llm.trade_note import (
    CancelFirstSnapshot,
    CloseoutEvidence,
    ExecutionFetch,
    SkipIndicator,
    TradeNoteInput,
    TradeNoteResult,
    TradePlan,
    execution_to_payload,
    exit_reasons_for,
    fetch_execution_window,
    fetch_weekly_executions,
    generate_trade_note,
    jst_date_label,
    summarize_pnl,
    weekly_trade_dates,
)
from backend.src.logging_config import configure_logging
from backend.src.persistence import dynamodb_store, s3_logger
from backend.src.persistence.dynamodb_store import LockReleaseOutcome
from config.market_calendar import is_market_holiday

configure_logging()

logger = logging.getLogger(__name__)

CloseoutMode = Literal["weekday", "friday"]


def _parse_closeout_mode(event: dict[str, Any]) -> CloseoutMode:
    """event["mode"] を検証して CloseoutMode を返す（#210 L-6）。

    省略時は実行日が土曜なら "friday"（週次総括）、それ以外は "weekday"。
    不正値は ValueError（手動 invoke のタイプミスを weekday 分岐に化けさせない）。
    """
    if "mode" not in event:
        return "friday" if _get_today_jst().weekday() == 5 else "weekday"
    raw = event["mode"]
    if raw not in ("weekday", "friday"):
        raise ValueError(f"invalid closeout mode: {raw!r}")
    if raw == "weekday":
        return "weekday"
    return "friday"


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """手仕舞いバッチエントリポイント（§6.2）。

    EventBridge の event に "mode" キーが含まれる場合はそれを使用する。
    省略時は実行日が土曜なら "friday"（週次総括）、それ以外は "weekday" と判定する。
    """
    reset_token_cache()
    try:
        mode = _parse_closeout_mode(event)
    except ValueError as exc:
        logger.error(
            "closeout_invalid_mode",
            extra={"error": str(exc), "event_mode": event.get("mode"), "llm_tokens": None},
        )
        return {"status": "error", "reason": "invalid_mode", "detail": str(exc)}
    return _run(mode=mode)


def _snapshot_cancel_first(
    result: CancelFirstResult,
    *,
    step3_error: str | None,
    cancel_skipped: bool,
) -> CancelFirstSnapshot | None:
    """S3 closeout payload と同じ None / 空 / ID 規則で生結果を運ぶ（#346）。

    ``cancel_skipped``（#226 note-only）は材料なし（``None``）。空 tuple にすると
    「対象なし」と書かれ、cancel-first を走らせていない事実と食い違う。
    ``step3_error`` 時の step1/step2 は S3 と同様 ``None``（不明）。空にすると
    「取り消すものが無かった」に化ける。
    """
    if cancel_skipped:
        return None
    return CancelFirstSnapshot(
        step1_cancelled=None if step3_error else tuple(str(x) for x in result.step1_cancelled_ids),
        step2_cancelled=None if step3_error else tuple(str(x) for x in result.step2_cancelled_ids),
        step3_closed=tuple(str(x) for x in result.step3_closed_ids),
        step3_residual=tuple(str(x) for x in result.step3_residual_ids),
    )


def _run(mode: CloseoutMode = "weekday") -> dict[str, Any]:
    """手仕舞いバッチ本体（§6.2 E13→E14→E15 結線）。"""
    # #339: フェーズ別所要時間の計測。壁時計ではなく monotonic を使う（NTP 補正で
    # 負の差分を出さないため）。この invocation の予算構造は下記のとおり:
    #   Lambda timeout 120 秒 = init + [cancel-first + 約定取得 + LLM + 永続化]
    # LLM 1 試行は SDK read timeout 90 秒（trade_note._REQUEST_TIMEOUT）。
    # 529 型の再試行（max_retries=1・retry-after 数秒）は 120 秒予算に収まる。
    # ハング×(retries+1) は 90×2=180 秒で 120 を超え得る。そのときは Lambda が
    # 先に落ち、#311 の note_pending と #313 watchdog に頼る（#351）。
    # **「非LLM < 30s なら SDK が必ず先に諦める」はハング+リトライでは不成立**。
    # Duration の絶対値より、529 型は non_llm_seconds、ハング死は note_pending を見る。
    _t_run_start = time.monotonic()
    symbol = os.environ.get("TRADE_SYMBOL", "USD_JPY")
    today = _get_today_jst()
    now_jst = _get_now_jst()

    # 前確定取引日を1回だけ解決し、判断ログ・orderId・execution 取得に共通で使う（§6 AGENTS.md #6）
    prev_trade_date = _resolve_prev_trade_date(today)

    # 監査証跡用メタデータ（§5.2 構造化ログ）
    _ts = now_jst.isoformat()
    _ts_ms = int(now_jst.timestamp() * 1000)

    logger.info(
        "closeout_start",
        extra={
            "mode": mode,
            "symbol": symbol,
            "date": today.isoformat(),
            "prev_trade_date": prev_trade_date.isoformat(),
            "timestamp": _ts,
            "timestamp_ms": _ts_ms,
            "llm_tokens": None,
        },
    )

    # §6 / #208: closeout 冪等ロック（対象取引日キーで原子的に獲得）。
    # M-2: 休場明けに prev_trade_date が処理済み取引日へ解決されても、既存ロックが
    #      skip させるため確定済み税務証跡（notes/logs の同日キー）を上書きしない。
    # M-3: EventBridge Scheduler の at-least-once 重複配信も同一キーで skip。
    # #213: 「確定した既存ロック（処理済み・並走中）」と「バックエンド障害」を区別する。
    lock_outcome = dynamodb_store.acquire_lock_outcome(
        today=prev_trade_date, job_type=dynamodb_store.JOB_CLOSEOUT
    )
    lock_failure = classify_lock_failure(lock_outcome)
    if lock_failure == LockFailureKind.LOCK_EXISTS:
        # 確定した既存ロック → 冪等 skip（M-2/M-3）。S3 には何も書かない。
        logger.warning(
            "closeout_lock_exists",
            extra={
                "mode": mode,
                "date": today.isoformat(),
                "prev_trade_date": prev_trade_date.isoformat(),
                "timestamp": _ts,
                "timestamp_ms": _ts_ms,
                "llm_tokens": None,
            },
        )
        return {
            "status": "skipped",
            "reason": "closeout_lock_exists",
            "mode": mode,
            "prev_trade_date": prev_trade_date.isoformat(),
        }

    if lock_failure == LockFailureKind.UNAVAILABLE:
        # バックエンド障害（DDB 例外・I/O）でロックを獲得できず。closeout_daily は
        # retry=0 のため手仕舞いが止まり建玉が次回 cron まで残り得る（§2）。
        # 冪等 skip に潰さず失敗扱いとし、人間へ強アラートで通知する。
        # S3 には書かない（処理済み取引日の確定証跡を上書きしないため M-2 完全保護）。
        notify_lock_unavailable(
            log_event="closeout_lock_unavailable",
            log_extra={
                "mode": mode,
                "date": today.isoformat(),
                "prev_trade_date": prev_trade_date.isoformat(),
                "timestamp": _ts,
                "timestamp_ms": _ts_ms,
                "llm_tokens": None,
            },
            slack_message=(
                f"[AI Sniper][ALERT] 手仕舞いロック取得失敗（DynamoDB 障害の疑い）"
                f"（mode={mode} 対象取引日={prev_trade_date.isoformat()}）\n"
                f"手仕舞いを実行できず建玉が残っている可能性があります。次回 cron で再試行。"
            ),
        )
        return {
            "status": "lock_unavailable",
            "reason": "lock_backend_error",
            "mode": mode,
            "prev_trade_date": prev_trade_date.isoformat(),
        }

    api_key, api_secret = get_gmo_credentials()

    with GmoFxClient(api_key=api_key, api_secret=api_secret) as client:
        # ------------------------------------------------------------------
        # E13: キャンセル・ファースト手仕舞い
        # ------------------------------------------------------------------
        # #207: cancel-first の例外をここで捕捉する。
        # Step0/Step1/Step2 の失敗は result のフラグで返るため、ここへ来る例外は実質
        # **Step3（建玉取得 or closeOrder）の失敗**である。従来は未捕捉で Lambda ごと
        # エラー終了しており、Step2 で SL/TP を消した直後の建玉が
        # 「無防備 かつ 無記録」のまま翌 4:30 まで放置されていた。
        #
        # 🔴 捕捉は **closeout_batch 側でのみ**行うこと。run_cancel_first を非送出化すると
        # 同じ関数を呼ぶ kill switch（backend/src/kill_switch/handler.py）が
        # GmoApiError.message_codes による fail-closed 判定（HTTP 500）を失い、
        # 決済できていないのに「成功」を返す（Issue #136 の設計を壊す）。
        step3_error: str | None = None
        at_risk_position_ids: list[str] = []
        _t_cancel_start = time.monotonic()
        try:
            result = run_cancel_first(client=client, symbol=symbol)
        except Exception as exc:
            step3_error = str(exc)
            # 実際に残っている建玉を取り直す。「決済しようとした一覧」ではなく
            # 「いま残っている一覧」を人に伝えるため（一部チャンクは成功し得る）。
            at_risk_position_ids = _probe_open_position_ids(client, symbol)
            _notify_step3_failure(
                reason="close_failed",
                mode=mode,
                prev_trade_date=prev_trade_date,
                error=step3_error,
                at_risk_position_ids=at_risk_position_ids,
                ts=_ts,
                ts_ms=_ts_ms,
            )
            # 空の結果で以降を継続する。**early return しない**のは、この時点で当日の
            # 約定が実在するのに打ち切ると約定明細を取得できず、latestExecutions は
            # 直近1日しか遡れないため税務証跡が永久欠損するため（#245 と同型）。
            result = CancelFirstResult()
        _cancel_first_seconds = round(time.monotonic() - _t_cancel_start, 3)

        # #226: friday + カレンダー休場日（full_close）の閉場のみ note-only。
        # 通常土曜の API 障害等は従来どおり failure（ロック解放・同朝再実行可）。
        # 休場判定は実行日（土曜）ではなく直前暦日（通常=金曜）を優先（Good Friday 等）。
        cancel_skipped = False
        if result.market_closed:
            _close_calendar_day = today - timedelta(days=1)
            _holiday_close = is_market_holiday(_close_calendar_day) or is_market_holiday(
                today
            )
            if mode != "friday" or not _holiday_close:
                return _handle_closeout_failure(
                    "market_closed",
                    mode=mode,
                    today=today,
                    prev_trade_date=prev_trade_date,
                    ts=_ts,
                    ts_ms=_ts_ms,
                    retry_next=result.retry_next,
                    cancel_first_seconds=_cancel_first_seconds,
                )
            cancel_skipped = True
            logger.warning(
                "closeout_friday_market_closed_note_only",
                extra={
                    "mode": mode,
                    "date": today.isoformat(),
                    "prev_trade_date": prev_trade_date.isoformat(),
                    "cancel_skipped": True,
                    "market_closed": True,
                    "market_holiday": True,
                    "timestamp": _ts,
                    "timestamp_ms": _ts_ms,
                    "llm_tokens": None,
                },
            )
            send_slack_alert(
                f"[AI Sniper][ALERT] 手仕舞いバッチ: friday モードで休場日閉場"
                f"（対象取引日={prev_trade_date.isoformat()}）\n"
                f"cancel-first をスキップし週次ノートのみ生成します。"
                f"建玉・残注文の決済は次回営業日の weekday 手仕舞いに委ねます（#226）。"
            )

        if result.step1_failed:
            return _handle_closeout_failure(
                "step1_failed",
                mode=mode,
                today=today,
                prev_trade_date=prev_trade_date,
                ts=_ts,
                ts_ms=_ts_ms,
                cancel_first_seconds=_cancel_first_seconds,
            )

        if result.step2_failed:
            return _handle_closeout_failure(
                "step2_failed",
                mode=mode,
                today=today,
                prev_trade_date=prev_trade_date,
                ts=_ts,
                ts_ms=_ts_ms,
                cancel_first_seconds=_cancel_first_seconds,
            )

        # #207: 例外にならなくても建玉が無防備に残りうる 2 つの状態を人へ通知する。
        # payload に出すだけでは夜間に誰も気付けない。
        if result.step3_verification_failed:
            at_risk_position_ids = _probe_open_position_ids(client, symbol)
            _notify_step3_failure(
                reason="verification_failed",
                mode=mode,
                prev_trade_date=prev_trade_date,
                error="決済後の建玉再取得に失敗し、クローズ結果を検証できなかった",
                at_risk_position_ids=at_risk_position_ids,
                ts=_ts,
                ts_ms=_ts_ms,
            )
        elif result.step3_residual_ids:
            # L1 の短ポーリング後も残った建玉のみ届く（一時的な約定待ちは除外済み）
            at_risk_position_ids = list(result.step3_residual_ids)
            _notify_step3_failure(
                reason="residual",
                mode=mode,
                prev_trade_date=prev_trade_date,
                error=(
                    "closeOrder は受理されたが、ポーリング後も建玉が残存"
                    f"（{len(result.step3_residual_ids)} 件）"
                ),
                at_risk_position_ids=at_risk_position_ids,
                ts=_ts,
                ts_ms=_ts_ms,
            )

        logger.info(
            "closeout_completed",
            extra={
                "mode": mode,
                "step1": result.step1_cancelled_ids,
                "step2": result.step2_cancelled_ids,
                "step3": result.step3_closed_ids,
                "step3_failed": step3_error is not None,
                "step3_verification_failed": result.step3_verification_failed,
                "cancel_skipped": cancel_skipped,
                # #339: 金が動く段の実測は以降で落ちても残す。cancel-first 自身が
                # 失敗した回は early return でここへ来ないため、_handle_closeout_failure
                # 側でも同じキーを出している。全体の内訳は closeout_phase_elapsed。
                "cancel_first_seconds": _cancel_first_seconds,
                "timestamp": _ts,
                "timestamp_ms": _ts_ms,
                "llm_tokens": None,
            },
        )

        # ------------------------------------------------------------------
        # E14: 約定取得 → トレードノート生成
        # ------------------------------------------------------------------
        # 前確定取引日の判断バッチ rationale を取得（friday モードも前取引日 = 金曜）
        # RuntimeError（読み込み障害）はフォールバックで継続し、payload に記録する
        decision_log_error: str | None = None
        try:
            decision_log = s3_logger.load_decision_log(prev_trade_date)
        except RuntimeError as exc:
            decision_log_error = str(exc)
            decision_log = None
            logger.error(
                "closeout_decision_log_load_failed",
                extra={
                    "date": prev_trade_date.isoformat(),
                    "error": decision_log_error,
                    "timestamp": _ts,
                    "timestamp_ms": _ts_ms,
                    "llm_tokens": None,
                },
            )
        decision_plan = (decision_log or {}).get("rationale", "（判断バッチプランなし）")
        # #249: 計画値を**構造化して**渡す。rationale（散文）だけだと LLM が文章中の
        # ピボット価格を SL と誤認する（2026-08-06 のノートで実際に発生）。
        # #251: friday モードは約定が 5 営業日分あるため、計画値も取引日ごとに揃える。
        if mode == "friday":
            trade_plans = _build_weekly_trade_plans(now_jst)
        else:
            trade_plans = (_build_trade_plan(decision_log, trade_date=prev_trade_date),)

        # 前確定取引日の orderId を取得（latestExecutions 補完に使う）
        order_id = dynamodb_store.get_order_id(today=prev_trade_date)
        order_ids = [order_id] if order_id else None

        note_result = None
        fetched = ExecutionFetch()
        # 「取得に成功して 0 件」と「取得できなかった」を区別する。前者だけを S3 に
        # 約定一覧として書く。取得失敗時に executions:[] を書くと、土曜の週次集約が
        # それを「その日は約定なし」と読み、事実と反対の週次ノートを生む（2026-08-05 と同型）。
        executions_fetched = False
        note_error: str | None = None
        # #280: 建玉がどう閉じたかの一次証拠。cancel-first が走っていない場合
        # （Step3 失敗で結果が空に差し替わった / #226 の note-only スキップ）は
        # **空集合ではなく None** にする。空集合は「取り消す対象が無かった」を意味し、
        # そのまま渡すと「OCO が発火した」と誤断定される。
        #
        # 🔴 step3_verification_failed も同型の穴。決済後の建玉再取得に失敗した回は
        # cancel_first が fail-closed で step3_closed_ids=[] を返すため、空集合を
        # そのまま渡すと「cancel-first 未介入」＝ OCO 発火に化ける。step2 の取消は
        # 実際に成功しているので証拠としては残し、フラグで OCO 分岐だけを封じる。
        run_evidence: CloseoutEvidence | None = (
            None
            if (step3_error is not None or cancel_skipped)
            else CloseoutEvidence(
                cancelled_root_order_ids=frozenset(result.step2_cancelled_ids),
                closed_position_ids=frozenset(result.step3_closed_ids),
                verification_failed=result.step3_verification_failed,
            )
        )
        exit_reasons: dict[str, str] = {}
        # 🔴 LLM を呼ぶ前にここまでで確定させる（税務証跡 §5.2）。
        # note_input が組めた ＝ 明細・損益・exit_reasons は揃っている、という意味。
        note_input: TradeNoteInput | None = None
        _t_fetch_start = time.monotonic()
        try:
            if mode == "friday":
                fetched = fetch_weekly_executions(
                    client=client,
                    now_jst=now_jst,
                    symbol=symbol,
                    order_ids=order_ids,
                    prev_trade_date=prev_trade_date,
                    live_evidence=run_evidence,
                )
                # friday は日をまたいで併合済みの証拠を使う（過去日は永続化ログ由来）
                evidence = fetched.evidence
            else:
                fetched = fetch_execution_window(
                    client=client,
                    now_jst=now_jst,
                    symbol=symbol,
                    order_ids=order_ids,
                    prev_trade_date=prev_trade_date,
                )
                evidence = run_evidence
            executions_fetched = True

            note_input = TradeNoteInput(
                decision_plan=decision_plan,
                executions=fetched.executions,
                now_jst=now_jst,
                is_friday_mode=(mode == "friday"),
                data_gaps=fetched.gaps,
                plans=trade_plans,
                closeout_evidence=evidence,
                cancel_first=_snapshot_cancel_first(
                    result, step3_error=step3_error, cancel_skipped=cancel_skipped
                ),
            )
            # ノートと同じ判定を S3 にも残す（税務証跡 §5.2）。ノート本文だけだと
            # 後から「なぜその理由になったか」を機械的に検証できない。
            exit_reasons = {
                s.position_id: reason for s, reason in exit_reasons_for(note_input)
            }
        except Exception as exc:
            note_input = None
            note_error = str(exc)
            logger.error(
                "closeout_execution_fetch_failed",
                extra={
                    "error": note_error,
                    "timestamp": _ts,
                    "timestamp_ms": _ts_ms,
                    "llm_tokens": None,
                },
            )
        # 成功・失敗の両方でここを通る（失敗側は #245 の時間予算 20 秒を使い切った
        # ケースを含むため、むしろ失敗側の実測が要る）。
        _fetch_seconds = round(time.monotonic() - _t_fetch_start, 3)

        # ------------------------------------------------------------------
        # E15: 永続化 — closeout_log / trade_note
        # S3 キーは対象取引日（prev_trade_date）を基準にする（§6 AGENTS.md #6）
        # ------------------------------------------------------------------
        def _build_closeout_payload(
            *, note: TradeNoteResult | None, note_pending: bool
        ) -> dict[str, Any]:
            """closeout ログの payload を組む。

            LLM 呼び出しの **前後で 2 回** 呼ぶ。ノート由来の 4 フィールド
            （note_generated / note_complete / llm_total_tokens / note_error）以外は
            呼び出し時点ですべて確定しているため、1 回目でも証跡としては完全になる。
            """
            _tokens = (note.input_tokens + note.output_tokens) if note else 0
            payload: dict[str, Any] = {
                # #207: 決済に失敗／結果を確認できなかった日を "ok" で成功扱いしない。
                # 無防備な建玉が残っている可能性があるため監査上も失敗として残す。
                "status": _closeout_status(
                    step3_error, result.step3_verification_failed, result.step3_residual_ids
                ),
                "mode": mode,
                "date": prev_trade_date.isoformat(),   # 対象取引日
                "run_date": today.isoformat(),          # バッチ実行日（監査用）
                "prev_trade_date": prev_trade_date.isoformat(),
                "timestamp": _ts,
                "timestamp_ms": _ts_ms,
                # #207 レビュー指摘 2: cancel-first が例外送出した場合、Step1/Step2 が
                # どこまで進んだかは**分からない**。空リストを書くと「1件も取り消していない」と
                # 読めるが、実際には Step2 が SL/TP を取消済みの可能性が高い（だから無防備）。
                # 0 件と「不明」を区別する（executions_count と同じ方針）。
                "step1_cancelled": None if step3_error else result.step1_cancelled_ids,
                "step2_cancelled": None if step3_error else result.step2_cancelled_ids,
                "step3_closed": result.step3_closed_ids,
                # Step3 の状態を監査可能にする（従来は payload に出ていなかった）
                "step3_residual": result.step3_residual_ids,
                "step3_verification_failed": result.step3_verification_failed,
                # 取得できなかった場合は 0 ではなく null。人が count だけ見て
                # 「その日は約定 0 件だった」と読むのを防ぐ（0 と「不明」は別物）。
                "executions_count": len(fetched.executions) if executions_fetched else None,
                "executions_fetched": executions_fetched,
                "note_generated": note is not None,
                "note_complete": note.is_complete if note else False,
                "llm_total_tokens": _tokens,
            }
            # #334: 他日の計画値が混入した疑い。ノート本文は S3 の notes/ 側にあるが、
            # 監査は closeout ログを見るため、検知の事実はこちらにも残す（§5.2）。
            if note is not None and note.cross_day_warnings:
                payload["note_cross_day_warnings"] = list(note.cross_day_warnings)
            # #342: 規律違反フレーズ。監査は closeout ログを見るため証跡側にも残す（§5.2）。
            if note is not None and note.discipline_warnings:
                payload["note_discipline_warnings"] = list(note.discipline_warnings)
            if note_pending:
                # 🔴 このキーが残っている ＝ 証跡は書けたがノート生成の後の
                # 上書きに到達しなかった（LLM ハング → Lambda タイムアウト等）。
                # 明細・損益・exit_reasons は下で書かれるため証跡としては完全。
                payload["note_pending"] = True
            if executions_fetched:
                # 約定明細と損益は税務証跡そのもの（§5.2）であり、土曜の週次集約の入力でもある。
                # 取得できた場合のみ書く（上の executions_fetched のコメント参照）。
                _pnl = summarize_pnl(fetched.executions)
                payload["executions"] = [
                    execution_to_payload(e) for e in fetched.executions
                ]
                payload["pnl"] = {
                    "open_count": _pnl.open_count,
                    "close_count": _pnl.close_count,
                    "loss_gain_total": str(_pnl.loss_gain_total),
                    "fee_total": str(_pnl.fee_total),
                    "settled_swap_total": str(_pnl.settled_swap_total),
                    "realized_total": str(_pnl.realized_total),
                    "unparsed_fields": list(_pnl.unparsed_fields),
                }
                # #280: 建玉ごとの決済理由。ノート本文と**同じ判定**を機械可読で残し、
                # 「時間切れ手仕舞い」と「OCO 発火」を後から突合できるようにする（§5.2）。
                # E18 の観測対象 #1（SL の実約定）はこの値で検証する。
                payload["exit_reasons"] = exit_reasons
            if fetched.gaps:
                payload["execution_data_gaps"] = list(fetched.gaps)
            if step3_error:
                payload["step3_error"] = step3_error
            # 無防備な建玉は失敗経路（例外）だけでなく verification_failed / residual でも生じる
            if at_risk_position_ids:
                payload["step3_at_risk_position_ids"] = at_risk_position_ids
            if cancel_skipped:
                payload["cancel_skipped"] = True
                payload["market_closed"] = True
            if note_error:
                payload["note_error"] = note_error
            if decision_log_error:
                payload["decision_log_error"] = decision_log_error
            return payload

        def _save_closeout_payload(payload: dict[str, Any]) -> bool:
            """夕方ログを対象取引日キーへ保存する（土曜実行でも金曜日付）。

            save_log() 内部で失敗時は Slack アラート送信済み（§1.5）。
            """
            if s3_logger.save_closeout_log(
                payload, today=prev_trade_date, raise_on_failure=False
            ):
                return True
            logger.error(
                "closeout_log_save_failed",
                extra={
                    "date": prev_trade_date.isoformat(),
                    "note_pending": bool(payload.get("note_pending")),
                    "timestamp": _ts,
                    "timestamp_ms": _ts_ms,
                    "llm_tokens": payload.get("llm_total_tokens"),
                },
            )
            return False

        # ------------------------------------------------------------------
        # 🔴 証跡を LLM より先に確定させる（§5.2）
        #
        # ノート生成は Anthropic API 呼び出しで、**SDK の既定タイムアウトは 600 秒**と
        # Lambda（120 秒）より長かった。ここでハングすると Lambda が先に落ち、
        # 従来は closeout ログが 1 行も残らなかった。latestExecutions は直近 1 日しか
        # 遡れないため翌日のリトライでは回収できず、明細は**永久欠損**する
        # （.claude/rules/gmo-api.md / #245）。
        #
        # 現在は #311 で SDK 側に 90 秒を明示しており（trade_note._REQUEST_TIMEOUT）、
        # 529 型の再試行（max_retries=1）は 120 秒予算に収まる。ハング×(retries+1) は
        # 90×2 で 120 を超え、Lambda が先に死に得る（#351）。「非LLM < 30s なら
        # SDK が必ず先に諦める」とは書かない。
        # それでもこの順序は緩めない: ハング+リトライでも GMO ハングでも、証跡を先に
        # 確定させておく方が安い（#339 実測・非LLM 2.9〜5.1 秒。ハング死は
        # note_pending + #313 watchdog）。
        #
        # ノート本文は失っても再生成できるが、約定明細は取り直せない。
        # 先に書いておけば LLM が何をしようと証跡は残る。
        # ------------------------------------------------------------------
        # None のままなら「LLM を呼んでいない」。0.0 と区別する（executions_count が
        # 0 と null を区別しているのと同じ規律）。
        _llm_seconds: float | None = None
        if note_input is not None:
            _save_closeout_payload(
                _build_closeout_payload(note=None, note_pending=True)
            )

            _t_llm_start = time.monotonic()
            try:
                note_result = generate_trade_note(note_input)
            except Exception as exc:
                note_error = str(exc)
                logger.error(
                    "closeout_note_generation_failed",
                    extra={
                        "error": note_error,
                        "timestamp": _ts,
                        "timestamp_ms": _ts_ms,
                        "llm_tokens": None,
                    },
                )
            finally:
                # 例外側（SDK read timeout 90 秒で諦めた場合を含む）も計測する。
                # 「LLM が予算をどれだけ食ったか」は成功時より失敗時に知りたい。
                _llm_seconds = round(time.monotonic() - _t_llm_start, 3)

        # ノートが生成できても本文が打ち切られていることがある（#309）。
        # 🔴 ここでは記録だけ。Slack 通知は S3 保存の**後**に送る。証跡より先に
        # 外向き I/O を挟むと「通知は出たが明細は残っていない」を作る（#245 と同型）。
        # 構造化ログは I/O を伴わずこの時点で残せるため、後段で落ちても
        # 打ち切りの事実が CloudWatch に残るようここで出す。
        # #334: 取り違えの検知も同じ理由でここに残す（外向き I/O の前）。
        if note_result is not None and note_result.cross_day_warnings:
            logger.warning(
                "closeout_note_cross_day_values",
                extra={
                    "mode": mode,
                    "prev_trade_date": prev_trade_date.isoformat(),
                    "count": len(note_result.cross_day_warnings),
                    "details": list(note_result.cross_day_warnings),
                    "timestamp": _ts,
                    "timestamp_ms": _ts_ms,
                    "llm_tokens": None,
                },
            )
        if note_result is not None and note_result.discipline_warnings:
            logger.warning(
                "closeout_note_discipline_violations",
                extra={
                    "mode": mode,
                    "prev_trade_date": prev_trade_date.isoformat(),
                    "count": len(note_result.discipline_warnings),
                    "details": list(note_result.discipline_warnings),
                    "timestamp": _ts,
                    "timestamp_ms": _ts_ms,
                    "llm_tokens": None,
                },
            )

        if note_result is not None and not note_result.is_complete:
            logger.warning(
                "closeout_note_incomplete",
                extra={
                    "mode": mode,
                    "output_tokens": note_result.output_tokens,
                    "timestamp": _ts,
                    "timestamp_ms": _ts_ms,
                    "llm_tokens": None,
                },
            )

        note_tokens = (
            (note_result.input_tokens + note_result.output_tokens) if note_result else 0
        )

        # ノート由来のフィールドを載せて確定版で上書きする。
        # note_input が None（取得失敗）の場合はここが唯一の書き込みになる。
        closeout_payload = _build_closeout_payload(note=note_result, note_pending=False)
        _save_closeout_payload(closeout_payload)

        if note_result is not None:
            note_payload: dict[str, Any] = {
                "date": prev_trade_date.isoformat(),    # 対象取引日
                "run_date": today.isoformat(),           # バッチ実行日（監査用）
                "mode": mode,
                "note_markdown": note_result.note_markdown,
                "model": note_result.model,
                "input_tokens": note_result.input_tokens,
                "output_tokens": note_result.output_tokens,
                "is_complete": note_result.is_complete,
                "executions_count": len(fetched.executions),
                "timestamp": _ts,
                "timestamp_ms": _ts_ms,
            }
            # ノートも対象取引日キーに保存
            if not s3_logger.save_trade_note(note_payload, today=prev_trade_date):
                logger.error(
                    "trade_note_save_failed",
                    extra={
                        "date": prev_trade_date.isoformat(),
                        "timestamp": _ts,
                        "timestamp_ms": _ts_ms,
                        "llm_tokens": note_tokens,
                    },
                )

            # ダッシュボードはノート生成成功時のみ更新（失敗時は既存 note を保護）
            _upsert_dashboard(
                prev_trade_date=prev_trade_date,
                note_markdown=note_result.note_markdown,
                now_jst=now_jst,
                ts=_ts,
                ts_ms=_ts_ms,
                llm_tokens=note_tokens,
            )

            # #309: 打ち切り検知の Slack 通知。S3 の note_complete: false は
            # 誰も見に行かないため通知が唯一の検知手段になる。
            # 🔴 永続化を**すべて終えてから**送る。send_slack_alert は最大5秒
            # ブロックする外向き I/O で、証跡より先に置くと「通知は出たが明細は
            # 残っていない」状態を作り得る（#245 と同型）。文面の「保存済み」も
            # この位置でようやく事実になる。
            # 発注・手仕舞いには影響しない後処理なので ALERT ではなく WARN 文面。
            if not note_result.is_complete:
                send_slack_alert(
                    f"[AI Sniper][WARN] トレードノートが不完全です"
                    f"（mode={mode} 対象取引日={prev_trade_date.isoformat()}）\n"
                    f"本文が途中で切れているか生成に失敗しています"
                    f"（output_tokens={note_result.output_tokens}）。\n"
                    f"約定明細・損益・exit_reasons は closeout ログに保存済みで、"
                    f"発注・手仕舞いへの影響はありません。ノート本文のみ再生成を検討してください。"
                )

            # #334: 他日の計画値の混入。ノートは §6.2 の改善サイクルの入力なので、
            # 誤った計画値のまま読まれると振り返りの前提が狂う。
            # 🔴 ノート末尾の確定表（Python 描画）が正であり、そちらは汚染されない。
            if note_result.cross_day_warnings:
                _detail = "\n".join(f"- {w}" for w in note_result.cross_day_warnings)
                send_slack_alert(
                    f"[AI Sniper][WARN] 週次ノートに他日の計画値が混入した疑い"
                    f"（mode={mode} 対象取引日={prev_trade_date.isoformat()}）\n"
                    f"{_detail}\n"
                    f"ノート末尾の「取引日ごとの計画値」表は Python が描画しており"
                    f"**そちらが正**です。散文側の数値を信用しないでください。\n"
                    f"約定明細・損益は closeout ログに保存済みで、発注・手仕舞いへの影響はありません。"
                )

            # #342: 戦略パラメータの見直し示唆。ノートは §6.2 の改善サイクルの入力なので、
            # 読んだ人が示唆に引きずられて後知恵でパラメータを変えるリスクがある。
            # 🔴 生成は正常終了しており is_complete は下げない（WARN のみ）。
            if note_result.discipline_warnings:
                _detail = "\n".join(f"- {w}" for w in note_result.discipline_warnings)
                send_slack_alert(
                    f"[AI Sniper][WARN] トレードノートに戦略見直しの示唆が出た疑い"
                    f"（mode={mode} 対象取引日={prev_trade_date.isoformat()}）\n"
                    f"{_detail}\n"
                    f"ノートは運用ログであり、戦略パラメータの変更提案は人間レビュー専用です"
                    f"（§6.2）。示唆に従って閾値を変えないでください。\n"
                    f"約定明細・損益は closeout ログに保存済みで、発注・手仕舞いへの影響はありません。"
                )
        else:
            logger.warning(
                "closeout_dashboard_note_skipped",
                extra={
                    "date": prev_trade_date.isoformat(),
                    "reason": "note_generation_failed",
                    "timestamp": _ts,
                    "timestamp_ms": _ts_ms,
                    "llm_tokens": None,
                },
            )

        # #208: ロックレコードに完了印を追記（in-flight/クラッシュとの運用上の区別）。
        # 失敗しても閉場処理自体は完了しているため戻り値には影響させない。
        lock_summary: dict[str, Any] = {
            # #207: ロックの完了印にも失敗を反映する（"ok" で塗り潰さない）。
            # ロック自体は #138 に従い保持する（cancel/close の POST を試行済みで
            # 結果が確定していないため、同一取引日での再送は危険側）。
            "status": _closeout_status(
                step3_error, result.step3_verification_failed, result.step3_residual_ids
            ),
            "mode": mode,
        }
        if cancel_skipped:
            lock_summary["cancel_skipped"] = True
            lock_summary["market_closed"] = True
        # #207 (b) L3: 「無防備な建玉」マーカー。Slack は流れるが DynamoDB は残るため、
        # 翌朝の確認や次回起動時の判断材料になる（建玉 ID と発生時刻を保持）。
        # update_summary は値を文字列化して保存するため、素直な文字列で入れる。
        if at_risk_position_ids:
            lock_summary["unprotected_position_ids"] = ",".join(at_risk_position_ids)
            lock_summary["unprotected_since"] = _ts
        if not dynamodb_store.update_summary(
            today=prev_trade_date,
            summary=lock_summary,
            job_type=dynamodb_store.JOB_CLOSEOUT,
        ):
            logger.warning(
                "closeout_lock_summary_update_failed",
                extra={
                    "date": prev_trade_date.isoformat(),
                    "timestamp": _ts,
                    "timestamp_ms": _ts_ms,
                    "llm_tokens": None,
                },
            )

        # ------------------------------------------------------------------
        # #339: 予算の内訳を 1 行で残す。
        #
        # 🔴 監視対象は Duration の絶対値ではなく、経路で分ける（#351）。
        # 529 型（短い retry-after + 再生成 1 回）は 120 秒予算に収まる。余裕は
        # **non_llm_seconds** で見る（#339 実測 2.9〜5.1 秒）。
        # ハング×(retries+1) は 90×2=180 秒で 120 を超え得る。非LLM が 30 秒未満
        # でも Lambda は死に得る。その経路は note_pending + #313 watchdog。
        #
        # ⚠️ Lambda の init（コールドスタート実測 約 0.9 秒）は _run の外なので
        # ここには含まれない。529 型の予算と突き合わせるときは足すこと。
        # 全体の実測は CloudWatch の REPORT Duration 側を見る。
        # ------------------------------------------------------------------
        _total_seconds = round(time.monotonic() - _t_run_start, 3)
        logger.info(
            "closeout_phase_elapsed",
            extra={
                "mode": mode,
                "date": prev_trade_date.isoformat(),
                "cancel_first_seconds": _cancel_first_seconds,
                "fetch_seconds": _fetch_seconds,
                "llm_seconds": _llm_seconds,
                "non_llm_seconds": round(_total_seconds - (_llm_seconds or 0.0), 3),
                "total_seconds": _total_seconds,
                "timestamp": _ts,
                "timestamp_ms": _ts_ms,
                "llm_tokens": note_tokens,
            },
        )

        return {**closeout_payload}


# ---------------------------------------------------------------------------
# 失敗系の監査ログ・強アラート（Issue #206 監査 H-1）
# ---------------------------------------------------------------------------

def _build_weekly_trade_plans(now_jst: datetime) -> tuple[TradePlan, ...]:
    """週次総括の対象取引日ごとに計画値を読み込む（#251）。

    約定の集約と**同じ日付集合**（`weekly_trade_dates`）を使う。ずれると
    「約定はあるのに計画値が無い日」が出てノートの整合が崩れる。

    🔴 1 日分の読み込み失敗で週次ノート全体を落とさない。読めなかった日は
    ``log_available=False`` として渡し、プロンプト側で「**取得できず・不明**」と
    書かせる（「その日は発注しなかった」と混同しない・#244 と同じ規律）。
    """
    plans: list[TradePlan] = []
    for d in weekly_trade_dates(now_jst):
        try:
            log = s3_logger.load_decision_log(d)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "closeout_weekly_decision_log_failed",
                extra={"date": d.isoformat(), "error": str(exc), "llm_tokens": None},
            )
            plans.append(TradePlan(trade_date=jst_date_label(d), log_available=False))
            continue
        if log is None:
            plans.append(TradePlan(trade_date=jst_date_label(d), log_available=False))
            continue
        plans.append(_build_trade_plan(log, trade_date=d))
    return tuple(plans)


def _build_trade_plan(
    decision_log: dict[str, Any] | None, *, trade_date: date | None = None
) -> TradePlan:
    """判断ログから計画値を構造化して取り出す（#249）。

    値は**文字列化して無加工で運ぶ**（再フォーマットや丸めをしない）。
    判断ログが無い / フィールド欠落でも空の TradePlan を返し、プロンプト側で
    「計画値なし・推測禁止」と明示させる。
    """
    log = decision_log or {}

    def _s(key: str) -> str:
        return _log_str(log, key)

    return TradePlan(
        direction=_s("direction"),
        order_type=_s("order_type"),
        entry_price=_s("entry_price"),
        stop_loss=_s("stop_loss"),
        take_profit=_s("take_profit"),
        lot=_s("lot"),
        sl_profile=_s("sl_profile"),
        rr_target=_s("rr_target"),
        # friday モードでは取引日ごとに 1 件並ぶ。日付（曜日つき・#254）を持たせて
        # プロンプト側でスコープを明示させる（#250 レビュー指摘 2）
        trade_date=jst_date_label(trade_date) if trade_date else "",
        status=_s("status"),
        no_entry_reason=_no_entry_reason(log),
        skip_indicators=_skip_indicators(log),
        edge_guard_details=_edge_guard_details(log),
    )


def _no_entry_reason(log: dict[str, Any]) -> str:
    """見送り理由を判断ログから取り出す（#318）。

    ``reason`` はハードルール由来の種別（``indicator_day_skip`` /
    ``no_momentum_signal_day_skip`` / ``high_vol_regime_day_skip``）。
    LLM 判断による見送りは ``reason`` が ``None`` で ``rationale`` にだけ理由が入る
    （2026-08-20 の FOMC 議事録の日が実例）。両方を拾わないと片方が欠ける。

    値は**無加工で運ぶ**（TradePlan 既存方針。再フォーマットや要約をしない）。
    """
    if _log_str(log, "status") != "no_entry":
        return ""
    return _log_str(log, "reason") or _log_str(log, "rationale")


def _skip_indicators(log: dict[str, Any]) -> tuple[SkipIndicator, ...]:
    """指標 day-skip の引き金になった経済指標を判断ログから取り出す（#333）。

    ``reason`` は ``indicator_day_skip`` という**種別**しか持たないため、これが
    無いとノートは「理由: indicator_day_skip」で止まり、**どの指標か**が落ちる。
    判断ログ（``skip_indicators``）には name / time_jst / currency が残っている。
    ``date_jst`` は #374 以降のログにだけある。無いキーは空文字のまま運ぶ。

    値は**無加工で運ぶ**（TradePlan 既存方針。整形・要約・時刻の再計算をしない）。
    """
    raw = log.get("skip_indicators")
    if not isinstance(raw, list):
        return ()
    out: list[SkipIndicator] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            SkipIndicator(
                name=_log_str(item, "name"),
                time_jst=_log_str(item, "time_jst"),
                currency=_log_str(item, "currency"),
                date_jst=_log_str(item, "date_jst"),
            )
        )
    return tuple(out)


def _edge_guard_details(log: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """エッジガード day-skip の判定値を判断ログから取り出す（#333）。

    ``reason`` の種別（G1/G2/G3）だけでは**どれくらい閾値を超えたか**が分からない。
    2026-08-18 の G2 は ``atr_pips`` 160.421 対 閾値 160.0 で差は 0.42 pips であり、
    大きく超過した日と同じ表示になってしまう。

    キーの順序は判断ログのまま保つ（G1/G2/G3 で入るキーが違うため、
    こちらで並べ替えると guard の種類が読み取りにくくなる）。
    値は**無加工で運ぶ**（丸め・単位変換をしない）。
    """
    raw = log.get("edge_guard_details")
    if not isinstance(raw, dict):
        return ()
    return tuple((str(k), _log_str(raw, k)) for k in raw)


def _log_str(log: dict[str, Any], key: str) -> str:
    """判断ログの値を文字列で取り出す（None は空文字）。

    値は**無加工で運ぶ**（TradePlan 既存方針。再フォーマットや丸めをしない）。
    """
    value = log.get(key)
    return "" if value is None else str(value)


def _closeout_status(
    step3_error: str | None, verification_failed: bool, residual_ids: list[str]
) -> str:
    """手仕舞いの結果ステータスを決める（#207）。

    「決済に失敗した」「決済できたか分からない」「受理されたが閉じていない」を区別する。
    いずれも建玉が無防備に残りうるが、人が確認すべき内容が違うため同じ値に潰さない。
    """
    if step3_error:
        return "step3_failed"
    if verification_failed:
        return "step3_unverified"
    if residual_ids:
        return "step3_residual"
    return "ok"


def _probe_open_position_ids(client: object, symbol: str) -> list[str]:
    """いま残っている建玉 ID を取り直す（#207 のアラート本文用・失敗しても例外にしない）。

    決済に失敗した直後に呼ぶ。「決済しようとした一覧」ではなく「**いま残っている一覧**」を
    人へ伝えるため（チャンク単位で一部だけ成功していることがある）。

    ここは既にエラー処理中なので、この GET が失敗しても握り潰して空リストを返す。
    ID が取れなくてもアラート自体は必ず送る（送れないほうが害が大きい）。
    """
    try:
        return list_open_position_ids(client, symbol)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "closeout_step3_at_risk_probe_failed",
            extra={"symbol": symbol, "error": str(exc), "llm_tokens": None},
        )
        return []


# Step3 が「建玉を無防備に残しうる」状態（#207）。文面を分けるのは、
# 人が取るべき行動の緊急度と確度が違うため。
#
# ``residual`` は #207 (b) L1 で**短ポーリング後も消えなかった**建玉のみを指すようになった。
# 一時的な約定待ちは L1 側で除外済みなので、ここに載せても誤報にならない
# （L1 導入前は正常運転でも発生していたため対象外にしていた）。
_STEP3_ALERT: dict[str, tuple[str, str]] = {
    "close_failed": (
        "決済に失敗しました",
        "決済 OCO（SL/TP）は Step2 で取消済みのため、この建玉は**損切りが無い状態**です。",
    ),
    "verification_failed": (
        "決済結果を確認できませんでした",
        "決済が成立したか不明です。建玉が残っている場合、"
        "決済 OCO（SL/TP）は Step2 で取消済みのため**損切りが無い状態**です。",
    ),
    "residual": (
        "決済注文は受理されたが建玉が閉じていません",
        "closeOrder は受理されましたが、数秒待っても建玉が残っています。"
        "決済 OCO（SL/TP）は Step2 で取消済みのため**損切りが無い状態**です。",
    ),
}


def _notify_step3_failure(
    *,
    reason: str,
    mode: CloseoutMode,
    prev_trade_date: date,
    error: str,
    at_risk_position_ids: list[str],
    ts: str,
    ts_ms: int,
) -> None:
    """建玉が無防備に残りうる Step3 の状態を監査ログと Slack 強アラートに落とす（#207）。

    キャンセル・ファーストは Step2 で決済 OCO（SL/TP）を**先に**消す（AGENTS.md #4）。
    その直後の Step3 が失敗（または結果を確認できない）と、建玉は **SL/TP なし**のまま
    次回起動（翌 4:30）まで残る。

    🔴 この状態は #245（約定取得失敗）とは性質が違う:
      - #245: 翌日には API にデータが無く**永久欠損**する
      - #207: 建玉は残っており**翌 closeout の Step3 で閉じられる**
    したがってアラートの目的は「永久欠損の防止」ではなく
    **無防備な時間の短縮（人が同じ朝のうちに手動決済できること）**。文面もそう書く。

    Args:
        reason: ``_STEP3_ALERT`` のキー（``close_failed`` / ``verification_failed``）。
    """
    headline, consequence = _STEP3_ALERT[reason]
    logger.error(
        "closeout_step3_failed",
        extra={
            "mode": mode,
            "reason": reason,
            "date": prev_trade_date.isoformat(),
            "error": error,
            "at_risk_position_ids": at_risk_position_ids,
            "impact": "Step2 で SL/TP を消した建玉が無防備なまま残っている可能性がある",
            "timestamp": ts,
            "timestamp_ms": ts_ms,
            "llm_tokens": None,
        },
    )
    positions_text = (
        "、".join(at_risk_position_ids) if at_risk_position_ids else "（建玉ID を特定できず）"
    )
    send_slack_alert(
        f"[AI Sniper][ALERT] 🔴 手仕舞い Step3: {headline}"
        f"（mode={mode} 対象取引日={prev_trade_date.isoformat()}）\n"
        f"詳細: {error}\n"
        f"無防備な可能性がある建玉: {positions_text}\n"
        f"{consequence}\n"
        f"次回の自動手仕舞いは翌 4:30 です。それまで待たず、"
        f"GMO の取引画面から手動で決済するか SL を入れ直してください。"
    )


_FAILURE_REASONS: dict[str, str] = {
    "market_closed": "市場閉場（§6.2 Step0）。次回 cron で再試行（§7.3）",
    "step1_failed": "Step1 新規注文キャンセル失敗。後続ステップ中断（AGENTS.md #4）",
    "step2_failed": "Step2 決済 OCO キャンセル失敗。後続ステップ中断（AGENTS.md #4）",
}


def _handle_closeout_failure(
    status: str,
    *,
    mode: CloseoutMode,
    today: date,
    prev_trade_date: date,
    ts: str,
    ts_ms: int,
    retry_next: bool | None = None,
    cancel_first_seconds: float | None = None,
) -> dict[str, Any]:
    """失敗系 early-return の共通処理（§2 / §5.2 / §7.3）。

    Lambda は正常終了する（次回 cron の再試行余地を残す）ため、人間への通知と
    監査証跡をここで確実に残す:
      1. error レベルの構造化ログ — #143 の app_errors アラーム（→ SNS → Slack）が発火
      2. S3 closeout ログ保存 — 監査証跡（§5.2）。翌営業日の再試行は別の
         prev_trade_date キーになるため失敗証跡は上書きされない
      3. Lambda 内から直接 Slack 強アラート — §2「閉場時は次回再試行＋強アラート送信」

    closeout ロックのライフサイクル（#138 §6.1 準拠・#208）:
      - market_closed: POST を一切送信していない（未処理確実）→ ロック解放し
        同一対象取引日の再試行余地を残す
      - step1_failed / step2_failed: cancel POST 試行済み（結果未確定）→ ロック保持
        （fail-safe: 怪しい日に POST を再送しない）
    """
    reason = _FAILURE_REASONS[status]

    if status == "market_closed":
        release_outcome = dynamodb_store.release_lock_outcome(
            today=prev_trade_date, job_type=dynamodb_store.JOB_CLOSEOUT
        )
        lock_released = release_outcome is LockReleaseOutcome.RELEASED
        # 条件不成立（REFUSED）は設計どおり。障害（FAILED）だけ通知する（#406）。残ったロックは
        # 同朝の再実行（土曜の週次など）を止める。
        if release_outcome is LockReleaseOutcome.FAILED:
            notify_lock_release_failed(
                job="closeout",
                day=prev_trade_date,
                error_event="closeout_lock_release_failed",
            )
    else:
        lock_released = False

    extra: dict[str, Any] = {
        "mode": mode,
        "reason": reason,
        "lock_released": lock_released,
        "timestamp": ts,
        "timestamp_ms": ts_ms,
        "llm_tokens": None,
    }
    if retry_next is not None:
        extra["retry_next"] = retry_next
    # #339: cancel-first 自身が失敗した回も金の段の実測を残す。この経路は
    # early return で closeout_completed / closeout_phase_elapsed に到達しない
    # ため、ここで出さないと「GMO が遅くて Step1 で落ちた」回の所要時間が
    # まるごと欠測する（遅さが原因かを後から切り分けられない）。
    if cancel_first_seconds is not None:
        extra["cancel_first_seconds"] = cancel_first_seconds
    logger.error(f"closeout_{status}", extra=extra)

    payload: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "mode": mode,
        "date": prev_trade_date.isoformat(),    # 対象取引日
        "run_date": today.isoformat(),           # バッチ実行日（監査用）
        "prev_trade_date": prev_trade_date.isoformat(),
        "timestamp": ts,
        "timestamp_ms": ts_ms,
        "llm_total_tokens": 0,
        "lock_released": lock_released,
    }
    if retry_next is not None:
        payload["retry_next"] = retry_next
    # 保存失敗時は save_log 内で Slack アラート送信済み（§1.5）
    if not s3_logger.save_closeout_log(payload, today=prev_trade_date, raise_on_failure=False):
        logger.error(
            "closeout_failure_audit_log_save_failed",
            extra={"status": status, "date": prev_trade_date.isoformat()},
        )

    send_slack_alert(
        f"[AI Sniper][ALERT] 手仕舞いバッチ失敗: {status}"
        f"（mode={mode} 対象取引日={prev_trade_date.isoformat()}）\n"
        f"{reason}。建玉が残っている可能性があります。"
    )

    response: dict[str, Any] = {"status": status, "mode": mode, "lock_released": lock_released}
    if retry_next is not None:
        response["retry_next"] = retry_next
    return response


# ---------------------------------------------------------------------------
# ダッシュボード集約 JSON
# ---------------------------------------------------------------------------


def _upsert_dashboard(
    prev_trade_date: date,
    note_markdown: str,
    now_jst: datetime,
    ts: str,
    ts_ms: int,
    llm_tokens: int,
) -> None:
    """既存のダッシュボード JSON に note 部分を追記して保存する（§6.3）。

    判断バッチが plan/lot/status 等を書き込み済みの場合はそれを保持して note のみ上書きする。
    load_dashboard が RuntimeError を送出した場合（読み込み障害）は保存をスキップし、
    既存データを破壊しない。
    """
    try:
        existing = s3_logger.load_dashboard() or {}
    except RuntimeError as exc:
        logger.error(
            "closeout_dashboard_load_failed_skip_save",
            extra={
                "date": prev_trade_date.isoformat(),
                "error": str(exc),
                "timestamp": ts,
                "timestamp_ms": ts_ms,
                "llm_tokens": llm_tokens,
            },
        )
        return

    dashboard: dict[str, Any] = {
        "status": existing.get("status", "idle"),
        "equityJpy": existing.get("equityJpy", 0),
        "lastUpdated": now_jst.isoformat(),
        "plan": existing.get("plan"),
        "lot": existing.get("lot"),
        "tradeNoteMarkdown": note_markdown,
        "logs": existing.get("logs", []),
    }

    if not s3_logger.save_dashboard(dashboard):
        logger.error(
            "closeout_dashboard_save_failed",
            extra={
                "date": prev_trade_date.isoformat(),
                "timestamp": ts,
                "timestamp_ms": ts_ms,
                "llm_tokens": llm_tokens,
            },
        )


# ---------------------------------------------------------------------------
# 時刻・取引日ユーティリティ
# ---------------------------------------------------------------------------


def _resolve_prev_trade_date(today: date) -> date:
    """直近の確定取引日を返す（土日・祝日を逆順に遡る）。§6 AGENTS.md #6。

    夜バッチは midnight JST（=翌日の開始）に動くため、
    today（= 翌日カレンダー日）から遡って前取引日を特定する。
    """
    prev = today - timedelta(days=1)
    while prev.weekday() >= 5 or is_market_holiday(prev):
        prev -= timedelta(days=1)
    return prev


def _get_today_jst() -> date:
    """現在日付を JST で返す（§6 時刻は常に JST）。"""
    jst_offset = timedelta(hours=9)
    return (datetime.now(tz=UTC) + jst_offset).date()


def _get_now_jst() -> datetime:
    """現在時刻を JST aware datetime で返す。"""
    from zoneinfo import ZoneInfo

    return datetime.now(tz=ZoneInfo("Asia/Tokyo"))
