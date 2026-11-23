---
description: GMO FX API の実仕様を安全に確認し仕様書「付録B」に記録する
argument-hint: "[E04|E05|E06 など対象スパイク]"
---

引数: ユーザーがコマンド名の直後にチャットで渡した対象スパイク名（Claude Code では自動展開される）。以下「上記引数」と呼ぶ。空でも可。

GMO FX API の実仕様を安全に確認し、仕様書「付録B」に記録する（Issue E04〜E06 用）。対象: 上記引数

## 原則
- **参照系 GET から**。発注系 POST はドライ/最小ロットでのみ、かつ明示確認後。
- 実レスポンスを見るまで実装に進まない。推測でフィールド名を書かない。

## 手順
1. **認証確認**: `GET /private/v1/account/assets` が 200 を返すこと。署名パス `/v1/account/assets`（`/private` なし）で署名できているか。401 なら署名パスを疑う。
2. **Public 形式確認**:
   - `GET /public/v1/symbols` → USD/JPY の tick size・最小注文/最小乖離を記録。
   - `GET /public/v1/klines`（`symbol=USD_JPY`, `interval=1day`/`4hour`, `priceType`, `date`）→ 日足・4時間足の実フィールドを記録。
3. **発注系（慎重に）**:
   - `POST /private/v1/ifoOrder` を **最小ロット（100通貨）で 1 回だけ** 疎通。IFD-OCO ボディ構造・`executionType` 等の実フィールド、`order_type`(LIMIT/STOP) のマッピングを確認。
   - 直後に手仕舞い手順（cancel → close）で建玉/注文を必ずクリーンアップ。
4. **記録**: 取得した実レスポンス（フィールド名・型・例値）を `docs/system_requirements.md` の「付録B: 実API レスポンス記録」に追記（見出しが無ければ新規作成）。
5. 記録を基に該当実装 Issue へ進む。

## 厳守
- レートリミット（POST 1/秒・GET 6/秒）。連続 POST 間に `sleep(>=1s)`。
- シークレットはログ出力しない。`.env` はコミットしない。
