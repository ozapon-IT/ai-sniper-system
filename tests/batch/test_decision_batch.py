"""判断バッチ統合テスト — 休場日早期 return（§3.5, §7.1）。"""

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from backend.src.batch.decision_batch import (
    _check_idempotency,
    _fetch_account,
    _round_tick_entry,
    lambda_handler,
)
from backend.src.models import AccountAssetsResponse
from backend.src.persistence.dynamodb_store import LockAcquireOutcome, LockReleaseOutcome


def _mock_economic_fetch(
    prompt_events: list[Any] | None = None,
    *,
    source_ok: bool = True,
) -> tuple[list[Any], Any]:
    """_fetch_economic_events のモック戻り値（prompt イベント + CalendarResult）。"""
    from backend.src.adapters.economic_calendar import (
        CalendarResult,
        ImpactLevel,
    )
    from backend.src.adapters.economic_calendar import (
        EconomicEvent as AdapterEvent,
    )

    prompt_events = prompt_events or []
    adapter_events = []
    for ev in prompt_events:
        h, m = map(int, ev.time_jst.split(":"))
        adapter_events.append(
            AdapterEvent(
                name=ev.name,
                datetime_jst=datetime(2026, 6, 29, h, m, tzinfo=ZoneInfo("Asia/Tokyo")),
                impact=ImpactLevel.HIGH,
                currency=ev.currency,
                forecast=getattr(ev, "forecast", None),
                previous=getattr(ev, "previous", None),
            )
        )
    return prompt_events, CalendarResult(events=adapter_events, source_ok=source_ok)


@pytest.fixture(autouse=True)
def _mock_gmo_credentials() -> Any:
    """全テストで get_gmo_credentials を自動モックする（Secrets Manager 不要）。"""
    with patch(
        "backend.src.batch.decision_batch.get_gmo_credentials",
        return_value=("test_key", "test_secret"),
    ):
        yield


class TestDecisionBatchHolidayEarlyReturn:
    """§7.1 必須テストケース: 休場日起動（早期 return）。"""

    def test_holiday_returns_skipped(self) -> None:
        """休場日に起動すると status=skipped で早期 return すること。"""
        with patch(
            "backend.src.batch.decision_batch._get_today_jst",
            return_value=date(2026, 1, 1),
        ):
            result = lambda_handler({}, None)

        assert result["status"] == "skipped"
        assert result["reason"] == "market_holiday"
        assert result["date"] == "2026-01-01"

    def test_holiday_christmas_returns_skipped(self) -> None:
        """クリスマス（2026-12-25）でも早期 return すること。"""
        with patch(
            "backend.src.batch.decision_batch._get_today_jst",
            return_value=date(2026, 12, 25),
        ):
            result = lambda_handler({}, None)

        assert result["status"] == "skipped"

    def test_normal_weekday_does_not_skip(self) -> None:
        """通常平日は休場日スキップが起きないこと（フル実装後も status != market_holiday）。"""
        jst = ZoneInfo("Asia/Tokyo")
        today = date(2026, 6, 15)
        now_jst = datetime(2026, 6, 15, 7, 30, 0, tzinfo=jst)

        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)

        with (
            patch("backend.src.batch.decision_batch._get_today_jst", return_value=today),
            patch("backend.src.batch.decision_batch._get_now_jst", return_value=now_jst),
            patch("backend.src.batch.decision_batch.GmoFxClient", return_value=mock_client),
            patch(
                "backend.src.batch.decision_batch._run",
                return_value={"status": "stubbed", "date": today.isoformat()},
            ) as mock_run,
        ):
            result = lambda_handler({}, None)

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["today"] == today
        assert mock_run.call_args.kwargs["now_jst"] == now_jst
        assert result == {"status": "stubbed", "date": today.isoformat()}
        assert result.get("reason") != "market_holiday"

    def test_good_friday_returns_skipped(self) -> None:
        """Good Friday（2026-04-03）で早期 return すること。"""
        with patch(
            "backend.src.batch.decision_batch._get_today_jst",
            return_value=date(2026, 4, 3),
        ):
            result = lambda_handler({}, None)

        assert result["status"] == "skipped"


