"""Lab offload (roadmap 14.9): heavy lab jobs run in a separate worker
process that reads a read-only lake snapshot, so the API stays responsive.

- :mod:`.settings`: ``[lab.offload]``.
- :mod:`.executor`: the ``LabExecutor`` seam (in-process default, worker).
- :mod:`.queue`: the worker queue over the ``jobs`` table.
- :mod:`.snapshot`: read-only lake snapshots.
- :mod:`.health`: queue health and Prometheus metrics.
- :mod:`.worker`: the worker loop (``python -m stonks.lab.offload worker``).
"""
