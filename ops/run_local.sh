#!/bin/bash
# 本機排程入口：更新資料 → 驗收 → commit → push（GitHub Pages 由 deploy.yml 在 push 後建置）。
#
# 任何一步失敗都不 push：寧可網站停在上一版，也不發布沒驗過的資料。
# 失敗時一律：還原 state/ 與 web/public/ 的未發布變更（保留 LLM 抽取快取）並發通知，
# 確保下一輪能從乾淨的工作樹重新開始，不會因殘留變更讓 git pull 每輪卡死。
# 退出碼：0 已發布或無變化；4 來源大量失敗；5 golden 精確率未達門檻；6 claude CLI 登入失效；
#         其他＝程式或環境錯誤（log 會記下失敗的行號）。
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
# 已針對「本輪失敗」發過具體通知時，on_exit 不再重複發泛用通知
FAIL_NOTIFIED=0
fail_notify() {
  FAIL_NOTIFIED=1
  notify "$1"
}

# 還原本輪產生、尚未發布的資料。state/extractions 是以內容雜湊為鍵的 LLM 結果快取，
# 與發布與否無關，保留下來可避免下一輪重花 token（成功發布時會一併 commit）。
restore_tree() {
  if [ -d "$REPO/.git/rebase-merge" ] || [ -d "$REPO/.git/rebase-apply" ]; then
    git -C "$REPO" rebase --abort || true
  fi
  git -C "$REPO" checkout -- state web/public || true
  git -C "$REPO" clean -fdq -e extractions -- state web/public || true
}

cd "$REPO"
# 同一時間只允許一個更新程序（shlock 會清掉已不存在 PID 的殘留鎖）
LOCK="$REPO/state/.run.lock"
if ! /usr/bin/shlock -f "$LOCK" -p $$; then
  echo "another run holds the lock; skip"
  exit 0
fi

FAILED_LINE=""
on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "==== $(date '+%F %T') failed rc=$rc line=${FAILED_LINE:-?}"
    restore_tree
    if [ "$FAIL_NOTIFIED" -eq 0 ]; then
      notify "本輪失敗（退出碼 $rc，run_local.sh 第 ${FAILED_LINE:-?} 行），網站維持上一版，詳見 $LOG_DIR"
    fi
  fi
  rm -f "$LOCK"
}
trap 'FAILED_LINE=$LINENO' ERR
trap on_exit EXIT

# 上一輪若被強制中止（斷電、kill），可能留下未發布的變更或中斷的 rebase
if ! git diff --quiet -- state web/public || [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
  echo "found leftovers from an interrupted run; restoring"
  restore_tree
fi

git switch -q "$BRANCH"
git pull -q --rebase origin "$BRANCH"

set +e
.venv/bin/radar run --llm-budget "$LLM_BUDGET"
status=$?
set -e
if [ "$status" -ne 0 ]; then
  fail_notify "本輪未發布（radar run 退出碼 $status），詳見 $LOG_DIR"
  exit "$status"
fi

# 局部讀取失敗（某分類 403、明細逾時）照常發布，但沿用的舊資料要讓人知道
partial="$(.venv/bin/python - <<'PY'
import json
run = json.load(open("state/run.json", encoding="utf-8"))
bad = [s for s in run["sources"] if s["status"] != "ok" or s.get("carried")]
print("、".join(f"{s['bank_name']}（沿用 {s.get('carried', 0)} 筆）" for s in bad))
PY
)"
if [ -n "$partial" ]; then
  echo "partial sources: $partial"
  notify "部分來源讀取不完整，已沿用上一輪資料：$partial"
fi

.venv/bin/python -m pytest -q -p no:cacheprovider
precision="$(.venv/bin/radar score | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["precision"])')"
echo "golden precision=$precision"
if ! .venv/bin/python -c "import sys; sys.exit(0 if float('$precision') >= float('$WARN_PRECISION') else 1)"; then
  notify "golden 精確率 $precision，請檢查是否有官方頁換了內容（照常發布）"
fi
if ! .venv/bin/python -c "import sys; sys.exit(0 if float('$precision') >= float('$MIN_PRECISION') else 1)"; then
  fail_notify "golden 精確率 $precision 低於 $MIN_PRECISION，本輪不發布"
  exit 5
fi

npm --prefix web run build >/dev/null

git add state web/public
if git diff --cached --quiet; then
  echo "no data change"
else
  git commit -q -m "data: refresh $(date '+%F %H:%M')"
fi
# 無變化也要 push：補送前一輪 commit 了但 push 失敗的資料
if ! git push -q origin "$BRANCH"; then
  echo "push rejected; rebasing once and retrying"
  git pull -q --rebase origin "$BRANCH"
  git push -q origin "$BRANCH"
fi
echo "==== $(date '+%F %T') published"
