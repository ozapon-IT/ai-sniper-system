"""tests/infra/test_terraform_version_pin.py — terraform CLI の版と dual-lock（#347）。

🔴 このテストが固定する契約:

Homebrew 直実行のままだと `brew upgrade terraform` が将来の削除マイナー
（DynamoDB ロック撤去）に黙って乗る。mise.toml と staging の
required_version を同じ版に固定し、backend "s3" が staging の 1 本だけ
であること、かつ Phase B の dual-lock（`use_lockfile = true` と
`dynamodb_table`）が両方残っていることをテキスト検査する。

HCL/TOML を読むだけ。`terraform` バイナリ非依存で CI に載る
（既存 test_orb_terraform.py と同じ）。
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MISE_TOML = _REPO_ROOT / "mise.toml"
_STAGING = _REPO_ROOT / "infra" / "terraform" / "environments" / "staging"
_STAGING_VERSIONS = _STAGING / "versions.tf"
_STAGING_BACKEND = _STAGING / "backend.tf"
_TF_ROOT = _REPO_ROOT / "infra" / "terraform"

_PINNED = "1.15.7"
_REQUIRED = f'~> {_PINNED}'
_BACKEND_S3 = re.compile(r'backend\s+"s3"')
_USE_LOCKFILE = re.compile(r"use_lockfile\s*=\s*true")
_DYNAMODB_TABLE = re.compile(r'dynamodb_table\s*=\s*"your-tflock-table"')


def _strip_comments(hcl: str) -> str:
    """HCL からコメントを除いた設定部分だけを返す（test_orb_terraform.py と同型）。

    出典コメントが `use_lockfile` に言及しても、設定行の有無とは独立に見る。
    """
    out: list[str] = []
    for line in hcl.splitlines():
        in_quote = False
        cut = len(line)
        for i, ch in enumerate(line):
            if ch == '"':
                in_quote = not in_quote
            elif ch == "#" and not in_quote:
                cut = i
                break
        code = line[:cut].rstrip()
        if code:
            out.append(code)
    return "\n".join(out)


def _mise_tools_block() -> str:
    text = _MISE_TOML.read_text(encoding="utf-8")
    match = re.search(r"(?ms)^\[tools\]\n(.*?)(?=^\[|\Z)", text)
    assert match, "mise.toml に [tools] が無い"
    return match.group(1)


def test_mise_toml_pins_terraform_patch() -> None:
    """mise.toml が terraform をパッチまで固定していること。

    cookbook 例の `terraform = "1"` は広すぎる（#347 不採用）。
    """
    tools = _mise_tools_block()
    assert f'terraform = "{_PINNED}"' in tools, (
        f"mise.toml の [tools] に terraform = \"{_PINNED}\" が無い。"
        " Homebrew 直実行に戻すと brew upgrade が 1.16+ に乗る（#347）"
    )


def test_staging_required_version_rejects_1_16() -> None:
    """staging ルートが ~> 1.15.7（または同等の上下限）であること。

    出典: https://developer.hashicorp.com/terraform/language/expressions/version-constraints
    （参照日 2026-09-05）。`~> 1.15.7` は `>= 1.15.7, < 1.16.0` と同義。
    """
    text = _STAGING_VERSIONS.read_text(encoding="utf-8")
    match = re.search(r'required_version\s*=\s*"([^"]+)"', text)
    assert match, f"{_STAGING_VERSIONS} に required_version が無い"
    constraint = match.group(1).strip()
    allowed = {_REQUIRED, f">= {_PINNED}, < 1.16.0", f">= {_PINNED},< 1.16.0"}
    assert constraint in allowed, (
        f"staging required_version が 1.16 以上を拒否していない: {constraint!r}。"
        f" 期待 {_REQUIRED}（または >= {_PINNED}, < 1.16.0）"
    )


def test_backend_s3_is_only_staging() -> None:
    """backend s3 は staging の 1 本だけであること（#347 対応案 3）。

    ORB / serverless_app モジュールに backend を足すと、ロック移行の対象が
    増える。テキスト検査で本数を固定する。
    """
    hits = [
        path.relative_to(_REPO_ROOT)
        for path in _TF_ROOT.rglob("*.tf")
        if ".terraform" not in path.parts and _BACKEND_S3.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == [Path("infra/terraform/environments/staging/backend.tf")], (
        f'backend "s3" が staging の 1 本だけではない: {hits}'
    )
    assert _STAGING_BACKEND.exists(), f"{_STAGING_BACKEND} が無い"


def test_staging_backend_is_dual_lock() -> None:
    """staging backend が use_lockfile と dynamodb_table の dual-lock であること。

    #347 Phase B。片方だけ（特に dynamodb_table 削除）を許さない。
    出典: https://developer.hashicorp.com/terraform/language/backend/s3
    （Issue 参照日 2026-09-03 / 再確認 2026-09-05）。
    """
    text = _strip_comments(_STAGING_BACKEND.read_text(encoding="utf-8"))
    assert _USE_LOCKFILE.search(text), (
        f"{_STAGING_BACKEND} に use_lockfile = true が無い（コメントは数えない）"
    )
    assert _DYNAMODB_TABLE.search(text), (
        f"{_STAGING_BACKEND} から dynamodb_table = \"your-tflock-table\" が消えている。"
        " Phase B は dual-lock。tflock 撤去は後続 Issue"
    )
