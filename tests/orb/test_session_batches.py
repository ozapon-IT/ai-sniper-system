"""ORB-4 セッションバッチ群のテスト（#270）。

設計書: docs/ai-dev/london-orb-demo-design.md §6.1・§6.2・§6.4・§6.5

ネットワーク・AWS 非依存。klines / ticker はフェイククライアントで供給し、
永続化は `LOCAL_PERSIST_DIR`（tmp_path）へ逃がす。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from backend.src.adapters import orb_quotes
from backend.src.batch import orb_poller, orb_session_open
from backend.src.orb.broker_models import BrokerSession, TickerObservation
from backend.src.orb.constants import LON

SESSION_DATE = "2026-08-10"  # 月曜・BST


def _lon(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 8, 10, h, m, s, tzinfo=LON)


def _ms(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1000))


def _minute_candles(
    *, high_at_0830: float = 150.00, low_at_0815: float = 149.80
) -> list[dict[str, str]]:
    """London 8:00〜8:59 の 1 分足 60 本（BID）。高安を指定位置に埋め込む。"""
    out: list[dict[str, str]] = []
    for i in range(60):
        t = _lon(8, 0) + timedelta(minutes=i)
        base = 149.90
        hi, lo = base + 0.01, base - 0.01
        if t.minute == 30:
            hi = high_at_0830
        if t.minute == 15:
            lo = low_at_0815
        out.append(
            {
                "openTime": _ms(t),
                "open": f"{base:.3f}",
                "high": f"{hi:.3f}",
                "low": f"{lo:.3f}",
                "close": f"{base:.3f}",
            }
        )
    return out


class FakeClient:
    """`public_get` だけを持つ最小のフェイク（Private 経路は存在しない）。"""

    def __init__(
        self,
        *,
        candles_1m: list[dict[str, str]] | None = None,
        candles_15m: list[dict[str, str]] | None = None,
        market_status: str = "OPEN",
        ticker_rows: list[dict[str, Any]] | None = None,
        error_paths: set[str] | None = None,
    ) -> None:
        self.candles_1m = candles_1m if candles_1m is not None else _minute_candles()
        self.candles_15m = candles_15m or []
        self.market_status = market_status
        self.ticker_rows = ticker_rows or []
        # 業務エラー（HTTP 200 + status!=0）を返すパス末尾（#327）
        self.error_paths = error_paths or set()
        self.calls: list[str] = []

    def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(path)
        if any(path.endswith(suffix) for suffix in self.error_paths):
            # GMO は業務エラーでも HTTP 200 を返す（.claude/rules/gmo-api.md）
            return _Resp(
                {
                    "status": 1,
                    "messages": [
                        {"message_code": "ERR-5003", "message_string": "Requests are too many."}
                    ],
                }
            )
        if path.endswith("/status"):
            return _Resp({"status": 0, "data": {"status": self.market_status}})
        if path.endswith("/ticker"):
            return _Resp({"status": 0, "data": self.ticker_rows})
        interval = (params or {}).get("interval")
        data = self.candles_1m if interval == "1min" else self.candles_15m
        return _Resp({"status": 0, "data": data})

    def close(self) -> None:  # pragma: no cover - 互換用
        pass


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture(autouse=True)
def _local_persist(tmp_path, monkeypatch) -> None:
    """永続化をテンポラリへ逃がす（AWS 非依存）。"""
    monkeypatch.setenv("LOCAL_PERSIST_DIR", str(tmp_path))
    for mod in ("backend.src.orb.session_store", "backend.src.orb.event_log"):
        import importlib

        m = importlib.import_module(mod)
        monkeypatch.setattr(m, "LOCAL_PERSIST_DIR", str(tmp_path), raising=False)


# ---------------------------------------------------------------- adapter


class TestQuotesAdapter:
    def test_klines_file_date_uses_jst_0600_boundary(self) -> None:
        from zoneinfo import ZoneInfo

        jst = ZoneInfo("Asia/Tokyo")
        # JST 05:59 は前暦日のファイル
        assert orb_quotes.klines_file_date(datetime(2026, 8, 10, 5, 59, tzinfo=jst)) == "20260809"
        assert orb_quotes.klines_file_date(datetime(2026, 8, 10, 6, 0, tzinfo=jst)) == "20260810"

    def test_in_progress_bar_is_filtered(self) -> None:
        """進行中足（openTime+60s > now）を掴まない（ORB-1b #268）。"""
        now = _lon(8, 30, 3).astimezone(UTC)
        client = FakeClient()
        bars = orb_quotes.fetch_confirmed_1m(client, date_str="20260810", now=now)
        # 8:30 の足は 8:31 に閉じるので now=8:30:03 では未確定 → 除外
        opens = {b.local_open.strftime("%H:%M") for b in bars}
        assert "08:29" in opens
        assert "08:30" not in opens

    def test_observation_uses_row_status(self) -> None:
        client = FakeClient(
            ticker_rows=[
                {
                    "symbol": "USD_JPY",
                    "bid": "150.100",
                    "ask": "150.105",
                    "timestamp": "2026-08-10T08:30:00.000000Z",
                    "status": "CLOSE",
                }
            ]
        )
        obs = orb_quotes.fetch_observation(client)
        assert obs.status == "CLOSE"
        assert obs.bid == pytest.approx(150.100)
        assert round((obs.ask - obs.bid) / 0.01, 2) == 0.50  # ORB-1b の現行スプレッド

    def test_missing_symbol_raises(self) -> None:
        with pytest.raises(LookupError):
            orb_quotes.fetch_observation(FakeClient(ticker_rows=[{"symbol": "EUR_JPY"}]))


