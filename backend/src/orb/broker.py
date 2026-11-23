"""仮想ブローカー状態機械（観測列駆動・純粋・I/O 非依存）。

設計書: docs/ai-dev/london-orb-demo-design.md §4.2・§6.1・§6.3・§6.4・§8.2

ORB-2 の ``resolve_fill``（1分足バー）は呼ばない。入力は ``TickerObservation`` の列。
永続化・Slack は呼び出し側アダプタが ``BrokerEvent`` を消費する。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from backend.src.orb.broker_models import (
    BrokerEvent,
    BrokerSession,
    BrokerState,
    DemoSignalClass,
    DirectionName,
    SkipReason,
    SlippageRecord,
    TickerObservation,
    VerifyOutcome,
)
from backend.src.orb.constants import CANCEL_TIME, EOD_TIME, LON, ORDER_TIME, PIP
from backend.src.orb.models import DayResult, RangeLevels, Trade

# 開場ゲート（ORB-1 実測・付録B §3）
_OPEN_STATUS = "OPEN"

# 11:00 境界で「欠測」とみなす間隔（想定ポーリング 5s の 2 倍超）
_BOUNDARY_GAP_MS = 10_000


def _ensure_london(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("TickerObservation.ts must be timezone-aware")
    return ts.astimezone(LON)


def _slip_pips(*, direction: DirectionName, trigger: float, observed_bid: float) -> float:
    """推定滑り（§6.3）: 買い = 観測BID − トリガー、売りは符号反転。"""
    raw = observed_bid - trigger
    return (raw if direction == "long" else -raw) / PIP


def _spread_pips(obs: TickerObservation) -> float:
    return (obs.ask - obs.bid) / PIP


def _poll_gap(
    prev: TickerObservation | None, obs: TickerObservation
) -> tuple[int | None, float | None]:
    if prev is None:
        return None, None
    gap_ms = int((_ensure_london(obs.ts) - _ensure_london(prev.ts)).total_seconds() * 1000)
    return gap_ms, obs.bid - prev.bid


def _make_slip(
    *,
    direction: DirectionName,
    trigger: float,
    obs: TickerObservation,
    prev: TickerObservation | None,
) -> SlippageRecord:
    gap_ms, price_gap = _poll_gap(prev, obs)
    return SlippageRecord(
        trigger_price=trigger,
        observed_bid=obs.bid,
        slip_pips=_slip_pips(direction=direction, trigger=trigger, observed_bid=obs.bid),
        spread_pips=_spread_pips(obs),
        poll_gap_ms=gap_ms,
        price_gap=price_gap,
    )


def _event(
    *,
    ts: datetime,
    reason: str,
    state_from: BrokerState,
    state_to: BrokerState,
    obs: TickerObservation | None = None,
    detail: dict[str, object] | None = None,
    notify_slack: bool = False,
) -> BrokerEvent:
    return BrokerEvent(
        ts=_ensure_london(ts) if ts.tzinfo else ts,
        reason=reason,
        state_from=state_from,
        state_to=state_to,
        bid=None if obs is None else obs.bid,
        ask=None if obs is None else obs.ask,
        detail=detail or {},
        notify_slack=notify_slack,
    )


def _crossed_both(prev_bid: float, curr_bid: float, levels: RangeLevels) -> bool:
    """連続パス仮定で区間が両ストップを覆えば両側到達（バックテスト ambiguous_both 相当）。"""
    lo, hi = min(prev_bid, curr_bid), max(prev_bid, curr_bid)
    return lo <= levels.sell_stop and hi >= levels.buy_stop


def open_session(
    session_date: str,
    *,
    halt: bool,
    levels: RangeLevels | None,
    skip_reason: SkipReason | None = None,
    ts: datetime | None = None,
) -> tuple[BrokerSession, list[BrokerEvent]]:
    """session-open（9:01）: IDLE → ARMED または SKIPPED。

    ``halt=True`` または ``levels is None`` / 明示 skip で SKIPPED（新規仮想発注なし）。
    """
    session = BrokerSession(session_date=session_date, state="IDLE")
    now = ts or datetime.now(tz=LON)
    events: list[BrokerEvent] = []

    if halt:
        nxt = session.evolve(state="SKIPPED", skip_reason="halted")
        events.append(
            _event(
                ts=now,
                reason="halted",
                state_from="IDLE",
                state_to="SKIPPED",
                detail={"skip_reason": "halted"},
            )
        )
        return nxt, events

    if skip_reason is not None:
        nxt = session.evolve(state="SKIPPED", skip_reason=skip_reason)
        events.append(
            _event(
                ts=now,
                reason=skip_reason,
                state_from="IDLE",
                state_to="SKIPPED",
                detail={"skip_reason": skip_reason},
            )
        )
        return nxt, events

    if levels is None:
        nxt = session.evolve(state="SKIPPED", skip_reason="no_range")
        events.append(
            _event(
                ts=now,
                reason="no_range",
                state_from="IDLE",
                state_to="SKIPPED",
                detail={"skip_reason": "no_range"},
            )
        )
        return nxt, events

    nxt = session.evolve(state="ARMED", levels=levels)
    events.append(
        _event(
            ts=now,
            reason="armed",
            state_from="IDLE",
            state_to="ARMED",
            detail={
                "buy_stop": levels.buy_stop,
                "sell_stop": levels.sell_stop,
                "range_high": levels.high,
                "range_low": levels.low,
            },
        )
    )
    return nxt, events


def apply_observation(
    session: BrokerSession,
    obs: TickerObservation,
) -> tuple[BrokerSession, list[BrokerEvent]]:
    """観測 1 点を適用する。終端状態では no-op（イベントなし）。"""
    if session.state in {"CLOSED", "SKIPPED", "CANCELLED", "ANOMALY", "UNCERTAIN", "IDLE"}:
        return session, []

    ts = _ensure_london(obs.ts)
    obs = TickerObservation(ts=ts, bid=obs.bid, ask=obs.ask, status=obs.status)

    # 開場ゲート: OPEN 以外は破棄（滑り母集団に入れない）
    if obs.status != _OPEN_STATUS:
        ev = _event(
            ts=ts,
            reason="market_closed",
            state_from=session.state,
            state_to=session.state,
            obs=obs,
            detail={"status": obs.status},
        )
        return session, [ev]

    if session.state == "ARMED":
        return _apply_armed(session, obs)
    if session.state == "FILLED":
        return _apply_filled(session, obs)
    return session, []


def apply_observations(
    session: BrokerSession,
    observations: Sequence[TickerObservation],
) -> tuple[BrokerSession, list[BrokerEvent]]:
    """観測列を順に適用する（単体テストの主入口）。"""
    all_events: list[BrokerEvent] = []
    cur = session
    for obs in observations:
        cur, evs = apply_observation(cur, obs)
        all_events.extend(evs)
    return cur, all_events


def _apply_armed(
    session: BrokerSession, obs: TickerObservation
) -> tuple[BrokerSession, list[BrokerEvent]]:
    assert session.levels is not None
    levels = session.levels
    ts = obs.ts
    t = ts.time()
    prev = session.last_open_obs
    events: list[BrokerEvent] = []

    # 11:00 境界（timestamp が正。11:00:00.000 以降は fill 不可）
    if t >= CANCEL_TIME:
        gap_ms, _ = _poll_gap(prev, obs)
        # オーダー窓の観測が無い、または欠測で 11:00 跨ぎが判定不能 → 不確定（ORB-6 で確定）
        missing_window = prev is None
        straddling_gap = (
            prev is not None
            and _ensure_london(prev.ts).time() < CANCEL_TIME
            and gap_ms is not None
            and gap_ms > _BOUNDARY_GAP_MS
        )
        if missing_window or straddling_gap:
            reason = "uncertain_no_open_obs" if missing_window else "uncertain_cancel_boundary"
            nxt = session.evolve(
                state="UNCERTAIN",
                uncertain_reason=reason,
                last_open_obs=obs,
            )
            events.append(
                _event(
                    ts=ts,
                    reason=reason,
                    state_from="ARMED",
                    state_to="UNCERTAIN",
                    obs=obs,
                    detail={"poll_gap_ms": gap_ms, "boundary": "11:00"},
                )
            )
            return nxt, events
        nxt = session.evolve(state="CANCELLED", last_open_obs=obs)
        events.append(
            _event(
                ts=ts,
                reason="no_fill",
                state_from="ARMED",
                state_to="CANCELLED",
                obs=obs,
                detail={"cancel_time": CANCEL_TIME.isoformat()},
            )
        )
        return nxt, events

    # 9:00 前の観測は記録のみ（通常 session-open は 9:01）
    if t < ORDER_TIME:
        return session.evolve(last_open_obs=obs), []

    hit_buy = obs.bid >= levels.buy_stop
    hit_sell = obs.bid <= levels.sell_stop
    gap_both = prev is not None and _crossed_both(prev.bid, obs.bid, levels)

    if (hit_buy and hit_sell) or gap_both:
        return _to_both_filled(session, obs, how="armed_both")

    if hit_buy:
        return _to_filled(session, obs, direction="long")
    if hit_sell:
        return _to_filled(session, obs, direction="short")

    return session.evolve(last_open_obs=obs), []


def _to_filled(
    session: BrokerSession,
    obs: TickerObservation,
    *,
    direction: DirectionName,
) -> tuple[BrokerSession, list[BrokerEvent]]:
    assert session.levels is not None
    levels = session.levels
    trigger = levels.buy_stop if direction == "long" else levels.sell_stop
    sl = levels.sell_stop if direction == "long" else levels.buy_stop
    slip = _make_slip(direction=direction, trigger=trigger, obs=obs, prev=session.last_open_obs)
    nxt = session.evolve(
        state="FILLED",
        direction=direction,
        entry_price=obs.bid,
        entry_ts=obs.ts,
        sl_price=sl,
        opposite_cancel_pending=True,
        last_open_obs=obs,
        entry_slip=slip,
    )
    ev = _event(
        ts=obs.ts,
        reason="filled",
        state_from="ARMED",
        state_to="FILLED",
        obs=obs,
        detail={
            "direction": direction,
            "trigger_price": trigger,
            "fill_price": obs.bid,
            "sl_price": sl,
            "slip_pips": slip.slip_pips,
            "spread_pips": slip.spread_pips,
            "poll_gap_ms": slip.poll_gap_ms,
            "price_gap": slip.price_gap,
            "opposite_cancel_pending": True,
        },
    )
    return nxt, [ev]


def _to_both_filled(
    session: BrokerSession,
    obs: TickerObservation,
    *,
    how: str,
) -> tuple[BrokerSession, list[BrokerEvent]]:
    """両建て異常: 即時仮想クローズ + Slack 通知フラグ（§4.2）。

    ARMED からの到達（``how=armed_both``）では direction 未確定のまま
    ``exit_price`` / ``exit_reason`` だけが入る。「エントリーが無いのに exit がある」のは
    両建てを即時クローズして建玉を持たなかった証跡であり、片側 fill 後の
    ``cancel_latency`` 経路とは記録形が異なる。
    """
    assert session.levels is not None
    slip = None
    if session.direction is not None and session.entry_price is not None:
        # 既に片側 fill 済みなら exit 滑りも記録
        trigger = session.sl_price if session.sl_price is not None else obs.bid
        slip = _make_slip(
            direction=session.direction,
            trigger=float(trigger),
            obs=obs,
            prev=session.last_open_obs,
        )
    nxt = session.evolve(
        state="ANOMALY",
        exit_price=obs.bid,
        exit_ts=obs.ts,
        exit_reason="both_filled",
        opposite_cancel_pending=False,
        last_open_obs=obs,
        exit_slip=slip,
    )
    ev = _event(
        ts=obs.ts,
        reason="both_filled",
        state_from=session.state,
        state_to="ANOMALY",
        obs=obs,
        detail={"how": how, "direction": session.direction},
        notify_slack=True,
    )
    return nxt, [ev]


def _apply_filled(
    session: BrokerSession, obs: TickerObservation
) -> tuple[BrokerSession, list[BrokerEvent]]:
    assert session.levels is not None
    assert session.direction is not None
    assert session.sl_price is not None
    ts = obs.ts
    t = ts.time()
    levels = session.levels
    direction = session.direction
    sl = session.sl_price

    # 片割れ取消レイテンシ窓（同一〜次サイクル）: 反対側到達 → both_filled
    if session.opposite_cancel_pending:
        opposite_hit = (direction == "long" and obs.bid <= levels.sell_stop) or (
            direction == "short" and obs.bid >= levels.buy_stop
        )
        if opposite_hit:
            return _to_both_filled(session, obs, how="cancel_latency")
        session = session.evolve(opposite_cancel_pending=False, last_open_obs=obs)
        # 取消確定後、同一観測で SL/EOD も判定する

    # EOD: 16:00 以降の最初の観測 BID（§6.4）
    if t >= EOD_TIME:
        slip = _make_slip(
            direction=direction,
            trigger=session.entry_price or obs.bid,
            obs=obs,
            prev=session.last_open_obs,
        )
        # EOD の「滑り」はエントリ基準ではなく決済値そのもの。exit_slip に観測を残す。
        # 主指標の中止条件は fill/SL 向けだが、記録項目として poll_gap 等を残す。
        nxt = session.evolve(
            state="CLOSED",
            exit_price=obs.bid,
            exit_ts=ts,
            exit_reason="eod",
            last_open_obs=obs,
            exit_slip=slip,
            opposite_cancel_pending=False,
        )
        ev = _event(
            ts=ts,
            reason="eod",
            state_from="FILLED",
            state_to="CLOSED",
            obs=obs,
            detail={
                "exit_price": obs.bid,
                "poll_gap_ms": slip.poll_gap_ms,
                "spread_pips": slip.spread_pips,
            },
        )
        return nxt, [ev]

    hit_sl = (direction == "long" and obs.bid <= sl) or (direction == "short" and obs.bid >= sl)
    if hit_sl:
        slip = _make_slip(direction=direction, trigger=sl, obs=obs, prev=session.last_open_obs)
        nxt = session.evolve(
            state="CLOSED",
            exit_price=obs.bid,
            exit_ts=ts,
            exit_reason="sl",
            last_open_obs=obs,
            exit_slip=slip,
            opposite_cancel_pending=False,
        )
        ev = _event(
            ts=ts,
            reason="sl",
            state_from="FILLED",
            state_to="CLOSED",
            obs=obs,
            detail={
                "sl_price": sl,
                "exit_price": obs.bid,
                "slip_pips": slip.slip_pips,
                "spread_pips": slip.spread_pips,
                "poll_gap_ms": slip.poll_gap_ms,
                "price_gap": slip.price_gap,
            },
        )
        return nxt, [ev]

    return session.evolve(last_open_obs=obs), []


def demo_signal_class(session: BrokerSession) -> DemoSignalClass:
    """仮想ブローカー結果を §8.2 突合用クラスへ写像する。"""
    if session.state == "ANOMALY" and session.exit_reason == "both_filled":
        return "both_filled"
    if session.state == "UNCERTAIN":
        return "uncertain"
    if session.state == "CANCELLED":
        return "no_fill"
    if session.state == "SKIPPED":
        if session.skip_reason == "no_range":
            return "no_range"
        return "skipped_other"
    if session.direction == "long":
        return "long"
    if session.direction == "short":
        return "short"
    return "skipped_other"


def _london_hour_minute(fill_et: str) -> tuple[int, int] | None:
    """``fill_et`` を Europe/London の (hour, minute) にする。

    tz-aware は ``astimezone(LON)``（BST=+1 / GMT=+0 を吸収）。naive と
    ``HH:MM`` / ``HH:MM:SS`` はローカル時計として読む（既存テスト用）。
    JST の 09:00 を London 09:00 と見なさない。
    """
    raw = fill_et.strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        time_part = raw.replace("T", " ").split()[-1]
        bits = time_part.split(":")
        if len(bits) < 2:
            return None
        try:
            return int(bits[0]), int(bits[1])
        except ValueError:
            return None
    local = dt.astimezone(LON) if dt.tzinfo is not None else dt
    return local.hour, local.minute


def _replay_fill_in_cancel_minute(trade: Trade) -> bool:
    """リプレイ約定がオラクルの 11:00 バー（openTime==11:00）由来か。

    オラクル / ORB-2 は ``tt > CANCEL_TIME`` で break するため openTime=11:00 を含む。
    仮想ブローカーは §4.2 どおり ``ts >= 11:00:00`` を fill しない（既知乖離・§8.2.1）。
    """
    hm = _london_hour_minute(trade.fill_et)
    if hm is None:
        return False
    return hm[0] == CANCEL_TIME.hour and hm[1] == CANCEL_TIME.minute


# 注文設置前バー（session-open 前の 09:00 と #307 残置 42 秒窓の 09:01）。
# ORDER_TIME に 09:01 を足して戦略を変えない。09:02 以降は含めない。
_FILL_BEFORE_ORDER_WINDOW: frozenset[tuple[int, int]] = frozenset({(9, 0), (9, 1)})


def replay_fill_before_order_window(trade: Trade) -> bool:
    """リプレイ約定が London 09:00 / 09:01 バー由来か（§8.2.1 / #360）。"""
    hm = _london_hour_minute(trade.fill_et)
    return hm in _FILL_BEFORE_ORDER_WINDOW


