"""MacroContextSource / format_macro_line のテスト（Issue #107 / #142 / §3.3）。"""

from __future__ import annotations

import logging
import sys
import urllib.parse
from typing import Any

import httpx
import pytest

from backend.src.adapters.macro_context import (
    MacroContextSource,
    MacroIndicator,
    MacroResult,
    YahooChartMacroSource,
    YFinanceMacroSource,
    build_source,
    format_macro_line,
)

# ---------------------------------------------------------------------------
# Fake source（テスト用）
# ---------------------------------------------------------------------------


class FakeMacroSource(MacroContextSource):
    """決定論的なテスト用マクロソース。"""

    def __init__(self, result: MacroResult) -> None:
        self._result = result

    def fetch(self) -> MacroResult:
        return self._result


def _full_result() -> MacroResult:
    return MacroResult(
        indicators=[
            MacroIndicator(label="US10Y", latest_value=4.25, overnight_change_pct=0.03),
            MacroIndicator(label="S&P先物", latest_value=5200.0, overnight_change_pct=-0.45),
            MacroIndicator(label="日経先物", latest_value=38000.0, overnight_change_pct=0.20),
        ],
        source_ok=True,
    )


def _partial_result() -> MacroResult:
    """一部指標のみ取得できた場合。"""
    return MacroResult(
        indicators=[
            MacroIndicator(label="US10Y", latest_value=4.30, overnight_change_pct=-0.02),
        ],
        source_ok=True,
    )


def _failed_result() -> MacroResult:
    return MacroResult(indicators=[], source_ok=False)


# ---------------------------------------------------------------------------
# format_macro_line
# ---------------------------------------------------------------------------


def test_format_macro_line_full() -> None:
    line = format_macro_line(_full_result())
    assert line.startswith("オーバーナイト米国:")
    # US10Y: 絶対値 + 変化率（%）
    assert "US10Y=4.25%(+0.03%)" in line
    # 先物: 絶対値 + 変化率
    assert "S&P先物=5200.00(-0.45%)" in line
    assert "日経先物=38000.00(+0.20%)" in line


def test_format_macro_line_partial() -> None:
    line = format_macro_line(_partial_result())
    assert "US10Y=4.30%(-0.02%)" in line
    # 他指標は含まれない
    assert "S&P先物" not in line


def test_format_macro_line_empty_returns_empty_string() -> None:
    line = format_macro_line(_failed_result())
    assert line == ""


# ---------------------------------------------------------------------------
# build_source
# ---------------------------------------------------------------------------


def test_build_source_yfinance_backward_compat() -> None:
    # yfinance キーは後方互換で残す（既定ではない）。
    src = build_source("yfinance")
    assert isinstance(src, YFinanceMacroSource)


def test_build_source_yahoo_chart() -> None:
    src = build_source("yahoo_chart")
    assert isinstance(src, YahooChartMacroSource)


def test_build_source_unknown_falls_back_to_yahoo_chart() -> None:
    src = build_source("nonexistent_source")
    assert isinstance(src, YahooChartMacroSource)


def test_build_source_none_defaults_to_yahoo_chart() -> None:
    # #142: 既定を httpx 軽量ソースに変更（本番で yfinance 未同梱の degrade を解消）。
    src = build_source(None)
    assert isinstance(src, YahooChartMacroSource)


# ---------------------------------------------------------------------------
# FakeSource 正常 / 部分欠落 / 全失敗のデグレード確認
# ---------------------------------------------------------------------------


def test_fake_source_full_ok() -> None:
    src = FakeMacroSource(_full_result())
    result = src.fetch()
    assert result.source_ok is True
    assert len(result.indicators) == 3


def test_fake_source_partial_ok() -> None:
    src = FakeMacroSource(_partial_result())
    result = src.fetch()
    assert result.source_ok is True
    assert len(result.indicators) == 1


def test_fake_source_failed_degrade() -> None:
    src = FakeMacroSource(_failed_result())
    result = src.fetch()
    assert result.source_ok is False
    assert result.indicators == []
    # format_macro_line が空文字列を返すことも確認
    assert format_macro_line(result) == ""


# ---------------------------------------------------------------------------
# yfinance 未インストール degrade（#142 / #143: warning のまま = AppErrors 非該当）
# ---------------------------------------------------------------------------


def test_yfinance_import_error_degrades_at_warning_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """yfinance 未インストール時は source_ok=False に degrade し、warning レベルで
    ログされること（error ではない）。AppErrors メトリクスフィルタ（#143）は
    ログレベル基準（$.level = "error"）に変わったため、この warning はアラーム対象外
    であることをここで固定する。"""
    src = YFinanceMacroSource()
    with (
        pytest.MonkeyPatch.context() as mp,
        caplog.at_level(logging.WARNING, logger="backend.src.adapters.macro_context"),
    ):
        mp.setitem(sys.modules, "yfinance", None)
        result = src.fetch()

    assert result.source_ok is False
    assert any(
        r.levelno == logging.WARNING and "macro_context_fetch_failed" in str(r.message)
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# YahooChartMacroSource（#142: httpx 直叩き軽量ソース）
# ---------------------------------------------------------------------------

# 実 API のティッカー（^TNX / ES=F / NKD=F）。build_transport で URL 末尾から復元する。
_SYMBOLS = {"US10Y": "^TNX", "S&P先物": "ES=F", "日経先物": "NKD=F"}


def _chart_payload(closes: list[float | None]) -> dict[str, Any]:
    """Yahoo chart v8 レスポンスの最小形。timestamp は close と同数の連番。"""
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "timestamp": list(range(len(closes))),
                    "indicators": {"quote": [{"close": closes}]},
                }
            ],
        }
    }


