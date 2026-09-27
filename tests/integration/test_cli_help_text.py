"""CLI help text keeps its bracketed words (docs mismatches in review 18.1).

Typer renders help with rich markup, which swallows anything that looks
like a tag, such as ``[notify]`` or ``[production].universe``. Help strings
escape those brackets, so the rendered help must show every one.
"""

from __future__ import annotations

import re

import click
import pytest
import typer
from typer.testing import CliRunner

from stonks.cli import app

_TOKEN = re.compile(r"\\?\[([A-Za-z_][\w.]*)\]")


def _commands(group: click.Command, path: tuple[str, ...] = ()):
    yield path, group
    if isinstance(group, click.Group):
        for name, sub in group.commands.items():
            yield from _commands(sub, (*path, name))


def _raw_help(command: click.Command) -> str:
    parts = [command.help or ""]
    parts += [getattr(p, "help", None) or "" for p in command.params]
    return "\n".join(parts)


_ALL = list(_commands(typer.main.get_command(app)))


@pytest.mark.parametrize(
    ("path", "command"),
    [(p, c) for p, c in _ALL if _TOKEN.search(_raw_help(c))],
    ids=lambda v: " ".join(v) if isinstance(v, tuple) else "",
)
def test_bracketed_words_survive_in_help(path, command):
    # A command that passes --help through (``schedule``) shows its own help
    # text in the parent's command list.
    passes_help_on = command.context_settings.get("help_option_names") == []
    args = [*path[:-1], "--help"] if passes_help_on else [*path, "--help"]
    result = CliRunner().invoke(app, args, env={"COLUMNS": "400"})
    assert result.exit_code == 0, result.output
    shown = " ".join(result.output.split())
    for token in _TOKEN.findall(_raw_help(command)):
        assert f"[{token}]" in shown, (
            f"'[{token}]' is missing from `stonks {' '.join(path)} --help`"
        )


def test_the_bracket_check_covers_the_commands_from_the_review():
    covered = {" ".join(p) for p, c in _ALL if _TOKEN.search(_raw_help(c))}
    assert {"schedule", "health", "serve"} <= covered


def test_tick_help_does_not_promise_a_single_winner():
    result = CliRunner().invoke(app, ["tick", "--help"], env={"COLUMNS": "400"})
    assert "pick a winner" not in result.output
    assert "\u00d7" not in result.output  # the multiplication sign breaks Windows consoles


def test_halts_kill_names_the_buys_only_option():
    result = CliRunner().invoke(app, ["halts", "kill", "--help"], env={"COLUMNS": "400"})
    shown = " ".join(result.output.split())
    assert "--buys-only" in shown and "only stops buys" in shown
    assert "--flatten" not in shown  # the deprecated alias is hidden


def test_help_does_not_leak_roadmap_numbers():
    for path, command in _ALL:
        assert "roadmap" not in (command.help or "").lower(), " ".join(path)


@pytest.mark.parametrize("name", ["list", "show"])
def test_registry_commands_have_help(name):
    command = typer.main.get_command(app).commands["registry"].commands[name]
    assert command.help
