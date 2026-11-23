"""GMO FX API Pydantic リクエスト/レスポンスモデル骨格 (§1.4).

フィールド名・型は付録B の実レスポンス確認後に拡充する。
現時点では受け入れ条件に必要な最低限の骨格のみ定義する。

GMO API は camelCase フィールドを返す。ruff N815（クラス内 mixedCase 禁止）を満たすため
snake_case フィールドに alias でマッピングし、model_config で populate_by_name=True とする。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# 公式仕様に由来する件数上限（出典: https://api.coin.z.com/fxdocs/・2026-07-30 参照）
# 詳細は docs/spikes/cancel_first_official_spec.md（付録B・Issue #230）
# ---------------------------------------------------------------------------

#: closeOrder の settlePosition は「最大10件まで指定可能」（公式パラメータ表）
MAX_SETTLE_POSITIONS = 10
#: cancelOrders は「最大10件まで注文を取消することができます」（公式注記）
MAX_CANCEL_ROOT_ORDER_IDS = 10

# ---------------------------------------------------------------------------
# 共通レスポンス
# ---------------------------------------------------------------------------


class GmoApiResponse(BaseModel):
    """GMO FX API の共通レスポンスラッパー."""

    status: int
    responsetime: str
    data: object | None = None


# ---------------------------------------------------------------------------
# IFD-OCO 発注 POST /private/v1/ifoOrder (§1.4, §3.4)
# ---------------------------------------------------------------------------


class IfoOrderRequest(BaseModel):
    """IFD-OCO 発注リクエスト.

    実スキーマは E06 スパイク（付録B docs/spikes/E06_ifoOrder_mapping.md §2）で確定。
    first* = エントリー（IFD 親）、second* = 決済 OCO ペア。
    secondStopPrice（SL）は AGENTS.md #5 に従い Python が §3.2 で算出し、LLM には出させない。
    """

    model_config = ConfigDict(populate_by_name=True)

    symbol: str = "USD_JPY"
    first_side: Literal["BUY", "SELL"] = Field(alias="firstSide")
    first_execution_type: Literal["LIMIT", "STOP"] = Field(
        default="LIMIT", alias="firstExecutionType"
    )
    first_size: int = Field(alias="firstSize")
    first_price: str = Field(alias="firstPrice")
    second_size: int = Field(alias="secondSize")
    second_limit_price: str = Field(alias="secondLimitPrice")
    second_stop_price: str = Field(alias="secondStopPrice")

    @field_validator("first_size", "second_size")
    @classmethod
    def size_must_be_valid_lot(cls, v: int) -> int:
        """ロット下限100・100倍数を強制する (§3.4, AGENTS.md #10)."""
        if v < 100:
            raise ValueError(f"size は最小 100 通貨が必要です: {v}")
        if v % 100 != 0:
            raise ValueError(f"size は 100 の倍数でなければなりません: {v}")
        return v

    @field_serializer("first_size", "second_size")
    def serialize_size_as_string(self, v: int) -> str:
        """size を **整数文字列** でワイヤーに載せる（2026-08-03 実発注スパイクで確定）.

        🔴 JSON number で送ると ERR-5105「Request parameter include mismatch type」で
        価格検証の手前に短絡し、**発注が1件も通らない**。E06 の記録
        （docs/spikes/E06_ifoOrder_mapping.md「100 以上・整数文字列」）どおり str 化する。

        Python 側は int のまま保持し、ロット下限100・100倍数の検証（上記 validator）を
        効かせ続ける。型を str にしないのはこの検証を失わないため。
        """
        return str(v)


class IfoOrderItem(BaseModel):
    """ifoOrder 成功レスポンスの 1 注文.

    実レスポンス確定（2026-08-03 スパイク・#91）: IFD-OCO は **3 件**返る。
    settleType=OPEN（エントリー）1 件 + settleType=CLOSE（TP の LIMIT / SL の STOP）2 件。
    3 件とも同じ ``rootOrderId`` を共有し、エントリーの ``orderId`` == ``rootOrderId``。
    """

    model_config = ConfigDict(populate_by_name=True)

    root_order_id: int = Field(alias="rootOrderId")
    order_id: int = Field(alias="orderId")
    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: str = Field(alias="orderType")
    execution_type: str = Field(alias="executionType")
    settle_type: Literal["OPEN", "CLOSE"] = Field(alias="settleType")
    size: str
    price: str
    status: str
    expiry: str | None = None
    timestamp: str | None = None


class IfoOrderResponse(BaseModel):
    """IFD-OCO 発注レスポンス.

    🔴 ``data`` は **注文オブジェクトの配列**（2026-08-03 スパイクで確定・#91）。
    従来は ``str | None``（注文ID直値）と推測していたが実際は list であり、
    ``data.get("data")`` をそのまま order_id に使うと壊れた値が永続化される。
    後続の cancel-first は ``rootOrderIds`` を使うため、保存すべきは ``rootOrderId``。
    """

    status: int
    # メタデータ。欠落しても注文ID抽出は続行する（これで unexpected_data_shape に
    # 倒すと、タイムスタンプが無いだけで「受理有無 未確定」扱いになり過剰に保守的）。
    responsetime: str | None = None
    data: list[IfoOrderItem] | None = None

    @model_validator(mode="after")
    def root_order_ids_must_agree(self) -> IfoOrderResponse:
        """全件が同一 ``rootOrderId`` を共有するという不変条件を実行時に強制する.

        実 API では 3 件とも同値（2026-08-03 スパイクで確認）。万一食い違ったら
        どれを親として保存すべきか決められないため、ここで弾いて呼び出し側の
        ``unexpected_data_shape``（未確定・ロック保持）へ倒す。壊れた ID を
        DynamoDB に書いて翌朝の closeout を壊すより安全。
        """
        if self.data:
            roots = {item.root_order_id for item in self.data}
            if len(roots) > 1:
                raise ValueError(f"rootOrderId が注文間で一致しません: {sorted(roots)}")
        return self

    def root_order_id(self) -> int | None:
        """cancel-first / 約定照合で使う親注文ID を返す（全件が同一値・上記 validator で担保）."""
        if not self.data:
            return None
        return self.data[0].root_order_id


# ---------------------------------------------------------------------------
# 口座資産 GET /private/v1/account/assets (§1.4)
# ---------------------------------------------------------------------------


# E04確定(#11): data は単一 dict（全フィールド str）。FX 口座は JPY 単一のため list ではない。
class AccountAssetsData(BaseModel):
    """account/assets の data 辞書（E04 スパイク確定・全フィールド文字列）.

    marginRatio は建玉ゼロ時に sentinel 巨大値(≒uint64 max)を返すため、
    実数比較には使わず margin_ratio プロパティ経由でアクセスすること。
    """

    model_config = ConfigDict(populate_by_name=True)

    equity: Decimal
    available_amount: Decimal = Field(alias="availableAmount")
    balance: Decimal
    transferable_amount: Decimal = Field(alias="transferableAmount")
    margin: Decimal
    position_loss_gain: Decimal = Field(alias="positionLossGain")
    total_swap: Decimal = Field(alias="totalSwap")
    estimated_trade_fee: Decimal = Field(alias="estimatedTradeFee")
    margin_ratio_raw: str = Field(alias="marginRatio")

    @property
    def margin_ratio(self) -> Decimal | None:
        """証拠金維持率。建玉ゼロ時の sentinel（9.2e20 超）は None を返す."""
        v = Decimal(self.margin_ratio_raw)
        return None if v > Decimal("1e18") else v


class AccountAssetsResponse(BaseModel):
    """account/assets レスポンス（E04 スパイク確定、data は単一辞書）."""

    status: int
    responsetime: str
    data: AccountAssetsData | None = None


# ---------------------------------------------------------------------------
# 建玉照会 GET /private/v1/openPositions (§1.4)
# ---------------------------------------------------------------------------


class PositionItem(BaseModel):
    """建玉一覧の要素.

    公式仕様（出典: https://api.coin.z.com/fxdocs/ GET /private/v1/openPositions・2026-07-30 参照）:
    ``positionId`` は **number**、``size`` / ``orderedSize`` は string。

    ``orderedSize``（注文中数量）が 0 以外 = その建玉にまだ有効注文が紐づいている。
    🔴 かつて「ERR-423 の予兆」と記していたが、**2026-08-11 の実測（n=2）で拒否は
    再現しなかった**（``orderedSize`` 満額のまま満額 close しても ``status:0`` で約定）。
    実測スコープは MARKET / 満額 / 単一建玉 / 開場中。
    詳細: docs/spikes/ordered_size_err423_latency.md 実験1

    NOTE: 実レスポンスの ``data`` は ``{"list": [...]}`` 形式であり ``OpenPositionsResponse`` は
    未整合（本モデルは未参照・cancel_first は生 dict を扱うため実害なし。整合は別 Issue）。
    """

    model_config = ConfigDict(populate_by_name=True)

    position_id: int = Field(alias="positionId")
    symbol: str
    side: Literal["BUY", "SELL"]
    size: str
    ordered_size: str | None = Field(default=None, alias="orderedSize")
    price: str
    loss_gain: str | None = Field(default=None, alias="lossGain")
    timestamp: str


class OpenPositionsResponse(BaseModel):
    status: int
    responsetime: str
    data: list[PositionItem] | None = None


# ---------------------------------------------------------------------------
# 約定照会 GET /private/v1/executions (§1.4)
# ---------------------------------------------------------------------------


class ExecutionItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    execution_id: str = Field(alias="executionId")
    order_id: str = Field(alias="orderId")
    symbol: str
    side: Literal["BUY", "SELL"]
    settle_type: str = Field(alias="settleType")
    size: str
    price: str
    loss_gain: str | None = Field(default=None, alias="lossGain")
    fee: str | None = None
    timestamp: str


class ExecutionsResponse(BaseModel):
    status: int
    responsetime: str
    data: list[ExecutionItem] | None = None


# ---------------------------------------------------------------------------
# 有効注文照会 GET /private/v1/activeOrders (§1.4)
# ---------------------------------------------------------------------------


class ActiveOrderItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    order_id: str = Field(alias="orderId")
    symbol: str
    side: Literal["BUY", "SELL"]
    execution_type: str = Field(alias="executionType")
    settle_type: str = Field(alias="settleType")
    size: str
    price: str | None = None
    status: str
    timestamp: str


class ActiveOrdersResponse(BaseModel):
    status: int
    responsetime: str
    data: list[ActiveOrderItem] | None = None


# ---------------------------------------------------------------------------
# 注文キャンセル POST /private/v1/cancelOrders (§1.4)
# ---------------------------------------------------------------------------


class CancelOrdersRequest(BaseModel):
    """注文キャンセルリクエスト（親注文ID 指定・最大10件）.

    公式仕様（出典: https://api.coin.z.com/fxdocs/ POST /private/v1/cancelOrders・2026-07-30 参照）:
      「rootOrderIds clientOrderIds いずれか1つが必須です。2つ同時には設定できません。
        最大10件まで注文を取消することができます。」
    公式リクエスト例は ``{"rootOrderIds": [123456789, 223456789]}``（**number 配列**）。

    🔴 ``orderId`` というパラメータは存在しない（Issue #230）。指定するのは activeOrders の
    ``orderId``（注文ID）ではなく ``rootOrderId``（親注文ID）である点に注意。
    ``clientOrderIds`` は本システムが顧客注文IDを設定していないため定義しない
    （未使用 Optional を定義すると ``null`` が実送信され ERR-5106 の温床になる）。
    """

    model_config = ConfigDict(populate_by_name=True)

    root_order_ids: list[int] = Field(alias="rootOrderIds")

    @field_validator("root_order_ids")
    @classmethod
    def _validate_root_order_ids(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("rootOrderIds は 1 件以上必要です")
        if len(v) > MAX_CANCEL_ROOT_ORDER_IDS:
            raise ValueError(
                f"rootOrderIds は最大 {MAX_CANCEL_ROOT_ORDER_IDS} 件まで: {len(v)} 件"
            )
        if len(set(v)) != len(v):
            raise ValueError(f"rootOrderIds に重複があります: {v}")
        return v


class CancelOrdersResponse(BaseModel):
    status: int
    responsetime: str
    data: object | None = None


# ---------------------------------------------------------------------------
# 建玉決済 POST /private/v1/closeOrder (§1.4)
#
# 🔴 FX API に建玉一括決済エンドポイント（closeBulkOrder）は存在しない。
#    一括決済は closeOrder の settlePosition（最大10件/回）で行う。
#    出典: https://api.coin.z.com/fxdocs/（2026-07-30 参照）/ 詳細は
#    docs/spikes/cancel_first_official_spec.md（付録B・Issue #230）
# ---------------------------------------------------------------------------


class SettlePositionItem(BaseModel):
    """closeOrder の ``settlePosition`` 配列要素.

    公式仕様（同上）: ``settlePosition.positionId`` は **number**、``settlePosition.size`` は **string**。

    ⚠️ 公式パラメータ表の行ラベルだけが ``settlePostion``（``i`` 欠落）と誤植だが、
    公式リクエスト例（JavaScript / Python / Go / Swift）はすべて ``settlePosition``。
    誤植には追随しない。

    ``position_id`` を ``int`` にしているのは公式が number だから。pydantic は数値文字列を
    int に正規化するため openPositions が文字列で返しても公式のワイヤ形式で送れる一方、
    非数値（例 ``"pos-1"``）は POST 前に ValidationError で落ちる（ERR-5106 の往復を待たない）。
    """

    model_config = ConfigDict(populate_by_name=True)

    position_id: int = Field(alias="positionId")
    size: str


class CloseOrderRequest(BaseModel):
    """決済注文リクエスト（``settlePosition`` 方式・成行のみ）.

    公式注記（同上）: 「size settlePosition いずれか1つが必須です。2つ同時には設定できません。」

    🔴 本モデルは **トップレベル ``size`` フィールドを持たない**。これにより排他制約を
    型構造で保証する（validator より強い保証で、ERR-5214「決済注文において、建玉IDを
    指定している場合に注文数量を指定すると返ってきます」を構造的に不可能にする）。
    ``size`` 方式（建玉指定なしの一括決済）が必要になったら別モデルを追加すること。

    🔴 トップレベル ``positionId`` というパラメータは公式仕様に存在しない（Issue #230 の欠陥）。

    ``executionType`` を ``MARKET`` に狭め、``limitPrice`` / ``stopPrice`` / ``lowerBound`` /
    ``upperBound`` / ``clientOrderId`` を定義しないのは、本システムが成行決済しか使わないため。
    未使用 Optional を定義すると ``model_dump_json`` が ``null`` を実送信し ERR-5106 を招く。
    """

    model_config = ConfigDict(populate_by_name=True)

    symbol: str = "USD_JPY"
    side: Literal["BUY", "SELL"]
    execution_type: Literal["MARKET"] = Field(default="MARKET", alias="executionType")
    settle_position: list[SettlePositionItem] = Field(alias="settlePosition")

    @field_validator("settle_position")
    @classmethod
    def _validate_settle_position(
        cls, v: list[SettlePositionItem]
    ) -> list[SettlePositionItem]:
        if not v:
            raise ValueError("settlePosition は 1 件以上必要です")
        if len(v) > MAX_SETTLE_POSITIONS:
            raise ValueError(
                f"settlePosition は最大 {MAX_SETTLE_POSITIONS} 件まで: {len(v)} 件"
            )
        position_ids = [item.position_id for item in v]
        if len(set(position_ids)) != len(position_ids):
            # ERR-512「決済注文において同じ建玉IDを2つ以上指定した場合に返ってきます」
            raise ValueError(f"settlePosition に重複 positionId があります: {position_ids}")
        return v


class CloseOrderResultItem(BaseModel):
    """closeOrder レスポンス ``data`` の要素（公式仕様・同上）.

    🔴 ``status`` は ``WAITING`` / ``EXECUTED`` / ``EXPIRED``。``HTTP 200 / status:0`` は
    **注文の受付成功**を意味するだけで、建玉が閉じた証拠ではない。決済完了は
    openPositions の差分または executions で確認する（§6.2）。
    """

    model_config = ConfigDict(populate_by_name=True)

    root_order_id: int = Field(alias="rootOrderId")
    order_id: int = Field(alias="orderId")
    client_order_id: str | None = Field(default=None, alias="clientOrderId")
    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: str = Field(alias="orderType")
    execution_type: str = Field(alias="executionType")
    settle_type: str = Field(alias="settleType")  # 決済区分: "CLOSE"
    size: str
    price: str | None = None  # LIMIT / STOP の場合のみ返却
    status: str
    expiry: str | None = None
    timestamp: str


class CloseOrderResponse(BaseModel):
    status: int
    responsetime: str
    data: list[CloseOrderResultItem] | None = None


# ---------------------------------------------------------------------------
# KLine（Public）GET /public/v1/klines (§1.4)
# ---------------------------------------------------------------------------


class KlineItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    open_time: str = Field(alias="openTime")
    open: str
    high: str
    low: str
    close: str
    volume: str


class KlinesResponse(BaseModel):
    status: int
    responsetime: str
    data: list[KlineItem] | None = None
