"""AS-14: ``configured_secrets`` covers every secret the server can hold, so
job errors, logs and API errors are scrubbed of all of them."""

from __future__ import annotations

from pydantic import BaseModel, SecretStr

from stonks.config import ENV_ONLY_SECRETS, Settings, configured_secrets


def _secret_fields(model: BaseModel, prefix: str = "") -> list[str]:
    """Dotted paths of every SecretStr field under ``model``."""
    out: list[str] = []
    for name, field in type(model).model_fields.items():
        value = getattr(model, name)
        path = f"{prefix}{name}"
        if isinstance(value, BaseModel):
            out.extend(_secret_fields(value, path + "."))
        elif field.annotation is not None and "SecretStr" in str(field.annotation):
            out.append(path)
    return out


def test_every_secret_field_in_settings_is_scrubbed():
    settings = Settings()
    paths = _secret_fields(settings)
    assert "brokers.alpaca.api_key" in paths and "sources.eodhd.api_key" in paths
    for i, path in enumerate(paths):
        *parents, leaf = path.split(".")
        target = settings
        for p in parents:
            target = getattr(target, p)
        setattr(target, leaf, SecretStr(f"secret-value-{i}"))
    found = set(configured_secrets(settings, environ={}))
    for i, path in enumerate(paths):
        assert f"secret-value-{i}" in found, path


def test_env_only_secrets_are_scrubbed():
    environ = {name: f"env-{name.lower()}" for name in ENV_ONLY_SECRETS}
    environ["STONKS_SECRET_KEYS"] = "k1:AAAAkeyone,k2:BBBBkeytwo"
    found = set(configured_secrets(Settings(), environ=environ))
    for name in ENV_ONLY_SECRETS:
        assert environ[name] in found, name
    assert {"AAAAkeyone", "BBBBkeytwo"} <= found
    for name in ("STONKS_SMTP_PASSWORD", "STONKS_VAPID_PRIVATE_KEY", "STONKS_METRICS_TOKEN"):
        assert name in ENV_ONLY_SECRETS
    assert "STONKS_SNAPTRADE_CONSUMER_KEY" in ENV_ONLY_SECRETS


def test_the_plain_webhook_url_and_a_raw_vendor_key_are_still_scrubbed():
    settings = Settings()
    settings.notify.webhook.url = "https://hooks.example/T0K3N"
    settings.sources.eodhd.api_key = "raw-assigned-key"  # type: ignore[assignment]
    found = set(configured_secrets(settings, environ={}))
    assert {"https://hooks.example/T0K3N", "raw-assigned-key"} <= found


def test_the_eodhd_key_never_shows_in_a_repr():
    settings = Settings.model_validate({"sources": {"eodhd": {"api_key": "vendor-xyz"}}})
    assert "vendor-xyz" not in repr(settings.sources.eodhd)
