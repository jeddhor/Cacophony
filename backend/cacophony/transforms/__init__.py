"""Post-generation transforms and record editing (sections 104, 105).

    cacophony transform out/employee.jsonl --set 'email = mask:4' --out masked.jsonl
    cacophony regenerate <run-id> --records 4823913-4823920

Two ways of changing a dataset that already exists, and one rule about both.

**An edit is recorded as a rule, not applied as a mutation.** A Cacophony
dataset is a pure function of its schema and its seed, and editing a row in an
output file breaks that silently: the file stops corresponding to anything, and
the next run overwrites the edit without noticing. So the Studio's record editor
saves a `patches:` rule, which is applied during generation and travels with the
project - and regenerating that record next year produces the same edited value.

**Regeneration is nearly free.** Record 4,823,913's seed is a hash of its
position (section 75), so reproducing exactly that record needs no state, no run
and no file. "This one row looks wrong" is a question with a cheap answer.
"""

from __future__ import annotations

from .expressions import RecordExpression
from .operations import (
    ALIASES,
    OPERATIONS,
    TransformError,
    apply_operations,
    describe_operations,
    parse_step,
)
from .pipeline import TRANSFORMABLE, TransformResult, transform_file
from .rules import FieldEdit, PatchRule, PatchSet, PatchStats, load_rules

__all__ = [
    "ALIASES",
    "OPERATIONS",
    "TRANSFORMABLE",
    "FieldEdit",
    "PatchRule",
    "PatchSet",
    "PatchStats",
    "RecordExpression",
    "TransformError",
    "TransformResult",
    "apply_operations",
    "describe_operations",
    "load_rules",
    "parse_step",
    "transform_file",
]
