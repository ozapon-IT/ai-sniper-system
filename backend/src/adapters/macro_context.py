"""オーバーナイト米国マクロコンテキストアダプタ — §3.3 補助入力（Issue #107）。

US10Y（米国債10年利回り）・S&P先物・日経225先物の直近値とオーバーナイト変化率を取得し、
LLM プロンプトの補助文脈として提供する。

取得失敗時はグレースフル・デグレード（MacroResult.source_ok=False）。
経済指標カレンダー（必須入力）とは異なり、マクロ取得失敗で NO_ENTRY に倒さない（§3.3）。

基準点: 直近2日分の終値を使い、
  オーバーナイト変化 = (最新終値 - 前 NY 引け) / 前 NY 引け × 100

既定ソースは `YahooChartMacroSource`（httpx 直叩き・依存ゼロ増）。Lambda 本番で yfinance が
未同梱のためマクロ入力が常時 degrade していた事象（Issue #142）を解消するために採用した。
yfinance ソース（`YFinanceMacroSource`）は依存追加が必要なため既定から外し、後方互換で残す。
"""

from __future__ import annotations

import logging
import math
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx

logger = logging.getLogger(__name__)


@dataclass
class MacroIndicator:
    """単一マクロ指標の直近値とオーバーナイト変化率。"""

    label: str  # プロンプト表示ラベル。例: "US10Y"
    latest_value: float  # 直近の価格/利回り
    overnight_change_pct: float  # 前 NY 引け比変化率（%）


@dataclass
class MacroResult:
    """マクロコンテキスト取得結果。source_ok=False 時は indicators が空になることがある。"""

    indicators: list[MacroIndicator] = field(default_factory=list)
    source_ok: bool = True  # False = 取得失敗（degrade）


# 全マクロソース共通のティッカー定義（単一の正）。
# ラベル -> Yahoo/yfinance シンボル。両ソースがこれを参照し、片方だけ更新して
# yfinance 指定時と yahoo_chart 指定時で対象がズレるのを防ぐ。
#   ^TNX  — 米国債10年利回り（%）
#   ES=F  — S&P500先物
#   NKD=F — 日経225先物（USD建て）
_MACRO_TICKERS: dict[str, str] = {
    "US10Y": "^TNX",
    "S&P先物": "ES=F",
    "日経先物": "NKD=F",
}


class MacroContextSource(ABC):
    @abstractmethod
    def fetch(self) -> MacroResult:
        """直近のオーバーナイトマクロ指標を返す。失敗時は source_ok=False。"""


class YFinanceMacroSource(MacroContextSource):
    """yfinance を用いたマクロ指標取得（非SLA・無料前提。§3.3 degrade 流儀）。

    ティッカー:
      ^TNX  — 米国債10年利回り（%）
      ES=F  — S&P500先物
      NKD=F — 日経225先物（USD建て）
    オーバーナイト変化の基準: 直近2日分の終値。
      change_pct = (today_last - prev_close) / prev_close * 100
    """

    TICKERS: ClassVar[dict[str, str]] = _MACRO_TICKERS
    PERIOD: ClassVar[str] = "2d"
    INTERVAL: ClassVar[str] = "1d"

    def fetch(self) -> MacroResult:
        try:
            import yfinance as yf  # ランタイム import — オプション依存
        except ImportError:
            logger.warning(
                {
                    "event": "macro_context_fetch_failed",
                    "reason": "yfinance not installed",
                    "llm_tokens": None,  # 非LLM経路（LLM 呼び出し未実施, §5.2）
                }
            )
            return MacroResult(indicators=[], source_ok=False)

        indicators: list[MacroIndicator] = []
        any_ok = False

        for label, ticker_sym in self.TICKERS.items():
            try:
                ticker = yf.Ticker(ticker_sym)
                hist = ticker.history(period=self.PERIOD, interval=self.INTERVAL)
                if hist.empty or len(hist) < 1:
                    logger.warning(
                        {
                            "event": "macro_indicator_empty",
                            "label": label,
                            "ticker": ticker_sym,
                            "llm_tokens": None,  # 非LLM経路（LLM 呼び出し未実施, §5.2）
                        }
                    )
                    continue

                latest = float(hist["Close"].iloc[-1])
                if not math.isfinite(latest):
                    logger.warning(
                        {
                            "event": "macro_indicator_nonfinite_latest",
                            "label": label,
                            "ticker": ticker_sym,
                            "latest_repr": repr(latest),
                            "llm_tokens": None,  # 非LLM経路（LLM 呼び出し未実施, §5.2）
                        }
                    )
                    continue
                if len(hist) < 2:
                    logger.warning(
                        {
                            "event": "macro_indicator_insufficient_history",
                            "label": label,
                            "ticker": ticker_sym,
                            "llm_tokens": None,  # 非LLM経路（LLM 呼び出し未実施, §5.2）
                        }
                    )
                    continue

                prev_close = float(hist["Close"].iloc[-2])
                if prev_close == 0:
                    logger.warning(
                        {
                            "event": "macro_indicator_invalid_prev_close",
                            "label": label,
                            "ticker": ticker_sym,
                            "llm_tokens": None,  # 非LLM経路（LLM 呼び出し未実施, §5.2）
                        }
                    )
                    continue
                change_pct = (latest - prev_close) / prev_close * 100
                if not math.isfinite(change_pct):
                    logger.warning(
                        {
                            "event": "macro_indicator_nonfinite_change_pct",
                            "label": label,
                            "ticker": ticker_sym,
                            "change_pct_repr": repr(change_pct),
                            "llm_tokens": None,  # 非LLM経路（LLM 呼び出し未実施, §5.2）
                        }
                    )
                    continue

                indicators.append(
                    MacroIndicator(
                        label=label,
                        latest_value=latest,
                        overnight_change_pct=change_pct,
                    )
                )
                any_ok = True
            except Exception as exc:
                logger.warning(
                    {
                        "event": "macro_indicator_fetch_failed",
                        "label": label,
                        "ticker": ticker_sym,
                        "error": str(exc),
                        "llm_tokens": None,  # 非LLM経路（LLM 呼び出し未実施, §5.2）
                    }
                )

        source_ok = any_ok and bool(indicators)
        logger.info(
            {
                "event": "macro_context_fetched",
                "indicators_count": len(indicators),
                "source_ok": source_ok,
                "llm_tokens": None,  # 非LLM経路（LLM 呼び出し未実施, §5.2）
            }
        )
        return MacroResult(indicators=indicators, source_ok=source_ok)


