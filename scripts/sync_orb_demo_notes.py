"""ORB デモノートをローカルのノート保管場所（vault）へ同期する（運用終了）。

運用は終了しており、CLI は実行を拒否する。証跡の正は S3 の orb-demo/notes/YYYY-MM-DD.md。
関数はテストのために残している。

歴史仕様（設計書 §7.2（運用終了・歴史）/ Issue #272）:
  S3（または `LOCAL_PERSIST_DIR`）の `orb-demo/notes/YYYY-MM-DD.md` を読み、
  vault の `FX/01_Trades/` へ未取り込み分だけコピーする。
  - 既存ファイルは上書きしない
  - 既取込の判定はセッション日単位（ファイル名単位ではない）
  - 連番は既存の最大値 + 1 から新規分にだけ振る（取込順であり日付順とは限らない）
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from backend.src.orb.note_store import list_note_dates, load_note

DEFAULT_VAULT = Path("vault")  # 運用終了のため実際には使われない
TRADES_SUBDIR = "FX/01_Trades"

# frontmatter から stage / direction を読むための最小パーサ（PyYAML 非依存）
_STAGE_RE = re.compile(r"^stage:\s*(\S+)\s*$", re.MULTILINE)
_DIRECTION_RE = re.compile(r"^direction:\s*(\S*)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Planned:
    session_date: str
    filename: str
    is_entry: bool
    exists: bool  # 同じセッション日のノートが vault に既にあるか（名前は問わない）


# vault 上の既存ノート名からセッション日と連番を読む
_VAULT_ENTRY_RE = re.compile(r"^T(\d+)_(\d{4}-\d{2}-\d{2})_\w+\.md$")
_VAULT_SKIP_RE = re.compile(r"^SK_(\d{4}-\d{2}-\d{2})\.md$")


def _parse(markdown: str) -> tuple[str, str]:
    """(stage, direction) を返す。"""
    stage_m = _STAGE_RE.search(markdown)
    dir_m = _DIRECTION_RE.search(markdown)
    return (
        stage_m.group(1) if stage_m else "",
        dir_m.group(1) if dir_m and dir_m.group(1) else "",
    )


def scan_vault(trades_dir: Path) -> tuple[set[str], int]:
    """vault の既存ノートから (**取込済みセッション日**, 最大連番) を返す。"""
    imported: set[str] = set()
    max_seq = 0
    if not trades_dir.exists():
        return imported, max_seq
    for path in trades_dir.glob("*.md"):
        entry = _VAULT_ENTRY_RE.match(path.name)
        if entry:
            max_seq = max(max_seq, int(entry.group(1)))
            imported.add(entry.group(2))
            continue
        skip = _VAULT_SKIP_RE.match(path.name)
        if skip:
            imported.add(skip.group(1))
    return imported, max_seq


def plan(vault: Path) -> list[Planned]:
    """同期計画を作る。

    既取込は**セッション日**で判定し、連番は既存の最大値 + 1 から新規分にだけ振る。
    """
    trades_dir = vault / TRADES_SUBDIR
    imported, sequence = scan_vault(trades_dir)
    out: list[Planned] = []
    for day in list_note_dates():
        markdown = load_note(day)
        if markdown is None:
            continue
        stage, direction = _parse(markdown)
        is_entry = stage == "entry" and bool(direction)
        already = day in imported
        if already:
            # 既存の名前は分からなくてよい（コピーしないため）。表示用に日付を出す。
            filename = f"（{day} は取込済み）"
        elif is_entry:
            sequence += 1
            filename = f"T{sequence:03d}_{day}_{direction}.md"
        else:
            filename = f"SK_{day}.md"
        out.append(
            Planned(
                session_date=day,
                filename=filename,
                is_entry=is_entry,
                exists=already,
            )
        )
    return out


def sync(vault: Path, *, apply: bool) -> dict[str, int]:
    """計画に従ってコピーする。`apply=False` なら何も書かない。"""
    trades_dir = vault / TRADES_SUBDIR
    stats = {"planned": 0, "copied": 0, "skipped_existing": 0}
    for item in plan(vault):
        stats["planned"] += 1
        if item.exists:
            stats["skipped_existing"] += 1
            print(f"  skip (取込済み): {item.session_date}")
            continue
        print(f"  {'copy' if apply else 'would copy'}: {item.filename}")
        if apply:
            markdown = load_note(item.session_date)
            if markdown is None:
                continue
            trades_dir.mkdir(parents=True, exist_ok=True)
            (trades_dir / item.filename).write_text(markdown, encoding="utf-8")
            stats["copied"] += 1
    return stats


_RETIRED_MESSAGE = (
    "運用終了（#362 / 2026-09-12）。"
    "証跡 SoT は S3 orb-demo/notes/YYYY-MM-DD.md。vault へ週次コピーしない。"
    "CLI は拒否する。関数は歴史的テスト用に残す。"
)


def main() -> None:
    """CLI は運用終了。`--apply` / `--dry-run` でも vault に書かず exit 2。"""
    p = argparse.ArgumentParser(
        description="運用終了（#362）。ORB デモノートの vault 同期は拒否する"
    )
    p.add_argument("--vault", type=Path, default=DEFAULT_VAULT, help="無視される（運用終了）")
    p.add_argument("--apply", action="store_true", help="無視される（運用終了・書き込まない）")
    p.add_argument("--dry-run", action="store_true", help="無視される（運用終了）")
    p.parse_args()
    print(_RETIRED_MESSAGE, file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
