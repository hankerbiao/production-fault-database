#!/usr/bin/env bash

set -Eeuo pipefail

if [[ "${1:-}" == "--remote" ]]; then
  ROOT_DIR="${2:?remote deployment directory is required}"
else
  ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
DEPLOY_STAGE=""

die() {
  printf 'Deployment failed: %s\n' "$*" >&2
  exit 1
}

with_proxy() (
  export https_proxy="$DEPLOY_HTTP_PROXY" http_proxy="$DEPLOY_HTTP_PROXY"
  export all_proxy="$DEPLOY_ALL_PROXY"
  export HTTPS_PROXY="$https_proxy" HTTP_PROXY="$http_proxy" ALL_PROXY="$all_proxy"
  "$@"
)

cleanup_stage() {
  if [[ -n "$DEPLOY_STAGE" && -d "$DEPLOY_STAGE" ]]; then
    find "$DEPLOY_STAGE" -depth -delete
  fi
}

env_value() {
  awk -F= -v key="$1" '
    /^[[:space:]]*#/ { next }
    {
      name = $1
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
      if (name == key) {
        value = substr($0, index($0, "=") + 1)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
        gsub(/^["\047]|["\047]$/, "", value)
        print value
      }
    }
  ' "$ROOT_DIR/.env" | tail -n 1
}

require_idle_sync() {
  local processes
  if processes="$(pgrep -af 'python[^ ]*[[:space:]].*scripts/(sync/|sources/scs_(doa|change)_sync\.py|maintenance/)' 2>/dev/null)"; then
    printf '%s\n' "$processes" >&2
    die "A sync or maintenance process is running; deploy after it finishes."
  fi
}

node_supported() {
  command -v node >/dev/null 2>&1 || return 1
  node -e '
    const [major, minor] = process.versions.node.split(".").map(Number);
    process.exit((major === 20 && minor >= 19) ||
      (major === 22 && minor >= 12) || major >= 24 ? 0 : 1);
  '
}

prepare_node() {
  local runtime="$ROOT_DIR/.run" architecture archive version="$1"
  if [[ -x "$runtime/node/bin/node" ]]; then
    export PATH="$runtime/node/bin:$PATH"
  fi
  node_supported && return 0
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Invalid Node version."
  case "$(uname -m)" in
    x86_64) architecture=x64 ;;
    aarch64|arm64) architecture=arm64 ;;
    *) die "Unsupported server architecture." ;;
  esac
  archive="node-v${version}-linux-${architecture}.tar.xz"
  printf 'Installing project Node %s...\n' "$version"
  with_proxy curl --fail --silent --show-error --connect-timeout 15 --max-time 300 \
    --retry 2 -o "$DEPLOY_STAGE/$archive" "https://nodejs.org/dist/v${version}/$archive"
  with_proxy curl --fail --silent --show-error --connect-timeout 15 --max-time 60 \
    --retry 2 -o "$DEPLOY_STAGE/SHASUMS256.txt" \
    "https://nodejs.org/dist/v${version}/SHASUMS256.txt"
  (
    cd "$DEPLOY_STAGE"
    awk -v name="$archive" '$2 == name { print }' SHASUMS256.txt > node.checksum
    [[ -s node.checksum ]] || die "Node checksum is missing."
    sha256sum --check node.checksum
    tar -xJf "$archive"
  )
  mv "$DEPLOY_STAGE/node-v${version}-linux-${architecture}" \
    "$runtime/node-v${version}-linux-${architecture}"
  ln -sfn "node-v${version}-linux-${architecture}" "$runtime/node"
  export PATH="$runtime/node/bin:$PATH"
  node_supported || die "Installed Node is incompatible."
}

