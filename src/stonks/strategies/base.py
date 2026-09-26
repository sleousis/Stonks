"""BaseStrategy — concrete common behavior for the Strategy Protocol.

Subclasses override whatever they need; the base handles param validation +
default-filling, no-op ``fit``, empty-Features ``extract_features``, and
JSON-param save/load. This is the cleanest entry point for rule-based
strategies; ML strategies layer their own save/load.

Metadata (BL-26) is a set of duck-typed class attributes with safe defaults:
``hypothesis``, ``alpha_family``, ``premise``, ``label_horizon_bars`` and
``required_history_bars``. Callers read them through :func:`strategy_metadata`
(``getattr`` with defaults), so strategies that don't subclass
``BaseStrategy`` work too. A strategy whose horizon or history depends on
its params returns them from ``param_metadata()``: ``BaseStrategy.__init__``
sets them on the instance (RS-07, RS-30), and the class attribute stays the
value for the default params (the catalog and the API read it).

``data_tickers()`` names the tickers a strategy reads but does not trade (a
reference market, an index filter, a regime condition's ticker). The lab
copies their data into every worker snapshot and modified lake, next to the
universe (RS-01). Read it through :func:`strategy_data_tickers`. An optional ``asset_classes`` instance param
overrides ``applicable_asset_classes``, for opt-in use of a strategy outside
its evidence base.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, ClassVar, Literal, get_args

from stonks.core.params import Params, ParamSpace, validate_params
from stonks.core.types import AssetClass, Features

AlphaFamily = Literal[
    "trend",
    "reversion",
    "carry",
    "value",
    "quality",
    "growth",
    "sentiment",
    "data_driven",
    "benchmark",
    "other",
]
Premise = Literal["trend", "mean_reversion", "none"]
ALPHA_FAMILIES: tuple[str, ...] = get_args(AlphaFamily)
PREMISES: tuple[str, ...] = get_args(Premise)
_ASSET_CLASSES: tuple[str, ...] = get_args(AssetClass)

#: Instance param that overrides ``applicable_asset_classes``.
ASSET_CLASSES_PARAM = "asset_classes"


@dataclass(frozen=True)
class StrategyMetadata:
    """A strategy's hypothesis card and capability hooks (BL-26)."""

    hypothesis: str = ""
    alpha_family: AlphaFamily = "other"
    premise: Premise = "none"
    label_horizon_bars: int = 0
    required_history_bars: int = 0
    applicable_asset_classes: tuple[AssetClass, ...] = ("equity",)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["applicable_asset_classes"] = list(self.applicable_asset_classes)
        return out


def strategy_metadata(target: Any) -> StrategyMetadata:
    """The metadata of a strategy class or instance, with defaults for any
    attribute it doesn't declare. An instance reports its own (possibly
    overridden) ``applicable_asset_classes``."""
    d = StrategyMetadata()
    return StrategyMetadata(
        hypothesis=getattr(target, "hypothesis", d.hypothesis),
        alpha_family=getattr(target, "alpha_family", d.alpha_family),
        premise=getattr(target, "premise", d.premise),
        label_horizon_bars=getattr(target, "label_horizon_bars", d.label_horizon_bars),
        required_history_bars=getattr(target, "required_history_bars", d.required_history_bars),
        applicable_asset_classes=tuple(
            getattr(target, "applicable_asset_classes", d.applicable_asset_classes)
        ),
    )


def strategy_data_tickers(target: Any) -> tuple[str, ...]:
    """The tickers ``target`` reads but never trades (``data_tickers()``),
    empty when it declares none. Blank names are dropped, order is kept."""
    fn = getattr(target, "data_tickers", None)
    if not callable(fn):
        return ()
    try:
        tickers = fn()
    except TypeError:  # called on a class: the hook needs bound params
        return ()
    return tuple(dict.fromkeys(str(t) for t in tickers if t))


def _check_metadata(cls: type) -> None:
    name = cls.__name__
    if not isinstance(cls.hypothesis, str):  # type: ignore[attr-defined]
        raise ValueError(f"{name}.hypothesis must be a str")
    if cls.alpha_family not in ALPHA_FAMILIES:  # type: ignore[attr-defined]
        raise ValueError(f"{name}.alpha_family must be one of {ALPHA_FAMILIES}")
    if cls.premise not in PREMISES:  # type: ignore[attr-defined]
        raise ValueError(f"{name}.premise must be one of {PREMISES}")
    for attr in ("label_horizon_bars", "required_history_bars"):
        value = getattr(cls, attr)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name}.{attr} must be a non-negative int, got {value!r}")


