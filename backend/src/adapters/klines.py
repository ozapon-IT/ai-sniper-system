"""GMO FX Public KLine・Symbols 取得アダプタ (§3.1, §3.4, E05).

付録 B 確定仕様（docs/spikes/E05_klines_appendix_b.md）:
  - date パラメータは YYYY（年）。YYYYMMDD は 404。
  - 配列末尾は常に進行中足。確定条件: openTime + interval_ms <= now_ms。
  - OHLC は末尾ゼロ省略の 10 進文字列（例: "157"）。Decimal で扱う。
  - volume フィールドなし。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import structlog

from backend.src.gmo_fx_endpoints import PUBLIC_KLINES, PUBLIC_SYMBOLS

if TYPE_CHECKING:
    from backend.src.clients.gmo_client import GmoFxClient

_log = structlog.get_logger(__name__)

_SYMBOL = "USD_JPY"
_JST = ZoneInfo("Asia/Tokyo")

# Pivot/ATR 算出は BID 足を使用する。BID/ASK はスプレッド分ずれるため
# 一方に統一しないと Pivot レベルとブラケット注文価格の比較がずれる（§3.1）。
PRICE_TYPE_FOR_INDICATORS: str = "BID"

# interval → ミリ秒。確定判定: openTime + interval_ms <= now_ms。
# 土日ギャップ（金→月）でも金曜 openTime + 24h = 土曜 < 月曜 となり正しく確定と判定される。
# 5min は backtest/context.py（BT-1, #151）が同一の確定条件を共有するために使用する。
_INTERVAL_MS: dict[str, int] = {
    "1day": 24 * 3_600 * 1_000,
    "4hour": 4 * 3_600 * 1_000,
    "5min": 5 * 60 * 1_000,
}

# 東京/欧州セッション分割境界（RD-9 #195, §3.2/§3.3）。初回固定・環境変数化禁止（§7.4）。
# 根拠: 日本の FX 教育で一般的な目安（東京 06:00〜15:00 / 欧州 15:00〜）。
TOKYO_EUROPE_SPLIT_JST = dt_time(15, 0)


@dataclass(frozen=True)
class SymbolInfo:
    """GET /public/v1/symbols から取得した通貨ペア仕様 (E05)."""

    tick_size: Decimal  # USD/JPY: Decimal("0.001") = 0.1pip
    min_lot: int  # USD/JPY: 100
    lot_step: int  # USD/JPY: 1
    max_lot: int  # USD/JPY: 500000


def fetch_symbol_info(client: GmoFxClient, symbol: str = _SYMBOL) -> SymbolInfo:
    """GET /public/v1/symbols から指定通貨ペアの仕様を取得する (E05).

    すべてのフィールドは文字列→ Decimal/int で受け取る。
    """
    resp = client.public_get(PUBLIC_SYMBOLS, params={"symbol": symbol})
    body = resp.json()
    if body.get("status") != 0:
        raise RuntimeError(f"symbols fetch failed: {body}")
    for item in body.get("data", []):
        if item.get("symbol") == symbol:
            return SymbolInfo(
                tick_size=Decimal(item["tickSize"]),
                min_lot=int(item["minOpenOrderSize"]),
                lot_step=int(item["sizeStep"]),
                max_lot=int(item["maxOrderSize"]),
            )
    raise RuntimeError(f"symbol {symbol!r} not found in symbols response")


def fetch_klines_for_year(
    client: GmoFxClient,
    symbol: str,
    interval: str,
    year: int,
) -> list[dict[str, str]] | None:
    """指定年の全ローソク足を取得する (date=YYYY 形式; YYYYMMDD は 404).

    Returns:
        openTime 昇順のローソク足リスト。API エラー時は None（空リストとは区別する）。
        None = 取得失敗（呼び出し元で前年フォールバックを行わない判断に使う）。
        []  = 取得成功だがデータなし（年初で当年足がまだない等）。
    """
    resp = client.public_get(
        PUBLIC_KLINES,
        params={
            "symbol": symbol,
            "priceType": PRICE_TYPE_FOR_INDICATORS,
            "interval": interval,
            "date": str(year),
        },
    )
    body = resp.json()
    # 成功: {"status": 0, "data": [...], "responsetime": "...Z"}
    # 失敗: {"status_code": 404, "message": "Not found"}（status キーなし）
    if body.get("status") != 0:
        _log.warning(
            "klines_fetch_failed",
            symbol=symbol,
            interval=interval,
            year=year,
            response=body,
            timestamp_ms=int(time.time() * 1000),
            llm_tokens=None,
        )
        return None  # API エラー（[] とは区別。呼び出し元でフォールバック禁止）
    candles: list[dict[str, str]] = body.get("data", [])
    return sorted(candles, key=lambda c: int(c["openTime"]))


def get_confirmed_candles(
    candles: list[dict[str, str]],
    now_ms: int,
    interval: str,
) -> list[dict[str, str]]:
    """進行中（未確定）ローソク足を除外し、確定済み足だけ返す.

    確定条件: openTime + interval_ms <= now_ms
    配列末尾の足は常に進行中のため除外される。土日ギャップ（金→月）でも
    「金曜 openTime + 24h = 土曜」が月曜の now より前なので正しく確定と判定される。

    Raises:
        ValueError: サポート外の interval が渡された場合。
    """
    ms = _INTERVAL_MS.get(interval)
    if ms is None:
        raise ValueError(f"unsupported interval: {interval!r}. supported: {list(_INTERVAL_MS)}")
    return [c for c in candles if int(c["openTime"]) + ms <= now_ms]


def get_confirmed_daily_candles(
    client: GmoFxClient,
    now: datetime,
) -> list[dict[str, str]]:
    """当年の確定済み日足を全件返す（ATR 算出用）.

    当年取得成功かつ確定足 15 本未満（年初）の場合のみ前年分を連結する。
    当年取得が API エラー（None）の場合は stale な前年データを返さず空リストを返す。
    戻り値は openTime 昇順。
    """
    now_ms = int(now.timestamp() * 1_000)
    # 年の判定は JST 基準（UTC 元日早朝 = JST 新年のケースで誤らない, AGENTS.md #6）
    year = now.astimezone(_JST).year

    candles = fetch_klines_for_year(client, _SYMBOL, "1day", year)
    if candles is None:
        # API エラー時は fail-closed — 前年の stale データで売買判断させない
        return []
    confirmed = get_confirmed_candles(candles, now_ms, "1day")

    # 当年取得成功かつ確定足が不足（年初 1〜14 営業日）の場合のみ前年を補完（§3.1）。
    if len(confirmed) < 15:
        time.sleep(1)  # GET 連続時のレートリミット遵守（§7.2）
        prev = fetch_klines_for_year(client, _SYMBOL, "1day", year - 1)
        if prev is not None:
            prev_confirmed = get_confirmed_candles(prev, now_ms, "1day")
            confirmed = prev_confirmed + confirmed

    return confirmed


def get_latest_confirmed_daily(
    client: GmoFxClient,
    now: datetime,
) -> dict[str, str] | None:
    """直近確定日足を 1 本返す（§3.1）.

    月曜起動時は金曜足が返る。確定足がなければ None（NO_ENTRY）。
    """
    confirmed = get_confirmed_daily_candles(client, now)
    if not confirmed:
        _log.warning(
            "no_confirmed_daily",
            now_utc=now.isoformat(),
            timestamp_ms=int(time.time() * 1000),
            llm_tokens=None,
        )
        return None
    return confirmed[-1]


def get_recent_4h_context(
    client: GmoFxClient,
    now: datetime,
) -> list[dict[str, str]]:
    """LLM 入力用の直近 6 本の 4 時間足を返す（昇順, §3.2）.

    末尾 7 本のうち進行中 1 本を除外 → 直近 6 本。年またぎも連結対応。
    4 時間足境界は UTC 4h 刻み（JST 01/05/09/13/17/21 時始まり）。
    """
    now_ms = int(now.timestamp() * 1_000)
    year = now.astimezone(_JST).year  # JST 基準（UTC 元日早朝で誤らない, AGENTS.md #6）

    candles = fetch_klines_for_year(client, _SYMBOL, "4hour", year)
    if candles is None:
        return []
    confirmed = get_confirmed_candles(candles, now_ms, "4hour")

    if len(confirmed) < 6:
        time.sleep(1)  # レートリミット遵守（§7.2）
        prev = fetch_klines_for_year(client, _SYMBOL, "4hour", year - 1)
        if prev is not None:
            prev_confirmed = get_confirmed_candles(prev, now_ms, "4hour")
            confirmed = prev_confirmed + confirmed

    return confirmed[-6:]


@dataclass(frozen=True)
class SessionSplitOhlc:
    """当日セッション OHLC（単一ブロック + 東京/欧州分割, RD-9 #195）。"""

    session_open: float
    session_high: float
    session_low: float
    tokyo_open: float | None
    tokyo_high: float | None
    tokyo_low: float | None
    europe_open: float | None
    europe_high: float | None
    europe_low: float | None


