"""Lambda 実行時ログ設定 — JSON 構造化出力（§5.2）。

Lambda ハンドラのモジュールトップで configure_logging() を1回呼ぶ。
- structlog ネイティブロガー（gmo_client / trading_day 等）→ JSONRenderer
- stdlib logging ロガー（decision_batch / position_sizing / pre_order 等）→ ProcessorFormatter
どちらも CloudWatch Logs に JSON 1行で出力される。

position_sizing のように logger.info(json.dumps({...})) で流す既存パターンは
_expand_json_event プロセッサが JSON 文字列を展開してトップレベルキーに昇格させる。
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_configured = False

# structlog/ProcessorFormatter が付与する予約キー — メッセージ本文より優先される
# StackInfoRenderer は "stack" を出力するため stack_info ではなく stack を保護する
_RESERVED_KEYS: frozenset[str] = frozenset(
    {"event", "level", "logger", "timestamp", "exception", "stack"}
)


def _expand_json_event(
    logger: Any,  # noqa: ARG001
    method: str,  # noqa: ARG001
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """event が JSON 文字列の場合にトップレベルキーへ展開する。

    position_sizing 等が logger.info(json.dumps({...})) で流すパターンに対応し、
    §5.2 のトークン数フィールドが構造化キーとして出力されるようにする。
    """

    event = event_dict.get("event", "")
    if isinstance(event, str) and event.startswith("{"):
        try:
            parsed = json.loads(event)
            if isinstance(parsed, dict):
                reserved_payload: dict[str, Any] = {}
                for key, value in parsed.items():
                    if key in _RESERVED_KEYS:
                        # 予約キーは専用ネスト payload_reserved に収めて欠落を防ぐ
                        reserved_payload[key] = value
                    else:
                        event_dict[key] = value
                if reserved_payload:
                    event_dict["payload_reserved"] = reserved_payload
                event_dict.pop("event", None)
        except (json.JSONDecodeError, ValueError):
            pass
    return event_dict


def configure_logging(level: int = logging.INFO) -> None:
    """structlog と stdlib logging を JSON 出力で統一設定する。

    冪等: 2回目以降の呼び出しは何もしない。
    Lambda runtime のデフォルトハンドラを置き換えて重複出力を防ぐ。
    """
    global _configured
    if _configured:
        return

    shared_processors: list[structlog.types.Processor] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        _expand_json_event,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Lambda runtime のデフォルトハンドラを除去して JSON 出力に一本化する。
    # pytest の LogCaptureHandler はテスト互換性のために保持する。
    root.handlers = [h for h in root.handlers if "LogCapture" in type(h).__name__]
    root.addHandler(handler)
    root.setLevel(level)

    _configured = True
