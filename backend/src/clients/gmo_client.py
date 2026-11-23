"""GMO FX API 統合 HTTP クライアント (§1.4, §7.2).

- Public API（KLine 等）は署名不要で GET できる。
- Private API は build_headers + request_with_retry で署名付き送信する。
- RateLimiter で POST ≥1.0秒 / GET ≥167ms のレートリミットを強制する。
- pre_attempt_hook により 401 リトライ前にもレートリミット待機を挟む。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
import structlog
from pydantic import BaseModel

from backend.src.gmo_errors import raise_for_gmo_status
from backend.src.gmo_fx_endpoints import (
    GMO_FX_HOST,
    PRIVATE_CANCEL_ORDERS,
    PRIVATE_CLOSE_ORDER,
    PRIVATE_EXECUTIONS,
    PRIVATE_IFO_ORDER,
    PRIVATE_LATEST_EXECUTIONS,
    PUBLIC_STATUS,
    to_sign_path,
)
from backend.src.rate_limiter import RateLimiter
from backend.src.retry import request_with_retry

_log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# 実送信を封じ込める発注系エンドポイント (AGENTS.md #1)
# FX API の発注系 POST はこの 3 つのみ。closeBulkOrder は FX API に存在しない（#230）ため
# 監視対象から外した（実送信するパス closeOrder は残るので封じ込めは弱化しない）。
_ORDER_PATHS: frozenset[str] = frozenset({
    PRIVATE_IFO_ORDER,
    PRIVATE_CLOSE_ORDER,
    PRIVATE_CANCEL_ORDERS,
})

# 実発注の解禁条件 (AGENTS.md #1)。
# 単一フラグの設定ミスで実送信されるのを防ぐため、独立した2つの明示設定を要求する
# (two-key interlock / defense-in-depth)。MVP_MODE（=100通貨固定。§4.2）とは責務を分離する。
_LIVE_ORDER_FLAG = "ALLOW_LIVE_ORDERS"   # "true"（大小非依存）で発注解禁の主フラグ
_PHASE_FLAG = "TRADING_PHASE"            # 実行フェーズ（既定 "1" = モック）
# 実送信を許すフェーズ: "2"=Phase 2 実取引 / "spike"=E06 ドライ疎通
_LIVE_PHASES: frozenset[str] = frozenset({"2", "spike"})

# 内部生成 httpx.Client の送信元 IPv4 バインドアドレス（GMO IP 制限が IPv4 のみ・ERR-5012 回避）。
# "0.0.0.0" は「OS が選んだ IPv4 アドレスから送出する」を意味し、IPv6 egress を抑止する。
_IPV4_BIND_ADDR: str = "0.0.0.0"

# latestExecutions の count 上限＝既定値。公式仕様「取得件数: 指定しない場合は100(最大値)」
# （出典: https://api.coin.z.com/fxdocs/・2026-08-05 参照）。同 API の取得範囲は
# 「直近1日分から最新100件」であり、これを超えて遡る手段は FX API に存在しない。
MAX_LATEST_EXECUTIONS_COUNT: int = 100

# 約定取得（読み取り専用 GET）の invocation 内リトライ（Issue #245）。
# 約定を取れる API は latestExecutions（直近1日分）しか無く、手仕舞い cron は 1 日 1 回。
# つまり**翌日の再実行では取り逃した明細を回収できない**ため、一過性の失敗はこの場で吸収する。
# DEADLINE は Lambda タイムアウト（既定 120 秒）を守るための時間予算。httpx のタイムアウトが
# 30 秒あるので、ハング系の失敗ではリトライせず即座に諦める（安い失敗だけ粘る）。
_EXECUTION_FETCH_MAX_ATTEMPTS: int = 3
_EXECUTION_FETCH_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0)
_EXECUTION_FETCH_DEADLINE_SECONDS: float = 20.0


def live_orders_allowed() -> bool:
    """実発注が解禁されているか（二重条件）— two-key interlock の**唯一の**判定関数.

    ALLOW_LIVE_ORDERS が "true"（大小非依存・前後空白無視）かつ TRADING_PHASE が
    実取引フェーズ（"2" / "spike"、前後空白無視）の両方を満たすときのみ True。
    Phase 1 で片方だけ設定ミスしても実送信を防ぐ（AGENTS.md #1）。

    パース仕様は Terraform が供給する canonical 値に合わせる:
    infra/terraform/modules/serverless_app/locals.tf は小文字 "true" / "false" を渡すため、
    大文字厳格一致（== "True"）にすると Phase 2 で実発注が永久ブロックされる（Issue #135）。
    判定を緩める/締める変更は必ずこの関数のみで行い、呼び出し側で独自パースしないこと
    （パーサ分裂は資金ゲートと kill switch の判定食い違いを生む）。
    """
    allow = os.environ.get(_LIVE_ORDER_FLAG, "").strip().lower() == "true"
    phase = os.environ.get(_PHASE_FLAG, "1").strip()
    return allow and phase in _LIVE_PHASES


class OrderBlockedError(RuntimeError):
    """Phase 1 モードで発注系 POST を実送信しようとした場合に送出する (AGENTS.md #1)."""


class GmoFxClient:
    """GMO FX API クライアント.

    Args:
        api_key: GMO FX API Key（Private API 利用時に必須）。
        api_secret: GMO FX API Secret（Private API 利用時に必須）。
        base_url: ベース URL。テスト時にモックサーバーへ切り替え可能。
        http_client: 外部から注入する httpx.Client。渡した場合は呼び出し元がライフサイクルを管理する
                     （close/context-manager では閉じない）。省略時は内部で生成し、クライアントが所有する。
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = GMO_FX_HOST,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url.rstrip("/")
        if http_client is not None:
            self._client = http_client
            self._owns_client = False  # 外部注入 → 呼び出し元が管理
        else:
            # IPv4 強制バインド: forex-api.coin.z.com は dual-stack で、既定の httpx.Client は
            # IPv6 を優先する。GMO の API キー IP 制限は IPv4（例: 211.128.29.10）のみ登録のため、
            # IPv6 egress だと接続元が許可外となり ERR-5012「Invalid API-KEY, IP, or permissions」
            # で失敗する（E04 スパイクで実測。真因切り分けは docs/spikes/E04_assets_response.md）。
            # local_address="0.0.0.0" で送信元を IPv4 にバインドし回避する。
            transport = httpx.HTTPTransport(local_address=_IPV4_BIND_ADDR)
            self._client = httpx.Client(timeout=30.0, transport=transport)
            self._owns_client = True
        self._limiter = RateLimiter()

    # ------------------------------------------------------------------
    # Public API（署名不要）
    # ------------------------------------------------------------------

    def public_get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """Public エンドポイントへ署名なし GET を送る.

        Args:
            path: エンドポイントパス（例: PUBLIC_KLINES = "/public/v1/klines"）。
            params: クエリパラメータ。
        """
        self._limiter.wait_get()
        url = self._base_url + path
        return self._client.get(url, params=params)

    # ------------------------------------------------------------------
    # Private API（署名必須）
    # ------------------------------------------------------------------

    def private_get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """Private エンドポイントへ署名付き GET を送る.

        リクエスト URL は /private/v1/... 、署名パスは /v1/... に変換して渡す（§7.2）。
        401 リトライ前にも wait_get() を挟みレートリミットを遵守する。
        """
        url = self._base_url + path
        resolved_sign_path = to_sign_path(path)  # /private/v1/... → /v1/...
        return request_with_retry(
            client=self._client,
            method="GET",
            url=url,
            request_path=path,
            sign_path=resolved_sign_path,
            api_key=self._api_key,
            api_secret=self._api_secret,
            pre_attempt_hook=self._limiter.wait_get,
            params=params,
        )

    def private_post(
        self, path: str, body: BaseModel | dict[str, Any] | None = None
    ) -> httpx.Response:
        """Private エンドポイントへ署名付き POST を送る.

        リクエスト URL は /private/v1/... 、署名パスは /v1/... に変換して渡す（§7.2）。
        POST 間のレートリミット（≥1.0秒）は pre_attempt_hook 経由で毎試行前に保証する（AGENTS.md #3）。
        BaseModel を渡した場合は by_alias=True でシリアライズし、ワイヤー形式（camelCase）を保証する。

        発注系エンドポイント（ifoOrder / closeOrder / cancelOrders）は
        ALLOW_LIVE_ORDERS=True かつ TRADING_PHASE∈{2,spike} の二重条件を満たす場合のみ実送信する
        （AGENTS.md #1 封じ込め・two-key interlock）。MVP_MODE（100通貨固定）は封じ込め判定に影響しない。
        """
        if path in _ORDER_PATHS and not live_orders_allowed():
            _log.warning(
                "order_blocked",
                path=path,
                reason="ALLOW_LIVE_ORDERS=True かつ TRADING_PHASE∈{2,spike} を満たさないため発注系 POST を遮断",
                timestamp_ms=int(time.time() * 1000),
                llm_tokens=None,  # 発注遮断時点では LLM 呼び出し未実施 (§5.2)
                rule="AGENTS.md #1 実発注の封じ込め",
            )
            raise OrderBlockedError(
                f"発注系 POST は封じ込め中のため実送信できません: {path}。"
                "実送信は ALLOW_LIVE_ORDERS=True かつ TRADING_PHASE∈{2,spike} の"
                "両方を満たす場合のみ許可されます (AGENTS.md #1)。"
            )
        url = self._base_url + path
        resolved_sign_path = to_sign_path(path)  # /private/v1/... → /v1/...
        if isinstance(body, BaseModel):
            body_str = body.model_dump_json(by_alias=True)
        else:
            body_str = json.dumps(body or {}, ensure_ascii=False, separators=(",", ":"))
        return request_with_retry(
            client=self._client,
            method="POST",
            url=url,
            request_path=path,
            sign_path=resolved_sign_path,
            api_key=self._api_key,
            api_secret=self._api_secret,
            body=body_str,
            pre_attempt_hook=self._limiter.wait_post,
            post_attempt_hook=self._limiter.mark_post_done,
        )

    def get_market_status(self) -> str:
        """GET /public/v1/status — 外国為替FXステータスを返す（§7.3 / Issue #207 (c)）。

        戻り値は ``"OPEN"`` / ``"CLOSE"`` / ``"MAINTENANCE"``
        （出典: https://api.coin.z.com/fxdocs/・2026-08-08 参照）。

        🔴 **エンベロープの ``status``（API 成否）と ``data.status``（市場状態）は同名で別物。**
        取り違えると「API 成功（status:0）＝開場」と誤読する。

        閉場中でも Private の参照系 GET は HTTP 200 + ``status: 0`` を返すため、
        「参照系 GET が通る＝取引時間内」という代理判定は**閉場を検知できない**
        （2026-08-08 土曜に実測・付録B docs/spikes/market_status_closed.md）。
        市場の開閉はこのエンドポイントで判定すること。

        Returns:
            市場ステータス文字列。取得失敗・HTTP エラー・ボディ status != 0 は例外を送出する。
        """
        resp = self.public_get(PUBLIC_STATUS)
        resp.raise_for_status()
        raise_for_gmo_status(resp, context="status")
        data = resp.json().get("data")
        if not isinstance(data, dict):
            # 形が変わったら「不明」を返す。呼び出し側は OPEN 以外を閉場扱いにするため
            # fail-safe に倒れる（AttributeError で潰れるより意図が明確）。
            _log.warning("market_status_unexpected_shape", data_type=type(data).__name__)
            return ""
        return str(data.get("status", ""))

    # ------------------------------------------------------------------
    # 約定照会ヘルパー（ExecutionClient Protocol 実装）
    # ------------------------------------------------------------------

    def _get_execution_list(
        self, path: str, params: dict[str, Any], *, context: str
    ) -> list[dict[str, Any]]:
        """約定系 GET の共通処理（HTTP 層 + ボディ status の**両方**を検査する）。

        🔴 GMO は業務エラーでも **HTTP 200 + ボディ ``{"status": 1, "messages": [...]}``**
        を返す。``raise_for_status()`` はこれを通過させ、``data`` キーが無いため
        素朴な実装は空リストを返して正常終了する。つまり **API エラーが「約定なし」に化ける**。
        2026-08-05 の手仕舞いではこれが原因で ``executions_count: 0`` となり、
        実際には約定・決済済みなのに LLM が「未約定」という事実と反対のノートを生成した
        （税務証跡の汚染）。``raise_for_gmo_status`` を必ず通すこと（付録B E04 §1）。

        🔴 **invocation 内でリトライする**（Issue #245）。約定を取れる API は
        ``latestExecutions``（直近1日分）しか無く、手仕舞い cron は 1 日 1 回のため、
        **翌日の再実行では取り逃した明細を回収できない**（付録B §5）。
        「次回 cron で再試行」という他経路の定石がここだけ成立しないので、
        一過性の失敗はこの場で吸収する。実発注中は約定明細が税務証跡そのもの（§5.2）。

        リトライして安全なのは **これが読み取り専用の GET だから**である。
        発注系 POST をこの経路に通してはならない（重複発注・AGENTS.md #1/#4）。

        Returns:
            約定レコードのリスト。リトライを尽くしても失敗した場合は最後の例外を送出する。
        """
        started = time.monotonic()
        last_error: Exception | None = None
        attempts_made = 0
        # 打ち切り理由。時間予算で 1 回目に抜けたのか、試行を使い切ったのかで
        # 運用上の意味がまったく違う（前者は GMO のハング / 後者は継続的な失敗）。
        stopped_reason = "max_attempts"

        for attempt in range(1, _EXECUTION_FETCH_MAX_ATTEMPTS + 1):
            attempts_made = attempt
            try:
                resp = self.private_get(path, params=params)
                resp.raise_for_status()
                raise_for_gmo_status(resp, context=context)
                inner = resp.json().get("data")
                if isinstance(inner, list):
                    return [r for r in inner if isinstance(r, dict)]
                if isinstance(inner, dict):
                    return [r for r in inner.get("list", []) if isinstance(r, dict)]
                return []
            except Exception as exc:
                last_error = exc
                elapsed = time.monotonic() - started
                # 残り試行が無い、または時間予算を使い切ったら諦める。
                # httpx のタイムアウトは 30 秒あり、ハング系の失敗を素朴に積むと
                # Lambda のタイムアウト（既定 120 秒）を食い潰して手仕舞い全体を落とす。
                # 「安い失敗は粘る・高い失敗は粘らない」形にする。
                if elapsed >= _EXECUTION_FETCH_DEADLINE_SECONDS:
                    stopped_reason = "deadline"
                    break
                if attempt == _EXECUTION_FETCH_MAX_ATTEMPTS:
                    stopped_reason = "max_attempts"
                    break
                backoff = _EXECUTION_FETCH_BACKOFF_SECONDS[attempt - 1]
                _log.warning(
                    "execution_fetch_retry",
                    context=context,
                    attempt=attempt,
                    max_attempts=_EXECUTION_FETCH_MAX_ATTEMPTS,
                    elapsed_seconds=round(elapsed, 3),
                    backoff_seconds=backoff,
                    error=str(exc),
                    rule="Issue #245: 翌日リトライでは約定明細を回収できない",
                )
                time.sleep(backoff)

        assert last_error is not None  # ループは return か break でしか抜けない
        _log.error(
            "execution_fetch_failed",
            context=context,
            # 実際に試行した回数。時間予算で打ち切ると上限より少なくなるため、
            # 定数をそのまま出すと「3 回粘ったが駄目だった」と誤読される（実際は 1 回）。
            attempts=attempts_made,
            max_attempts=_EXECUTION_FETCH_MAX_ATTEMPTS,
            stopped_reason=stopped_reason,
            elapsed_seconds=round(time.monotonic() - started, 3),
            error=str(last_error),
            impact="この取引日の約定明細は翌日には API から回収できない（税務証跡の欠損）",
        )
        raise last_error

    def get_executions(self, *, order_id: str | int) -> list[dict[str, Any]]:
        """GET /private/v1/executions — **指定注文の**約定を取得する（§6.2）。

        公式仕様（出典: https://api.coin.z.com/fxdocs/・2026-08-05 参照）:
          「orderId executionId いずれか1つが必須です。2つ同時には設定できません。」
        → 受け付けるのは ``orderId``（number）か ``executionId``（カンマ区切り最大10件）のみ。
        **``symbol`` / ``from`` / ``to`` / ``date`` というパラメータは存在しない**
        （旧実装はこれらを送って ERR-5106 を踏み、上記の「約定なし」化けを起こしていた）。

        なお、この注文 ID で返るのはその注文の約定だけである。IFD-OCO のエントリー
        （``settleType=OPEN``）と決済（``CLOSE``）は **別 orderId** で約定するため、
        エントリーの ID だけでは決済約定と実損益に到達できない。決済側を含めて取るには
        :meth:`get_latest_executions` を使うこと（§6.2・付録B）。

        引数はキーワード専用にしている。``symbol`` を渡していた旧シグネチャと
        位置引数で取り違えると、また静かに誤った問い合わせになるため。
        """
        return self._get_execution_list(
            PRIVATE_EXECUTIONS, {"orderId": order_id}, context="executions"
        )

    def get_latest_executions(
        self, *, symbol: str, count: int = MAX_LATEST_EXECUTIONS_COUNT
    ) -> list[dict[str, Any]]:
        """GET /private/v1/latestExecutions — 銘柄の最新約定一覧を取得する（§6.2）。

        公式仕様（出典: https://api.coin.z.com/fxdocs/・2026-08-05 参照）:
          「最新約定一覧を取得します。**直近1日分から最新100件**の約定情報を返します。」
          ``symbol`` は Required / ``count`` は Optional「指定しない場合は100(最大値)」。
        → **``orderId`` というパラメータは存在しない**（旧実装は orderId を送って
          ERR-5106「Invalid request parameter. symbol」を踏んでいた）。

        エントリー（``settleType=OPEN``）と決済（``CLOSE``）の**両方**が返るため、
        実損益（``lossGain`` / ``fee`` / ``settledSwap``）に到達できる唯一の経路である。
        ただし取得範囲は **直近1日分・最大100件** に限られる。それより古い約定は
        この API では取得できない（日付範囲指定の約定履歴エンドポイントは FX API に存在しない）。

        Args:
            symbol: 通貨ペア（例: "USD_JPY"）。必須。
            count: 取得件数。既定＝最大値の 100。

        Returns:
            約定レコードのリスト。HTTP エラー・ボディ status != 0 は例外を送出する。
        """
        if not 1 <= count <= MAX_LATEST_EXECUTIONS_COUNT:
            raise ValueError(
                f"count は 1〜{MAX_LATEST_EXECUTIONS_COUNT} の範囲で指定してください: {count}"
            )
        return self._get_execution_list(
            PRIVATE_LATEST_EXECUTIONS,
            {"symbol": symbol, "count": count},
            context="latestExecutions",
        )

    def close(self) -> None:
        """内部生成クライアントのみクローズする。外部注入クライアントは閉じない."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> GmoFxClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