def tokyo_europe_boundary_ms(day_jst: date) -> int:
    """指定 JST 日付の 15:00 JST をミリ秒で返す（分割境界）。"""
    boundary = datetime.combine(day_jst, TOKYO_EUROPE_SPLIT_JST, tzinfo=_JST)
    return int(boundary.timestamp() * 1_000)


def ohlc_from_confirmed_candles(
    candles: list[dict[str, str]],
) -> tuple[float, float, float] | None:
    """確定足リストから (始値, 高値, 安値) を返す。空なら None。"""
    if not candles:
        return None
    return (
        float(candles[0]["open"]),
        max(float(c["high"]) for c in candles),
        min(float(c["low"]) for c in candles),
    )


def split_session_ohlc_from_confirmed(
    confirmed: list[dict[str, str]],
    boundary_ms: int,
) -> SessionSplitOhlc | None:
    """確定 5 分足から単一ブロック + 東京/欧州分割 OHLC を算出する（RD-9 #195）。

    分割: openTime < boundary_ms → 東京、openTime >= boundary_ms → 欧州。
    14:55 確定足は東京のみ、15:00 以降は欧州。
    """
    session = ohlc_from_confirmed_candles(confirmed)
    if session is None:
        return None
    tokyo_candles = [c for c in confirmed if int(c["openTime"]) < boundary_ms]
    europe_candles = [c for c in confirmed if int(c["openTime"]) >= boundary_ms]
    tokyo = ohlc_from_confirmed_candles(tokyo_candles)
    europe = ohlc_from_confirmed_candles(europe_candles)
    return SessionSplitOhlc(
        session_open=session[0],
        session_high=session[1],
        session_low=session[2],
        tokyo_open=tokyo[0] if tokyo else None,
        tokyo_high=tokyo[1] if tokyo else None,
        tokyo_low=tokyo[2] if tokyo else None,
        europe_open=europe[0] if europe else None,
        europe_high=europe[1] if europe else None,
        europe_low=europe[2] if europe else None,
    )


