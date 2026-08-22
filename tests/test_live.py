"""Live generation (design document sections 35, 94).

    Produce approximately 250 authentication events/sec, 50 endpoint
    events/sec, 8 alerts/minute. This transforms Cacophony into a workload
    generator.

A workload generator is only worth having if it produces the workload it was
asked for, so most of what is checked here is *attainment*: does a stream
configured for 250/s actually deliver 250/s, does a slow destination slow the
stream rather than fill memory, and does a stream that says "12,000 delivered"
mean it.

The rate tests use short durations and generous tolerances on purpose. A test
suite that fails when the machine running it is briefly busy is a test suite
people learn to ignore.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from cacophony.core.errors import CacophonyError, OutputError, SchemaError
from cacophony.core.record import GeneratedRecord
from cacophony.live import LiveStream, StreamConfig, create_sink, parse_rate
from cacophony.live.rates import Rate, RateLimiter
from cacophony.live.sinks import (
    SINK_TYPES,
    FileStreamSink,
    HttpSink,
    KafkaSink,
    StreamSink,
    SyslogSink,
)
from cacophony.schema.compiler import compile_project
from helpers import make_project

# --------------------------------------------------------------------------- #
# Rates
# --------------------------------------------------------------------------- #


class TestRateParsing:
    @pytest.mark.parametrize(
        ("text", "per_second"),
        [
            ("250/s", 250.0),
            ("250 per second", 250.0),
            ("8 per minute", 8 / 60),
            ("8/min", 8 / 60),
            ("1200/hour", 1200 / 3600),
            ("0.5/s", 0.5),
            ("30", 30.0),
            ("2/day", 2 / 86400),
        ],
    )
    def test_the_ways_people_write_a_rate(self, text: str, per_second: float) -> None:
        assert parse_rate(text).per_second == pytest.approx(per_second)

    def test_it_keeps_the_words_it_was_given(self) -> None:
        """A dashboard should echo "8 per minute", not "0.13/s"."""
        assert parse_rate("8 per minute").render() == "8 per minute"

    def test_a_rate_with_no_source_renders_sensibly(self) -> None:
        assert Rate(per_second=250).render() == "250/s"
        assert Rate(per_second=0.5).render() == "30/min"
        assert Rate(per_second=0.001).render() == "3.6/hour"

    def test_nonsense_is_refused_with_an_example(self) -> None:
        with pytest.raises(SchemaError, match="250/s"):
            parse_rate("as fast as possible")

    def test_an_unknown_unit_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="unit"):
            parse_rate("5/fortnight")

    def test_a_negative_rate_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="negative"):
            Rate(per_second=-1)


class TestRateLimiter:
    def test_it_issues_nothing_before_time_passes(self) -> None:
        assert RateLimiter(parse_rate("100/s")).take(50) == 0

    def test_tokens_accrue_with_the_clock(self) -> None:
        import time

        limiter = RateLimiter(parse_rate("1000/s"))
        time.sleep(0.05)
        assert 30 <= limiter.take(1000) <= 70

    def test_the_bucket_has_a_ceiling(self) -> None:
        """A stream paused for an hour must not emit an hour of events at once."""
        import time

        limiter = RateLimiter(parse_rate("1000/s"), burst_seconds=0.05)
        time.sleep(0.3)
        assert limiter.take(10_000) <= limiter.ceiling

    def test_a_zero_rate_issues_nothing(self) -> None:
        import time

        limiter = RateLimiter(parse_rate("0/s"))
        time.sleep(0.02)
        assert limiter.take(100) == 0

    def test_retargeting_takes_effect_immediately(self) -> None:
        import time

        limiter = RateLimiter(parse_rate("10000/s"), burst_seconds=1.0)
        time.sleep(0.05)
        limiter.retarget(parse_rate("1/s"))
        # The backlog built at the old rate must not survive the slow-down.
        assert limiter.take(10_000) <= 2

    def test_wait_time_reflects_the_rate(self) -> None:
        assert RateLimiter(parse_rate("10/s")).wait_time() == pytest.approx(0.1, abs=0.02)


# --------------------------------------------------------------------------- #
# Sinks
# --------------------------------------------------------------------------- #


def some_records(count: int = 3) -> list[GeneratedRecord]:
    return [
        GeneratedRecord(entity="login", id=f"L{index}", values={"id": index, "user": "u1"})
        for index in range(count)
    ]


class TestSinkSelection:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("stdout", "stdout"),
            ("file", "file"),
            ("file:///tmp/x.jsonl", "file"),
            ("syslog://localhost:514", "syslog"),
            ("syslog+tcp://localhost:601", "syslog"),
            ("http://localhost:8080/ingest", "http"),
            ("https://example.com/ingest", "http"),
            ("kafka://localhost:9092/events", "kafka"),
        ],
    )
    def test_a_destination_can_be_written_as_a_uri(self, spec: str, expected: str) -> None:
        assert create_sink(spec).name == expected

    def test_a_uri_carries_its_details(self) -> None:
        sink = create_sink("syslog+tcp://siem.internal:601")
        assert isinstance(sink, SyslogSink)
        assert (sink.host, sink.port, sink.protocol) == ("siem.internal", 601, "tcp")

    def test_a_mapping_works_too(self) -> None:
        sink = create_sink({"type": "file", "path": "/tmp/a.jsonl", "rotate_bytes": 10})
        assert isinstance(sink, FileStreamSink)
        assert sink.rotate_bytes == 10

    def test_an_unknown_destination_lists_the_known_ones(self) -> None:
        with pytest.raises(OutputError, match="syslog"):
            create_sink("carrier-pigeon")

    def test_an_unknown_scheme_is_refused(self) -> None:
        with pytest.raises(OutputError, match="scheme"):
            create_sink("gopher://example.com")

    def test_http_needs_a_url(self) -> None:
        with pytest.raises(OutputError, match="url"):
            HttpSink()

    def test_kafka_needs_a_topic(self) -> None:
        with pytest.raises(OutputError, match="topic"):
            KafkaSink()

    def test_every_registered_sink_implements_the_interface(self) -> None:
        for name, sink_class in SINK_TYPES.items():
            assert issubclass(sink_class, StreamSink), name
            assert sink_class.name == name


class TestFileSink:
    def test_it_appends_one_json_object_per_line(self, tmp_path: Path) -> None:
        sink = create_sink({"type": "file", "path": str(tmp_path / "s.jsonl")})
        asyncio.run(sink.open())
        asyncio.run(sink.send(some_records(4)))
        asyncio.run(sink.close())

        lines = (tmp_path / "s.jsonl").read_text().strip().split("\n")
        assert len(lines) == 4
        assert json.loads(lines[0])["id"] == 0
        assert sink.stats.delivered == 4

    def test_it_rotates_so_a_stream_does_not_fill_a_disk(self, tmp_path: Path) -> None:
        """The difference between a stream sink and a file writer."""
        sink = create_sink(
            {"type": "file", "path": str(tmp_path / "s.jsonl"), "rotate_bytes": 200, "keep": 3}
        )
        asyncio.run(sink.open())
        for _ in range(8):
            asyncio.run(sink.send(some_records(4)))
        asyncio.run(sink.close())

        rolled = list(tmp_path.glob("s.*.jsonl"))
        assert rolled, "the file should have been rotated"
        assert len(rolled) <= 3, "old files should be pruned"

    def test_a_failure_is_counted_not_raised(self, tmp_path: Path) -> None:
        sink = FileStreamSink(path=str(tmp_path / "s.jsonl"))
        asyncio.run(sink.open())
        sink._handle.close()  # simulate the file going away underneath us
        asyncio.run(sink.send(some_records(2)))
        assert sink.stats.failed == 2
        assert sink.stats.last_error


class TestDatabaseSink:
    """Section 35's database destination: a stream a dashboard can query."""

    def test_it_creates_a_table_and_inserts(self, tmp_path: Path) -> None:
        import sqlite3

        path = tmp_path / "live.db"
        sink = create_sink({"type": "database", "path": str(path)})
        asyncio.run(sink.open())
        asyncio.run(sink.send(some_records(5)))
        asyncio.run(sink.close())

        connection = sqlite3.connect(path)
        rows = connection.execute("select id, user from login order by id").fetchall()
        connection.close()
        assert rows == [(index, "u1") for index in range(5)]
        assert sink.stats.delivered == 5

    def test_each_flush_is_its_own_transaction(self, tmp_path: Path) -> None:
        """A stream has no end; one open transaction would be one lost stream."""
        import sqlite3

        path = tmp_path / "live.db"
        sink = create_sink({"type": "database", "path": str(path)})
        asyncio.run(sink.open())
        asyncio.run(sink.send(some_records(3)))

        # Read from a *different* connection while the sink is still running.
        connection = sqlite3.connect(path)
        visible = connection.execute("select count(*) from login").fetchone()[0]
        connection.close()
        asyncio.run(sink.close())
        assert visible == 3

    def test_one_table_per_entity(self, tmp_path: Path) -> None:
        import sqlite3

        records = [
            GeneratedRecord(entity="login", id="L1", values={"id": 1}),
            GeneratedRecord(entity="alert", id="A1", values={"id": 1, "severity": "high"}),
        ]
        sink = create_sink({"type": "database", "path": str(tmp_path / "live.db")})
        asyncio.run(sink.open())
        asyncio.run(sink.send(records))
        asyncio.run(sink.close())

        connection = sqlite3.connect(tmp_path / "live.db")
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
        connection.close()
        assert {"login", "alert"} <= tables

    def test_a_uri_names_the_file_and_optionally_the_table(self, tmp_path: Path) -> None:
        sink = create_sink(f"db://{tmp_path / 'live.db'}#events")
        assert sink.name == "database"
        assert sink.table == "events"

    def test_a_structured_value_is_stored_as_json(self, tmp_path: Path) -> None:
        import sqlite3

        record = GeneratedRecord(entity="login", id="L1", values={"tags": ["a", "b"]})
        sink = create_sink({"type": "database", "path": str(tmp_path / "live.db")})
        asyncio.run(sink.open())
        asyncio.run(sink.send([record]))
        asyncio.run(sink.close())

        connection = sqlite3.connect(tmp_path / "live.db")
        stored = connection.execute("select tags from login").fetchone()[0]
        connection.close()
        assert json.loads(stored) == ["a", "b"]


