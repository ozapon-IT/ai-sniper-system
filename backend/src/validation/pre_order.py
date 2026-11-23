"""発注前価格整合性バリデーション（§3.4）.

POST /private/v1/ifoOrder 送信前に必須実行する。
違反時は ValidationError を送出し、呼び出し元は発注をスキップする。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

logger = logging.getLogger(__name__)

# E05 (#12) で tick=0.001 確定済み（docs/spikes/E05_klines_appendix_b.md）。
USD_JPY_TICK_SIZE = Decimal("0.001")

# GMO FX API の USD/JPY エントリー最小乖離（現値からの最小距離）。
# SPIKE-TODO(#91): Phase 2 実発注スパイクで価格序列違反エラーコードと共に確定予定。
USD_JPY_MIN_ENTRY_DISTANCE = Decimal("0.003")

# 現値乖離上限の係数の既定（§3.4・Ver 3.6）。|entry − current_mid| ≤ 係数 × ATR14。
MAX_ENTRY_DISTANCE_ATR_DEFAULT = 0.3


class ValidationReasonCode(StrEnum):
    PRICE_ORDER_VIOLATION = "PRICE_ORDER_VIOLATION"
    ORDER_TYPE_ENTRY_POSITION_VIOLATION = "ORDER_TYPE_ENTRY_POSITION_VIOLATION"
    RR_RATIO_BELOW_MINIMUM = "RR_RATIO_BELOW_MINIMUM"
    TICK_SIZE_VIOLATION = "TICK_SIZE_VIOLATION"
    MIN_ENTRY_DISTANCE_VIOLATION = "MIN_ENTRY_DISTANCE_VIOLATION"
    MAX_ENTRY_DISTANCE_VIOLATION = "MAX_ENTRY_DISTANCE_VIOLATION"
    LOT_BELOW_MINIMUM = "LOT_BELOW_MINIMUM"
    LOT_NOT_MULTIPLE_OF_100 = "LOT_NOT_MULTIPLE_OF_100"


@dataclass
class ValidationError(Exception):
    """発注前バリデーション違反（§3.4）.

    Attributes:
        reason_code: 違反理由コード
        detail: 人間可読な説明
    """

    reason_code: ValidationReasonCode
    detail: str

    def __str__(self) -> str:
        return f"[{self.reason_code}] {self.detail}"


def _quantize(price: float) -> Decimal:
    return Decimal(str(price)).quantize(USD_JPY_TICK_SIZE)


def _is_tick_aligned(price: float) -> bool:
    d = Decimal(str(price))
    return d == d.quantize(USD_JPY_TICK_SIZE)


def validate_pre_order(
    *,
    side: Literal["LONG", "SHORT"],
    entry_price: float,
    take_profit: float,
    stop_loss: float,
    lot: int,
    current_price: float,
    atr: float,
    order_type: Literal["LIMIT", "STOP"] = "LIMIT",
    min_rr_ratio: float | None = None,
    max_entry_distance_atr: float | None = None,
) -> None:
    """発注前バリデーション（§3.4）を実行する。

    違反時は ValidationError を送出する。正常時は None を返す。

    Args:
        side: "LONG" or "SHORT"
        entry_price: エントリー価格
        take_profit: 利確価格
        stop_loss: 損切り価格（Python 算出済み）
        lot: 発注数量（通貨単位）
        current_price: 現在レート（最小乖離チェック用）
        atr: 日足 ATR14。現値乖離上限チェックに使用（§3.4・Ver 3.6）。**必須**
            （省略すると TypeError。安全ガードの無音スキップを防ぐ）。
        order_type: `LIMIT`（押し目/戻り）または `STOP`（ブレイク）。entry と現値の位置関係を検証する。
        min_rr_ratio: R:R 下限（省略時は環境変数 MIN_RR_RATIO、デフォルト 1.0）
        max_entry_distance_atr: 現値乖離上限の係数（省略時は環境変数
            MAX_ENTRY_DISTANCE_ATR、デフォルト 0.3）。
    """
    if min_rr_ratio is None:
        min_rr_ratio = float(os.environ.get("MIN_RR_RATIO", "1.0"))
    if max_entry_distance_atr is None:
        max_entry_distance_atr = float(
            os.environ.get("MAX_ENTRY_DISTANCE_ATR", str(MAX_ENTRY_DISTANCE_ATR_DEFAULT))
        )

    ep = Decimal(str(entry_price))
    tp = Decimal(str(take_profit))
    sl = Decimal(str(stop_loss))
    cur = Decimal(str(current_price))

    # 1. 方向別価格序列チェック
    if side not in ("LONG", "SHORT"):
        detail = f"未対応の side={side!r} です"
        _log_violation(ValidationReasonCode.PRICE_ORDER_VIOLATION, detail)
        raise ValidationError(ValidationReasonCode.PRICE_ORDER_VIOLATION, detail)

    if side == "LONG":
        if not (sl < ep < tp):
            detail = f"LONG の価格序列違反: SL={sl} < Entry={ep} < TP={tp} を満たしません"
            _log_violation(ValidationReasonCode.PRICE_ORDER_VIOLATION, detail)
            raise ValidationError(ValidationReasonCode.PRICE_ORDER_VIOLATION, detail)
    else:  # SHORT
        if not (tp < ep < sl):
            detail = f"SHORT の価格序列違反: TP={tp} < Entry={ep} < SL={sl} を満たしません"
            _log_violation(ValidationReasonCode.PRICE_ORDER_VIOLATION, detail)
            raise ValidationError(ValidationReasonCode.PRICE_ORDER_VIOLATION, detail)

    # 1b. order_type と entry 価格位置（LIMIT=押し目/戻り、STOP=ブレイク）
    if order_type == "LIMIT":
        if side == "LONG" and ep > cur:
            detail = (
                f"LONG LIMIT は現値以下の押し目が必要です: entry={ep} > current={cur}"
            )
            _log_violation(ValidationReasonCode.ORDER_TYPE_ENTRY_POSITION_VIOLATION, detail)
            raise ValidationError(ValidationReasonCode.ORDER_TYPE_ENTRY_POSITION_VIOLATION, detail)
        if side == "SHORT" and ep < cur:
            detail = (
                f"SHORT LIMIT は現値以上の戻りが必要です: entry={ep} < current={cur}"
            )
            _log_violation(ValidationReasonCode.ORDER_TYPE_ENTRY_POSITION_VIOLATION, detail)
            raise ValidationError(ValidationReasonCode.ORDER_TYPE_ENTRY_POSITION_VIOLATION, detail)
    elif order_type == "STOP":
        if side == "LONG" and ep < cur:
            detail = (
                f"LONG STOP は現値以上のブレイクが必要です: entry={ep} < current={cur}"
            )
            _log_violation(ValidationReasonCode.ORDER_TYPE_ENTRY_POSITION_VIOLATION, detail)
            raise ValidationError(ValidationReasonCode.ORDER_TYPE_ENTRY_POSITION_VIOLATION, detail)
        if side == "SHORT" and ep > cur:
            detail = (
                f"SHORT STOP は現値以下のブレイクが必要です: entry={ep} > current={cur}"
            )
            _log_violation(ValidationReasonCode.ORDER_TYPE_ENTRY_POSITION_VIOLATION, detail)
            raise ValidationError(ValidationReasonCode.ORDER_TYPE_ENTRY_POSITION_VIOLATION, detail)
    else:
        detail = f"未対応の order_type={order_type!r} です"
        _log_violation(ValidationReasonCode.ORDER_TYPE_ENTRY_POSITION_VIOLATION, detail)
        raise ValidationError(ValidationReasonCode.ORDER_TYPE_ENTRY_POSITION_VIOLATION, detail)

    # 2. R:R 下限チェック
    reward = abs(tp - ep)
    risk = abs(ep - sl)
    if risk == 0 or reward < Decimal(str(min_rr_ratio)) * risk:
        actual_rr = float(reward / risk) if risk != 0 else 0.0
        detail = (
            f"R:R={actual_rr:.3f} が MIN_RR_RATIO={min_rr_ratio} を下回ります "
            f"(reward={reward}, risk={risk})"
        )
        _log_violation(ValidationReasonCode.RR_RATIO_BELOW_MINIMUM, detail)
        raise ValidationError(ValidationReasonCode.RR_RATIO_BELOW_MINIMUM, detail)

    # 3. tick size 整合チェック
    for name, val in [
        ("entry_price", entry_price),
        ("take_profit", take_profit),
        ("stop_loss", stop_loss),
    ]:
        if not _is_tick_aligned(val):
            detail = f"{name}={val} が tick size {USD_JPY_TICK_SIZE} に整合していません"
            _log_violation(ValidationReasonCode.TICK_SIZE_VIOLATION, detail)
            raise ValidationError(ValidationReasonCode.TICK_SIZE_VIOLATION, detail)

    # 4. 現値からの最小乖離チェック（エントリー価格 vs 現在レート）
    entry_distance = abs(ep - cur)
    if entry_distance < USD_JPY_MIN_ENTRY_DISTANCE:
        detail = (
            f"エントリー価格={ep} と現値={cur} の乖離={entry_distance} が "
            f"最小乖離={USD_JPY_MIN_ENTRY_DISTANCE} を下回ります"
        )
        _log_violation(ValidationReasonCode.MIN_ENTRY_DISTANCE_VIOLATION, detail)
        raise ValidationError(ValidationReasonCode.MIN_ENTRY_DISTANCE_VIOLATION, detail)

    # 4b. 現値からの乖離上限チェック（§3.4・Ver 3.6）。atr は必須のため常に評価する。
    # 遠すぎるアンカー（約定率が大きく落ちる）を機械的に排除する。
    max_distance = Decimal(str(max_entry_distance_atr)) * Decimal(str(atr))
    if entry_distance > max_distance:
        detail = (
            f"エントリー価格={ep} と現値={cur} の乖離={entry_distance} が "
            f"上限={max_distance}（{max_entry_distance_atr}×ATR14={atr}）を超えています"
        )
        _log_violation(ValidationReasonCode.MAX_ENTRY_DISTANCE_VIOLATION, detail)
        raise ValidationError(ValidationReasonCode.MAX_ENTRY_DISTANCE_VIOLATION, detail)

    # 5. ロット下限・100通貨単位チェック
    if lot < 100:
        detail = f"lot={lot} が最小ロット 100 通貨を下回ります"
        _log_violation(ValidationReasonCode.LOT_BELOW_MINIMUM, detail)
        raise ValidationError(ValidationReasonCode.LOT_BELOW_MINIMUM, detail)

    if lot % 100 != 0:
        detail = f"lot={lot} が 100 通貨単位になっていません"
        _log_violation(ValidationReasonCode.LOT_NOT_MULTIPLE_OF_100, detail)
        raise ValidationError(ValidationReasonCode.LOT_NOT_MULTIPLE_OF_100, detail)

    logger.info(
        {
            "event": "pre_order_validation_passed",
            "side": side,
            "order_type": order_type,
            "entry_price": entry_price,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "lot": lot,
            "rr_ratio": float(reward / risk),
        }
    )


def _log_violation(reason_code: ValidationReasonCode, detail: str) -> None:
    logger.warning(
        {
            "event": "pre_order_validation_failed",
            "reason_code": reason_code.value,
            "detail": detail,
        }
    )
