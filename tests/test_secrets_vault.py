"""Round-trip correctness for the encrypted secrets vault
(`sidepage.core.secrets_vault`, v4 §9).

The vault has no in-memory cache — `_load_store` always re-reads and
decrypts `vault_data_file()` from disk (see that module's docstring) — so
calling `get_secret` after `set_secret` in the same test process already
exercises a genuine encrypt-write / read-decrypt round trip, not just a
dict lookup. These tests exist because nothing previously verified that
what comes back out of the vault is byte-for-byte what went in, or that
the on-disk file is actually encrypted rather than merely obfuscated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidepage.config.settings import vault_data_file, vault_key_file
from sidepage.core import secrets_vault
from sidepage.core.exceptions import SecretNotFoundError


@pytest.fixture
def sidepage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path))
    return tmp_path


def test_set_then_get_round_trips_exact_value(sidepage_home: Path) -> None:
    secrets_vault.set_secret("api-key", "sk-super-secret-12345")
    assert secrets_vault.get_secret("api-key") == "sk-super-secret-12345"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a" * 5000,
        "unicode: üñîçødé \U0001f600",
        "newlines\nand\ttabs",
        '{"looks": "like json"}',
    ],
)
def test_round_trip_preserves_edge_case_values(sidepage_home: Path, value: str) -> None:
    secrets_vault.set_secret("edge-case", value)
    assert secrets_vault.get_secret("edge-case") == value


def test_data_file_does_not_contain_plaintext(sidepage_home: Path) -> None:
    """Proves the vault is actually encrypted, not just JSON/base64 with
    no real confidentiality — the plaintext secret must not appear
    anywhere in the on-disk ciphertext."""
    secret_value = "sk-this-exact-string-must-not-leak-onto-disk"
    secrets_vault.set_secret("leak-check", secret_value)

    on_disk = vault_data_file().read_bytes()
    assert secret_value.encode() not in on_disk
    assert b"leak-check" not in on_disk


def test_multiple_secrets_round_trip_independently(sidepage_home: Path) -> None:
    pairs = {"one": "value-one", "two": "value-two", "three": "value-three"}
    for name, value in pairs.items():
        secrets_vault.set_secret(name, value)

    for name, value in pairs.items():
        assert secrets_vault.get_secret(name) == value
    assert secrets_vault.list_secrets() == sorted(pairs)


def test_overwriting_a_secret_round_trips_the_new_value(sidepage_home: Path) -> None:
    secrets_vault.set_secret("rotating", "old-value")
    secrets_vault.set_secret("rotating", "new-value")
    assert secrets_vault.get_secret("rotating") == "new-value"


def test_remove_then_get_raises_not_found(sidepage_home: Path) -> None:
    secrets_vault.set_secret("temp", "value")
    secrets_vault.remove_secret("temp")
    with pytest.raises(SecretNotFoundError):
        secrets_vault.get_secret("temp")


def test_get_missing_secret_raises_not_found(sidepage_home: Path) -> None:
    with pytest.raises(SecretNotFoundError):
        secrets_vault.get_secret("never-set")


def test_key_file_is_reused_across_calls_not_regenerated(sidepage_home: Path) -> None:
    """A regenerated key on every write would make every previously
    stored secret undecryptable — this is what would actually happen if
    `_load_key` didn't check for an existing key file first."""
    secrets_vault.set_secret("first", "value-1")
    key_after_first = vault_key_file().read_bytes()

    secrets_vault.set_secret("second", "value-2")
    key_after_second = vault_key_file().read_bytes()

    assert key_after_first == key_after_second
    # Both secrets must still be independently decryptable with that one key.
    assert secrets_vault.get_secret("first") == "value-1"
    assert secrets_vault.get_secret("second") == "value-2"
