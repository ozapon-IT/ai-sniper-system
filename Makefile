.PHONY: install install-backtest lint test fmt

# 🔴 ランタイム依存は **Lambda の pin から**入れる（#322）。
# pyproject の `>=` から解決すると CI が本番と違う SDK を検証してしまう
# （#320 で anthropic 1.0.0 を引いて CI が全滅。上限を付けた後も
#  CI 0.125.0 / 本番 0.112.0 の乖離が残っていた）。
#   1) ランタイムを Lambda pin から
#   2) editable は --no-deps（再解決で pin を上書きさせない）
#   3) dev ツールにも -c を効かせる（推移依存が pin を巻き戻さないように）
# ⚠️ boto3 / botocore は Lambda ランタイム同梱のため pin では揃わない（#322）。
#    本番の版は AWS が決める。テストは全モックのため実害は小さい。
#    → ランタイムの pin 対象に**入れない**（tests/infra/test_runtime_pins.py が固定）。
#    CI の再現性のための固定だけ requirements-dev.txt 側で行う（本番検証にはならない）。
# dev ツール（mypy / ruff / pytest 系）は requirements-dev.txt + constraints-dev.txt で
# **exact pin**（#323）。上限（`mypy<3` 等）ではマイナー更新を止められないため。
install:
	pip install -r infra/terraform/lambda-requirements.txt -c infra/terraform/lambda-constraints.txt
	pip install -e . --no-deps
	pip install -r requirements-dev.txt \
		-c infra/terraform/lambda-constraints.txt \
		-c constraints-dev.txt

# ローカルバックテスト用（fxlab・プライベートリポジトリ。GitHub の読み取り権限が要る）。
# 入れないと tests/backtest は test_guardrails.py 以外を収集しない（tests/backtest/conftest.py）。
install-backtest:
	pip install -r requirements-backtest.txt

lint:
	ruff check backend/ backtest/ scripts/backtest.py tests/
	mypy backend/src/

fmt:
	ruff format backend/ backtest/ tests/

test:
	pytest
