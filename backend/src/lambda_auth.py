"""Lambda 共有シークレット取得ユーティリティ（§1.6）.

GMO 認証情報 / Anthropic キー / Slack Webhook を Secrets Manager から取得し、
専用フラグが立っているときのみ環境変数フォールバックを許可する（ローカル開発専用）。

🔴 **ダッシュボード用の Bearer トークン（`dashboard-token`）は廃止した**
（Ver 3.7 / §1.6.1・#177）。read-api / kill-switch は HTTP 公開せず、認証境界は
IAM（`lambda:InvokeFunction`）に置く。静的クライアントに長期の共有秘密を置けない以上、
Bearer 方式では #177 を解けないため。
"""

from __future__ import annotations

import json as _json
import logging
import os

import boto3

logger = logging.getLogger(__name__)

GMO_CREDENTIALS_SECRET_NAME = os.environ.get(
    "GMO_CREDENTIALS_SECRET_NAME", "your-app/gmo-credentials"
)
ANTHROPIC_SECRET_NAME = os.environ.get(
    "ANTHROPIC_SECRET_NAME", "your-app/anthropic-api-key"
)
SLACK_WEBHOOK_SECRET_NAME = os.environ.get(
    "SLACK_WEBHOOK_SECRET_NAME", "your-app/slack-webhook-url"
)

_cached_gmo_api_key: str | None = None
_cached_gmo_api_secret: str | None = None
_cached_anthropic_key: str | None = None
_cached_slack_webhook: str | None = None


def _local_fallback_allowed(flag: str) -> bool:
    """ローカル開発専用 env フォールバックの許可判定（Issue #139）.

    Lambda 実行環境（AWS_LAMBDA_FUNCTION_NAME が必ずセットされる）では
    フラグが立っていても env の平文シークレットを使わせない。
    """
    return (
        os.environ.get(flag, "").strip().lower() == "true"
        and not os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    )



def get_gmo_credentials() -> tuple[str, str]:
    """Secrets Manager から GMO API キー / シークレットを取得し invocation 内でキャッシュ (§7.2, AGENTS.md #8).

    Returns:
        (api_key, api_secret) のタプル。

    ローカル開発専用: ALLOW_LOCAL_GMO_CREDENTIALS=true かつ GMO_API_KEY/GMO_API_SECRET が
    環境変数に設定されている場合のみ環境変数を使用する。
    """
    global _cached_gmo_api_key, _cached_gmo_api_secret
    if _cached_gmo_api_key is not None and _cached_gmo_api_secret is not None:
        return _cached_gmo_api_key, _cached_gmo_api_secret

    # ローカル開発専用フォールバック（Bearer トークンフラグとは独立した別フラグ）
    if _local_fallback_allowed("ALLOW_LOCAL_GMO_CREDENTIALS"):
        env_key = os.environ.get("GMO_API_KEY", "").strip()
        env_secret = os.environ.get("GMO_API_SECRET", "").strip()
        if env_key and env_secret:
            _cached_gmo_api_key = env_key
            _cached_gmo_api_secret = env_secret
            return _cached_gmo_api_key, _cached_gmo_api_secret

    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    sm_client = boto3.client("secretsmanager", region_name=region)
    resp = sm_client.get_secret_value(SecretId=GMO_CREDENTIALS_SECRET_NAME)
    secret_str = resp.get("SecretString", "")
    if not secret_str:
        raise RuntimeError(f"Secrets Manager の {GMO_CREDENTIALS_SECRET_NAME} が空です")
    creds = _json.loads(secret_str)
    if not isinstance(creds, dict):
        raise RuntimeError(
            f"Secrets Manager の {GMO_CREDENTIALS_SECRET_NAME} は JSON object である必要があります"
        )
    api_key = creds.get("api_key")
    api_secret = creds.get("api_secret")
    # None や数値は str() で "None" になり空チェックをすり抜けるため isinstance で先に弾く
    if not isinstance(api_key, str) or not isinstance(api_secret, str):
        raise RuntimeError(
            f"Secrets Manager の {GMO_CREDENTIALS_SECRET_NAME} に api_key/api_secret がありません"
        )
    _cached_gmo_api_key = api_key.strip()
    _cached_gmo_api_secret = api_secret.strip()
    if not _cached_gmo_api_key or not _cached_gmo_api_secret:
        raise RuntimeError(
            f"Secrets Manager の {GMO_CREDENTIALS_SECRET_NAME} に api_key/api_secret がありません"
        )
    return _cached_gmo_api_key, _cached_gmo_api_secret


