"""tests/infra/test_watchdog_independence.py — 監視は trading cron と運命を共有しない（#295）。

🔴 これが本ウォッチドッグの**設計上いちばん重要な性質**。

現行の監視（§5.4 / #105）は「起動したが失敗した」系しか見ておらず、Scheduler が
DISABLED なら全アラームが沈黙する。ウォッチドッグはその穴を埋めるが、**監視自身が
`var.schedule_state` に乗っていると、事故の再現（#294 型 = 全 schedule が DISABLED）で
監視ごと止まり、穴が復活する**。

「便利だから for_each をひとつにまとめよう」という将来のリファクタで静かに壊れる種類の
性質なので、機械的に固定する。
"""

from __future__ import annotations

import re
from pathlib import Path

_TF = (
    Path(__file__).resolve().parents[2]
    / "infra" / "terraform" / "modules" / "serverless_app" / "scheduler.tf"
)


def _resource_block(name: str) -> str:
    s = _TF.read_text()
    m = re.search(
        rf'resource "aws_scheduler_schedule" "{name}" \{{(.*?)\n\}}', s, re.S
    )
    assert m, f'resource "aws_scheduler_schedule" "{name}" が見つからない'
    return m.group(1)


def test_watchdog_schedule_exists() -> None:
    assert _resource_block("watchdog"), "ウォッチドッグ用スケジュールが無い"


def test_watchdog_state_is_independent_variable() -> None:
    """🔴 `state` は `var.watchdog_schedule_state`。`var.schedule_state` を参照しないこと。

    既定は ENABLED（#295）。計画停止（#371）は別変数を明示的に DISABLED にする。
    schedule_state に乗せると、trading cron が止まる事故で監視も同時に止まる。
    """
    block = _resource_block("watchdog")
    m = re.search(r"^\s*state\s*=\s*(.+?)\s*$", block, re.M)
    assert m, "watchdog スケジュールに state が無い"
    value = m.group(1).split("#")[0].strip()
    assert value == "var.watchdog_schedule_state", (
        f"🔴 watchdog の state が {value}。var.watchdog_schedule_state であること。"
        "var.schedule_state を参照すると、trading cron が止まる事故で監視も同時に止まる（#295）。"
    )
    assert "var.schedule_state" not in block, (
        "🔴 watchdog リソースが var.schedule_state を参照している（#295）。"
    )

    variables = (
        Path(__file__).resolve().parents[2]
        / "infra"
        / "terraform"
        / "modules"
        / "serverless_app"
        / "variables.tf"
    ).read_text()
    var_block = re.search(
        r'variable "watchdog_schedule_state" \{(.*?)\n\}', variables, re.S
    )
    assert var_block, "variable watchdog_schedule_state が無い"
    default = re.search(r'^\s*default\s*=\s*(.+)$', var_block.group(1), re.M)
    assert default and default.group(1).strip() == '"ENABLED"', (
        "🔴 watchdog_schedule_state のモジュール既定は ENABLED。"
        "DISABLED にすると新しい呼び出し元で監視が最初から沈黙する（#295）。"
    )


def test_trading_schedules_use_per_key_state() -> None:
    """判断と手仕舞いは別変数。for_each のキーとリソースアドレスは維持する。

    closeout を var.schedule_state に戻すと、建玉残存時に手仕舞いだけ
    ENABLED に戻せば判断 20:30 も再開する（#371）。
    """
    block = _resource_block("this")
    assert "for_each = local.schedules" in block
    assert "local.schedule_state_by_key[each.key]" in block
    s = _TF.read_text()
    m = re.search(r"schedule_state_by_key = \{(.*?)\n  \}", s, re.S)
    assert m, "schedule_state_by_key が無い"
    body = m.group(1)
    assert re.search(r"decision_weekday\s*=\s*var\.schedule_state\b", body)
    closeout = re.search(r"closeout_daily\s*=\s*(.+)", body)
    assert closeout and closeout.group(1).strip() == "var.closeout_schedule_state", (
        "closeout_daily は var.closeout_schedule_state であること"
    )


def test_watchdog_is_not_in_trading_for_each() -> None:
    """🔴 watchdog を `local.schedules` に混ぜないこと（混ぜると state を共有する）。"""
    s = _TF.read_text()
    m = re.search(r"\n  schedules = \{(.*?)\n  \}\n", s, re.S)
    assert m, "local.schedules が見つからない"
    assert "watchdog" not in m.group(1), (
        "🔴 watchdog が local.schedules に入っている。"
        "var.schedule_state を共有してしまい #295 の穴が復活する。"
    )


def test_scheduler_iam_covers_watchdog_target() -> None:
    """🔴 Scheduler の invoke 権限に watchdog のターゲットが含まれること。

    含め忘れると Scheduler がウォッチドッグを起動できず、**監視が沈黙する**
    （#130 の「発火するのに Lambda 未到達」と同型）。
    """
    s = _TF.read_text()
    m = re.search(
        r'data "aws_iam_policy_document" "scheduler_invoke" \{(.*?)\n\}', s, re.S
    )
    assert m, "scheduler_invoke ポリシーが見つからない"
    assert "local.watchdog_schedules" in m.group(1), (
        "🔴 scheduler_invoke の resources が local.watchdog_schedules を含んでいない。"
        "Scheduler がウォッチドッグを invoke できず監視が沈黙する。"
    )
