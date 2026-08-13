"""Cacophony - a synthetic reality compiler.

Cacophony turns a declarative description of *what data means* into arbitrarily
large volumes of structurally valid, semantically believable, internally
consistent synthetic records.

The package is layered so that later phases (LLM providers, media generation,
scenarios, distributed execution) extend the platform rather than rewrite it:

    cacophony.core        primitive types, seeds, records, provider-neutral interfaces
    cacophony.schema      project schema, compiler, dependency graph, planner, linter
    cacophony.generation  the generator registry and the record engine
    cacophony.validation  structural / constraint / referential validation
    cacophony.outputs     output writers (CSV, JSON, JSONL, Parquet, ...)
    cacophony.providers   language-model / image / speech provider interfaces
    cacophony.scenarios   scenario engine (behavioural overlays)
    cacophony.plugins     third-party extension protocol
    cacophony.cli         command-line interface
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
