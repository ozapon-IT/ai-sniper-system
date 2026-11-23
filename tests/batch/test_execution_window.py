"""tests/batch/test_execution_window.py — 約定取得テスト（E14, §6.2）。

🔴 このファイルが固定する契約（2026-08-05 のノート捏造事故・付録B
   docs/spikes/executions_api_spec.md）:

- 主取得は ``latestExecutions(symbol=, count=)``。``executions`` は ``orderId`` 専用。
- 決済約定（``settleType=CLOSE``・エントリーとは**別 orderId**）がノートに届くこと。
- 「取得できなかった」を「約定なし」に化けさせないこと（gaps / 再 raise）。
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.src.clients.gmo_client import MAX_LATEST_EXECUTIONS_COUNT
from backend.src.engine.trading_day import JST
from backend.src.llm.trade_note import (
    DailyCloseout,
    Execution,
    TradeNoteInput,
    TradePlan,
    execution_to_payload,
    fetch_execution_window,
    fetch_weekly_executions,
    summarize_pnl,
)

# 手仕舞いバッチ起動時刻（翌 4:30 JST cron 直後）
_CLOSEOUT_AFTER_ENTRY = datetime.datetime(2026, 6, 25, 4, 35, 0, tzinfo=JST)  # 木曜


# ---------------------------------------------------------------------------
# テスト用モッククライアント
# ---------------------------------------------------------------------------

def _make_exec_client(
    latest: list[dict[str, Any]] | None = None,
    by_order: dict[str, list[dict[str, Any]]] | None = None,
) -> Any:
    """公式シグネチャ（キーワード専用）に一致するモック。"""
    client = MagicMock()

    def get_latest_executions(
        *, symbol: str, count: int = MAX_LATEST_EXECUTIONS_COUNT
    ) -> list[dict[str, Any]]:
        return list(latest or [])

    def get_executions(*, order_id: str | int) -> list[dict[str, Any]]:
        return list((by_order or {}).get(str(order_id), []))

    client.get_latest_executions.side_effect = get_latest_executions
    client.get_executions.side_effect = get_executions
    return client


def _raw_exec(
    execution_id: str,
    order_id: str = "ord001",
    timestamp: str = "2026-06-24T11:30:00.000Z",  # = 6/24 20:30 JST
    settle_type: str = "OPEN",
    side: str = "BUY",
    price: str = "150.681",
    loss_gain: str = "0",
    fee: str = "0",
    settled_swap: str = "0",
) -> dict[str, Any]:
    return {
        "executionId": execution_id,
        "orderId": order_id,
        "positionId": "pos001",
        "symbol": "USD_JPY",
        "side": side,
        "settleType": settle_type,
        "price": price,
        "size": "100",
        "lossGain": loss_gain,
        "fee": fee,
        "settledSwap": settled_swap,
        "timestamp": timestamp,
    }


# ---------------------------------------------------------------------------
# 平日: 時間窓テスト
# ---------------------------------------------------------------------------

class TestFetchExecutionWindow:
    def test_uses_latest_executions_with_symbol_and_count(self) -> None:
        """主取得は latestExecutions(symbol=, count=)。

        旧実装は executions?symbol=&from=&to= を投げていたが、公式仕様上その
        パラメータは存在せず ERR-5106 になる（A/C の回帰固定）。
        """
        client = _make_exec_client(latest=[])

        fetch_execution_window(client, _CLOSEOUT_AFTER_ENTRY)

        kwargs = client.get_latest_executions.call_args.kwargs
        assert kwargs["symbol"] == "USD_JPY"
        assert kwargs["count"] == MAX_LATEST_EXECUTIONS_COUNT
        # executions は orderId 補完が無ければ一切呼ばれない
        client.get_executions.assert_not_called()

    def test_includes_prev_day_2030_entry(self) -> None:
        """前日 20:30 エントリーの約定が時間窓に含まれる（§6.2 受け入れ条件）。"""
        raw = _raw_exec("ex001", timestamp="2026-06-24T11:30:00.000Z")  # 20:30 JST
        client = _make_exec_client(latest=[raw])

        result = fetch_execution_window(client, _CLOSEOUT_AFTER_ENTRY)

        assert [e.execution_id for e in result.executions] == ["ex001"]

    def test_excludes_execution_before_window_start(self) -> None:
        """前営業日 20:00 JST より前の約定は窓外として除外される。"""
        early = _raw_exec("ex_early", timestamp="2026-06-24T10:00:00.000Z")  # 19:00 JST
        inside = _raw_exec("ex_in", timestamp="2026-06-24T11:30:00.000Z")
        client = _make_exec_client(latest=[early, inside])

        result = fetch_execution_window(client, _CLOSEOUT_AFTER_ENTRY)

        assert [e.execution_id for e in result.executions] == ["ex_in"]

    def test_close_side_execution_reaches_note(self) -> None:
        """決済約定（別 orderId・settleType=CLOSE）が取得できる（原因 D の回帰固定）。

        2026-08-05 はエントリー 1000201 に対し決済が 1000202 で発生し、
        DynamoDB のエントリー rootOrderId だけでは永久に到達できなかった。
        """
        entry = _raw_exec("ex_open", order_id="1000201", settle_type="OPEN",
                          price="150.681", timestamp="2026-06-24T11:30:00.000Z")
        close = _raw_exec("ex_close", order_id="1000202", settle_type="CLOSE",
                          side="SELL", price="150.781", loss_gain="10.0", fee="-0.63",
                          timestamp="2026-06-24T19:29:00.000Z")
        client = _make_exec_client(latest=[entry, close])

        result = fetch_execution_window(
            client, _CLOSEOUT_AFTER_ENTRY, order_ids=["1000201"]
        )

        by_id = {e.execution_id: e for e in result.executions}
        assert set(by_id) == {"ex_open", "ex_close"}
        assert by_id["ex_close"].settle_type == "CLOSE"
        assert by_id["ex_close"].loss_gain == "10.0"

    def test_primary_fetch_failure_raises(self) -> None:
        """主取得の失敗は呼び出し元へ再 raise される（不完全ノート防止 §6.2）。"""
        client = MagicMock()
        client.get_latest_executions.side_effect = RuntimeError("API timeout")

        with pytest.raises(RuntimeError):
            fetch_execution_window(client, _CLOSEOUT_AFTER_ENTRY)

    def test_order_id_supplement_deduplicates(self) -> None:
        """orderId 補完で取得した約定が主取得分と重複排除される。"""
        raw = _raw_exec("ex001", order_id="ord001")
        client = _make_exec_client(latest=[raw], by_order={"ord001": [raw]})

        result = fetch_execution_window(
            client, _CLOSEOUT_AFTER_ENTRY, order_ids=["ord001"]
        )

        ids = [e.execution_id for e in result.executions]
        assert ids.count("ex001") == 1

    def test_order_id_supplement_adds_missing_entry(self) -> None:
        """主取得の窓から漏れた約定が orderId 補完で拾われる。"""
        extra = _raw_exec("ex002", order_id="ord002")
        client = _make_exec_client(latest=[], by_order={"ord002": [extra]})

        result = fetch_execution_window(
            client, _CLOSEOUT_AFTER_ENTRY, order_ids=["ord002"]
        )

        assert [e.execution_id for e in result.executions] == ["ex002"]

    def test_order_id_supplement_failure_is_warning_only(self) -> None:
        """補完の失敗は主取得を巻き添えにせず gaps に記録される。"""
        client = _make_exec_client(latest=[_raw_exec("ex001")])
        client.get_executions.side_effect = RuntimeError("boom")

        result = fetch_execution_window(
            client, _CLOSEOUT_AFTER_ENTRY, order_ids=["ord999"]
        )

        assert [e.execution_id for e in result.executions] == ["ex001"]
        assert any("ord999" in g for g in result.gaps)

    def test_count_limit_reached_records_gap(self) -> None:
        """100 件上限に達したら「古い約定が見えていない」と gaps に残す。"""
        latest = [
            _raw_exec(f"ex{i:03d}") for i in range(MAX_LATEST_EXECUTIONS_COUNT)
        ]
        client = _make_exec_client(latest=latest)

        result = fetch_execution_window(client, _CLOSEOUT_AFTER_ENTRY)

        assert any(str(MAX_LATEST_EXECUTIONS_COUNT) in g for g in result.gaps)

    def test_window_exceeding_one_day_lookback_records_gap(self) -> None:
        """窓が latestExecutions の遡及範囲（直近1日分）を超えたら gaps に残す。

        月曜手仕舞い等で前営業日が金曜になるケース。API は 1 日分しか返さないため、
        「約定なし」ではなく「見えていない」と扱わせる。
        """
        now = datetime.datetime(2026, 6, 29, 4, 35, 0, tzinfo=JST)  # 月曜
        client = _make_exec_client(latest=[])

        result = fetch_execution_window(
            client, now, prev_trade_date=datetime.date(2026, 6, 26)  # 金曜
        )

        assert any("直近1日分" in g for g in result.gaps)

    def test_close_execution_after_now_jst_is_kept(self) -> None:
        """🔴 手仕舞い中に発生した CLOSE 約定を落とさない（窓に上限を付けない）。

        ``now_jst`` はバッチ先頭で固定され、そのあと run_cancel_first が数秒走る。
        Step3 の成行 closeOrder による CLOSE 約定は必ず ``now_jst`` より後に発生するため、
        上限フィルタがあるとその日の決済約定と実損益が丸ごと落ちる。決済は別 orderId
        なので orderId 補完でも救えない。
        """
        # now_jst = 木曜 4:35 JST（= 木曜 19:35 UTC 前日ではなく 木曜 04:35 JST）
        # CLOSE はその 12 秒後（cancel-first の sleep 分）に約定した想定
        close_after_now = _raw_exec(
            "ex_close_late",
            order_id="1000202",
            settle_type="CLOSE",
            side="SELL",
            price="150.781",
            loss_gain="10.0",
            fee="-0.63",
            timestamp="2026-06-24T19:35:12.000Z",  # = 6/25 04:35:12 JST（now_jst の 12 秒後）
        )
        client = _make_exec_client(latest=[close_after_now])

        result = fetch_execution_window(
            client, _CLOSEOUT_AFTER_ENTRY, order_ids=["1000201"]
        )

        assert [e.execution_id for e in result.executions] == ["ex_close_late"]
        assert summarize_pnl(result.executions).realized_total == Decimal("9.37")

    def test_unparsable_timestamp_is_kept_not_dropped(self) -> None:
        """時刻を解釈できない約定は窓外扱いで捨てない（税務証跡から実約定を落とさない）。"""
        broken = _raw_exec("ex_broken", timestamp="not-a-timestamp")
        client = _make_exec_client(latest=[broken])

        result = fetch_execution_window(client, _CLOSEOUT_AFTER_ENTRY)

        assert [e.execution_id for e in result.executions] == ["ex_broken"]


# ---------------------------------------------------------------------------
# 損益サマリ
# ---------------------------------------------------------------------------

class TestSummarizePnl:
    def test_realized_total_is_loss_gain_plus_fee_plus_swap(self) -> None:
        """実現損益 = lossGain + fee（負値）+ settledSwap。"""
        executions = [
            Execution(
                execution_id="ex_open", order_id="1", symbol="USD_JPY", side="BUY",
                price="150.681", size="100", timestamp="2026-06-24T11:30:00.000Z",
                settle_type="OPEN", loss_gain="0", fee="0", settled_swap="0",
            ),
            Execution(
                execution_id="ex_close", order_id="2", symbol="USD_JPY", side="SELL",
                price="150.781", size="100", timestamp="2026-06-24T19:29:00.000Z",
                settle_type="CLOSE", loss_gain="10.0", fee="-0.63", settled_swap="0",
            ),
        ]

        pnl = summarize_pnl(executions)

        assert pnl.open_count == 1
        assert pnl.close_count == 1
        assert str(pnl.realized_total) == "9.37"

    def test_unparsable_amount_is_excluded_and_reported(self) -> None:
        """解釈できない金額は 0 として黙って足さず、除外したことを記録する。"""
        executions = [
            Execution(
                execution_id="ex1", order_id="1", symbol="USD_JPY", side="SELL",
                price="1", size="100", timestamp="2026-06-24T19:29:00.000Z",
                settle_type="CLOSE", loss_gain="???", fee="-1",
            ),
        ]

        pnl = summarize_pnl(executions)

        assert pnl.unparsed_fields == ("lossGain@ex1",)
        assert str(pnl.realized_total) == "-1"

    def test_empty_executions_yield_zero(self) -> None:
        pnl = summarize_pnl([])
        assert pnl.realized_total == 0
        assert pnl.open_count == 0


# ---------------------------------------------------------------------------
# 土曜: 週次集約
# ---------------------------------------------------------------------------

class TestFetchWeeklyExecutions:
    def test_merges_live_latest_day_and_persisted_days(self) -> None:
        """最新取引日はライブ取得、それ以前は永続化ログから集約する。"""
        now = datetime.datetime(2026, 6, 27, 4, 35, 0, tzinfo=JST)  # 土曜
        client = _make_exec_client(latest=[
            _raw_exec("ex_fri", timestamp="2026-06-26T11:30:00.000Z")
        ])

        persisted = {
            datetime.date(2026, 6, 25): DailyCloseout([_make_exec("ex_thu")]),
            datetime.date(2026, 6, 24): DailyCloseout([_make_exec("ex_wed")]),
            datetime.date(2026, 6, 23): DailyCloseout([_make_exec("ex_tue")]),
            datetime.date(2026, 6, 22): DailyCloseout([_make_exec("ex_mon")]),
        }

        result = fetch_weekly_executions(
            client, now,
            prev_trade_date=datetime.date(2026, 6, 26),
            load_daily_closeout=persisted.get,
        )

        ids = {e.execution_id for e in result.executions}
        assert ids == {"ex_fri", "ex_thu", "ex_wed", "ex_tue", "ex_mon"}
        assert result.gaps == ()

    def test_never_calls_executions_with_date_param(self) -> None:
        """executions?symbol=&date= は実在しないパラメータなので投げない（原因 A の回帰固定）。"""
        now = datetime.datetime(2026, 6, 27, 4, 35, 0, tzinfo=JST)
        client = _make_exec_client(latest=[])

        fetch_weekly_executions(
            client, now,
            prev_trade_date=datetime.date(2026, 6, 26),
            load_daily_closeout=lambda d: DailyCloseout([]),
        )

        for call in client.get_executions.call_args_list:
            assert "date" not in call.kwargs
            assert "symbol" not in call.kwargs

    def test_missing_persisted_log_becomes_gap_not_silence(self) -> None:
        """日次ログが無い日は「約定なし」ではなく gaps に記録される。"""
        now = datetime.datetime(2026, 6, 27, 4, 35, 0, tzinfo=JST)
        client = _make_exec_client(latest=[])

        result = fetch_weekly_executions(
            client, now,
            prev_trade_date=datetime.date(2026, 6, 26),
            load_daily_closeout=lambda d: None,
        )

        assert len(result.gaps) == 4  # 金曜以外の 4 取引日
        assert all("確認できない" in g for g in result.gaps)

    def test_persisted_load_error_becomes_gap(self) -> None:
        """日次ログの読み込み失敗も gaps に記録され、他の日を巻き添えにしない。"""
        now = datetime.datetime(2026, 6, 27, 4, 35, 0, tzinfo=JST)
        client = _make_exec_client(latest=[])

        def loader(d: datetime.date) -> DailyCloseout | None:
            if d == datetime.date(2026, 6, 25):
                raise RuntimeError("S3 down")
            return DailyCloseout([_make_exec(f"ex_{d.isoformat()}")])

        result = fetch_weekly_executions(
            client, now,
            prev_trade_date=datetime.date(2026, 6, 26),
            load_daily_closeout=loader,
        )

        assert any("S3 down" in g for g in result.gaps)
        assert len(result.executions) == 3  # 6/24, 6/23, 6/22

    def test_deduplicates_same_execution_id(self) -> None:
        """同一 executionId が複数日分に含まれても 1 件に統合される（§6.2 受け入れ条件）。"""
        now = datetime.datetime(2026, 6, 27, 4, 35, 0, tzinfo=JST)
        client = _make_exec_client(latest=[])

        result = fetch_weekly_executions(
            client, now,
            prev_trade_date=datetime.date(2026, 6, 26),
            load_daily_closeout=lambda d: DailyCloseout([_make_exec("ex_dup")]),
        )

        ids = [e.execution_id for e in result.executions]
        assert ids.count("ex_dup") == 1

    def test_trade_dates_fewer_than_5_raises(self) -> None:
        """trade_dates が 5 件未満の場合は ValueError が raise される。"""
        now = datetime.datetime(2026, 6, 27, 4, 35, 0, tzinfo=JST)
        client = _make_exec_client(latest=[])
        only_4 = [
            datetime.date(2026, 6, 26),
            datetime.date(2026, 6, 25),
            datetime.date(2026, 6, 24),
            datetime.date(2026, 6, 23),
        ]

        with pytest.raises(ValueError):
            fetch_weekly_executions(client, now, trade_dates=only_4)

    def test_skips_weekend_days(self) -> None:
        """週末（土日）は 5 営業日カウントに含めない。"""
        now = datetime.datetime(2026, 6, 27, 4, 35, 0, tzinfo=JST)
        client = _make_exec_client(latest=[])
        asked: list[datetime.date] = []

        def loader(d: datetime.date) -> DailyCloseout | None:
            asked.append(d)
            return DailyCloseout([])

        fetch_weekly_executions(client, now, load_daily_closeout=loader)

        for d in asked:
            assert d.weekday() < 5, f"{d} is weekend"


# ---------------------------------------------------------------------------
# 永続化ラウンドトリップ（週次集約の入力になる）
# ---------------------------------------------------------------------------

def _make_exec(execution_id: str) -> Execution:
    return Execution(
        execution_id=execution_id,
        order_id="ord",
        symbol="USD_JPY",
        side="BUY",
        price="150.681",
        size="100",
        timestamp="2026-06-24T11:30:00.000Z",
        settle_type="OPEN",
    )


def test_execution_payload_roundtrip() -> None:
    """S3 へ書いた約定が損失なく復元できる（週次集約が依存する）。"""
    from backend.src.llm.trade_note import execution_from_payload

    original = Execution(
        execution_id="ex1", order_id="ord1", symbol="USD_JPY", side="SELL",
        price="150.781", size="100", timestamp="2026-06-24T19:29:00.000Z",
        settle_type="CLOSE", position_id="pos1", loss_gain="10.0", fee="-0.63",
        settled_swap="0", amount="15778.8",
    )

    assert execution_from_payload(execution_to_payload(original)) == original


def test_execution_from_payload_ignores_unknown_keys() -> None:
    """古い/新しいログに未知キーがあっても復元が壊れない。"""
    from backend.src.llm.trade_note import execution_from_payload

    ex = execution_from_payload({"execution_id": "ex1", "unknown_future_field": "x"})

    assert ex.execution_id == "ex1"


# ---------------------------------------------------------------------------
# #249: 計画値を構造化して渡し、LLM に単位換算・SL 推測をさせない
#
# 2026-08-06 のノートで発生した誤り:
#   「16.3 円 / 100 通貨 ≒ 0.163pips」 → 正しくは 16.3 pips（円をそのまま pips と誤記）
#   「150.53 → 150.23 = 30pips」      → S1(150.23) を SL(149.923) と誤認。正しくは 60.7pips
# ---------------------------------------------------------------------------

_DAY3_RATIONALE = (
    "欧州で東京高値を上抜け後に押し戻し→上ブレイク後の押し目LONG。"
    "Pivot P=150.593付近の押し目を150.53で拾い、S1=150.23の手前を意識した"
    "ワイドSLで保有。本日指標なしで乱高下リスク低く夜間保有に適した局面。"
)

_DAY3_PLAN = TradePlan(
    direction="LONG",
    order_type="LIMIT",
    entry_price="150.53",
    stop_loss="149.923",
    take_profit="151.259",
    lot="100",
    sl_profile="wide",
    rr_target="1.2",
)

_DAY3_EXECUTIONS = [
    Execution(
        execution_id="500001", order_id="1000301", symbol="USD_JPY", side="BUY",
        price="150.53", size="100", timestamp="2026-08-05T11:33:00.000Z",
        settle_type="OPEN", position_id="700001", loss_gain="0", fee="-0.316",
        settled_swap="0",
    ),
    Execution(
        execution_id="500002", order_id="1000302", symbol="USD_JPY", side="SELL",
        price="150.693", size="100", timestamp="2026-08-05T19:30:00.000Z",
        settle_type="CLOSE", position_id="700001", loss_gain="16.3", fee="-0.316",
        settled_swap="0",
    ),
]


def _day3_prompt(plan: TradePlan = _DAY3_PLAN) -> str:
    from backend.src.llm.trade_note import _build_note_prompt

    _, user = _build_note_prompt(
        TradeNoteInput(
            decision_plan=_DAY3_RATIONALE,
            executions=_DAY3_EXECUTIONS,
            now_jst=datetime.datetime(2026, 8, 6, 4, 30, tzinfo=JST),
            plans=(plan,),
        )
    )
    return user


class TestPlanValuesInPrompt:
    def test_actual_stop_loss_is_given_not_the_pivot_in_prose(self) -> None:
        """🔴 実際の SL が構造化データで渡ること（rationale 中の S1 と紛らわしくても）。"""
        prompt = _day3_prompt()

        assert "損切り (SL): 149.923" in prompt
        assert "利確 (TP): 151.259" in prompt
        # 文章中のピボットを SL と解釈しないよう明示している
        assert "SL/TP と解釈しないこと" in prompt

    def test_plan_pips_are_precomputed(self) -> None:
        """SL/TP 幅を Python が pips で算出して渡すこと（LLM に換算させない）。"""
        prompt = _day3_prompt()

        # |150.53 - 149.923| / 0.01 = 60.7 pips（ノートは 30pips と誤記していた）
        assert "60.7 pips" in prompt
        # |151.259 - 150.53| / 0.01 = 72.9 pips
        assert "72.9 pips" in prompt

    def test_executed_move_pips_is_precomputed(self) -> None:
        """約定値幅も pips で渡すこと（「16.3 円 ≒ 0.163pips」の誤記を防ぐ）。"""
        prompt = _day3_prompt()

        # (150.735 - 150.53) / 0.01 = 16.3 pips
        assert "約定値幅: **16.3 pips**" in prompt

    def test_unit_conversion_is_forbidden(self) -> None:
        """円 ↔ pips の自前換算を禁じる指示が入っていること。"""
        from backend.src.llm.trade_note import _build_note_prompt

        system, _ = _build_note_prompt(
            TradeNoteInput(
                decision_plan="p", executions=[],
                now_jst=datetime.datetime(2026, 8, 6, 4, 30, tzinfo=JST),
            )
        )
        assert "単位換算をしない" in system

    def test_missing_plan_says_do_not_guess(self) -> None:
        """計画値が無い場合は「推測して書くな」と明示すること（黙って空欄にしない）。"""
        prompt = _day3_prompt(plan=TradePlan())

        assert "SL/TP を文章から推測して書かないこと" in prompt

    def test_short_move_pips_sign_follows_direction(self) -> None:
        """売建は下落が利益なので pips の符号が反転すること。"""
        from backend.src.llm.trade_note import _executed_move_pips

        short = [
            Execution(
                execution_id="1", order_id="1", symbol="USD_JPY", side="SELL",
                price="150.693", size="100", timestamp="2026-08-05T11:00:00.000Z",
                settle_type="OPEN",
            ),
            Execution(
                execution_id="2", order_id="2", symbol="USD_JPY", side="BUY",
                price="150.53", size="100", timestamp="2026-08-05T19:00:00.000Z",
                settle_type="CLOSE",
            ),
        ]
        assert _executed_move_pips(short) == Decimal("16.3")

    def test_move_pips_skipped_when_ambiguous(self) -> None:
        """建玉が複数混在する場合は値幅を算出しない（曖昧な数値を渡さない）。"""
        from backend.src.llm.trade_note import _executed_move_pips

        assert _executed_move_pips(_DAY3_EXECUTIONS + _DAY3_EXECUTIONS[:1]) is None


class TestPlanScopeInWeeklyNote:
    """#251 / #250: friday モードは取引日ごとに計画値を並べ、他日へ流用させない。"""

    @staticmethod
    def _weekly_prompt(plans: tuple[TradePlan, ...]) -> str:
        from backend.src.llm.trade_note import _build_note_prompt

        _, user = _build_note_prompt(
            TradeNoteInput(
                decision_plan="p", executions=_DAY3_EXECUTIONS,
                now_jst=datetime.datetime(2026, 8, 8, 4, 30, tzinfo=JST),
                is_friday_mode=True, plans=plans,
            )
        )
        return user

    def test_each_trade_date_has_its_own_row(self) -> None:
        """取引日ごとに 1 行。日付は曜日つきで、他日へ当てはめない指示がある。

        🔴 SL 幅が**異なる 2 日**を使う。同じ幅だと「1 日分の値を使い回しても通る」
        テストになり、日ごとに算出していることを固定できない。
        """
        plans = (
            # 幅 60.7 pips
            TradePlan(trade_date="2026-08-06(木)", direction="LONG", order_type="LIMIT",
                      entry_price="150.84", stop_loss="150.233", take_profit="151.569",
                      status="ok"),
            # 幅 59.7 pips（意図的に別の値）
            TradePlan(trade_date="2026-08-04(火)", direction="LONG", order_type="LIMIT",
                      entry_price="150.681", stop_loss="150.084", take_profit="151.398",
                      status="ok"),
        )
        prompt = self._weekly_prompt(plans)

        rows = {
            line.split("|")[1].strip(): line
            for line in prompt.splitlines()
            if line.startswith("| 2026-")
        }
        assert set(rows) == {"2026-08-06(木)", "2026-08-04(火)"}
        # 各行に**その日の**幅が入っている（取り違え・使い回しの検出）
        assert "60.7 pips" in rows["2026-08-06(木)"]
        assert "72.9 pips" in rows["2026-08-06(木)"]
        assert "59.7 pips" in rows["2026-08-04(火)"]
        assert "71.7 pips" in rows["2026-08-04(火)"]
        assert "60.7 pips" not in rows["2026-08-04(火)"]
        assert "他の日に当てはめないこと" in prompt

    def test_no_entry_and_unavailable_are_distinguished(self) -> None:
        """🔴 「発注しなかった」と「判断ログを読めなかった」を混同しないこと。"""
        plans = (
            TradePlan(trade_date="2026-08-07(金)", status="no_entry"),
            TradePlan(trade_date="2026-08-03(月)", log_available=False),
        )
        prompt = self._weekly_prompt(plans)

        assert "NO_ENTRY（発注していない）" in prompt
        assert "判断ログを取得できず" in prompt
        assert "推測しないこと" in prompt

    def test_empty_plans_says_do_not_guess(self) -> None:
        prompt = self._weekly_prompt(())

        assert "SL/TP を文章から推測して書かないこと" in prompt