def get_session_ohlc(
    client: GmoFxClient,
    now: datetime,
) -> tuple[float, float, float] | None:
    """当日セッションの (始値, 高値, 安値) を返す（RD-5 #170, §3.2/§3.3 当日セッション情報）.

    5 分足の date=YYYYMMDD ファイルは JST 06:00 起点の 1 日分（E05 §8・BT-1 #151 で実確認）。
    判断時刻までに確定した足のみ（openTime + 5min <= now）から算出するため、未来情報や
    判断時刻以降の値動きを含まない（カットオフは get_confirmed_candles と同一条件）。

    date は JST の当日（YYYYMMDD）を用いる（06:00 JST 切替に一致, AGENTS.md #6）。BID 足で統一
    （Pivot/ATR と同じ PRICE_TYPE_FOR_INDICATORS — スプレッド分のずれを避ける, §3.1）。

    GET のレートリミット（6回/秒, §7.2）は public_get 内の RateLimiter.wait_get() が一元管理する
    ため、ここで明示 sleep は挟まない（CodeRabbit PR #178 指摘）。

    Returns:
        (session_open, session_high, session_low) or None
        （取得失敗・当日確定足なし時。呼び出し元は degrade して描画を省く — NO_ENTRY に倒さない）
    """
    split = get_split_session_ohlc(client, now)
    if split is None:
        return None
    return split.session_open, split.session_high, split.session_low


def get_split_session_ohlc(
    client: GmoFxClient,
    now: datetime,
) -> SessionSplitOhlc | None:
    """当日セッションの単一ブロック + 東京/欧州分割 OHLC を返す（RD-9 #195, §3.2/§3.3）。

    5 分足の date=YYYYMMDD ファイルは JST 06:00 起点。判断時刻までの確定足のみ使用
    （get_confirmed_candles と同一カットオフ）。取得失敗・確定足なし時は None（degrade）。
    """
    now_ms = int(now.timestamp() * 1_000)
    day_jst = now.astimezone(_JST).date()
    date_str = day_jst.strftime("%Y%m%d")
    resp = client.public_get(
        PUBLIC_KLINES,
        params={
            "symbol": _SYMBOL,
            "priceType": PRICE_TYPE_FOR_INDICATORS,
            "interval": "5min",
            "date": date_str,
        },
    )
    body = resp.json()
    if body.get("status") != 0:
        _log.warning(
            "session_ohlc_fetch_failed",
            date=date_str,
            response=body,
            timestamp_ms=int(time.time() * 1000),
            llm_tokens=None,
        )
        return None
    candles: list[dict[str, str]] = body.get("data", [])
    # API 順序保証は未記載のため防御的に昇順ソート（年次 API と同様・#210 L-1）。
    # ohlc_from_confirmed_candles は candles[0] を session_open に使う。
    candles = sorted(candles, key=lambda c: int(c["openTime"]))
    confirmed = get_confirmed_candles(candles, now_ms, "5min")
    if not confirmed:
        return None
    return split_session_ohlc_from_confirmed(
        confirmed, tokyo_europe_boundary_ms(day_jst)
    )
