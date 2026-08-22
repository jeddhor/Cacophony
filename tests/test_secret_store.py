"""The encrypted secret store (design document section 63).

Section 63 offers three places a credential may live and this is the third. The
tests that matter are the ones about what it does *not* do: it does not write a
credential where anything can read it, it does not open without the passphrase,
and it does not have a default passphrase - which would make the whole thing
theatre.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from cacophony.providers.secret_store import (
    PASSPHRASE_ENV,
    EncryptedSecretStore,
    SecretStoreError,
    default_store_path,
)
from cacophony.providers.secrets import SecretResolver

PASSPHRASE = "a passphrase nobody would guess"


@pytest.fixture
def store(tmp_path: Path) -> EncryptedSecretStore:
    return EncryptedSecretStore(tmp_path / "secrets.enc", passphrase=PASSPHRASE)


class TestTheStore:
    def test_a_stored_secret_comes_back(self, store: EncryptedSecretStore) -> None:
        store.put("openai_api_key", "sk-not-real")
        assert store.get("openai_api_key") == "sk-not-real"

    def test_it_survives_being_closed_and_reopened(self, store: EncryptedSecretStore) -> None:
        store.put("openai_api_key", "sk-not-real")
        reopened = EncryptedSecretStore(store.path, passphrase=PASSPHRASE)
        assert reopened.get("openai_api_key") == "sk-not-real"

    def test_the_file_does_not_contain_the_secret(self, store: EncryptedSecretStore) -> None:
        """The entire point, stated as a test."""
        store.put("openai_api_key", "sk-not-real")
        assert "sk-not-real" not in store.path.read_text(encoding="utf-8")

    def test_the_file_is_not_world_readable(self, store: EncryptedSecretStore) -> None:
        store.put("openai_api_key", "sk-not-real")
        assert store.path.stat().st_mode & 0o077 == 0

    def test_the_wrong_passphrase_does_not_open_it(self, store: EncryptedSecretStore) -> None:
        store.put("openai_api_key", "sk-not-real")
        wrong = EncryptedSecretStore(store.path, passphrase="not that one")
        with pytest.raises(SecretStoreError, match="passphrase"):
            wrong.get("openai_api_key")

    def test_there_is_no_default_passphrase(self, tmp_path: Path, monkeypatch) -> None:
        """A store everybody can open is not a store."""
        monkeypatch.delenv(PASSPHRASE_ENV, raising=False)
        keeper = EncryptedSecretStore(tmp_path / "secrets.enc")
        with pytest.raises(SecretStoreError, match="no default"):
            keeper.put("openai_api_key", "sk-not-real")

    def test_the_passphrase_can_come_from_the_environment(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv(PASSPHRASE_ENV, PASSPHRASE)
        keeper = EncryptedSecretStore(tmp_path / "secrets.enc")
        keeper.put("token", "value")
        assert EncryptedSecretStore(tmp_path / "secrets.enc").get("token") == "value"

    def test_listing_names_ids_and_never_values(self, store: EncryptedSecretStore) -> None:
        store.put("one", "first")
        store.put("two", "second")
        assert store.ids() == ["one", "two"]

    def test_forgetting_removes_it(self, store: EncryptedSecretStore) -> None:
        store.put("one", "first")
        assert store.forget("one") is True
        assert EncryptedSecretStore(store.path, passphrase=PASSPHRASE).ids() == []
        assert store.forget("one") is False

    def test_an_entry_cannot_be_moved_to_another_name(self, store: EncryptedSecretStore) -> None:
        """The id is authenticated, not just the value."""
        import json

        store.put("harmless", "value")
        document = json.loads(store.path.read_text(encoding="utf-8"))
        document["entries"]["admin_token"] = document["entries"].pop("harmless")
        store.path.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(SecretStoreError):
            EncryptedSecretStore(store.path, passphrase=PASSPHRASE).get("admin_token")

    def test_the_default_path_follows_xdg(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert default_store_path() == tmp_path / "cacophony" / "secrets.enc"


class TestResolutionOrder:
    """Section 63's chain, with the store in it."""

    def test_the_store_resolves_a_secret(self, store: EncryptedSecretStore) -> None:
        store.put("api_key", "from-the-store")
        resolver = SecretResolver(
            use_keyring=False, store_path=store.path, environ={}, passphrase=PASSPHRASE
        )
        assert resolver.resolve("api_key") == "from-the-store"

    def test_the_environment_still_wins(self, store: EncryptedSecretStore) -> None:
        """A one-off run overrides the file without editing it."""
        store.put("api_key", "from-the-store")
        resolver = SecretResolver(
            use_keyring=False,
            store_path=store.path,
            environ={"CACOPHONY_SECRET_API_KEY": "from-the-environment"},
            passphrase=PASSPHRASE,
        )
        assert resolver.resolve("api_key") == "from-the-environment"

    def test_a_store_that_cannot_be_opened_is_not_an_error(
        self, store: EncryptedSecretStore
    ) -> None:
        """Resolution is best-effort: the credential may be somewhere else."""
        store.put("api_key", "from-the-store")
        resolver = SecretResolver(
            use_keyring=False, store_path=store.path, environ={}, passphrase="wrong"
        )
        assert resolver.resolve("api_key") is None

    def test_no_store_at_all_is_not_an_error(self, tmp_path: Path) -> None:
        resolver = SecretResolver(
            use_keyring=False, store_path=tmp_path / "nothing.enc", environ={}
        )
        assert resolver.resolve("api_key") is None

    def test_the_advice_names_all_three_places(self, tmp_path: Path) -> None:
        resolver = SecretResolver(
            use_keyring=False, store_path=tmp_path / "nothing.enc", environ={}
        )
        with pytest.raises(LookupError, match="cacophony secrets set"):
            resolver.require("api_key")
