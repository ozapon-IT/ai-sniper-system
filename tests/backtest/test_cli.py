"""scripts/backtest.py CLI エントリポイントのテスト（BT-3）。

argparse フロー・終了コード（OOS 拒否 = 2、run 不在 = 1、正常 = 0）を
サブプロセスで検証する（本体ロジックは test_baseline_report.py が直接検証済み）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _ROOT / "scripts" / "backtest.py"


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=cwd or _ROOT,
    )


class TestCliExitCodes:
    def test_status_with_empty_registry_exits_zero(self, tmp_path: Path) -> None:
        result = _run_cli("status", "--registry", str(tmp_path / "none.jsonl"))
        assert result.returncode == 0
        assert "空" in result.stdout

    def test_oos_window_refused_with_exit_code_2(self, tmp_path: Path) -> None:
        result = _run_cli(
            "run",
            "--from",
            "2026-02-02",
            "--to",
            "2026-02-06",
            "--source",
            "rule",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--registry",
            str(tmp_path / "e.jsonl"),
        )
        assert result.returncode == 2
        assert "REFUSED" in result.stderr
        assert not (tmp_path / "e.jsonl").exists()  # 拒否された run は記録されない

    def test_in_sample_empty_cache_run_exits_zero_and_registers(self, tmp_path: Path) -> None:
        """IS 期間はキャッシュ空でも完走し（全日スキップ）、レジストリに記録される。"""
        result = _run_cli(
            "run",
            "--from",
            "2024-01-08",
            "--to",
            "2024-01-12",
            "--source",
            "rule",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--registry",
            str(tmp_path / "e.jsonl"),
        )
        assert result.returncode == 0
        assert "バックテストレポート" in result.stdout
        assert (tmp_path / "e.jsonl").exists()

    def test_report_missing_run_exits_one(self, tmp_path: Path) -> None:
        result = _run_cli("report", "--run-id", "no-such-run", "--runs-dir", str(tmp_path))
        assert result.returncode == 1

    def test_prepare_oos_refused_with_exit_code_2(self, tmp_path: Path) -> None:
        result = _run_cli(
            "prepare",
            "--from",
            "2026-02-02",
            "--to",
            "2026-02-06",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--runs-dir",
            str(tmp_path / "runs"),
        )
        assert result.returncode == 2
        assert "REFUSED" in result.stderr

    def test_prepare_in_sample_empty_cache_exits_zero(self, tmp_path: Path) -> None:
        """IS 期間はキャッシュ空でも完走する（全日スキップ・requests 0 件）。"""
        result = _run_cli(
            "prepare",
            "--from",
            "2024-01-08",
            "--to",
            "2024-01-09",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--runs-dir",
            str(tmp_path / "runs"),
        )
        assert result.returncode == 0
        assert "requests: 0 件" in result.stdout

    def test_check_reports_zero_pending_for_empty_prepare(self, tmp_path: Path) -> None:
        """requests 0 件の prepare 直後は check が pending 0 / exit 0 を返す。

        pending あり時の run --run-id 拒否（exit 3）は
        test_run_with_run_id_refuses_pending_decisions が検証する。
        """
        prep = _run_cli(
            "prepare",
            "--from",
            "2024-01-08",
            "--to",
            "2024-01-09",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--runs-dir",
            str(tmp_path / "runs"),
        )
        assert prep.returncode == 0
        run_id = prep.stdout.split("run_id: ")[1].splitlines()[0].strip()
        check = _run_cli("check", "--run-id", run_id, "--runs-dir", str(tmp_path / "runs"))
        assert check.returncode == 0
        assert "pending: 0" in check.stdout

    def test_run_default_preset_is_night(self, tmp_path: Path) -> None:
        """CLI 既定の session-preset は夜型（本番同型）。"""
        import json

        result = _run_cli(
            "run",
            "--from",
            "2024-01-08",
            "--to",
            "2024-01-12",
            "--source",
            "rule",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--registry",
            str(tmp_path / "e.jsonl"),
        )
        assert result.returncode == 0
        entry = json.loads((tmp_path / "e.jsonl").read_text().splitlines()[0])
        assert entry["session"]["decision_time"] == "20:30"

    def test_run_with_night_preset_records_session_in_registry(self, tmp_path: Path) -> None:
        """--session-preset night が RunConfig に貫通しレジストリへ記録される（RD-1）。"""
        import json

        result = _run_cli(
            "run",
            "--from",
            "2024-01-08",
            "--to",
            "2024-01-12",
            "--source",
            "rule",
            "--session-preset",
            "night",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--registry",
            str(tmp_path / "e.jsonl"),
        )
        assert result.returncode == 0
        assert "判断 20:30" in result.stdout  # report.md の実行条件行
        entry = json.loads((tmp_path / "e.jsonl").read_text().splitlines()[0])
        assert entry["session"] == {
            "decision_time": "20:30",
            "closeout_time": "04:30",
            "friday_closeout_time": "04:30",
        }

    def test_run_night_records_sizing_and_env(self, tmp_path: Path) -> None:
        """倍率ソースと結果に効く環境変数が report / 台帳に残る（§7-6）。"""
        import json

        result = _run_cli(
            "run",
            "--from",
            "2024-01-08",
            "--to",
            "2024-01-12",
            "--source",
            "night_day_momentum",
            "--session-preset",
            "night",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--registry",
            str(tmp_path / "e.jsonl"),
        )
        assert result.returncode == 0
        assert "結果に効く環境変数:" in result.stdout
        assert "倍率ソース: **frozen**" in result.stdout
        entry = json.loads((tmp_path / "e.jsonl").read_text().splitlines()[0])
        assert entry["source"] == "night_day_momentum"
        assert set(entry["env"]) >= {"RISK_PERCENT", "MAX_LEVERAGE", "MIN_RR_RATIO"}

    def test_run_invalid_session_window_refused(self, tmp_path: Path) -> None:
        """判断 06:00 前は 5 分足ファイル起点より前 → 構築時検証で拒否（exit 2）。"""
        result = _run_cli(
            "run",
            "--from",
            "2024-01-08",
            "--to",
            "2024-01-12",
            "--source",
            "rule",
            "--decision-time",
            "05:00",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--registry",
            str(tmp_path / "e.jsonl"),
        )
        assert result.returncode == 2
        assert "REFUSED" in result.stderr
        assert not (tmp_path / "e.jsonl").exists()

    def test_prepare_records_session_in_run_meta(self, tmp_path: Path) -> None:
        """prepare のセッション窓が run_meta.json に残る（run --run-id 再生の唯一の正）。"""
        import json

        prep = _run_cli(
            "prepare",
            "--from",
            "2024-01-08",
            "--to",
            "2024-01-09",
            "--session-preset",
            "night",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--runs-dir",
            str(tmp_path / "runs"),
        )
        assert prep.returncode == 0
        run_id = prep.stdout.split("run_id: ")[1].splitlines()[0].strip()
        meta = json.loads(
            (tmp_path / "runs" / run_id / "run_meta.json").read_text(encoding="utf-8")
        )
        assert meta["session"]["decision_time"] == "20:30"

    def test_run_with_run_id_refuses_session_flags(self, tmp_path: Path) -> None:
        """--run-id のセッション窓は prepare 記録が唯一の正 → CLI 上書きは拒否（exit 2）。"""
        prep = _run_cli(
            "prepare",
            "--from",
            "2024-01-08",
            "--to",
            "2024-01-09",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--runs-dir",
            str(tmp_path / "runs"),
        )
        assert prep.returncode == 0
        run_id = prep.stdout.split("run_id: ")[1].splitlines()[0].strip()
        result = _run_cli(
            "run",
            "--run-id",
            run_id,
            "--session-preset",
            "morning",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--registry",
            str(tmp_path / "e.jsonl"),
        )
        assert result.returncode == 2
        assert "REFUSED" in result.stderr
        assert not (tmp_path / "e.jsonl").exists()

    def test_run_with_run_id_refuses_pending_decisions(self, tmp_path: Path) -> None:
        """decide 未完了の run --run-id は exit 3 で拒否する（中断→再開の強制）。"""
        from datetime import date

        from backtest.data import KlinesStore
        from tests.backtest.test_baseline_report import _populate_cache

        days = [date(2025, 7, 3), date(2025, 7, 4)]
        _populate_cache(KlinesStore(cache_dir=tmp_path / "cache"), days)
        prep = _run_cli(
            "prepare",
            "--from",
            "2025-07-03",
            "--to",
            "2025-07-04",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--runs-dir",
            str(tmp_path / "runs"),
        )
        assert prep.returncode == 0
        assert "requests: 2 件" in prep.stdout
        run_id = prep.stdout.split("run_id: ")[1].splitlines()[0].strip()

        result = _run_cli(
            "run",
            "--run-id",
            run_id,
            "--cache-dir",
            str(tmp_path / "cache"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--registry",
            str(tmp_path / "e.jsonl"),
        )
        assert result.returncode == 3
        assert "pending=2" in result.stderr
        assert not (tmp_path / "e.jsonl").exists()  # 拒否された run は台帳に残らない