class TestSyslogFraming:
    def test_rfc_5424(self) -> None:
        sink = SyslogSink(host="h", app_name="app")
        message = sink.frame(some_records(1)[0])
        assert message.startswith("<134>1 ")
        assert " app " in message
        assert json.loads(message[message.index("{") :])["id"] == 0

    def test_rfc_3164(self) -> None:
        sink = SyslogSink(rfc="3164", app_name="app")
        assert sink.frame(some_records(1)[0]).startswith("<134>")

    def test_the_priority_is_facility_times_eight_plus_severity(self) -> None:
        sink = SyslogSink(facility=1, severity=3)
        assert sink.frame(some_records(1)[0]).startswith("<11>")

    def test_it_reaches_a_real_collector(self) -> None:
        """Framing that only Cacophony can read is not syslog."""
        received: list[bytes] = []
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        server.settimeout(3)
        port = server.getsockname()[1]

        def listen() -> None:
            for _ in range(3):
                try:
                    received.append(server.recvfrom(65535)[0])
                except TimeoutError:
                    return

        thread = threading.Thread(target=listen, daemon=True)
        thread.start()

        sink = create_sink(f"syslog://127.0.0.1:{port}")
        asyncio.run(sink.open())
        asyncio.run(sink.send(some_records(3)))
        asyncio.run(sink.close())
        thread.join(timeout=3)
        server.close()

        assert len(received) == 3
        assert received[0].startswith(b"<134>1 ")
        assert sink.stats.delivered == 3


