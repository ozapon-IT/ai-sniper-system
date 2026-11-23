"""tests/infra/test_spec_sections.py — 仕様書 §1.6.x の連番・順序を固定する。

🔴 なぜ必要か（2026-08-12 の実際の事故）:

#291 で §1.6.4 を §1.6.3 の**前**に挿入してしまい、その後 `1.6.4 〜 §2` の範囲を
置換した際に **§1.6.3（再検討トリガー）を巻き込んで削除**した。
§1.6.3 は「この判断を将来へ自動的に引き継がせない」ための中心統制であり、
本文が消えても他の節（§1.6.1 の「見直す条件は 1.6.3 に定める」）は参照し続けるため、
**壊れたことに気づけない**。番号の欠落と順序の乱れを機械的に落とす。
"""

from __future__ import annotations

import re
from pathlib import Path

_SPEC = Path(__file__).resolve().parents[2] / "docs" / "system_requirements.md"
_HEADING_RE = re.compile(r"^#### (1\.6\.\d+)\s", re.M)


def _numbers() -> list[str]:
    return _HEADING_RE.findall(_SPEC.read_text())


def test_subsections_are_contiguous_and_ordered() -> None:
    """§1.6.1 から欠番なし・昇順で並んでいること。"""
    found = _numbers()
    assert found, "§1.6.x の見出しが 1 つも無い"
    expected = [f"1.6.{i}" for i in range(1, len(found) + 1)]
    assert found == expected, f"§1.6.x の連番/順序が壊れている: {found}"


def test_no_duplicate_subsections() -> None:
    found = _numbers()
    assert len(found) == len(set(found)), f"§1.6.x に重複がある: {found}"


def test_referenced_subsections_exist() -> None:
    """本文が参照している §1.6.x が実在すること（1.6.3 消失の再発防止）。"""
    text = _SPEC.read_text()
    referenced = set(re.findall(r"(?:§|節\s*)?(1\.6\.\d+)", text))
    existing = set(_numbers())
    missing = sorted(referenced - existing)
    assert not missing, f"参照されているが本文が無い節: {missing}"
