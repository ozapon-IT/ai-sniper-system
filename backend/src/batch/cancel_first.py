"""キャンセル・ファースト手仕舞いロジック（§2, §6.2, AGENTS.md #4）。

キャンセル・ファーストシーケンス:
  Step0: 市場状態確認（取引時間内か）
  Step1: 残存新規 IFD-OCO 注文キャンセル (cancelOrders / rootOrderIds ≤10件)
  Step2: 決済 OCO 注文キャンセル (cancelOrders / rootOrderIds ≤10件)
  Step3: 建玉成行クローズ (closeOrder / settlePosition ≤10件バッチ)

各 POST 呼び出し間に ≥1 秒 sleep を強制する（AGENTS.md #3, §6.2）。
Step1/Step2 失敗時は後続ステップを実行しない（AGENTS.md #4）。

🔴 FX API に建玉一括決済エンドポイント（closeBulkOrder）は存在しない（Issue #230）。
   closeBulkOrder は暗号資産API側の名称で、公式 FX ドキュメント全文に出現 0 件。
   一括決済は closeOrder の settlePosition（最大10件/回）で行う。
   cancelOrders も ``orderId`` ではなく ``rootOrderIds``（親注文ID の number 配列・最大10件）。
   出典: https://api.coin.z.com/fxdocs/（2026-07-30 参照）
   詳細: docs/spikes/cancel_first_official_spec.md（付録B）
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from itertools import islice
from typing import Any, Literal, Protocol

import structlog
from pydantic import BaseModel

from backend.src.gmo_errors import GmoApiError, raise_for_gmo_status
from backend.src.gmo_fx_endpoints import (
    PRIVATE_ACTIVE_ORDERS,
    PRIVATE_CANCEL_ORDERS,
    PRIVATE_CLOSE_ORDER,
    PRIVATE_OPEN_POSITIONS,
)
from backend.src.models import (
    MAX_CANCEL_ROOT_ORDER_IDS,
    MAX_SETTLE_POSITIONS,
    CancelOrdersRequest,
    CloseOrderRequest,
    SettlePositionItem,
)

_log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# POST 間の最小スリープ秒数（AGENTS.md #3, §6.2）
_POST_SLEEP_SECONDS = 1.0

# 市場が開いていると判定するステータス（§7.3 / Issue #207 (c)）。
# GET /public/v1/status の data.status は MAINTENANCE / CLOSE / OPEN の 3 値。
# 🔴 これ以外（未知の値・空）は**閉場扱い**にする（fail-safe）。
# 閉場中の実レスポンスは付録B docs/spikes/market_status_closed.md に記録。
MARKET_STATUS_OPEN = "OPEN"

# activeOrders / openPositions の count 既定値（=公式の最大値）。
# 取得件数がこれに達したら prevId ページングが必要な可能性がある（現状は未対応・警告のみ）。
_LIST_PAGE_LIMIT = 100

# Step2（決済 OCO cancel）の反映待ちポーリング設定（Issue #241）。
# cancelOrders は「受付」であり反映は非同期。POST 直後の GET は **取消前の状態**を返す
# （参照系 GET は同一スナップショットを再利用し、世代更新は約 0.7〜1.7 秒間隔・2026-08-11 実測）。
#
# 🔴 待つ理由（2026-08-11 の実測で因果を更新）:
#   当初は「反映前に Step3 へ進むと ERR-423 / ERR-189 で決済が失敗する」を根拠にしていたが、
#   **その拒否は再現しなかった**（決済 OCO が orderedSize 満額で残ったまま満額 close しても
#   status:0 / EXECUTED で通る・n=2）。現在の根拠は次の 2 点:
#     1. 取消反映を待ってから進むことで、Step3 が扱う状態を単純にする
#     2. TP/SL の約定と close のレースを避ける（二重決済の経路を減らす）
#   実測スコープは MARKET / 満額 / 単一建玉 / 開場中。分割決済・複数建玉・LIMIT 決済・
#   閉場前後は未検証なので、拒否が起き得る前提での待機は維持する（外す利益より
#   「SL/TP を消した建玉が翌 4:30 まで裸で残る」リスクが大きい・#207）。
#
# 上限 5 回 × 1 秒 ≒ 4 秒の根拠（2026-08-11・東京 3 + ロンドン 1 サイクル）:
#   取消後の次世代スナップショットは常に t0 から約 1.5 秒以内に orderedSize=0 を示した。
#   間隔を 1.0 秒より細かくしても、スナップショット世代境界より細かくは観測できない。
# 詳細: docs/spikes/ordered_size_err423_latency.md 実験2
_ORDERED_SIZE_POLL_MAX_ATTEMPTS = 5
_ORDERED_SIZE_POLL_INTERVAL_SECONDS = 1.0

# Step3 の closeOrder 再送（Issue #207 (b) L1）。
# 🔴 再送してよいのは「GMO が応答を返して**明示的に拒否**した」場合だけ。
#    タイムアウト・接続断・HTTP エラーは「受付済みかもしれない」ので再送しない
#    （#138「未約定確実な失敗のみ」と同じ判断軸）。
# 時間予算: 1 チャンクあたり最悪 3 試行 ×（ゲートポーリング 2 秒 + POST 間 1 秒）≒ 9 秒。
# 手仕舞い全体（約定取得リトライ 20 秒 ×2 + LLM ノート）でも Lambda の 120 秒に収まる。
_CLOSE_RETRY_MAX_ATTEMPTS = 3
# ゲートのポーリングが短い（2回/1秒）のは、**本命の取消反映待ちが Step3 の直前に
# 済んでいる**ため（_fetch_positions_after_cancel_reflection・最大約4秒・Issue #241）。
# ここは「その後もまだ有効注文が残っていないか」を確かめる最終確認なので短くてよい。
_CLOSE_RETRY_GATE_POLL_ATTEMPTS = 2
_CLOSE_RETRY_GATE_POLL_INTERVAL_SECONDS = 1.0

# 決済後の残存判別ポーリング（Issue #207 (b) L1）。
# closeOrder の成功は**約定を保証しない**（status は WAITING＝受付のことも、EXECUTED＝同期約定の
# こともある・2026-08-11 実測）。直後の 1 回の GET では
# 「約定待ちの一時的な残存」と「本当に閉じられていない」を区別できない。
# 数秒ポーリングして消えなければ後者と判断し、呼び出し元が強アラートに載せる。
_RESIDUAL_POLL_ATTEMPTS = 3
_RESIDUAL_POLL_INTERVAL_SECONDS = 1.0


class CloseOrderTransportError(RuntimeError):
    """closeOrder が HTTP エラー応答を返した（受付されたか**不明**）。

    ボディの業務エラー（``GmoApiError``）は「GMO が明示的に拒否した」＝未受付が確実だが、
    HTTP 5xx 等はサーバ側で処理済みの可能性を否定できない。両者を型で区別し、
    こちらは**再送しない**（Issue #207 (b) L1）。
    """


class _CloseRetryDecision(Enum):
    """closeOrder を再送してよいかの判定（Issue #207 (b) L1 の状態機械）."""

    RETRY = "retry"                  # 建玉が残り、有効注文も無い → 再送可
    ALREADY_CLOSED = "already_closed"  # 建玉が消えた → 成功扱い
    ABORT = "abort"                  # 観測できない / 有効注文が動いている → 撃たない


