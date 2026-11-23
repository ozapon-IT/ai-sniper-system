"""generate_decision のユニットテスト（#16, §3, §5.2）。

実 API は叩かず、messages.parse を持つフェイククライアントを注入する。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.src.llm.client import (
    DEFAULT_DECISION_MODEL,
    generate_decision,
)
from backend.src.llm.prompt import EconomicEvent, PromptContext
from backend.src.llm.trade_decision import TradeDecision


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeResponse:
    def __init__(
        self,
        parsed_output: TradeDecision | None,
        stop_reason: str = "end_turn",
        input_tokens: int = 100,
        output_tokens: int = 30,
    ) -> None:
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.usage = _FakeUsage(input_tokens, output_tokens)


class _FakeMessages:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_kwargs: dict[str, object] = {}

    def parse(self, **kwargs: object) -> _FakeResponse:
        self.last_kwargs = kwargs
        return self._response


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _FakeMessages(response)


def _ctx() -> PromptContext:
    return PromptContext(
        current_time_jst="2026-06-29T07:30:00",
        day_of_week="Monday",
        symbol="USD_JPY",
        current_price_mid=159.812,
        pivot_p=159.9,
        pivot_r1=160.45,
        pivot_r2=160.8,
        pivot_s1=159.18,
        pivot_s2=158.6,
        atr_14d=0.42,
        prev_day_change=0.65,
        equity_jpy=100_000.0,
        economic_events=[EconomicEvent("21:30", "USD", "Core CPI", "High")],
    )


def _decision() -> TradeDecision:
    return TradeDecision(
        direction="LONG",
        execution_required=True,
        order_type="LIMIT",
        entry_price=159.18,
        take_profit=160.45,
        referenced_pivot_level="S1",
        rationale="押し目買い",
        sl_profile="wide",
        rr_target=1.5,
    )


def test_normal_returns_parsed_decision_with_tokens() -> None:
    client = _FakeClient(_FakeResponse(_decision(), input_tokens=120, output_tokens=40))
    result = generate_decision(_ctx(), client=client)
    assert result.decision.direction == "LONG"
    assert result.decision.should_enter is True
    assert result.input_tokens == 120
    assert result.output_tokens == 40
    assert result.fallback is None  # 実際に判断を返したときは None（#404）


def test_refusal_falls_back_to_no_entry() -> None:
    """stop_reason=refusal は NO_ENTRY に倒し、トークン数は記録する（安全側, §3）。"""
    client = _FakeClient(
        _FakeResponse(None, stop_reason="refusal", input_tokens=15, output_tokens=0)
    )
    result = generate_decision(_ctx(), client=client)
    assert result.decision.direction == "NO_ENTRY"
    assert result.decision.should_enter is False
    assert result.input_tokens == 15  # 税務証跡は拒否時も記録（§5.2）
    assert result.fallback == "refusal"


def test_parse_failure_falls_back_to_no_entry() -> None:
    """parsed_output が None（パース失敗）でも NO_ENTRY に倒す。"""
    client = _FakeClient(_FakeResponse(None, stop_reason="end_turn"))
    result = generate_decision(_ctx(), client=client)
    assert result.decision.direction == "NO_ENTRY"
    assert result.fallback == "parse_failed"


class _RaisingMessages:
    def parse(self, **kwargs: object) -> object:
        raise RuntimeError("simulated transport/auth/SDK error")


class _RaisingClient:
    def __init__(self) -> None:
        self.messages = _RaisingMessages()


def test_exception_falls_back_to_no_entry() -> None:
    """messages.parse が例外（通信/認証/SDK）でも fail-closed で NO_ENTRY に倒す（§3）。"""
    result = generate_decision(_ctx(), client=_RaisingClient())
    assert result.decision.direction == "NO_ENTRY"
    assert result.decision.should_enter is False
    assert result.input_tokens == 0
    assert result.fallback == "call_failed"


def test_model_taken_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DECISION_MODEL", "claude-haiku-4-5")
    client = _FakeClient(_FakeResponse(_decision()))
    result = generate_decision(_ctx(), client=client)
    assert result.model == "claude-haiku-4-5"
    assert client.messages.last_kwargs["model"] == "claude-haiku-4-5"


def test_default_model_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DECISION_MODEL", raising=False)
    client = _FakeClient(_FakeResponse(_decision()))
    result = generate_decision(_ctx(), client=client)
    assert result.model == DEFAULT_DECISION_MODEL == "claude-sonnet-4-6"


def test_explicit_model_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DECISION_MODEL", "claude-sonnet-4-6")
    client = _FakeClient(_FakeResponse(_decision()))
    result = generate_decision(_ctx(), client=client, model="claude-opus-4-8")
    assert result.model == "claude-opus-4-8"


def test_output_format_is_trade_decision() -> None:
    """messages.parse に output_format=TradeDecision が渡される。"""
    client = _FakeClient(_FakeResponse(_decision()))
    generate_decision(_ctx(), client=client)
    assert client.messages.last_kwargs["output_format"] is TradeDecision


# ---------------------------------------------------------------------------
# SDK リクエストタイムアウト（Lambda タイムアウトとの整合）
# ---------------------------------------------------------------------------


def _construct_real_client() -> object:
    """llm_client を渡さず Anthropic() 実体生成パスを通し、コンストラクタ引数を捕まえる。"""
    import anthropic

    from backend.src.llm import client as llm_client_mod

    with (
        patch.object(anthropic, "Anthropic") as mock_ctor,
        patch(
            "backend.src.lambda_auth.get_anthropic_api_key",
            return_value="sk-test",
        ),
    ):
        mock_ctor.return_value = _FakeClient(_FakeResponse(_decision()))
        llm_client_mod.generate_decision(_ctx())
    return mock_ctor


def test_read_timeout_is_explicit() -> None:
    """SDK 既定（read/write 600秒）のままだと decision Lambda の 120 秒を超える。"""
    timeout = _construct_real_client().call_args.kwargs["timeout"]  # type: ignore[attr-defined]
    assert timeout.read == 60.0
    assert timeout.write == 60.0


def test_connect_timeout_stays_tight() -> None:
    """connect まで緩めないこと（float を渡すと 4 要素すべて同値になる）。"""
    timeout = _construct_real_client().call_args.kwargs["timeout"]  # type: ignore[attr-defined]
    assert timeout.connect == 5.0


def test_retries_disabled() -> None:
    """タイムアウトは再試行されるため max_retries=0 でないと上限が効かない。"""
    kwargs = _construct_real_client().call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["max_retries"] == 0


def test_effective_ceiling_fits_lambda_timeout() -> None:
    """実効上限（read timeout × 試行回数）が Lambda の 120 秒に収まること。"""
    kwargs = _construct_real_client().call_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["timeout"].read * (kwargs["max_retries"] + 1) < 120
