"""ポジションサイズ算出（§4.1）。

D_stoploss_pips → V_raw → V_rounded（100通貨切り捨て）を算出する。
MVP_MODE=True 時は V_raw/V_safe を毎回ログ出力し、発注数量は 100 固定（§4.2）。
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


def calculate_position_size(
    entry_price: float,
    stop_loss: float,
    equity: float,
    risk_percent: float | None = None,
) -> dict[str, float]:
    """D_stoploss_pips から V_raw / V_rounded を算出する（§4.1）。

    Args:
        entry_price: エントリー価格
        stop_loss: stop_loss 価格（Python エンリッチ済み）
        equity: 口座有効純資産（円）
        risk_percent: リスク比率（既定は環境変数 RISK_PERCENT、なければ 0.01）

    Returns:
        {"d_stoploss_pips": float, "l_max": float, "v_raw": float, "v_rounded": int}
    """
    if risk_percent is None:
        risk_percent = float(os.environ.get("RISK_PERCENT", "0.01"))

    d_stoploss_pips = abs(entry_price - stop_loss) / 0.01
    if d_stoploss_pips == 0:
        raise ValueError("D_stoploss_pips が 0 です。entry_price と stop_loss が同値です。")

    l_max = equity * risk_percent
    # 1 pip = 0.01 JPY/通貨（USD/JPY）
    d_stoploss_jpy = d_stoploss_pips * 0.01
    v_raw = l_max / d_stoploss_jpy
    v_rounded = math.floor(v_raw / 100) * 100

    return {
        "d_stoploss_pips": d_stoploss_pips,
        "l_max": l_max,
        "v_raw": v_raw,
        "v_rounded": v_rounded,
    }


def resolve_order_size(
    v_safe: int,
    v_raw: float,
    llm_token_usage: int | None = None,
) -> int:
    """発注ロット数を確定する（§4.2 MVP_MODE）。

    MVP_MODE=True: 発注は 100 通貨固定。V_raw/V_safe は必ずログ出力する。
    MVP_MODE=False or 未設定: v_safe をそのまま返す。
    v_safe=0（NO_ENTRY）の場合は MVP_MODE によらず 0 を返す。

    Args:
        v_safe: calculate_v_safe で確定した本番ロット数（0=NO_ENTRY）
        v_raw: calculate_position_size で算出した理論ロット数
        llm_token_usage: LLM トークン消費数（税務証跡 §5.2）

    Returns:
        実際に発注するロット数（int）。v_safe=0 の場合は 0。
    """
    # 証拠金不足 / レバ上限超過（v_safe<=0）→ MVP_MODE によらず発注しない
    mvp_mode = os.environ.get("MVP_MODE", "False").lower() in {"true", "1"}
    order_size = 0 if v_safe <= 0 else (100 if mvp_mode else v_safe)

    now = datetime.now(tz=UTC)
    log_payload = {
        "timestamp": now.isoformat(),
        "timestamp_ms": round(now.timestamp() * 1000),
        "mvp_mode": mvp_mode,
        "v_raw": v_raw,
        "v_safe": v_safe,
        "order_size": order_size,
        "llm_tokens": llm_token_usage,
    }
    logger.info(json.dumps(log_payload, ensure_ascii=False))

    return order_size
