"""ロンドンORB デモの Public API アダプタ（ticker / status / 1分足 klines）。

設計書: docs/ai-dev/london-orb-demo-design.md §6.1（開場ゲート）・§6.2（レンジ確定）
付録B: docs/spikes/london_orb_api_spec.md §2（分足は JST 06:00 起点の 1 日分）・§6（平日実測）

🔴 **本モジュールが ORB デモで唯一ネットワークに触れる層**。使うのは `public_get` のみで、
Private / 発注系エンドポイントには触れない。

配置理由: 判定コア・状態機械を API クライアントから切り離すため（設計書 §3.2）。
`backend/src/orb/` は **#331 の import 境界**（`scripts/check_import_boundary.py` 契約4・CI 必須）により
`backend.src.clients` / `backend.src.persistence` を直接 import できない。
そのため API に触れる本アダプタは `backend/src/adapters/` に置く。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.src.clients.gmo_client import GmoFxClient
from backend.src.gmo_errors import raise_for_gmo_status
from backend.src.gmo_fx_endpoints import PUBLIC_KLINES, PUBLIC_STATUS, PUBLIC_TICKER
from backend.src.orb.broker_models import TickerObservation
from backend.src.orb.constants import BAR_MINUTES_15, JST_DAY_START, LON
from backend.src.orb.models import Bar1m, Bar15

logger = logging.getLogger(__name__)

SYMBOL = "USD_JPY"
PRICE_TYPE = "BID"  # 判定は BID 基準（Phase 0 / 設計書 §6.3）
MARKET_OPEN = "OPEN"


def fetch_market_status(client: GmoFxClient) -> str:
    """``GET /public/v1/status`` の ``data.status``（OPEN / CLOSE / MAINTENANCE）。

    🔴 エンベロープの ``status``（API 成否）と ``data.status``（市場状態）は同名で別物
    （付録B `market_status_closed.md`）。**両方見る**。

    🔴 **業務エラーを「閉場」に化けさせない**（#327）。GMO は業務エラーでも
    HTTP 200 + ``{"status": 1, ...}`` を返す（`.claude/rules/gmo-api.md`）。検査しないと
    ``data`` 欠落 → ``"UNKNOWN"`` → 呼び出し側が `market_closed` と分類し、
    §6.5 の機会損失（`OUTAGE_SKIP_REASONS` は `system_outage` のみ）に**計上されない**。
    合否基準である稼働率に偽陰性の穴が開くため、ここは例外にして呼び出し側に判断させる。

    Raises:
        GmoApiError: エンベロープ ``status`` が 0 以外のとき。
    """
    resp = client.public_get(PUBLIC_STATUS)
    raise_for_gmo_status(resp, context="public/status")
    body = resp.json()
    data = body.get("data", {}) if isinstance(body, dict) else {}
    return str(data.get("status", "UNKNOWN"))


def fetch_observation(client: GmoFxClient, *, now: datetime | None = None) -> TickerObservation:
    """``GET /public/v1/ticker`` から USD/JPY の観測点を 1 つ作る。

    開場判定にはレスポンス行の ``status`` を用いる。**その気配自身に結び付いた状態**であり、
    別リクエストの `/public/v1/status` より観測点との整合が高い（かつ 1 リクエスト節約できる）。
    バッチ開始時の疎通・開場確認には `fetch_market_status` を併用する。

    Raises:
        GmoApiError: エンベロープ ``status`` が 0 以外のとき（#327）。
        LookupError: ticker レスポンスに USD/JPY 行が無い場合。
    """
    resp = client.public_get(PUBLIC_TICKER)
    raise_for_gmo_status(resp, context="public/ticker")
    body = resp.json()
    rows = body.get("data", []) if isinstance(body, dict) else []
    for row in rows:
        if row.get("symbol") != SYMBOL:
            continue
        ts_raw = str(row.get("timestamp", ""))
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            ts = now or datetime.now(tz=UTC)
        return TickerObservation(
            ts=ts.astimezone(LON),
            bid=float(row["bid"]),
            ask=float(row["ask"]),
            status=str(row.get("status", "UNKNOWN")),
        )
    raise LookupError(f"{SYMBOL} not found in ticker response")


def klines_file_date(now_jst: datetime) -> str:
    """分足 klines の ``date=YYYYMMDD``（**JST 06:00 起点の 1 日分**・付録B §2 / E05 §8）。

    JST 06:00 より前は前暦日のファイルに属する。
    """
    day = now_jst - timedelta(days=1) if now_jst.time() < JST_DAY_START else now_jst
    return day.strftime("%Y%m%d")


def _fetch_raw_klines(client: GmoFxClient, *, interval: str, date_str: str) -> list[dict[str, Any]]:
    resp = client.public_get(
        PUBLIC_KLINES,
        params={
            "symbol": SYMBOL,
            "priceType": PRICE_TYPE,
            "interval": interval,
            "date": date_str,
        },
    )
    # 業務エラーを「足が無い」に化けさせない（#327）。呼び出し側の retry / `system_outage`
    # 分類に載せるため、空配列で返さず例外にする。
    raise_for_gmo_status(resp, context=f"public/klines:{interval}")
    body = resp.json()
    return body.get("data", []) if isinstance(body, dict) else []


def _is_confirmed(open_time_ms: int, *, interval_min: int, now: datetime) -> bool:
    """確定足か（``openTime + interval <= now``・E05 §5）。

    🔴 ORB-1b（#268）実測: klines は足が開いた 4〜6 秒後から**進行中の足**を返し、
    閉じてから最大 +5.4 秒まで OHLC が更新される。進行中足を掴まないため必須のフィルタ。
    """
    close_ms = open_time_ms + interval_min * 60_000
    return close_ms <= int(now.timestamp() * 1000)


def fetch_confirmed_1m(
    client: GmoFxClient, *, date_str: str, now: datetime | None = None
) -> list[Bar1m]:
    """確定済み 1 分足を London 時刻付きで返す（昇順）。"""
    ref = now or datetime.now(tz=UTC)
    out: list[Bar1m] = []
    for cd in _fetch_raw_klines(client, interval="1min", date_str=date_str):
        ot = int(cd["openTime"])
        if not _is_confirmed(ot, interval_min=1, now=ref):
            continue
        out.append(
            Bar1m(
                local_open=datetime.fromtimestamp(ot / 1000, tz=UTC).astimezone(LON),
                open=float(cd["open"]),
                high=float(cd["high"]),
                low=float(cd["low"]),
                close=float(cd["close"]),
            )
        )
    out.sort(key=lambda b: b.local_open)
    return out


def fetch_confirmed_15m(
    client: GmoFxClient, *, date_str: str, now: datetime | None = None
) -> list[Bar15]:
    """確定済み 15 分足を返す（``local_close`` 付き・昇順）。

    15 分足は **終値時刻**で判定する（1 分足の openTime 基準とは非対称・`orb/models.py`）。
    """
    ref = now or datetime.now(tz=UTC)
    out: list[Bar15] = []
    for cd in _fetch_raw_klines(client, interval="15min", date_str=date_str):
        ot = int(cd["openTime"])
        if not _is_confirmed(ot, interval_min=BAR_MINUTES_15, now=ref):
            continue
        open_utc = datetime.fromtimestamp(ot / 1000, tz=UTC)
        out.append(
            Bar15(
                open_time=open_utc,
                open=float(cd["open"]),
                high=float(cd["high"]),
                low=float(cd["low"]),
                close=float(cd["close"]),
                local_close=(open_utc + timedelta(minutes=BAR_MINUTES_15)).astimezone(LON),
            )
        )
    out.sort(key=lambda b: b.local_close)
    return out


def same_london_day(bars_open: list[Bar1m], session_date: str) -> list[Bar1m]:
    """当該 London 暦日の 1 分足だけに絞る。"""
    return [b for b in bars_open if b.local_open.date().isoformat() == session_date]


def same_london_day_15(bars: list[Bar15], session_date: str) -> list[Bar15]:
    """当該 London 暦日の 15 分足だけに絞る（終値時刻基準）。"""
    return [b for b in bars if b.local_close.date().isoformat() == session_date]


def london_now() -> datetime:
    """現在時刻（Europe/London）。テストから差し替えやすいよう関数にしておく。"""
    return datetime.now(tz=LON)


__all__ = [
    "MARKET_OPEN",
    "fetch_confirmed_1m",
    "fetch_confirmed_15m",
    "fetch_market_status",
    "fetch_observation",
    "klines_file_date",
    "london_now",
    "same_london_day",
    "same_london_day_15",
]
