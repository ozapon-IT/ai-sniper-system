"""判断・IFD-OCO 注文予約バッチ Lambda ハンドラー（§2, §3.5, §6.1）。

E12: データ収集 → LLM → エンリッチ → tick 丸め → バリデーション → 発注（Phase 1 スタブ）
フロー詳細は system_requirements.md §2 を参照。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Literal

import httpx
from pydantic import ValidationError as PydanticValidationError

from backend.src.adapters.economic_calendar import (
    DEGRADE_CAUSE_TZ_UNRESOLVED,
    CalendarResult,
    IndicatorDaySkipResult,
    evaluate_indicator_day_skip,
)
from backend.src.adapters.economic_calendar import (
    build_source as build_calendar_source,
)
from backend.src.adapters.klines import (
    SessionSplitOhlc,
    get_confirmed_daily_candles,
    get_recent_4h_context,
    get_split_session_ohlc,
)
from backend.src.adapters.macro_context import MacroResult
from backend.src.adapters.macro_context import build_source as build_macro_source
from backend.src.alerting.slack_alert import send_slack_alert
from backend.src.batch.lock_acquire import (
    LockFailureKind,
    classify_lock_failure,
    notify_lock_release_failed,
    notify_lock_unavailable,
)
from backend.src.clients.gmo_client import GmoFxClient, OrderBlockedError, live_orders_allowed
from backend.src.engine.atr import calculate_atr
from backend.src.engine.enrich import (
    Side,
    calculate_atr_h,
    calculate_stop_loss,
    calculate_take_profit,
)
from backend.src.engine.leverage_cap import calculate_v_safe
from backend.src.engine.pivot import calculate_pivot
from backend.src.engine.position_sizing import calculate_position_size, resolve_order_size
from backend.src.gmo_errors import raise_for_gmo_status
from backend.src.gmo_fx_endpoints import (
    PRIVATE_ACCOUNT_ASSETS,
    PRIVATE_ACTIVE_ORDERS,
    PRIVATE_IFO_ORDER,
    PRIVATE_OPEN_POSITIONS,
    PUBLIC_TICKER,
)
from backend.src.lambda_auth import get_gmo_credentials, reset_token_cache
from backend.src.llm.client import generate_decision
from backend.src.llm.prompt import EconomicEvent, PromptContext
from backend.src.logging_config import configure_logging
from backend.src.models import IfoOrderRequest, IfoOrderResponse
from backend.src.persistence import dynamodb_store, s3_logger
from backend.src.persistence.dynamodb_store import LockReleaseOutcome
from backend.src.validation.day_skip_guards import evaluate_edge_guard_day_skip
from backend.src.validation.pre_order import ValidationError, validate_pre_order
from config.market_calendar import is_market_holiday, is_us_thin_market

configure_logging()
logger = logging.getLogger(__name__)

SYMBOL = "USD_JPY"
# E05 (#12) で tick=0.001 確定済み（docs/spikes/E05_klines_appendix_b.md）。
USD_JPY_TICK_SIZE = Decimal("0.001")


# ---------------------------------------------------------------------------
# Lambda エントリポイント
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """判断バッチエントリポイント（§2, §6.1）。

    EventBridge から毎日 PM 8:30 JST（月〜金）に起動される。
    休場日チェックを先頭で行い、休場日なら早期 return する（§3.5）。
    """
    reset_token_cache()
    today = _get_today_jst()
    now_jst = _get_now_jst()
    # #404: 見送りの日に判断をやり直すための invoke の payload `{"force_redecide": true}`。
    # 判断済みマーカーの確認だけを飛ばす（冪等チェック・当日ロックはそのまま）。
    # `is True`: 文字列 "true" や 1 では有効にしない。Scheduler の input は null なので定期実行に影響しない。
    force_redecide = isinstance(event, dict) and event.get("force_redecide") is True
    logger.info(
        "decision_batch_start",
        extra={"date": today.isoformat(), "force_redecide": force_redecide},
    )

    if is_market_holiday(today):
        logger.info(
            "decision_batch_skipped",
            extra={"reason": "market_holiday", "date": today.isoformat()},
        )
        return {"status": "skipped", "reason": "market_holiday", "date": today.isoformat()}

    api_key, api_secret = get_gmo_credentials()

    with GmoFxClient(api_key=api_key, api_secret=api_secret) as client:
        return _run(client=client, today=today, now_jst=now_jst, force_redecide=force_redecide)


# ---------------------------------------------------------------------------
# 本体（テスト用に client 注入可能）
# ---------------------------------------------------------------------------


def _build_economic_events_log(economic_events: list[EconomicEvent]) -> list[dict[str, Any]]:
    """§5.2 / #163: BT-5 リプレイ用 economic_events スナップショット。"""
    return [
        {
            "time_jst": ev.time_jst,
            "date_jst": ev.date_jst,
            "currency": ev.currency,
            "name": ev.name,
            "impact": ev.impact,
            "forecast": ev.forecast,
            "previous": ev.previous,
        }
        for ev in economic_events
    ]


# LlmResult.fallback の既知の値（#404）。
_LLM_FALLBACKS = ("call_failed", "refusal", "parse_failed")

# 判断ログ（logs/decision/{date}.json）の status の重み（#402）。
# 発注に至った結果（stubbed / ok / failed / persistence_failed 等）> no_entry > skipped。
# 同じ取引日の 2 回目の起動（Scheduler の retry_attempts=2・at-least-once 配信・手動再実行）が
# 1 回目のより重い判断ログを軽い status のログで上書きすると、税務証跡（§5.2）と翌朝の
# 手仕舞い・トレードノートの材料が失われる。
_DECISION_STATUS_SKIPPED_RANK = 0
_DECISION_STATUS_NO_ENTRY_RANK = 1
_DECISION_STATUS_ORDER_RANK = 2


def _decision_status_rank(status: object) -> int:
    if status == "skipped":
        return _DECISION_STATUS_SKIPPED_RANK
    if status == "no_entry":
        return _DECISION_STATUS_NO_ENTRY_RANK
    return _DECISION_STATUS_ORDER_RANK  # 未知の status も重い側に倒す（上書きしない）


def _read_existing_decision_log(today: date) -> dict[str, Any] | None:
    """同じ取引日の既存の判断ログを読む（なければ None）。テストが差し替える継ぎ目。

    存在しないキーの GetObject が 404 でなく 403 になり得るため、存在確認は一覧で行う
    （s3_logger.load_decision_log_if_exists）。"""
    return s3_logger.load_decision_log_if_exists(today)


