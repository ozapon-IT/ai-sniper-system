#!/usr/bin/env python3
"""ERR-423 採取 + `orderedSize` 取消反映遅延の計測ドライバ.

Runbook: `docs/spikes/ordered_size_err423_latency.md`（付録B）
関連: `docs/spikes/cancel_first_official_spec.md` §4 / §6 / §8-1
既存ドライバ: `scripts/spike_live_order.py`（entry の作成＝phaseB はそちらを使う）

目的（技術仕様の実測のみ。損益・手法の評価はしない）:
  1. **実験1**: 決済 OCO が残ったまま（`orderedSize != "0"`）満額 `closeOrder` した時の
     実 `message_code` / `message_string` を採る。ERR-423 は 2026-08-03 時点で**未観測**。
  2. **実験2**: `cancelOrders` 受付成功（= t0）から建玉の `orderedSize == "0"` までの
     経過時間を**短い間隔でポーリング**して測る。既存 phaseD は `sleep(3)` 後に 1 回
     見るだけなので分布が測れていない。

安全設計:
  - **読み取り（GET のみ）と危険操作（発注系 POST）をサブコマンドで分離**する。
    `preflight` / `state` はリスクなし。
  - 危険サブコマンドは**明示フラグなしでは走らない**（`--yes-*`）。
  - 発注系 POST の実送信可否は判定を独自実装せず `GmoFxClient` の two-key interlock
    （`ALLOW_LIVE_ORDERS=true` かつ `TRADING_PHASE∈{2,spike}`）に委ねる（AGENTS.md #1）。
  - POST 間 ≥1 秒 / GET ≥167ms のレートリミットは `GmoFxClient` の RateLimiter が
    保証する（AGENTS.md #3）。本スクリプトのポーリング間隔はさらに保守的な下限
    `_MIN_POLL_INTERVAL_SECONDS` でクランプする。
  - **実験2 の手仕舞いは既定 ON**（フラグの付け忘れが最も危険な失敗になるため）。
    計測だけしたい場合は `--no-flatten` で明示的にオプトアウトする。
  - 危険サブコマンドは**終了時に必ず状態を再確認**する。flat で終わるはずのものは
    `verify_flat`、flat で終わらないもの（実験1）は `warn_not_flat` が
    「まだ終わっていない」と次に撃つコマンドを名指しする。復旧は `flatten`
    サブコマンド → それでも駄目なら GMO の Web / アプリから手動決済。**放置しない。**
  - API キーはログに出さない。識別用に末尾 4 桁のみ `mask()` で表示する（AGENTS.md #8）。

使い方（two-key を開けたシェルで。詳細な手順は上記 Runbook を参照）:
    python scripts/spike_ordered_size_latency.py preflight       # GET のみ
    python scripts/spike_ordered_size_latency.py state           # GET のみ（orderedSize 付き）
    # entry は既存ドライバで作る:
    #   python scripts/spike_live_order.py phaseB --side BUY --price 156.480
    python scripts/spike_ordered_size_latency.py exp1-close-without-cancel \\
        --position-id 123456789 --yes-close-without-cancel
    python scripts/spike_ordered_size_latency.py exp1-oversize \\
        --position-id 123456789 --yes-oversize
    # 実験2 は計測後にそのまま成行決済して flat へ戻す（--no-flatten で明示的に外せる）
    python scripts/spike_ordered_size_latency.py exp2-cancel-latency \\
        --root-order-id 123456789 --yes-cancel --out scratchpad/err423_exp2_cycle1.jsonl
    python scripts/spike_ordered_size_latency.py flatten --position-id 123456789
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

sys.path.insert(0, ".")

from backend.src.clients.gmo_client import (  # noqa: E402
    GmoFxClient,
    OrderBlockedError,
    live_orders_allowed,
)
from backend.src.gmo_errors import raise_for_gmo_status  # noqa: E402
from backend.src.gmo_fx_endpoints import (  # noqa: E402
    PRIVATE_ACTIVE_ORDERS,
    PRIVATE_CANCEL_ORDERS,
    PRIVATE_CLOSE_ORDER,
    PRIVATE_OPEN_POSITIONS,
    PUBLIC_STATUS,
    PUBLIC_TICKER,
)
from backend.src.lambda_auth import get_gmo_credentials  # noqa: E402
from scripts.spike_common import mask  # noqa: E402

SYMBOL = "USD_JPY"
SIZE = 100  # minOpenOrderSize（公式 symbols で確認済み・MVP_MODE と同じ最小ロット）

# 市場が開いていると判定する値（`GET /public/v1/status` の data.status・#207 (c)）。
# エンベロープの status（API 成否）と data.status（市場状態）は同名で別物。
MARKET_STATUS_OPEN = "OPEN"

# 🔴 ポーリング間隔の下限。公式 GET レートリミットは 6回/秒（=167ms）で、
# GmoFxClient の RateLimiter がその 167ms を強制する（AGENTS.md #3）。
# 本スクリプトは 1 サイクルにつき openPositions + activeOrders の **2 GET** を撃つため、
# 名目間隔がこれを下回ると実測値が「自分の待ち時間」に汚染される。
# 余裕を見て 250ms を下限とし（≒ 8 GET/秒相当の枠を 4 GET/秒しか使わない）、
# 既定は 500ms（= 4 GET/秒）にする。
_MIN_POLL_INTERVAL_SECONDS = 0.25
_DEFAULT_POLL_INTERVAL_SECONDS = 0.5
# 反映が来ないまま延々回さないための上限。既定 30 秒は本番実装
# `_ORDERED_SIZE_POLL_MAX_ATTEMPTS`（最大約 4 秒）の妥当性を判定するのに十分な幅。
_DEFAULT_POLL_TIMEOUT_SECONDS = 30.0

# flat 検証（危険サブコマンド終了時）。closeOrder は「受付」であり即時約定ではないため
# 1 回の GET では判定できない（cancel_first.py の _verify_close_with_poll と同じ理由）。
_FLAT_CHECK_ATTEMPTS = 5
_FLAT_CHECK_INTERVAL_SECONDS = 1.0


# ---------------------------------------------------------------------------
# 出力ヘルパ — 採取が目的なので全文を残しつつ、機械可読な行も併せて出す
# ---------------------------------------------------------------------------


class Recorder:
    """構造化レコードを stdout（`REC ` 接頭辞）と任意の JSONL ファイルへ書き出す."""

    def __init__(self, out_path: str | None) -> None:
        self._fh = None
        if out_path:
            parent = os.path.dirname(out_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._fh = open(out_path, "a", encoding="utf-8")  # noqa: SIM115
            print(f"[rec] JSONL 追記先: {out_path}")

    def emit(self, kind: str, **fields: Any) -> None:
        record = {"kind": kind, "wall_utc": _utc_now(), **fields}
        line = json.dumps(record, ensure_ascii=False)
        print(f"REC {line}")
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def _utc_now() -> str:
    """ローカル時刻に依存しない UTC ISO8601（GMO の responsetime と突合するため）."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def dump(label: str, resp: httpx.Response) -> dict[str, Any]:
    """レスポンスを全文表示し、パース結果を返す（採取用・要約しない）."""
    print(f"\n{'=' * 72}\n{label}\n{'=' * 72}")
    print(f"HTTP {resp.status_code}")
    try:
        body: dict[str, Any] = resp.json()
    except ValueError:
        print(f"(JSON パース不可) {resp.text}")
        return {}
    print(json.dumps(body, ensure_ascii=False, indent=2))
    if body.get("status") != 0:
        for m in body.get("messages", []) or []:
            print(f"\n  🔴 message_code   : {m.get('message_code')}")
            print(f"  🔴 message_string : {m.get('message_string')}")
    return body


