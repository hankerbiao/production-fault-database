import os
import subprocess
from pathlib import Path

import pytest

REMOTE = Path(__file__).resolve().parents[1] / "scripts" / "deploy-remote.sh"


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / "server"
    stage = tmp_path / "stage"
    tools = tmp_path / "tools"
    for path in (root / ".run", root / "scripts", root / "frontend/dist", stage / "scripts", stage / "frontend/dist", stage / ".deploy-meta", tools):
        path.mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text("PORT=18080\n")
    (root / "scripts/example.py").write_text("old\n")
    (root / "scripts/obsolete.py").write_text("obsolete\n")
    (root / "frontend/dist/index.html").write_text("old frontend")
    (root / ".run/fault-gateway").write_text("old binary")
    (root / ".run/fault-gateway").chmod(0o755)
    (stage / "scripts/example.py").write_text("new\n")
    (stage / "scripts/new.py").write_text("new file\n")
    (stage / "frontend/dist/index.html").write_text("new frontend")
    (stage / "fault-gateway").write_text("new binary")
    (stage / "fault-gateway").chmod(0o755)
    (stage / ".deploy-meta/deleted-files").write_bytes(b"scripts/obsolete.py\0")
    devctl = '''#!/usr/bin/env bash
printf '%s\\n' "$1" >> "$TEST_LOG"
if [[ "$1" == start && ${FAIL_START:-0} == 1 && $(cat "$RUNTIME_DIR/fault-gateway") == 'new binary' ]]; then exit 1; fi
'''
    for path in (root / "scripts/devctl.sh", stage / "scripts/devctl.sh"):
        path.write_text(devctl)
    flock = tools / "flock"
    flock.write_text("#!/usr/bin/env bash\nexit 0\n")
    flock.chmod(0o755)
    curl = tools / "curl"
    curl.write_text('#!/usr/bin/env bash\nexit "${FAIL_HEALTH:-0}"\n')
    curl.chmod(0o755)
    archive = tmp_path / "package.tar"
    subprocess.run(["tar", "-cf", str(archive), "-C", str(stage), "."], check=True)
    env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "TEST_LOG": str(tmp_path / "log")}

    def run(**extra):
        return subprocess.run(["bash", str(REMOTE), str(root), str(archive), "test123"], env={**env, **extra}, capture_output=True, text=True)
    return run, root


def test_deploys_into_existing_directory(deployment):
    run, root = deployment
    result = run()
    assert result.returncode == 0, result.stderr
    assert (root / "scripts/example.py").read_text() == "new\n"
    assert (root / "frontend/dist/index.html").read_text() == "new frontend"
    assert (root / ".run/fault-gateway").read_text() == "new binary"
    assert (root / ".env").read_text() == "PORT=18080\n"
    assert not (root / "scripts/obsolete.py").exists()
    assert not (root / ".run/current").exists()
    assert (root / ".run/deploy-backups/test123/files/scripts/example.py").read_text() == "old\n"
    assert (root / ".run/deploy-backups/test123/files/scripts/obsolete.py").read_text() == "obsolete\n"


@pytest.mark.parametrize("failure", ["FAIL_START", "FAIL_HEALTH"])
def test_failure_restores_previous_files(deployment, failure):
    run, root = deployment
    result = run(**{failure: "1"})
    assert result.returncode != 0
    assert (root / "scripts/example.py").read_text() == "old\n"
    assert (root / "frontend/dist/index.html").read_text() == "old frontend"
    assert (root / ".run/fault-gateway").read_text() == "old binary"
    assert (root / "scripts/obsolete.py").read_text() == "obsolete\n"
    assert not (root / "scripts/new.py").exists()
