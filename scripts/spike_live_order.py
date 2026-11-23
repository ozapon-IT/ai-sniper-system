#!/usr/bin/env python3
"""実発注スパイク ドライバ — #91 / #235 / #236 を1セッションで採取する.

続編スパイク: `docs/spikes/ordered_size_err423_latency.md` / `scripts/spike_ordered_size_latency.py`
  - ERR-423（**後半条件**＝決済 OCO が有効注文枠を押さえたまま満額 close）の実採取
  - `cancelOrders` の取消反映遅延の**分布**計測（下記 Phase D の `sleep(3)` + 1 回 GET では測れない）
  entry の作成（phaseB）と残存注文の取消（phaseD）は引き続き本スクリプトを使う。

安全設計:
  - **フェーズを1つずつ手動で実行する。** 自動で次へ進まない（連鎖実行しない）。
  - 発注系 POST は two-key interlock（ALLOW_LIVE_ORDERS=true かつ TRADING_PHASE∈{2,spike}）を
    満たすシェルでのみ実送信される。満たさなければ GmoFxClient が OrderBlockedError を送出する
    （AGENTS.md #1）。本スクリプトは判定を独自実装せず client 側に委ねる。
  - 全レスポンスを整形して標準出力に出す。**採取が目的なので要約せず全文を残すこと。**
  - POST 間のレートリミット（≥1秒）は GmoFxClient の RateLimiter が保証する（AGENTS.md #3）。

使い方（あなたのシェルで two-key を開けた状態で）:
    python scripts/spike_live_order.py phase0     # GET のみ・リスクなし
    python scripts/spike_live_order.py phaseA     # 無効ボディ probe（拒否される想定）
    python scripts/spike_live_order.py phaseB --price 156.480 --side BUY
    # SL レグの実約定を狙う（#288 タスク1）: SL を絞り TP を遠ざける
    python scripts/spike_live_order.py phaseB --price 156.480 --side BUY --sl-pips 5 --tp-pips 40
    python scripts/spike_live_order.py status     # いつでも建玉/注文を確認
    python scripts/spike_live_order.py phaseC --position-id 123456789
    python scripts/spike_live_order.py phaseD --root-order-id 123456789
    python scripts/spike_live_order.py phaseE --position-id 123456789
"""

from __future__ import annotations

import argparse
import json
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
from backend.src.gmo_fx_endpoints import (  # noqa: E402
    PRIVATE_ACCOUNT_ASSETS,
    PRIVATE_ACTIVE_ORDERS,
    PRIVATE_CANCEL_ORDERS,
    PRIVATE_CLOSE_ORDER,
    PRIVATE_IFO_ORDER,
    PRIVATE_OPEN_POSITIONS,
    PUBLIC_TICKER,
)
from backend.src.lambda_auth import get_gmo_credentials  # noqa: E402

SYMBOL = "USD_JPY"
SIZE = 100  # minOpenOrderSize（公式 symbols で確認済み）

# USD/JPY の 1 pip = 0.01 円（tick は 0.001 = 0.1pip・E05 確定）。
_PIP = 0.01
# phaseB の TP/SL 既定幅。従来のハードコード ±0.400 円 = ±40pips と同値（挙動を変えない）。
_DEFAULT_TP_PIPS = 40.0
_DEFAULT_SL_PIPS = 40.0
# これを下回る SL 幅は「約定直後のノイズで即発火し得る」と警告する閾値。
_TIGHT_SL_WARN_PIPS = 10.0


# ---------------------------------------------------------------------------
# 出力ヘルパ — 採取が目的なので必ず全文を出す
# ---------------------------------------------------------------------------