def _messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    """業務エラーの messages 配列を取り出す（`message_code` は**原文のまま**記録する）."""
    return [m for m in (body.get("messages") or []) if isinstance(m, dict)]


def client() -> GmoFxClient:
    try:
        key, secret = get_gmo_credentials()
    except Exception as exc:  # noqa: BLE001 — 資格情報経路は多様なので理由を問わず案内する
        print(f"\n❌ GMO 資格情報を取得できません: {type(exc).__name__}: {exc}")
        print("\nローカル実行では以下のいずれかが必要です:")
        print("  (a) ローカル資格情報:")
        print("      export ALLOW_LOCAL_GMO_CREDENTIALS=true")
        print("      export GMO_API_KEY=...  GMO_API_SECRET=...   # 履歴に残すなら read -s を推奨")
        print("  (b) Secrets Manager 経由: export AWS_PROFILE=your-sso-profile AWS_REGION=ap-northeast-1")
        raise SystemExit(3) from exc
    # 🔴 シークレット本体は出さない。どの鍵を使っているかの識別だけ（AGENTS.md #8）
    print(f"[auth] GMO_API_KEY={mask(key)}")
    return GmoFxClient(api_key=key, api_secret=secret)


def guard_banner() -> None:
    allowed = live_orders_allowed()
    mark = "🔴 実発注が有効" if allowed else "🟢 封じ込め中（発注系 POST はブロックされる）"
    print(f"\n[two-key] {mark}")
    if not allowed:
        print("  → 危険サブコマンドを実行するには、あなたのシェルで以下が必要:")
        print("     export ALLOW_LIVE_ORDERS=true TRADING_PHASE=spike")
        print("  （.env の常設値は安全側のまま。プロセス内でのみ開けること）")


# ---------------------------------------------------------------------------
# 参照系（GET のみ・リスクなし）
# ---------------------------------------------------------------------------


