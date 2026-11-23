"""BT-3（sources / runner / report）のユニットテスト。

受け入れ条件（#153）:
  - RuleBasedSource で全期間 E2E がオフライン実行できる
  - OOS 期間が --unlock-oos なしで拒否される
  - 同一設定の再実行で experiments.jsonl の試行カウントが増える
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.src.engine.enrich import calculate_stop_loss, calculate_take_profit
from backend.src.llm.prompt import PromptContext
from backtest.data import KlinesStore
from backtest.engine import NIGHT_SESSION, SimConfig, TradeResult
from backtest.report import (
    MIN_SAMPLE_TRADES,
    append_experiment,
    compute_metrics,
    config_hash,
    render_report,
    save_run,
)
from backtest.runner import OOS_START, OosLockedError, RunConfig, RunOutcome, run_backtest
from backtest.sources import (
    MockSource,
    NightDayMomentumSource,
    NightPTrendSource,
    RuleBasedSource,
    uses_decision_sizing_params,
)

_JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture(autouse=True)
def _fixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PERCENT", "0.01")
    monkeypatch.setenv("MAX_LEVERAGE", "10")
    monkeypatch.setenv("MIN_RR_RATIO", "1.0")
    monkeypatch.setenv("MVP_MODE", "False")


def _ctx(current: float = 156.0, pivot: float = 155.5, atr: float = 0.4) -> PromptContext:
    return PromptContext(
        current_time_jst="08:00",
        day_of_week="水",
        symbol="USD_JPY",
        current_price_mid=current,
        pivot_p=pivot,
        pivot_r1=pivot + 0.5,
        pivot_r2=pivot + 1.0,
        pivot_s1=pivot - 0.5,
        pivot_s2=pivot - 1.0,
        atr_14d=atr,
        prev_day_change=0.001,
        equity_jpy=500_000.0,
    )


class TestRuleBasedSource:
    def test_above_pivot_goes_long_limit_at_pivot(self) -> None:
        out = RuleBasedSource().decide(date(2025, 7, 2), _ctx(current=156.0, pivot=155.5))
        d = out.decision
        assert (d.direction, d.order_type, d.entry_price) == ("LONG", "LIMIT", 155.5)
        # SL/TP は engine の session_vol が算出（decision には持たない・Ver 3.6）
        assert "take_profit" not in d.model_dump()
        assert d.atr_multiplier == 0.7
        assert out.llm_tokens is None  # 非 LLM は None（0 と偽らない）
        # 安全ロジック検証: enrich の SL/TP で価格序列（SL < Entry < TP）と R:R >= 1 が成立
        sl = calculate_stop_loss(
            entry_price=d.entry_price, atr=0.4, atr_multiplier=d.atr_multiplier, side="LONG"
        )
        tp = calculate_take_profit(
            entry_price=d.entry_price, stop_loss=sl, rr_target=d.rr_target, side="LONG"
        )
        assert sl < d.entry_price < tp
        assert (tp - d.entry_price) >= 1.0 * (d.entry_price - sl)

    def test_below_pivot_goes_short(self) -> None:
        out = RuleBasedSource().decide(date(2025, 7, 2), _ctx(current=155.0, pivot=155.5))
        d = out.decision
        assert (d.direction, d.entry_price) == ("SHORT", 155.5)
        assert "take_profit" not in d.model_dump()
        # 安全ロジック検証: SHORT の価格序列（TP < Entry < SL）と R:R >= 1
        sl = calculate_stop_loss(
            entry_price=d.entry_price, atr=0.4, atr_multiplier=d.atr_multiplier, side="SHORT"
        )
        tp = calculate_take_profit(
            entry_price=d.entry_price, stop_loss=sl, rr_target=d.rr_target, side="SHORT"
        )
        assert tp < d.entry_price < sl
        assert (d.entry_price - tp) >= 1.0 * (sl - d.entry_price)

    def test_zero_atr_is_no_entry(self) -> None:
        """ATR=0（14本フラット）は SL=entry になり得るため NO_ENTRY に倒す。"""
        out = RuleBasedSource().decide(date(2025, 7, 2), _ctx(atr=0.0))
        assert out.decision.direction == "NO_ENTRY"
        assert "ATR" in out.decision.rationale

    def test_at_pivot_no_entry(self) -> None:
        out = RuleBasedSource().decide(date(2025, 7, 2), _ctx(current=155.5, pivot=155.5))
        assert out.decision.direction == "NO_ENTRY"

    def test_deterministic(self) -> None:
        ctx = _ctx()
        a = RuleBasedSource().decide(date(2025, 7, 2), ctx)
        b = RuleBasedSource().decide(date(2025, 7, 2), ctx)
        assert a.decision == b.decision


class TestMockSource:
    def test_wraps_mock_llm_with_token_meta(self) -> None:
        out = MockSource().decide(date(2025, 7, 2), _ctx())
        assert out.decision.direction == "LONG"  # mock_llm は S1 押し目買い固定
        assert out.llm_tokens is not None and out.llm_tokens > 0
        assert out.model is not None


class TestDecisionSizingParamSource:
    """SL/TP 倍率ソースの自動判定（#187）。"""

    @pytest.mark.parametrize(
        ("source_name", "expected"),
        [
            ("rule_based", False),
            ("night_p_trend", False),
            ("night_day_momentum", False),
            ("mock", True),
            ("subagent", True),
            ("decision_log_replay", True),
        ],
    )
    def test_uses_decision_sizing_params(self, source_name: str, expected: bool) -> None:
        assert uses_decision_sizing_params(source_name) is expected


