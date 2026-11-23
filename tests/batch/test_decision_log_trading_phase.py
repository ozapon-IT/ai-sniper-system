"""判断ログの trading_phase（#383 / §7.4）。

保存 payload の値は実行時 ``TRADING_PHASE`` を strip した生文字列。
未設定と strip 後の空はキーを出さない。既定の ``1`` は補わない。許可外は補正しない。
AWS / GMO / S3 put は使わない。``live_orders_allowed`` の成否は変えない。
"""

from __future__ import annotations

import ast
from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from backend.src.adapters.economic_calendar import CalendarResult, ImpactLevel
from backend.src.adapters.economic_calendar import EconomicEvent as AdapterEvent
from backend.src.batch.decision_batch import _run, lambda_handler
from backend.src.clients.gmo_client import live_orders_allowed
from backend.src.llm.prompt import EconomicEvent
from backend.src.persistence.dynamodb_store import LockAcquireOutcome, LockReleaseOutcome

JST = ZoneInfo("Asia/Tokyo")
TODAY = date(2026, 6, 29)
NOW = datetime(2026, 6, 29, 20, 30, 0, tzinfo=JST)
_BATCH_PATH = (
    Path(__file__).resolve().parents[2] / "backend" / "src" / "batch" / "decision_batch.py"
)

_CANDLE: dict[str, str] = {
    "openTime": "1782399600000",
    "open": "156.000",
    "high": "156.800",
    "low": "155.600",
    "close": "156.400",
    "volume": "8888",
}
_NARROW = [{"bid": "156.799", "ask": "156.800"}]
_WIDE = [{"bid": "156.750", "ask": "156.800"}]

_PHASE_CASES: list[tuple[str | None, str | None]] = [
    (None, None),
    ("", None),
    ("   ", None),
    (" 2 ", "2"),
    ("1", "1"),
    ("3", "3"),
    ("spike", "spike"),
    (" Spike ", "Spike"),
]
_NON_LIVE_PATHS = [
    "skipped",
    "indicator_day_skip",
    "edge_guard",
    "llm_no_entry",
    "validation_failed",
    "sl_tp_failed",
    "lot_zero",
    "missing_order_type",
    "stub",
    "dynamodb_lock_exists",
    "dynamodb_lock_unavailable",
]
_LIVE_PATHS = ["order_ok", "order_failed", "persistence_failed"]
_LIVE_PHASES: list[tuple[str, str]] = [(" 2 ", "2"), (" spike ", "spike")]

_EXPECT: dict[str, tuple[str, str | None]] = {
    "skipped": ("skipped", "active_orders_exist"),
    "indicator_day_skip": ("no_entry", "indicator_day_skip"),
    "edge_guard": ("no_entry", "wide_spread_day_skip"),
    "llm_no_entry": ("no_entry", None),
    "validation_failed": ("no_entry", "validation_failed"),
    "sl_tp_failed": ("no_entry", "sl_tp_calculation_failed"),
    "lot_zero": ("no_entry", "lot_zero"),
    "missing_order_type": ("no_entry", "missing_order_type"),
    "stub": ("stubbed", "live_orders_disabled"),
    "dynamodb_lock_exists": ("skipped", "dynamodb_lock_exists"),
    "dynamodb_lock_unavailable": ("skipped", "dynamodb_lock_unavailable"),
    "order_ok": ("ok", None),
    "order_failed": ("failed", None),
    "persistence_failed": ("persistence_failed", "order_id_write_failed"),
}


class _BeforeSaveError(RuntimeError):
    """保存より前で落とすためのテスト例外。"""


def _func_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_audit_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _func_name(node.func) == "_audit_fields"


def _expr_carries(expr: ast.AST, carrying: set[str]) -> bool:
    """式が ``_audit_fields`` の展開を含むか。キー名の文字列では判定しない。"""
    if isinstance(expr, ast.Name):
        return expr.id in carrying
    if _is_audit_call(expr):
        return True
    if isinstance(expr, ast.Dict):
        return any(
            key is None and _expr_carries(value, carrying)
            for key, value in zip(expr.keys, expr.values, strict=True)
        )
    return False


