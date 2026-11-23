"""LLM 入力コンテキスト PromptContext とプロンプト整形（#16, §3.3）。

Python が組み立てて LLM に渡す文脈。LLM は stop_loss/ロットを決めない（§3.2）。
±30分の指標回避は day-skip hard rule（§3.3）で Python が実施。
保有窓内で和集合に入らない指標だけを、日付付き JST でプロンプトに渡す。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from backend.src.adapters.macro_context import MacroResult, format_macro_line
from backend.src.engine.trading_day import jst_month_day, jst_time_label
from backend.src.llm.sl_profile import (
    SL_PROFILE_TO_ATR,
    compute_recommended_sl_profile,
    session_range_over_atr,
)


@dataclass(frozen=True)
class EconomicEvent:
    """高インパクト経済指標（USD/JPY のみ抽出済み, §3.3）。

    forecast/previous はプロンプトには使わないが、BT-5 リプレイ用に S3 ログへ残す
    （カレンダー API は this-week のみ返すため後から再取得できない。Issue #163）。
    """

    time_jst: str  # "21:30"（HH:MM のまま。日付は date_jst）
    currency: str  # "USD" / "JPY"
    name: str
    impact: str  # "High"
    forecast: str | None = None
    previous: str | None = None
    date_jst: str = ""  # "YYYY-MM-DD"。無い過去ログ・未設定は空（#374）


@dataclass(frozen=True)
class PromptContext:
    """朝の意思決定 LLM への入力（#16 で確定）。"""

    current_time_jst: str
    day_of_week: str
    symbol: str
    current_price_mid: float
    pivot_p: float
    pivot_r1: float
    pivot_r2: float
    pivot_s1: float
    pivot_s2: float
    atr_14d: float
    prev_day_change: float
    equity_jpy: float
    economic_events: list[EconomicEvent] = field(default_factory=list)
    recent_4h_candles: list[dict[str, str]] = field(default_factory=list)
    us_thin_market: bool = False
    # §3.3 補助入力（Issue #107）。取得失敗時は None（degrade — NO_ENTRY に倒さない）
    macro_context: MacroResult | None = None
    # 当日セッション情報（06:00 JST 起点〜判断時刻の確定足から算出。夜型の当日モメンタム判断用）。
    # RD-2 #167 で session_open を先行定義し、RD-5 #170 で high/low を追加して本番プロンプトに
    # 当日セッション情報として描画する（東京＋ロンドン序盤の文脈）。取得できない場合は None
    # （degrade — 描画を省くだけで NO_ENTRY には倒さない）。
    session_open: float | None = None
    session_high: float | None = None
    session_low: float | None = None
    # 東京/欧州分割（06:00〜15:00 / 15:00〜判断時刻, RD-9 #195）。取得できない側は None（degrade）。
    tokyo_open: float | None = None
    tokyo_high: float | None = None
    tokyo_low: float | None = None
    europe_open: float | None = None
    europe_high: float | None = None
    europe_low: float | None = None
    # 判断日（JST・YYYY-MM-DD）。空でないときだけ現在時刻行に月日を出す（#376）。
    # 判断バッチだけ埋める。バックテストは空のまま（絶対日付・月日を出さない）。
    decision_date_jst: str = ""