class TestNightBaselines:
    """夜型ベースライン（RD-2 #167）。エントリーは現値±0.05×ATR の押し目/戻り LIMIT。"""

    def test_p_trend_above_pivot_long_pullback(self) -> None:
        out = NightPTrendSource().decide(
            date(2025, 7, 2), _ctx(current=156.0, pivot=155.5, atr=0.4)
        )
        d = out.decision
        assert (d.direction, d.order_type) == ("LONG", "LIMIT")
        assert d.entry_price == pytest.approx(156.0 - 0.05 * 0.4)  # 現値の下に押し目
        assert out.llm_tokens is None

    def test_p_trend_below_pivot_short_rally(self) -> None:
        out = NightPTrendSource().decide(
            date(2025, 7, 2), _ctx(current=155.0, pivot=155.5, atr=0.4)
        )
        d = out.decision
        assert d.direction == "SHORT"
        assert d.entry_price == pytest.approx(155.0 + 0.05 * 0.4)  # 現値の上に戻り

    def test_p_trend_records_pivot_p(self) -> None:
        """P 回帰型は判断根拠 P を監査値に記録する（§5.2）。"""
        out = NightPTrendSource().decide(date(2025, 7, 2), _ctx(current=156.0, pivot=155.5))
        assert out.decision.referenced_pivot_level == "P"

    def test_p_trend_at_pivot_no_entry(self) -> None:
        out = NightPTrendSource().decide(date(2025, 7, 2), _ctx(current=155.5, pivot=155.5))
        assert out.decision.direction == "NO_ENTRY"

    def test_p_trend_zero_atr_no_entry(self) -> None:
        out = NightPTrendSource().decide(date(2025, 7, 2), _ctx(atr=0.0))
        assert out.decision.direction == "NO_ENTRY"

    def test_day_momentum_up_day_long(self) -> None:
        ctx = _ctx(current=156.0, atr=0.4)
        ctx = replace(ctx, session_open=155.0)  # 当日始値 < 現値 → 陽線
        out = NightDayMomentumSource().decide(date(2025, 7, 2), ctx)
        assert out.decision.direction == "LONG"
        assert out.decision.entry_price == pytest.approx(156.0 - 0.05 * 0.4)
        # 判断根拠は当日始値であり Pivot P ではない → 監査値は NONE（CodeRabbit 指摘）
        assert out.decision.referenced_pivot_level == "NONE"

    def test_day_momentum_down_day_short(self) -> None:
        ctx = replace(_ctx(current=154.0, atr=0.4), session_open=155.0)  # 陰線
        out = NightDayMomentumSource().decide(date(2025, 7, 2), ctx)
        assert out.decision.direction == "SHORT"

    def test_day_momentum_missing_session_open_degrades(self) -> None:
        """当日始値なし（session_open=None）は NO_ENTRY に degrade。"""
        out = NightDayMomentumSource().decide(date(2025, 7, 2), _ctx())  # session_open 既定 None
        assert out.decision.direction == "NO_ENTRY"
        assert "当日セッション始値なし" in out.decision.rationale

    def test_night_baselines_pass_session_vol_pre_order(self) -> None:
        """夜型判断 + session_vol で pre_order を通過する（R:R construction 担保・reject ゼロ）。"""
        from backtest.engine import SimConfig, simulate_day

        ctx = replace(_ctx(current=156.0, pivot=155.5, atr=0.4), session_open=155.0)
        for source in (NightPTrendSource(), NightDayMomentumSource()):
            out = source.decide(date(2025, 7, 2), ctx)
            result = simulate_day(
                day=date(2025, 7, 2),
                decision=out.decision,
                atr=ctx.atr_14d,
                equity_jpy=500_000.0,
                current_price=ctx.current_price_mid,
                cutoff_ms=0,
                bars=None,  # no_data だが pre_order は通過済みのはず
                config=SimConfig(),
            )
            assert result.status != "rejected", f"{source.name} が reject された: {result.detail}"


