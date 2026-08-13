"""Secret handling (design document section 63).

Provider credentials must never appear inside project files by default. A
project file is a thing people commit, diff, attach to tickets and paste into
chat; a credential in one is a credential published.

So configurations reference a *logical secret id*, and this module resolves it
at run time from, in order:

1. an explicit override supplied by the caller,
2. the environment variable ``CACOPHONY_SECRET_<ID>``,
3. the environment variable named by the id itself, upper-cased,
4. the OS keychain, if ``keyring`` is installed.

Resolution failures are not fatal here. Plenty of local providers - Ollama and
llama.cpp among them - need no credential at all, so a missing secret is only
an error at the point something actually tries to authenticate with it.
"""

from __future__ import annotations

import os
import re
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
        environ: dict[str, str] | None = None,
    ) -> None:
        self.overrides = dict(overrides or {})
        self.use_keyring = use_keyring
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
                f"variable {secret_env_var(secret_id)}, or store it in the OS keychain "
                f"under service '{KEYRING_SERVICE}'. Never put the credential in the "
                f"project file (design document section 63)."
            )
        return value

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
