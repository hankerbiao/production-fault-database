#!/usr/bin/env bash

# Initialize the Python, Go, and frontend dependencies for this repository.
# The script is safe to run repeatedly: it does not overwrite an existing .env.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-${ROOT_DIR}/.venv}"
SKIP_HDBCLI="${SKIP_HDBCLI:-0}"
CHECK_ONLY=0
LOCK_DIR="${ROOT_DIR}/.prepare-env.lock"

usage() {
  cat <<'EOF'
用法: scripts/prepare_env.sh [选项]

初始化项目的 Python、Go 和前端依赖，可重复执行。

选项:
  --python PATH       指定 Python 解释器，默认使用 PYTHON_BIN 或 python3
  --venv PATH         指定虚拟环境目录，默认 .venv
  --skip-hdbcli       不安装 SAP HANA 的 hdbcli 驱动
  --check-only        只检查工具链和依赖，不创建或安装任何内容
  -h, --help          显示帮助

环境变量:
  PYTHON_BIN          同 --python
  VENV_DIR            同 --venv
  SKIP_HDBCLI=1       同 --skip-hdbcli
EOF
}

die() {
  printf '错误: %s\n' "$*" >&2
  exit 1
}

note() { printf '\n[%s] %s\n' "$1" "$2"; }

command_or_die() {
  command -v "$1" >/dev/null 2>&1 || die "找不到命令 '$1'。请先安装后重新执行。"
}

cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

while (($# > 0)); do
  case "$1" in
    --python)
      (($# >= 2)) || die "--python 需要一个路径"
      PYTHON_BIN="$2"
      shift 2
      ;;
    --venv)
      (($# >= 2)) || die "--venv 需要一个路径"
      VENV_DIR="$2"
      shift 2
      ;;
    --skip-hdbcli)
      SKIP_HDBCLI=1
      shift
      ;;
    --check-only)
      CHECK_ONLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "未知选项: $1（使用 --help 查看用法）"
      ;;
  esac
done

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  die "已有另一个环境准备任务正在执行: $LOCK_DIR"
fi
trap cleanup EXIT

[[ "$VENV_DIR" = /* ]] || VENV_DIR="${ROOT_DIR}/${VENV_DIR}"
mkdir -p "$(dirname "$VENV_DIR")"
VENV_DIR="$(cd "$(dirname "$VENV_DIR")" && pwd)/$(basename "$VENV_DIR")"

note "检查工具链" "项目目录: ${ROOT_DIR}"
command_or_die "$PYTHON_BIN"
command_or_die go
command_or_die npm

PYTHON_VERSION="$($PYTHON_BIN -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
printf 'Python: %s (%s)\n' "$PYTHON_BIN" "$PYTHON_VERSION"
printf 'Go: %s\n' "$(go version)"
printf 'Node: %s\n' "$(node --version 2>/dev/null || true)"
printf 'npm: %s\n' "$(npm --version)"

if ((CHECK_ONLY)); then
  [[ -x "${VENV_DIR}/bin/python" ]] || die "虚拟环境不存在: ${VENV_DIR}"
  "${VENV_DIR}/bin/python" -c 'import httpx, pymongo; print("Python 依赖: httpx、pymongo 已安装")' \
    || die "Python 依赖缺失，请去掉 --check-only 执行安装"
  ((SKIP_HDBCLI)) || "${VENV_DIR}/bin/python" -c 'import hdbcli; print("Python 依赖: hdbcli 已安装")' \
    || die "hdbcli 未安装；若当前环境不需要 HANA 同步，请使用 --skip-hdbcli"
  [[ -d "${ROOT_DIR}/frontend/node_modules" ]] || die "前端依赖未安装，请去掉 --check-only 执行安装"
  note "检查完成" "环境看起来已准备好"
  exit 0
fi

note "准备 Python 环境" "虚拟环境: ${VENV_DIR}"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
else
  printf '已存在虚拟环境，跳过创建。\n'
fi

PYTHON="${VENV_DIR}/bin/python"
PIP=("${VENV_DIR}/bin/python" -m pip)
"${PIP[@]}" install --disable-pip-version-check --no-input -r "${ROOT_DIR}/requirements-dev.txt"

if ((SKIP_HDBCLI)); then
  printf '已跳过 hdbcli 安装（后续不能在此虚拟环境执行 HANA 同步）。\n'
elif ! "$PYTHON" -c 'import hdbcli' >/dev/null 2>&1; then
  printf '未检测到 hdbcli，尝试安装 SAP HANA 驱动。\n'
  if ! "${PIP[@]}" install --disable-pip-version-check --no-input hdbcli; then
    die "hdbcli 安装失败。请确认 Python 版本和平台受 SAP 驱动支持，或使用 --skip-hdbcli 完成非 HANA 环境初始化。"
  fi
else
  printf 'hdbcli 已安装，跳过。\n'
fi

note "准备 Go 后端" "下载 Go 模块"
(cd "${ROOT_DIR}/backend" && go mod download)

note "准备前端" "安装 package-lock.json 中锁定的依赖"
(cd "${ROOT_DIR}/frontend" && npm ci --no-audit --no-fund)

note "准备环境配置" "只在根目录 .env 不存在时生成"
ENV_FILE="${ROOT_DIR}/.env"
if [[ -e "$ENV_FILE" ]]; then
  printf '已存在 .env，保持原文件不变。\n'
else
  cp "${ROOT_DIR}/backend/.env.example" "$ENV_FILE"
  # Point sync commands at this checkout so the generated config works locally.
  sed -i.bak \
    -e "s#^SYNC_PYTHON=.*#SYNC_PYTHON=${VENV_DIR}/bin/python#" \
    -e "s#^SYNC_SCRIPT_PATH=.*#SYNC_SCRIPT_PATH=${ROOT_DIR}/sync_sales_orders.py#" \
    -e "s#^SYNC_VIEW_SCRIPT_DIR=.*#SYNC_VIEW_SCRIPT_DIR=${ROOT_DIR}#" \
    -e 's#^PORT=.*#PORT=18080#' \
    "$ENV_FILE"
  rm -f "${ENV_FILE}.bak"
  printf '已生成 .env（来自 backend/.env.example）；请按实际 MongoDB/SAP 环境补充凭据。\n'
fi

note "初始化完成" "Python: ${VENV_DIR}/bin/python"
printf '启动后端: cd backend && go run ./cmd/server\n'
printf '启动前端: cd frontend && npm run dev\n'
printf '检查环境: scripts/prepare_env.sh --check-only\n'
