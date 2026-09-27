"""The Telegram bot (roadmap 20.3): a notification channel plus commands.

- :mod:`~stonks.telegram.api`: the :class:`TelegramApi` seam and its HTTP
  implementation over the Bot API (the only module that speaks HTTP to
  Telegram). :mod:`~stonks.telegram.fake` is the hermetic fake.
- :mod:`~stonks.telegram.links`: one-time link codes and the chat to user
  links (one chat, one user).
- :mod:`~stonks.telegram.channel`: the ``telegram`` notification channel.
- :mod:`~stonks.telegram.commands`: what each command does, as the linked user.
- :mod:`~stonks.telegram.bot`: long polling with a stored offset, and the
  hosted runner ``stonks serve`` starts when the bot is enabled.

The bot token comes from ``STONKS_TELEGRAM_BOT_TOKEN`` only, never TOML.
"""
