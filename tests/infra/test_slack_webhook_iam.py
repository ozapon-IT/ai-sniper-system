"""send_slack_alert を呼ぶ Lambda は slack-webhook-url を IAM で読めること（#350）。

🔴 2026-09-04、closeout-batch が Step3 失敗などの強アラートを送ろうとして
``slack_alert_webhook_unavailable``（AccessDenied）で落ちた。シークレット自体は
存在するが、金を触るバッチの実行ロールに ``GetSecretValue`` が無かった。
IAM は当初から欠落しており、``send_slack_alert()`` が初めて呼ばれた日に発覚した。

代償統制（CloudWatch ``{ $.level = "error" }`` → SNS → slack-notifier）は効くが、
届くのは汎用アラーム文面だけで建玉 ID が載らない。trading-safety.md が要求する
「Step3 失敗 = Slack 強アラート（建玉ID 明記）」を満たさない。

このテストは terraform 定義を検査する（state は見ない）。新しい Lambda が
``send_slack_alert`` を呼び始めたとき、IAM を付け忘れると同じ穴が開く。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_LAMBDA_TF = _ROOT / "infra" / "terraform" / "modules" / "serverless_app" / "lambda.tf"
_PKG = "backend.src"
_PKG_ROOT = _ROOT / "backend" / "src"

_SLACK_SECRET_VAR = "var.slack_webhook_secret_name"

# webhook 取得そのもの。ラッパー名は列挙しない（新しいヘルパを足しても辿れること）。
_SLACK_LEAVES = frozenset({"send_slack_alert", "get_slack_webhook_url"})

_FUNC_RE = re.compile(r"^    (\w+) = \{$(.*?)^    \}$", re.M | re.S)


@dataclass(frozen=True)
class LambdaFunction:
    key: str
    handler: str
    secret_names: tuple[str, ...]

    @property
    def handler_path(self) -> Path:
        """``backend.src.foo.bar.lambda_handler`` → ``backend/src/foo/bar.py``."""
        parts = self.handler.split(".")
        assert parts[-1] == "lambda_handler", (
            f"handler が lambda_handler で終わらない: {self.handler}"
        )
        rel = "/".join(parts[:-1]) + ".py"
        return _ROOT / rel

    @property
    def grants_slack_webhook(self) -> bool:
        return _SLACK_SECRET_VAR in self.secret_names


@dataclass(frozen=True)
class _Binding:
    """ローカル名 → import 先。``attr is None`` ならモジュール別名。"""

    module: str
    attr: str | None


@dataclass
class _Parsed:
    tree: ast.AST
    bindings: dict[str, _Binding]
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]


_PARSE_CACHE: dict[Path, _Parsed] = {}


def _parse_functions(text: str) -> dict[str, LambdaFunction]:
    """``local.functions`` の各ブロックから handler と secret_names を拾う。"""
    found: dict[str, LambdaFunction] = {}
    for key, body in _FUNC_RE.findall(text):
        handler_m = re.search(r'handler\s*=\s*"([^"]+)"', body)
        secrets_m = re.search(r"secret_names\s*=\s*\[(.*?)\]", body, re.S)
        assert handler_m, f"{key}: handler が無い"
        assert secrets_m, f"{key}: secret_names が無い"
        secrets = tuple(tok.strip() for tok in secrets_m.group(1).split(",") if tok.strip())
        found[key] = LambdaFunction(
            key=key,
            handler=handler_m.group(1),
            secret_names=secrets,
        )
    return found


def _module_file(module: str) -> Path | None:
    if module != _PKG and not module.startswith(_PKG + "."):
        return None
    rest = module[len(_PKG) :].lstrip(".")
    if rest:
        py = _PKG_ROOT / (rest.replace(".", "/") + ".py")
        if py.is_file():
            return py
        init = _PKG_ROOT / rest.replace(".", "/") / "__init__.py"
    else:
        init = _PKG_ROOT / "__init__.py"
    return init if init.is_file() else None


def _function_defs(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = node
    return found


def _bindings(tree: ast.AST) -> dict[str, _Binding]:
    """``backend.src`` への import だけを拾う。"""
    out: dict[str, _Binding] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name != _PKG and not alias.name.startswith(_PKG + "."):
                    continue
                if alias.asname is None:
                    continue
                out[alias.asname] = _Binding(alias.name, None)
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if node.module != _PKG and not node.module.startswith(_PKG + "."):
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                qualified = f"{node.module}.{alias.name}"
                if _module_file(qualified) is not None:
                    out[local] = _Binding(qualified, None)
                else:
                    out[local] = _Binding(node.module, alias.name)
    return out


def _parse_source(source: str) -> _Parsed:
    tree = ast.parse(source)
    return _Parsed(tree=tree, bindings=_bindings(tree), functions=_function_defs(tree))


def _parse_path(path: Path) -> _Parsed:
    cached = _PARSE_CACHE.get(path)
    if cached is not None:
        return cached
    parsed = _parse_source(path.read_text(encoding="utf-8"))
    _PARSE_CACHE[path] = parsed
    return parsed


def _resolve_call(
    func: ast.expr, bindings: dict[str, _Binding]
) -> str | tuple[str, str, str | None] | None:
    """``"leaf"`` / ``("local", name, None)`` / ``("remote", module, func)``。"""
    if isinstance(func, ast.Name):
        if func.id in _SLACK_LEAVES:
            return "leaf"
        binding = bindings.get(func.id)
        if binding is not None and binding.attr is not None:
            return ("remote", binding.module, binding.attr)
        return ("local", func.id, None)
    if isinstance(func, ast.Attribute):
        if func.attr in _SLACK_LEAVES:
            return "leaf"
        if isinstance(func.value, ast.Name):
            binding = bindings.get(func.value.id)
            if binding is not None and binding.attr is None:
                return ("remote", binding.module, func.attr)
    return None


def _reach(
    parsed: _Parsed,
    *,
    module_id: str,
    func_name: str | None,
    visited: set[tuple[str, str]],
) -> bool:
    key = (module_id, func_name or "*")
    if key in visited:
        return False
    visited.add(key)

    if func_name is None:
        calls = [n for n in ast.walk(parsed.tree) if isinstance(n, ast.Call)]
    else:
        fn = parsed.functions.get(func_name)
        if fn is None:
            return False
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]

    for call in calls:
        target = _resolve_call(call.func, parsed.bindings)
        if target == "leaf":
            return True
        if not isinstance(target, tuple):
            continue
        kind, module_or_name, attr = target
        if kind == "local":
            if _reach(parsed, module_id=module_id, func_name=module_or_name, visited=visited):
                return True
        elif kind == "remote":
            assert attr is not None
            path = _module_file(module_or_name)
            if path is None:
                continue
            remote = _parse_path(path)
            if _reach(remote, module_id=str(path), func_name=attr, visited=visited):
                return True
    return False


def handler_reaches_slack_webhook(source: str) -> bool:
    """ソースが Slack webhook 取得に到達するか。

    import の有無ではなく **呼び出しグラフ** を ``backend.src`` 配下へ辿る。
    素の import 追跡だと read_api → s3_logger で誤検知するため、呼ばれた関数だけを見る。
    ラッパー名の allowlist は持たない（新しいヘルパを列挙し忘れても検出する）。
    """
    parsed = _parse_source(source)
    return _reach(parsed, module_id="<snippet>", func_name=None, visited=set())


def _load_functions() -> dict[str, LambdaFunction]:
    assert _LAMBDA_TF.exists(), f"lambda.tf が無い: {_LAMBDA_TF}"
    return _parse_functions(_LAMBDA_TF.read_text(encoding="utf-8"))


def _handler_reaches(fn: LambdaFunction) -> bool:
    src = fn.handler_path.read_text(encoding="utf-8")
    parsed = _parse_source(src)
    return _reach(parsed, module_id=str(fn.handler_path), func_name=None, visited=set())


# ---------------------------------------------------------------------------
# 検出器そのもの（誤検知・見逃しで IAM テストが空洞化しないこと）
# ---------------------------------------------------------------------------


def test_detector_flags_direct_send_slack_alert() -> None:
    assert handler_reaches_slack_webhook("from x import send_slack_alert\nsend_slack_alert('hi')\n")


def test_detector_ignores_unused_import() -> None:
    """import しただけで呼んでいなければ対象外（import グラフ追跡との違い）。"""
    src = "from backend.src.alerting.slack_alert import send_slack_alert\n"
    assert not handler_reaches_slack_webhook(src)


def test_detector_flags_s3_write_not_read() -> None:
    """AGENTS.md #9: 書き込み失敗が Slack に行く。読み取りだけの import は対象外。"""
    write_src = (
        "from backend.src.persistence import s3_logger\ns3_logger.save_decision_log(p, today=t)\n"
    )
    read_src = "from backend.src.persistence import s3_logger\ns3_logger.load_dashboard()\n"
    assert handler_reaches_slack_webhook(write_src)
    assert not handler_reaches_slack_webhook(read_src)


