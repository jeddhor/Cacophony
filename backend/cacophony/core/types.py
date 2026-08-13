"""Primitive data types (design document section 7).

Types provide *validation* without necessarily determining *generation
behaviour*. A field declared ``type: string`` may be produced by a sequence, a
Faker provider, a regex pattern or a language model; the type only constrains
what counts as an acceptable value once produced.

Each type therefore exposes two operations:

``coerce``
    Best-effort conversion of a produced value into the type's canonical
    Python representation. Generators return native values wherever possible,
    but scripts, lookups and language models return whatever they like.

``check``
    A predicate used by the structural validator. Returns ``None`` when the
    value is acceptable, or a human-readable reason when it is not.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import re
import uuid as _uuid
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

__all__ = ["DataType", "TypeCheckResult", "check_value", "coerce_value", "python_type_name"]


class DataType(StrEnum):
    """The primitive types Cacophony understands out of the box."""

    STRING = "string"
    TEXT = "text"
    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    UUID = "uuid"
    DATE = "date"
    TIME = "time"
    DATETIME = "datetime"
    DURATION = "duration"
    ENUM = "enum"
    ARRAY = "array"
    OBJECT = "object"
    BINARY = "binary"
    IMAGE = "image"
    AUDIO = "audio"
    FILE = "file"
    URI = "uri"
    IP_ADDRESS = "ip_address"
    CIDR = "cidr"
    MAC_ADDRESS = "mac_address"
    HOSTNAME = "hostname"
    EMAIL = "email"
    PHONE = "phone"
    GEO_POINT = "geo_point"
    JSON = "json"
    CUSTOM = "custom"

    @property
    def is_numeric(self) -> bool:
        return self in {DataType.INTEGER, DataType.FLOAT, DataType.DECIMAL}

    @property
    def is_textual(self) -> bool:
        return self in {
            DataType.STRING,
            DataType.TEXT,
            DataType.URI,
            DataType.EMAIL,
            DataType.HOSTNAME,
            DataType.PHONE,
            DataType.MAC_ADDRESS,
            DataType.CIDR,
            DataType.IP_ADDRESS,
            DataType.ENUM,
        }

    @property
    def is_temporal(self) -> bool:
        return self in {
            DataType.DATE,
            DataType.TIME,
            DataType.DATETIME,
            DataType.DURATION,
        }

    @property
    def is_media(self) -> bool:
        """Media types are materialised as assets on disk, not inline values."""
        return self in {DataType.IMAGE, DataType.AUDIO, DataType.FILE, DataType.BINARY}


TypeCheckResult = str | None
"""``None`` when a value is acceptable, otherwise the reason it is not."""


# --------------------------------------------------------------------------- #
# Format helpers
# --------------------------------------------------------------------------- #

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_DURATION_RE = re.compile(
    r"^P(?!$)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+(\.\d+)?S)?)?$"
)


def python_type_name(value: Any) -> str:
    return type(value).__name__


# --------------------------------------------------------------------------- #
# Coercion
# --------------------------------------------------------------------------- #


def coerce_value(value: Any, data_type: DataType) -> Any:
    """Convert ``value`` into the canonical representation for ``data_type``.

    Coercion is deliberately forgiving: it exists so that values arriving from
    loosely typed sources (lookup tables, scripts, language models) land in the
    same shape as values produced by native generators. When conversion is
    impossible the original value is returned unchanged and the structural
    validator reports the mismatch.
    """
    if value is None:
        return None

    try:
        return _COERCERS[data_type](value)
    except (TypeError, ValueError, InvalidOperation, OverflowError):
        return value


def _coerce_str(value: Any) -> Any:
    return value if isinstance(value, str) else str(value)


def _coerce_int(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError("not an integer")
    return int(str(value).strip())


def _coerce_float(value: Any) -> Any:
    if isinstance(value, bool):
        return float(value)
    return float(value) if not isinstance(value, float) else value


def _coerce_decimal(value: Any) -> Any:
    return value if isinstance(value, Decimal) else Decimal(str(value))


_TRUE = {"true", "t", "yes", "y", "1", "on"}
_FALSE = {"false", "f", "no", "n", "0", "off"}


def _coerce_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError("not a boolean")


def _coerce_uuid(value: Any) -> Any:
    return value if isinstance(value, _uuid.UUID) else _uuid.UUID(str(value))


def _coerce_date(value: Any) -> Any:
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    return _dt.date.fromisoformat(str(value))


def _coerce_time(value: Any) -> Any:
    if isinstance(value, _dt.datetime):
        return value.time()
    if isinstance(value, _dt.time):
        return value
    return _dt.time.fromisoformat(str(value))


def _coerce_datetime(value: Any) -> Any:
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time.min)
    text = str(value).strip()
    # Accept a trailing 'Z' - very common in log-shaped synthetic data.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(text)


def _coerce_duration(value: Any) -> Any:
    if isinstance(value, _dt.timedelta):
        return value
    if isinstance(value, (int, float)):
        return _dt.timedelta(seconds=float(value))
    return str(value)


def _coerce_array(value: Any) -> Any:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError("not an array")
        return parsed
    raise ValueError("not an array")


def _coerce_object(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("not an object")
        return parsed
    raise ValueError("not an object")


def _coerce_json(value: Any) -> Any:
    if isinstance(value, (dict, list, int, float, bool)) or value is None:
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _coerce_binary(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    raise ValueError("not binary")


def _coerce_ip(value: Any) -> Any:
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return str(value)
    return str(ipaddress.ip_address(str(value).strip()))


def _coerce_cidr(value: Any) -> Any:
    if isinstance(value, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
        return str(value)
    return str(ipaddress.ip_network(str(value).strip(), strict=False))


def _coerce_geo(value: Any) -> Any:
    """Canonical geographic point representation: ``{"lat": float, "lon": float}``."""
    if isinstance(value, dict):
        lat = value.get("lat", value.get("latitude"))
        lon = value.get("lon", value.get("lng", value.get("longitude")))
        if lat is None or lon is None:
            raise ValueError("missing lat/lon")
        return {"lat": float(lat), "lon": float(lon)}
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return {"lat": float(value[0]), "lon": float(value[1])}
    if isinstance(value, str) and "," in value:
        lat_text, lon_text = value.split(",", 1)
        return {"lat": float(lat_text), "lon": float(lon_text)}
    raise ValueError("not a geographic point")


def _identity(value: Any) -> Any:
    return value


_COERCERS: dict[DataType, Any] = {
    DataType.STRING: _coerce_str,
    DataType.TEXT: _coerce_str,
    DataType.INTEGER: _coerce_int,
    DataType.FLOAT: _coerce_float,
    DataType.DECIMAL: _coerce_decimal,
    DataType.BOOLEAN: _coerce_bool,
    DataType.UUID: _coerce_uuid,
    DataType.DATE: _coerce_date,
    DataType.TIME: _coerce_time,
    DataType.DATETIME: _coerce_datetime,
    DataType.DURATION: _coerce_duration,
    DataType.ENUM: _coerce_str,
    DataType.ARRAY: _coerce_array,
    DataType.OBJECT: _coerce_object,
    DataType.BINARY: _coerce_binary,
    DataType.IMAGE: _coerce_str,
    DataType.AUDIO: _coerce_str,
    DataType.FILE: _coerce_str,
    DataType.URI: _coerce_str,
    DataType.IP_ADDRESS: _coerce_ip,
    DataType.CIDR: _coerce_cidr,
    DataType.MAC_ADDRESS: _coerce_str,
    DataType.HOSTNAME: _coerce_str,
    DataType.EMAIL: _coerce_str,
    DataType.PHONE: _coerce_str,
    DataType.GEO_POINT: _coerce_geo,
    DataType.JSON: _coerce_json,
    DataType.CUSTOM: _identity,
}


# --------------------------------------------------------------------------- #
# Checking
# --------------------------------------------------------------------------- #


def check_value(value: Any, data_type: DataType) -> TypeCheckResult:
    """Return ``None`` if ``value`` is a valid ``data_type``, else the reason."""
    if value is None:
        return None  # nullability is a field-level concern, not a type concern
    checker = _CHECKERS.get(data_type)
    if checker is None:
        return None
    return checker(value)


def _expect(value: Any, expected: type | tuple[type, ...], label: str) -> TypeCheckResult:
    if isinstance(value, expected):
        return None
    return f"expected {label}, got {python_type_name(value)}"


def _check_str(value: Any) -> TypeCheckResult:
    return _expect(value, str, "a string")


def _check_int(value: Any) -> TypeCheckResult:
    if isinstance(value, bool):
        return "expected an integer, got bool"
    return _expect(value, int, "an integer")


def _check_float(value: Any) -> TypeCheckResult:
    if isinstance(value, bool):
        return "expected a float, got bool"
    return _expect(value, (int, float), "a number")


def _check_decimal(value: Any) -> TypeCheckResult:
    return _expect(value, (Decimal, int, float), "a decimal")


def _check_bool(value: Any) -> TypeCheckResult:
    return _expect(value, bool, "a boolean")


def _check_uuid(value: Any) -> TypeCheckResult:
    if isinstance(value, _uuid.UUID):
        return None
    if isinstance(value, str):
        try:
            _uuid.UUID(value)
        except ValueError:
            return "not a valid UUID"
        return None
    return f"expected a UUID, got {python_type_name(value)}"


def _check_date(value: Any) -> TypeCheckResult:
    if isinstance(value, _dt.datetime):
        return "expected a date, got datetime"
    return _expect(value, _dt.date, "a date")


def _check_time(value: Any) -> TypeCheckResult:
    return _expect(value, _dt.time, "a time")


def _check_datetime(value: Any) -> TypeCheckResult:
    return _expect(value, _dt.datetime, "a datetime")


def _check_duration(value: Any) -> TypeCheckResult:
    if isinstance(value, _dt.timedelta):
        return None
    if isinstance(value, str) and _DURATION_RE.match(value):
        return None
    return "expected a timedelta or ISO-8601 duration string"


def _check_array(value: Any) -> TypeCheckResult:
    return _expect(value, list, "an array")


def _check_object(value: Any) -> TypeCheckResult:
    return _expect(value, dict, "an object")


def _check_binary(value: Any) -> TypeCheckResult:
    return _expect(value, (bytes, bytearray, memoryview), "binary data")


def _check_uri(value: Any) -> TypeCheckResult:
    if not isinstance(value, str):
        return f"expected a URI string, got {python_type_name(value)}"
    return None if _URI_RE.match(value) else "not a valid URI (missing scheme)"


def _check_ip(value: Any) -> TypeCheckResult:
    if not isinstance(value, str):
        return f"expected an IP address string, got {python_type_name(value)}"
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return "not a valid IP address"
    return None


def _check_cidr(value: Any) -> TypeCheckResult:
    if not isinstance(value, str):
        return f"expected a CIDR string, got {python_type_name(value)}"
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        return "not a valid CIDR block"
    return None


def _check_mac(value: Any) -> TypeCheckResult:
    if not isinstance(value, str):
        return f"expected a MAC address string, got {python_type_name(value)}"
    return None if _MAC_RE.match(value) else "not a valid MAC address"


def _check_hostname(value: Any) -> TypeCheckResult:
    if not isinstance(value, str):
        return f"expected a hostname string, got {python_type_name(value)}"
    return None if _HOSTNAME_RE.match(value) else "not a valid hostname"


def _check_email(value: Any) -> TypeCheckResult:
    if not isinstance(value, str):
        return f"expected an email string, got {python_type_name(value)}"
    return None if _EMAIL_RE.match(value) else "not a valid email address"


def _check_geo(value: Any) -> TypeCheckResult:
    if not isinstance(value, dict) or "lat" not in value or "lon" not in value:
        return "expected an object with 'lat' and 'lon'"
    try:
        lat, lon = float(value["lat"]), float(value["lon"])
    except (TypeError, ValueError):
        return "'lat' and 'lon' must be numbers"
    if not -90.0 <= lat <= 90.0:
        return f"latitude {lat} is outside [-90, 90]"
    if not -180.0 <= lon <= 180.0:
        return f"longitude {lon} is outside [-180, 180]"
    return None


def _check_json(value: Any) -> TypeCheckResult:
    try:
        json.dumps(value, default=str)
    except (TypeError, ValueError):
        return "value is not JSON-serialisable"
    return None


_CHECKERS: dict[DataType, Any] = {
    DataType.STRING: _check_str,
    DataType.TEXT: _check_str,
    DataType.INTEGER: _check_int,
    DataType.FLOAT: _check_float,
    DataType.DECIMAL: _check_decimal,
    DataType.BOOLEAN: _check_bool,
    DataType.UUID: _check_uuid,
    DataType.DATE: _check_date,
    DataType.TIME: _check_time,
    DataType.DATETIME: _check_datetime,
    DataType.DURATION: _check_duration,
    DataType.ENUM: _check_str,
    DataType.ARRAY: _check_array,
    DataType.OBJECT: _check_object,
    DataType.BINARY: _check_binary,
    DataType.IMAGE: _check_str,
    DataType.AUDIO: _check_str,
    DataType.FILE: _check_str,
    DataType.URI: _check_uri,
    DataType.IP_ADDRESS: _check_ip,
    DataType.CIDR: _check_cidr,
    DataType.MAC_ADDRESS: _check_mac,
    DataType.HOSTNAME: _check_hostname,
    DataType.EMAIL: _check_email,
    DataType.PHONE: _check_str,
    DataType.GEO_POINT: _check_geo,
    DataType.JSON: _check_json,
}
