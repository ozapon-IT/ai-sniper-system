"""#207 (c) スパイク: FX 閉場中の参照系レスポンスを採取する。

土日（FX 閉場）に実行する。**GET のみ**・read-only で two-key interlock とは無関係。

目的:
  1. `GET /public/v1/status` が閉場中に何を返すか（公式は MAINTENANCE/CLOSE/OPEN）
  2. 現行 Step0 の代理判定（`GET openPositions` の正常応答）が閉場中も通ってしまうか
  3. `GET activeOrders` の閉場中の挙動

結果は docs/spikes/ に記録する（付録B・AGENTS.md）。
"""

from __future__ import annotations

import datetime
import json
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(dotenv_path=os.path.join(REPO, ".env"))

from backend.src.clients.gmo_client import GmoFxClient  # noqa: E402
from backend.src.gmo_fx_endpoints import (  # noqa: E402
    PRIVATE_ACCOUNT_ASSETS,
    PRIVATE_ACTIVE_ORDERS,
    PRIVATE_OPEN_POSITIONS,
    PUBLIC_TICKER,
)

JST = ZoneInfo("Asia/Tokyo")
PUBLIC_STATUS = "/public/v1/status"   # ← 現行 gmo_fx_endpoints.py に定数が無い


def _dump(label: str, resp: object) -> dict:
    code = getattr(resp, "status_code", None)
    try:
        body = resp.json()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        body = {"__parse_error__": str(exc), "__text__": getattr(resp, "text", "")[:500]}
    print(f"\n--- {label} ---")
    print(f"HTTP {code}")
    print(json.dumps(body, ensure_ascii=False, indent=2)[:1200])
    return {"label": label, "http_status": code, "body": body}


def main() -> int:
    now = datetime.datetime.now(tz=JST)
    print(f"採取時刻: {now.isoformat()}（{'土日=閉場' if now.weekday() >= 5 else '平日'}）")

    api_key = os.environ["GMO_API_KEY"]
    api_secret = os.environ["GMO_API_SECRET"]
    records = []

    with GmoFxClient(api_key=api_key, api_secret=api_secret) as c:
        # Public（署名不要）
        records.append(_dump("GET /public/v1/status", c.public_get(PUBLIC_STATUS)))
        records.append(_dump("GET /public/v1/ticker", c.public_get(PUBLIC_TICKER, {"symbol": "USD_JPY"})))
        # Private（署名あり・すべて read-only）
        records.append(
            _dump("GET /private/v1/openPositions", c.private_get(PRIVATE_OPEN_POSITIONS, {"symbol": "USD_JPY"}))
        )
        records.append(
            _dump("GET /private/v1/activeOrders", c.private_get(PRIVATE_ACTIVE_ORDERS, {"symbol": "USD_JPY"}))
        )
        records.append(_dump("GET /private/v1/account/assets", c.private_get(PRIVATE_ACCOUNT_ASSETS)))

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_closed_responses.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"collected_at": now.isoformat(), "records": records}, f, ensure_ascii=False, indent=2)
    print(f"\n保存: {out}")

    # 判定サマリ
    print("\n" + "=" * 60)
    print("判定サマリ")
    print("=" * 60)
    st = next(r for r in records if r["label"].endswith("/v1/status"))
    market = (st["body"].get("data") or {}).get("status")
    print(f"  公式ステータス           : {market}")
    op = next(r for r in records if "openPositions" in r["label"])
    print(f"  openPositions HTTP/body  : {op['http_status']} / status={op['body'].get('status')}")
    print(f"  → 代理判定は閉場を検知できるか: "
          f"{'いいえ（素通り）' if op['body'].get('status') == 0 else 'はい'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
