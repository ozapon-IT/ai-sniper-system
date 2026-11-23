"""バックテスト約定エンジン（BT-2, 設計書 §5）。

1 日 1 本の IFD-OCO（LIMIT/STOP エントリー + OCO TP/SL + 夜間強制手仕舞い）を
5 分足 BID/ASK 系列の前方走査で再生する。

本番フローの写像（morning_batch と同一の順序・同一の関数）:
  TradeDecision → tick 丸め → enrich（SL 決定論算出）→ サイジング →
  pre_order バリデーション（reject は発注せず記録）→ ブラケット注文の約定判定。

強制手仕舞い時刻（infra/terraform scheduler.tf の cron が唯一の正・§1.1, §6.2）:
  - 本番夜型（NIGHT_SESSION）: 判断 PM 8:30（cron(30 20 ? * MON-FRI)）、
    手仕舞い AM 4:30 単一 cron（cron(30 4 ? * TUE-SAT)）
  - 朝型（MORNING_SESSION）は RD-1 A/B 用プリセット
    （判断 08:00 / 翌 0:00 / 土 5:00）。`--session-preset morning` で指定
  いずれも当日 5 分足ファイル（JST 06:00 起点・翌 05:55 まで）内に収まるため、
  1 営業日のシミュレーションは当日ファイルだけで完結する。

悲観側規約（backtesting.py の adversarial 思想と同じ・設計書 §5）:
  - 同一バーで TP/SL 両到達 → SL 優先
  - エントリーバーでも SL/TP を判定（エントリー → 即 SL があり得る）
  - STOP エントリーと SL exit はギャップ時に不利側（バー始値）へ滑る
  - LIMIT エントリーと TP exit は指値どおり（有利側ギャップの改善は見込まない）

バーは昇順リストを先頭から一度だけ走査する（未来バーを参照する API を持たない）。
約定判定そのものは fxlab.fills.simulate_bracket（ここは本番数理で注文を組み立てる側）。
"""

from __future__ import annotations

import math
import time as _time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Literal, TypeAlias
from zoneinfo import ZoneInfo

import structlog
from fxlab.bars import BidAskBar
from fxlab.fills import BracketOutcome, FillMode, simulate_bracket

from backend.src.engine.enrich import (
    ATR_MULT_DEFAULT,
    ATR_MULT_MAX,
    ATR_MULT_MIN,
    HOLD_HOURS_DEFAULT,
    RR_TARGET_DEFAULT,
    RR_TARGET_MAX,
    RR_TARGET_MIN,
    USD_JPY_TICK_SIZE,
    calculate_stop_loss,
    calculate_take_profit,
)
from backend.src.engine.leverage_cap import calculate_v_safe
from backend.src.engine.position_sizing import calculate_position_size, resolve_order_size
from backend.src.llm.trade_decision import TradeDecision
from backend.src.validation.pre_order import ValidationError, validate_pre_order
from backtest.data import KlinesStore

_log = structlog.get_logger(__name__)

_JST = ZoneInfo("Asia/Tokyo")

_PIP = 0.01  # USD/JPY: 1 pip = 0.01 円
_TICK = float(USD_JPY_TICK_SIZE)
_MS_5MIN = 5 * 60 * 1_000

Direction = Literal["LONG", "SHORT"]

# 新数理の凍結パラメータ（RD-2 #167 / RD-4 #169・親 #165 の実測根拠）。本番 enrich の定数を
# 唯一の正として参照する（バックテストと本番で数理が乖離しないため）。ATR_h = ATR14×√(HOLD/24)、
# SL = entry ∓ atr_mult×ATR_h、TP = entry ± rr_target×|entry−SL|。チューニング禁止（設計書 §7.4）。
# 実測: 夜窓レンジ/ATR14 中央値 0.59 ≈ √(8/24)=0.577。0.7×√(8/24)=0.404×ATR14 が
# tp/sl 発火をエグジット過半にできる SL 幅（med ATR 時 ≈ 44 pips）。
SESSION_VOL_HOLD_HOURS = HOLD_HOURS_DEFAULT
SESSION_VOL_ATR_MULT = ATR_MULT_DEFAULT
SESSION_VOL_RR_TARGET = RR_TARGET_DEFAULT
_SESSION_VOL_ATR_MULT_MIN = ATR_MULT_MIN
_SESSION_VOL_ATR_MULT_MAX = ATR_MULT_MAX
_SESSION_VOL_RR_TARGET_MIN = RR_TARGET_MIN
_SESSION_VOL_RR_TARGET_MAX = RR_TARGET_MAX