class TestHttpSink:
    def test_ndjson_is_the_default_body(self) -> None:
        sink = HttpSink(url="http://x/ingest")
        payload, content_type = sink._payload(some_records(3))
        assert content_type == "application/x-ndjson"
        assert len(payload.decode().strip().split("\n")) == 3

    def test_an_array_body(self) -> None:
        sink = HttpSink(url="http://x/ingest", body="array")
        payload, content_type = sink._payload(some_records(3))
        assert content_type == "application/json"
        assert len(json.loads(payload)) == 3


# --------------------------------------------------------------------------- #
# The stream
# --------------------------------------------------------------------------- #


class CountingSink(StreamSink):
    """Accepts everything and remembers the batch sizes."""

    name = "counting"

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.batches: list[int] = []
        self.records: list[GeneratedRecord] = []

    async def send(self, records: Any) -> int:
        self.batches.append(len(records))
        self.records.extend(records)
        return self._note(len(records), 0, 0)


class RefusingSink(StreamSink):
    """Rejects everything, the way a downed collector does."""

    name = "refusing"

    async def send(self, records: Any) -> int:
        return self._note(0, len(records), 0, "destination unavailable")


LOGINS: dict[str, Any] = {
    "user": {
        "count": 50,
        "primary_key": "user_id",
        "fields": {"user_id": {"type": "integer", "generator": "sequence"}},
    },
    "login": {
        "count": 1000,
        "simulation": {"subject": "user", "distribution": "skewed"},
        "fields": {
            "login_id": {"type": "integer", "generator": "sequence"},
            "user": {"generator": "subject"},
            "at": {"type": "datetime", "generator": "event_time"},
        },
    },
}


