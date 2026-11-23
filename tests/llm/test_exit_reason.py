"""tests/llm/test_exit_reason.py — 決済理由の判定（Issue #280, §6.2）。

🔴 このファイルが固定する契約:

2026-08-10 のノートは 4:30 cron の強制決済を「計画の利確目標に到達する前に決済売却を
実行」と書き、「利確水準まで待つメリット・デメリットの事後検証が必要」という**存在しない
裁量判断**を反省した。決済理由が LLM に渡っていなかったため、空白を埋めて創作したもの。

判定は価格や時刻ではなく **ID 突合** を一次証拠にする:
  - IFD-OCO は ``OPEN 約定の orderId == rootOrderId``（付録B executions_api_spec.md）
  - Step2 はその ``rootOrderId`` を cancel する
  → OPEN.order_id ∈ step2_cancelled ⇒ 決済 OCO は消えた ⇒ OCO は発火し得ない

🔴 逆は成り立たない。``step2_cancelled`` に無いことは「OCO が発火した」を意味せず、
「4:30 時点で取り消す対象が無かった」だけ。Step3 の成行 close で閉じた建玉も該当するため、
``step3_closed`` も time_stop の正の証拠として扱う。ここを落とすと「Step3 で決済したのに
価格が計画 TP に近いので利確と断定」という逆方向の捏造が起きる。
"""

from __future__ import annotations

import datetime

import pytest

from backend.src.engine.trading_day import JST
from backend.src.llm.trade_note import (
    CloseoutEvidence,
    DailyCloseout,
    Execution,
    ExitReason,
    TradeNoteInput,
    TradePlan,
    determine_exit_reason,
    exit_reasons_for,
    fetch_weekly_executions,
    position_summaries,
)

# ---------------------------------------------------------------------------
# 2026-08-10（Day 6）の事象を再現した架空データ（公開版では価格・ID・時刻・損益を置き換えている）。
# ---------------------------------------------------------------------------

_ROOT = "1000401"      # エントリー orderId = rootOrderId
_POSITION = "700201"
_ENTRY = "150.00"
_TP = "150.814"
_SL = "149.322"


def _open_exec(*, order_id: str = _ROOT, price: str = _ENTRY, side: str = "BUY") -> Execution:
    return Execution(
        execution_id="500101",
        order_id=order_id,
        symbol="USD_JPY",
        side=side,
        price=price,
        size="100",
        timestamp="2026-08-10T12:00:00.000Z",  # 21:00 JST
        settle_type="OPEN",
        position_id=_POSITION,
        loss_gain="0",
        fee="-0.300",
        settled_swap="0",
    )


def _close_exec(*, price: str, order_id: str = "1000402", side: str = "SELL") -> Execution:
    return Execution(
        execution_id="500102",
        order_id=order_id,
        symbol="USD_JPY",
        side=side,
        price=price,
        size="100",
        timestamp="2026-08-10T19:30:00.000Z",  # 翌 04:30 JST
        settle_type="CLOSE",
        position_id=_POSITION,
        loss_gain="40.0",
        fee="-0.300",
        settled_swap="0",
    )


def _plan(*, tp: str = _TP, sl: str = _SL, direction: str = "LONG") -> TradePlan:
    return TradePlan(
        direction=direction,
        order_type="LIMIT",
        entry_price=_ENTRY,
        stop_loss=sl,
        take_profit=tp,
        lot="100",
        sl_profile="wide",
        rr_target="1.2",
        trade_date="2026-08-10(月)",
        status="ok",
    )


#: 「plan 引数を省略した」と「plan=None を明示した」を区別する番兵。
#: 既定値を None にすると、None を渡すテストが既定 plan に差し替わって通ってしまう。
_DEFAULT = object()


def _reason(
    *,
    close_price: str,
    evidence: CloseoutEvidence | None,
    plan: TradePlan | None | object = _DEFAULT,
    side: str = "BUY",
) -> ExitReason:
    close_side = "SELL" if side == "BUY" else "BUY"
    executions = [
        _open_exec(side=side),
        _close_exec(price=close_price, side=close_side),
    ]
    summaries = position_summaries(executions)
    assert len(summaries) == 1
    resolved = _plan() if plan is _DEFAULT else plan
    assert resolved is None or isinstance(resolved, TradePlan)
    return determine_exit_reason(summaries[0], resolved, evidence)


# ---------------------------------------------------------------------------
# 一次判定: ID 突合
# ---------------------------------------------------------------------------