# exit_reason / 非約定ステータス（設計書 §5 出力仕様）
Status = Literal[
    "no_entry",  # LLM 見送り（should_enter=False）
    "lot_zero",  # サイジング結果 0（証拠金不足・レバ上限）
    "rejected",  # §3.4 発注前バリデーション違反（発注せず記録）
    "no_data",  # 当日 5 分足キャッシュ欠損（休場日等）
    "no_fill",  # エントリー未約定のまま手仕舞い時刻 → キャンセル
    "tp",  # OCO 利確
    "sl",  # OCO 損切り
    "closeout",  # 夜間強制手仕舞い（成行）
]


@dataclass(frozen=True)
class SimConfig:
    """約定シミュレーション設定。

    slippage_pips は感度分析専用（設計書 §5: チューニング対象にしない）。
    STOP エントリー・SL exit・closeout exit にのみ不利方向へ適用する。
    available_ratio は口座履歴が存在しないための近似（利用可能証拠金 = equity × ratio）。

    SL/TP は本番 enrich（ATR_h 基準・RD-4 #169）で決定論算出する。
      use_decision_sizing_params=False（凍結ベースライン既定）では cfg.session_vol_* を使い
      decision の倍率は無視する（RD-2 再現・チューニング禁止 §7.4）。
      use_decision_sizing_params=True（LLM / BT-5）では decision.atr_multiplier / rr_target を
      本番 decision_batch と同様に使用する（#187）。
    """

    fill_mode: FillMode = "touch"
    slippage_pips: float = 0.0
    available_ratio: float = 1.0
    use_decision_sizing_params: bool = False
    hold_hours: float = SESSION_VOL_HOLD_HOURS
    session_vol_atr_mult: float = SESSION_VOL_ATR_MULT
    session_vol_rr_target: float = SESSION_VOL_RR_TARGET

    @property
    def sizing_param_source(self) -> Literal["frozen", "decision"]:
        """レポート用: SL/TP 倍率の出所（frozen = cfg 凍結値 / decision = LLM 出力）。"""
        return "decision" if self.use_decision_sizing_params else "frozen"

    def __post_init__(self) -> None:
        """安全系設定の実行時検証（Literal は実行時制約にならない）。

        負の slippage は悲観側が有利側に反転し、available_ratio > 1 は証拠金を
        楽観化するため拒否する。SL/TP 倍率は範囲外を拒否する
        （黙ってクランプすると凍結値からのズレが気付かれないため）。
        """
        if self.fill_mode not in ("touch", "strict"):
            raise ValueError(f"unsupported fill_mode: {self.fill_mode!r}")
        if self.slippage_pips < 0:
            raise ValueError(f"slippage_pips は非負でなければなりません: {self.slippage_pips}")
        if not (0 < self.available_ratio <= 1):
            raise ValueError(
                f"available_ratio は (0, 1] でなければなりません: {self.available_ratio}"
            )
        if self.hold_hours <= 0:
            raise ValueError(f"hold_hours は正でなければなりません: {self.hold_hours}")
        if not (
            _SESSION_VOL_ATR_MULT_MIN <= self.session_vol_atr_mult <= _SESSION_VOL_ATR_MULT_MAX
        ):
            raise ValueError(
                f"session_vol_atr_mult は [{_SESSION_VOL_ATR_MULT_MIN}, "
                f"{_SESSION_VOL_ATR_MULT_MAX}] でなければなりません: {self.session_vol_atr_mult}"
            )
        if not (
            _SESSION_VOL_RR_TARGET_MIN <= self.session_vol_rr_target <= _SESSION_VOL_RR_TARGET_MAX
        ):
            raise ValueError(
                f"session_vol_rr_target は [{_SESSION_VOL_RR_TARGET_MIN}, "
                f"{_SESSION_VOL_RR_TARGET_MAX}] でなければなりません: {self.session_vol_rr_target}"
            )


