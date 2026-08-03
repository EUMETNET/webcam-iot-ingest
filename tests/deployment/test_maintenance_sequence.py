import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deployment/systemd/pilot/run-maintenance-sequence"


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_failure_does_not_prevent_later_maintenance_steps(tmp_path: Path):
    commands = tmp_path / "commands.log"
    binary = tmp_path / "bin"
    binary.mkdir()
    _executable(
        binary / "docker",
        """#!/usr/bin/env bash
echo "$*" >> "$COMMAND_LOG"
if [[ "$*" == *"$FAIL_MATCH"* ]]; then exit 1; fi
""",
    )
    _executable(binary / "curl", "#!/usr/bin/env bash\ncat >/dev/null\n")
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{binary}:{environment['PATH']}",
            "COMMAND_LOG": str(commands),
            "FAIL_MATCH": "discovery.windy.windy_discovery_workflow",
            "WEBCAM_MAINTENANCE_LOCK_FILE": str(tmp_path / "maintenance.lock"),
            "WEBCAM_CLEANUP_TIMEOUT_S": "5",
            "WEBCAM_WINDY_DISCOVERY_TIMEOUT_S": "5",
            "WEBCAM_FINTRAFFIC_DISCOVERY_TIMEOUT_S": "5",
            "WEBCAM_SKAPING_DISCOVERY_TIMEOUT_S": "5",
            "WEBCAM_DATABASE_BACKUP_TIMEOUT_S": "5",
        }
    )

    completed = subprocess.run(
        [str(SCRIPT)], cwd=ROOT, env=environment, capture_output=True, text=True
    )

    assert completed.returncode == 1
    log = commands.read_text(encoding="utf-8")
    cleanup = log.index("storage.s3_spool_cleanup")
    windy = log.index("discovery.windy.windy_discovery_workflow")
    fintraffic = log.index("discovery.fintraffic.fintraffic_discovery_workflow")
    skaping = log.index("discovery.skaping.skaping_discovery_workflow")
    backup = log.index("database.database_backup")
    assert cleanup < windy < fintraffic < skaping < backup
    assert '"step":"discovery_windy"' in completed.stdout
    assert '"result":"failure"' in completed.stdout


def test_lock_prevents_overlapping_sequence(tmp_path: Path):
    lock = tmp_path / "maintenance.lock"
    environment = os.environ.copy()
    environment["WEBCAM_MAINTENANCE_LOCK_FILE"] = str(lock)
    holder = subprocess.Popen(
        ["flock", str(lock), "sh", "-c", "echo ready; sleep 5"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "ready"
    try:
        completed = subprocess.run(
            [str(SCRIPT)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    assert completed.returncode == 75
    assert "already_running" in completed.stdout


def test_unwritable_lock_has_distinct_error(tmp_path: Path):
    environment = os.environ.copy()
    environment["WEBCAM_MAINTENANCE_LOCK_FILE"] = str(tmp_path / "missing" / "lock")

    completed = subprocess.run(
        [str(SCRIPT)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 73
    assert "lock_error" in completed.stderr