def test_detector_follows_wrapper_without_allowlist() -> None:
    """新しいラッパーを葉に足さなくても、実体（send_slack_alert）まで辿ること。"""
    src = (
        "from backend.src.batch.lock_acquire import notify_lock_unavailable\n"
        "notify_lock_unavailable(log_event='x', log_extra={}, slack_message='m')\n"
    )
    assert "send_slack_alert" not in src
    assert handler_reaches_slack_webhook(src)


# ---------------------------------------------------------------------------
# 本番の契約: callers ⊆ IAM 許可
# ---------------------------------------------------------------------------


def test_parser_finds_all_serverless_app_functions() -> None:
    """関数が増えたときに検査漏れしないよう、キー集合を固定する。"""
    keys = set(_load_functions())
    assert keys == {
        "decision_batch",
        "closeout_batch",
        "read_api",
        "kill_switch",
        "slack_notifier",
        "closeout_watchdog",
    }


def test_known_callers_are_still_detected() -> None:
    """検出器が壊れ、closeout/decision を見逃すと #350 が再発してもテストが緑になる。"""
    callers = {k for k, fn in _load_functions().items() if _handler_reaches(fn)}
    missing = {"decision_batch", "closeout_batch", "closeout_watchdog", "kill_switch"} - callers
    assert not missing, (
        f"検出器が既知の Slack 呼び出し元を見逃している: {sorted(missing)}。"
        "backend.src の呼び出しグラフを辿れていない"
    )


