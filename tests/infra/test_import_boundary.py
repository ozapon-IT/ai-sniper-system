"""tests/infra/test_import_boundary.py — 夜型 / ORB の import 境界テスト（#331）。

🔴 このテストが守る契約:

ORB デモは**リポジトリを分離しない**判断をした（2026-08-28）。共有している 6 モジュールに
事故の知識が集中しており、複製すると片方だけ直るため。その代わり、リポジトリ境界が
やっていたはずの「勝手に絡まらない保証」をこのテストが引き受ける。

加えて ``docs/architecture.md`` / ``docs/features.md`` は #329 / PR #330 で
**「逆依存 0」を事実として明記した**。テストが無ければ次の PR で静かに壊れ、
ドキュメントが嘘になる。

``check_no_public_endpoints.py`` のテストと同じ方針で、「検出できること」だけでなく
「**正常系で誤検知しないこと**」も固定する。誤検知で CI が常に赤いと、いずれ無効化されるため。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.check_import_boundary import (
    _ALLOWED_SHARED,
    _imported_modules,
    find_violations,
)

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "backend" / "src"


def _write(root: Path, rel: str, source: str) -> None:
    """``root`` 配下に擬似モジュールを置く。"""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


# ---------------------------------------------------------------------------
# 契約 1: 夜型 → ORB は禁止（逆依存 0）
# ---------------------------------------------------------------------------

_NIGHT_SIDE_FILES = [
    "batch/decision_batch.py",
    "batch/closeout_batch.py",
    "kill_switch/handler.py",
    "read_api/handler.py",
    "monitor/closeout_watchdog.py",
    "engine/enrich.py",
    "llm/client.py",
]

_ORB_MODULE_FORMS = [
    "backend.src.orb",
    "backend.src.orb.session_store",
    "backend.src.batch.orb_poller",
    "backend.src.adapters.orb_quotes",
]


@pytest.mark.parametrize("night_file", _NIGHT_SIDE_FILES)
def test_night_importing_orb_is_detected(tmp_path: Path, night_file: str) -> None:
    """夜型のどのモジュールからでも ORB import を検出すること。"""
    _write(tmp_path, night_file, "from backend.src.orb.session_store import load_session\n")
    violations = find_violations([tmp_path])
    assert len(violations) == 1
    assert violations[0].rule == "night_imports_orb"
    assert violations[0].path == night_file


@pytest.mark.parametrize("orb_module", _ORB_MODULE_FORMS)
def test_all_orb_module_forms_are_detected(tmp_path: Path, orb_module: str) -> None:
    """ORB の指し方（コア / batch / adapter / パッケージ自体）を取りこぼさないこと。"""
    _write(tmp_path, "batch/decision_batch.py", f"import {orb_module}\n")
    violations = find_violations([tmp_path])
    assert [v.rule for v in violations] == ["night_imports_orb"]
    assert violations[0].imported == orb_module


def test_orb_internal_imports_are_allowed(tmp_path: Path) -> None:
    """ORB 内部での相互参照は自由（orb_verify → orb_note_batch の実例）。"""
    _write(
        tmp_path,
        "batch/orb_verify.py",
        "from backend.src.batch.orb_note_batch import run_note_generation\n"
        "from backend.src.orb.session_store import load_session\n",
    )
    assert find_violations([tmp_path]) == []


def test_from_package_import_submodule_is_detected(tmp_path: Path) -> None:
    """🔴 ``from pkg import submodule`` 形式を見逃さないこと。

    ``from a.b import c`` を ``a.b`` に潰すと
    ``from backend.src.batch import orb_poller`` が ``backend.src.batch`` になり、
    **禁止 import をすり抜ける**（見逃しは危険な方向）。候補に ``a.b.c`` も含めて塞ぐ。
    """
    _write(
        tmp_path,
        "batch/closeout_batch.py",
        "from backend.src.batch import orb_poller\nfrom backend.src.adapters import orb_quotes\n",
    )
    violations = find_violations([tmp_path])
    assert {v.imported for v in violations} == {
        "backend.src.batch.orb_poller",
        "backend.src.adapters.orb_quotes",
    }
    assert {v.rule for v in violations} == {"night_imports_orb"}


def test_from_package_import_allowed_module_is_not_flagged(tmp_path: Path) -> None:
    """逆に ``from backend.src import logging_config`` を誤検知しないこと。

    禁止判定は候補の**どれかが該当したら違反**、許可判定は候補の**どれかが許可
    されていれば OK** という非対称な扱いで両立させている。
    """
    _write(
        tmp_path,
        "batch/orb_poller.py",
        "from backend.src import logging_config\nfrom backend.src.clients import gmo_client\n",
    )
    assert find_violations([tmp_path]) == []


def test_adjacent_package_name_is_not_matched(tmp_path: Path) -> None:
    """``backend.src.llm_foo`` のような隣接名を禁止扱いしないこと。

    素の ``startswith`` だと ``backend.src.llm`` に前方一致してしまう。
    境界は「パッケージ配下か」で引くので区切りの ``.`` まで見る。
    """
    _write(tmp_path, "orb/range.py", "from backend.src.llm_foo import helper\n")
    violations = find_violations([tmp_path])
    # 判断系ではなく「未許可の共有依存」として出るのが正しい
    assert [v.rule for v in violations] == ["shared_surface_grew"]


# ---------------------------------------------------------------------------
# 契約 2: ORB → 夜型の判断系は禁止
# ---------------------------------------------------------------------------

_DECISION_LAYER = [
    "backend.src.llm.client",
    "backend.src.engine.atr",
    "backend.src.validation.pre_order",
]


@pytest.mark.parametrize("module", _DECISION_LAYER)
def test_orb_importing_decision_layer_is_detected(tmp_path: Path, module: str) -> None:
    """ORB が LLM / 数理 / 発注バリデーションに触れたら落とすこと。

    ORB は「判断ゼロの決定論」であることが設計の核（設計書 §0）。
    """
    _write(tmp_path, "batch/orb_session_open.py", f"from {module} import thing\n")
    violations = find_violations([tmp_path])
    assert [v.rule for v in violations] == ["orb_imports_decision_layer"]


# ---------------------------------------------------------------------------
# 契約 3: 共有面のゴールデン
# ---------------------------------------------------------------------------


def test_new_shared_dependency_is_detected(tmp_path: Path) -> None:
    """許可リストに無い共有モジュールへの依存を検出すること。"""
    _write(tmp_path, "batch/orb_weekly.py", "from backend.src.rate_limiter import RateLimiter\n")
    violations = find_violations([tmp_path])
    assert [v.rule for v in violations] == ["shared_surface_grew"]
    assert violations[0].imported == "backend.src.rate_limiter"


def test_shared_violation_reports_module_not_attribute(tmp_path: Path) -> None:
    """報告名は許可リストにそのまま貼れるモジュール名であること。

    ``from backend.src import rate_limiter`` のように基底が ``backend.src`` そのものの
    ときだけ、具体名の方を出す（基底では情報が無いため）。
    """
    _write(tmp_path, "orb/note.py", "from backend.src import rate_limiter\n")
    violations = find_violations([tmp_path])
    assert [v.imported for v in violations] == ["backend.src.rate_limiter"]


def test_allowed_shared_modules_pass(tmp_path: Path) -> None:
    """許可リスト内の import は誤検知しないこと。"""
    body = "".join(f"import {m}\n" for m in sorted(_ALLOWED_SHARED))
    _write(tmp_path, "batch/orb_verify.py", body)
    assert find_violations([tmp_path]) == []


def test_allowlist_matches_reality() -> None:
    """🔴 許可リストが実際の依存と一致していること（ゴールデン）。

    許可リストに載っているのに誰も使っていないモジュールがあれば、共有面は
    **狭められる**。「昔使っていた」が残り続けると、境界の意味が薄れていく。
    """
    used = {v.imported for v in find_violations([_BACKEND_SRC]) if v.rule == "shared_surface_grew"}
    assert used == set(), f"未許可の依存が増えている: {sorted(used)}"

    # 逆向き: 許可しているが実際には使われていないものを検出する。
    # 🔴 走査は checker の _imported_modules を使う。ここで AST を再実装すると
    #    判定がずれ、「テストは通るが checker は落ちる」（またはその逆）が起きる。
    actually_used: set[str] = set()
    for path in _BACKEND_SRC.rglob("*.py"):
        rel = path.relative_to(_BACKEND_SRC).as_posix()
        if not (
            rel.startswith("orb/")
            or rel.startswith("batch/orb_")
            or rel == "adapters/orb_quotes.py"
        ):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for candidates, _lineno in _imported_modules(tree):
            actually_used.update(candidates)

    unused = _ALLOWED_SHARED - actually_used
    assert unused == set(), (
        f"許可リストに載っているが ORB から使われていない: {sorted(unused)}。"
        "共有面は狭いほどよいので、_ALLOWED_SHARED から外すこと"
    )


# ---------------------------------------------------------------------------
# 契約 4: orb/ コアは I/O を直接持たない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    ["backend.src.clients.gmo_client", "backend.src.persistence.s3_logger"],
)
def test_orb_core_importing_io_is_detected(tmp_path: Path, module: str) -> None:
    """``orb/`` コアが GMO / S3 を直接叩いたら落とすこと。

    外部 I/O は ``batch/orb_*`` と ``adapters/orb_quotes`` に閉じる。コアが純粋なら
    リプレイ検証（設計書 §8）とゴールデンテストが素直に書ける。
    """
    _write(tmp_path, "orb/evaluate.py", f"from {module} import thing\n")
    violations = find_violations([tmp_path])
    assert [v.rule for v in violations] == ["orb_core_imports_io"]


def test_orb_batch_layer_may_use_io(tmp_path: Path) -> None:
    """同じ import でも batch / adapter 層なら許可されること（契約 4 は core 限定）。"""
    _write(
        tmp_path, "batch/orb_poller.py", "from backend.src.clients.gmo_client import GmoClient\n"
    )
    _write(
        tmp_path, "adapters/orb_quotes.py", "from backend.src.clients.gmo_client import GmoClient\n"
    )
    assert find_violations([tmp_path]) == []


# ---------------------------------------------------------------------------
# 誤検知しないこと / 実ツリー
# ---------------------------------------------------------------------------


def test_third_party_and_relative_imports_are_ignored(tmp_path: Path) -> None:
    """外部ライブラリと相対 import は対象外（境界を跨がないため）。"""
    _write(
        tmp_path,
        "orb/range.py",
        "from __future__ import annotations\n"
        "import json\n"
        "import httpx\n"
        "from pydantic import BaseModel\n"
        "from . import constants\n"
        "from .models import OrbSession\n",
    )
    assert find_violations([tmp_path]) == []


def test_night_side_may_use_anything_else(tmp_path: Path) -> None:
    """夜型側は ORB 以外なら自由（境界は ORB との間にだけ引く）。"""
    _write(
        tmp_path,
        "batch/closeout_batch.py",
        "from backend.src.clients.gmo_client import GmoClient\n"
        "from backend.src.llm.trade_note import build_note\n"
        "from backend.src.engine.atr import compute_atr\n"
        "from backend.src.persistence.s3_logger import S3Logger\n",
    )
    assert find_violations([tmp_path]) == []


def test_skips_pycache(tmp_path: Path) -> None:
    """``__pycache__`` 配下は対象外（古いコピーで恒久的な誤検知になる）。"""
    _write(tmp_path, "batch/__pycache__/decision_batch.py", "import backend.src.orb\n")
    assert find_violations([tmp_path]) == []


def test_missing_root_does_not_crash(tmp_path: Path) -> None:
    """存在しないパスを渡しても落ちない（CI の実行位置差で落とさない）。"""
    assert find_violations([tmp_path / "nope"]) == []


def test_real_backend_tree_is_clean() -> None:
    """🔴 実際の backend/src が境界を守っていること（本番の契約そのもの）。

    ここが赤くなったら、ORB と夜型が絡み始めている。ドキュメント
    （architecture.md / features.md）の「逆依存 0」も同時に嘘になる。
    """
    assert _BACKEND_SRC.exists(), f"backend/src が見つからない: {_BACKEND_SRC}"
    violations = find_violations([_BACKEND_SRC])
    assert violations == [], (
        "夜型 / ORB の import 境界が壊れている（#331）。"
        "ORB はリポジトリ分離しない代わりにこの境界で守っている: "
        f"{[(v.path, v.imported, v.rule) for v in violations]}"
    )
