"""Parser tests, especially for the quirks that fail silently."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quiet.probe.model import RawLogLine, RawWindow, SpanRecord, WindowSpec
from quiet.probe.parse import (
    classify_line,
    event_time,
    parse_events,
    parse_logs,
    parse_window,
    subwindows,
)

T0 = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


def spec(minutes: float = 10.0, *, start: datetime = T0) -> WindowSpec:
    return WindowSpec(
        run_id="r0",
        problem_id="payment_dose_10-detection-1",
        variant="10%",
        label="fault",
        namespace="astronomy-shop",
        t_start=start,
        t_end=start + timedelta(minutes=minutes),
    )


def iso(offset_min: float) -> str:
    return (T0 + timedelta(minutes=offset_min)).isoformat().replace("+00:00", "Z")


class TestEventTimestamps:
    """A naive lastTimestamp filter returns zero events on a modern
    cluster, and zero events looks exactly like a quiet failure."""

    def test_reads_core_v1_last_timestamp(self):
        assert event_time({"lastTimestamp": iso(1)}) == T0 + timedelta(minutes=1)

    def test_reads_events_k8s_io_event_time(self):
        assert event_time({"eventTime": iso(2)}) == T0 + timedelta(minutes=2)

    def test_reads_series_last_observed_time(self):
        ev = {"series": {"lastObservedTime": iso(3)}, "lastTimestamp": None}
        assert event_time(ev) == T0 + timedelta(minutes=3)

    def test_falls_back_to_creation_timestamp(self):
        ev = {"metadata": {"creationTimestamp": iso(4)}}
        assert event_time(ev) == T0 + timedelta(minutes=4)

    def test_returns_none_when_nothing_parses(self):
        assert event_time({"lastTimestamp": "not-a-date"}) is None

    def test_modern_events_are_actually_counted(self):
        """The regression this whole class exists for."""
        raw = RawWindow(
            spec=spec(),
            events=[
                {"type": "Warning", "reason": "BackOff", "eventTime": iso(5)},
                {"type": "Warning", "reason": "Unhealthy", "eventTime": iso(6)},
            ],
        )
        stats = parse_events(raw)
        assert stats.warning_count == 2, "eventTime-only events must not vanish"


class TestEventCounting:
    def test_honours_the_repeat_count(self):
        raw = RawWindow(
            spec=spec(),
            events=[{"type": "Warning", "reason": "BackOff", "count": 17, "lastTimestamp": iso(1)}],
        )
        assert parse_events(raw).warning_count == 17

    def test_events_outside_the_window_are_dropped(self):
        raw = RawWindow(
            spec=spec(minutes=5),
            events=[
                {"type": "Warning", "reason": "A", "lastTimestamp": iso(1)},
                {"type": "Warning", "reason": "B", "lastTimestamp": iso(99)},
            ],
        )
        assert parse_events(raw).warning_count == 1

    def test_separates_warning_from_normal(self):
        raw = RawWindow(
            spec=spec(),
            events=[
                {"type": "Warning", "reason": "A", "lastTimestamp": iso(1)},
                {"type": "Normal", "reason": "Pulled", "lastTimestamp": iso(2)},
            ],
        )
        stats = parse_events(raw)
        assert (stats.warning_count, stats.normal_count) == (1, 1)


class TestRestartsVersusChurn:
    """A rollout replaces pods; it does not restart containers. Confusing
    the two makes the injection tool look like the fault."""

    def test_rollout_is_churn_not_restarts(self):
        raw = RawWindow(
            spec=spec(),
            pods_at_start={"flagd-old": {"flagd": 0}},
            pods_at_end={"flagd-new": {"flagd": 0}},
        )
        stats = parse_events(raw)
        assert stats.restart_delta == 0, "a replaced pod is not a restart"
        assert stats.pod_churn == 2, "one pod gone, one appeared"

    def test_in_place_restart_is_counted(self):
        raw = RawWindow(
            spec=spec(),
            pods_at_start={"payment-0": {"payment": 1}},
            pods_at_end={"payment-0": {"payment": 4}},
        )
        stats = parse_events(raw)
        assert stats.restart_delta == 3
        assert stats.pod_churn == 0

    def test_new_container_without_a_baseline_is_ignored(self):
        raw = RawWindow(
            spec=spec(),
            pods_at_start={"payment-0": {"payment": 2}},
            pods_at_end={"payment-0": {"payment": 2, "sidecar": 5}},
        )
        assert parse_events(raw).restart_delta == 0

    def test_counter_reset_never_goes_negative(self):
        raw = RawWindow(
            spec=spec(),
            pods_at_start={"payment-0": {"payment": 7}},
            pods_at_end={"payment-0": {"payment": 0}},
        )
        assert parse_events(raw).restart_delta == 0


class TestLogClassification:
    @pytest.mark.parametrize(
        "line,expected",
        [
            ("PANIC: nil pointer", "fatal"),
            ("level=error msg=charge failed", "error"),
            ("connection refused", None),
            ("GET /cart HTTP/1.1 200", None),
            ('{"status": 503}', "http_5xx"),
            ("rpc error: code = Unavailable", "grpc_error"),
            ("everything is fine", None),
        ],
    )
    def test_patterns(self, line, expected):
        assert classify_line(line) == expected

    def test_specific_rules_beat_the_generic_one(self):
        """The generic 'error' rule matches almost every specific line
        too, so ordering it first made grpc_error and http_5xx dead code."""
        assert classify_line("rpc error: code = Internal") == "grpc_error"
        assert classify_line('error status=500') == "http_5xx"

    def test_case_insensitive(self):
        assert classify_line("ERROR") == classify_line("error")

    def test_counts_each_line_once(self):
        raw = RawWindow(
            spec=spec(),
            logs=[RawLogLine(pod="p", ts=iso(1), text="fatal error exception failed")],
        )
        stats = parse_logs(raw)
        assert stats.error_lines == 1
        assert sum(stats.patterns.values()) == 1

    def test_per_pod_rates_are_tracked(self):
        raw = RawWindow(
            spec=spec(),
            logs=[
                RawLogLine(pod="a", ts=iso(1), text="error boom"),
                RawLogLine(pod="a", ts=iso(2), text="all good"),
                RawLogLine(pod="b", ts=iso(3), text="all good"),
            ],
        )
        stats = parse_logs(raw)
        assert stats.per_pod == {"a": (1, 2), "b": (0, 1)}

    def test_truncation_is_flagged(self):
        raw = RawWindow(spec=spec(), truncated_pods=["payment-0"])
        assert parse_logs(raw).truncated is True

    def test_untimestamped_lines_are_kept(self):
        """A line without a timestamp cannot be windowed out; dropping it
        would silently shrink the denominator."""
        raw = RawWindow(spec=spec(), logs=[RawLogLine(pod="p", ts="", text="error x")])
        assert parse_logs(raw).total_lines == 1


class TestTraces:
    def _raw(self) -> RawWindow:
        base = T0.timestamp() * 1000.0
        spans = [
            SpanRecord(
                service="payment",
                start_ms=base + i * 1000,
                duration_ms=20.0,
                has_error=(i % 10 == 0),
            )
            for i in range(100)
        ]
        spans += [
            SpanRecord(service="cart", start_ms=base + i * 1000, duration_ms=10.0)
            for i in range(100)
        ]
        return RawWindow(spec=spec(minutes=10), spans=spans)

    def test_groups_by_service(self):
        traces = parse_window(self._raw()).traces
        assert set(traces.per_service) == {"payment", "cart"}
        assert traces.per_service["payment"].error_spans == 10
        assert traces.per_service["cart"].error_spans == 0

    def test_total_aggregates_all_services(self):
        traces = parse_window(self._raw()).traces
        assert traces.total.spans == 200
        assert traces.total.error_spans == 10

    def test_spans_outside_the_window_are_dropped(self):
        raw = self._raw()
        narrow = raw.spec.model_copy(
            update={"t_end": raw.spec.t_start + timedelta(seconds=50)}
        )
        traces = parse_window(raw, spec=narrow).traces
        assert traces.total.spans < 200


class TestSubwindows:
    """The offline window-length axis: re-cut one capture, no cluster."""

    def _long_capture(self) -> RawWindow:
        base = T0.timestamp() * 1000.0
        return RawWindow(
            spec=spec(minutes=30),
            spans=[
                SpanRecord(service="payment", start_ms=base + i * 1000, duration_ms=20.0)
                for i in range(1800)
            ],
        )

    def test_splits_into_consecutive_windows(self):
        windows = subwindows(self._long_capture(), minutes=10, count=3)
        assert len(windows) == 3
        assert windows[0].spec.t_end == windows[1].spec.t_start

    def test_shorter_windows_hold_fewer_spans(self):
        raw = self._long_capture()
        short = subwindows(raw, minutes=5, count=1)[0]
        long_ = subwindows(raw, minutes=20, count=1)[0]
        assert short.traces.total.spans < long_.traces.total.spans

    def test_refuses_to_exceed_the_capture(self):
        with pytest.raises(ValueError, match="too short"):
            subwindows(self._long_capture(), minutes=20, count=3)

    def test_rejects_nonsense_arguments(self):
        with pytest.raises(ValueError):
            subwindows(self._long_capture(), minutes=0, count=1)


class TestResources:
    def test_variance_needs_two_points(self):
        base = T0.timestamp()
        raw = RawWindow(
            spec=spec(),
            resource_series={"p|cpu": [(base + 1, 0.5)]},
        )
        series = parse_window(raw).resources.series["p|cpu"]
        assert series.n == 1 and series.var == 0.0

    def test_sample_variance_is_computed(self):
        base = T0.timestamp()
        raw = RawWindow(
            spec=spec(),
            resource_series={"p|cpu": [(base + i, v) for i, v in enumerate([1.0, 2.0, 3.0])]},
        )
        series = parse_window(raw).resources.series["p|cpu"]
        assert series.mean == pytest.approx(2.0)
        assert series.var == pytest.approx(1.0)

    def test_points_outside_the_window_are_dropped(self):
        base = T0.timestamp()
        raw = RawWindow(
            spec=spec(minutes=1),
            resource_series={"p|cpu": [(base + 10, 1.0), (base + 9999, 99.0)]},
        )
        assert parse_window(raw).resources.series["p|cpu"].n == 1


class TestParseWindow:
    def test_rejects_a_zero_length_window(self):
        raw = RawWindow(spec=spec())
        bad = raw.spec.model_copy(update={"t_end": raw.spec.t_start})
        with pytest.raises(ValueError, match="positive duration"):
            parse_window(raw, spec=bad)

    def test_collection_errors_are_carried_through(self):
        raw = RawWindow(spec=spec(), collection_errors=["jaeger: timeout"])
        assert parse_window(raw).collection_errors == ["jaeger: timeout"]

    def test_round_trips_through_json(self):
        raw = RawWindow(
            spec=spec(),
            logs=[RawLogLine(pod="p", ts=iso(1), text="error")],
            spans=[SpanRecord(service="payment", start_ms=1.0, duration_ms=2.0)],
        )
        again = RawWindow.model_validate_json(raw.model_dump_json())
        assert again.spans[0].service == "payment"
        assert again.logs[0].text == "error"
