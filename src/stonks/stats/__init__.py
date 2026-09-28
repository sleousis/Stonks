"""Statistical inference kernel: numpy + scipy only, no Stonks imports.

``backtest/``, ``lab/`` and ``production/`` all build on it:

- :mod:`stonks.stats.sharpe` - Sharpe standard error, PSR, MinTRL, expected
  maximum Sharpe, DSR and the effective number of trials.
- :mod:`stonks.stats.bootstrap` - stationary bootstrap and Sharpe CIs.
- :mod:`stonks.stats.hac` - Newey-West standard error of a mean.
- :mod:`stonks.stats.pbo` - probability of backtest overfitting (CSCV).
- :mod:`stonks.stats.multiple_testing` - Holm and Benjamini-Hochberg.
- :mod:`stonks.stats.data_snooping` - White's Reality Check, Hansen's SPA
  and the Romano-Wolf step-down over many strategies at once.
"""