def _save_decision_log(payload: dict[str, Any], *, today: date) -> bool:
    """判断ログを保存する。同じ日の既存ログより軽い status では**上書きしない**（#402）。

    - 書く側が発注結果（重み最大）なら読み込みなしでそのまま保存する（通常の発注経路に
      余計な S3 読み込みを足さない）。
    - 既存ログがより重い（例: 1 回目が stubbed / ok、2 回目が skipped / no_entry）ときは保存せず、
      警告ログだけ残す。同じ重みどうし（skipped → skipped 等）は最新で上書きする。
    - 既存ログの読み込みに失敗したときは、軽い status の保存を**しない**（fail-closed）。
      `s3_logger.load_log` の契約（読めないときは保存をスキップして既存データを破壊しない）と
      同じ向き。守りたいのは重い側（発注結果）のログで、失うのは「読めず、かつその日にまだ
      ログが無い」ときの skip / 見送りの 1 件だけ。内容は構造化ログ（decision_batch_skipped /
      decision_batch_no_entry）にも残り、error ログと Slack で気づけるようにする。
    - 🔴 残る隙: 読み込みと保存は原子的でない。1 回目がまだ保存前の瞬間に 2 回目が先に
      読み込むと（ロック取得前の経路）、後から書いた側が勝つ。ロックと冪等チェックにより
      二重発注は起きない（影響は判断ログの内容のみ）。
    """
    new_rank = _decision_status_rank(payload.get("status"))
    if new_rank < _DECISION_STATUS_ORDER_RANK:
        try:
            existing = _read_existing_decision_log(today)
        except RuntimeError as exc:
            logger.error(
                "decision_log_existing_read_failed_not_saving",
                extra={
                    "date": today.isoformat(),
                    "error": str(exc),
                    "new_status": payload.get("status"),
                    "new_reason": payload.get("reason"),
                },
            )
            send_slack_alert(
                f"[AI Sniper][ALERT] 判断ログの既存確認に失敗したため保存をスキップしました"
                f"（date={today.isoformat()} status={payload.get('status')} "
                f"reason={payload.get('reason')}）\n"
                f"1 回目の判断ログを上書きしないための保護です。内容は CloudWatch の"
                f"構造化ログ（decision_batch_skipped / decision_batch_no_entry）に残っています。"
            )
            return False
        if isinstance(existing, dict):
            existing_rank = _decision_status_rank(existing.get("status"))
            if existing_rank > new_rank:
                logger.warning(
                    "decision_log_not_overwritten",
                    extra={
                        "date": today.isoformat(),
                        "existing_status": existing.get("status"),
                        "new_status": payload.get("status"),
                        "new_reason": payload.get("reason"),
                    },
                )
                return False
    return s3_logger.save_decision_log(payload, today=today)


def _mark_decision_concluded(today: date, *, reason: str | None) -> None:
    """当日の判断に結論（見送りを含む）が出たことを DynamoDB に記録する（#404）。

    同じ日の 2 回目の起動が、見送りと判断した日に LLM を呼び直して発注まで進まないようにする。
    記録に失敗しても判断結果は変えない（error ログだけ。従来どおり同じ日の再起動で判断をやり直し得る）。
    🔴 transient な失敗（`skipped` の各理由・`calendar_degraded`）では呼ばない: 再起動で回復できる。
    """
    if not dynamodb_store.mark_decision_concluded(today, status="no_entry", reason=reason):
        logger.error(
            "decision_concluded_marker_write_failed",
            extra={"date": today.isoformat(), "reason": reason},
        )