remote_deploy() {
  [[ $# == 7 ]] || die "Invalid remote deployment arguments."
  ROOT_DIR="$1"
  local branch="$2" revision="$3" remote="$4" node_version="$7"
  DEPLOY_HTTP_PROXY="$5"
  DEPLOY_ALL_PROXY="$6"
  cd "$ROOT_DIR"
  [[ -f .env ]] || die "Server .env is missing."
  [[ "$(git branch --show-current)" == "$branch" ]] || die "Server branch differs from $branch."
  [[ -z "$(git status --porcelain)" ]] || die "Server has uncommitted files."
  for tool in git go python3 curl flock pgrep sha256sum tar; do
    command -v "$tool" >/dev/null 2>&1 || die "Missing server tool: $tool"
  done
  mkdir -p "$ROOT_DIR/.run"
  exec 9>"$ROOT_DIR/.run/deploy.lock"
  flock -n 9 || die "Another deployment is running."
  require_idle_sync
  DEPLOY_STAGE="$(mktemp -d "$ROOT_DIR/.run/deploy-build.XXXXXX")"
  trap cleanup_stage EXIT
  local previous_revision
  previous_revision="$(git rev-parse HEAD)"
  printf 'Pulling %s via proxy...\n' "$branch"
  with_proxy git pull --ff-only "$remote" "$branch"
  [[ "$(git rev-parse HEAD)" == "$revision" ]] || die "Server revision differs from the pushed revision."

  prepare_node "$node_version"
  node --version
  go version
  if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
  fi
  if ! git diff --quiet "$previous_revision" "$revision" -- requirements.txt ||
    ! .venv/bin/python -c 'import httpx, pymongo, hdbcli' >/dev/null 2>&1; then
    with_proxy .venv/bin/python -m pip install --disable-pip-version-check \
      --no-input -r requirements.txt
  fi
  .venv/bin/python -c 'import httpx, pymongo, hdbcli; print("Python runtime dependencies: OK")'
  if [[ ! -f frontend/node_modules/vite/bin/vite.js ]] ||
    ! git diff --quiet "$previous_revision" "$revision" -- frontend/package.json frontend/package-lock.json ||
    ! (cd frontend && npm ls --depth=0 >/dev/null 2>&1); then
    (cd frontend && with_proxy npm ci --no-audit --no-fund)
  fi
  (cd frontend && npm run build)
  (cd backend && with_proxy go build -o "$DEPLOY_STAGE/fault-gateway" ./cmd/server)
  require_idle_sync

  # Runtime requests to MongoDB/SAP/SCS must use the server's normal network.
  unset https_proxy http_proxy all_proxy HTTPS_PROXY HTTP_PROXY ALL_PROXY
  export ENV_FILE="$ROOT_DIR/.env" RUNTIME_DIR="$ROOT_DIR/.run"
  export SYNC_PYTHON="$ROOT_DIR/.venv/bin/python" SYNC_SCRIPT_DIR="$ROOT_DIR"
  export BACKEND_PORT="${BACKEND_PORT:-$(env_value PORT)}"
  BACKEND_PORT="${BACKEND_PORT:-18080}"
  export FRONTEND_PORT="${FRONTEND_PORT:-5174}"
  export VITE_API_TARGET="http://127.0.0.1:${BACKEND_PORT}"
  export FRONTEND_URL="${FRONTEND_URL:-http://127.0.0.1:${FRONTEND_PORT}/}"

  printf 'Restarting backend and frontend...\n'
  bash scripts/devctl.sh stop
  mv "$DEPLOY_STAGE/fault-gateway" "$ROOT_DIR/.run/fault-gateway"
  BACKEND_SKIP_BUILD=1 bash scripts/devctl.sh start 9>&-
  bash scripts/devctl.sh status
  curl --noproxy '*' --fail --silent --show-error --max-time 10 \
    "http://127.0.0.1:${BACKEND_PORT}/api/health"
  printf '\n'
  curl --noproxy '*' --fail --silent --show-error --max-time 10 \
    -o /dev/null "http://127.0.0.1:${FRONTEND_PORT}/"
  curl --noproxy '*' --fail --silent --show-error --max-time 10 \
    "http://127.0.0.1:${FRONTEND_PORT}/api/health"
  printf '\n'
  local task_count
  task_count="$(curl --noproxy '*' --fail --silent --show-error --max-time 10 \
    "http://127.0.0.1:${BACKEND_PORT}/api/sync/tasks" |
    .venv/bin/python -c 'import json,sys; print(len(json.load(sys.stdin)["items"]))')"
  printf 'Deployment complete: revision=%s tasks=%s backend_port=%s frontend_port=%s\n' \
    "$revision" "$task_count" "$BACKEND_PORT" "$FRONTEND_PORT"
}

case "${1:-}" in
  --remote)
    shift
    remote_deploy "$@"
    exit 0
    ;;
  -h|--help)
    printf '%s\n' \
      'Usage: bash scripts/deploy.sh' \
      'Commits all local changes, pushes the current branch, pulls and restarts the server.' \
      'DEPLOY_SSH_TARGET: root@10.17.150.235' \
      'DEPLOY_REMOTE_DIR: /data/production-fault-database' \
      'DEPLOY_GIT_REMOTE: origin' \
      'DEPLOY_HTTP_PROXY: http://10.2.51.151:7897' \
      'DEPLOY_ALL_PROXY: socks5://10.2.51.151:7897' \
      'DEPLOY_NODE_VERSION: 22.23.3 (used when server Node is incompatible)' \
      'DEPLOY_COMMIT_MESSAGE: optional commit message' \
      'DEPLOY_SSH_PASSWORD: overrides the built-in internal deployment password' \
      'Set DEPLOY_SSH_PASSWORD to an empty value to use your SSH key or an interactive password prompt.'
    exit 0
    ;;
  "") ;;
  *) die "Unknown argument: $1" ;;
