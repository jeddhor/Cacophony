"""An encrypted secret store (design document section 63).

Section 63 offers three places a credential may live: the OS keychain, an
environment variable, or an encrypted store. The first two were built; this is
the third, and it exists for the machines the first two serve badly.

A headless Linux box or a container has no keychain, which leaves environment
variables — and an environment variable is readable in ``/proc``, inherited by
every child process, and printed by anything that dumps the environment on
crash. This is a file instead: one passphrase, many secrets, and nothing
sensitive in the process environment except the passphrase itself.

That last clause is the honest limit of it. A passphrase in the environment of
the process that reads the store is a smaller secret in the same place, not a
different kind of safety. It is worth having because one passphrase can be
rotated, prompted for, or supplied by a launcher, and because the file itself
can be backed up and synchronised without publishing what is in it.

The construction is deliberately ordinary: scrypt from the passphrase to a key,
AES-GCM per entry with a fresh nonce, the salt stored beside them. No custom
cryptography, and no default passphrase — a store with a key everybody knows is
a store that lies about what it is.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets as _secrets
from pathlib import Path
from typing import Any, Final

from ..core.errors import CacophonyError
from ..core.files import atomic_write_text

__all__ = [
    "PASSPHRASE_ENV",
    "EncryptedSecretStore",
    "SecretStoreError",
    "default_store_path",
]

#: Where the passphrase comes from when nothing prompts for one.
PASSPHRASE_ENV: Final = "CACOPHONY_SECRETS_PASSPHRASE"

#: scrypt parameters. Interactive-grade: this runs once per process, and a
#: store that takes a second to open is a store people work around.
_SCRYPT_N: Final = 2**15
_SCRYPT_R: Final = 8
_SCRYPT_P: Final = 1
_KEY_BYTES: Final = 32
_SALT_BYTES: Final = 16
_NONCE_BYTES: Final = 12

_FORMAT: Final = 1


class SecretStoreError(CacophonyError):
    """The store could not be opened, read or written."""


def default_store_path() -> Path:
    """``~/.config/cacophony/secrets.enc``, or ``$XDG_CONFIG_HOME``'s version."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "cacophony" / "secrets.enc"


class EncryptedSecretStore:
    """Secrets in a file, under one passphrase.

    Opened lazily: constructing one costs nothing, and a resolver that consults
    a store nobody has created should not pay for a key derivation to find
    that out.
    """

    def __init__(self, path: str | Path | None = None, *, passphrase: str | None = None) -> None:
        self.path = Path(path) if path is not None else default_store_path()
        self._passphrase = passphrase
        self._entries: dict[str, str] | None = None

    # -- reading ------------------------------------------------------------ #

    def exists(self) -> bool:
        return self.path.is_file()

    def ids(self) -> list[str]:
        """The secret ids this store holds. Never the values."""
        return sorted(self._load())

    def get(self, secret_id: str) -> str | None:
        return self._load().get(secret_id)

    # -- writing ------------------------------------------------------------ #

    def put(self, secret_id: str, value: str) -> None:
        entries = dict(self._load())
        entries[secret_id] = value
        self._save(entries)

    def forget(self, secret_id: str) -> bool:
        entries = dict(self._load())
        if secret_id not in entries:
            return False
        del entries[secret_id]
        self._save(entries)
        return True

    # -- internals ----------------------------------------------------------- #

    def _passphrase_for(self, *, creating: bool = False) -> str:
        if self._passphrase:
            return self._passphrase
        from_env = os.environ.get(PASSPHRASE_ENV)
        if from_env:
            return from_env
        what = "choose" if creating else "supply"
        raise SecretStoreError(
            f"the encrypted secret store needs a passphrase. Set {PASSPHRASE_ENV} to "
            f"{what} one, or pass --passphrase. There is deliberately no default: a "
            "store everybody can open is not a store."
        )

    @staticmethod
    def _key(passphrase: str, salt: bytes) -> bytes:
        """Derive the file key. Slow on purpose - that is the whole point.

        `cryptography`'s scrypt rather than `hashlib`'s: OpenSSL applies a
        32 MiB ceiling to scrypt by default, and these parameters need exactly
        that, so the standard-library version fails on some builds and not
        others depending on how it was compiled.
        """
        try:
            from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise SecretStoreError(
                "the encrypted secret store needs the cryptography package: "
                "pip install 'cacophony[secrets]'"
            ) from exc

        kdf = Scrypt(salt=salt, length=_KEY_BYTES, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
        return kdf.derive(passphrase.encode("utf-8"))

    def _cipher(self, key: bytes) -> Any:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise SecretStoreError(
                "the encrypted secret store needs the cryptography package: "
                "pip install 'cacophony[secrets]'"
            ) from exc
        return AESGCM(key)

    def _load(self) -> dict[str, str]:
        if self._entries is not None:
            return self._entries
        if not self.exists():
            self._entries = {}
            return self._entries

        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SecretStoreError(f"{self.path} is not a readable secret store: {exc}") from exc

        if document.get("format") != _FORMAT:
            raise SecretStoreError(
                f"{self.path} is version {document.get('format')!r}, and this build "
                f"reads version {_FORMAT}."
            )

        salt = base64.b64decode(document["salt"])
        cipher = self._cipher(self._key(self._passphrase_for(), salt))

        entries: dict[str, str] = {}
        for secret_id, payload in (document.get("entries") or {}).items():
            nonce = base64.b64decode(payload["nonce"])
            blob = base64.b64decode(payload["value"])
            try:
                entries[secret_id] = cipher.decrypt(nonce, blob, secret_id.encode("utf-8")).decode(
                    "utf-8"
                )
            except Exception as exc:
                # One message for every way this goes wrong, because they are
                # the same problem: this passphrase does not open this file.
                raise SecretStoreError(
                    f"{self.path} could not be decrypted. Check the passphrase in {PASSPHRASE_ENV}."
                ) from exc

        self._entries = entries
        return entries

    def _save(self, entries: dict[str, str]) -> None:
        salt = _secrets.token_bytes(_SALT_BYTES)
        cipher = self._cipher(self._key(self._passphrase_for(creating=not self.exists()), salt))

        payload: dict[str, Any] = {"format": _FORMAT, "salt": base64.b64encode(salt).decode()}
        stored: dict[str, dict[str, str]] = {}
        for secret_id, value in entries.items():
            nonce = _secrets.token_bytes(_NONCE_BYTES)
            # The id is authenticated as well as the value, so an entry cannot
            # be moved to another name inside the file without detection.
            blob = cipher.encrypt(nonce, value.encode("utf-8"), secret_id.encode("utf-8"))
            stored[secret_id] = {
                "nonce": base64.b64encode(nonce).decode(),
                "value": base64.b64encode(blob).decode(),
            }
        payload["entries"] = stored

        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(payload, indent=2) + "\n")
        with contextlib.suppress(OSError):  # a filesystem without modes
            self.path.chmod(0o600)
        self._entries = dict(entries)
