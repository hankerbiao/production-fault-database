#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
[[ -f .env ]] || { printf '缺少服务器 .env 配置\n' >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { printf '工作区存在未提交改动，请先处理\n' >&2; exit 1; }
branch="$(git branch --show-current)"
[[ -n "$branch" ]] || { printf '当前不在分支上\n' >&2; exit 1; }
mkdir -p .run
exec 9>.run/deploy.lock
flock -n 9 || { printf '已有部署正在运行\n' >&2; exit 1; }
export https_proxy="${DEPLOY_HTTP_PROXY:-http://10.2.48.250:7897}"
export http_proxy="$https_proxy"
export all_proxy="${DEPLOY_ALL_PROXY:-socks5://10.2.48.250:7897}"
export HTTPS_PROXY="$https_proxy" HTTP_PROXY="$http_proxy" ALL_PROXY="$all_proxy"
printf '更新分支 %s...\n' "$branch"
git pull --ff-only origin "$branch"
printf '编译后端...\n'
stage="$(mktemp .run/fault-gateway.build.XXXXXX)"
trap 'rm -f "$stage"' EXIT
(cd backend && go build -o "../$stage" ./cmd/server)
unset https_proxy http_proxy all_proxy HTTPS_PROXY HTTP_PROXY ALL_PROXY
bash scripts/devctl.sh stop backend
mv "$stage" .run/fault-gateway
BACKEND_SKIP_BUILD=1 bash scripts/devctl.sh start backend
bash scripts/devctl.sh status
printf '更新完成，版本: %s\n' "$(git rev-parse --short HEAD)"