def _get_list(c: GmoFxClient, path: str, context: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """`data.list` 形式の参照系 GET。**業務エラーを空リストに化けさせない**.

    🔴 GMO はエラーでも HTTP 200 + body `{"status": 1, ...}` を返す
    （`.claude/rules/gmo-api.md`）。素の `.json()` だけだと `data` が無く空リストになり、
    「建玉ゼロ＝ flat」「注文ゼロ＝取消反映済み」に化ける。本スパイクは
    まさにその 2 つを測るので、ここで落とす。
    """
    resp = c.private_get(path, {"symbol": SYMBOL})
    raise_for_gmo_status(resp, context=context)
    body: dict[str, Any] = resp.json()
    return list((body.get("data") or {}).get("list") or []), body


def _get_positions(c: GmoFxClient) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return _get_list(c, PRIVATE_OPEN_POSITIONS, "openPositions")


def _get_active_orders(c: GmoFxClient) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return _get_list(c, PRIVATE_ACTIVE_ORDERS, "activeOrders")


def _find_position(positions: list[dict[str, Any]], position_id: int) -> dict[str, Any] | None:
    for p in positions:
        try:
            if int(p.get("positionId")) == position_id:  # type: ignore[arg-type]
                return p
        except (TypeError, ValueError):
            continue
    return None


def _close_side_of(position: dict[str, Any]) -> str:
    """建玉から決済方向（反対売買）を導く。

    🔴 `--side` を人手で渡す設計（既存 phaseE）は、間違えると**建玉が増える**。
    ここでは openPositions が返した side から機械的に反転させる。
    """
    side = position.get("side")
    if side == "BUY":
        return "SELL"
    if side == "SELL":
        return "BUY"
    raise SystemExit(f"❌ 建玉の side が不正です: {side!r}（決済方向を決められない）")


def preflight(c: GmoFxClient, rec: Recorder) -> int:
    """開始条件の確認（GET のみ）。市場 OPEN・flat・スプレッドを見る."""
    print("\n### preflight: 開始条件の確認（GET のみ・リスクなし）")

    st = dump(f"P-1 GET {PUBLIC_STATUS}（市場状態）", c.public_get(PUBLIC_STATUS))
    market_status = str((st.get("data") or {}).get("status", ""))
    rec.emit("preflight_market_status", market_status=market_status, responsetime=st.get("responsetime"))

    pos_body = dump(f"P-2 GET {PRIVATE_OPEN_POSITIONS}", c.private_get(PRIVATE_OPEN_POSITIONS, {"symbol": SYMBOL}))
    ord_body = dump(f"P-3 GET {PRIVATE_ACTIVE_ORDERS}", c.private_get(PRIVATE_ACTIVE_ORDERS, {"symbol": SYMBOL}))
    tick = dump(f"P-4 GET {PUBLIC_TICKER}", c.public_get(PUBLIC_TICKER, {"symbol": SYMBOL}))

    # 🔴 GMO はエラーでも HTTP 200 + body status:1 を返す（.claude/rules/gmo-api.md）。
    #    これを見ないと「認証失敗＝ data なし」が「建玉ゼロ＝ flat」に化ける。
    for label, body in (("openPositions", pos_body), ("activeOrders", ord_body)):
        if body.get("status") != 0:
            print(f"\n❌ {label} が業務エラー（body status={body.get('status')}）。")
            print("   flat 判定に使えないので中止。署名パスが /v1/... か確認（AGENTS.md #2）。")
            return 1

    n_pos = len((pos_body.get("data") or {}).get("list") or [])
    n_ord = len((ord_body.get("data") or {}).get("list") or [])

    print(f"\n--- 判定 ---\n市場 data.status = {market_status!r} / 建玉 {n_pos} 件 / 注文 {n_ord} 件")
    if market_status != MARKET_STATUS_OPEN:
        print("❌ 市場が OPEN ではない。閉場中のスパイクは別問題（本 Runbook の対象外）。中止。")
        return 1
    if n_pos or n_ord:
        print("❌ flat から始められない。先に手仕舞ってから再実行すること（flatten サブコマンド）。中止。")
        return 1

    for t in tick.get("data") or []:
        if t.get("symbol") == SYMBOL:
            ask, bid = float(t["ask"]), float(t["bid"])
            spread = (ask - bid) / 0.01
            mid = (ask + bid) / 2
            print(f"現値 mid={mid:.4f} / ask={ask} / bid={bid} / spread={spread:.2f} pips")
            if spread > 3.0:
                print("❌ スプレッドが 3pips 超（ワイド帯）。約定が読めないので中止。")
                return 1
            print("\n✅ preflight 通過。entry は既存ドライバで作る:")
            print(f"     python scripts/spike_live_order.py phaseB --side BUY  --price {mid - 0.005:.3f}")
            print(f"     python scripts/spike_live_order.py phaseB --side SELL --price {mid + 0.005:.3f}")
            print("   約定後に `state` で positionId / rootOrderId / orderedSize を確認する。")
            return 0

    print("❌ ticker に対象シンボルが無い。中止。")
    return 1


def state(c: GmoFxClient, rec: Recorder) -> int:
    """建玉・注文の現状（GET のみ）。**`orderedSize` を明示表示する**のが既存 status との差."""
    positions, pos_body = _get_positions(c)
    orders, ord_body = _get_active_orders(c)

    print(f"\n{'=' * 72}\nstate: 建玉 {len(positions)} 件 / 注文 {len(orders)} 件\n{'=' * 72}")
    for p in positions:
        print(
            f"  建玉 positionId={p.get('positionId')} side={p.get('side')} "
            f"size={p.get('size')} orderedSize={p.get('orderedSize')!r} price={p.get('price')}"
        )
    for o in orders:
        print(
            f"  注文 rootOrderId={o.get('rootOrderId')} orderId={o.get('orderId')} "
            f"side={o.get('side')} settleType={o.get('settleType')} "
            f"executionType={o.get('executionType')} status={o.get('status')}"
        )

    rec.emit(
        "state",
        positions=[
            {
                "positionId": p.get("positionId"),
                "side": p.get("side"),
                "size": p.get("size"),
                "orderedSize": p.get("orderedSize"),
            }
            for p in positions
        ],
        active_orders_count=len(orders),
        root_order_ids=sorted({o.get("rootOrderId") for o in orders if o.get("rootOrderId") is not None}),
        positions_responsetime=pos_body.get("responsetime"),
        orders_responsetime=ord_body.get("responsetime"),
    )

    ordered_nonzero = [p for p in positions if str(p.get("orderedSize") or "0") not in ("0", "")]
    if positions and orders and ordered_nonzero:
        print("\n✅ [1次約定済み + 決済 OCO 残存 + orderedSize != 0] = 実験1 / 実験2 の開始状態")
        print("   実験1: exp1-close-without-cancel --position-id <id> --yes-close-without-cancel")
        print("   実験2: exp2-cancel-latency --root-order-id <id> --yes-cancel")
    elif positions and not orders:
        print("\n⚠️ 建玉のみ・注文なし。決済 OCO が無いので実験1/2 は成立しない（対照 close は可能）。")
    elif not positions and not orders:
        print("\n🟢 flat（建玉も注文もなし）")
    return 0


# ---------------------------------------------------------------------------
# 危険操作の共通ゲート
# ---------------------------------------------------------------------------


def _require_confirm(flag_value: bool, flag_name: str) -> None:
    if not flag_value:
        raise SystemExit(
            f"❌ このサブコマンドは危険操作です。実行するには {flag_name} を明示すること。\n"
            "   （Runbook: docs/spikes/ordered_size_err423_latency.md を先に読むこと）"
        )


def verify_flat(c: GmoFxClient, rec: Recorder) -> bool:
    """終了時の flat 検証。空でなければ**強く警告**して復旧手順を出す.

    closeOrder / cancelOrders の成功は「受付」であり即時反映ではないため、
    1 回の GET では判定できない。数秒ポーリングする。

    🔴 GET 自体が失敗した場合は **flat とみなさない**（見えない = 安全ではない）。
    """
    print(f"\n--- 終了時 flat 検証（最大 {_FLAT_CHECK_ATTEMPTS} 回 / {_FLAT_CHECK_INTERVAL_SECONDS}s 間隔）---")
    positions: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    for attempt in range(1, _FLAT_CHECK_ATTEMPTS + 1):
        try:
            positions, pos_body = _get_positions(c)
            orders, ord_body = _get_active_orders(c)
        except Exception as exc:  # noqa: BLE001 — 検証不能は「残っているかもしれない」側に倒す
            print(f"  [{attempt}] ❌ 状態を取得できない: {type(exc).__name__}: {exc}")
            rec.emit("flat_check_fetch_failed", attempt=attempt, error=f"{type(exc).__name__}: {exc}")
            if attempt < _FLAT_CHECK_ATTEMPTS:
                time.sleep(_FLAT_CHECK_INTERVAL_SECONDS)
                continue
            print("\n" + "!" * 72)
            print("🔴 flat かどうか**確認できなかった**。flat とみなしてはならない。")
            print("   GMO の Web / アプリで建玉・注文を必ず目視確認すること。")
            print("!" * 72)
            return False
        print(f"  [{attempt}] 建玉 {len(positions)} 件 / 注文 {len(orders)} 件")
        # positions / orders は別々の GET。片方だけ stale な可能性を事後に判定できるよう、
        # 件数と一緒に双方の responsetime を毎回残す（2026-08-11 レビュー対応）。
        rec.emit(
            "flat_check_poll",
            attempt=attempt,
            position_count=len(positions),
            active_orders_count=len(orders),
            positions_responsetime=pos_body.get("responsetime"),
            orders_responsetime=ord_body.get("responsetime"),
        )
        if not positions and not orders:
            rec.emit("flat_verified", attempts=attempt)
            print("🟢 flat 確認。")
            return True
        if attempt < _FLAT_CHECK_ATTEMPTS:
            time.sleep(_FLAT_CHECK_INTERVAL_SECONDS)

    rec.emit(
        "flat_violation",
        residual_position_ids=[p.get("positionId") for p in positions],
        residual_order_ids=[o.get("rootOrderId") for o in orders],
    )
    print("\n" + "!" * 72)
    print("🔴 flat になっていない。**放置しないこと。**")
    for p in positions:
        print(f"   残存建玉 positionId={p.get('positionId')} side={p.get('side')} size={p.get('size')} orderedSize={p.get('orderedSize')!r}")
    for o in orders:
        print(f"   残存注文 rootOrderId={o.get('rootOrderId')} settleType={o.get('settleType')} status={o.get('status')}")
    print("\n   復旧手順:")
    print("     1) 注文が残る  : python scripts/spike_live_order.py phaseD --root-order-id <rootOrderId>")
    print("     2) 建玉が残る  : python scripts/spike_ordered_size_latency.py flatten --position-id <positionId>")
    print("     3) それでも駄目: GMO の Web / アプリから手動決済する")
    print("!" * 72)
    return False


def _resolve_next_commands(
    template: str, *, root_order_ids: list[int], position_ids: list[int]
) -> list[str]:
    """次コマンドのプレースホルダを**実測した ID で埋める**（そのままコピペできる形にする）.

    事故処理の最中に `<rootOrderId>` を人が読み替える手間を残さない。
    埋める材料が無ければテンプレートのまま返す（嘘の ID を出さない）。
    """
    if "<rootOrderId>" in template:
        ids = root_order_ids
        token = "<rootOrderId>"
    elif "<positionId>" in template:
        ids = position_ids
        token = "<positionId>"
    else:
        return [template]
    if not ids:
        return [template]
    return [template.replace(token, str(i)) for i in ids]


def warn_not_flat(
    c: GmoFxClient, rec: Recorder, *, next_command: str, extra: str | None = None
) -> None:
    """**flat で終わらないサブコマンドの締め**。残存を数え、次に撃つコマンドを名指しする.

    実験1 は「拒否されれば OCO 残存」「`status:0` なら決済進行中」で、どちらも
    flat では終わらない（それが実験の前提）。`state` の一覧だけだと
    「まだ終わっていない」ことが埋もれるため、最後にもう一段強く出す。

    `next_command` の `<rootOrderId>` / `<positionId>` は**実測 ID で埋めて**出力する。
    """
    try:
        positions, pos_body = _get_positions(c)
        orders, ord_body = _get_active_orders(c)
    except Exception as exc:  # noqa: BLE001 — 見えないなら安全側（残っている前提）に倒す
        rec.emit("not_flat_check_failed", error=f"{type(exc).__name__}: {exc}")
        print("\n" + "!" * 72)
        print(f"🔴 残存を確認できなかった（{type(exc).__name__}）。flat とみなさないこと。")
        print("   GMO の Web / アプリで建玉・注文を目視確認する。")
        print("!" * 72)
        return

    if not positions and not orders:
        rec.emit(
            "not_flat_check_clear",
            positions_responsetime=pos_body.get("responsetime"),
            orders_responsetime=ord_body.get("responsetime"),
        )
        print("\n🟢 建玉も注文も残っていない（このセッションは終了してよい）。")
        return

    residual_position_ids = [
        i for i in (_safe_int(p.get("positionId")) for p in positions) if i is not None
    ]
    residual_root_order_ids = sorted(
        {i for i in (_safe_int(o.get("rootOrderId")) for o in orders) if i is not None}
    )
    commands = _resolve_next_commands(
        next_command,
        root_order_ids=residual_root_order_ids,
        position_ids=residual_position_ids,
    )

    rec.emit(
        "not_flat",
        residual_position_ids=residual_position_ids,
        residual_root_order_ids=residual_root_order_ids,
        next_commands=commands,
        # 🔴 openPositions と activeOrders は**別々の GET**（約150ms 差）で、しかも
        #    GET は POST より前の responsetime を返すことがある（2026-08-11 実測）。
        #    「建玉0 / 注文残」を見たとき、真のラグか片方が stale かは
        #    この 2 つの responsetime を突き合わせないと判定できない。
        #    サイクル2 でこれを記録しておらず、副次発見1 を未確定に落とす羽目になった。
        positions_responsetime=pos_body.get("responsetime"),
        orders_responsetime=ord_body.get("responsetime"),
    )
    print("\n" + "!" * 72)
    print(f"🔴 **まだ flat ではない**（建玉 {len(positions)} 件 / 注文 {len(orders)} 件）。放置しないこと。")
    if extra:
        print(f"   {extra}")
    print("\n   次に実行する:")
    for command in commands:
        print(f"     python scripts/spike_ordered_size_latency.py {command}")
    print("\n   駄目なら: GMO の Web / アプリから手動決済する")
    print("!" * 72)


# ---------------------------------------------------------------------------
# 実験1 — ERR-423（有効注文枠）採取
# ---------------------------------------------------------------------------


def exp1_close_without_cancel(c: GmoFxClient, rec: Recorder, position_id: int) -> int:
    """決済 OCO を**取り消さずに**満額 close する（ERR-423 の後半条件を狙う）.

    🔴 期待は ERR-423 だが**決め打ちしない**。返ってきた message_code を一次情報とする。
    🔴 成功（status:0）してしまった場合は建玉が決済され得る。その場合も flat 検証まで走る。
    """
    print("\n### 実験1: 決済 OCO 残存のまま満額 closeOrder（ERR-423 の後半条件狙い）")

    positions, _ = _get_positions(c)
    position = _find_position(positions, position_id)
    if position is None:
        print(f"❌ positionId={position_id} が openPositions に無い。中止。")
        return 1

    ordered_size = str(position.get("orderedSize") or "")
    size = str(position.get("size") or "")
    close_side = _close_side_of(position)
    orders, _ = _get_active_orders(c)

    print(f"\n対象建玉: positionId={position_id} side={position.get('side')} size={size!r} orderedSize={ordered_size!r}")
    print(f"決済方向（自動導出）: {close_side} / 同時点の activeOrders {len(orders)} 件")
    if ordered_size in ("", "0"):
        print("\n❌ orderedSize が 0。**この状態では ERR-423 の後半条件に触れない**（対照実験になる）。")
        print("   決済 OCO が生きている状態で実行すること。中止。")
        return 1

    rec.emit(
        "exp1_precondition",
        position_id=position_id,
        size=size,
        ordered_size=ordered_size,
        close_side=close_side,
        active_orders_count=len(orders),
    )

    body = dump(
        f"E1 POST {PRIVATE_CLOSE_ORDER}（cancel せず満額 close・settlePosition のみ / トップレベル size なし）",
        c.private_post(
            PRIVATE_CLOSE_ORDER,
            {
                "symbol": SYMBOL,
                "side": close_side,
                "executionType": "MARKET",
                "settlePosition": [{"positionId": position_id, "size": size}],
            },
        ),
    )
    rec.emit(
        "exp1_close_without_cancel_response",
        position_id=position_id,
        size=size,
        ordered_size_before=ordered_size,
        status=body.get("status"),
        messages=_messages(body),
        responsetime=body.get("responsetime"),
        data=body.get("data"),
    )

    print("\n--- 記録先 ---")
    print("message_code / message_string を docs/spikes/ordered_size_err423_latency.md 実験1 結果欄へ")
    if body.get("status") == 0:
        print("⚠️ status:0 で**受付された**。ERR-423 は出なかった（＝有効注文枠の制約に触れていない）。")
        print("   建玉が決済されている可能性が高い。この事実をそのまま記録すること。")
    else:
        codes = [m.get("message_code") for m in _messages(body)]
        print(f"取得した message_code: {codes}（ERR-423 でなくても**そのまま**記録する）")

    print("\n次: 決済 OCO を取り消して flat へ戻す（手仕舞いは既定 ON）")
    print("   python scripts/spike_ordered_size_latency.py exp2-cancel-latency --root-order-id <id> --yes-cancel")
    return 0


def exp1_oversize(c: GmoFxClient, rec: Recorder, position_id: int) -> int:
    """対照: 建玉数量を超える size を送る（2026-08-03 実測は ERR-189）.

    実験1 の ERR-423 と**別コード**であることを同一セッションで確認するための対照。
    建玉は動かない想定（拒否されるため）。
    """
    print("\n### 実験1-対照: 数量超過 close（前半条件・2026-08-03 実測は ERR-189）")

    positions, _ = _get_positions(c)
    position = _find_position(positions, position_id)
    if position is None:
        print(f"❌ positionId={position_id} が openPositions に無い。中止。")
        return 1
    close_side = _close_side_of(position)
    size = str(position.get("size") or "")
    oversize = str(int(size) * 2) if size.isdigit() else str(SIZE * 2)

    body = dump(
        f"E1c POST {PRIVATE_CLOSE_ORDER}（size={oversize} vs 建玉 {size}）",
        c.private_post(
            PRIVATE_CLOSE_ORDER,
            {
                "symbol": SYMBOL,
                "side": close_side,
                "executionType": "MARKET",
                "settlePosition": [{"positionId": position_id, "size": oversize}],
            },
        ),
    )
    rec.emit(
        "exp1_oversize_response",
        position_id=position_id,
        size=size,
        sent_size=oversize,
        ordered_size=position.get("orderedSize"),
        status=body.get("status"),
        messages=_messages(body),
        responsetime=body.get("responsetime"),
    )
    print("\n⚠️ status:0 で通った場合は建玉が動いている。`state` で必ず確認すること。")
    return 0


# ---------------------------------------------------------------------------
# 実験2 — 取消反映遅延の計測
# ---------------------------------------------------------------------------


def exp2_cancel_latency(
    c: GmoFxClient,
    rec: Recorder,
    root_order_id: int,
    *,
    interval: float,
    timeout: float,
    then_flatten: bool,
) -> int:
    """`cancelOrders` 受付（t0）→ `orderedSize == "0"` までの経過秒を測る.

    既存 phaseD（`sleep(3)` 後に 1 回 GET）との差:
      - 固定 sleep をやめ、**短い間隔でポーリング**して分布を測る
      - `openPositions.orderedSize` と `activeOrders` 件数を**両方**追い、乖離を見る
      - 各応答の `responsetime`（サーバ時刻）とローカル `time.monotonic()` を併記する
    """
    interval = max(interval, _MIN_POLL_INTERVAL_SECONDS)
    print(f"\n### 実験2: 取消反映遅延の計測（間隔 {interval:.3f}s / 上限 {timeout:.1f}s）")

    positions_before, _ = _get_positions(c)
    orders_before, _ = _get_active_orders(c)
    target_ids = [
        int(p["positionId"]) for p in positions_before if p.get("positionId") is not None
    ]
    if not target_ids:
        print("❌ 建玉が無い。決済 OCO 残存状態を作ってから実行すること。中止。")
        return 1

    print(f"\n取消前: 建玉 {len(positions_before)} 件 / 注文 {len(orders_before)} 件")
    for p in positions_before:
        print(f"  positionId={p.get('positionId')} size={p.get('size')} orderedSize={p.get('orderedSize')!r}")
    rec.emit(
        "exp2_before",
        root_order_id=root_order_id,
        positions=[
            {"positionId": p.get("positionId"), "size": p.get("size"), "orderedSize": p.get("orderedSize")}
            for p in positions_before
        ],
        active_orders_count=len(orders_before),
        poll_interval_seconds=interval,
        poll_timeout_seconds=timeout,
    )

    # --- t0: cancelOrders の応答を受け取った瞬間 -------------------------------
    resp = c.private_post(PRIVATE_CANCEL_ORDERS, {"rootOrderIds": [root_order_id]})
    t0 = time.monotonic()
    cancel_body = dump(f"E2 POST {PRIVATE_CANCEL_ORDERS} rootOrderIds=[{root_order_id}]", resp)
    rec.emit(
        "exp2_cancel_accepted",
        root_order_id=root_order_id,
        status=cancel_body.get("status"),
        messages=_messages(cancel_body),
        responsetime=cancel_body.get("responsetime"),
        data=cancel_body.get("data"),
    )
    if cancel_body.get("status") != 0:
        print("\n❌ cancelOrders が受付されなかった。t0 が定義できないので計測中止。")
        print("   建玉と注文が残っているので `state` で確認し、必要なら flatten すること。")
        verify_flat(c, rec)
        return 1

    # --- ポーリング -----------------------------------------------------------
    print("\n--- ポーリング開始（t0 = cancelOrders 応答受信時刻）---")
    ordered_size_zero_at: float | None = None
    active_orders_empty_at: float | None = None
    # 「POST 直後 1 発目の GET が取消前のままか」（2026-08-03 の事象の再現確認）。
    # 経過秒ではなく **1 発目の観測そのもの**で判定する。
    # 🔴 1 発目で建玉ごと消えていた（TP/SL 約定・決済済み）ケースは「反映が遅い」ではなく
    #    **測定不能**。混同すると stale=True が水増しされるので別フィールドで区別する。
    first_poll_ordered_size_zero: bool | None = None
    first_poll_active_orders: int | None = None
    first_poll_vanished: bool | None = None
    attempt = 0

    while True:
        attempt += 1
        try:
            positions, pos_body = _get_positions(c)
            t_pos = time.monotonic() - t0
            orders, ord_body = _get_active_orders(c)
            t_ord = time.monotonic() - t0
        except Exception as exc:  # noqa: BLE001
            # 🔴 計測は諦めるが、**手仕舞い（既定 ON）は必ず通す**。
            #    ここで例外を投げ上げると SL/TP を消した建玉が裸のまま残る。
            print(f"\n❌ ポーリング中に状態を取得できなくなった: {type(exc).__name__}: {exc}")
            rec.emit("exp2_poll_failed", attempt=attempt, error=f"{type(exc).__name__}: {exc}")
            break

        tracked = [p for p in positions if _safe_int(p.get("positionId")) in target_ids]
        ordered_sizes = {str(p.get("positionId")): str(p.get("orderedSize") or "") for p in tracked}
        all_zero = bool(tracked) and all(v in ("0", "") for v in ordered_sizes.values())
        # 建玉ごと消えた場合（決済済み）は「有効注文枠の観測対象が消えた」ので測定不能
        vanished = not tracked

        if attempt == 1:
            first_poll_ordered_size_zero = all_zero
            first_poll_active_orders = len(orders)
            first_poll_vanished = vanished
        if all_zero and ordered_size_zero_at is None:
            ordered_size_zero_at = t_pos
        if not orders and active_orders_empty_at is None:
            active_orders_empty_at = t_ord

        rec.emit(
            "exp2_poll",
            attempt=attempt,
            t_positions=round(t_pos, 4),
            t_active_orders=round(t_ord, 4),
            ordered_sizes=ordered_sizes,
            positions_vanished=vanished,
            active_orders_count=len(orders),
            positions_responsetime=pos_body.get("responsetime"),
            orders_responsetime=ord_body.get("responsetime"),
        )
        print(
            f"  [{attempt:3d}] t={t_pos:6.3f}s orderedSize={ordered_sizes} "
            f"activeOrders={len(orders)} (t={t_ord:6.3f}s)"
        )

        if vanished:
            print("\n⚠️ 追跡対象の建玉が消えた（決済済み）。orderedSize の遅延は測定できない。")
            break
        if ordered_size_zero_at is not None and active_orders_empty_at is not None:
            break
        if time.monotonic() - t0 >= timeout:
            print(f"\n⚠️ タイムアウト（{timeout:.1f}s）。反映が観測できなかった。")
            break
        time.sleep(interval)

    result = {
        "root_order_id": root_order_id,
        "attempts": attempt,
        "poll_interval_seconds": interval,
        "ordered_size_zero_seconds": round(ordered_size_zero_at, 4) if ordered_size_zero_at is not None else None,
        "active_orders_empty_seconds": round(active_orders_empty_at, 4) if active_orders_empty_at is not None else None,
        # True = 1 発目の GET でまだ取消が反映されていなかった（= 遅延あり）。
        # 🔴 建玉が消えていた場合（first_poll_vanished）は「遅延」ではなく**測定不能**なので
        #    stale には数えない。両者を混同すると遅延の再現率を水増しする。
        "first_poll_still_stale": (
            first_poll_ordered_size_zero is False and first_poll_vanished is False
        ),
        "first_poll_vanished": first_poll_vanished,
        "first_poll_active_orders_count": first_poll_active_orders,
        # 建玉が途中で消えた（TP/SL 約定など）＝ orderedSize の遅延は測れていない
        "measurable": ordered_size_zero_at is not None,
    }
    rec.emit("exp2_result", **result)

    print("\n--- 実験2 の結果 ---")
    print(f"  t0 → orderedSize == 0    : {result['ordered_size_zero_seconds']} 秒")
    print(f"  t0 → activeOrders 空     : {result['active_orders_empty_seconds']} 秒")
    print(f"  1 発目の GET が取消前か  : {result['first_poll_still_stale']}"
          f"（activeOrders {first_poll_active_orders} 件 / 建玉消失={first_poll_vanished}）")
    print(f"  ポーリング {attempt} 回 / 間隔 {interval:.3f}s")
    if not result["measurable"]:
        print("  ⚠️ orderedSize == 0 を観測できていない（建玉消失 or タイムアウト）＝ **このサイクルは無効**。")
        print("     結果表には『測定不能』として記録し、遅延サンプルには数えないこと。")
    print("  → docs/spikes/ordered_size_err423_latency.md 実験2 の表へ 1 行追記すること")

    if then_flatten:
        print("\n--- 手仕舞い: 建玉を成行決済して flat へ戻す ---")
        for position_id in target_ids:
            # 1 件失敗しても残りを撃つ。**裸の建玉を残さない**ことが最優先。
            try:
                flatten(c, rec, position_id, verify=False)
            except Exception as exc:  # noqa: BLE001
                print(f"❌ positionId={position_id} の決済に失敗: {type(exc).__name__}: {exc}")
                rec.emit("flatten_failed", position_id=position_id, error=f"{type(exc).__name__}: {exc}")
    return 0


def _safe_int(raw: Any) -> int | None:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# flatten — 復旧経路（建玉を成行決済して flat へ戻す）
# ---------------------------------------------------------------------------


def flatten(c: GmoFxClient, rec: Recorder, position_id: int, *, verify: bool = True) -> int:
    """指定建玉を成行決済する。決済方向は openPositions から自動導出する.

    `--position-id` の明示指定そのものがゲート。復旧経路なので追加フラグは要求しない
    （事故時に素早く撃てることを優先する）。
    """
    positions, _ = _get_positions(c)
    position = _find_position(positions, position_id)
    if position is None:
        print(f"🟢 positionId={position_id} は openPositions に無い（既に決済済み）。")
        return 0

    close_side = _close_side_of(position)
    size = str(position.get("size") or "")
    ordered_size = str(position.get("orderedSize") or "")
    if ordered_size not in ("", "0"):
        print(f"⚠️ orderedSize={ordered_size!r} が残っている。ERR-423 で弾かれる可能性がある。")
        print("   先に決済 OCO を取り消すこと: spike_live_order.py phaseD --root-order-id <id>")

    try:
        body = dump(
            f"FLATTEN POST {PRIVATE_CLOSE_ORDER} positionId={position_id} side={close_side} size={size}",
            c.private_post(
                PRIVATE_CLOSE_ORDER,
                {
                    "symbol": SYMBOL,
                    "side": close_side,
                    "executionType": "MARKET",
                    "settlePosition": [{"positionId": position_id, "size": size}],
                },
            ),
        )
    except Exception as exc:  # noqa: BLE001 — POST 失敗でも flat 検証まで必ず到達させる
        print(f"\n❌ closeOrder の送信に失敗: {type(exc).__name__}: {exc}")
        rec.emit("flatten_post_failed", position_id=position_id, error=f"{type(exc).__name__}: {exc}")
        # 🔴 再送はしない（受付済みかもしれない・#207 L1 と同じ判断軸）。状態を見てから人が判断する。
        if verify:
            verify_flat(c, rec)
        return 1

    rec.emit(
        "flatten_response",
        position_id=position_id,
        close_side=close_side,
        size=size,
        ordered_size=ordered_size,
        status=body.get("status"),
        messages=_messages(body),
        responsetime=body.get("responsetime"),
    )
    print("\n⚠️ closeOrder の成功は status:WAITING の『受付』であり約定ではない（§6.2）。")
    if verify:
        verify_flat(c, rec)
    return 0


# ---------------------------------------------------------------------------


_DANGEROUS = {"exp1-close-without-cancel", "exp1-oversize", "exp2-cancel-latency", "flatten"}


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "command",
        choices=["preflight", "state", "exp1-close-without-cancel", "exp1-oversize", "exp2-cancel-latency", "flatten"],
    )
    p.add_argument("--position-id", type=int, help="対象建玉ID（実験1 / flatten）")
    p.add_argument("--root-order-id", type=int, help="取消する親注文ID（実験2）")
    p.add_argument(
        "--interval",
        type=float,
        default=_DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"実験2 のポーリング間隔（秒）。下限 {_MIN_POLL_INTERVAL_SECONDS}s にクランプ（GET 6回/秒・AGENTS.md #3）",
    )
    p.add_argument("--timeout", type=float, default=_DEFAULT_POLL_TIMEOUT_SECONDS, help="実験2 のポーリング上限（秒）")
    p.add_argument("--out", help="構造化レコード（JSONL）の追記先ファイル")
    # 🔴 手仕舞いは**既定 ON**。実験2 は cancel した瞬間から建玉が SL/TP なしで裸になるので、
    #    「フラグの付け忘れ」が最も危険な失敗になる。安全側を既定にし、外すほうを明示させる。
    p.add_argument(
        "--no-flatten",
        action="store_true",
        help="🔴 実験2 の完了後に手仕舞いしない（建玉が SL/TP なしで残る）。計測だけしたい場合の明示オプトアウト",
    )
    # 危険操作の明示フラグ（既定 False。付け忘れたら走らない）
    p.add_argument("--yes-close-without-cancel", action="store_true", help="実験1 を実行する明示同意")
    p.add_argument("--yes-oversize", action="store_true", help="実験1-対照（数量超過）を実行する明示同意")
    p.add_argument("--yes-cancel", action="store_true", help="実験2（決済 OCO を取り消す）を実行する明示同意")
    a = p.parse_args()

    guard_banner()

    # 🔴 ゲートは**ネットワークにも資格情報にも触れる前**に全部通す。
    #    「危険フラグを付け忘れたが、資格情報エラーで落ちただけ」を見分けられなくしない。
    if a.command in _DANGEROUS and not live_orders_allowed():
        print("\n❌ two-key が開いていないため危険サブコマンドは実行できない（発注系 POST は遮断される）。")
        return 2
    if a.command == "exp1-close-without-cancel":
        _require_confirm(a.yes_close_without_cancel, "--yes-close-without-cancel")
    if a.command == "exp1-oversize":
        _require_confirm(a.yes_oversize, "--yes-oversize")
    if a.command == "exp2-cancel-latency":
        _require_confirm(a.yes_cancel, "--yes-cancel")
    if a.command in {"exp1-close-without-cancel", "exp1-oversize", "flatten"} and a.position_id is None:
        p.error(f"{a.command} には --position-id が必要")
    if a.command == "exp2-cancel-latency" and a.root_order_id is None:
        p.error("exp2-cancel-latency には --root-order-id が必要")

    rec = Recorder(a.out)
    c = client()
    try:
        if a.command == "preflight":
            return preflight(c, rec)
        if a.command == "state":
            return state(c, rec)

        if a.command == "exp1-close-without-cancel":
            code = exp1_close_without_cancel(c, rec, a.position_id)
            state(c, rec)
            warn_not_flat(c, rec, next_command="exp2-cancel-latency --root-order-id <rootOrderId> --yes-cancel")
            return code

        if a.command == "exp1-oversize":
            code = exp1_oversize(c, rec, a.position_id)
            state(c, rec)
            warn_not_flat(c, rec, next_command="exp2-cancel-latency --root-order-id <rootOrderId> --yes-cancel")
            return code

        if a.command == "exp2-cancel-latency":
            then_flatten = not a.no_flatten
            code = exp2_cancel_latency(
                c,
                rec,
                a.root_order_id,
                interval=a.interval,
                timeout=a.timeout,
                then_flatten=then_flatten,
            )
            if then_flatten:
                verify_flat(c, rec)
            else:
                state(c, rec)
                warn_not_flat(
                    c,
                    rec,
                    next_command="flatten --position-id <positionId>",
                    extra="🔴 --no-flatten を使ったので建玉は SL/TP なしで残っている。**いま**手仕舞うこと。",
                )
            return code

        if a.command == "flatten":
            return flatten(c, rec, a.position_id)
    except OrderBlockedError as e:
        print(f"\n🟢 発注はブロックされました（封じ込め動作は正常）:\n  {e}")
        return 2
    finally:
        rec.close()
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