class TestMetrics:
    def _result(self, status: str, pnl: float | None = None, r: float | None = None) -> TradeResult:
        return TradeResult(day=date(2025, 7, 2), status=status, pnl_pips=pnl, r_multiple=r)  # type: ignore[arg-type]

    def test_compute_metrics(self) -> None:
        results = [
            self._result("tp", 100.0, 100 / 60),
            self._result("sl", -60.0, -1.0),
            self._result("closeout", 40.0, 40 / 60),
            self._result("no_entry"),
            self._result("rejected"),
            self._result("no_fill"),
        ]
        m = compute_metrics(results)
        assert m.days == 6
        assert m.trades == 3
        assert (m.wins, m.losses) == (2, 1)
        assert m.win_rate == pytest.approx(2 / 3)
        assert m.profit_factor == pytest.approx(140 / 60)
        assert m.total_pnl_pips == pytest.approx(80.0)
        # 累積 R: 1.667 → 0.667 → 1.333。ピーク 1.667 → 最大 DD = 1.0
        assert m.max_drawdown_r == pytest.approx(1.0)
        assert m.low_sample is True  # 3 < 30
        assert m.status_counts["no_entry"] == 1

    def test_empty_results(self) -> None:
        m = compute_metrics([])
        assert m.trades == 0
        assert m.win_rate == 0.0
        assert m.low_sample is True


def _outcome(
    start: date = date(2024, 1, 1),
    end: date = date(2024, 1, 31),
    is_oos: bool = False,
) -> RunOutcome:
    return RunOutcome(
        source_name="rule_based",
        config=RunConfig(start=start, end=end, unlock_oos=is_oos),
        results=[],
        skipped_days=[],
        is_oos=is_oos,
    )


