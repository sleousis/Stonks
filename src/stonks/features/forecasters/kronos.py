"""Kronos adapters (MIT code and weights): a foundation model for candles.

Kronos reads open, high, low, close and volume, quantizes each candle into
tokens and samples future candles. Its code is not on PyPI, so it takes
two steps to install:

1. ``uv sync --extra kronos`` installs torch and the model's other needs.
2. Clone https://github.com/shiyu-coder/Kronos and point
   ``STONKS_KRONOS_REPO`` (or the ``repo_path`` option) at the clone. The
   adapter loads the clone's ``model`` package under a private name.

Models (weights on Hugging Face under ``NeoQuasar``):

- ``kronos_small``: Kronos-small (24.7M) with Kronos-Tokenizer-base,
  context 512. Weights released 2025-06-30.
- ``kronos_mini``: Kronos-mini (4.1M) with Kronos-Tokenizer-2k, context
  2048. Weights released 2025-07-01.

The README does not give the date range of the pretraining candles, so the
cutoff is the release date. Each forecast draws ``n_samples`` paths; the
quantiles come from them.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from stonks.features.forecasters._pretrained import ModelUnavailableError, PretrainedForecaster
from stonks.features.forecasters.base import DEFAULT_LEVELS, Forecast, _check_levels

__all__ = ["KronosMiniForecaster", "KronosSmallForecaster"]

#: Environment variable naming the Kronos clone.
REPO_ENV = "STONKS_KRONOS_REPO"
#: Private module name the clone's ``model`` package is loaded under.
_MODULE = "_stonks_kronos_model"
_OHLCV = ["open", "high", "low", "close", "volume"]


def _repo(path: str | os.PathLike[str] | None) -> Path | None:
    raw = path or os.environ.get(REPO_ENV)
    if not raw:
        return None
    root = Path(raw)
    return root if (root / "model" / "__init__.py").is_file() else None


def _load_package(root: Path) -> Any:
    if _MODULE in sys.modules:
        return sys.modules[_MODULE]
    init = root / "model" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        _MODULE, init, submodule_search_locations=[str(init.parent)]
    )
    if spec is None or spec.loader is None:
        raise ModelUnavailableError(f"cannot load the Kronos code from {root}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE] = module
    spec.loader.exec_module(module)
    return module


class _Kronos(PretrainedForecaster):
    licence: ClassVar[str] = "MIT"
    requires: ClassVar[tuple[str, ...]] = ("torch", "einops", "huggingface_hub", "safetensors")
    extra: ClassVar[str | None] = "kronos"
    reads_ohlcv: ClassVar[bool] = True
    tokenizer_id: ClassVar[str] = ""

    def __init__(
        self,
        model_id: str | None = None,
        *,
        tokenizer_id: str | None = None,
        repo_path: str | None = None,
        n_samples: int = 20,
        temperature: float = 1.0,
        top_p: float = 0.9,
        **options: Any,
    ) -> None:
        super().__init__(model_id, **options)
        if n_samples < 1:
            raise ValueError(f"n_samples must be >= 1, got {n_samples}")
        self.tokenizer_name = tokenizer_id or self.tokenizer_id
        self.repo_path = repo_path
        self.n_samples = int(n_samples)
        self.temperature = float(temperature)
        self.top_p = float(top_p)

    @classmethod
    def is_available(cls) -> bool:
        return super().is_available() and _repo(None) is not None

    def _require(self) -> None:
        super()._require()
        if _repo(self.repo_path) is None:
            raise ModelUnavailableError(
                f"forecaster {self.name!r} needs the Kronos code: clone "
                f"https://github.com/shiyu-coder/Kronos and set {REPO_ENV} to the clone"
            )

    def _load(self) -> Any:
        root = _repo(self.repo_path)
        assert root is not None  # checked by _require
        package = _load_package(root)
        kw = {"local_files_only": self.local_files_only}
        tokenizer = package.KronosTokenizer.from_pretrained(self.tokenizer_name, **kw)
        model = package.Kronos.from_pretrained(self.model_id, **kw)
        return package.KronosPredictor(
            model, tokenizer, device=self.device, max_context=self.max_context
        )

    def predict_batch(
        self,
        contexts: Sequence[pd.DataFrame],
        horizon: int,
        levels: Sequence[float] = DEFAULT_LEVELS,
    ) -> list[Forecast]:
        h = self._check_horizon(horizon)
        lv = _check_levels(levels)
        self._closes(contexts)  # the same checks as every model
        predictor = self.model()
        out = []
        for context in contexts:
            missing = [c for c in _OHLCV if c not in context.columns]
            if missing or "timestamp" not in context.columns:
                raise ValueError(f"Kronos needs timestamp and OHLCV columns, missing {missing}")
            frame = context.iloc[-self.max_context :]
            stamps = pd.to_datetime(frame["timestamp"]).reset_index(drop=True)
            future = pd.Series(pd.bdate_range(stamps.iloc[-1], periods=h + 1)[1:])
            x = frame[_OHLCV].astype(float).reset_index(drop=True)
            paths = np.empty((self.n_samples, h))
            for s in range(self.n_samples):
                pred = predictor.predict(
                    df=x,
                    x_timestamp=stamps,
                    y_timestamp=future,
                    pred_len=h,
                    T=self.temperature,
                    top_p=self.top_p,
                    sample_count=1,
                    verbose=False,
                )
                paths[s] = pred["close"].to_numpy(dtype=float)[:h]
            out.append(Forecast.from_samples(float(x["close"].iloc[-1]), paths, lv))
        return out


class KronosSmallForecaster(_Kronos):
    name: ClassVar[str] = "kronos_small"
    description: ClassVar[str] = "Kronos-small (MIT), reads OHLCV candles, context 512."
    release_date: ClassVar[date | None] = date(2025, 6, 30)
    default_model_id: ClassVar[str] = "NeoQuasar/Kronos-small"
    tokenizer_id: ClassVar[str] = "NeoQuasar/Kronos-Tokenizer-base"
    default_max_context: ClassVar[int] = 512


class KronosMiniForecaster(_Kronos):
    name: ClassVar[str] = "kronos_mini"
    description: ClassVar[str] = "Kronos-mini (MIT), reads OHLCV candles, context 2048."
    release_date: ClassVar[date | None] = date(2025, 7, 1)
    default_model_id: ClassVar[str] = "NeoQuasar/Kronos-mini"
    tokenizer_id: ClassVar[str] = "NeoQuasar/Kronos-Tokenizer-2k"
    default_max_context: ClassVar[int] = 2048