def _run(
    client: GmoFxClient,
    today: date,
    now_jst: datetime,
    *,
    force_redecide: bool = False,
) -> dict[str, Any]:
    """判断バッチ本体（§2 シーケンス）。テストでは client をモックして呼ぶ。"""

    def _audit_fields(llm_tokens: int = 0, api_reqs: int = 0) -> dict[str, Any]:
        """全分岐共通の監査必須フィールドを返す（§5.2 税務証跡）。

        ``trading_phase`` は実行時の ``TRADING_PHASE`` を strip した生文字列（#383 / §7.4）。
        未設定、または strip 後が空ならキーを書かない。既定の ``"1"`` は補わない。
        許可外の値も別の値へ直さない。発注可否（``live_orders_allowed``）の判定は変えない。
        """
        fields: dict[str, Any] = {
            "timestamp": now_jst.isoformat(),
            "llm_total_tokens": llm_tokens,
            "api_requests": api_reqs,
        }
        phase = os.environ.get("TRADING_PHASE", "").strip()
        if phase:
            fields["trading_phase"] = phase
        return fields

    def _skip(
        reason: str,
        api_reqs: int = 0,
        *,
        spread_pips: float | None = None,
        equity_jpy: float | None = None,
    ) -> dict[str, Any]:
        """skipped 終了時に S3 へも保存してから return する（監査ログ保全）。"""
        payload: dict[str, Any] = {
            "status": "skipped",
            "reason": reason,
            "date": today.isoformat(),
            **_audit_fields(api_reqs=api_reqs),
        }
        if spread_pips is not None:
            payload["spread_pips"] = spread_pips
        # #411: 口座を取得できた後に止まった日だけ有効純資産を残す（取得前は推測せずキーを入れない）。
        if equity_jpy is not None:
            payload["equity_jpy"] = equity_jpy
        logger.info("decision_batch_skipped", extra=payload)
        _save_decision_log(payload, today=today)
        return payload

    # #404: 同じ日に既に結論（見送りを含む）を出していたら、判断をやり直さない。
    # 見送りの日は当日ロックを取らない（発注に進まないため）ので、再起動された 2 回目は
    # 冪等チェックを通って LLM を呼び直し、今度は発注と判断すると発注まで進み得た。
    # `is True`: マーカー確認が不確かな値（モック等）を返しても「結論済み」と誤判定しない。
    if force_redecide:
        # 人間が invoke で明示したやり直し。マーカーの確認だけを飛ばす。冪等チェック（注文・建玉）と
        # 当日ロックはこのあとそのまま通る（発注済みの日は dynamodb_lock_exists で見送られる）。
        logger.warning("decision_batch_force_redecide", extra={"date": today.isoformat()})
    elif dynamodb_store.is_decision_concluded(today) is True:
        return _skip("decision_already_concluded")

    # §2 冪等性チェック — fail-closed: 確認不能時は発注不可
    idempotency = _check_idempotency(client=client, today=today)
    if idempotency["skip"]:
        return _skip(idempotency["reason"])

    # §3.1: 直近確定日足 + ATR 算出
    daily_candles = _fetch_daily_candles(client=client, now_jst=now_jst)
    if daily_candles is None:
        return _skip("no_confirmed_candle")

    latest_candle, all_candles = daily_candles
    atr = calculate_atr(all_candles)
    pivot = calculate_pivot(
        high=float(latest_candle["high"]),
        low=float(latest_candle["low"]),
        close=float(latest_candle["close"]),
    )

    # §3.2: 4時間足取得（LLM 文脈用）
    recent_4h_candles = _fetch_4h_candles(client=client, now_jst=now_jst)

    # §3.2/§3.3: 当日セッション情報（06:00 JST〜判断時刻の始値/高値/安値）。夜型の方向判断の主材料。
    # 取得失敗は degrade（None — プロンプト描画を省くだけで NO_ENTRY に倒さない, RD-5 #170）。
    session_split = _fetch_session_split(client=client, now_jst=now_jst)
    session_open = session_split.session_open if session_split else None
    session_high = session_split.session_high if session_split else None
    session_low = session_split.session_low if session_split else None
    tokyo_open = session_split.tokyo_open if session_split else None
    tokyo_high = session_split.tokyo_high if session_split else None
    tokyo_low = session_split.tokyo_low if session_split else None
    europe_open = session_split.europe_open if session_split else None
    europe_high = session_split.europe_high if session_split else None
    europe_low = session_split.europe_low if session_split else None

    # 現在レート取得（mid + 判断時スプレッド — RD-8 G1）
    ticker = _fetch_ticker(client=client)
    if ticker is None:
        return _skip("no_current_price")
    current_price, spread_pips = ticker

    # 口座資産取得 — 取得失敗時は no_entry（推測資産で発注しない）
    account = _fetch_account(client=client)
    if account is None:
        return _skip("no_account_data", spread_pips=spread_pips)
    # Decimal→float: 下流の sizing/leverage 関数は float を期待するため境界で変換
    equity, available_amount = float(account[0]), float(account[1])

    # §3.3: 経済指標取得（必須入力）
    economic_events, calendar_result = _fetch_economic_events(today=today)
    calendar_source_ok = calendar_result.source_ok

    # §3.3 / RD-7 #172: day-skip hard rule（LLM 呼び出し前）
    day_skip = evaluate_indicator_day_skip(calendar_result)
    if day_skip.skip:
        economic_events_log = _build_economic_events_log(economic_events)
        skip_indicators = [
            {
                "name": ev.name,
                "time_jst": ev.datetime_jst.strftime("%H:%M"),
                "date_jst": ev.datetime_jst.strftime("%Y-%m-%d"),
                "currency": ev.currency,
            }
            for ev in day_skip.triggering_events
        ]
        log_payload: dict[str, Any] = {
            "status": "no_entry",
            "reason": day_skip.reason,
            "date": today.isoformat(),
            "direction": "NO_ENTRY",
            "execution_required": False,
            "rationale": _day_skip_rationale(day_skip),
            "sentiment_summary": None,
            "referenced_pivot_level": "NONE",
            "order_type": None,
            "sl_profile": None,
            "atr_multiplier": None,
            "rr_target": None,
            "economic_events": economic_events_log,
            "calendar_source_ok": calendar_source_ok,
            "calendar_degrade_cause": day_skip.calendar_degrade_cause,
            "calendar_tz_key": day_skip.calendar_tz_key,
            "skip_indicators": skip_indicators,
            "edge_guard_details": None,
            # RD-17 #219 / §6 限界 #3: G1 閾値の継続監視用に 20:30 実スプレッドを全終了パスで記録
            "spread_pips": spread_pips,
            # #411: 口座取得後の終了パスなので有効純資産（円）を残す（ロット計算・発注には使わない）。
            "equity_jpy": equity,
            "llm_input_tokens": 0,
            "llm_output_tokens": 0,
            "llm_model": None,
            "macro_context": [],
            "macro_context_source_ok": None,
            "macro_context_injected": False,
            "session_open": session_open,
            "session_high": session_high,
            "session_low": session_low,
            **_audit_fields(api_reqs=1),
        }
        logger.info("decision_batch_no_entry", extra=log_payload)
        _save_decision_log(log_payload, today=today)
        # calendar_degraded はカレンダー取得の失敗による fail-safe 見送りで、再起動で回復し得るため
        # 「結論」にしない（#404）。指標 day-skip は決定論的な結論。
        if day_skip.reason != "calendar_degraded":
            _mark_decision_concluded(today, reason=day_skip.reason)
        # calendar_degraded のときだけ。指標 day-skip はこの分岐でも送らない（#376）。
        if day_skip.reason == "calendar_degraded":
            _notify_calendar_degraded(today, day_skip, calendar_source_ok)
        return log_payload

    # §3.3 / RD-8 #194: エッジガード day-skip hard rule（LLM 呼び出し前・指標 day-skip の直後）
    edge_skip = evaluate_edge_guard_day_skip(
        current_price_mid=current_price,
        spread_pips=spread_pips,
        atr_14d=atr,
        session_open=session_open,
    )
    if edge_skip.skip:
        economic_events_log = _build_economic_events_log(economic_events)
        log_payload = {
            "status": "no_entry",
            "reason": edge_skip.reason,
            "date": today.isoformat(),
            "direction": "NO_ENTRY",
            "execution_required": False,
            "rationale": "エッジガード day-skip hard rule（§3.3 / RD-8 #194）",
            "sentiment_summary": None,
            "referenced_pivot_level": "NONE",
            "order_type": None,
            "sl_profile": None,
            "atr_multiplier": None,
            "rr_target": None,
            "economic_events": economic_events_log,
            "calendar_source_ok": calendar_source_ok,
            "skip_indicators": [],
            "edge_guard_details": edge_skip.details,
            "spread_pips": spread_pips,
            # #411: 口座取得後の終了パスなので有効純資産（円）を残す（ロット計算・発注には使わない）。
            "equity_jpy": equity,
            "llm_input_tokens": 0,
            "llm_output_tokens": 0,
            "llm_model": None,
            "macro_context": [],
            "macro_context_source_ok": None,
            "macro_context_injected": False,
            "session_open": session_open,
            "session_high": session_high,
            "session_low": session_low,
            **_audit_fields(api_reqs=1),
        }
        logger.info("decision_batch_no_entry", extra=log_payload)
        _save_decision_log(log_payload, today=today)
        _mark_decision_concluded(today, reason=edge_skip.reason)
        return log_payload

    # §3.3: オーバーナイトマクロ取得（補助入力・取得失敗時は degrade、NO_ENTRY に倒さない）
    macro_context = _fetch_macro_context()

    # §3 LLM 意思決定
    prev_close = float(latest_candle["close"])
    prev_open = float(latest_candle["open"])
    prev_day_change = (prev_close - prev_open) / prev_open if prev_open != 0 else 0.0

    ctx = PromptContext(
        current_time_jst=now_jst.strftime("%H:%M"),
        day_of_week=["月", "火", "水", "木", "金", "土", "日"][today.weekday()],
        decision_date_jst=today.isoformat(),
        symbol=SYMBOL,
        current_price_mid=current_price,
        pivot_p=pivot.p,
        pivot_r1=pivot.r1,
        pivot_r2=pivot.r2,
        pivot_s1=pivot.s1,
        pivot_s2=pivot.s2,
        atr_14d=atr,
        prev_day_change=prev_day_change,
        equity_jpy=equity,
        economic_events=economic_events,
        recent_4h_candles=recent_4h_candles,
        us_thin_market=is_us_thin_market(today),
        macro_context=macro_context,
        session_open=session_open,
        session_high=session_high,
        session_low=session_low,
        tokyo_open=tokyo_open,
        tokyo_high=tokyo_high,
        tokyo_low=tokyo_low,
        europe_open=europe_open,
        europe_high=europe_high,
        europe_low=europe_low,
    )

    llm_result = generate_decision(ctx)
    decision = llm_result.decision

    # §5.2 構造化ログ: 注入したマクロ文脈を残してフォワードテスト評価に利用可能にする
    # None（timeout/例外）/ source_ok=False（部分失敗）/ 空リスト（正常だが指標なし）を区別できるよう記録
    macro_log: list[dict[str, object]] = []
    macro_source_ok: bool | None = None
    if macro_context is not None:
        macro_source_ok = macro_context.source_ok
        for ind in macro_context.indicators:
            macro_log.append(
                {
                    "label": ind.label,
                    "latest_value": ind.latest_value,
                    "overnight_change_pct": ind.overnight_change_pct,
                }
            )

    # #163: BT-5 リプレイ用。economic_events は this-week のみ返す外部 API のため
    # 後から再取得できず、no_entry 日も含め全終了パスで S3 に残す（見送り判断こそリプレイ価値が高い）。
    economic_events_log = _build_economic_events_log(economic_events)

    # LLM が判断を返せず安全側の NO_ENTRY に倒したときの理由（None=実際に判断を返した）。
    # 既知の値だけを採る（想定外の値・モックの属性は None＝通常の判断として扱う）。
    llm_fallback = (
        llm_result.fallback if llm_result.fallback in _LLM_FALLBACKS else None
    )

    log_base: dict[str, Any] = {
        "date": today.isoformat(),
        "llm_fallback": llm_fallback,
        "direction": decision.direction,
        "execution_required": decision.execution_required,
        "rationale": decision.rationale,
        "sentiment_summary": decision.sentiment_summary,
        "referenced_pivot_level": decision.referenced_pivot_level,
        "order_type": decision.order_type,
        "sl_profile": decision.sl_profile,
        "atr_multiplier": decision.atr_multiplier,
        "rr_target": decision.rr_target,
        "economic_events": economic_events_log,
        "calendar_source_ok": calendar_source_ok,
        "llm_input_tokens": llm_result.input_tokens,
        "llm_output_tokens": llm_result.output_tokens,
        "llm_model": llm_result.model,
        "macro_context": macro_log,
        "macro_context_source_ok": macro_source_ok,
        "macro_context_injected": bool(macro_log),
        # RD-17 #219 / §6 限界 #3: G1 閾値の継続監視用に 20:30 実スプレッドを全終了パスで記録
        "spread_pips": spread_pips,
        # #411: 口座取得後の終了パスなので有効純資産（円）を残す（ロット計算・発注には使わない）。
        "equity_jpy": equity,
        # RD-5 #170: LLM が見た当日セッション情報（06:00 JST〜判断時刻の確定足由来）。
        "session_open": ctx.session_open,
        "session_high": ctx.session_high,
        "session_low": ctx.session_low,
        **_audit_fields(
            llm_tokens=llm_result.input_tokens + llm_result.output_tokens,
            api_reqs=1,
        ),
    }

    # should_enter は @property（括弧不要）
    if not decision.should_enter:
        logger.info("decision_batch_no_entry", extra=log_base)
        _save_decision_log({**log_base, "status": "no_entry"}, today=today)
        # LLM が判断を返せなかった（呼び出し失敗・拒否・パース失敗）ための見送りは「結論」では
        # ない。通信障害・過負荷などは再起動で回復し得るので、マーカーで当日を締めない（#404）。
        if llm_fallback is None:
            _mark_decision_concluded(today, reason=None)
        return {"status": "no_entry", **log_base}

    # M-1: tick 丸めを先に行い、丸め済み entry から SL → TP を決定論算出する（§3.2, §3.4・Ver 3.6）
    # #238: 丸め向きは方向依存（LONG=切り下げ / SHORT=切り上げ）。現値から遠ざける側に倒す。
    entry_price = _round_tick_entry(
        decision.entry_price,  # type: ignore[arg-type]
        decision.direction,  # type: ignore[arg-type]
    )

    # §3.2: SL/TP 決定論的算出（ATR_h 基準）。ATR 非正/非有限は enrich が ValueError を送出するため、
    # ここで捕捉して no_entry に畳む（1 件の異常値で Lambda 全体を落とし監査ログ保存を飛ばさない）。
    try:
        stop_loss = calculate_stop_loss(
            entry_price=entry_price,
            atr=atr,
            atr_multiplier=decision.atr_multiplier,
            side=decision.direction,  # type: ignore[arg-type]
        )
        # TP = entry ± rr_target × |entry − SL|。LLM 値は使わない。
        take_profit = calculate_take_profit(
            entry_price=entry_price,
            stop_loss=stop_loss,
            rr_target=decision.rr_target,
            side=decision.direction,  # type: ignore[arg-type]
        )
        atr_h = calculate_atr_h(atr)
    except ValueError as exc:
        logger.warning(
            "decision_batch_sl_tp_calculation_failed",
            extra={**log_base, "sl_tp_error": str(exc)},
        )
        _save_decision_log(
            {**log_base, "status": "no_entry", "reason": "sl_tp_calculation_failed",
             "detail": str(exc)},
            today=today,
        )
        _mark_decision_concluded(today, reason="sl_tp_calculation_failed")
        return {
            **log_base,
            "status": "no_entry",
            "reason": "sl_tp_calculation_failed",
            "detail": str(exc),
        }

    # §4.1 + §4.2: ポジションサイズ算出 → レバレッジ上限・証拠金チェック
    sizing = calculate_position_size(
        entry_price=entry_price,
        stop_loss=stop_loss,
        equity=equity,
    )
    v_safe = calculate_v_safe(
        v_rounded=int(sizing["v_rounded"]),
        equity=equity,
        current_price=current_price,
        available_amount=available_amount,
    )
    lot = resolve_order_size(
        v_safe=v_safe,
        v_raw=sizing["v_raw"],
        llm_token_usage=llm_result.input_tokens + llm_result.output_tokens,
    )

    if lot == 0:
        logger.info("decision_batch_no_entry", extra={**log_base, "reason": "lot_zero"})
        _save_decision_log(
            {**log_base, "status": "no_entry", "reason": "lot_zero"}, today=today
        )
        _mark_decision_concluded(today, reason="lot_zero")
        return {"status": "no_entry", "reason": "lot_zero", **log_base}

    # §3.4: 発注前バリデーション
    if decision.order_type is None:
        logger.warning(
            "decision_batch_missing_order_type",
            extra={**log_base, "reason": "missing_order_type"},
        )
        _save_decision_log(
            {**log_base, "status": "no_entry", "reason": "missing_order_type"},
            today=today,
        )
        _mark_decision_concluded(today, reason="missing_order_type")
        return {"status": "no_entry", "reason": "missing_order_type", **log_base}
    order_type = decision.order_type
    try:
        validate_pre_order(
            side=decision.direction,  # type: ignore[arg-type]
            entry_price=entry_price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            lot=lot,
            current_price=current_price,
            atr=atr,
            order_type=order_type,
        )
    except ValidationError as exc:
        logger.warning(
            "decision_batch_validation_failed",
            extra={**log_base, "validation_error": str(exc)},
        )
        _save_decision_log(
            {**log_base, "status": "no_entry", "reason": "validation_failed", "detail": str(exc)},
            today=today,
        )
        _mark_decision_concluded(today, reason="validation_failed")
        return {"status": "no_entry", "reason": "validation_failed", "detail": str(exc)}

    # §2: 発注前に DynamoDB ロックを原子的に獲得（ConditionExpression で二重発注防止）。
    # #210 L-3: 既存ロック（二重起動）とバックエンド障害を reason で分離する（closeout と同型）。
    lock_outcome = dynamodb_store.acquire_lock_outcome(today=today)
    lock_failure = classify_lock_failure(lock_outcome)
    if lock_failure == LockFailureKind.LOCK_EXISTS:
        return _skip("dynamodb_lock_exists", spread_pips=spread_pips, equity_jpy=equity)
    if lock_failure == LockFailureKind.UNAVAILABLE:
        notify_lock_unavailable(
            log_event="dynamodb_lock_unavailable",
            log_extra={
                "date": today.isoformat(),
                "timestamp": now_jst.isoformat(),
                "llm_tokens": None,
            },
            slack_message=(
                f"[AI Sniper][ALERT] 判断バッチのロック取得失敗（DynamoDB 障害の疑い）"
                f"（date={today.isoformat()}）\n"
                f"発注をスキップしました。次回 cron / 手動再実行で再試行。"
            ),
        )
        return _skip("dynamodb_lock_unavailable", spread_pips=spread_pips, equity_jpy=equity)

    # §2 / AGENTS.md #1: two-key interlock — 発注直前で明示チェック（defense-in-depth）
    # GmoFxClient.private_post でも同様に評価されるが、バッチ層でも明示することで
    # 単独テスト・コードレビュー双方で安全条件が可視化される。
    # 判定は共有ヘルパー経由のみ。独自パースはパーサ分裂を生む（Issue #135）。
    if not live_orders_allowed():
        order_result: dict[str, Any] = {
            "stubbed": True,
            "orderId": None,
            "reason": "live_orders_disabled",
        }
        logger.info(
            "ifo_order_stubbed",
            extra={
                "side": decision.direction,
                "order_type": order_type,
                "entry_price": entry_price,
                "take_profit": take_profit,
                "stop_loss": stop_loss,
                "lot": lot,
                "reason": "phase1_two_key_interlock",
            },
        )
    else:
        # §2: IFD-OCO 実発注（Phase 2 / spike のみ）
        order_result = _place_ifo_order(
            client=client,
            side=decision.direction,
            order_type=order_type,
            entry_price=entry_price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            lot=lot,
        )

    _order_status = (
        "stubbed"
        if order_result.get("stubbed") is True
        else "ok"
        if order_result.get("orderId") and not order_result.get("error")
        else "failed"
    )
    result: dict[str, Any] = {
        "status": _order_status,
        **log_base,
        "entry_price": entry_price,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "atr_h": atr_h,
        "lot": lot,
        **order_result,
    }
    # 発注成功時に実 orderId を DynamoDB へ書き戻す（§1.5）
    if (
        _order_status == "ok"
        and order_result.get("orderId")
        and not dynamodb_store.write_order_id(today=today, order_id=str(order_result["orderId"]))
    ):
        result["order_status"] = result["status"]
        result["status"] = "persistence_failed"
        result["reason"] = "order_id_write_failed"
        logger.error("decision_batch_order_id_write_failed", extra=result)
        _save_decision_log(result, today=today)
        return result

    # #138: 発注失敗時のロックライフサイクル（§6.1）
    # - 未約定確実（order_not_placed=True）→ ロック解放し当日の再試行を可能にする。
    #   再試行時は activeOrders/openPositions の冪等チェックが二重発注を防ぐ。
    # - 未確定（5xx・タイムアウト等）→ ロック保持（fail-safe: 怪しい日は張り直さない）。
    #   lock_state を記録し、運用が手動確認で区別できるようにする。
    if _order_status == "failed":
        if order_result.get("order_not_placed") is True:
            release_outcome = dynamodb_store.release_lock_outcome(today=today)
            released = release_outcome is LockReleaseOutcome.RELEASED
            result["lock_released"] = released
            logger.info(
                "dynamodb_lock_released_for_retry",
                extra={"date": today.isoformat(), "released": released},
            )
            # 条件不成立（REFUSED）は設計どおり。障害（FAILED）だけ通知する（#406）。
            if release_outcome is LockReleaseOutcome.FAILED:
                notify_lock_release_failed(
                    job="decision",
                    day=today,
                    error_event="decision_lock_release_failed",
                )
        else:
            result["lock_released"] = False
            # S3 側の最終監査ログにも lock_state を残す（DDB 更新失敗時も追跡可能に）
            result["lock_state"] = "failed_unconfirmed"
            result["lock_state_persisted"] = dynamodb_store.update_summary(
                today=today, summary={"lock_state": "failed_unconfirmed"}
            )

    _save_decision_log(result, today=today)
    if _order_status == "failed":
        logger.error("decision_batch_order_failed", extra=result)
    else:
        logger.info("decision_batch_completed", extra=result)
    return result