class TestIdEvidence:
    def test_cancelled_root_is_time_stop(self) -> None:
        """2026-08-10 相当: OPEN の orderId が step2_cancelled にある → 時間切れ。

        これが本 Issue の本体。実際のログでも step2_cancelled にはエントリーの注文 ID が入っており、
        OPEN 約定の orderId と一致する。
        """
        evidence = CloseoutEvidence(
            cancelled_root_order_ids=frozenset({_ROOT}),
            closed_position_ids=frozenset({_POSITION}),
        )
        assert _reason(close_price="150.40", evidence=evidence) == "time_stop"

    def test_step3_closed_is_time_stop_even_when_price_matches_tp(self) -> None:
        """🔴 step2 が空でも step3 が閉じた建玉は時間切れ。価格が TP でも利確と断定しない。

        決済 OCO が先に消えて建玉だけ残り、Step3 の成行 close で閉じる経路がある。
        step2 の空集合を「OCO が発火した」と読むと、ここで逆方向の捏造が起きる。
        """
        evidence = CloseoutEvidence(
            cancelled_root_order_ids=frozenset(),
            closed_position_ids=frozenset({_POSITION}),
        )
        assert _reason(close_price=_TP, evidence=evidence) == "time_stop"

    def test_evidence_none_is_unknown(self) -> None:
        """判定材料が無い（step2/step3 が None で保存された日）→ 断定しない。"""
        assert _reason(close_price=_TP, evidence=None) == "unknown"

    def test_position_in_unknown_set_is_unknown(self) -> None:
        """friday 集約で判定材料が欠けた日の建玉 → 断定しない。"""
        evidence = CloseoutEvidence(unknown_position_ids=frozenset({_POSITION}))
        assert _reason(close_price=_TP, evidence=evidence) == "unknown"


class TestVerificationFailed:
    """🔴 決済後の建玉再取得に失敗した回（fail-closed）。

    `cancel_first` は ``ClosePositionsOutcome(verification_failed=True)`` を返し
    ``closed_ids`` は **空のまま**になる。この空を「cancel-first 未介入」と読むと
    OCO 発火に化け、成行の約定価格が計画 TP/SL に達していれば断定してしまう。
    本 PR が潰した逆方向の捏造と同型。
    """

    def test_empty_step2_and_step3_does_not_become_take_profit(self) -> None:
        """step2 空 + step3 空 + 決済価格 = 計画 TP でも利確と断定しない。"""
        evidence = CloseoutEvidence(
            cancelled_root_order_ids=frozenset(),
            closed_position_ids=frozenset(),
            verification_failed=True,
        )
        assert _reason(close_price=_TP, evidence=evidence) == "unknown"

    def test_empty_step2_and_step3_does_not_become_stop_loss(self) -> None:
        evidence = CloseoutEvidence(verification_failed=True)
        assert _reason(close_price=_SL, evidence=evidence) == "unknown"

    def test_positive_id_evidence_still_wins(self) -> None:
        """step2 の取消は実際に成功しているので time_stop は保たれる。

        検証不能で潰すのは **OCO 発火への分岐だけ**。正の ID 証拠まで打ち消すと、
        時間切れと分かっている建玉まで unknown に落ちて記録が痩せる。
        """
        evidence = CloseoutEvidence(
            cancelled_root_order_ids=frozenset({_ROOT}), verification_failed=True
        )
        assert _reason(close_price=_TP, evidence=evidence) == "time_stop"

    def test_persisted_log_restores_the_flag(self) -> None:
        """🔴 土曜週次でも塞がれていること。

        永続化ログの ``step3_closed`` は検証失敗時も **空 list** で保存されるため、
        list 検査だけでは素通りする。フラグを復元しないと週次で同じ捏造が起きる。
        """
        from unittest.mock import patch

        from backend.src.llm.trade_note import _load_persisted_closeout

        log = {
            "executions": [],
            "step2_cancelled": [],
            "step3_closed": [],
            "step3_verification_failed": True,
        }
        with patch(
            "backend.src.persistence.s3_logger.load_closeout_log", return_value=log
        ):
            daily = _load_persisted_closeout(datetime.date(2026, 8, 10))

        assert daily is not None
        assert daily.evidence is not None
        assert daily.evidence.verification_failed is True

    def test_weekly_merge_limits_unknown_to_that_day(self) -> None:
        """検証失敗はその日の建玉だけを unknown 化し、他日を巻き添えにしない。"""
        now = datetime.datetime(2026, 6, 27, 4, 35, tzinfo=JST)
        other_position = "pos-other"

        def loader(d: datetime.date) -> DailyCloseout | None:
            if d == datetime.date(2026, 6, 25):
                return DailyCloseout(
                    [_open_exec()],  # positionId = _POSITION
                    evidence=CloseoutEvidence(verification_failed=True),
                )
            ex = Execution(
                execution_id=f"ex-{d.isoformat()}",
                order_id="root-other",
                symbol="USD_JPY",
                side="BUY",
                price="149.23",
                size="100",
                timestamp="2026-06-24T11:30:00.000Z",
                settle_type="OPEN",
                position_id=other_position,
            )
            return DailyCloseout([ex], evidence=CloseoutEvidence())

        from unittest.mock import MagicMock

        client = MagicMock()
        client.get_latest_executions.return_value = []
        client.get_executions.return_value = []

        result = fetch_weekly_executions(
            client, now,
            prev_trade_date=datetime.date(2026, 6, 26),
            load_daily_closeout=loader,
        )

        assert result.evidence is not None
        assert result.evidence.unknown_position_ids == frozenset({_POSITION})
        assert other_position not in result.evidence.unknown_position_ids