@dataclass(frozen=True)
class SessionWindow:
    """セッション窓（判断時刻と強制手仕舞い時刻・すべて JST）。RD-1（#166）。

    closeout_time / friday_closeout_time は「判断日の翌カレンダー日」の時刻。
    既定値は本番（scheduler.tf cron）と同じ夜型（`NIGHT_SESSION`）:
      判断 20:30 / 全営業日 → 翌 4:30。
    朝型プリセット（`MORNING_SESSION`）は RD-1 A/B 用:
      判断 08:00 / 月〜木 → 翌 0:00 / 金 → 土曜 5:00。

    制約（fail-closed）: 5 分足キャッシュの日付ファイルは JST 06:00 起点・翌 05:55 まで
    （backtest/data.py）。窓が当日ファイル内で完結しないと closeout までのバーを黙って
    失い悲観側規約が破れるため、構築時に検証して拒否する。
    """

    decision_time: time = time(20, 30)
    closeout_time: time = time(4, 30)
    friday_closeout_time: time = time(4, 30)

    def __post_init__(self) -> None:
        # 全時刻を 5 分足グリッド（openTime は :00/:05/…）に整合させる。非整合値は
        # 実挙動が設定値からずれる: closeout は採用最終バーが closeout_ms 未満で最遅の
        # ため exit が設定値を最大 5 分弱超過し、悲観側の保有時間上限が緩む。decision は
        # 直前確定足が古くなる（cutoff 未満で最遅の確定足を使う）。プリセット
        # morning/night は全て 5 分整合のため、この検証で弾かれるのは任意上書き時のみ。
        def _require_5min_grid(label: str, t: time) -> None:
            if (t.hour * 60 + t.minute) % 5 != 0:
                raise ValueError(
                    f"{label} は 5 分足バーの区切りに合わせ 5 分単位でなければなりません"
                    "（非整合値は判断/手仕舞いの実時刻が設定値からずれる）: "
                    f"{t.isoformat('minutes')}"
                )

        if self.decision_time < time(6, 5):
            # 5 分足ファイルは JST 06:00 起点（最古の足 openTime=06:00）。build_day_context は
            # 「openTime + 5分 <= cutoff」の確定足を要求するため、cutoff=06:00 だと確定足が
            # 0 本になり全日 no_5min_price でスキップされる。最短でも 06:05 が必要。
            raise ValueError(
                "decision_time は 06:05 JST 以降でなければなりません（5 分足ファイルは "
                "JST 06:00 起点で、cutoff=06:00 だと確定足が 0 本になる）: "
                f"{self.decision_time.isoformat('minutes')}"
            )
        _require_5min_grid("decision_time", self.decision_time)
        for label, t in (
            ("closeout_time", self.closeout_time),
            ("friday_closeout_time", self.friday_closeout_time),
        ):
            if t > time(5, 55):
                raise ValueError(
                    f"{label} は翌日 05:55 JST 以前でなければなりません（当日 5 分足ファイル"
                    "の終端。窓外の closeout はバー欠落で悲観側規約が破れる）: "
                    f"{t.isoformat('minutes')}"
                )
            _require_5min_grid(label, t)

    def as_dict(self) -> dict[str, str]:
        """レジストリ・レポート記録用（HH:MM 文字列・config_hash の入力にも使う）。"""
        return {
            "decision_time": self.decision_time.isoformat("minutes"),
            "closeout_time": self.closeout_time.isoformat("minutes"),
            "friday_closeout_time": self.friday_closeout_time.isoformat("minutes"),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, str]) -> SessionWindow:
        """as_dict の逆変換（run_meta.json からの復元用）。検証は __post_init__ に委ねる。"""
        return cls(
            decision_time=time.fromisoformat(payload["decision_time"]),
            closeout_time=time.fromisoformat(payload["closeout_time"]),
            friday_closeout_time=time.fromisoformat(payload["friday_closeout_time"]),
        )


