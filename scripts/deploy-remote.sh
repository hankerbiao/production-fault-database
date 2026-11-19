#!/usr/bin/env bash
set -Eeuo pipefail
root="$1"; archive="$2"; deployment_id="$3"
stage="$root/.run/deploy-stage-$deployment_id"
backup="$root/.run/deploy-backups/$deployment_id"
mkdir -p "$root/.run" "$root/.run/deploy-backups"
exec 9>"$root/.run/deploy.lock"
flock -n 9 || { echo '已有部署正在运行' >&2; exit 1; }
cleanup() { rm -rf "$stage"; rm -f "$archive"; }
trap cleanup EXIT
[[ -f "$root/.env" && -f "$root/scripts/devctl.sh" && -x "$root/.run/fault-gateway" ]] || { echo '远端现有环境不完整' >&2; exit 1; }
[[ ! -e "$backup" ]] || { echo '备份目录已存在' >&2; exit 1; }
mkdir "$stage"
tar -xf "$archive" -C "$stage"
[[ -x "$stage/fault-gateway" && -f "$stage/frontend/dist/index.html" && -f "$stage/scripts/devctl.sh" && -f "$stage/.deploy-meta/deleted-files" ]] || { echo '发布包不完整' >&2; exit 1; }
if pgrep -f '[s]cripts/sync/syncctl.py run' >/dev/null; then echo '同步任务正在运行，取消部署' >&2; exit 1; fi
mkdir -p "$backup/files"
cp -p "$root/.run/fault-gateway" "$backup/fault-gateway"
# Record new files separately so rollback also removes files absent before deployment.
: > "$backup/new-files"
while IFS= read -r -d '' file; do
  relative="${file#"$stage/"}"
  [[ "$relative" == fault-gateway || "$relative" == .deploy-meta/deleted-files ]] && continue
  [[ -e "$root/$relative" || -L "$root/$relative" ]] || printf '%s\0' "$relative" >> "$backup/new-files"
done < <(find "$stage" \( -type f -o -type l \) -print0)
export ENV_FILE="$root/.env" RUNTIME_DIR="$root/.run" DEVCTL_PROJECT_ROOT="$root"
export FRONTEND_PRODUCTION=1 BACKEND_SKIP_BUILD=1
export BACKEND_PORT="${BACKEND_PORT:-$(awk -F= '$1=="PORT" {v=$2} END {gsub(/[ "\047]/,"",v); print v}' "$root/.env")}"
export BACKEND_PORT="${BACKEND_PORT:-18080}" FRONTEND_PORT="${FRONTEND_PORT:-5174}"
export VITE_API_TARGET="http://127.0.0.1:${BACKEND_PORT}"
cd "$root"
rollback() {
  echo "部署失败，正在恢复备份: $backup" >&2
  bash "$root/scripts/devctl.sh" stop 9>&- || true
  while IFS= read -r -d '' file; do rm -f -- "$root/$file"; done < "$backup/new-files"
  rsync -a --ignore-times "$backup/files/" "$root/" || { echo '恢复文件失败，请检查备份' >&2; return 1; }
  cp -p "$backup/fault-gateway" "$root/.run/fault-gateway" || return 1
  bash "$root/scripts/devctl.sh" start 9>&- || { echo '恢复服务失败，请检查日志' >&2; return 1; }
}
delete_removed_files() {
  local relative target saved
  while IFS= read -r -d '' relative; do
    [[ "$relative" != /* && "$relative" != .. && "$relative" != ../* && "$relative" != */.. && "$relative" != *'/../'* ]] || {
      echo "删除清单包含无效路径: $relative" >&2
      return 1
    }
    target="$root/$relative"
    [[ -e "$target" || -L "$target" ]] || continue
    [[ -f "$target" || -L "$target" ]] || {
      echo "拒绝删除非文件路径: $relative" >&2
      return 1
    }
    saved="$backup/files/$relative"
    mkdir -p "$(dirname "$saved")" || return 1
    cp -a -- "$target" "$saved" || return 1
    rm -f -- "$target" || return 1
  done < "$stage/.deploy-meta/deleted-files"
  rm -rf "$stage/.deploy-meta"
}
# Save every overwritten file and keep unrelated remote files in place.
if ! bash "$root/scripts/devctl.sh" stop 9>&-; then echo '停止服务失败' >&2; exit 1; fi
if ! delete_removed_files ||
   ! rsync -a --ignore-times --backup --backup-dir="$backup/files" --exclude='/fault-gateway' "$stage/" "$root/" ||
   ! cp -p "$stage/fault-gateway" "$root/.run/fault-gateway" ||
   ! bash "$root/scripts/devctl.sh" start 9>&- ||
   ! curl --noproxy '*' --fail --silent --show-error --max-time 10 "http://127.0.0.1:${BACKEND_PORT}/api/health" ||
   ! curl --noproxy '*' --fail --silent --show-error --max-time 10 -o /dev/null "http://127.0.0.1:${FRONTEND_PORT}/"; then
  rollback || true
  exit 1
fi
printf '\n部署验证通过，备份目录: %s\n' "$backup"