# ---------------------------------------------------------------------------
# 冪等性チェック（fail-closed）
# ---------------------------------------------------------------------------


def _check_idempotency(client: GmoFxClient, today: date) -> dict[str, Any]:
    """activeOrders / openPositions を確認し二重発注を防ぐ（§2）。

    取得失敗・非 200 レスポンスは「確認不能」として skip に倒す（fail-closed）。
    DynamoDB ロックは発注直前に原子的に獲得するため、ここでは確認しない。
    """
    # GET activeOrders — 失敗時は skip
    try:
        resp = client.private_get(PRIVATE_ACTIVE_ORDERS, params={"symbol": SYMBOL})
        if resp.status_code != 200:
            logger.error(
                "active_orders_fetch_non200",
                extra={"status_code": resp.status_code},
            )
            return {"skip": True, "reason": "active_orders_fetch_failed"}
        body = resp.json()
        if body.get("status") != 0:
            logger.error(
                "active_orders_api_error",
                extra={"status": body.get("status"), "messages": body.get("messages")},
            )
            return {"skip": True, "reason": "active_orders_fetch_failed"}
        orders = body.get("data", {}).get("list", [])
        if orders:
            return {"skip": True, "reason": "active_orders_exist"}
    except Exception as exc:
        logger.error("active_orders_fetch_failed", extra={"error": str(exc)})
        return {"skip": True, "reason": "active_orders_fetch_failed"}

    # GET openPositions — 失敗時は skip
    try:
        resp = client.private_get(PRIVATE_OPEN_POSITIONS, params={"symbol": SYMBOL})
        if resp.status_code != 200:
            logger.error(
                "open_positions_fetch_non200",
                extra={"status_code": resp.status_code},
            )
            return {"skip": True, "reason": "open_positions_fetch_failed"}
        body = resp.json()
        if body.get("status") != 0:
            logger.error(
                "open_positions_api_error",
                extra={"status": body.get("status"), "messages": body.get("messages")},
            )
            return {"skip": True, "reason": "open_positions_fetch_failed"}
        positions = body.get("data", {}).get("list", [])
        if positions:
            return {"skip": True, "reason": "open_positions_exist"}
    except Exception as exc:
        logger.error("open_positions_fetch_failed", extra={"error": str(exc)})
        return {"skip": True, "reason": "open_positions_fetch_failed"}

    return {"skip": False, "reason": None}


