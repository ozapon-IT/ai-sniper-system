"""Kill Switch Lambda テスト（§1.6, §6.3, Issue #26）.

受け入れ条件:
  - 認証なし POST が 401 を返す
  - 正しい Bearer で POST が 200 を返す
  - 建玉なし状態で呼び出しても Lambda が例外終了しない（no-op 正常終了）
  - 手仕舞いバッチと同時実行シミュレーションで二重決済が発生しない
  - 実行ログが S3 に記録される
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import backend.src.lambda_auth as _auth
from backend.src.gmo_errors import GmoApiError
from backend.src.kill_switch.handler import _is_no_position_error, lambda_handler

VALID_TOKEN = "kill-switch-test-token"
SPA_ORIGIN = "https://dashboard.example.com"


def _make_event(
    method: str = "POST",
    token: str | None = VALID_TOKEN,
    origin: str | None = SPA_ORIGIN,
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    return {
        "httpMethod": method,
        "rawPath": "/kill",
        "headers": headers,
    }


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """各テストで環境変数を設定し、シークレットキャッシュをリセットする."""
    monkeypatch.setenv("CORS_ALLOW_ORIGIN", SPA_ORIGIN)
    monkeypatch.setenv("TRADE_SYMBOL", "USD_JPY")
    # Phase 1 デフォルト: 発注封じ込め有効
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "False")
    monkeypatch.setenv("TRADING_PHASE", "1")
    _auth.reset_token_cache()


# ---------------------------------------------------------------------------
# 認証テスト
# ---------------------------------------------------------------------------


class TestNoAppLayerAuth:
    """🔴 アプリ層の認証を持たないこと（Ver 3.7 / §1.6.1・#177）。

    認証境界は IAM（``lambda:InvokeFunction``）であり、本関数は HTTP 公開しない。
    旧実装は Secrets Manager の共有トークンを Bearer で検証していたが、静的
    クライアントに長期の共有秘密を置けない以上その方式では #177 を解けないため廃止した。

    🔴 **この契約は「HTTP 公開しない」とセットでのみ安全**。公開すると無認証で
    全建玉を成行決済できる。公開リソースの追加は CI
    （``scripts/check_no_public_endpoints.py``）が落とす。
    """

    def test_no_auth_header_succeeds(self) -> None:
        """Authorization ヘッダ無しの素の invoke が通ること（緊急時の実際の使い方）."""
        with patch("backend.src.kill_switch.handler.s3_logger") as mock_s3:
            mock_s3.save_kill_switch_log.return_value = True
            resp = lambda_handler(_make_event(token=None), None)
        assert resp["statusCode"] == 200

    def test_bare_invoke_event_succeeds(self) -> None:
        """コンソールの空イベント相当（headers すら無い）でも通ること.

        ランブック §緊急決済ラダーの手順は `{"httpMethod": "POST"}` のみを送る。
        headers キーの欠落で落ちないことを固定する。
        """
        with patch("backend.src.kill_switch.handler.s3_logger"):
            resp = lambda_handler({"httpMethod": "POST"}, None)
        assert resp["statusCode"] == 200

    def test_bogus_token_is_ignored(self) -> None:
        """デタラメな Bearer が付いていても 401 にしない（検証自体が無い）."""
        with patch("backend.src.kill_switch.handler.s3_logger"):
            resp = lambda_handler(_make_event(token="bogus"), None)
        assert resp["statusCode"] == 200

    def test_handler_does_not_reference_verify_bearer(self) -> None:
        """認証が「うっかり復活」していないことをソースで固定する.

        ⚠️ 補助線であり完全ではない（別名 import や lambda_auth.verify_bearer の
        直接参照はすり抜ける）。**本命の統制は CI の公開禁止**
        （scripts/check_no_public_endpoints.py）であり、これは早期検知用。
        """
        import backend.src.kill_switch.handler as h

        assert not hasattr(h, "verify_bearer")


# ---------------------------------------------------------------------------
# HTTP メソッドテスト
# ---------------------------------------------------------------------------


class TestHttpMethod:
    def test_get_returns_405(self) -> None:
        resp = lambda_handler(_make_event(method="GET"), None)
        assert resp["statusCode"] == 405

    def test_options_preflight_allowed_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = lambda_handler(_make_event(method="OPTIONS"), None)
        assert resp["statusCode"] == 204
        assert "Access-Control-Allow-Origin" in resp["headers"]

    def test_options_preflight_disallowed_origin(self) -> None:
        event = _make_event(method="OPTIONS", origin="https://evil.com")
        resp = lambda_handler(event, None)
        assert resp["statusCode"] == 403


# ---------------------------------------------------------------------------
# 発注封じ込め（two-key interlock）テスト
# ---------------------------------------------------------------------------


class TestTwoKeyInterlock:
    def test_phase1_stub_mode_no_live_post(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Phase 1 ではスタブモードで実 POST を送信しない（AGENTS.md #1）."""
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "False")
        monkeypatch.setenv("TRADING_PHASE", "1")

        with (
            patch("backend.src.kill_switch.handler.s3_logger") as mock_s3,
            patch("backend.src.kill_switch.handler.GmoFxClient") as mock_client_cls,
        ):
            mock_s3.save_kill_switch_log.return_value = True
            resp = lambda_handler(_make_event(), None)

        mock_client_cls.assert_not_called()
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200
        assert body["mode"] == "stub"

    def test_allow_live_only_no_phase_still_stub(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ALLOW_LIVE_ORDERS=True だけでは実 POST 不可（single key は安全側）."""
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "True")
        monkeypatch.setenv("TRADING_PHASE", "1")

        with (
            patch("backend.src.kill_switch.handler.s3_logger"),
            patch("backend.src.kill_switch.handler.GmoFxClient") as mock_client_cls,
        ):
            lambda_handler(_make_event(), None)

        mock_client_cls.assert_not_called()

    def test_phase2_with_live_flag_uses_real_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ALLOW_LIVE_ORDERS=True かつ TRADING_PHASE=2 で GmoFxClient を呼ぶ."""
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "True")
        monkeypatch.setenv("TRADING_PHASE", "2")

        mock_result = MagicMock()
        mock_result.step1_cancelled_ids = []
        mock_result.step2_cancelled_ids = []
        mock_result.step3_closed_ids = []
        mock_result.market_closed = False
        mock_result.step1_failed = False
        mock_result.step2_failed = False

        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with (
            patch("backend.src.kill_switch.handler.s3_logger"),
            patch("backend.src.kill_switch.handler.GmoFxClient", return_value=mock_ctx),
            patch("backend.src.kill_switch.handler.run_cancel_first", return_value=mock_result),
            patch("backend.src.kill_switch.handler.get_gmo_credentials", return_value=("k", "s")),
        ):
            resp = lambda_handler(_make_event(), None)

        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200
        assert body["mode"] == "live"

    def test_spike_with_live_flag_uses_real_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ALLOW_LIVE_ORDERS=True かつ TRADING_PHASE=spike で live 経路を使う（two-key interlock）."""
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "True")
        monkeypatch.setenv("TRADING_PHASE", "spike")

        mock_result = MagicMock()
        mock_result.step1_cancelled_ids = []
        mock_result.step2_cancelled_ids = []
        mock_result.step3_closed_ids = []
        mock_result.market_closed = False
        mock_result.step1_failed = False
        mock_result.step2_failed = False

        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with (
            patch("backend.src.kill_switch.handler.s3_logger"),
            patch("backend.src.kill_switch.handler.GmoFxClient", return_value=mock_ctx) as mock_client_cls,
            patch("backend.src.kill_switch.handler.run_cancel_first", return_value=mock_result) as mock_run_cancel,
            patch("backend.src.kill_switch.handler.get_gmo_credentials", return_value=("k", "s")) as mock_get_creds,
        ):
            resp = lambda_handler(_make_event(), None)

        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200
        assert body["mode"] == "live"
        mock_get_creds.assert_called_once()
        mock_client_cls.assert_called_once()
        mock_run_cancel.assert_called_once()


# ---------------------------------------------------------------------------
# 冪等性テスト
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_no_position_error_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """確認済み「建玉なし」message_code の完全一致で no-op 正常終了する（§1.6）.

        実際の「建玉なし」コードは付録B未確定のため、テストではコード集合を
        パッチして完全一致機構そのものを検証する。
        """
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "True")
        monkeypatch.setenv("TRADING_PHASE", "2")

        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        no_pos_error = GmoApiError(
            "GMO API error (closeOrder): status=1 codes=['ERR-TEST-NOPOS']",
            status=1,
            message_codes=("ERR-TEST-NOPOS",),
        )
        with (
            patch("backend.src.kill_switch.handler.s3_logger") as mock_s3,
            patch("backend.src.kill_switch.handler.GmoFxClient", return_value=mock_ctx),
            patch(
                "backend.src.kill_switch.handler._NO_POSITION_ERROR_CODES",
                frozenset({"ERR-TEST-NOPOS"}),
            ),
            patch(
                "backend.src.kill_switch.handler.run_cancel_first",
                side_effect=no_pos_error,
            ),
            patch("backend.src.kill_switch.handler.get_gmo_credentials", return_value=("k", "s")),
        ):
            mock_s3.save_kill_switch_log.return_value = True
            resp = lambda_handler(_make_event(), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["status"] == "ok"
        assert body["step3_closed"] == []

    def test_misleading_numeric_fragment_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """メッセージ中の数値断片（size:"14001" 等）で偽 no-op 成功にならない（Issue #136）.

        旧実装は "4001" の部分一致で本物の決済失敗を 200 "ok" として吸収していた。
        構造化コードを持たない例外は fail-closed（500）に倒す。
        """
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "True")
        monkeypatch.setenv("TRADING_PHASE", "2")

        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with (
            patch("backend.src.kill_switch.handler.s3_logger") as mock_s3,
            patch("backend.src.kill_switch.handler.GmoFxClient", return_value=mock_ctx),
            patch(
                "backend.src.kill_switch.handler.run_cancel_first",
                side_effect=RuntimeError('GMO close failed: size: "14001" is invalid'),
            ),
            patch("backend.src.kill_switch.handler.get_gmo_credentials", return_value=("k", "s")),
        ):
            mock_s3.save_kill_switch_log.return_value = True
            resp = lambda_handler(_make_event(), None)

        assert resp["statusCode"] == 500

    def test_unknown_gmo_error_code_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未確認の message_code は no-op にせず失敗（500）に倒す（fail-closed）."""
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "True")
        monkeypatch.setenv("TRADING_PHASE", "2")

        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        unknown_error = GmoApiError(
            "GMO API error (closeOrder): status=1 codes=['ERR-5003']",
            status=1,
            message_codes=("ERR-5003",),
        )
        with (
            patch("backend.src.kill_switch.handler.s3_logger") as mock_s3,
            patch("backend.src.kill_switch.handler.GmoFxClient", return_value=mock_ctx),
            patch(
                "backend.src.kill_switch.handler.run_cancel_first",
                side_effect=unknown_error,
            ),
            patch("backend.src.kill_switch.handler.get_gmo_credentials", return_value=("k", "s")),
        ):
            mock_s3.save_kill_switch_log.return_value = True
            resp = lambda_handler(_make_event(), None)

        assert resp["statusCode"] == 500

    def test_double_invocation_does_not_multiply_closes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """live モードで 2 回目の呼び出しが二重クローズを起こさない（§1.6 冪等性）.

        1 回目: 建玉あり → cancel_first が閉じる
        2 回目: 建玉なし → run_cancel_first がエラーなしで空結果を返す（通常経路）
        """
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "True")
        monkeypatch.setenv("TRADING_PHASE", "2")

        mock_result = MagicMock()
        mock_result.step1_cancelled_ids = ["ord-1"]
        mock_result.step2_cancelled_ids = []
        mock_result.step3_closed_ids = ["pos-1"]
        mock_result.market_closed = False
        mock_result.step1_failed = False
        mock_result.step2_failed = False

        empty_result = MagicMock()
        empty_result.step1_cancelled_ids = []
        empty_result.step2_cancelled_ids = []
        empty_result.step3_closed_ids = []
        empty_result.market_closed = False
        empty_result.step1_failed = False
        empty_result.step2_failed = False

        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        # 1 回目: 正常クローズ / 2 回目: 建玉なし → 空結果（エラーなし）
        cancel_first_calls = [mock_result, empty_result]

        def _side_effect(*_a: object, **_kw: object) -> object:
            val = cancel_first_calls.pop(0)
            if isinstance(val, Exception):
                raise val
            return val

        with (
            patch("backend.src.kill_switch.handler.s3_logger"),
            patch("backend.src.kill_switch.handler.GmoFxClient", return_value=mock_ctx),
            patch("backend.src.kill_switch.handler.run_cancel_first", side_effect=_side_effect),
            patch("backend.src.kill_switch.handler.get_gmo_credentials", return_value=("k", "s")),
        ):
            resp1 = lambda_handler(_make_event(), None)
            resp2 = lambda_handler(_make_event(), None)

        # 1 回目: 実クローズ成功
        assert resp1["statusCode"] == 200
        body1 = json.loads(resp1["body"])
        assert body1["mode"] == "live"
        assert body1["step3_closed"] == ["pos-1"]

        # 2 回目: no-op 正常終了（二重決済なし）
        assert resp2["statusCode"] == 200
        body2 = json.loads(resp2["body"])
        assert body2["mode"] == "live"
        assert body2["step3_closed"] == []  # 2 回目はクローズ実行なし


