"""トレードノート生成（§6.2, E14）。

- 火〜金 4:30: ``latestExecutions`` で銘柄の最新約定を取り、前営業日 20:00 JST 以降で絞る
- 土曜 4:30（friday モード）: 当日分をライブ取得し、それ以前は永続化済み日次ログから集約
- LLM へ「狙撃プラン」「約定結果」を渡し Markdown ノートを生成する
- MVP では戦略パラメータの自動変更を行わない（§6.2, AGENTS.md #5）

🔴 約定取得の設計根拠（2026-08-05 の事故・付録B docs/spikes/executions_api_spec.md）

FX API の約定系 GET は 2 本しかなく、どちらも**日付範囲で問い合わせられない**
（出典: https://api.coin.z.com/fxdocs/・2026-08-05 参照）:

- ``executions``: ``orderId`` か ``executionId`` のいずれか 1 つのみ。``symbol`` /
  ``from`` / ``to`` / ``date`` は**存在しない**。旧実装はこれらを送り ERR-5106 を踏んでいた。
- ``latestExecutions``: ``symbol`` 必須 + ``count``（既定＝最大 100）。
  取得範囲は「**直近1日分から最新100件**」。``orderId`` は**存在しない**。

さらに IFD-OCO はエントリー（``settleType=OPEN``）と決済（``CLOSE``）が**別 orderId**
で約定するため、DynamoDB に保存されたエントリーの ``rootOrderId`` を辿るだけでは
決済約定にも実損益にも永久に到達しない。よって**主取得は ``latestExecutions``**
（OPEN/CLOSE 双方が返る唯一の経路）とし、``executions?orderId=`` は補完に留める。
"""

from __future__ import annotations

import datetime
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol

import httpx
import structlog

from backend.src.clients.gmo_client import MAX_LATEST_EXECUTIONS_COUNT
from backend.src.engine.trading_day import JST, jst_dated_time_label
from backend.src.llm.client import DEFAULT_NIGHT_MODEL
from backend.src.validation.day_skip_guards import USD_JPY_PIP
from config.market_calendar import is_market_holiday

_log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# 出力上限。**平日と friday で必要量が桁違い**なので分ける（#309）。
# 平日は 1 建玉分の本文で実測 401〜520 tokens。friday は「取引計画テーブル ×5営業日 +
# 建玉ごとの詳細 + 自己評価 + 週次総括」を出すため、建玉 2 件で 2048 に到達し
# 本文が途中で切れた（2026-08-15 実測）。
#
# 🔴 friday を無闇に上げないこと。ただし **理由は「証跡ごと失われる」ではない**（#339）。
# #311 以降、約定明細・損益・exit_reasons（税務証跡 §5.2）は LLM 呼び出しの **前** に
# S3 へ書かれる（`closeout_batch._run` の note_pending 書き込み）。ここでタイムアウト
# しても失われるのは **ノート本文だけ** で、再生成できる。#245 型の永久欠損ではない。
#
# 上げるときに効く制約は 2 つある（#351 で時計側を書き分け）:
#   1. 時計 — 529 型（短い retry-after + 再生成 1 回）は Lambda 120 秒に収まる。
#      ハング×(max_retries+1) は 90×2=180 秒で 120 を超え得る。そのときは
#      SDK より Lambda が先に落ち、#311 の note_pending と #313 watchdog に頼る。
#      **「非LLM < 30s なら SDK が必ず先に諦める」はハング+リトライでは不成立**。
#      実測の非LLM区間は 2.9〜5.1 秒（下記参照日・n=10）。529 型の余裕はここに出る。
#   2. トークン — 実測スループットは約 85〜94 tokens/秒（friday: 1798 tokens/19.2 秒・
#      1897 tokens/22.3 秒）。4096 に張り付いても約 48 秒で、529 型の時計には届かない。
#
# つまり **成功生成では先に効くのは max_tokens であって Lambda の時計ではない**。
# 実際、60 日で唯一のノート事故（2026-08-15・output_tokens=2048 ちょうど）は
# タイムアウトではなく打ち切りだった。上限到達は is_complete=False で検知できる
# （#309）ので、足りなければ実測して上げてよい。判断に使う実測値は
# `closeout_phase_elapsed`（非LLM区間・529 型）と `trade_note_generated` の
# output_tokens / max_tokens 比、ハング死は `note_pending` / #313（#339 / #351）。
# 出典: CloudWatch 実測（closeout-batch・2026-07-30〜08-28・2026-08-29 参照）。
_MAX_TOKENS_WEEKDAY = 2048
_MAX_TOKENS_FRIDAY = 4096

# Anthropic SDK のリクエストタイムアウト（秒）。
# 🔴 **明示しないと SDK 既定の 600 秒**になり、closeout Lambda の 120 秒を大きく超える。
# 1 試行あたりの待ちを 90 秒に頭打ちし、529 型の再試行（下記 `_SDK_MAX_RETRIES`）を
# 120 秒予算に収める。ハング×(retries+1) は 90×2=180 秒で 120 を超え得る。
# そのときは Lambda が先に落ち、ノートも Slack も出ず `note_pending` が残る。
# それは #311 証跡先行 + #313 watchdog で拾う前提であり、**「非LLM < 30s なら
# SDK が必ず先に諦める」とは書かない**（#351）。
# 1 試行が 90 秒で切れた場合は既存の except が fallback ノート
# （is_complete=False）に倒すので通知経路に乗る。
#
# 値の根拠: 実測スループット約 85〜94 tokens/秒 で friday 上限 4096 は約 48 秒
# （2026-08-29 CloudWatch 参照・上の _MAX_TOKENS_* 参照）。529 型ならこの生成 1 回
# + retry-after 数秒 + 非LLM（実測 2.9〜5.1 秒）が 120 に収まる。timeout 90 を
# 下げて hang×retries を 120 に収めない（ロック 1）。
#
# この値と `lambda_timeout_seconds` はセットで見直すこと。529 型の監視値は
# closeout_batch の `closeout_phase_elapsed.non_llm_seconds`。ハング死は
# `note_pending` / #313。
#
# 🔴 float を渡すと connect にも同じ値が入り、SDK 既定の connect=5 秒より**緩くなる**
# （実測: timeout=90.0 → Timeout(timeout=90.0) で 4 要素すべて 90 秒）。
# 待ちたいのは生成（read）であって接続確立ではないため connect は既定の 5 秒に据える。
_REQUEST_TIMEOUT_SECONDS = 90.0
_REQUEST_TIMEOUT = httpx.Timeout(_REQUEST_TIMEOUT_SECONDS, connect=5.0)

# SDK 再試行回数（#351）。529 overloaded 等の短命な一時障害を 1 回だけ拾う。
# 🔴 **2（SDK 既定）には戻さない**。ハング時の最悪待ちは timeout × (max_retries+1) で、
# 既定 2 だと 90×3=270 秒まで伸びる。1 なら 90×2=180 秒。
# この積は Lambda 120 秒を超え得るが、**それを理由に timeout 90 を下げない**
# （#339 の 30 秒不変条件＝非LLM区間の余裕。#341 実測 Duration p95 26.4s /
# 非LLM 2.9〜5.1s）。ハングしても #311 の証跡先行で約定明細は残る。
# 529 の再試行は SDK が `retry-after` を尊重して数秒で終わる（独自 cap は置かない）。
# 出典: https://docs.anthropic.com/en/api/errors （2026-09-05 参照）
# / https://platform.claude.com/docs/en/api/sdks/python#retries （2026-09-05 参照）。
# 判断バッチ（llm/client.py）は従来どおり max_retries=0。ここだけ 1。
_SDK_MAX_RETRIES = 1

# 正常終了を表す stop_reason。**allowlist**（未知の値は不完全に倒す）。
# Anthropic API は max_tokens 到達も safety refusal も例外ではなく
# HTTP 200 + stop_reason で返すため、検査しないと打ち切られた本文が
# 「正常完了」として保存される（#309）。判断経路の llm/client.py が
# refusal を検査しているのと同じ規律をノート経路にも適用する。
_COMPLETE_STOP_REASONS = frozenset({"end_turn", "stop_sequence"})

# 平日時間窓: 前営業日 20:00 JST から現在まで（§6.2 Ver 3.6）
_WINDOW_START_HOUR = 20

# latestExecutions の取得範囲は「直近1日分から最新100件」。窓がこれを超えると
# 取りこぼしが起き得るため tripwire として警告する（公式仕様・上記出典）。
_LATEST_EXECUTIONS_LOOKBACK = datetime.timedelta(days=1)
_LATEST_COUNT = MAX_LATEST_EXECUTIONS_COUNT

# friday モードで集約する取引日数（§6.2 週次総括）
_WEEKLY_TRADE_DAYS = 5


# ---------------------------------------------------------------------------
# データ型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Execution:
    """約定レコード（GMO ``/executions``・``/latestExecutions`` 共通）。

    フィールド名は公式レスポンス（出典: https://api.coin.z.com/fxdocs/・2026-08-05 参照）
    に 1:1 対応する。``settle_type`` / ``loss_gain`` / ``fee`` / ``settled_swap`` は
    実損益レポート（#25 DoD）と税務証跡（§5.2）に必須。
    """

    execution_id: str
    order_id: str
    symbol: str
    side: str          # "BUY" / "SELL"
    price: str
    size: str
    timestamp: str     # ISO 8601（UTC・末尾 Z）
    settle_type: str = ""    # "OPEN"（新規）/ "CLOSE"（決済）
    position_id: str = ""
    loss_gain: str = ""      # 決済損益（CLOSE のみ非ゼロ）
    fee: str = ""            # 取引手数料（負値）
    settled_swap: str = ""   # 決済スワップ
    amount: str = ""         # 受渡金額


@dataclass(frozen=True)
class ExecutionFetch:
    """約定取得の結果と、取得できなかった範囲。

    ``gaps`` は「データが無い」ではなく「**見えていない**」区間を表す。両者を混同すると
    2026-08-05 のように「約定 0 件」を「約定しなかった」と解釈したノートが生成される。
    LLM プロンプトへ明示的に渡し、断定を防ぐ（本モジュール冒頭の設計根拠を参照）。
    """

    executions: list[Execution] = field(default_factory=list)
    gaps: tuple[str, ...] = ()
    #: 建玉がどう閉じたかの一次証拠（friday モードは日をまたいで併合済み）。
    #: ``None`` = 材料なし＝全建玉 ``unknown``。
    evidence: CloseoutEvidence | None = None


#: 建玉の決済理由（Issue #280）。``backend/src/orb/broker_models.py`` の同名 Literal とは
#: **別物**（あちらは ORB サブシステムの ``sl`` / ``eod`` / ``both_filled``）。混同しないこと。
ExitReason = Literal["time_stop", "take_profit", "stop_loss", "unknown"]

#: LLM に渡す決済理由の日本語ラベル。``time_stop`` の説明文が本 Issue の本体:
#: これを渡さないと LLM が「利確水準まで待つべきだった」等の**存在しない裁量判断**を書く。
EXIT_REASON_LABELS: dict[ExitReason, str] = {
    # 文言は cancel-first のどの経路（Step2 の取消 / Step3 の成行 close）で閉じた場合でも
    # 正しくなるようにする。主眼は「裁量判断ではない」を LLM に伝えること。
    "time_stop": (
        "時間切れ手仕舞い"
        "（保有窓 HOLD_HOURS の満了により 4:30 の cancel-first が決済した。"
        "**設計どおりの強制決済であり、裁量判断ではない**）"
    ),
    "take_profit": "利確 OCO 発火（計画 TP に到達して決済された）",
    "stop_loss": "損切り OCO 発火（計画 SL に到達して決済された）",
    "unknown": "**判定不能**（決済理由を推測して書かないこと）",
}


