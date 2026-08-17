"""Duplicate detection (design document section 59).

    LLMs often repeat themselves.

They do, and the repetition is invisible to every other check in the platform.
A model asked five thousand times for "a short professional biography" will
hand back the same three biographies with the names changed, and each one is a
perfectly valid record: the right type, the right length, no constraint
violated. Nothing but a comparison against the rest of the dataset can see it.

Four techniques, as section 59 lists them:

``exact``
    The value, hashed. Catches a model returning byte-identical text.

``normalized``
    Casefolded, punctuation stripped, whitespace collapsed. Catches "Dr. Amara
    Okonkwo" against "dr amara okonkwo" - the same repetition wearing a hat.

``minhash``
    Jaccard similarity over word shingles, estimated from MinHash signatures
    and found with LSH banding. Catches a paragraph rewritten around one
    clause, which is what a model actually does when asked for variety.

``fuzzy``
    The same LSH candidates, confirmed with a real sequence ratio rather than
    an estimate. Slower and exact; LSH has already narrowed the field to a
    handful, so the cost is bounded.

Embeddings are the fifth technique section 59 names and are not implemented:
they need an embedding provider, and no adapter offers one. Declaring the
method raises rather than silently doing something else.

**Everything here is bounded, and says by how much.**

Exact and normalized detection uses a Bloom filter rather than a set, because a
set of ten million digests is most of a gigabyte and this has to run beside the
generator, not instead of it. A Bloom filter has false positives; the rate is
computed from the filter's own dimensions and reported alongside the result, so
a duplication figure of 0.4% comes with the news that up to 0.1% of it may be
imaginary. It has no false negatives, so a report of *zero* duplicates is exact.

Near-duplicate detection holds a sliding window of recent signatures. That is a
deliberate choice rather than a compromise: model repetition is *local* - the
same three biographies come back within a few hundred calls - and a window of
fifty thousand records catches that on any dataset size, while a uniform sample
of the same size would almost never hold both members of a pair.

**Deliberate duplicates are exempt.** Entropy injection re-emits whole records
on purpose (section 24), and reporting those would be reporting the feature.
The same lesson the validator learned in the worlds phase.
"""

from __future__ import annotations

import hashlib
import re
import struct
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from ..core.errors import SchemaError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from ..core.record import GeneratedRecord
    from ..schema.models import DuplicationSpec
    from ..schema.plan import CompiledEntity

__all__ = [
    "METHODS",
    "BloomFilter",
    "DuplicateDetector",
    "DuplicationReport",
    "MinHashIndex",
    "normalise",
    "shingles",
]

#: The techniques section 59 lists. ``embeddings`` is named there and refused
#: here; see the module docstring.
METHODS = ("exact", "normalized", "minhash", "fuzzy")

_PUNCTUATION = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

#: A large prime below 2^61, for the permutation family. Signature values stay
#: below it, so a signature is a tuple of machine-word integers.
_PRIME = (1 << 61) - 1


