"""A port-forward that stays open for a whole run.

IMPURE. Requires a cluster.

Held open once per run rather than per query. The upstream observer APIs
open and close a forward around each call -- and worse, ``TraceAPI``
calls ``cleanup()`` from inside ``extract_traces`` and again from
``save_traces``, so it is effectively single-use. Repeated setup and
teardown is the main source of flakiness in that code.

The local port is chosen by the OS (bind 0) and read back from kubectl's
own output, which avoids the upstream ``PrometheusAPI`` bug of picking a
free port, forwarding to it, and then querying a different hardcoded one.
"""

from __future__ import annotations

import re
import subprocess
import time
import urllib.error
import urllib.request

_FORWARD_RE = re.compile(r"Forwarding from 127\.0\.0\.1:(\d+)")


class PortForwardError(RuntimeError):
    pass


class PortForward:
    def __init__(
        self,
        target: str,
        remote_port: int,
        namespace: str,
        *,
        ready_timeout: float = 30.0,
        probe_path: str | None = None,
    ) -> None:
        self.target = target
        self.remote_port = remote_port
        self.namespace = namespace
        self.ready_timeout = ready_timeout
        self.probe_path = probe_path
        self.local_port: int | None = None
        self._proc: subprocess.Popen[str] | None = None

    @property
    def base_url(self) -> str:
        if self.local_port is None:
            raise PortForwardError("port-forward is not open")
        return f"http://127.0.0.1:{self.local_port}"

    def __enter__(self) -> "PortForward":
        cmd = [
            "kubectl", "port-forward", self.target,
            f":{self.remote_port}",  # empty local port => OS picks one
            "-n", self.namespace,
        ]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )

        deadline = time.monotonic() + self.ready_timeout
        assert self._proc.stdout is not None
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                if self._proc.poll() is not None:
                    raise PortForwardError(
                        f"port-forward to {self.target} exited immediately"
                    )
                continue
            match = _FORWARD_RE.search(line)
            if match:
                self.local_port = int(match.group(1))
                break
        if self.local_port is None:
            self.close()
            raise PortForwardError(
                f"port-forward to {self.target} did not report a local port "
                f"within {self.ready_timeout}s"
            )

        if self.probe_path is not None:
            self._wait_healthy(deadline)
        return self

    def _wait_healthy(self, deadline: float) -> None:
        url = f"{self.base_url}{self.probe_path}"
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    if resp.status < 500:
                        return
            except Exception as exc:  # noqa: BLE001 - any failure means not ready yet
                last = exc
            time.sleep(0.5)
        self.close()
        raise PortForwardError(f"{url} never became healthy: {last!r}")

    def close(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
        self.local_port = None

    def __exit__(self, *exc: object) -> None:
        self.close()
