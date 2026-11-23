"""logging_config.configure_logging() のユニットテスト（§5.2）。"""

from __future__ import annotations

import json
import logging

import pytest

import backend.src.logging_config as lc
from backend.src.logging_config import configure_logging


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    """各テスト前後に設定フラグとルートハンドラをリセットする。"""
    lc._configured = False
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    root.handlers.clear()
    yield
    lc._configured = False
    root.handlers.clear()
    root.handlers.extend(original_handlers)
    root.setLevel(original_level)


class TestConfigureLogging:
    def test_stdlib_logger_outputs_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        """stdlib logging.getLogger でも JSON 1行で出力されること。"""
        configure_logging()

        logging.getLogger("test.module").info("hello_json")

        out = capsys.readouterr().out.strip()
        assert out, "ログ出力が空"
        payload = json.loads(out)
        assert payload["event"] == "hello_json"
        assert "timestamp" in payload
        assert "level" in payload

    def test_extra_fields_appear_as_json_keys(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """extra= で渡したフィールドが JSON のトップレベルキーとして含まれること（§5.2 トークン数など）。"""
        configure_logging()

        logging.getLogger("test.extra").info(
            "order_event",
            extra={"tokens_used": 123, "date": "2026-06-27"},
        )

        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["tokens_used"] == 123
        assert payload["date"] == "2026-06-27"

    def test_idempotent_second_call_does_not_add_handler(self) -> None:
        """2回呼んでもハンドラが重複追加されないこと。"""
        configure_logging()
        count = len(logging.getLogger().handlers)
        configure_logging()
        assert len(logging.getLogger().handlers) == count

    def test_replaces_existing_handlers(self) -> None:
        """Lambda runtime のデフォルトハンドラが置き換えられ重複出力しないこと。"""
        dummy = logging.StreamHandler()
        logging.getLogger().addHandler(dummy)

        configure_logging()

        # pytest の LogCaptureHandler を除いた実ハンドラが JSON ハンドラ1本のみであること
        non_test_handlers = [
            h for h in logging.getLogger().handlers if "LogCapture" not in type(h).__name__
        ]
        assert len(non_test_handlers) == 1
        assert non_test_handlers[0] is not dummy

    def test_json_string_event_expanded_to_top_level_keys(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """logger.info(json.dumps({...})) パターンのキーがトップレベルに展開されること（§5.2 トークン数経路）。"""
        configure_logging()

        import json as _json
        logging.getLogger("position_sizing").info(
            _json.dumps({"v_raw": 1500.0, "llm_token_usage": 200, "order_size": 100}),
        )

        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["v_raw"] == 1500.0
        assert payload["llm_token_usage"] == 200
        assert payload["order_size"] == 100

    def test_position_sizing_caplog_compatibility(
        self, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """position_sizing の caplog+json.loads パターンが configure_logging() 後も維持され、
        かつ stdout の JSON でも v_raw がトップレベルキーとして出力されること。"""
        configure_logging()

        with caplog.at_level(logging.INFO, logger="compat_test"):
            logging.getLogger("compat_test").info(json.dumps({"v_raw": 1000.0}))

        # caplog.messages[0] は生メッセージ（JSON 文字列）のまま
        assert caplog.messages, "caplog にログが記録されていない"
        parsed_msg = json.loads(caplog.messages[0])
        assert parsed_msg["v_raw"] == 1000.0

        # stdout には v_raw がトップレベルキーとして出力される
        stdout_line = capsys.readouterr().out.strip()
        assert stdout_line, "stdout に出力がない"
        payload = json.loads(stdout_line)
        assert payload["v_raw"] == 1000.0

    def test_logger_name_in_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        """logger 名が JSON の logger キーに含まれること。"""
        configure_logging()

        logging.getLogger("backend.src.batch.decision_batch").info("startup")

        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["logger"] == "backend.src.batch.decision_batch"

    def test_reserved_keys_in_message_do_not_overwrite_metadata(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """メッセージ本文の JSON に予約キーが含まれても §5.2 の監査メタデータが上書きされないこと。"""
        configure_logging()

        logging.getLogger("test.reserved").info(
            json.dumps({
                "level": "fake",
                "logger": "spoof",
                "timestamp": "1970-01-01T00:00:00Z",
                "llm_token_usage": 200,
            }),
        )

        payload = json.loads(capsys.readouterr().out.strip())
        # 実メタデータが保持されていること
        assert payload["level"] == "info"
        assert payload["logger"] == "test.reserved"
        assert payload["timestamp"] != "1970-01-01T00:00:00Z"
        # 衝突キーは payload_reserved ネストに退避されていること（フラット衝突を防ぐ）
        reserved = payload["payload_reserved"]
        assert reserved["level"] == "fake"
        assert reserved["logger"] == "spoof"
        assert reserved["timestamp"] == "1970-01-01T00:00:00Z"
        # 予約外のフィールドは通常通りトップレベルに展開
        assert payload["llm_token_usage"] == 200