def _demo_opposite_direction(demo_cls: DemoSignalClass, direction: int) -> bool:
    """デモが long/short のときだけ、リプレイと逆方向か。"""
    if demo_cls == "short":
        return direction == 1
    if demo_cls == "long":
        return direction == -1
    return False


def is_fill_before_order_window_pair(replay: DayResult, demo: BrokerSession) -> bool:
    """09:00/09:01 fill ↔ デモ ``no_fill`` / 逆方向（§8.2.1 の比較不能クラス）。

    ``UNCERTAIN`` / ``both_filled`` / ``ambiguous_both``（``trade is None``）は含めない。
    """
    if replay.trade is None:
        return False
    demo_cls = demo_signal_class(demo)
    if not replay_fill_before_order_window(replay.trade):
        return False
    return demo_cls == "no_fill" or _demo_opposite_direction(demo_cls, replay.trade.direction)


def signals_match_replay(replay: DayResult, demo: BrokerSession) -> VerifyOutcome:
    """日次一致検証用の三値結果（§8.2 / §8.2.1）。

    - ``match``: 一致（または既知乖離として一致扱い。11:00 バー fill↔デモ no_fill）
    - ``mismatch``: 分類不一致 → ORB-6 が halt 対象に数える
    - ``defer``: 判定保留（``UNCERTAIN``、または 09:00/09:01 バー fill↔デモ
      no_fill/逆方向の ``fill_before_order_window``）→ **不一致に数えない**
    """
    demo_cls = demo_signal_class(demo)

    # 設計書 §8.2.1: UNCERTAIN はリプレイ確定待ち。分類不一致としては数えない。
    if demo_cls == "uncertain":
        return "defer"

    if demo_cls == "both_filled":
        # ambiguous_both ↔ both_filled、および cancel_latency（リプレイは先触れ側 fill±SL）
        if replay.skip_reason == "ambiguous_both":
            return "match"
        if replay.trade is not None:
            return "match"
        return "mismatch"

    if replay.skip_reason == "no_range":
        return "match" if demo_cls == "no_range" else "mismatch"
    if replay.skip_reason == "no_fill":
        return "match" if demo_cls == "no_fill" else "mismatch"
    if replay.skip_reason == "ambiguous_both":
        # both_filled 以外（上で処理済み）は不一致
        return "mismatch"

    if replay.trade is None:
        return "mismatch"

    # 既知乖離: リプレイが 11:00 バーで fill、デモは 11:00:00 以降を取消 → no_fill
    if demo_cls == "no_fill" and _replay_fill_in_cancel_minute(replay.trade):
        return "match"

    # 既知乖離（比較不能）: 09:00/09:01 バー fill ↔ デモ no_fill / 逆方向 → defer
    # 注文はレンジ確定後に乗る。11:00（両方正しい → match）とは別。§8.2.1 / #360。
    if is_fill_before_order_window_pair(replay, demo):
        return "defer"

    if replay.trade.direction == 1:
        return "match" if demo_cls == "long" else "mismatch"
    if replay.trade.direction == -1:
        return "match" if demo_cls == "short" else "mismatch"
    return "mismatch"


def should_count_as_mismatch(replay: DayResult, demo: BrokerSession) -> bool:
    """ORB-6 用: 分類不一致として halt カウントするか（``defer`` / ``match`` は False）。"""
    return signals_match_replay(replay, demo) == "mismatch"
