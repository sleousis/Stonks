"""``python -m stonks.connections``: manage broker connections from a shell.

The CLI acts as a local admin user (``--as <user id>``, default the
bootstrap owner ``usr_owner``), because shell access to the VM already
implies admin; scheduled passes and key rotation run as ``service:scheduler``.

Commands::

    providers                      enabled providers
    list                           the user's connections
    accounts CONNECTION            accounts on a connection (and linked portfolio)
    connect PROVIDER [--label L] [--real]   API-key providers: fields from
                                   STONKS_CONNECT_<FIELD> (e.g. STONKS_CONNECT_API_KEY) or a
                                   hidden prompt. --real: the keys are for a real-money
                                   account (default: paper or demo)
    connect PROVIDER --redirect URL   hosted-portal providers: prints the portal link
    callback CONNECTION --state S [--outcome O]   finish a portal connection
    link CONNECTION ACCOUNT [--portfolio ID] [--name N]
    sync [CONNECTION] [--due]      sync one connection now, or every due one
    disconnect CONNECTION --yes
    import-env                     [brokers].kind = alpaca: make pf_default mirror it
    rotate-keys                    re-seal credentials under the active master key

Secrets: credentials are read from the environment or a hidden prompt, never
from arguments (they would land in shell history), and never printed.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from typing import Any

from stonks.accounts import DEFAULT_OWNER_ID, NotFound, Scope, UserRepository
from stonks.connections.base import ConnectionsError
from stonks.connections.registry import enabled_provider
from stonks.connections.service import ConnectionService
from stonks.connections.settings import ConnectionsConfig
from stonks.security import SecretBoxError
from stonks.store.state import SqliteState

FIELD_ENV_PREFIX = "STONKS_CONNECT_"


class _UsageError(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m stonks.connections", description=__doc__.split("\n")[0]
    )
    p.add_argument(
        "--config", type=Path, default=None, help="TOML config (default config/default.toml)"
    )
    p.add_argument("--as", dest="as_user", default=DEFAULT_OWNER_ID, help="act as this user id")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("providers")
    sub.add_parser("list")
    a = sub.add_parser("accounts")
    a.add_argument("connection")
    c = sub.add_parser("connect")
    c.add_argument("provider")
    c.add_argument("--label")
    c.add_argument("--redirect", help="callback URL for hosted-portal providers")
    c.add_argument("--no-link", action="store_true", help="don't create broker portfolios")
    c.add_argument(
        "--real",
        action="store_true",
        help="the keys are for a real-money account (default: paper or demo)",
    )
    cb = sub.add_parser("callback")
    cb.add_argument("connection")
    cb.add_argument("--state", required=True)
    cb.add_argument("--outcome")
    ln = sub.add_parser("link")
    ln.add_argument("connection")
    ln.add_argument("account")
    ln.add_argument("--portfolio")
    ln.add_argument("--name")
    s = sub.add_parser("sync")
    s.add_argument("connection", nargs="?")
    s.add_argument("--due", action="store_true", help="every due connection (scheduler pass)")
    d = sub.add_parser("disconnect")
    d.add_argument("connection")
    d.add_argument("--yes", action="store_true")
    sub.add_parser("import-env")
    sub.add_parser("rotate-keys")
    return p  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except ImportError:  # pragma: no cover
        pass
    args = _parser().parse_args(argv)
    from stonks.config import load_settings

    try:
        settings = load_settings(args.config)
        config = ConnectionsConfig.load(args.config)
        _STDOUT[0] = sys.stdout
        with SqliteState(settings.state.path) as state, redirect_stdout(sys.stderr):
            state.migrate()
            service = ConnectionService(state, config)
            return _dispatch(args, settings, config, state, service)
    except _UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ConnectionsError, NotFound, SecretBoxError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(
    args: argparse.Namespace,
    settings: Any,
    config: ConnectionsConfig,
    state: SqliteState,
    service: ConnectionService,
) -> int:
    scheduler = Scope.service("scheduler")
    cmd = args.command
    if cmd == "providers":
        providers = service.available_providers(scheduler)
        if args.json:
            _emit_json([asdict(p) for p in providers])
        elif not providers:
            _say("no provider is enabled; an admin adds names to [connections].enabled_providers")
        for p in providers if not args.json else []:
            _say(f"{p.name}\t{p.auth_flow}\t{','.join(p.capabilities)}")
        return 0
    if cmd == "rotate-keys":
        _say(f"re-sealed {service.rotate_credentials(scheduler)} credential(s)")
        return 0
    if cmd == "sync" and args.due:
        results = service.sync_due(scheduler)
        _print_sync(args, results)
        return 0 if all(r.ok for r in results) else 1
    scope = _scope(state, args.as_user)
    if cmd == "list":
        rows = service.list(scope)
        if args.json:
            _emit_json([asdict(r) for r in rows])
        for r in rows if not args.json else []:
            _say(f"{r.id}\t{r.provider}\t{r.status}\t{r.last_sync_at or '-'}\t{r.label or ''}")
        return 0
    if cmd == "accounts":
        rows = service.accounts(scope, args.connection)
        if args.json:
            _emit_json([asdict(r) for r in rows])
        for a in rows if not args.json else []:
            _say(
                f"{a.external_account_id}\t{a.name}\t{a.number_mask or ''}\t{a.portfolio_id or '-'}"
            )
        return 0
    if cmd == "connect":
        return _connect(args, config, service, scope)
    if cmd == "callback":
        rec = service.complete_portal(scope, args.connection, args.state, outcome=args.outcome)
        _say(f"connection {rec.id} {rec.status}")
        return 0
    if cmd == "link":
        pid = service.link_account(
            scope, args.connection, args.account, portfolio_id=args.portfolio, name=args.name
        )
        _say(f"account {args.account} -> portfolio {pid}")
        return 0
    if cmd == "sync":
        if not args.connection:
            raise _UsageError("give a connection id or --due")
        result = service.sync(scope, args.connection)
        _print_sync(args, [result])
        return 0 if result.ok else 1
    if cmd == "disconnect":
        if not args.yes:
            raise _UsageError("disconnect deletes stored credentials and activities; add --yes")
        out = service.disconnect(scope, args.connection)
        _say(f"disconnected {out.connection_id}; archived portfolios: "
              f"{', '.join(out.archived_portfolios) or 'none'}")  # fmt: skip
        if out.remote_removed is False:
            print(f"warning: provider-side user not removed: {out.remote_error}", file=sys.stderr)
        return 0
    if cmd == "import-env":
        result = service.import_env(scope, settings)
        _say(result.message)
        if result.subscriptions_switched:
            _say(f"subscriptions switched to auto: {', '.join(result.subscriptions_switched)}")
        return 0
    raise _UsageError(f"unknown command {cmd!r}")  # pragma: no cover


def _connect(
    args: argparse.Namespace, config: ConnectionsConfig, service: ConnectionService, scope: Scope
) -> int:
    cls = enabled_provider(config, args.provider)
    if cls.auth_flow == "portal":
        if not args.redirect:
            raise _UsageError(f"{args.provider} needs --redirect <callback URL>")
        link = service.start_portal(scope, args.provider, args.redirect, label=args.label)
        _say(f"connection {link.connection_id} pending")
        _say(f"open this link to connect (expires {link.expires_at}):\n{link.url}")
        return 0
    if args.real and not cls.has_paper:
        raise _UsageError(f"{args.provider} has no paper account, so --real does not apply")
    fields = {name: _field(name) for name in cls.credential_fields}
    if cls.has_paper:
        fields["paper"] = "false" if args.real else "true"
    rec = service.connect_with_keys(
        scope, args.provider, fields, label=args.label, link_accounts=not args.no_link
    )
    _say(f"connection {rec.id} {rec.status}")
    return 0


def _field(name: str) -> str:
    env = f"{FIELD_ENV_PREFIX}{name.upper()}"
    value = os.environ.get(env)
    if value:
        return value
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(f"{name}: ")
    raise _UsageError(f"set {env} (or run interactively to be prompted)")


def _scope(state: SqliteState, user_id: str) -> Scope:
    user = UserRepository(state).get(user_id)
    if user.status != "active":
        raise NotFound(f"user {user_id!r} not found")
    return Scope.for_user(user)


def _print_sync(args: argparse.Namespace, results: list[Any]) -> None:
    if args.json:
        _emit_json([asdict(r) for r in results])
        return
    for r in results:
        line = f"{r.connection_id}\t{r.status}"
        if r.error:
            line += f"\t{r.error}"
        _say(line)
        for p in r.portfolios:
            detail = (
                f"  {p.portfolio_id}: {p.positions} positions, {p.activities_new} new activities"
            )
            if p.unmapped:
                detail += f"; not covered: {', '.join(p.unmapped)}"
            if p.error:
                detail += f"; error: {p.error}"
            _say(detail)


def _emit_json(data: Any) -> None:
    _say(json.dumps(data, indent=2, default=str))


_STDOUT: list[Any] = [sys.stdout]


def _say(*parts: Any) -> None:
    """Command output goes to the real stdout; everything else printed
    while a command runs (structlog's JSON lines) goes to stderr."""
    print(*parts, file=_STDOUT[0])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