class TestJstWeekdayLabels:
    """#254: 曜日を渡す（LLM に日付から導出させない）。"""

    def test_execution_lines_carry_jst_weekday(self) -> None:
        prompt = _day3_prompt()

        # 2026-08-05T11:33:00Z = 2026-08-05(水) 20:33:00 JST
        assert "2026-08-05(水) 20:33:00 JST" in prompt
        # 2026-08-05T19:30:00Z = 2026-08-06(木) 04:30:00 JST
        assert "2026-08-06(木) 04:30:00 JST" in prompt
        # 監査用に元の UTC 文字列も残す
        assert "2026-08-05T11:33:00.000Z UTC" in prompt

    def test_now_line_carries_weekday(self) -> None:
        prompt = _day3_prompt()

        assert "日時(JST): 2026-08-06(木) 04:30" in prompt

    def test_weekday_derivation_is_forbidden(self) -> None:
        from backend.src.llm.trade_note import _build_note_prompt

        system, _ = _build_note_prompt(
            TradeNoteInput(
                decision_plan="p", executions=[],
                now_jst=datetime.datetime(2026, 8, 6, 4, 30, tzinfo=JST),
            )
        )
        assert "曜日は与えられたものだけを使う" in system

    def test_jst_date_label_matches_calendar(self) -> None:
        from backend.src.llm.trade_note import jst_date_label

        assert jst_date_label(datetime.date(2026, 8, 3)) == "2026-08-03(月)"
        assert jst_date_label(datetime.date(2026, 8, 7)) == "2026-08-07(金)"


