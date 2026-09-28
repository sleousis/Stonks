"""Live options at a real broker (roadmap 17.8). Off by default.

Every path passes one gate (``gate.py``): ``[production.options] live``,
the portfolio's live stage (``live_small`` or higher) and its options
approval level (``approval.py``). Closing a position never needs them.
See ``docs/design/options.md`` section 11.
"""
