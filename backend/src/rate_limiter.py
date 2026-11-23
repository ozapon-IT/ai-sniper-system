"""GMO FX API レートリミッター (§7.2, AGENTS.md #3).

POST: 1回/秒 → 前回 POST 応答受信後 から ≥1.0 秒のスリープを強制。
GET:  6回/秒 → 前回 GET から ≥167ms のスリープを強制。

使い方（POST）:
    limiter.wait_post()          # 送信前: in-flight 解消＋クールダウン待機、枠を確保
    response = client.post(...)  # 実際の送信
    limiter.mark_post_done()     # 応答受信後: クールダウン起点を更新し次の wait を解放

並列安全性:
    wait_post() は Condition ロック保持中に _post_in_flight を True にしてから
    返るため、複数スレッドが同時に wait_post() を通過できない。
    前回の POST が応答未受信（in-flight）の間は後続スレッドが mark_post_done()
    の notify まで待機し、応答受信後 ≥1.0 秒の直列化が保証される。
"""

import threading
import time


class RateLimiter:
    """メソッド種別ごとに送信タイミングを管理し、必要な sleep を挿入する."""

    POST_INTERVAL: float = 1.0      # § 7.2: POST 上限 1回/秒
    GET_INTERVAL: float = 1.0 / 6  # § 7.2: GET 上限 6回/秒 → ≈167ms

    def __init__(self) -> None:
        # POST 送信が応答待ち中かを示すフラグ
        self._post_in_flight: bool = False
        # 次回 POST を送信できる最早時刻（monotonic）。-inf = 即時可
        self._next_post_allowed: float = float("-inf")
        # Condition: _post_in_flight の変化を後続スレッドへ通知するために使用
        self._post_cond: threading.Condition = threading.Condition()
        self._last_get: float = float("-inf")
        self._get_lock: threading.Lock = threading.Lock()

    def wait_post(self) -> None:
        """POST 送信前に呼ぶ。前回 mark_post_done() から ≥1.0 秒かつ in-flight なしを保証する。

        ロック保持中に _post_in_flight を True にして返すため、並列呼び出しでも
        複数スレッドが同時に POST を送ることを防ぐ。
        """
        while True:
            with self._post_cond:
                # in-flight の POST がある間は待機
                while self._post_in_flight:
                    self._post_cond.wait()
                now = time.monotonic()
                remaining = self._next_post_allowed - now
                if remaining <= 0:
                    # クールダウン済み・in-flight なし → 枠を確保して返る
                    self._post_in_flight = True
                    return
            # クールダウン待機（time.sleep はロック外で行い deadlock を防ぐ）
            time.sleep(remaining)
            # ループして再チェック（sleep 中に別スレッドが割り込む可能性があるため）

    def mark_post_done(self) -> None:
        """POST 応答受信後に呼ぶ。クールダウン起点を更新し、後続の wait_post() を解放する。"""
        with self._post_cond:
            self._next_post_allowed = time.monotonic() + self.POST_INTERVAL
            self._post_in_flight = False
            self._post_cond.notify_all()

    def wait_get(self) -> None:
        """GET 送信前に呼ぶ。前回 GET から ≥167ms を保証する。"""
        with self._get_lock:
            elapsed = time.monotonic() - self._last_get
            if elapsed < self.GET_INTERVAL:
                time.sleep(self.GET_INTERVAL - elapsed)
            self._last_get = time.monotonic()
