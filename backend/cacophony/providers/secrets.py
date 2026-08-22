"""Secret handling (design document section 63).

Provider credentials must never appear inside project files by default. A
project file is a thing people commit, diff, attach to tickets and paste into
chat; a credential in one is a credential published.

So configurations reference a *logical secret id*, and this module resolves it
at run time from, in order:

1. an explicit override supplied by the caller,
2. the environment variable ``CACOPHONY_SECRET_<ID>``,
3. the environment variable named by the id itself, upper-cased,
4. the encrypted store, if one exists and its passphrase is available,
5. the OS keychain, if ``keyring`` is installed.

The environment comes before the file so a one-off run can override what is
stored without editing it, and the keychain comes last because a machine with
both has usually chosen the file deliberately.

Resolution failures are not fatal here. Plenty of local providers - Ollama and
llama.cpp among them - need no credential at all, so a missing secret is only
an error at the point something actually tries to authenticate with it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final

__all__ = ["SecretResolver", "redact", "secret_env_var"]

_ENV_PREFIX: Final = "CACOPHONY_SECRET_"
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")

#: Keychain service name used when ``keyring`` is available.
KEYRING_SERVICE: Final = "cacophony"


def secret_env_var(secret_id: str) -> str:
    """The conventional environment variable for a logical secret id.

    >>> secret_env_var("openai-main")
    'CACOPHONY_SECRET_OPENAI_MAIN'
    """
    return _ENV_PREFIX + _NON_ALNUM.sub("_", secret_id).strip("_").upper()


def redact(value: str | None, *, keep: int = 4) -> str:
    """Render a credential safely for logs and terminal output."""
    if not value:
        return "<unset>"
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


class SecretResolver:
    """Resolves logical secret ids to credential values."""

    def __init__(
        self,
        *,
        overrides: dict[str, str] | None = None,
        use_keyring: bool = True,
        use_store: bool = True,
        store_path: str | Path | None = None,
        passphrase: str | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.overrides = dict(overrides or {})
        self.use_keyring = use_keyring
        #: Consult the encrypted store (section 63). Off for a resolver that
        #: must not prompt or fail on a passphrase - a health check, say.
        self.use_store = use_store
        self.store_path = store_path
        #: Supplied rather than read from the environment. For a caller that
        #: has one already - the CLI, holding what it prompted for.
        self.passphrase = passphrase
        self._environ = environ if environ is not None else os.environ
        self._cache: dict[str, str | None] = {}

    def resolve(self, secret_id: str | None) -> str | None:
        """Return the credential for ``secret_id``, or ``None`` if unset."""
        if not secret_id:
            return None
        if secret_id in self._cache:
            return self._cache[secret_id]

        value = (
            self.overrides.get(secret_id)
            or self._environ.get(secret_env_var(secret_id))
            or self._environ.get(_NON_ALNUM.sub("_", secret_id).strip("_").upper())
            or self._from_store(secret_id)
            or self._from_keyring(secret_id)
        )
        self._cache[secret_id] = value
        return value

    def require(self, secret_id: str) -> str:
        """Resolve a secret or explain, precisely, how to supply it."""
        value = self.resolve(secret_id)
        if value is None:
            raise LookupError(
                f"No credential found for secret id '{secret_id}'. Set the environment "
                f"variable {secret_env_var(secret_id)}, put it in the encrypted store "
                f"with `cacophony secrets set {secret_id}`, or store it in the OS "
                f"keychain under service '{KEYRING_SERVICE}'. Never put the credential "
                f"in the project file (design document section 63)."
            )
        return value

    def _from_store(self, secret_id: str) -> str | None:
        """The encrypted store, if there is one and it can be opened.

        A store that cannot be opened - no passphrase, wrong passphrase, no
        `cryptography` installed - is not an error here. Resolution is
        best-effort by design: the credential may be somewhere else, and the
        run only fails if something actually needs one it cannot find.
        """
        if not self.use_store:
            return None
        try:
            from .secret_store import EncryptedSecretStore

            store = EncryptedSecretStore(self.store_path, passphrase=self.passphrase)
            if not store.exists():
                return None
            return store.get(secret_id)
        except Exception:
            return None

    def _from_keyring(self, secret_id: str) -> str | None:
        if not self.use_keyring:
            return None
        try:
            import keyring
        except ImportError:
            return None
        try:
            return keyring.get_password(KEYRING_SERVICE, secret_id)
        except Exception:
            return None

    def sources(self, secret_id: str) -> list[str]:
        """Where this secret could be supplied from, for diagnostics."""
        return [
            f"environment: {secret_env_var(secret_id)}",
            f"keychain: service '{KEYRING_SERVICE}', account '{secret_id}'",
        ]

    def clear(self) -> None:
        self._cache.clear()


DEFAULT_RESOLVER = SecretResolver()
"""Process-wide resolver, used when no other is supplied."""