class TestEnvelopeStatusChecked:
    """🔴 HTTP 200 + `status != 0` の業務エラーを「値なし」に化けさせない（#327 / #243 同型）。

    GMO は業務エラーでも HTTP 200 を返すため、エンベロープの `status` を見ないと
    `data` 欠落＝データなしに化ける（`.claude/rules/gmo-api.md`）。
    """

    def test_market_status_raises_on_business_error(self) -> None:
        from backend.src.gmo_errors import GmoApiError

        with pytest.raises(GmoApiError):
            orb_quotes.fetch_market_status(FakeClient(error_paths={"/status"}))

    def test_market_status_still_reads_data_status(self) -> None:
        """`data.status`（市場状態）の読み取りは従来どおり。エンベロープとは別物。"""
        assert orb_quotes.fetch_market_status(FakeClient(market_status="OPEN")) == "OPEN"
        assert orb_quotes.fetch_market_status(FakeClient(market_status="MAINTENANCE")) == "MAINTENANCE"

    def test_ticker_business_error_is_distinct_from_missing_symbol(self) -> None:
        """業務エラー（GmoApiError）と「行が無い」（LookupError）を区別できること。"""
        from backend.src.gmo_errors import GmoApiError

        with pytest.raises(GmoApiError):
            orb_quotes.fetch_observation(FakeClient(error_paths={"/ticker"}))
        with pytest.raises(LookupError):
            orb_quotes.fetch_observation(FakeClient(ticker_rows=[{"symbol": "EUR_JPY"}]))

    def test_klines_business_error_is_not_empty_bars(self) -> None:
        from backend.src.gmo_errors import GmoApiError

        with pytest.raises(GmoApiError):
            orb_quotes.fetch_confirmed_1m(
                FakeClient(error_paths={"/klines"}), date_str="20260810", now=_lon(9, 1)
            )


class TestApiErrorIsOutageNotClosed:
    """🔴 業務エラーを `market_closed` に落とさない（#327）。

    `weekly.OUTAGE_SKIP_REASONS` は `system_outage` のみ。`market_closed` に分類すると
    §6.5 の機会損失に**計上されず**、その日が無通知で消える。稼働率は §1.2 の合否基準。
    """

    def test_session_open_classifies_status_api_error_as_system_outage(self) -> None:
        res = orb_session_open.run_session_open(
            client=FakeClient(error_paths={"/status"}), now=_lon(9, 1)
        )
        assert res["status"] == "skipped"
        assert res["reason"] == "system_outage", "API エラーが market_closed に化けている"

    def test_system_outage_is_counted_as_opportunity_loss(self) -> None:
        """分類が weekly の機会損失に載ることまで確認する（分類の意味が目的）。"""
        from backend.src.orb import weekly

        assert "system_outage" in weekly.OUTAGE_SKIP_REASONS
        assert "market_closed" not in weekly.OUTAGE_SKIP_REASONS

    def test_session_open_still_reports_market_closed_for_real_closure(self) -> None:
        """本物の閉場は従来どおり `market_closed`（過剰に outage へ倒さない）。"""
        res = orb_session_open.run_session_open(
            client=FakeClient(market_status="MAINTENANCE"), now=_lon(9, 1)
        )
        assert res["reason"] == "market_closed"
        assert res["market_status"] == "MAINTENANCE"

    def test_klines_api_error_falls_through_to_system_outage(self) -> None:
        slept: list[float] = []
        res = orb_session_open.run_session_open(
            client=FakeClient(error_paths={"/klines"}), now=_lon(9, 1), sleep=slept.append
        )
        assert res["status"] == "skipped"
        assert res["reason"] == "system_outage"
        assert len(slept) == orb_session_open.RANGE_RETRY_MAX - 1, "リトライしていない"

    def test_poller_gate_api_error_writes_no_market_closed_event(self) -> None:
        """poller のゲートでも `market_closed` イベントを残さない。"""
        from backend.src.orb.event_log import load_events

        orb_session_open.run_session_open(client=FakeClient(), now=_lon(9, 1))
        before = len(load_events(SESSION_DATE))

        res = orb_poller.run_poller(client=FakeClient(error_paths={"/status"}), now=_lon(9, 5))
        assert res["status"] == "api_error"
        assert res["polls"] == 0
        assert len(load_events(SESSION_DATE)) == before, (
            "API エラーが market_closed イベントとして記録されている"
        )


# ---------------------------------------------------------------- session-open


