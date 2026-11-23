"""tests/persistence/test_s3_logger.py — S3 ログ保存のテスト（§1.5, §5.2）。"""

from __future__ import annotations

import json
import urllib.request
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from backend.src.persistence import s3_logger

TODAY = date(2025, 6, 28)
# 秘匿 webhook URL のダミー。ログへの漏洩検査に使う（AGENTS.md #8）。
WEBHOOK = "https://hooks.slack.com/services/T000/B000/SECRETTOKEN"
SAMPLE_LOG = {
    "status": "ok",
    "date": "2025-06-28",
    "llm_total_tokens": 1234,
    "api_requests": 5,
}


# ---------------------------------------------------------------------------
# S3 パス規約テスト
# ---------------------------------------------------------------------------


def test_s3_key_decision_log():
    assert s3_logger.s3_key_decision_log(TODAY) == "logs/decision/2025-06-28.json"


def test_s3_key_closeout_log():
    assert s3_logger.s3_key_closeout_log(TODAY) == "logs/closeout/2025-06-28.json"


def test_s3_key_trade_note():
    assert s3_logger.s3_key_trade_note(TODAY) == "notes/2025-06-28.json"


def test_s3_key_dashboard():
    assert s3_logger.S3_KEY_DASHBOARD == "summary/dashboard.json"


# ---------------------------------------------------------------------------
# ファイル fallback テスト（LOCAL_PERSIST_DIR）
# ---------------------------------------------------------------------------


def test_save_decision_log_file_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_PERSIST_DIR", str(tmp_path))
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", str(tmp_path))

    result = s3_logger.save_decision_log(SAMPLE_LOG, TODAY)

    assert result is True
    written = (tmp_path / "logs" / "decision" / "2025-06-28.json").read_text(encoding="utf-8")
    data = json.loads(written)
    assert data["llm_total_tokens"] == 1234
    assert data["api_requests"] == 5


def test_save_closeout_log_file_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", str(tmp_path))

    result = s3_logger.save_closeout_log({"status": "closed"}, TODAY)

    assert result is True
    written = (tmp_path / "logs" / "closeout" / "2025-06-28.json").read_text(encoding="utf-8")
    assert json.loads(written) == {"status": "closed"}


def test_save_trade_note_file_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", str(tmp_path))

    result = s3_logger.save_trade_note({"note": "good trade"}, TODAY)

    assert result is True
    written = (tmp_path / "notes" / "2025-06-28.json").read_text(encoding="utf-8")
    assert json.loads(written) == {"note": "good trade"}


def test_save_dashboard_file_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", str(tmp_path))

    result = s3_logger.save_dashboard({"summary": "test"})

    assert result is True
    written = (tmp_path / "summary" / "dashboard.json").read_text(encoding="utf-8")
    assert json.loads(written) == {"summary": "test"}