def dump(label: str, resp: httpx.Response) -> dict[str, Any]:
    """レスポンスを全文表示し、パース結果を返す（採取用）。"""
    print(f"\n{'=' * 72}\n{label}\n{'=' * 72}")
    print(f"HTTP {resp.status_code}")
    try:
        body = resp.json()
        print(json.dumps(body, ensure_ascii=False, indent=2))
    except ValueError:
        print(f"(JSON パース不可) {resp.text}")
        return {}
    status = body.get("status")
    if status != 0:
        for m in body.get("messages", []):
            print(f"\n  🔴 message_code   : {m.get('message_code')}")
            print(f"  🔴 message_string : {m.get('message_string')}")
    return body


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
    return GmoFxClient(api_key=key, api_secret=secret)


def guard_banner() -> None:
    allowed = live_orders_allowed()
    mark = "🔴 実発注が有効" if allowed else "🟢 封じ込め中（発注系 POST はブロックされる）"
    print(f"\n[two-key] {mark}")
    if not allowed:
        print("  → 発注系フェーズを実行するには、あなたのシェルで以下が必要:")
        print("     export ALLOW_LIVE_ORDERS=true TRADING_PHASE=spike")


# ---------------------------------------------------------------------------
# Phase 0 — 事前確認（GET のみ）
# ---------------------------------------------------------------------------


def phase0(c: GmoFxClient) -> None:
    print("\n### Phase 0: 事前確認（GET のみ・リスクなし）")

    body = dump(f"0-1 GET {PRIVATE_ACCOUNT_ASSETS}（認証・署名パス確認）", c.private_get(PRIVATE_ACCOUNT_ASSETS))
    if body.get("status") != 0:
        print("\n❌ 認証に失敗。署名パスが /v1/... になっているか確認（AGENTS.md #2）。中止。")
        return

    pos = dump(f"0-2 GET {PRIVATE_OPEN_POSITIONS}（建玉が空か）", c.private_get(PRIVATE_OPEN_POSITIONS, {"symbol": SYMBOL}))
    ord_ = dump(f"0-3 GET {PRIVATE_ACTIVE_ORDERS}（注文が空か）", c.private_get(PRIVATE_ACTIVE_ORDERS, {"symbol": SYMBOL}))
    tick = dump(f"0-4 GET {PUBLIC_TICKER}（現値・スプレッド）", c.public_get(PUBLIC_TICKER, {"symbol": SYMBOL}))

    n_pos = len((pos.get("data") or {}).get("list", []) or [])
    n_ord = len((ord_.get("data") or {}).get("list", []) or [])
    print(f"\n--- 判定 ---\n建玉 {n_pos} 件 / 注文 {n_ord} 件")
    if n_pos or n_ord:
        print("❌ クリーンな状態から始められない。先に手仕舞ってから再実行すること。中止。")
        return

    for t in (tick.get("data") or []):
        if t.get("symbol") == SYMBOL:
            ask, bid = float(t["ask"]), float(t["bid"])
            spread = (ask - bid) / 0.01
            mid = (ask + bid) / 2
            print(f"現値 mid={mid:.4f} / ask={ask} / bid={bid} / spread={spread:.2f} pips")
            if spread > 3.0:
                print("❌ スプレッドが 3pips 超（ワイド帯）。中止。")
                return
            print("\n✅ Phase 0 通過。Phase B の entry 候補:")
            print(f"     BUY  なら現値のすぐ下: {mid - 0.005:.3f} 付近（下ヒゲで約定）")
            print(f"     SELL なら現値のすぐ上: {mid + 0.005:.3f} 付近（上ヒゲで約定）")


# ---------------------------------------------------------------------------
# Phase A — 無効ボディ probe（#91 のエラーコード採取）
# ---------------------------------------------------------------------------


