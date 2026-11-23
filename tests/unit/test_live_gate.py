"""two-key interlock パーサ統一の回帰テスト（Issue #135, AGENTS.md #1）。

実発注解禁判定（ALLOW_LIVE_ORDERS / TRADING_PHASE）が 3 箇所
（gmo_client 資金ゲート / morning_batch / kill_switch）で別々のパーサだったため、
表記ゆれ（小文字 "true"・前後空白）で判定が食い違い、
- Terraform 実値（locals.tf の小文字 "true"）では実発注が永久ブロック
- kill_switch だけ live 判定になり緊急停止が 500 失敗（half-live）
という不整合が起きていた。本テストは単一ヘルパーへの集約と、
表記ゆれ全パターンでの 3 箇所一致を保証する。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from backend.src.batch import decision_batch
from backend.src.clients import gmo_client
from backend.src.clients.gmo_client import GmoFxClient, live_orders_allowed
from backend.src.gmo_fx_endpoints import PRIVATE_IFO_ORDER
from backend.src.kill_switch import handler as kill_switch_handler


def _set_env(monkeypatch: pytest.MonkeyPatch, allow: str | None, phase: str | None) -> None:
    if allow is None:
        monkeypatch.delenv("ALLOW_LIVE_ORDERS", raising=False)
    else:
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", allow)
    if phase is None:
        monkeypatch.delenv("TRADING_PHASE", raising=False)
    else:
        monkeypatch.setenv("TRADING_PHASE", phase)


# (ALLOW_LIVE_ORDERS, TRADING_PHASE, 期待値) — 表記ゆれを網羅した真理値表
_TRUTH_TABLE: list[tuple[str | None, str | None, bool]] = [
    # 解禁されるべき組（大小非依存・strip 後に一致）
    ("true", "2", True),  # Terraform locals.tf の実値（小文字）
    ("True", "2", True),  # 従来ドキュメント表記
    ("TRUE", "2", True),
    (" true ", "2", True),  # 前後空白
    ("true", " 2 ", True),  # phase 側の空白
    ("True", "spike", True),  # E06 スパイク
    (" true", " spike ", True),
    # ブロックされるべき組（two-key の片方欠け・値不正）
    (None, None, False),
    ("true", None, False),  # phase 未設定（既定 "1"）
    ("true", "1", False),  # モックフェーズ
    ("false", "2", False),
    ("False", "2", False),
    (None, "2", False),
    ("", "2", False),
    ("1", "2", False),  # 真偽値として不正な表記は解禁しない
    ("truthy", "2", False),
    ("true", "3", False),  # 未定義フェーズ
    ("true", "", False),
]


@pytest.mark.parametrize(("allow", "phase", "expected"), _TRUTH_TABLE)
def test_live_orders_allowed_truth_table(
    allow: str | None,
    phase: str | None,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共有ヘルパーが表記ゆれ（大小・空白）を正規化しつつ two-key を厳守する。"""
    _set_env(monkeypatch, allow, phase)
    assert live_orders_allowed() is expected


@pytest.mark.parametrize(("allow", "phase", "expected"), _TRUTH_TABLE)
def test_kill_switch_agrees_with_shared_gate(
    allow: str | None,
    phase: str | None,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kill_switch の live 判定が共有ヘルパーと全パターンで一致する（half-live 防止）。"""
    _set_env(monkeypatch, allow, phase)
    assert kill_switch_handler._is_live_mode() is expected
    assert kill_switch_handler._is_live_mode() == live_orders_allowed()


def test_gate_is_single_sourced() -> None:
    """3 箇所が同一の関数オブジェクトを参照している（パーサ再分裂の防止）。"""
    assert decision_batch.live_orders_allowed is gmo_client.live_orders_allowed
    assert kill_switch_handler.live_orders_allowed is gmo_client.live_orders_allowed


def test_terraform_values_unlock_order_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """Terraform が実際に供給する値（小文字 "true" / "2"）で資金ゲートが解禁される。

    infra/terraform/modules/serverless_app/locals.tf:25 は
    `ALLOW_LIVE_ORDERS = var.allow_live_orders ? "true" : "false"` で小文字を渡す。
    旧実装（== "True" 厳格一致）では Phase 2 で実発注が永久ブロックされた（Issue #135）。
    """
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "true")
    monkeypatch.setenv("TRADING_PHASE", "2")
    mock_client = MagicMock(spec=httpx.Client)

    with (
        patch("backend.src.clients.gmo_client.RateLimiter"),
        patch("backend.src.clients.gmo_client.request_with_retry") as mock_retry,
    ):
        resp = MagicMock(spec=httpx.Response)
        mock_retry.return_value = resp
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        gmo.private_post(PRIVATE_IFO_ORDER, body={"symbol": "USD_JPY"})

    mock_retry.assert_called_once()


def test_terraform_false_value_blocks_order_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terraform の既定値（小文字 "false"）では確実にブロックされる（安全側）。"""
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "false")
    monkeypatch.setenv("TRADING_PHASE", "2")
    mock_client = MagicMock(spec=httpx.Client)

    with patch("backend.src.clients.gmo_client.RateLimiter"):
        gmo = GmoFxClient(api_key="k", api_secret="s", http_client=mock_client)
        with pytest.raises(gmo_client.OrderBlockedError):
            gmo.private_post(PRIVATE_IFO_ORDER)

    mock_client.request.assert_not_called()