# ---------------------------------------------------------------------------
# Protocol: モック差し替え可能な最小 API クライアントインターフェース
# ---------------------------------------------------------------------------


class ForexClient(Protocol):
    """手仕舞いバッチが依存する GMO FX API の最小インターフェース."""

    def get_market_status(self) -> str:
        """外国為替FXステータス（``OPEN`` / ``CLOSE`` / ``MAINTENANCE``）を返す（#207 (c)）."""
        ...

    def private_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Private GET を送る."""
        ...

    def private_post(
        self, path: str, body: BaseModel | dict[str, Any] | None = None
    ) -> Any:
        """Private POST を送る（BaseModel は by_alias でシリアライズされる）."""
        ...


# ---------------------------------------------------------------------------
# 結果データクラス
# ---------------------------------------------------------------------------


@dataclass
class CancelFirstResult:
    """キャンセル・ファーストシーケンスの実行結果."""

    market_closed: bool = False
    step1_cancelled_ids: list[str] = field(default_factory=list)
    step2_cancelled_ids: list[str] = field(default_factory=list)
    step3_closed_ids: list[str] = field(default_factory=list)
    step1_failed: bool = False
    step2_failed: bool = False
    retry_next: bool = False  # §7.3: 次回再試行フラグ
    #: 🔴 closeOrder 送信後、**短ポーリングしても消えなかった**建玉（Issue #207 (b) L1）。
    #: closeOrder の成功は約定を保証しないため（WAITING = 受付のこともある）、
    #: 直後 1 回の GET では約定待ちの一時的な残存と区別できなかった。
    #: 数秒ポーリングしても残る = **本当に閉じられていない**＝ Step2 で SL/TP を消した
    #: 建玉が無防備な可能性がある。呼び出し元は強アラート対象として扱うこと。
    step3_residual_ids: list[str] = field(default_factory=list)
    #: 決済後の建玉再取得に失敗し、クローズ結果を検証できなかった（fail-closed）
    step3_verification_failed: bool = False


@dataclass(frozen=True)
class ClosePositionsOutcome:
    """Step3 の決済結果（closed / 残存 / 検証不能 を区別する）."""

    closed_ids: list[str] = field(default_factory=list)
    residual_ids: list[str] = field(default_factory=list)
    verification_failed: bool = False


# ---------------------------------------------------------------------------
# メイン関数
# ---------------------------------------------------------------------------


def run_cancel_first(client: ForexClient, symbol: str = "USD_JPY") -> CancelFirstResult:
    """キャンセル・ファーストシーケンスを実行する（§2, §6.2）。

    Args:
        client: GMO FX API クライアント（実装またはモック）。
        symbol: 対象通貨ペア（デフォルト USD_JPY）。

    Returns:
        シーケンス実行結果。
    """
    result = CancelFirstResult()

    # ------------------------------------------------------------------
    # Step0: 市場状態確認（§6.2 Step0, §7.3）
    # ------------------------------------------------------------------
    market_open = _check_market_open(client, symbol)
    if not market_open:
        _log.warning(
            "cancel_first_market_closed",
            symbol=symbol,
            action="retry_next_run",
            rule="§6.2 Step0, §7.3",
        )
        result.market_closed = True
        result.retry_next = True
        return result

    # ------------------------------------------------------------------
    # Step1: 残存新規 IFD-OCO 注文（親注文）キャンセル（§6.2 Step1）
    # activeOrders 取得失敗も Step1 失敗として扱い Step2/3 を止める（AGENTS.md #4）。
    # 取得失敗を「注文なし」と解釈すると cancel-first の安全条件を壊すため。
    # ------------------------------------------------------------------
    try:
        new_order_ids = _fetch_new_order_ids(client, symbol)
        if new_order_ids:
            _cancel_orders(client, new_order_ids)
            result.step1_cancelled_ids = [str(i) for i in new_order_ids]
        else:
            _log.info("cancel_first_step1_no_orders", symbol=symbol)
    except Exception:
        _log.exception(
            "cancel_first_step1_failed",
            symbol=symbol,
            rule="AGENTS.md #4: Step1 失敗時 Step2/3 を実行しない",
        )
        result.step1_failed = True
        return result

    # ------------------------------------------------------------------
    # Step2: 決済 OCO 注文（子注文）キャンセル（§6.2 Step2）
    # レートリミット用の ≥1 秒 sleep は _cancel_orders / _close_positions が各 POST 直後に
    # 行うため、ステップ境界で追加する必要はない（AGENTS.md #3）。
    # 一方、**取消の反映待ち**は別問題で、Step3 の直前に
    # _fetch_positions_after_cancel_reflection が orderedSize を見て行う（Issue #241）。
    # activeOrders 取得失敗も Step2 失敗として Step3 を止める。
    # 子 OCO を取り消せたか不明なまま成行クローズへ進むと建玉がロックされる恐れがある。
    # ------------------------------------------------------------------
    try:
        settlement_order_ids = _fetch_settlement_order_ids(client, symbol)
        if settlement_order_ids:
            _cancel_orders(client, settlement_order_ids)
            result.step2_cancelled_ids = [str(i) for i in settlement_order_ids]
        else:
            _log.info("cancel_first_step2_no_orders", symbol=symbol)
    except Exception:
        _log.exception(
            "cancel_first_step2_failed",
            symbol=symbol,
            rule="AGENTS.md #4: Step2 失敗時 Step3 を実行しない",
        )
        result.step2_failed = True
        return result

    # ------------------------------------------------------------------
    # Step3: 建玉成行クローズ（§6.2 Step3: closeOrder + settlePosition バッチ）
    # 建玉取得失敗を「建玉なし」と解釈すると決済失敗が偽成功になるため、
    # 呼び出し元へ再送出して fail-closed に倒す（Issue #136）。
    # ------------------------------------------------------------------
    try:
        positions = _fetch_positions_after_cancel_reflection(
            client,
            symbol,
            cancelled_in_step2=bool(result.step2_cancelled_ids),
        )
    except Exception:
        _log.exception("cancel_first_step3_fetch_failed", symbol=symbol)
        raise

    if positions:
        outcome = _close_positions(client, symbol, positions)
        result.step3_closed_ids = outcome.closed_ids
        result.step3_residual_ids = outcome.residual_ids
        result.step3_verification_failed = outcome.verification_failed
    else:
        _log.info("cancel_first_step3_no_positions", symbol=symbol)

    _log.info(
        "cancel_first_completed",
        step1=result.step1_cancelled_ids,
        step2=result.step2_cancelled_ids,
        step3=result.step3_closed_ids,
        step3_residual=result.step3_residual_ids,
        step3_verification_failed=result.step3_verification_failed,
    )
    return result


# ---------------------------------------------------------------------------
# ヘルパー関数
# ---------------------------------------------------------------------------


def _check_market_open(client: ForexClient, symbol: str) -> bool:
    """市場が開いており、かつ API が健全かを確認する（§6.2 Step0 / Issue #207 (c)）。

    **2 条件をどちらも満たしたときだけ True** を返す:

    1. **公式ステータスが ``OPEN``**（``GET /public/v1/status`` の ``data.status``）
    2. 参照系 GET が健全（HTTP 5xx でない・ボディ ``status`` が 0）

    🔴 かつては 2 だけを見て「参照系 GET が通る＝取引時間内」と判断していたが、
    **閉場中でも参照系 GET は HTTP 200 + ``status: 0`` を返す**ため閉場を検知できなかった
    （2026-08-08 土曜に実測・付録B docs/spikes/market_status_closed.md）。
    Step0 を素通りすると Step1/2 で SL/TP を消したうえで Step3 が閉場エラーになり、
    無防備な建玉を残す経路に入る（#207）。

    1 だけでなく 2 も残すのは、**市場は開いているが API が壊れている**ケース
    （5xx・業務エラー）を検知するため。どちらの失敗も閉場扱い（次回再試行）に倒す。
    """
    # 1) 公式の市場ステータス（署名不要・data.status が OPEN/CLOSE/MAINTENANCE）
    try:
        market_status = client.get_market_status()
    except Exception:
        _log.exception(
            "cancel_first_market_status_fetch_failed",
            symbol=symbol,
            rule="ステータス不明は閉場扱い（fail-safe）",
        )
        return False

    if market_status != MARKET_STATUS_OPEN:
        _log.warning(
            "cancel_first_market_status_not_open",
            symbol=symbol,
            market_status=market_status,
            rule="§7.3: OPEN 以外（CLOSE / MAINTENANCE / 不明）は閉場扱い",
        )
        return False

    # 2) 参照系 GET の健全性（市場は開いているが API が壊れているケースの検知）
    try:
        resp = client.private_get(PRIVATE_OPEN_POSITIONS, params={"symbol": symbol})
        raise_for_gmo_status(resp, context="openPositions(step0)")
        status = getattr(resp, "status_code", None) or getattr(resp, "status", None)
        return not (status is not None and status >= 500)
    except Exception:
        _log.exception("cancel_first_market_check_failed", symbol=symbol)
        return False


def _fetch_new_order_ids(client: ForexClient, symbol: str) -> list[int]:
    """新規 IFD-OCO の親注文 ID（rootOrderId）を取得する（§6.2 Step1）。

    settleType=="OPEN" の activeOrders が対象。
    取得失敗時は呼び出し元に例外を伝播させる（「注文なし」と混同しないため）。
    ボディ status != 0（HTTP 200 の業務エラー）も「注文なし」と混同せず例外に倒す。
    """
    return _fetch_root_order_ids(client, symbol, settle_type="OPEN", step="step1")


def _fetch_settlement_order_ids(client: ForexClient, symbol: str) -> list[int]:
    """決済 OCO 注文（子注文）の親注文 ID（rootOrderId）を取得する（§6.2 Step2）。

    settleType=="CLOSE" の activeOrders が対象。
    取得失敗時は呼び出し元に例外を伝播させる。ボディ status != 0 も例外に倒す。
    """
    return _fetch_root_order_ids(client, symbol, settle_type="CLOSE", step="step2")


def _fetch_root_order_ids(
    client: ForexClient, symbol: str, *, settle_type: str, step: str
) -> list[int]:
    """activeOrders から指定 settleType の親注文 ID を重複排除して取得する。

    🔴 cancelOrders に渡すのは ``rootOrderId``（親注文ID）であり ``orderId``（注文ID）
    ではない（公式仕様・Issue #230）。IFD-OCO の 2 次注文（決済 OCO の利確・損切）は
    同一 ``rootOrderId`` を共有するため、順序を保ったまま重複排除する。
    """
    resp = client.private_get(PRIVATE_ACTIVE_ORDERS, params={"symbol": symbol})
    raise_for_gmo_status(resp, context=f"activeOrders({step})")
    data = _extract_data(resp)
    if not data:
        return []
    orders = data if isinstance(data, list) else data.get("list", [])
    _warn_if_page_limit_reached(orders, source="activeOrders", symbol=symbol)

    root_ids: list[int] = []
    for order in orders:
        if not isinstance(order, dict):
            _log.error(
                "cancel_first_active_order_malformed",
                step=step,
                symbol=symbol,
                entry_type=type(order).__name__,
            )
            raise ValueError(f"activeOrders({step}) の要素が dict ではありません")
        if order.get("settleType") != settle_type:
            continue
        root_ids.append(
            _require_int(
                order.get("rootOrderId"),
                field_name="rootOrderId",
                context=f"activeOrders({step})",
            )
        )

    deduped = list(dict.fromkeys(root_ids))
    if len(deduped) != len(root_ids):
        # OCO の 2 子注文は同一 rootOrderId を共有する（重複は正常。10件枠を無駄にしない）
        _log.info(
            "cancel_first_root_order_ids_deduped",
            step=step,
            symbol=symbol,
            before=len(root_ids),
            after=len(deduped),
        )
    return deduped


def _cancel_orders(client: ForexClient, root_order_ids: list[int]) -> None:
    """指定親注文 ID を cancelOrders API でキャンセルする（§6.2）。

    公式仕様（出典: https://api.coin.z.com/fxdocs/・2026-07-30 参照）:
      「rootOrderIds clientOrderIds いずれか1つが必須です。2つ同時には設定できません。
        最大10件まで注文を取消することができます。」
    → ``{"rootOrderIds": [<number>, ...]}`` を最大10件ずつ POST する。
      ``orderId`` というパラメータは存在しない（Issue #230）。

    ボディ status != 0 は GmoApiError に倒す（キャンセル失敗を成功と誤認して
    後続ステップへ進むと建玉ロックの恐れ、AGENTS.md #4）。
    各 POST 直後に ≥1 秒 sleep する（AGENTS.md #3）。
    """
    chunks = list(_chunked(root_order_ids, MAX_CANCEL_ROOT_ORDER_IDS))
    for index, chunk in enumerate(chunks, start=1):
        body = CancelOrdersRequest(rootOrderIds=list(chunk))
        resp = client.private_post(PRIVATE_CANCEL_ORDERS, body)
        raise_for_gmo_status(resp, context="cancelOrders")
        _log.info(
            "cancel_first_cancelled",
            order_ids=list(chunk),
            chunk_index=index,
            chunk_total=len(chunks),
        )
        _warn_if_cancel_not_acknowledged(resp, requested=list(chunk))
        _sleep_between_posts()


def _warn_if_cancel_not_acknowledged(resp: Any, *, requested: list[int]) -> None:
    """cancelOrders の ``data.success`` と要求 ID を突き合わせ、欠落を警告する。

    公式レスポンスは「取消**受付**に成功した親注文ID」の ``success`` 配列のみで、
    failed 相当のプロパティは文書化されていない。取消が受け付けられていない注文が
    残ったまま Step3 へ進むのは想定外の状態なので、観測できるようにログへ残す。
    **pass/fail 判定は変えない**（受付セマンティクスのため）。

    🔴 かつては「Step3 で ERR-423 を踏む」を警告理由にしていたが、**その拒否は
    再現しなかった**（2026-08-11 実測・n=2）。現在の理由は「取消漏れという
    想定外の状態を検知するため」であり、特定のエラーコードを予期するものではない。
    詳細: docs/spikes/ordered_size_err423_latency.md
    """
    data = _extract_data(resp)
    if not isinstance(data, dict):
        return
    success = data.get("success")
    if not isinstance(success, list):
        return
    acknowledged: set[int] = set()
    for entry in success:
        raw: Any = entry.get("rootOrderId") if isinstance(entry, dict) else entry
        if raw is None:
            continue
        try:
            acknowledged.add(int(raw))
        except (TypeError, ValueError):
            continue
    missing = [i for i in requested if i not in acknowledged]
    if missing:
        _log.warning(
            "cancel_first_cancel_not_acknowledged",
            requested=requested,
            acknowledged=sorted(acknowledged),
            missing=missing,
            rule="§6.2 Step2: 取消漏れのまま Step3 へ進むのは想定外の状態",
        )


def _fetch_open_positions(client: ForexClient, symbol: str) -> list[dict[str, Any]]:
    """建玉一覧を取得する（§6.2 Step3）。取得失敗時は例外を伝播させる。

    ボディ status != 0 は「建玉なし」と混同せず GmoApiError に倒す。
    """
    resp = client.private_get(PRIVATE_OPEN_POSITIONS, params={"symbol": symbol})
    raise_for_gmo_status(resp, context="openPositions")
    data = _extract_data(resp)
    if not data:
        return []
    positions: list[dict[str, Any]] = (
        data if isinstance(data, list) else data.get("list", [])
    )
    _warn_if_page_limit_reached(positions, source="openPositions", symbol=symbol)
    return positions


def list_open_position_ids(client: ForexClient, symbol: str) -> list[str]:
    """残存建玉の ID 一覧を正規化して返す（#207 のアラート本文用の公開ヘルパ）。

    手仕舞い失敗時に「いま無防備な建玉はどれか」を人へ伝えるために使う。
    取得失敗は呼び出し元へ送出する（握り潰すかは呼び出し側の方針）。
    """
    return [
        _position_key(p.get("positionId"))
        for p in _fetch_open_positions(client, symbol)
        if isinstance(p, dict)
    ]


def _fetch_positions_after_cancel_reflection(
    client: ForexClient,
    symbol: str,
    *,
    cancelled_in_step2: bool,
) -> list[dict[str, Any]]:
    """Step2 の取消が反映されるまで待ってから建玉一覧を返す（Issue #241・§6.2）。

    ``cancelOrders`` は「受付」であり反映は非同期。**取消反映の待ちとレース回避**のために
    Step3 の手前で待つ（Issue #241）。

    🔴 **待つ理由が変わった**（2026-08-11 実測・n=2）。当初は「反映前に close すると
    『総有効注文数量との合計が総建玉数量を超過』して ERR-423 / ERR-189 になる」を根拠に
    していたが、**その拒否は再現しなかった**（決済 OCO が ``orderedSize`` 満額で残ったまま
    満額 close しても ``status:0`` / ``EXECUTED`` で通る）。現在の根拠:

    1. Step3 が扱う状態を単純にする（有効注文が消えてから決済する）
    2. TP/SL の約定と ``closeOrder`` のレースを避ける

    実測スコープは MARKET / 満額 / 単一建玉 / 開場中。分割決済・複数建玉・``LIMIT`` 決済・
    閉場前後は未検証のため、**待機そのものは維持する**（外す利益より「SL/TP を消した建玉が
    翌 4:30 まで裸で残る」リスクが大きい・Issue #207）。

    判定には建玉の ``orderedSize``（その建玉に紐づく有効注文の数量）を使う。
    🔴 ``activeOrders`` の件数と**突き合わせてはいけない**。両者は独立したスナップショットで
    別々にドリフトし、「閉じた建玉が残って見える」合成像が発生する（2026-08-11 実測・n=3）。
    単一エンドポイントの ``orderedSize`` だけで判定すること。

    詳細: docs/spikes/ordered_size_err423_latency.md 実験1 / 実験2

    Step2 で 1 件も取り消していない場合は待たない。反映を待つ対象が無く、それでも
    ``orderedSize`` が非 0 なら別要因（取消対象外の注文）であり、待っても解消しない。

    タイムアウトしても例外にはせず、取得済みの建玉をそのまま返す。後続の
    ``_close_positions`` が警告を出したうえで満額 close を試み、失敗時は
    fail-closed で次回起動に委ねる（従来挙動を維持）。
    """
    positions = _fetch_open_positions(client, symbol)
    if not cancelled_in_step2 or not positions:
        return positions

    for attempt in range(1, _ORDERED_SIZE_POLL_MAX_ATTEMPTS + 1):
        pending = [p for p in positions if _is_nonzero_size(p.get("orderedSize"))]
        if not pending:
            if attempt > 1:
                # 実運用での反映遅延の分布を測るため、待った場合のみ記録する（#241 DoD）
                _log.info(
                    "cancel_first_ordered_size_reflected",
                    symbol=symbol,
                    attempts=attempt - 1,
                    waited_seconds=(attempt - 1) * _ORDERED_SIZE_POLL_INTERVAL_SECONDS,
                )
            return positions

        if attempt == _ORDERED_SIZE_POLL_MAX_ATTEMPTS:
            break

        _log.info(
            "cancel_first_ordered_size_poll",
            symbol=symbol,
            attempt=attempt,
            max_attempts=_ORDERED_SIZE_POLL_MAX_ATTEMPTS,
            pending_position_ids=[_position_key(p.get("positionId")) for p in pending],
            rule="§6.2 Step2 の cancelOrders は受付＝非同期（Issue #241）",
        )
        time.sleep(_ORDERED_SIZE_POLL_INTERVAL_SECONDS)
        positions = _fetch_open_positions(client, symbol)

    _log.warning(
        "cancel_first_ordered_size_poll_timeout",
        symbol=symbol,
        attempts=_ORDERED_SIZE_POLL_MAX_ATTEMPTS,
        waited_seconds=(_ORDERED_SIZE_POLL_MAX_ATTEMPTS - 1)
        * _ORDERED_SIZE_POLL_INTERVAL_SECONDS,
        rule="満額 close を試み、失敗時は fail-closed で次回起動に委ねる",
        note="実測（2026-08-11・n=2）では orderedSize 非0 のまま close しても通った。"
        "タイムアウト＝決済失敗ではない",
    )
    return positions


def _raise_for_close_http_status(resp: Any) -> None:
    """closeOrder の HTTP ステータスを検査する（Issue #207 (b) L1）。

    従来は ``raise_for_gmo_status`` しか通しておらず、HTTP 5xx で JSON でないボディが
    返ると**何も検知せず「受付成功」として記録**していた（``_parse_body`` が None を
    返して素通りするため）。HTTP エラーは「受付されたか不明」なので、
    ``GmoApiError``（明示的な拒否）とは**別の型**に落として再送対象から外す。
    """
    status = getattr(resp, "status_code", None)
    if status is not None and status >= 400:
        raise CloseOrderTransportError(
            f"closeOrder が HTTP {status} を返しました（受付されたか不明のため再送しません）"
        )


def _decide_close_retry(
    client: ForexClient, symbol: str, chunk_ids: set[str]
) -> _CloseRetryDecision:
    """closeOrder を再送してよいかをゲート判定する（Issue #207 (b) L1）。

    | 観測 | 判定 |
    |---|---|
    | 対象の建玉が消えた | ``ALREADY_CLOSED``（成功扱い・再送しない） |
    | 建玉あり かつ ``orderedSize != 0`` | 有効注文が動いている → ポーリングし、消えなければ ``ABORT`` |
    | 建玉あり かつ ``orderedSize == 0`` | ``RETRY`` |
    | ``openPositions`` の GET 自体が失敗 | ``ABORT``（**見えないのに撃たない**） |

    ``orderedSize`` を見るのは、``closeOrder`` の成功が約定を保証せず（``WAITING`` = 受付の
    こともある）、反映にラグがあるため（Issue #241 で実測）。建玉が残っていることは
    「受付されていない」ことの証明にはならない。
    """
    for attempt in range(1, _CLOSE_RETRY_GATE_POLL_ATTEMPTS + 1):
        try:
            positions = _fetch_open_positions(client, symbol)
        except Exception:
            _log.exception(
                "cancel_first_close_retry_gate_fetch_failed",
                symbol=symbol,
                rule="見えないのに再送しない（Issue #207 L1）",
            )
            return _CloseRetryDecision.ABORT

        remaining = [
            p
            for p in positions
            if isinstance(p, dict) and _position_key(p.get("positionId")) in chunk_ids
        ]
        if not remaining:
            return _CloseRetryDecision.ALREADY_CLOSED

        pending = [p for p in remaining if _is_nonzero_size(p.get("orderedSize"))]
        if not pending:
            return _CloseRetryDecision.RETRY

        if attempt == _CLOSE_RETRY_GATE_POLL_ATTEMPTS:
            break
        _log.info(
            "cancel_first_close_retry_gate_poll",
            symbol=symbol,
            attempt=attempt,
            pending_position_ids=[_position_key(p.get("positionId")) for p in pending],
            rule="有効注文が動いている間は再送しない（Issue #207 L1）",
        )
        time.sleep(_CLOSE_RETRY_GATE_POLL_INTERVAL_SECONDS)

    _log.warning(
        "cancel_first_close_retry_gate_ordered_size_pending",
        symbol=symbol,
        chunk_ids=sorted(chunk_ids),
        rule="orderedSize が残ったままのため再送を見送る（二重受付を避ける）",
    )
    return _CloseRetryDecision.ABORT


def _submit_close_chunk(
    client: ForexClient,
    symbol: str,
    close_side: Literal["BUY", "SELL"],
    chunk: list[SettlePositionItem],
    *,
    chunk_index: int,
    chunk_total: int,
) -> None:
    """1 チャンクを closeOrder へ送る。**確定拒否のみ**ゲート付きで再送する（Issue #207 L1）。

    再送対象は ``GmoApiError``（HTTP 応答を受け取り GMO が明示的に拒否＝未受付が確実）だけ。
    特定のエラーコードを想定した仕組みではない（かつて典型例として ERR-423 を挙げていたが、
    **2026-08-11 の実測でその経路は再現しなかった**。詳細は
    docs/spikes/ordered_size_err423_latency.md 実験1）。拒否コードが何であれ
    「明示的に拒否された＝未受付が確実」なら再送してよい、という判断軸は変わらない。

    タイムアウト・接続断・HTTP エラーは「受付済みかもしれない」ため**再送しない**。
    ただし**失敗として送出はする**（呼び出し元の fail-closed と kill switch の判定を壊さない）。
    """
    chunk_ids = {_position_key(item.position_id) for item in chunk}
    body = CloseOrderRequest(symbol=symbol, side=close_side, settlePosition=list(chunk))

    for attempt in range(1, _CLOSE_RETRY_MAX_ATTEMPTS + 1):
        try:
            resp = client.private_post(PRIVATE_CLOSE_ORDER, body)
            _raise_for_close_http_status(resp)
            raise_for_gmo_status(resp, context="closeOrder")
        except GmoApiError as exc:
            if attempt == _CLOSE_RETRY_MAX_ATTEMPTS:
                raise
            decision = _decide_close_retry(client, symbol, chunk_ids)
            if decision is _CloseRetryDecision.ALREADY_CLOSED:
                _log.info(
                    "cancel_first_close_already_closed",
                    symbol=symbol,
                    position_ids=sorted(chunk_ids),
                    note="拒否応答だったが建玉は消えていた（再送不要）",
                )
                return
            if decision is _CloseRetryDecision.ABORT:
                raise
            _log.warning(
                "cancel_first_close_retry",
                symbol=symbol,
                attempt=attempt,
                max_attempts=_CLOSE_RETRY_MAX_ATTEMPTS,
                message_codes=list(exc.message_codes),
                position_ids=sorted(chunk_ids),
                rule="確定拒否 かつ 有効注文なし → 再送可（Issue #207 L1）",
            )
            # 再送の前も POST 間隔を守る（AGENTS.md #3）
            _sleep_between_posts()
            continue

        _log.info(
            "cancel_first_close_order_submitted",
            symbol=symbol,
            close_side=close_side,
            position_ids=[item.position_id for item in chunk],
            position_count=len(chunk),
            chunk_index=chunk_index,
            chunk_total=chunk_total,
            attempt=attempt,
            rule="§6.2 Step3 / settlePosition 最大10件",
        )
        return


def _verify_close_with_poll(
    client: ForexClient, symbol: str, submitted_keys: list[str]
) -> ClosePositionsOutcome:
    """決済後の建玉を短ポーリングし、残存が一時的か恒久かを判別する（Issue #207 L1）。

    ``closeOrder`` の成功は約定を保証しない（``status`` は ``WAITING`` = 受付のことも、
    ``EXECUTED`` = 同期約定のこともある）。直後 1 回の GET だけでは
    「約定待ちの一時的な残存」と「本当に閉じられていない」を区別できなかった。数秒ポーリングしても消えない場合のみ ``residual_ids`` に載せる。
    → 呼び出し元はこれを**強アラート対象**として扱ってよい（PR #247 レビュー指摘 1）。
    """
    remaining_keys: set[str] = set()

    for attempt in range(1, _RESIDUAL_POLL_ATTEMPTS + 1):
        try:
            remaining = _fetch_open_positions(client, symbol)
        except Exception:
            # 再取得失敗 → クローズ結果を確認できないため fail-closed
            _log.exception("cancel_first_refetch_failed_after_close", symbol=symbol)
            return ClosePositionsOutcome(verification_failed=True)

        remaining_keys = {
            _position_key(p.get("positionId")) for p in remaining if isinstance(p, dict)
        }
        if not any(key in remaining_keys for key in submitted_keys):
            if attempt > 1:
                _log.info(
                    "cancel_first_step3_residual_cleared",
                    symbol=symbol,
                    attempts=attempt - 1,
                    waited_seconds=(attempt - 1) * _RESIDUAL_POLL_INTERVAL_SECONDS,
                )
            return ClosePositionsOutcome(closed_ids=list(submitted_keys))

        if attempt == _RESIDUAL_POLL_ATTEMPTS:
            break
        time.sleep(_RESIDUAL_POLL_INTERVAL_SECONDS)

    closed_ids = [key for key in submitted_keys if key not in remaining_keys]
    residual_ids = [key for key in submitted_keys if key in remaining_keys]
    _log.warning(
        "cancel_first_step3_residual_positions",
        symbol=symbol,
        residual_ids=residual_ids,
        residual_count=len(residual_ids),
        polled_seconds=(_RESIDUAL_POLL_ATTEMPTS - 1) * _RESIDUAL_POLL_INTERVAL_SECONDS,
        note="ポーリング後も残存＝一時的な約定待ちではない。SL/TP なしの可能性がある",
    )
    return ClosePositionsOutcome(closed_ids=closed_ids, residual_ids=residual_ids)


def _close_positions(
    client: ForexClient, symbol: str, positions: list[dict[str, Any]]
) -> ClosePositionsOutcome:
    """建玉を成行クローズする（§6.2 Step3: closeOrder + settlePosition バッチ）。

    公式仕様（出典: https://api.coin.z.com/fxdocs/・2026-07-30 参照）:
      「size settlePosition いずれか1つが必須です。2つ同時には設定できません。」
      ``settlePosition`` は最大10件／``positionId`` は number／``size`` は string。
    → 建玉指定の決済は ``settlePosition`` 配列で行い、トップレベル ``size`` は送らない
      （送ると ERR-5214）。FX API に closeBulkOrder は存在しない（Issue #230）。

    手順:
      1. 全建玉を POST 前に検証・正規化する（不正なら 1 件も送らず fail-closed）。
      2. 決済方向でグループ化し（決定論的に sorted）、≤10 件ずつ closeOrder に投げる。
      3. 全 POST 後に建玉を **1 回だけ**再取得し、差分でクローズを確認する。
         closeOrder の成功は約定確認ではないため（WAITING = 受付のこともある）、
         残存は ``residual_ids`` として記録する。再取得失敗はクローズ確認が
         取れないため fail-closed（closed_ids 空 + verification_failed）。
    """
    groups = _build_settle_groups(symbol, positions)
    submitted_keys = [
        _position_key(item.position_id)
        for close_side in sorted(groups)
        for item in groups[close_side]
    ]

    # 決済方向でグループ化して送信する。closeOrder の side は「決済注文の売買区分」なので
    # BUY 建玉と SELL 建玉を同一リクエストに混在させられない。
    # sorted() で順序を固定する（set のままだと文字列ハッシュのランダム化で POST 順が
    # 再現不能になり、監査証跡とテストの両方が非決定になる）。
    for close_side in sorted(groups):
        chunks = list(_chunked(groups[close_side], MAX_SETTLE_POSITIONS))
        for index, chunk in enumerate(chunks, start=1):
            _submit_close_chunk(
                client,
                symbol,
                close_side,
                chunk,
                chunk_index=index,
                chunk_total=len(chunks),
            )
            # 🔴 スリープはチャンク境界で **必ず** 行う（AGENTS.md #3）。
            # _submit_close_chunk の内側に置くと、拒否応答だが建玉は消えていた
            # （ALREADY_CLOSED）経路で return したときに抜けてしまい、
            # 多チャンク時に次の POST が 1 秒未満になる（#248 レビュー指摘 1）。
            # 最終チャンクの後もスリープするのは、直後の検証 GET までに
            # 約定反映の猶予を確保する意図もあるため。
            _sleep_between_posts()

    return _verify_close_with_poll(client, symbol, submitted_keys)


def _build_settle_groups(
    symbol: str, positions: list[dict[str, Any]]
) -> dict[Literal["BUY", "SELL"], list[SettlePositionItem]]:
    """建玉を検証・正規化し、決済方向ごとの settlePosition 要素にまとめる。

    POST を 1 件も送る前に全建玉を検証し、欠落・不正があれば例外に倒す（fail-closed）。
    旧実装は ``p.get("positionId", "")`` / ``p.get("size", "0")`` / ``side`` の暗黙
    ``BUY`` 既定でこれらを黙って送っており、空 ID・数量 0・逆方向決済の危険があった。
    """
    groups: dict[Literal["BUY", "SELL"], list[SettlePositionItem]] = {}
    seen: set[int] = set()

    for position in positions:
        if not isinstance(position, dict):
            _log.error(
                "cancel_first_position_malformed",
                symbol=symbol,
                entry_type=type(position).__name__,
            )
            raise ValueError("openPositions の要素が dict ではありません")

        position_id = _require_int(
            position.get("positionId"),
            field_name="positionId",
            context="openPositions",
        )
        size = _require_size(position.get("size"), position_id=position_id)
        close_side = _close_side_for(position.get("side"), position_id=position_id)

        if position_id in seen:
            # ERR-512「決済注文において同じ建玉IDを2つ以上指定した場合」を回避する
            _log.warning(
                "cancel_first_duplicate_position_id",
                symbol=symbol,
                position_id=position_id,
                expected_error="ERR-512",
            )
            continue
        seen.add(position_id)

        if _is_nonzero_size(position.get("orderedSize")):
            # ``orderedSize != 0`` = その建玉にまだ有効注文が紐づいているサイン。
            # Step2 の cancel は「受付」（非同期）のため反映ラグで残ることがある。
            #
            # 🔴 かつては「ERR-423 の後半条件に触れて close が失敗する予兆」としていたが、
            #    **2026-08-11 の実測（n=2）で拒否は再現しなかった**（orderedSize 満額のまま
            #    満額 close しても status:0 / EXECUTED で通る）。ラベルを事実に合わせる。
            #    ただし実測スコープは MARKET / 満額 / 単一建玉 / 開場中で、分割決済・
            #    複数建玉・LIMIT 決済・閉場前後は未検証のため、**観測は続ける**。
            # size は満額のまま送り、失敗した場合は fail-closed で次回起動に委ねる。
            _log.warning(
                "cancel_first_close_ordered_size_nonzero",
                symbol=symbol,
                position_id=position_id,
                size=size,
                ordered_size=position.get("orderedSize"),
                rule="§6.2 Step2 の cancelOrders は受付＝非同期",
                note="実測（2026-08-11・MARKET/満額/単一建玉・n=2）では拒否されず約定した。"
                "未検証条件があるため観測のみ継続",
            )

        groups.setdefault(close_side, []).append(
            SettlePositionItem(positionId=position_id, size=size)
        )

    return groups


def _close_side_for(raw_side: Any, *, position_id: int) -> Literal["BUY", "SELL"]:
    """建玉の売買区分から決済注文の売買区分（反対売買）を決める。

    不正値を暗黙に ``BUY`` へ倒すと逆方向の決済注文になり建玉が増えるため、
    BUY / SELL 以外は例外にする（fail-closed）。
    """
    if raw_side == "BUY":
        return "SELL"
    if raw_side == "SELL":
        return "BUY"
    _log.error(
        "cancel_first_position_side_invalid",
        position_id=position_id,
        side=raw_side,
        rule="決済方向を誤ると建玉が増える",
    )
    raise ValueError(f"建玉 {position_id} の side が不正です: {raw_side!r}")


def _require_int(raw: Any, *, field_name: str, context: str) -> int:
    """公式仕様が number のフィールドを int に正規化する（欠落・非数値は fail-closed）."""
    if raw is None or raw == "":
        _log.error(
            "cancel_first_field_missing", field=field_name, context=context
        )
        raise ValueError(f"{context}: {field_name} が欠落しています")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        _log.error(
            "cancel_first_field_not_numeric",
            field=field_name,
            context=context,
            value=raw,
            rule="公式仕様は number（ERR-5106 を待たずローカルで落とす）",
        )
        raise ValueError(
            f"{context}: {field_name} が数値ではありません: {raw!r}"
        ) from exc


def _require_size(raw: Any, *, position_id: int) -> str:
    """建玉数量を検証する。GMO が返した文字列を**無加工**で使う（推測禁止）."""
    if raw is None or raw == "":
        _log.error("cancel_first_position_size_missing", position_id=position_id)
        raise ValueError(f"建玉 {position_id} の size が欠落しています")
    if not isinstance(raw, str):
        # 公式仕様は string。型が違う場合は正規形が不明なので推測せず落とす。
        _log.error(
            "cancel_first_position_size_not_string",
            position_id=position_id,
            value_type=type(raw).__name__,
            rule="公式仕様: openPositions の size は string",
        )
        raise ValueError(f"建玉 {position_id} の size が文字列ではありません: {raw!r}")
    if not _is_nonzero_size(raw):
        _log.error("cancel_first_position_size_zero", position_id=position_id)
        raise ValueError(f"建玉 {position_id} の size が 0 です（ERR-5126 になる）")
    return raw


def _is_nonzero_size(raw: Any) -> bool:
    """数量文字列が 0 以外か判定する（None / 空は 0 扱い、解釈不能は非0 扱い）."""
    if raw is None or raw == "":
        return False
    try:
        return Decimal(str(raw)) != 0
    except (InvalidOperation, ValueError):
        # 解釈できない値は「残っているかもしれない」側に倒して警告対象にする
        return True


def _position_key(raw: Any) -> str:
    """建玉ID / 親注文ID の照合キーを 1 箇所で正規化する。

    ``openPositions`` は number を返すが、モックやレスポンス差異で文字列が来ることも
    あるため、``1000342`` と ``"1000342"`` を同一キーに寄せる（int / str 混在で
    決済確認の差分計算が壊れるのを防ぐ）。
    """
    try:
        return str(int(raw))
    except (TypeError, ValueError):
        return str(raw)


def _chunked(items: Iterable[Any], size: int) -> Iterator[list[Any]]:
    """イテラブルを最大 ``size`` 件のリストに分割する（公式の件数上限に合わせる）."""
    iterator = iter(items)
    while chunk := list(islice(iterator, size)):
        yield chunk


def _warn_if_page_limit_reached(
    entries: list[Any], *, source: str, symbol: str
) -> None:
    """取得件数が公式の count 最大値に達していたら警告する（prevId 未対応の tripwire）."""
    if len(entries) >= _LIST_PAGE_LIMIT:
        _log.warning(
            "cancel_first_page_limit_reached",
            source=source,
            symbol=symbol,
            count=len(entries),
            page_limit=_LIST_PAGE_LIMIT,
            rule="count 既定=最大100・prevId ページング未対応",
        )


def _extract_data(resp: Any) -> Any:
    """レスポンスオブジェクトから data フィールドを取り出す。

    実クライアントは httpx.Response（.json() 経由）、
    モックは直接 dict を返す場合があるため両方に対応する。
    """
    if hasattr(resp, "json"):
        parsed = resp.json()
        return parsed.get("data") if isinstance(parsed, dict) else None
    if isinstance(resp, dict):
        return resp.get("data")
    return getattr(resp, "data", None)


def _sleep_between_posts() -> None:
    """POST 間に ≥1 秒スリープする（AGENTS.md #3, §6.2）。"""
    time.sleep(_POST_SLEEP_SECONDS)
