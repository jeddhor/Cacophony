"""Live generation (design document sections 35, 94).

    Produce approximately 250 authentication events/sec, 50 endpoint
    events/sec, 8 alerts/minute. This transforms Cacophony into a workload
    generator.

:mod:`~cacophony.live.rates`
    Reading a rate the way people write one, and holding it without drifting.

:mod:`~cacophony.live.sinks`
    Where a stream's records go: stdout, a rotating file, syslog, HTTP, Kafka.

:mod:`~cacophony.live.stream`
    The loop. Unbounded indices, wall-clock timestamps, interleaved subjects,
    and backpressure that slows the stream rather than filling memory.
"""

from .rates import Rate, RateLimiter, parse_rate
from .sinks import SINK_TYPES, DeliveryStats, StreamSink, create_sink
from .stream import EntityStream, LiveStream, StreamConfig, StreamStats

__all__ = [
    "SINK_TYPES",
    "DeliveryStats",
    "EntityStream",
    "LiveStream",
    "Rate",
    "RateLimiter",
    "StreamConfig",
    "StreamSink",
    "StreamStats",
    "create_sink",
    "parse_rate",
]