def _overrides_phase(expr: ast.AST) -> bool:
    """同じ dict リテラルが ``trading_phase`` を直書きしていないか。"""
    if not isinstance(expr, ast.Dict):
        return False
    for key, value in zip(expr.keys, expr.values, strict=True):
        if isinstance(key, ast.Constant) and key.value == "trading_phase":
            return True
        if key is None and _overrides_phase(value):
            return True
    return False


def _payload_ok(expr: ast.AST, carrying: set[str]) -> bool:
    return _expr_carries(expr, carrying) and not _overrides_phase(expr)


def _is_save_fn_ref(expr: ast.AST, name: str) -> bool:
    """呼び出しではなく、保存関数そのものの参照か。"""
    if isinstance(expr, ast.Name):
        return expr.id == name
    if isinstance(expr, ast.Attribute):
        return expr.attr == name
    return False


def _assigns_save_alias(stmt: ast.stmt) -> bool:
    """``save_decision_log`` を別名へ束縛する代入か。"""
    if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
        return False
    value = stmt.value
    return value is not None and _is_save_fn_ref(value, "save_decision_log")


def _is_phase_subscript(target: ast.AST) -> bool:
    return (
        isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Name)
        and isinstance(target.slice, ast.Constant)
        and target.slice.value == "trading_phase"
    )


def _bind(target: ast.AST, carries: bool, carrying: set[str]) -> None:
    if isinstance(target, ast.Name):
        if carries:
            carrying.add(target.id)
        else:
            carrying.discard(target.id)
        return
    if isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            if isinstance(elt, ast.Name):
                carrying.discard(elt.id)
        return
    if (
        _is_phase_subscript(target)
        and isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Name)
    ):
        carrying.discard(target.value.id)


def _apply_assignment(stmt: ast.stmt, carrying: set[str]) -> None:
    if isinstance(stmt, ast.Assign):
        carries = _expr_carries(stmt.value, carrying) and not _overrides_phase(stmt.value)
        for target in stmt.targets:
            _bind(target, carries, carrying)
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        carries = _expr_carries(stmt.value, carrying) and not _overrides_phase(stmt.value)
        _bind(stmt.target, carries, carrying)
    elif isinstance(stmt, ast.AugAssign):
        _bind(stmt.target, False, carrying)


def _direct_exprs(stmt: ast.stmt) -> list[ast.AST]:
    if isinstance(stmt, ast.Expr):
        return [stmt.value]
    if isinstance(stmt, ast.Assign):
        return [stmt.value]
    if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        return [stmt.value]
    if isinstance(stmt, ast.AugAssign):
        return [stmt.value]
    if isinstance(stmt, ast.Return) and stmt.value is not None:
        return [stmt.value]
    if isinstance(stmt, ast.If):
        return [stmt.test]
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        return [stmt.iter]
    if isinstance(stmt, ast.While):
        return [stmt.test]
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return [item.context_expr for item in stmt.items]
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return list(stmt.decorator_list)
    if isinstance(stmt, ast.Assert):
        return [stmt.test]
    if isinstance(stmt, ast.Raise) and stmt.exc is not None:
        return [stmt.exc]
    return []


# 判断ログの保存呼び出しとして監査フィールドを検査する関数名。
# `_save_decision_log` は「既存のより重い判断ログを上書きしない」ラッパー（#402）で、
# 各呼び出し元の payload をそのまま検査対象にする（ラッパー内部の転送呼び出しは除外）。
_SAVE_FUNCS = ("save_decision_log", "_save_decision_log")
_SAVE_WRAPPER_NAME = "_save_decision_log"


def _save_payload(call: ast.Call) -> ast.AST | None:
    func = _func_name(call.func)
    if func not in _SAVE_FUNCS:
        return None
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == "payload":
            return kw.value
    return None


def _saves_in(expr: ast.AST, carrying: set[str]) -> list[tuple[int, bool]]:
    found: list[tuple[int, bool]] = []
    for node in ast.walk(expr):
        if not isinstance(node, ast.Call):
            continue
        if _func_name(node.func) == "save_log":
            found.append((node.lineno, False))
            continue
        payload = _save_payload(node)
        if payload is None and _func_name(node.func) in _SAVE_FUNCS:
            found.append((node.lineno, False))
        elif payload is not None:
            found.append((node.lineno, _payload_ok(payload, carrying)))
    return found


