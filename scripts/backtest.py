"""バックテスト CLI（BT-3, 設計書 §2, §3）。

Usage:
    python scripts/backtest.py run --from 2024-01-06 --to 2024-03-29 --source rule
    python scripts/backtest.py run --from 2026-01-05 --to 2026-03-31 --source rule --unlock-oos
    python scripts/backtest.py report --run-id 20260703T101500123456-rule_based
    python scripts/backtest.py status
    python scripts/backtest.py prepare --from 2024-01-06 --to 2024-03-29
    python scripts/backtest.py check --run-id <run_id>
    python scripts/backtest.py run --run-id <run_id> --chunk-size 15

完全オフライン（キャッシュ必須）。キャッシュ構築は fxlab（`fxlab fetch`）または
`python -m backtest.data` で行う。
サブエージェント判断のオーケストレーションは /backtest コマンド
（.claude/commands/backtest.md）が prepare → decide → check → run を進める。
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import UTC, date, datetime, time
from pathlib import Path

import structlog

# プロジェクトルートを sys.path の先頭に強制する。本スクリプト（backtest.py）は
# パッケージ backtest/ と同名のため、scripts/ ディレクトリ（sys.path[0]）が先に
# 解決されると自分自身をパッケージとして import しようとして落ちる。
# PYTHONPATH でルートが既に「後方に」入っている環境（CI）でも先頭を保証する。
_ROOT = Path(__file__).resolve().parent.parent
_ROOT_STR = str(_ROOT)
if _ROOT_STR in sys.path:
    sys.path.remove(_ROOT_STR)
sys.path.insert(0, _ROOT_STR)

from backtest.data import KlinesStore  # noqa: E402
from backtest.engine import (  # noqa: E402
    MORNING_SESSION,
    SESSION_PRESETS,
    SessionWindow,
    SimConfig,
)
from backtest.report import (  # noqa: E402
    append_experiment,
    compute_metrics,
    reserve_run_dir,
    save_run,
)
from backtest.runner import OosLockedError, RunConfig, run_backtest  # noqa: E402
from backtest.sources import (  # noqa: E402
    DecisionSource,
    MockSource,
    NightDayMomentumSource,
    NightPTrendSource,
    RuleBasedSource,
    uses_decision_sizing_params,
)
from backtest.subagent import (  # noqa: E402
    SubagentFileSource,
    check_run,
    load_run_meta,
    prepare_run,
)

_DEFAULT_CACHE = _ROOT / "backtest" / "cache"
_DEFAULT_RUNS = _ROOT / "backtest" / "runs"
_DEFAULT_REGISTRY = _ROOT / "backtest" / "experiments.jsonl"

_SOURCES: dict[str, type] = {
    "rule": RuleBasedSource,
    "mock": MockSource,
    "night_p_trend": NightPTrendSource,
    "night_day_momentum": NightDayMomentumSource,
}

_log = structlog.get_logger(__name__)


def _parse_hhmm(value: str) -> time:
    """--decision-time 等の HH:MM 引数パーサ（不正値は argparse エラーに変換）。"""
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"HH:MM 形式で指定してください: {value!r}") from exc


def _resolve_session(args: argparse.Namespace) -> SessionWindow:
    """--session-preset を基底に、個別の時刻フラグで上書きした SessionWindow を返す。

    SessionWindow の構築時検証（決定 06:00 以降・closeout 翌 05:55 以前）は
    ValueError のまま伝播させる（不正な窓で run を作らない）。
    """
    base = SESSION_PRESETS[args.session_preset]
    return SessionWindow(
        decision_time=args.decision_time or base.decision_time,
        closeout_time=args.closeout_time or base.closeout_time,
        friday_closeout_time=args.friday_closeout_time or base.friday_closeout_time,
    )


def _session_flags_given(args: argparse.Namespace) -> bool:
    """セッション窓を明示指定したか（--run-id との併用拒否の判定に使う）。"""
    return bool(
        args.session_preset != "night"
        or args.decision_time
        or args.closeout_time
        or args.friday_closeout_time
    )


def _add_session_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--session-preset",
        choices=sorted(SESSION_PRESETS),
        default="night",
        help="セッション窓プリセット（night=現行本番 20:30/翌4:30/土4:30（バックテスト既定）、"
        "morning=朝型ゴールデン互換 8:00/翌0:00/土5:00。RD-1 #166 / RD-6 #171）",
    )
    parser.add_argument(
        "--decision-time",
        type=_parse_hhmm,
        default=None,
        help="判断時刻 HH:MM JST（プリセットの個別上書き・06:00 以降）",
    )
    parser.add_argument(
        "--closeout-time",
        type=_parse_hhmm,
        default=None,
        help="月〜木の強制手仕舞い時刻 HH:MM JST（翌日・05:55 以前）",
    )
    parser.add_argument(
        "--friday-closeout-time",
        type=_parse_hhmm,
        default=None,
        help="金曜建玉の強制手仕舞い時刻 HH:MM JST（土曜・05:55 以前）",
    )


def _build_sim_config(args: argparse.Namespace, *, source_name: str | None = None) -> SimConfig:
    """CLI 引数から SimConfig を構築する（--run-id / --from・--to 両パスで共有）。

    資金を扱うシミュレーション設定の食い違いを防ぐため、両分岐で同一のヘルパーを使う
    （フィールド追加時の片側更新漏れ防止・CodeRabbit 指摘 PR #174）。
  --use-decision-sizing 未指定時は source 種別で自動（凍結ベースライン=False / LLM=True）。
    """
    use_decision = args.use_decision_sizing
    if use_decision is None:
        use_decision = uses_decision_sizing_params(source_name) if source_name else False
    return SimConfig(
        fill_mode=args.fill_mode,
        slippage_pips=args.slippage,
        use_decision_sizing_params=use_decision,
    )


def _audit_refusal(reason: str, **fields: object) -> None:
    """拒否パスの構造化監査ログ（§5.2。stderr の人間向け表示とは別に残す）。"""
    _log.warning(
        "backtest_cli_refused",
        reason=reason,
        **fields,
        timestamp_ms=int(_time.time() * 1000),
        llm_tokens=None,
    )


def _cmd_run(args: argparse.Namespace) -> int:
    store = KlinesStore(cache_dir=Path(args.cache_dir))

    if args.run_id:
        # サブエージェント判断 run（BT-4）: prepare 済みディレクトリの decisions を消費
        run_dir = Path(args.runs_dir) / args.run_id
        if not run_dir.exists():
            print(f"run が見つかりません: {run_dir}", file=sys.stderr)
            return 1
        check = check_run(run_dir)
        if not check.ok:
            _audit_refusal(
                "decide_incomplete",
                run_id=args.run_id,
                pending=len(check.pending),
                unparsable=check.unparsable,
                order_violations=check.order_violations,
            )
            print(
                f"REFUSED: decide フェーズ未完了 pending={len(check.pending)} "
                f"unparsable={check.unparsable} order_violations={check.order_violations}。"
                "check サブコマンドで詳細を確認し decide を再開してください。",
                file=sys.stderr,
            )
            return 3
        meta = load_run_meta(run_dir)
        # セッション窓は prepare 時の記録が唯一の正（判断コンテキストが窓時刻で構築済み）。
        # CLI からの上書きは判断と約定の窓不一致を生むため拒否する（fail-closed）
        if _session_flags_given(args):
            _audit_refusal("session_flags_with_run_id", run_id=args.run_id)
            print(
                "REFUSED: --run-id 指定時のセッション窓は prepare 時の run_meta.json が"
                "唯一の正です（--session-preset / --decision-time 等は指定できません）",
                file=sys.stderr,
            )
            return 2
        meta_session = meta.get("session")
        session = (
            SessionWindow.from_dict(meta_session)  # type: ignore[arg-type]
            if isinstance(meta_session, dict)
            else MORNING_SESSION  # RD-1 以前の prepare 済み run（朝型固定時代）の後方互換
        )
        source = SubagentFileSource(run_dir)
        config = RunConfig(
            start=date.fromisoformat(str(meta["start"])),
            end=date.fromisoformat(str(meta["end"])),
            equity_jpy=float(meta["equity_jpy"]),  # type: ignore[arg-type]
            sim=_build_sim_config(args, source_name="subagent"),
            session=session,
            unlock_oos=args.unlock_oos,
        )
        return _execute_run(
            args,
            store,
            source,
            config,
            run_id=str(meta["run_id"]),
            run_dir=run_dir,
            model=str(meta["model"]),
            extra_meta={
                "評価対象モデル": meta["model"],
                "chunk_size": args.chunk_size if args.chunk_size is not None else "未記録",
                "ブラインドモード": meta["blind"],
            },
        )

    if not (args.start and args.end):
        print("--run-id なしの run には --from / --to が必要です", file=sys.stderr)
        return 2
    source = _SOURCES[args.source]()
    try:
        session = _resolve_session(args)
    except ValueError as exc:
        _audit_refusal("invalid_session_window", error=str(exc))
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    config = RunConfig(
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        sim=_build_sim_config(args, source_name=source.name),
        session=session,
        unlock_oos=args.unlock_oos,
    )

    run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}-{source.name}"
    # 台帳（experiments.jsonl）追記より前に run ディレクトリを原子的に「予約」する。
    # 追記後の失敗では実体のない孤立エントリが台帳に残るため（§5.2）、mkdir の
    # 原子性で check-then-act の隙間なく衝突を排除する
    try:
        run_dir = reserve_run_dir(Path(args.runs_dir), run_id)
    except FileExistsError as exc:
        _audit_refusal("run_id_collision", run_id=run_id)
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return _execute_run(args, store, source, config, run_id=run_id, run_dir=run_dir)


def _execute_run(
    args: argparse.Namespace,
    store: KlinesStore,
    source: DecisionSource,
    config: RunConfig,
    run_id: str,
    run_dir: Path,
    model: str | None = None,
    extra_meta: dict[str, object] | None = None,
) -> int:
    if (run_dir / "report.md").exists():
        # 監査成果物の上書き禁止。再評価は新しい run として実施する（§7-6）
        _audit_refusal("report_exists", run_id=run_id)
        print(f"REFUSED: {run_dir} には既にレポートが存在します", file=sys.stderr)
        return 2

    try:
        outcome = run_backtest(store, source, config)
    except OosLockedError as exc:
        _audit_refusal(
            "oos_locked",
            run_id=run_id,
            start=config.start.isoformat(),
            end=config.end.isoformat(),
        )
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    target = compute_metrics(outcome.results)

    # 3 列比較の必須ベースライン（§7-3）: 評価対象が rule 以外なら同一条件で再計算
    if source.name == "rule_based":
        baseline_rule = target
    else:
        baseline_outcome = run_backtest(store, RuleBasedSource(), config)
        baseline_rule = compute_metrics(baseline_outcome.results)

    registry = append_experiment(
        Path(args.registry), outcome, run_id, target, model=model, chunk_size=args.chunk_size
    )
    save_run(
        Path(args.runs_dir),
        run_id,
        outcome,
        target,
        baseline_rule,
        registry,
        model=model,
        run_dir=run_dir,
        extra_meta=extra_meta,
    )

    print((run_dir / "report.md").read_text(encoding="utf-8"))
    print(f"saved: {run_dir}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    path = Path(args.runs_dir) / args.run_id / "report.md"
    if not path.exists():
        print(f"run が見つかりません: {path}", file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8"))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    import json

    registry_path = Path(args.registry)
    if not registry_path.exists():
        print("実験レジストリはまだ空です（run 未実行）")
        return 0
    entries = [
        json.loads(line)
        for line in registry_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    oos_runs = sum(1 for e in entries if e.get("is_oos"))
    print(f"総 run 数: {len(entries)} / OOS 実行: {oos_runs} 回")
    for e in entries[-10:]:
        m = e.get("metrics", {})
        print(
            f"  {e['run_id']}  {e['start']}..{e['end']}  src={e['source']}"
            f"  trades={m.get('trades')}  expR={m.get('expectancy_r')}"
            f"{'  [OOS]' if e.get('is_oos') else ''}"
        )
    return 0


def _cmd_prepare(args: argparse.Namespace) -> int:
    store = KlinesStore(cache_dir=Path(args.cache_dir))
    try:
        session = _resolve_session(args)
    except ValueError as exc:
        _audit_refusal("invalid_session_window", error=str(exc))
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    try:
        result = prepare_run(
            store,
            Path(args.runs_dir),
            start=date.fromisoformat(args.start),
            end=date.fromisoformat(args.end),
            blind=not args.no_blind,
            session=session,
            unlock_oos=args.unlock_oos,
        )
    except OosLockedError as exc:
        _audit_refusal("oos_locked_prepare", start=args.start, end=args.end)
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(
        f"run_id: {result.run_id}\n"
        f"requests: {result.prepared} 件 / スキップ: {len(result.skipped_days)} 日\n"
        f"dir: {result.run_dir}"
    )
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    run_dir = Path(args.runs_dir) / args.run_id
    if not run_dir.exists():
        print(f"run が見つかりません: {run_dir}", file=sys.stderr)
        return 1
    check = check_run(run_dir)
    print(
        f"total: {check.total} / pending: {len(check.pending)} / "
        f"unparsable: {len(check.unparsable)} / order_violations: {len(check.order_violations)}"
    )
    if check.pending:
        print("pending: " + " ".join(check.pending))
    if check.unparsable:
        print("unparsable: " + " ".join(check.unparsable))
    if check.order_violations:
        print("order_violations: " + " ".join(check.order_violations))
    return 0 if check.ok else 3


def main() -> int:
    parser = argparse.ArgumentParser(description="ローカルバックテスト CLI（設計書 §2）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="バックテストを実行しレポートを生成する")
    p_run.add_argument(
        "--from", dest="start", default=None, help="開始日 YYYY-MM-DD（--run-id 指定時は不要）"
    )
    p_run.add_argument(
        "--to", dest="end", default=None, help="終了日 YYYY-MM-DD（--run-id 指定時は不要）"
    )
    p_run.add_argument("--source", choices=sorted(_SOURCES), default="rule")
    p_run.add_argument(
        "--use-decision-sizing",
        dest="use_decision_sizing",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="SL/TP 倍率を decision から取得（LLM/BT-5・本番同挙動）。"
        "未指定時は source 種別で自動（凍結ベースライン=frozen / mock・subagent=decision）",
    )
    p_run.add_argument("--fill-mode", choices=["touch", "strict"], default="touch")
    p_run.add_argument("--slippage", type=float, default=0.0, help="感度分析専用（pips）")
    p_run.add_argument(
        "--unlock-oos",
        action="store_true",
        help="OOS（2026-01-01〜）実行の明示アンロック（§7-1。実行はレジストリに記録される）",
    )
    p_run.add_argument("--cache-dir", default=str(_DEFAULT_CACHE))
    p_run.add_argument("--runs-dir", default=str(_DEFAULT_RUNS))
    p_run.add_argument("--registry", default=str(_DEFAULT_REGISTRY))
    p_run.add_argument(
        "--run-id",
        default=None,
        help="prepare 済み run の decisions（サブエージェント判断）でシミュレーションする",
    )
    p_run.add_argument(
        "--chunk-size",
        type=int,
        default=None,
        help="decide フェーズのチャンクサイズ（記録用・設計書 §4.2）",
    )
    _add_session_args(p_run)
    p_run.set_defaults(func=_cmd_run)

    p_report = sub.add_parser("report", help="保存済み run のレポートを表示する")
    p_report.add_argument("--run-id", required=True)
    p_report.add_argument("--runs-dir", default=str(_DEFAULT_RUNS))
    p_report.set_defaults(func=_cmd_report)

    p_status = sub.add_parser("status", help="実験レジストリの要約を表示する")
    p_status.add_argument("--registry", default=str(_DEFAULT_REGISTRY))
    p_status.set_defaults(func=_cmd_status)

    p_prepare = sub.add_parser(
        "prepare", help="サブエージェント判断リクエスト一式を生成する（phase 1）"
    )
    p_prepare.add_argument("--from", dest="start", required=True, help="開始日 YYYY-MM-DD")
    p_prepare.add_argument("--to", dest="end", required=True, help="終了日 YYYY-MM-DD")
    p_prepare.add_argument(
        "--no-blind",
        action="store_true",
        help="ブラインドモード（連番ファイル名・既定）を無効化し日付ファイル名にする",
    )
    p_prepare.add_argument("--unlock-oos", action="store_true")
    p_prepare.add_argument("--cache-dir", default=str(_DEFAULT_CACHE))
    p_prepare.add_argument("--runs-dir", default=str(_DEFAULT_RUNS))
    _add_session_args(p_prepare)
    p_prepare.set_defaults(func=_cmd_prepare)

    p_check = sub.add_parser("check", help="decide フェーズの完了・順序を検査する")
    p_check.add_argument("--run-id", required=True)
    p_check.add_argument("--runs-dir", default=str(_DEFAULT_RUNS))
    p_check.set_defaults(func=_cmd_check)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