SYSTEM_PROMPT = (
    "あなたは USD/JPY 専用の予約型トレード判断エンジンです。"
    "夜（日本時間 20:30 前後）に判断し、翌朝までの保有を前提に IFD-OCO の新規エントリー方針を決めます。"
    "当日の東京＋ロンドン序盤の値動き（当日セッション情報）を方向判断の主材料とし、"
    "Python が算出した数理レール（Pivot/ATR）の上でエントリー方向と価格を決めます。\n"
    "厳守事項:\n"
    "- まず sentiment_summary と rationale で市場の根拠を述べ、その後に direction 以降を決める。\n"
    "- 出力できるのは sentiment_summary / rationale / direction / referenced_pivot_level / "
    "order_type / entry_price / sl_profile / rr_target / execution_required のみ。\n"
    "- 損切り価格(stop_loss)・利確価格(take_profit)・ロット数は **出力しない**。"
    "Python が以下の式で決定論的に算出する:\n"
    "    ATR_h = ATR(14d) × √(8/24)（保有 8 時間相当のボラティリティ）\n"
    "    stop_loss  = entry ∓ atr_multiplier × ATR_h  （LONG は −、SHORT は +）\n"
    "    take_profit = entry ± rr_target × |entry − stop_loss|  （LONG は +、SHORT は −）\n"
    "- sl_profile は損切り幅の離散ノブ（**必ず指定**）。Python が atr_multiplier へマップする:\n"
    "    standard → atr_multiplier 0.7（通常ボラ）\n"
    "    wide → atr_multiplier 0.8（高ボラ・当日レンジ拡大など SL 拡大が妥当な局面）\n"
    "ユーザープロンプトの「ボラティリティシグナル」に推奨 sl_profile（STANDARD / WIDE）が示される。"
    "推奨と市場文脈が整合するときはそれに従い、明確に反する根拠があるときのみ逆を選ぶ。\n"
    "- rr_target はリスクリワード目標。1.0〜1.5 の範囲で指定する（未指定・範外は Python が既定 1.0 / クランプ）。"
    "既定は 1.0。トレンド継続・明確な方向根拠があるときのみ 1.2 を指定する（安易な 1.2 引き上げは避ける）。"
    "系は R:R 下限（MIN_RR）検証を通すため、rr_target を下限未満にはできない。\n"
    "- entry_price は現値近傍を基本とする: 現値から概ね 0.1×ATR(14d) 以内の押し目/戻りを狙う LIMIT を第一候補にする。"
    "STOP（ブレイク追随）は強い根拠がある場合のみ用いる。"
    "現値から 0.3×ATR(14d) を超える entry は発注前検証で棄却されるため避ける。\n"
    "- entry_price の向きと最小距離は hard 条件（違反すると発注前検証で棄却される・#238）:\n"
    "    LONG（LIMIT）は entry_price ≤ 現値（押し目を待つ）\n"
    "    SHORT（LIMIT）は entry_price ≥ 現値（戻りを待つ）\n"
    "  かつ現値から **最小 0.3 pips（0.003 円）以上** 離すこと。現値に貼り付けた entry"
    "（例: 現値 162.4905 に対し 162.49）は最小乖離違反・方向違反で棄却される。"
    "実用上は現値から 1 pip 以上離した水準を選ぶこと。\n"
    "- 見送り（NO_ENTRY / execution_required=false）は積極的理由がある場合に限る:"
    "方向根拠の矛盾・不明瞭、高インパクト指標の近接・保有中リスク。"
    "セッション分割・東京レンジブレイク（レンジ内含む）は方向判断の参考情報であり、"
    "レンジ内であること自体は見送り理由にしない"
    "（シグナルが薄い日は判断バッチ冒頭の hard rule で既に除外済み）。\n"
    "- rationale は日本語で簡潔に（200文字以内）。\n"
    "ユーザープロンプトの指標発表時刻は JST である。タイムゾーンを自分で補完・変換しない"
    "（UTC と読まない・時差を足し引きしない）。"
    "誤った TZ で読むと保有窓内の指標を窓の外と誤認する。\n"
    "保有窓（約8時間・20:30→翌4:30 JST）内の主要イベント（時刻は JST・冬時間は+1h）:\n"
    "- 21:30（冬22:30） 米国高インパクト指標: 発表直前エントリーは避ける（hard skip は米国東部 "
    "7:30〜10:00（夏は JST 20:30〜23:00、冬は JST 20:30〜翌 0:00）を処理済み）。"
    "保有中の乱高下リスクを rationale に織り込む\n"
    "- 23:00（冬24:00） NYカット（オプション権利行使）: 大口オプション設定レート付近では磁力・"
    "通過後の急変に注意\n"
    "- 24:00（冬25:00） ロンドンフィキシング: 大口フローで反転または一方向推進が起きやすい帯\n"
    "JST 固定 cron のため保有中の NY 時間帯イベントは DST でずれる（スケジュール変更はしない）。"
)


def _format_4h_candles(candles: list[dict[str, str]]) -> str:
    """直近 4 時間足を 1 行サマリーとしてプロンプトに埋め込む。足がない場合は空文字列。"""
    if not candles:
        return ""
    lines = []
    for c in candles[-6:]:  # 直近 6 本（24 時間分）だけ表示
        close_raw = c.get("close")
        open_raw = c.get("open")
        try:
            close_f = float(close_raw) if close_raw is not None else None
            open_f = float(open_raw) if open_raw is not None else None
        except (TypeError, ValueError):
            close_f = open_f = None
        direction = (
            "?" if close_f is None or open_f is None else ("↑" if close_f >= open_f else "↓")
        )
        lines.append(f"    close={close_raw or '?'} {direction}")
    return "4h 直近足(古→新):\n" + "\n".join(lines) + "\n"


