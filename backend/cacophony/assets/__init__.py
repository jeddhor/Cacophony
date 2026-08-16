"""The asset layer (design document sections 18-23, 81, 82).

Everything to do with generated *files* rather than generated values: where
they go, what formats they are written in, and what is recorded about them.

:mod:`~cacophony.assets.store`
    The asset store. Derived paths, content-addressed deduplication and a
    manifest sidecar answering section 81's "what belongs to this record?"

:mod:`~cacophony.assets.imaging`
    A small raster canvas and a PNG encoder, written from the standard library.

:mod:`~cacophony.assets.audio`
    WAV writing, duration measurement, and enough synthesis to exercise an
    audio pipeline without a TTS engine.

:mod:`~cacophony.assets.documents`
    Placeholder-filled templates, page layout, and a PDF writer.
"""

from .audio import DEFAULT_SAMPLE_RATE, concatenate, duration_of, encode_wav, speech_like
from .documents import Document, render_pdf, render_template
from .imaging import Canvas, encode_png
from .store import MANIFEST_NAME, AssetStats, AssetStore, StoredAsset, extension_for

__all__ = [
    "DEFAULT_SAMPLE_RATE",
    "MANIFEST_NAME",
    "AssetStats",
    "AssetStore",
    "Canvas",
    "Document",
    "StoredAsset",
    "concatenate",
    "duration_of",
    "encode_png",
    "encode_wav",
    "extension_for",
    "render_pdf",
    "render_template",
    "speech_like",
]
