"""The transform catalogue (design document section 105).

    Post-generation transformations may include: lowercase, uppercase,
    truncate, hash, format date, encode, mask, normalize, add noise, round,
    compress.

One definition, two callers. The ``transform`` generator applies these to a
value while a record is being built; ``cacophony transform`` applies them to a
file that already exists. Two copies of ``mask`` would drift, and the day they
disagreed would be the day somebody's masked column stopped matching the masked
column beside it.

**Every operation is deterministic**, including ``add_noise``. That is not a
detail: a dataset is a pure function of its schema and seed (section 75), and an
operation that reached for a random number would make a transformed dataset
unreproducible - so the jitter is derived by hashing the value and the
operation's salt. The same value always moves the same way, which is also what
makes a transform re-runnable over a file without changing what it already did.

**Nothing here loses data silently.** An operation that cannot apply to a value
- rounding a name, formatting a date that is not a date - raises rather than
returning the value unchanged or ``None``, because a masking pass that quietly
skipped half a column is worse than one that stopped.
"""

from __future__ import annotations

import base64
import hashlib
import re
import unicodedata
import zlib
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from ..core.errors import CacophonyError

__all__ = ["OPERATIONS", "TransformError", "apply_operations", "describe_operations", "parse_step"]


class TransformError(CacophonyError):
    """An operation that could not be applied to a value."""


#: Distinguishes noise derivation from every other hash in the platform.
_NOISE_SALT = "cacophony.transform.noise"

_SLUG = re.compile(r"[^a-z0-9]+")


def _text(value: Any, operation: str) -> str:
    if value is None:
        raise TransformError(f"{operation} needs a value, and this one is null")
    return str(value)