def _format_session(ctx: PromptContext) -> str:
    """当日セッション情報（06:00 JST〜判断時刻）を 1 ブロックで描画する（RD-5 #170, §3.2/§3.3）。

    始値が取れないとき（degrade）は空文字列を返し、本番プロンプトから当該ブロックを省く。
    当日騰落は始値→現在値の変化率（東京＋ロンドン序盤の当日モメンタム）。高値/安値は
    取得できたものだけ描画する（判断時刻までの確定足のみで算出済み・未来情報を含まない）。
    """
    if ctx.session_open is None:
        return ""
    parts = [f"始値={ctx.session_open}"]
    if ctx.session_high is not None:
        parts.append(f"高値={ctx.session_high}")
    if ctx.session_low is not None:
        parts.append(f"安値={ctx.session_low}")
    parts.append(f"現在値={ctx.current_price_mid}")
    if ctx.session_open != 0:
        change_pct = (ctx.current_price_mid - ctx.session_open) / ctx.session_open * 100
        parts.append(f"当日騰落={change_pct:+.2f}%")
    return "当日セッション(06:00〜, 東京+ロンドン序盤): " + " ".join(parts) + "\n"


def _format_split_session_block(ctx: PromptContext) -> str:
    """東京/欧州分割セッション（RD-9 #195, RD-11 #200）。分割データが無いときは空文字列（degrade）。"""
    if ctx.tokyo_open is None and ctx.europe_open is None:
        return ""
    lines: list[str] = [
        "（以下は方向判断の参考。レンジ内であっても見送り理由にしない）",
    ]
    if ctx.tokyo_open is not None:
        parts = [f"始値={ctx.tokyo_open}"]
        if ctx.tokyo_high is not None:
            parts.append(f"高値={ctx.tokyo_high}")
        if ctx.tokyo_low is not None:
            parts.append(f"安値={ctx.tokyo_low}")
        lines.append("東京(06:00〜15:00): " + " ".join(parts))
    if ctx.europe_open is not None:
        parts = [f"始値={ctx.europe_open}"]
        if ctx.europe_high is not None:
            parts.append(f"高値={ctx.europe_high}")
        if ctx.europe_low is not None:
            parts.append(f"安値={ctx.europe_low}")
        lines.append("欧州(15:00〜現在): " + " ".join(parts))
    break_label = _tokyo_range_break_label(ctx)
    if break_label is not None:
        lines.append(f"東京レンジブレイク: {break_label}")
    return "セッション分割:\n" + "\n".join(f"  {line}" for line in lines) + "\n"


def _tokyo_range_break_label(ctx: PromptContext) -> str | None:
    """欧州が東京レンジをブレイクしたか（方向参考・見送り理由にはしない, RD-11 #200）。"""
    if (
        ctx.tokyo_high is None
        or ctx.tokyo_low is None
        or ctx.europe_high is None
        or ctx.europe_low is None
    ):
        return None
    up = ctx.europe_high > ctx.tokyo_high
    down = ctx.europe_low < ctx.tokyo_low
    if up and down:
        return "上下両ブレイク"
    if up:
        return "上ブレイク"
    if down:
        return "下ブレイク"
    return "レンジ内"


def _format_volatility_signal(ctx: PromptContext) -> str:
    """ボラティリティシグナルブロック（RD-12 #203）。絶対日付なし・Python 決定論。"""
    recommended = compute_recommended_sl_profile(ctx.atr_14d)
    range_ratio = session_range_over_atr(ctx.session_high, ctx.session_low, ctx.atr_14d)
    lines = [
        "ボラティリティシグナル（Python 算出・sl_profile 選択の参考）:",
        f"  ATR(14d)={ctx.atr_14d}",
    ]
    if range_ratio is not None:
        lines.append(f"  当日レンジ幅/ATR14={range_ratio:.2f}")
    lines.append(f"  推奨 sl_profile: {recommended}")
    lines.append(
        f"  （マップ: standard→{SL_PROFILE_TO_ATR['standard']}, wide→{SL_PROFILE_TO_ATR['wide']}）"
    )
    return "\n".join(lines) + "\n"