def _ifo_body(side: str, first_price: str, tp: str, sl: str) -> dict[str, Any]:
    """ワイヤー形式（camelCase）。probe では意図的に不正な *価格* を渡す。

    🔴 `firstSize` / `secondSize` は **整数文字列**（"100"）。JSON number で送ると
    ERR-5105「Request parameter include mismatch type」で価格検証の手前に短絡する
    （2026-08-03 スパイクで実測。E06 記録 docs/spikes/E06_ifoOrder_mapping.md と一致）。
    本番 `IfoOrderRequest` も同じ誤りを持っていたが `field_serializer` で修正済み。
    """
    return {
        "symbol": SYMBOL,
        "firstSide": side,
        "firstExecutionType": "LIMIT",
        "firstPrice": first_price,
        "firstSize": str(SIZE),
        "secondSize": str(SIZE),
        "secondLimitPrice": tp,
        "secondStopPrice": sl,
    }


def phase_a(c: GmoFxClient) -> None:
    print("\n### Phase A: 無効ボディ probe（注文は作られない想定）")
    print("目的: 価格序列違反 / 最小乖離違反のエラーコードを採る（#91）")

    tick = c.public_get(PUBLIC_TICKER, {"symbol": SYMBOL}).json()
    mid = next((float(t["ask"]) + float(t["bid"])) / 2 for t in tick["data"] if t["symbol"] == SYMBOL)
    print(f"\n現値 mid = {mid:.4f}")

    # A-1: 価格序列違反 — BUY なのに TP が entry より下（LONG は SL < entry < TP が正）
    dump(
        "A-1 価格序列違反（BUY で TP < entry）",
        c.private_post(
            PRIVATE_IFO_ORDER,
            _ifo_body("BUY", f"{mid - 0.500:.3f}", f"{mid - 1.000:.3f}", f"{mid - 1.500:.3f}"),
        ),
    )

    # A-2: 最小乖離違反 — entry を現値に貼り付ける
    dump(
        "A-2 最小乖離違反（entry を現値に貼り付け）",
        c.private_post(
            PRIVATE_IFO_ORDER,
            _ifo_body("BUY", f"{mid:.3f}", f"{mid + 0.500:.3f}", f"{mid - 0.500:.3f}"),
        ),
    )

    print("\n--- 記録先 ---")
    print("上記 message_code / message_string を docs/spikes/E06_ifoOrder_mapping.md §4 へ")
    print("⚠️ どちらかが status:0 で通ってしまった場合は注文が実在する。status で確認し即キャンセルすること。")


# ---------------------------------------------------------------------------
# Phase B — 実発注（#91 の成功レスポンス採取）
# ---------------------------------------------------------------------------


