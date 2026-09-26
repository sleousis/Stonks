"""SecretBox: authenticated envelope encryption for credentials at rest."""

from __future__ import annotations

import base64

import pytest

from stonks.security import (
    KeyRing,
    Sealed,
    SecretBox,
    SecretBoxError,
    SecretKeyMissing,
    generate_key,
)

K1 = generate_key()
K2 = generate_key()


def _box(*pairs: tuple[str, str]) -> SecretBox:
    return SecretBox(KeyRing.parse(",".join(f"{kid}:{key}" for kid, key in pairs)))


def test_round_trip_with_associated_data():
    box = _box(("k1", K1))
    sealed = box.seal(b"api-secret", aad="broker_credentials:con_1")
    assert isinstance(sealed, Sealed)
    assert sealed.key_id == "k1"
    assert b"api-secret" not in sealed.token.encode()
    assert box.open(sealed, aad="broker_credentials:con_1") == b"api-secret"


def test_every_seal_is_randomised():
    box = _box(("k1", K1))
    a = box.seal(b"same", aad="x")
    b = box.seal(b"same", aad="x")
    assert a.token != b.token


def test_wrong_associated_data_fails_closed():
    box = _box(("k1", K1))
    sealed = box.seal(b"secret", aad="broker_credentials:con_1")
    with pytest.raises(SecretBoxError):
        box.open(sealed, aad="broker_credentials:con_2")


def test_tampered_token_fails_closed():
    box = _box(("k1", K1))
    sealed = box.seal(b"secret", aad="a")
    prefix, kid, body = sealed.token.split(":", 2)
    raw = bytearray(base64.urlsafe_b64decode(body))
    raw[-1] ^= 0x01
    forged = Sealed.parse(f"{prefix}:{kid}:{base64.urlsafe_b64encode(bytes(raw)).decode()}")
    with pytest.raises(SecretBoxError):
        box.open(forged, aad="a")


def test_unknown_key_id_is_a_clear_error():
    sealed = _box(("old", K1)).seal(b"x", aad="a")
    with pytest.raises(SecretBoxError, match="old"):
        _box(("new", K2)).open(sealed, aad="a")


def test_rotation_rewraps_under_the_active_key():
    old_box = _box(("k1", K1))
    sealed = old_box.seal(b"secret", aad="a")
    rotated_box = _box(("k2", K2), ("k1", K1))  # first = active
    assert rotated_box.active_key_id == "k2"
    assert rotated_box.needs_rotation(sealed)
    fresh = rotated_box.rotate(sealed, aad="a")
    assert fresh.key_id == "k2"
    assert not rotated_box.needs_rotation(fresh)
    assert rotated_box.open(fresh, aad="a") == b"secret"
    # Once k1 is retired, only the rotated token opens.
    retired = _box(("k2", K2))
    assert retired.open(fresh, aad="a") == b"secret"
    with pytest.raises(SecretBoxError):
        retired.open(sealed, aad="a")


def test_sealed_parse_round_trip_and_rejects_garbage():
    sealed = _box(("k1", K1)).seal(b"x", aad="a")
    assert Sealed.parse(sealed.token) == sealed
    for bad in ("", "nope", "sb0:k1:AAAA", "sb1::AAAA", "sb1:k1:"):
        with pytest.raises(SecretBoxError):
            Sealed.parse(bad)


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "k1",
        "k1:not-base64!!",
        "k1:" + base64.urlsafe_b64encode(b"short").decode(),
        f"k1:{K1},k1:{K2}",
        f"bad id:{K1}",
    ],
)
def test_keyring_rejects_bad_specs_without_echoing_keys(spec):
    with pytest.raises(SecretBoxError) as info:
        KeyRing.parse(spec)
    for key in (K1, K2):
        assert key not in str(info.value)


def test_from_env_reads_keys_variable(monkeypatch):
    monkeypatch.setenv("STONKS_SECRET_KEYS", f"k2:{K2},k1:{K1}")
    box = SecretBox.from_env()
    assert box.active_key_id == "k2"


def test_from_env_reads_key_file(monkeypatch, tmp_path):
    path = tmp_path / "keys"
    path.write_text(f"# comment\nk3:{K1}\n\nk1:{K2}\n")
    monkeypatch.setenv("STONKS_SECRET_KEY_FILE", str(path))
    assert SecretBox.from_env().active_key_id == "k3"


def test_from_env_without_keys_explains_how_to_configure(monkeypatch):
    monkeypatch.delenv("STONKS_SECRET_KEYS", raising=False)
    monkeypatch.delenv("STONKS_SECRET_KEY_FILE", raising=False)
    with pytest.raises(SecretKeyMissing, match="STONKS_SECRET_KEYS"):
        SecretBox.from_env()


def test_reprs_never_show_key_material():
    box = _box(("k1", K1))
    assert K1 not in repr(box)
    assert K1 not in repr(box._keyring)
    assert "k1" in repr(box)


def test_generate_key_is_32_random_bytes():
    raw = base64.urlsafe_b64decode(generate_key())
    assert len(raw) == 32
    assert generate_key() != generate_key()


def test_keygen_prints_a_usable_entry(capsys):
    from stonks.security.__main__ import main

    assert main(["keygen", "--id", "k9"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("k9:")
    assert SecretBox(KeyRing.parse(line)).active_key_id == "k9"