class TestExperimentRegistry:
    def test_trial_count_increments_for_same_window(self, tmp_path: Path) -> None:
        registry = tmp_path / "experiments.jsonl"
        m = compute_metrics([])
        s1 = append_experiment(registry, _outcome(), "run-1", m)
        s2 = append_experiment(registry, _outcome(), "run-2", m)
        assert (s1.window_trials, s2.window_trials) == (1, 2)
        # 別ウィンドウはカウント独立
        s3 = append_experiment(registry, _outcome(end=date(2024, 2, 29)), "run-3", m)
        assert s3.window_trials == 1

    def test_oos_runs_counted(self, tmp_path: Path) -> None:
        registry = tmp_path / "experiments.jsonl"
        m = compute_metrics([])
        append_experiment(registry, _outcome(), "run-1", m)
        s = append_experiment(
            registry,
            _outcome(start=date(2026, 1, 5), end=date(2026, 1, 30), is_oos=True),
            "run-2",
            m,
        )
        assert s.oos_runs == 1

    def test_corrupt_line_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        """壊れた 1 行が以後の全実験記録をブロックしない（台帳を単一障害点にしない）。"""
        registry = tmp_path / "experiments.jsonl"
        m = compute_metrics([])
        append_experiment(registry, _outcome(), "run-1", m)
        with registry.open("a", encoding="utf-8") as f:
            f.write('{"broken": \n')  # クラッシュ時の部分書き込みを模擬
        s = append_experiment(registry, _outcome(), "run-2", m)
        assert s.window_trials == 2  # 壊れ行はスキップ・正常行はカウント

    def test_llm_tokens_recorded_in_registry(self, tmp_path: Path) -> None:
        """LLM 実行時は実トークン合計、非 LLM は null が台帳に残る（§5.2）。"""
        import json as _json

        registry = tmp_path / "experiments.jsonl"
        m = compute_metrics([])
        outcome = RunOutcome(
            source_name="mock",
            config=RunConfig(start=date(2024, 1, 1), end=date(2024, 1, 31)),
            results=[],
            skipped_days=[],
            is_oos=False,
            total_llm_tokens=736,
        )
        append_experiment(registry, outcome, "run-llm", m)
        append_experiment(registry, _outcome(), "run-rule", m)
        lines = [_json.loads(x) for x in registry.read_text().splitlines()]
        assert lines[0]["llm_tokens"] == 736
        assert lines[1]["llm_tokens"] is None

    def test_config_hash_changes_with_window(self) -> None:
        h1 = config_hash(_outcome())
        h2 = config_hash(_outcome(end=date(2024, 2, 29)))
        assert h1 != h2

    def test_config_hash_changes_with_env_and_prompt(self) -> None:
        """本番モジュールが env から読む値・判断プロンプトが違えば別設定（§7-6）。"""
        base = _outcome()
        assert config_hash(replace(base, env={"RISK_PERCENT": "0.02"})) != config_hash(
            replace(base, env={"RISK_PERCENT": "0.01"})
        )
        assert config_hash(replace(base, prompt_sha="aaa")) != config_hash(
            replace(base, prompt_sha="bbb")
        )

    def test_run_records_env_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from backtest.runner import RESULT_AFFECTING_ENV

        monkeypatch.setenv("MAX_DECISION_SPREAD_PIPS", "2.5")
        outcome = run_backtest(
            KlinesStore(cache_dir=Path("/nonexistent")),
            RuleBasedSource(),
            RunConfig(start=date(2024, 1, 1), end=date(2024, 1, 2)),
        )
        assert set(outcome.env) == set(RESULT_AFFECTING_ENV)
        assert outcome.env["MAX_DECISION_SPREAD_PIPS"] == "2.5"
        assert outcome.env["RISK_PERCENT"] == "0.01"  # autouse fixture

    def test_config_hash_changes_with_session_window(self) -> None:
        """セッション窓（朝型/夜型）が違う run を同一設定として扱わない（RD-1 #166）。"""
        from backtest.engine import MORNING_SESSION, NIGHT_SESSION

        morning = RunOutcome(
            source_name="rule_based",
            config=RunConfig(
                start=date(2024, 1, 1), end=date(2024, 1, 31), session=MORNING_SESSION
            ),
            results=[],
            skipped_days=[],
            is_oos=False,
        )
        night = RunOutcome(
            source_name="rule_based",
            config=RunConfig(start=date(2024, 1, 1), end=date(2024, 1, 31), session=NIGHT_SESSION),
            results=[],
            skipped_days=[],
            is_oos=False,
        )
        assert config_hash(morning) != config_hash(night)

    def test_config_hash_changes_with_sizing_param_source(self) -> None:
        """倍率ソース（frozen / decision）が違う run を同一設定として扱わない（#187）。"""
        frozen = _outcome()
        decision = RunOutcome(
            source_name="mock",
            config=RunConfig(
                start=date(2024, 1, 1),
                end=date(2024, 1, 31),
                sim=SimConfig(use_decision_sizing_params=True),
            ),
            results=[],
            skipped_days=[],
            is_oos=False,
        )
        assert config_hash(frozen) != config_hash(decision)

    def test_session_recorded_in_registry(self, tmp_path: Path) -> None:
        """レジストリの各エントリにセッション窓が記録される（RD-1 受け入れ条件）。"""
        import json as _json

        from backtest.engine import MORNING_SESSION

        registry = tmp_path / "experiments.jsonl"
        m = compute_metrics([])
        morning = RunOutcome(
            source_name="rule_based",
            config=RunConfig(
                start=date(2024, 1, 1), end=date(2024, 1, 31), session=MORNING_SESSION
            ),
            results=[],
            skipped_days=[],
            is_oos=False,
        )
        night = RunOutcome(
            source_name="rule_based",
            config=RunConfig(start=date(2024, 1, 1), end=date(2024, 1, 31), session=NIGHT_SESSION),
            results=[],
            skipped_days=[],
            is_oos=False,
        )
        append_experiment(registry, morning, "run-morning", m)
        append_experiment(registry, night, "run-night", m)
        lines = [_json.loads(x) for x in registry.read_text().splitlines()]
        assert lines[0]["session"]["decision_time"] == "08:00"
        assert lines[1]["session"] == {
            "decision_time": "20:30",
            "closeout_time": "04:30",
            "friday_closeout_time": "04:30",
        }


