"""ORB デモ Terraform の不変条件（Issue #299）。

設計書 §3.2（封じ込め）・§5（Europe/London 例外）・§6.1（別テーブル）

HCL を文字列として検査する軽量ガード。`terraform` バイナリ非依存で CI に載る。
"""

from __future__ import annotations

from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "infra" / "terraform" / "modules" / "orb_demo"
APP_MODULE = Path(__file__).resolve().parents[2] / "infra" / "terraform" / "modules" / "serverless_app"
STAGING = Path(__file__).resolve().parents[2] / "infra" / "terraform" / "environments" / "staging"


def _strip_comments(hcl: str) -> str:
    """HCL からコメントを除いた**設定部分だけ**を返す。

    ガードは「設定に現れないこと」を検査したい。説明文で禁止トークンに言及するのは
    むしろ望ましい（なぜ渡さないかを書く）ので、コメントを拾って落ちてはいけない。
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


def _read(path: Path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def _module_text() -> str:
    return "\n".join(_read(p) for p in sorted(MODULE.glob("*.tf")))


class TestContainment:
    """§3.2 封じ込め第2層: ORB に GMO / Anthropic のシークレットを与えない。"""

    @pytest.mark.parametrize(
        "token",
        [
            "gmo_credentials_secret",
            "GMO_CREDENTIALS_SECRET_NAME",
            "anthropic_secret",
            "ANTHROPIC_SECRET_NAME",
            "ALLOW_LIVE_ORDERS",
            "TRADING_PHASE",
            "MVP_MODE",
        ],
    )
    def test_no_trading_secrets_or_two_key(self, token: str) -> None:
        assert token not in _module_text(), (
            f"ORB モジュールに {token} が現れている。"
            "ORB は Public API のみで完結し、実発注を構造的に不可能にする（設計書 §3.2）"
        )

    def test_only_slack_secret_is_granted(self) -> None:
        iam = _read(MODULE / "iam.tf")
        assert "var.slack_webhook_secret_arn" in iam
        # GetSecretValue の resources に Slack 以外が出ていないこと
        assert iam.count("secretsmanager:GetSecretValue") == 1

    def test_s3_is_scoped_to_orb_prefix(self) -> None:
        """夜型のトレードノート・税務証跡を ORB から書き換えられないこと。"""
        iam = _read(MODULE / "iam.tf")
        assert "/orb-demo/*" in iam
        assert 'actions   = ["s3:PutObject"]' in iam
        # バケット全体への PutObject が無いこと
        assert '${var.s3_log_bucket_name}/*"' not in iam

    def test_app_module_exposes_only_slack_secret(self) -> None:
        """夜型モジュールが GMO / Anthropic の ARN を output していないこと。"""
        outputs = _read(APP_MODULE / "outputs.tf")
        assert "slack_webhook_secret_arn" in outputs
        for token in ("gmo_credentials_secret_arn", "anthropic_secret_arn"):
            assert token not in outputs, f"{token} が output されている（ORB へ漏れうる）"


class TestSchedule:
    def test_timezone_is_europe_london(self) -> None:
        """AGENTS.md 規約6 の例外。JST に読み替えてはならない（設計書 §5）。"""
        sched = _read(MODULE / "scheduler.tf")
        assert 'schedule_expression_timezone = "Europe/London"' in sched
        assert "Asia/Tokyo" not in sched

    def test_poller_covers_eod_1600(self) -> None:
        """EOD は「16:00 以降最初の観測」で確定する（§6.4）。15:55 までだと取り切れない。"""
        sched = _read(MODULE / "scheduler.tf")
        assert "cron(0 16 ? * MON-FRI *)" in sched, "poller の 16:00 回が無い"

    def test_session_open_is_0901_not_0900(self) -> None:
        """9:00 ちょうどだと 8:59 足の未確定値を掴む（ORB-1b 実測・§6.2）。"""
        sched = _read(MODULE / "scheduler.tf")
        assert "cron(1 9 ? * MON-FRI *)" in sched

    def test_intraday_poller_starts_each_hour_at_zero(self) -> None:
        """🔴 `*/5` であって `5/5` ではない（#306）。

        AWS の `/` は「開始値からの増分」なので `5/5` は 5,10,…,55 で **:00 に折り返さない**。
        9:00 の回を避ける意図で `5/5` と書いた結果、毎時 :00 が落ち、`run_seconds=300` と
        合わせて **:00:35〜:05:33 の約 5 分 × 1 日 6 回**が観測できていなかった
        （2026-08-14 実測: 起動 78 回 = 11×7 + EOD）。
        """
        sched = _read(MODULE / "scheduler.tf")
        assert "cron(*/5 9-15 ? * MON-FRI *)" in sched, "intraday poller が */5 でない"
        assert "cron(5/5" not in sched, "5/5 は毎時 :00 を落とす（#306 の再発）"

    def test_poller_covers_0902_open_window(self) -> None:
        """🔴 9:01→9:05 は `ARMED` なのに観測が無い（#307）。

        エントリー窓は 9:00 に開くが session_open は 9:01、intraday の初回は 9:05。
        ORB はブレイク直後にトリガーが走りやすく、盲点だと §6.3 の推定滑りが悪化側へ偏る。
        """
        sched = _read(MODULE / "scheduler.tf")
        assert "cron(2 9 ? * MON-FRI *)" in sched, "9:02 の poller が無い"

    def test_no_per_minute_poller(self) -> None:
        """毎分起動は禁止（#307）。300 秒 × 毎分 = 定常同時実行 5 本でアカウント上限 10 を圧迫する。"""
        sched = _read(MODULE / "scheduler.tf")
        for bad in ("cron(2/1 ", "cron(*/1 ", "cron(0/1 "):
            assert bad not in sched, f"毎分起動 {bad} が入っている"

    def test_scheduler_source_arn_is_schedule_group(self) -> None:
        """schedule-group ARN でないと assume 不可で全 cron が permanent 失敗（#129/#130）。"""
        sched = _read(MODULE / "scheduler.tf")
        assert "aws_scheduler_schedule_group.orb.arn" in sched
        assert "schedule-group/default" not in sched

    def test_uses_dedicated_schedule_group(self) -> None:
        """夜型 default グループに混ぜると実弾アラームが汚染される。"""
        sched = _read(MODULE / "scheduler.tf")
        assert "aws_scheduler_schedule_group.orb.name" in sched
        assert 'group_name  = "default"' not in sched
        assert 'group_name = "default"' not in sched

    def test_scheduler_role_can_send_to_dlq(self) -> None:
        """SendToDLQ が無いと invoke 失敗の証跡が消える（夜型 #104）。"""
        sched = _read(MODULE / "scheduler.tf")
        assert "sqs:SendMessage" in sched
        assert "SendToDLQ" in sched

    def test_poller_eod_is_short_window(self) -> None:
        """16:00 回が 300 秒だと 15:55 回と二重書きする。"""
        sched = _read(MODULE / "scheduler.tf")
        assert "run_seconds = 30" in sched

    def test_resource_uses_schedule_state_variable(self) -> None:
        sched = _read(MODULE / "scheduler.tf")
        assert "state = var.schedule_state" in sched

    def test_default_state_is_disabled(self) -> None:
        """apply しただけでデモのクロックが動き出さないこと。"""
        variables = _read(MODULE / "variables.tf")
        assert 'default     = "DISABLED"' in variables