class TestSessionOpen:
    def test_arms_from_1m_range(self) -> None:
        client = FakeClient()
        res = orb_session_open.run_session_open(client=client, now=_lon(9, 1))
        assert res["status"] == "armed"
        assert res["range_source"] == "1m"
        # レンジ = 8:00〜8:59 の高安 ±1pip
        assert res["range_high"] == pytest.approx(150.00)
        assert res["range_low"] == pytest.approx(149.80)
        assert res["buy_stop"] == pytest.approx(150.01)
        assert res["sell_stop"] == pytest.approx(149.79)

    def test_falls_back_to_15m_when_1m_missing(self) -> None:
        """1分足欠損（2025-06-20 型）でも 15分足で確定する（§6.2）。"""
        bars15 = []
        for i in range(4):
            open_utc = (_lon(8, 0) + timedelta(minutes=15 * i)).astimezone(UTC)
            bars15.append(
                {
                    "openTime": _ms(open_utc),
                    "open": "149.90",
                    "high": "150.00" if i == 2 else "149.95",
                    "low": "149.80" if i == 1 else "149.85",
                    "close": "149.90",
                }
            )
        client = FakeClient(candles_1m=[], candles_15m=bars15)
        res = orb_session_open.run_session_open(client=client, now=_lon(9, 1))
        assert res["status"] == "armed"
        assert res["range_source"] == "15m"
        assert res["buy_stop"] == pytest.approx(150.01)

    def test_retries_then_system_outage(self) -> None:
        slept: list[float] = []
        client = FakeClient(candles_1m=[], candles_15m=[])
        res = orb_session_open.run_session_open(
            client=client, now=_lon(9, 1), sleep=slept.append
        )
        assert res["status"] == "skipped"
        assert res["reason"] == "system_outage"
        assert len(slept) == orb_session_open.RANGE_RETRY_MAX - 1  # 最終試行後は待たない

    @pytest.mark.parametrize("status", ["CLOSE", "MAINTENANCE"])
    def test_market_closed_is_not_counted_as_holiday(self, status: str) -> None:
        """閉場・メンテは `market_closed`。休場カレンダー由来の `market_holiday` と混ぜない（§6.5）。"""
        client = FakeClient(market_status=status)
        res = orb_session_open.run_session_open(client=client, now=_lon(9, 1))
        assert res["status"] == "skipped"
        assert res["reason"] == "market_closed"
        assert res["market_status"] == status

        from backend.src.orb.event_log import load_events
        from backend.src.orb.session_store import load_session

        session = load_session(SESSION_DATE)
        assert session is not None
        assert session.skip_reason == "market_closed"
        events = load_events(SESSION_DATE)
        assert events and events[-1]["detail"]["market_status"] == status

    def test_market_holiday_reason_is_distinct(self, monkeypatch) -> None:
        monkeypatch.setattr(orb_session_open, "_is_market_holiday", lambda _d: True)
        res = orb_session_open.run_session_open(client=FakeClient(), now=_lon(9, 1))
        assert res["reason"] == "market_holiday"

    def test_retry_rereads_clock_for_confirmation(self) -> None:
        """リトライ時は確定足の基準時刻を読み直す（待機中に確定した足を拾う）。"""
        seen: list[datetime] = []
        client = FakeClient(candles_1m=[], candles_15m=[])
        ticks = iter([_lon(9, 1), _lon(9, 2), _lon(9, 3)])

        def clock() -> datetime:
            t = next(ticks)
            seen.append(t)
            return t

        orb_session_open.run_session_open(
            client=client, now=_lon(9, 1), sleep=lambda _s: None, clock=clock
        )
        assert seen == [_lon(9, 1), _lon(9, 2), _lon(9, 3)]

    def test_halt_skips_without_fetching_klines(self, monkeypatch) -> None:
        monkeypatch.setattr(orb_session_open, "get_halt", lambda: True)
        client = FakeClient()
        res = orb_session_open.run_session_open(client=client, now=_lon(9, 1))
        assert res == {"status": "skipped", "session_date": SESSION_DATE, "reason": "halted"}
        assert client.calls == []  # halt 時は API を叩かない

    def test_disabled_skips_without_fetching_klines(self, monkeypatch) -> None:
        """ORB_DEMO_ENABLED=false はマスタースイッチ。API を叩かず SKIPPED(disabled)。"""
        monkeypatch.setenv("ORB_DEMO_ENABLED", "false")
        client = FakeClient()
        res = orb_session_open.run_session_open(client=client, now=_lon(9, 1))
        assert res == {"status": "skipped", "session_date": SESSION_DATE, "reason": "disabled"}
        assert client.calls == []

        from backend.src.orb.session_store import load_session

        session = load_session(SESSION_DATE)
        assert session is not None
        assert session.skip_reason == "disabled"


# ---------------------------------------------------------------- poller


