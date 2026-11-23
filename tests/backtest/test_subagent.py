"""backtest/subagent.py（BT-4）のユニットテスト。

受け入れ条件（#154）:
  - decisions キャッシュにより中断→再開が可能（未処理分のみ列挙）
  - スキーマ違反 JSON が no_entry に倒れる（stop_loss を含む出力の拒否・ガードレール #5）
  - レポートに評価対象モデル・chunk_size・ブラインドモード有無が明記される
  - 学習知識バイアスの免責ヘッダがレポートに自動挿入される
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from backend.src.llm.client import DEFAULT_DECISION_MODEL
from backend.src.llm.prompt import build_user_prompt
from backtest.context import build_day_context
from backtest.data import KlinesStore
from backtest.engine import NIGHT_SESSION
from backtest.report import append_experiment, compute_metrics, save_run
from backtest.runner import OosLockedError, RunConfig, run_backtest
from backtest.subagent import (
    BACKTEST_DECIDER_MODEL,
    DECISIONS_DIR,
    REQUESTS_DIR,
    PrepareResult,
    SubagentFileSource,
    check_run,
    load_manifest,
    parse_decision_payload,
    prepare_run,
)
from tests.backtest.test_baseline_report import _candle, _jst_ms, _populate_cache

_ROOT = Path(__file__).resolve().parent.parent.parent

# 木・金・月（週末跨ぎ・2025 = In-Sample）
_DAYS = [date(2025, 7, 3), date(2025, 7, 4), date(2025, 7, 7)]

_VALID_DECISION = {
    "rationale": "テスト用の妥当な判断",
    "direction": "LONG",
    "referenced_pivot_level": "P",
    "order_type": "LIMIT",
    "entry_price": 155.5,
    "sl_profile": "wide",
    "execution_required": True,
}


@pytest.fixture(autouse=True)
def _fixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PERCENT", "0.01")
    monkeypatch.setenv("MAX_LEVERAGE", "10")
    monkeypatch.setenv("MIN_RR_RATIO", "1.0")
    monkeypatch.setenv("MVP_MODE", "False")


def _prepared(tmp_path: Path, blind: bool = True) -> tuple[KlinesStore, PrepareResult]:
    store = KlinesStore(cache_dir=tmp_path / "cache")
    _populate_cache(store, _DAYS)
    result = prepare_run(
        store, tmp_path / "runs", start=_DAYS[0], end=_DAYS[-1], blind=blind, run_id="run-t"
    )
    return store, result


def _write_decision(run_dir: Path, name: str, payload: dict[str, object] | str) -> Path:
    path = run_dir / DECISIONS_DIR / f"{name}.json"
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return path


class TestPrepare:
    def test_blind_mode_uses_sequential_names_and_manifest_maps_dates(self, tmp_path: Path) -> None:
        _, result = _prepared(tmp_path)
        assert result.prepared == 3
        names = sorted(p.stem for p in (result.run_dir / REQUESTS_DIR).glob("*.json"))
        assert names == ["0001", "0002", "0003"]
        manifest = load_manifest(result.run_dir)
        assert [e["date"] for e in manifest["entries"]] == [d.isoformat() for d in _DAYS]

    def test_blind_request_payload_contains_no_absolute_date(self, tmp_path: Path) -> None:
        """ブラインド担保: リクエスト本文に ISO 日付・和暦年が現れない（§6.3）。"""
        _, result = _prepared(tmp_path)
        for path in (result.run_dir / REQUESTS_DIR).glob("*.json"):
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"\d{4}-\d{2}-\d{2}", text), path.name
            assert not re.search(r"20\d{2}年", text), path.name

    def test_blind_request_omits_decision_month_day(self, tmp_path: Path) -> None:
        """#376: バックテストは decision_date_jst を空のままにする。

        判断バッチの現在時刻行は ``M/D HH:MM (曜)``。バックテストは時刻と曜日のみ。
        """
        _, result = _prepared(tmp_path)
        for path in (result.run_dir / REQUESTS_DIR).glob("*.json"):
            request = json.loads(path.read_text(encoding="utf-8"))
            prompt = request["user_prompt"]
            assert re.search(r"現在時刻\(JST\): \d{1,2}:\d{2} \(", prompt), path.name
            assert not re.search(r"現在時刻\(JST\): \d{1,2}/\d{1,2} ", prompt), path.name

    def test_request_user_prompt_matches_production_builder(self, tmp_path: Path) -> None:
        """user_prompt は同じ build_user_prompt の出力と一致する。

        判断日は context が空のままなので、現在時刻行に月日は出ない（#376）。
        判断バッチは decision_date_jst を埋めて月日を出す。
        """
        store, result = _prepared(tmp_path)
        day_ctx = build_day_context(
            _DAYS[0], store, decision_time=NIGHT_SESSION.decision_time
        )
        assert day_ctx is not None
        request = json.loads(
            (result.run_dir / REQUESTS_DIR / "0001.json").read_text(encoding="utf-8")
        )
        assert request["user_prompt"] == build_user_prompt(day_ctx.prompt_context)
        assert "stop_loss" not in request["output_schema"]["properties"]  # スキーマに SL なし

    def test_non_blind_uses_date_names(self, tmp_path: Path) -> None:
        _, result = _prepared(tmp_path, blind=False)
        names = sorted(p.stem for p in (result.run_dir / REQUESTS_DIR).glob("*.json"))
        assert names == [d.isoformat() for d in _DAYS]

    def test_prepare_refuses_oos_without_unlock(self, tmp_path: Path) -> None:
        store = KlinesStore(cache_dir=tmp_path / "cache")
        with pytest.raises(OosLockedError, match="prepare"):
            prepare_run(store, tmp_path / "runs", start=date(2026, 2, 2), end=date(2026, 2, 6))


class TestParseDecision:
    def test_valid_payload(self) -> None:
        d = parse_decision_payload(json.dumps(_VALID_DECISION), "0001")
        assert d.direction == "LONG"
        assert d.should_enter is True

    @pytest.mark.parametrize("key", ["stop_loss", "stopLoss", "lot", "v_safe"])
    def test_forbidden_key_falls_to_no_entry(self, key: str) -> None:
        """ガードレール #5: SL・ロットを出力する判断は無効化して no_entry に倒す。"""
        payload = {**_VALID_DECISION, key: 154.9}
        d = parse_decision_payload(json.dumps(payload), "0001")
        assert d.direction == "NO_ENTRY"
        assert d.should_enter is False
        assert "禁止キー" in d.rationale

    def test_broken_json_falls_to_no_entry(self) -> None:
        d = parse_decision_payload('{"direction": ', "0001")
        assert d.direction == "NO_ENTRY"
        assert "パース失敗" in d.rationale

    def test_schema_violation_falls_to_no_entry(self) -> None:
        # actionable なのに entry_price 欠落 → pydantic が拒否 → no_entry
        payload = {**_VALID_DECISION}
        del payload["entry_price"]
        d = parse_decision_payload(json.dumps(payload), "0001")
        assert d.direction == "NO_ENTRY"
        assert "スキーマ違反" in d.rationale

    def test_non_object_falls_to_no_entry(self) -> None:
        d = parse_decision_payload('["LONG"]', "0001")
        assert d.direction == "NO_ENTRY"


