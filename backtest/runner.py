"""バックテスト日次ループ（BT-3, 設計書 §2 phase 3 / §7-1）。

キャッシュ済みデータのみで [start, end] の営業日を順に再生する（完全オフライン）。
1 営業日 = build_day_context（BT-1・厳密カットオフ）→ DecisionSource →
load_session_bars + simulate_day（BT-2・悲観側規約）。

OOS（Out-of-Sample）封印（設計書 §7-1）:
  In-Sample:      2023-11-01 〜 2025-12-31
  Out-of-Sample:  2026-01-01 〜（ホールドアウト）
  OOS 期間に触れる run は unlock_oos=True の明示がない限り OosLockedError で拒否する。
  「OOS は最後に少数回」の規律をコードで強制し、全 OOS 実行は実験レジストリに記録される。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta

from fxlab.registry import Period, PeriodLockedError, PeriodPlan

from backend.src.llm.trade_decision import no_entry
from backend.src.validation.day_skip_guards import EdgeGuardDaySkipResult
from backtest.context import DEFAULT_EQUITY_JPY, DayContext, build_day_context
from backtest.data import KlinesStore
from backtest.edge_guard import EDGE_GUARD_NO_ENTRY_RATIONALE, evaluate_day_context_edge_guard
from backtest.engine import (
    NIGHT_SESSION,
    SessionWindow,
    SimConfig,
    TradeResult,
    closeout_ms_for,
    load_session_bars,
    simulate_day,
)
from backtest.sources import DecisionSource

# 本番モジュールが実行時に環境変数から読む、結果に効く値（サイジング・バリデーション・
# エッジガード・SL/TP 既定）。run 開始時に記録し config_hash に含める（環境次第で結果が
# 変わっても台帳上は同一設定に見える、を防ぐ）。
RESULT_AFFECTING_ENV = (
    "RISK_PERCENT",
    "MAX_LEVERAGE",
    "MVP_MODE",
    "MIN_RR_RATIO",
    "HOLD_HOURS",
    "RR_TARGET_DEFAULT",
    "MAX_DECISION_SPREAD_PIPS",
    "MAX_ATR_DAY_SKIP_PIPS",
    "MIN_SESSION_MOMENTUM_ATR",
)


def env_snapshot() -> dict[str, str | None]:
    return {k: os.environ.get(k) for k in RESULT_AFFECTING_ENV}


IS_START = date(2023, 11, 1)  # 分足データの提供開始（2023-10-28）以降の月初
OOS_START = date(2026, 1, 1)  # ホールドアウト開始

PERIODS = PeriodPlan(
    (
        Period("IS", IS_START, OOS_START - timedelta(days=1)),
        Period("OOS", OOS_START, date.max, sealed=True),
    )
)


class OosLockedError(PeriodLockedError):
    """OOS 期間への run を unlock なしで実行しようとした（設計書 §7-1）。"""


def check_oos(start: date, end: date, *, unlock_oos: bool, stage: str = "run") -> None:
    """OOS 封印の唯一の判定点（run と prepare で共有）。"""
    try:
        PERIODS.check(start, end, unlock=["OOS"] if unlock_oos else ())
    except PeriodLockedError as exc:
        raise OosLockedError(
            f"window end={end.isoformat()} は OOS（{OOS_START.isoformat()}〜）に触れています。"
            f"ホールドアウトの規律（設計書 §7-1）により {stage} には --unlock-oos の"
            "明示が必要です。"
        ) from exc


@dataclass(frozen=True)
class TradingDay:
    """営業日ループの 1 日分。context=None はコンテキスト構築不能（休場・キャッシュ欠損）。"""

    day: date
    context: DayContext | None
    edge_guard: EdgeGuardDaySkipResult | None


def iter_trading_days(
    store: KlinesStore,
    start: date,
    end: date,
    *,
    equity_jpy: float,
    session: SessionWindow,
) -> Iterator[TradingDay]:
    """[start, end] の平日を順に、判断コンテキストとエッジガード判定付きで返す。

    run（本ファイル）と prepare（subagent.py）が同じループを使う。
    """
    day = start
    while day <= end:
        if day.weekday() < 5:  # 土日は営業日でない
            ctx = build_day_context(
                day, store, equity_jpy=equity_jpy, decision_time=session.decision_time
            )
            guard = evaluate_day_context_edge_guard(ctx) if ctx is not None else None
            yield TradingDay(day, ctx, guard)
        day += timedelta(days=1)


@dataclass(frozen=True)
class RunConfig:
    """1 回のバックテスト実行の設定。config_hash の対象。"""

    start: date
    end: date
    equity_jpy: float = DEFAULT_EQUITY_JPY
    sim: SimConfig = field(default_factory=SimConfig)
    session: SessionWindow = field(default_factory=lambda: NIGHT_SESSION)  # 既定 = 夜型（本番同型・RD-6）
    unlock_oos: bool = False

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"start > end: {self.start} > {self.end}")

    @property
    def touches_oos(self) -> bool:
        return bool(PERIODS.sealed_touched(self.start, self.end))


@dataclass(frozen=True)
class RunOutcome:
    """run_backtest の結果一式（レポート・レジストリ入力）。"""

    source_name: str
    config: RunConfig
    results: list[TradeResult]
    skipped_days: list[date]  # コンテキスト構築不能（休場・キャッシュ欠損）
    is_oos: bool
    # run 全体の実 LLM トークン合計（税務証跡 §5.2）。非 LLM ソースは None（0 と偽らない）
    total_llm_tokens: int | None = None
    # run 開始時の結果に効く環境変数（RESULT_AFFECTING_ENV）
    env: dict[str, str | None] = field(default_factory=dict)
    # 判断に使ったシステムプロンプトのハッシュ（LLM 判断ソースのみ・§7-6）
    prompt_sha: str | None = None


def run_backtest(
    store: KlinesStore,
    source: DecisionSource,
    config: RunConfig,
) -> RunOutcome:
    """営業日ループでバックテストを実行する（オフライン・前方走査のみ）。

    Raises:
        OosLockedError: OOS 期間に触れる window を unlock_oos なしで実行した場合。
    """
    check_oos(config.start, config.end, unlock_oos=config.unlock_oos)
    env = env_snapshot()

    results: list[TradeResult] = []
    skipped: list[date] = []
    total_llm_tokens: int | None = None
    for td in iter_trading_days(
        store,
        config.start,
        config.end,
        equity_jpy=config.equity_jpy,
        session=config.session,
    ):
        day, day_ctx = td.day, td.context
        if day_ctx is None:
            skipped.append(day)
            continue
        ctx = day_ctx.prompt_context
        if td.edge_guard is not None and td.edge_guard.skip:
            results.append(
                simulate_day(
                    day=day,
                    decision=no_entry(f"{EDGE_GUARD_NO_ENTRY_RATIONALE}: {td.edge_guard.reason}"),
                    atr=ctx.atr_14d,
                    equity_jpy=config.equity_jpy,
                    current_price=ctx.current_price_mid,
                    cutoff_ms=day_ctx.cutoff_ms,
                    bars=None,
                    config=config.sim,
                    llm_token_usage=None,
                )
            )
            continue
        out = source.decide(day, ctx)
        if out.llm_tokens is not None:
            total_llm_tokens = (total_llm_tokens or 0) + out.llm_tokens
        # should_enter=False の日はエンジンがバーを参照しないため読み込み自体を省く
        bars = (
            load_session_bars(store, day, day_ctx.cutoff_ms, closeout_ms_for(day, config.session))
            if out.decision.should_enter
            else None
        )
        results.append(
            simulate_day(
                day=day,
                decision=out.decision,
                atr=ctx.atr_14d,
                equity_jpy=config.equity_jpy,
                current_price=ctx.current_price_mid,
                cutoff_ms=day_ctx.cutoff_ms,
                bars=bars,
                config=config.sim,
                llm_token_usage=out.llm_tokens,
            )
        )

    return RunOutcome(
        source_name=source.name,
        config=config,
        results=results,
        skipped_days=skipped,
        is_oos=config.touches_oos,
        total_llm_tokens=total_llm_tokens,
        env=env,
        prompt_sha=getattr(source, "prompt_sha", None),
    )
