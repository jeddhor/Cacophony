"""The plugin protocol (design document section 44).

Planned categories: ``GeneratorPlugin``, ``ValidatorPlugin``,
``TransformPlugin``, ``OutputPlugin``, ``LanguageModelPlugin``,
``ImagePlugin``, ``SpeechPlugin`` and ``ScenarioPlugin``. Each ships a manifest
describing what it provides.

The extension points already exist: ``cacophony.generation.registry.REGISTRY``
for generators, ``cacophony.providers.registry.PROVIDER_REGISTRY`` for
backends, and ``cacophony.outputs.OUTPUT_FORMATS`` for writers. What this phase
adds is discovery, manifests and isolation - the parts that need to be right
before third-party code is loaded from a project directory.
"""