@dataclass(frozen=True)
class CloseoutEvidence:
    """cancel-first が残した「建玉をどう閉じたか」の一次証拠（Issue #280）。

    🔴 **価格・時刻ではなく ID で判定する**ための材料。IFD-OCO は
    ``OPEN 約定の orderId == rootOrderId`` であり（付録B `docs/spikes/executions_api_spec.md`）、
    Step2 はその ``rootOrderId`` を cancel する。よって

        OPEN 約定の order_id ∈ cancelled_root_order_ids  →  決済 OCO は取り消された
                                                          →  OCO は発火し得ない ＝ 時間切れ

    が許容誤差なしで成り立つ。2026-08-10 のノートはこの材料が渡っていなかったため、
    時間切れ手仕舞いを「計画 TP まで待たずに決済した」という裁量判断として記述した。

    🔴 ``cancelled_root_order_ids`` に**無いこと**は「OCO が発火した」を意味しない。
    「4:30 時点で取り消す対象の CLOSE 注文が無かった」だけで、OCO が先に消えて建玉だけ
    残り Step3 の成行 close で閉じた場合も該当する。そのため ``closed_position_ids``
    （Step3 が実際に成行決済した建玉）も **time_stop の正の証拠**として併せて持つ。
    """

    #: Step2 が取り消した決済 OCO の親注文 ID（= エントリーの rootOrderId）。
    cancelled_root_order_ids: frozenset[str] = frozenset()
    #: Step3（成行 close）で決済した建玉 ID。
    closed_position_ids: frozenset[str] = frozenset()
    #: 判定材料が欠けている建玉 ID（friday モードで、その日のログに step2/step3 が
    #: 記録されていなかった場合）。**空集合と混同すると「OCO 発火」と誤断定する**。
    unknown_position_ids: frozenset[str] = frozenset()
    #: 🔴 Step3 の決済後に建玉を再取得できず、**何が閉じたか検証できなかった**回
    #: （`cancel_first` は fail-closed で ``closed_ids=[]`` を返す）。
    #: このとき ``closed_position_ids`` の空は「1件も閉じなかった」ではなく**不明**。
    #: step2 の取消は実際に成功しているので time_stop の正の証拠として残しつつ、
    #: **OCO 発火の分岐だけを禁止**する（空を「cancel-first 未介入」と読ませない）。
    verification_failed: bool = False


@dataclass(frozen=True)
class DailyCloseout:
    """永続化済み手仕舞いログ 1 日分（friday モードの復元単位・Issue #280）。

    ``evidence`` が ``None`` なのは、その日のログに ``step2_cancelled`` /
    ``step3_closed`` が記録されていない場合（Step3 失敗時は ``None`` で保存される・
    `closeout_batch` 参照）。その日の建玉は決済理由を **判定不能** として扱う。
    """

    executions: list[Execution] = field(default_factory=list)
    evidence: CloseoutEvidence | None = None


@dataclass(frozen=True)
class PnlSummary:
    """約定から決定論的に算出した損益サマリ（LLM に計算させない）。"""

    open_count: int = 0
    close_count: int = 0
    loss_gain_total: Decimal = Decimal(0)
    fee_total: Decimal = Decimal(0)
    settled_swap_total: Decimal = Decimal(0)
    unparsed_fields: tuple[str, ...] = ()

    @property
    def realized_total(self) -> Decimal:
        """実現損益 = 決済損益 + 取引手数料（負値）+ 決済スワップ。"""
        return self.loss_gain_total + self.fee_total + self.settled_swap_total


@dataclass(frozen=True)
class SkipIndicator:
    """指標 day-skip の引き金になった経済指標 1 件（#333）。

    判断ログ ``skip_indicators`` の各要素をそのまま運ぶ。
    値は**無加工**（``TradePlan`` 既存方針。要約・整形・再計算をしない）。
    """

    name: str = ""
    time_jst: str = ""
    currency: str = ""
    #: ``YYYY-MM-DD``。判断ログに無い過去分は空のまま、描画は ``HH:MM JST``（#374）。
    date_jst: str = ""


@dataclass(frozen=True)
class TradePlan:
    """判断バッチが決めた**計画値**（Issue #249）。

    🔴 これを構造化して渡さないと、LLM は ``rationale``（散文）から SL/TP を**推測**する。
    以前のノートでは rationale 中のピボット値（S1）を SL と誤認し、
    リスク幅を実際の半分ほどに書いた。
    損益を Python 算出値で渡す方針（#244）の**計画値側の積み残し**。

    値は判断ログの文字列表現をそのまま保持する（推測・再フォーマットをしない）。
    """

    direction: str = ""       # "LONG" / "SHORT"
    order_type: str = ""      # "LIMIT" / "STOP"
    entry_price: str = ""
    stop_loss: str = ""
    take_profit: str = ""
    lot: str = ""
    sl_profile: str = ""
    rr_target: str = ""
    #: この計画値がどの取引日のものか（``YYYY-MM-DD(曜)``）。friday モードでは
    #: 取引日ごとに 1 件並ぶため、**どの日の値かを明示しないと**
    #: LLM が他日の約定にこの SL/TP を当てはめてしまう。
    trade_date: str = ""
    #: 判断ログの status（``ok`` / ``no_entry``）。
    #: 🔴 「その日は発注しなかった（no_entry）」と「判断ログを読めなかった（unavailable）」は
    #: 別物。前者は事実、後者は**不明**。混同するとノートが断定を誤る（#244 と同じ規律）。
    status: str = ""
    #: 判断ログを取得できたか。False なら計画値の有無自体が不明。
    log_available: bool = True
    #: 見送り理由（#318）。``status == "no_entry"`` の日にだけ入る。
    #: 判断ログの ``reason``（day-skip 種別）を第一とし、``reason`` が無い
    #: ＝ LLM 判断による見送りのときは ``rationale`` を運ぶ。
    #:
    #: 🔴 これを渡さないと LLM は「無発注日の判断根拠は記録なし」と書く。
    #: 判断ログには実際には残っているため**事実誤認**になる（2026-08-22 の週次で発生）。
    #: #249/#254/#256/#258/#280 と同型で、Python が渡さなかった項目をノートが外すパターン。
    no_entry_reason: str = ""
    #: 指標 day-skip の引き金になった経済指標（#333）。
    #: ``no_entry_reason`` は ``indicator_day_skip`` という**種別**しか持たないため、
    #: これが無いと「どの指標で見送ったか」がノートから落ちる。判断ログには残っている。
    skip_indicators: tuple[SkipIndicator, ...] = ()
    #: エッジガード day-skip の判定値（#333）。``(キー, 値)`` の順序付きペア。
    #: 🔴 dict ではなくタプルなのは ``frozen=True`` の dataclass に入れるため。
    #:
    #: 種別（G1 スプレッド超過 / G2 高ボラ / G3 モメンタム不足）だけでは
    #: **閾値の真上で弾かれた日と大きく超過した日を区別できない**。
    #: 2026-08-18 の G2 は ``atr_pips`` 160.421 に対し閾値 160.0 で、差は 0.42 pips だった。
    edge_guard_details: tuple[tuple[str, str], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any((self.entry_price, self.stop_loss, self.take_profit))


@dataclass(frozen=True)
class CancelFirstSnapshot:
    """cancel-first の生結果（#346）。値は無加工（TradePlan 方針）。

    各フィールド:
      None  = 不明（Step3 例外で S3 が null、またはログ欠落相当）
      ()    = 対象なし（取り消すものが無かった）
      (id,) = 実際に cancel / close した ID（str。S3 と同じ文字列化）
    """

    step1_cancelled: tuple[str, ...] | None = None
    step2_cancelled: tuple[str, ...] | None = None
    step3_closed: tuple[str, ...] | None = None
    step3_residual: tuple[str, ...] | None = None


@dataclass(frozen=True)
class TradeNoteInput:
    """ノート生成に渡す入力。"""

    decision_plan: str                  # 判断バッチが生成した狙撃プラン（rationale 等）
    executions: list[Execution]
    now_jst: datetime.datetime
    is_friday_mode: bool = False       # 土曜週次総括モード
    data_gaps: tuple[str, ...] = ()    # 取得できなかった範囲（「約定なし」と区別する）
    #: 計画値（#249）。平日は 1 件、friday モードは取引日ごとに最大 5 件（#251）。
    plans: tuple[TradePlan, ...] = ()
    #: 建玉がどう閉じたかの一次証拠（#280）。``None`` = 材料なし＝全建玉 ``unknown``。
    closeout_evidence: CloseoutEvidence | None = None
    #: cancel-first の生結果（#346）。None = このランでは材料なし
    #: （#226 cancel skip / 旧テスト）。空スナップショットと混同しない。
    cancel_first: CancelFirstSnapshot | None = None


@dataclass(frozen=True)
class TradeNoteResult:
    """生成ノートとトークン消費数（S3 永続化 / 税務証跡 §5.2 用）。"""

    note_markdown: str
    input_tokens: int
    output_tokens: int
    model: str
    #: 本文を最後まで信用してよいか。``False`` になるのは
    #: ①LLM 呼び出しが例外で失敗 ②``stop_reason`` が正常終了以外
    #: （``max_tokens`` 打ち切り・``refusal``・未知の値。#309）の 2 経路。
    #: 既定 ``True`` は成功パスで必ず明示上書きする。
    is_complete: bool = True
    #: 他日の計画値がその日の記述に混入した疑い（#334）。
    #: 🔴 これが空でないことは **ノートが誤っている可能性**を意味するが、
    #: ``is_complete`` は下げない。生成自体は正常終了しており、
    #: 「打ち切られた」と「取り違えた」は別の障害だから（区別できないと運用判断を誤る）。
    cross_day_warnings: tuple[str, ...] = ()
    #: 戦略パラメータの見直し示唆・自前集計など、規律違反フレーズの疑い（#342）。
    #: 🔴 ``is_complete`` は下げない（#334 と同じ。WARN のみで失敗にしない）。
    discipline_warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Protocol: 約定取得クライアント（本番は GmoClient, テストではモックを注入）
# ---------------------------------------------------------------------------

class ExecutionClient(Protocol):
    """約定取得インターフェース。

    引数は**キーワード専用**。旧シグネチャ（``get_executions(symbol)`` /
    ``get_latest_executions(order_id)`` ）と取り違えても位置引数で通ってしまわないようにする。
    """

    def get_executions(self, *, order_id: str | int) -> list[dict[str, Any]]:
        """GET /private/v1/executions?orderId=... — 指定注文の約定のみ。"""
        ...

    def get_latest_executions(
        self, *, symbol: str, count: int = ...
    ) -> list[dict[str, Any]]:
        """GET /private/v1/latestExecutions?symbol=&count= — 直近1日分/最大100件。"""
        ...


# ---------------------------------------------------------------------------
# 約定取得ユーティリティ
# ---------------------------------------------------------------------------

_EXECUTION_FIELDS: frozenset[str] = frozenset(Execution.__dataclass_fields__)


def _to_execution(raw: dict[str, Any]) -> Execution:
    """GMO の生レスポンス 1 件を Execution に正規化する。"""
    return Execution(
        execution_id=str(raw.get("executionId", "")),
        order_id=str(raw.get("orderId", "")),
        symbol=str(raw.get("symbol", "")),
        side=str(raw.get("side", "")),
        price=str(raw.get("price", "")),
        size=str(raw.get("size", "")),
        timestamp=str(raw.get("timestamp", "")),
        settle_type=str(raw.get("settleType", "")),
        position_id=str(raw.get("positionId", "")),
        loss_gain=str(raw.get("lossGain", "")),
        fee=str(raw.get("fee", "")),
        settled_swap=str(raw.get("settledSwap", "")),
        amount=str(raw.get("amount", "")),
    )


def execution_to_payload(ex: Execution) -> dict[str, Any]:
    """S3 永続化用の dict へ変換する（税務証跡 §5.2・週次集約の入力）。"""
    return asdict(ex)


def execution_from_payload(payload: dict[str, Any]) -> Execution:
    """:func:`execution_to_payload` の逆変換。

    未知キーは無視し、欠落キーは空文字で埋める。1 件の破損レコードで例外を投げると、
    週次集約側ではその**日 1 日分がまるごと gap 扱い**になってしまうため、
    復元できる情報は残す方針にする。
    """
    return Execution(**{k: str(payload.get(k, "")) for k in _EXECUTION_FIELDS})


def _parse_execution_timestamp(raw: str) -> datetime.datetime | None:
    """約定日時を aware datetime に変換する（GMO は UTC・末尾 Z の ISO 8601）。

    解釈できない場合は None を返す。呼び出し側は**窓外として捨てず**警告のうえ残すこと
    （税務証跡から実在の約定を落とすほうが害が大きい）。
    """
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # 旧実装が想定していた Unix ミリ秒文字列も一応受ける
        if text.isdigit():
            return datetime.datetime.fromtimestamp(int(text) / 1000, tz=datetime.UTC)
        return None
    if parsed.tzinfo is None:
        # GMO は必ず Z 付きだが、tz 無しが来たら JST ではなく UTC と解釈する
        # （§6.2 の窓判定でズレるより、API の実仕様に寄せる）
        return parsed.replace(tzinfo=datetime.UTC)
    return parsed


def _decimal_or_none(raw: str) -> Decimal | None:
    """金額文字列を Decimal に変換する（空・解釈不能は None）。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


_JP_WEEKDAYS = "月火水木金土日"


def jst_date_label(d: datetime.date) -> str:
    """``2026-08-07(金)`` 形式のラベルを返す（Issue #254）。

    🔴 曜日を渡さないと LLM が日付から**推測して外す**。2026-08-08 の週次ノートでは
    3 箇所すべて 1 日ずれた（08-04 を月曜と記載。実際は火曜）。
    pips と同じ「与えられていない派生値を導出させない」規律（#249）。
    """
    return f"{d.isoformat()}({_JP_WEEKDAYS[d.weekday()]})"


def _jst_datetime_label(dt: datetime.datetime) -> str:
    """約定日時を JST の ``YYYY-MM-DD(曜) HH:MM:SS`` で表す（Issue #254）。"""
    local = dt.astimezone(JST)
    return f"{jst_date_label(local.date())} {local.strftime('%H:%M:%S')}"


def _pips_between(a: str, b: str) -> Decimal | None:
    """2 つの価格文字列の差を pips で返す（絶対値・小数第1位まで）。

    USD/JPY の 1 pip = 0.01 円（``validation.day_skip_guards.USD_JPY_PIP`` と同じ前提）。
    どちらかが解釈できなければ None。**LLM に単位換算をさせないため**の関数（Issue #249）。
    """
    da, db = _decimal_or_none(a), _decimal_or_none(b)
    if da is None or db is None:
        return None
    return (abs(da - db) / Decimal(str(USD_JPY_PIP))).quantize(Decimal("0.1"))


def _format_duration(delta: datetime.timedelta) -> str:
    """保有時間を「N時間M分」形式にする（Issue #256）。"""
    total = int(delta.total_seconds())
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}時間{minutes}分"
    if minutes:
        return f"{minutes}分{seconds}秒"
    return f"{seconds}秒"


