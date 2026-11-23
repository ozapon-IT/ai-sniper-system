"""レバレッジ上限によるロット制限と V_safe 算出（§4.2）。

V_leverage_cap = floor((A_equity × MAX_LEVERAGE) / (P_current × 100)) × 100
V_safe = min(V_rounded, V_leverage_cap)
証拠金不足時は NO_ENTRY を返す。
"""

from __future__ import annotations

import math
import os

NO_ENTRY = 0  # 発注見送りを示すセンチネル値


def _get_max_leverage() -> float:
    """環境変数 MAX_LEVERAGE を検証して返す（§4.2）。

    既定は spec/.env.example と同じ 10（安全側）。env 未設定時に過大なロットを許さない。
    実資金系のため、0 以下（ゼロ除算・ロット上限/必要証拠金の破綻）は fail-loud で弾く。
    """
    max_leverage = float(os.environ.get("MAX_LEVERAGE", "10"))
    if max_leverage <= 0:
        raise ValueError(f"MAX_LEVERAGE は正の数でなければなりません: {max_leverage}")
    return max_leverage


def calculate_leverage_cap(equity: float, current_price: float) -> int:
    """レバレッジ上限による最大ロット数を算出する（§4.2）。

    Args:
        equity: 口座有効純資産（円）
        current_price: 現在価格（発注時の参考価格）

    Returns:
        100通貨単位のレバレッジ上限ロット数（int）
    """
    max_leverage = _get_max_leverage()
    cap = math.floor((equity * max_leverage) / (current_price * 100)) * 100
    return int(cap)


def calculate_v_safe(
    v_rounded: int,
    equity: float,
    current_price: float,
    available_amount: float,
) -> int:
    """V_safe を算出する（§4.2）。

    V_safe = min(V_rounded, V_leverage_cap)
    V_safe > 0 かつ 証拠金（availableAmount）が必要証拠金を上回る場合に発注可能。
    証拠金不足時は NO_ENTRY (0) を返す。

    Args:
        v_rounded: ポジションサイジング算出済みの丸めロット数
        equity: 口座有効純資産（円）
        current_price: 現在価格
        available_amount: 口座利用可能証拠金（円）

    Returns:
        発注ロット数（int）。証拠金不足 or ロット0 の場合は NO_ENTRY (0)。
    """
    v_leverage_cap = calculate_leverage_cap(equity, current_price)
    v_safe = min(v_rounded, v_leverage_cap)

    if v_safe <= 0:
        return NO_ENTRY

    # 必要証拠金の概算: 建玉額 / MAX_LEVERAGE
    # 名目建玉 = v_safe 通貨 × P_current × (1/100通貨単位) → v_safe × P_current
    # USD/JPY の場合 1通貨 ≒ P_current JPY
    max_leverage = _get_max_leverage()
    required_margin = (v_safe * current_price) / max_leverage
    if available_amount < required_margin:
        return NO_ENTRY

    return int(v_safe)
