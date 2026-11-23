# staging 環境の運用設定。
#
# このファイルは Terraform が自動で読み込む（*.auto.tfvars）。素の `terraform apply` でこの値が使われる。
# 運用設定は `-var` で渡さない。`-var` はこのファイルより優先されるため一時的に効き、
# 次の素の apply でこのファイルの値に戻ってしまう。設定を変えるときはこのファイルを編集して PR を出す。
#
# モジュール側（modules/serverless_app/variables.tf）の既定値は安全側
# （スケジュール無効・allow_live_orders=false・trading_phase="1"）。
# このファイルは「この環境では明示的に設定する」という宣言で、モジュールの既定は変えない。
# 新しい環境やテストは、何も設定しなければ発注しない状態から始まる。
#
# 値は tests/infra/test_operating_posture.py が固定している。変えるときはテストも同じ PR で更新する。
#
# 公開版の値は「すべてのスケジュールを止め、実発注もしない」停止状態。
# 運用を再開するときは、先に手仕舞いと監視を有効にし、そのあと判断と trading_phase を戻す
# （判断だけが動いて手仕舞いが動かない状態を作らないため）。

# 判断（20:30 JST）
schedule_state = "DISABLED"
# 手仕舞い（4:30 JST）。判断とは別の変数。
closeout_schedule_state = "DISABLED"
# 手仕舞いの欠測監視（5:00 JST）。建玉と注文が 0 のときだけ止める。
watchdog_schedule_state = "DISABLED"

# 実発注の two-key interlock（AGENTS.md の安全ガードレール 1）。実発注には両方が必要。
# 公開版は両方とも安全側（このまま apply しても発注系 POST は送られない）。
# 実取引に切り替えるときは allow_live_orders = true と trading_phase = "2" を両方そろえる。
allow_live_orders = false
trading_phase     = "1"

# ロンドン ORB デモ（実発注なし）。
# orb_schedule_state はスケジュールの有効・無効、orb_demo_enabled は Lambda 側のスイッチ。
# スケジュールを止めるだけなら orb_demo_enabled は true のままにする
# （false にすると Lambda の更新が差分に入り、再開時の変更が増えるため）。
orb_schedule_state = "DISABLED"
orb_demo_enabled   = true
