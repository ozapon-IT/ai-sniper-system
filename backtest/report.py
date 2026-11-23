"""指標算出・レポート生成・実験レジストリ（BT-3, 設計書 §7, §8）。

カーブフィッティング対策をコードで強制する:
  - 全レポートに NO_TRADE / rule_based / 評価対象 の 3 列比較を必須掲載（§7-3）
  - トレード数 < 30 は「参考値」フラグ（§7-5）
  - 全 run を experiments.jsonl に追記し、同一ウィンドウ試行回数と OOS 実行回数を
    常時表示（§7-1, §7-2。多重検定の自覚を強制する）
  - 学習知識バイアスの免責ヘッダを全レポートに自動挿入（§6.3）
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time as _time
from dataclasses import asdict, dataclass
from pathlib import Path

import structlog
from fxlab.registry import ExperimentRegistry, git_sha
from fxlab.registry import config_hash as _hash

from backtest.engine import TradeResult
from backtest.runner import RunOutcome

_log = structlog.get_logger(__name__)

MIN_SAMPLE_TRADES = 30  # これ未満の統計は「参考値」（設計書 §7-5）

_CLOSED_STATUSES = ("tp", "sl", "closeout")

DISCLAIMER = (
    "> **免責（設計書 §6.3 / §7）**: 本レポートは相対比較（戦略 A vs B・変更前後）と"
    "メカニクス検証のための参考値である。LLM 判断を含む場合、モデルの学習データに"
    "対象期間の相場の「結果」が含まれるため（学習知識バイアス）、絶対成績としての"
    "解釈は行わないこと。真の評価は Phase 1 実記録の前向きリプレイ（BT-5）で行う。"
)


@dataclass(frozen=True)
class Metrics:
    """クローズ済みトレードの統計（R 倍数・pips 正規化）。"""

    days: int  # シミュレーション対象日数（結果レコード数）
    trades: int  # クローズ済み（tp/sl/closeout）
    wins: int
    losses: int
    win_rate: float  # trades=0 なら 0.0
    profit_factor: float  # 損失ゼロ時は inf ではなく 0.0（NO_TRADE と区別は trades で）
    expectancy_r: float  # 平均 R 倍数
    max_drawdown_r: float  # 累積 R の最大ドローダウン（正の値）
    total_pnl_pips: float
    status_counts: dict[str, int]
    low_sample: bool  # trades < MIN_SAMPLE_TRADES


NO_TRADE_METRICS = Metrics(
    days=0,
    trades=0,
    wins=0,
    losses=0,
    win_rate=0.0,
    profit_factor=0.0,
    expectancy_r=0.0,
    max_drawdown_r=0.0,
    total_pnl_pips=0.0,
    status_counts={},
    low_sample=True,
)


def compute_metrics(results: list[TradeResult]) -> Metrics:
    """TradeResult 列から統計を算出する。"""
    status_counts: dict[str, int] = {}
    for r in results:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1

    closed = [r for r in results if r.status in _CLOSED_STATUSES]
    pnls = [r.pnl_pips for r in closed if r.pnl_pips is not None]
    rs = [r.r_multiple for r in closed if r.r_multiple is not None]
    if len(pnls) != len(closed) or len(rs) != len(closed):
        # クローズ済みは pnl/r を持つ契約（engine._closed）。破れをバグとして可視化する
        _log.warning(
            "backtest_metrics_missing_pnl",
            closed=len(closed),
            with_pnl=len(pnls),
            with_r=len(rs),
            timestamp_ms=int(_time.time() * 1000),
            llm_tokens=None,
        )

    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)

    # 累積 R の最大ドローダウン
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for r_val in rs:
        cum += r_val
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return Metrics(
        days=len(results),
        trades=len(closed),
        wins=wins,
        losses=losses,
        win_rate=wins / len(closed) if closed else 0.0,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else 0.0,
        expectancy_r=sum(rs) / len(rs) if rs else 0.0,
        max_drawdown_r=max_dd,
        total_pnl_pips=sum(pnls),
        status_counts=status_counts,
        low_sample=len(closed) < MIN_SAMPLE_TRADES,
    )


# ---------------------------------------------------------------------------
# 実験レジストリ（experiments.jsonl）
# ---------------------------------------------------------------------------


def config_hash(outcome: RunOutcome) -> str:
    """run 設定の同一性判定用ハッシュ（12 桁）。プロンプト変更も 1 実験（§7-6）。"""
    cfg = outcome.config
    payload = {
        "source": outcome.source_name,
        "start": cfg.start.isoformat(),
        "end": cfg.end.isoformat(),
        "equity_jpy": cfg.equity_jpy,
        "fill_mode": cfg.sim.fill_mode,
        "slippage_pips": cfg.sim.slippage_pips,
        "available_ratio": cfg.sim.available_ratio,
        # セッション窓（RD-1 #166）: 窓が違う run を同一設定として扱わない
        "session": cfg.session.as_dict(),
        "use_decision_sizing_params": cfg.sim.use_decision_sizing_params,
        "hold_hours": cfg.sim.hold_hours,
        "session_vol_atr_mult": cfg.sim.session_vol_atr_mult,
        "session_vol_rr_target": cfg.sim.session_vol_rr_target,
        # 本番モジュールが環境変数から読む値・判断プロンプト（§7-6）
        "env": outcome.env,
        "prompt_sha": outcome.prompt_sha,
    }
    return _hash(payload)


@dataclass(frozen=True)
class RegistryStats:
    """レジストリ集計（レポートに常時表示・§7-1/7-2）。"""

    window_trials: int  # 同一 (start, end) への通算試行回数（今回を含む）
    oos_runs: int  # OOS 実行の通算回数（今回を含む）


def append_experiment(
    registry_path: Path,
    outcome: RunOutcome,
    run_id: str,
    metrics: Metrics,
    model: str | None = None,
    chunk_size: int | None = None,
) -> RegistryStats:
    """experiments.jsonl へ 1 run を追記し、試行回数の集計を返す。"""
    cfg = outcome.config
    # 追記と集計は fxlab が排他ロック下で行う。試行回数カウントは規律の安全装置（§7-1/7-2）で、
    # 並行実行の TOCTOU で過小カウントされると規律自体が無力化されるため
    stats = ExperimentRegistry(registry_path, sealed_key="is_oos").append(
        {
            "run_id": run_id,
            "git_sha": git_sha(),
            "config_hash": config_hash(outcome),
            "source": outcome.source_name,
            "model": model,
            "chunk_size": chunk_size,  # サブエージェント判断時の文脈チャンク（設計書 §4.2）
            "llm_tokens": outcome.total_llm_tokens,
            "start": cfg.start.isoformat(),
            "end": cfg.end.isoformat(),
            "session": cfg.session.as_dict(),  # セッション窓（RD-1 #166）
            "env": outcome.env,
            "prompt_sha": outcome.prompt_sha,
            "is_oos": outcome.is_oos,
            "metrics": {
                "days": metrics.days,
                "trades": metrics.trades,
                "win_rate": round(metrics.win_rate, 4),
                "profit_factor": round(metrics.profit_factor, 4),
                "expectancy_r": round(metrics.expectancy_r, 4),
                "max_drawdown_r": round(metrics.max_drawdown_r, 4),
                "total_pnl_pips": round(metrics.total_pnl_pips, 1),
            },
        }
    )
    window_trials, oos_runs = stats.window_trials, stats.sealed_runs

    _log.info(
        "backtest_experiment_recorded",
        run_id=run_id,
        window_trials=window_trials,
        oos_runs=oos_runs,
        timestamp_ms=int(_time.time() * 1000),
        llm_tokens=outcome.total_llm_tokens,
    )
    return RegistryStats(window_trials=window_trials, oos_runs=oos_runs)


# ---------------------------------------------------------------------------
# レポート生成
# ---------------------------------------------------------------------------


def _fmt(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def render_report(
    run_id: str,
    outcome: RunOutcome,
    target: Metrics,
    baseline_rule: Metrics,
    registry: RegistryStats,
    model: str | None = None,
    extra_meta: dict[str, object] | None = None,
) -> str:
    """report.md を生成する。3 列比較（NO_TRADE / rule_based / 評価対象）を強制。"""
    cfg = outcome.config
    low_sample_note = (
        f"\n> ⚠️ **参考値**: クローズ済みトレード {target.trades} 件 < {MIN_SAMPLE_TRADES} 件。"
        "統計的な結論を出さないこと（設計書 §7-5）。"
        if target.low_sample
        else ""
    )
    oos_note = "**OOS（ホールドアウト）実行**" if outcome.is_oos else "In-Sample 実行"

    def _row(name: str, f: str) -> str:
        no_trade = getattr(NO_TRADE_METRICS, f)
        rule = getattr(baseline_rule, f)
        tgt = getattr(target, f)
        if f == "days":
            no_trade = tgt  # NO_TRADE も同一ウィンドウの評価（0 日と誤読させない）
        if isinstance(no_trade, float):
            return f"| {name} | {_fmt(no_trade)} | {_fmt(rule)} | {_fmt(tgt)} |"
        return f"| {name} | {no_trade} | {rule} | {tgt} |"

    status_lines = "\n".join(
        f"  - {status}: {count}" for status, count in sorted(target.status_counts.items())
    )
    extra_lines = "\n".join(f"- {k}: {v}" for k, v in (extra_meta or {}).items())
    if cfg.sim.use_decision_sizing_params:
        sizing_detail = "（SL/TP は decision.atr_multiplier / decision.rr_target・本番同挙動）"
    else:
        sizing_detail = (
            f"（SL={cfg.sim.session_vol_atr_mult}×ATR_h[hold={cfg.sim.hold_hours}h]"
            f"・TP={cfg.sim.session_vol_rr_target}×SL幅）"
        )
    env_set = {k: v for k, v in outcome.env.items() if v is not None}
    env_line = ", ".join(f"{k}={v}" for k, v in sorted(env_set.items())) or "（すべて既定）"

    return f"""# バックテストレポート: {run_id}