class YahooChartMacroSource(MacroContextSource):
    """Yahoo Finance chart API (v8) を httpx で直叩きする軽量マクロ取得ソース（依存ゼロ増）。

    yfinance と同一ティッカー（^TNX / ES=F / NKD=F）を使い、日足終値の直近2本から
    オーバーナイト変化率を算出する。yfinance は pandas/numpy を連れてきて Lambda zip が
    肥大するため（かつ本番未同梱で degrade していた Issue #142）、その代替として httpx
    （既にランタイム依存）で実装する。

    エンドポイント（Yahoo の公式ドキュメントは存在しない・非SLA・無料前提。§3.3 の degrade 流儀）:
      GET https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1mo
      レスポンス JSON: chart.result[0].timestamp[] と indicators.quote[0].close[]
        （休場日・未確定日は close が null。無効シンボルは chart.result=null + chart.error）。
    非自明な挙動: **User-Agent ヘッダ必須**。付けないと HTTP 429（"Edge: Too Many Requests"）。
      出典/実挙動確認（参照日 2026-07-02）: curl 実測で UA 付き 3 ティッカー全て 200、
      UA 無しは 429 を確認。公式ドキュメントが無いエンドポイントのため実レスポンスで裏取り。
    """

    # プロンプト表示ラベル -> Yahoo シンボル（yfinance と共通）
    TICKERS: ClassVar[dict[str, str]] = _MACRO_TICKERS
    BASE_URL: ClassVar[str] = "https://query1.finance.yahoo.com/v8/finance/chart/"
    RANGE: ClassVar[str] = "1mo"  # 休場・連休を跨いでも直近2営業日の終値を確実に含める余裕
    INTERVAL: ClassVar[str] = "1d"
    # httpx に float を渡すと connect/read/write/pool の各フェーズに個別に 3s が効く
    # （合計ではない）。単一リクエストの現実的な最悪待ち時間は数フェーズ分になり得るが、
    # 全体の hard cap は decision_batch 側の _MACRO_FETCH_TIMEOUT_SEC=10（別スレッド締切）
    # が担保する。ここは 1 フェーズが詰まったときに即座に諦めて次ティッカーへ進むための値。
    TIMEOUT: ClassVar[float] = 3.0
    # UA 無しだと 429 になるため付与（上のクラス docstring 参照）。
    HEADERS: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0 (compatible; ai-sniper-system/1.0)"
    }

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        # transport はテスト用の注入シーム（httpx.MockTransport）。本番では None。
        self._transport = transport

    def fetch(self) -> MacroResult:
        indicators: list[MacroIndicator] = []
        with httpx.Client(
            timeout=self.TIMEOUT,
            headers=self.HEADERS,
            transport=self._transport,
        ) as client:
            for label, symbol in self.TICKERS.items():
                try:
                    ind = self._fetch_one(client, label, symbol)
                except Exception as exc:
                    logger.warning(
                        {
                            "event": "macro_indicator_fetch_failed",
                            "label": label,
                            "ticker": symbol,
                            "error": str(exc),
                            "llm_tokens": None,  # 非LLM経路（LLM 呼び出し未実施, §5.2）
                        }
                    )
                    continue
                if ind is not None:
                    indicators.append(ind)

        source_ok = bool(indicators)
        logger.info(
            {
                "event": "macro_context_fetched",
                "source": "yahoo_chart",
                "indicators_count": len(indicators),
                "source_ok": source_ok,
                "llm_tokens": None,  # 非LLM経路（LLM 呼び出し未実施, §5.2）
            }
        )
        return MacroResult(indicators=indicators, source_ok=source_ok)

    def _fetch_one(
        self, client: httpx.Client, label: str, symbol: str
    ) -> MacroIndicator | None:
        url = self.BASE_URL + urllib.parse.quote(symbol, safe="")
        resp = client.get(url, params={"interval": self.INTERVAL, "range": self.RANGE})
        resp.raise_for_status()
        closes = self._extract_closes(resp.json())
        if len(closes) < 2:
            logger.warning(
                {
                    "event": "macro_indicator_insufficient_history",
                    "label": label,
                    "ticker": symbol,
                    "valid_close_count": len(closes),
                    "llm_tokens": None,  # 非LLM経路（LLM 呼び出し未実施, §5.2）
                }
            )
            return None

        prev_close, latest = closes[-2], closes[-1]  # _extract_closes が有限・>0 を保証
        change_pct = (latest - prev_close) / prev_close * 100
        if not math.isfinite(change_pct):
            logger.warning(
                {
                    "event": "macro_indicator_nonfinite_change_pct",
                    "label": label,
                    "ticker": symbol,
                    "change_pct_repr": repr(change_pct),
                    "llm_tokens": None,  # 非LLM経路（LLM 呼び出し未実施, §5.2）
                }
            )
            return None

        return MacroIndicator(
            label=label,
            latest_value=latest,
            overnight_change_pct=change_pct,
        )

    @staticmethod
    def _extract_closes(payload: dict[str, Any]) -> list[float]:
        """chart レスポンスから有限・正の日足終値のみを時系列順で抽出する。

        休場日の null、未確定日の 0.0/非有限値は除外する（yfinance 実装の prev_close==0 /
        非有限ガードと同等の防御）。無効シンボル時は chart.result=null で TypeError を送出し、
        呼び出し側で degrade する。
        """
        result = payload["chart"]["result"][0]
        raw_closes = result["indicators"]["quote"][0]["close"]
        valid: list[float] = []
        for c in raw_closes:
            if c is None:
                continue
            v = float(c)
            if not math.isfinite(v) or v <= 0:
                continue
            valid.append(v)
        return valid


