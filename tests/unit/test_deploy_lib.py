"""deploy/scripts/lib.sh: which services deploy, rollback, backup and
restore stop and start, and which the host check expects to run."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

LIB = Path(__file__).parents[2] / "deploy" / "scripts" / "lib.sh"
BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(
    BASH is None or sys.platform == "win32", reason="needs a POSIX bash"
)


def _run(tmp_path: Path, profiles: str, function: str) -> list[str]:
    (tmp_path / ".env").write_text(f"COMPOSE_PROFILES={profiles}\n", encoding="utf-8")
    out = subprocess.run(
        [BASH, "-c", f'. "$1"; {function}', "lib", str(LIB)],
        env={"DEPLOY_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


@needs_bash
@pytest.mark.parametrize(
    ("profiles", "expected"),
    [
        ("", ["api", "caddy"]),
        ("scheduler", ["api", "caddy", "scheduler"]),
        ("scheduler,lab-worker", ["api", "caddy", "scheduler", "lab-worker"]),
        ("lab-worker,ai", ["api", "caddy", "lab-worker"]),
    ],
)
def test_expected_services_follow_the_profiles(tmp_path, profiles, expected):
    assert _run(tmp_path, profiles, "expected_services") == expected


@needs_bash
def test_the_lab_worker_counts_as_a_writer_of_the_data_volume(tmp_path):
    """It writes job state to state.sqlite, so a snapshot, a migration or a
    restore must not run while it is up."""
    writers = _run(tmp_path, "scheduler,lab-worker", "expected_services | grep -v caddy")
    assert writers == ["api", "scheduler", "lab-worker"]


@needs_bash
def test_deploy_refuses_the_placeholder_api_token(tmp_path):
    """``change-me`` was the example value, and the legacy token signs in as
    the admin. deploy.sh stops before it touches anything."""
    shutil.copytree(LIB.parent, tmp_path / "scripts")
    (tmp_path / ".env").write_text("STONKS_API_TOKEN=change-me\n", encoding="utf-8")
    out = subprocess.run(
        [BASH, str(tmp_path / "scripts" / "deploy.sh"), "v1.0.0"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 2
    assert "STONKS_API_TOKEN" in out.stderr
    assert not (tmp_path / ".previous-tag").exists()


def test_the_example_env_has_no_working_placeholder_credentials():
    values = {}
    for name in (".env.example", "backup.env.example"):
        example = (LIB.parents[1] / name).read_text(encoding="utf-8")
        values.update(
            line.split("=", 1)
            for line in example.splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        )
    for key in ("STONKS_API_TOKEN", "RESTIC_PASSWORD", "STONKS_SECRET_KEYS"):
        assert values[key] == "", f"{key} must be empty in the example, not a guessable value"


FAKE_DOCKER = """#!/usr/bin/env bash
# Records every call. With FAKE_BROKEN set, "compose up" fails and the api
# never turns healthy (a broken release and a broken rollback). A snapshot
# tar leaves an empty file behind.
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$*" in
*" up "*) [ -z "${FAKE_BROKEN:-}" ] || exit 1 ;;
*" ps --status running --services"*) printf 'api\ncaddy\nscheduler\n' ;;
*" ps -q api"*) echo cid ;;
inspect*) [ -z "${FAKE_BROKEN:-}" ] || exit 1; echo healthy ;;
*"tar czf /out/"*)
	name="${*##*tar czf /out/}"
	touch "$FAKE_SNAPDIR/${name%% *}"
	;;
