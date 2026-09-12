"""Window orchestration: open a window, close it, keep the raw capture.

IMPURE. Requires a cluster. Everything it produces is verbatim; all
summarising happens later in :mod:`quiet.probe.parse`.

``begin`` exists only because two things cannot be reconstructed after
the fact: container restart counts need a reading at both edges to be
differenced, and the pod census at window open is what distinguishes an
in-place restart from a rollout replacing the pod. Everything else is
fetched retrospectively by timestamp at ``end``.

Every source is wrapped: a failure appends to ``collection_errors`` and
leaves that channel empty, which downgrades it to unusable during
scoring. One dead source must never cost a whole run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .model import RawWindow, WindowLabel, WindowSpec
from .sources import jaeger, kube, prom
from .sources.portforward import PortForward

#: Do not go finer than the Prometheus scrape interval (1m in the
#: vendored chart): extra points would be interpolation, not observation.
DEFAULT_PROM_STEP = 60


@dataclass
class WindowHandle:
    spec_fields: dict
    t_start: datetime
    pods_at_start: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class Endpoints:
    """Long-lived connections, opened once per run rather than per query."""

    jaeger_base_url: str | None = None
    prometheus_base_url: str | None = None


class ClusterCollector:
    def __init__(
        self,
        namespace: str,
        endpoints: Endpoints,
        *,
        prom_step: int = DEFAULT_PROM_STEP,
    ) -> None:
        self.namespace = namespace
        self.endpoints = endpoints
        self.prom_step = prom_step

    def begin(
        self, *, run_id: str, problem_id: str, variant: str, label: WindowLabel
    ) -> WindowHandle:
        handle = WindowHandle(
            spec_fields={
                "run_id": run_id,
                "problem_id": problem_id,
                "variant": variant,
                "label": label,
                "namespace": self.namespace,
            },
            t_start=datetime.now(timezone.utc),
        )
        try:
            handle.pods_at_start, _ = kube.pod_census(self.namespace)
        except Exception as exc:  # noqa: BLE001
            handle.errors.append(f"pod_census@start: {exc!r}")
        return handle

    def end(self, handle: WindowHandle) -> RawWindow:
        t_end = datetime.now(timezone.utc)
        spec = WindowSpec(**handle.spec_fields, t_start=handle.t_start, t_end=t_end)
        raw = RawWindow(spec=spec, pods_at_start=handle.pods_at_start)
        raw.collection_errors.extend(handle.errors)

        try:
            raw.pods_at_end, raw.not_ready_at_end = kube.pod_census(self.namespace)
        except Exception as exc:  # noqa: BLE001
            raw.collection_errors.append(f"pod_census@end: {exc!r}")

        try:
            raw.events = kube.get_events(self.namespace)
        except Exception as exc:  # noqa: BLE001
            raw.collection_errors.append(f"events: {exc!r}")

        try:
            raw.logs, raw.truncated_pods = kube.get_logs(
                self.namespace, handle.t_start
            )
        except Exception as exc:  # noqa: BLE001
            raw.collection_errors.append(f"logs: {exc!r}")

        if self.endpoints.jaeger_base_url:
            try:
                raw.spans, problems = jaeger.fetch_spans(
                    self.endpoints.jaeger_base_url, handle.t_start, t_end
                )
                raw.collection_errors.extend(problems)
            except Exception as exc:  # noqa: BLE001
                raw.collection_errors.append(f"jaeger: {exc!r}")
        else:
            raw.collection_errors.append("jaeger: no endpoint configured")

        if self.endpoints.prometheus_base_url:
            try:
                raw.resource_series, problems = prom.fetch_series(
                    self.endpoints.prometheus_base_url,
                    self.namespace,
                    handle.t_start,
                    t_end,
                    step=self.prom_step,
                )
                raw.collection_errors.extend(problems)
            except Exception as exc:  # noqa: BLE001
                raw.collection_errors.append(f"prometheus: {exc!r}")
        else:
            raw.collection_errors.append("prometheus: no endpoint configured")

        return raw


def open_endpoints(namespace: str) -> tuple[Endpoints, list[PortForward]]:
    """Open the forwards a run needs. Caller must close what comes back.

    Jaeger lives in the application namespace; Prometheus is installed by
    the orchestrator into ``observe`` as a NodePort service. Both are
    reached by forward rather than NodePort so this works identically on
    kind, on a nested cluster and on a remote box.
    """
    forwards: list[PortForward] = []
    endpoints = Endpoints()

    # Service naming varies by chart version and release name. Measured on
    # opentelemetry-demo 0.37.2: the query service is `jaeger-query`, which
    # neither of the names the upstream TraceAPI looks for would have found.
    # Ordered most-likely-first; the first one that answers /api/services wins.
    for svc in ("svc/jaeger-query", "svc/jaeger",
                "svc/astronomy-shop-jaeger-query", "svc/jaeger-out"):
        fwd = PortForward(svc, 16686, namespace, probe_path="/api/services")
        try:
            fwd.__enter__()
            endpoints.jaeger_base_url = fwd.base_url
            forwards.append(fwd)
            break
        except Exception:  # noqa: BLE001 - try the next name
            fwd.close()

    prom_fwd = PortForward(
        "svc/prometheus-server", 80, "observe", probe_path="/-/ready"
    )
    try:
        prom_fwd.__enter__()
        endpoints.prometheus_base_url = prom_fwd.base_url
        forwards.append(prom_fwd)
    except Exception:  # noqa: BLE001
        prom_fwd.close()

    return endpoints, forwards


def close_all(forwards: list[PortForward]) -> None:
    for fwd in forwards:
        fwd.close()