@dataclass(frozen=True)
class PositionSummary:
    """建玉 1 件ぶんの確定値（Issue #258）。

    🔴 **建玉ごとの粒度を渡さないと LLM が内訳を逆算して外す。**
    以前の週次ノートは建玉別の実現損益を CLOSE 側の手数料だけで計算し
    （OPEN 側の手数料の抜け）、**部分の和が全体とずれる内部矛盾**を起こした。
    合計だけ渡して内訳を渡さなかったのが原因。
    """

    position_id: str
    side: str                 # 新規側の売買区分（BUY/SELL）
    size: str
    #: 新規約定の orderId。IFD-OCO では **エントリーの rootOrderId と一致**するため、
    #: `CloseoutEvidence.cancelled_root_order_ids` との突合キーになる（Issue #280）。
    open_order_id: str
    opened_at: datetime.datetime
    closed_at: datetime.datetime
    open_price: str
    close_price: str
    move_pips: Decimal | None
    loss_gain: Decimal
    fee: Decimal
    settled_swap: Decimal

    @property
    def holding(self) -> datetime.timedelta:
        return self.closed_at - self.opened_at

    @property
    def realized(self) -> Decimal:
        """この建玉の実現損益（**両サイドの手数料**を含む）。"""
        return self.loss_gain + self.fee + self.settled_swap


def position_summaries(executions: Sequence[Execution]) -> list[PositionSummary]:
    """建玉ごとの確定値（保有時間・値幅 pips・損益）を算出する（Issue #256 / #258）。

    渡す粒度を建玉に揃えることで、LLM が計算する余地を無くす。
    pips（#249）・曜日（#254）・保有時間（#256）と同じ処方箋。

    OPEN / CLOSE が 1 件ずつ揃い、時刻・金額がすべて解釈できる建玉のみを対象にする。
    分割決済など複数件ある建玉や、解釈できない値を含む建玉は**あえて出さない**
    （曖昧な値を渡すより、プロンプトで書かせないほうが安全）。

    🔴 除外の粒度が :func:`summarize_pnl` と**意図的に違う**:
      - ``summarize_pnl``: **フィールド単位**で除外（解釈できない値だけ合計から外す）。
        全体の損益はできるだけ拾いたいため。
      - ``position_summaries``: **建玉単位**で除外。内訳は「一部だけ正しい行」を出すと
        読み手が誤解するので、丸ごと出さないほうが安全。
    この非対称でズレが生じた場合は :func:`_holdings_section` が
    「建玉別の合計は全体と一致しない」と明示するため、黙って矛盾することはない。
    """
    by_position: dict[str, list[Execution]] = {}
    for ex in executions:
        if ex.position_id:
            by_position.setdefault(ex.position_id, []).append(ex)

    summaries: list[PositionSummary] = []
    for position_id, group in sorted(by_position.items()):
        opens = [e for e in group if e.settle_type == "OPEN"]
        closes = [e for e in group if e.settle_type == "CLOSE"]
        if len(opens) != 1 or len(closes) != 1:
            continue
        start = _parse_execution_timestamp(opens[0].timestamp)
        end = _parse_execution_timestamp(closes[0].timestamp)
        if start is None or end is None:
            continue
        if end < start:
            _log.warning(
                "execution_close_before_open",
                position_id=position_id,
                open_timestamp=opens[0].timestamp,
                close_timestamp=closes[0].timestamp,
            )
            continue

        # 金額は **両サイド**を合算する（OPEN 側の手数料を落とさない）
        money: dict[str, Decimal] = {"lossGain": Decimal(0), "fee": Decimal(0), "swap": Decimal(0)}
        broken = False
        for ex in group:
            for key, raw in (
                ("lossGain", ex.loss_gain),
                ("fee", ex.fee),
                ("swap", ex.settled_swap),
            ):
                if not (raw or "").strip():
                    continue
                value = _decimal_or_none(raw)
                if value is None:
                    broken = True
                    break
                money[key] += value
            if broken:
                break
        if broken:
            _log.warning("position_summary_amount_unparsed", position_id=position_id)
            continue

        entry = _decimal_or_none(opens[0].price)
        exit_ = _decimal_or_none(closes[0].price)
        move = None
        if entry is not None and exit_ is not None:
            diff = exit_ - entry
            if opens[0].side == "SELL":  # 売建は下落が利益
                diff = -diff
            move = (diff / Decimal(str(USD_JPY_PIP))).quantize(Decimal("0.1"))

        summaries.append(
            PositionSummary(
                position_id=position_id,
                side=opens[0].side,
                size=opens[0].size,
                open_order_id=opens[0].order_id,
                opened_at=start,
                closed_at=end,
                open_price=opens[0].price,
                close_price=closes[0].price,
                move_pips=move,
                loss_gain=money["lossGain"],
                fee=money["fee"],
                settled_swap=money["swap"],
            )
        )
    return summaries


def determine_exit_reason(
    summary: PositionSummary,
    plan: TradePlan | None,
    evidence: CloseoutEvidence | None,
) -> ExitReason:
    """建玉 1 件の決済理由を決定論的に判定する（Issue #280）。

    🔴 **推測しない。** 判定できない材料しか無いときは断定せず ``unknown`` を返す。
    「TP 発火」と誤って書くくらいなら「判定できない」と書かせるほうが安全という規律は
    #244（ノート捏造対策）と同じ。

    判定順序（上ほど強い証拠）:

    1. 材料が無い（``evidence is None``）→ ``unknown``
    2. OPEN の ``order_id`` が Step2 で cancel された → 決済 OCO は消えている → ``time_stop``
    3. その建玉を Step3 が成行 close した → ``time_stop``
    4. 材料が欠けている（``unknown_position_ids`` / ``verification_failed``）→ ``unknown``
    5. ここまで来た＝cancel-first がこの建玉に触れていない＝**決済 OCO が発火した**。
       どちらの脚かは**注文種別のセマンティクス**で決める（許容誤差を置かない）:

       - TP は LIMIT。計画値**と同値かそれより有利な側**でしか約定しない
       - SL は STOP。計画値**と同値かそれより不利な側**でしか約定しない（スリッページ）

       LONG なら ``close >= tp`` が利確、``close <= sl`` が損切り。**その間の価格は
       どちらの脚でも約定し得ない**ため ``unknown`` に落ちる。距離の閾値を置くと
       「両方遠い」「差が tick 程度」を作為的に切り分けることになるため採らない。

    🔴 2 を 3 より先に見るのは順序の都合であって優先ではない（両方 ``time_stop``）。
    3 が必要なのは、決済 OCO が先に消えて建玉だけ残り Step2 が空になる経路があるため。
    これを落とすと「Step3 で成行決済したのに計画 TP の近くだったので利確と断定」という
    **逆方向の捏造**が起きる。

    🔴 2・3（正の ID 証拠）を 4（材料不足）より**先に**見るのは意図的。
    ``verification_failed`` は「Step3 の後に建玉を再取得できなかった」だけで、
    Step2 の取消自体は成功している。正の証拠を材料不足で打ち消すと、実際には
    時間切れと分かっている建玉まで ``unknown`` に落ちて記録が痩せる。
    禁止したいのは **OCO 発火への分岐だけ**（空集合を「cancel-first 未介入」と読むこと）。
    """
    if evidence is None:
        return "unknown"
    if summary.open_order_id and summary.open_order_id in evidence.cancelled_root_order_ids:
        return "time_stop"
    if summary.position_id in evidence.closed_position_ids:
        return "time_stop"
    if summary.position_id in evidence.unknown_position_ids or evidence.verification_failed:
        return "unknown"

    if plan is None or plan.is_empty or not plan.log_available:
        return "unknown"
    tp = _decimal_or_none(plan.take_profit)
    sl = _decimal_or_none(plan.stop_loss)
    close = _decimal_or_none(summary.close_price)
    if tp is None or sl is None or close is None:
        return "unknown"

    if summary.side == "BUY":       # LONG: SL < Entry < TP（§3.4 価格序列）
        if not sl < tp:
            return "unknown"        # 計画値が壊れている。断定しない
        if close >= tp:
            return "take_profit"
        if close <= sl:
            return "stop_loss"
    elif summary.side == "SELL":    # SHORT: TP < Entry < SL
        if not tp < sl:
            return "unknown"
        if close <= tp:
            return "take_profit"
        if close >= sl:
            return "stop_loss"
    return "unknown"


