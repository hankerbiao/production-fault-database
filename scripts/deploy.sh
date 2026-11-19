#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_STAGE=""
REMOTE_STAGE=""

fail() { printf '部署失败: %s\n' "$*" >&2; exit 1; }
cleanup() { [[ -z "$LOCAL_STAGE" || ! -d "$LOCAL_STAGE" ]] || rm -rf "$LOCAL_STAGE"; }
trap cleanup EXIT

usage() {
  cat <<'USAGE'
用法: bash scripts/deploy.sh

本机完成前后端构建，通过 SSH/rsync 部署到 235，远端自动切换、重启和验证。

可覆盖环境变量:
  DEPLOY_SSH_TARGET       默认 nc235（使用本机 SSH 别名）
  DEPLOY_REMOTE_DIR       默认 /data/production-fault-database
  DEPLOY_SSH_PASSWORD     设置后使用 sshpass；为空则使用 SSH key/交互式认证
  DEPLOY_NODE_BIN         本机 Node 命令，默认 node
  BACKEND_PORT            远端后端端口，默认读取远端 .env 的 PORT 或 18080
  FRONTEND_PORT           远端前端端口，默认 5174
USAGE
}

[[ "${1:-}" != "-h" && "${1:-}" != "--help" ]] || { usage; exit 0; }
[[ -z "${1:-}" ]] || fail "未知参数: $1"

cd "$ROOT_DIR"
for tool in git go npm tar ssh rsync curl; do command -v "$tool" >/dev/null 2>&1 || fail "本机缺少命令: $tool"; done

node_bin="${DEPLOY_NODE_BIN:-node}"
"$node_bin" --version >/dev/null || fail "本机 Node 不可用"

mkdir -p "$ROOT_DIR/.run"
LOCAL_STAGE="$(mktemp -d "$ROOT_DIR/.run/deploy-local.XXXXXX")"
local_lock="$ROOT_DIR/.run/deploy-local.lock"
mkdir "$local_lock" 2>/dev/null || fail "已有部署正在运行"
trap 'rmdir "$local_lock" 2>/dev/null || true; cleanup' EXIT

printf '交叉编译 Linux amd64 后端...\n'
(cd backend && GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -o "$LOCAL_STAGE/fault-gateway" ./cmd/server)
printf '本机编译前端...\n'
(cd frontend && npm run build)
[[ -x "$LOCAL_STAGE/fault-gateway" && -f frontend/dist/index.html ]] || fail "构建产物不完整"

release_id="$(date +%Y%m%d%H%M%S)-$$"
package="$LOCAL_STAGE/release.tar"
printf '打包发布版本 %s...\n' "$release_id"
# Only project files (tracked and unignored untracked) are published; runtime data and secrets stay local.
export COPYFILE_DISABLE=1
git ls-files --cached --others --exclude-standard -z |
  while IFS= read -r -d '' path; do
    if [[ -e "$path" || -L "$path" ]]; then printf '%s\0' "$path"; fi
  done |
  tar --no-xattrs --no-acls -cf "$package" --null -T -
tar --no-xattrs --no-acls --append --file "$package" -C "$LOCAL_STAGE" fault-gateway
# Put the compiled frontend under the same path expected by the production devctl mode.
tar --no-xattrs --no-acls --append --file "$package" frontend/dist
mkdir -p "$LOCAL_STAGE/.deploy-meta"
git ls-files --deleted -z > "$LOCAL_STAGE/.deploy-meta/deleted-files"
tar --no-xattrs --no-acls --append --file "$package" -C "$LOCAL_STAGE" .deploy-meta/deleted-files

# Validate the local archive before sending it.
tar -tf "$package" | grep -E '(^|/)fault-gateway$' >/dev/null || fail '发布包缺少后端二进制'
tar -tf "$package" | grep -E '^(\./)?frontend/dist/index\.html$' >/dev/null || fail '发布包缺少前端构建产物'

target="${DEPLOY_SSH_TARGET:-nc235}"
directory="${DEPLOY_REMOTE_DIR:-/data/production-fault-database}"
password="${DEPLOY_SSH_PASSWORD-}"
ssh_options=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)

remote_copy() {
  if [[ -n "$password" ]]; then
    command -v sshpass >/dev/null 2>&1 || fail '密码认证需要本机安装 sshpass'
    SSHPASS="$password" sshpass -e rsync -az --partial -e "ssh ${ssh_options[*]}" "$package" "$target:/tmp/production-fault-db-${release_id}.tar"
  else
    rsync -az --partial -e "ssh ${ssh_options[*]}" "$package" "$target:/tmp/production-fault-db-${release_id}.tar"
  fi
}

printf '上传到 %s:%s...\n' "$target" "$directory"
remote_copy
printf -v remote_command 'BACKEND_PORT=%q FRONTEND_PORT=%q bash -s -- %q %q %q' \
  "${BACKEND_PORT:-}" "${FRONTEND_PORT:-}" "$directory" "/tmp/production-fault-db-${release_id}.tar" "$release_id"
if [[ -n "$password" ]]; then
  SSHPASS="$password" sshpass -e ssh "${ssh_options[@]}" "$target" "$remote_command" < "$ROOT_DIR/scripts/deploy-remote.sh"
else
  ssh "${ssh_options[@]}" "$target" "$remote_command" < "$ROOT_DIR/scripts/deploy-remote.sh"
fi
printf '部署完成: %s\n' "$release_id"
