"""サブエージェント連携（BT-4, 設計書 §2, §4.1, §4.2, §6.2, §6.3）。

LLM 判断を Anthropic API キーではなく Claude Code セッションのサブエージェント
（.claude/agents/backtest-decider.md・本番朝バッチと同一モデル）に委譲するための
ファイル受け渡しプロトコル。

  prepare: 営業日ごとに requests/<name>.json を生成
           （SYSTEM_PROMPT と build_user_prompt は本番と同一。
            decision_date_jst は空のままなので、現在時刻行は時刻と曜日のみ。
            判断バッチは判断日の月日を出す。#376）
  decide:  サブエージェントが名前昇順に 1 件ずつ読み decisions/<name>.json を書く
  check:   完了・順序（先読み禁止の事後検査）・パース可否を検証
  run:     SubagentFileSource が decisions を消費してシミュレーション

ブラインドモード（既定・設計書 §6.3）:
  ファイル名を連番（0001.json …）にし、リクエスト本文にも絶対日付を含めない。
  判断バッチの現在時刻行は判断日の月日を含む（#376）が、バックテストは
  decision_date_jst を空のままにするので、現在時刻行は時刻と曜日のみ
  （tests/backtest/test_subagent.py で担保）。
  連番 → 実日付の対応は manifest.json に保持し、サブエージェントには読ませない。
  価格水準から時代は推定され得るため完全な目隠しではない（レポート免責参照）。

判断ファイルの検証（ガードレール #5）:
  stop_loss / lot 等の禁止キーを含む出力・スキーマ違反・壊れ JSON はすべて
  本番同様 no_entry() に倒して記録する（クラッシュさせない）。
"""

from __future__ import annotations

import json
import time as _time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import structlog
from fxlab.registry import text_hash

from backend.src.llm.client import DEFAULT_DECISION_MODEL
from backend.src.llm.prompt import SYSTEM_PROMPT, PromptContext, build_user_prompt
from backend.src.llm.trade_decision import TradeDecision, no_entry
from backtest.context import DEFAULT_EQUITY_JPY
from backtest.data import KlinesStore
from backtest.engine import NIGHT_SESSION, SessionWindow
from backtest.report import reserve_run_dir
from backtest.runner import check_oos, iter_trading_days
from backtest.sources import DecisionOutput

_log = structlog.get_logger(__name__)

# 本番判断バッチ（DECISION_MODEL 既定値）と同一モデルをピン留め（設計書 §4.1）。
# .claude/agents/backtest-decider.md の frontmatter と一致していることを
# tests/backtest/test_subagent.py のドリフトテストで担保する。
BACKTEST_DECIDER_MODEL = DEFAULT_DECISION_MODEL

# 判断 JSON に含まれてはならないキー（AGENTS.md #5: SL・ロットは Python 決定論）
FORBIDDEN_DECISION_KEYS = ("stop_loss", "stopLoss", "lot", "position_size", "v_raw", "v_safe")

_WEEKDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]

REQUESTS_DIR = "requests"
DECISIONS_DIR = "decisions"
MANIFEST_FILE = "manifest.json"
RUN_META_FILE = "run_meta.json"


@dataclass(frozen=True)
class PrepareResult:
    run_id: str
    run_dir: Path
    prepared: int
    skipped_days: list[date]  # 全スキップ日（context_skipped_days + edge_guard_skipped）
    context_skipped_days: list[date]  # コンテキスト構築不能（休場・キャッシュ欠損）
    edge_guard_skipped: list[tuple[date, str]]  # RD-8 エッジガード（G1〜G3）: (date, reason)