{DISCLAIMER}
{low_sample_note}

## 実行条件

- 期間: {cfg.start.isoformat()} 〜 {cfg.end.isoformat()}（{oos_note}）
- 評価対象ソース: **{outcome.source_name}**（model: {model or "-"}）
- セッション窓 (JST): 判断 {cfg.session.decision_time.isoformat("minutes")} / closeout 翌 \
{cfg.session.closeout_time.isoformat("minutes")}（金曜: 翌 \
{cfg.session.friday_closeout_time.isoformat("minutes")}）
- 倍率ソース: **{cfg.sim.sizing_param_source}**{sizing_detail}
- fill_mode: {cfg.sim.fill_mode} / slippage: {cfg.sim.slippage_pips} pips / equity: {cfg.equity_jpy:,.0f} 円
- 結果に効く環境変数: {env_line}
- スキップ日数（休場・キャッシュ欠損）: {len(outcome.skipped_days)}
{extra_lines}

## 規律メタ（設計書 §7）

- 同一ウィンドウへの通算試行回数: **{registry.window_trials} 回**（多重検定に注意）
- OOS 実行の通算回数: **{registry.oos_runs} 回**（OOS は最後に少数回）

## 成績比較（3 列必須・§7-3）

| 指標 | NO_TRADE | rule_based | {outcome.source_name} |
|---|---|---|---|
{_row("対象日数", "days")}
{_row("トレード数", "trades")}
{_row("勝率", "win_rate")}
{_row("プロフィットファクター", "profit_factor")}
{_row("期待値 (R)", "expectancy_r")}
{_row("最大DD (R)", "max_drawdown_r")}
{_row("損益合計 (pips)", "total_pnl_pips")}