class TestPositionSummaries:
    """#256 / #258: 建玉ごとの確定値（保有時間・値幅・損益）を Python が算出して渡す。

    誤りの履歴:
      - 保有 21:03→翌04:30 を「約3.5時間」（正しくは約7時間27分）
      - 建玉別の実現損益を CLOSE 側の手数料だけで計算し、部分の和が全体とずれた
    """

    @staticmethod
    def _pair(
        position_id: str, open_ts: str, close_ts: str,
        *, loss_gain: str = "0", fee_open: str = "0", fee_close: str = "0",
        open_price: str = "150.84", close_price: str = "151.34",
    ) -> list[Execution]:
        return [
            Execution(execution_id=f"{position_id}o", order_id="1", symbol="USD_JPY",
                      side="BUY", price=open_price, size="100", timestamp=open_ts,
                      settle_type="OPEN", position_id=position_id,
                      loss_gain="0", fee=fee_open, settled_swap="0"),
            Execution(execution_id=f"{position_id}c", order_id="2", symbol="USD_JPY",
                      side="SELL", price=close_price, size="100", timestamp=close_ts,
                      settle_type="CLOSE", position_id=position_id,
                      loss_gain=loss_gain, fee=fee_close, settled_swap="0"),
        ]

    def test_duration_matches_actual_elapsed_time(self) -> None:
        """🔴 実際に誤った Day 4 のケース: 21:03 → 翌 04:30:20 は約 7.5 時間。"""
        from backend.src.llm.trade_note import position_summaries

        s = position_summaries(
            self._pair("700301", "2026-08-06T12:03:00.000Z", "2026-08-06T19:30:20.000Z")
        )
        assert len(s) == 1
        assert str(s[0].holding) == "7:27:20"

    def test_realized_includes_both_side_fees(self) -> None:
        """🔴 実現損益に **OPEN 側の手数料**も含めること（部分の和がずれた原因）。"""
        from backend.src.llm.trade_note import position_summaries

        s = position_summaries(
            self._pair("A", "2026-08-04T11:30:00.000Z", "2026-08-04T19:30:00.000Z",
                       loss_gain="10.0", fee_open="-0.316", fee_close="-0.316")
        )[0]
        assert s.fee == Decimal("-0.632")
        assert s.realized == Decimal("9.368")

    def test_parts_sum_to_whole(self) -> None:
        """🔴 建玉別の和が全体（summarize_pnl）と一致すること。

        週次ノートが内部矛盾を起こさないための不変条件。
        """
        from backend.src.llm.trade_note import position_summaries

        execs = (
            self._pair("A", "2026-08-04T11:30:00.000Z", "2026-08-04T19:30:00.000Z",
                       loss_gain="10.0", fee_open="-0.316", fee_close="-0.316")
            + self._pair("B", "2026-08-05T11:30:00.000Z", "2026-08-05T19:30:00.000Z",
                         loss_gain="25.0", fee_open="-0.316", fee_close="-0.316")
            + self._pair("C", "2026-08-06T12:00:00.000Z", "2026-08-06T19:30:00.000Z",
                         loss_gain="50.0", fee_open="-0.316", fee_close="-0.317")
        )
        parts = sum((p.realized for p in position_summaries(execs)), Decimal(0))
        assert parts == summarize_pnl(execs).realized_total == Decimal("83.103")

    def test_each_position_gets_its_own_values(self) -> None:
        """建玉ごとに個別算出（1 件の値を使い回さない）。"""
        from backend.src.llm.trade_note import position_summaries

        execs = (
            self._pair("A", "2026-08-04T11:30:00.000Z", "2026-08-04T19:30:00.000Z",
                       loss_gain="10.0", fee_open="-0.316", fee_close="-0.316")
            + self._pair("B", "2026-08-06T12:03:00.000Z", "2026-08-06T19:30:20.000Z",
                         loss_gain="50.0", fee_open="-0.316", fee_close="-0.317")
        )
        by_id = {p.position_id: p for p in position_summaries(execs)}
        assert by_id["A"].realized == Decimal("9.368")
        assert by_id["B"].realized == Decimal("49.367")
        assert by_id["A"].holding != by_id["B"].holding

    def test_partial_position_is_skipped(self) -> None:
        """OPEN/CLOSE が 1 件ずつ揃わない建玉は算出しない（曖昧な値を渡さない）。"""
        from backend.src.llm.trade_note import position_summaries

        assert position_summaries(
            self._pair("A", "2026-08-04T11:30:00.000Z", "x")[:1]
        ) == []

    def test_close_before_open_is_skipped(self) -> None:
        """決済が新規より前という異常データは出さない。"""
        from backend.src.llm.trade_note import position_summaries

        assert position_summaries(
            self._pair("A", "2026-08-04T19:30:00.000Z", "2026-08-04T11:30:00.000Z")
        ) == []

    def test_unparsable_amount_skips_position(self) -> None:
        """金額を解釈できない建玉は出さない（誤った損益を渡さない）。"""
        from backend.src.llm.trade_note import position_summaries

        assert position_summaries(
            self._pair("A", "2026-08-04T11:30:00.000Z", "2026-08-04T19:30:00.000Z",
                       loss_gain="???")
        ) == []

    def test_prompt_carries_position_values_and_forbids_self_calculation(self) -> None:
        from backend.src.llm.trade_note import _build_note_prompt

        system, user = _build_note_prompt(
            TradeNoteInput(
                decision_plan="p",
                executions=self._pair(
                    "700301", "2026-08-06T12:03:00.000Z", "2026-08-06T19:30:20.000Z",
                    loss_gain="50.0", fee_open="-0.316", fee_close="-0.317",
                ),
                now_jst=datetime.datetime(2026, 8, 7, 4, 30, tzinfo=JST),
            )
        )
        assert "建玉ごとのサマリ" in user
        assert "保有 **7時間27分**" in user
        assert "実現損益 49.367 円" in user
        assert "時間差・保有期間を自分で計算しない" in system
        # 建玉別の値を合計から逆算させない（#259 レビュー指摘 1）
        assert "合計から内訳を逆算" in system

    def test_coverage_mismatch_is_flagged(self) -> None:
        """🔴 建玉別の和が全体と一致しないときは、その旨をプロンプトに明示する。

        黙って並べると「内訳の合計＝週間損益」と誤読される。
        """
        from backend.src.llm.trade_note import _build_note_prompt

        # 建玉に紐づかない CLOSE を混ぜる（サマリ対象外だが合計には入る）
        orphan = Execution(
            execution_id="x", order_id="9", symbol="USD_JPY", side="SELL",
            price="150.00", size="100", timestamp="2026-08-06T19:00:00.000Z",
            settle_type="CLOSE", position_id="", loss_gain="5.0", fee="0",
            settled_swap="0",
        )
        _, user = _build_note_prompt(
            TradeNoteInput(
                decision_plan="p",
                executions=self._pair(
                    "700301", "2026-08-06T12:03:00.000Z", "2026-08-06T19:30:20.000Z",
                    loss_gain="50.0", fee_open="-0.316", fee_close="-0.317",
                ) + [orphan],
                now_jst=datetime.datetime(2026, 8, 7, 4, 30, tzinfo=JST),
            )
        )
        assert "一致しない" in user
        assert "内訳の合計を週間損益として書かないこと" in user