def _walk_block(stmts: list[ast.stmt], carrying: set[str]) -> list[tuple[int, bool]]:
    found: list[tuple[int, bool]] = []
    for stmt in stmts:
        for expr in _direct_exprs(stmt):
            found.extend(_saves_in(expr, carrying))
        if _assigns_save_alias(stmt):
            found.append((stmt.lineno, False))
        if isinstance(stmt, ast.FunctionDef) and stmt.name == _SAVE_WRAPPER_NAME:
            continue  # ラッパー内部の転送呼び出しは、呼び出し元側で payload を検査する
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.extend(_walk_block(stmt.body, set()))
            continue
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            _apply_assignment(stmt, carrying)
            continue
        if isinstance(stmt, ast.If):
            found.extend(_walk_block(stmt.body, set(carrying)))
            found.extend(_walk_block(stmt.orelse, set(carrying)))
            continue
        if isinstance(stmt, ast.Try):
            found.extend(_walk_block(stmt.body, set(carrying)))
            for handler in stmt.handlers:
                found.extend(_walk_block(handler.body, set(carrying)))
            found.extend(_walk_block(stmt.orelse, set(carrying)))
            found.extend(_walk_block(stmt.finalbody, set(carrying)))
            continue
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            found.extend(_walk_block(stmt.body, set(carrying)))
            found.extend(_walk_block(stmt.orelse, set(carrying)))
            continue
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            found.extend(_walk_block(stmt.body, set(carrying)))
            continue
        if isinstance(stmt, ast.Match):
            for case in stmt.cases:
                found.extend(_walk_block(case.body, set(carrying)))
    return found


def audit_save_calls(source: str) -> list[tuple[int, bool]]:
    """保存呼び出しと、payload が監査フィールドを運ぶかを列挙する。

    ``save_decision_log`` の別名代入と、``save_log`` の直接呼び出しは運ばないものとして返す。
    """
    tree = ast.parse(source)
    return _walk_block(tree.body, set())


def _calendar(when: str | None = None) -> tuple[list[EconomicEvent], CalendarResult]:
    if when is None:
        return [], CalendarResult()
    hour, minute = (int(part) for part in when.split(":"))
    prompt = [EconomicEvent(time_jst=when, currency="USD", name="Non-Farm Payrolls", impact="High")]
    adapter = [
        AdapterEvent(
            name="Non-Farm Payrolls",
            datetime_jst=datetime(2026, 6, 29, hour, minute, tzinfo=JST),
            impact=ImpactLevel.HIGH,
            currency="USD",
        )
    ]
    return prompt, CalendarResult(events=adapter, source_ok=True)


def _decision(kind: str) -> Any:
    from backend.src.llm.trade_decision import TradeDecision, no_entry

    if kind == "no_entry":
        return no_entry("見送り")
    entry = 150.0 if kind == "far" else 156.5
    decision = TradeDecision(
        direction="LONG",
        execution_required=True,
        order_type="LIMIT",
        entry_price=entry,
        referenced_pivot_level="R1",
        sl_profile="standard",
        rr_target=1.0,
        rationale="テスト",
        sentiment_summary=None,
    )
    if kind == "drop_order_type":
        decision.order_type = None
    return decision


def _llm(kind: str) -> MagicMock:
    from backend.src.llm.client import LlmResult

    result = MagicMock(spec=LlmResult)
    result.decision = _decision(kind)
    result.input_tokens = 10
    result.output_tokens = 5
    result.model = "mock"
    return result


