"""Live trading at a real broker (roadmap Phase 19, ``docs/design/live-trading.md``).

- ``context.py``: :class:`LiveContext`, what the live safeguards and the
  account rules see of a live book, and :func:`build_live_context`;
- ``quotes.py``: the reference price of a ticker (a live quote, else a
  delayed one, else the lake close);
- ``allocation.py``: the amount the owner lets Stonks trade per live
  portfolio (set by hand, audited);
- ``runaway.py``: the ``runaway`` halt a run with too many closes opens;
- ``settings.py``: ``[production.live]``.

The safeguards themselves are registered risk rules in
``production/rules/`` (``capital_ramp``, ``live_caps``, ``price_band``,
``max_orders``, ``account_rules``).
"""
