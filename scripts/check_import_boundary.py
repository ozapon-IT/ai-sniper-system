#!/usr/bin/env python3
"""夜型スナイパー / ロンドン ORB デモの import 境界を検査する（#331）.

🔴 **なぜ CI で見張るのか**

2026-08-28 に ORB のリポジトリ分離を検討し、**見送る判断**をした。理由は共有している
6 モジュール（``logging_config`` / ``gmo_errors`` / ``clients.gmo_client`` /
``alerting.slack_alert`` / ``persistence.s3_logger`` / ``gmo_fx_endpoints``）に
**事故の知識が集中している**ためで、複製すると片方だけ直る。実例として ORB の
``orb_quotes`` にエンベロープ検査を足した #327 は、夜型で踏んだ #243 と同型の
バグをパターン認識で見つけたものだった。

分離を見送った結果、**「勝手に絡まらない保証」だけが未担保**で残る。リポジトリ境界が
やっていたはずの仕事を、このチェックが代わりに引き受ける。

さらに ``docs/architecture.md`` と ``docs/features.md`` は #329 / PR #330 で
**「逆依存 0」「夜型のコードは ORB を一切 import しない」と事実として明記した**。
テストが無ければ次の PR で静かに壊れ、ドキュメントが嘘になる。

検査する 4 つの契約:

1. **夜型 → ORB の import が無いこと**（逆依存 0）
   ORB は夜型の配管を借りる側であり、逆は無い。ここが破れると 2 サブシステムが
   相互依存になり、ORB のデモ終了後に切り離せなくなる。
2. **ORB → ``llm`` / ``engine`` / ``validation`` の import が無いこと**
   ORB は「判断ゼロの決定論」であることが設計の核（設計書 §0・LLM を使わない）。
   夜型の判断系に触れた時点でその前提が崩れる。
3. **ORB が依存してよい共有モジュールが許可リストの範囲内であること**（ゴールデン）
   増えること自体は禁止しないが、**無自覚に増えることを禁止する**。追加は
   ``_ALLOWED_SHARED`` に理由付きで足すこと（＝レビューで見える差分になる）。
4. **``orb/`` コアが夜型の I/O 実装（``clients`` / ``persistence``）を import しないこと**
   ⚠️ **「``orb/`` がピュア」という意味ではない。** ``session_store`` / ``event_log`` /
   ``note_store`` は boto3 で DynamoDB・S3 を直接叩いており、ORB 自身の永続化層である。
   この契約が禁じるのは**夜型の I/O 実装にコアがくっつくこと**で、GMO アクセスは
   ``batch/orb_*`` と ``adapters/orb_quotes``、永続化は ORB 自身の 3 モジュールに閉じる。
   ``evaluate`` / ``broker`` / ``range`` など判定ロジック側は実際にピュアで、
   リプレイ検証（§8）とゴールデンテストが素直に書ける。

⚠️ **検出範囲の限界（認識した上で許容している）**

- **動的 import は見ない。** ``importlib.import_module``、文字列で組み立てたモジュール名、
  ``__getattr__`` 経由の遅延ロードは検出しない。現在のコードベースにそうした呼び出しは無い。
- **``backend.src`` 配下しか見ない。** 例えば ``orb_session_open`` は
  ``from config.market_calendar import is_market_holiday`` しているが、``config/`` は
  ``_PACKAGE`` の外なので契約 3（共有面のゴールデン）の対象外である。許可リストの 6 件は
  「ORB が依存する全モジュール」ではなく「**ORB が依存する夜型モジュール**」であることに注意。
  ``config/`` は両サブシステムが等しく使う設定であり、境界の内側ではない。

いずれも単一利用者・レビュー前提の運用では十分と判断している。

使い方::

    python scripts/check_import_boundary.py            # 既定の backend/src を検査
    python scripts/check_import_boundary.py <path> ...
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

#: 検査対象のパッケージルート（リポジトリルートからの相対）。
_DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "backend" / "src"

#: import されたモジュール名の接頭辞。この配下だけを境界検査の対象にする。
_PACKAGE = "backend.src"

# ---------------------------------------------------------------------------
# ORB 側の定義（パスで判定する）
# ---------------------------------------------------------------------------

#: ORB サブシステムに属するモジュールのパターン（backend/src からの相対 POSIX パス）。
#: ``orb/`` はコア、``batch/orb_*`` は Lambda ハンドラ、``adapters/orb_quotes.py`` は
#: GMO Public API アダプタ。この 3 つで ORB は閉じている。
_ORB_CORE_PREFIX = "orb/"
_ORB_BATCH_PREFIX = "batch/orb_"
_ORB_ADAPTER = "adapters/orb_quotes.py"

#: ORB 側が import してよい非 ORB モジュールの許可リスト（契約 3 のゴールデン）。
#:
#: 🔴 **増やすときは「なぜ ORB がそれを必要とするか」を PR 本文に書くこと。**
#: 共有面が広がるほど、将来 ORB を切り離すコストが上がる。
_ALLOWED_SHARED: frozenset[str] = frozenset(
    {
        "backend.src.logging_config",  # 構造化ログ（JSON・タイムスタンプ）
        "backend.src.gmo_errors",  # HTTP 200 + status:1 の業務エラー検出（#243 / #327）
        "backend.src.gmo_fx_endpoints",  # エンドポイント定数（パスのハードコード禁止）
        "backend.src.clients.gmo_client",  # Public API の GET（public_get のみ使用）
        "backend.src.alerting.slack_alert",  # halt / 週次サマリの通知
        "backend.src.persistence.s3_logger",  # ノート・検証結果の保存
    }
)

#: ORB が触ってはならない夜型の判断系（契約 2）。
#: ORB は LLM を使わない決定論ルールであり、これらに依存した時点で前提が崩れる。
_FORBIDDEN_FOR_ORB: tuple[str, ...] = (
    "backend.src.llm",
    "backend.src.engine",
    "backend.src.validation",
)

#: ``orb/`` コアが直接触ってはならない**夜型の** I/O 実装（契約 4）。
#: ORB 自身の永続化（session_store / event_log / note_store の boto3）は対象外。
#: 禁じているのは「夜型の GMO / S3 実装にコアがくっつくこと」。
_FORBIDDEN_FOR_ORB_CORE: tuple[str, ...] = (
    "backend.src.clients",
    "backend.src.persistence",
)


class Violation(NamedTuple):
    """検出した境界違反。"""

    path: str  # backend/src からの相対パス
    lineno: int
    imported: str  # import されたモジュール名
    rule: str  # 破られた契約の識別子
    reason: str  # 人間向けの説明


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _is_orb_path(rel: str) -> bool:
    """backend/src からの相対パスが ORB 側かを判定する。"""
    return (
        rel.startswith(_ORB_CORE_PREFIX) or rel.startswith(_ORB_BATCH_PREFIX) or rel == _ORB_ADAPTER
    )


def _is_orb_core_path(rel: str) -> bool:
    """``orb/`` パッケージ（コア）かを判定する。"""
    return rel.startswith(_ORB_CORE_PREFIX)


def _is_orb_module(module: str) -> bool:
    """import されたモジュール名が ORB 側かを判定する。"""
    return (
        module == f"{_PACKAGE}.orb"
        or module.startswith(f"{_PACKAGE}.orb.")
        or module.startswith(f"{_PACKAGE}.batch.orb_")
        or module == f"{_PACKAGE}.adapters.orb_quotes"
    )


def _under(module: str, prefix: str) -> bool:
    """``module`` が ``prefix`` そのものか、その配下かを判定する。

    素の ``str.startswith`` は ``backend.src.llm_foo`` のような**隣接名**にも
    マッチしてしまう。境界は「パッケージの配下か」で引きたいので、区切りの
    ``.`` まで見る。
    """
    return module == prefix or module.startswith(f"{prefix}.")


def _under_any(module: str, prefixes: Iterable[str]) -> bool:
    return any(_under(module, p) for p in prefixes)


def _named(candidates: tuple[str, ...], pred: Callable[[str], bool]) -> str:
    """報告に使う名前を選ぶ。

    禁止に触れた候補があればそれを出す（``backend.src.batch`` ではなく
    ``backend.src.batch.orb_poller`` と出た方が、どこを直せばよいか分かる）。
    """
    return next((c for c in candidates if pred(c)), candidates[-1])


def _allowlist_name(candidates: tuple[str, ...]) -> str:
    """契約 3 の報告名を選ぶ。

    許可リストに足すのは**モジュール名**なので、属性まで含んだ名前を出すと
    そのままコピーして貼れない。``from backend.src.rate_limiter import RateLimiter``
    なら ``backend.src.rate_limiter`` を出す。

    ただし ``from backend.src import rate_limiter`` のように基底が ``backend.src``
    そのものの場合は、基底では情報が無いので具体名の方を出す。
    """
    base = candidates[0]
    return candidates[-1] if base == _PACKAGE else base


def _imported_modules(tree: ast.AST) -> list[tuple[tuple[str, ...], int]]:
    """モジュール内の絶対 import を ``(候補モジュール名, 行番号)`` で列挙する。

    ``from a.b import c`` は ``c`` がサブモジュールか属性かを静的に決められない。
    そこで **``a.b`` と ``a.b.c`` の両方を候補**として返す。片方だけ見ると:

    - ``a.b`` だけ … ``from backend.src.batch import orb_poller`` が
      ``backend.src.batch`` に潰れ、**禁止 import を見逃す**（危険な方向）
    - ``a.b.c`` だけ … ``from backend.src.logging_config import get_logger`` が
      ``backend.src.logging_config.get_logger`` になり、許可リストと一致しない

    呼び出し側が「禁止判定は候補のどれかが該当したら違反」「許可判定は候補の
    どれかが許可されていれば OK」と**非対称**に使うことで両方を満たす。

    相対 import（``from . import x``）はパッケージ内参照であり、境界を跨がないため
    対象外にする。
    """
    found: list[tuple[tuple[str, ...], int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(((alias.name,), node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            base = node.module
            for alias in node.names:
                found.append(((base, f"{base}.{alias.name}"), node.lineno))
    return found


def _check_file(path: Path, root: Path) -> list[Violation]:
    rel = _rel(path, root)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except SyntaxError as exc:  # pragma: no cover - 構文エラーは pytest 本体が先に落ちる
        return [Violation(rel, exc.lineno or 0, "", "parse_error", f"構文解析に失敗: {exc}")]

    orb_side = _is_orb_path(rel)
    orb_core = _is_orb_core_path(rel)
    violations: list[Violation] = []

    for candidates, lineno in _imported_modules(tree):
        if not any(_under(c, _PACKAGE) for c in candidates):
            continue

        if not orb_side:
            # 契約 1: 夜型 → ORB は禁止
            # 🔴 候補の**どれか**が ORB なら違反。`from backend.src.batch import orb_poller`
            #    のような書き方を見逃さないため（見逃しは危険な方向）。
            if any(_is_orb_module(c) for c in candidates):
                violations.append(
                    Violation(
                        rel,
                        lineno,
                        _named(candidates, _is_orb_module),
                        "night_imports_orb",
                        "夜型スナイパーが ORB を import している。依存は ORB → 共有基盤の一方向でなければならない",
                    )
                )
            continue

        # ここから ORB 側のファイル
        if any(_is_orb_module(c) for c in candidates):
            continue  # ORB 内部の参照は自由

        # 契約 2: ORB → 夜型の判断系は禁止（禁止側なので候補のどれかで当てる）
        if any(_under_any(c, _FORBIDDEN_FOR_ORB) for c in candidates):
            violations.append(
                Violation(
                    rel,
                    lineno,
                    _named(candidates, lambda c: _under_any(c, _FORBIDDEN_FOR_ORB)),
                    "orb_imports_decision_layer",
                    "ORB が夜型の判断系を import している。ORB は LLM を使わない決定論ルールであることが設計の核",
                )
            )
            continue

        # 契約 4: orb/ コアは夜型の I/O 実装を直接持たない
        if orb_core and any(_under_any(c, _FORBIDDEN_FOR_ORB_CORE) for c in candidates):
            violations.append(
                Violation(
                    rel,
                    lineno,
                    _named(candidates, lambda c: _under_any(c, _FORBIDDEN_FOR_ORB_CORE)),
                    "orb_core_imports_io",
                    "orb/ コアが夜型の I/O 実装を import している。GMO は batch/orb_* と orb_quotes、"
                    "永続化は session_store / event_log / note_store に閉じる",
                )
            )
            continue

        # 契約 3: 共有面のゴールデン
        # 🔴 こちらは許可側なので候補の**どれかが許可されていれば OK**。
        #    `from backend.src import logging_config` を誤検知しないため。
        if not any(c in _ALLOWED_SHARED for c in candidates):
            violations.append(
                Violation(
                    rel,
                    lineno,
                    _allowlist_name(candidates),
                    "shared_surface_grew",
                    "ORB が新しい共有モジュールに依存した。意図的なら _ALLOWED_SHARED に理由付きで追加すること",
                )
            )

    return violations


def find_violations(roots: Iterable[Path]) -> list[Violation]:
    """``roots`` 配下の ``*.py`` を走査して境界違反を返す。

    存在しないパスは黙って読み飛ばす（CI の実行位置差で落とさないため。
    ``check_no_public_endpoints.py`` と同じ方針）。
    """
    violations: list[Violation] = []
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            violations.extend(_check_file(path, root))
    return violations


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv[1:]] or [_DEFAULT_ROOT]
    violations = find_violations(roots)
    if not violations:
        print(f"import 境界 OK: {', '.join(str(r) for r in roots)}")
        return 0

    print("🔴 import 境界違反を検出しました（#331）\n", file=sys.stderr)
    for v in violations:
        print(f"  {v.path}:{v.lineno}  {v.imported}", file=sys.stderr)
        print(f"      [{v.rule}] {v.reason}", file=sys.stderr)
    print(
        "\nこの境界は ORB をリポジトリ分離しない代わりに置いたものです（2026-08-28 判断・#331）。"
        "\n意図した変更なら scripts/check_import_boundary.py の許可リストを更新し、"
        "\nなぜ共有面を広げるのかを PR 本文に書いてください。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