## 評価対象の内訳（status 別日数）

{status_lines or "  - なし"}
"""


def reserve_run_dir(runs_dir: Path, run_id: str) -> Path:
    """run ディレクトリを原子的に作成（予約）する。

    mkdir の原子性で check-then-act の隙間なく run_id 衝突を排除する。
    台帳（experiments.jsonl）追記より前に呼ぶことで、追記後の衝突失敗による
    「実体のない孤立エントリ」を構造的に防ぐ（§5.2）。

    Raises:
        FileExistsError: run_id が既に存在する（監査成果物のサイレント上書き防止）。
    """
    run_dir = runs_dir / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise FileExistsError(f"run_id 衝突: {run_dir} は既に存在します") from None
    return run_dir


def save_run(
    runs_dir: Path,
    run_id: str,
    outcome: RunOutcome,
    target: Metrics,
    baseline_rule: Metrics,
    registry: RegistryStats,
    model: str | None = None,
    run_dir: Path | None = None,
    extra_meta: dict[str, object] | None = None,
) -> Path:
    """run 一式（report.md / report.json / trades.jsonl）を保存し run ディレクトリを返す。

    run_dir 省略時はここで予約する（衝突は FileExistsError）。台帳追記より前に
    予約したい場合は reserve_run_dir の戻り値を渡す。
    """
    if run_dir is None:
        run_dir = reserve_run_dir(runs_dir, run_id)

    report_md = render_report(
        run_id, outcome, target, baseline_rule, registry, model=model, extra_meta=extra_meta
    )
    _atomic_write_text(run_dir / "report.md", report_md)

    report_json = {
        "run_id": run_id,
        "source": outcome.source_name,
        "model": model,
        "llm_tokens": outcome.total_llm_tokens,  # experiments.jsonl と対称に（§5.2）
        "extra_meta": extra_meta or {},
        "config_hash": config_hash(outcome),
        "start": outcome.config.start.isoformat(),
        "end": outcome.config.end.isoformat(),
        "session": outcome.config.session.as_dict(),  # セッション窓（RD-1 #166）
        "env": outcome.env,
        "prompt_sha": outcome.prompt_sha,
        "sizing_param_source": outcome.config.sim.sizing_param_source,  # frozen | decision（#187）
        "is_oos": outcome.is_oos,
        "window_trials": registry.window_trials,
        "oos_runs": registry.oos_runs,
        "skipped_days": [d.isoformat() for d in outcome.skipped_days],
        "metrics": asdict(target),
        "baseline_rule_metrics": asdict(baseline_rule),
    }
    _atomic_write_text(
        run_dir / "report.json", json.dumps(report_json, ensure_ascii=False, indent=2)
    )

    trade_lines: list[str] = []
    for r in outcome.results:
        row = asdict(r)
        row["day"] = r.day.isoformat()
        trade_lines.append(json.dumps(row, ensure_ascii=False))
    _atomic_write_text(run_dir / "trades.jsonl", "\n".join(trade_lines) + "\n")

    return run_dir


def _atomic_write_text(path: Path, content: str) -> None:
    """クラッシュ時に壊れた監査成果物が残らないよう一時ファイル経由で置き換える。

    KlinesStore.save（backtest/data.py）と同じ mkstemp + os.replace パターン。
    書き込み失敗時は一時ファイルを削除する（run ディレクトリに残骸を残さない）。
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