def normalise(text: str) -> str:
    """Casefold, strip punctuation, collapse whitespace, normalise Unicode.

    NFKC first, so a full-width comma and an ASCII one agree before the
    punctuation strip removes both. Without it a model that answers in
    typographically fancy text looks like it never repeats.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", folded)).strip()


def shingles(text: str, size: int = 3) -> set[str]:
    """Overlapping word n-grams of the normalised text.

    Words rather than characters. Character shingles find two texts that share
    spelling; word shingles find two texts that share *phrasing*, which is what
    a repetitive model produces. Short texts fall back to their whole selves so
    a three-word answer is still comparable.
    """
    words = normalise(text).split()
    if not words:
        return set()
    if len(words) <= size:
        return {" ".join(words)}
    return {" ".join(words[index : index + size]) for index in range(len(words) - size + 1)}


def _digest64(value: str) -> int:
    return int.from_bytes(hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest(), "little")


class BloomFilter:
    """A bounded set membership test with no false negatives.

    Sized from the number of items expected and the false-positive rate asked
    for, both of which are reported back: a duplication figure computed from a
    probabilistic structure has to say so.

    The hash family is Kirsch-Mitzenmacher - two independent 64-bit hashes
    combined as ``h1 + i * h2`` - so *k* probes cost one digest rather than *k*.
    """

    def __init__(self, capacity: int, error_rate: float = 0.001) -> None:
        self.capacity = max(1, int(capacity))
        self.error_rate = min(0.5, max(1e-9, float(error_rate)))

        # The standard sizing: m = -n ln p / (ln 2)^2, k = m/n ln 2.
        import math

        bits = int(-self.capacity * math.log(self.error_rate) / (math.log(2) ** 2)) + 1
        self.bits = max(64, bits)
        self.probes = max(1, min(16, round(self.bits / self.capacity * math.log(2))))
        self._bytes = bytearray((self.bits + 7) // 8)
        self.added = 0

    @property
    def size_bytes(self) -> int:
        return len(self._bytes)

    @property
    def load(self) -> float:
        """How full the filter is, relative to what it was sized for."""
        return self.added / self.capacity

    @property
    def effective_error_rate(self) -> float:
        """False-positive probability at the current load.

        Reported rather than assumed, because a filter given more items than it
        was sized for degrades - and a caller told "0.1%" while the true figure
        is 8% has been misled by the thing that was supposed to be honest.
        """
        import math

        exponent = -self.probes * self.added / self.bits
        return (1.0 - math.exp(exponent)) ** self.probes

    def _positions(self, value: str) -> Iterable[int]:
        digest = hashlib.blake2b(value.encode("utf-8"), digest_size=16).digest()
        first, second = struct.unpack("<QQ", digest)
        second |= 1  # never zero, or every probe lands in one place
        for index in range(self.probes):
            yield (first + index * second) % self.bits

    def add(self, value: str) -> bool:
        """Add a value. True if it was (probably) already present."""
        seen = True
        for position in self._positions(value):
            byte, bit = divmod(position, 8)
            mask = 1 << bit
            if not self._bytes[byte] & mask:
                seen = False
                self._bytes[byte] |= mask
        self.added += 1
        return seen

    def __contains__(self, value: str) -> bool:
        return all(
            self._bytes[position // 8] & (1 << (position % 8))
            for position in self._positions(value)
        )

    def describe(self) -> dict[str, Any]:
        return {
            "capacity": self.capacity,
            "added": self.added,
            "bits": self.bits,
            "probes": self.probes,
            "size_bytes": self.size_bytes,
            "false_positive_rate": round(self.effective_error_rate, 8),
        }


class MinHashIndex:
    """A sliding window of signatures, indexed for near-duplicate lookup.

    LSH banding: a signature is cut into *b* bands of *r* rows, and two
    signatures are candidates if any band matches exactly. Tuning *b* and *r*
    sets the similarity at which a pair becomes likely to be found - the
    S-curve threshold is approximately ``(1/b) ** (1/r)``, and the bands here
    are chosen to put that near the configured similarity.

    Bounded by construction. Evicting the oldest signature also removes its
    band keys, so the index holds ``window`` entries whatever the dataset size.
    """

    def __init__(
        self,
        *,
        window: int = 50_000,
        signature_size: int = 64,
        similarity: float = 0.7,
        shingle_size: int = 3,
    ) -> None:
        self.window = max(2, int(window))
        self.signature_size = max(8, int(signature_size))
        self.similarity = min(0.999, max(0.05, float(similarity)))
        self.shingle_size = max(1, int(shingle_size))

        self.bands, self.rows = _band_layout(self.signature_size, self.similarity)
        #: Permutation coefficients. Derived rather than random so an index
        #: built twice compares texts the same way (section 75's principle,
        #: applied to a measurement rather than to data).
        self._coefficients = [
            (
                (_digest64(f"a{index}") % (_PRIME - 1)) + 1,
                _digest64(f"b{index}") % _PRIME,
            )
            for index in range(self.signature_size)
        ]

        self._order: deque[int] = deque()
        self._signatures: dict[int, tuple[int, ...]] = {}
        self._texts: dict[int, str] = {}
        self._buckets: dict[tuple[int, int], set[int]] = {}
        self._next = 0

    def signature(self, text: str) -> tuple[int, ...] | None:
        """The MinHash signature of a text, or None if there is nothing to hash."""
        grams = shingles(text, self.shingle_size)
        if not grams:
            return None
        hashed = [_digest64(gram) % _PRIME for gram in grams]
        return tuple(
            min((a * value + b) % _PRIME for value in hashed) for a, b in self._coefficients
        )

    @staticmethod
    def estimate(left: Sequence[int], right: Sequence[int]) -> float:
        """Estimated Jaccard similarity: the fraction of rows that agree."""
        if not left:
            return 0.0
        matches = sum(1 for a, b in zip(left, right, strict=True) if a == b)
        return matches / len(left)

    def _band_keys(self, signature: Sequence[int]) -> list[tuple[int, int]]:
        keys: list[tuple[int, int]] = []
        for band in range(self.bands):
            chunk = signature[band * self.rows : (band + 1) * self.rows]
            keys.append((band, hash(chunk)))
        return keys

    def candidates(self, signature: Sequence[int]) -> set[int]:
        found: set[int] = set()
        for key in self._band_keys(signature):
            found |= self._buckets.get(key, set())
        return found

    def add(self, text: str, signature: Sequence[int]) -> int:
        """Store a text and return its handle."""
        handle = self._next
        self._next += 1
        tupled = tuple(signature)
        self._signatures[handle] = tupled
        self._texts[handle] = text
        self._order.append(handle)
        for key in self._band_keys(tupled):
            self._buckets.setdefault(key, set()).add(handle)
        if len(self._order) > self.window:
            self._evict()
        return handle

    def _evict(self) -> None:
        handle = self._order.popleft()
        signature = self._signatures.pop(handle, None)
        self._texts.pop(handle, None)
        if signature is None:
            return
        for key in self._band_keys(signature):
            bucket = self._buckets.get(key)
            if bucket is None:
                continue
            bucket.discard(handle)
            if not bucket:
                del self._buckets[key]

    def text_of(self, handle: int) -> str:
        return self._texts.get(handle, "")

    def signature_of(self, handle: int) -> tuple[int, ...]:
        return self._signatures.get(handle, ())

    def __len__(self) -> int:
        return len(self._order)

    def describe(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "held": len(self),
            "signature_size": self.signature_size,
            "bands": self.bands,
            "rows_per_band": self.rows,
            "similarity": self.similarity,
            "shingle_size": self.shingle_size,
        }


def _band_layout(signature_size: int, similarity: float) -> tuple[int, int]:
    """Bands and rows for an LSH threshold at or just below ``similarity``.

    Only layouts that divide the signature evenly are usable, and there are few
    enough to enumerate, so this is a search rather than a formula.

    *Below* rather than *closest*, deliberately. The banding decides which pairs
    are even looked at; the similarity comparison afterwards decides which ones
    count. A threshold above the target means genuinely similar pairs are never
    proposed and can never be found, which is a silent false negative - while a
    threshold below it means a few extra candidates get compared and rejected,
    which costs a little arithmetic. Recall is the property worth buying here.
    """
    below = (0, 0)
    below_threshold = -1.0
    closest = (1, signature_size)
    closest_error = float("inf")

    for rows in range(1, signature_size + 1):
        if signature_size % rows:
            continue
        bands = signature_size // rows
        threshold = (1.0 / bands) ** (1.0 / rows)
        if threshold <= similarity and threshold > below_threshold:
            below_threshold, below = threshold, (bands, rows)
        error = abs(threshold - similarity)
        if error < closest_error:
            closest_error, closest = error, (bands, rows)

    return below if below != (0, 0) else closest


@dataclass(slots=True)
class DuplicateExample:
    """One duplication, kept so a report can show rather than assert."""

    kind: str
    field: str
    record_index: int
    matched_index: int | None
    similarity: float
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "field": self.field,
            "record_index": self.record_index,
            "matched_index": self.matched_index,
            "similarity": round(self.similarity, 4),
            "excerpt": self.excerpt,
        }


@dataclass(slots=True)
class DuplicationReport:
    """What duplicate detection found, and how much of it to trust."""

    entity: str
    checked: int = 0
    #: Values examined. A record with three checked fields contributes three.
    values: int = 0
    exact: int = 0
    normalized: int = 0
    near: int = 0
    exempt: int = 0
    fields: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    examples: list[DuplicateExample] = field(default_factory=list)
    #: Thresholds from the schema, and whether they held.
    max_exact: float | None = None
    max_near: float | None = None
    filter_stats: dict[str, Any] = field(default_factory=dict)
    index_stats: dict[str, Any] = field(default_factory=dict)

    @property
    def exact_rate(self) -> float:
        return self.exact / self.values if self.values else 0.0

    @property
    def normalized_rate(self) -> float:
        return self.normalized / self.values if self.values else 0.0

    @property
    def near_rate(self) -> float:
        return self.near / self.values if self.values else 0.0

    @property
    def uniqueness(self) -> float:
        """Section 58's score: the fraction of values that were not repeats."""
        repeated = self.exact + self.normalized + self.near
        return max(0.0, 1.0 - repeated / self.values) if self.values else 1.0

    @property
    def breaches(self) -> list[str]:
        """Thresholds the dataset failed, as sentences."""
        found: list[str] = []
        if self.max_exact is not None and self.exact_rate > self.max_exact:
            found.append(
                f"{self.entity}: {self.exact_rate:.2%} of values are exact duplicates, "
                f"above the {self.max_exact:.2%} allowed"
            )
        if self.max_near is not None and self.near_rate > self.max_near:
            found.append(
                f"{self.entity}: {self.near_rate:.2%} of values are near duplicates, "
                f"above the {self.max_near:.2%} allowed"
            )
        return found

    @property
    def ok(self) -> bool:
        return not self.breaches

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "checked_records": self.checked,
            "checked_values": self.values,
            "fields": list(self.fields),
            "methods": list(self.methods),
            "exact": self.exact,
            "normalized": self.normalized,
            "near": self.near,
            "exempt_duplicates": self.exempt,
            "exact_rate": round(self.exact_rate, 6),
            "normalized_rate": round(self.normalized_rate, 6),
            "near_rate": round(self.near_rate, 6),
            "uniqueness": round(self.uniqueness, 6),
            "ok": self.ok,
            "breaches": self.breaches,
            "examples": [example.to_dict() for example in self.examples],
            # How much of the above to believe.
            "bloom": dict(self.filter_stats),
            "index": dict(self.index_stats),
        }