def _parse_asset_classes(value: Any) -> tuple[AssetClass, ...]:
    if isinstance(value, str | bytes | Mapping) or not isinstance(value, list | tuple):
        raise ValueError(f"asset_classes must be a list of asset classes, got {value!r}")
    if not value:
        raise ValueError("asset_classes must be non-empty when given")
    unknown = [v for v in value if v not in _ASSET_CLASSES]
    if unknown:
        raise ValueError(
            f"asset_classes has unknown classes {unknown}; choose from {_ASSET_CLASSES}"
        )
    return tuple(dict.fromkeys(value))


class BaseStrategy:
    id: ClassVar[str] = "base"
    # ---- metadata (BL-26); override per strategy ----------------------------
    #: Mechanism, expected sign, who loses money to us, and when it should fail.
    hypothesis: ClassVar[str] = ""
    alpha_family: ClassVar[AlphaFamily] = "other"
    premise: ClassVar[Premise] = "none"
    #: Bars until a decision's outcome is known (embargo length for the lab).
    label_horizon_bars: ClassVar[int] = 0
    #: Bars of history ``decide`` needs before it can act.
    required_history_bars: ClassVar[int] = 0
    # AssetClass intersection — the Ranker drops universe tickers whose
    # asset class isn't in this tuple. Default keeps every existing
    # strategy equity-only without an opt-in change. Cross-class
    # strategies override (e.g. ``("equity", "crypto")``).
    applicable_asset_classes: ClassVar[tuple[AssetClass, ...]] = ("equity",)
    #: Negative scores mean "short", not only "less long" (roadmap 16).
    #: A short opens only when the strategy and the book both allow it;
    #: off by default. Read with ``getattr`` so any Strategy works.
    supports_short: ClassVar[bool] = False

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
        _check_metadata(cls)

    @classmethod
    def parameter_spec(cls) -> ParamSpace:
        return []

    def __init__(self, params: Params) -> None:
        spec = self.parameter_spec()
        params = dict(params)
        override = None
        if ASSET_CLASSES_PARAM in params and not any(s.name == ASSET_CLASSES_PARAM for s in spec):
            override = _parse_asset_classes(params[ASSET_CLASSES_PARAM])
            params[ASSET_CLASSES_PARAM] = list(override)
        validate_params(
            {k: v for k, v in params.items() if override is None or k != ASSET_CLASSES_PARAM},
            spec,
        )
        defaults = {s.name: s.default for s in spec}
        self.params: dict[str, Any] = {**defaults, **params}
        if override is not None:
            self.applicable_asset_classes = override  # type: ignore[misc]
            self._asset_classes_override = override
        for attr, value in self.param_metadata().items():
            if attr not in ("label_horizon_bars", "required_history_bars"):
                raise ValueError(f"param_metadata may not set {attr!r}")
            setattr(self, attr, max(0, int(value)))

    def param_metadata(self) -> dict[str, int]:
        """``label_horizon_bars`` / ``required_history_bars`` that depend on
        the params (see the module doc); empty keeps the class values. May
        read ``self.params`` only: it runs inside ``__init__``."""
        return {}

    def __setattr__(self, name: str, value: Any) -> None:
        # An explicit ``asset_classes`` override wins over classes a subclass
        # derives at init (a rule spec's universe, a wrapper's inner).
        if name == "applicable_asset_classes" and "_asset_classes_override" in self.__dict__:
            return
        super().__setattr__(name, value)

    # --- defaults ------------------------------------------------------------

    def extract_features(self, ticker: str, as_of: date, lake: Any) -> Features:
        return Features(values={})

    def fit(self, dataset: Any) -> None:
        return None

    def data_tickers(self) -> tuple[str, ...]:
        """Tickers this strategy reads but does not trade (see module doc)."""
        return ()

    # --- persistence ---------------------------------------------------------

    def save(self, path: Path) -> None:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        (path / "params.json").write_text(json.dumps(self.params, sort_keys=True, indent=2))
        (path / "meta.json").write_text(
            json.dumps(
                {
                    "id": self.id,
                    "class_path": self._class_path(),
                    "metadata": strategy_metadata(self).to_dict(),
                },
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