# ---------------------------------------------------------------------------
# データ取得ヘルパー
# ---------------------------------------------------------------------------


def _fetch_daily_candles(
    client: GmoFxClient,
    now_jst: datetime,
) -> tuple[dict[str, str], list[dict[str, str]]] | None:
    """直近確定日足 1 本 + ATR 算出用の直近 15 本を返す（§3.1, §3.2）。

    klines.get_confirmed_daily_candles で年ベース API（date=YYYY）から取得し、
    進行中足を除外した確定済み足のリストを使用する。
    ATR には末尾 15 本を渡す。

    Returns:
        (latest_candle, last_15_candles sorted by openTime asc) or None
    """
    now_utc = now_jst.astimezone(UTC)
    all_confirmed = get_confirmed_daily_candles(client, now_utc)
    if len(all_confirmed) < 15:
        logger.warning("insufficient_daily_candles", extra={"count": len(all_confirmed)})
        return None
    latest = all_confirmed[-1]
    return latest, all_confirmed[-15:]


def _fetch_4h_candles(client: GmoFxClient, now_jst: datetime) -> list[dict[str, str]]:
    """4 時間足を直近 6 本取得する（§3.2 LLM 文脈用）。"""
    try:
        now_utc = now_jst.astimezone(UTC)
        return get_recent_4h_context(client, now_utc)
    except Exception as exc:
        logger.warning("fetch_4h_candles_failed", extra={"error": str(exc)})
    return []


