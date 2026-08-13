"""Core domain layer: types, seeds, records, contexts and the key interfaces.

Nothing in this package may import from ``cacophony.generation``,
``cacophony.providers`` or ``cacophony.outputs``. The dependency arrow points
inwards, which is what keeps the seams described in section 97 real rather than
decorative.
"""

from .context import GenerationContext
from .errors import (
    CacophonyError,
    CircularDependencyError,
    GenerationError,
    GeneratorConfigError,
    GeneratorNotFoundError,
    OutputError,
    ProviderError,
    SchemaError,
    UnknownFieldReferenceError,
)
from .interfaces import (
    Capability,
    GeneratedValue,
    Generator,
    HealthStatus,
    OutputWriter,
    Provider,
    SyncGenerator,
    Validator,
)
from .provenance import FieldProvenance, ProvenanceMode, RecordProvenance
from .record import GeneratedAsset, GeneratedRecord, to_jsonable
from .seeds import SeedChain, derive_seed, numpy_generator, rng_for
from .types import DataType, check_value, coerce_value

__all__ = [
    "CacophonyError",
    "Capability",
    "CircularDependencyError",
    "DataType",
    "FieldProvenance",
    "GeneratedAsset",
    "GeneratedRecord",
    "GeneratedValue",
    "GenerationContext",
    "GenerationError",
    "Generator",
    "GeneratorConfigError",
    "GeneratorNotFoundError",
    "HealthStatus",
    "OutputError",
    "OutputWriter",
    "ProvenanceMode",
    "Provider",
    "ProviderError",
    "RecordProvenance",
    "SchemaError",
    "SeedChain",
    "SyncGenerator",
    "UnknownFieldReferenceError",
    "Validator",
    "check_value",
    "coerce_value",
    "derive_seed",
    "numpy_generator",
    "rng_for",
    "to_jsonable",
]