def test_save_local_failure_triggers_slack_alert(tmp_path, monkeypatch):
    """ローカル書き込み失敗時も Slack アラートが送信される。"""
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", str(tmp_path))

    alerts: list[str] = []
    monkeypatch.setattr(s3_logger, "_slack_alert", lambda msg: alerts.append(msg))

    def _raise(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(s3_logger, "_write_local", _raise)

    result = s3_logger.save_decision_log(SAMPLE_LOG, TODAY)

    assert result is False
    assert len(alerts) == 1
    assert "ローカル永続化失敗" in alerts[0]


# ---------------------------------------------------------------------------
# S3 書き込み成功テスト
# ---------------------------------------------------------------------------


def test_save_decision_log_s3_success(monkeypatch):
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", "")
    mock_s3 = MagicMock()
    with patch.object(s3_logger, "_boto3_client", return_value=mock_s3):
        result = s3_logger.save_decision_log(SAMPLE_LOG, TODAY)

    assert result is True
    mock_s3.put_object.assert_called_once()
    call_kwargs = mock_s3.put_object.call_args[1]
    assert call_kwargs["Key"] == "logs/decision/2025-06-28.json"
    body = json.loads(call_kwargs["Body"])
    assert body["llm_total_tokens"] == 1234


# ---------------------------------------------------------------------------
# S3 書き込み失敗 → Slack アラート + False 返却
# ---------------------------------------------------------------------------


def test_save_log_s3_failure_triggers_slack_alert(monkeypatch):
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", "")

    mock_s3 = MagicMock()
    mock_s3.put_object.side_effect = RuntimeError("S3 unavailable")

    alerts: list[str] = []
    monkeypatch.setattr(s3_logger, "_slack_alert", lambda msg: alerts.append(msg))

    with patch.object(s3_logger, "_boto3_client", return_value=mock_s3):
        result = s3_logger.save_decision_log(SAMPLE_LOG, TODAY)

    assert result is False
    assert len(alerts) == 1
    assert "S3 書き込み失敗" in alerts[0]


def test_save_log_s3_failure_webhook_fetch_failure_no_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """webhook 取得に失敗しても save_log は例外を伝播せず False を返す。"""
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", "")

    def _fetch_fail() -> str:
        raise RuntimeError("Secrets Manager unavailable")

    # _slack_alert の実体は alerting.slack_alert に集約されている（#206）
    monkeypatch.setattr(
        "backend.src.alerting.slack_alert.get_slack_webhook_url", _fetch_fail
    )

    mock_s3 = MagicMock()
    mock_s3.put_object.side_effect = RuntimeError("S3 unavailable")

    with patch.object(s3_logger, "_boto3_client", return_value=mock_s3):
        result = s3_logger.save_decision_log(SAMPLE_LOG, TODAY)

    assert result is False  # 例外を伝播せず False を返す


def test_save_log_raise_on_failure(monkeypatch):
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", "")

    mock_s3 = MagicMock()
    mock_s3.put_object.side_effect = RuntimeError("S3 unavailable")
    monkeypatch.setattr(s3_logger, "_slack_alert", lambda msg: None)

    with (
        patch.object(s3_logger, "_boto3_client", return_value=mock_s3),
        pytest.raises(RuntimeError, match="S3 unavailable"),
    ):
        s3_logger.save_decision_log(SAMPLE_LOG, TODAY, raise_on_failure=True)


# ---------------------------------------------------------------------------
# _slack_alert — send_slack_alert への委譲（#206 で alerting/slack_alert に集約。
# 実体のテストは tests/alerting/test_slack_alert.py）
# ---------------------------------------------------------------------------


def test_slack_alert_delegates_to_send_slack_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    """_slack_alert が alerting.slack_alert.send_slack_alert に委譲すること。"""
    sent: list[str] = []

    def _fake_send(message: str) -> bool:
        sent.append(message)
        return True

    monkeypatch.setattr(s3_logger, "send_slack_alert", _fake_send)

    s3_logger._slack_alert("test message")

    assert sent == ["test message"]


def test_save_log_s3_failure_sends_alert_via_secrets_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本番相当（SLACK_WEBHOOK_URL env なし）でも永続化失敗アラートが POST まで到達する。

    Issue #140 (A): 旧実装は env 未設定時に _slack_alert が黙って no-op になっていた。
    """
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", "")
    monkeypatch.setattr(
        "backend.src.alerting.slack_alert.get_slack_webhook_url", lambda: WEBHOOK
    )

    posted: list[urllib.request.Request] = []

    def _fake_urlopen(req: urllib.request.Request, timeout: float | None = None) -> MagicMock:
        posted.append(req)
        return MagicMock()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    mock_s3 = MagicMock()
    mock_s3.put_object.side_effect = RuntimeError("S3 unavailable")

    with patch.object(s3_logger, "_boto3_client", return_value=mock_s3):
        result = s3_logger.save_decision_log(SAMPLE_LOG, TODAY)

    assert result is False
    assert len(posted) == 1
    assert posted[0].full_url == WEBHOOK
    assert "S3 書き込み失敗" in json.loads(posted[0].data.decode())["text"]


# ---------------------------------------------------------------------------
# ログ JSON の必須フィールド検証（§5.2 税務証跡）
# ---------------------------------------------------------------------------


def test_log_payload_required_fields_persisted(tmp_path, monkeypatch):
    """save_decision_log が実際に書き込む JSON に timestamp / llm_total_tokens / api_requests が含まれる（§5.2）。"""
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", str(tmp_path))

    payload = {
        "status": "ok",
        "date": "2025-06-28",
        "timestamp": "2025-06-28T07:30:00+09:00",
        "llm_total_tokens": 999,
        "api_requests": 3,
    }
    s3_logger.save_decision_log(payload, TODAY)

    written = json.loads(
        (tmp_path / "logs" / "decision" / "2025-06-28.json").read_text(encoding="utf-8")
    )
    assert written["timestamp"] == "2025-06-28T07:30:00+09:00"
    assert written["llm_total_tokens"] == 999
    assert written["api_requests"] == 3


def test_load_decision_log_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", str(tmp_path))
    assert s3_logger.load_decision_log(TODAY) is None


def test_load_decision_log_reads_decision_key(tmp_path, monkeypatch):
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", str(tmp_path))
    decision_dir = tmp_path / "logs" / "decision"
    decision_dir.mkdir(parents=True)
    (decision_dir / "2025-06-28.json").write_text(
        json.dumps({"source": "decision"}), encoding="utf-8"
    )
    assert s3_logger.load_decision_log(TODAY) == {"source": "decision"}


def test_load_closeout_log_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", str(tmp_path))
    assert s3_logger.load_closeout_log(TODAY) is None


def test_load_closeout_log_reads_closeout_key(tmp_path, monkeypatch):
    monkeypatch.setattr(s3_logger, "LOCAL_PERSIST_DIR", str(tmp_path))
    closeout_dir = tmp_path / "logs" / "closeout"
    closeout_dir.mkdir(parents=True)
    (closeout_dir / "2025-06-28.json").write_text(
        json.dumps({"source": "closeout"}), encoding="utf-8"
    )
    assert s3_logger.load_closeout_log(TODAY) == {"source": "closeout"}