_ERROR_PAYLOAD = {"chart": {"result": None, "error": {"code": "Not Found"}}}


def _build_transport(
    per_symbol: dict[str, Any],
    captured: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """symbol -> (payload dict | int status) のマップで応答する MockTransport。

    captured を渡すと各リクエストを記録し、UA ヘッダ検証などに使える。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        symbol = urllib.parse.unquote(request.url.path.rsplit("/", 1)[-1])
        spec = per_symbol.get(symbol)
        if isinstance(spec, int):
            return httpx.Response(spec)
        if spec is None:
            return httpx.Response(404)
        return httpx.Response(200, json=spec)

    return httpx.MockTransport(handler)


def test_yahoo_source_full_ok() -> None:
    # 3 ティッカーとも直近2終値が有効 → 3 指標・source_ok。
    per_symbol = {
        "^TNX": _chart_payload([4.392, 4.475]),
        "ES=F": _chart_payload([5200.0, 5174.0]),  # -0.5%
        "NKD=F": _chart_payload([38000.0, 38076.0]),  # +0.2%
    }
    src = YahooChartMacroSource(transport=_build_transport(per_symbol))
    result = src.fetch()

    assert result.source_ok is True
    assert len(result.indicators) == 3
    by_label = {i.label: i for i in result.indicators}
    assert by_label["US10Y"].latest_value == 4.475
    assert round(by_label["US10Y"].overnight_change_pct, 6) == round(
        (4.475 - 4.392) / 4.392 * 100, 6
    )
    assert round(by_label["S&P先物"].overnight_change_pct, 3) == -0.5
    assert round(by_label["日経先物"].overnight_change_pct, 3) == 0.2


def test_yahoo_source_filters_null_zero_and_uses_last_two_valid() -> None:
    # null / 0.0 / 非有限は除外し、有効終値の直近2本で算出する。
    per_symbol = {
        "^TNX": _chart_payload([4.30, None, 0.0, 4.40, 4.44]),  # 直近2有効=4.40,4.44
        "ES=F": _chart_payload([5000.0, 5100.0]),
        "NKD=F": _chart_payload([37000.0, 37370.0]),
    }
    src = YahooChartMacroSource(transport=_build_transport(per_symbol))
    result = src.fetch()

    by_label = {i.label: i for i in result.indicators}
    assert by_label["US10Y"].latest_value == 4.44
    # (4.44 - 4.40) / 4.40 * 100
    assert round(by_label["US10Y"].overnight_change_pct, 4) == round((4.44 - 4.40) / 4.40 * 100, 4)


def test_yahoo_source_insufficient_history_skips_indicator() -> None:
    # 有効終値が1本のみ → その指標はスキップ、他は残る（部分取得）。
    per_symbol = {
        "^TNX": _chart_payload([None, 4.40]),  # 有効1本 → skip
        "ES=F": _chart_payload([5000.0, 5100.0]),
        "NKD=F": _chart_payload([37000.0, 37370.0]),
    }
    src = YahooChartMacroSource(transport=_build_transport(per_symbol))
    result = src.fetch()

    labels = {i.label for i in result.indicators}
    assert labels == {"S&P先物", "日経先物"}
    assert result.source_ok is True


def test_yahoo_source_http_error_on_one_ticker_degrades_partial() -> None:
    # 1 ティッカーが 500 → その指標のみ落ち、他は成功（部分取得）。
    per_symbol = {
        "^TNX": 500,
        "ES=F": _chart_payload([5000.0, 5100.0]),
        "NKD=F": _chart_payload([37000.0, 37370.0]),
    }
    src = YahooChartMacroSource(transport=_build_transport(per_symbol))
    result = src.fetch()

    labels = {i.label for i in result.indicators}
    assert "US10Y" not in labels
    assert labels == {"S&P先物", "日経先物"}
    assert result.source_ok is True


def test_yahoo_source_all_fail_degrades_to_empty() -> None:
    # 全滅（無効シンボル error payload / 500）→ source_ok False・空・行省略。
    per_symbol = {"^TNX": _ERROR_PAYLOAD, "ES=F": 500, "NKD=F": _ERROR_PAYLOAD}
    src = YahooChartMacroSource(transport=_build_transport(per_symbol))
    result = src.fetch()

    assert result.source_ok is False
    assert result.indicators == []
    assert format_macro_line(result) == ""


def test_yahoo_source_sends_user_agent_header() -> None:
    # UA 無しだと Yahoo は 429 を返す（実挙動確認済み）。UA が必ず送られることを固定。
    captured: list[httpx.Request] = []
    per_symbol = {sym: _chart_payload([1.0, 1.01]) for sym in _SYMBOLS.values()}
    src = YahooChartMacroSource(transport=_build_transport(per_symbol, captured))
    src.fetch()

    assert len(captured) == 3
    for req in captured:
        assert req.headers.get("User-Agent") == "Mozilla/5.0 (compatible; ai-sniper-system/1.0)"
