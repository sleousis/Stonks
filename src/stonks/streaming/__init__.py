"""Live price streams (roadmap 21.1): the ``StreamingSource`` seam, ticks to
1m bars, the bar writer, the recorder and replayer, and the supervised
runner. Off by default (``[streaming] enabled``). See ``docs/design/intraday.md``.

This package imports nothing at load time, so ``stonks.config`` can import
its settings without a cycle.
"""