class TestPoller:
    def test_no_session_is_noop(self) -> None:
        res = orb_poller.run_poller(client=FakeClient(), now=_lon(9, 5))
        assert res["status"] == "no_session"

    def test_market_closed_takes_no_observations_but_records_event(self) -> None:
        """観測は採らないが稼働イベントは残す（§6.1 ゲート / §6.5 稼働率）。"""
        orb_session_open.run_session_open(client=FakeClient(), now=_lon(9, 1))
        from backend.src.orb.event_log import load_events

        before = len(load_events(SESSION_DATE))

        client = FakeClient(market_status="MAINTENANCE")
        res = orb_poller.run_poller(client=client, now=_lon(9, 5))
        assert res["status"] == "market_closed"
        assert "/ticker" not in "".join(client.calls)

        events = load_events(SESSION_DATE)
        assert len(events) == before + 1
        gate = events[-1]
        assert gate["reason"] == "market_closed"
        assert gate["detail"] == {"status": "MAINTENANCE", "phase": "poller_gate"}
        assert gate["state_from"] == gate["state_to"] == "ARMED"

    def test_poll_interval_env(self, monkeypatch) -> None:
        monkeypatch.setenv("ORB_POLL_INTERVAL_SEC", "0.5")
        assert orb_poller.poll_interval_sec() == 1.0  # 下限 1
        monkeypatch.setenv("ORB_POLL_INTERVAL_SEC", "bogus")
        assert orb_poller.poll_interval_sec() == 5.0


# ---------------------------------------------------------------- E2E


class TestReplayDrivenE2E:
    """受け入れ条件: 記録済み気配のリプレイ駆動で 1 セッション E2E（§11 ORB-4）。"""

    def _armed_session(self):
        orb_session_open.run_session_open(client=FakeClient(), now=_lon(9, 1))
        from backend.src.orb.session_store import load_session

        s = load_session(SESSION_DATE)
        assert s is not None and s.state == "ARMED"
        return s

    def test_long_fill_then_eod(self) -> None:
        session = self._armed_session()
        quotes = [
            TickerObservation(ts=_lon(9, 10), bid=149.95, ask=149.955, status="OPEN"),
            TickerObservation(ts=_lon(9, 20), bid=150.02, ask=150.025, status="OPEN"),  # buy_stop 到達
            TickerObservation(ts=_lon(12, 0), bid=150.30, ask=150.305, status="OPEN"),
            TickerObservation(ts=_lon(16, 0, 2), bid=150.55, ask=150.555, status="OPEN"),  # EOD
        ]
        final, events = orb_poller.drive_session(session, quotes)
        assert final.state == "CLOSED"
        assert final.direction == "long"
        assert final.exit_reason == "eod"
        assert final.entry_price == pytest.approx(150.02)
        assert final.exit_price == pytest.approx(150.55)
        assert final.entry_slip is not None
        # 推定滑り = 観測BID − トリガー = 150.02 − 150.01 = +1.0pip
        assert final.entry_slip.slip_pips == pytest.approx(1.0)
        assert {e.reason for e in events} >= {"filled", "eod"}

    def test_short_fill_then_sl(self) -> None:
        session = self._armed_session()
        quotes = [
            TickerObservation(ts=_lon(9, 30), bid=149.78, ask=149.785, status="OPEN"),  # sell_stop
            TickerObservation(ts=_lon(9, 31), bid=149.80, ask=149.805, status="OPEN"),
            TickerObservation(ts=_lon(10, 0), bid=150.05, ask=150.055, status="OPEN"),  # SL=buy_stop
        ]
        final, _ = orb_poller.drive_session(session, quotes)
        assert final.state == "CLOSED"
        assert final.direction == "short"
        assert final.exit_reason == "sl"

    def test_closed_observations_do_not_pollute(self) -> None:
        """閉場観測は破棄され、滑りの基準（直前観測）にもならない（§6.1 / §6.3）。"""
        session = self._armed_session()
        quotes = [
            TickerObservation(ts=_lon(9, 10), bid=149.95, ask=149.955, status="OPEN"),
            # 閉場中の停止価格・10pips スプレッド（ORB-1b 実測形）
            TickerObservation(ts=_lon(9, 15), bid=151.00, ask=151.10, status="CLOSE"),
            TickerObservation(ts=_lon(9, 20), bid=150.02, ask=150.025, status="OPEN"),
        ]
        final, _ = orb_poller.drive_session(session, quotes)
        assert final.state == "FILLED"
        assert final.entry_slip is not None
        # 直前観測は 9:10 の OPEN 分（閉場の 9:15 ではない）
        assert final.entry_slip.poll_gap_ms == 10 * 60 * 1000
        assert final.entry_slip.spread_pips == pytest.approx(0.5)

    def test_no_fill_cancelled_at_1100(self) -> None:
        """11:00 直前まで観測が続いていれば `no_fill` で確定する。

        ポーリングは既定 5 秒間隔なので、11:00 を跨ぐ 2 点は数秒差で並ぶのが正常。
        欠測で跨いだ場合は `UNCERTAIN`（ORB-3 `test_uncertain_when_gap_straddles_1100`）。
        """
        session = self._armed_session()
        quotes = [
            TickerObservation(ts=_lon(9, 30), bid=149.90, ask=149.905, status="OPEN"),
            TickerObservation(ts=_lon(10, 59, 57), bid=149.91, ask=149.915, status="OPEN"),
            TickerObservation(ts=_lon(11, 0, 1), bid=149.92, ask=149.925, status="OPEN"),
        ]
        final, _ = orb_poller.drive_session(session, quotes)
        assert final.state == "CANCELLED"


# ---------------------------------------------------------------- containment


