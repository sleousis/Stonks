"""``python -m stonks.ops backup | verify | restore | list | prune``."""

from stonks.ops.commands import app

if __name__ == "__main__":
    app(prog_name="python -m stonks.ops")
