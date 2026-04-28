"""BaseStrategy — concrete common behavior for the Strategy Protocol.

Subclasses override whatever they need; the base handles param validation +
default-filling, no-op ``fit``, empty-Features ``extract_features``, and
JSON-param save/load. This is the cleanest entry point for rule-based
strategies; ML strategies layer their own save/load.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

from stonks.core.params import Params, ParamSpace, validate_params
from stonks.core.types import AssetClass, Features


class BaseStrategy:
    id: ClassVar[str] = "base"
    # AssetClass intersection — the Ranker drops universe tickers whose
    # asset class isn't in this tuple. Default keeps every existing
    # strategy equity-only without an opt-in change. Cross-class
    # strategies override (e.g. ``("equity", "crypto")``).
    applicable_asset_classes: ClassVar[tuple[AssetClass, ...]] = ("equity",)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # Catch the ``applicable_asset_classes = ()`` footgun at class
        # definition time. Empty tuple silently means "applies to
        # nothing" — the Ranker filter would then drop every ticker
        # for the strategy with no error signal.
        super().__init_subclass__(**kwargs)
        if not cls.applicable_asset_classes:
            raise ValueError(
                f"{cls.__name__}.applicable_asset_classes must be non-empty; "
                f"declare at least one AssetClass the strategy is meant to handle"
            )

    @classmethod
    def parameter_spec(cls) -> ParamSpace:
        return []

    def __init__(self, params: Params) -> None:
        spec = self.parameter_spec()
        validate_params(params, spec)
        defaults = {s.name: s.default for s in spec}
        self.params: dict[str, Any] = {**defaults, **dict(params)}

    # --- defaults ------------------------------------------------------------

    def extract_features(self, ticker: str, as_of: date, lake: Any) -> Features:
        return Features(values={})

    def fit(self, dataset: Any) -> None:
        return None

    # --- persistence ---------------------------------------------------------

    def save(self, path: Path) -> None:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        (path / "params.json").write_text(json.dumps(self.params, sort_keys=True, indent=2))
        (path / "meta.json").write_text(
            json.dumps(
                {"id": self.id, "class_path": self._class_path()},
                sort_keys=True,
                indent=2,
            )
        )

    @classmethod
    def load(cls, path: Path) -> BaseStrategy:
        params = json.loads((Path(path) / "params.json").read_text())
        return cls(params)

    @classmethod
    def _class_path(cls) -> str:
        return f"{cls.__module__}:{cls.__name__}"