class TestOosSeal:
    def test_oos_window_refused_without_unlock(self, tmp_path: Path) -> None:
        store = KlinesStore(cache_dir=tmp_path)
        config = RunConfig(start=OOS_START, end=OOS_START + timedelta(days=4))
        with pytest.raises(OosLockedError, match="unlock-oos"):
            run_backtest(store, RuleBasedSource(), config)

    def test_oos_window_runs_with_unlock(self, tmp_path: Path) -> None:
        store = KlinesStore(cache_dir=tmp_path)  # キャッシュ空 → 全日スキップだが拒否されない
        config = RunConfig(start=OOS_START, end=OOS_START + timedelta(days=4), unlock_oos=True)
        outcome = run_backtest(store, RuleBasedSource(), config)
        assert outcome.is_oos is True
        assert outcome.results == []

    def test_in_sample_window_runs_without_unlock(self, tmp_path: Path) -> None:
        store = KlinesStore(cache_dir=tmp_path)
        outcome = run_backtest(
            store, RuleBasedSource(), RunConfig(start=date(2024, 1, 8), end=date(2024, 1, 12))
        )
        assert outcome.is_oos is False


# ---------------------------------------------------------------------------
# E2E（オフライン・合成キャッシュ）
# ---------------------------------------------------------------------------


def _jst_ms(d: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(d, time(hour, minute), tzinfo=_JST).timestamp() * 1_000)


def _candle(t_ms: int, o: float, h: float, lo: float, c: float) -> dict[str, str]:
    return {
        "openTime": str(t_ms),
        "open": f"{o:.3f}",
        "high": f"{h:.3f}",
        "low": f"{lo:.3f}",
        "close": f"{c:.3f}",
    }


