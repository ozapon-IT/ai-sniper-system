"""tests/infra/test_runtime_pins.py — CI が本番と同じランタイム依存を検証していること（#322）。

🔴 このテストが固定する契約:

CI は長らく `pip install -e ".[dev]"` で pyproject の `>=` から解決しており、
**Lambda に載る依存を本番と違う版で検証していた**。#320 では anthropic 1.0.0
（HTTP クライアントが httpx → httpx2）を引いて全 PR と main の CI が落ち、
上限を付けた後も「CI 0.125.0 / 本番 0.112.0」の乖離が残っていた。

インストール手順（Makefile / ci.yml）を戻すと乖離は**静かに復活する**。
CI が緑のまま本番と違う SDK を検証する状態は、次の #320 を見逃す。
そこで導入済みの版を実測し、Lambda の pin と一致することをここで固定する。

⚠️ boto3 / botocore は対象外。`lambda-requirements.txt` の方針で
**Lambda ランタイム同梱**（サイズ削減）であり、本番の版は AWS が決めるため
pin では揃わない。テストは全モックのため実害は小さい（#322）。
"""

from __future__ import annotations

import pathlib
from importlib.metadata import PackageNotFoundError, version

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_REQUIREMENTS = _REPO_ROOT / "infra" / "terraform" / "lambda-requirements.txt"
_CONSTRAINTS = _REPO_ROOT / "infra" / "terraform" / "lambda-constraints.txt"


def _parse_pins(path: pathlib.Path) -> dict[str, str]:
    """`name==version` 形式を読む（コメント・空行は無視）。"""
    pins: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, pinned = line.split("==", 1)
        pins[name.strip()] = pinned.strip()
    return pins


def test_requirements_file_is_readable() -> None:
    """pin ファイル自体が存在し、空でないこと（パス変更の検知）。"""
    assert _parse_pins(_REQUIREMENTS), f"{_REQUIREMENTS} に pin が無い"
    assert _parse_pins(_CONSTRAINTS), f"{_CONSTRAINTS} に pin が無い"


@pytest.mark.parametrize("name,pinned", sorted(_parse_pins(_REQUIREMENTS).items()))
def test_installed_runtime_matches_lambda_pin(name: str, pinned: str) -> None:
    """導入済みのランタイム依存が Lambda の pin と一致すること。

    ずれていたらインストール手順が pyproject の `>=` に戻っている
    （`make install` を使っていない）。CI が本番と違う SDK を検証している状態。
    """
    try:
        installed = version(name)
    except PackageNotFoundError:  # pragma: no cover - 導入漏れは即座に分かる
        pytest.fail(f"{name} が未導入。`make install` で入れること（#322）")
    assert installed == pinned, (
        f"{name}: 導入済み {installed} が Lambda pin {pinned} と違う。"
        f" `make install` を使うこと（pyproject の >= から解決していないか確認・#322）"
    )


def test_requirements_are_covered_by_constraints() -> None:
    """requirements の各行が constraints にも同じ版で載っていること。

    片方だけ更新すると build_lambda.sh が別の版を掴む。
    """
    constraints = _parse_pins(_CONSTRAINTS)
    for name, pinned in _parse_pins(_REQUIREMENTS).items():
        assert name in constraints, f"{name} が constraints に無い"
        assert constraints[name] == pinned, (
            f"{name}: requirements {pinned} と constraints {constraints[name]} が不一致"
        )


def test_boto3_is_intentionally_unpinned() -> None:
    """boto3 を pin 対象に入れないこと（Lambda ランタイム同梱・#322）。

    追加すると Lambda バンドルが肥大化し、しかも本番の版とは揃わない
    （AWS が決めるため）。意図的な除外であることを固定する。
    """
    for path in (_REQUIREMENTS, _CONSTRAINTS):
        pins = _parse_pins(path)
        assert "boto3" not in pins, f"{path.name} に boto3 が入っている（ランタイム同梱のため不要）"
        assert "botocore" not in pins, f"{path.name} に botocore が入っている（同上）"
