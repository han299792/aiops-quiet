"""Write-once run directories.

PURE (filesystem only -- no cluster, no network).

Runs are append-only because the earlier attempt at this experiment left
nothing behind to re-examine. Two rules enforce it:

* ``create`` uses ``mkdir(exist_ok=False)``, so a run id collision is a
  hard error rather than a silent overwrite.
* ``write`` refuses to replace an existing file.

``run.json`` is written last and is the completion marker, which makes a
crashed run unambiguous -- the directory exists without it -- and lets a
resumed campaign skip what already finished.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

MARKER = "run.json"


class RunDirError(RuntimeError):
    pass


def timestamp_slug(when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    return when.strftime("%Y-%m-%dT%H-%M-%SZ")


def run_id(condition: str, replicate: int, *, when: datetime | None = None) -> str:
    """``2026-09-09T12-00-00Z__payment_dose_10__r03``.

    The timestamp leads so directories sort chronologically, and the
    condition and replicate are in the name so a directory is
    identifiable without opening it.
    """
    return f"{timestamp_slug(when)}__{condition}__r{replicate:02d}"


class RunDir:
    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def create(cls, root: Path, run_id: str) -> "RunDir":
        path = Path(root) / run_id
        try:
            path.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise RunDirError(
                f"run directory already exists: {path}. Run ids are never "
                f"reused; results are append-only."
            ) from exc
        return cls(path)

    @classmethod
    def open(cls, path: Path) -> "RunDir":
        if not Path(path).is_dir():
            raise RunDirError(f"not a run directory: {path}")
        return cls(Path(path))

    def is_complete(self) -> bool:
        return (self.path / MARKER).exists()

    def write(self, name: str, payload: Any, *, allow_overwrite: bool = False) -> Path:
        """Atomically write JSON. Refuses to clobber by default."""
        target = self.path / name
        if target.exists() and not allow_overwrite:
            raise RunDirError(f"refusing to overwrite {target}")
        target.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(payload, BaseModel):
            text = payload.model_dump_json(indent=2)
        else:
            text = json.dumps(payload, indent=2, default=str)

        # tmp + replace so a crash mid-write cannot leave a half file that
        # later reads as valid-but-truncated.
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return target

    def read(self, name: str) -> Any:
        return json.loads((self.path / name).read_text())

    def finalize(self, record: Any) -> Path:
        """Write the completion marker. Must be last."""
        return self.write(MARKER, record)


def iter_runs(root: Path, *, complete_only: bool = False) -> list[RunDir]:
    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        run = RunDir(child)
        if complete_only and not run.is_complete():
            continue
        out.append(run)
    return out


def incomplete_runs(root: Path) -> list[RunDir]:
    """Directories with no marker: crashed, aborted, or still running.

    Reported rather than cleaned up. A run that vanished is a run that
    cannot be counted, and the discard rate is itself a number worth
    publishing.
    """
    return [r for r in iter_runs(root) if not r.is_complete()]
