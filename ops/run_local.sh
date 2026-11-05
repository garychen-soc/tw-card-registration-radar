#!/bin/bash
# 本機排程入口：更新資料 → 驗收 → commit → push（GitHub Pages 由 deploy.yml 在 push 後建置）。
#
# 任何一步失敗都不 push：寧可網站停在上一版，也不發布沒驗過的資料。
# 退出碼：0 已發布或無變化；4 來源大量失敗；5 golden 精確率未達門檻；其他＝程式錯誤。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$HOME/Library/Logs/tw-card-radar"
BRANCH="${RADAR_BRANCH:-main}"
LLM_BUDGET="${RADAR_LLM_BUDGET:-120}"
# golden 精確率：低於 WARN 通知（多半是官方頁換內容、golden 該更新）；低於 BLOCK 才不發布
WARN_PRECISION="${RADAR_WARN_PRECISION:-0.98}"
MIN_PRECISION="${RADAR_MIN_PRECISION:-0.90}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin"

mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/run-$(date +%Y%m%d).log" 2>&1
echo "==== $(date '+%F %T') start"

notify() {
  /usr/bin/osascript -e "display notification \"$1\" with title \"刷卡登錄雷達\"" || true
}

cd "$REPO"
# 同一時間只允許一個更新程序（shlock 會清掉已不存在 PID 的殘留鎖）
LOCK="$REPO/state/.run.lock"
if ! /usr/bin/shlock -f "$LOCK" -p $$; then
  echo "another run holds the lock; skip"
  exit 0
fi
trap 'rm -f "$LOCK"' EXIT

git switch -q "$BRANCH"
git pull -q --rebase origin "$BRANCH"

set +e
.venv/bin/radar run --llm-budget "$LLM_BUDGET"
status=$?
set -e
if [ "$status" -ne 0 ]; then
  notify "本輪未發布（radar run 退出碼 $status），詳見 $LOG_DIR"
  exit "$status"
fi

.venv/bin/python -m pytest -q
precision="$(.venv/bin/radar score | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["precision"])')"
echo "golden precision=$precision"
if ! .venv/bin/python -c "import sys; sys.exit(0 if float('$precision') >= float('$WARN_PRECISION') else 1)"; then
  notify "golden 精確率 $precision，請檢查是否有官方頁換了內容（照常發布）"
fi
if .venv/bin/python -c "import sys; sys.exit(0 if float('$precision') >= float('$MIN_PRECISION') else 1)"; then
  :
else
  git checkout -- state web/public
  notify "golden 精確率 $precision 低於 $MIN_PRECISION，本輪不發布"
  exit 5
fi

npm --prefix web run build >/dev/null

git add state web/public
if git diff --cached --quiet; then
  echo "no data change"
  exit 0
fi
git commit -q -m "data: refresh $(date '+%F %H:%M')"
git push -q origin "$BRANCH"
echo "==== $(date '+%F %T') published"