esac
exit 0
"""


def _server(tmp_path: Path, deployed: str, previous: str | None = None) -> Path:
    """A deploy folder with fake docker, curl, sleep and crontab."""
    deploy_dir = tmp_path / "deploy"
    shutil.copytree(LIB.parent, deploy_dir / "scripts")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in {
        "docker": FAKE_DOCKER,
        "curl": '#!/bin/sh\n[ -z "$FAKE_BROKEN" ] || exit 7\n',
        "sleep": "#!/bin/sh\nexit 0\n",
        "crontab": "#!/bin/sh\ncat >/dev/null\n",
    }.items():
        (fake_bin / name).write_text(body, encoding="utf-8", newline="\n")
        (fake_bin / name).chmod(0o755)
    data = tmp_path / "srv" / "data"
    (tmp_path / "srv" / "snapshots").mkdir(parents=True)
    (deploy_dir / ".env").write_text(
        f"STONKS_IMAGE=stonks\nSTONKS_IMAGE_TAG={deployed}\nSTONKS_DATA_PATH={data}\n"
        "COMPOSE_PROFILES=scheduler\nSTONKS_API_TOKEN=x\n",
        encoding="utf-8",
    )
    (deploy_dir / ".deployed-tag").write_text(f"{deployed}\n", encoding="utf-8")
    if previous:
        (deploy_dir / ".previous-tag").write_text(f"{previous}\n", encoding="utf-8")
    (deploy_dir / "crontab").write_text("", encoding="utf-8")
    return deploy_dir


def _deploy(deploy_dir: Path, tag: str, broken: bool = False) -> subprocess.CompletedProcess:
    root = deploy_dir.parent
    return subprocess.run(
        [BASH, str(deploy_dir / "scripts" / "deploy.sh"), tag],
        env={
            "PATH": f"{root / 'bin'}:/usr/bin:/bin",
            "FAKE_LOG": str(root / "docker.log"),
            "FAKE_SNAPDIR": str(root / "srv" / "snapshots"),
            "FAKE_BROKEN": "1" if broken else "",
            "HOME": str(root),
        },
        capture_output=True,
        text=True,
        check=False,
    )


@needs_bash
def test_a_failed_rollback_still_reports_its_outcome(tmp_path):
    """The new tag fails to start and so does the previous one. The rollback
    must still reach its health check and say the stack is not healthy,
    instead of dying on the failed ``compose up`` without a word."""
    deploy_dir = _server(tmp_path, deployed="v1.0.0")
    out = _deploy(deploy_dir, "v1.1.0", broken=True)
    assert out.returncode == 1
    assert "rollback to v1.0.0 is NOT healthy" in out.stderr
    assert "STONKS_IMAGE_TAG=v1.0.0" in (deploy_dir / ".env").read_text(encoding="utf-8")
    calls = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert calls.count(" up -d") == 2  # the new tag, then the rollback


@needs_bash
def test_a_good_deploy_records_the_tags(tmp_path):
    deploy_dir = _server(tmp_path, deployed="v1.0.0")
    out = _deploy(deploy_dir, "v1.1.0")
    assert out.returncode == 0, out.stderr
    assert (deploy_dir / ".deployed-tag").read_text(encoding="utf-8").strip() == "v1.1.0"
    assert (deploy_dir / ".previous-tag").read_text(encoding="utf-8").strip() == "v1.0.0"


@needs_bash
def test_redeploying_the_same_tag_keeps_the_rollback_target(tmp_path):
    """Running the Deploy workflow again for the live tag must not make
    rollback.sh "roll back" to that same tag."""
    deploy_dir = _server(tmp_path, deployed="v1.1.0", previous="v1.0.0")
    out = _deploy(deploy_dir, "v1.1.0")
    assert out.returncode == 0, out.stderr
    assert (deploy_dir / ".deployed-tag").read_text(encoding="utf-8").strip() == "v1.1.0"
    assert (deploy_dir / ".previous-tag").read_text(encoding="utf-8").strip() == "v1.0.0"


def test_the_example_env_lists_the_telegram_and_assistant_secrets_empty():
    """Both are env-only secrets the app reads (review wave 2): the deploy
    template names them, empty, so an operator finds where they go."""
    example = (LIB.parents[1] / ".env.example").read_text(encoding="utf-8")
    values = dict(
        line.split("=", 1)
        for line in example.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    for key in ("STONKS_TELEGRAM_BOT_TOKEN", "STONKS_ASSISTANT_API_KEY"):
        assert values.get(key) == "", f"{key} must be listed empty in deploy/.env.example"


# ---- Backup secrets live in deploy/backup.env, away from the app ----------

COMPOSE = LIB.parents[1] / "compose.yaml"
BACKUP_EXAMPLE = LIB.parents[1] / "backup.env.example"
#: What a compromised app container must never read: the restic password
#: and bucket keys could read or delete the off-server backups, and the
#: host-side ping URLs could fake a healthy backup or host.
BACKUP_KEYS = {
    "RESTIC_REPOSITORY",
    "RESTIC_PASSWORD",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_DEFAULT_REGION",
    "B2_ACCOUNT_ID",
    "B2_ACCOUNT_KEY",
    "HC_PING_BACKUP",
    "HC_PING_RESTORE_TEST",
    "HC_PING_HOST",
}


def _assignments(path: Path, *, commented: bool = False) -> dict[str, str]:
    """KEY=value lines, optionally also the commented-out ``# KEY=value`` ones."""
    found = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if commented and line.startswith("#"):
            line = line.lstrip("#").strip()
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            if key.isupper() and " " not in key:
                found[key] = value
    return found


