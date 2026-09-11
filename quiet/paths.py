"""Where the AIOpsLab checkout lives.

PURE MODULE — paths only, no imports of ``aiopslab`` itself.

This project is its own repository and pulls AIOpsLab in as a submodule
at ``vendor/AIOpsLab``, pinned to a commit. Two reasons:

* The scoring core is ~7k lines and touches ``aiopslab`` through three
  symbols in one file. Burying that inside a fork of someone else's
  benchmark makes both halves illegible — the contribution that belongs
  upstream (a fault problem and four bug fixes) and the instrument that
  does not.
* A pinned submodule records which framework commit produced a result,
  which is the same reproducibility contract PREREG asks for everywhere
  else.

Override with ``AIOPSLAB_ROOT`` when working against a checkout
elsewhere — a sibling clone, or the lab machine's existing one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Default location of the pinned submodule.
DEFAULT_AIOPSLAB_ROOT = REPO_ROOT / "vendor" / "AIOpsLab"


def aiopslab_root() -> Path:
    env = os.environ.get("AIOPSLAB_ROOT")
    return Path(env).expanduser().resolve() if env else DEFAULT_AIOPSLAB_ROOT


AIOPSLAB_ROOT = aiopslab_root()


def ensure_importable() -> Path:
    """Put the AIOpsLab checkout on ``sys.path`` and return its root.

    Called from the impure entry points immediately before they import
    ``aiopslab``. Never called at module import time: importing
    ``aiopslab`` reads a gitignored ``config.yml`` and loads a kubeconfig,
    so doing it eagerly would make this package unimportable on a laptop
    — which ``tests/test_import_purity.py`` forbids.
    """
    root = aiopslab_root()
    if not (root / "aiopslab").is_dir():
        raise FileNotFoundError(
            f"AIOpsLab checkout not found at {root}.\n"
            f"  git submodule update --init --recursive\n"
            f"or point AIOPSLAB_ROOT at an existing checkout."
        )
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root
