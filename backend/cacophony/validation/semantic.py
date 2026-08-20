"""Section 57's semantic category: asking a model whether a record makes sense.

    Does this biography plausibly correspond to the supplied employee profile?

    Semantic validation should be optional because of cost.

Optional, and off unless asked for - which is not only about cost. Judging
generated text with a language model makes the measurement depend on the same
kind of machinery that produced the thing being measured, which is the objection
recorded against section 67's semantic quality scoring and the reason the model
benchmark refuses it. Here the objection is weaker, because a run's own
provider is a different model doing a different job, and the answer is reported
as an opinion with the model's name attached rather than as a score.

Two things keep it honest. It samples rather than judging everything, so the
cost is bounded and stated. And every verdict carries the model that gave it, so
nobody reads "97% plausible" without also reading which model thought so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.seeds import mix_seed

#: Distinguishes a judge's seed from every other derivation (section 75).
_JUDGE_SALT = 0x53454D41

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.record import GeneratedRecord
    from ..generation.runtime import GenerationRuntime
    from ..schema.models import SemanticSpec
    from ..schema.plan import CompiledEntity

__all__ = ["SemanticEvaluator"]

#: What the judge is asked to return. Structured, for the reason section 13
#: gives: what comes back is text until something checks it.
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["plausible"],
    "additionalProperties": False,
    "properties": {
        "plausible": {
            "type": "boolean",
            "description": "Whether the value fits the record and the field's stated meaning.",
        },
        "reason": {"type": "string", "description": "One short sentence."},
    },
}

_SYSTEM = (
    "You are checking synthetic data for plausibility, not for truth. "
    "The records are invented on purpose; say whether the value is a believable "
    "example of what the field describes, given the rest of the record. "
    "Answer with JSON only."
)


@dataclass(slots=True)
class SemanticEvaluator:
    """Collects a bounded sample during a run and judges it at the end."""

    entity: CompiledEntity
    spec: SemanticSpec
    fields: tuple[str, ...]
    sample: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    seen: int = 0
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @classmethod
    def for_entity(cls, entity: CompiledEntity, spec: SemanticSpec) -> SemanticEvaluator | None:
        """None when there is nothing here worth asking about."""
        if not spec.enabled:
            return None
        names = tuple(spec.fields) or tuple(
            compiled.name
            for compiled in entity.fields
            if type(compiled.generator).requires_provider == "language_model"
        )
        if not names:
            return None
        return cls(entity=entity, spec=spec, fields=names)

    def observe(self, record: GeneratedRecord) -> None:
        """Keep every nth record, up to the sample size.

        Chosen by position rather than at random, so the same run judges the
        same records - section 75's property applied to the measurement as well
        as to the data.
        """
        index = self.seen
        self.seen += 1
        if len(self.sample) >= self.spec.sample:
            return
        stride = max(1, self.spec.every)
        if index % stride:
            return
        self.sample.append((index, dict(record.values)))

    async def evaluate(self, runtime: GenerationRuntime) -> dict[str, Any] | None:
        """Ask the judge about the sample. Never raises into a run."""
        if not self.sample:
            return None

        try:
            provider = runtime.language_model(self.spec.provider)
        except Exception as exc:  # pragma: no cover - configuration
            self.error = f"no language model to judge with: {exc}"
            return self.summary()

        from ..providers.base import GenerationRequest

        for index, values in self.sample:
            for name in self.fields:
                if name not in values or values[name] is None:
                    continue
                request = GenerationRequest(
                    prompt=self._prompt(name, values),
                    system=_SYSTEM,
                    model=self.spec.model or getattr(provider, "model", None),
                    json_schema=VERDICT_SCHEMA,
                    max_tokens=200,
                    temperature=0.0,
                    # A fixed seed per sampled record, so re-judging the same
                    # run asks the same questions in the same way.
                    seed=mix_seed(_JUDGE_SALT, _JUDGE_SALT, index),
                )
                try:
                    answer = await provider.generate(request)
                except Exception as exc:  # pragma: no cover - provider failure
                    self.error = f"the judge could not be reached: {exc}"
                    return self.summary()
                self.verdicts.append(
                    {
                        "record": index,
                        "field": name,
                        **_read(answer.text),
                        "model": request.model or "",
                    }
                )
        return self.summary()

    def _prompt(self, name: str, values: dict[str, Any]) -> str:
        compiled = next((f for f in self.entity.fields if f.name == name), None)
        meaning = (compiled.spec.meaning if compiled else None) or name
        context = {
            key: value
            for key, value in values.items()
            if key != name and not str(key).startswith("_")
        }
        return (
            f"Field: {name}\n"
            f"What it should mean: {meaning}\n"
            f"Value: {values[name]!r}\n\n"
            f"The rest of the record: {context}\n\n"
            "Is the value a plausible example of what the field describes, "
            "given the rest of the record?"
        )

    def summary(self) -> dict[str, Any]:
        judged = len(self.verdicts)
        plausible = sum(1 for verdict in self.verdicts if verdict.get("plausible"))
        models = sorted({str(v.get("model") or "") for v in self.verdicts} - {""})
        data: dict[str, Any] = {
            "judged": judged,
            "plausible": plausible,
            "rate": round(plausible / judged, 4) if judged else None,
            "sampled_records": len(self.sample),
            "of_records": self.seen,
            "fields": list(self.fields),
            # Named, always. "97% plausible" is not a number anybody should read
            # without knowing which model said so.
            "judged_by": models,
        }
        if self.error:
            data["error"] = self.error
        if self.spec.threshold is not None and judged:
            data["threshold"] = self.spec.threshold
            data["meets_threshold"] = (plausible / judged) >= self.spec.threshold
        doubted = [v for v in self.verdicts if not v.get("plausible")][:3]
        if doubted:
            data["examples"] = doubted
        return data


def _read(text: str) -> dict[str, Any]:
    """The verdict, or a recorded failure to produce one."""
    import json

    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {"plausible": None, "reason": "the judge did not answer in JSON"}
        try:
            payload = json.loads(text[start : end + 1])
        except ValueError:
            return {"plausible": None, "reason": "the judge did not answer in JSON"}
    return {
        "plausible": bool(payload.get("plausible")),
        "reason": str(payload.get("reason") or "")[:200],
    }