def phase_b(
    c: GmoFxClient,
    side: str,
    price: float,
    tp_pips: float = _DEFAULT_TP_PIPS,
    sl_pips: float = _DEFAULT_SL_PIPS,
) -> None:
    """IFD-OCO を 1 件出す。TP/SL 幅は pips 指定できる（既定は従来どおり ±40pips）。

    🔴 **SL 幅を絞れるようにした理由**（Issue #288 タスク1）:
    実発注の初期段階では、**SL が実際に約定するのを一度も見ていなかった**。SL 40pips のままでは発火まで平均で数時間かかり、しかも同じ幅の TP が
    先に当たる確率が同程度あるため、**SL を狙い撃ちできない**。
    SL を数 pips に絞り TP を遠ざけることで、SL レグの実約定を短時間で確定させる。

    サーバ側に最小乖離の検証は無い（E06 §4-3 実測・`USD_JPY_MIN_ENTRY_DISTANCE = 0.003`
    はクライアント側だけの制約）ため、タイトな SL でも GMO は受け付ける。

    ⚠️ SL を絞るほど**約定直後のノイズで即発火**する。狙いが「SL が約定すること」の
    確認なのでそれ自体は目的に適うが、entry 直後のスプレッド分で刺さると
    「値動きで到達した」とは言いにくい。5pips 程度が速さと解釈のしやすさの折り合い。
    """
    print("\n### Phase B: 実発注 1件（100通貨）")
    print("目的: 成功レスポンス data の実形を採る（#91 最重要）")

    tp_delta, sl_delta = tp_pips * _PIP, sl_pips * _PIP
    if side == "BUY":
        tp, sl = price + tp_delta, price - sl_delta
    else:
        tp, sl = price - tp_delta, price + sl_delta

    print(f"TP {tp_pips:g}pips / SL {sl_pips:g}pips（entry {price:.3f} → TP {tp:.3f} / SL {sl:.3f}）")
    if sl_pips < _TIGHT_SL_WARN_PIPS:
        print(
            f"⚠️ SL が {sl_pips:g}pips と狭い。約定直後のスプレッドで即発火し得る"
            f"（想定損失 ≒ {sl_pips * _PIP * SIZE:.1f}円 + 手数料）。"
        )

    body = _ifo_body(side, f"{price:.3f}", f"{tp:.3f}", f"{sl:.3f}")
    print(f"\n送信ボディ:\n{json.dumps(body, ensure_ascii=False, indent=2)}")

    resp = dump(f"B-1/B-2 POST {PRIVATE_IFO_ORDER}", c.private_post(PRIVATE_IFO_ORDER, body))

    # ✅ #91 は解決済み（2026-08-03 実測 → models.IfoOrderResponse.data: list[IfoOrderItem]）。
    #    2026-08-11 の再実測でも list（3 件・全件同一 rootOrderId）を確認。
    #    以下は「本番モデルの前提と合っているか」の回帰チェックとして残す。
    print("\n--- #91 回帰チェック（本番は list[IfoOrderItem] を前提に実装済み）---")
    data = resp.get("data")
    print(f"data の Python 型 : {type(data).__name__}")
    print(f"data の中身       : {data!r}")
    if isinstance(data, list):
        root_ids = {item.get("rootOrderId") for item in data if isinstance(item, dict)}
        print(f"→ ✅ data は list（{len(data)} 件）。本番 IfoOrderResponse の前提どおり。")
        print(f"   rootOrderId: {root_ids}（全件同一なら OK。保存すべきはこの値）")
        if len(root_ids) > 1:
            print("   🔴 rootOrderId が割れている。models.py の validator が弾く不変条件の破れ。**要調査**。")
    else:
        print(f"→ 🔴 data が list ではない（{type(data).__name__}）。本番 IfoOrderResponse の前提が崩れる。**要調査**。")

    print("\n次: `status` で約定を待つ → 建玉が出たら positionId と 残存2次注文の rootOrderId を記録")


# ---------------------------------------------------------------------------
# status — いつでも状態確認（GET のみ）
# ---------------------------------------------------------------------------


def status(c: GmoFxClient) -> None:
    pos = dump(f"GET {PRIVATE_OPEN_POSITIONS}", c.private_get(PRIVATE_OPEN_POSITIONS, {"symbol": SYMBOL}))
    ord_ = dump(f"GET {PRIVATE_ACTIVE_ORDERS}", c.private_get(PRIVATE_ACTIVE_ORDERS, {"symbol": SYMBOL}))

    positions = (pos.get("data") or {}).get("list", []) or []
    orders = (ord_.get("data") or {}).get("list", []) or []

    print(f"\n--- 状態 ---\n建玉 {len(positions)} 件 / 注文 {len(orders)} 件")
    for p in positions:
        print(f"  建玉 positionId={p.get('positionId')} side={p.get('side')} size={p.get('size')} price={p.get('price')}")
    for o in orders:
        print(
            f"  注文 rootOrderId={o.get('rootOrderId')} orderId={o.get('orderId')} "
            f"side={o.get('side')} settleType={o.get('settleType')} "
            f"executionType={o.get('executionType')} status={o.get('status')}"
        )
    if positions and orders:
        print("\n✅ [1次約定済み + 2次OCO残存] = #236 のテスト対象状態")
        print("   → phaseC（ERR採取）→ phaseD（#236 本命）へ")
    elif positions and not orders:
        print("\n⚠️ 建玉のみ・注文なし。2次OCO が既に無いので #236 は検証できない。")
    elif not positions and not orders:
        print("\n🟢 flat（建玉も注文もなし）")