class TestBatchContainment:
    """バッチ層も発注系 POST / two-key / LLM を参照しない（設計書 §3.2）。"""

    def test_no_forbidden_refs_in_orb_batches(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "backend" / "src"
        targets = [
            root / "batch" / "orb_session_open.py",
            root / "batch" / "orb_poller.py",
            root / "adapters" / "orb_quotes.py",
        ]
        forbidden = (
            "ifoOrder",
            "ifdOrder",
            "closeOrder",
            "cancelOrders",
            "ALLOW_LIVE_ORDERS",
            "TRADING_PHASE",
            "MVP_MODE",
            "anthropic",
            "private_get",
            "private_post",
            "PRIVATE_",
        )
        hits: list[str] = []
        for path in targets:
            text = path.read_text()
            hits.extend(f"{path.name}:{tok}" for tok in forbidden if tok in text)
        assert hits == [], f"forbidden refs: {hits}"


class TestStructuredLogging:
    """🔴 ORB バッチのログが CloudWatch に出ること（#316）。

    `lambda_handler` の中で `logging.basicConfig` を呼んでも **Lambda では no-op**
    （起動時に root へハンドラが入っているため）。稼働開始 2026-08-14 から 2026-08-17 まで、
    ORB の 4 ロググループに**アプリログが 1 行も出ていなかった**。夜型と同じく
    `configure_logging()` をモジュールトップで呼ぶ。
    """

    BATCHES = (
        "orb_poller",
        "orb_session_open",
        "orb_verify",
        "orb_weekly",
        "orb_note_batch",
    )

    def _source(self, name: str) -> str:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "backend" / "src" / "batch"
        return (root / f"{name}.py").read_text(encoding="utf-8")

    @pytest.mark.parametrize("name", BATCHES)
    def test_configures_logging_at_module_top(self, name: str) -> None:
        src = self._source(name)
        assert "from backend.src.logging_config import configure_logging" in src
        # モジュールトップ = 最初の def / class より前で呼ばれていること
        call = src.index("configure_logging()")
        first_def = min(
            (src.index(tok) for tok in ("\ndef ", "\nclass ") if tok in src),
            default=len(src),
        )
        assert call < first_def, f"{name}: configure_logging() がモジュールトップにない"

    @pytest.mark.parametrize("name", BATCHES)
    def test_no_basic_config(self, name: str) -> None:
        """`basicConfig` は Lambda で no-op。復活させない。

        判定はコメント行を除いた**実コード**に対して行う（本 PR で入れた
        「なぜ basicConfig を使わないか」の説明コメント自体で誤爆しないため）。
        """
        code = "\n".join(
            line for line in self._source(name).splitlines() if not line.strip().startswith("#")
        )
        assert "basicConfig" not in code, f"{name}: basicConfig が復活している"


class TestPollerSummaryLog:
    """§6.5 の稼働率は戻り値ではなくログで観測する（#316）。"""

    def test_summary_is_logged_for_each_exit_path(self, caplog) -> None:
        import logging as _logging

        orb_session_open.run_session_open(client=FakeClient(), now=_lon(9, 1))

        with caplog.at_level(_logging.INFO, logger="backend.src.batch.orb_poller"):
            res = orb_poller.run_poller(
                client=FakeClient(market_status="MAINTENANCE"), now=_lon(9, 5)
            )
        assert res["status"] == "market_closed"
        rec = [r for r in caplog.records if r.getMessage() == "orb_poller_summary"]
        assert len(rec) == 1, "サマリが出ていない"
        assert rec[0].status == "market_closed"  # type: ignore[attr-defined]

    def test_summary_carries_the_guard_counters(self, caplog) -> None:
        """`rejected_transitions` / `save_failures` がログから読めること（#304 の観測手段）。

        🔴 **`ok` 経路で見る。** 早期 return する `no_session` / `market_closed` は
        カウンタを持たないので、そちらで assert してもこの受け入れ条件を満たさない。
        """
        import logging as _logging

        orb_session_open.run_session_open(client=FakeClient(), now=_lon(9, 1))
        rows = [
            {
                "symbol": "USD_JPY",
                "bid": "149.900",  # 両ストップの内側 = 約定しない
                "ask": "149.905",
                "timestamp": _lon(9, 5, 35).astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "status": "OPEN",
            }
        ]
        state = {"n": -1}

        def monotonic() -> float:
            state["n"] += 1
            return float(state["n"])

        with caplog.at_level(_logging.INFO, logger="backend.src.batch.orb_poller"):
            res = orb_poller.run_poller(
                client=FakeClient(ticker_rows=rows),
                now=_lon(9, 5, 35),
                run_seconds=2.5,  # 2 回まわす
                interval_sec=0.0,
                sleep=lambda _s: None,
                monotonic=monotonic,
            )

        assert res["status"] == "ok"
        rec = [r for r in caplog.records if r.getMessage() == "orb_poller_summary"]
        assert len(rec) == 1
        summary = rec[0]
        assert summary.status == "ok"  # type: ignore[attr-defined]
        assert summary.polls == 2  # type: ignore[attr-defined]
        assert summary.rejected_transitions == 0  # type: ignore[attr-defined]
        assert summary.save_failures == 0  # type: ignore[attr-defined]
        assert summary.errors == 0  # type: ignore[attr-defined]
        assert summary.closed_observations == 0  # type: ignore[attr-defined]


class TestSaveRegressionGuard:
    """15:55 と 16:00 の poller が重なっても EOD を消さないこと（#299 apply 事故）。

    本来は Lambda の予約同時実行で直列化する設計だったが、アカウントの同時実行上限が
    10 で予約できず apply が失敗した。インフラで直列化できないため書き込み側で守る。
    """

    def _levels(self):
        from backend.src.orb.models import RangeLevels

        return RangeLevels(high=150.00, low=149.80, buy_stop=150.01, sell_stop=149.79)

    def test_terminal_is_sticky_closed_sl_vs_closed_eod(self) -> None:
        """🔴 本番の重なりは **終端 vs 終端**（レビュー指摘）。

        15:55 回がメモリ上で CLOSED(sl) になっていても DynamoDB は 16:00 頃まで
        更新されない。16:00 回は古い FILLED から EOD して CLOSED(eod) を書く。
        その後 15:55 が CLOSED(sl) を書くと、ランク比較（3 vs 3）では素通りして
        **EOD が消える**。終端は上書き禁止にする。
        """
        from backend.src.orb.session_store import load_session, save_session

        eod = BrokerSession(
            session_date=SESSION_DATE,
            state="CLOSED",
            levels=self._levels(),
            direction="long",
            entry_price=150.02,
            exit_price=150.55,
            exit_reason="eod",
        )
        assert save_session(eod) is True

        sl_late = BrokerSession(
            session_date=SESSION_DATE,
            state="CLOSED",
            levels=self._levels(),
            direction="long",
            entry_price=150.02,
            exit_price=149.79,
            exit_reason="sl",
        )
        assert save_session(sl_late) is True  # エラーにはしない

        after = load_session(SESSION_DATE)
        assert after is not None
        assert after.exit_reason == "eod", "終端どうしで上書きされた"

    def test_terminal_is_sticky_against_any_state(self) -> None:
        """終端が入ったら ARMED でも FILLED でも上書きしない。"""
        from backend.src.orb.session_store import load_session, save_session

        save_session(
            BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=self._levels())
        )
        for st in ("ARMED", "FILLED", "SKIPPED"):
            save_session(
                BrokerSession(session_date=SESSION_DATE, state=st, levels=self._levels())  # type: ignore[arg-type]
            )
        after = load_session(SESSION_DATE)
        assert after is not None and after.state == "CANCELLED"

    def test_stale_filled_does_not_overwrite_closed(self) -> None:
        from backend.src.orb.session_store import load_session, save_session

        closed = BrokerSession(
            session_date=SESSION_DATE,
            state="CLOSED",
            levels=self._levels(),
            direction="long",
            entry_price=150.02,
            exit_price=150.55,
            exit_reason="eod",
        )
        assert save_session(closed) is True

        # 遅れて到着した 15:55 側の save（まだ FILLED のまま）
        stale = BrokerSession(
            session_date=SESSION_DATE,
            state="FILLED",
            levels=self._levels(),
            direction="long",
            entry_price=150.02,
        )
        assert save_session(stale) is True  # エラーにはしない

        after = load_session(SESSION_DATE)
        assert after is not None
        assert after.state == "CLOSED", "EOD が古い save に上書きされた"
        assert after.exit_reason == "eod"

    def test_forward_progress_is_saved(self) -> None:
        from backend.src.orb.session_store import load_session, save_session

        save_session(BrokerSession(session_date=SESSION_DATE, state="ARMED", levels=self._levels()))
        save_session(
            BrokerSession(
                session_date=SESSION_DATE, state="FILLED", levels=self._levels(), direction="long"
            )
        )
        s = load_session(SESSION_DATE)
        assert s is not None and s.state == "FILLED"

    def test_allow_regression_escape_hatch(self) -> None:
        from backend.src.orb.session_store import load_session, save_session

        save_session(BrokerSession(session_date=SESSION_DATE, state="CLOSED", levels=self._levels()))
        save_session(
            BrokerSession(session_date=SESSION_DATE, state="ARMED", levels=self._levels()),
            allow_regression=True,
        )
        s = load_session(SESSION_DATE)
        assert s is not None and s.state == "ARMED"


