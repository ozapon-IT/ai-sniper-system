"""tests/backtest の収集条件。

backtest/ は fxlab（fx-backtest-lab・プライベートリポジトリ）に依存する。fxlab が
入っていない環境（GitHub の読み取りトークン未設定の CI など）では、発注経路の
トークン検査（test_guardrails.py）だけを残して他を収集しない。
入れ方: ``make install-backtest``（requirements-backtest.txt）。
"""

from __future__ import annotations

import importlib.util
import warnings

FXLAB_AVAILABLE = importlib.util.find_spec("fxlab") is not None

collect_ignore_glob: list[str] = []
if not FXLAB_AVAILABLE:
    warnings.warn(
        "fxlab が無いため tests/backtest は test_guardrails.py 以外を収集しない"
        "（make install-backtest で入る）",
        stacklevel=1,
    )
    collect_ignore_glob = [
        "test_baseline_report.py",
        "test_cli.py",
        "test_context.py",
        "test_data.py",
        "test_engine.py",
        "test_subagent.py",
    ]