def _number(value: Any, operation: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise TransformError(f"{operation} needs a number, got {value!r}") from exc


def _moment(value: Any, operation: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time())
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise TransformError(
                f"{operation} needs a date or datetime, and {value!r} is not one"
            ) from exc
    raise TransformError(f"{operation} needs a date or datetime, got {type(value).__name__}")


# --------------------------------------------------------------------------- #
# The operations
# --------------------------------------------------------------------------- #


def _lowercase(value: Any, _arg: str | None) -> Any:
    return _text(value, "lowercase").lower()


def _uppercase(value: Any, _arg: str | None) -> Any:
    return _text(value, "uppercase").upper()


def _title(value: Any, _arg: str | None) -> Any:
    return _text(value, "title").title()


def _strip(value: Any, _arg: str | None) -> Any:
    return _text(value, "strip").strip()


def _truncate(value: Any, arg: str | None) -> Any:
    """Cut to at most ``arg`` characters. Default 50."""
    limit = int(arg or 50)
    if limit < 0:
        raise TransformError("truncate needs a length of zero or more")
    return _text(value, "truncate")[:limit]


def _normalize(value: Any, _arg: str | None) -> Any:
    """Collapse whitespace and normalise Unicode to NFKC."""
    text = unicodedata.normalize("NFKC", _text(value, "normalize"))
    return " ".join(text.split())


def _slug(value: Any, _arg: str | None) -> Any:
    return _SLUG.sub("-", _text(value, "slug").lower()).strip("-")


def _hash(value: Any, arg: str | None) -> Any:
    """A stable digest of the value, ``arg`` hex characters long.

    BLAKE2b, like every other hash in the platform. Not a password hash and not
    claimed to be one: it is here so a column can be replaced by something
    stable and join-compatible, which is what a pseudonymised export needs.
    """
    width = int(arg or 32)
    if not 4 <= width <= 128:
        raise TransformError("hash length must be between 4 and 128 characters")
    digest = hashlib.blake2b(_text(value, "hash").encode("utf-8"), digest_size=64).hexdigest()
    return digest[:width]


def _mask(value: Any, arg: str | None) -> Any:
    """Replace all but the last ``arg`` characters with asterisks. Default 4."""
    keep = int(arg or 4)
    if keep < 0:
        raise TransformError("mask needs a number of trailing characters to keep")
    text = _text(value, "mask")
    if keep == 0:
        return "*" * len(text)
    return "*" * max(len(text) - keep, 0) + text[-keep:]


def _round(value: Any, arg: str | None) -> Any:
    """Round to ``arg`` decimal places, keeping a Decimal a Decimal."""
    places = int(arg or 0)
    if isinstance(value, Decimal):
        return round(value, places)
    result = round(_number(value, "round"), places)
    return int(result) if places <= 0 else result


def _format_date(value: Any, arg: str | None) -> Any:
    """Render a date or datetime with a ``strftime`` pattern.

    ``format_date:%Y-%m`` turns a timestamp into a month, which is the usual
    reason: coarsening a date is a privacy measure (section 61) as much as a
    formatting one.
    """
    pattern = arg or "%Y-%m-%d"
    return _moment(value, "format_date").strftime(pattern)


def _encode(value: Any, arg: str | None) -> Any:
    """Encode as ``base64``, ``hex``, ``url`` or ``json``."""
    kind = (arg or "base64").lower()
    text = _text(value, "encode")
    if kind == "base64":
        return base64.b64encode(text.encode("utf-8")).decode("ascii")
    if kind == "hex":
        return text.encode("utf-8").hex()
    if kind == "url":
        from urllib.parse import quote

        return quote(text, safe="")
    if kind == "json":
        import json

        return json.dumps(value, default=str)
    raise TransformError(f"unknown encoding '{kind}'. Use base64, hex, url or json.")


def _compress(value: Any, arg: str | None) -> Any:
    """Deflate the value and return it base64-encoded.

    Base64 rather than raw bytes, because the result has to survive being
    written to CSV or JSON. A column that became unprintable bytes would break
    every writer except Parquet.
    """
    level = int(arg or 6)
    if not 1 <= level <= 9:
        raise TransformError("compress level must be between 1 and 9")
    raw = _text(value, "compress").encode("utf-8")
    return base64.b64encode(zlib.compress(raw, level)).decode("ascii")


def _decompress(value: Any, _arg: str | None) -> Any:
    """The inverse of ``compress``, so a transformed file can be read back."""
    try:
        return zlib.decompress(base64.b64decode(_text(value, "decompress"))).decode("utf-8")
    except (zlib.error, ValueError) as exc:
        raise TransformError(f"could not decompress {value!r}: {exc}") from exc


def _add_noise(value: Any, arg: str | None) -> Any:
    """Jitter a number by up to ``arg`` per cent, deterministically.

    The offset is derived by hashing the value, not drawn from a generator. A
    transform that reached for a random number would make a transformed dataset
    unreproducible - and would change the file every time it was re-run, which
    is the one thing a pipeline step must not do.

    Statistical noise for differential privacy this is not, and it does not
    claim to be: it is here to blur a figure enough that it is no longer the
    figure, which is what section 61's "no real values" needs of a number.
    """
    percent = float(arg or 5.0)
    if not 0 < percent <= 100:
        raise TransformError("add_noise needs a percentage above 0 and at most 100")

    number = _number(value, "add_noise")
    digest = hashlib.blake2b(f"{_NOISE_SALT}:{value!r}".encode(), digest_size=8).digest()
    # A fraction in [-1, 1), from the digest alone.
    fraction = int.from_bytes(digest, "little") / float(1 << 64) * 2.0 - 1.0
    jittered = number * (1.0 + fraction * percent / 100.0)

    if isinstance(value, Decimal):
        return Decimal(str(round(jittered, 6)))
    if isinstance(value, int) and not isinstance(value, bool):
        return round(jittered)
    return round(jittered, 6)


def _nullify(value: Any, _arg: str | None) -> Any:
    """Drop the value entirely. The bluntest privacy measure there is."""
    return None


#: Section 105's list, plus the inverses and helpers that make it usable.
OPERATIONS: dict[str, Any] = {
    "lowercase": _lowercase,
    "uppercase": _uppercase,
    "title": _title,
    "strip": _strip,
    "truncate": _truncate,
    "normalize": _normalize,
    "slug": _slug,
    "hash": _hash,
    "mask": _mask,
    "round": _round,
    "format_date": _format_date,
    "encode": _encode,
    "compress": _compress,
    "decompress": _decompress,
    "add_noise": _add_noise,
    "nullify": _nullify,
}

#: Spellings people reach for.
ALIASES = {
    "lower": "lowercase",
    "upper": "uppercase",
    "trim": "strip",
    "trunc": "truncate",
    "redact": "mask",
    "date_format": "format_date",
    "noise": "add_noise",
    "null": "nullify",
    "b64": "encode",
}


def parse_step(step: str) -> tuple[str, str | None]:
    """Read ``mask:4`` into its name and argument.

    Raises on an unknown name rather than ignoring it. A pipeline that silently
    skipped ``masc:4`` would produce an unmasked column and report success.
    """
    name, _, argument = str(step).strip().partition(":")
    name = ALIASES.get(name.lower(), name.lower())
    if name not in OPERATIONS:
        known = ", ".join(sorted(OPERATIONS))
        raise TransformError(f"unknown transformation '{step}'. Available: {known}")
    return name, argument or None


def apply_operations(value: Any, steps: list[tuple[str, str | None]]) -> Any:
    """Thread a value through a parsed pipeline.

    A null short-circuits, because every operation but ``nullify`` needs a value
    and a pipeline that raised on every null column would be unusable on real
    data - where nulls are exactly what a chaos preset put there on purpose.
    """
    for name, argument in steps:
        if value is None and name != "nullify":
            return None
        value = OPERATIONS[name](value, argument)
    return value


def describe_operations(steps: list[tuple[str, str | None]]) -> str:
    return " -> ".join(f"{name}:{arg}" if arg else name for name, arg in steps)
