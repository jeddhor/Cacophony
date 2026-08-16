"""Built-in generators.

Importing this package registers every built-in generator with
:data:`cacophony.generation.registry.REGISTRY`. Each module here corresponds to
a group of strategies from design document section 8.
"""

from .base import OptionsMixin
from .categorical import LookupGenerator, WeightedGenerator
from .composite import CompositeGenerator, TransformGenerator
from .deferred import PendingGenerator, PlaceholderMixin, ScriptGenerator
from .faker_gen import FakerGenerator
from .identifiers import GovernmentIdGenerator, PhoneNumberGenerator
from .llm import LanguageModelGenerator
from .media import DocumentGenerator, ImageGenerator, SpeechGenerator
from .network import IpAddressGenerator, MacAddressGenerator
from .numeric import BooleanGenerator, DistributionGenerator, RandomGenerator
from .reference import ReferenceGenerator
from .simple import ConstantGenerator, NullGenerator, SequenceGenerator, UuidGenerator
from .temporal import TimestampGenerator
from .text import ExpressionGenerator, PatternGenerator, TemplateGenerator

__all__ = [
    "BooleanGenerator",
    "CompositeGenerator",
    "ConstantGenerator",
    "DistributionGenerator",
    "DocumentGenerator",
    "ExpressionGenerator",
    "FakerGenerator",
    "GovernmentIdGenerator",
    "ImageGenerator",
    "IpAddressGenerator",
    "LanguageModelGenerator",
    "LookupGenerator",
    "MacAddressGenerator",
    "NullGenerator",
    "OptionsMixin",
    "PatternGenerator",
    "PendingGenerator",
    "PhoneNumberGenerator",
    "PlaceholderMixin",
    "RandomGenerator",
    "ReferenceGenerator",
    "ScriptGenerator",
    "SequenceGenerator",
    "SpeechGenerator",
    "TemplateGenerator",
    "TimestampGenerator",
    "TransformGenerator",
    "UuidGenerator",
    "WeightedGenerator",
]