# 本番と同一の夜型（既定・RD 再設計 #165）と、RD-1 A/B 用の朝型
NIGHT_SESSION = SessionWindow()
MORNING_SESSION = SessionWindow(
    decision_time=time(8, 0),
    closeout_time=time(0, 0),
    friday_closeout_time=time(5, 0),
)

SESSION_PRESETS: dict[str, SessionWindow] = {
    "morning": MORNING_SESSION,
    "night": NIGHT_SESSION,
}


# BID/ASK を openTime で突合した 5 分足 1 本（fxlab の型をそのまま使う）
Bar5m: TypeAlias = BidAskBar


@dataclass(frozen=True)
class TradeResult:
    """1 営業日 = 1 レコードのシミュレーション結果（設計書 §5 出力仕様）。"""

    day: date
    status: Status
    direction: Direction | None = None
    order_type: str | None = None
    entry_price: float | None = None  # 発注価格（tick 丸め済み）
    fill_price: float | None = None  # 実約定価格（STOP ギャップ・slippage 反映）
    take_profit: float | None = None
    stop_loss: float | None = None
    lot: int = 0
    filled_at_ms: int | None = None
    exit_at_ms: int | None = None
    exit_price: float | None = None
    pnl_pips: float | None = None
    r_multiple: float | None = None
    detail: str = ""
    sizing: dict[str, float] = field(default_factory=dict)


def round_tick_entry(price: float, side: Direction) -> float:
    """entry_price の tick 丸め（現値から遠ざける向き・LONG 切り下げ / SHORT 切り上げ）。

    本番 decision_batch._round_tick_entry と同一定義（§3.4・#238）。バッチモジュールは
    発注経路を含むため import できず（AGENTS.md #1・tests/backtest/test_guardrails.py）、
    ここに同一実装を置く。同値性は tests/backtest/test_engine.py のゴールデンテストで担保。
    """
    rounding = ROUND_DOWN if side == "LONG" else ROUND_UP
    return float(Decimal(str(price)).quantize(USD_JPY_TICK_SIZE, rounding=rounding))


def closeout_ms_for(day: date, session: SessionWindow | None = None) -> int:
    """営業日 day の建玉の強制手仕舞い時刻（epoch ms）を返す。

    session 省略時は夜型（本番と同じ・全営業日 翌 4:30 JST）。
    closeout はどの窓でも「翌カレンダー日」の時刻（SessionWindow docstring）。
    土日は営業日でないため ValueError。
    """
    window = session or NIGHT_SESSION
    weekday = day.weekday()
    if weekday >= 5:
        raise ValueError(f"{day.isoformat()} は営業日ではありません（土日）")
    t = window.friday_closeout_time if weekday == 4 else window.closeout_time
    dt = datetime.combine(day + timedelta(days=1), t, tzinfo=_JST)
    return int(dt.timestamp() * 1_000)