_INDICATOR_NAME_MAX_LEN = 120


def _sanitize_indicator_name(name: object) -> str:
    """プロンプトに出す指標名。改行と制御文字を除き、長さを切る。

    フィードの ``title`` は外部入力。文字列でないとき（``None`` など）は
    ``str()`` してから同じ処理をする。``None`` は ``None`` と出る（PR 前と同じ）。
    TypeError で判断を止めない（#375 指摘 6）。改行があると箇条書きが割れる。
    ログとノートの ``name`` は無加工のまま（ここでは描画だけ）。
    """
    text = str(name)
    kept = "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in text)
    collapsed = " ".join(kept.split())
    return collapsed[:_INDICATOR_NAME_MAX_LEN]


def _format_economic_event(e: EconomicEvent) -> str:
    """1 件を ``M/D HH:MM JST（保有中） (通貨) 名称 [impact=…]`` で並べる（#345 / #374）。

    🔴 **時刻には必ず JST を添える。** ノート経路（#343）は ``23:00`` だけ渡して
    LLM が UTC を補完し、9 時間ずれた時刻が税務証跡に残った。

    ``（保有中）`` は月日と時刻が両方あるときだけ付ける（#376）。ラベル文字列の
    比較では決めない。窓の外は渡さないので、外である行は出ない。
    ``date_jst`` が空・読めないときは ``HH:MM JST`` のみ（日付を作らない）。
    ``time_jst`` は判断バッチが ``strftime("%H:%M")`` で書く素の ``HH:MM`` のまま。
    """
    month_day = jst_month_day(e.date_jst)
    time_label = jst_time_label(e.time_jst)
    if month_day and time_label:
        prefix = f"{month_day} {time_label}（保有中） "
    elif time_label:
        prefix = f"{time_label} "
    else:
        prefix = ""
    return f"  - {prefix}({e.currency}) {_sanitize_indicator_name(e.name)} [impact={e.impact}]"


def _current_time_value(ctx: PromptContext) -> str:
    """現在時刻行の値。月日があるときだけ ``M/D HH:MM (曜)``。"""
    clock = f"{ctx.current_time_jst} ({ctx.day_of_week})"
    month_day = jst_month_day(ctx.decision_date_jst)
    if not month_day:
        return clock
    return f"{month_day} {clock}"


def build_user_prompt(ctx: PromptContext) -> str:
    """PromptContext を LLM 用のユーザープロンプト文字列に整形する。"""
    if ctx.economic_events:
        events = "\n".join(_format_economic_event(e) for e in ctx.economic_events)
    else:
        events = "  - なし"

    # §3.3 補助入力: 取得できた指標のみ1行で描画（取得失敗時は省く）
    macro_line = ""
    if ctx.macro_context is not None:
        line = format_macro_line(ctx.macro_context)
        if line:
            macro_line = line + "\n"

    return (
        f"対象: {ctx.symbol}\n"
        f"現在時刻(JST): {_current_time_value(ctx)}\n"
        f"現在値(mid): {ctx.current_price_mid}\n"
        + _format_session(ctx)
        + _format_split_session_block(ctx)
        + f"前日騰落: {ctx.prev_day_change:+.3f}\n"
        f"有効純資産(参考・サイジングはしない): {ctx.equity_jpy}\n" + macro_line + "数理レール:\n"
        f"  P={ctx.pivot_p} R1={ctx.pivot_r1} R2={ctx.pivot_r2} "
        f"S1={ctx.pivot_s1} S2={ctx.pivot_s2}\n"
        f"  ATR(14d)={ctx.atr_14d}\n"
        + _format_volatility_signal(ctx)
        + _format_4h_candles(ctx.recent_4h_candles)
        + (
            "【注意】本日は US 祝日（薄商い）です。NY 勢の参加が少なく流動性が低い可能性があります。"
            "R:R 評価を保守的に、根拠が薄い場合は NO_ENTRY を優先してください。\n"
            if ctx.us_thin_market
            else ""
        )
        + "保有窓内の高インパクト指標(USD/JPY・時刻はJST):\n"
        f"{events}\n"
        "上記を踏まえ、IFD-OCO の新規エントリー方針を判断してください。"
    )