# ---------------------------------------------------------------------------
# 二次判定: 注文種別のセマンティクス（許容誤差を置かない）
# ---------------------------------------------------------------------------

_OCO_FIRED = CloseoutEvidence(
    cancelled_root_order_ids=frozenset(), closed_position_ids=frozenset()
)


class TestOcoLegDiscrimination:
    def test_long_fill_at_or_above_tp_is_take_profit(self) -> None:
        """LONG の TP は LIMIT。計画値ちょうど、およびそれより有利な側で約定する。"""
        assert _reason(close_price=_TP, evidence=_OCO_FIRED) == "take_profit"
        assert _reason(close_price="150.83", evidence=_OCO_FIRED) == "take_profit"

    def test_long_fill_at_or_below_sl_is_stop_loss(self) -> None:
        """LONG の SL は STOP。スリッページで計画値より下に滑っても損切りと判定する。"""
        assert _reason(close_price=_SL, evidence=_OCO_FIRED) == "stop_loss"
        assert _reason(close_price="149.28", evidence=_OCO_FIRED) == "stop_loss"

    def test_long_fill_between_sl_and_tp_is_unknown(self) -> None:
        """🔴 SL と TP の間の価格はどちらの脚でも約定し得ない → 断定しない。

        距離の閾値を置くと「TP に近いから利確」と作為的に切り分けてしまう。
        """
        assert _reason(close_price="150.40", evidence=_OCO_FIRED) == "unknown"

    def test_short_legs_are_mirrored(self) -> None:
        """SHORT は TP < Entry < SL（§3.4 価格序列）。判定も反転する。"""
        short_plan = _plan(tp="149.18", sl="150.68", direction="SHORT")
        assert _reason(
            close_price="149.18", evidence=_OCO_FIRED, plan=short_plan, side="SELL"
        ) == "take_profit"
        assert _reason(
            close_price="150.73", evidence=_OCO_FIRED, plan=short_plan, side="SELL"
        ) == "stop_loss"
        assert _reason(
            close_price="149.83", evidence=_OCO_FIRED, plan=short_plan, side="SELL"
        ) == "unknown"

    @pytest.mark.parametrize(
        "plan",
        [
            None,
            TradePlan(),                                        # 計画値が空
            TradePlan(stop_loss=_SL, take_profit=_TP, log_available=False),  # ログ不明
            TradePlan(stop_loss="", take_profit=_TP),           # SL 欠落
            TradePlan(stop_loss="---", take_profit=_TP),        # 解釈不能
            TradePlan(stop_loss=_TP, take_profit=_SL),          # 価格序列が壊れている
        ],
    )
    def test_unusable_plan_is_unknown(self, plan: TradePlan | None) -> None:
        """計画値が使えないなら OCO の脚は特定できない → 断定しない。"""
        assert _reason(close_price=_TP, evidence=_OCO_FIRED, plan=plan) == "unknown"


# ---------------------------------------------------------------------------
# プロンプトへの反映
# ---------------------------------------------------------------------------

class TestPromptSection:
    def _prompt(self, evidence: CloseoutEvidence | None) -> str:
        from backend.src.llm.trade_note import _build_note_prompt

        inp = TradeNoteInput(
            decision_plan="押し目 LONG",
            executions=[_open_exec(), _close_exec(price="150.40")],
            now_jst=datetime.datetime(2026, 8, 11, 4, 30, tzinfo=JST),
            plans=(_plan(),),
            closeout_evidence=evidence,
        )
        _, user = _build_note_prompt(inp)
        return user

    def test_time_stop_is_labelled_as_forced_exit(self) -> None:
        """時間切れは「裁量判断ではない」と明示されて渡る（本 Issue の目的）。"""
        user = self._prompt(
            CloseoutEvidence(cancelled_root_order_ids=frozenset({_ROOT}))
        )
        assert "## 決済理由" in user
        assert f"positionId={_POSITION}" in user
        assert "時間切れ手仕舞い" in user
        assert "裁量判断ではない" in user

    def test_unknown_is_labelled_as_undecidable(self) -> None:
        user = self._prompt(None)
        assert "判定不能" in user

    def test_system_prompt_forbids_inventing_discretion_and_ratios(self) -> None:
        """system 側に「裁量判断の捏造禁止」「比率の自前計算禁止」があること（DoD）。"""
        from backend.src.llm.trade_note import _build_note_prompt

        system, _ = _build_note_prompt(
            TradeNoteInput(
                decision_plan="x",
                executions=[],
                now_jst=datetime.datetime(2026, 8, 11, 4, 30, tzinfo=JST),
            )
        )
        assert "決済理由は「## 決済理由」の値のみを使う" in system
        assert "存在しない裁量判断があったかのように書かない" in system
        assert "比率・割合を自分で計算しない" in system


