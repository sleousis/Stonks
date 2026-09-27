"""``stonks telegram``: link codes, link status and the bot's poll loop
(roadmap 20.3). Mounted by :mod:`stonks.cli`.

The shell is the operator, so ``--user`` names whose chat a command is
about. The bot token comes from ``STONKS_TELEGRAM_BOT_TOKEN`` only."""

from __future__ import annotations

from typing import Any

import typer
from rich.console import Console

app = typer.Typer(help="Telegram bot: link a chat and run the bot", no_args_is_help=True)

_USER = typer.Option(..., "--user", help="the user (email or id)")


def _context() -> Any:
    from stonks.app.context import AppContext
    from stonks.cli import _settings

    context = AppContext(_settings())
    with context.state() as state:
        state.migrate()
    return context


def _user_id(context: Any, user: str) -> str:
    from stonks.accounts import NotFound, UserRepository

    with context.state() as state:
        users = UserRepository(state)
        try:
            return (users.get_by_email(user) if "@" in user else users.get(user)).id
        except NotFound:
            raise typer.BadParameter(f"no user {user!r}", param_hint="--user") from None


@app.command("link-code")
def link_code(user: str = _USER) -> None:
    """Make a one-time code. Send /link CODE to the bot from the chat."""
    from stonks.telegram.links import LinkStore
    from stonks.telegram.settings import bot_configured

    context = _context()
    uid = _user_id(context, user)
    if not bot_configured():
        Console().print("[yellow]note[/yellow]: STONKS_TELEGRAM_BOT_TOKEN is not set here")
    with context.state() as state:
        code, expires = LinkStore(state).create_code(
            uid, minutes=context.settings.telegram.link_code_minutes, actor="service:cli"
        )
    Console().print(f"code [bold]{code}[/bold], valid until {expires.isoformat()}")
    Console().print(f"send: /link {code}")


@app.command("status")
def status(user: str = _USER) -> None:
    """Show the bot's configuration and the user's linked chat."""
    from stonks.telegram.links import LinkStore
    from stonks.telegram.settings import bot_configured

    context = _context()
    uid = _user_id(context, user)
    with context.state() as state:
        link = LinkStore(state).for_user(uid)
    cfg = context.settings.telegram
    Console().print(f"token set: {bot_configured()}, polling enabled: {cfg.enabled}")
    if link is None:
        Console().print("no chat linked")
    else:
        name = f"@{link.username}" if link.username else "(no username)"
        Console().print(f"linked to {name} since {link.linked_at.isoformat()}")


@app.command("unlink")
def unlink(user: str = _USER) -> None:
    """Unlink the user's chat."""
    from stonks.telegram.links import LinkStore

    context = _context()
    uid = _user_id(context, user)
    with context.state() as state:
        done = LinkStore(state).unlink_user(uid, actor="service:cli")
    Console().print("unlinked" if done else "no chat was linked")


@app.command("poll")
def poll(once: bool = typer.Option(False, "--once", help="one round, then exit")) -> None:
    """Run the bot in the foreground (long polling, no webhook needed).
    Don't run it next to a `stonks serve` that has \\[telegram].enabled on."""
    from stonks.telegram.bot import build_bot

    context = _context()
    bot = build_bot(context, context.settings.telegram)
    if bot is None:
        raise typer.BadParameter("set STONKS_TELEGRAM_BOT_TOKEN first")
    if once:
        handled = bot.poll_once(timeout=0)
        Console().print(f"handled {handled} update(s)")
        return
    Console().print("polling Telegram, Ctrl+C to stop")
    try:
        while True:
            bot.poll_once()
    except KeyboardInterrupt:
        Console().print("stopped")