class TestFetchAccountFailClosed:
    """§4.2 証拠金 fail-closed: E04確定フォーム(dict)・status!=0 で None を返す。"""

    # E04確定(#11): data は単一 dict（list ではない）
    _FULL_DATA: dict = {
        "equity": "1000000",
        "availableAmount": "800000",
        "balance": "1000000",
        "transferableAmount": "900000",
        "margin": "0",
        "positionLossGain": "0",
        "totalSwap": "0",
        "estimatedTradeFee": "0",
        "marginRatio": "922337203685477580700",
    }

    def _make_client(self, data: dict | None, status: int = 0, http_status: int = 200) -> MagicMock:
        """assets エンドポイントのレスポンスを返すモッククライアントを生成する。"""
        resp = MagicMock()
        resp.status_code = http_status
        resp.json.return_value = {"status": status, "data": data}
        client = MagicMock()
        client.private_get.return_value = resp
        return client

    def _call(self, client: MagicMock) -> tuple[Decimal, Decimal] | None:
        with patch("backend.src.batch.decision_batch.time.sleep"):
            return _fetch_account(client)

    def test_returns_equity_and_available_when_both_present(self) -> None:
        """E04確定フォーム(dict)で equity / availableAmount が揃えば Decimal タプルを返す。"""
        client = self._make_client(self._FULL_DATA)
        result = self._call(client)
        assert result == (Decimal("1000000"), Decimal("800000"))

    def test_returns_none_when_available_field_missing(self) -> None:
        """availableAmount フィールドが無い場合は equity で代用せず None を返す（fail-closed）。"""
        data = {k: v for k, v in self._FULL_DATA.items() if k != "availableAmount"}
        client = self._make_client(data)
        result = self._call(client)
        assert result is None

    def test_returns_none_when_equity_field_missing(self) -> None:
        """equity フィールドが無い場合は None を返す（fail-closed）。"""
        data = {k: v for k, v in self._FULL_DATA.items() if k != "equity"}
        client = self._make_client(data)
        result = self._call(client)
        assert result is None

    def test_logs_fetch_account_missing_field_at_error_level(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """必須フィールド欠落（fetch_account_missing_field）が error レベルでログされること
        （#143: AppErrors メトリクスフィルタはログレベル基準のため error 必須）。"""
        data = {k: v for k, v in self._FULL_DATA.items() if k != "equity"}
        client = self._make_client(data)
        with caplog.at_level(logging.WARNING, logger="backend.src.batch.decision_batch"):
            result = self._call(client)

        assert result is None
        assert any(
            r.message == "fetch_account_missing_field" and r.levelno == logging.ERROR
            for r in caplog.records
        )

    def test_returns_none_when_data_is_none(self) -> None:
        """data が null（不在）の場合は None を返す（fail-closed）。"""
        client = self._make_client(None)
        result = self._call(client)
        assert result is None

    def test_returns_none_on_http_error(self) -> None:
        """HTTP 200 以外の場合は None を返す。"""
        resp = MagicMock()
        resp.status_code = 500
        client = MagicMock()
        client.private_get.return_value = resp
        result = self._call(client)
        assert result is None

    def test_logs_fetch_account_no_data_at_error_level(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """HTTP 200 以外（fetch_account_no_data 分岐）は error レベルでログされること
        （#143: AppErrors メトリクスフィルタはログレベル基準のため error 必須）。"""
        resp = MagicMock()
        resp.status_code = 500
        client = MagicMock()
        client.private_get.return_value = resp
        with caplog.at_level(logging.WARNING, logger="backend.src.batch.decision_batch"):
            result = self._call(client)

        assert result is None
        assert any(
            r.message == "fetch_account_no_data" and r.levelno == logging.ERROR
            for r in caplog.records
        )

    def test_logs_fetch_account_failed_at_error_level_on_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """private_get が例外送出したとき fetch_account_failed が error レベルでログされること
        （#143）。"""
        client = MagicMock()
        client.private_get.side_effect = RuntimeError("network boom")
        with caplog.at_level(logging.WARNING, logger="backend.src.batch.decision_batch"):
            result = self._call(client)

        assert result is None
        assert any(
            r.message == "fetch_account_failed" and r.levelno == logging.ERROR
            for r in caplog.records
        )

    def test_returns_none_on_api_logical_error(self) -> None:
        """HTTP 200 でも status != 0（GMO 論理エラー）のときは発注に進まない。"""
        client = self._make_client(self._FULL_DATA, status=1)
        result = self._call(client)
        assert result is None

    def test_logs_message_code_on_api_logical_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """status != 0 時に GMO messages（message_code）が error レベルでログに含まれること
        （#96 / #143: AppErrors メトリクスフィルタはログレベル基準のため error 必須）。"""
        messages = [{"message_code": "ERR-5141", "message_string": "Some error"}]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"status": 1, "messages": messages, "data": None}
        client = MagicMock()
        client.private_get.return_value = resp

        with (
            patch("backend.src.batch.decision_batch.time.sleep"),
            caplog.at_level(logging.WARNING, logger="backend.src.batch.decision_batch"),
        ):
            result = self._call(client)

        assert result is None
        assert any(
            r.message == "fetch_account_api_error"
            and r.levelno == logging.ERROR
            and any(
                m.get("message_code") == "ERR-5141" for m in (getattr(r, "messages", None) or [])
            )
            for r in caplog.records
        )


class TestCheckIdempotencyApiError:
    """_check_idempotency の status!=0 fail-closed 分岐を検証する（#96）。"""

    def _make_client(self, path_status_map: dict) -> MagicMock:
        """path ごとに異なるレスポンスを返すモッククライアント。"""

        def _side_effect(path: str, params: Any = None) -> MagicMock:
            body = path_status_map.get(path, {"status": 0, "data": {"list": []}})
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = body
            return resp

        client = MagicMock()
        client.private_get.side_effect = _side_effect
        return client

    def test_active_orders_status_nonzero_returns_skip(self) -> None:
        """activeOrders が status!=0 を返したとき skip=True / reason=active_orders_fetch_failed。"""
        from backend.src.gmo_fx_endpoints import PRIVATE_ACTIVE_ORDERS

        client = self._make_client({PRIVATE_ACTIVE_ORDERS: {"status": 1, "messages": []}})
        result = _check_idempotency(client=client, today=date(2026, 6, 30))
        assert result == {"skip": True, "reason": "active_orders_fetch_failed"}

    def test_active_orders_status_nonzero_logs_message_code(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """activeOrders status!=0 時に message_code が error レベルでログに含まれること
        （#96 / #143: AppErrors メトリクスフィルタはログレベル基準のため error 必須）。"""
        from backend.src.gmo_fx_endpoints import PRIVATE_ACTIVE_ORDERS

        messages = [{"message_code": "ERR-5141", "message_string": "test"}]
        client = self._make_client({PRIVATE_ACTIVE_ORDERS: {"status": 1, "messages": messages}})
        with caplog.at_level(logging.WARNING, logger="backend.src.batch.decision_batch"):
            _check_idempotency(client=client, today=date(2026, 6, 30))

        assert any(
            r.message == "active_orders_api_error"
            and r.levelno == logging.ERROR
            and any(
                m.get("message_code") == "ERR-5141" for m in (getattr(r, "messages", None) or [])
            )
            for r in caplog.records
        )

    def test_open_positions_status_nonzero_returns_skip(self) -> None:
        """openPositions が status!=0 を返したとき skip=True / reason=open_positions_fetch_failed。"""
        from backend.src.gmo_fx_endpoints import PRIVATE_OPEN_POSITIONS

        client = self._make_client({PRIVATE_OPEN_POSITIONS: {"status": 1, "messages": []}})
        result = _check_idempotency(client=client, today=date(2026, 6, 30))
        assert result == {"skip": True, "reason": "open_positions_fetch_failed"}

    def test_open_positions_status_nonzero_logs_message_code(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """openPositions status!=0 時に message_code が error レベルでログに含まれること
        （#96 / #143: AppErrors メトリクスフィルタはログレベル基準のため error 必須）。"""
        from backend.src.gmo_fx_endpoints import PRIVATE_OPEN_POSITIONS

        messages = [{"message_code": "ERR-9999", "message_string": "positions error"}]
        client = self._make_client({PRIVATE_OPEN_POSITIONS: {"status": 1, "messages": messages}})
        with caplog.at_level(logging.WARNING, logger="backend.src.batch.decision_batch"):
            _check_idempotency(client=client, today=date(2026, 6, 30))

        assert any(
            r.message == "open_positions_api_error"
            and r.levelno == logging.ERROR
            and any(
                m.get("message_code") == "ERR-9999" for m in (getattr(r, "messages", None) or [])
            )
            for r in caplog.records
        )

    @pytest.mark.parametrize(
        "target_endpoint,http_status,raises,expected_message,expected_reason",
        [
            pytest.param(
                "active_orders",
                500,
                False,
                "active_orders_fetch_non200",
                "active_orders_fetch_failed",
                id="active_orders_non200",
            ),
            pytest.param(
                "active_orders",
                None,
                True,
                "active_orders_fetch_failed",
                "active_orders_fetch_failed",
                id="active_orders_exception",
            ),
            pytest.param(
                "open_positions",
                500,
                False,
                "open_positions_fetch_non200",
                "open_positions_fetch_failed",
                id="open_positions_non200",
            ),
            pytest.param(
                "open_positions",
                None,
                True,
                "open_positions_fetch_failed",
                "open_positions_fetch_failed",
                id="open_positions_exception",
            ),
        ],
    )
    def test_fetch_failure_logs_error_level(
        self,
        caplog: pytest.LogCaptureFixture,
        target_endpoint: str,
        http_status: int | None,
        raises: bool,
        expected_message: str,
        expected_reason: str,
    ) -> None:
        """activeOrders/openPositions の HTTP 非200・例外はいずれも error レベルで
        ログされること（#143: AppErrors メトリクスフィルタはログレベル基準のため error 必須）。"""
        from backend.src.gmo_fx_endpoints import PRIVATE_ACTIVE_ORDERS, PRIVATE_OPEN_POSITIONS

        target_path = (
            PRIVATE_ACTIVE_ORDERS if target_endpoint == "active_orders" else PRIVATE_OPEN_POSITIONS
        )

        def _side_effect(path: str, params: Any = None) -> MagicMock:
            # activeOrders は openPositions より先に呼ばれるため、対象でない方は常に正常応答させる。
            if path != target_path:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {"status": 0, "data": {"list": []}}
                return resp
            if raises:
                raise RuntimeError("network boom")
            resp = MagicMock()
            resp.status_code = http_status
            return resp

        client = MagicMock()
        client.private_get.side_effect = _side_effect
        with caplog.at_level(logging.WARNING, logger="backend.src.batch.decision_batch"):
            result = _check_idempotency(client=client, today=date(2026, 6, 30))

        assert result == {"skip": True, "reason": expected_reason}
        assert any(
            r.message == expected_message and r.levelno == logging.ERROR for r in caplog.records
        )


class TestAccountAssetsResponseModel:
    """AccountAssetsResponse モデルの dict 形契約・alias マッピングを検証する（#89 対応）。"""

    _RAW_RESPONSE: dict = {
        "status": 0,
        "responsetime": "2026-06-29T00:00:00.000Z",
        "data": {
            "equity": "100000",
            "availableAmount": "100000",
            "balance": "100000",
            "transferableAmount": "100000",
            "margin": "0",
            "positionLossGain": "0",
            "totalSwap": "0",
            "estimatedTradeFee": "0",
            "marginRatio": "922337203685477580700",
        },
    }

    def test_model_validate_dict_form(self) -> None:
        """E04確定 dict 形レスポンスを model_validate して data が AccountAssetsData になる。"""
        from decimal import Decimal

        resp = AccountAssetsResponse.model_validate(self._RAW_RESPONSE)
        assert resp.status == 0
        assert resp.data is not None
        assert resp.data.equity == Decimal("100000")
        assert resp.data.available_amount == Decimal(
            "100000"
        )  # alias availableAmount → available_amount

    def test_model_validate_alias_mapping(self) -> None:
        """camelCase alias が snake_case フィールドへ正しくマッピングされる。"""
        from decimal import Decimal

        resp = AccountAssetsResponse.model_validate(self._RAW_RESPONSE)
        assert resp.data is not None
        assert resp.data.transferable_amount == Decimal("100000")
        assert resp.data.position_loss_gain == Decimal("0")
        assert resp.data.total_swap == Decimal("0")
        assert resp.data.estimated_trade_fee == Decimal("0")
        # marginRatio は sentinel 巨大値なので margin_ratio プロパティは None を返す
        assert resp.data.margin_ratio is None
        assert resp.data.margin_ratio_raw == "922337203685477580700"

    def test_model_validate_margin_ratio_normal_value(self) -> None:
        """sentinel 以外の marginRatio は Decimal として返る。"""
        from decimal import Decimal

        raw = {
            **self._RAW_RESPONSE,
            "data": {**self._RAW_RESPONSE["data"], "marginRatio": "250.5"},
        }
        resp = AccountAssetsResponse.model_validate(raw)
        assert resp.data is not None
        assert resp.data.margin_ratio == Decimal("250.5")

    def test_model_validate_none_data(self) -> None:
        """data が null の場合 data フィールドは None。"""
        resp = AccountAssetsResponse.model_validate(
            {"status": 1, "responsetime": "...", "data": None}
        )
        assert resp.data is None


class TestFetchMacroContextDegrade:
    """`_fetch_macro_context()` の degrade 契約（§3.3 / #107 / #113）。

    マクロは補助入力。取得失敗・タイムアウト時は例外を送出せず None を返す
    （NO_ENTRY に倒さず判断バッチを止めない、AGENTS.md degrade 方針）。
    """

    def test_returns_none_when_build_source_raises(self) -> None:
        """build_macro_source が例外を投げても送出せず None を返す。"""
        from backend.src.batch.decision_batch import _fetch_macro_context

        with patch(
            "backend.src.batch.decision_batch.build_macro_source",
            side_effect=RuntimeError("build boom"),
        ):
            assert _fetch_macro_context() is None

    def test_returns_none_when_source_fetch_raises(self) -> None:
        """source.fetch() が例外を投げても送出せず None を返す。"""
        from backend.src.batch.decision_batch import _fetch_macro_context

        bad_source = MagicMock()
        bad_source.fetch.side_effect = RuntimeError("fetch boom")
        with patch(
            "backend.src.batch.decision_batch.build_macro_source",
            return_value=bad_source,
        ):
            assert _fetch_macro_context() is None

    def test_returns_none_on_timeout(self) -> None:
        """fetch が締切を超えると None を返す（外部 API 詰まりで判断バッチを止めない）。"""
        import threading

        from backend.src.batch.decision_batch import _fetch_macro_context

        # Event ベース: 締切超過を確認したら即解放し、ワーカーを join 遅延なく終わらせる
        # （sleep(5) だと ThreadPoolExecutor の atexit join でテスト終了が最大5秒遅れる）
        slow_source = MagicMock()
        _release = threading.Event()
        slow_source.fetch.side_effect = lambda: _release.wait(5)  # 締切（0.05s）を確実に超える
        with (
            patch("backend.src.batch.decision_batch._MACRO_FETCH_TIMEOUT_SEC", 0.05),
            patch(
                "backend.src.batch.decision_batch.build_macro_source",
                return_value=slow_source,
            ),
        ):
            try:
                assert _fetch_macro_context() is None
            finally:
                _release.set()


class TestFetchEconomicEventsSourceEnv:
    """#210 L-2: ECONOMIC_CALENDAR_SOURCE 経由で build_source を呼ぶ。"""

    def test_uses_build_calendar_source_with_env(self) -> None:
        from backend.src.adapters.economic_calendar import CalendarResult
        from backend.src.batch.decision_batch import _fetch_economic_events

        mock_source = MagicMock()
        mock_source.fetch.return_value = CalendarResult(events=[], source_ok=True)
        with (
            patch.dict("os.environ", {"ECONOMIC_CALENDAR_SOURCE": "forexfactory"}, clear=False),
            patch(
                "backend.src.batch.decision_batch.build_calendar_source",
                return_value=mock_source,
            ) as build,
        ):
            events, result = _fetch_economic_events(date(2026, 6, 29))

        build.assert_called_once_with("forexfactory")
        mock_source.fetch.assert_called_once()
        # 判断バッチは判断日だけを渡す。両端は adapter が作る（#379）。
        assert mock_source.fetch.call_args.args[0] == date(2026, 6, 29)
        assert events == []
        assert result.source_ok is True

    def test_date_jst_reaches_user_prompt(self) -> None:
        """本番変換が date_jst を付け、プロンプトは日付付き JST になる（#375 指摘 2）。

        ``decision_batch.py`` の ``date_jst=ev.datetime_jst.strftime(...)`` を消すと
        見出しは「保有窓内」のまま行が ``04:00 JST`` に戻り、このテストが落ちる。
        """
        from backend.src.adapters.economic_calendar import CalendarResult, ImpactLevel
        from backend.src.adapters.economic_calendar import EconomicEvent as AdapterEvent
        from backend.src.batch.decision_batch import _fetch_economic_events
        from backend.src.llm.prompt import PromptContext, build_user_prompt

        adapter_event = AdapterEvent(
            name="FOMC Statement",
            datetime_jst=datetime(2026, 12, 10, 4, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
            impact=ImpactLevel.HIGH,
            currency="USD",
        )
        mock_source = MagicMock()
        mock_source.fetch.return_value = CalendarResult(events=[adapter_event], source_ok=True)
        with patch(
            "backend.src.batch.decision_batch.build_calendar_source",
            return_value=mock_source,
        ):
            events, result = _fetch_economic_events(date(2026, 12, 9))

        assert result.source_ok is True
        assert len(events) == 1
        assert events[0].time_jst == "04:00"
        assert events[0].date_jst == "2026-12-10"
        prompt = build_user_prompt(
            PromptContext(
                current_time_jst="20:30",
                day_of_week="水",
                symbol="USD_JPY",
                current_price_mid=155.0,
                pivot_p=155.0,
                pivot_r1=155.5,
                pivot_r2=156.0,
                pivot_s1=154.5,
                pivot_s2=154.0,
                atr_14d=0.4,
                prev_day_change=0.1,
                equity_jpy=100_000.0,
                economic_events=events,
            )
        )
        assert "保有窓内の高インパクト指標(USD/JPY・時刻はJST):" in prompt
        assert "12/10 04:00 JST（保有中） (USD) FOMC Statement" in prompt


class TestDecisionBatchMacroDegradeWiring:
    """マクロ degrade の配線（#113）: 取得失敗でも NO_ENTRY に倒れず正常系を継続する。

    `_run` の正常系（mock LLM が entry 判断）まで進み、戻り値（log_base 由来）に
    `macro_context_source_ok` / `macro_context_injected=False` が記録されることを確認する。
    """

    _JST = ZoneInfo("Asia/Tokyo")
    _MONDAY = date(2026, 6, 29)
    _CANDLE: dict[str, str] = {
        "openTime": "1782399600000",
        "open": "156.000",
        "high": "156.800",
        "low": "155.600",
        "close": "156.400",
        "volume": "8888",
    }
    _TICKER = [{"bid": "156.799", "ask": "156.800"}]
    # E04確定(#11): assets の data は単一 dict（list ではない）。equity/availableAmount 必須。
    _ACCOUNT: dict[str, str] = {
        "equity": "600000",
        "availableAmount": "500000",
        "balance": "600000",
        "transferableAmount": "500000",
        "margin": "0",
        "positionLossGain": "0",
        "totalSwap": "0",
        "estimatedTradeFee": "0",
        "marginRatio": "922337203685477580700",
    }

    def _build_mock_client(self) -> MagicMock:
        from backend.src.gmo_fx_endpoints import (
            PRIVATE_ACCOUNT_ASSETS,
            PRIVATE_ACTIVE_ORDERS,
            PRIVATE_OPEN_POSITIONS,
            PUBLIC_KLINES,
            PUBLIC_TICKER,
        )

        mock = MagicMock()

        def _public_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            if path == PUBLIC_KLINES:
                resp.json.return_value = {"status": 0, "data": [self._CANDLE] * 15}
            elif path == PUBLIC_TICKER:
                resp.json.return_value = {"status": 0, "data": self._TICKER}
            else:
                resp.json.return_value = {"data": []}
            return resp

        def _private_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            if path in (PRIVATE_ACTIVE_ORDERS, PRIVATE_OPEN_POSITIONS):
                resp.json.return_value = {"status": 0, "data": {"list": []}}
            elif path == PRIVATE_ACCOUNT_ASSETS:
                resp.json.return_value = {"status": 0, "data": self._ACCOUNT}
            else:
                resp.json.return_value = {"data": []}
            return resp

        mock.public_get.side_effect = _public_get
        mock.private_get.side_effect = _private_get
        mock.__enter__ = lambda s: s
        mock.__exit__ = MagicMock(return_value=False)
        return mock

    def _make_llm_result(self) -> Any:
        from backend.src.llm.client import LlmResult
        from backend.src.llm.trade_decision import TradeDecision

        # entry は現値(mid≈156.79)近傍（§3.4 現値乖離上限 0.3×ATR14=0.69 の内側）。
        # SL/TP は Python が ATR_h/rr_target から決定論算出する（Ver 3.6・LLM は出さない）。
        decision = TradeDecision(
            direction="LONG",
            execution_required=True,
            order_type="LIMIT",
            entry_price=156.500,
            referenced_pivot_level="R1",
            sl_profile="standard",
            rr_target=1.0,
            rationale="テスト",
            sentiment_summary=None,
        )
        result = MagicMock(spec=LlmResult)
        result.decision = decision
        result.input_tokens = 10
        result.output_tokens = 5
        result.model = "mock"
        return result

    def _make_llm_result_no_entry(self) -> Any:
        from backend.src.llm.client import LlmResult
        from backend.src.llm.trade_decision import no_entry

        result = MagicMock(spec=LlmResult)
        result.decision = no_entry(rationale="見送り")
        result.input_tokens = 10
        result.output_tokens = 5
        result.model = "mock"
        return result

    def _run_with_macro(
        self,
        macro_context: Any,
        economic_events: Any = None,
        llm_result: Any = None,
    ) -> dict[str, Any]:
        """マクロ取得結果を差し替えて判断バッチ本体を実行する（発注は Phase 1 スタブ）。"""
        import os

        from backend.src.batch.decision_batch import _run as decision_run

        client = self._build_mock_client()
        now_jst = datetime(2026, 6, 29, 20, 30, 0, tzinfo=self._JST)
        if economic_events is None:
            economic_events = _mock_economic_fetch()
        if llm_result is None:
            llm_result = self._make_llm_result()
        with (
            # two-key interlock を安全側に固定（ambient env で live-order 分岐に入らせない）
            patch.dict(os.environ, {"ALLOW_LIVE_ORDERS": "", "TRADING_PHASE": "1"}, clear=False),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=[self._CANDLE] * 15,
            ),
            patch(
                "backend.src.batch.decision_batch.generate_decision",
                return_value=llm_result,
            ),
            patch(
                "backend.src.batch.decision_batch._fetch_macro_context",
                return_value=macro_context,
            ),
            patch(
                "backend.src.batch.decision_batch.dynamodb_store.acquire_lock_outcome",
                return_value=LockAcquireOutcome(acquired=True),
            ),
            patch(
                "backend.src.batch.decision_batch.dynamodb_store.write_order_id", return_value=True
            ),
            patch("backend.src.batch.decision_batch.s3_logger.save_decision_log"),
            patch("backend.src.batch.decision_batch.time.sleep"),
            patch(
                "backend.src.batch.decision_batch._fetch_economic_events",
                return_value=economic_events,
            ),
            patch("backend.src.batch.decision_batch.is_us_thin_market", return_value=False),
            patch("backend.src.batch.decision_batch.send_slack_alert", return_value=True),
        ):
            return decision_run(client=client, today=self._MONDAY, now_jst=now_jst)

    def test_macro_none_does_not_force_no_entry(self) -> None:
        """マクロ取得 None（timeout/例外）でも NO_ENTRY に倒れず正常系（stubbed）を継続する。"""
        result = self._run_with_macro(None)

        assert result["status"] == "stubbed", f"NO_ENTRY に倒れた: {result}"
        assert result["macro_context_source_ok"] is None
        assert result["macro_context_injected"] is False
        assert result["macro_context"] == []

    def test_macro_source_failed_does_not_force_no_entry(self) -> None:
        """マクロ source_ok=False（部分失敗）でも NO_ENTRY に倒れず継続し、フラグが記録される。"""
        from backend.src.adapters.macro_context import MacroResult

        result = self._run_with_macro(MacroResult(indicators=[], source_ok=False))

        assert result["status"] == "stubbed", f"NO_ENTRY に倒れた: {result}"
        assert result["macro_context_source_ok"] is False
        assert result["macro_context_injected"] is False
        assert result["macro_context"] == []

    def test_bt5_replay_fields_recorded(self) -> None:
        """#163: BT-5 リプレイ用フィールドが S3 ログ（result）に記録される。

        economic_events は forecast/previous を含めて保存し、calendar_source_ok で
        degrade を判別できるようにする（macro_context_source_ok と同型）。
        order_type/sl_profile/atr_multiplier/referenced_pivot_level/sentiment_summary は
        LLM 出力（クランプ後の実効値含む）をそのまま監査ログへ残す。
        """
        from backend.src.llm.prompt import EconomicEvent

        events = [
            EconomicEvent(
                time_jst="23:30",
                currency="USD",
                name="FOMC Minutes",
                impact="High",
                forecast=None,
                previous=None,
                date_jst="2026-06-29",
            )
        ]
        result = self._run_with_macro(None, economic_events=_mock_economic_fetch(events))

        assert result["status"] == "stubbed"
        assert result["calendar_source_ok"] is True
        assert result["economic_events"] == [
            {
                "time_jst": "23:30",
                "date_jst": "2026-06-29",
                "currency": "USD",
                "name": "FOMC Minutes",
                "impact": "High",
                "forecast": None,
                "previous": None,
            }
        ]
        assert result["order_type"] == "LIMIT"
        assert result["sl_profile"] == "standard"
        assert result["atr_multiplier"] == 0.7
        assert result["rr_target"] == 1.0
        assert result["referenced_pivot_level"] == "R1"
        assert result["sentiment_summary"] is None

    def test_bt5_replay_fields_recorded_on_calendar_degrade(self) -> None:
        """カレンダー取得失敗（degrade）時は NO_ENTRY（§3.3）かつ calendar_source_ok=False。"""
        result = self._run_with_macro(None, economic_events=_mock_economic_fetch(source_ok=False))

        assert result["status"] == "no_entry"
        assert result["reason"] == "calendar_degraded"
        assert result["calendar_source_ok"] is False
        assert result["economic_events"] == []
        assert result["llm_input_tokens"] == 0

    def test_bt5_replay_fields_recorded_on_no_entry(self) -> None:
        """#163: no_entry 日も economic_events を残す（見送り判断こそリプレイ価値が高い）。"""
        from backend.src.llm.prompt import EconomicEvent

        events = [
            EconomicEvent(
                time_jst="23:30",
                currency="USD",
                name="FOMC Minutes",
                impact="High",
                forecast="3.2%",
                previous="3.1%",
                date_jst="2026-06-29",
            )
        ]
        result = self._run_with_macro(
            None,
            economic_events=_mock_economic_fetch(events),
            llm_result=self._make_llm_result_no_entry(),
        )

        assert result["status"] == "no_entry"
        assert result["calendar_source_ok"] is True
        assert result["economic_events"] == [
            {
                "time_jst": "23:30",
                "date_jst": "2026-06-29",
                "currency": "USD",
                "name": "FOMC Minutes",
                "impact": "High",
                "forecast": "3.2%",
                "previous": "3.1%",
            }
        ]
        assert result["order_type"] is None
        assert result["referenced_pivot_level"] == "NONE"


class TestIndicatorDaySkipHardRule:
    """RD-7 #172: 指標 day-skip hard rule（§3.3 / §6.1）。"""

    _JST: ZoneInfo = ZoneInfo("Asia/Tokyo")
    _MONDAY: date = date(2026, 6, 29)
    _CANDLE: dict[str, str] = {
        "openTime": "1782399600000",
        "open": "156.000",
        "high": "156.800",
        "low": "155.600",
        "close": "156.400",
        "volume": "8888",
    }
    _TICKER: list[dict[str, str]] = [{"bid": "156.799", "ask": "156.800"}]
    _ACCOUNT: dict[str, str] = {
        "equity": "600000",
        "availableAmount": "500000",
    }

    def _build_mock_client(self) -> MagicMock:
        from backend.src.gmo_fx_endpoints import (
            PRIVATE_ACCOUNT_ASSETS,
            PRIVATE_ACTIVE_ORDERS,
            PRIVATE_OPEN_POSITIONS,
            PUBLIC_TICKER,
        )

        mock = MagicMock()

        def _public_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            if path == PUBLIC_TICKER:
                resp.json.return_value = {"status": 0, "data": self._TICKER}
            else:
                resp.json.return_value = {"data": []}
            return resp

        def _private_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            if path in (PRIVATE_ACTIVE_ORDERS, PRIVATE_OPEN_POSITIONS):
                resp.json.return_value = {"status": 0, "data": {"list": []}}
            elif path == PRIVATE_ACCOUNT_ASSETS:
                resp.json.return_value = {"status": 0, "data": self._ACCOUNT}
            else:
                resp.json.return_value = {"data": []}
            return resp

        mock.public_get.side_effect = _public_get
        mock.private_get.side_effect = _private_get
        return mock

    def _make_llm_result(self) -> Any:
        from backend.src.llm.client import LlmResult
        from backend.src.llm.trade_decision import TradeDecision

        decision = TradeDecision(
            direction="LONG",
            execution_required=True,
            order_type="LIMIT",
            entry_price=156.500,
            referenced_pivot_level="R1",
            sl_profile="standard",
            rr_target=1.0,
            rationale="テスト",
            sentiment_summary=None,
        )
        result = MagicMock(spec=LlmResult)
        result.decision = decision
        result.input_tokens = 10
        result.output_tokens = 5
        result.model = "mock"
        return result

    def _run(
        self,
        economic_events: tuple[list[Any], Any],
        *,
        mock_acquire: MagicMock | None = None,
        save: MagicMock | None = None,
        slack: MagicMock | None = None,
    ) -> tuple[dict[str, Any], MagicMock]:
        import os

        from backend.src.batch.decision_batch import _run as decision_run

        client = self._build_mock_client()
        now_jst = datetime(2026, 6, 29, 20, 30, 0, tzinfo=self._JST)
        llm_mock = MagicMock(return_value=self._make_llm_result())
        acquire = mock_acquire or MagicMock(return_value=LockAcquireOutcome(acquired=True))
        save_mock = save if save is not None else MagicMock(return_value=True)
        slack_mock = slack if slack is not None else MagicMock(return_value=True)
        with (
            patch.dict(os.environ, {"ALLOW_LIVE_ORDERS": "", "TRADING_PHASE": "1"}, clear=False),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=[self._CANDLE] * 15,
            ),
            patch(
                "backend.src.batch.decision_batch.generate_decision",
                llm_mock,
            ),
            patch("backend.src.batch.decision_batch._fetch_macro_context", return_value=None),
            patch("backend.src.batch.decision_batch.dynamodb_store.acquire_lock_outcome", acquire),
            patch("backend.src.batch.decision_batch.s3_logger.save_decision_log", save_mock),
            patch("backend.src.batch.decision_batch.send_slack_alert", slack_mock),
            patch("backend.src.batch.decision_batch.time.sleep"),
            patch(
                "backend.src.batch.decision_batch._fetch_economic_events",
                return_value=economic_events,
            ),
            patch("backend.src.batch.decision_batch.is_us_thin_market", return_value=False),
        ):
            result = decision_run(client=client, today=self._MONDAY, now_jst=now_jst)
        return result, llm_mock

    def test_21_30_employment_day_skips_before_llm(self) -> None:
        """21:30 雇用統計日 → day-skip（LLM 未呼び出し）。"""
        from backend.src.llm.prompt import EconomicEvent

        events = [
            EconomicEvent(
                time_jst="21:30",
                currency="USD",
                name="Non-Farm Payrolls",
                impact="High",
            )
        ]
        mock_acquire = MagicMock(return_value=LockAcquireOutcome(acquired=True))
        result, llm_mock = self._run(
            _mock_economic_fetch(events),
            mock_acquire=mock_acquire,
        )

        assert result["status"] == "no_entry"
        assert result["reason"] == "indicator_day_skip"
        assert result["skip_indicators"] == [
            {
                "name": "Non-Farm Payrolls",
                "time_jst": "21:30",
                "date_jst": "2026-06-29",
                "currency": "USD",
            }
        ]
        assert result["llm_input_tokens"] == 0
        llm_mock.assert_not_called()
        mock_acquire.assert_not_called()

    def test_23_30_indicator_day_proceeds_to_llm(self) -> None:
        """23:30 指標日 → day-skip 対象外で LLM へ進む。"""
        from backend.src.llm.prompt import EconomicEvent

        events = [
            EconomicEvent(
                time_jst="23:30",
                currency="USD",
                name="FOMC Minutes",
                impact="High",
            )
        ]
        result, llm_mock = self._run(_mock_economic_fetch(events))

        assert result["status"] == "stubbed"
        llm_mock.assert_called_once()

    def test_no_indicator_day_proceeds_to_llm(self) -> None:
        """指標なし → LLM へ進む。"""
        result, llm_mock = self._run(_mock_economic_fetch())

        assert result["status"] == "stubbed"
        llm_mock.assert_called_once()

    def test_calendar_failure_no_entry_without_llm(self) -> None:
        """カレンダー失敗 → NO_ENTRY（LLM 未呼び出し）。"""
        mock_acquire = MagicMock(return_value=LockAcquireOutcome(acquired=True))
        result, llm_mock = self._run(
            _mock_economic_fetch(source_ok=False),
            mock_acquire=mock_acquire,
        )

        assert result["status"] == "no_entry"
        assert result["reason"] == "calendar_degraded"
        assert result["calendar_degrade_cause"] == "calendar_source"
        assert result["calendar_tz_key"] is None
        assert result["rationale"] == "経済指標カレンダー取得失敗（保守的 degrade）"
        llm_mock.assert_not_called()
        mock_acquire.assert_not_called()

    def test_calendar_degraded_sends_one_slack_after_log(self) -> None:
        """calendar_degraded はログ保存のあと Slack を 1 回。本文にフィードは入れない（#376）。"""
        from backend.src.llm.prompt import EconomicEvent

        order: list[str] = []
        messages: list[str] = []

        def _save(*_args: object, **_kwargs: object) -> bool:
            order.append("save")
            return True

        def _slack(message: str) -> bool:
            order.append("slack")
            messages.append(message)
            return True

        events = [
            EconomicEvent(
                time_jst="21:30",
                currency="USD",
                name="UNIQUE_FEED_TITLE_XYZ",
                impact="High",
            )
        ]
        result, llm_mock = self._run(
            _mock_economic_fetch(events, source_ok=False),
            save=MagicMock(side_effect=_save),
            slack=MagicMock(side_effect=_slack),
        )

        assert result["status"] == "no_entry"
        assert result["reason"] == "calendar_degraded"
        assert order == ["save", "slack"]
        assert len(messages) == 1
        message = messages[0]
        assert "date=2026-06-29" in message
        assert "cause=calendar_source" in message
        assert "tz_key=" in message
        assert "source_ok=false" in message
        assert "UNIQUE_FEED_TITLE_XYZ" not in message
        assert "http" not in message
        assert "hooks.slack" not in message
        llm_mock.assert_not_called()

    def test_indicator_day_skip_does_not_slack(self) -> None:
        from backend.src.llm.prompt import EconomicEvent

        slack = MagicMock(return_value=True)
        events = [
            EconomicEvent(
                time_jst="21:30",
                currency="USD",
                name="Non-Farm Payrolls",
                impact="High",
            )
        ]
        result, _llm = self._run(_mock_economic_fetch(events), slack=slack)
        assert result["reason"] == "indicator_day_skip"
        slack.assert_not_called()

    def test_calendar_slack_failure_still_no_entry(self) -> None:
        """送信失敗は False。例外は投げず、NO_ENTRY は変えない（#376）。"""
        slack = MagicMock(return_value=False)
        result, llm_mock = self._run(_mock_economic_fetch(source_ok=False), slack=slack)
        assert result["status"] == "no_entry"
        assert result["reason"] == "calendar_degraded"
        assert result["direction"] == "NO_ENTRY"
        slack.assert_called_once()
        message = slack.call_args.args[0]
        assert message == (
            "[AI Sniper] calendar_degraded で NO_ENTRY。"
            " date=2026-06-29"
            " cause=calendar_source"
            " tz_key="
            " source_ok=false"
        )
        llm_mock.assert_not_called()

    def test_winter_midnight_feed_reaches_skip_indicators(self) -> None:
        """冬 10:00 ET は翌 00:00 JST。requests.get だけモックし _run の skip_indicators まで届く。

        夏 21:30 の ``test_21_30_employment_day_skips_before_llm`` は残す（#376）。
        """
        import os

        from backend.src.batch.decision_batch import _run as decision_run

        today = date(2026, 12, 1)
        now_jst = datetime(2026, 12, 1, 20, 30, 0, tzinfo=self._JST)
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = [
            {
                "title": "ISM Manufacturing PMI",
                "country": "USD",
                "date": "2026-12-01T10:00:00-05:00",
                "impact": "High",
                "forecast": "48.5",
                "previous": "48.7",
            }
        ]
        llm_mock = MagicMock(return_value=self._make_llm_result())
        with (
            patch.dict(os.environ, {"ALLOW_LIVE_ORDERS": "", "TRADING_PHASE": "1"}, clear=False),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=[self._CANDLE] * 15,
            ),
            patch("backend.src.batch.decision_batch.generate_decision", llm_mock),
            patch("backend.src.batch.decision_batch._fetch_macro_context", return_value=None),
            patch(
                "backend.src.batch.decision_batch.dynamodb_store.acquire_lock_outcome",
                MagicMock(return_value=LockAcquireOutcome(acquired=True)),
            ),
            patch(
                "backend.src.batch.decision_batch.s3_logger.save_decision_log", return_value=True
            ),
            patch("backend.src.batch.decision_batch.send_slack_alert", return_value=True),
            patch("backend.src.batch.decision_batch.time.sleep"),
            patch("backend.src.adapters.economic_calendar.requests.get", return_value=response),
            patch("backend.src.batch.decision_batch.is_us_thin_market", return_value=False),
        ):
            result = decision_run(client=self._build_mock_client(), today=today, now_jst=now_jst)

        assert result["status"] == "no_entry"
        assert result["reason"] == "indicator_day_skip"
        assert result["skip_indicators"] == [
            {
                "name": "ISM Manufacturing PMI",
                "time_jst": "00:00",
                "date_jst": "2026-12-02",
                "currency": "USD",
            }
        ]
        llm_mock.assert_not_called()

    def test_ny_tz_failure_keeps_source_ok_in_log(self) -> None:
        """フィード成功のまま America/New_York が落ちても source_ok は true（#376）。

        Slack 本文の cause / tz_key / source_ok=true を固定する（#377 レビュー）。
        取得失敗（calendar_source・source_ok=false・tz_key 空）に見えると落ちる。
        """
        from zoneinfo import ZoneInfo as RealZoneInfo
        from zoneinfo import ZoneInfoNotFoundError

        secret = "NY_DETAIL_DO_NOT_LOG"
        messages: list[str] = []

        def _zone(key: str) -> RealZoneInfo:
            if key == "America/New_York":
                raise ZoneInfoNotFoundError(secret)
            return RealZoneInfo(key)

        def _slack(message: str) -> bool:
            messages.append(message)
            return True

        with patch("backend.src.adapters.economic_calendar.ZoneInfo", side_effect=_zone):
            result, llm_mock = self._run(
                _mock_economic_fetch(),
                slack=MagicMock(side_effect=_slack),
            )
        assert result["status"] == "no_entry"
        assert result["reason"] == "calendar_degraded"
        assert result["calendar_source_ok"] is True
        assert result["calendar_degrade_cause"] == "tz_unresolved"
        assert result["calendar_tz_key"] == "America/New_York"
        assert result["rationale"] == "タイムゾーン解決失敗（America/New_York）（保守的 degrade）"
        assert messages == [
            "[AI Sniper] calendar_degraded で NO_ENTRY。"
            " date=2026-06-29"
            " cause=tz_unresolved"
            " tz_key=America/New_York"
            " source_ok=true"
        ]
        assert secret not in messages[0]
        llm_mock.assert_not_called()

    def test_decision_batch_fills_decision_date_jst(self) -> None:
        from backend.src.llm.prompt import build_user_prompt

        _result, llm_mock = self._run(_mock_economic_fetch())
        ctx = llm_mock.call_args.args[0]
        assert ctx.decision_date_jst == "2026-06-29"
        assert "現在時刻(JST): 6/29 20:30 (月)" in build_user_prompt(ctx)


class TestEdgeGuardDaySkipHardRule:
    """RD-8 #194: エッジガード day-skip hard rule（§3.3 / §6.1）。"""

    _JST: ZoneInfo = ZoneInfo("Asia/Tokyo")
    _MONDAY: date = date(2026, 6, 29)
    _CANDLE: dict[str, str] = {
        "openTime": "1782399600000",
        "open": "156.000",
        "high": "156.800",
        "low": "155.600",
        "close": "156.400",
        "volume": "8888",
    }
    _LOW_VOL_CANDLE: dict[str, str] = {
        "openTime": "1782399600000",
        "open": "156.000",
        "high": "156.500",
        "low": "155.900",
        "close": "156.200",
        "volume": "8888",
    }
    _HIGH_VOL_CANDLE: dict[str, str] = {
        "openTime": "1782399600000",
        "open": "155.000",
        "high": "157.000",
        "low": "155.000",
        "close": "156.000",
        "volume": "8888",
    }
    _NARROW_TICKER: list[dict[str, str]] = [{"bid": "156.799", "ask": "156.800"}]
    _WIDE_TICKER: list[dict[str, str]] = [{"bid": "156.750", "ask": "156.800"}]
    _ACCOUNT: dict[str, str] = {
        "equity": "600000",
        "availableAmount": "500000",
    }

    def _build_mock_client(
        self,
        ticker: list[dict[str, str]] | None = None,
        *,
        ticker_status: int = 0,
    ) -> MagicMock:
        from backend.src.gmo_fx_endpoints import (
            PRIVATE_ACCOUNT_ASSETS,
            PRIVATE_ACTIVE_ORDERS,
            PRIVATE_OPEN_POSITIONS,
            PUBLIC_TICKER,
        )

        mock = MagicMock()
        quote = ticker if ticker is not None else self._NARROW_TICKER

        def _public_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            if path == PUBLIC_TICKER:
                # #243: 業務エラー（status:1）を注入できるようにする
                resp.json.return_value = {
                    "status": ticker_status,
                    "messages": [],
                    "data": quote,
                }
            else:
                resp.json.return_value = {"data": []}
            return resp

        def _private_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            if path in (PRIVATE_ACTIVE_ORDERS, PRIVATE_OPEN_POSITIONS):
                resp.json.return_value = {"status": 0, "data": {"list": []}}
            elif path == PRIVATE_ACCOUNT_ASSETS:
                resp.json.return_value = {"status": 0, "data": self._ACCOUNT}
            else:
                resp.json.return_value = {"data": []}
            return resp

        mock.public_get.side_effect = _public_get
        mock.private_get.side_effect = _private_get
        return mock

    def _make_llm_result(self) -> Any:
        from backend.src.llm.client import LlmResult
        from backend.src.llm.trade_decision import TradeDecision

        decision = TradeDecision(
            direction="LONG",
            execution_required=True,
            order_type="LIMIT",
            entry_price=156.500,
            referenced_pivot_level="R1",
            sl_profile="standard",
            rr_target=1.0,
            rationale="テスト",
            sentiment_summary=None,
        )
        result = MagicMock(spec=LlmResult)
        result.decision = decision
        result.input_tokens = 10
        result.output_tokens = 5
        result.model = "mock"
        return result

    def _run(
        self,
        *,
        ticker: list[dict[str, str]] | None = None,
        ticker_status: int = 0,
        candles: list[dict[str, str]] | None = None,
        session_stats: tuple[float, float, float] | None = None,
        economic_events: tuple[list[Any], Any] | None = None,
        acquire_lock_return: bool = True,
        lock_exists: bool = True,
    ) -> tuple[dict[str, Any], MagicMock, MagicMock]:
        import os

        from backend.src.adapters.klines import SessionSplitOhlc
        from backend.src.batch.decision_batch import _run as decision_run

        client = self._build_mock_client(ticker=ticker, ticker_status=ticker_status)
        now_jst = datetime(2026, 6, 29, 20, 30, 0, tzinfo=self._JST)
        llm_mock = MagicMock(return_value=self._make_llm_result())
        outcome = LockAcquireOutcome(
            acquired=acquire_lock_return,
            lock_exists=(not acquire_lock_return) and lock_exists,
        )
        acquire = MagicMock(return_value=outcome)
        events = economic_events if economic_events is not None else _mock_economic_fetch()
        candle_list = candles if candles is not None else [self._CANDLE] * 15
        session_split = (
            SessionSplitOhlc(
                session_open=session_stats[0],
                session_high=session_stats[1],
                session_low=session_stats[2],
                tokyo_open=session_stats[0],
                tokyo_high=session_stats[1],
                tokyo_low=session_stats[2],
                europe_open=session_stats[0],
                europe_high=session_stats[1],
                europe_low=session_stats[2],
            )
            if session_stats is not None
            else None
        )
        with (
            patch.dict(os.environ, {"ALLOW_LIVE_ORDERS": "", "TRADING_PHASE": "1"}, clear=False),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=candle_list,
            ),
            patch("backend.src.batch.decision_batch.generate_decision", llm_mock),
            patch("backend.src.batch.decision_batch._fetch_macro_context", return_value=None),
            patch("backend.src.batch.decision_batch.dynamodb_store.acquire_lock_outcome", acquire),
            patch("backend.src.batch.decision_batch.s3_logger.save_decision_log"),
            patch("backend.src.batch.decision_batch.time.sleep"),
            patch("backend.src.batch.decision_batch._fetch_economic_events", return_value=events),
            patch("backend.src.batch.decision_batch.is_us_thin_market", return_value=False),
            patch(
                "backend.src.batch.decision_batch.get_split_session_ohlc",
                return_value=session_split,
            ),
        ):
            result = decision_run(client=client, today=self._MONDAY, now_jst=now_jst)
        return result, llm_mock, acquire

    def test_wide_spread_skips_before_llm(self) -> None:
        """G1: ワイドスプレッド日 → day-skip（LLM 未呼び出し）。"""
        result, llm_mock, acquire = self._run(ticker=self._WIDE_TICKER)

        assert result["status"] == "no_entry"
        assert result["reason"] == "wide_spread_day_skip"
        assert result["edge_guard_details"]["spread_pips"] == pytest.approx(5.0)
        llm_mock.assert_not_called()
        acquire.assert_not_called()

    def test_edge_guard_does_not_slack(self) -> None:
        with patch("backend.src.batch.decision_batch.send_slack_alert") as slack:
            result, _llm, _acquire = self._run(ticker=self._WIDE_TICKER)
        assert result["reason"] == "wide_spread_day_skip"
        slack.assert_not_called()

    def test_ticker_business_error_skips_before_llm(self) -> None:
        """#243: HTTP 200 + body status:1 の ticker で NO_ENTRY に倒れること。

        GMO は業務エラーでも HTTP 200 を返す。`status_code` だけ見ていると
        業務エラーが「data が空」＝データなしに化ける
        （2026-08-05 のノート捏造事故と同じ未検査パターン）。

        🔴 **data を付けた状態で status:1 にしている。** data の有無で判断していると
        素通りする経路であり、ここが本 Issue の要点。
        現状は fail-closed で NO_ENTRY に倒れるため実害は無いが、
        「値が無ければ既定値を使う」等に変わった瞬間に事故化する。
        """
        result, llm_mock, acquire = self._run(ticker_status=1)

        # `_skip` 経路のため status は "skipped"（エッジガードの "no_entry" とは別）
        assert result["status"] == "skipped"
        assert result["reason"] == "no_current_price"
        llm_mock.assert_not_called()
        acquire.assert_not_called()

    def test_gmo_normal_spread_proceeds_to_llm(self) -> None:
        """#219 受け入れ本体: GMO 通常スプレッド 0.5 pips（2026-07 実測 ticker 値）は G1 を pass する。"""
        result, llm_mock, _ = self._run(ticker=[{"bid": "162.483", "ask": "162.488"}])

        assert result.get("reason") != "wide_spread_day_skip"
        llm_mock.assert_called_once()

    def test_fetch_ticker_rounds_float_noise(self) -> None:
        """#219 ①: (ask−bid)/0.01 の float ノイズを産地で 0.01 pip 精度に丸める。"""
        from backend.src.batch.decision_batch import _fetch_ticker

        client = self._build_mock_client(ticker=[{"bid": "162.483", "ask": "162.488"}])
        with patch("backend.src.batch.decision_batch.time.sleep"):
            ticker = _fetch_ticker(client=client)

        assert ticker is not None
        _, spread_pips = ticker
        assert spread_pips == 0.5  # 丸めなしでは 0.49999999999954525

    def test_spread_pips_recorded_on_all_exit_paths(self) -> None:
        """#219 DoD: 20:30 実スプレッドを skip/entry を問わず全終了パスに記録する（§6 限界 #3 の継続監視）。"""
        from backend.src.llm.prompt import EconomicEvent

        # エッジガード skip パス
        edge_skip, _, _ = self._run(ticker=self._WIDE_TICKER)
        assert edge_skip["spread_pips"] == pytest.approx(5.0)

        # 指標 day-skip パス
        events = [
            EconomicEvent(
                time_jst="21:30", currency="USD", name="Non-Farm Payrolls", impact="High"
            )
        ]
        indicator_skip, _, _ = self._run(economic_events=_mock_economic_fetch(events))
        assert indicator_skip["spread_pips"] == pytest.approx(0.1)

        # LLM 経路（entry/no_entry 共通の log_base）
        entry_path, llm_mock, _ = self._run()
        llm_mock.assert_called_once()
        assert entry_path["spread_pips"] == pytest.approx(0.1)

        # no_account_data skip パス（ticker 取得後）
        with patch("backend.src.batch.decision_batch._fetch_account", return_value=None):
            account_skip, _, _ = self._run()
        assert account_skip["reason"] == "no_account_data"
        assert account_skip["spread_pips"] == pytest.approx(0.1)

        # dynamodb_lock_exists skip パス（LLM 通過後・発注前）
        lock_skip, llm_mock, acquire = self._run(acquire_lock_return=False)
        llm_mock.assert_called_once()
        acquire.assert_called_once()
        assert lock_skip["reason"] == "dynamodb_lock_exists"
        assert lock_skip["spread_pips"] == pytest.approx(0.1)

    def test_equity_jpy_recorded_only_after_account_fetch(self) -> None:
        """#411: 口座を取得できた日の判断ログに equity_jpy（有効純資産・float）を残す。

        取得前に止まる経路（no_account_data など）は推測せずキーを入れない。
        ロット計算に渡る equity と同じ値であること（発注・ロットの挙動は変えない）。
        """
        from backend.src.llm.prompt import EconomicEvent

        expected = 600000.0  # _ACCOUNT["equity"]

        # エッジガード day-skip（口座取得後）
        edge_skip, _, _ = self._run(ticker=self._WIDE_TICKER)
        assert edge_skip["equity_jpy"] == expected
        assert isinstance(edge_skip["equity_jpy"], float)

        # 指標 day-skip（口座取得後）
        events = [
            EconomicEvent(
                time_jst="21:30", currency="USD", name="Non-Farm Payrolls", impact="High"
            )
        ]
        indicator_skip, _, _ = self._run(economic_events=_mock_economic_fetch(events))
        assert indicator_skip["equity_jpy"] == expected

        # LLM 経路（log_base）
        entry_path, _, _ = self._run()
        assert entry_path["equity_jpy"] == expected

        # 口座取得後のロック失敗 skip
        lock_skip, _, _ = self._run(acquire_lock_return=False)
        assert lock_skip["reason"] == "dynamodb_lock_exists"
        assert lock_skip["equity_jpy"] == expected

        # 口座取得後のロック障害（DynamoDB 障害の疑い）skip も同じ
        with patch("backend.src.batch.lock_acquire.send_slack_alert"):
            unavailable_skip, _, _ = self._run(acquire_lock_return=False, lock_exists=False)
        assert unavailable_skip["reason"] == "dynamodb_lock_unavailable"
        assert unavailable_skip["equity_jpy"] == expected

        # 口座取得に失敗した日は equity_jpy を入れない（推測資産を記録しない）
        with patch("backend.src.batch.decision_batch._fetch_account", return_value=None):
            account_skip, _, _ = self._run()
        assert account_skip["reason"] == "no_account_data"
        assert "equity_jpy" not in account_skip

    def test_high_vol_regime_skips_before_llm(self) -> None:
        """G2: 高ボラレジーム日 → day-skip（LLM 未呼び出し）。"""
        result, llm_mock, acquire = self._run(candles=[self._HIGH_VOL_CANDLE] * 15)

        assert result["status"] == "no_entry"
        assert result["reason"] == "high_vol_regime_day_skip"
        assert result["edge_guard_details"]["atr_pips"] >= 160.0
        llm_mock.assert_not_called()
        acquire.assert_not_called()

    def test_no_momentum_signal_skips_before_llm(self) -> None:
        """G3: 無シグナル日 → day-skip（LLM 未呼び出し）。"""
        # mid ≈ 156.7995, session_open=156.779 → move < 0.05 * ATR（低ボラ足）
        result, llm_mock, acquire = self._run(
            candles=[self._LOW_VOL_CANDLE] * 15,
            session_stats=(156.779, 157.0, 156.0),
        )

        assert result["status"] == "no_entry"
        assert result["reason"] == "no_momentum_signal_day_skip"
        llm_mock.assert_not_called()
        acquire.assert_not_called()

    def test_indicator_skip_takes_precedence_over_edge_guard(self) -> None:
        """指標 day-skip と G1 両方該当 → 指標理由が優先。"""
        from backend.src.llm.prompt import EconomicEvent

        events = [
            EconomicEvent(
                time_jst="21:30",
                currency="USD",
                name="Non-Farm Payrolls",
                impact="High",
            )
        ]
        result, llm_mock, acquire = self._run(
            ticker=self._WIDE_TICKER,
            economic_events=_mock_economic_fetch(events),
        )

        assert result["status"] == "no_entry"
        assert result["reason"] == "indicator_day_skip"
        llm_mock.assert_not_called()
        acquire.assert_not_called()