esac

cd "$ROOT_DIR"
target="${DEPLOY_SSH_TARGET:-root@10.17.150.235}"
deploy_password="${DEPLOY_SSH_PASSWORD-test123}"
directory="${DEPLOY_REMOTE_DIR:-/data/production-fault-database}"
remote="${DEPLOY_GIT_REMOTE:-origin}"
branch="$(git branch --show-current)"
[[ -n "$branch" ]] || die "Cannot deploy a detached HEAD."
mkdir -p .run
local_lock="$ROOT_DIR/.run/deploy-local.lock"
mkdir "$local_lock" 2>/dev/null || die "Another local deployment is running."
trap 'rmdir "$local_lock"' EXIT
git add --all
if ! git diff --cached --quiet; then
  git commit -m "${DEPLOY_COMMIT_MESSAGE:-Deploy project updates $(date +%Y-%m-%dT%H:%M:%S)}"
fi
revision="$(git rev-parse HEAD)"
git push "$remote" "HEAD:refs/heads/$branch"
printf 'Pushed %s; deploying to %s:%s...\n' "$revision" "$target" "$directory"
printf -v remote_command 'bash -s -- --remote %q %q %q %q %q %q %q' \
  "$directory" "$branch" "$revision" "$remote" \
  "${DEPLOY_HTTP_PROXY:-http://10.2.51.151:7897}" \
  "${DEPLOY_ALL_PROXY:-socks5://10.2.51.151:7897}" \
  "${DEPLOY_NODE_VERSION:-22.23.3}"
ssh_options=(
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=10
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=3
)
if [[ -n "$deploy_password" ]]; then
  command -v sshpass >/dev/null 2>&1 || die "Password authentication requires sshpass."
  SSHPASS="$deploy_password" sshpass -e ssh "${ssh_options[@]}" \
    "$target" "$remote_command" < "$ROOT_DIR/scripts/deploy.sh"
else
  ssh "${ssh_options[@]}" "$target" "$remote_command" < "$ROOT_DIR/scripts/deploy.sh"
fi