def _plan_for_position(
    summary: PositionSummary, plans: Sequence[TradePlan]
) -> TradePlan | None:
    """建玉の取引日に対応する計画値を返す（friday モードで日をまたぐため・#251）。

    建玉の取引日 = **新規約定の JST 日付**。エントリーは 20:30 以降・決済は翌 4:30 なので、
    CLOSE 側の日付で引くと 1 日ずれて他日の SL/TP を当てることになる。

    平日モード（計画 1 件）は日付ラベルが無くてもその 1 件を使う。friday モードで
    該当日が見つからなければ ``None``（＝ ``unknown``）に落とす。
    """
    if not plans:
        return None
    if len(plans) == 1 and not plans[0].trade_date:
        return plans[0]
    iso = summary.opened_at.astimezone(JST).date().isoformat()
    for plan in plans:
        if plan.trade_date.startswith(iso):
            return plan
    return None


def _executed_move_pips(executions: Sequence[Execution]) -> Decimal | None:
    """約定の値幅を pips で返す（新規1件・決済1件の単純ケースのみ）。

    複数建玉が混在する場合は**あえて算出しない**（None）。曖昧な数値を渡すより、
    プロンプトで「与えられていない換算はしない」と指示するほうが安全。
    """
    opens = [e for e in executions if e.settle_type == "OPEN"]
    closes = [e for e in executions if e.settle_type == "CLOSE"]
    if len(opens) != 1 or len(closes) != 1:
        return None
    entry, exit_ = _decimal_or_none(opens[0].price), _decimal_or_none(closes[0].price)
    if entry is None or exit_ is None:
        return None
    move = exit_ - entry
    if opens[0].side == "SELL":  # 売建は下落が利益
        move = -move
    return (move / Decimal(str(USD_JPY_PIP))).quantize(Decimal("0.1"))


def summarize_pnl(executions: Sequence[Execution]) -> PnlSummary:
    """約定一覧から損益サマリを決定論的に算出する（LLM に計算させない・AGENTS.md #5 の精神）。

    実現損益 = ``lossGain`` 合計 + ``fee`` 合計（負値）+ ``settledSwap`` 合計。
    解釈できなかったフィールドは ``unparsed_fields`` に記録し、合計から除外する
    （黙って 0 として足すと損益を過少・過大に報告する）。
    """
    loss_gain = fee = swap = Decimal(0)
    open_count = close_count = 0
    unparsed: list[str] = []

    for ex in executions:
        if ex.settle_type == "OPEN":
            open_count += 1
        elif ex.settle_type == "CLOSE":
            close_count += 1
        for field_name, raw in (
            ("lossGain", ex.loss_gain),
            ("fee", ex.fee),
            ("settledSwap", ex.settled_swap),
        ):
            if not (raw or "").strip():
                continue
            value = _decimal_or_none(raw)
            if value is None:
                unparsed.append(f"{field_name}@{ex.execution_id}")
                continue
            if field_name == "lossGain":
                loss_gain += value
            elif field_name == "fee":
                fee += value
            else:
                swap += value

    if unparsed:
        _log.warning("execution_amount_unparsed", fields=unparsed)

    return PnlSummary(
        open_count=open_count,
        close_count=close_count,
        loss_gain_total=loss_gain,
        fee_total=fee,
        settled_swap_total=swap,
        unparsed_fields=tuple(unparsed),
    )


def _prev_business_day_naive(d: datetime.date) -> datetime.date:
    """土日のみ遡る前営業日（祝日は呼び出し元が prev_trade_date で上書きすること）。"""
    prev = d - datetime.timedelta(days=1)
    while prev.weekday() >= 5:  # 5=土, 6=日
        prev -= datetime.timedelta(days=1)
    return prev


def fetch_execution_window(
    client: ExecutionClient,
    now_jst: datetime.datetime,
    symbol: str = "USD_JPY",
    order_ids: Sequence[str] | None = None,
    prev_trade_date: datetime.date | None = None,
) -> ExecutionFetch:
    """平日: 前営業日 20:00 JST 以降の約定を取得する（§6.2）。

    主取得は ``latestExecutions?symbol=&count=``（**エントリーと決済の両方**が返る唯一の
    経路）で、返ってきた一覧を **窓の下限のみ**で絞る。API 側に日付範囲パラメータは無いためである
    （本モジュール冒頭の設計根拠を参照）。``order_ids`` が渡された場合は
    ``executions?orderId=`` で補完するが、これはエントリー側しか返さないので**補助**に留める。

    上限を設けない理由はフィルタ部のコメントを参照（手仕舞い中に発生する CLOSE 約定を
    落とさないため）。

    Args:
        client: 約定取得クライアント。
        now_jst: 現在時刻（JST）。窓の**始点の算出**と遡及範囲の tripwire にのみ使い、
            約定の足切り上限には使わない。
        symbol: 通貨ペア。
        order_ids: 判断バッチが DynamoDB に保存した rootOrderId。窓や 100 件上限から
            漏れたエントリー約定を拾い直すための補完に使う。
        prev_trade_date: 確定取引日。判断バッチの get_latest_confirmed_candle 結果を渡すと
            祝日をまたぐ場合も正確に機能する。省略時は土日スキップで代替（祝日は非対応）。

    Raises:
        Exception: 主取得 get_latest_executions() 失敗時は呼び出し元へ再 raise する。
            税務証跡として不完全な約定一覧でノートを生成しないため（§6.2）。
            **ボディ status != 0 もここで例外になる**（GmoFxClient が raise_for_gmo_status
            を通すため）。これが無いと API エラーが「約定なし」に化ける（2026-08-05 の事故）。
    """
    prev_biz = prev_trade_date or _prev_business_day_naive(now_jst.date())
    window_start = datetime.datetime(
        prev_biz.year, prev_biz.month, prev_biz.day,
        _WINDOW_START_HOUR, 0, 0, tzinfo=JST,
    )

    gaps: list[str] = []

    # 主取得の失敗は呼び出し元へ再 raise（不完全ノート生成を防ぐ §6.2）
    raw_list = client.get_latest_executions(symbol=symbol, count=_LATEST_COUNT)

    # tripwire 1: 100 件上限に達した = それより古い約定が見えていない可能性がある
    if len(raw_list) >= _LATEST_COUNT:
        gaps.append(
            f"latestExecutions が上限 {_LATEST_COUNT} 件に達したため、"
            "これより古い約定は取得できていない"
        )
        _log.warning(
            "latest_executions_count_limit_reached",
            symbol=symbol,
            count=len(raw_list),
            limit=_LATEST_COUNT,
            rule="公式仕様: 直近1日分から最新100件",
        )

    # tripwire 2: 窓の始点が API の遡及範囲（直近1日）より古い
    if now_jst - window_start > _LATEST_EXECUTIONS_LOOKBACK:
        gaps.append(
            f"要求窓（{window_start.isoformat()} 〜）が latestExecutions の遡及範囲"
            "（直近1日分）を超えているため、窓の前半は取得できていない可能性がある"
        )
        _log.warning(
            "latest_executions_window_exceeds_lookback",
            symbol=symbol,
            window_start=window_start.isoformat(),
            now=now_jst.isoformat(),
            rule="公式仕様: 直近1日分から最新100件",
        )

    # 🔴 フィルタは**下限のみ**。上限（``parsed <= now_jst``）を付けてはならない。
    # ``now_jst`` はバッチ先頭で固定され、そのあと run_cancel_first が走る
    # （POST 間の ≥1 秒 sleep と orderedSize 反映ポーリングで数秒かかる）。
    # Step3 の成行 closeOrder で生まれた **CLOSE 約定の timestamp は必ず now_jst より後**
    # になるため、上限を付けるとその日の決済約定と実損益が丸ごと窓外に落ちる。
    # 決済は別 orderId なので orderId 補完でも救えない（設計根拠 D）。
    # latestExecutions は「最新の約定」しか返さない＝未来の約定は原理的に含まれないので、
    # 上限フィルタは元々何も守っていない。
    executions: dict[str, Execution] = {}
    for raw in raw_list:
        ex = _to_execution(raw)
        parsed = _parse_execution_timestamp(ex.timestamp)
        if parsed is None:
            # 時刻不明の約定を窓外扱いで捨てない（税務証跡から実在の約定が消えるほうが害が大きい）
            _log.warning(
                "execution_timestamp_unparsed",
                execution_id=ex.execution_id,
                timestamp=ex.timestamp,
            )
        elif parsed < window_start:
            continue
        executions[ex.execution_id] = ex

    # orderId 参照で補完（補完失敗は warning のみ — 主取得が成功しているため続行）
    for oid in order_ids or ():
        try:
            for raw in client.get_executions(order_id=oid):
                ex = _to_execution(raw)
                executions.setdefault(ex.execution_id, ex)
        except Exception as exc:
            _log.warning("order_executions_fetch_failed", order_id=oid, error=str(exc))
            gaps.append(f"orderId={oid} の約定補完に失敗した: {exc}")

    result = list(executions.values())
    _log.info(
        "execution_window_fetched",
        symbol=symbol,
        window_start=window_start.isoformat(),
        fetched=len(raw_list),
        in_window=len(result),
        gaps=len(gaps),
    )
    return ExecutionFetch(executions=result, gaps=tuple(gaps))


def weekly_trade_dates(
    now_jst: datetime.datetime,
    trade_dates: list[datetime.date] | None = None,
) -> list[datetime.date]:
    """週次総括の対象取引日（直近 5 営業日・降順）を返す（§6.2）。

    約定の集約（:func:`fetch_weekly_executions`）と計画値の読み込み（#251）が
    **同じ日付集合**を使うために切り出してある。両者がずれると、約定はあるのに
    計画値が無い日が出てノートの整合が崩れる。
    """
    if trade_dates is not None:
        unique_trade_dates = sorted(set(trade_dates), reverse=True)
        if len(unique_trade_dates) < _WEEKLY_TRADE_DAYS:
            raise ValueError(
                f"trade_dates must contain at least {_WEEKLY_TRADE_DAYS} unique confirmed "
                f"trade dates, got {len(unique_trade_dates)}"
            )
        return unique_trade_dates[:_WEEKLY_TRADE_DAYS]

    # 土日・祝日スキップ（呼び出し元が trade_dates を渡す場合はそちらが優先）
    days: list[datetime.date] = []
    cursor = now_jst.date() - datetime.timedelta(days=1)
    while len(days) < _WEEKLY_TRADE_DAYS:
        if cursor.weekday() < 5 and not is_market_holiday(cursor):
            days.append(cursor)
        cursor -= datetime.timedelta(days=1)
    return days