def _env_files(service: dict) -> list[str]:
    return [item["path"] if isinstance(item, dict) else item for item in service["env_file"]]


def test_the_app_containers_never_get_the_backup_env_file():
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    for name in ("api", "scheduler", "lab-worker"):
        assert _env_files(compose["services"][name]) == [".env"], name
    assert _env_files(compose["services"]["restic"]) == ["backup.env"]


def test_the_backup_secrets_moved_out_of_the_app_example():
    app_keys = set(_assignments(LIB.parents[1] / ".env.example", commented=True))
    assert not app_keys & BACKUP_KEYS, "backup secrets belong in backup.env.example"


def test_the_backup_example_lists_every_backup_key_with_no_working_secret():
    values = _assignments(BACKUP_EXAMPLE, commented=True)
    assert set(values) == BACKUP_KEYS
    for key in ("RESTIC_PASSWORD", "AWS_SECRET_ACCESS_KEY", "B2_ACCOUNT_KEY"):
        assert values[key] == "", f"{key} must be empty in the example"


def test_lib_names_the_same_backup_keys_as_the_example():
    """lib.sh's list drives the old-layout warning and the migration."""
    text = LIB.read_text(encoding="utf-8")
    line = next(row for row in text.splitlines() if row.startswith("BACKUP_ENV_KEYS="))
    assert set(line.split("=", 1)[1].strip('"').split()) == BACKUP_KEYS


def test_git_ignores_the_real_backup_env():
    ignored = (LIB.parents[1] / ".gitignore").read_text(encoding="utf-8").split()
    assert "backup.env" in ignored


def _lib(deploy_dir: Path, function: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, "-c", f'. "$1"; {function}', "lib", str(LIB)],
        env={"DEPLOY_DIR": str(deploy_dir), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )


@needs_bash
def test_backup_env_get_prefers_backup_env_and_falls_back_to_the_old_layout(tmp_path):
    (tmp_path / ".env").write_text("RESTIC_PASSWORD=old\nHC_PING_BACKUP=hc-old\n")
    (tmp_path / "backup.env").write_text("RESTIC_PASSWORD=new\n")
    assert _lib(tmp_path, "backup_env_get RESTIC_PASSWORD").stdout == "new"
    assert _lib(tmp_path, "backup_env_get HC_PING_BACKUP").stdout == "hc-old"
    assert _lib(tmp_path, "backup_env_get HC_PING_HOST fallback").stdout == "fallback"


@needs_bash
def test_legacy_backup_keys_lists_only_filled_backup_keys_left_in_env(tmp_path):
    (tmp_path / ".env").write_text("RESTIC_PASSWORD=secret\nAWS_ACCESS_KEY_ID=\nEODHD_API_KEY=x\n")
    assert _lib(tmp_path, "legacy_backup_keys").stdout.split() == ["RESTIC_PASSWORD"]


def _migrate(deploy_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(deploy_dir / "scripts" / "migrate-backup-env.sh")],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


