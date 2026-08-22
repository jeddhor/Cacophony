"""The model benchmark (design document section 67).

    **Test this model against this schema.**

    MODEL               VALID   SPEED   DUPLICATION
    Model A             99.8%   42 t/s    0.8%
    Model B             96.2%   71 t/s    2.4%
    Model C             100%    19 t/s    0.3%

The question is deliberately narrow. Not "which model is better" - that is a
leaderboard, and leaderboards are answered by other people - but "which of the
models I can actually reach produces records *this schema* accepts, and how
fast". A model that scores brilliantly on reasoning benchmarks and cannot
reliably return a JSON object with two string fields is the wrong model for
this job, and only running it against the schema will say so.

So the benchmark generates real records through the real pipeline. Not a
synthetic prompt, not a sample of one field: the same prompt compiler, the same
structured-output enforcement, the same validators, the same duplicate
detection that a run would use. The numbers therefore mean what they say.

**Fairness is the whole difficulty.** Three things would silently invalidate a
comparison, and each is prevented here rather than documented as a caveat:

*Different data.* Every model generates the same record indices from the same
seed, so they are answering the same questions in the same order.

*A warm cache.* Cacophony caches model output by content (section 76), so the
second model would be measured on the first model's answers and score
infinitely fast. The cache is forced off.

*Different concurrency.* Throughput depends on how many requests are in flight,
so every model is run at the concurrency the project declares for it - and the
report states it, because 71 tokens/sec at four concurrent requests is not
comparable to 42 at one.

**What "semantic quality" means here.** Section 67 lists it, and it cannot be
measured without a judge model - which would make the benchmark depend on
exactly the thing it is trying to assess. What is measured instead is named
honestly: how often a field came back empty, how often it broke its declared
length, and how often the model answered with boilerplate about being a
language model rather than with the content. Those are proxies. They are also
the failures that actually happen.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.errors import CacophonyError, SchemaError
from ..providers.cache import CacheMode, GenerationCache

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ..schema.plan import CompiledProject

__all__ = ["BenchmarkResult", "ModelScore", "benchmark_models"]

#: Answers that are about the model rather than about the record. A field asked
#: for a biography and given one of these has failed in a way no type check
#: catches.
_REFUSALS = re.compile(
    r"\b(as an ai|as a language model|i (?:cannot|can't|am unable to)|"
    r"i do not have (?:access|enough)|i'm sorry, (?:but )?i)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ModelScore:
    """One model's result against one schema."""

    model: str
    provider: str
    records: int = 0
    #: Values produced by a model-backed field, which is what every rate below
    #: is a fraction of.
    values: int = 0

    parse_failures: int = 0
    repairs: int = 0
    call_failures: int = 0
    retries: int = 0
    calls: int = 0

    empty: int = 0
    over_length: int = 0
    #: Values that stop dead at the length limit, mid-word.
    #:
    #: Found in real output rather than anticipated. A provider that enforces
    #: the JSON Schema natively stops decoding at ``maxLength``, so the value
    #: is never *over* length - it is cut. "...failed to handle queueing
    #: mechanism, led 3" passes every check in the platform and is not a
    #: sentence. A model that needs 140 characters to answer a question given
    #: 90 is the wrong model, or the limit is the wrong limit, and either way
    #: somebody should be told.
    clipped: int = 0
    refusals: int = 0

    validation_failures: int = 0
    duplicate_values: int = 0

    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    latency_ms: float = 0.0
    concurrency: int = 1
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.records > 0

    @property
    def json_validity(self) -> float:
        """How often a call's answer parsed without repair.

        Section 67's ``VALID`` column. Repairs count against it: a model whose
        output needs the repair stage on one call in twenty is measurably worse
        than one whose does not, even though both eventually produce a record.
        """
        if not self.calls:
            return 1.0
        clean = self.calls - self.parse_failures - self.repairs
        return max(0.0, clean / self.calls)

    @property
    def field_validity(self) -> float:
        """How often a produced record satisfied its own schema."""
        if not self.records:
            return 1.0
        return max(0.0, (self.records - self.validation_failures) / self.records)

    @property
    def usable(self) -> float:
        """Values fit to put in a dataset.

        Not empty, not over their declared length, not cut off mid-word at it,
        and not a paragraph about being a language model.
        """
        if not self.values:
            return 1.0
        bad = self.empty + self.over_length + self.clipped + self.refusals
        return max(0.0, (self.values - bad) / self.values)

    @property
    def duplication(self) -> float:
        return self.duplicate_values / self.values if self.values else 0.0

    @property
    def tokens_per_second(self) -> float:
        return self.completion_tokens / self.seconds if self.seconds else 0.0

    @property
    def records_per_second(self) -> float:
        return self.records / self.seconds if self.seconds else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "ok": self.ok,
            "error": self.error,
            "records": self.records,
            "values": self.values,
            "calls": self.calls,
            "concurrency": self.concurrency,
            "json_validity": round(self.json_validity, 6),
            "field_validity": round(self.field_validity, 6),
            "usable": round(self.usable, 6),
            "duplication": round(self.duplication, 6),
            "tokens_per_second": round(self.tokens_per_second, 2),
            "records_per_second": round(self.records_per_second, 3),
            "latency_ms": round(self.latency_ms, 1),
            "seconds": round(self.seconds, 3),
            "completion_tokens": self.completion_tokens,
            "prompt_tokens": self.prompt_tokens,
            "parse_failures": self.parse_failures,
            "repairs": self.repairs,
            "retries": self.retries,
            "call_failures": self.call_failures,
            "empty_values": self.empty,
            "over_length_values": self.over_length,
            "clipped_values": self.clipped,
            "refusals": self.refusals,
            "validation_failures": self.validation_failures,
        }