def _populate_cache(store: KlinesStore, days: list[date]) -> None:
    """指定営業日群の E2E に必要な年次・日次キャッシュを合成する。"""
    # 日足: 最終日+その前 30 平日（各日 06:00 JST 始まり・ゆるやかな上昇トレンド）
    daily_days: list[date] = []
    d = days[-1]
    while len(daily_days) < 30:
        if d.weekday() < 5:
            daily_days.append(d)
        d -= timedelta(days=1)
    daily_days.reverse()
    # 年跨ぎでも正しい年ファイルに入るよう、足の実日付でグルーピングして保存する
    daily_by_year: dict[int, list[dict[str, str]]] = {}
    for i, dd in enumerate(daily_days):
        daily_by_year.setdefault(dd.year, []).append(
            _candle(
                _jst_ms(dd, 6),
                155.0 + i * 0.02,
                155.8 + i * 0.02,
                154.6 + i * 0.02,
                155.5 + i * 0.02,
            )
        )
    for y, candles in daily_by_year.items():
        store.save("BID", "1day", str(y), candles)
    # 4 時間足: 期間全体をカバー（JST 01/05/.. grid の代わりに 05:00 起点から遡る近似で十分）
    h4_by_year: dict[int, dict[int, dict[str, str]]] = {}
    for dd in days:
        start = _jst_ms(dd, 5)
        for i in range(9):
            t = start - i * 4 * 3_600 * 1_000
            bar_year = datetime.fromtimestamp(t / 1_000, tz=_JST).year
            h4_by_year.setdefault(bar_year, {})[t] = _candle(t, 155.5, 155.7, 155.3, 155.6)
    for y, h4 in h4_by_year.items():
        store.save("BID", "4hour", str(y), [h4[t] for t in sorted(h4)])
    # 5 分足: 各営業日 06:00〜23:55（現値がピボットの少し上 → rule_based は LONG LIMIT at P）。
    # close は session_open（06:00 始値 155.6）から RD-8 G3 閾値（0.05×ATR）を超える動きにする。
    for dd in days:
        bid = []
        t = _jst_ms(dd, 6)
        end = _jst_ms(dd, 23, 55)
        while t <= end:
            bid.append(_candle(t, 155.6, 155.75, 155.45, 155.67))
            t += 5 * 60 * 1_000
        store.save("BID", "5min", dd.strftime("%Y%m%d"), bid)
        ask = [
            {
                **c,
                "open": f"{float(c['open']) + 0.004:.3f}",
                "high": f"{float(c['high']) + 0.004:.3f}",
                "low": f"{float(c['low']) + 0.004:.3f}",
                "close": f"{float(c['close']) + 0.004:.3f}",
            }
            for c in bid
        ]
        store.save("ASK", "5min", dd.strftime("%Y%m%d"), ask)


def _extend_5min_to_full_day(store: KlinesStore, days: list[date]) -> None:
    """5 分足を実キャッシュと同じ全日レンジ（06:00 → 翌 05:55）に張り替える。

    _populate_cache は 23:55 までしか置かないため、夜窓（20:30 → 翌 4:30）の
    日跨ぎ経路を通すテストではこちらで上書きする。
    """
    for dd in days:
        bid = []
        t = _jst_ms(dd, 6)
        end = _jst_ms(dd + timedelta(days=1), 5, 55)
        while t <= end:
            bid.append(_candle(t, 155.6, 155.75, 155.45, 155.67))
            t += 5 * 60 * 1_000
        store.save("BID", "5min", dd.strftime("%Y%m%d"), bid)
        ask = [
            {
                **c,
                "open": f"{float(c['open']) + 0.004:.3f}",
                "high": f"{float(c['high']) + 0.004:.3f}",
                "low": f"{float(c['low']) + 0.004:.3f}",
                "close": f"{float(c['close']) + 0.004:.3f}",
            }
            for c in bid
        ]
        store.save("ASK", "5min", dd.strftime("%Y%m%d"), ask)