class TestCheckAndResume:
    def test_pending_lists_only_missing(self, tmp_path: Path) -> None:
        """受け入れ条件: 中断→再開（未処理分のみ列挙・追記で解消）。"""
        _, result = _prepared(tmp_path)
        _write_decision(result.run_dir, "0001", _VALID_DECISION)
        check = check_run(result.run_dir)
        assert check.pending == ["0002", "0003"]
        assert not check.ok

        _write_decision(result.run_dir, "0002", _VALID_DECISION)
        _write_decision(result.run_dir, "0003", _VALID_DECISION)
        assert check_run(result.run_dir).ok

    def test_unparsable_decision_flagged(self, tmp_path: Path) -> None:
        _, result = _prepared(tmp_path)
        _write_decision(result.run_dir, "0001", '{"broken": ')
        _write_decision(result.run_dir, "0002", _VALID_DECISION)
        _write_decision(result.run_dir, "0003", _VALID_DECISION)
        check = check_run(result.run_dir)
        assert check.unparsable == ["0001"]

    def test_order_violation_detected(self, tmp_path: Path) -> None:
        """先読み禁止の事後検証: mtime が時系列順に非減少でない場合を検出する。"""
        _, result = _prepared(tmp_path)
        p1 = _write_decision(result.run_dir, "0001", _VALID_DECISION)
        _write_decision(result.run_dir, "0002", _VALID_DECISION)
        _write_decision(result.run_dir, "0003", _VALID_DECISION)
        # 0001 を最新に偽装 → 0002 が「直前より古い」= 順序違反として検出される
        latest = max(
            (result.run_dir / DECISIONS_DIR / f"{n}.json").stat().st_mtime for n in ("0002", "0003")
        )
        os.utime(p1, (latest + 10, latest + 10))
        check = check_run(result.run_dir)
        assert check.order_violations == ["0002"]