def _fetch_session_split(
    client: GmoFxClient, now_jst: datetime
) -> SessionSplitOhlc | None:
    """当日セッション OHLC（単一 + 東京/欧州分割）を取得する（§3.2/§3.3, RD-5/RD-9）。

    取得失敗は degrade — None を返して続行（4h/マクロと同じ扱い。NO_ENTRY には倒さない）。
    """
    try:
        now_utc = now_jst.astimezone(UTC)
        return get_split_session_ohlc(client, now_utc)
    except Exception as exc:
        logger.warning("fetch_session_split_failed", extra={"error": str(exc)})
    return None


def _fetch_ticker(client: GmoFxClient) -> tuple[float, float] | None:
    """現在レート（mid）と判断時スプレッド（pips）を取得する（RD-8 G1）。

    🔴 **ボディの ``status`` を必ず検査する**（#243 / .claude/rules/gmo-api.md）。
    GMO は業務エラーでも **HTTP 200 + ``{"status": 1, "messages": [...]}``** を返すため、
    ``status_code`` だけ見ると業務エラーが「``data`` が空」＝データなしに化ける。
    2026-08-05 のノート捏造事故はこの未検査パターンが原因だった（付録B
    ``docs/spikes/executions_api_spec.md`` §1）。

    現状は ``None`` を返せば呼び出し元が ``no_current_price`` で NO_ENTRY に倒れるため
    実害は無いが、**「値が無ければ既定値を使う」等に変わった瞬間に事故化する**。
    区別を関数の側で持たせておく。
    """
    time.sleep(1)
    try:
        resp = client.public_get(PUBLIC_TICKER, params={"symbol": SYMBOL})
        if resp.status_code == 200:
            # HTTP 層とボディ status の**両方**を検査する（#243）。
            # GmoApiError は下の except が拾い fetch_ticker_failed として記録される。
            raise_for_gmo_status(resp, context="ticker")
            items = resp.json().get("data", [])
            if items:
                bid = float(items[0]["bid"])
                ask = float(items[0]["ask"])
                mid = (bid + ask) / 2
                # USD/JPY: 1 pip = 0.01。float ノイズを S3 ログに残さないよう 0.01 pip 精度に丸める（RD-17 #219）
                spread_pips = round((ask - bid) / 0.01, 2)
                return mid, spread_pips
    except Exception as exc:
        logger.warning("fetch_ticker_failed", extra={"error": str(exc)})
    return None


