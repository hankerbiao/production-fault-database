import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / "server"
    tools = tmp_path / "tools"
    tools.mkdir()
    for directory in ("scripts", "backend", "frontend/node_modules/vite/bin", ".venv/bin"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text("PORT=18080\n")
    (root / "frontend/node_modules/vite/bin/vite.js").touch()
    log = tmp_path / "commands.log"
    stub = tools / "tool"
    stub.write_text(
        r"""#!/usr/bin/env bash
set -eu
tool="$(basename "$0")"
printf '%s proxy=%s %s\n' "$tool" "${http_proxy:-none}" "$*" >> "$TEST_LOG"
case "$tool" in
  git)
    case "$1" in
      branch) printf 'main\n' ;;
      rev-parse) printf 'abc123\n' ;;
      pull) [[ "${TEST_FAILURE:-}" != pull ]] ;;
    esac
    ;;
  pgrep)
    if [[ "${TEST_FAILURE:-}" == sync ]]; then
      printf '123 python scripts/sync/syncctl.py run\n'
      exit 0
    fi
    exit 1
    ;;
  node) [[ "$1" != --version ]] || printf 'v22.23.3\n' ;;
  go)
    if [[ "$1" == build ]]; then
      [[ "${TEST_FAILURE:-}" != build ]] || exit 1
      printf '#!/bin/sh\nexit 0\n' > "$3"
      chmod +x "$3"
    fi
    ;;
  python)
    if [[ "${2:-}" == *json.load* ]]; then
      cat >/dev/null
      printf '9\n'
    fi
    ;;
  curl)
    [[ "${TEST_FAILURE:-}" != health ]] || exit 22
    printf '{"status":"ok","items":[]}\n'
    ;;
esac
"""
    )
    stub.chmod(0o755)
    for name in ("git", "go", "npm", "node", "python3", "curl", "flock", "pgrep"):
        (tools / name).symlink_to(stub)
    (root / ".venv/bin/python").symlink_to(stub)
    (root / "scripts/devctl.sh").write_text(
        r"""#!/usr/bin/env bash
set -eu
printf 'devctl proxy=%s skip_build=%s %s\n' "${http_proxy:-none}" "${BACKEND_SKIP_BUILD:-0}" "$*" >> "$TEST_LOG"
if [[ "$1" == start ]]; then
  if (: <&9) 2>/dev/null; then
    printf 'deployment lock inherited\n' >> "$TEST_LOG"
    exit 1
  fi
  [[ -x "$RUNTIME_DIR/fault-gateway" ]]
fi
"""
    )
    environment = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "TEST_LOG": str(log),
        "http_proxy": "http://unrelated-proxy:8080",
    }

    def run(failure=""):
        result = subprocess.run(
            [
                "bash", str(SCRIPT), "--remote", str(root), "main", "abc123",
                "origin", "http://10.2.51.151:7897",
                "socks5://10.2.51.151:7897", "22.23.3",
            ],
            env={**environment, "TEST_FAILURE": failure},
            text=True,
            capture_output=True,
            timeout=10,
        )
        return result, log.read_text() if log.exists() else "", root

    return run


def test_pull_uses_proxy_and_build_precedes_restart(deployment):
    result, commands, root = deployment()
    assert result.returncode == 0, result.stderr
    assert "git proxy=http://10.2.51.151:7897 pull --ff-only origin main" in commands
    assert commands.index("go proxy=http://10.2.51.151:7897 build") < commands.index("devctl")
    assert "devctl proxy=none skip_build=1 start" in commands
    assert "deployment lock inherited" not in commands
    assert "revision=abc123 tasks=9" in result.stdout
    assert (root / ".run/fault-gateway").is_file()
    assert not list((root / ".run").glob("deploy-build.*"))


@pytest.mark.parametrize("failure", ["pull", "build", "sync"])
def test_failure_before_restart_keeps_services_running(deployment, failure):
    result, commands, root = deployment(failure)
    assert result.returncode != 0
    assert "devctl" not in commands
    assert not list((root / ".run").glob("deploy-build.*"))
    if failure == "sync":
        assert " pull " not in commands


def test_health_failure_is_not_reported_as_success(deployment):
    result, commands, _root = deployment("health")
    assert result.returncode != 0
    assert "devctl proxy=none skip_build=1 start" in commands
    assert "Deployment complete:" not in result.stdout
