"""Factors (roadmap 22.2, 22.3, 22.8): named, reusable date-by-ticker
signals computed once over a whole universe.

- :mod:`stonks.factors.expression`: the expression language (Qlib style).
- :mod:`stonks.factors.sql`: expressions compiled to DuckDB SQL.
- :mod:`stonks.factors.engine`: panels read from the lake, adjusted as of
  each date.
"""