class TestEntryPrecedence:
    """#304: entry は先着確定。2 度目の fill 判定が滑り統計（§6.3）を汚さないこと。

    終端 sticky（`TestSaveRegressionGuard`）は **FILLED → FILLED を素通し**する
    （同ランク・非終端）ため、これは別の防御が要る。
    """

    def _levels(self):
        from backend.src.orb.models import RangeLevels

        return RangeLevels(high=150.00, low=149.80, buy_stop=150.01, sell_stop=149.79)

    def _filled(self, *, entry_price: float, entry_ts, state: str = "FILLED", **kw):
        return BrokerSession(
            session_date=SESSION_DATE,
            state=state,  # type: ignore[arg-type]
            levels=self._levels(),
            direction="short",
            entry_price=entry_price,
            entry_ts=entry_ts,
            sl_price=150.01,
            **kw,
        )

    def test_second_fill_cannot_replace_first_entry(self) -> None:
        """2026-08-14 の実事象: 真 fill 149.78 を後発の 149.70 が上書きし得た。"""
        from backend.src.orb.session_store import (
            SAVE_REJECTED,
            load_session,
            save_session,
            save_session_checked,
        )

        first = self._filled(entry_price=149.78, entry_ts=_lon(9, 5, 35))
        assert save_session(first) is True

        second = self._filled(entry_price=149.70, entry_ts=_lon(9, 10, 34))
        assert save_session_checked(second) == SAVE_REJECTED
        assert save_session(second) is True  # 異常ではないのでエラーにはしない

        after = load_session(SESSION_DATE)
        assert after is not None
        assert after.entry_price == pytest.approx(149.78), "後発の fill が entry を奪った"
        assert after.entry_ts == _lon(9, 5, 35)

    def test_armed_cannot_clear_confirmed_entry(self) -> None:
        """古い ARMED の save が確定済み entry を消さないこと。"""
        from backend.src.orb.session_store import (
            SAVE_REJECTED,
            load_session,
            save_session_checked,
        )

        assert (
            save_session_checked(self._filled(entry_price=149.78, entry_ts=_lon(9, 5, 35)))
            != SAVE_REJECTED
        )
        stale_armed = BrokerSession(
            session_date=SESSION_DATE, state="ARMED", levels=self._levels()
        )
        assert save_session_checked(stale_armed) == SAVE_REJECTED

        after = load_session(SESSION_DATE)
        assert after is not None and after.state == "FILLED"
        assert after.entry_price == pytest.approx(149.78)

    def test_put_condition_is_fixed(self) -> None:
        """DynamoDB 条件式を固定する（ローカル経路のテストだけでは式を守れない）。"""
        from backend.src.orb.session_store import build_put_condition

        no_entry = build_put_condition(None)
        assert no_entry["ConditionExpression"] == (
            "(attribute_not_exists(session_date) OR #st IN (:s0, :s1, :s2))"
            " AND (attribute_not_exists(#ets))"
        )
        assert no_entry["ExpressionAttributeNames"] == {"#st": "state", "#ets": "entry_ts"}
        assert ":ets" not in no_entry["ExpressionAttributeValues"]
        assert no_entry["ExpressionAttributeValues"] == {
            ":s0": {"S": "IDLE"},
            ":s1": {"S": "ARMED"},
            ":s2": {"S": "FILLED"},
        }

        with_entry = build_put_condition("2026-08-14T09:05:35+01:00")
        assert with_entry["ConditionExpression"] == (
            "(attribute_not_exists(session_date) OR #st IN (:s0, :s1, :s2))"
            " AND (attribute_not_exists(#ets) OR #ets = :ets)"
        )
        assert with_entry["ExpressionAttributeValues"][":ets"] == {
            "S": "2026-08-14T09:05:35+01:00"
        }

    def test_same_entry_progresses_to_terminal(self) -> None:
        """同じ entry を保つ前進（FILLED → CLOSED）は通ること（過剰防御でない）。"""
        from backend.src.orb.session_store import (
            SAVE_REJECTED,
            load_session,
            save_session_checked,
        )

        entry_ts = _lon(9, 5, 35)
        save_session_checked(self._filled(entry_price=149.78, entry_ts=entry_ts))
        closed = self._filled(
            entry_price=149.78,
            entry_ts=entry_ts,
            state="CLOSED",
            exit_price=149.50,
            exit_reason="eod",
        )
        assert save_session_checked(closed) != SAVE_REJECTED

        after = load_session(SESSION_DATE)
        assert after is not None
        assert after.state == "CLOSED" and after.exit_reason == "eod"