def test_send_slack_alert_callers_have_webhook_iam() -> None:
    """🔴 send_slack_alert に到達する Lambda ⊆ slack-webhook-url の GetSecretValue。

    修正前（closeout/decision の secret_names に webhook が無い状態）ではここが落ちる。
    """
    missing: list[str] = []
    for key, fn in _load_functions().items():
        if _handler_reaches(fn) and not fn.grants_slack_webhook:
            missing.append(key)
    assert not missing, (
        "send_slack_alert を呼ぶ Lambda に slack-webhook-url の IAM が無い（#350）: "
        f"{missing}。infra/terraform/modules/serverless_app/lambda.tf の secret_names に "
        "var.slack_webhook_secret_name を追加すること"
    )


def test_read_api_does_not_get_slack_webhook() -> None:
    """最小権限（AGENTS.md #8）。read_api は Slack を送らず、シークレットも読まない。"""
    fn = _load_functions()["read_api"]
    assert not _handler_reaches(fn)
    assert not fn.grants_slack_webhook
    assert fn.secret_names == ()


def test_iam_tf_still_uses_exact_secret_arns() -> None:
    """wildcard ``secret:your-app/slack-webhook-url-*`` に戻さないこと（iam.tf の方針）。"""
    iam = (_ROOT / "infra" / "terraform" / "modules" / "serverless_app" / "iam.tf").read_text(
        encoding="utf-8"
    )
    assert "local.secret_arns[n]" in iam
    assert "slack-webhook-url-*" not in iam
