"""The pure parts of the Jaeger source.

The fetch itself needs a cluster, but error detection and deduplication
decide every error RATE the metric channel computes, so they are pulled
out and tested here.
"""

from __future__ import annotations

import pytest

from quiet.probe.model import SpanRecord
from quiet.probe.sources.jaeger import dedupe_spans, span_has_error


def tags(**kw) -> dict:
    return {"tags": [{"key": k, "value": v} for k, v in kw.items()]}


class TestSpanErrorDetection:
    def test_clean_span(self):
        assert span_has_error(tags(**{"http.status_code": 200})) is False

    def test_no_tags_at_all(self):
        assert span_has_error({}) is False

    @pytest.mark.parametrize("value", [True, "true", "True"])
    def test_error_tag_in_its_various_encodings(self, value):
        assert span_has_error({"tags": [{"key": "error", "value": value}]}) is True

    def test_error_tag_false_is_not_an_error(self):
        assert span_has_error({"tags": [{"key": "error", "value": False}]}) is False

    def test_otel_status_code(self):
        assert span_has_error(tags(**{"otel.status_code": "ERROR"})) is True
        assert span_has_error(tags(**{"otel.status_code": "OK"})) is False

    def test_http_5xx_only(self):
        assert span_has_error(tags(**{"http.status_code": 500})) is True
        assert span_has_error(tags(**{"http.status_code": 503})) is True
        assert span_has_error(tags(**{"http.status_code": 404})) is False, (
            "a 4xx is the caller's fault, not a service failure"
        )

    def test_newer_otel_http_attribute_name(self):
        assert span_has_error(tags(**{"http.response.status_code": 502})) is True

    def test_grpc_nonzero_status(self):
        assert span_has_error(tags(**{"rpc.grpc.status_code": 14})) is True
        assert span_has_error(tags(**{"rpc.grpc.status_code": 0})) is False

    def test_unparseable_status_is_not_an_error(self):
        """A malformed tag must not manufacture errors -- that would
        inflate the fault window and fake explicitness."""
        assert span_has_error(tags(**{"http.status_code": "n/a"})) is False
        assert span_has_error(tags(**{"rpc.grpc.status_code": None})) is False


class TestDeduplication:
    def span(self, service="payment", start=1.0, dur=2.0, op="charge") -> SpanRecord:
        return SpanRecord(
            service=service, operation=op, start_ms=start, duration_ms=dur
        )

    def test_identical_spans_collapse(self):
        assert len(dedupe_spans([self.span(), self.span(), self.span()])) == 1

    def test_distinct_spans_survive(self):
        spans = [self.span(start=1.0), self.span(start=2.0), self.span(service="cart")]
        assert len(dedupe_spans(spans)) == 3

    def test_order_is_preserved(self):
        spans = [self.span(start=3.0), self.span(start=1.0), self.span(start=3.0)]
        assert [s.start_ms for s in dedupe_spans(spans)] == [3.0, 1.0]

    def test_empty_input(self):
        assert dedupe_spans([]) == []

    def test_duplication_would_distort_error_rates(self):
        """Why this matters: a shared span counted once per participating
        service skews the rate, and not by a constant factor -- fan-out
        differs per service."""
        shared_error = SpanRecord(
            service="payment", operation="charge", start_ms=1.0,
            duration_ms=2.0, has_error=True,
        )
        clean = [
            SpanRecord(service="payment", operation="charge", start_ms=float(i),
                       duration_ms=2.0)
            for i in range(2, 12)
        ]
        # The same failing span returned by three service queries.
        raw = [shared_error] * 3 + clean
        assert sum(s.has_error for s in raw) / len(raw) == pytest.approx(3 / 13)
        unique = dedupe_spans(raw)
        assert sum(s.has_error for s in unique) / len(unique) == pytest.approx(1 / 11)