@needs_bash
def test_the_migration_moves_the_backup_keys_out_of_env(tmp_path):
    shutil.copytree(LIB.parent, tmp_path / "scripts")
    (tmp_path / ".env").write_text(
        "EODHD_API_KEY=e\nRESTIC_REPOSITORY=s3:x\nRESTIC_PASSWORD=pw\n"
        "AWS_ACCESS_KEY_ID=\nHC_PING_BACKUP=https://hc/1\nHC_PING_TICK=https://hc/2\n"
    )
    out = _migrate(tmp_path)
    assert out.returncode == 0, out.stderr
    env = (tmp_path / ".env").read_text()
    backup = (tmp_path / "backup.env").read_text()
    assert env == "EODHD_API_KEY=e\nHC_PING_TICK=https://hc/2\n"
    for line in ("RESTIC_REPOSITORY=s3:x", "RESTIC_PASSWORD=pw", "HC_PING_BACKUP=https://hc/1"):
        assert line in backup.splitlines()
    assert (tmp_path / "backup.env").stat().st_mode & 0o777 == 0o600
    # A second run changes nothing.
    again = _migrate(tmp_path)
    assert again.returncode == 0, again.stderr
    assert (tmp_path / "backup.env").read_text() == backup


@needs_bash
def test_the_migration_never_overwrites_a_value_already_in_backup_env(tmp_path):
    shutil.copytree(LIB.parent, tmp_path / "scripts")
    (tmp_path / ".env").write_text("RESTIC_PASSWORD=old\n")
    (tmp_path / "backup.env").write_text("RESTIC_PASSWORD=new\n")
    out = _migrate(tmp_path)
    assert out.returncode == 0, out.stderr
    assert (tmp_path / "backup.env").read_text() == "RESTIC_PASSWORD=new\n"
    assert (tmp_path / ".env").read_text() == ""
    assert "RESTIC_PASSWORD" in out.stderr  # says which old value it dropped


@needs_bash
def test_deploy_warns_while_backup_secrets_sit_in_env(tmp_path):
    deploy_dir = _server(tmp_path, deployed="v1.0.0")
    with (deploy_dir / ".env").open("a") as env:
        env.write("RESTIC_PASSWORD=pw\n")
    out = _deploy(deploy_dir, "v1.1.0")
    assert out.returncode == 0, out.stderr
    assert "migrate-backup-env.sh" in out.stderr
    assert "RESTIC_PASSWORD" in out.stderr


@needs_bash
def test_deploy_is_quiet_about_the_layout_once_migrated(tmp_path):
    deploy_dir = _server(tmp_path, deployed="v1.0.0")
    (deploy_dir / "backup.env").write_text("RESTIC_PASSWORD=pw\n")
    out = _deploy(deploy_dir, "v1.1.0")
    assert out.returncode == 0, out.stderr
    assert "migrate-backup-env.sh" not in out.stderr


def _backup(deploy_dir: Path) -> subprocess.CompletedProcess:
    shutil.copytree(LIB.parents[1] / "backup", deploy_dir / "backup")
    root = deploy_dir.parent
    return subprocess.run(
        [BASH, str(deploy_dir / "backup" / "backup.sh")],
        env={
            "PATH": f"{root / 'bin'}:/usr/bin:/bin",
            "FAKE_LOG": str(root / "docker.log"),
            "FAKE_SNAPDIR": str(root / "srv" / "snapshots"),
            "FAKE_BROKEN": "",
            "HOME": str(root),
        },
        capture_output=True,
        text=True,
        check=False,
    )


@needs_bash
def test_backup_still_runs_on_the_old_layout_and_says_how_to_migrate(tmp_path):
    """Until the operator moves the keys, restic gets them by name from the
    script's environment, never as values on the command line."""
    deploy_dir = _server(tmp_path, deployed="v1.0.0")
    with (deploy_dir / ".env").open("a") as env:
        env.write("RESTIC_REPOSITORY=s3:x\nRESTIC_PASSWORD=pw-secret\n")
    out = _backup(deploy_dir)
    assert out.returncode == 0, out.stderr
    assert "migrate-backup-env.sh" in out.stderr
    calls = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "-e RESTIC_PASSWORD" in calls
    assert "pw-secret" not in calls


@needs_bash
def test_backup_reads_backup_env_and_passes_nothing_extra(tmp_path):
    deploy_dir = _server(tmp_path, deployed="v1.0.0")
    (deploy_dir / "backup.env").write_text("RESTIC_REPOSITORY=s3:x\nRESTIC_PASSWORD=pw\n")
    out = _backup(deploy_dir)
    assert out.returncode == 0, out.stderr
    assert "migrate-backup-env.sh" not in out.stderr
    calls = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "run --rm -T restic backup" in calls
    assert "-e RESTIC" not in calls