def fetch_weekly_executions(
    client: ExecutionClient,
    now_jst: datetime.datetime,
    symbol: str = "USD_JPY",
    trade_dates: list[datetime.date] | None = None,
    order_ids: Sequence[str] | None = None,
    prev_trade_date: datetime.date | None = None,
    load_daily_closeout: Callable[[datetime.date], DailyCloseout | None] | None = None,
    live_evidence: CloseoutEvidence | None = None,
) -> ExecutionFetch:
    """土曜（friday モード）: 直近 5 取引日分の約定を集約する（§6.2）。

    🔴 **API から 5 日分を取り直すことはできない。** FX API の約定系 GET は
    ``executions``（orderId / executionId のみ）と ``latestExecutions``（直近1日分）
    しかなく、日付範囲で問い合わせる手段が存在しないため（本モジュール冒頭の設計根拠）。
    旧実装は ``executions?symbol=&date=`` を投げていたが、これは実在しないパラメータで
    ERR-5106 になり、しかも当時は例外化されず全日「約定なし」に化けていた。

    そこで:
      - 最新取引日（この土曜バッチが手仕舞う金曜）は :func:`fetch_execution_window` でライブ取得
      - それ以前の取引日は、各日の平日手仕舞いが S3 に永続化した約定一覧から復元

    永続化ログが読めなかった日は「約定なし」ではなく ``gaps`` に記録する。両者を混同すると
    「その日は取引しなかった」と断定するノートが生成される（2026-08-05 の事故と同型）。

    決済理由の判定材料（#280）も同じ経路で併合する。``step2_cancelled`` /
    ``step3_closed`` が記録されていない日の建玉は ``unknown_position_ids`` に落とし、
    **空集合と区別する**。ここを混ぜると「取り消し対象に無い＝OCO が発火した」と
    誤断定する（Step3 失敗日は両方 ``None`` で保存されるため実際に起こり得る）。

    Args:
        trade_dates: 確定取引日リスト（5件以上）。省略時は土日・祝日スキップで代替。
        load_daily_closeout: 取引日 → :class:`DailyCloseout` を返す関数。省略時は S3 から読む。
        live_evidence: 最新取引日（ライブ取得分）の決済理由判定材料。
    """
    business_days = weekly_trade_dates(now_jst, trade_dates=trade_dates)
    loader = load_daily_closeout or _load_persisted_closeout
    latest_day = prev_trade_date or business_days[0]
    seen: dict[str, Execution] = {}
    gaps: list[str] = []
    cancelled_roots: set[str] = set()
    closed_positions: set[str] = set()
    unknown_positions: set[str] = set()

    def _merge_evidence(
        executions: Sequence[Execution], evidence: CloseoutEvidence | None
    ) -> None:
        """1 日分の判定材料を併合する。``None`` の日はその日の建玉を判定不能に落とす。

        ``verification_failed`` は**その日の建玉に限定して** ``unknown`` 化する。
        週全体のフラグとして持ち上げると、検証できた他日の建玉まで巻き添えで
        判定不能になる（正の ID 証拠はそのまま残るので time_stop は保たれる）。
        """
        if evidence is None:
            unknown_positions.update(e.position_id for e in executions if e.position_id)
            return
        cancelled_roots.update(evidence.cancelled_root_order_ids)
        closed_positions.update(evidence.closed_position_ids)
        unknown_positions.update(evidence.unknown_position_ids)
        if evidence.verification_failed:
            unknown_positions.update(e.position_id for e in executions if e.position_id)

    # 1) 最新取引日はライブ取得（この土曜バッチが手仕舞う日 = まだ永続化されていない）
    live = fetch_execution_window(
        client,
        now_jst,
        symbol=symbol,
        order_ids=order_ids,
        prev_trade_date=latest_day,
    )
    for ex in live.executions:
        seen.setdefault(ex.execution_id, ex)
    gaps.extend(live.gaps)
    _merge_evidence(live.executions, live_evidence)

    # 2) それ以前は永続化済み日次ログから復元
    for biz_day in business_days:
        if biz_day == latest_day:
            continue  # 1) のライブ取得で網羅済み
        if biz_day > latest_day:
            # prev_trade_date と営業日算出が食い違ったときのみ到達する。黙って落とすと
            # 「その日は取引しなかった」と読まれるため、欠損として明示する。
            _log.warning(
                "weekly_executions_day_after_latest",
                date=biz_day.isoformat(),
                latest_day=latest_day.isoformat(),
            )
            gaps.append(
                f"{biz_day.isoformat()}: 手仕舞い対象日（{latest_day.isoformat()}）より新しく、"
                "取得対象から外れた"
            )
            continue
        try:
            daily = loader(biz_day)
        except Exception as exc:
            _log.warning(
                "weekly_executions_load_failed", date=biz_day.isoformat(), error=str(exc)
            )
            gaps.append(f"{biz_day.isoformat()}: 永続化ログの読み込みに失敗した（{exc}）")
            continue
        if daily is None:
            _log.warning("weekly_executions_log_missing", date=biz_day.isoformat())
            gaps.append(
                f"{biz_day.isoformat()}: 手仕舞いログが見つからず、約定の有無を確認できない"
            )
            continue
        for ex in daily.executions:
            seen.setdefault(ex.execution_id, ex)
        _merge_evidence(daily.executions, daily.evidence)

    _log.info(
        "weekly_executions_merged",
        count=len(seen),
        days=[d.isoformat() for d in business_days],
        gaps=len(gaps),
        exit_reason_unknown=len(unknown_positions),
    )
    return ExecutionFetch(
        executions=list(seen.values()),
        gaps=tuple(gaps),
        evidence=CloseoutEvidence(
            cancelled_root_order_ids=frozenset(cancelled_roots),
            closed_position_ids=frozenset(closed_positions),
            unknown_position_ids=frozenset(unknown_positions),
        ),
    )


def _load_persisted_closeout(trade_date: datetime.date) -> DailyCloseout | None:
    """S3 の手仕舞いログから当該取引日の約定一覧と判定材料を復元する（friday の既定ローダ）。

    ログ自体が無い場合と、ログはあるが ``executions`` キーが無い（＝ #244 より前に
    書かれた古いログ）場合は、いずれも「確認できない」として None を返す。

    ``step2_cancelled`` / ``step3_closed`` は Step3 失敗時に ``None`` で保存されるため
    （`closeout_batch` 参照）、list でなければ ``evidence=None`` にして
    **その日の建玉を判定不能**に落とす。空集合として扱うと「取り消されていない
    ＝ OCO が発火した」と誤断定する（#280）。

    🔴 ``step3_verification_failed`` の日も同様に危険。決済後の再取得に失敗した回は
    ``step3_closed`` が **空 list として保存される**（fail-closed）ため、list 検査だけでは
    素通りして「cancel-first 未介入」と読まれる。フラグを復元して OCO 分岐を封じる。
    """
    from backend.src.persistence import s3_logger

    log = s3_logger.load_closeout_log(trade_date)
    if log is None:
        return None
    raw = log.get("executions")
    if not isinstance(raw, list):
        return None
    executions = [execution_from_payload(r) for r in raw if isinstance(r, dict)]

    cancelled, closed = log.get("step2_cancelled"), log.get("step3_closed")
    evidence = (
        CloseoutEvidence(
            cancelled_root_order_ids=frozenset(str(x) for x in cancelled),
            closed_position_ids=frozenset(str(x) for x in closed),
            verification_failed=bool(log.get("step3_verification_failed")),
        )
        if isinstance(cancelled, list) and isinstance(closed, list)
        else None
    )
    return DailyCloseout(executions=executions, evidence=evidence)


# ---------------------------------------------------------------------------
# プロンプト生成
# ---------------------------------------------------------------------------

# 平日・週次共通の禁止ブロック（#342）。system 側は短い一行（自動変更提案の禁止 /
# 未提供情報の創作禁止）に留め、詳細はここが正。二重に置くと片方だけ直されて食い違う。
_DISCIPLINE_SECTION = (
    "\n\n## 書かないこと（平日・週次共通）\n"
    "- **勝率・平均保有時間・その他の統計値を自分で計算しない。**"
    "与えられていない集計値は書かない。"
    "週数件のサンプルでは統計的な意味を持たず、誤った一般化を誘発するため。\n"
    "- **発注基準・閾値・パラメータの見直しや再検討を示唆しない。**"
    "「〜の再検討推奨」「〜を見直すべき」「今後の検証対象とすること」等も"
    "**戦略パラメータの変更提案に当たる**。"
    "改善案の提示は人間レビュー専用であり、ノートの役割ではない。\n"
    "- **与えられていない情報についての推測をしない。**"
    "保有中の値動き・相場の反転／レンジ判定は渡していない。"
    "「反転した可能性」「レンジ内推移となった」等の創作は禁止。"
)


def _ids_cell(ids: tuple[str, ...] | None) -> str:
    """cancel-first の ID 欄を連結するだけ（#346）。件数集計や再判定はしない。"""
    if ids is None:
        return "**不明**（cancel-first の結果を確認できなかった）"
    if not ids:
        return "（対象なし）"
    return ", ".join(ids)


def _step1_unfilled_confirmed(inp: TradeNoteInput) -> bool:
    """Step1 に注文 ID がある = 未約定 cancel の確定事実（#346）。"""
    cf = inp.cancel_first
    return cf is not None and cf.step1_cancelled is not None and len(cf.step1_cancelled) > 0


def _cancel_first_section(inp: TradeNoteInput) -> str:
    """cancel-first の生結果セクション（#346）。材料が無ければ空文字。"""
    cf = inp.cancel_first
    if cf is None:
        return ""
    date_bit = ""
    if inp.plans and inp.plans[0].trade_date:
        date_bit = f"（対象取引日 {inp.plans[0].trade_date}）"
    lines = (
        f"\n## cancel-first 結果{date_bit}（Python が記録した生値・推測禁止）\n"
        f"- Step1（新規注文 cancel）: {_ids_cell(cf.step1_cancelled)}\n"
        f"- Step2（決済 OCO cancel）: {_ids_cell(cf.step2_cancelled)}\n"
        f"- Step3（成行 close）: {_ids_cell(cf.step3_closed)}\n"
        f"- Step3 residual（短ポーリング後も残った建玉）: {_ids_cell(cf.step3_residual)}\n"
    )
    if _step1_unfilled_confirmed(inp):
        lines += (
            "🔴 Step1 に注文 ID がある = 4:30 時点で activeOrders（settleType=OPEN）に"
            "残っていた新規注文を cancel-first が取り消した。"
            "**その注文 ID については未約定でキャンセルされた確定事実である。**"
            "当日の約定履歴が空なら「約定しなかった」と書いてよい。"
            "約定履歴に別の約定がある場合は、キャンセルした注文 ID についてのみ未約定と書く。"
            "ティックデータ欠損・板・1分足不足など、与えていない理由を創作しない。"
            "本システムはティックデータを取得していない。\n"
        )
    return lines


