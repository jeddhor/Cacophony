"""Distributed generation (design document sections 84, 95).

    Cacophony Controller
          ├── CPU Worker Node
          ├── LLM GPU Node
          ├── InvokeAI Node
          └── TTS Node

One claim carries the whole phase: a dataset produced by many workers is
*byte-identical* to the same dataset produced by one. If that holds, almost
everything a distributed system usually has to be careful about stops
mattering - a half-done shard can be thrown away, a duplicated shard is
harmless, and the parts need no merge, only concatenation.

So the tests are arranged around that claim and the ways it could fail:

    identity        many workers, awkward shard sizes, same bytes
    identity        with simulations, state folds, scenarios and chaos
    resilience      a worker that dies mid-shard, and the bytes still match
    routing         a shard needing a GPU never lands on a node without one
    staleness       a worker that comes back late cannot double-write

The lease protocol is tested through ``LocalTransport`` rather than a socket,
because it is the same protocol either way: ``HttpTransport`` is a JSON
encoding of these calls, and the HTTP tests below check the encoding, not the
protocol.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import time
from pathlib import Path
from typing import Any

import pytest

from cacophony.assets.store import AssetStore
from cacophony.core.errors import CacophonyError, OutputError
from cacophony.distributed import (
    Capabilities,
    Controller,
    LocalTransport,
    Shard,
    Worker,
    assemble,
    capabilities_for,
    plan_shards,
    shard_parts,
)
from cacophony.distributed.assembly import collect_assets
from cacophony.distributed.capabilities import WorkerProfile, describe_host
from cacophony.distributed.cluster import run_cluster
from cacophony.distributed.leases import Lease, LeaseState
from cacophony.generation.engine import GenerationEngine
from cacophony.outputs import create_writer
from cacophony.schema.compiler import compile_project
from cacophony.schema.loader import load_project
from helpers import TEMPLATES, make_project

# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def simple_project():
    """Three small entities, one of which references another."""
    return compile_project(
        make_project(
            {
                "team": {
                    "count": 6,
                    "fields": {
                        "id": {"generator": "sequence", "prefix": "TEAM-"},
                        "name": {"generator": "faker", "provider": "company"},
                    },
                },
                "member": {
                    "count": 40,
                    "fields": {
                        "id": {"generator": "sequence", "prefix": "M-"},
                        "name": {"generator": "faker", "provider": "name"},
                        "team": {"generator": "reference", "entity": "team", "field": "id"},
                    },
                },
            }
        )
    )


@pytest.fixture
def corporate():
    return compile_project(load_project(TEMPLATES / "corporate-directory.yaml"))


async def _single_machine(
    compiled, directory: Path, counts: dict[str, int], assets: Any | None = None
) -> dict[str, Path]:
    """The reference: one engine, one file per entity, start to finish."""
    engine = GenerationEngine(compiled, counts=counts, assets=assets)
    written: dict[str, Path] = {}
    for name in compiled.entity_order:
        entity = compiled.entity(name)
        path = directory / f"{name}.jsonl"
        writer = create_writer("jsonl", path, columns=entity.spec.field_names())
        await writer.open()
        async for chunk in engine.stream(name, count=counts[name], batch_size=250):
            if chunk.records:
                await writer.write_batch(chunk.records)
        await writer.close()
        written[name] = path
    return written


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# capabilities
# --------------------------------------------------------------------------- #


class TestCapabilities:
    def test_deterministic_is_always_present(self) -> None:
        """Every worker can do arithmetic; nothing has to say so."""
        assert "deterministic" in Capabilities.of([]).names
        assert "deterministic" in Capabilities.of(["image"]).names

    def test_satisfies_is_one_directional(self) -> None:
        """A worker may advertise more than a shard needs, never less."""
        node = Capabilities.of(["deterministic", "image", "language_model"])
        assert node.satisfies(Capabilities.of(["image"]))
        assert node.satisfies(Capabilities.of([]))
        assert not Capabilities.of(["deterministic"]).satisfies(Capabilities.of(["image"]))

    def test_missing_for_names_the_gap(self) -> None:
        gap = Capabilities.of(["deterministic"]).missing_for(Capabilities.of(["image", "speech"]))
        assert gap == frozenset({"image", "speech"})

    def test_unknown_capability_is_refused(self) -> None:
        with pytest.raises(ValueError, match="quantum"):
            Capabilities.of(["quantum"])

    def test_union(self) -> None:
        merged = Capabilities.of(["image"]) | Capabilities.of(["speech"])
        assert merged.names >= {"image", "speech", "deterministic"}

    def test_read_off_the_compiled_entity(self) -> None:
        """Requirements come from the generators, not from a declaration.

        A schema author never writes ``requires: image``; the compiler already
        knows which fields call a diffusion model.
        """
        compiled = compile_project(
            make_project(
                {
                    "person": {
                        "count": 2,
                        "fields": {
                            "name": {"generator": "faker", "provider": "name"},
                            "portrait": {
                                "generator": "image",
                                "provider": "art",
                                "prompt": "a face",
                            },
                        },
                    }
                },
                providers={
                    "art": {"type": "image", "adapter": "procedural", "base_url": "memory://"}
                },
            )
        )
        assert capabilities_for(compiled.entity("person")).names >= {"deterministic", "image"}

    def test_plain_entity_needs_nothing_special(self, simple_project) -> None:
        assert capabilities_for(simple_project.entity("member")).names == {"deterministic"}

    def test_profile_round_trip(self) -> None:
        profile = WorkerProfile(
            id="gpu-1",
            capabilities=Capabilities.of(["image", "speech"]),
            concurrency=4,
            host=describe_host(),
            schema_hash="abc123",
            version="0.1.0",
        )
        restored = WorkerProfile.from_dict(profile.to_dict())
        assert restored.id == "gpu-1"
        assert restored.capabilities.names == profile.capabilities.names
        assert restored.concurrency == 4
        assert restored.schema_hash == "abc123"

    def test_host_description_is_reportable(self) -> None:
        host = describe_host()
        assert host["hostname"]
        assert host["cpus"] >= 1


# --------------------------------------------------------------------------- #
# shards and leases
# --------------------------------------------------------------------------- #


class TestShardsAndLeases:
    def test_shards_tile_the_entity_exactly_once(self, corporate) -> None:
        """No gaps, no overlaps - the property the whole phase rests on."""
        counts = {"employee": 1000, "device": 700, "location": 5}
        shards = plan_shards(corporate, shard_size=137, counts=counts)

        for entity, total in counts.items():
            ranges = sorted((shard.offset, shard.end) for shard in shards if shard.entity == entity)
            assert ranges[0][0] == 0
            assert ranges[-1][1] == total
            for (_, end), (start, _) in itertools.pairwise(ranges):
                assert end == start

    def test_shard_round_trip(self) -> None:
        shard = Shard(entity="event", offset=4_000, count=500, requires=Capabilities.of(["image"]))
        restored = Shard.from_dict(shard.to_dict())
        assert (restored.entity, restored.offset, restored.count) == ("event", 4_000, 500)
        assert restored.requires.names == shard.requires.names
        assert restored.id == shard.id

    def test_grant_increments_the_generation(self) -> None:
        lease = Lease(shard=Shard(entity="e", offset=0, count=10))
        lease.grant("a")
        lease.release()
        lease.grant("b")
        assert lease.generation == 2
        assert lease.attempts == 2
        assert lease.held_by("b", 2)
        assert not lease.held_by("a", 1)

    def test_expiry_is_a_deadline_not_a_countdown(self) -> None:
        lease = Lease(shard=Shard(entity="e", offset=0, count=10))
        lease.grant("a", ttl=0.01)
        time.sleep(0.02)
        assert lease.is_expired
        lease.renew(ttl=5.0)
        assert not lease.is_expired

    def test_pending_lease_cannot_expire(self) -> None:
        assert not Lease(shard=Shard(entity="e", offset=0, count=1)).is_expired

    def test_terminal_states(self) -> None:
        assert LeaseState.COMPLETED.is_terminal
        assert LeaseState.FAILED.is_terminal
        assert not LeaseState.LEASED.is_terminal


# --------------------------------------------------------------------------- #
# the controller
# --------------------------------------------------------------------------- #


def _worker(controller: Controller, name: str, *what: str) -> WorkerProfile:
    profile = WorkerProfile(
        id=name,
        capabilities=Capabilities.of(what or ["deterministic"]),
        schema_hash=controller.schema_hash,
    )
    controller.register(profile)
    return profile


class TestController:
    def test_plans_every_entity(self, simple_project) -> None:
        controller = Controller(simple_project, shard_size=10)
        assert controller.total_records == 46
        assert len(controller.leases) == 1 + 4  # 6 members of one shard, 40 in four

    def test_counts_override_the_schema(self, simple_project) -> None:
        controller = Controller(simple_project, shard_size=100, counts={"team": 2, "member": 3})
        assert controller.total_records == 5

    def test_lease_requires_registration(self, simple_project) -> None:
        controller = Controller(simple_project)
        with pytest.raises(CacophonyError, match="has not registered"):
            controller.acquire("nobody")

    def test_a_different_schema_is_refused(self, simple_project) -> None:
        """Two schemas make a dataset that is neither."""
        controller = Controller(simple_project)
        with pytest.raises(CacophonyError, match="different schema"):
            controller.register(
                WorkerProfile(id="odd", capabilities=Capabilities.of([]), schema_hash="nonsense")
            )

    def test_dependencies_are_respected(self, simple_project) -> None:
        """A referencing entity waits for what it references."""
        controller = Controller(simple_project, shard_size=10)
        _worker(controller, "a")

        first = controller.acquire("a", count=10)
        assert {lease.shard.entity for lease in first} == {"team"}

        for lease in first:
            controller.complete("a", lease.shard.id, lease.generation, lease.shard.count)
        second = controller.acquire("a", count=10)
        assert {lease.shard.entity for lease in second} == {"member"}

    def test_routing_skips_shards_a_worker_cannot_do(self) -> None:
        """Section 84's whole scheduling rule."""
        compiled = compile_project(
            make_project(
                {
                    "plain": {
                        "count": 4,
                        "fields": {"name": {"generator": "faker", "provider": "name"}},
                    },
                    "illustrated": {
                        "count": 4,
                        "fields": {"art": {"generator": "image", "provider": "art", "prompt": "x"}},
                    },
                },
                providers={
                    "art": {"type": "image", "adapter": "procedural", "base_url": "memory://"}
                },
            )
        )
        controller = Controller(compiled, shard_size=2)
        _worker(controller, "cpu")
        _worker(controller, "gpu", "image")

        cpu = controller.acquire("cpu", count=10)
        assert {lease.shard.entity for lease in cpu} == {"plain"}

        gpu = controller.acquire("gpu", count=10)
        assert {lease.shard.entity for lease in gpu} == {"illustrated"}

    def test_unmet_capabilities_are_named(self) -> None:
        compiled = compile_project(
            make_project(
                {
                    "illustrated": {
                        "count": 2,
                        "fields": {"art": {"generator": "image", "provider": "art", "prompt": "x"}},
                    }
                },
                providers={
                    "art": {"type": "image", "adapter": "procedural", "base_url": "memory://"}
                },
            )
        )
        controller = Controller(compiled, shard_size=2)
        _worker(controller, "cpu")
        assert controller.unmet_requirements() == {"image"}
        assert controller.is_stalled

    def test_no_workers_at_all_is_stalled(self, simple_project) -> None:
        assert Controller(simple_project).is_stalled

    def test_an_expired_lease_goes_back_into_the_pool(self, simple_project) -> None:
        controller = Controller(simple_project, shard_size=100, lease_seconds=1.0)
        _worker(controller, "flaky")
        [lease] = controller.acquire("flaky")

        lease.expires_at = time.monotonic() - 1  # the worker went quiet
        reclaimed = controller.reclaim()

        assert reclaimed == [lease]
        assert lease.state is LeaseState.PENDING
        assert controller.stats.reassigned == 1
        assert controller.workers["flaky"].holding == set()

    def test_a_stale_worker_cannot_double_write(self, simple_project) -> None:
        """The reason the generation counter exists.

        The first worker comes back after its lease was reassigned and the
        second worker already finished. Its results are refused - and refusing
        them costs nothing, because they were the same bytes.
        """
        controller = Controller(simple_project, shard_size=100, lease_seconds=1.0)
        _worker(controller, "slow")
        _worker(controller, "fast")

        [first] = controller.acquire("slow")
        stale_generation = first.generation

        first.expires_at = time.monotonic() - 1
        controller.reclaim()
        [second] = controller.acquire("fast")
        assert second.shard.id == first.shard.id

        assert controller.complete("fast", second.shard.id, second.generation, 6)
        assert not controller.complete("slow", first.shard.id, stale_generation, 6)
        assert controller.stats.records == 6

    def test_a_resent_completion_is_not_counted_twice(self, simple_project) -> None:
        """The network will lose an acknowledgement eventually.

        A worker that resends ``complete`` because the reply went missing must
        not have its shard counted a second time.
        """
        controller = Controller(simple_project, shard_size=100)
        _worker(controller, "a")
        [lease] = controller.acquire("a")

        assert controller.complete("a", lease.shard.id, lease.generation, 6)
        assert not controller.complete("a", lease.shard.id, lease.generation, 6)
        assert not controller.renew("a", lease.shard.id, lease.generation)
        assert not controller.report_failure("a", lease.shard.id, lease.generation, "late")

        assert controller.stats.records == 6
        assert controller.stats.shards_completed == 1
        assert controller.progress <= 1.0

    def test_renewal_is_refused_once_the_lease_moved(self, simple_project) -> None:
        controller = Controller(simple_project, shard_size=100, lease_seconds=1.0)
        _worker(controller, "slow")
        _worker(controller, "fast")

        [lease] = controller.acquire("slow")
        generation = lease.generation
        lease.expires_at = time.monotonic() - 1
        controller.reclaim()
        controller.acquire("fast")

        assert not controller.renew("slow", lease.shard.id, generation)

    def test_a_shard_is_given_up_on_eventually(self, simple_project) -> None:
        """Section 66: never permit an infinite retry loop."""
        controller = Controller(simple_project, shard_size=100, max_attempts=2)
        _worker(controller, "broken")

        for _ in range(2):
            [lease] = controller.acquire("broken")
            controller.report_failure("broken", lease.shard.id, lease.generation, "no disk")

        assert lease.state is LeaseState.FAILED
        assert lease.error == "no disk"
        assert controller.stats.shards_failed == 1
        assert controller.failures() == [lease]

    def test_a_reported_failure_returns_the_shard_immediately(self, simple_project) -> None:
        controller = Controller(simple_project, shard_size=100, max_attempts=5)
        _worker(controller, "a")
        [lease] = controller.acquire("a")
        controller.report_failure("a", lease.shard.id, lease.generation, "transient")
        assert lease.state is LeaseState.PENDING
        assert controller.workers["a"].failures == 1

    def test_health_is_measured_in_silence(self, simple_project) -> None:
        # The controller will not believe a worker is dead sooner than its
        # lease expires, and a lease is never shorter than a second, so this
        # test waits one out.
        controller = Controller(simple_project, lease_seconds=1.0, worker_timeout=0.05)
        assert controller.worker_timeout == 1.0
        _worker(controller, "a")
        assert controller.alive_workers()
        time.sleep(1.05)
        assert not controller.alive_workers()
        controller.heartbeat("a")
        assert controller.alive_workers()

    def test_describe_is_serialisable(self, simple_project) -> None:
        import json

        controller = Controller(simple_project, shard_size=10)
        _worker(controller, "a")
        controller.acquire("a")
        payload = json.loads(json.dumps(controller.describe()))
        assert payload["shards"] == 5
        assert payload["workers"][0]["id"] == "a"
        assert payload["states"]["leased"] == 1

    def test_progress_reaches_one(self, simple_project) -> None:
        controller = Controller(simple_project, shard_size=100)
        _worker(controller, "a")
        while not controller.is_finished:
            granted = controller.acquire("a", count=4)
            assert granted, "a controller that is not finished must have work"
            for lease in granted:
                controller.complete("a", lease.shard.id, lease.generation, lease.shard.count)
        assert controller.progress == pytest.approx(1.0)
        assert controller.stats.records == 46