def logins_project() -> Any:
    return compile_project(
        make_project(LOGINS, timeline={"start": "2026-01-01", "end": "2026-12-31"})
    )


def stream_for(rates: dict[str, str], **config: Any) -> tuple[LiveStream, CountingSink]:
    sink = CountingSink()
    settings = StreamConfig(
        rates={name: parse_rate(rate) for name, rate in rates.items()},
        sinks=[sink],
        **config,
    )
    return LiveStream(logins_project(), settings), sink


class TestStreamControl:
    def test_an_unknown_entity_is_refused(self) -> None:
        with pytest.raises(CacophonyError, match="ghost"):
            stream_for({"ghost": "10/s"})

    def test_a_stream_with_no_rates_is_refused(self) -> None:
        with pytest.raises(CacophonyError, match="rate"):
            stream_for({})

    def test_retargeting_changes_the_rate(self) -> None:
        stream, _sink = stream_for({"login": "10/s"})
        stream.retarget("login", "500/s")
        assert stream.streams["login"].rate.per_second == 500
        assert stream.config.rates["login"].per_second == 500

    def test_retargeting_an_unstreamed_entity_is_refused(self) -> None:
        stream, _sink = stream_for({"login": "10/s"})
        with pytest.raises(CacophonyError, match="user"):
            stream.retarget("user", "5/s")

    def test_stopping_before_it_starts(self) -> None:
        stream, sink = stream_for({"login": "100/s"}, duration_seconds=5.0)
        stream.stop()
        asyncio.run(stream.run())
        assert stream.stats.generated == 0
        assert sink.stats.delivered == 0


