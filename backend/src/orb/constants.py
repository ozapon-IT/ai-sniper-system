"""ロンドンORB 判定コアの凍結定数。

出典: docs/ai-dev/london-orb-demo-design.md §2（ORB ルール定義）
オラクル: scratchpad/phase0_usdjpy_london_orb.py（変更禁止）と同一値。
"""

from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo

LON = ZoneInfo("Europe/London")
# 判定は常に London。JST は klines ファイル日付（JST 06:00 起点）の算出にのみ使う。
JST = ZoneInfo("Asia/Tokyo")

PIP: float = 0.01
STOP_BUFFER: float = 1.0 * PIP  # ±1.0pip
WARMUP_BARS: int = 500

RANGE_START: time = time(8, 0)
ORDER_TIME: time = time(9, 0)
CANCEL_TIME: time = time(11, 0)
EOD_TIME: time = time(16, 0)

# 15分足4本（RANGE_START < local_close <= ORDER_TIME）
RANGE_BAR_COUNT_15M: int = 4
# 1分足60本（ORDER 直前の 8:00〜8:59 openTime）
RANGE_BAR_COUNT_1M: int = 60

# 1分足読み込み窓（オラクル load_minute_by_day と同一）
MINUTE_LOAD_LO: time = time(7, 45)
MINUTE_LOAD_HI: time = time(16, 30)

# 窓日判定: 8:00 < local_close <= 11:00 の足が1本でもあれば窓日
WINDOW_END: time = time(11, 0)

BAR_MINUTES_15: int = 15

# 分足 klines の date=YYYYMMDD ファイルは **JST 06:00 起点の 1 日分**
# （付録B docs/spikes/E05_klines_appendix_b.md §8 / london_orb_api_spec.md §2）。
JST_DAY_START: time = time(6, 0)
