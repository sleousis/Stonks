"""The streaming source registry (roadmap 21.1)."""

from __future__ import annotations

import pytest

from stonks.config import Settings
from stonks.streaming import registry
from stonks.streaming.base import StreamConfigError, StreamContext, StreamingSource
from stonks.streaming.registry import register_stream_source, stream_source_class


def test_every_source_module_is_discovered():
    assert {"eodhd", "ibkr", "replay"} <= set(registry.stream_source_classes())


def test_unknown_source_is_a_config_error():
    with pytest.raises(StreamConfigError, match="nope"):
        stream_source_class("nope")


@pytest.mark.parametrize("bad", ["", "Upper", "has-dash", "1abc"])
def test_bad_ids_are_refused(bad):
    with pytest.raises(ValueError, match="bad streaming source id"):
        register_stream_source(bad)


def test_an_id_cannot_be_taken_twice(monkeypatch):
    monkeypatch.setattr(registry, "_SOURCES", dict(registry._SOURCES))

    class One(StreamingSource):
        @classmethod
        def from_settings(cls, ctx):
            return cls()

        def stream(self, tickers):
            return iter(())

    class Two(One):
        pass

    register_stream_source("dup_test")(One)
    register_stream_source("dup_test")(One)  # the same class again is fine (re-import)
    with pytest.raises(ValueError, match="twice"):
        register_stream_source("dup_test")(Two)
    assert One.source_id == "dup_test"
    assert One().supports("ANY.US")


def test_build_uses_the_configured_source(tmp_path):
    s = Settings()
    s.streaming.source = "replay"
    s.streaming.replay.path = str(tmp_path)
    src = registry.build_stream_source(StreamContext(settings=s))
    assert src.source_id == "replay"


def test_settings_refuse_keys_in_toml():
    from stonks.streaming.settings import StreamingSettings

    with pytest.raises(ValueError, match="environment"):
        StreamingSettings.model_validate({"api_key": "x"})
    with pytest.raises(ValueError, match="environment"):
        StreamingSettings.model_validate({"eodhd": {"api_token": "x"}})
    assert StreamingSettings().enabled is False
