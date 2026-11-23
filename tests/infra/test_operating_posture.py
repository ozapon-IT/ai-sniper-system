"""tests/infra/test_operating_posture.py — staging の運用姿勢が宣言されていることを固定する。

🔴 なぜ必要か（2026-08-13 に発見した実際の穴）:

`schedule_state` / `allow_live_orders` / `trading_phase` の実値はどこにも保存されておらず、
`terraform apply -var=... -var=... -var=...` で都度渡す運用だった。README のデプロイ手順は
素の `terraform apply` だったため、**手順どおりに実行すると 2 つの cron が DISABLED になり
two-key も閉じる**（＝システムが黙って停止する）。しかも cron 停止は監視で検知できない
（起動しないので Errors も DroppedCount も出ない）。

`phase2.auto.tfvars`（terraform が自動ロード）に値を置いて解消した。このファイルが
消える / 値が安全側に戻ると同じ事故が再発するため、テストで固定する。

📌 **運用姿勢を変える PR では、このテストも同じ PR で更新すること。**
落ちるのは仕様どおりの挙動であり、姿勢変更がレビューを経ずに起きないための歯止め。

⚠️ **これは「本番が実際にどうなっているか」の検査ではない**（terraform state は見ない）。
宣言ファイルが存在し意図した値であることだけを見る。実インフラとの一致は plan で確認する。
"""

from __future__ import annotations

import re
from pathlib import Path

_TFVARS = (
    Path(__file__).resolve().parents[2]
    / "infra" / "terraform" / "environments" / "staging" / "phase2.auto.tfvars"
)


def _values() -> dict[str, str]:
    text = _TFVARS.read_text()
    return {
        m.group(1): m.group(2).strip().strip('"')
        for m in re.finditer(r"^\s*([a-z_]+)\s*=\s*(.+)$", text, re.M)
    }


def test_auto_tfvars_exists() -> None:
    """自動ロードされる宣言ファイルが存在すること。

    消すと素の `terraform apply` がモジュール既定（安全側）に落ち、cron が止まる。
    """
    assert _TFVARS.exists(), f"運用姿勢の宣言ファイルが無い: {_TFVARS}"


def _protection_holds(decision: str, closeout: str, watchdog: str) -> bool:
    """判断 ENABLED なら手仕舞いも監視も ENABLED。手仕舞い ENABLED なら監視も ENABLED。"""
    decision_ok = decision != "ENABLED" or (
        closeout == "ENABLED" and watchdog == "ENABLED"
    )
    closeout_ok = closeout != "ENABLED" or watchdog == "ENABLED"
    return decision_ok and closeout_ok


def test_two_key_posture() -> None:
    """公開版の two-key。allow_live_orders=false・phase "1" の両方とも安全側。

    実発注は AND（live_orders_allowed）。どちらか片方だけでも ifoOrder / closeOrder /
    cancelOrders は送られない。実取引には両方を切り替える必要がある。
    """
    v = _values()
    assert v.get("allow_live_orders") == "false", f"allow_live_orders が安全側でない: {v}"
    assert v.get("trading_phase") == "1", f"計画停止の phase が 1 でない: {v}"
    assert v.get("orb_demo_enabled") == "true", (
        f"ORB デモのマスタースイッチが宣言されていない/閉じている: {v}。"
        "スケジュール停止で false にしない（ORB の Lambda env が変わる）"
    )


def test_schedule_posture() -> None:
    """2026-11 計画停止。判断・手仕舞い・watchdog・ORB はそれぞれ DISABLED。"""
    v = _values()
    assert v.get("schedule_state") == "DISABLED", f"判断 cron が計画停止でない: {v}"
    assert v.get("closeout_schedule_state") == "DISABLED", (
        f"手仕舞い cron が計画停止でない: {v}"
    )
    assert v.get("watchdog_schedule_state") == "DISABLED", (
        f"ウォッチドッグが計画停止でない: {v}。"
        "建玉 0 のときだけ止める（#295 / #371）"
    )
    # 🔴 姿勢は phase2.auto.tfvars で宣言する。`-var` で渡すと次の素の apply で
    #    この宣言に戻り、意図しない再開・停止になる（#294 型）。
    assert v.get("orb_schedule_state") == "DISABLED", (
        f"ORB cron が計画停止でない: {v}。"
        "止めるときも phase2.auto.tfvars を変えて PR する（-var 禁止）"
    )


def test_schedule_posture_obeys_protection_implication() -> None:
    """宣言値が「判断 ENABLED ⇒ 手仕舞いも監視も ENABLED」を満たすこと。"""
    v = _values()
    assert _protection_holds(
        v.get("schedule_state", ""),
        v.get("closeout_schedule_state", ""),
        v.get("watchdog_schedule_state", ""),
    ), f"スケジュールの含意が崩れている: {v}"


def test_protection_implication_cases() -> None:
    """含意そのもの。計画停止（全部 DISABLED）と、判断を戻さず手仕舞いだけ戻す形を許す。"""
    assert _protection_holds("DISABLED", "DISABLED", "DISABLED")
    assert _protection_holds("DISABLED", "DISABLED", "ENABLED")
    assert _protection_holds("DISABLED", "ENABLED", "ENABLED")
    assert _protection_holds("ENABLED", "ENABLED", "ENABLED")
    assert not _protection_holds("ENABLED", "DISABLED", "DISABLED")
    assert not _protection_holds("ENABLED", "DISABLED", "ENABLED")
    assert not _protection_holds("ENABLED", "ENABLED", "DISABLED")
    assert not _protection_holds("DISABLED", "ENABLED", "DISABLED")


def test_module_validation_encodes_schedule_implication() -> None:
    """含意は tfvars テストだけでなく、モジュールの cross-variable validation にある。"""
    mod = (
        Path(__file__).resolve().parents[2]
        / "infra"
        / "terraform"
        / "modules"
        / "serverless_app"
        / "variables.tf"
    ).read_text()
    assert 'var.schedule_state != "ENABLED" || var.closeout_schedule_state == "ENABLED"' in mod
    assert 'var.closeout_schedule_state != "ENABLED" || var.watchdog_schedule_state == "ENABLED"' in mod
    assert 'var.schedule_state != "ENABLED" || var.watchdog_schedule_state == "ENABLED"' in mod


def test_module_defaults_stay_safe() -> None:
    """🔴 モジュール側の既定は**安全側のまま**であること（AGENTS.md #1）。

    auto.tfvars は「staging 環境が明示的にオプトインする」宣言であり、
    モジュールの既定を書き換えるのとは意味が違う。既定まで危険側にすると、
    新しい呼び出し元（別環境・テスト）が黙って実発注可能になる。
    """
    mod = (
        Path(__file__).resolve().parents[2]
        / "infra" / "terraform" / "modules" / "serverless_app" / "variables.tf"
    ).read_text()

    for var, expected in [
        ("schedule_state", '"DISABLED"'),
        ("closeout_schedule_state", '"DISABLED"'),
        ("watchdog_schedule_state", '"ENABLED"'),
        ("allow_live_orders", "false"),
        ("trading_phase", '"1"'),
    ]:
        block = re.search(rf'variable "{var}" \{{(.*?)\n\}}', mod, re.S)
        assert block, f"variable {var} が見つからない"
        default = re.search(r"^\s*default\s*=\s*(.+)$", block.group(1), re.M)
        assert default, f"variable {var} に default が無い"
        assert default.group(1).strip() == expected, (
            f"🔴 モジュール既定 {var} が安全側でない: {default.group(1).strip()} "
            f"(期待 {expected})。AGENTS.md #1 の two-key を弱める変更。"
        )
