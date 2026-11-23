"""ORB-5 記録パイプラインのテスト（#272）。

設計書 §7.1（frontmatter 写像）・§7.2（vault 同期は運用終了・テストは歴史仕様）
記録様式: バックテスト記録システム §1（命名）・§2（frontmatter）・§4（stage）
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime
from typing import Any

import pytest

from backend.src.orb.broker_models import BrokerSession, SlippageRecord
from backend.src.orb.constants import LON
from backend.src.orb.models import RangeLevels
from backend.src.orb.note import (
    NoteContext,
    build_frontmatter,
    note_skip_reason,
    render_note,
    stage_of,
    vault_filename,
)

LEVELS = RangeLevels(high=150.00, low=149.80, buy_stop=150.01, sell_stop=149.79)
SESSION_DATE = "2026-08-10"  # 月曜


def _lon(h: int, m: int) -> datetime:
    return datetime(2026, 8, 10, h, m, tzinfo=LON)


def _scalar(raw: str) -> Any:
    """frontmatter のスカラ値を dataview 相当に解釈する。"""
    text = raw.strip()
    if text == "":
        return None
    if text in {"true", "false"}:
        return text == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _parse_frontmatter(markdown: str) -> dict[str, Any]:
    """`---` で挟まれた frontmatter を厳密に解釈する（2 スペース入れ子・`[a, b]` 対応）。"""
    assert markdown.startswith("---\n"), "frontmatter が先頭に無い"
    body = markdown.split("---\n", 2)
    lines = [ln for ln in body[1].splitlines() if ln.strip()]
    out: dict[str, Any] = {}
    nested_key: str | None = None
    for i, line in enumerate(lines):
        if line.startswith("  ") and nested_key is not None:
            key, _, value = line.strip().partition(":")
            out[nested_key][key] = _scalar(value)
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        # YAML と同じ規則: `key:` が入れ子になるのは**次行が字下げされている**ときだけ。
        # そうでなければ null（`result_pips:` 等の空欄がこれに当たる）。
        next_indented = i + 1 < len(lines) and lines[i + 1].startswith("  ")
        if value == "" and next_indented:
            nested_key = key
            out[key] = {}
        elif value.startswith("[") and value.endswith("]"):
            nested_key = None
            inner = value[1:-1].strip()
            out[key] = [v.strip() for v in inner.split(",")] if inner else []
        else:
            nested_key = None
            out[key] = _scalar(value)
    return out


def _slip(slip_pips: float, spread: float = 0.5, gap: int | None = 5000) -> SlippageRecord:
    return SlippageRecord(
        trigger_price=150.01,
        observed_bid=150.02,
        slip_pips=slip_pips,
        spread_pips=spread,
        poll_gap_ms=gap,
        price_gap=0.001,
    )


def _filled_long(exit_price: float = 150.55, exit_reason: str = "eod") -> BrokerSession:
    return BrokerSession(
        session_date=SESSION_DATE,
        state="CLOSED",
        levels=LEVELS,
        direction="long",
        entry_price=150.02,
        entry_ts=_lon(9, 20),
        sl_price=149.79,
        exit_price=exit_price,
        exit_ts=_lon(16, 0),
        exit_reason=exit_reason,  # type: ignore[arg-type]
        entry_slip=_slip(1.0),
        exit_slip=_slip(0.0),
    )


def _skipped(reason: str) -> BrokerSession:
    """SKIPPED（session-open で弾かれた）セッション。

    ⚠️ `no_fill` は SKIPPED ではなく **CANCELLED**（11:00 まで未約定）なので
    `_cancelled()` を使う。混同すると stage が `gate` になり本番（`order`）とずれる。
    """
    assert reason != "no_fill", "no_fill は _cancelled() を使う"
    return BrokerSession(
        session_date=SESSION_DATE,
        state="SKIPPED",
        levels=None if reason == "no_range" else LEVELS,
        skip_reason=reason,  # type: ignore[arg-type]
    )


def _cancelled() -> BrokerSession:
    """11:00 まで未約定で両側取消（本番の no_fill 経路）。"""
    return BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=LEVELS)


# ---------------------------------------------------------------- mapping


class TestStageMapping:
    @pytest.mark.parametrize(
        ("session", "expected"),
        [
            (_filled_long(), "entry"),
            (_skipped("no_range"), "range"),
            (_skipped("market_holiday"), "gate"),
            (_skipped("market_closed"), "gate"),
            (_skipped("system_outage"), "gate"),
            (_skipped("halted"), "gate"),
            (_skipped("disabled"), "gate"),
            (BrokerSession(session_date=SESSION_DATE, state="CANCELLED", levels=LEVELS), "order"),
            (
                BrokerSession(
                    session_date=SESSION_DATE,
                    state="ANOMALY",
                    levels=LEVELS,
                    exit_reason="both_filled",
                ),
                "order",
            ),
            (BrokerSession(session_date=SESSION_DATE, state="UNCERTAIN", levels=LEVELS), "order"),
        ],
    )
    def test_stage(self, session: BrokerSession, expected: str) -> None:
        assert stage_of(session) == expected

    def test_note_skip_reason_extends_typed_literal(self) -> None:
        """`both_filled` / `uncertain` は SkipReason に無いがノートでは区別する（記録システム §4-1）。"""
        anomaly = BrokerSession(
            session_date=SESSION_DATE, state="ANOMALY", exit_reason="both_filled"
        )
        assert note_skip_reason(anomaly) == "both_filled"
        assert note_skip_reason(BrokerSession(session_date=SESSION_DATE, state="UNCERTAIN")) == "uncertain"
        assert note_skip_reason(BrokerSession(session_date=SESSION_DATE, state="CANCELLED")) == "no_fill"


class TestFrontmatter:
    def test_schema_keys_match_recording_system(self) -> None:
        """記録システム §2 の必須キーを網羅する（dataview 互換）。"""
        fm = build_frontmatter(_filled_long())
        required = {
            "type", "mode", "date", "weekday", "entry_time", "exit_time", "direction",
            "setup", "entry_price", "exit_price", "sl_pips", "result_pips",
            "rr_planned", "rr_actual", "result", "entry_tf", "exit_reason",
            "rule_compliance", "skip_reason", "stage", "raw", "tags",
        }
        assert required <= set(fm)
        assert fm["type"] == "trade"
        assert fm["mode"] == "demo"
        assert fm["setup"] == "london_orb"
        assert fm["rule_compliance"] is True

    def test_numbers_and_labels(self) -> None:
        fm = build_frontmatter(_filled_long())
        # entry 150.02 → exit 150.55 = +53.0pips / risk = |150.02-149.79| = 23.0pips
        assert fm["result_pips"] == pytest.approx(53.0)
        assert fm["sl_pips"] == pytest.approx(23.0)
        assert fm["rr_actual"] == pytest.approx(53.0 / 23.0, rel=1e-3)
        assert fm["result"] == "win"
        assert fm["exit_reason"] == "time"  # EOD は記録システム語彙の time
        assert fm["weekday"] == "月"
        assert fm["rr_planned"] is None  # TP なしルール

    def test_jst_times(self) -> None:
        """`entry_time` / `exit_time` は JST（記録システム §2）。

        London 9:20 BST → JST 17:20。EOD の London 16:00 BST → JST **翌日 00:00**。
        記録システム §1 のとおり `date` は**セッション日のまま**にし、決済が翌日に
        及ぶことは `exit_time` が朝方の値になることで表現する。
        """
        fm = build_frontmatter(_filled_long())
        assert fm["entry_time"] == "17:20"
        assert fm["exit_time"] == "00:00"
        assert fm["date"] == SESSION_DATE  # 決済が翌日でもセッション日は動かさない

    def test_loss_and_be(self) -> None:
        assert build_frontmatter(_filled_long(exit_price=149.79, exit_reason="sl"))["result"] == "lose"
        assert build_frontmatter(_filled_long(exit_price=150.02))["result"] == "be"

    def test_skip_has_no_trade_numbers(self) -> None:
        fm = build_frontmatter(_skipped("no_range"))
        assert fm["result"] == "skip"
        assert fm["result_pips"] is None
        assert fm["rr_actual"] is None
        assert fm["stage"] == "range"

    def test_raw_slippage_block(self) -> None:
        fm = build_frontmatter(_filled_long())
        raw = fm["raw"]
        assert raw["ref_high"] == pytest.approx(150.00)
        assert raw["buy_stop"] == pytest.approx(150.01)
        assert raw["slip_entry_pips"] == pytest.approx(1.0)
        assert raw["spread_at_fill_pips"] == pytest.approx(0.5)
        assert raw["poll_gap_ms"] == 5000

    def test_eod_gap_only_when_reference_supplied(self) -> None:
        """`eod_gap_pips` は verify（ORB-6）が基準値を渡したときだけ埋まる（§6.4）。"""
        assert build_frontmatter(_filled_long())["raw"]["eod_gap_pips"] is None
        fm = build_frontmatter(_filled_long(), NoteContext(eod_close=150.50))
        # 仮想決済 150.55 − 15分足終値 150.50 = +5.0pips
        assert fm["raw"]["eod_gap_pips"] == pytest.approx(5.0)

    def test_eod_gap_not_set_for_sl_exit(self) -> None:
        fm = build_frontmatter(
            _filled_long(exit_price=149.79, exit_reason="sl"), NoteContext(eod_close=150.50)
        )
        assert fm["raw"]["eod_gap_pips"] is None


class TestRender:
    def test_frontmatter_block_is_wellformed(self) -> None:
        md = render_note(_filled_long())
        assert md.startswith("---\n")
        body = md.split("---", 2)
        assert len(body) >= 3
        fm_text = body[1]
        assert "mode: demo" in fm_text
        assert "setup: london_orb" in fm_text
        assert "tags: [trade, orb, demo]" in fm_text
        # raw は入れ子で出力される
        assert "raw:" in fm_text
        assert "  ref_high: 150" in fm_text

    def test_frontmatter_parses_as_dataview_expects(self) -> None:
        """frontmatter を実際にパースして値・型を検証する。

        PyYAML はプロジェクト依存に無いため、dataview が必要とする最小構文
        （`key: value` / 2 スペース入れ子 / `[a, b]` リスト）を厳密に解釈する
        パーサをテスト内に置いて検証する。受け入れ条件「dataview 集計に載る」の実質確認。
        """
        md = render_note(_filled_long(), NoteContext(eod_close=150.50))
        parsed = _parse_frontmatter(md)

        assert parsed["type"] == "trade"
        assert parsed["mode"] == "demo"
        assert parsed["stage"] == "entry"
        assert parsed["setup"] == "london_orb"
        assert parsed["rule_compliance"] is True
        assert parsed["tags"] == ["trade", "orb", "demo"]
        # dataview の数値集計対象は数値として読めること（末尾ゼロは除去されるので int でも可）
        assert isinstance(parsed["result_pips"], int | float)
        assert parsed["result_pips"] == pytest.approx(53.0)
        assert isinstance(parsed["raw"], dict)
        assert parsed["raw"]["eod_gap_pips"] == pytest.approx(5.0)
        assert parsed["raw"]["poll_gap_ms"] == 5000

    def test_skip_note_leaves_numeric_fields_empty(self) -> None:
        """見送り日は数値欄が空（dataview で数値集計から自然に外れる）。"""
        parsed = _parse_frontmatter(render_note(_skipped("no_range")))
        assert parsed["result"] == "skip"
        assert parsed["result_pips"] is None
        assert parsed["direction"] is None
        assert parsed["raw"]["ref_high"] is None

    def test_skip_note_states_reason(self) -> None:
        md = render_note(_skipped("market_closed"))
        assert "市場が開いておらず見送り" in md
        assert "skip_reason: market_closed" in md

    def test_body_marks_day_rollover(self) -> None:
        """EOD は JST で翌日になるため本文で「翌」を明示する（frontmatter は HH:MM のまま）。"""
        md = render_note(_filled_long())
        assert "翌 00:00 JST" in md
        assert "（17:20 JST）" in md  # エントリーは当日なので接頭辞なし
        assert "exit_time: 00:00" in md  # 記録システム §2 準拠は維持

    def test_note_declares_mock_limitation(self) -> None:
        """滑りは下界近似である旨をノートに必ず書く（§12 限界1・§6.3）。"""
        md = render_note(_filled_long())
        assert "気配ベースの下界近似" in md
        assert "損益でエッジを判定しません" in md


class TestVaultFilename:
    def test_entry_consumes_sequence(self) -> None:
        assert vault_filename(_filled_long(), sequence=1) == "T001_2026-08-10_long.md"

    def test_skip_has_no_sequence(self) -> None:
        assert vault_filename(_skipped("no_range")) == "SK_2026-08-10.md"

    def test_entry_requires_sequence(self) -> None:
        with pytest.raises(ValueError, match="sequence"):
            vault_filename(_filled_long())


# ---------------------------------------------------------------- store / sync


@pytest.fixture()
def local_store(tmp_path, monkeypatch):
    """ノート・セッションの**両方**をローカルへ逃がす（AWS へ触れない）。

    片方だけ patch すると `load_session` が実 DynamoDB を叩いて
    `NoCredentialsError` になる。
    """
    monkeypatch.setenv("LOCAL_PERSIST_DIR", str(tmp_path))
    notes = importlib.import_module("backend.src.orb.note_store")
    for mod_name in ("backend.src.orb.note_store", "backend.src.orb.session_store"):
        mod = importlib.import_module(mod_name)
        monkeypatch.setattr(mod, "LOCAL_PERSIST_DIR", str(tmp_path), raising=False)
    return notes


class TestNoteStore:
    def test_roundtrip_and_listing(self, local_store) -> None:
        local_store.save_note("2026-08-10", "# a\n")
        local_store.save_note("2026-08-11", "# b\n")
        assert local_store.load_note("2026-08-10") == "# a\n"
        assert local_store.load_note("2026-08-12") is None
        assert local_store.list_note_dates() == ["2026-08-10", "2026-08-11"]

    def test_key_is_date_addressed(self, local_store) -> None:
        """S3 は日付キー。連番は同期時に振るため再生成が冪等（§7.1 実装注記）。"""
        assert local_store.s3_key_orb_note("2026-08-10") == "orb-demo/notes/2026-08-10.md"


class TestVaultSync:
    def _seed(self, store) -> None:
        store.save_note("2026-08-10", render_note(_filled_long()))
        store.save_note("2026-08-11", render_note(_cancelled()))
        store.save_note("2026-08-12", render_note(_filled_long()))

    def test_sequence_only_for_entries(self, local_store, tmp_path) -> None:
        self._seed(local_store)
        import scripts.sync_orb_demo_notes as sync_mod

        importlib.reload(sync_mod)
        planned = sync_mod.plan(tmp_path / "vault")
        names = [p.filename for p in planned]
        assert names == ["T001_2026-08-10_long.md", "SK_2026-08-11.md", "T002_2026-08-12_long.md"]

    def test_dry_run_writes_nothing(self, local_store, tmp_path) -> None:
        self._seed(local_store)
        import scripts.sync_orb_demo_notes as sync_mod

        importlib.reload(sync_mod)
        vault = tmp_path / "vault"
        stats = sync_mod.sync(vault, apply=False)
        assert stats["planned"] == 3
        assert stats["copied"] == 0
        assert not (vault / sync_mod.TRADES_SUBDIR).exists()

    def test_backfilled_day_does_not_duplicate_a_session(self, local_store, tmp_path) -> None:
        """途中日を後から埋めても、同じセッション日のノートは 1 本のまま。

        既取込判定を**ファイル名**でやると後続日の連番がずれ、同じ日が別名で
        二重コピーされて dataview が二重計上する（レビュー指摘・実際に再現した）。
        判定はセッション日単位、連番は既存最大 + 1 から新規分だけに振る。
        """
        import scripts.sync_orb_demo_notes as sync_mod

        importlib.reload(sync_mod)
        vault = tmp_path / "vault"
        trades = vault / sync_mod.TRADES_SUBDIR

        local_store.save_note("2026-08-10", render_note(_filled_long()))
        local_store.save_note("2026-08-12", render_note(_filled_long()))
        assert sync_mod.sync(vault, apply=True)["copied"] == 2

        # 後から途中日を追加
        local_store.save_note("2026-08-11", render_note(_filled_long()))
        second = sync_mod.sync(vault, apply=True)
        assert second["copied"] == 1
        assert second["skipped_existing"] == 2

        names = sorted(p.name for p in trades.glob("*.md"))
        days = [n.split("_")[1].removesuffix(".md") for n in names]
        assert len(days) == len(set(days)), f"セッション日が重複: {names}"
        # 連番は取込順（後埋めは後ろの番号）。既存はリネームしない。
        assert names == [
            "T001_2026-08-10_long.md",
            "T002_2026-08-12_long.md",
            "T003_2026-08-11_long.md",
        ]

        # 3 回目は完全に no-op
        assert sync_mod.sync(vault, apply=True)["copied"] == 0

    def test_scan_vault_reads_dates_and_max_sequence(self, tmp_path) -> None:
        import scripts.sync_orb_demo_notes as sync_mod

        trades = tmp_path / sync_mod.TRADES_SUBDIR
        trades.mkdir(parents=True)
        for name in ("T001_2026-08-10_long.md", "T007_2026-08-12_short.md", "SK_2026-08-11.md"):
            (trades / name).write_text("x", encoding="utf-8")
        imported, max_seq = sync_mod.scan_vault(trades)
        assert imported == {"2026-08-10", "2026-08-11", "2026-08-12"}
        assert max_seq == 7

    def test_apply_then_rerun_does_not_overwrite(self, local_store, tmp_path) -> None:
        """既存ファイルは上書きしない（§7.2・記録システム §7-6）。"""
        self._seed(local_store)
        import scripts.sync_orb_demo_notes as sync_mod

        importlib.reload(sync_mod)
        vault = tmp_path / "vault"
        first = sync_mod.sync(vault, apply=True)
        assert first["copied"] == 3

        target = vault / sync_mod.TRADES_SUBDIR / "T001_2026-08-10_long.md"
        target.write_text("人が訂正した内容\n", encoding="utf-8")

        second = sync_mod.sync(vault, apply=True)
        assert second["copied"] == 0
        assert second["skipped_existing"] == 3
        assert target.read_text(encoding="utf-8") == "人が訂正した内容\n"

    def test_main_refuses_writes_and_exits_2(self, tmp_path, monkeypatch, capsys) -> None:
        """CLI は運用終了。--apply でも vault に書かず exit 2（#362）。"""
        import scripts.sync_orb_demo_notes as sync_mod

        importlib.reload(sync_mod)
        vault = tmp_path / "vault"
        monkeypatch.setattr(
            sys,
            "argv",
            ["sync_orb_demo_notes.py", "--vault", str(vault), "--apply"],
        )
        with pytest.raises(SystemExit) as exc:
            sync_mod.main()
        assert exc.value.code == 2
        assert not vault.exists()
        err = capsys.readouterr().err
        assert "運用終了" in err
        assert "#362" in err


