"""Distributed generation (design document sections 84 and 95).

    Cacophony Controller
          ├── CPU Worker Node      deterministic fields
          ├── LLM GPU Node         language-model enrichment
          ├── InvokeAI Node        images
          └── TTS Node             speech

A run is cut into shards - contiguous index ranges - and handed out as leases.
Workers advertise what they can do, the scheduler gives them only shards they
can do, and a worker that stops answering has its shards handed to somebody
else.

The whole thing is small because of section 75. A record's seed is a hash of
its position, so record 4,823,913 is the same record on any machine, in any
order, at any time. There is no RNG state to partition, no ordering to
preserve, and no merge: the parts concatenate into exactly the bytes one
machine would have written.
"""

from __future__ import annotations

from .assembly import AssemblyResult, assemble, collect_assets, shard_parts
from .capabilities import CAPABILITIES, Capabilities, WorkerProfile, capabilities_for
from .controller import Controller, ControllerStats, WorkerRecord, plan_shards
from .leases import Lease, LeaseState, Shard
from .transport import ControllerTransport, HttpTransport, LocalTransport
from .worker import ShardResult, Worker, WorkerStats

__all__ = [
    "CAPABILITIES",
    "AssemblyResult",
    "Capabilities",
    "Controller",
    "ControllerStats",
    "ControllerTransport",
    "HttpTransport",
    "Lease",
    "LeaseState",
    "LocalTransport",
    "Shard",
    "ShardResult",
    "Worker",
    "WorkerProfile",
    "WorkerRecord",
    "WorkerStats",
    "assemble",
    "capabilities_for",
    "collect_assets",
    "plan_shards",
    "shard_parts",
]
