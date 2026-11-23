"""tests/monitor/test_closeout_watchdog.py — 手仕舞い欠測ウォッチドッグ（Issue #295）。

🔴 このテストが固定する契約:

現行の監視は「起動したが失敗した」系しか見ておらず、Scheduler が DISABLED なら
**すべてのアラームが沈黙する**。本ウォッチドッグはその穴を埋める最後の砦なので、
「アラートを出すべきときに出す」だけでなく「**出すべきでないときに黙る**」も固定する。
誤発火が続くと運用者が無視するようになり、結局沈黙と同じになるため。
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import patch

import pytest

from backend.src.monitor import closeout_watchdog as wd

# 2026-08-13 は木曜（closeout が動く日）
_THU = datetime.date(2026, 8, 13)
_SUN = datetime.date(2026, 8, 16)
_MON = datetime.date(2026, 8, 17)


def _run(today: datetime.date, log: dict[str, Any] | None, *, raises: bool = False) -> tuple[dict, list[str]]:
    sent: list[str] = []
    loader = (lambda d: (_ for _ in ()).throw(RuntimeError("S3 down"))) if raises else (lambda d: log)
    with (
        patch.object(wd, "_today_jst", return_value=today),
        patch.object(wd.s3_logger, "load_closeout_log", side_effect=loader),
        patch.object(wd, "send_slack_alert", side_effect=lambda m: sent.append(m) or True),
    ):
        return wd.lambda_handler({}, None), sent


class TestAlertsWhenMissing:
    def test_no_log_alerts(self) -> None:
        """ログが無い＝手仕舞いが実行されていない → 強アラート。"""
        res, sent = _run(_THU, None)
        assert res["status"] == "missing"
        assert res["reason"] == "log_missing"
        assert len(sent) == 1
        assert "手仕舞いが実行されていない可能性" in sent[0]

    def test_stale_log_alerts(self) -> None:
        """🔴 ログはあるが run_date が古い → アラート。

        月曜が休場だと火曜の実行が金曜に解決し、**土曜の実行が書いた金曜のログ**が
        既に存在する。存在チェックだけだと火曜の失敗を素通りしてしまう。
        """
        res, sent = _run(_THU, {"run_date": "2026-08-11", "status": "ok"})
        assert res["status"] == "missing"
        assert res["reason"] == "log_stale"
        assert "run_date=2026-08-11" in sent[0]

    def test_s3_error_alerts_fail_closed(self) -> None:
        """🔴 読めない＝「無い」ではないが、確認できない時点で沈黙しない（fail-closed）。"""
        res, sent = _run(_THU, None, raises=True)
        assert res["status"] == "error"
        assert len(sent) == 1
        assert "確認できません" in sent[0]


class TestSilentWhenHealthy:
    def test_fresh_log_is_silent(self) -> None:
        res, sent = _run(_THU, {"run_date": _THU.isoformat(), "status": "ok"})
        assert res["status"] == "ok"
        assert sent == []

    def test_step3_failed_log_is_still_silent(self) -> None:
        """closeout が失敗していても**本ウォッチドッグは黙る**。

        Step3 失敗は closeout 自身が強アラートを出す（#207）。ここで二重に出すと
        「どちらを見ればよいか」が曖昧になる。本ウォッチドッグの責務は
        **実行されたかどうか**だけ。
        """
        res, sent = _run(_THU, {"run_date": _THU.isoformat(), "status": "step3_failed"})
        assert res["status"] == "ok"
        assert sent == []

    def test_note_pending_false_is_silent(self) -> None:
        """note_pending が False で明示されていても沈黙すること。"""
        res, sent = _run(
            _THU, {"run_date": _THU.isoformat(), "status": "ok", "note_pending": False}
        )
        assert res["status"] == "ok"
        assert res["note_pending"] is False
        assert sent == []

    def test_absent_note_pending_is_silent(self) -> None:
        """確定版はキー自体が消える。欠落を「保留中」と誤読しないこと。"""
        res, sent = _run(_THU, {"run_date": _THU.isoformat(), "status": "ok"})
        assert res["note_pending"] is False
        assert sent == []


class TestWarnsWhenNoteMissing:
    """🔴 ログはあるがノートが無い日を拾う（#313）。

    #311 で証跡を LLM より先に保存するようにしたため、LLM がハングして Lambda が
    落ちても closeout ログは残る。存在チェックだけだと沈黙し、ノートが無いことに
    誰も気づかない。SDK の明示タイムアウトは「LLM 開始時点で残り時間が read timeout
    以上ある」ときだけ先に発火するので、この経路は塞ぎきれない。
    """

    def test_note_pending_warns_but_keeps_status_ok(self) -> None:
        res, sent = _run(
            _THU, {"run_date": _THU.isoformat(), "status": "ok", "note_pending": True}
        )
        # 手仕舞い自体は完了しているので主契約は ok のまま（status を汚さない）
        assert res["status"] == "ok"
        assert res["note_pending"] is True
        assert len(sent) == 1
        assert "トレードノートが生成されていません" in sent[0]

    def test_note_pending_alert_does_not_trigger_emergency_response(self) -> None:
        """証跡は無事なので WARN 文面であること。

        欠測アラートと同じ強さで出すと、建玉確認や緊急決済ラダーに人を走らせる。
        """
        _, sent = _run(
            _THU, {"run_date": _THU.isoformat(), "status": "ok", "note_pending": True}
        )
        assert "[WARN]" in sent[0]
        assert "保存済み" in sent[0]
        assert "ノート本文のみ再生成してください" in sent[0]
        assert "緊急決済" not in sent[0]
        assert "建玉が残っている場合" not in sent[0]

    def test_stale_log_with_note_pending_alerts_as_missing_only(self) -> None:
        """古いログは欠測アラートが優先。二重通知しないこと。"""
        res, sent = _run(
            _THU, {"run_date": "2026-08-11", "status": "ok", "note_pending": True}
        )
        assert res["status"] == "missing"
        assert len(sent) == 1
        assert "手仕舞いが実行されていない可能性" in sent[0]


class TestSkipsWhenNotScheduled:
    @pytest.mark.parametrize("day,label", [(_SUN, "日曜"), (_MON, "月曜")])
    def test_non_closeout_weekday_skips(self, day: datetime.date, label: str) -> None:
        """closeout は火〜土のみ。日曜・月曜は動かないので黙る。"""
        res, sent = _run(day, None)
        assert res["status"] == "skipped"
        assert res["reason"] == "closeout_not_scheduled_today"
        assert sent == []

    def test_market_holiday_does_not_skip(self) -> None:
        """🔴 休場日は**スキップしない**（レビュー指摘で方針変更）。

        closeout には休場の早期 return が無く、EventBridge も休場を知らないので
        **火〜土は休場でも起動してログを書く**。ここでスキップすると
        「休場の朝に trading cron だけ止まっていても検知しない」盲点になる。
        """
        # 当日だけ休場にする（全日 True にすると resolve_prev_trade_date の遡りが尽きない）
        with patch.object(wd, "is_market_holiday", side_effect=lambda d: d == _THU):
            res, sent = _run(_THU, None)
        assert res["status"] == "missing"
        assert len(sent) == 1


def _fresh(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    log: dict[str, Any] = {"run_date": _THU.isoformat(), "status": "ok"}
    if extra:
        log.update(extra)
    return log


class TestExecutionsUnfetched:
    """JSON bool の false だけを別 ALERT にする（#245）。"""

    def test_ok_false_alerts_without_ladder(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level("ERROR")
        res, sent = _run(_THU, _fresh({"executions_fetched": False, "status": "ok"}))
        assert res["status"] == "ok"
        assert res["executions_fetched"] is False
        assert res["executions_alert"] == "explicit_false"
        assert len(sent) == 1
        text = sent[0]
        assert "約定明細を取得できていません" in text
        assert "executions_fetched=false" in text
        assert "対象取引日=2026-08-12" in text
        assert "当朝 run_date=2026-08-13" in text
        assert "境界は未確認" in text
        assert "できるだけ早く" in text
        assert "この通知を理由に" in text
        assert "再 invoke しない" in text
        assert "緊急決済ラダーへは誘導しない" in text
        assert "緊急決済ラダーを優先" not in text
        assert "取得に進んでいない" not in text
        record = next(
            r
            for r in caplog.records
            if r.message == "closeout_watchdog_executions_unfetched"
        )
        assert record.closeout_status == "ok"
        assert record.executions_alert == "explicit_false"

    @pytest.mark.parametrize(
        "status",
        ["step3_failed", "step3_unverified", "step3_residual"],
    )
    def test_step3_false_prioritizes_position_response(self, status: str) -> None:
        res, sent = _run(_THU, _fresh({"executions_fetched": False, "status": status}))
        assert res["executions_alert"] == "explicit_false"
        assert len(sent) == 1
        assert status not in sent[0]
        assert "4:30" in sent[0]
        assert "緊急決済ラダーを優先" in sent[0]
        assert "止めない" in sent[0]
        assert "取得に進んでいない" not in sent[0]
        assert "この通知を理由に" in sent[0]

    @pytest.mark.parametrize(
        "status",
        ["market_closed", "step1_failed", "step2_failed"],
    )
    def test_early_return_missing_flag_is_not_attempted(self, status: str) -> None:
        res, sent = _run(_THU, _fresh({"status": status}))
        assert res["executions_fetched"] is None
        assert res["executions_alert"] == "not_attempted"
        assert len(sent) == 1
        assert "取得に進んでいない" in sent[0]
        assert "false だと断言しない" in sent[0]
        assert "executions_fetched=false" not in sent[0]
        assert status not in sent[0]
        assert "境界は未確認" in sent[0]
        assert "できるだけ早く" in sent[0]
        if status == "market_closed":
            assert "次回 cron" in sent[0]
            assert "再 invoke はしない" in sent[0]
        else:
            assert "4:30" in sent[0]
            assert "消えたとは断言しない" in sent[0]

    def test_string_false_with_step2_is_not_attempted_not_explicit(self) -> None:
        res, sent = _run(
            _THU, _fresh({"status": "step2_failed", "executions_fetched": "false"})
        )
        assert res["executions_fetched"] is None
        assert res["executions_alert"] == "not_attempted"
        assert "executions_fetched=false" not in sent[0]

    def test_step2_explicit_false_is_not_also_not_attempted(self) -> None:
        res, sent = _run(
            _THU, _fresh({"status": "step2_failed", "executions_fetched": False})
        )
        assert res["executions_alert"] == "explicit_false"
        assert len(sent) == 1
        assert "executions_fetched=false" in sent[0]
        assert "取得に進んでいない" not in sent[0]
        assert "step2_failed" not in sent[0]

    def test_step2_true_does_not_alert(self) -> None:
        res, sent = _run(
            _THU, _fresh({"status": "step2_failed", "executions_fetched": True})
        )
        assert res["status"] == "ok"
        assert res["executions_fetched"] is True
        assert res["executions_alert"] is None
        assert sent == []

    @pytest.mark.parametrize(
        "value",
        [True, None, "false", 0],
        ids=["true", "null", "string", "numeric-zero"],
    )
    def test_non_false_bool_does_not_alert(self, value: object) -> None:
        extra: dict[str, Any] = {"executions_fetched": value, "executions": []}
        res, sent = _run(_THU, _fresh(extra))
        assert res["status"] == "ok"
        assert sent == []
        assert res["executions_alert"] is None
        if value is True:
            assert res["executions_fetched"] is True
        else:
            assert res["executions_fetched"] is None

    def test_missing_key_does_not_alert(self) -> None:
        res, sent = _run(_THU, _fresh())
        assert res["executions_fetched"] is None
        assert sent == []

    def test_note_pending_and_false_send_both(self) -> None:
        res, sent = _run(_THU, _fresh({"executions_fetched": False, "note_pending": True}))
        assert res["status"] == "ok"
        assert res["note_pending"] is True
        assert res["executions_fetched"] is False
        assert len(sent) == 2
        warn = [m for m in sent if m.startswith("[AI Sniper][WARN]")]
        alert = [m for m in sent if m.startswith("[AI Sniper][ALERT]")]
        assert len(warn) == 1 and "トレードノートが生成されていません" in warn[0]
        assert len(alert) == 1 and "約定明細を取得できていません" in alert[0]
        assert "保存済みです" not in warn[0]
        assert "再生成してください" not in warn[0]
        assert "未確認" in warn[0]
        assert "この通知を理由に" in warn[0]
        assert "再 invoke しない" in warn[0]
        assert "発注 POST" in warn[0]
        assert "ロックを削除しない" in warn[0]
        assert "上書きしない" in warn[0]
        assert "緊急決済ラダーを優先" not in warn[0]

    @pytest.mark.parametrize(
        "status",
        ["step3_failed", "step3_unverified", "step3_residual"],
    )
    def test_explicit_false_note_pending_step3_warn_matches_alert(self, status: str) -> None:
        res, sent = _run(
            _THU,
            _fresh(
                {"executions_fetched": False, "note_pending": True, "status": status}
            ),
        )
        assert res["executions_alert"] == "explicit_false"
        warn = [m for m in sent if m.startswith("[AI Sniper][WARN]")]
        alert = [m for m in sent if m.startswith("[AI Sniper][ALERT]")]
        assert len(warn) == 1 and len(alert) == 1
        assert "保存済みです" not in warn[0]
        assert "手仕舞い自体は完了" not in warn[0]
        assert "建玉・注文への影響はありません" not in warn[0]
        assert "再生成してください" not in warn[0]
        assert "この通知を理由に" in warn[0]
        assert "再 invoke しない" in warn[0]
        assert "緊急決済ラダーを優先" in warn[0]
        assert "止めない" in warn[0]
        assert status not in warn[0]
        assert status not in alert[0]

    @pytest.mark.parametrize(
        "status",
        ["market_closed", "step1_failed", "step2_failed"],
    )
    def test_not_attempted_note_pending_warn_is_not_all_clear(self, status: str) -> None:
        res, sent = _run(_THU, _fresh({"note_pending": True, "status": status}))
        assert res["executions_alert"] == "not_attempted"
        warn = [m for m in sent if m.startswith("[AI Sniper][WARN]")]
        alert = [m for m in sent if m.startswith("[AI Sniper][ALERT]")]
        assert len(warn) == 1 and len(alert) == 1
        assert "取得に進んでいない" in warn[0]
        assert "保存済みです" not in warn[0]
        assert "手仕舞い自体は完了" not in warn[0]
        assert "建玉・注文への影響はありません" not in warn[0]
        assert "再生成してください" not in warn[0]
        assert "この通知を理由に" in warn[0]
        assert "再 invoke しない" in warn[0]
        assert status not in warn[0]
        assert status not in alert[0]
        if status == "market_closed":
            assert "次回 cron" in warn[0]
        else:
            assert "4:30" in warn[0]
            assert "消えたとは断言しない" in warn[0]

    def test_true_ok_note_pending_keeps_traditional_warn(self) -> None:
        res, sent = _run(
            _THU,
            _fresh({"executions_fetched": True, "note_pending": True, "status": "ok"}),
        )
        assert res["executions_alert"] is None
        assert len(sent) == 1
        assert "保存済みです" in sent[0]
        assert "ノート本文のみ再生成してください" in sent[0]
        assert "建玉・注文への影響はありません" in sent[0]

    def test_step3_true_note_pending_drops_all_clear(self) -> None:
        res, sent = _run(
            _THU,
            _fresh(
                {
                    "status": "step3_failed",
                    "executions_fetched": True,
                    "note_pending": True,
                }
            ),
        )
        assert res["executions_alert"] is None
        assert len(sent) == 1
        assert sent[0].startswith("[AI Sniper][WARN]")
        assert "手仕舞い自体は完了" not in sent[0]
        assert "建玉・注文への影響はありません" not in sent[0]
        assert "4:30" in sent[0]
        assert "緊急決済ラダーを優先" in sent[0]
        assert "step3_failed" not in sent[0]

    def test_stale_false_is_missing_only(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level("ERROR")
        res, sent = _run(
            _THU,
            {"run_date": "2026-08-11", "status": "ok", "executions_fetched": False},
        )
        assert res["status"] == "missing"
        assert res["executions_fetched"] is None
        assert len(sent) == 1
        assert "手仕舞いが実行されていない可能性" in sent[0]
        assert "約定明細を取得できていません" not in sent[0]
        assert "closeout_watchdog_executions_unfetched" not in caplog.text

    def test_no_log_does_not_add_unfetched_alert(self) -> None:
        res, sent = _run(_THU, None)
        assert res["status"] == "missing"
        assert res["executions_fetched"] is None
        assert len(sent) == 1
        assert "約定明細を取得できていません" not in sent[0]

    def test_s3_error_does_not_add_unfetched_alert(self) -> None:
        res, sent = _run(_THU, None, raises=True)
        assert res["status"] == "error"
        assert res["executions_fetched"] is None
        assert len(sent) == 1
        assert "確認できません" in sent[0]
        assert "約定明細を取得できていません" not in sent[0]

    @pytest.mark.parametrize("day", [_SUN, _MON])
    def test_weekend_skip_ignores_false(self, day: datetime.date) -> None:
        res, sent = _run(day, _fresh({"executions_fetched": False}))
        assert res["status"] == "skipped"
        assert res["executions_fetched"] is None
        assert sent == []


def test_watchdog_does_not_import_gmo() -> None:
    """案内は文面だけ。watchdog から GMO クライアントを import しない。"""
    import ast
    from pathlib import Path

    source = Path(wd.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    assert not any("gmo" in name or ".clients" in name for name in modules)


def test_prev_trade_date_matches_closeout_batch() -> None:
    """🔴 対象取引日の解決規則が closeout_batch と一致すること。

    ずれると別の取引日のログを見に行き、欠測を見逃す / 誤検知する。
    """
    from backend.src.batch.closeout_batch import _resolve_prev_trade_date

    for offset in range(0, 30):
        d = datetime.date(2026, 8, 1) + datetime.timedelta(days=offset)
        assert wd.resolve_prev_trade_date(d) == _resolve_prev_trade_date(d), d
