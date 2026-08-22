"""The ``cacophony secrets`` commands (design document section 63).

    cacophony secrets set openai_api_key
    cacophony secrets list
    cacophony secrets forget openai_api_key

Kept out of ``main.py`` for the same reason the bundle commands are: this is
the code that decides where a credential lives, and it should be readable on
its own rather than found between two argument lists.

Nothing here ever prints a secret. `list` prints ids, because the question it
answers is "what does this machine know about", and a command that answered it
by dumping credentials to a terminal would be a command that puts them in a
scrollback buffer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ..core.errors import CacophonyError
from .theme import console, error_console

__all__ = ["register"]

# Module level, not inside `register`: annotations are strings under
# `from __future__ import annotations`, and Typer resolves them by evaluating
# them in the module's globals - where a local would not be.
StoreOpt = typer.Option("--store", help="Path to the encrypted store.")
PassphraseOpt = typer.Option(
    "--passphrase", help="The store's passphrase. Prefer CACOPHONY_SECRETS_PASSPHRASE."
)


def register(app: typer.Typer) -> None:
    """Attach the ``secrets`` command group."""
    secrets = typer.Typer(
        name="secrets",
        help="Keep provider credentials in an encrypted file (section 63).",
        no_args_is_help=True,
    )
    app.add_typer(secrets)

    def _store(path: Path | None, passphrase: str | None) -> object:
        from ..providers.secret_store import EncryptedSecretStore

        return EncryptedSecretStore(path, passphrase=passphrase)

    @secrets.command("set")
    def set_secret(
        secret_id: Annotated[str, typer.Argument(help="The logical secret id.")],
        value: Annotated[
            str | None,
            typer.Argument(help="The credential. Omit to be prompted, which is safer."),
        ] = None,
        store: Annotated[Path | None, StoreOpt] = None,
        passphrase: Annotated[str | None, PassphraseOpt] = None,
    ) -> None:
        """Store one credential.

        Omit the value and it is prompted for without echo: a credential typed
        as an argument is a credential in the shell history.
        """
        secret = value if value is not None else typer.prompt("Credential", hide_input=True)
        try:
            keeper = _store(store, passphrase)
            keeper.put(secret_id, secret)  # type: ignore[attr-defined]
        except CacophonyError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=2) from exc

        console.print(
            f"[cacophony.ok]stored[/] {secret_id}  [cacophony.muted]{keeper.path}[/]"  # type: ignore[attr-defined]
        )

    @secrets.command("list")
    def list_secrets(
        store: Annotated[Path | None, StoreOpt] = None,
        passphrase: Annotated[str | None, PassphraseOpt] = None,
        as_json: Annotated[bool, typer.Option("--json", help="Emit the ids as JSON.")] = False,
    ) -> None:
        """What this store holds. Ids only - never the credentials."""
        import json

        try:
            keeper = _store(store, passphrase)
            ids = keeper.ids()  # type: ignore[attr-defined]
        except CacophonyError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=2) from exc

        if as_json:
            console.print_json(json.dumps({"store": str(keeper.path), "ids": ids}))  # type: ignore[attr-defined]
            return

        if not ids:
            console.print(
                f"[cacophony.muted]no secrets stored in {keeper.path}[/]"  # type: ignore[attr-defined]
            )
            return
        console.print(f"[cacophony.muted]{keeper.path}[/]")  # type: ignore[attr-defined]
        for secret_id in ids:
            console.print(f"  {secret_id}")

    @secrets.command("forget")
    def forget_secret(
        secret_id: Annotated[str, typer.Argument(help="The logical secret id.")],
        store: Annotated[Path | None, StoreOpt] = None,
        passphrase: Annotated[str | None, PassphraseOpt] = None,
    ) -> None:
        """Remove one credential from the store."""
        try:
            keeper = _store(store, passphrase)
            removed = keeper.forget(secret_id)  # type: ignore[attr-defined]
        except CacophonyError as exc:
            error_console.print(f"[cacophony.error]error[/] {exc}")
            raise typer.Exit(code=2) from exc

        if not removed:
            error_console.print(f"[cacophony.warn]warning[/] no secret called '{secret_id}'")
            raise typer.Exit(code=1)
        console.print(f"[cacophony.ok]forgotten[/] {secret_id}")