def _build_note_prompt(inp: TradeNoteInput) -> tuple[str, str]:
    """system / user プロンプトを返す。

    ノート捏造対策（2026-08-05）: 約定履歴が空でも、LLM に「約定しなかった」と断定させない。
    実際には約定・決済済みなのに API エラーが「約定なし」に化け、「押し目に届かず未約定」
    という事実と反対のノートが生成された。取得できなかった範囲（``data_gaps``）を明示し、
    損益は Python が算出した値のみを使わせる。
    """
    system = (
        "あなたはUSD/JPY FXトレードの振り返りノート担当AIです。\n"
        "与えられた情報を基に、以下3セクションを含む Markdown ノートを生成してください:\n"
        "1. ## 狙撃プラン\n"
        "2. ## 約定結果\n"
        "3. ## 自己評価\n"
        "重要: 戦略パラメータの自動変更提案は絶対に行わない（人間レビュー専用）。\n"
        "税務証跡として保存するため、客観的事実を正確に記録する。\n"
        "\n"
        "事実認定の規律（厳守）:\n"
        "- 与えられた約定履歴・損益サマリに**書かれていることだけ**を事実として書く。\n"
        "- 損益・約定価格・数量・**pips** は与えられた数値をそのまま使う。自分で計算し直さない。\n"
        "- **単位換算をしない**（円 ↔ pips の変換を自分で行わない）。"
        "与えられていない pips 値は書かない。\n"
        "- 損切り・利確の価格は「## 計画値」の値のみを使う。"
        "文章中のピボット等の価格を SL/TP と解釈しない。\n"
        "- **曜日は与えられたものだけを使う**（日付から自分で曜日を導出しない）。\n"
        "- **時刻のタイムゾーンを自分で補完・変換しない**"
        "（与えられた表記のまま書く。「JST」が添えられていれば JST のまま、"
        "添えられていなければタイムゾーンを付けない）。\n"
        "  2026-09-01 のノートは JST の指標時刻に UTC を補完し、9 時間ずれた時刻を残した。\n"
        "- **時間差・保有期間を自分で計算しない**"
        "（与えられた「保有時間」の値だけを使い、無ければ書かない）。\n"
        "- 建玉ごとの損益・値幅・保有時間は「## 建玉ごとのサマリ」の値を**そのまま**使う。"
        "合計から内訳を逆算したり、内訳を足して合計を作り直したりしない。\n"
        "- 約定履歴が空でも「約定しなかった」「押し目に届かなかった」等と**断定しない**。\n"
        "  取得できたデータ上は約定が確認できない、という書き方に留める。\n"
    )
    if _step1_unfilled_confirmed(inp):
        system += (
            "- **例外（#346）:** 「## cancel-first 結果」の Step1 に注文 ID がある場合、\n"
            "  その注文は 4:30 時点で未約定のままキャンセルされた**確定事実**である。\n"
            "  その注文について「約定しなかった」と書いてよい。\n"
            "  当日の約定履歴が空なら結果を「約定なし」と書いてよい。\n"
        )
    system += (
        "- 「取得できなかった範囲」が示されている場合、その区間について"
        "約定の有無を推測で述べない。データ欠損である旨を明記する。\n"
        "- **本システムはティックデータを取得していない。**\n"
        "  ティック・板・1分足の欠損や「データが不完全なため断定できない」を理由にしない。\n"
        "  与えられていないデータ品質の話を創作しない。\n"
        "- **決済理由は「## 決済理由」の値のみを使う**。"
        "約定価格・約定時刻・計画値との位置関係から自分で推測しない。\n"
        "- 決済理由が「時間切れ手仕舞い」の建玉について、"
        "**「早期に決済した」「利確水準まで待つべきだった」「決済タイミングの判断」等、"
        "存在しない裁量判断があったかのように書かない**。"
        "保有窓の満了による設計どおりの強制決済であり、待つ・待たないという選択肢は無い。\n"
        "- **比率・割合を自分で計算しない**"
        "（TP 達成率・実現 R:R・目標比などを算出しない。与えられていない比率は書かない）。\n"
        "- 値動きや相場理由など、与えられていない情報を創作しない。"
    )

    if inp.executions:
        exec_lines = "\n".join(
            f"- [{e.settle_type or '区分不明'}] executionId={e.execution_id} "
            f"orderId={e.order_id} positionId={e.position_id} "
            f"{e.side} {e.size}通貨 @ {e.price} "
            f"（{_execution_time_label(e)}） "
            f"lossGain={e.loss_gain or '-'} fee={e.fee or '-'} "
            f"settledSwap={e.settled_swap or '-'}"
            for e in sorted(inp.executions, key=lambda x: x.timestamp)
        )
    else:
        exec_lines = (
            "取得できた範囲に約定レコードは存在しなかった"
            "（＝「約定しなかった」と断定できるわけではない）"
        )

    pnl = summarize_pnl(inp.executions)
    pnl_lines = (
        f"- 約定件数: {len(inp.executions)} 件"
        f"（新規 OPEN {pnl.open_count} 件 / 決済 CLOSE {pnl.close_count} 件）\n"
        f"- 決済損益 lossGain 合計: {pnl.loss_gain_total} 円\n"
        f"- 取引手数料 fee 合計: {pnl.fee_total} 円\n"
        f"- 決済スワップ settledSwap 合計: {pnl.settled_swap_total} 円\n"
        f"- **実現損益 合計: {pnl.realized_total} 円**（lossGain + fee + settledSwap）"
    )
    # 値幅 pips も Python が算出して渡す（LLM に円→pips 換算をさせない・#249）
    move_pips = _executed_move_pips(inp.executions)
    if move_pips is not None:
        pnl_lines += f"\n- 約定値幅: **{move_pips} pips**（新規→決済の価格差）"
    if pnl.unparsed_fields:
        pnl_lines += (
            f"\n- ⚠ 解釈できず合計から除外したフィールド: "
            f"{', '.join(pnl.unparsed_fields)}（損益は不完全）"
        )

    gap_section = ""
    if inp.data_gaps:
        gap_section = "\n\n## 取得できなかった範囲（データ欠損・推測禁止）\n" + "\n".join(
            f"- {g}" for g in inp.data_gaps
        )

    weekly_section = ""
    if inp.is_friday_mode:
        weekly_section = (
            "\n\n## 追加指示（週次総括）\n"
            "セクション「## 週次総括」を末尾に追加し、今週1週間の取引を振り返ること。\n"
            "\n"
            "書くこと:\n"
            "- 取引日ごとの結果を**散文で**。エントリー・SL・TP の数値は書かず、"
            "「計画どおり」「SL 到達」「時間切れ」のように**言葉で**述べる"
            "（数値は末尾の確定表に載る）。\n"
            "- **発注しなかった日は「## 計画値」の理由をそのまま書く**"
            "（理由が示されている日を「判断根拠が記録されていない」と書かない）。\n"
            "- 週の損益は「## 損益サマリ」の確定値をそのまま使う。\n"
            "\n"
            "🔴 計画値（エントリー・SL・TP）の転記について（#334）:\n"
            "- **取引日ごとの計画値の表を作らない。** 同じ内容の確定表を"
            "Python がノート末尾に自動で付ける。あなたが書くと二重になる。\n"
            "- 散文で**日ごとの SL/TP・エントリー価格を書き写さない**。"
            "必要なら「計画どおり」「SL 到達」のように**言葉で**述べ、"
            "数値は末尾の確定表に委ねること。\n"
            "- 実際に 2026-08-29 の週次ノートで、ある日の行に**別の日の SL/TP**が"
            "書かれた。各行に取引日ラベルが付いていても起きている。\n"
            "\n"
            "🔴 書かないこと（週次総括で特に外しやすい）:\n"
            "- 来週の相場見通し・方向性の予想を書かない（与えられていない情報の創作に当たる）。"
        )

    user = (
        f"日時(JST): {jst_date_label(inp.now_jst.date())} "
        f"{inp.now_jst.strftime('%H:%M')} "
        f"({'金曜週次' if inp.is_friday_mode else '平日'})\n\n"
        f"## 本日の狙撃プラン（判断根拠・散文）\n{inp.decision_plan}\n"
        f"{_plans_section(inp)}\n"
        f"## 約定履歴\n{exec_lines}\n"
        f"{_cancel_first_section(inp)}"
        f"{_holdings_section(inp.executions, pnl)}\n"
        f"{_exit_reason_section(inp)}"
        f"## 損益サマリ（Python が算出した確定値・再計算禁止）\n{pnl_lines}"
        f"{gap_section}"
        f"{_DISCIPLINE_SECTION}"
        f"{weekly_section}\n\n"
        "上記を元にトレードノートを生成してください。"
    )
    return system, user


def exit_reasons_for(inp: TradeNoteInput) -> list[tuple[PositionSummary, ExitReason]]:
    """入力中の各建玉と決済理由の組を返す（Issue #280）。

    プロンプト生成と S3 への永続化（税務証跡 §5.2）の**両方**がこの 1 本を使う。
    別々に判定すると、ノートに書いた理由と監査ログの理由が食い違い得る。
    """
    return [
        (s, determine_exit_reason(s, _plan_for_position(s, inp.plans), inp.closeout_evidence))
        for s in position_summaries(inp.executions)
    ]


def _exit_reason_section(inp: TradeNoteInput) -> str:
    """決済理由セクション（Issue #280）。対象建玉が無ければ空文字。

    🔴 これを渡さないと LLM が空白を埋めて創作する。2026-08-10 のノートは
    4:30 cron の強制決済を「計画の利確目標に到達する前に決済売却を実行」と書き、
    「利確水準まで待つメリット・デメリットの事後検証が必要」と**存在しない裁量判断**を
    反省した。#249（pips）/ #254（曜日）/ #256・#258（建玉別内訳）と同型の処方箋。
    """
    pairs = exit_reasons_for(inp)
    if not pairs:
        return ""
    lines = "\n".join(
        f"- positionId={s.position_id}: {EXIT_REASON_LABELS[reason]}" for s, reason in pairs
    )
    return (
        "\n## 決済理由（Python が判定した確定値・推測禁止）\n"
        f"{lines}\n"
        "🔴 決済理由は上記の値のみを使うこと。約定価格・約定時刻・計画値との位置関係から"
        "**自分で推測して別の理由を書かない**。「判定不能」の建玉は判定不能と書く。\n"
    )


def _holdings_section(executions: Sequence[Execution], pnl: PnlSummary) -> str:
    """建玉ごとのサマリセクション（Issue #256 / #258）。対象が無ければ空文字。

    保有時間・値幅 pips・損益を**建玉ごと**に渡す。合計だけ渡して内訳を渡さないと、
    LLM が内訳を逆算して外し、部分の和が全体と合わない矛盾したノートになる
    （以前の週次ノートで実際にずれた）。

    建玉別の和が全体と一致しない場合（一部の建玉が対象外）は**その旨も明示**する。
    黙って並べると、読み手が「内訳の合計＝週間損益」と誤読する。
    """
    summaries = position_summaries(executions)
    if not summaries:
        return ""

    lines = []
    for s in summaries:
        move = f"（値幅 **{s.move_pips} pips**）" if s.move_pips is not None else ""
        lines.append(
            f"- positionId={s.position_id}: {s.side} {s.size}通貨 "
            f"{s.open_price} → {s.close_price}{move}\n"
            f"  {_jst_datetime_label(s.opened_at)} → {_jst_datetime_label(s.closed_at)} JST"
            f"（保有 **{_format_duration(s.holding)}**）\n"
            f"  決済損益 {s.loss_gain} 円 / 手数料 {s.fee} 円 / スワップ {s.settled_swap} 円"
            f" → **実現損益 {s.realized} 円**"
        )

    covered = sum((s.realized for s in summaries), Decimal(0))
    note = ""
    if covered != pnl.realized_total:
        note = (
            f"\n⚠ **建玉別の合計（{covered} 円）は全体（{pnl.realized_total} 円）と一致しない**。"
            "対象外の約定があるため、内訳の合計を週間損益として書かないこと。"
        )

    return (
        "\n## 建玉ごとのサマリ（Python が算出した確定値・再計算禁止）\n"
        + "\n".join(lines)
        + note
        + "\n"
    )


def _plans_section(inp: TradeNoteInput) -> str:
    """モードに応じた計画値セクションを返す（#249 / #251）。

    平日は 1 日分の詳細、friday モードは取引日ごとの表。
    どちらも「与えられた日以外の SL/TP を書かない」ことを明示する。
    """
    if inp.is_friday_mode:
        if not inp.plans:
            return (
                "\n## 計画値（取引日ごと）\n"
                "（計画値が渡されていない。**SL/TP を文章から推測して書かないこと**）\n"
            )
        return _build_weekly_plan_section(inp.plans)
    plan = inp.plans[0] if inp.plans else TradePlan()
    return _build_plan_section(plan)


def _execution_time_label(ex: Execution) -> str:
    """約定日時を「JST（曜日つき） / 元の UTC」で表す（Issue #254）。

    読み手は JST の取引日で考えるため JST を先に出す。監査で原文と突き合わせられるよう
    GMO が返した UTC 文字列も残す。時刻を解釈できない場合は UTC 文字列のみ。
    """
    parsed = _parse_execution_timestamp(ex.timestamp)
    if parsed is None:
        return f"{ex.timestamp} UTC・JST 変換不能"
    return f"{_jst_datetime_label(parsed)} JST / {ex.timestamp} UTC"


