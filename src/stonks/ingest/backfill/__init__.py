"""The resumable vendor backfill behind ``stonks ingest backfill``
(docs/backfill.md).

Work is split into phases (one module each in :mod:`.phases`, discovered by
:mod:`.registry`) and each phase into units (an exchange, a ticker, a
calendar, a currency pair). :class:`~.state.BackfillState` keeps every
unit's progress in the state database, :class:`~.budget.CallBudget` keeps
the run inside the vendor's daily allowance, and
:class:`~.runner.BackfillRunner` fetches on at most two threads and writes
through the ingest pipeline on one.

This package's ``__init__`` stays empty of imports so ``stonks.config`` can
load :mod:`.settings` without an import cycle.
"""
