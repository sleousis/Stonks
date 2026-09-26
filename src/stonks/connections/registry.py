"""Provider registry. A provider is one module in
``stonks.connections.providers`` whose adapter class is decorated with
:func:`register_provider`; adding one never edits a list.

Registered is not usable: :func:`enabled_provider` also requires the name in
``[connections].enabled_providers`` and the provider's app-level settings.
"""

from __future__ import annotations

import importlib
import pkgutil
import threading
from collections.abc import Callable

from stonks.connections.base import BrokerConnection, ProviderDisabled
from stonks.connections.settings import ConnectionsConfig

_PROVIDERS: dict[str, type[BrokerConnection]] = {}
_LOCK = threading.Lock()
_DISCOVERED = False


def register_provider(
    name: str,
) -> Callable[[type[BrokerConnection]], type[BrokerConnection]]:
    def decorate(cls: type[BrokerConnection]) -> type[BrokerConnection]:
        if not name or name != name.lower() or not name.replace("_", "").isalnum():
            raise ValueError(f"bad provider name {name!r}")
        existing = _PROVIDERS.get(name)
        if existing is not None and existing.__qualname__ != cls.__qualname__:
            raise ValueError(f"provider {name!r} is registered twice")
        cls.provider = name
        _PROVIDERS[name] = cls
        return cls

    return decorate


def _discover() -> None:
    global _DISCOVERED
    with _LOCK:
        if _DISCOVERED:
            return
        import stonks.connections.providers as package

        for info in pkgutil.iter_modules(package.__path__):
            if not info.name.startswith("_"):
                importlib.import_module(f"{package.__name__}.{info.name}")
        _DISCOVERED = True


def provider_classes() -> dict[str, type[BrokerConnection]]:
    """Every registered provider, enabled or not, by name."""
    _discover()
    return dict(sorted(_PROVIDERS.items()))


def provider_class(name: str) -> type[BrokerConnection]:
    cls = provider_classes().get(name)
    if cls is None:
        raise ProviderDisabled(f"unknown provider {name!r}")
    return cls


def enabled_provider(config: ConnectionsConfig, name: str) -> type[BrokerConnection]:
    """The provider's adapter class if an admin enabled it and it is
    configured; :class:`ProviderDisabled` / ``ProviderNotConfigured`` otherwise."""
    cls = provider_class(name)
    if not config.is_enabled(name):
        raise ProviderDisabled(
            f"provider {name!r} is not enabled; an admin adds it to [connections].enabled_providers"
        )
    cls.check_configured(config)
    return cls