def _fetch_account(client: GmoFxClient) -> tuple[Decimal, Decimal] | None:
    """口座有効純資産と利用可能証拠金を取得する（§4.2 証拠金チェック用）。

    Returns:
        (equity, available_amount) or None（取得失敗時 — 推測資産で発注しない）
    """
    time.sleep(1)
    try:
        resp = client.private_get(PRIVATE_ACCOUNT_ASSETS)
        if resp.status_code == 200:
            body = resp.json()
            if body.get("status") != 0:
                logger.error(
                    "fetch_account_api_error",
                    extra={
                        "status": body.get("status"),
                        "messages": body.get("messages"),
                    },
                )
                return None
            # E04確定(#11): data は単一 dict（list ではない。FX 口座は JPY 単一）
            data = body.get("data") or {}
            raw_equity = data.get("equity")
            raw_available = data.get("availableAmount")
            # fail-closed: どちらか不在なら推測資産で発注しない（§4.2）
            if raw_equity is None or raw_available is None:
                logger.error("fetch_account_missing_field", extra={"data_keys": list(data.keys())})
                return None
            # APIは全フィールド文字列（E04 §2）。精度損失を避けるため Decimal で保持。
            return Decimal(raw_equity), Decimal(raw_available)
    except Exception as exc:
        logger.error("fetch_account_failed", extra={"error": str(exc)})
    logger.error("fetch_account_no_data")
    return None


def _day_skip_rationale(day_skip: IndicatorDaySkipResult) -> str:
    """day-skip の rationale。tz 解決失敗だけ文面を分ける（reason は変えない・#376）。"""
    if day_skip.reason == "indicator_day_skip":
        return "指標 day-skip hard rule（§3.3）"
    if day_skip.calendar_degrade_cause == DEGRADE_CAUSE_TZ_UNRESOLVED and day_skip.calendar_tz_key:
        return f"タイムゾーン解決失敗（{day_skip.calendar_tz_key}）（保守的 degrade）"
    return "経済指標カレンダー取得失敗（保守的 degrade）"


def _notify_calendar_degraded(
    today: date,
    day_skip: IndicatorDaySkipResult,
    source_ok: bool,
) -> None:
    """calendar_degraded をログ保存後に 1 回知らせる（#376 / §6.1）。

    本文は日付・cause・tz_key・source_ok のみ。並びと文言は変えない。
    webhook URL とフィード本文は入れない。
    ``send_slack_alert`` は失敗すると False を返し、例外は出さない。戻り値は見ない。
    呼び出し元の NO_ENTRY は変えない。
    """
    tz_text = day_skip.calendar_tz_key or ""
    source_text = "true" if source_ok else "false"
    message = (
        "[AI Sniper] calendar_degraded で NO_ENTRY。"
        f" date={today.isoformat()}"
        f" cause={day_skip.calendar_degrade_cause}"
        f" tz_key={tz_text}"
        f" source_ok={source_text}"
    )
    send_slack_alert(message)


def _fetch_economic_events(today: date) -> tuple[list[EconomicEvent], CalendarResult]:
    """経済指標カレンダーから高インパクト USD/JPY イベントを取得する（§3.3）。

    判断日だけを渡す。保有窓の両端は adapter が作る（#379）。

    Returns:
        (prompt_events, calendar_result)。取得失敗時は calendar_result.source_ok=False。
        day-skip 判定は evaluate_indicator_day_skip(calendar_result) で行う（§3.3 degrade）。
    """
    try:
        source_name = os.environ.get("ECONOMIC_CALENDAR_SOURCE", "forexfactory")
        source = build_calendar_source(source_name)
        result = source.fetch(today)
        events: list[EconomicEvent] = []
        for ev in result.events:
            if ev.impact.value == "High" and ev.currency in {"USD", "JPY"}:
                events.append(
                    EconomicEvent(
                        time_jst=ev.datetime_jst.strftime("%H:%M"),
                        currency=ev.currency,
                        name=ev.name,
                        impact=ev.impact.value,
                        forecast=ev.forecast,
                        previous=ev.previous,
                        date_jst=ev.datetime_jst.strftime("%Y-%m-%d"),
                    )
                )
        return events, result
    except Exception as exc:
        logger.warning("fetch_economic_events_failed", extra={"error": str(exc)})
        return [], CalendarResult(events=[], source_ok=False)


_MACRO_FETCH_TIMEOUT_SEC = 10  # 外部 API 詰まりで判断バッチ全体を止めないための締切


def _fetch_macro_context() -> MacroResult | None:
    """オーバーナイト米国マクロ指標を取得する（§3.3 補助入力）。

    取得失敗は degrade — None を返して続行。NO_ENTRY には倒さない。
    タイムアウト超過時も None を返す（外部 API 詰まりで判断バッチを止めない）。
    """
    import concurrent.futures

    source_name = os.environ.get("MACRO_CONTEXT_SOURCE", "yahoo_chart")
    try:
        source = build_macro_source(source_name)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(source.fetch)
        try:
            return future.result(timeout=_MACRO_FETCH_TIMEOUT_SEC)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "fetch_macro_context_timeout", extra={"timeout_sec": _MACRO_FETCH_TIMEOUT_SEC}
            )
            pool.shutdown(wait=False)  # タイムアウト後は完了を待たずに即 return
            return None
        finally:
            pool.shutdown(wait=False)
    except Exception as exc:
        logger.warning("fetch_macro_context_failed", extra={"error": str(exc)})
        return None


# ---------------------------------------------------------------------------
# 発注（Phase 1: スタブ）
# ---------------------------------------------------------------------------


