"""Operator commands for backups: ``python -m stonks.ops <command>``.

``app`` is a Typer app; the main CLI mounts it as ``stonks backup``
(``stonks backup backup|verify|restore|list|prune``).
Store paths come from the usual settings (``config/default.toml``,
``STONKS_DATA_DIR``); backups go to ``[backup].dir`` or, by default, a
``backups`` folder next to the lake.
"""

from __future__ import annotations

from pathlib import Path

import typer

from stonks.ops.backup import (
    BackupError,
    DataPaths,
    LocalFilesystemTarget,
    configured_target,
    expired_backups,
    run_configured_backup,
    verify_backup,
)
from stonks.ops.restore import RestoreError, restore_backup

app = typer.Typer(help="Backups of the lake, state DB and artifacts.", no_args_is_help=True)

_CONFIG = typer.Option(None, "--config", help="settings TOML (default config/default.toml)")
_DEST = typer.Option(
    None, "--dest", help="backup folder (default [backup].dir or <lake dir>/backups)"
)
_DATA_DIR = typer.Option(
    None, "--data-dir", help="restore into this folder instead of the configured paths"
)
_FORCE = typer.Option(
    False, "--force", help="move existing data aside (never deleted) and restore over it"
)


def _settings(config: Path | None):
    from dotenv import load_dotenv

    from stonks.config import load_settings
    from stonks.logging import configure_logging

    load_dotenv(override=False)
    settings = load_settings(config)
    configure_logging(level=settings.logging.level)
    return settings


def _target(settings, dest: Path | None) -> LocalFilesystemTarget:
    return configured_target(settings, dest)


def _resolve(target: LocalFilesystemTarget, backup: str) -> Path:
    path = Path(backup)
    if path.is_dir():
        return path
    return target.fetch(backup)


def _fail(message: str) -> None:
    typer.echo(message)
    raise typer.Exit(1)


@app.command()
def backup(
    dest: Path | None = _DEST,
    no_prune: bool = typer.Option(False, "--no-prune", help="keep every older backup"),
    config: Path | None = _CONFIG,
) -> None:
    """Take a consistent backup, verify it, then apply the retention policy."""
    settings = _settings(config)
    target = _target(settings, dest)
    try:
        result = run_configured_backup(settings, dest=dest, prune=not no_prune)
    except BackupError as exc:
        _fail(f"backup failed: {exc}")
    typer.echo(f"backup {result.ref.id} written to {target.root / result.ref.id}")
    for old in result.pruned:
        typer.echo(f"pruned {old}")


@app.command()
def verify(
    backup: str = typer.Argument(..., help="backup id or folder"),
    dest: Path | None = _DEST,
    config: Path | None = _CONFIG,
) -> None:
    """Re-check a backup's checksums, schema versions and row counts."""
    settings = _settings(config)
    try:
        path = _resolve(_target(settings, dest), backup)
    except BackupError as exc:
        _fail(str(exc))
    report = verify_backup(path)
    if not report.ok:
        _fail("\n".join([f"backup {path} FAILED verification:", *report.problems]))
    typer.echo(f"backup {path} OK")


@app.command()
def restore(
    backup: str = typer.Argument(..., help="backup id or folder"),
    data_dir: Path | None = _DATA_DIR,
    force: bool = _FORCE,
    dest: Path | None = _DEST,
    config: Path | None = _CONFIG,
) -> None:
    """Restore a backup into an empty data dir, then run migrations."""
    settings = _settings(config)
    try:
        path = _resolve(_target(settings, dest), backup)
    except BackupError as exc:
        _fail(str(exc))
    target = DataPaths.under(data_dir) if data_dir else DataPaths.from_settings(settings)
    try:
        result = restore_backup(path, target, force=force)
    except RestoreError as exc:
        _fail(f"restore refused: {exc}")
    typer.echo(f"restored {path} into {target.lake.parent}")
    for aside in result.moved_aside:
        typer.echo(f"previous data kept at {aside}")
    if result.lake_migrations_applied or result.state_migrations_applied:
        typer.echo(
            f"migrations applied: lake {result.lake_migrations_applied}, "
            f"state {result.state_migrations_applied}"
        )


@app.command("list")
def list_backups(dest: Path | None = _DEST, config: Path | None = _CONFIG) -> None:
    """List backups, newest first."""
    target = _target(_settings(config), dest)
    refs = target.list()
    if not refs:
        typer.echo(f"no backups under {target.root}")
    for ref in refs:
        typer.echo(f"{ref.id}  {ref.created_at.isoformat()}")


@app.command()
def prune(dest: Path | None = _DEST, config: Path | None = _CONFIG) -> None:
    """Delete backups the retention policy no longer keeps."""
    settings = _settings(config)
    target = _target(settings, dest)
    for old in expired_backups(target.list(), settings.backup.retention):
        target.delete(old.id)
        typer.echo(f"pruned {old.id}")