def get_anthropic_api_key() -> str:
    """Secrets Manager から Anthropic API キーを取得し invocation 内でキャッシュ (§7.2, AGENTS.md #8).

    ローカル開発専用: ALLOW_LOCAL_ANTHROPIC_KEY=true かつ ANTHROPIC_API_KEY が
    環境変数に設定されている場合のみ環境変数を使用する。
    """
    global _cached_anthropic_key
    if _cached_anthropic_key is not None:
        return _cached_anthropic_key

    # ローカル開発専用フォールバック（GMO / dashboard フラグとは独立した別フラグ）
    if _local_fallback_allowed("ALLOW_LOCAL_ANTHROPIC_KEY"):
        env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if env_key:
            _cached_anthropic_key = env_key
            return _cached_anthropic_key

    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    sm_client = boto3.client("secretsmanager", region_name=region)
    resp = sm_client.get_secret_value(SecretId=ANTHROPIC_SECRET_NAME)
    secret = resp.get("SecretString", "").strip()
    if not secret:
        raise RuntimeError(f"Secrets Manager の {ANTHROPIC_SECRET_NAME} が空です")
    _cached_anthropic_key = secret
    return _cached_anthropic_key


def get_slack_webhook_url() -> str:
    """Secrets Manager から Slack incoming webhook URL を取得し invocation 内でキャッシュ (§7.2, AGENTS.md #8).

    監視アラート通知 Lambda（slack_notifier）が使用する（Issue #105）。
    webhook URL は秘匿情報のため env / tfstate に平文を残さず Secrets Manager から取得する。

    ローカル開発専用: ALLOW_LOCAL_SLACK_WEBHOOK=true かつ SLACK_WEBHOOK_URL が
    環境変数に設定されている場合のみ環境変数を使用する。
    """
    global _cached_slack_webhook
    if _cached_slack_webhook is not None:
        return _cached_slack_webhook

    # ローカル開発専用フォールバック（他フラグとは独立した別フラグ）。
    # 本番バイパス防止: Lambda 実行環境（AWS_LAMBDA_FUNCTION_NAME が必ずセットされる）では
    # フラグが立っていても env の平文 webhook を使わず、必ず Secrets Manager 経由にする。
    if _local_fallback_allowed("ALLOW_LOCAL_SLACK_WEBHOOK"):
        env_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
        if env_url:
            _cached_slack_webhook = env_url
            return _cached_slack_webhook

    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    sm_client = boto3.client("secretsmanager", region_name=region)
    resp = sm_client.get_secret_value(SecretId=SLACK_WEBHOOK_SECRET_NAME)
    secret = resp.get("SecretString", "").strip()
    if not secret:
        raise RuntimeError(f"Secrets Manager の {SLACK_WEBHOOK_SECRET_NAME} が空です")
    _cached_slack_webhook = secret
    return _cached_slack_webhook



def reset_token_cache() -> None:
    """invocation 開始時またはテストでキャッシュをクリアする.

    Lambda ウォームコンテナではモジュールグローバルが引き継がれるため、
    各 lambda_handler の先頭でこの関数を呼ぶことでトークンローテーションに対応する。
    """
    global _cached_gmo_api_key, _cached_gmo_api_secret, _cached_anthropic_key
    global _cached_slack_webhook
    _cached_gmo_api_key = None
    _cached_gmo_api_secret = None
    _cached_anthropic_key = None
    _cached_slack_webhook = None