# ---------------------------------------------------------------------------
# Phase C — ERR-254 / ERR-423 採取（#235）
# ---------------------------------------------------------------------------


def phase_c(c: GmoFxClient, position_id: int) -> None:
    print("\n### Phase C: ERR-254 / ERR-423 採取（#235・建玉は動かない）")

    # C-1: 存在しない建玉ID → ERR-254 期待
    dump(
        "C-1 存在しない positionId で closeOrder → ERR-254 期待",
        c.private_post(
            PRIVATE_CLOSE_ORDER,
            {
                "symbol": SYMBOL,
                "side": "SELL",
                "executionType": "MARKET",
                "settlePosition": [{"positionId": 999999999, "size": str(SIZE)}],
            },
        ),
    )

    # C-2: 建玉数量を超える size → ERR-423 期待だったが、2026-08-03 の実測は **ERR-189**
    #      （"Exceeds the order quantity."）。ERR-423 は後半条件（総有効注文数量との合計超過）
    #      専用と推定されるが未観測。後半条件を狙う手順は
    #      docs/spikes/ordered_size_err423_latency.md 実験1（scripts/spike_ordered_size_latency.py）。
    dump(
        f"C-2 建玉数量超過の size（{SIZE * 2} vs 建玉 {SIZE}）→ 実測は ERR-189",
        c.private_post(
            PRIVATE_CLOSE_ORDER,
            {
                "symbol": SYMBOL,
                "side": "SELL",
                "executionType": "MARKET",
                "settlePosition": [{"positionId": position_id, "size": str(SIZE * 2)}],
            },
        ),
    )

    print("\n--- 記録先 ---")
    print("message_code / message_string を docs/spikes/cancel_first_official_spec.md §6 へ")
    print("⚠️ side は建玉と反対側を指定すること（BUY 建玉なら SELL）。違う場合は上記を書き換えて再実行。")


# ---------------------------------------------------------------------------
# Phase D — #236 本命
# ---------------------------------------------------------------------------


def phase_d(c: GmoFxClient, root_order_id: int) -> None:
    print("\n### Phase D: cancelOrders で残存2次注文を取消せるか（#236 本命）")

    before = c.private_get(PRIVATE_ACTIVE_ORDERS, {"symbol": SYMBOL}).json()
    n_before = len((before.get("data") or {}).get("list", []) or [])
    print(f"\n取消前の注文数: {n_before}")

    dump(
        f"D-1/D-2 POST {PRIVATE_CANCEL_ORDERS} rootOrderIds=[{root_order_id}]",
        c.private_post(PRIVATE_CANCEL_ORDERS, {"rootOrderIds": [root_order_id]}),
    )

    # 🔴 取消の反映には遅延がある。POST 直後に GET すると取消前の状態が返り、
    # 「効いていない」と誤判定する（2026-08-03 スパイクで実際に踏んだ。GET の
    # responsetime が POST より前になっていた）。再取得の前に必ず待つ。
    # ⚠️ この固定 sleep(3) では**遅延の分布が測れない**（3 秒より速かったのか遅かったのかが
    #    原理的に分からない）。分布を測るのは scripts/spike_ordered_size_latency.py の
    #    exp2-cancel-latency（500ms 間隔ポーリング）。ここは #236 の可否判定用に据え置く。
    print("\n(取消の反映を待機: 3秒)")
    time.sleep(3.0)

    after = dump(f"D-3 GET {PRIVATE_ACTIVE_ORDERS}（再取得）", c.private_get(PRIVATE_ACTIVE_ORDERS, {"symbol": SYMBOL}))
    n_after = len((after.get("data") or {}).get("list", []) or [])

    print(f"\n--- 🔴 #236 の判定 ---\n取消前 {n_before} 件 → 取消後 {n_after} 件")
    if n_after < n_before:
        print("✅ 2次注文が消えた → cancel-first Step2 は現状実装で正しい。")
        print("   cancel_first_official_spec.md §8-1 を『確定』へ更新。Phase 2 に進んでよい。")
    else:
        print("❌ 消えていない → **Step2 が実質 no-op**。")
        print("   決済OCO が残ったまま Step3 に進むと ERR-423 で建玉ロック（AGENTS.md #4 の防止対象）。")
        print("   代替を試す: cancelBulkOrder（symbols + side + settleType=CLOSE）/ clientOrderIds 経路")
        print("   🔴 修正するまで E18（#25）に進まないこと。")


