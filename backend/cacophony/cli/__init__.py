"""Command-line interface (design document section 37).

Section 96 sketches ``cli/`` as a sibling of ``backend/``. It lives inside the
backend package instead, because the CLI is a thin presentation layer over the
same schema, generation and output objects the API will use, and splitting it
into a separate distribution would buy nothing but an import path.
"""

from .main import app, run

__all__ = ["app", "run"]
