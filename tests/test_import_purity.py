"""The pure layer must not drag in AIOpsLab.

This is enforced mechanically rather than by convention because the
coupling would be invisible until it bites, and it bites in the worst
place: on a laptop with no cluster, mid-analysis.

Three separate import-time landmines make this a real risk, not a
hypothetical one:

* ``aiopslab/paths.py`` reads ``aiopslab/config.yml`` at import. That
  file is gitignored and absent on a fresh checkout, so the import
  raises FileNotFoundError.
* ``aiopslab/observer/__init__.py`` calls ``load_kube_config()`` at
  import. No kubeconfig, no import.
* ``AstronomyShop.__init__`` calls ``create_namespace()``. Merely
  constructing a problem object talks to a cluster.

So a stray convenience import inside the scoring path would make the
entire offline workflow -- reparse, rescore, power simulation -- fail
away from the lab machine.
"""

from __future__ import annotations

import subprocess
import sys

PURE_MODULES = [
    "quiet.probe.model",
    "quiet.probe.stats",
    "quiet.probe.score",
    "quiet.probe.calibrate",
    "quiet.harness.leak",
]

_PROBE = """
import sys
import {module}
leaked = sorted(m for m in sys.modules if m == "aiopslab" or m.startswith("aiopslab."))
if leaked:
    print("LEAKED:" + ",".join(leaked))
    raise SystemExit(1)
print("CLEAN")
"""


def _import_in_subprocess(module: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(module=module)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def test_each_pure_module_imports_without_aiopslab():
    for module in PURE_MODULES:
        code, output = _import_in_subprocess(module)
        assert code == 0, f"{module} pulled in aiopslab or failed: {output}"
        assert "CLEAN" in output


def test_whole_pure_layer_together_stays_clean():
    joined = "; ".join(f"import {m}" for m in PURE_MODULES)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; {joined}; "
            'leaked=[m for m in sys.modules if m.startswith("aiopslab")]; '
            'print("LEAKED:"+",".join(leaked) if leaked else "CLEAN"); '
            "raise SystemExit(1 if leaked else 0)",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_run_probe_imports_cleanly_despite_being_impure():
    """run_probe drives a cluster, but its IMPORT must stay clean.

    aiopslab is imported inside the functions that need it, so `--help`,
    argument validation and unit tests all work on a laptop. A top-level
    import would make the module unloadable anywhere without a kubeconfig
    and a config.yml.
    """
    code, output = _import_in_subprocess("quiet.harness.run_probe")
    assert code == 0, f"run_probe pulled in aiopslab at import time: {output}"


def test_pure_layer_does_not_need_network_or_kube_client():
    """Neither requests nor the kubernetes client should be reachable
    from the scoring path; they belong to the cluster-side extras."""
    joined = "; ".join(f"import {m}" for m in PURE_MODULES)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; {joined}; "
            'bad=[m for m in sys.modules if m.split(".")[0] in ("requests","kubernetes","urllib3")]; '
            'print("PULLED:"+",".join(sorted(set(bad))) if bad else "CLEAN"); '
            "raise SystemExit(1 if bad else 0)",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