# ---------------------------------------------------------------------------
# S3 ログテスト
# ---------------------------------------------------------------------------


class TestS3Logging:
    def test_log_saved_to_s3_on_success(self) -> None:
        """正常終了時に S3 へ実行ログを保存する（§1.5）."""
        with patch("backend.src.kill_switch.handler.s3_logger") as mock_s3:
            mock_s3.save_kill_switch_log.return_value = True
            lambda_handler(_make_event(), None)

        mock_s3.save_kill_switch_log.assert_called_once()
        saved_payload = mock_s3.save_kill_switch_log.call_args[0][0]
        assert "timestamp" in saved_payload
        assert "symbol" in saved_payload
        assert saved_payload["status"] == "ok"

    def test_log_saved_even_on_gmo_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GMO エラー（fail-closed の 500）時もログを保存する."""
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "True")
        monkeypatch.setenv("TRADING_PHASE", "2")

        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with (
            patch("backend.src.kill_switch.handler.s3_logger") as mock_s3,
            patch("backend.src.kill_switch.handler.GmoFxClient", return_value=mock_ctx),
            patch(
                "backend.src.kill_switch.handler.run_cancel_first",
                side_effect=RuntimeError("5009: already closed"),
            ),
            patch("backend.src.kill_switch.handler.get_gmo_credentials", return_value=("k", "s")),
        ):
            mock_s3.save_kill_switch_log.return_value = True
            lambda_handler(_make_event(), None)

        mock_s3.save_kill_switch_log.assert_called_once()


# ---------------------------------------------------------------------------
# _is_no_position_error ユニットテスト
# ---------------------------------------------------------------------------


class TestIsNoPositionError:
    """構造化 message_code の完全一致判定（Issue #136）.

    実際の「建玉なし」コードは付録B未確定のため、コード集合をパッチして
    完全一致機構そのものを検証する。
    """

    _CODES = frozenset({"ERR-TEST-NOPOS", "ERR-TEST-CLOSED"})

    def test_exact_code_match_is_noop(self) -> None:
        exc = GmoApiError("closeOrder failed", status=1, message_codes=("ERR-TEST-NOPOS",))
        with patch("backend.src.kill_switch.handler._NO_POSITION_ERROR_CODES", self._CODES):
            assert _is_no_position_error(exc) is True

    def test_all_codes_must_match(self) -> None:
        """既知＋未知コード混在は本物の失敗の可能性があるため no-op にしない."""
        exc = GmoApiError(
            "closeOrder failed",
            status=1,
            message_codes=("ERR-TEST-NOPOS", "ERR-5003"),
        )
        with patch("backend.src.kill_switch.handler._NO_POSITION_ERROR_CODES", self._CODES):
            assert _is_no_position_error(exc) is False

    def test_empty_codes_fail_closed(self) -> None:
        """message_codes が空の GmoApiError は判定不能 → fail-closed."""
        exc = GmoApiError("unknown", status=1, message_codes=())
        with patch("backend.src.kill_switch.handler._NO_POSITION_ERROR_CODES", self._CODES):
            assert _is_no_position_error(exc) is False

    def test_default_code_set_is_empty_fail_closed(self) -> None:
        """既定のコード集合は空（付録B未確定のため）→ 全 GmoApiError が fail-closed."""
        exc = GmoApiError("closeOrder failed", status=1, message_codes=("ERR-TEST-NOPOS",))
        assert _is_no_position_error(exc) is False

    @pytest.mark.parametrize(
        "msg",
        [
            # 旧実装が部分一致で偽 no-op にしていた紛らわしい文字列（Issue #136）
            'GMO close failed: size: "14001" is invalid',
            "rate limit exceeded at 2026-05-00814001",
            "positionId 94002X rejected",
            "error code 5008",
            "GMO 5009 already closed",
            "建玉なし",
            "決済済み",
            "network timeout",
            "authentication failed",
        ],
    )
    def test_plain_exceptions_never_match(self, msg: str) -> None:
        """構造化コードを持たない例外は文字列内容に関わらず no-op にしない."""
        with patch("backend.src.kill_switch.handler._NO_POSITION_ERROR_CODES", self._CODES):
            assert _is_no_position_error(RuntimeError(msg)) is False


class TestKillSwitchMarketClosed:
    """🔴 閉場時に「成功」を返さないこと（#261 レビュー指摘 2）。

    #207 (c) で Step0 が公式ステータス判定になり、**週末・メンテ中は必ず market_closed** に
    なる。従来は参照系 GET が落ちた稀なケースだけだったため目立たなかったが、
    "ok" のまま返すと緊急停止を押した人が「建玉は決済された」と誤読する。
    """

    @staticmethod
    def _result(*, market_closed: bool, closed_ids: list[str]) -> MagicMock:
        r = MagicMock()
        r.step1_cancelled_ids = []
        r.step2_cancelled_ids = []
        r.step3_closed_ids = closed_ids
        r.market_closed = market_closed
        r.step1_failed = False
        r.step2_failed = False
        return r

    @staticmethod
    def _run(result: MagicMock, monkeypatch: pytest.MonkeyPatch) -> dict:
        # two-key を満たして live 経路に入れる（未設定だと dry-run で空結果になる）
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "True")
        monkeypatch.setenv("TRADING_PHASE", "2")
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        with (
            patch("backend.src.kill_switch.handler.s3_logger"),
            patch("backend.src.kill_switch.handler.GmoFxClient", return_value=ctx),
            patch("backend.src.kill_switch.handler.run_cancel_first", return_value=result),
            patch("backend.src.kill_switch.handler.get_gmo_credentials", return_value=("k", "s")),
        ):
            resp = lambda_handler(_make_event(), None)
        return json.loads(resp["body"])

    def test_market_closed_is_not_reported_as_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = self._run(self._result(market_closed=True, closed_ids=[]), monkeypatch)

        assert body["status"] == "market_closed", "閉場を ok で返してはいけない"
        assert body["market_closed"] is True
        assert body["step3_closed"] == []

    def test_normal_run_still_reports_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = self._run(
            self._result(market_closed=False, closed_ids=["700101"]), monkeypatch
        )

        assert body["status"] == "ok"
        assert body["step3_closed"] == ["700101"]