class TestNoteBatch:
    def test_generates_from_stored_session(self, local_store) -> None:
        session_mod = importlib.import_module("backend.src.orb.session_store")
        session_mod.save_session(_filled_long())

        from backend.src.batch import orb_note_batch

        res = orb_note_batch.run_note_generation(SESSION_DATE, eod_close=150.50)
        assert res["status"] == "ok"
        assert res["stage"] == "entry"
        saved = local_store.load_note(SESSION_DATE)
        assert saved is not None
        assert "eod_gap_pips: 5" in saved

    def test_missing_session(self, local_store) -> None:
        from backend.src.batch import orb_note_batch

        assert orb_note_batch.run_note_generation("1999-01-01")["status"] == "no_session"

    def test_refuses_non_terminal_session(self, local_store) -> None:
        """保有中（FILLED）に生成すると stage=entry なのに result=skip の不整合ノートになる。"""
        session_mod = importlib.import_module("backend.src.orb.session_store")
        from dataclasses import replace as dc_replace

        session_mod.save_session(dc_replace(_filled_long(), state="FILLED", exit_price=None))

        from backend.src.batch import orb_note_batch

        res = orb_note_batch.run_note_generation(SESSION_DATE)
        assert res["status"] == "not_terminal"
        assert res["state"] == "FILLED"
        assert local_store.load_note(SESSION_DATE) is None

        forced = orb_note_batch.run_note_generation(SESSION_DATE, allow_incomplete=True)
        assert forced["status"] == "ok"

    def test_save_failure_alerts(self, local_store, monkeypatch) -> None:
        """永続化失敗は Slack 通知（AGENTS.md #9）。"""
        session_mod = importlib.import_module("backend.src.orb.session_store")
        session_mod.save_session(_filled_long())

        from backend.src.batch import orb_note_batch

        sent: list[str] = []
        monkeypatch.setattr(orb_note_batch, "save_note", lambda *_a, **_k: False)
        monkeypatch.setattr(orb_note_batch, "_alert", sent.append)
        res = orb_note_batch.run_note_generation(SESSION_DATE)
        assert res["status"] == "save_failed"
        assert sent and SESSION_DATE in sent[0]


class TestContainment:
    def test_note_pipeline_has_no_forbidden_refs(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        targets = [
            root / "backend/src/orb/note.py",
            root / "backend/src/orb/note_store.py",
            root / "backend/src/batch/orb_note_batch.py",
            root / "scripts/sync_orb_demo_notes.py",
        ]
        forbidden = (
            "ifoOrder", "ifdOrder", "closeOrder", "cancelOrders",
            "ALLOW_LIVE_ORDERS", "TRADING_PHASE", "MVP_MODE",
            "gmo_client", "anthropic", "private_get", "private_post", "PRIVATE_",
        )
        hits = [
            f"{p.name}:{tok}" for p in targets for tok in forbidden if tok in p.read_text()
        ]
        assert hits == [], f"forbidden refs: {hits}"