class TestStreamOutput:
    def test_max_records_is_exact(self) -> None:
        stream, sink = stream_for(
            {"login": "5000/s"}, max_records=250, duration_seconds=10.0, batch_size=32
        )
        asyncio.run(stream.run())
        assert stream.stats.generated == 250
        assert sink.stats.delivered == 250

    def test_records_are_delivered_in_batches_not_one_at_a_time(self) -> None:
        """250 deliveries a second to an HTTP endpoint is a denial of service."""
        stream, sink = stream_for(
            {"login": "2000/s"}, max_records=1000, duration_seconds=10.0, batch_size=100
        )
        asyncio.run(stream.run())
        assert sink.batches
        assert max(sink.batches) > 1
        assert len(sink.batches) < 100, "should not be one delivery per record"

    def test_no_batch_exceeds_the_configured_size(self) -> None:
        stream, sink = stream_for(
            {"login": "5000/s"}, max_records=900, duration_seconds=10.0, batch_size=50
        )
        asyncio.run(stream.run())
        assert max(sink.batches) <= 50

    def test_indices_continue_rather_than_repeat(self) -> None:
        stream, sink = stream_for({"login": "5000/s"}, max_records=200, duration_seconds=10.0)
        asyncio.run(stream.run())
        ids = [record.values["login_id"] for record in sink.records]
        assert len(set(ids)) == len(ids), "a stream must not repeat records"

    def test_it_can_resume_where_a_previous_stream_stopped(self) -> None:
        first, sink_a = stream_for({"login": "5000/s"}, max_records=100, duration_seconds=10.0)
        asyncio.run(first.run())
        reached = first.streams["login"].index

        second, sink_b = stream_for(
            {"login": "5000/s"}, max_records=100, duration_seconds=10.0, start_index=reached
        )
        asyncio.run(second.run())

        seen = {r.values["login_id"] for r in sink_a.records}
        assert not seen & {r.values["login_id"] for r in sink_b.records}

    def test_events_are_spread_across_subjects(self) -> None:
        """A stream's events interleave; they do not all belong to subject zero."""
        stream, sink = stream_for({"login": "5000/s"}, max_records=500, duration_seconds=10.0)
        asyncio.run(stream.run())
        subjects = {record.values["user"] for record in sink.records}
        assert len(subjects) > 10

    def test_live_time_stamps_events_with_the_wall_clock(self) -> None:
        import datetime as dt

        stream, sink = stream_for({"login": "5000/s"}, max_records=20, duration_seconds=10.0)
        asyncio.run(stream.run())
        now = dt.datetime.now()
        for record in sink.records:
            assert abs((now - record.values["at"]).total_seconds()) < 60

    def test_historical_time_keeps_the_generated_timestamp(self) -> None:
        stream, sink = stream_for(
            {"login": "5000/s"}, max_records=20, duration_seconds=10.0, live_time=False
        )
        asyncio.run(stream.run())
        assert all(record.values["at"].year == 2026 for record in sink.records)

    def test_several_entities_stream_at_their_own_rates(self) -> None:
        stream = stream_for(
            {"login": "400/s", "user": "100/s"}, duration_seconds=1.5, batch_size=50
        )[0]
        asyncio.run(stream.run())
        counts = stream.stats.by_entity
        assert counts["login"] > counts["user"] * 2


@pytest.mark.slow
class TestAttainment:
    """Does a stream produce the rate it was asked for (section 35)?"""

    @pytest.mark.parametrize("rate", ["50/s", "500/s"])
    def test_the_requested_rate_is_met(self, rate: str) -> None:
        stream = stream_for({"login": rate}, duration_seconds=2.0, batch_size=64)[0]
        stats = asyncio.run(stream.run())
        assert stats.attainment == pytest.approx(1.0, abs=0.15)

    def test_attainment_is_reported_when_it_cannot_keep_up(self) -> None:
        """A workload generator that quietly under-delivers measures nothing."""
        stream = stream_for({"login": "5000000/s"}, duration_seconds=1.0, batch_size=500)[0]
        stats = asyncio.run(stream.run())
        assert stats.attainment < 0.5
        assert stats.to_dict()["target_records_per_second"] == 5_000_000


class TestFailureHandling:
    def test_a_refused_delivery_is_counted_not_fatal(self) -> None:
        sink = RefusingSink()
        config = StreamConfig(
            rates={"login": parse_rate("2000/s")},
            sinks=[sink],
            max_records=100,
            duration_seconds=5.0,
        )
        stream = LiveStream(logins_project(), config)
        asyncio.run(stream.run())

        assert stream.state == "completed"
        assert stream.stats.generated == 100
        assert stream.stats.dropped == 100
        assert sink.stats.failed == 100

    def test_abort_stops_the_stream(self) -> None:
        config = StreamConfig(
            rates={"login": parse_rate("2000/s")},
            sinks=[RefusingSink()],
            max_records=100,
            duration_seconds=5.0,
            on_error="abort",
        )
        stream = LiveStream(logins_project(), config)
        with pytest.raises(CacophonyError, match="rejected"):
            asyncio.run(stream.run())
        assert stream.state == "failed"

    def test_one_failing_destination_does_not_stop_another(self) -> None:
        good = CountingSink()
        config = StreamConfig(
            rates={"login": parse_rate("2000/s")},
            sinks=[RefusingSink(), good],
            max_records=60,
            duration_seconds=5.0,
        )
        asyncio.run(LiveStream(logins_project(), config).run())
        assert good.stats.delivered == 60


