"""Generator workers (design document section 27).

The pipeline's worker pools - rule, Faker, LLM, image, TTS, script and plugin -
each with independently configurable concurrency (section 30). Phase one runs
generation inline in :mod:`cacophony.generation.engine`, which is correct for
CPU-bound deterministic generators; the pools matter once a worker can block on
a network call.
"""
