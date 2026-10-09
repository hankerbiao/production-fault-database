#!/usr/bin/env bash

# Manage the local Go API and Vite development server as one application.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${RUNTIME_DIR:-${ROOT_DIR}/.run}"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"
BACKEND_PID_FILE="${RUNTIME_DIR}/backend.pid"
FRONTEND_PID_FILE="${RUNTIME_DIR}/frontend.pid"
BACKEND_LOG="${RUNTIME_DIR}/backend.log"
FRONTEND_LOG="${RUNTIME_DIR}/frontend.log"

die() {
  printf '错误: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
用法: scripts/devctl.sh <命令> [服务]

统一管理本地前后端服务。

命令:
  start [all|backend|frontend]    启动服务，默认启动前后端
  stop [all|backend|frontend]     停止服务，默认停止前后端
  restart [all|backend|frontend] 重启服务，默认重启前后端
  status                         查看进程和端口状态
  logs [all|backend|frontend]     查看服务日志，默认同时查看两份日志
  help                           显示帮助

环境变量:
  BACKEND_PORT                   后端端口，默认读取 .env 的 PORT 或 18080
  FRONTEND_PORT                  前端端口，默认 5174
  VITE_API_TARGET                前端代理地址，默认 http://127.0.0.1:${BACKEND_PORT}
  FRONTEND_HOST                 前端监听地址，默认 0.0.0.0（允许外部机器访问）
  ENV_FILE                       后端环境文件，默认项目根目录 .env
  RUNTIME_DIR                    PID、日志和后端构建文件目录，默认项目根目录 .run

示例:
  scripts/devctl.sh start
  scripts/devctl.sh status
  scripts/devctl.sh logs backend
  scripts/devctl.sh restart frontend
EOF
}

env_value() {
  local key="$1"
  local file="$2"
  [[ -f "$file" ]] || return 0
  awk -F= -v key="$key" '
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
  ' "$file" | tail -n 1
}

ENV_PORT="$(env_value PORT "$ENV_FILE")"
BACKEND_PORT="${BACKEND_PORT:-${PORT:-${ENV_PORT:-18080}}}"
FRONTEND_PORT="${FRONTEND_PORT:-5174}"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
FRONTEND_HOST="${FRONTEND_HOST:-0.0.0.0}"
VITE_API_TARGET="${VITE_API_TARGET:-http://${BACKEND_HOST}:${BACKEND_PORT}}"

mkdir -p "$RUNTIME_DIR"

command_or_die() {
  command -v "$1" >/dev/null 2>&1 || die "找不到命令 '$1'，请先安装项目依赖。"
}

pid_from_file() {
  local file="$1"
  [[ -s "$file" ]] || return 1
  local pid
  pid="$(tr -d '[:space:]' < "$file")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$pid"
}

pid_is_alive() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null
}

pid_matches() {
  local pid="$1"
  local pattern="$2"
  pid_is_alive "$pid" || return 1
  ps -p "$pid" -o command= 2>/dev/null | grep -F -- "$pattern" >/dev/null 2>&1
}

service_pid_file() {
  case "$1" in
    backend) printf '%s\n' "$BACKEND_PID_FILE" ;;
    frontend) printf '%s\n' "$FRONTEND_PID_FILE" ;;
    *) die "未知服务: $1" ;;
  esac
}

service_pattern() {
  case "$1" in
    backend) printf '%s\n' "fault-gateway" ;;
    frontend) printf '%s\n' "vite/bin/vite.js" ;;
    *) die "未知服务: $1" ;;
  esac
}

service_pid() {
  local service="$1"
  local file
  file="$(service_pid_file "$service")"
  local pid
  if pid="$(pid_from_file "$file")" && pid_matches "$pid" "$(service_pattern "$service")"; then
    printf '%s\n' "$pid"
    return 0
  fi
  return 1
}

cleanup_stale_pid_file() {
  local service="$1"
  local file
  file="$(service_pid_file "$service")"
  local pid
  if pid="$(pid_from_file "$file")" && ! pid_matches "$pid" "$(service_pattern "$service")"; then
    rm -f "$file"
  fi
}

port_is_open() {
  local port="$1"
  command -v curl >/dev/null 2>&1 || return 1
  curl -sS --connect-timeout 1 --max-time 2 "http://127.0.0.1:${port}/" -o /dev/null >/dev/null 2>&1
}

