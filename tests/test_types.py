"""Primitive type validation and coercion (design document section 7)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest

from cacophony.core.types import DataType, check_value, coerce_value


@pytest.mark.parametrize(
    ("value", "data_type"),
    [
        ("hello", DataType.STRING),
        (42, DataType.INTEGER),
        (4.2, DataType.FLOAT),
        (Decimal("1.50"), DataType.DECIMAL),
        (True, DataType.BOOLEAN),
        (uuid.uuid4(), DataType.UUID),
        (str(uuid.uuid4()), DataType.UUID),
        (dt.date(2026, 1, 1), DataType.DATE),
        (dt.time(9, 30), DataType.TIME),
        (dt.datetime(2026, 1, 1, 9, 30), DataType.DATETIME),
        (dt.timedelta(hours=3), DataType.DURATION),
        ("PT3H", DataType.DURATION),
        ([1, 2], DataType.ARRAY),
        ({"a": 1}, DataType.OBJECT),
        (b"bytes", DataType.BINARY),
        ("https://example.com/x", DataType.URI),
        ("192.0.2.14", DataType.IP_ADDRESS),
        ("2001:db8::1", DataType.IP_ADDRESS),
        ("192.0.2.0/24", DataType.CIDR),
        ("00:00:5e:00:53:af", DataType.MAC_ADDRESS),
        ("host.example.com", DataType.HOSTNAME),
        ("a.b@example.com", DataType.EMAIL),
        ({"lat": 39.7, "lon": -104.9}, DataType.GEO_POINT),
    ],
)
def test_valid_values_pass(value: Any, data_type: DataType) -> None:
    assert check_value(value, data_type) is None


@pytest.mark.parametrize(
    ("value", "data_type"),
    [
        (42, DataType.STRING),
        ("nope", DataType.INTEGER),
        (True, DataType.INTEGER),  # bool is not an integer here, by design
        (4.5, DataType.BOOLEAN),
        ("not-a-uuid", DataType.UUID),
        (dt.datetime(2026, 1, 1), DataType.DATE),  # a datetime is not a date
        ("2026-01-01", DataType.DATE),
        ({"a": 1}, DataType.ARRAY),
        ([1], DataType.OBJECT),
        ("no-scheme", DataType.URI),
        ("999.1.1.1", DataType.IP_ADDRESS),
        ("zz:00:5e:00:53:af", DataType.MAC_ADDRESS),
        ("not an email", DataType.EMAIL),
        ({"lat": 200, "lon": 0}, DataType.GEO_POINT),
    ],
)
def test_invalid_values_report_a_reason(value: Any, data_type: DataType) -> None:
    reason = check_value(value, data_type)
    assert isinstance(reason, str) and reason


def test_null_is_a_field_concern_not_a_type_concern() -> None:
    for data_type in DataType:
        assert check_value(None, data_type) is None


@pytest.mark.parametrize(
    ("raw", "data_type", "expected"),
    [
        ("42", DataType.INTEGER, 42),
        (42.0, DataType.INTEGER, 42),
        ("4.5", DataType.FLOAT, 4.5),
        ("yes", DataType.BOOLEAN, True),
        ("off", DataType.BOOLEAN, False),
        ("2026-01-01", DataType.DATE, dt.date(2026, 1, 1)),
        (
            "2026-01-01T09:30:00Z",
            DataType.DATETIME,
            dt.datetime(2026, 1, 1, 9, 30, tzinfo=dt.UTC),
        ),
        ("[1, 2]", DataType.ARRAY, [1, 2]),
        ('{"a": 1}', DataType.OBJECT, {"a": 1}),
        ("39.7,-104.9", DataType.GEO_POINT, {"lat": 39.7, "lon": -104.9}),
        ([39.7, -104.9], DataType.GEO_POINT, {"lat": 39.7, "lon": -104.9}),
    ],
)
def test_coercion(raw: Any, data_type: DataType, expected: Any) -> None:
    assert coerce_value(raw, data_type) == expected


def test_coercion_returns_the_original_when_it_cannot_convert() -> None:
    """A failed coercion must not raise; the structural validator reports it."""
    sentinel = object()
    assert coerce_value(sentinel, DataType.INTEGER) is sentinel


def test_coercion_preserves_none() -> None:
    assert coerce_value(None, DataType.INTEGER) is None


def test_datetime_z_suffix_is_accepted() -> None:
    """Log-shaped synthetic data uses trailing 'Z' constantly.

    'Z' means UTC, so the coerced value keeps that offset rather than
    silently becoming a naive local timestamp.
    """
    value = coerce_value("2026-03-04T05:06:07Z", DataType.DATETIME)
    assert isinstance(value, dt.datetime)
    assert value.utcoffset() == dt.timedelta(0)


class TestTypeGroups:
    def test_numeric(self) -> None:
        assert DataType.INTEGER.is_numeric and not DataType.STRING.is_numeric

    def test_temporal(self) -> None:
        assert DataType.DATETIME.is_temporal and not DataType.INTEGER.is_temporal

    def test_media(self) -> None:
        assert DataType.IMAGE.is_media and not DataType.TEXT.is_media

    def test_numeric_and_textual_are_disjoint(self) -> None:
        for data_type in DataType:
            assert not (data_type.is_numeric and data_type.is_textual)
