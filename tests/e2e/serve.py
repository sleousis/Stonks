"""``stonks serve`` for the e2e stack, with every data source swapped for the
canned one (:mod:`tests.e2e.fake_market`), so no request can reach a vendor,
the assistant's model swapped for :mod:`tests.e2e.fake_assistant`, and,
when ``STONKS_E2E_TELEGRAM_LOG`` is set, Telegram swapped for a file
(:mod:`tests.e2e.fake_telegram`).

Started by :func:`tests.e2e.stack.start_server` with the stack root as the
working directory (``config/default.toml`` is read from there) and
``STONKS_E2E_MARKET_END`` naming the last bar date.
"""

from __future__ import annotations

import os
import sys
from datetime import date


def _install_canned_source() -> None:
    import stonks.app.context as context
    import stonks.ingest.sources.registry as registry
    from tests.e2e.fake_market import CannedDataSource, build_market

    market = build_market(date.fromisoformat(os.environ["STONKS_E2E_MARKET_END"]))

    def canned(_cfg: object) -> CannedDataSource:
        return CannedDataSource(market)

    # Every registered id builds the canned source; the app context uses it
    # for ingest, lab ensure and universe ensure alike.
    for source_id in list(registry._FACTORIES):
        registry._FACTORIES[source_id] = canned
    context.AppContext.build_source = lambda self, source_id=None: CannedDataSource(market)


def _install_fake_assistant() -> None:
    """The assistant talks to :class:`KeywordChatModel`, never a model server
    (``[assistant] base_url`` in the stack config only switches it on)."""
    import stonks.app.assistant as assistant
    from tests.e2e.fake_assistant import KeywordChatModel

    assistant.default_model = lambda _config: KeywordChatModel()


def _install_fake_telegram() -> None:
    """With ``STONKS_E2E_TELEGRAM_LOG`` set, every Telegram send lands in
    that file (:mod:`tests.e2e.fake_telegram`), never at Telegram."""
    from tests.e2e.fake_telegram import LOG_ENV, FileTelegramApi

    path = os.environ.get(LOG_ENV)
    if not path:
        return
    import stonks.telegram.bot as bot
    import stonks.telegram.channel as channel

    def fake(*_args: object, **_kwargs: object) -> FileTelegramApi:
        return FileTelegramApi(path)

    channel.HttpTelegramApi = fake  # type: ignore[assignment,misc]
    bot.HttpTelegramApi = fake  # type: ignore[assignment,misc]


def main(argv: list[str] | None = None) -> None:
    _install_canned_source()
    _install_fake_assistant()
    _install_fake_telegram()
    from stonks.cli import app

    app(["serve", *(argv if argv is not None else sys.argv[1:])])


if __name__ == "__main__":
    main()
