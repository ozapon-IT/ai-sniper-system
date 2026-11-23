"""backtest パッケージのガードレール適合テスト（AGENTS.md #1）。

バックテストは Public GET とローカルキャッシュのみで完結し、
発注系コード（batch / private API / 発注エンドポイント）に構造的に到達しない。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKTEST_DIR = _ROOT / "backtest"

# 発注・Private API への到達を示すトークン（ソースに存在してはならない）。
# GMO FX の発注・変更・取消・決済系エンドポイント名を網羅する
# （公式 https://api.coin.z.com/fxdocs/ 2026-07-02 参照。本システム未使用の
# ifdOrder / changeIfdOrder 等も将来の混入検知のため含める。単純発注 /v1/order は
# 誤検知を避けトークン化しない — "/private/" と "PRIVATE_" が到達経路を捕捉する）。
# "cancelOrder"（単数）は "cancelOrders" も部分一致で捕捉する。
# "closeBulkOrder" は FX API に存在しない（暗号資産API側の名称・#230）。混入は
# 仕様誤認のサインなので、実在しないエンドポイント名も検知対象として残す。
_FORBIDDEN_TOKENS = (
    "private_post",
    "private_get",
    "PRIVATE_",
    "/private/",
    "ifoOrder",
    "ifdOrder",
    "changeOrder",
    "changeOcoOrder",
    "changeIfdOrder",
    "changeIfoOrder",
    "cancelOrder",
    "cancelBulkOrder",
    "closeOrder",
    "closeBulkOrder",
    "speedOrder",
)


def test_backtest_sources_do_not_reference_order_paths() -> None:
    py_files = sorted(_BACKTEST_DIR.rglob("*.py"))
    assert py_files, "backtest/ に Python ソースが見つからない"
    for path in py_files:
        source = path.read_text(encoding="utf-8")
        for token in _FORBIDDEN_TOKENS:
            assert token not in source, f"{path.name} が発注系トークン {token!r} を参照している"


# backtest の import で読み込みを許可する backend.* モジュール（許可リスト方式）。
# すべて POST・外部送信を持たない純関数/定数/スキーマ。batch（発注オーケストレーション）、
# clients（HTTP クライアント）、persistence / alerting（外部送信副作用）等は列挙しない
# ことで混入を検知する。backtest が新たに本番モジュールを再利用する場合は、
# 純関数であることを確認したうえでここに明示追加すること。
_ALLOWED_BACKEND_MODULES = frozenset(
    {
        "backend",
        "backend.src",
        "backend.src.adapters",
        "backend.src.adapters.klines",
        "backend.src.adapters.macro_context",  # prompt.py が型・整形関数を import
        "backend.src.engine",
        "backend.src.engine.atr",
        "backend.src.engine.enrich",
        "backend.src.engine.leverage_cap",
        "backend.src.engine.pivot",
        "backend.src.engine.position_sizing",
        "backend.src.engine.trading_day",  # JST 定数 + jst_time_label（純関数・#345）
        "backend.src.gmo_fx_endpoints",
        "backend.src.llm",
        "backend.src.llm.client",  # mock_llm が LlmResult を import（import 時純粋・anthropic は遅延）
        "backend.src.llm.mock_llm",  # sources.MockSource が使用（決定論・API 呼び出しなし）
        "backend.src.llm.prompt",
        "backend.src.llm.sl_profile",  # RD-12 ボラシグナル・sl_profile マップ（純関数）
        "backend.src.llm.trade_decision",
        "backend.src.validation",
        "backend.src.validation.day_skip_guards",  # RD-8 エッジガード（純関数・#198 BT 貫通）
        "backend.src.validation.pre_order",
    }
)


@pytest.mark.skipif(
    importlib.util.find_spec("fxlab") is None,
    reason="fxlab が無いと backtest を import できない（make install-backtest）",
)
def test_importing_backtest_loads_only_allowlisted_backend_modules() -> None:
    """backtest の import でロードされる backend.* を許可リストで検証（サブプロセス）。

    同一プロセスだと他テストの import 済みモジュール（sys.modules）に汚染されるため、
    クリーンなインタプリタで確認する。denylist（batch のみ禁止）ではなく allowlist に
    することで、alerting / persistence / clients 等の副作用モジュールの混入も検知する。
    """
    code = (
        "import sys; "
        "import backtest.data, backtest.context, backtest.engine, "
        "backtest.edge_guard, backtest.sources, backtest.runner, backtest.report, backtest.subagent; "
        f"allowed = {sorted(_ALLOWED_BACKEND_MODULES)!r}; "
        "loaded = [m for m in sys.modules if m.startswith('backend')]; "
        "disallowed = [m for m in loaded if m not in allowed]; "
        "assert not disallowed, f'non-allowlisted backend modules loaded: {disallowed}'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
