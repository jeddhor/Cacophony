"""Entry point for ``python -m cacophony`` and for a frozen build.

A console script (``cacophony``) is what a ``pip install`` produces, and it is
what everything else in the documentation uses. This exists for the two cases
that cannot use one:

``python -m cacophony``
    Running from a checkout without installing.

A frozen backend
    The desktop bundle (section 41) freezes an interpreter and the package into
    one executable, and a freezer needs a module to start from rather than an
    entry point generated at install time.
"""

from __future__ import annotations


def main() -> None:
    from .cli.main import run

    run()


if __name__ == "__main__":
    main()