def _table_safe(text: str) -> str:
    """Markdown テーブルのセルに入れても壊れない形に整える（#318）。

    見送り理由には LLM 生成の ``rationale`` が入り得る。改行やパイプが
    含まれると**表そのものが崩れ**、他の日の計画値まで読めなくなる。

    🔴 整えるのは**描画だけ**。``TradePlan`` が保持する値は無加工のまま
    （既存方針: 推測・再フォーマットをしない）。要約や切り詰めもしない
    ——理由の肝が末尾にある場合に落ちるため。
    """
    return " ".join(text.replace("|", "/").split())


def _format_skip_indicator(ind: SkipIndicator) -> str:
    """``名称（時刻 JST・通貨）`` の形に並べる（#333・#343）。

    時刻も通貨も無ければ名称だけ。値の加工はしない（並べ替えと連結のみ）。

    🔴 **時刻には必ず JST を添える（#343）。** 2026-09-01 の本番ノートで
    ``23:00`` とだけ渡した結果、LLM がタイムゾーンを補完して **「23:00 UTC」**
    と書いた。判断ログは ``time_jst``（JST）であり、9 時間ずれた時刻が
    税務証跡に残った。``AGENTS.md`` 安全ガードレール #6「時刻は常に JST」。

    これは**値の加工ではなく単位の明示**である。``time_jst`` というフィールド名が
    既に持っている情報を、描画で落とさないための付与にすぎない
    （``TradePlan`` の「無加工で運ぶ」方針と矛盾しない）。

    ``time_jst`` は判断バッチが ``strftime("%H:%M")`` で書く素の ``HH:MM``
    （`backend/src/batch/decision_batch.py`）なので、タイムゾーンの二重表記は起きない。
    値が空の日は**付けない**——無い情報を作らないため。
    ``date_jst`` があるときだけ ``M/D HH:MM JST``（#374）。無い過去ログは ``HH:MM JST``。
    ラベル付与は :func:`backend.src.engine.trading_day.jst_dated_time_label`。
    """
    time_label = jst_dated_time_label(ind.date_jst, ind.time_jst)
    meta = "・".join(bit for bit in (time_label, ind.currency) if bit)
    return f"{ind.name}（{meta}）" if meta else ind.name


def _skip_detail_text(plan: TradePlan) -> str:
    """見送り理由に添える内訳を組み立てる（#333）。

    種別（``indicator_day_skip`` / ``high_vol_regime_day_skip`` 等）だけでは
    **どの指標か / どれくらい閾値を超えたか**が分からない。判断ログには残っている
    ため、渡さないとノート側だけで情報が落ちる（#249/#254/#256/#258/#280/#318 と同型）。

    🔴 表示のための連結だけを行う。値そのものは無加工で、丸め・単位変換・要約をしない。
    改行やパイプは :func:`_table_safe` が吸収する（表を壊さないため）。
    """
    parts: list[str] = []
    if plan.skip_indicators:
        # 🔴 区切りは読点。指標名自体に "/" が入る（``Core PCE Price Index m/m`` /
        #    ``Prelim GDP q/q``）ため、スラッシュ区切りだと境界が読めなくなる。
        names = "、".join(_format_skip_indicator(ind) for ind in plan.skip_indicators)
        parts.append(f"対象指標: {names}")
    if plan.edge_guard_details:
        values = " / ".join(f"{k}={v}" for k, v in plan.edge_guard_details)
        parts.append(f"判定値: {values}")
    return " ".join(parts)


def _no_entry_cell(plan: TradePlan) -> str:
    """見送り日の理由セル（種別 + 内訳）。表と平日セクションで同じ文面を使う。"""
    reason = _table_safe(plan.no_entry_reason)
    detail = _skip_detail_text(plan)
    if detail:
        reason = f"{reason} — {_table_safe(detail)}" if reason else _table_safe(detail)
    return reason


def _plan_row(plan: TradePlan) -> str:
    """計画値 1 日分を 1 行にまとめる（friday モードの表用）。"""
    label = plan.trade_date or "日付不明"
    if not plan.log_available:
        return f"| {label} | ⚠ 判断ログを取得できず（計画値**不明**・推測しないこと） | | | | | |"
    if plan.status == "no_entry" or plan.is_empty:
        reason = "NO_ENTRY（発注していない）" if plan.status == "no_entry" else "計画値なし"
        # #318: 見送り理由が分かっている日は必ず出す。空欄にすると
        # 「判断根拠は記録なし」と書かれる（判断ログには残っているので事実誤認）。
        # #333: 種別だけでなく対象指標名・エッジガードの判定値も出す。
        detail = _no_entry_cell(plan)
        if detail:
            reason += f" — 理由: {detail}"
        return f"| {label} | {reason} | | | | | |"
    sl_pips = _pips_between(plan.entry_price, plan.stop_loss)
    tp_pips = _pips_between(plan.take_profit, plan.entry_price)
    return (
        f"| {label} | {plan.direction or '-'} {plan.order_type or ''} "
        f"| {plan.entry_price or '-'} | {plan.stop_loss or '-'} | {plan.take_profit or '-'} "
        f"| {f'{sl_pips} pips' if sl_pips is not None else '-'} "
        f"| {f'{tp_pips} pips' if tp_pips is not None else '-'} |"
    )


def _build_weekly_plan_section(plans: Sequence[TradePlan]) -> str:
    """friday モードの計画値セクション（Issue #251）。

    取引日ごとに 1 行。**「NO_ENTRY」と「判断ログを取得できず」を区別**して出す
    （前者は事実、後者は不明。#244 と同じ規律）。
    """
    header = (
        "\n## 計画値（取引日ごと・判断バッチの確定値）\n"
        "🔴 各行はその取引日**のみ**の値。他の日に当てはめないこと。\n\n"
        "| 取引日 | 方向 | エントリー | SL | TP | SL幅 | TP幅 |\n"
        "|---|---|---|---|---|---|---|\n"
    )
    rows = "\n".join(_plan_row(p) for p in plans)
    return header + rows + "\n"


