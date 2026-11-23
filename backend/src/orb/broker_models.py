"""仮想ブローカーの観測・セッション型（クライアント非依存）。

設計書: docs/ai-dev/london-orb-demo-design.md §6.1・§6.3
ORB-4 が Public ticker / status から本型へ写像する。ここでは API クライアントを持たない。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from backend.src.orb.models import RangeLevels

BrokerState = Literal[
    "IDLE",
    "ARMED",
    "FILLED",
    "CLOSED",
    "SKIPPED",
    "CANCELLED",
    "ANOMALY",
    "UNCERTAIN",
]

SkipReason = Literal[
    "no_range",
    "market_holiday",  # 休場カレンダー（config/market_holidays.json）由来。データ無しが正常
    "market_closed",  # API の市場状態が OPEN でない（CLOSE / MAINTENANCE）。休場とは別に数える
    "halted",
    "system_outage",
    "disabled",  # ORB_DEMO_ENABLED=false。マスタースイッチ。機会損失には数えない
]

ExitReason = Literal["sl", "eod", "both_filled"]

DirectionName = Literal["long", "short"]

# §8.2 突合用のシグナル分類（リプレイ側の ambiguous_both と対になる）
DemoSignalClass = Literal[
    "no_range",
    "no_fill",
    "long",
    "short",
    "both_filled",
    "uncertain",
    "skipped_other",
]

# §8.2 一致検証の三値（match / mismatch のみ halt 判定に使う。defer は不一致に数えない）
VerifyOutcome = Literal["match", "mismatch", "defer"]


@dataclass(frozen=True)
class TickerObservation:
    """離散 ticker 観測点。status は開場ゲート用（設計書 §6.1）。"""

    ts: datetime  # timezone-aware（Europe/London 推奨）
    bid: float
    ask: float
    status: str  # "OPEN" / "CLOSE" / "MAINTENANCE" 等


@dataclass(frozen=True)
class SlippageRecord:
    """到達観測点の滑り・スプレッド・欠測（§6.3）。開場ゲート通過分のみ。"""

    trigger_price: float
    observed_bid: float
    slip_pips: float
    spread_pips: float
    poll_gap_ms: int | None
    price_gap: float | None


@dataclass(frozen=True)
class BrokerEvent:
    """状態遷移・稼働イベント（S3 JSONL 1 行の正）。"""

    ts: datetime
    reason: str
    state_from: BrokerState
    state_to: BrokerState
    bid: float | None = None
    ask: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    notify_slack: bool = False

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "reason": self.reason,
            "state_from": self.state_from,
            "state_to": self.state_to,
            "bid": self.bid,
            "ask": self.ask,
            "detail": self.detail,
            "notify_slack": self.notify_slack,
        }


@dataclass(frozen=True)
class BrokerSession:
    """1 セッションの仮想ブローカー状態（DynamoDB レコードの正）。"""

    session_date: str  # Europe/London の暦日 YYYY-MM-DD
    state: BrokerState = "IDLE"
    levels: RangeLevels | None = None
    direction: DirectionName | None = None
    entry_price: float | None = None
    entry_ts: datetime | None = None
    sl_price: float | None = None
    exit_price: float | None = None
    exit_ts: datetime | None = None
    exit_reason: ExitReason | None = None
    skip_reason: SkipReason | None = None
    # fill 直後の 1 観測サイクル: 反対側エントリー未取消として both_filled を検知する
    opposite_cancel_pending: bool = False
    last_open_obs: TickerObservation | None = None
    entry_slip: SlippageRecord | None = None
    exit_slip: SlippageRecord | None = None
    uncertain_reason: str | None = None

    def evolve(self, **changes: Any) -> BrokerSession:
        return replace(self, **changes)
