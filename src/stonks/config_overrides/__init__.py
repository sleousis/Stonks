"""System settings an admin edits in the console (complexity audit F61).

The TOML config (``config/default.toml`` plus env) stays the base. A small
catalog of safe, non-secret operational keys (risk limits, the trading
universe, schedule switches and times, notification defaults) can be
overridden from the admin console. Overrides live in the state DB
(``settings_overrides``, one row per key) and every change writes an
``audit_log`` row. :func:`apply_overrides` lays them over the base settings
with full pydantic validation, section by section, so a bad value is
refused on write and a stored value that no longer validates is skipped
(and reported), never fatal.

Secrets, store paths, the API, auth and broker settings are never in the
catalog: they stay in the environment and TOML.
"""

from stonks.config_overrides.apply import (
    NotEditable,
    apply_override,
    apply_overrides,
    read_value,
    with_overrides,
)
from stonks.config_overrides.catalog import (
    Applies,
    EditableSetting,
    Group,
    editable_settings,
    find_setting,
)
from stonks.config_overrides.store import OverrideRow, OverrideStore, load_override_values

__all__ = [
    "Applies",
    "EditableSetting",
    "Group",
    "NotEditable",
    "OverrideRow",
    "OverrideStore",
    "apply_override",
    "apply_overrides",
    "editable_settings",
    "find_setting",
    "load_override_values",
    "read_value",
    "with_overrides",
]