def build_source(source_name: str | None = "yahoo_chart") -> MacroContextSource:
    normalized = (source_name or "yahoo_chart").strip().lower()
    sources: dict[str, type[MacroContextSource]] = {
        "yahoo_chart": YahooChartMacroSource,
        "yahoo": YahooChartMacroSource,
        "yfinance": YFinanceMacroSource,  # 後方互換（依存追加が必要・既定ではない）
    }
    cls = sources.get(normalized)
    if cls is None:
        logger.warning(
            {
                "event": "unknown_macro_source",
                "source": source_name,
                "fallback": "yahoo_chart",
                "llm_tokens": None,  # 非LLM経路（LLM 呼び出し未実施, §5.2）
            }
        )
        cls = YahooChartMacroSource
    return cls()


def format_macro_line(result: MacroResult) -> str:
    """MacroResult を LLM プロンプト用の1行文字列に変換する。

    例: "オーバーナイト米国: US10Y=4.25%(+0.03) S&P先物=-0.45% 日経先物=+0.20%"
    取得できた指標のみ記載。全指標欠落時は空文字列を返す（行を省く）。
    """
    if not result.indicators:
        return ""

    parts: list[str] = []
    for ind in result.indicators:
        if ind.label == "US10Y":
            parts.append(f"US10Y={ind.latest_value:.2f}%({ind.overnight_change_pct:+.2f}%)")
        else:
            parts.append(f"{ind.label}={ind.latest_value:.2f}({ind.overnight_change_pct:+.2f}%)")

    return "オーバーナイト米国: " + " ".join(parts)