def load_session_bars(
    store: KlinesStore,
    day: date,
    cutoff_ms: int,
    closeout_ms: int,
) -> list[Bar5m] | None:
    """当日 5 分足キャッシュから [cutoff, closeout) のセッションバーを構築する。

    キャッシュ欠損（BID/ASK いずれか）は None（= no_data、当日スキップ）。
    セッション窓内で BID/ASK の時刻集合が一致しない場合も None。
    片側欠損バーを黙って除外すると、そのバーで起きた SL/TP/closeout を
    見逃して安全側でなくなるため、当日全体を no_data に倒す（fail-closed）。
    """
    bid = store.load("BID", "5min", day.strftime("%Y%m%d"))
    ask = store.load("ASK", "5min", day.strftime("%Y%m%d"))
    if bid is None or ask is None:
        return None
    bid_window = {
        int(c["openTime"]): c for c in bid if cutoff_ms <= int(c["openTime"]) < closeout_ms
    }
    ask_window = {
        int(c["openTime"]): c for c in ask if cutoff_ms <= int(c["openTime"]) < closeout_ms
    }
    if set(bid_window) != set(ask_window):
        _log.warning(
            "backtest_session_bid_ask_mismatch",
            day=day.isoformat(),
            bid_only=sorted(set(bid_window) - set(ask_window)),
            ask_only=sorted(set(ask_window) - set(bid_window)),
            timestamp_ms=int(_time.time() * 1000),
            llm_tokens=None,
        )
        return None
    bars: list[Bar5m] = []
    for t in sorted(bid_window):
        b, a = bid_window[t], ask_window[t]
        bars.append(
            Bar5m(
                open_time_ms=t,
                bid_open=float(b["open"]),
                bid_high=float(b["high"]),
                bid_low=float(b["low"]),
                bid_close=float(b["close"]),
                ask_open=float(a["open"]),
                ask_high=float(a["high"]),
                ask_low=float(a["low"]),
                ask_close=float(a["close"]),
            )
        )
    return bars


def simulate_day(
    *,
    day: date,
    decision: TradeDecision,
    atr: float,
    equity_jpy: float,
    current_price: float,
    cutoff_ms: int,
    bars: list[Bar5m] | None,
    config: SimConfig | None = None,
    llm_token_usage: int | None = None,
) -> TradeResult:
    """1 営業日分の IFD-OCO ライフサイクルを再生する。

    Args:
        day: 対象営業日（判断日）
        decision: 検証済み TradeDecision（LLM / ルールベース / リプレイ）
        atr: 判断時点の日足 ATR（DayContext.prompt_context.atr_14d）
        equity_jpy: 有効純資産（サイジング用）
        current_price: 判断時点の現値 mid（最小乖離チェック用）
        cutoff_ms: 判断時刻 epoch ms（DayContext.cutoff_ms。これ以降のバーのみ使用）
        bars: load_session_bars の結果。None = キャッシュ欠損（no_data）
        config: SimConfig（省略時は既定値）
        llm_token_usage: 判断に要した実 LLM トークン数（監査ログ §5.2 用）。
            ルールベース等 LLM 非経由は None（0 固定で「消費なし」と偽らない）
    """
    cfg = config or SimConfig()

    if not decision.should_enter:
        return TradeResult(day=day, status="no_entry", detail=decision.rationale[:200])

    # ATR 非正/非有限だと SL = entry となり sizing が ValueError で run 全体を落とすため、
    # クラッシュではなく rejected として記録する（判断ソースを問わない防御）
    if not (math.isfinite(atr) and atr > 0):
        return TradeResult(
            day=day,
            status="rejected",
            detail=f"ATR が非正または非有限のため発注不可: {atr!r}",
        )

    # 本番 morning_batch と同一順序: tick 丸め → SL 算出 → サイジング → バリデーション
    direction: Direction = decision.direction  # type: ignore[assignment]
    order_type = decision.order_type or "LIMIT"
    entry_price = round_tick_entry(decision.entry_price, direction)  # type: ignore[arg-type]
    # 新数理（RD-4 #169）: SL/TP とも本番 enrich（ATR_h 基準）で決定論算出する。
    # 凍結ベースラインは cfg.session_vol_*、LLM/BT-5 は decision の倍率（#187・本番同挙動）。
    if cfg.use_decision_sizing_params:
        atr_mult = decision.atr_multiplier
        rr_target = decision.rr_target
    else:
        atr_mult = cfg.session_vol_atr_mult
        rr_target = cfg.session_vol_rr_target
    # ガードレール #5: SL/TP は Python 決定論算出（本番 enrich が数理の単一の正）
    stop_loss = calculate_stop_loss(entry_price, atr, atr_mult, direction, hold_hours=cfg.hold_hours)
    take_profit = calculate_take_profit(entry_price, stop_loss, rr_target, direction)

    sizing = calculate_position_size(
        entry_price=entry_price, stop_loss=stop_loss, equity=equity_jpy
    )
    v_safe = calculate_v_safe(
        v_rounded=int(sizing["v_rounded"]),
        equity=equity_jpy,
        current_price=current_price,
        available_amount=equity_jpy * cfg.available_ratio,
    )
    lot = resolve_order_size(v_safe=v_safe, v_raw=sizing["v_raw"], llm_token_usage=llm_token_usage)

    base = {
        "direction": direction,
        "order_type": order_type,
        "entry_price": entry_price,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "sizing": sizing,
    }

    if lot == 0:
        return TradeResult(day=day, status="lot_zero", **base)  # type: ignore[arg-type]

    try:
        # atr は必須（安全ガードの無音スキップ防止）。ただし現値乖離上限（§3.4・Ver 3.6）は
        # 本番発注ゲートであり、バックテストでは **明示的に** 無効化する（上限を +inf に設定）:
        # 凍結ベンチマークの rule_based は P 回帰で現値から離れるため。夜型ソースは ±0.05×ATR で
        # 元々上限内。cap の実効は本番 pre_order テストで担保する。
        validate_pre_order(
            side=direction,
            entry_price=entry_price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            lot=lot,
            current_price=current_price,
            atr=atr,
            order_type=order_type,
            max_entry_distance_atr=float("inf"),
        )
    except ValidationError as exc:
        return TradeResult(
            day=day,
            status="rejected",
            lot=lot,
            detail=str(exc),
            **base,  # type: ignore[arg-type]
        )

    if bars is None:
        return TradeResult(day=day, status="no_data", lot=lot, **base)  # type: ignore[arg-type]

    # 約定判定は fxlab（悲観側規約: 同一バー両到達は SL 優先・エントリーバーでも判定・
    # STOP エントリーと SL はギャップ時に不利側・TP は指値どおり）
    outcome = simulate_bracket(
        bars,
        direction=direction,
        order_type=order_type,
        entry=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        active_from_ms=cutoff_ms,
        tick=_TICK,
        bar_ms=_MS_5MIN,
        fill_mode=cfg.fill_mode,
        slippage=cfg.slippage_pips * _PIP,
        tp_gap="limit",
    )
    if outcome.status == "no_fill":
        return TradeResult(day=day, status="no_fill", lot=lot, **base)  # type: ignore[arg-type]
    assert outcome.fill_price is not None and outcome.exit_price is not None
    return _closed(day, base, lot, outcome)


