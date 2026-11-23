"""TradeDecision の JSON スキーマ検証（付録A.1, §3.2 / RD-12）。

LLM 出力スキーマに stop_loss / ロットが現れないことを保証する。
atr_multiplier は sl_profile から Python 導出のためスキーマには含めない。
"""

from __future__ import annotations

from backend.src.llm.trade_decision import TradeDecision


def test_schema_fields_match_appendix_a1() -> None:
    """スキーマのフィールドが付録A.1 / RD-12 と一致する。"""
    fields = set(TradeDecision.model_fields.keys())
    expected = {
        "direction",
        "execution_required",
        "order_type",
        "entry_price",
        "rr_target",
        "referenced_pivot_level",
        "rationale",
        "sentiment_summary",
        "sl_profile",
    }
    assert fields == expected


def test_schema_has_no_stop_loss_take_profit_or_lot() -> None:
    """JSON スキーマに stop_loss / take_profit / ロット系が存在しない（§3.2 Ver 3.6 / #36）。"""
    schema = TradeDecision.model_json_schema()
    props = set(schema.get("properties", {}).keys())
    for forbidden in ("stop_loss", "take_profit", "v_raw", "v_safe", "size", "lot"):
        assert forbidden not in props
    # atr_multiplier は LLM 出力ではなく sl_profile から Python 導出
    assert "atr_multiplier" not in props


def test_field_order_cot_first() -> None:
    """CoT-first フィールド順のロック（付録A.1 §3.2）。rationale/sentiment_summary が direction より前。"""
    keys = list(TradeDecision.model_fields.keys())
    expected_order = [
        "sentiment_summary",
        "rationale",
        "direction",
        "referenced_pivot_level",
        "order_type",
        "entry_price",
        "sl_profile",
        "rr_target",
        "execution_required",
    ]
    assert keys == expected_order
    assert keys.index("rationale") < keys.index("direction")
    assert keys.index("sentiment_summary") < keys.index("direction")
    required = set(TradeDecision.model_json_schema().get("required", []))
    assert required == {"rationale", "direction", "execution_required", "sl_profile"}


def test_direction_enum_values() -> None:
    schema = TradeDecision.model_json_schema()
    dumped = TradeDecision(
        direction="SHORT",
        execution_required=False,
        rationale="x",
        sl_profile="standard",
    ).model_dump()
    assert dumped["direction"] == "SHORT"
    assert "properties" in schema
