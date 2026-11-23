"""スパイク（E04〜E06）共通ユーティリティ.

`.env`（Git 管理外）から GMO API キーを読み込み、認証済み GmoFxClient を生成する。
実レスポンスを docs/spikes/ に記録する際は mask() でシークレットを必ず伏せること（AGENTS.md #8）。

使い方（例）:
    from scripts.spike_common import make_client
    client = make_client()
    resp = client.private_get("/private/v1/account/assets")  # E04
    print(resp.status_code, resp.json())
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from backend.src.clients.gmo_client import GmoFxClient


def make_client() -> GmoFxClient:
    """`.env` の GMO_API_KEY / GMO_API_SECRET から認証済みクライアントを生成する."""
    load_dotenv()
    api_key = os.environ.get("GMO_API_KEY", "")
    api_secret = os.environ.get("GMO_API_SECRET", "")
    if not api_key or not api_secret:
        raise SystemExit("GMO_API_KEY / GMO_API_SECRET が未設定です（.env を確認）。")
    return GmoFxClient(api_key=api_key, api_secret=api_secret)


def mask(value: str, keep: int = 4) -> str:
    """シークレットの末尾 keep 文字だけ残してマスクする（ログ・記録用、AGENTS.md #8）."""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]