class TestDescription:
    def test_a_stream_describes_itself(self) -> None:
        stream, _sink = stream_for({"login": "250/s"}, max_records=50, duration_seconds=5.0)
        asyncio.run(stream.run())
        described = stream.describe()

        assert described["state"] == "completed"
        assert described["config"]["rates"] == {"login": "250/s"}
        assert described["stats"]["generated"] == 50
        assert described["streams"][0]["entity"] == "login"
        assert described["sinks"][0]["delivered"] == 50


class TestStreamCli:
    def test_rates_are_parsed_from_the_command_line(self) -> None:
        from cacophony.cli.stream import parse_rates

        compiled = logins_project()
        rates = parse_rates(["login=250/s", "user=1 per minute"], compiled)
        assert rates["login"].per_second == 250
        assert rates["user"].per_second == pytest.approx(1 / 60)

    def test_a_bare_rate_applies_to_the_last_entity(self) -> None:
        from cacophony.cli.stream import parse_rates

        compiled = logins_project()
        rates = parse_rates(["100/s"], compiled)
        assert list(rates) == [compiled.entity_order[-1]]

    def test_an_unknown_entity_lists_the_known_ones(self) -> None:
        from cacophony.cli.stream import parse_rates

        with pytest.raises(CacophonyError, match="login"):
            parse_rates(["ghost=1/s"], logins_project())


class TestRetargetedAttainment:
    """Attainment when the request itself changes (section 94).

    A stream turned up from 200/s to 800/s has not been failing for the first
    ten minutes - it was not asked for 800/s then. The denominator therefore
    integrates the request over time rather than assuming it was constant, or
    the number the whole dashboard is built around reads 400%.
    """

    def test_the_denominator_follows_the_request(self) -> None:
        from cacophony.live.stream import StreamStats

        stats = StreamStats()
        stats.set_target(100.0)
        stats._target_since -= 10.0  # ten seconds at 100/s: 1,000 owed
        stats.note("e", 1_000)
        assert stats.attainment == pytest.approx(1.0, abs=0.02)

        stats.set_target(400.0)
        stats._target_since -= 10.0  # ten more at 400/s: 4,000 more owed
        stats.note("e", 4_000)
        assert stats.expected == pytest.approx(5_000, rel=0.02)
        assert stats.attainment == pytest.approx(1.0, abs=0.02)

    def test_turning_a_stream_up_does_not_invent_attainment(self) -> None:
        """The bug this replaced: a lifetime mean over the newest target."""
        from cacophony.live.stream import StreamStats

        stats = StreamStats()
        stats.set_target(200.0)
        stats.started_at -= 10.0
        stats._target_since -= 10.0
        stats.note("e", 2_000)

        stats.set_target(800.0)
        # Immediately after the change almost nothing is owed at the new rate,
        # so attainment stays at 1.0 rather than jumping to 2,000/800 = 250%.
        assert stats.attainment == pytest.approx(1.0, abs=0.05)

    def test_a_shortfall_is_still_reported(self) -> None:
        from cacophony.live.stream import StreamStats

        stats = StreamStats()
        stats.set_target(1_000.0)
        stats._target_since -= 10.0
        stats.note("e", 5_000)
        assert stats.attainment == pytest.approx(0.5, abs=0.02)

    def test_retarget_updates_the_target_on_the_stream(self) -> None:
        stream, _sink = stream_for({"login": "100/s"})
        stream.stats.set_target(100.0)
        stream.retarget("login", "400/s")
        assert stream.stats.target_rate == 400.0
        assert stream.stats.to_dict()["target_records_per_second"] == 400.0