def _closed(
    day: date, base: Mapping[str, object], lot: int, outcome: BracketOutcome
) -> TradeResult:
    """クローズ済みトレードの損益を確定して TradeResult を返す。

    exit_at_ms は SL/TP がバー openTime、closeout がバー終了時刻（fxlab が決める）。
    """
    assert outcome.filled_at_ms is not None and outcome.fill_price is not None
    assert outcome.exit_price is not None and outcome.exit_at_ms is not None
    direction = base["direction"]
    entry_price = base["entry_price"]
    stop_loss = base["stop_loss"]
    assert isinstance(entry_price, float) and isinstance(stop_loss, float)

    if direction == "LONG":
        pnl_pips = (outcome.exit_price - outcome.fill_price) / _PIP
    else:
        pnl_pips = (outcome.fill_price - outcome.exit_price) / _PIP
    risk_pips = abs(entry_price - stop_loss) / _PIP
    r_multiple = pnl_pips / risk_pips if risk_pips > 0 else 0.0

    return TradeResult(
        day=day,
        status=outcome.status,
        lot=lot,
        filled_at_ms=outcome.filled_at_ms,
        fill_price=outcome.fill_price,
        exit_at_ms=outcome.exit_at_ms,
        exit_price=outcome.exit_price,
        pnl_pips=round(pnl_pips, 3),
        r_multiple=round(r_multiple, 4),
        **base,  # type: ignore[arg-type]
    )