def _build_plan_section(plan: TradePlan) -> str:
    """**平日ノート**の計画値セクション（Issue #249）。

    pips は Python が算出して渡す。LLM に円 ↔ pips の換算をさせると誤る
    （以前のノートで円の値をそのまま pips として誤記した）。

    見出しに対象取引日を入れるのは、約定履歴の日付と突き合わせられるようにするため。
    friday モード（取引日ごとに複数件）は :func:`_build_weekly_plan_section` が扱う。
    """
    scope = f"（対象取引日: {plan.trade_date}）" if plan.trade_date else ""

    if plan.is_empty:
        # #318 レビュー Nit: 平日も見送り理由を出す。判断ログの rationale は
        # ハードルール日だと「エッジガード day-skip hard rule（§3.3 / RD-8 #194）」という
        # **総称**しか入っておらず、G2（高ボラ）か G3（モメンタム不足）かが分からない。
        # 種別は reason にしかないため、ここで出さないと平日ノートから情報が落ちる。
        if plan.status == "no_entry":
            # #333: 対象指標名・エッジガードの判定値まで含める。
            reason_text = _no_entry_cell(plan)
            detail = (
                f"（**発注していない**。理由: {reason_text}）"
                if reason_text
                else "（**発注していない**。理由は判断ログから取得できていない）"
            )
            return (
                f"\n## 計画値{scope}\n"
                f"{detail}\n"
                "**SL/TP を文章から推測して書かないこと。**\n"
            )
        return (
            f"\n## 計画値{scope}\n"
            "（判断ログに計画値が無い。**SL/TP を文章から推測して書かないこと**）\n"
        )

    sl_pips = _pips_between(plan.entry_price, plan.stop_loss)
    tp_pips = _pips_between(plan.take_profit, plan.entry_price)
    lines = [
        "",
        f"## 計画値{scope}（判断バッチの確定値・これ以外を SL/TP と解釈しないこと）",
        f"- 方向: {plan.direction or '-'} / 注文種別: {plan.order_type or '-'}",
        f"- エントリー: {plan.entry_price or '-'}",
        f"- 損切り (SL): {plan.stop_loss or '-'}"
        + (f"（エントリーから **{sl_pips} pips**）" if sl_pips is not None else ""),
        f"- 利確 (TP): {plan.take_profit or '-'}"
        + (f"（エントリーから **{tp_pips} pips**）" if tp_pips is not None else ""),
        f"- ロット: {plan.lot or '-'} 通貨 / sl_profile: {plan.sl_profile or '-'}"
        f" / rr_target: {plan.rr_target or '-'}",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 確定表の描画と転記ミス検出（Issue #334）
# ---------------------------------------------------------------------------

#: ノート本文に差し込む確定表の見出し。検出側でこのセクションを除外するのに使う。
_AUTHORITATIVE_HEADING = "## 取引日ごとの計画値（Python 生成・LLM 未経由）"

#: 本文から日付参照を拾う正規表現。``2026-08-24`` / ``08-24`` / ``8/24`` を拾う。
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_SHORT_DATE_RE = re.compile(r"(?<![\d-])(\d{1,2})[-/](\d{1,2})(?![\d-])")

#: 価格らしき数値。整数部と小数部を持つものだけを見る。
_DECIMAL_RE = re.compile(r"\d+\.\d+")


def build_authoritative_weekly_section(plans: Sequence[TradePlan]) -> str:
    """週次ノートに差し込む「取引日ごとの計画値」表を Python が描画する（#334）。

    🔴 **LLM に転記させない。** 2026-08-29 の週次ノートで、08-24 の行に 08-27 の
    SL/TP が書かれた。プロンプト側の表は ``trade_date`` ラベル付きで正しく渡って
    おり（切り分け済み）、**入力ではなく転記で壊れた**。

    #249/#254/#256/#258/#280/#318 は「Python が渡さなかったから LLM が外した」で、
    対策は「構造化して渡す」だった。本件は渡した上で取り違えているため、同じ対策が
    効かない。既存規律「数値は Python が算出して渡す」を **転記まで** 広げる。

    出力トークンも減るため、Lambda 120 秒 / max_tokens 4096 の制約にも有利（#309/#311）。
    """
    rows = "\n".join(_plan_row(p) for p in plans)
    return (
        f"\n\n{_AUTHORITATIVE_HEADING}\n\n"
        "この表は判断ログから Python が直接描画している（LLM の転記を経ていない・#334）。\n"
        "本文と食い違う場合は**この表が正**。\n\n"
        "| 取引日 | 方向 | エントリー | SL | TP | SL幅 | TP幅 |\n"
        "|---|---|---|---|---|---|---|\n"
        f"{rows}\n"
    )


def _plan_values_by_date(plans: Sequence[TradePlan]) -> dict[str, set[Decimal]]:
    """``YYYY-MM-DD`` → その日の計画価格（entry / SL / TP）の集合。"""
    by_date: dict[str, set[Decimal]] = {}
    for plan in plans:
        m = _ISO_DATE_RE.search(plan.trade_date)
        if not m:
            continue
        iso = m.group(0)
        values = by_date.setdefault(iso, set())
        for raw in (plan.entry_price, plan.stop_loss, plan.take_profit):
            parsed = _decimal_or_none(raw)
            if parsed is not None:
                values.add(parsed)
    return by_date


def _referenced_date(line: str, known_dates: Sequence[str]) -> str | None:
    """行が参照している取引日を 1 つだけ特定する。

    複数の日付が同じ行にあるときは **None**（どの日の記述か決められないため、
    誤検知を避けて見送る）。``08-24`` や ``8/24`` のような短縮形は、既知の
    取引日に一意に解決できるときだけ採用する。
    """
    found = {m.group(0) for m in _ISO_DATE_RE.finditer(line) if m.group(0) in known_dates}
    if not found:
        for m in _SHORT_DATE_RE.finditer(line):
            month, day = int(m.group(1)), int(m.group(2))
            matches = {
                d
                for d in known_dates
                if int(d[5:7]) == month and int(d[8:10]) == day
            }
            found |= matches
    return found.pop() if len(found) == 1 else None


def find_cross_day_value_leaks(
    note_markdown: str, plans: Sequence[TradePlan]
) -> tuple[str, ...]:
    """他日の計画値が混入した行を検出する（#334 案2）。

    🔴 **狙いを「取り違え」に絞っている。** 「知らない数値が出てきた」ではなく
    「**別の取引日の計画値**が、その日の記述に出てきた」だけを拾う。捏造検知
    （#244 系）とは別物で、こちらは値が実在するぶん既存の対策で拾えない。

    絞り込みの効果として誤検知が出にくい: 損益や pips がたまたま他日の SL と
    ビット単位で一致しない限り鳴らない。

    ⚠️ **検出しないもの（設計どおりの絞り込み）**:

    - 日付トークンが無い行（「月曜は SL …」のような書き方）
    - 丸めて書かれた値（``159.06``。比較は ``Decimal`` の完全一致）
    - SL 幅 pips の取り違え（owners は entry / SL / TP の**価格**のみ）
    - 同じ SL を 2 日が共有する場合（その値では鳴らない。USD/JPY の 3 桁価格では稀）

    これで足りるのは、**散文を正にしない**前提があるから。振り返りの正は
    :func:`build_authoritative_weekly_section` が描く確定表（案1）であり、
    本関数は「散文がまた混ぜたときに気づく」ための狭い検出に徹する。

    Args:
        note_markdown: 生成されたノート本文。
        plans: friday モードの取引日ごとの計画値。

    Returns:
        人が読める警告文のタプル。問題なしなら空。
    """
    by_date = _plan_values_by_date(plans)
    if len(by_date) < 2:
        # 取引日が 1 日以下なら「他日」が存在しないので取り違えは起こり得ない。
        return ()

    owners: dict[Decimal, set[str]] = {}
    for iso, values in by_date.items():
        for value in values:
            owners.setdefault(value, set()).add(iso)

    known_dates = sorted(by_date)
    warnings: list[str] = []
    # Python が描画した確定表は正しいので検査対象から外す（自分の出力で鳴らさない）。
    body = note_markdown.split(_AUTHORITATIVE_HEADING)[0]

    for lineno, line in enumerate(body.splitlines(), start=1):
        line_iso = _referenced_date(line, known_dates)
        if line_iso is None:
            continue
        for token in _DECIMAL_RE.findall(line):
            parsed = _decimal_or_none(token)
            if parsed is None or parsed not in owners:
                continue
            if line_iso in owners[parsed]:
                continue  # その日の値なら正しい
            others = ", ".join(sorted(owners[parsed]))
            warnings.append(
                f"L{lineno}: {line_iso} の記述に {token} が出ているが、"
                f"この値は {others} の計画値であり {line_iso} のものではない"
            )
    return tuple(warnings)


# ---------------------------------------------------------------------------
# 規律違反フレーズ検出（Issue #342 案2）
# ---------------------------------------------------------------------------

# 実ノートで出た提案表現と、渡していない集計語に限定する。
# 裸の「推奨」「見直し」「示唆」は正しい否定形や運用文にも出るため入れない。
_DISCIPLINE_PHRASES: tuple[tuple[str, str], ...] = (
    ("再検討", "パラメータ再検討の示唆"),
    ("見直すべき", "見直し提案"),
    ("検証対象", "検証対象にする提案"),
    ("今後への示唆", "改善提案セクション"),
    ("改善案", "改善案の提示"),
    ("勝率", "自前計算した勝率"),
    ("平均保有時間", "自前計算した平均保有時間"),
)

# 同じ**句**に否定があれば見送る（行単位だと同一行の別句の違反を取りこぼす）。
# 「余地はない」「記載しない」等の開いた否定は ``な[いかくけ]`` / ``ません`` に吸収する。
_DISCIPLINE_NEGATION_RE = re.compile(
    r"行わない|禁止|提案に当たる|人間レビュー専用|役割ではない|"
    r"な[いかくけ]|ありませ|ません|無[いかくけ]|不要|対象外|範囲外|委ね"
)
_DISCIPLINE_CLAUSE_RE = re.compile(r"[。、]")


def find_discipline_violations(note_markdown: str) -> tuple[str, ...]:
    """禁止フレーズを検出する（#342 案2）。

    #334 で「プロンプトだけでは防げない」ことが実証されている。本関数は
    生成後の本文を見て、戦略パラメータの見直し示唆や自前集計を拾う。

    🔴 **WARN 専用。** ``is_complete`` は下げない（#334 と同じ扱い）。
    自然文なので誤検知は #334 の数値検出より起きやすい。同じ句に否定
    （「行わない」「余地はない」「対象外」等）があれば見送る。判定は行ではなく
    ``。`` / ``、`` で割った句単位（同一行の別句にある違反を取りこぼさない）。

    Returns:
        人が読める警告文のタプル。問題なしなら空。
    """
    warnings: list[str] = []
    for lineno, line in enumerate(note_markdown.splitlines(), start=1):
        for clause in _DISCIPLINE_CLAUSE_RE.split(line):
            if not clause or _DISCIPLINE_NEGATION_RE.search(clause):
                continue
            for phrase, label in _DISCIPLINE_PHRASES:
                if phrase in clause:
                    warnings.append(f"L{lineno}: {label}（「{phrase}」）")
                    break
    return tuple(warnings)


# ---------------------------------------------------------------------------
# ノート生成
# ---------------------------------------------------------------------------

class _MessageClient(Protocol):
    """messages.create を持つクライアント（Anthropic 実体 / テスト用フェイクを注入可能）。"""

    messages: Any


def max_tokens_for(*, is_friday_mode: bool) -> int:
    """モードに応じた出力上限を返す（#309）。"""
    return _MAX_TOKENS_FRIDAY if is_friday_mode else _MAX_TOKENS_WEEKDAY


def _is_complete_stop_reason(stop_reason: object) -> bool:
    """``stop_reason`` が正常終了を示すか（#309）。

    未知の値・欠落はすべて **不完全** に倒す（fail-safe）。実 SDK は非ストリーミング
    応答に必ず ``stop_reason`` を入れるため、ここに来る未知の値は
    「こちらが想定していない終了理由」＝本文が信用できない状態を意味する。

    ``MagicMock`` のようなオブジェクトが素通りしないよう、``str`` 以外は
    その時点で不完全と判定する。
    """
    return isinstance(stop_reason, str) and stop_reason in _COMPLETE_STOP_REASONS


def _with_authoritative_sections(note_text: str, inp: TradeNoteInput) -> str:
    """LLM 本文に Python 描画の確定セクションを足す（#334）。

    friday モードで取引日が複数ある週だけ付ける。平日ノートは計画値が 1 件しか
    無く、取り違えの相手が存在しないため対象外（不要な冗長化を避ける）。
    """
    if not inp.is_friday_mode or len(inp.plans) < 2:
        return note_text
    return note_text + build_authoritative_weekly_section(inp.plans)


def generate_trade_note(
    inp: TradeNoteInput,
    *,
    llm_client: _MessageClient | None = None,
    model: str | None = None,
) -> TradeNoteResult:
    """トレードノート Markdown を LLM で生成する（§6.2）。

    Args:
        inp: TradeNoteInput
        llm_client: messages.create を持つクライアント。省略時は Anthropic() を生成。
        model: 使用モデル。省略時は env NIGHT_MODEL（既定 claude-haiku-4-5）。

    Returns:
        TradeNoteResult。生成失敗時は is_complete=False でエラー内容をノート本文に記録し返す。
    """
    resolved_model = model or os.environ.get("NIGHT_MODEL", DEFAULT_NIGHT_MODEL)

    try:
        api: Any = llm_client
        if api is None:
            from anthropic import Anthropic

            from backend.src.lambda_auth import get_anthropic_api_key

            api = Anthropic(
                api_key=get_anthropic_api_key(),
                timeout=_REQUEST_TIMEOUT,
                max_retries=_SDK_MAX_RETRIES,  # 529 を 1 回だけ拾う（#351）。2 には戻さない
            )

        system_prompt, user_prompt = _build_note_prompt(inp)
        response = api.messages.create(
            model=resolved_model,
            max_tokens=max_tokens_for(is_friday_mode=inp.is_friday_mode),
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:
        _log.warning("trade_note_llm_failed", error=str(exc))
        fallback = (
            f"# トレードノート（LLM生成失敗）\n\n"
            f"エラー: {exc}\n\n"
            f"## 約定件数\n{len(inp.executions)} 件"
        )
        # 🔴 生成に失敗しても計画値は判断ログから分かる。確定表だけでも残す
        #    （税務証跡 §5.2 の観点で、空のノートより手掛かりがある方がよい）。
        return TradeNoteResult(
            note_markdown=_with_authoritative_sections(fallback, inp),
            input_tokens=0,
            output_tokens=0,
            model=resolved_model,
            is_complete=False,
        )

    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)

    content = response.content
    note_text = content[0].text if content else ""

    # #334: LLM 本文を検査してから確定表を付ける。順序が逆だと、Python が描いた
    # 正しい表まで検査対象になり、無害な一致で鳴る。
    cross_day_warnings = find_cross_day_value_leaks(note_text, inp.plans)
    # #342: 規律違反フレーズ。確定表の前後どちらでもフレーズは出ないが、
    # LLM 本文だけを見る（Python が足した文面で鳴らさない）。
    discipline_warnings = find_discipline_violations(note_text)
    note_text = _with_authoritative_sections(note_text, inp)

    # HTTP 200 でも打ち切り・refusal はあり得る。例外は飛ばないので明示的に見る（#309）。
    stop_reason = getattr(response, "stop_reason", None)
    is_complete = _is_complete_stop_reason(stop_reason)
    if not is_complete:
        _log.warning(
            "trade_note_incomplete",
            model=resolved_model,
            stop_reason=str(stop_reason),
            is_friday_mode=inp.is_friday_mode,
            output_tokens=output_tokens,
            max_tokens=max_tokens_for(is_friday_mode=inp.is_friday_mode),
        )

    _log.info(
        "trade_note_generated",
        model=resolved_model,
        executions=len(inp.executions),
        is_friday_mode=inp.is_friday_mode,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stop_reason=str(stop_reason),
        is_complete=is_complete,
        # #339: 正常系にも上限を出す。output_tokens だけだと「上限に近づいている」を
        # コード版を知らずに判定できず、打ち切り（#309）が起きてからしか気付けない。
        # 上限引き上げの撤退線は output_tokens / max_tokens 比で引く。
        max_tokens=max_tokens_for(is_friday_mode=inp.is_friday_mode),
    )
    if cross_day_warnings:
        # 🔴 is_complete は下げない。生成は正常終了しており、「打ち切り」と
        # 「取り違え」を同じフラグに潰すと運用判断を誤る（#309 と別の障害）。
        _log.warning(
            "trade_note_cross_day_values",
            model=resolved_model,
            is_friday_mode=inp.is_friday_mode,
            count=len(cross_day_warnings),
            details=list(cross_day_warnings),
        )
    if discipline_warnings:
        # 🔴 is_complete は下げない（#342: WARN のみ。#334 と同じ扱い）。
        _log.warning(
            "trade_note_discipline_violations",
            model=resolved_model,
            is_friday_mode=inp.is_friday_mode,
            count=len(discipline_warnings),
            details=list(discipline_warnings),
        )

    return TradeNoteResult(
        note_markdown=note_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=resolved_model,
        is_complete=is_complete,
        cross_day_warnings=cross_day_warnings,
        discipline_warnings=discipline_warnings,
    )