class TestSubagentFileSource:
    def test_reads_decisions_and_records_model(self, tmp_path: Path) -> None:
        store, result = _prepared(tmp_path)
        for name in ("0001", "0002", "0003"):
            _write_decision(result.run_dir, name, _VALID_DECISION)
        source = SubagentFileSource(result.run_dir)
        day_ctx = build_day_context(
            _DAYS[0], store, decision_time=NIGHT_SESSION.decision_time
        )
        assert day_ctx is not None
        out = source.decide(_DAYS[0], day_ctx.prompt_context)
        assert out.decision.direction == "LONG"
        assert out.model == BACKTEST_DECIDER_MODEL
        assert out.llm_tokens is None  # サブエージェント経由では取得不能 → 0 と偽らない

    def test_missing_decision_falls_to_no_entry(self, tmp_path: Path) -> None:
        store, result = _prepared(tmp_path)
        source = SubagentFileSource(result.run_dir)
        day_ctx = build_day_context(
            _DAYS[0], store, decision_time=NIGHT_SESSION.decision_time
        )
        assert day_ctx is not None
        out = source.decide(_DAYS[0], day_ctx.prompt_context)
        assert out.decision.direction == "NO_ENTRY"


class TestEndToEndWithReport:
    def test_simulate_and_report_include_model_chunk_blind(self, tmp_path: Path) -> None:
        """受け入れ条件: モデル・chunk_size・ブラインド有無・免責がレポートに載る。"""
        store, result = _prepared(tmp_path)
        for name in ("0001", "0002", "0003"):
            _write_decision(result.run_dir, name, _VALID_DECISION)
        assert check_run(result.run_dir).ok

        source = SubagentFileSource(result.run_dir)
        config = RunConfig(start=_DAYS[0], end=_DAYS[-1])
        outcome = run_backtest(store, source, config)
        assert len(outcome.results) == 3
        assert outcome.total_llm_tokens is None

        metrics = compute_metrics(outcome.results)
        registry = append_experiment(
            tmp_path / "e.jsonl",
            outcome,
            "run-t",
            metrics,
            model=BACKTEST_DECIDER_MODEL,
            chunk_size=15,
        )
        save_run(
            tmp_path / "runs",
            "run-t",
            outcome,
            metrics,
            metrics,
            registry,
            model=BACKTEST_DECIDER_MODEL,
            run_dir=result.run_dir,
            extra_meta={
                "評価対象モデル": BACKTEST_DECIDER_MODEL,
                "chunk_size": 15,
                "ブラインドモード": True,
            },
        )
        report_md = (result.run_dir / "report.md").read_text(encoding="utf-8")
        assert BACKTEST_DECIDER_MODEL in report_md
        assert "chunk_size: 15" in report_md
        assert "ブラインドモード: True" in report_md
        assert "免責" in report_md  # 学習知識バイアスの免責ヘッダ（§6.3）
        # 台帳にも chunk_size / model が残る
        entry = json.loads((tmp_path / "e.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert entry["model"] == BACKTEST_DECIDER_MODEL
        assert entry["chunk_size"] == 15


def _apply_ask_spread(bid_candles: list[dict[str, str]], spread_yen: float) -> list[dict[str, str]]:
    return [
        {
            **c,
            "open": f"{float(c['open']) + spread_yen:.3f}",
            "high": f"{float(c['high']) + spread_yen:.3f}",
            "low": f"{float(c['low']) + spread_yen:.3f}",
            "close": f"{float(c['close']) + spread_yen:.3f}",
        }
        for c in bid_candles
    ]


def _set_day_ask_spread(store: KlinesStore, day: date, ask_spread_yen: float) -> None:
    """既存 BID 5 分足を維持し、ASK 側だけスプレッドを上書きする。"""
    bid = store.load("BID", "5min", day.strftime("%Y%m%d"))
    if bid is None:
        raise ValueError(f"BID 5min cache missing for {day}")
    ask = _apply_ask_spread(bid, ask_spread_yen)
    store.save("ASK", "5min", day.strftime("%Y%m%d"), ask)


def _save_5min_with_spread(
    store: KlinesStore,
    day: date,
    *,
    ask_spread_yen: float,
    flat_close: float = 155.65,
) -> None:
    """単一 close の 5 分足（モメンタムゼロ）と指定スプレッドで上書きする。"""
    bid = []
    t = _jst_ms(day, 6)
    end = _jst_ms(day, 23, 55)
    while t <= end:
        bid.append(_candle(t, flat_close, flat_close + 0.05, flat_close - 0.05, flat_close))
        t += 5 * 60 * 1_000
    store.save("BID", "5min", day.strftime("%Y%m%d"), bid)
    ask = _apply_ask_spread(bid, ask_spread_yen)
    store.save("ASK", "5min", day.strftime("%Y%m%d"), ask)


def _save_high_vol_daily(store: KlinesStore, anchor: date) -> None:
    """G2 境界（ATR 160 pips）を超える日足レンジに差し替える。"""
    daily_days: list[date] = []
    d = anchor
    while len(daily_days) < 30:
        if d.weekday() < 5:
            daily_days.append(d)
        d -= timedelta(days=1)
    daily_days.reverse()
    daily_by_year: dict[int, list[dict[str, str]]] = {}
    for i, dd in enumerate(daily_days):
        base = 155.0 + i * 0.02
        daily_by_year.setdefault(dd.year, []).append(
            _candle(_jst_ms(dd, 6), base, base + 2.0, base - 2.0, base + 0.5)
        )
    for y, candles in daily_by_year.items():
        store.save("BID", "1day", str(y), candles)


class TestEdgeGuardPrepare:
    """RD-8 エッジガード day-skip の prepare 貫通（Issue #198）。"""

    def test_g1_wide_spread_skips_request_and_manifest(self, tmp_path: Path) -> None:
        store = KlinesStore(cache_dir=tmp_path / "cache")
        _populate_cache(store, _DAYS)
        _set_day_ask_spread(store, _DAYS[0], ask_spread_yen=0.012)  # 1.2 pips > 閾値 1.0（RD-17 #219）

        result = prepare_run(
            store, tmp_path / "runs", start=_DAYS[0], end=_DAYS[-1], run_id="edge-g1"
        )
        manifest = load_manifest(result.run_dir)
        assert result.prepared == 2
        assert manifest["skipped"] == []
        assert manifest["edge_guard_skipped"] == [
            {"date": _DAYS[0].isoformat(), "reason": "wide_spread_day_skip"}
        ]
        assert [e["date"] for e in manifest["entries"]] == [
            _DAYS[1].isoformat(),
            _DAYS[2].isoformat(),
        ]
        assert len(list((result.run_dir / REQUESTS_DIR).glob("*.json"))) == 2

    def test_g1_just_below_threshold_prepares(self, tmp_path: Path) -> None:
        store = KlinesStore(cache_dir=tmp_path / "cache")
        _populate_cache(store, _DAYS)  # 既定 0.4 pips（MAX_DECISION_SPREAD_PIPS 1.0 未満）

        result = prepare_run(
            store, tmp_path / "runs", start=_DAYS[0], end=_DAYS[-1], run_id="edge-g1-pass"
        )
        assert result.prepared == 3
        assert load_manifest(result.run_dir)["skipped"] == []
        assert load_manifest(result.run_dir)["edge_guard_skipped"] == []

    def test_g2_high_vol_skips(self, tmp_path: Path) -> None:
        store = KlinesStore(cache_dir=tmp_path / "cache")
        _populate_cache(store, _DAYS)
        _save_high_vol_daily(store, _DAYS[0])

        result = prepare_run(
            store, tmp_path / "runs", start=_DAYS[0], end=_DAYS[0], run_id="edge-g2"
        )
        manifest = load_manifest(result.run_dir)
        assert result.prepared == 0
        assert manifest["skipped"] == []
        assert manifest["edge_guard_skipped"] == [
            {"date": _DAYS[0].isoformat(), "reason": "high_vol_regime_day_skip"}
        ]

    def test_g3_no_momentum_skips(self, tmp_path: Path) -> None:
        store = KlinesStore(cache_dir=tmp_path / "cache")
        _populate_cache(store, _DAYS)
        # 当日始値=現値（フラット）→ session_move=0 < 0.05*ATR
        _save_5min_with_spread(store, _DAYS[0], ask_spread_yen=0.004, flat_close=155.65)

        result = prepare_run(
            store, tmp_path / "runs", start=_DAYS[0], end=_DAYS[-1], run_id="edge-g3"
        )
        manifest = load_manifest(result.run_dir)
        assert result.prepared == 2
        assert manifest["skipped"] == []
        assert manifest["edge_guard_skipped"] == [
            {"date": _DAYS[0].isoformat(), "reason": "no_momentum_signal_day_skip"}
        ]

    def test_runner_applies_edge_guard_no_entry(self, tmp_path: Path) -> None:
        """機械 run 経路でも G1 skip 日は simulate に no_entry が載る。"""
        from backtest.sources import RuleBasedSource

        store = KlinesStore(cache_dir=tmp_path / "cache")
        _populate_cache(store, [_DAYS[0]])
        _set_day_ask_spread(store, _DAYS[0], ask_spread_yen=0.012)

        outcome = run_backtest(
            store,
            RuleBasedSource(),
            RunConfig(start=_DAYS[0], end=_DAYS[0]),
        )
        assert len(outcome.results) == 1
        assert outcome.results[0].status == "no_entry"
        assert "エッジガード" in outcome.results[0].detail
        assert "wide_spread_day_skip" in outcome.results[0].detail


class TestAgentDefinitionDrift:
    """コード側のピン留めと .claude/agents 定義の乖離を検知する。"""

    def test_model_pinned_to_production_decision_model(self) -> None:
        assert BACKTEST_DECIDER_MODEL == DEFAULT_DECISION_MODEL

    def test_agent_file_matches_constants(self) -> None:
        agent_md = (_ROOT / ".claude" / "agents" / "backtest-decider.md").read_text(
            encoding="utf-8"
        )
        assert f"model: {BACKTEST_DECIDER_MODEL}" in agent_md
        assert "tools: Read, Write" in agent_md
        assert "stop_loss" in agent_md  # 出力禁止の明記
        assert "先読み禁止" in agent_md