class TestOrderFailureLockLifecycle:
    """#138: DynamoDB 当日ロックのライフサイクル（§6.1）。

    未約定確実な発注失敗のみロックを解放して当日再試行を可能にし、
    未確定の失敗（5xx・タイムアウト等）は fail-safe でロックを保持する。
    """

    _JST: ZoneInfo = ZoneInfo("Asia/Tokyo")
    _MONDAY: date = date(2026, 6, 29)
    _CANDLE: dict[str, str] = {
        "openTime": "1782399600000",
        "open": "156.000",
        "high": "156.800",
        "low": "155.600",
        "close": "156.400",
        "volume": "8888",
    }
    _TICKER: list[dict[str, str]] = [{"bid": "156.799", "ask": "156.800"}]
    _ACCOUNT: dict[str, str] = {
        "equity": "600000",
        "availableAmount": "500000",
    }

    def _build_mock_client(self, post_behavior: Any) -> MagicMock:
        """post_behavior: httpx レスポンス相当の MagicMock か、例外（side_effect 用）。"""
        from backend.src.gmo_fx_endpoints import (
            PRIVATE_ACCOUNT_ASSETS,
            PRIVATE_ACTIVE_ORDERS,
            PRIVATE_OPEN_POSITIONS,
            PUBLIC_TICKER,
        )

        mock = MagicMock()

        def _public_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            if path == PUBLIC_TICKER:
                resp.json.return_value = {"status": 0, "data": self._TICKER}
            else:
                resp.json.return_value = {"data": []}
            return resp

        def _private_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            if path in (PRIVATE_ACTIVE_ORDERS, PRIVATE_OPEN_POSITIONS):
                resp.json.return_value = {"status": 0, "data": {"list": []}}
            elif path == PRIVATE_ACCOUNT_ASSETS:
                resp.json.return_value = {"status": 0, "data": self._ACCOUNT}
            else:
                resp.json.return_value = {"data": []}
            return resp

        mock.public_get.side_effect = _public_get
        mock.private_get.side_effect = _private_get
        if isinstance(post_behavior, Exception):
            mock.private_post.side_effect = post_behavior
        else:
            mock.private_post.return_value = post_behavior
        return mock

    def _make_llm_result(
        self,
        *,
        order_type: str = "LIMIT",
        entry_price: float = 156.500,
    ) -> Any:
        from backend.src.llm.client import LlmResult
        from backend.src.llm.trade_decision import TradeDecision

        decision = TradeDecision(
            direction="LONG",
            execution_required=True,
            order_type=order_type,  # type: ignore[arg-type]
            entry_price=entry_price,
            referenced_pivot_level="R1",
            sl_profile="standard",
            rr_target=1.0,
            rationale="テスト",
            sentiment_summary=None,
        )
        result = MagicMock(spec=LlmResult)
        result.decision = decision
        result.input_tokens = 10
        result.output_tokens = 5
        result.model = "mock"
        return result

    def _run_batch(
        self,
        post_behavior: Any,
        live: bool = True,
        acquire_lock_return: bool = True,
        lock_exists: bool = True,
        llm_result: Any | None = None,
    ) -> tuple[dict[str, Any], Any]:
        """判断バッチ本体を実行し (result, dynamodb モック群) を返す。

        live=True では live_orders_allowed をモックして発注分岐へ入れるが、
        client 自体が MagicMock のため実送信は発生しない（two-key interlock の
        実装は TestGmoClient 側で検証済み。ここではロックライフサイクルを見る）。
        """
        from backend.src.batch.decision_batch import _run as decision_run

        client = self._build_mock_client(post_behavior)
        now_jst = datetime(2026, 6, 29, 20, 30, 0, tzinfo=self._JST)
        lock_outcome = LockAcquireOutcome(
            acquired=acquire_lock_return,
            lock_exists=(not acquire_lock_return) and lock_exists,
        )
        with (
            patch(
                "backend.src.batch.decision_batch.live_orders_allowed",
                return_value=live,
            ),
            patch(
                "backend.src.batch.decision_batch.get_confirmed_daily_candles",
                return_value=[self._CANDLE] * 15,
            ),
            patch(
                "backend.src.batch.decision_batch.generate_decision",
                return_value=llm_result if llm_result is not None else self._make_llm_result(),
            ),
            patch("backend.src.batch.decision_batch._fetch_macro_context", return_value=None),
            patch(
                "backend.src.batch.decision_batch.dynamodb_store.acquire_lock_outcome",
                return_value=lock_outcome,
            ) as mock_acquire,
            patch(
                "backend.src.batch.decision_batch.dynamodb_store.release_lock_outcome",
                return_value=LockReleaseOutcome.RELEASED,
            ) as mock_release,
            patch(
                "backend.src.batch.decision_batch.dynamodb_store.update_summary",
                return_value=True,
            ) as mock_summary,
            patch(
                "backend.src.batch.decision_batch.dynamodb_store.write_order_id",
                return_value=True,
            ) as mock_write,
            patch("backend.src.batch.decision_batch.s3_logger.save_decision_log"),
            patch("backend.src.batch.decision_batch.time.sleep"),
            patch("backend.src.batch.decision_batch._fetch_economic_events", return_value=_mock_economic_fetch()),
            patch("backend.src.batch.decision_batch.is_us_thin_market", return_value=False),
        ):
            result = decision_run(client=client, today=self._MONDAY, now_jst=now_jst)
        mocks = MagicMock(
            acquire=mock_acquire,
            release=mock_release,
            summary=mock_summary,
            write=mock_write,
            client=client,
        )
        return result, mocks

    @staticmethod
    def _post_response(status_code: int, body: dict[str, Any] | None = None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = str(body)
        resp.json.return_value = body or {}
        return resp

    def _assert_failed_unconfirmed(self, result: dict[str, Any], mocks: Any) -> None:
        """未確定失敗の共通契約: ロック保持 + lock_state の監査記録（S3/DDB 両方）。"""
        assert result["status"] == "failed"
        assert result["lock_released"] is False
        # S3 監査ログ（result）側にも lock_state が残る
        assert result["lock_state"] == "failed_unconfirmed"
        assert result["lock_state_persisted"] is True
        mocks.release.assert_not_called()
        mocks.write.assert_not_called()
        mocks.summary.assert_called_once_with(
            today=self._MONDAY, summary={"lock_state": "failed_unconfirmed"}
        )

    def test_transient_5xx_keeps_lock(self) -> None:
        """5xx は約定有無が未確定 — ロックを保持し lock_state を記録する（fail-safe）。"""
        result, mocks = self._run_batch(self._post_response(503))

        self._assert_failed_unconfirmed(result, mocks)

    def test_http_timeout_keeps_lock_and_does_not_crash(self) -> None:
        """タイムアウトは例外伝播させず failed に畳み、ロックを保持する。"""
        import httpx

        result, mocks = self._run_batch(httpx.ReadTimeout("timed out"))

        self._assert_failed_unconfirmed(result, mocks)

    def test_confirmed_4xx_rejection_releases_lock(self) -> None:
        """4xx は未約定確実 — ロックを解放し当日再試行を可能にする。"""
        result, mocks = self._run_batch(self._post_response(400))

        assert result["status"] == "failed"
        assert result["lock_released"] is True
        mocks.release.assert_called_once_with(today=self._MONDAY)

    def test_confirmed_api_rejection_releases_lock(self) -> None:
        """HTTP 200 でも body status != 0 は業務拒否＝未約定確実 — ロック解放。"""
        result, mocks = self._run_batch(
            self._post_response(200, {"status": 5, "messages": [{"message_code": "ERR-5003"}]})
        )

        assert result["status"] == "failed"
        assert result["lock_released"] is True
        mocks.release.assert_called_once_with(today=self._MONDAY)

    def test_ok_without_order_id_keeps_lock(self) -> None:
        """status=0 なのに orderId 不在は受理可能性を否定できず未確定 — ロック保持。"""
        result, mocks = self._run_batch(self._post_response(200, {"status": 0}))

        self._assert_failed_unconfirmed(result, mocks)

    def test_legacy_string_data_shape_keeps_lock(self) -> None:
        """data が旧推測形（注文ID直値の str）なら未確定 — ロック保持（#91 回帰）。

        実 API は注文オブジェクトの配列を返す。想定外の形で受理有無を断定できない以上、
        壊れた order_id を書くより未確定に倒す方が安全（翌朝の closeout を壊さない）。
        """
        result, mocks = self._run_batch(
            self._post_response(200, {"status": 0, "data": "1000101"})
        )

        assert result["error"] == "unexpected_data_shape"
        assert result["order_not_placed"] is False
        self._assert_failed_unconfirmed(result, mocks)

    def test_mismatched_root_order_ids_keep_lock(self) -> None:
        """3 件の rootOrderId が食い違ったら未確定 — どれを親IDとすべきか決められない。"""
        broken = _ifo_success_data()
        broken[1]["rootOrderId"] = 99999999  # 実 API では起きない不変条件違反
        result, mocks = self._run_batch(self._post_response(200, {"status": 0, "data": broken}))

        assert result["error"] == "unexpected_data_shape"
        assert result["order_not_placed"] is False
        self._assert_failed_unconfirmed(result, mocks)

    def test_success_keeps_lock_and_writes_order_id(self) -> None:
        """発注成功時はロック保持のまま orderId を書き戻す（回帰）。"""
        result, mocks = self._run_batch(self._post_response(200, {"status": 0, "data": _ifo_success_data()}))

        assert result["status"] == "ok"
        assert "lock_released" not in result
        mocks.release.assert_not_called()
        # 保存されるのは rootOrderId（cancel-first が rootOrderIds を要求するため・§6.2）
        mocks.write.assert_called_once_with(today=self._MONDAY, order_id="1000101")

    def test_stop_order_type_maps_to_first_execution_type(self) -> None:
        """STOP 判断時に ifoOrder body の firstExecutionType が STOP になる（#186）。"""
        # mid≈156.79。LONG STOP は entry >= current のブレイク側。
        llm_result = self._make_llm_result(order_type="STOP", entry_price=157.000)
        result, mocks = self._run_batch(
            self._post_response(200, {"status": 0, "data": _ifo_success_data()}),
            llm_result=llm_result,
        )

        assert result["status"] == "ok"
        mocks.client.private_post.assert_called_once()
        body = mocks.client.private_post.call_args.kwargs["body"]
        assert body.first_execution_type == "STOP"

    def test_stub_mode_consumes_lock_without_release(self) -> None:
        """stub モードでもロックを消費し解放しない（モック週は当日 1 回・本番と同型）。"""
        result, mocks = self._run_batch(post_behavior=self._post_response(200), live=False)

        assert result["status"] == "stubbed"
        mocks.acquire.assert_called_once_with(today=self._MONDAY)
        mocks.release.assert_not_called()
        # 未発注なのに偽 orderId をロックへ書き戻すとリトライ判定を狂わせる — 回帰防止
        mocks.write.assert_not_called()

    def test_existing_lock_still_prevents_double_order(self) -> None:
        """二重発注防止の回帰: ロック既存なら発注に進まず skip する。"""
        result, mocks = self._run_batch(self._post_response(200), acquire_lock_return=False)

        assert result["status"] == "skipped"
        assert result["reason"] == "dynamodb_lock_exists"
        mocks.client.private_post.assert_not_called()

    def test_lock_backend_failure_reason_is_unavailable(self) -> None:
        """#210 L-3: DDB 障害は dynamodb_lock_exists に潰さず unavailable にする。"""
        with patch("backend.src.batch.lock_acquire.send_slack_alert") as slack:
            result, mocks = self._run_batch(
                self._post_response(200),
                acquire_lock_return=False,
                lock_exists=False,
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "dynamodb_lock_unavailable"
        mocks.client.private_post.assert_not_called()
        slack.assert_called_once()

    def test_missing_api_status_keeps_lock(self) -> None:
        """body に status が無いレスポンスは拒否と断定できず未確定 — ロック保持（fail-safe）。"""
        result, mocks = self._run_batch(self._post_response(200, {"data": "ORD-1"}))

        self._assert_failed_unconfirmed(result, mocks)

    def test_invalid_json_keeps_lock(self) -> None:
        """HTTP 200 でも JSON パース不能なら未確定 — ロック保持（fail-safe）。"""
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "<html>gateway error</html>"
        resp.json.side_effect = ValueError("invalid json")
        result, mocks = self._run_batch(resp)

        self._assert_failed_unconfirmed(result, mocks)

    def test_non_dict_json_keeps_lock(self) -> None:
        """valid JSON でも dict 以外（配列等）は AttributeError で落とさず未確定 — ロック保持。"""
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "[]"
        resp.json.return_value = []
        result, mocks = self._run_batch(resp)

        self._assert_failed_unconfirmed(result, mocks)


def _ifo_success_data(root_order_id: int = 1000101) -> list[dict[str, object]]:
    """ifoOrder 成功レスポンスの data（2026-08-03 実発注スパイクで採取した実形・#91）。

    IFD-OCO は 3 件返り、3 件とも同じ rootOrderId を共有する。
    """
    base = {
        "orderType": "IFDOCO",
        "size": "100",
        "status": "WAITING",
        "symbol": "USD_JPY",
        "expiry": "20260902",
        "timestamp": "2026-08-03T06:00:00.000Z",
        "rootOrderId": root_order_id,
    }
    return [
        {**base, "orderId": root_order_id, "settleType": "OPEN", "side": "BUY",
         "executionType": "LIMIT", "price": "156.428"},
        {**base, "orderId": root_order_id + 1, "settleType": "CLOSE", "side": "SELL",
         "executionType": "LIMIT", "price": "156.928"},
        {**base, "orderId": root_order_id + 2, "settleType": "CLOSE", "side": "SELL",
         "executionType": "STOP", "price": "155.928"},
    ]


class TestEntryTickRoundingDirection:
    """#238: entry の tick 丸めは方向依存（現値から遠ざける向き）。

    無条件 ROUND_DOWN だと SHORT の entry が現値を下回る側へ押し込まれ、
    pre_order の ORDER_TYPE_ENTRY_POSITION_VIOLATION で reject される
    （2026-07 の live 実測 3 件がこの機序）。
    """

    def test_long_rounds_down_away_from_current(self) -> None:
        """LONG は切り下げ = より深い押し目（entry <= 現値 を維持）。"""
        assert _round_tick_entry(163.6465, "LONG") == 163.646

    def test_short_rounds_up_away_from_current(self) -> None:
        """SHORT は切り上げ = より高い戻り（entry >= 現値 を維持）。"""
        assert _round_tick_entry(162.4905, "SHORT") == 162.491

    def test_exact_tick_is_unchanged_both_sides(self) -> None:
        """既に tick 上の値は方向によらず変化しない。"""
        assert _round_tick_entry(162.491, "LONG") == 162.491
        assert _round_tick_entry(162.491, "SHORT") == 162.491

    @pytest.mark.parametrize(
        ("current", "side"),
        [
            (162.4905, "SHORT"),  # live 2026-07-09: 旧実装は 162.490 で −0.05pips 違反
            (163.0425, "SHORT"),  # live 2026-07-22: 旧実装は 163.040 で −0.25pips 違反
            (163.6465, "LONG"),  # live 2026-07-29: LONG は切り下げで現値以下を維持
        ],
    )
    def test_live_reject_cases_no_longer_violate_direction(
        self, current: float, side: str
    ) -> None:
        """live で reject された 3 ケースの現値を丸めても方向条件を破らない。

        LLM が entry に現値を指定した最悪ケースを想定する。丸め後は
        LONG: entry <= current / SHORT: entry >= current が成立していること。
        """
        rounded = _round_tick_entry(current, side)  # type: ignore[arg-type]
        if side == "LONG":
            assert rounded <= current
        else:
            assert rounded >= current


# ---------------------------------------------------------------------------
# #243: ticker のボディ status 検査（HTTP 200 の業務エラー）
# ---------------------------------------------------------------------------


class TestTickerBodyStatus:
    """GMO は業務エラーでも HTTP 200 + body status:1 を返す（#243）。

    `status_code` だけ見ると業務エラーが「data が空」＝データなしに化ける。
    2026-08-05 のノート捏造事故と同じ未検査パターン
    （.claude/rules/gmo-api.md / 付録B executions_api_spec.md §1）。
    """

    def _fetch(self, body: dict[str, Any]) -> tuple[float, float] | None:
        from unittest.mock import MagicMock, patch

        from backend.src.batch.decision_batch import _fetch_ticker

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = body
        client = MagicMock()
        client.public_get.return_value = resp
        # レートリミットの sleep(1) をテストで待たない
        with patch("backend.src.batch.decision_batch.time.sleep"):
            return _fetch_ticker(client=client)

    def test_normal_response_returns_mid_and_spread(self) -> None:
        got = self._fetch({"status": 0, "data": [{"bid": "156.799", "ask": "156.800"}]})
        assert got is not None
        mid, spread_pips = got
        assert mid == pytest.approx(156.7995)
        assert spread_pips == pytest.approx(0.1)

    def test_business_error_returns_none(self) -> None:
        """🔴 HTTP 200 + status:1 を「データなし」に化けさせないこと。"""
        got = self._fetch(
            {
                "status": 1,
                "messages": [
                    {"message_code": "ERR-5003", "message_string": "Requests are too many."}
                ],
            }
        )
        assert got is None

    def test_business_error_with_data_still_returns_none(self) -> None:
        """status:1 なら data が付いていても採用しないこと。

        「data があるかどうか」で判断していると素通りする経路。
        """
        got = self._fetch(
            {"status": 1, "messages": [], "data": [{"bid": "1.0", "ask": "2.0"}]}
        )
        assert got is None

    def test_empty_data_returns_none(self) -> None:
        """従来どおり、正常応答で data が空なら None（fail-closed）。"""
        assert self._fetch({"status": 0, "data": []}) is None
