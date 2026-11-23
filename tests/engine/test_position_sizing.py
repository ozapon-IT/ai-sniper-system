"""engine/position_sizing.py のユニットテスト（§4.1, §4.2）。"""

from __future__ import annotations

import json
import logging

import pytest

from backend.src.engine.position_sizing import calculate_position_size, resolve_order_size


class TestCalculatePositionSize:
    def test_basic_v_raw(self) -> None:
        """Equity=100000, RISK=0.01, D_stoploss=50pips → V_raw=2000（§受け入れ条件）。

        entry=150.000, stop=149.500 → D_stoploss=50pips
        L_max = 100000 * 0.01 = 1000 JPY
        D_stoploss_jpy = 50 * 0.01 = 0.50 JPY/通貨
        V_raw = 1000 / 0.50 = 2000
        """
        result = calculate_position_size(
            entry_price=150.000,
            stop_loss=149.500,
            equity=100_000.0,
            risk_percent=0.01,
        )
        assert result["d_stoploss_pips"] == pytest.approx(50.0)
        assert result["l_max"] == pytest.approx(1000.0)
        assert result["v_raw"] == pytest.approx(2000.0)
        assert result["v_rounded"] == 2000

    def test_v_rounded_floors_to_100(self) -> None:
        """V_raw が 100 の倍数でない場合は切り捨て。"""
        # entry=150.000, stop=149.530 → D_stoploss=47pips
        # L_max=1000, D_stoploss_jpy=0.47, V_raw≈2127.6 → V_rounded=2100
        result = calculate_position_size(
            entry_price=150.000,
            stop_loss=149.530,
            equity=100_000.0,
            risk_percent=0.01,
        )
        assert result["v_rounded"] % 100 == 0
        assert result["v_rounded"] <= result["v_raw"]

    def test_uses_env_risk_percent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """RISK_PERCENT 環境変数が参照される。"""
        monkeypatch.setenv("RISK_PERCENT", "0.02")
        result = calculate_position_size(
            entry_price=150.000,
            stop_loss=149.500,
            equity=100_000.0,
        )
        assert result["l_max"] == pytest.approx(2000.0)

    def test_zero_stoploss_raises(self) -> None:
        """entry と stop_loss が同値なら ValueError。"""
        with pytest.raises(ValueError, match="D_stoploss_pips が 0"):
            calculate_position_size(150.0, 150.0, 100_000.0, 0.01)

    def test_short_direction(self) -> None:
        """SHORT: stop が entry より高くても pips は絶対値で計算。"""
        result = calculate_position_size(
            entry_price=150.000,
            stop_loss=150.500,  # SHORT では stop_loss > entry
            equity=100_000.0,
            risk_percent=0.01,
        )
        assert result["d_stoploss_pips"] == pytest.approx(50.0)
        assert result["v_raw"] == pytest.approx(2000.0)


class TestResolveOrderSize:
    def test_mvp_mode_returns_100(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MVP_MODE=True: 発注は 100 通貨固定（§4.2 受け入れ条件）。"""
        monkeypatch.setenv("MVP_MODE", "True")
        size = resolve_order_size(v_safe=2000, v_raw=2000.0)
        assert size == 100

    def test_mvp_mode_logs_v_raw(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """MVP_MODE=True でも V_raw がログに記録される（§4.2 受け入れ条件）。"""
        monkeypatch.setenv("MVP_MODE", "True")
        with caplog.at_level(logging.INFO, logger="backend.src.engine.position_sizing"):
            resolve_order_size(v_safe=2000, v_raw=2345.6)
        log_text = "\n".join(caplog.messages)
        payload = json.loads(log_text)
        assert payload["v_raw"] == pytest.approx(2345.6)
        assert payload["order_size"] == 100

    def test_non_mvp_mode_returns_v_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MVP_MODE=False: v_safe をそのまま返す。"""
        monkeypatch.setenv("MVP_MODE", "False")
        size = resolve_order_size(v_safe=2000, v_raw=2000.0)
        assert size == 2000

    def test_no_entry_overrides_mvp_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """v_safe=0 (NO_ENTRY) なら MVP_MODE=True でも 0 を返す（§CodeRabbit 指摘: Critical）。"""
        monkeypatch.setenv("MVP_MODE", "True")
        size = resolve_order_size(v_safe=0, v_raw=2000.0)
        assert size == 0

    def test_log_contains_timestamp_and_standard_keys(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ログに timestamp / timestamp_ms / llm_tokens（標準キー）が含まれる（§5.2 税務証跡）。"""
        monkeypatch.setenv("MVP_MODE", "False")
        with caplog.at_level(logging.INFO, logger="backend.src.engine.position_sizing"):
            resolve_order_size(v_safe=2000, v_raw=2000.0, llm_token_usage=512)
        payload = json.loads(caplog.messages[0])
        assert "timestamp" in payload
        assert isinstance(payload["timestamp_ms"], int)
        assert payload["llm_tokens"] == 512
        assert "llm_token_usage" not in payload

    def test_no_entry_log_uses_standard_keys(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """v_safe=0 経路のログも標準キー llm_tokens / timestamp_ms を使う（§5.2）。"""
        monkeypatch.setenv("MVP_MODE", "True")
        with caplog.at_level(logging.INFO, logger="backend.src.engine.position_sizing"):
            resolve_order_size(v_safe=0, v_raw=2000.0, llm_token_usage=256)
        payload = json.loads(caplog.messages[0])
        assert payload["order_size"] == 0
        assert isinstance(payload["timestamp_ms"], int)
        assert payload["llm_tokens"] == 256
        assert "llm_token_usage" not in payload