@dataclass(slots=True)
class BenchmarkResult:
    """Every model's score, and what they were all asked to do."""

    project: str
    entity: str
    records: int
    seed: int
    fields: list[str] = field(default_factory=list)
    scores: list[ModelScore] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return any(score.ok for score in self.scores)

    def ranked(self, by: str = "json_validity") -> list[ModelScore]:
        """Scores worth comparing, best first; failures last, in order."""
        working = [score for score in self.scores if score.ok]
        broken = [score for score in self.scores if not score.ok]
        working.sort(key=lambda score: getattr(score, by), reverse=True)
        return [*working, *broken]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "entity": self.entity,
            "records": self.records,
            "seed": self.seed,
            "fields": list(self.fields),
            "models": [score.to_dict() for score in self.scores],
        }


def model_backed_fields(compiled: CompiledProject, entity: str) -> list[str]:
    """The fields of an entity a language model produces."""
    return [
        field_.name
        for field_ in compiled.entity(entity).fields
        if type(field_.generator).requires_provider == "language_model"
    ]


def default_entity(compiled: CompiledProject) -> str:
    """The entity with the most model-backed fields.

    A benchmark of an entity with no model fields measures nothing, so the
    default is the one that exercises the model hardest rather than the first
    one declared.
    """
    best = ""
    most = 0
    for name in compiled.entity_order:
        count = len(model_backed_fields(compiled, name))
        if count > most:
            best, most = name, count
    if not best:
        raise SchemaError(
            "no entity in this project has a language-model field, so there is nothing "
            "to benchmark a model against. Add a field with 'generator: llm' or a "
            "'semantic:' description that needs one."
        )
    return best


async def benchmark_models(
    compiled: CompiledProject,
    models: Sequence[str],
    *,
    entity: str | None = None,
    records: int = 100,
    provider: str | None = None,
    on_model: Any = None,
) -> BenchmarkResult:
    """Run each model against the schema and score it (section 67)."""
    from ..providers.registry import PROVIDER_REGISTRY

    target = entity or default_entity(compiled)
    fields = model_backed_fields(compiled, target)
    if not fields:
        raise SchemaError(
            f"'{target}' has no language-model fields, so a model benchmark over it would "
            f"measure nothing. Try: {', '.join(compiled.entity_order)}"
        )

    provider_id = provider or _sole_language_provider(compiled, provider)
    result = BenchmarkResult(
        project=compiled.name,
        entity=target,
        records=max(1, records),
        seed=compiled.seed,
        fields=fields,
    )

    for model in models:
        if on_model is not None:
            on_model(model)
        score = await _score_one(
            compiled,
            model=model,
            provider_id=provider_id,
            entity=target,
            fields=fields,
            records=result.records,
            registry=PROVIDER_REGISTRY,
        )
        result.scores.append(score)
    return result


def _sole_language_provider(compiled: CompiledProject, requested: str | None) -> str:
    language = [
        spec.id for spec in compiled.spec.providers.values() if spec.type == "language_model"
    ]
    if requested:
        if requested not in language:
            raise SchemaError(
                f"'{requested}' is not a language-model provider in this project. "
                f"Available: {', '.join(language) or '<none>'}"
            )
        return requested
    if not language:
        raise SchemaError(
            "this project declares no language-model provider, so there is no server to "
            "benchmark models on (design document section 43)."
        )
    if len(language) > 1:
        raise SchemaError(
            f"this project declares several language-model providers "
            f"({', '.join(language)}). Name one with --provider."
        )
    return language[0]