class TestOverlappingInvocations:
    """#304: 起動が重なり後発が **1 周期古い ARMED** を読んでも二重 fill にならないこと。

    `run_seconds`(300s) が起動間隔(300s)と同値で実行時間が 301.3s のため、本番では
    起動どうしが必ず重なる（2026-08-14 CloudWatch 実測）。ここでは古い読みを
    `load_session` の差し替えで再現する。
    """

    def _ticker(self, bid: float, ts) -> list[dict[str, Any]]:
        return [
            {
                "symbol": "USD_JPY",
                "bid": f"{bid:.3f}",
                "ask": f"{bid + 0.005:.3f}",
                "timestamp": ts.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "status": "OPEN",
            }
        ]

    def _clock(self, iterations: int):
        """`monotonic` を差し替え、ループ回数を `iterations` に固定する。"""
        state = {"n": -1}

        def monotonic() -> float:
            state["n"] += 1
            return float(state["n"])

        return monotonic, float(iterations) + 0.5

    def _run_once(self, *, bid: float, ts, iterations: int = 1) -> dict[str, Any]:
        monotonic, run_seconds = self._clock(iterations)
        return orb_poller.run_poller(
            client=FakeClient(ticker_rows=self._ticker(bid, ts)),
            now=ts,
            run_seconds=run_seconds,
            interval_sec=0.0,
            sleep=lambda _s: None,
            monotonic=monotonic,
        )

    def test_stale_armed_read_does_not_duplicate_fill(self, monkeypatch) -> None:
        import importlib

        from backend.src.orb.event_log import load_events

        store = importlib.import_module("backend.src.orb.session_store")

        orb_session_open.run_session_open(client=FakeClient(), now=_lon(9, 1))
        stale_armed = store.load_session(SESSION_DATE)
        assert stale_armed is not None and stale_armed.state == "ARMED"

        # 起動#1: sell_stop(149.79) 割れで SHORT 約定。遷移時に即永続化される。
        first = self._run_once(bid=149.78, ts=_lon(9, 5, 35))
        assert first["state"] == "FILLED"
        assert first["rejected_transitions"] == 0
        persisted = store.load_session(SESSION_DATE)
        assert persisted is not None and persisted.entry_price == pytest.approx(149.78)

        # 起動#2: 古い ARMED を掴む（修正前の実挙動）。初回の load だけ古い値を返す。
        real_load = store.load_session
        calls = {"n": 0}

        def fake_load(day: str):
            calls["n"] += 1
            return stale_armed if calls["n"] == 1 else real_load(day)

        monkeypatch.setattr(orb_poller, "load_session", fake_load)
        second = self._run_once(bid=149.70, ts=_lon(9, 10, 34))

        assert second["rejected_transitions"] == 1, "偽の fill が拒否されていない"
        assert second["events"] == 0, "拒否された遷移をイベントログに積んでいる"
        # 拒否後に権威ある状態を読み直して続行している
        assert second["state"] == "FILLED"
        assert calls["n"] >= 2

        after = store.load_session(SESSION_DATE)
        assert after is not None
        assert after.entry_price == pytest.approx(149.78), "偽 fill が entry を奪った"

        fills = [e for e in load_events(SESSION_DATE) if e["reason"] == "filled"]
        assert len(fills) == 1, f"filled が重複した: {fills}"
        assert fills[0]["detail"]["fill_price"] == pytest.approx(149.78)

    def test_save_failure_does_not_log_the_fill(self) -> None:
        """🔴 DynamoDB 書き込みが落ちたら fill を S3 に積まない（レビュー Should-fix 1）。

        積むと永続状態は `ARMED` のままなのに `filled` 行だけ残り、次起動が別価格で
        再 fill して初日の「`filled` が 2 行」がインフラ障害時に再現する。
        """
        import importlib

        from backend.src.orb.event_log import load_events
        from backend.src.orb.session_store import SAVE_FAILED

        store = importlib.import_module("backend.src.orb.session_store")
        orb_session_open.run_session_open(client=FakeClient(), now=_lon(9, 1))

        with patch.object(orb_poller, "save_session_checked", return_value=SAVE_FAILED):
            res = self._run_once(bid=149.78, ts=_lon(9, 5, 35))

        assert res["save_failures"] >= 1
        assert res["events"] == 0
        assert [e for e in load_events(SESSION_DATE) if e["reason"] == "filled"] == []
        # 永続状態も ARMED のまま＝ログと状態が食い違っていない
        after = store.load_session(SESSION_DATE)
        assert after is not None and after.state == "ARMED"

    def test_market_closed_does_not_save_every_poll(self) -> None:
        """`market_closed` は同状態イベント。毎ポール save + S3 RMW しない（レビュー Nit）。"""
        orb_session_open.run_session_open(client=FakeClient(), now=_lon(9, 1))

        rows = self._ticker(149.90, _lon(9, 5, 35))
        rows[0]["status"] = "CLOSE"
        monotonic, run_seconds = self._clock(3)
        with (
            patch.object(orb_poller, "save_session_checked") as spy_save,
            patch.object(orb_poller, "append_events", return_value=True) as spy_append,
        ):
            res = orb_poller.run_poller(
                client=FakeClient(ticker_rows=rows),
                now=_lon(9, 5, 35),
                run_seconds=run_seconds,
                interval_sec=0.0,
                sleep=lambda _s: None,
                monotonic=monotonic,
            )

        assert res["closed_observations"] == 3
        assert res["events"] == 3
        assert spy_save.call_count == 0, "非遷移イベントで save が走っている"
        assert spy_append.call_count == 1, "非遷移イベントが 1 件ずつ S3 に書かれている"

    def test_transition_is_persisted_before_run_ends(self) -> None:
        """遷移は**ループ終了を待たず**に永続化される（次起動が古い状態を読まない）。"""
        import importlib

        store = importlib.import_module("backend.src.orb.session_store")
        orb_session_open.run_session_open(client=FakeClient(), now=_lon(9, 1))

        seen: list[str] = []
        real_save = store.save_session_checked

        def spy(session, **kw):
            seen.append(session.state)
            return real_save(session, **kw)

        # ループを 3 回まわしても、FILLED の save は 1 回目の観測時点で起きる。
        with patch.object(orb_poller, "save_session_checked", side_effect=spy):
            res = self._run_once(bid=149.78, ts=_lon(9, 5, 35), iterations=3)

        assert res["state"] == "FILLED"
        assert seen[:1] == ["FILLED"], f"遷移時に save されていない: {seen}"