@dataclass(frozen=True)
class CheckResult:
    """decide フェーズの完了検査（中断→再開・先読み禁止の事後検証）。"""

    total: int
    pending: list[str]  # decisions 未生成の name（昇順）
    unparsable: list[str]  # JSON として読めない decisions
    order_violations: list[str]  # mtime が直前より古い name（昇順処理違反の疑い）

    @property
    def ok(self) -> bool:
        return not (self.pending or self.unparsable or self.order_violations)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def prepare_run(
    store: KlinesStore,
    runs_dir: Path,
    start: date,
    end: date,
    *,
    blind: bool = True,
    equity_jpy: float = DEFAULT_EQUITY_JPY,
    session: SessionWindow = NIGHT_SESSION,
    unlock_oos: bool = False,
    run_id: str | None = None,
) -> PrepareResult:
    """営業日ごとの判断リクエスト一式を生成する（phase 1: prepare）。

    OOS 封印は prepare 段階で適用する（判断生成自体が OOS への接触のため）。

    Raises:
        OosLockedError: OOS 期間に触れる window を unlock_oos なしで prepare した場合。
    """
    check_oos(start, end, unlock_oos=unlock_oos, stage="prepare 段階")

    resolved_run_id = run_id or (f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}-subagent")
    run_dir = reserve_run_dir(runs_dir, resolved_run_id)
    (run_dir / REQUESTS_DIR).mkdir()
    (run_dir / DECISIONS_DIR).mkdir()

    entries: list[dict[str, str]] = []
    context_skipped: list[date] = []
    edge_guard_skipped: list[tuple[date, str]] = []
    index = 0
    for td in iter_trading_days(store, start, end, equity_jpy=equity_jpy, session=session):
        day, day_ctx = td.day, td.context
        if day_ctx is None:
            context_skipped.append(day)
            continue
        if td.edge_guard is not None and td.edge_guard.skip:
            edge_guard_skipped.append((day, td.edge_guard.reason or "unknown"))
            continue
        index += 1
        name = f"{index:04d}" if blind else day.isoformat()
        request = _build_request(
            name=name,
            index=index,
            ctx=day_ctx.prompt_context,
            weekday=_WEEKDAY_NAMES[day.weekday()],
            blind=blind,
        )
        _atomic_write_json(run_dir / REQUESTS_DIR / f"{name}.json", request)
        entries.append({"name": name, "date": day.isoformat()})

    manifest = {
        "blind": blind,
        "entries": entries,  # 昇順（時系列）。サブエージェントには読ませない
        "skipped": [d.isoformat() for d in context_skipped],
        "edge_guard_skipped": [
            {"date": d.isoformat(), "reason": reason} for d, reason in edge_guard_skipped
        ],
    }
    _atomic_write_json(run_dir / MANIFEST_FILE, manifest)
    _atomic_write_json(
        run_dir / RUN_META_FILE,
        {
            "run_id": resolved_run_id,
            "source": "subagent",
            "model": BACKTEST_DECIDER_MODEL,
            "blind": blind,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "equity_jpy": equity_jpy,
            "session": session.as_dict(),  # run --run-id が同一窓で再生するための記録（RD-1）
            "system_prompt_sha": text_hash(SYSTEM_PROMPT),  # config_hash 用（§7-6）
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    _log.info(
        "backtest_prepare_done",
        run_id=resolved_run_id,
        prepared=len(entries),
        skipped=len(context_skipped) + len(edge_guard_skipped),
        blind=blind,
        timestamp_ms=int(_time.time() * 1000),
        llm_tokens=None,
    )
    all_skipped = sorted(context_skipped + [d for d, _ in edge_guard_skipped])
    return PrepareResult(
        run_id=resolved_run_id,
        run_dir=run_dir,
        prepared=len(entries),
        skipped_days=all_skipped,
        context_skipped_days=context_skipped,
        edge_guard_skipped=edge_guard_skipped,
    )


def _build_request(
    name: str, index: int, ctx: PromptContext, weekday: str, blind: bool
) -> dict[str, object]:
    """1 営業日分のリクエスト。``SYSTEM_PROMPT`` と ``build_user_prompt`` は本番と同一。

    ブラインドモードでも user_prompt はそのまま使う。判断バッチは
    ``decision_date_jst`` があるとき現在時刻行を ``M/D HH:MM (曜)`` にする（#376）。
    バックテストの ``build_day_context`` はそれを空のままにするので、
    現在時刻行は時刻と曜日のみで、判断日の月日は出ない
    （tests/backtest/test_subagent.py で担保）。
    """
    return {
        "name": name,
        "label": f"Day {index} ({weekday})",
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt": build_user_prompt(ctx),
        "output_schema": TradeDecision.model_json_schema(),
        "output_instructions": (
            "output_schema に適合する JSON オブジェクトのみを "
            f"decisions/{name}.json に書き込むこと。"
            "stop_loss・ロット数は出力しない（Python が決定論的に算出する）。"
            "見送り時は direction=NO_ENTRY / execution_required=false。"
        ),
    }


def parse_decision_payload(text: str, name: str) -> TradeDecision:
    """decisions/<name>.json の中身を検証済み TradeDecision に変換する。

    失敗はすべて本番同様 no_entry に倒す（AGENTS.md #5・付録 A.1 のパース失敗時挙動）:
      - JSON として読めない / オブジェクトでない
      - 禁止キー（stop_loss / lot 等）を含む — LLM に SL・ロットを出させない
      - スキーマ違反（pydantic ValidationError）
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return no_entry(f"subagent 判断のパース失敗 ({name}): {exc}")
    if not isinstance(payload, dict):
        return no_entry(f"subagent 判断が JSON オブジェクトでない ({name})")

    present_forbidden = [k for k in FORBIDDEN_DECISION_KEYS if k in payload]
    if present_forbidden:
        return no_entry(
            f"subagent 判断に禁止キー {present_forbidden} が含まれる ({name})。"
            "stop_loss/ロットは Python が決定論算出する（ガードレール #5）"
        )

    try:
        return TradeDecision.model_validate(payload)
    except Exception as exc:  # pydantic.ValidationError を含む
        return no_entry(f"subagent 判断のスキーマ違反 ({name}): {str(exc)[:200]}")


def load_manifest(run_dir: Path) -> dict[str, object]:
    return json.loads((run_dir / MANIFEST_FILE).read_text(encoding="utf-8"))


def load_run_meta(run_dir: Path) -> dict[str, object]:
    return json.loads((run_dir / RUN_META_FILE).read_text(encoding="utf-8"))


def check_run(run_dir: Path) -> CheckResult:
    """decide フェーズの完了・順序・パース可否を検査する。

    順序検査（設計書 §6.2）: manifest の時系列順に decisions の mtime（ns 精度）が
    非減少であること。違反はチャンク再実行の対象（先読みの疑い）。

    注意: この検査は改ざん検知のヒューリスティック（defense-in-depth）である。
    decisions は判断エージェント自身が書くため、ファイル内に連番等の「順序の自己申告」を
    持たせても信頼根拠にはならず、OS 観測値である mtime_ns が最良の帯域外シグナルになる。
    順序の構造的な保証は /backtest コマンドのプロトコル
    （チャンク逐次・並列起動禁止・違反チャンクの decisions 破棄と再実行）が担う。
    """
    manifest = load_manifest(run_dir)
    entries = manifest["entries"]
    if not isinstance(entries, list):
        # assert は python -O で消えるため、壊れた manifest は例外で fail-closed にする
        raise ValueError(f"manifest.entries が list でない（壊れた manifest）: {run_dir}")

    pending: list[str] = []
    unparsable: list[str] = []
    order_violations: list[str] = []
    prev_mtime_ns: int | None = None

    for entry in entries:
        name = str(entry["name"])
        path = run_dir / DECISIONS_DIR / f"{name}.json"
        if not path.exists():
            pending.append(name)
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            unparsable.append(name)
        mtime_ns = path.stat().st_mtime_ns  # 秒精度だと同一秒内の逆順を見逃す
        if prev_mtime_ns is not None and mtime_ns < prev_mtime_ns:
            order_violations.append(name)
        prev_mtime_ns = mtime_ns

    return CheckResult(
        total=len(entries),
        pending=pending,
        unparsable=unparsable,
        order_violations=order_violations,
    )


class SubagentFileSource:
    """decisions/ ディレクトリを消費する DecisionSource（phase 3: simulate 用）。

    llm_tokens はサブエージェント経由では取得できないため None
    （0 と偽らない・§5.2）。model は run_meta のピン留め値を記録する。
    """

    name: str = "subagent"

    def __init__(self, run_dir: Path) -> None:
        self._run_dir = run_dir
        manifest = load_manifest(run_dir)
        entries = manifest["entries"]
        if not isinstance(entries, list):
            raise ValueError(f"manifest.entries が list でない（壊れた manifest）: {run_dir}")
        self._name_by_date = {str(e["date"]): str(e["name"]) for e in entries}
        meta = load_run_meta(run_dir)
        self._model = str(meta["model"])
        # 判断に使ったプロンプト。run_meta に無い古い run は先頭リクエストから求める
        sha = meta.get("system_prompt_sha")
        if sha is None and entries:
            first = run_dir / REQUESTS_DIR / f"{entries[0]['name']}.json"
            if first.exists():
                sha = text_hash(json.loads(first.read_text(encoding="utf-8"))["system_prompt"])
        self.prompt_sha: str | None = str(sha) if sha is not None else None

    def decide(self, day: date, ctx: PromptContext) -> DecisionOutput:
        name = self._name_by_date.get(day.isoformat())
        if name is None:
            return DecisionOutput(
                decision=no_entry(f"subagent: manifest に {day.isoformat()} がない"),
                model=self._model,
            )
        path = self._run_dir / DECISIONS_DIR / f"{name}.json"
        if not path.exists():
            # run 前の check_run で検出されるのが正道。ここは最終フォールバック
            return DecisionOutput(
                decision=no_entry(f"subagent: decision 未生成 ({name})"), model=self._model
            )
        decision = parse_decision_payload(path.read_text(encoding="utf-8"), name)
        return DecisionOutput(decision=decision, llm_tokens=None, model=self._model)