class TestSeparation:
    def test_orb_uses_its_own_sessions_table(self) -> None:
        """夜型の job-lock とは別テーブル（設計書 §6.1）。"""
        text = _module_text()
        assert "orb_demo_sessions" in text
        assert "your-job-lock-table" not in text

    def test_staging_calls_orb_as_separate_module(self) -> None:
        main = _read(STAGING / "main.tf")
        assert 'module "orb_demo"' in main
        assert "../../modules/orb_demo" in main

    def test_app_module_has_no_orb_resources(self) -> None:
        """夜型モジュール側に ORB のリソースを足していないこと（爆発半径の分離）。"""
        app = "\n".join(_read(p) for p in sorted(APP_MODULE.glob("*.tf")))
        for token in ("orb_demo_sessions", "orb_session_open", "orb_poller", "Europe/London"):
            assert token not in app, f"夜型モジュールに ORB の {token} が混入している"

    def test_staging_declares_orb_posture_in_autotfvars(self) -> None:
        """姿勢が auto.tfvars で**宣言されている**こと（`-var` 依存にしない）。

        🔴 値そのもの（ENABLED/DISABLED）は `test_operating_posture.py` が唯一の正。
        ここで値まで固定すると、姿勢変更のたびに 2 箇所直すことになり、
        片方の直し忘れで「テストは緑だが姿勢は宣言と違う」状態を作りうる。
        本テストが守るのは「`-var` ではなく auto.tfvars に書いてある」ことだけ。
        """
        tfvars = (STAGING / "phase2.auto.tfvars").read_text(encoding="utf-8")
        assert "orb_schedule_state" in tfvars, "ORB cron の姿勢が宣言されていない"
        assert "orb_demo_enabled" in tfvars, "ORB マスタースイッチが宣言されていない"

    def test_monitoring_is_on_orb_schedule_group(self) -> None:
        mon = _read(MODULE / "monitoring.tf")
        assert "ScheduleGroup = aws_scheduler_schedule_group.orb.name" in mon
        assert 'ScheduleGroup = "default"' not in mon
