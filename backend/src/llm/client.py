"""判断バッチの意思決定 LLM クライアント（#16, §3, 付録A）。

- Anthropic `messages.parse` + Pydantic で TradeDecision を型安全に取得。
- モデルは DECISION_MODEL（既定 claude-sonnet-4-6）を env から取得。
- トークン消費数を記録（税務証跡 §5.2）。
- stop_reason == "refusal" / パース失敗 は安全側で **NO_ENTRY** に倒す。
- LLM は stop_loss/ロットを出さない（§3.2）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
import structlog

from .prompt import SYSTEM_PROMPT, PromptContext, build_user_prompt
from .trade_decision import TradeDecision, no_entry

_log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

DEFAULT_DECISION_MODEL = "claude-sonnet-4-6"
DEFAULT_NIGHT_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 1024

# Anthropic SDK のリクエストタイムアウト（秒）。
# 🔴 **明示しないと SDK 既定の 600 秒**になり、decision Lambda の 120 秒を大きく超える。
# 既定のままだと LLM がハングしたとき Lambda が先に落ち、判断ログすら残らない。
# 明示しておけば SDK 側が先に諦め、下の except が fail-closed で NO_ENTRY に倒すため、
# 「見送った」という記録が残る（AGENTS.md #1・§3）。
# max_tokens は 1024 と小さく、実測でも判断バッチの LLM 呼び出しは 10 秒未満。
#
# 🔴 float を渡すと connect にも同じ値が入り、SDK 既定の connect=5 秒より**緩くなる**。
# 待ちたいのは生成（read）であって接続確立ではないため connect は既定の 5 秒に据える。
_REQUEST_TIMEOUT_SECONDS = 60.0
_REQUEST_TIMEOUT = httpx.Timeout(_REQUEST_TIMEOUT_SECONDS, connect=5.0)


#: LLM が判断を返せず、安全側の NO_ENTRY に倒したときの理由（#404）。
#: `None` は LLM が実際に判断を返したことを表す。見送り（NO_ENTRY の判断）と、判断できなかった
#: ことを区別するために使う（後者は再起動で回復し得るので、判断済みマーカーを書かない）。
LlmFallback = Literal["call_failed", "refusal", "parse_failed"]


@dataclass(frozen=True)
class LlmResult:
    """LLM 呼び出し結果。decision と税務証跡用のトークン消費数（§5.2）。"""

    decision: TradeDecision
    input_tokens: int
    output_tokens: int
    model: str
    fallback: LlmFallback | None = None


class _ParseClient(Protocol):
    """`messages.parse` を持つクライアント（Anthropic 実体 / テスト用フェイクを注入可能）。"""

    messages: Any


def _decision_model(model: str | None) -> str:
    if model:
        return model
    return os.environ.get("DECISION_MODEL", DEFAULT_DECISION_MODEL)


def generate_decision(
    context: PromptContext,
    *,
    client: _ParseClient | None = None,
    model: str | None = None,
) -> LlmResult:
    """判断バッチの TradeDecision を生成する。

    Args:
        context: LLM 入力コンテキスト（§3.3）。
        client: messages.parse を持つクライアント。省略時は Anthropic() を生成
                （Secrets Manager の ANTHROPIC_SECRET_NAME、またはローカル許可時の
                ANTHROPIC_API_KEY を使用）。テストではフェイクを注入する。
        model: 使用モデル。省略時は env DECISION_MODEL（既定 claude-sonnet-4-6）。

    Returns:
        LlmResult。refusal / パース失敗時は NO_ENTRY（安全側）。
    """
    resolved_model = _decision_model(model)
    try:
        api: Any = client
        if api is None:
            from anthropic import Anthropic  # 遅延 import（テストは実APIキー不要）

            from backend.src.lambda_auth import get_anthropic_api_key

            api = Anthropic(
                api_key=get_anthropic_api_key(),
                timeout=_REQUEST_TIMEOUT,
                max_retries=0,  # Lambda の残り時間を再試行で食い潰さない
            )
        response = api.messages.parse(
            model=resolved_model,
            max_tokens=_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_prompt(context)}],
            output_format=TradeDecision,
        )
    except Exception as exc:  # noqa: BLE001 — fail-closed: 通信/認証/SDK/検証例外も見送りに倒す
        # LLM 呼び出し失敗で判断バッチを落とさない。安全側で NO_ENTRY（取引しない＝安全）。
        _log.warning(
            "llm_call_failed",
            model=resolved_model,
            error=str(exc),
            rule="fail-closed → NO_ENTRY (§3, AGENTS.md #1)",
        )
        return LlmResult(
            decision=no_entry("LLM 呼び出しに失敗したため見送り"),
            input_tokens=0,
            output_tokens=0,
            model=resolved_model,
            fallback="call_failed",
        )

    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)

    # 安全側: 拒否は NO_ENTRY（content を読む前に判定する）。
    if getattr(response, "stop_reason", None) == "refusal":
        _log.warning(
            "llm_refusal",
            model=resolved_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            rule="安全側で NO_ENTRY (§3, AGENTS.md #1)",
        )
        return LlmResult(
            decision=no_entry("LLM が安全上の理由で判断を拒否したため見送り"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=resolved_model,
            fallback="refusal",
        )

    decision = getattr(response, "parsed_output", None)
    if not isinstance(decision, TradeDecision):
        _log.warning(
            "llm_parse_failed",
            model=resolved_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return LlmResult(
            decision=no_entry("LLM 出力をパースできなかったため見送り"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=resolved_model,
            fallback="parse_failed",
        )

    _log.info(
        "llm_decision",
        model=resolved_model,
        direction=decision.direction,
        execution_required=decision.execution_required,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return LlmResult(
        decision=decision,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=resolved_model,
    )