# ---------------------------------------------------------------------------
# friday モード: 日をまたいだ併合
# ---------------------------------------------------------------------------

class TestWeeklyEvidenceMerge:
    def _client(self) -> object:
        from unittest.mock import MagicMock

        client = MagicMock()
        client.get_latest_executions.return_value = []
        client.get_executions.return_value = []
        return client

    def test_day_without_evidence_marks_its_positions_unknown(self) -> None:
        """🔴 step2/step3 が記録されていない日の建玉は unknown に落ちる。

        Step3 失敗日は両方 None で保存される。これを空集合として併合すると
        「取り消し対象に無い＝OCO が発火した」と誤断定する。
        """
        now = datetime.datetime(2026, 6, 27, 4, 35, tzinfo=JST)  # 土曜

        def loader(d: datetime.date) -> DailyCloseout | None:
            if d == datetime.date(2026, 6, 25):
                # 判定材料なしの日（Step3 失敗）
                return DailyCloseout([_open_exec()], evidence=None)
            return DailyCloseout(
                [],
                evidence=CloseoutEvidence(cancelled_root_order_ids=frozenset({"other"})),
            )

        result = fetch_weekly_executions(
            self._client(), now,
            prev_trade_date=datetime.date(2026, 6, 26),
            load_daily_closeout=loader,
        )

        assert result.evidence is not None
        assert _POSITION in result.evidence.unknown_position_ids
        assert "other" in result.evidence.cancelled_root_order_ids

    def test_live_day_evidence_is_merged(self) -> None:
        """最新取引日（ライブ取得分）の証拠も併合される。"""
        now = datetime.datetime(2026, 6, 27, 4, 35, tzinfo=JST)

        result = fetch_weekly_executions(
            self._client(), now,
            prev_trade_date=datetime.date(2026, 6, 26),
            load_daily_closeout=lambda d: DailyCloseout([]),
            live_evidence=CloseoutEvidence(
                cancelled_root_order_ids=frozenset({_ROOT}),
                closed_position_ids=frozenset({_POSITION}),
            ),
        )

        assert result.evidence is not None
        assert result.evidence.cancelled_root_order_ids == frozenset({_ROOT})
        assert result.evidence.closed_position_ids == frozenset({_POSITION})


# ---------------------------------------------------------------------------
# 計画値の紐付け（friday は日をまたぐ）
# ---------------------------------------------------------------------------

def test_plan_is_matched_by_open_trade_date() -> None:
    """建玉の取引日 = 新規約定の JST 日付。CLOSE の日付で引くと 1 日ずれる。

    Day 6 は OPEN 08-10 21:00 / CLOSE 08-11 04:30。CLOSE 基準だと 08-11 の計画値
    （存在しない）を探しに行き、他日の SL/TP を当てる余地が生まれる。
    """
    inp = TradeNoteInput(
        decision_plan="x",
        executions=[_open_exec(), _close_exec(price=_TP)],
        now_jst=datetime.datetime(2026, 8, 11, 4, 30, tzinfo=JST),
        plans=(
            _plan(),                                     # 2026-08-10
            TradePlan(trade_date="2026-08-11(火)", stop_loss="1.0", take_profit="2.0"),
        ),
        closeout_evidence=_OCO_FIRED,
    )
    pairs = exit_reasons_for(inp)
    assert [r for _, r in pairs] == ["take_profit"]


def test_unmatched_trade_date_is_unknown() -> None:
    """該当取引日の計画値が無ければ、他日の値を当てずに unknown に落とす。"""
    inp = TradeNoteInput(
        decision_plan="x",
        executions=[_open_exec(), _close_exec(price=_TP)],
        now_jst=datetime.datetime(2026, 8, 11, 4, 30, tzinfo=JST),
        plans=(TradePlan(trade_date="2026-08-07(金)", stop_loss=_SL, take_profit=_TP),),
        closeout_evidence=_OCO_FIRED,
    )
    assert [r for _, r in exit_reasons_for(inp)] == ["unknown"]
