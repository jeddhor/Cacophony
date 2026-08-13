"""Terminal styling (design document section 45).

The visual identity is meant to communicate *controlled chaos*: dark graphite,
luminous violet, electric cyan, magenta highlights. The terminal can carry that
much more cheaply than it can carry waveforms and particles, so the CLI borrows
the palette and leaves the motion to the eventual UI.

Colours are only ever a second channel. Every message that matters says what it
means in words too, so the CLI stays readable when piped, logged, or read by
someone who cannot distinguish violet from magenta.
"""

from __future__ import annotations

from rich.console import Console
from rich.theme import Theme

__all__ = ["CACOPHONY_THEME", "console", "error_console"]

CACOPHONY_THEME = Theme(
    {
        "cacophony.brand": "bold #b388ff",
        "cacophony.accent": "#22d3ee",
        "cacophony.highlight": "#f472b6",
        "cacophony.muted": "#8b8b9e",
        "cacophony.rule": "#6d28d9",
        "cacophony.ok": "bold #4ade80",
        "cacophony.warn": "bold #fbbf24",
        "cacophony.error": "bold #f87171",
        "cacophony.info": "#22d3ee",
        # Generator provenance colours, used by the preview table header
        # (section 51) so a glance shows which engine produced which column.
        "gen.rule": "#8b8b9e",
        "gen.faker": "#22d3ee",
        "gen.llm": "#b388ff",
        "gen.media": "#f472b6",
        "gen.derived": "#4ade80",
    }
)

console = Console(theme=CACOPHONY_THEME)
error_console = Console(theme=CACOPHONY_THEME, stderr=True)


#: Which colour a generator's values get in the preview table.
_GENERATOR_STYLES: dict[str, str] = {
    "faker": "gen.faker",
    "llm": "gen.llm",
    "image": "gen.media",
    "tts": "gen.media",
    "expression": "gen.derived",
    "template": "gen.derived",
    "transform": "gen.derived",
    "composite": "gen.derived",
    "reference": "gen.derived",
}


def style_for_generator(name: str) -> str:
    return _GENERATOR_STYLES.get(name, "gen.rule")


#: Short uppercase label shown above each preview column (section 51).
_GENERATOR_LABELS: dict[str, str] = {
    "faker": "FAKER",
    "llm": "LLM",
    "image": "IMAGE",
    "tts": "TTS",
    "expression": "EXPR",
    "template": "TMPL",
    "sequence": "SEQ",
    "constant": "CONST",
    "weighted": "WEIGHT",
    "distribution": "DIST",
    "lookup": "LOOKUP",
    "pattern": "PATTERN",
    "datetime": "TIME",
    "uuid": "UUID",
    "reference": "REF",
    "composite": "COMP",
    "transform": "XFORM",
}


def label_for_generator(name: str) -> str:
    return _GENERATOR_LABELS.get(name, name.upper()[:8])