#: Most examples worth keeping. A report is read by a person.
_MAX_EXAMPLES = 10
#: How much of a repeated value to quote back.
_EXCERPT = 160


class DuplicateDetector:
    """Feeds records through the configured techniques (section 59).

    One detector per entity, created by the engine and fed the same records the
    writer gets. It holds no records - only digests, signatures and a bounded
    window - so it costs the same on a run of ten thousand and a run of ten
    million.
    """

    def __init__(
        self,
        entity: CompiledEntity,
        spec: DuplicationSpec,
        *,
        expected_records: int | None = None,
    ) -> None:
        self.entity = entity
        self.spec = spec
        self.report = DuplicationReport(entity=entity.name)

        unknown = [method for method in spec.methods if method not in METHODS]
        if unknown:
            if "embeddings" in unknown:
                raise SchemaError(
                    "duplication method 'embeddings' needs an embedding provider, and no "
                    "adapter offers one yet. Use 'minhash' or 'fuzzy' for semantic-ish "
                    "similarity over phrasing (design document section 59)."
                )
            raise SchemaError(
                f"unknown duplication method(s): {', '.join(sorted(unknown))}. "
                f"Available: {', '.join(METHODS)}"
            )

        self.fields = self._resolve_fields()
        self.report.fields = list(self.fields)
        self.report.methods = list(spec.methods)
        self.report.max_exact = spec.max_exact
        self.report.max_near = spec.max_near

        capacity = max(1_000, int(expected_records or entity.count) * max(1, len(self.fields)))
        self._exact = (
            BloomFilter(capacity, spec.error_rate) if self._wants("exact", "normalized") else None
        )
        self._index = (
            MinHashIndex(
                window=spec.window,
                signature_size=spec.signature_size,
                similarity=spec.similarity,
                shingle_size=spec.shingle,
            )
            if self._wants("minhash", "fuzzy")
            else None
        )

    def _wants(self, *methods: str) -> bool:
        return any(method in self.spec.methods for method in methods)

    def _resolve_fields(self) -> list[str]:
        """Which fields to compare.

        Declared, or else the fields where repetition actually happens: the
        long-form text a model wrote. Comparing an employee id against every
        other employee id would find nothing and cost a great deal, and
        comparing a weighted choice would report that ``active`` recurs -
        which is what a weighted choice is for.
        """
        if self.spec.fields == ["*"]:
            return ["*"]
        if self.spec.fields:
            known = set(self.entity.spec.field_names())
            missing = [name for name in self.spec.fields if name not in known]
            if missing:
                raise SchemaError(
                    f"duplication.fields names field(s) {self.entity.name} does not have: "
                    f"{', '.join(missing)}"
                )
            return list(self.spec.fields)

        return [
            compiled.name
            for compiled in self.entity.fields
            if _is_free_text(compiled) and not compiled.spec.unique
        ]

    # -- the check ------------------------------------------------------------ #

    def observe(self, record: GeneratedRecord) -> None:
        """Note one record. Called once per record, in generation order."""
        from ..simulation.chaos import DUPLICATE_MARK

        self.report.checked += 1
        if DUPLICATE_MARK in record.damage:
            # Asked for. Reporting it would be reporting the feature.
            self.report.exempt += 1
            return

        index = record.provenance.record_index if record.provenance else self.report.checked - 1
        for name, text in self._values(record):
            if not text:
                continue
            self.report.values += 1
            if self._check_hashes(name, text, index):
                continue
            self._check_similarity(name, text, index)

    def _values(self, record: GeneratedRecord) -> list[tuple[str, str]]:
        if self.fields == ["*"]:
            # The whole record as one value, in field order, so two identical
            # rows collide and two rows differing anywhere do not.
            joined = "␟".join(
                str(record.values.get(name, "")) for name in self.entity.spec.field_names()
            )
            return [("*", joined)]
        return [
            (name, str(record.values[name]))
            for name in self.fields
            if record.values.get(name) is not None
        ]

    def _check_hashes(self, name: str, text: str, index: int) -> bool:
        """Exact and normalised hashing. True if this value was a repeat."""
        if self._exact is None:
            return False

        if "exact" in self.spec.methods and self._exact.add(f"e␟{name}␟{text}"):
            self.report.exact += 1
            self._remember("exact", name, index, None, 1.0, text)
            return True

        if "normalized" in self.spec.methods:
            folded = normalise(text)
            if folded and self._exact.add(f"n␟{name}␟{folded}"):
                self.report.normalized += 1
                self._remember("normalized", name, index, None, 1.0, text)
                return True
        return False

    def _check_similarity(self, name: str, text: str, index: int) -> None:
        """MinHash and fuzzy comparison against the window."""
        if self._index is None:
            return

        signature = self._index.signature(text)
        if signature is None:
            return

        best_handle: int | None = None
        best_score = 0.0
        for handle in self._index.candidates(signature):
            score = self._index.estimate(signature, self._index.signature_of(handle))
            if score > best_score:
                best_score, best_handle = score, handle

        if best_handle is not None and best_score >= self._index.similarity:
            if "fuzzy" in self.spec.methods:
                # LSH proposed; confirm with a real ratio. An estimate over 64
                # rows is ±6% at one standard deviation, and a threshold
                # decided by an estimate reports pairs that are not there.
                # autojunk=False is load-bearing. With the default on, any
                # character appearing in more than 1% of a sequence longer than
                # 200 characters is treated as noise - which for prose means
                # the vowels. Measured on two biographies differing only in the
                # name, the default scored 0.014 and the truth is 0.956. A
                # confirmation step that rejects real duplicates is worse than
                # no confirmation step.
                best_score = SequenceMatcher(
                    None,
                    normalise(text),
                    normalise(self._index.text_of(best_handle)),
                    autojunk=False,
                ).ratio()
            if best_score >= self._index.similarity:
                self.report.near += 1
                self._remember("near", name, index, best_handle, best_score, text)

        self._index.add(text, signature)

    def _remember(
        self,
        kind: str,
        name: str,
        index: int,
        matched: int | None,
        score: float,
        text: str,
    ) -> None:
        if len(self.report.examples) >= _MAX_EXAMPLES:
            return
        excerpt = text if len(text) <= _EXCERPT else text[: _EXCERPT - 1] + "…"
        self.report.examples.append(
            DuplicateExample(
                kind=kind,
                field=name,
                record_index=index,
                matched_index=matched,
                similarity=score,
                excerpt=excerpt,
            )
        )

    def finish(self) -> DuplicationReport:
        """Close the report, recording how much of it to trust."""
        if self._exact is not None:
            self.report.filter_stats = self._exact.describe()
        if self._index is not None:
            self.report.index_stats = self._index.describe()
        return self.report


def _is_free_text(compiled: Any) -> bool:
    """Whether a field holds prose worth comparing.

    Model-written fields always; long strings and declared text otherwise.
    A postcode is a string and repeats constantly, which is correct.
    """
    spec = compiled.spec
    if type(compiled.generator).requires_provider == "language_model":
        return True
    if spec.type.value in ("text",):
        return True
    if spec.type.value == "string":
        limit = spec.constraints.max_length
        return bool(limit and limit >= 80)
    return False