# --------------------------------------------------------------------------- #
# workers
# --------------------------------------------------------------------------- #


class TestWorker:
    def test_capabilities_come_from_configured_providers(self) -> None:
        compiled = compile_project(
            make_project(
                {
                    "person": {
                        "count": 1,
                        "fields": {"name": {"generator": "faker", "provider": "name"}},
                    }
                },
                providers={
                    "art": {"type": "image", "adapter": "procedural", "base_url": "memory://"},
                    "voice": {"type": "speech", "adapter": "procedural", "base_url": "memory://"},
                },
            )
        )
        node = Worker(compiled, LocalTransport(Controller(compiled)), output_dir="/tmp")
        assert node.capabilities.names >= {"deterministic", "image", "speech"}

    def test_unknown_output_format_is_refused(self, simple_project) -> None:
        with pytest.raises(CacophonyError, match="Unknown output format"):
            Worker(
                simple_project,
                LocalTransport(Controller(simple_project)),
                output_dir="/tmp",
                output_format="papyrus",
            )

    @pytest.mark.parametrize("fmt", ["sqlite", "sql"])
    def test_relational_formats_are_refused(self, simple_project, fmt: str) -> None:
        """A database split across shards is not a database.

        Each shard would be a separate file, and the foreign keys the format
        exists to enforce would not resolve.
        """
        with pytest.raises(CacophonyError, match="foreign keys would not resolve"):
            Worker(
                simple_project,
                LocalTransport(Controller(simple_project)),
                output_dir="/tmp",
                output_format=fmt,
            )

    def test_shard_files_are_named_after_the_offset(self, simple_project, tmp_path) -> None:
        node = Worker(
            simple_project, LocalTransport(Controller(simple_project)), output_dir=tmp_path
        )
        path = node.shard_path(Shard(entity="member", offset=50_000, count=1_000))
        assert path.name == "member.part000050000.jsonl"

    def test_a_worker_produces_what_it_leased(self, simple_project, tmp_path) -> None:
        controller = Controller(simple_project, shard_size=17)
        node = Worker(
            simple_project,
            LocalTransport(controller),
            output_dir=tmp_path,
            worker_id="solo",
            idle_timeout=0.0,
            poll_seconds=0.0,
        )
        stats = asyncio.run(node.run())

        assert stats.records == 46
        assert controller.is_finished
        assert len(stats.files) == len(controller.leases)

    def test_a_lost_lease_is_abandoned_not_finished(self, simple_project, tmp_path) -> None:
        """A worker that lost its shard must not write a second copy."""
        controller = Controller(simple_project, shard_size=100)
        node = Worker(
            simple_project,
            LocalTransport(controller),
            output_dir=tmp_path,
            worker_id="slow",
            idle_timeout=0.0,
            poll_seconds=0.0,
        )
        controller.register(node.profile)
        [lease] = controller.acquire("slow")
        payload = lease.to_dict()

        # While it was working, the controller took the shard back and somebody
        # else finished it.
        lease.release()
        _worker(controller, "fast")
        [reassigned] = controller.acquire("fast")
        controller.complete("fast", reassigned.shard.id, reassigned.generation, 6)

        result = asyncio.run(node._run_shard(payload))

        assert result.abandoned
        assert node.stats.abandoned == 1
        assert not list(tmp_path.glob("team.part*"))

    def test_a_failing_shard_is_reported(self, simple_project, tmp_path, monkeypatch) -> None:
        controller = Controller(simple_project, shard_size=100, max_attempts=1)
        node = Worker(
            simple_project,
            LocalTransport(controller),
            output_dir=tmp_path,
            worker_id="broken",
            idle_timeout=0.0,
            poll_seconds=0.0,
        )
        controller.register(node.profile)
        [lease] = controller.acquire("broken")

        async def explode(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("the disk is on fire")

        monkeypatch.setattr(node, "_generate", explode)
        result = asyncio.run(node._run_shard(lease.to_dict()))

        assert not result.ok
        assert "on fire" in (result.error or "")
        assert lease.state is LeaseState.FAILED
        assert controller.failures()


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #


class TestAssembly:
    def test_parts_sort_numerically(self, tmp_path) -> None:
        """Not lexically. A reordered dataset is a silent failure."""
        for offset in (0, 500_000, 1_000, 200):
            (tmp_path / f"e.part{offset:09d}.jsonl").write_text(f"{offset}\n")
        found = shard_parts(tmp_path, "e", ".jsonl")
        assert [path.name for path in found] == [
            "e.part000000000.jsonl",
            "e.part000000200.jsonl",
            "e.part000001000.jsonl",
            "e.part000500000.jsonl",
        ]

    def test_another_entity_is_not_swept_up(self, tmp_path) -> None:
        (tmp_path / "user.part000000000.jsonl").write_text("a\n")
        (tmp_path / "user_group.part000000000.jsonl").write_text("b\n")
        assert len(shard_parts(tmp_path, "user", ".jsonl")) == 1

    def test_a_part_without_a_final_newline_is_not_glued_on(self, tmp_path) -> None:
        (tmp_path / "e.part000000000.jsonl").write_text('{"a":1}')  # truncated writer
        (tmp_path / "e.part000000001.jsonl").write_text('{"a":2}\n')
        result = assemble(tmp_path, "e", "jsonl", destination=tmp_path / "joined.jsonl")
        assert result.path.read_text() == '{"a":1}\n{"a":2}\n'
        assert result.records == 2

    def test_csv_keeps_one_header(self, tmp_path) -> None:
        (tmp_path / "e.part000000000.csv").write_text("id,name\n1,a\n")
        (tmp_path / "e.part000000001.csv").write_text("id,name\n2,b\n")
        result = assemble(tmp_path, "e", "csv", destination=tmp_path / "joined.csv")
        assert result.path.read_text() == "id,name\n1,a\n2,b\n"
        assert result.records == 2

    def test_json_arrays_are_re_bracketed(self, tmp_path) -> None:
        (tmp_path / "e.part000000000.json").write_text('[{"a": 1}]')
        (tmp_path / "e.part000000001.json").write_text('[{"a": 2}]')
        result = assemble(tmp_path, "e", "json", destination=tmp_path / "joined.json")
        import json

        assert json.loads(result.path.read_text()) == [{"a": 1}, {"a": 2}]

    def test_parquet_is_not_pretended_into_one_file(self, tmp_path) -> None:
        with pytest.raises(OutputError, match="per-file footer"):
            assemble(tmp_path, "e", "parquet")

    def test_missing_parts_are_an_error_not_an_empty_file(self, tmp_path) -> None:
        with pytest.raises(OutputError, match="no e parts"):
            assemble(tmp_path, "e", "jsonl")

    def test_will_not_assemble_over_a_part(self, tmp_path) -> None:
        part = tmp_path / "e.part000000000.jsonl"
        part.write_text("x\n")
        with pytest.raises(OutputError, match="over one of themselves"):
            assemble(tmp_path, "e", "jsonl", destination=part)

    def test_remove_parts(self, tmp_path) -> None:
        (tmp_path / "e.part000000000.jsonl").write_text("a\n")
        assemble(tmp_path, "e", "jsonl", remove_parts=True)
        assert not shard_parts(tmp_path, "e", ".jsonl")

    def test_assets_are_gathered_by_content_address(self, tmp_path) -> None:
        """Two workers producing the same asset agree rather than collide."""
        for name in ("one", "two"):
            root = tmp_path / name / "ab"
            root.mkdir(parents=True)
            (root / "abcdef.png").write_bytes(b"same bytes")
        (tmp_path / "two" / "ab" / "999999.png").write_bytes(b"other")

        copied = collect_assets([tmp_path / "one", tmp_path / "two"], tmp_path / "shared")

        assert copied == 2
        assert (tmp_path / "shared" / "ab" / "abcdef.png").read_bytes() == b"same bytes"
        assert (tmp_path / "shared" / "ab" / "999999.png").exists()


# --------------------------------------------------------------------------- #
# the claim
# --------------------------------------------------------------------------- #


class TestByteIdentity:
    """Many workers, one dataset, the same bytes."""

    def test_distributed_output_matches_single_machine(self, corporate, tmp_path) -> None:
        counts = {"employee": 600, "device": 700, "location": 5}
        one, many = tmp_path / "one", tmp_path / "many"
        one.mkdir()

        reference = asyncio.run(_single_machine(corporate, one, counts))
        result = asyncio.run(
            run_cluster(
                corporate,
                output_dir=many,
                workers=4,
                # Deliberately not a round number, and not a multiple of the
                # batch size: shard boundaries must not be able to land
                # anywhere interesting.
                shard_size=137,
                batch_size=64,
                counts=counts,
            )
        )

        assert result.ok
        assert result.records == sum(counts.values())
        for name in corporate.entity_order:
            joined = assemble(many, name, "jsonl", destination=many / f"{name}.joined.jsonl")
            assert _digest(joined.path) == _digest(reference[name]), name

    @pytest.mark.slow
    def test_identical_with_simulations_scenarios_and_chaos(self, tmp_path) -> None:
        """The hard case: state folds, timelines, scenarios and deliberate damage.

        A stateful simulation is a fold over one subject's contiguous block
        (section 26), so a shard boundary that falls inside a block forces a
        replay. If any of that were sensitive to *where the run started*, this
        is the test that would say so.
        """
        compiled = compile_project(load_project(TEMPLATES / "security-operations.yaml"))
        # Every entity, because the reference run walks `entity_order` - and
        # the template has twelve of them now.
        counts = dict.fromkeys(compiled.entity_order, 120)
        counts.update(
            {
                "user": 200,
                "device": 300,
                "authentication": 2_500,
                "security_finding": 400,
                "endpoint_event": 800,
                "network_connection": 800,
            }
        )
        one, many = tmp_path / "one", tmp_path / "many"
        one.mkdir()

        reference = asyncio.run(_single_machine(compiled, one, counts))
        asyncio.run(
            run_cluster(
                compiled, output_dir=many, workers=3, shard_size=311, batch_size=50, counts=counts
            )
        )

        for name in compiled.entity_order:
            joined = assemble(many, name, "jsonl", destination=many / f"{name}.joined.jsonl")
            assert _digest(joined.path) == _digest(reference[name]), name

    def test_identical_after_a_worker_dies_mid_shard(self, corporate, tmp_path) -> None:
        """A shard is redone, not resumed - and nobody can tell.

        The dying worker leaves a half-written file behind, because a process
        that is killed does not get to clean up. The replacement writes to the
        same offset-named path, so the partial file is overwritten rather than
        left to corrupt the dataset.
        """
        counts = {"employee": 400, "device": 400, "location": 5}
        one, many = tmp_path / "one", tmp_path / "many"
        one.mkdir()
        many.mkdir()
        reference = asyncio.run(_single_machine(corporate, one, counts))

        controller = Controller(corporate, shard_size=97, lease_seconds=30.0, counts=counts)
        transport = LocalTransport(controller)

        # A worker that takes a shard, writes part of it, and is never heard
        # from again.
        casualty = Worker(
            corporate,
            transport,
            output_dir=many,
            worker_id="casualty",
            counts=counts,
            batch_size=32,
            idle_timeout=0.0,
            poll_seconds=0.0,
        )
        controller.register(casualty.profile)
        [doomed] = controller.acquire("casualty")
        path = casualty.shard_path(doomed.shard)
        path.write_text('{"truncated": true}\n')  # what a kill -9 leaves behind

        doomed.expires_at = time.monotonic() - 1
        controller.reclaim()
        assert controller.stats.reassigned == 1

        survivor = Worker(
            corporate,
            transport,
            output_dir=many,
            worker_id="survivor",
            counts=counts,
            batch_size=32,
            idle_timeout=0.0,
            poll_seconds=0.0,
        )
        asyncio.run(survivor.run())

        assert controller.is_finished
        for name in corporate.entity_order:
            joined = assemble(many, name, "jsonl", destination=many / f"{name}.joined.jsonl")
            assert _digest(joined.path) == _digest(reference[name]), name

    def test_media_is_identical_across_workers(self, tmp_path) -> None:
        """Shared artifact storage, verified on the artifacts.

        Assets are content-addressed and their bytes come from the record's
        seed, so several nodes writing into one directory produce exactly the
        files one node would have. The only thing that legitimately differs is
        where the directory *is*, which is an input to the run.
        """
        compiled = compile_project(load_project(TEMPLATES / "multimodal-support.yaml"))
        counts = dict.fromkeys(compiled.entity_order, 12)
        one, many = tmp_path / "one", tmp_path / "many"
        one.mkdir()

        reference = asyncio.run(
            _single_machine(compiled, one, counts, assets=AssetStore(one / "assets"))
        )
        asyncio.run(
            run_cluster(
                compiled,
                output_dir=many,
                workers=3,
                shard_size=5,
                batch_size=4,
                counts=counts,
                assets=AssetStore(many / "assets"),
            )
        )

        for name in compiled.entity_order:
            joined = assemble(many, name, "jsonl", destination=many / f"{name}.joined.jsonl")
            assert joined.path.read_bytes().replace(str(many).encode(), b"<root>") == reference[
                name
            ].read_bytes().replace(str(one).encode(), b"<root>"), name

        produced = {
            path.relative_to(one / "assets"): _digest(path)
            for path in (one / "assets").rglob("*")
            if path.is_file() and "manifest" not in path.name
        }
        distributed = {
            path.relative_to(many / "assets"): _digest(path)
            for path in (many / "assets").rglob("*")
            if path.is_file() and "manifest" not in path.name
        }
        assert produced and produced == distributed

    def test_one_worker_and_many_workers_agree(self, simple_project, tmp_path) -> None:
        """Worker count is not an input to the data."""
        digests = []
        for workers in (1, 2, 7):
            directory = tmp_path / f"w{workers}"
            asyncio.run(
                run_cluster(simple_project, output_dir=directory, workers=workers, shard_size=7)
            )
            joined = assemble(directory, "member", "jsonl")
            digests.append(_digest(joined.path))
        assert len(set(digests)) == 1


# --------------------------------------------------------------------------- #
# the cluster
# --------------------------------------------------------------------------- #


class TestCluster:
    def test_reports_per_worker(self, simple_project, tmp_path) -> None:
        result = asyncio.run(
            run_cluster(simple_project, output_dir=tmp_path, workers=3, shard_size=5)
        )
        assert result.ok
        assert result.records == 46
        assert len(result.workers) == 3
        assert sum(worker["records"] for worker in result.workers) == 46
        assert result.status["finished"]

    def test_progress_is_published_while_it_runs(self, simple_project, tmp_path) -> None:
        seen: list[dict[str, Any]] = []
        asyncio.run(
            run_cluster(
                simple_project,
                output_dir=tmp_path,
                workers=2,
                shard_size=5,
                on_progress=seen.append,
                progress_interval=0.01,
            )
        )
        assert seen
        assert seen[-1]["progress"] == pytest.approx(1.0)

    def test_an_empty_entity_makes_no_shards(self) -> None:
        compiled = compile_project(
            make_project(
                {
                    "nothing": {
                        "count": 0,
                        "fields": {"a": {"generator": "faker", "provider": "name"}},
                    }
                }
            )
        )
        controller = Controller(compiled)
        assert not controller.leases
        assert controller.is_finished
        assert not controller.is_stalled


# --------------------------------------------------------------------------- #
# the wire
# --------------------------------------------------------------------------- #


@pytest.fixture
def http_controller(simple_project):
    """A controller behind its real HTTP routes."""
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from cacophony.distributed.service import create_controller_app

    controller = Controller(simple_project, shard_size=10, lease_seconds=5.0)
    app = create_controller_app(controller, token="hunter2")
    assert fastapi
    with TestClient(app) as client:
        yield controller, client


class TestHttpProtocol:
    def _register(self, client, controller, name="a", capabilities=("deterministic",)):
        return client.post(
            "/register",
            headers={"Authorization": "Bearer hunter2"},
            json={
                "id": name,
                "capabilities": list(capabilities),
                "schema_hash": controller.schema_hash,
            },
        )

    def test_the_whole_protocol_over_json(self, http_controller) -> None:
        controller, client = http_controller
        auth = {"Authorization": "Bearer hunter2"}

        assert self._register(client, controller).status_code == 200

        leased = client.post("/lease", headers=auth, json={"worker_id": "a", "count": 4}).json()
        # Only ``team`` is available: ``member`` references it and waits.
        assert [lease["entity"] for lease in leased["leases"]] == ["team"]
        first = leased["leases"][0]

        renewed = client.post(
            "/renew",
            headers=auth,
            json={
                "worker_id": "a",
                "shard_id": first["id"],
                "generation": first["generation"],
            },
        ).json()
        assert renewed["held"]

        done = client.post(
            "/complete",
            headers=auth,
            json={
                "worker_id": "a",
                "shard_id": first["id"],
                "generation": first["generation"],
                "records": first["count"],
            },
        ).json()
        assert done["accepted"]

        # The same call again is refused: the shard is no longer this worker's.
        again = client.post(
            "/complete",
            headers=auth,
            json={
                "worker_id": "a",
                "shard_id": first["id"],
                "generation": first["generation"],
                "records": first["count"],
            },
        ).json()
        assert not again["accepted"]
        assert controller.stats.records == first["count"]

    def test_failure_is_reported_over_the_wire(self, http_controller) -> None:
        controller, client = http_controller
        auth = {"Authorization": "Bearer hunter2"}
        self._register(client, controller)
        lease = client.post("/lease", headers=auth, json={"worker_id": "a"}).json()["leases"][0]

        response = client.post(
            "/fail",
            headers=auth,
            json={
                "worker_id": "a",
                "shard_id": lease["id"],
                "generation": lease["generation"],
                "reason": "no GPU",
            },
        )
        assert response.json()["accepted"]
        assert controller.leases[lease["id"]].error == "no GPU"

    def test_a_token_is_required(self, http_controller) -> None:
        _controller, client = http_controller
        assert client.post("/register", json={"id": "rogue"}).status_code == 401
        assert (
            client.post(
                "/register", headers={"Authorization": "Bearer wrong"}, json={"id": "rogue"}
            ).status_code
            == 401
        )

    def test_a_mismatched_schema_is_a_readable_error(self, http_controller) -> None:
        _controller, client = http_controller
        response = client.post(
            "/register",
            headers={"Authorization": "Bearer hunter2"},
            json={"id": "odd", "schema_hash": "deadbeef"},
        )
        assert response.status_code == 400
        assert "different schema" in response.json()["detail"]

    def test_status_and_health_need_no_token(self, http_controller) -> None:
        _controller, client = http_controller
        assert client.get("/health").json()["ok"]
        assert client.get("/status").json()["shards"] == 5

    def test_shards_can_be_filtered(self, http_controller) -> None:
        controller, client = http_controller
        self._register(client, controller)
        client.post("/lease", headers={"Authorization": "Bearer hunter2"}, json={"worker_id": "a"})
        assert len(client.get("/shards", params={"state": "leased"}).json()) == 1
        assert len(client.get("/shards").json()) == 5

    def test_a_worker_runs_a_whole_project_over_http(self, http_controller, tmp_path) -> None:
        """The transport is an encoding, not a different protocol."""
        controller, client = http_controller

        class ClientTransport:
            """The HTTP protocol, driven through Starlette's test client."""

            def __init__(self) -> None:
                self.auth = {"Authorization": "Bearer hunter2"}

            async def register(self, profile):
                return client.post("/register", headers=self.auth, json=profile.to_dict()).json()

            async def lease(self, worker_id, count=1):
                payload = {"worker_id": worker_id, "count": count}
                return client.post("/lease", headers=self.auth, json=payload).json()["leases"]

            async def renew(self, worker_id, shard_id, generation):
                payload = {
                    "worker_id": worker_id,
                    "shard_id": shard_id,
                    "generation": generation,
                }
                return client.post("/renew", headers=self.auth, json=payload).json()["held"]

            async def complete(self, worker_id, shard_id, generation, records, **extra):
                payload = {
                    "worker_id": worker_id,
                    "shard_id": shard_id,
                    "generation": generation,
                    "records": records,
                }
                return client.post("/complete", headers=self.auth, json=payload).json()["accepted"]

            async def fail(self, worker_id, shard_id, generation, reason):
                payload = {
                    "worker_id": worker_id,
                    "shard_id": shard_id,
                    "generation": generation,
                    "reason": reason,
                }
                return client.post("/fail", headers=self.auth, json=payload).json()["accepted"]

            async def status(self):
                return client.get("/status").json()

            async def close(self):
                return None

        node = Worker(
            controller.compiled,
            ClientTransport(),
            output_dir=tmp_path,
            worker_id="remote",
            idle_timeout=0.0,
            poll_seconds=0.0,
        )
        stats = asyncio.run(node.run())

        assert stats.records == 46
        assert controller.is_finished