class TestEndToEnd:
    def test_rule_based_e2e_offline(self, tmp_path: Path) -> None:
        """受け入れ条件: RuleBasedSource の E2E がキャッシュのみで完走する（週末スキップ込み）。"""
        days = [date(2025, 7, 3), date(2025, 7, 4), date(2025, 7, 7)]  # 木・金・月（週末跨ぎ）
        store = KlinesStore(cache_dir=tmp_path / "cache")
        _populate_cache(store, days)

        config = RunConfig(start=days[0], end=days[-1])
        outcome = run_backtest(store, RuleBasedSource(), config)

        assert len(outcome.results) == 3  # 土日はスキップ・営業日 3 日ぶん
        valid = {"no_entry", "lot_zero", "rejected", "no_data", "no_fill", "tp", "sl", "closeout"}
        assert {r.status for r in outcome.results} <= valid
        # 判断は全日 actionable（現値 > P）→ 非 no_entry
        assert all(r.status != "no_entry" for r in outcome.results)

        # レポート一式の保存と 3 列比較・免責ヘッダ
        metrics = compute_metrics(outcome.results)
        registry = append_experiment(tmp_path / "experiments.jsonl", outcome, "run-e2e", metrics)
        run_dir = save_run(tmp_path / "runs", "run-e2e", outcome, metrics, metrics, registry)
        report_md = (run_dir / "report.md").read_text(encoding="utf-8")
        assert "NO_TRADE | rule_based |" in report_md
        assert "免責" in report_md
        assert str(MIN_SAMPLE_TRADES) in report_md  # 3 トレード < 30 → 参考値フラグ
        assert (run_dir / "report.json").exists()
        assert (run_dir / "trades.jsonl").exists()
        # アトミック書き込みの一時ファイルが残っていない・llm_tokens が対称に記録される
        import json as _json

        assert sorted(f.name for f in run_dir.iterdir()) == [
            "report.json",
            "report.md",
            "trades.jsonl",
        ]
        report_json = _json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        assert report_json["llm_tokens"] is None  # rule_based は非 LLM
        assert report_json["session"]["decision_time"] == "20:30"  # 既定 = 夜型（本番同型）

    def test_night_session_e2e_offline(self, tmp_path: Path) -> None:
        """夜型窓（20:30 判断 → 翌 4:30 closeout）の E2E（RD-1 受け入れ条件）。

        decision_time が build_day_context へ貫通し、判断時刻前の fill が
        存在しないこと・exit が closeout 時刻を超えないことを検証する。
        """
        from backtest.engine import closeout_ms_for

        days = [date(2025, 7, 3), date(2025, 7, 4), date(2025, 7, 7)]  # 木・金・月（週末跨ぎ）
        store = KlinesStore(cache_dir=tmp_path / "cache")
        _populate_cache(store, days)
        _extend_5min_to_full_day(store, days)

        config = RunConfig(start=days[0], end=days[-1], session=NIGHT_SESSION)
        outcome = run_backtest(store, RuleBasedSource(), config)

        assert len(outcome.results) == 3
        valid = {"no_entry", "lot_zero", "rejected", "no_data", "no_fill", "tp", "sl", "closeout"}
        assert {r.status for r in outcome.results} <= valid
        for r in outcome.results:
            cutoff = _jst_ms(r.day, 20, 30)
            closeout = closeout_ms_for(r.day, NIGHT_SESSION)
            if r.filled_at_ms is not None:
                assert r.filled_at_ms >= cutoff  # 判断時刻前に注文は存在しない
            if r.exit_at_ms is not None:
                assert r.exit_at_ms <= closeout  # 金曜含め翌 4:30 を超えて保有しない


class TestSaveRunCollision:
    def test_existing_run_dir_is_not_overwritten(self, tmp_path: Path) -> None:
        """同一 run_id での再実行が監査成果物を静かに上書きしない。"""
        outcome = _outcome()
        m = compute_metrics([])
        registry = append_experiment(tmp_path / "e.jsonl", outcome, "run-dup", m)
        save_run(tmp_path / "runs", "run-dup", outcome, m, m, registry)
        with pytest.raises(FileExistsError, match="run_id 衝突"):
            save_run(tmp_path / "runs", "run-dup", outcome, m, m, registry)


class TestRenderReport:
    def test_report_shows_discipline_meta(self, tmp_path: Path) -> None:
        outcome = _outcome()
        m = compute_metrics([])
        registry = append_experiment(tmp_path / "e.jsonl", outcome, "run-x", m)
        md = render_report("run-x", outcome, m, m, registry)
        assert "同一ウィンドウへの通算試行回数: **1 回**" in md
        assert "OOS 実行の通算回数: **0 回**" in md