def _place_ifo_order(
    client: GmoFxClient,
    side: str,
    order_type: Literal["LIMIT", "STOP"],
    entry_price: float,
    take_profit: float,
    stop_loss: float,
    lot: int,
) -> dict[str, Any]:
    """IFD-OCO 発注。Phase 1 では OrderBlockedError をキャッチしてスタブ応答を返す（§2, AGENTS.md #1）。

    失敗時は `order_not_placed` で未約定の確度を区別する（#138, §6.1 ロックライフサイクル）:
    - True: GMO が発注を確実に拒否（HTTP 4xx / body status != 0）— ロック解放可
    - False: 約定有無が未確定（HTTP 5xx / 通信例外 / orderId 不在の 200）— ロック保持
    """
    # E06 (#13) で first*/second* スキーマ確定（docs/spikes/E06_ifoOrder_mapping.md §2）。
    # secondStopPrice(SL) は Python 算出値を受け取る（AGENTS.md #5）。
    if side not in {"LONG", "SHORT"}:
        raise ValueError(f"unsupported side: {side}")
    body = IfoOrderRequest(
        symbol=SYMBOL,
        firstSide="BUY" if side == "LONG" else "SELL",
        firstExecutionType=order_type,
        firstPrice=str(entry_price),
        firstSize=lot,
        secondSize=lot,
        secondLimitPrice=str(take_profit),
        secondStopPrice=str(stop_loss),
    )
    try:
        resp = client.private_post(PRIVATE_IFO_ORDER, body=body)
        if resp.status_code not in {200, 201}:
            logger.error(
                "ifo_order_api_error",
                extra={"status_code": resp.status_code, "body": resp.text},
            )
            # 4xx はリクエスト拒否＝未約定確実。5xx はサーバ側で処理された可能性を排除
            # できないため未確定として扱う（fail-safe）。
            return {
                "orderId": None,
                "stubbed": False,
                "error": f"status={resp.status_code}",
                "order_not_placed": 400 <= resp.status_code < 500,
            }
        try:
            data = resp.json()
        except ValueError:
            # HTTP 200 なのに JSON でない — 受理済みの可能性を否定できず未確定（fail-safe）
            logger.error("ifo_order_invalid_json", extra={"body": resp.text})
            return {
                "orderId": None,
                "stubbed": False,
                "error": "invalid_json",
                "order_not_placed": False,
            }
        if not isinstance(data, dict):
            # valid JSON でも dict 以外（配列・文字列等）— AttributeError で落とさず未確定に畳む
            logger.error(
                "ifo_order_invalid_json_shape",
                extra={"response_type": type(data).__name__},
            )
            return {
                "orderId": None,
                "stubbed": False,
                "error": "invalid_json_shape",
                "order_not_placed": False,
            }
        api_status = data.get("status")
        if api_status is None:
            # status フィールド欠落 — 拒否と断定できず未確定（fail-safe）
            logger.error("ifo_order_missing_status", extra={"response": data})
            return {
                "orderId": None,
                "stubbed": False,
                "error": "missing_api_status",
                "order_not_placed": False,
            }
        if api_status != 0:
            # HTTP 200 でも body status != 0 は GMO の業務拒否（レートリミット等）＝未約定確実
            logger.error(
                "ifo_order_rejected",
                extra={"status": api_status, "messages": data.get("messages")},
            )
            return {
                "orderId": None,
                "stubbed": False,
                "error": f"api_status={api_status}",
                "order_not_placed": True,
            }
        # 🔴 data は **注文オブジェクトの配列**（2026-08-03 実発注スパイクで確定・#91）。
        # IFD-OCO は 3 件（OPEN 1 + CLOSE 2）返り、3 件とも同じ rootOrderId を共有する。
        # 保存するのは rootOrderId — cancel-first の Step1/Step2 が rootOrderIds を要求するため
        # （§6.2・AGENTS.md #4）。従来は data 直値を order_id と推測しており壊れた値が入っていた。
        try:
            parsed = IfoOrderResponse.model_validate(data)
            order_id = parsed.root_order_id()
        except PydanticValidationError as exc:
            # status=0 なのに想定の形でない — 受理済みの可能性を否定できず未確定（fail-safe）
            logger.error(
                "ifo_order_unexpected_data_shape",
                extra={"response": data, "error": str(exc)},
            )
            return {
                "orderId": None,
                "stubbed": False,
                "error": "unexpected_data_shape",
                "order_not_placed": False,
            }
        if not order_id:
            # status=0 なのに rootOrderId 不在 — 受理済みの可能性を否定できず未確定
            logger.error("ifo_order_no_order_id", extra={"response": data})
            return {
                "orderId": None,
                "stubbed": False,
                "error": "no_order_id",
                "order_not_placed": False,
            }
        logger.info(
            "ifo_order_placed",
            extra={"order_id": order_id, "side": side, "entry_price": entry_price},
        )
        return {"orderId": str(order_id), "stubbed": False}
    except httpx.HTTPError as exc:
        # タイムアウト・接続断はリクエストが GMO に到達済みの可能性があり未確定（fail-safe）。
        # 例外を伝播させるとロック保持のまま S3 監査ログも残らないため、ここで失敗結果に畳む。
        logger.error("ifo_order_request_failed", extra={"error": str(exc)})
        return {
            "orderId": None,
            "stubbed": False,
            "error": f"http_error: {exc}",
            "order_not_placed": False,
        }
    except OrderBlockedError:
        logger.info(
            "ifo_order_stubbed",
            extra={
                "side": side,
                "entry_price": entry_price,
                "take_profit": take_profit,
                "stop_loss": stop_loss,
                "lot": lot,
                "reason": "phase1_mock",
            },
        )
        return {"orderId": None, "stubbed": True}


# ---------------------------------------------------------------------------
# tick 丸め（entry 用 M-1）
# ---------------------------------------------------------------------------


def _round_tick_entry(price: float, side: Side) -> float:
    """entry_price を tick 刻みへ「現値から遠ざける向き」に丸める（§3.4・#238）。

    LONG は切り下げ（より深い押し目）/ SHORT は切り上げ（より高い戻り）。
    LONG は `entry <= current`、SHORT は `entry >= current` が発注前検証の hard 条件
    （`pre_order` の ORDER_TYPE_ENTRY_POSITION_VIOLATION）であり、無条件 ROUND_DOWN だと
    SHORT の entry が現値を下回る側へ押し込まれて reject になる（#238 実測 07-09・07-22）。
    SL/TP の「entry から遠ざける向き」（enrich._round_away_from_entry）と同じ設計で、
    丸めが常に約定しにくい＝安全side へ倒れるようにする。
    """
    rounding = ROUND_DOWN if side == "LONG" else ROUND_UP
    return float(Decimal(str(price)).quantize(USD_JPY_TICK_SIZE, rounding=rounding))


# ---------------------------------------------------------------------------
# 時刻ユーティリティ
# ---------------------------------------------------------------------------


def _get_today_jst() -> date:
    """現在日付を JST で返す（§6 時刻は常に JST）。"""
    jst_offset = timedelta(hours=9)
    return (datetime.now(tz=UTC) + jst_offset).date()


def _get_now_jst() -> datetime:
    """現在時刻を JST aware datetime で返す。"""
    from zoneinfo import ZoneInfo

    return datetime.now(tz=ZoneInfo("Asia/Tokyo"))