wait_for_url() {
  local url="$1"
  local attempts="${2:-20}"
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if curl -sS --connect-timeout 1 --max-time 2 "$url" -o /dev/null >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

start_backend() {
  cleanup_stale_pid_file backend
  if pid="$(service_pid backend 2>/dev/null)"; then
    printf '后端已运行 (PID %s, http://%s:%s)\n' "$pid" "$BACKEND_HOST" "$BACKEND_PORT"
    return 0
  fi

  command_or_die go
  command_or_die curl
  local binary="${RUNTIME_DIR}/fault-gateway"
  printf '编译后端...\n'
  (cd "$ROOT_DIR/backend" && go build -o "$binary" ./cmd/server)

  printf '启动后端 (端口 %s)...\n' "$BACKEND_PORT"
  ENV_FILE="$ENV_FILE" PORT="$BACKEND_PORT" \
    nohup "$binary" >>"$BACKEND_LOG" 2>&1 < /dev/null &
  local pid=$!
  printf '%s\n' "$pid" > "$BACKEND_PID_FILE"

  if ! wait_for_url "http://${BACKEND_HOST}:${BACKEND_PORT}/api/health" 20; then
    rm -f "$BACKEND_PID_FILE"
    printf '后端启动失败，请查看 %s\n' "$BACKEND_LOG" >&2
    return 1
  fi
  printf '后端已启动 (PID %s, http://%s:%s)\n' "$pid" "$BACKEND_HOST" "$BACKEND_PORT"
}

start_frontend() {
  cleanup_stale_pid_file frontend
  if pid="$(service_pid frontend 2>/dev/null)"; then
    printf '前端已运行 (PID %s, http://%s:%s)\n' "$pid" "$FRONTEND_HOST" "$FRONTEND_PORT"
    return 0
  fi

  command_or_die node
  command_or_die curl
  local vite_bin="${ROOT_DIR}/frontend/node_modules/vite/bin/vite.js"
  [[ -f "$vite_bin" ]] || die "前端依赖不存在，请先执行 ./scripts/prepare_env.sh"

  printf '启动前端 (端口 %s)...\n' "$FRONTEND_PORT"
  (
    cd "$ROOT_DIR/frontend"
    exec env VITE_API_TARGET="$VITE_API_TARGET" nohup node "$vite_bin" \
      --host "$FRONTEND_HOST" --port "$FRONTEND_PORT"
  ) >>"$FRONTEND_LOG" 2>&1 < /dev/null &
  local pid=$!
  printf '%s\n' "$pid" > "$FRONTEND_PID_FILE"

  if ! wait_for_url "http://127.0.0.1:${FRONTEND_PORT}/" 20; then
    rm -f "$FRONTEND_PID_FILE"
    printf '前端启动失败，请查看 %s\n' "$FRONTEND_LOG" >&2
    return 1
  fi
  printf '前端已启动 (PID %s, http://%s:%s)\n' "$pid" "$FRONTEND_HOST" "$FRONTEND_PORT"
}

stop_pid_tree() {
  local pid="$1"
  local child
  local children
  children="$(pgrep -P "$pid" 2>/dev/null || true)"
  for child in $children; do
    stop_pid_tree "$child"
  done
  kill -TERM "$pid" 2>/dev/null || true
}

stop_service() {
  local service="$1"
  local file
  file="$(service_pid_file "$service")"
  local pid
  if ! pid="$(service_pid "$service" 2>/dev/null)"; then
    rm -f "$file"
    printf '%s 未运行\n' "$service"
    return 0
  fi

  printf '停止%s (PID %s)...\n' "$service" "$pid"
  stop_pid_tree "$pid"
  local attempt
  for ((attempt = 1; attempt <= 10; attempt++)); do
    pid_is_alive "$pid" || break
    sleep 0.5
  done
  if pid_is_alive "$pid"; then
    printf '进程未响应 TERM，强制停止 PID %s\n' "$pid" >&2
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$file"
  printf '%s 已停止\n' "$service"
}

start() {
  local target="${1:-all}"
  case "$target" in
    all)
      start_backend
      start_frontend
      ;;
    backend) start_backend ;;
    frontend) start_frontend ;;
    *) die "未知服务: $target" ;;
  esac
}

stop() {
  local target="${1:-all}"
  case "$target" in
    all)
      stop_service frontend
      stop_service backend
      ;;
    backend|frontend) stop_service "$target" ;;
    *) die "未知服务: $target" ;;
  esac
}

status_service() {
  local service="$1"
  local port
  local pid
  cleanup_stale_pid_file "$service"
  if [[ "$service" == backend ]]; then
    port="$BACKEND_PORT"
  else
    port="$FRONTEND_PORT"
  fi
  if pid="$(service_pid "$service" 2>/dev/null)"; then
    printf '%-8s running  PID=%s  port=%s\n' "$service" "$pid" "$port"
  elif port_is_open "$port"; then
    printf '%-8s unmanaged port=%s (已有其他进程监听)\n' "$service" "$port"
  else
    printf '%-8s stopped  port=%s\n' "$service" "$port"
  fi
}

status() {
  status_service backend
  status_service frontend
}

logs() {
  local target="${1:-all}"
  case "$target" in
    backend)
      touch "$BACKEND_LOG"
      exec tail -f "$BACKEND_LOG"
      ;;
    frontend)
      touch "$FRONTEND_LOG"
      exec tail -f "$FRONTEND_LOG"
      ;;
    all)
      touch "$BACKEND_LOG" "$FRONTEND_LOG"
      exec tail -f "$BACKEND_LOG" "$FRONTEND_LOG"
      ;;
    *) die "未知服务: $target" ;;
  esac
}

command="${1:-help}"
case "$command" in
  start) start "${2:-all}" ;;
  stop) stop "${2:-all}" ;;
  restart)
    stop "${2:-all}"
    start "${2:-all}"
    ;;
  status) status ;;
  logs) logs "${2:-all}" ;;
  help|-h|--help) usage ;;
  *) usage >&2; die "未知命令: $command" ;;
esac
