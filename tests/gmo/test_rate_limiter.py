"""RateLimiter ユニットテスト (§7.2, AGENTS.md #3)."""

import threading
import time
from unittest.mock import patch

from backend.src.rate_limiter import RateLimiter

# ---------------------------------------------------------------------------
# wait_post / mark_post_done（単一スレッド・時刻モック）
# ---------------------------------------------------------------------------


def test_post_sleep_enforced_on_rapid_consecutive_calls() -> None:
    """連続 POST で応答受信後 ≥1.0秒の sleep が挿入されること."""
    limiter = RateLimiter()
    sleep_calls: list[float] = []

    with (
        patch("backend.src.rate_limiter.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        patch("backend.src.rate_limiter.time.monotonic", side_effect=[
            # wait_post 1回目: now=0.0, remaining=-inf → no sleep, _post_in_flight=True
            0.0,
            # mark_post_done: _next_post_allowed = 0.3 + 1.0 = 1.3
            0.3,
            # wait_post 2回目（ループ1回目）: now=0.8, remaining=0.5 → sleep(0.5)、ループ継続
            0.8,
            # wait_post 2回目（ループ2回目）: now=1.3, remaining=0 → _post_in_flight=True
            1.3,
        ]),
    ):
        limiter.wait_post()
        limiter.mark_post_done()   # 応答受信後（_next_post_allowed = 1.3）
        limiter.wait_post()        # remaining=0.5 → sleep(0.5) → retry → 通過

    assert len(sleep_calls) == 1
    assert abs(sleep_calls[0] - 0.5) < 1e-9


def test_post_no_sleep_when_interval_elapsed_after_response() -> None:
    """応答受信後から ≥1.0秒 経っていれば sleep しない."""
    limiter = RateLimiter()
    sleep_calls: list[float] = []

    with (
        patch("backend.src.rate_limiter.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        patch("backend.src.rate_limiter.time.monotonic", side_effect=[
            # wait_post 1回目: now=0.0, remaining=-inf → _post_in_flight=True
            0.0,
            # mark_post_done: _next_post_allowed = 0.2 + 1.0 = 1.2
            0.2,
            # wait_post 2回目: now=1.5, remaining=1.2-1.5=-0.3 → _post_in_flight=True
            1.5,
        ]),
    ):
        limiter.wait_post()
        limiter.mark_post_done()
        limiter.wait_post()

    assert sleep_calls == []


# ---------------------------------------------------------------------------
# in-flight ブロッキング（実スレッドで検証）
# ---------------------------------------------------------------------------


def test_second_post_blocks_while_first_is_in_flight() -> None:
    """POST in-flight 中は別スレッドの wait_post() がブロックされること.

    POST_INTERVAL = 0.0 にしてクールダウン待機なしで in-flight ブロッキングのみ検証する。
    """
    limiter = RateLimiter()
    limiter.POST_INTERVAL = 0.0  # クールダウンを無効化

    # Thread A が POST 枠を取得（mark_post_done は呼ばない = in-flight 状態）
    limiter.wait_post()

    unblocked = threading.Event()

    def thread_b() -> None:
        limiter.wait_post()  # in-flight 中はここでブロック
        unblocked.set()

    t = threading.Thread(target=thread_b, daemon=True)
    t.start()

    # Thread B が wait_post に到達してブロックされる時間を確保
    time.sleep(0.05)
    assert not unblocked.is_set(), "in-flight 中は wait_post がブロックされるべき"

    # Thread A が POST を完了 → Thread B が解放される
    limiter.mark_post_done()

    t.join(timeout=2.0)
    assert unblocked.is_set(), "mark_post_done 後に wait_post がブロック解除されるべき"


# ---------------------------------------------------------------------------
# wait_get
# ---------------------------------------------------------------------------


def test_get_sleep_enforced_on_rapid_consecutive_calls() -> None:
    """連続 GET で ≥167ms の sleep が挿入されること."""
    limiter = RateLimiter()
    sleep_calls: list[float] = []

    with (
        patch("backend.src.rate_limiter.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        patch("backend.src.rate_limiter.time.monotonic", side_effect=[
            0.0,    # 1回目 elapsed
            0.0,    # 1回目 _last_get 更新
            0.05,   # 2回目 elapsed = 0.05 < 0.167 → sleep
            0.167,  # 2回目 _last_get 更新
        ]),
    ):
        limiter.wait_get()
        limiter.wait_get()

    assert len(sleep_calls) == 1
    assert sleep_calls[0] > 0.1


def test_get_no_sleep_when_interval_elapsed() -> None:
    """前回 GET から ≥167ms 経っていれば sleep しない."""
    limiter = RateLimiter()
    sleep_calls: list[float] = []

    with (
        patch("backend.src.rate_limiter.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        patch("backend.src.rate_limiter.time.monotonic", side_effect=[
            0.0, 0.0,
            0.5, 0.5,  # elapsed >= GET_INTERVAL → sleep なし
        ]),
    ):
        limiter.wait_get()
        limiter.wait_get()

    assert sleep_calls == []


def test_post_and_get_limits_are_independent() -> None:
    """POST カウンタが進んでも GET カウンタは独立していること."""
    limiter = RateLimiter()
    sleep_calls: list[float] = []

    with (
        patch("backend.src.rate_limiter.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        patch("backend.src.rate_limiter.time.monotonic", side_effect=[
            # wait_get 1回目（初回なので sleep なし）
            0.0, 0.0,   # elapsed (inf) → no sleep; _last_get = 0.0
            # wait_post 1回目（初回なので sleep なし）
            0.0,        # now=0.0, remaining=-inf → _post_in_flight=True
            # mark_post_done
            0.05,       # _next_post_allowed = 1.05
            # wait_get 2回目（elapsed = 0.05 < 0.167 → sleep あり）
            0.05, 0.167,
        ]),
    ):
        limiter.wait_get()           # 1回目 GET: sleep なし
        limiter.wait_post()          # 1回目 POST: sleep なし（GET カウンタに影響しない）
        limiter.mark_post_done()     # POST 応答受信
        limiter.wait_get()           # 2回目 GET: elapsed 0.05 < 0.167 → sleep あり

    assert len(sleep_calls) == 1  # GET 2回目のみ sleep
    assert sleep_calls[0] > 0.1