async def _score_one(
    compiled: CompiledProject,
    *,
    model: str,
    provider_id: str,
    entity: str,
    fields: Sequence[str],
    records: int,
    registry: Any,
) -> ModelScore:
    """Generate ``records`` records with one model and measure everything."""
    from copy import deepcopy

    from ..validation.duplication import DuplicateDetector
    from .engine import FailurePolicy, GenerationEngine
    from .runtime import GenerationRuntime

    # A copy, because the model is written into the provider spec and one
    # model's settings must not leak into the next one's run.
    spec = deepcopy(compiled.spec)
    spec.providers[provider_id].model = model
    concurrency = spec.providers[provider_id].concurrency

    score = ModelScore(model=model, provider=provider_id, concurrency=concurrency)

    runtime = GenerationRuntime.for_project(
        spec,
        # Forced off. With the cache on, the second model is scored on the
        # first model's answers and reports an impossible speed.
        cache=GenerationCache(mode=CacheMode.DISABLED),
    )

    compiled_copy = _recompile(spec)
    engine = GenerationEngine(
        compiled_copy,
        runtime=runtime,
        # Every model answers the same questions in the same order.
        counts=dict.fromkeys(compiled_copy.entity_order, records),
        validate=True,
        drop_invalid=False,
        # The whole point is to count what a model got wrong, so an invalid
        # record is a score rather than a reason to stop. A model that cannot be
        # reached at all is still an abort, which the benchmark catches and
        # reports as that model's error.
        validation_policy=FailurePolicy.REPORT,
        # Damage would be counted as a model failure, and a chaotic run's
        # duplicate records would be counted as repetition.
        chaos=False,
        detect_duplicates=False,
    )

    # Duplication is measured here rather than by the engine, so the fields
    # compared are exactly the model-backed ones whatever the schema declares.
    detector = DuplicateDetector(
        compiled_copy.entity(entity),
        _benchmark_duplication_spec(list(fields)),
        expected_records=records,
    )

    started = time.perf_counter()
    try:
        produced = 0
        async for chunk in engine.stream(entity, count=records, batch_size=min(records, 20)):
            for record in chunk.records:
                produced += 1
                detector.observe(record)
                _score_values(score, compiled_copy, entity, record, fields)
        score.records = produced
    except CacophonyError as exc:
        score.error = str(exc)
    except Exception as exc:
        # One model failing must not end the comparison - a model that cannot
        # be reached is a result, and the other models still have scores.
        score.error = f"{type(exc).__name__}: {exc}"
    score.seconds = time.perf_counter() - started

    stats = runtime.stats
    score.calls = stats.llm_calls
    score.parse_failures = stats.parse_failures
    score.repairs = stats.repairs
    score.retries = stats.llm_retries
    score.call_failures = stats.llm_failures
    score.prompt_tokens = stats.prompt_tokens
    score.completion_tokens = stats.completion_tokens
    score.latency_ms = stats.mean_latency_ms

    entity_stats = engine.stats.get(entity)
    if entity_stats is not None:
        score.validation_failures = entity_stats.rejected

    report = detector.finish()
    score.duplicate_values = report.exact + report.normalized + report.near

    await runtime.aclose()
    return score


def _benchmark_duplication_spec(fields: list[str]) -> Any:
    from ..schema.models import DuplicationSpec

    return DuplicationSpec(
        enabled=True,
        fields=fields,
        methods=["exact", "normalized", "minhash"],
        # A hundred records is a small window; hold all of them.
        window=10_000,
    )


def _recompile(spec: Any) -> CompiledProject:
    from ..schema.compiler import compile_project

    return compile_project(spec)


def _score_values(
    score: ModelScore,
    compiled: CompiledProject,
    entity: str,
    record: Any,
    fields: Sequence[str],
) -> None:
    """Count the failures no type check catches."""
    specs = compiled.entity(entity).spec.fields
    for name in fields:
        value = record.values.get(name)
        score.values += 1
        if value is None or not str(value).strip():
            score.empty += 1
            continue

        text = str(value)
        limit = specs[name].constraints.max_length
        if limit and len(text) > limit:
            score.over_length += 1
        elif limit and _is_clipped(text, limit):
            score.clipped += 1
        if _REFUSALS.search(text):
            score.refusals += 1


def _is_clipped(text: str, limit: int) -> bool:
    """Whether a value was cut off at its limit rather than finished.

    A value at or within two characters of the limit that does not end on
    sentence-ending punctuation was almost certainly stopped by the decoder
    rather than by the model. Two characters of slack because a constrained
    decoder may stop one short of the limit rather than exactly on it.
    """
    if len(text) < limit - 2:
        return False
    return not text.rstrip().endswith((".", "!", "?", "\u2026", '"', ")"))


def render_table(result: BenchmarkResult, *, by: str = "json_validity") -> list[list[str]]:
    """Section 67's table, as rows ready for a renderer."""
    rows = [["MODEL", "VALID", "FIELDS", "USABLE", "CLIPPED", "SPEED", "DUPLICATION", "LATENCY"]]
    for score in result.ranked(by):
        if not score.ok:
            rows.append([score.model, "failed", "-", "-", "-", "-", "-", "-"])
            continue
        rows.append(
            [
                score.model,
                f"{score.json_validity:.1%}",
                f"{score.field_validity:.1%}",
                f"{score.usable:.1%}",
                f"{score.clipped:,}",
                f"{score.tokens_per_second:.0f} t/s",
                f"{score.duplication:.1%}",
                f"{score.latency_ms:.0f} ms",
            ]
        )
    return rows


def run_benchmark(
    compiled: CompiledProject,
    models: Sequence[str],
    **options: Any,
) -> BenchmarkResult:
    """Synchronous wrapper, for the CLI."""
    return asyncio.run(benchmark_models(compiled, models, **options))
