"""Check the cluster before spending an hour on a capture.

IMPURE. Runs on the lab machine.

Two jobs, both on the critical path for the first session:

1. **Fail in two minutes instead of seventy.** A capture is warmup +
   two windows; if Jaeger is unreachable or the flag variants are not
   what the dose ladder assumes, that is discovered at the end. This
   checks the same things first.

2. **Measure the three numbers the window length depends on.** POWER.md
   concluded that the shape of the dose-response curve hinges on two
   unmeasured parameters, and refused to fix the window length until
   they were known. Those numbers are here -- spans per minute per
   service, steady-state error rate, and the charge fraction of the
   payment service -- so the ladder can be set from measurement rather
   than guessed, before any long run.

Run it, read the last section, put the numbers into POWER.md, re-run the
power simulation, then start the capture.
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

OK = "OK  "
WARN = "WARN"
FAIL = "FAIL"

#: The dose ladder is meaningless if the deployed chart does not offer these.
EXPECTED_VARIANTS = {"off", "10%", "25%", "50%", "75%", "90%", "100%"}


class Check:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.failed = False

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))
        if status == FAIL:
            self.failed = True
        print(f"[{status}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def run(args: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or p.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, repr(exc)


def check_cluster(c: Check, namespace: str) -> None:
    code, out = run(["kubectl", "config", "current-context"])
    if code != 0:
        c.add(FAIL, "kubectl context", "no context set — is KUBECONFIG exported?")
        return
    c.add(OK, "kubectl context", out)

    code, out = run(["kubectl", "get", "ns", namespace, "-o", "name"])
    if code != 0:
        c.add(FAIL, f"namespace {namespace}", "not found — deploy the app first")
        return
    c.add(OK, f"namespace {namespace}")

    code, out = run(["kubectl", "get", "pods", "-n", namespace, "-o", "json"])
    if code != 0:
        c.add(FAIL, "pods", out[:200])
        return
    items = json.loads(out).get("items", [])
    if not items:
        c.add(FAIL, "pods", "namespace is empty")
        return
    not_ready = []
    restarts = 0
    for it in items:
        st = it.get("status") or {}
        cs = st.get("containerStatuses") or []
        restarts += sum(int(s.get("restartCount", 0)) for s in cs)
        if not cs or not all(s.get("ready") for s in cs):
            not_ready.append(it["metadata"]["name"])
    if not_ready:
        c.add(FAIL, "pods ready", f"{len(not_ready)} not ready: {not_ready[:5]}")
    else:
        c.add(OK, "pods ready", f"{len(items)} pods")
    # Restarts are not fatal, but a baseline that is already churning
    # means the event channel starts dirty.
    c.add(OK if restarts == 0 else WARN, "restart count", str(restarts))


def check_flagd(c: Check, namespace: str) -> None:
    code, out = run(
        ["kubectl", "get", "cm", "flagd-config", "-n", namespace, "-o", "json"]
    )
    if code != 0:
        c.add(WARN, "flagd-config", "absent (fine for hotel-reservation)")
        return
    try:
        data = json.loads(out)["data"]["demo.flagd.json"]
        flags = json.loads(data)["flags"]
    except Exception as exc:  # noqa: BLE001
        c.add(FAIL, "flagd-config", f"unparseable: {exc!r}")
        return

    dirty = [k for k, v in flags.items() if v.get("defaultVariant") not in (None, "off")]
    if dirty:
        c.add(FAIL, "flag baseline", f"not off: {dirty} — previous run leaked")
    else:
        c.add(OK, "flag baseline", f"{len(flags)} flags all off")

    pf = flags.get("paymentFailure")
    if pf is None:
        c.add(FAIL, "paymentFailure", "flag absent — chart version wrong?")
        return
    got = set(pf.get("variants", {}))
    missing = EXPECTED_VARIANTS - got
    if missing:
        c.add(
            FAIL,
            "dose ladder",
            f"missing {sorted(missing)} — check the chart version pin (0.37.2)",
        )
    else:
        c.add(OK, "dose ladder", f"{len(got)} variants present")


def check_chaos(c: Check) -> None:
    kinds = "podchaos,networkchaos,stresschaos,iochaos,httpchaos"
    code, out = run(["kubectl", "get", kinds, "-A", "-o", "name"])
    if code != 0:
        c.add(OK, "chaos CRs", "Chaos Mesh not installed")
        return
    leftovers = [ln for ln in out.splitlines() if ln.strip()]
    if leftovers:
        c.add(FAIL, "chaos CRs", f"{len(leftovers)} left over — baseline contaminated")
    else:
        c.add(OK, "chaos CRs", "none")


def measure(c: Check, namespace: str, minutes: float) -> dict:
    """Sample live telemetry and return the numbers POWER.md needs."""
    from ..probe.collect import close_all, open_endpoints
    from ..probe.sources import jaeger, prom

    stats: dict = {}
    endpoints, forwards = open_endpoints(namespace)
    try:
        if not endpoints.jaeger_base_url:
            c.add(FAIL, "jaeger", "no endpoint — check the service name")
            return stats
        c.add(OK, "jaeger", endpoints.jaeger_base_url)

        if endpoints.prometheus_base_url:
            c.add(OK, "prometheus", endpoints.prometheus_base_url)
            end = datetime.now(timezone.utc)
            series, problems = prom.fetch_series(
                endpoints.prometheus_base_url, namespace,
                end - timedelta(minutes=minutes), end,
            )
            c.add(
                OK if series else WARN,
                "prometheus series",
                f"{len(series)} series" + (f"; {problems[:1]}" if problems else ""),
            )
        else:
            c.add(WARN, "prometheus", "no endpoint — resource channel unusable")

        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=minutes)
        spans, problems = jaeger.fetch_spans(endpoints.jaeger_base_url, start, end)
        for p in problems[:3]:
            c.add(WARN, "jaeger fetch", p[:160])
        if not spans:
            c.add(FAIL, "spans", "zero spans — no traffic, or sampling is off")
            return stats
        c.add(OK, "spans", f"{len(spans)} over {minutes:g} min")

        by_service = collections.Counter(s.service for s in spans)
        errors = sum(1 for s in spans if s.has_error)
        stats["minutes"] = minutes
        stats["total_spans"] = len(spans)
        stats["baseline_error_rate"] = errors / len(spans)
        stats["spans_per_min"] = {k: v / minutes for k, v in by_service.items()}

        pay = [s for s in spans if s.service == "payment"]
        charge = [s for s in pay if "charge" in s.operation.lower()]
        stats["payment_spans"] = len(pay)
        stats["charge_fraction"] = (len(charge) / len(pay)) if pay else None
        return stats
    finally:
        close_all(forwards)


def report(stats: dict) -> None:
    if not stats:
        return
    print("\n" + "=" * 62)
    print("POWER.md 가 요구한 값 — 이 셋을 넣고 창 길이를 확정한다")
    print("=" * 62)

    spm = stats["spans_per_min"]
    print("\n서비스별 분당 span (상위 12):")
    for svc, rate in sorted(spm.items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {svc:<34} {rate:9.1f}/min")

    err = stats["baseline_error_rate"]
    print(f"\n기저 에러율: {err:.4%}")
    if err > 0.05:
        print("  ⚠ 5% 초과 — 10% 용량이 묻힌다. 사다리 하단을 버리거나 창을 크게 늘려야 한다")
    else:
        print("  OK — 저용량이 관측 가능한 범위")

    cf = stats.get("charge_fraction")
    pay_rate = spm.get("payment", 0.0)
    if cf is None:
        print("\ncharge_fraction: payment span 없음 — 워크로드가 결제를 안 태우고 있다")
    else:
        print(f"\npayment span {stats['payment_spans']}건 중 charge 비율: {cf:.1%}")
        if cf >= 0.25:
            print("  OK — 10% 용량도 명시적으로 보일 것이다 (곡선 상단이 포화될 수 있음)")
        elif cf >= 0.10:
            print("  경계 — 무릎 구간이다. 창을 넉넉히 잡는다")
        else:
            print("  ⚠ 낮다 — 100% 용량조차 조용할 수 있다. 창을 크게 늘려야 한다")

    if pay_rate > 0 and cf:
        for target in (1000, 2000, 5000):
            need = target / pay_rate
            print(f"  payment span {target}개를 모으려면 창 {need:.1f}분")

    print("\n다음: 위 값을 POWER.md 에 적고")
    print("      python -m quiet.analysis.power  를 다시 돌린 뒤 창 길이를 확정한다.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Check the cluster and measure the parameters the window length depends on."
    )
    ap.add_argument("--namespace", default="astronomy-shop")
    ap.add_argument("--minutes", type=float, default=3.0,
                    help="sampling window for the measurements (default 3)")
    ap.add_argument("--skip-measure", action="store_true",
                    help="checks only, no telemetry sampling")
    args = ap.parse_args(argv)

    c = Check()
    print(f"=== preflight: {args.namespace} ===\n")
    check_cluster(c, args.namespace)
    check_flagd(c, args.namespace)
    check_chaos(c)

    stats = {}
    if not c.failed and not args.skip_measure:
        print(f"\n--- {args.minutes:g}분 표본 수집 ---")
        stats = measure(c, args.namespace, args.minutes)

    print()
    if c.failed:
        print("FAIL — 위 항목을 고친 뒤 캡처를 시작한다. 지금 돌리면 70분을 버린다.")
        return 1
    report(stats)
    print("\npreflight 통과.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
