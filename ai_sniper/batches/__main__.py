"""CLI エントリポイント: python -m ai_sniper.batches <batch> [--mode <mode>].

Examples:
  python -m ai_sniper.batches decision
  python -m ai_sniper.batches closeout --mode weekday
  python -m ai_sniper.batches closeout --mode friday
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Sniper バッチ実行")
    subparsers = parser.add_subparsers(dest="batch", required=True)

    subparsers.add_parser("decision", help="判断・IFD-OCO 注文予約バッチ（§2, §6.1）")

    closeout_parser = subparsers.add_parser("closeout", help="手仕舞いバッチ（§6.2）")
    closeout_parser.add_argument(
        "--mode",
        choices=["weekday", "friday"],
        default="weekday",
        help="weekday: 火〜金 4:30 JST / friday: 土曜 4:30 JST（週次総括）",
    )

    args = parser.parse_args()

    if args.batch == "decision":
        from backend.src.batch.decision_batch import lambda_handler

        result = lambda_handler({}, None)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result.get("status") in {"ok", "stubbed", "skipped", "no_entry"} else 1)

    if args.batch == "closeout":
        from backend.src.batch.closeout_batch import _run

        result = _run(mode=args.mode)
        print(json.dumps(result, ensure_ascii=False))
        # skipped = 冪等 skip（既存ロック）で正常。lock_unavailable/失敗系は非 0。
        sys.exit(0 if result.get("status") in {"ok", "market_closed", "skipped"} else 1)


if __name__ == "__main__":
    main()