def _post(status: int, body: dict[str, Any] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = "stub-body"
    resp.json.return_value = body if body is not None else {"status": 0, "data": []}
    return resp


def _ok_body() -> dict[str, Any]:
    item = {
        "executionType": "LIMIT",
        "orderId": 1000101,
        "orderType": "IFDOCO",
        "price": "156.500",
        "rootOrderId": 1000101,
        "settleType": "OPEN",
        "side": "BUY",
        "size": "100",
        "status": "WAITING",
        "symbol": "USD_JPY",
    }
    return {"status": 0, "data": [item]}


def _client(
    *,
    active_orders: list[Any] | None = None,
    ticker: list[dict[str, str]] | None = None,
    post: MagicMock | None = None,
) -> MagicMock:
    client = MagicMock()
    quote = ticker if ticker is not None else _NARROW

    def _public_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        if "ticker" in path:
            resp.json.return_value = {"status": 0, "messages": [], "data": quote}
        else:
            resp.json.return_value = {"status": 0, "data": []}
        return resp

    def _private_get(path: str, params: dict[str, Any] | None = None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        if "activeOrders" in path:
            resp.json.return_value = {"status": 0, "data": {"list": active_orders or []}}
        elif "openPositions" in path:
            resp.json.return_value = {"status": 0, "data": {"list": []}}
        elif "assets" in path:
            resp.json.return_value = {
                "status": 0,
                "data": {"equity": "600000", "availableAmount": "500000"},
            }
        else:
            resp.json.return_value = {"status": 0, "data": {}}
        return resp

    client.public_get.side_effect = _public_get
    client.private_get.side_effect = _private_get
    if post is not None:
        client.private_post.return_value = post
    return client


def _apply_phase(
    monkeypatch: pytest.MonkeyPatch,
    phase: str | None,
    allow: str,
    *,
    spread_limit: str | None = None,
) -> None:
    if phase is None:
        monkeypatch.delenv("TRADING_PHASE", raising=False)
    else:
        monkeypatch.setenv("TRADING_PHASE", phase)
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", allow)
    if spread_limit is not None:
        monkeypatch.setenv("MAX_DECISION_SPREAD_PIPS", spread_limit)


def _run_path(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    phase: str | None,
    allow: str,
) -> tuple[dict[str, Any], MagicMock, MagicMock, MagicMock]:
    kind = "enter"
    active: list[Any] | None = None
    ticker = _NARROW
    events = _calendar()
    spread_limit = None
    post = _post(200, _ok_body())
    write_ok = True
    use_stop_loss = False
    use_lot = False
    lock = LockAcquireOutcome(acquired=True)
    if path == "skipped":
        active = [{"orderId": 1}]
    elif path == "indicator_day_skip":
        events = _calendar("21:30")
    elif path == "edge_guard":
        ticker = _WIDE
        spread_limit = "1.0"
    elif path == "llm_no_entry":
        kind = "no_entry"
    elif path == "validation_failed":
        kind = "far"
    elif path == "sl_tp_failed":
        use_stop_loss = True
    elif path == "lot_zero":
        use_lot = True
    elif path == "missing_order_type":
        kind = "drop_order_type"
    elif path == "order_failed":
        post = _post(400, {"status": 1})
    elif path == "persistence_failed":
        post = _post(200, _ok_body())
        write_ok = False
    elif path == "dynamodb_lock_exists":
        lock = LockAcquireOutcome(acquired=False, lock_exists=True)
    elif path == "dynamodb_lock_unavailable":
        lock = LockAcquireOutcome(acquired=False, lock_exists=False)
    elif path not in {"stub", "order_ok"}:
        raise AssertionError(f"unknown path: {path}")

    _apply_phase(monkeypatch, phase, allow, spread_limit=spread_limit)
    client = _client(active_orders=active, ticker=ticker, post=post)
    llm = MagicMock(return_value=_llm(kind))
    save = MagicMock(return_value=True)
    s3_put = MagicMock(name="save_log")
    patches = [
        patch("backend.src.batch.decision_batch.time.sleep"),
        patch(
            "backend.src.batch.decision_batch.get_confirmed_daily_candles",
            return_value=[_CANDLE] * 15,
        ),
        patch("backend.src.batch.decision_batch.generate_decision", llm),
        patch("backend.src.batch.decision_batch._fetch_macro_context", return_value=None),
        patch("backend.src.batch.decision_batch._fetch_economic_events", return_value=events),
        patch("backend.src.batch.decision_batch.get_split_session_ohlc", return_value=None),
        patch("backend.src.batch.decision_batch.is_us_thin_market", return_value=False),
        patch("backend.src.batch.decision_batch.send_slack_alert", return_value=True),
        patch(
            "backend.src.batch.decision_batch.dynamodb_store.acquire_lock_outcome",
            return_value=lock,
        ),
        patch("backend.src.batch.lock_acquire.send_slack_alert", return_value=True),
        patch(
            "backend.src.batch.decision_batch.dynamodb_store.write_order_id",
            return_value=write_ok,
        ),
        patch(
            "backend.src.batch.decision_batch.dynamodb_store.release_lock_outcome",
            return_value=LockReleaseOutcome.RELEASED,
        ),
        patch(
            "backend.src.batch.decision_batch.dynamodb_store.update_summary",
            return_value=True,
        ),
        patch("backend.src.batch.decision_batch.s3_logger.save_decision_log", save),
        patch("backend.src.persistence.s3_logger.save_log", s3_put),
    ]
    if use_stop_loss:
        patches.append(
            patch(
                "backend.src.batch.decision_batch.calculate_stop_loss",
                side_effect=ValueError("atr"),
            )
        )
    if use_lot:
        patches.append(patch("backend.src.batch.decision_batch.resolve_order_size", return_value=0))
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        result = _run(client=client, today=TODAY, now_jst=NOW)
    if path in {"skipped", "indicator_day_skip", "edge_guard"}:
        llm.assert_not_called()
    return result, save, client, s3_put


def _saved(save: MagicMock) -> dict[str, Any]:
    assert save.call_count == 1
    payload = save.call_args.args[0]
    assert isinstance(payload, dict)
    assert save.call_args.kwargs["today"] == TODAY
    return payload


def _assert_phase(payload: dict[str, Any], expected: str | None) -> None:
    assert payload["timestamp"] == NOW.isoformat()
    assert "llm_total_tokens" in payload
    assert "api_requests" in payload
    if expected is None:
        assert "trading_phase" not in payload
    else:
        assert payload["trading_phase"] == expected
        assert isinstance(payload["trading_phase"], str)


def _assert_path(
    path: str,
    result: dict[str, Any],
    save: MagicMock,
    client: MagicMock,
    s3_put: MagicMock,
    expected_phase: str | None,
    *,
    posted: bool,
) -> None:
    status, reason = _EXPECT[path]
    payload = _saved(save)
    assert s3_put.call_count == 0
    assert payload["status"] == status
    assert result["status"] == status
    if reason is None:
        assert payload.get("reason") != "live_orders_disabled"
    else:
        assert payload["reason"] == reason
    # 契約は保存 payload。validation_failed の戻り値は従来どおり短い dict のまま。
    _assert_phase(payload, expected_phase)
    if "trading_phase" in result:
        assert result["trading_phase"] == expected_phase
    if posted:
        client.private_post.assert_called()
    else:
        client.private_post.assert_not_called()


@pytest.mark.parametrize("path", _NON_LIVE_PATHS)
@pytest.mark.parametrize(("phase", "expected"), _PHASE_CASES)
def test_non_live_paths_record_runtime_phase(
    path: str,
    phase: str | None,
    expected: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """発注に進まない保存経路は、設定値の strip 結果と payload が一致する。"""
    result, save, client, s3_put = _run_path(path, monkeypatch, phase=phase, allow="")
    _assert_path(path, result, save, client, s3_put, expected, posted=False)
    assert live_orders_allowed() is False


@pytest.mark.parametrize("path", _LIVE_PATHS)
@pytest.mark.parametrize(("phase", "expected"), _LIVE_PHASES)
def test_order_result_paths_record_runtime_phase(
    path: str,
    phase: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """スタブ以外の発注結果も同じ strip 規則で、ゲートが真のときだけ POST する。"""
    result, save, client, s3_put = _run_path(path, monkeypatch, phase=phase, allow="true")
    _assert_path(path, result, save, client, s3_put, expected, posted=True)
    assert live_orders_allowed() is True


@pytest.mark.parametrize(
    ("allow", "phase", "gate", "expected_phase"),
    [
        ("true", None, False, None),
        ("true", "", False, None),
        ("true", "   ", False, None),
        ("true", "1", False, "1"),
        ("true", "3", False, "3"),
        ("true", " Spike ", False, "Spike"),
        ("false", "2", False, "2"),
        ("false", " 2 ", False, "2"),
        ("true", " 2 ", True, "2"),
        ("true", " spike ", True, "spike"),
        ("TRUE", "spike", True, "spike"),
    ],
)
def test_order_gate_matches_live_orders_allowed(
    allow: str,
    phase: str | None,
    gate: bool,
    expected_phase: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """監査の記録が発注可否を変えない。未設定をログの ``1`` にしない。"""
    result, save, client, s3_put = _run_path("stub", monkeypatch, phase=phase, allow=allow)
    assert live_orders_allowed() is gate
    payload = _saved(save)
    assert s3_put.call_count == 0
    _assert_phase(payload, expected_phase)
    if gate:
        client.private_post.assert_called()
        assert result["status"] == "ok"
    else:
        client.private_post.assert_not_called()
        assert result["status"] == "stubbed"
        assert result["stubbed"] is True


def test_holiday_early_return_does_not_save(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_PHASE", "2")
    save = MagicMock(return_value=True)
    s3_put = MagicMock(name="save_log")
    with (
        patch("backend.src.batch.decision_batch._get_today_jst", return_value=date(2026, 1, 1)),
        patch("backend.src.batch.decision_batch._get_now_jst", return_value=NOW),
        patch("backend.src.batch.decision_batch.s3_logger.save_decision_log", save),
        patch("backend.src.persistence.s3_logger.save_log", s3_put),
        patch("backend.src.batch.decision_batch.GmoFxClient") as gmo,
    ):
        result = lambda_handler({}, None)
    assert result == {"status": "skipped", "reason": "market_holiday", "date": "2026-01-01"}
    assert "trading_phase" not in result
    save.assert_not_called()
    assert s3_put.call_count == 0
    gmo.assert_not_called()


def test_exception_before_save_does_not_create_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_PHASE", "2")
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "true")
    client = _client()
    save = MagicMock(return_value=True)
    s3_put = MagicMock(name="save_log")
    with (
        patch("backend.src.batch.decision_batch.time.sleep"),
        patch(
            "backend.src.batch.decision_batch.get_confirmed_daily_candles",
            side_effect=_BeforeSaveError("before save"),
        ),
        patch("backend.src.batch.decision_batch.s3_logger.save_decision_log", save),
        patch("backend.src.persistence.s3_logger.save_log", s3_put),
        pytest.raises(_BeforeSaveError, match="before save"),
    ):
        _run(client=client, today=TODAY, now_jst=NOW)
    save.assert_not_called()
    assert s3_put.call_count == 0
    client.private_post.assert_not_called()


def test_credential_error_before_run_does_not_save(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_PHASE", "2")
    save = MagicMock(return_value=True)
    s3_put = MagicMock(name="save_log")
    with (
        patch("backend.src.batch.decision_batch._get_today_jst", return_value=TODAY),
        patch("backend.src.batch.decision_batch._get_now_jst", return_value=NOW),
        patch(
            "backend.src.batch.decision_batch.get_gmo_credentials",
            side_effect=_BeforeSaveError("credentials"),
        ),
        patch("backend.src.batch.decision_batch.s3_logger.save_decision_log", save),
        patch("backend.src.persistence.s3_logger.save_log", s3_put),
        pytest.raises(_BeforeSaveError, match="credentials"),
    ):
        lambda_handler({}, None)
    save.assert_not_called()
    assert s3_put.call_count == 0


def test_every_save_call_carries_audit_fields() -> None:
    """呼び出し個数は固定しない。監査を運ばない保存が一つでもあれば落ちる。"""
    source = _BATCH_PATH.read_text(encoding="utf-8")
    found = audit_save_calls(source)
    assert found, "save_decision_log の呼び出しが見つからない"
    missing = [line for line, ok in found if not ok]
    assert missing == []


def test_save_wrapper_call_sites_are_audited_and_wrapper_only_forwards() -> None:
    """#402: `_save_decision_log` の呼び出し元は検査対象。ラッパーは payload を無加工で転送する。"""
    source = _BATCH_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    wrapper_calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _func_name(n.func) == _SAVE_WRAPPER_NAME
    ]
    assert len(wrapper_calls) >= 8, "ラッパー経由の保存呼び出しが検査対象に入っていない"
    found_lines = {line for line, _ in audit_save_calls(source)}
    assert {n.lineno for n in wrapper_calls} <= found_lines
    wrapper = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == _SAVE_WRAPPER_NAME
    )
    inner = [
        n
        for n in ast.walk(wrapper)
        if isinstance(n, ast.Call) and _func_name(n.func) == "save_decision_log"
    ]
    assert len(inner) == 1
    assert isinstance(inner[0].args[0], ast.Name) and inner[0].args[0].id == "payload"


def test_checker_rejects_wrapper_call_without_audit() -> None:
    source = (
        "def _run():\n"
        "    _save_decision_log({'status': 'no_entry'}, today=today)\n"
    )
    assert audit_save_calls(source) == [(2, False)]


def test_checker_rejects_save_appended_without_audit() -> None:
    source = _BATCH_PATH.read_text(encoding="utf-8")
    found = audit_save_calls(source)
    extra = "\n\ndef _new_path(today):\n    s3_logger.save_decision_log({'status': 'no_entry'}, today=today)\n"
    combined = audit_save_calls(source + extra)
    assert [ok for _, ok in combined[:-1]] == [ok for _, ok in found]
    assert combined[-1][1] is False


def test_checker_rejects_phase_text_without_audit_call() -> None:
    source = (
        "def _run():\n"
        "    payload = {'trading_phase': '2', 'status': 'skipped'}\n"
        "    s3_logger.save_decision_log(payload, today=today)\n"
    )
    found = audit_save_calls(source)
    assert found == [(3, False)]


def test_checker_rejects_reassignment_and_override() -> None:
    source = (
        "def _run():\n"
        "    def _audit_fields():\n"
        "        return {}\n"
        "    payload = {**_audit_fields()}\n"
        "    payload = {'status': 'skipped'}\n"
        "    s3_logger.save_decision_log(payload, today=today)\n"
        "    other = {**_audit_fields(), 'trading_phase': '1'}\n"
        "    s3_logger.save_decision_log(other, today=today)\n"
        "    kept = {**_audit_fields()}\n"
        "    kept['trading_phase'] = '1'\n"
        "    s3_logger.save_decision_log(kept, today=today)\n"
        "    good = {**_audit_fields()}\n"
        "    s3_logger.save_decision_log({**good, 'status': 'ok'}, today=today)\n"
    )
    found = audit_save_calls(source)
    assert [ok for _, ok in found] == [False, False, False, True]


def test_checker_rejects_save_alias_and_direct_save_log() -> None:
    """別名への代入と ``save_log`` の直接呼び出しは、監査を運ぶ保存と認めない。"""
    source = (
        "def _run():\n"
        "    def _audit_fields():\n"
        "        return {}\n"
        "    payload = {**_audit_fields()}\n"
        "    _s = s3_logger.save_decision_log\n"
        "    _s(payload, today=today)\n"
        "    s3_logger.save_log(payload, 'decision-log/x.json')\n"
        "    s3_logger.save_decision_log(payload, today=today)\n"
    )
    found = audit_save_calls(source)
    assert [ok for _, ok in found] == [False, False, True]


def test_checker_rejects_alias_and_save_log_appended_to_batch() -> None:
    source = _BATCH_PATH.read_text(encoding="utf-8")
    found = audit_save_calls(source)
    extra = (
        "\n\ndef _alias_path(today, payload):\n"
        "    _s = s3_logger.save_decision_log\n"
        "    _s(payload, today=today)\n"
        "    s3_logger.save_log(payload, 'decision-log/x.json')\n"
    )
    combined = audit_save_calls(source + extra)
    assert [ok for _, ok in combined[: len(found)]] == [ok for _, ok in found]
    assert [ok for _, ok in combined[len(found) :]] == [False, False]


def test_checker_accepts_inline_and_named_audit() -> None:
    source = (
        "def _run():\n"
        "    def _audit_fields(api_reqs=0):\n"
        "        return {}\n"
        "    s3_logger.save_decision_log({**_audit_fields()}, today=today)\n"
        "    base = {**_audit_fields(api_reqs=1)}\n"
        "    result = {'status': 'stubbed', **base}\n"
        "    s3_logger.save_decision_log(result, today=today)\n"
    )
    found = audit_save_calls(source)
    assert [ok for _, ok in found] == [True, True]