# ---------------------------------------------------------------------------
# Phase E — 手仕舞い（必ず flat へ）
# ---------------------------------------------------------------------------


def phase_e(c: GmoFxClient, position_id: int, side: str) -> None:
    print("\n### Phase E: 手仕舞い（cron は助けてくれない・必ず完遂）")

    dump(
        f"E-2 POST {PRIVATE_CLOSE_ORDER} settlePosition=[{{positionId:{position_id}, size:'{SIZE}'}}]",
        c.private_post(
            PRIVATE_CLOSE_ORDER,
            {
                "symbol": SYMBOL,
                "side": side,
                "executionType": "MARKET",
                "settlePosition": [{"positionId": position_id, "size": str(SIZE)}],
            },
        ),
    )

    print("\n⚠️ closeOrder の成功は status:WAITING の『受付』であり約定ではない（§6.2）。")
    print("   → `status` サブコマンドで openPositions の差分を必ず確認すること。")
    print("   → 空にならない場合は GMO の Web / アプリから手動決済。放置しない。")


# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("phase", choices=["phase0", "phaseA", "phaseB", "status", "phaseC", "phaseD", "phaseE"])
    p.add_argument("--side", choices=["BUY", "SELL"], default="BUY", help="phaseB: 新規方向 / phaseC,E: 決済方向（建玉の反対）")
    p.add_argument("--price", type=float, help="phaseB: entry 価格")
    p.add_argument(
        "--tp-pips",
        type=float,
        default=_DEFAULT_TP_PIPS,
        help=f"phaseB: TP 幅 pips（既定 {_DEFAULT_TP_PIPS:g}）",
    )
    p.add_argument(
        "--sl-pips",
        type=float,
        default=_DEFAULT_SL_PIPS,
        help=(
            f"phaseB: SL 幅 pips（既定 {_DEFAULT_SL_PIPS:g}）。"
            "SL レグの実約定を狙うスパイク（#288）では 5 程度に絞る"
        ),
    )
    p.add_argument("--position-id", type=int, help="phaseC / phaseE: 建玉ID")
    p.add_argument("--root-order-id", type=int, help="phaseD: 残存2次注文の親注文ID")
    a = p.parse_args()

    guard_banner()
    c = client()
    try:
        if a.phase == "phase0":
            phase0(c)
        elif a.phase == "phaseA":
            phase_a(c)
        elif a.phase == "phaseB":
            if a.price is None:
                p.error("phaseB には --price が必要")
            if a.tp_pips <= 0 or a.sl_pips <= 0:
                p.error("--tp-pips / --sl-pips は正の値（価格序列が壊れる）")
            phase_b(c, a.side, a.price, a.tp_pips, a.sl_pips)
        elif a.phase == "status":
            status(c)
        elif a.phase == "phaseC":
            if a.position_id is None:
                p.error("phaseC には --position-id が必要")
            phase_c(c, a.position_id)
        elif a.phase == "phaseD":
            if a.root_order_id is None:
                p.error("phaseD には --root-order-id が必要")
            phase_d(c, a.root_order_id)
        elif a.phase == "phaseE":
            if a.position_id is None:
                p.error("phaseE には --position-id が必要")
            phase_e(c, a.position_id, a.side)
    except OrderBlockedError as e:
        print(f"\n🟢 発注はブロックされました（封じ込め動作は正常）:\n  {e}")
        return 2
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
