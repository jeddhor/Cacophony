"""The generator recommendation engine (design document section 68).

    first_name       -> Faker recommended
    age              -> Statistical generator recommended
    employee_number  -> Sequence recommended
    biography        -> LLM recommended
    portrait         -> Image generator required

"This is essential for scale." A schema that quietly routed every unannotated
field to a language model would turn a ten-million-row run into a week of GPU
time for values Faker produces in microseconds.

The engine reads three signals, in decreasing order of authority:

1. the field's declared **type** - ``uuid``, ``image`` and ``date`` decide themselves;
2. the field's **name** - ``first_name``, ``created_at``, ``ip_address`` are conventions;
3. the field's **semantic description** - free prose implies a language model.

Every recommendation carries its reasoning so the UI can show why a generator
was chosen, and so the plan stays inspectable (section 4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.types import DataType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..schema.models import EntitySpec, FieldSpec, ProjectSpec

__all__ = ["Recommendation", "recommend_generator"]


@dataclass(slots=True)
class Recommendation:
    """A suggested generator, its options, and why it was suggested."""

    generator: str
    options: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    confidence: float = 0.5

    def describe(self) -> str:
        return f"{self.generator} ({self.reason}, confidence {self.confidence:.0%})"


# --------------------------------------------------------------------------- #
# Name conventions
# --------------------------------------------------------------------------- #

#: Field-name substring -> Faker provider. Ordered longest-first at match time
#: so ``first_name`` beats ``name``.
_FAKER_BY_NAME: dict[str, str] = {
    "first_name": "first_name",
    "given_name": "first_name",
    "last_name": "last_name",
    "family_name": "last_name",
    "surname": "last_name",
    "middle_name": "first_name",
    "full_name": "name",
    "display_name": "name",
    "company_name": "company",
    "company": "company",
    "organization": "company",
    "employer": "company",
    "job_title": "job",
    "occupation": "job",
    "street_address": "street_address",
    "address": "address",
    "city": "city",
    "state": "state",
    "province": "state",
    "country": "country",
    "postal_code": "postcode",
    "postcode": "postcode",
    "zip_code": "postcode",
    "zipcode": "postcode",
    "latitude": "latitude",
    "longitude": "longitude",
    "timezone": "timezone",
    "user_agent": "user_agent",
    "color": "color_name",
    "currency": "currency_code",
    "language": "language_name",
    "license_plate": "license_plate",
    "iban": "iban",
    "isbn": "isbn13",
    "credit_card": "credit_card_number",
    "username": "user_name",
    "user_name": "user_name",
    "login": "user_name",
    "password": "password",
    "email": "email",
    "url": "url",
    "website": "url",
    "domain": "domain_name",
    "hostname": "hostname",
    "slug": "slug",
    "sentence": "sentence",
    "paragraph": "paragraph",
    "word": "word",
}

#: Names that imply a monotonically increasing identifier.
_SEQUENCE_NAMES = re.compile(
    r"(^|_)(id|number|no|num|seq|sequence|index|ordinal|row)$|^(employee|customer|order|ticket|"
    r"invoice|record|account)_(id|number|no|num)$"
)

#: Names that imply an enumerated set, with sensible defaults for each.
_CATEGORICAL_DEFAULTS: dict[str, list[Any]] = {
    "status": ["active", "inactive", "pending", "suspended"],
    "state": ["open", "in_progress", "resolved", "closed"],
    "severity": ["critical", "high", "medium", "low", "informational"],
    "priority": ["p1", "p2", "p3", "p4"],
    "sentiment": ["positive", "neutral", "negative"],
    "result": ["success", "failure"],
    "outcome": ["success", "failure"],
    "gender": ["female", "male", "non_binary", "undisclosed"],
    "department": [
        "Engineering",
        "Sales",
        "Marketing",
        "Finance",
        "Human Resources",
        "Operations",
        "Legal",
        "Support",
        "Security",
    ],
    "os": ["Windows", "macOS", "Linux", "Other"],
    "operating_system": ["Windows", "macOS", "Linux", "Other"],
    "browser": ["Chrome", "Safari", "Firefox", "Edge", "Other"],
    "protocol": ["tcp", "udp", "icmp"],
    "environment": ["production", "staging", "development"],
    "tier": ["free", "standard", "premium", "enterprise"],
}

#: Names that imply prose long enough to be worth a language model.
_PROSE_NAMES = re.compile(
    r"(bio|biography|description|summary|notes?|comment|body|content|message|narrative|"
    r"justification|rationale|resolution|analysis|review|feedback|abstract|overview|story)"
)

#: Names implying a timestamp.
_TEMPORAL_NAMES = re.compile(
    r"(_at|_on|_date|_time|date|time|timestamp|birthday|dob)$|^(date|time)"
)

#: Age-shaped fields get a distribution rather than a uniform draw.
_AGE_NAMES = re.compile(r"(^|_)age$")

#: Telephone- and government-identifier-shaped names, which must never be
#: routed to Faker - see design document section 62.
_PHONE_NAMES = re.compile(r"(phone|telephone|mobile|cell|fax)")
_SSN_NAMES = re.compile(r"(ssn|social_security|national_id|tax_id|nino)")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def recommend_generator(
    field_spec: FieldSpec,
    *,
    entity: EntitySpec | None = None,
    project: ProjectSpec | None = None,
) -> Recommendation:
    """Recommend a generator for a field that did not name one."""
    name = (field_spec.name or "").lower()
    data_type = field_spec.type
    meaning = field_spec.meaning

    for rule in (
        _by_constraints,
        _by_decisive_type,
        _by_name_convention,
        _by_meaning,
        _by_fallback_type,
    ):
        recommendation = rule(field_spec, name, data_type, meaning)
        if recommendation is not None:
            if project is not None and field_spec.locale is None:
                recommendation.options.setdefault("locale", project.project.locale)
            _drop_irrelevant_locale(recommendation)
            return recommendation

    return Recommendation(
        generator="constant",
        options={"value": None},
        reason="no signal available",
        confidence=0.1,
    )


# --------------------------------------------------------------------------- #
# Rules, in priority order
# --------------------------------------------------------------------------- #


def _by_constraints(
    field_spec: FieldSpec, name: str, data_type: DataType, meaning: str | None
) -> Recommendation | None:
    """An explicit enum answers the question outright."""
    if field_spec.constraints.enum:
        return Recommendation(
            generator="weighted",
            options={"choices": list(field_spec.constraints.enum)},
            reason="the field declares an enum constraint",
            confidence=0.99,
        )
    if field_spec.constraints.pattern and data_type.is_textual:
        return None  # a regex constraint is validated, not generated from
    return None


def _by_decisive_type(
    field_spec: FieldSpec, name: str, data_type: DataType, meaning: str | None
) -> Recommendation | None:
    """Types that determine their own generator regardless of name."""
    decisive: dict[DataType, Recommendation] = {
        DataType.UUID: Recommendation("uuid", {}, "the field is a UUID", 0.99),
        DataType.IMAGE: Recommendation("image", {}, "the field is an image", 0.99),
        DataType.AUDIO: Recommendation("tts", {}, "the field is audio", 0.99),
        DataType.IP_ADDRESS: Recommendation("ip", {}, "the field is an IP address", 0.95),
        DataType.MAC_ADDRESS: Recommendation("mac", {}, "the field is a MAC address", 0.95),
        DataType.BOOLEAN: Recommendation("boolean", {}, "the field is boolean", 0.9),
    }
    return decisive.get(data_type)


def _by_name_convention(
    field_spec: FieldSpec, name: str, data_type: DataType, meaning: str | None
) -> Recommendation | None:
    """Conventional field names carry more information than their types."""
    if not name:
        return None

    # Sensitive-identifier names are checked first. "mobile_number" ends in
    # "_number" and would otherwise be read as a sequence, and "fax" would fall
    # through to Faker - either way producing a value that could dial a real
    # telephone, which is exactly what section 62 forbids.
    if _PHONE_NAMES.search(name) and data_type in (DataType.STRING, DataType.PHONE):
        return Recommendation(
            generator="phone",
            options={},
            reason="the name indicates a telephone number; section 62 requires a safe range",
            confidence=0.85,
        )

    if _SSN_NAMES.search(name):
        return Recommendation(
            generator="government_id",
            options={},
            reason="the name indicates a government identifier; section 62 requires a safe range",
            confidence=0.85,
        )

    if _AGE_NAMES.search(name) and data_type in (DataType.INTEGER, DataType.FLOAT):
        return Recommendation(
            generator="distribution",
            options={"distribution": "normal", "mean": 39, "stddev": 11, "min": 18, "max": 68},
            reason="age is better modelled by a distribution than a uniform draw",
            confidence=0.8,
        )

    if _SEQUENCE_NAMES.search(name) and data_type in (
        DataType.INTEGER,
        DataType.STRING,
    ):
        options: dict[str, Any] = {}
        if data_type is DataType.STRING and field_spec.primary_key:
            prefix = (name.split("_")[0][:3] or "rec").upper()
            options["format"] = f"{prefix}-{{000000}}"
        return Recommendation(
            generator="sequence",
            options=options,
            reason="the name indicates a monotonic identifier",
            confidence=0.85,
        )

    # Longest match wins, so "first_name" is not shadowed by "name".
    for keyword in sorted(_FAKER_BY_NAME, key=len, reverse=True):
        if keyword in name:
            return Recommendation(
                generator="faker",
                options={"provider": _FAKER_BY_NAME[keyword]},
                reason=f"the name matches the Faker provider '{_FAKER_BY_NAME[keyword]}'",
                confidence=0.9,
            )

    for keyword, choices in _CATEGORICAL_DEFAULTS.items():
        if name == keyword or name.endswith(f"_{keyword}"):
            return Recommendation(
                generator="weighted",
                options={"choices": choices},
                reason=f"'{keyword}' is a conventional enumerated field",
                confidence=0.6,
            )

    if (data_type.is_temporal or _TEMPORAL_NAMES.search(name)) and (
        data_type.is_temporal or data_type.is_textual
    ):
        return Recommendation(
            generator="datetime",
            options={},
            reason="the name or type indicates a point in time",
            confidence=0.8,
        )

    if _PROSE_NAMES.search(name) and data_type in (DataType.TEXT, DataType.STRING):
        return Recommendation(
            generator="llm",
            options={},
            reason="the name indicates free prose, which a language model writes best",
            confidence=0.7,
        )

    return None


def _by_meaning(
    field_spec: FieldSpec, name: str, data_type: DataType, meaning: str | None
) -> Recommendation | None:
    """A semantic description with no other signal implies a language model.

    Short descriptions of atomic values ("Person's given name") are still
    routed to Faker by the name rules above; this rule catches the genuinely
    open-ended ones, which is exactly what section 9 intends.
    """
    if not meaning:
        return None
    if data_type is DataType.TEXT or len(meaning.split()) >= 6:
        return Recommendation(
            generator="llm",
            options={},
            reason="the field has a semantic description and no deterministic equivalent",
            confidence=0.65,
        )
    return None


def _by_fallback_type(
    field_spec: FieldSpec, name: str, data_type: DataType, meaning: str | None
) -> Recommendation | None:
    """Last resort: pick something reasonable for the declared type."""
    fallbacks: dict[DataType, Recommendation] = {
        DataType.INTEGER: Recommendation("random", {}, "integer field with no other signal", 0.3),
        DataType.FLOAT: Recommendation("random", {}, "float field with no other signal", 0.3),
        DataType.DECIMAL: Recommendation("random", {}, "decimal field with no other signal", 0.3),
        DataType.DATE: Recommendation("datetime", {}, "date field", 0.5),
        DataType.TIME: Recommendation("datetime", {}, "time field", 0.5),
        DataType.DATETIME: Recommendation("datetime", {}, "datetime field", 0.5),
        DataType.EMAIL: Recommendation("faker", {"provider": "email"}, "email field", 0.8),
        DataType.HOSTNAME: Recommendation("faker", {"provider": "hostname"}, "hostname field", 0.8),
        DataType.URI: Recommendation("faker", {"provider": "url"}, "URI field", 0.8),
        DataType.PHONE: Recommendation(
            "phone", {}, "telephone field, drawn from the fictitious 555 block", 0.85
        ),
        DataType.TEXT: Recommendation("llm", {}, "long-text field", 0.5),
        DataType.STRING: Recommendation(
            "faker", {"provider": "word"}, "string field with no other signal", 0.2
        ),
        DataType.ARRAY: Recommendation(
            "constant", {"value": []}, "array field with no element definition", 0.2
        ),
        DataType.OBJECT: Recommendation(
            "constant", {"value": {}}, "object field with no shape definition", 0.2
        ),
        DataType.JSON: Recommendation("constant", {"value": {}}, "JSON field", 0.2),
    }
    return fallbacks.get(data_type)


def _drop_irrelevant_locale(recommendation: Recommendation) -> None:
    """Only Faker understands ``locale``; other generators would reject it."""
    if recommendation.generator != "faker":
        recommendation.options.pop("locale", None)
