"""tests/infra/test_dev_pins.py — dev ツールが exact pin どおりに入っていること（#323）。

🔴 このテストが固定する契約:

dev ツールは長らく pyproject の `>=` から解決しており、宣言と実態が大きく
乖離していた（`mypy>=1.10` に対し実測 2.3.1 でメジャーを 1 つ跨いで稼働）。
いまは緑でも、**リリースが出た日に理由なく赤くなる**。#320 の anthropic と同じ構図。

⚠️ **上限（`mypy<3` 等）では止まらない。** すでに 2.3.1 に乗っているので `<3` は
2.4 を通すし、リンタは**マイナーでも新ルールが増える**（ruff は 0.x）。
anthropic に `<1` が効いたのは 1.0 の httpx2 という切れ目が見えていたからで、
同じ手はここでは使えない。だから exact pin にしている。

なお **stub 系（mypy-boto3-* / botocore-stubs / types-s3transfer）も対象**。
型スタブは mypy の結果に直接効くため、ここが動くと「コードを触っていないのに
型エラーが増える」が起きる。
"""

from __future__ import annotations

import pathlib
import re
from importlib.metadata import PackageNotFoundError, version

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_REQUIREMENTS = _REPO_ROOT / "requirements-dev.txt"
_CONSTRAINTS = _REPO_ROOT / "constraints-dev.txt"
_LAMBDA_CONSTRAINTS = _REPO_ROOT / "infra" / "terraform" / "lambda-constraints.txt"

#: `name[extra1,extra2]==1.2.3` から名前と版を取る。
_PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*==\s*(?P<version>\S+)$")


def _parse_pins(path: pathlib.Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _PIN.match(line)
        if m:
            pins[m.group("name")] = m.group("version")
    return pins


def test_pin_files_are_readable() -> None:
    assert _parse_pins(_REQUIREMENTS), f"{_REQUIREMENTS.name} に pin が無い"
    assert _parse_pins(_CONSTRAINTS), f"{_CONSTRAINTS.name} に pin が無い"


def test_no_range_specifiers_in_requirements() -> None:
    """`>=` や `<` が混ざっていないこと。

    範囲に戻すと #323 の目的（突然赤くならない）が失われる。
    """
    for raw in _REQUIREMENTS.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert _PIN.match(line), f"exact pin でない行がある: {line!r}"


@pytest.mark.parametrize("name,pinned", sorted(_parse_pins(_CONSTRAINTS).items()))
def test_installed_dev_dependency_matches_pin(name: str, pinned: str) -> None:
    """導入済みの版が完全 pin と一致すること。

    🔴 **直接依存 7 件だけでなく constraints 全体を見る**（#323 レビュー Should-fix）。
    直接依存だけ照合していると、`make install` を使わずに 7 件だけ手で揃えた場合に
    **スタブが動いてもテストが緑のまま**になり、「スタブが動くと型エラーが増える」を
    止めるという本来の目的を果たせない。

    ずれていたら `make install` を使っていない（pyproject の `>=` から入れている）。
    """
    try:
        installed = version(name)
    except PackageNotFoundError:  # pragma: no cover
        pytest.fail(f"{name} が未導入。`make install` で入れること（#323）")
    assert installed == pinned, (
        f"{name}: 導入済み {installed} が pin {pinned} と違う。"
        f" `make install` を使うこと（#323）"
    )


def test_requirements_are_covered_by_constraints() -> None:
    """直接依存が完全 pin 側にも同じ版で載っていること。"""
    constraints = _parse_pins(_CONSTRAINTS)
    for name, pinned in _parse_pins(_REQUIREMENTS).items():
        assert name in constraints, f"{name} が {_CONSTRAINTS.name} に無い"
        assert constraints[name] == pinned, (
            f"{name}: requirements {pinned} と constraints {constraints[name]} が不一致"
        )


def test_dev_constraints_do_not_overlap_lambda_constraints() -> None:
    """ランタイム側の pin と重複しないこと。

    make install は両方を `-c` で渡す。同じパッケージが別の版で載っていると
    解決が衝突する。ランタイム側の正は lambda-constraints.txt であり、
    dev 側はその差分だけを持つ。
    """
    overlap = set(_parse_pins(_CONSTRAINTS)) & set(_parse_pins(_LAMBDA_CONSTRAINTS))
    assert not overlap, (
        f"dev と Lambda の constraints が重複している: {sorted(overlap)}。"
        f" ランタイム側の正は lambda-constraints.txt。dev 側からは外すこと"
    )


def test_type_stubs_are_pinned() -> None:
    """型スタブが完全 pin に載っていること。

    mypy 本体だけ固定してもスタブが動くと型エラーが増える。
    載っていれば上の :func:`test_installed_dev_dependency_matches_pin` が
    導入済みの版まで照合する。
    """
    constraints = _parse_pins(_CONSTRAINTS)
    for stub in ("botocore-stubs", "mypy-boto3-s3", "mypy-boto3-dynamodb", "mypy-boto3-secretsmanager"):
        assert stub in constraints, f"{stub} が {_CONSTRAINTS.name} に無い（型結果に効く）"
