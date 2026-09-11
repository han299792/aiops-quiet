"""Token accounting and a hard spending cap.

PURE apart from reading and writing one JSON file.

AIOpsLab tracks no cost at all — every client discards ``response.usage``,
and ``in_tokens``/``out_tokens`` in the evaluator count the flattened
trace once, which badly undercounts a multi-turn agent that resends its
history every step.

Two properties matter more than precision here:

* **The check happens BEFORE a run, not after.** Learning the bill
  afterwards is how a $5.89 estimate becomes $40.18.
* **The ledger is persisted after every call.** A campaign that dies
  mid-run must not lose its accounting, or the resume overspends.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field


class Usage(BaseModel):
    """One LLM call, as reported by the provider."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    #: Anthropic reports these separately and prices them differently.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        if self.model != other.model:
            raise ValueError(f"cannot add usage across models: {self.model} vs {other.model}")
        return Usage(
            model=self.model,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


class Price(BaseModel):
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0


class Pricing(BaseModel):
    """Prices, stamped with a version.

    Kept as data rather than hardcoded so that a later price change
    cannot silently reinterpret an archived cost. ``version`` goes into
    every ledger entry.
    """

    version: str
    models: dict[str, Price]

    def cost(self, u: Usage) -> float:
        p = self.models.get(u.model)
        if p is None:
            raise KeyError(
                f"no price for model {u.model!r}; known: {sorted(self.models)}"
            )
        return (
            u.input_tokens * p.input
            + u.output_tokens * p.output
            + u.cache_read_tokens * p.cache_read
            + u.cache_write_tokens * p.cache_write
        ) / 1_000_000


class Budget(BaseModel):
    max_usd: float
    #: Refuse to start a run when the remaining budget is below this.
    #: Set from the pilot's measured cost per run, times a safety factor.
    reserve_per_run_usd: float = 0.0


class BudgetExceeded(RuntimeError):
    pass


class Entry(BaseModel):
    ts: datetime
    run_id: str
    usage: Usage
    cost_usd: float
    pricing_version: str


class Ledger(BaseModel):
    budget: Budget
    entries: list[Entry] = Field(default_factory=list)

    @property
    def spent_usd(self) -> float:
        return sum(e.cost_usd for e in self.entries)

    @property
    def remaining_usd(self) -> float:
        return self.budget.max_usd - self.spent_usd

    def by_run(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in self.entries:
            out[e.run_id] = out.get(e.run_id, 0.0) + e.cost_usd
        return out

    def mean_cost_per_run(self) -> float | None:
        per = self.by_run()
        return (sum(per.values()) / len(per)) if per else None


class BudgetLedger:
    """Persisted ledger with a pre-run gate."""

    def __init__(self, path: Path, budget: Budget, pricing: Pricing) -> None:
        self.path = Path(path)
        self.pricing = pricing
        if self.path.exists():
            self.ledger = Ledger.model_validate_json(self.path.read_text())
            # The cap can be raised or lowered between sessions; the
            # spending history is what must not be rewritten.
            self.ledger.budget = budget
        else:
            self.ledger = Ledger(budget=budget)
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(self.ledger.model_dump_json(indent=2))
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def charge(self, run_id: str, usage: Usage) -> float:
        """Record one call. Persisted immediately, before returning."""
        cost = self.pricing.cost(usage)
        self.ledger.entries.append(
            Entry(
                ts=datetime.now(timezone.utc),
                run_id=run_id,
                usage=usage,
                cost_usd=cost,
                pricing_version=self.pricing.version,
            )
        )
        self._save()
        return cost

    def preflight(self, *, reserve_usd: float | None = None) -> None:
        """Raise if the next run cannot be afforded. Call BEFORE it starts.

        The reserve defaults to the budget's own figure, or -- once there
        is history -- the measured mean cost per run, whichever is larger.
        Stopping one run early is cheap; discovering the overrun after it
        is not.
        """
        if reserve_usd is None:
            measured = self.ledger.mean_cost_per_run() or 0.0
            reserve_usd = max(self.ledger.budget.reserve_per_run_usd, measured)
        if self.ledger.remaining_usd < reserve_usd:
            raise BudgetExceeded(
                f"budget exhausted: spent ${self.ledger.spent_usd:.2f} of "
                f"${self.ledger.budget.max_usd:.2f}, "
                f"${self.ledger.remaining_usd:.2f} left but a run needs about "
                f"${reserve_usd:.2f}. Report the runs completed so far."
            )

    def check(self) -> None:
        """Raise if the cap is already blown. Call between steps."""
        if self.ledger.remaining_usd <= 0:
            raise BudgetExceeded(
                f"budget exhausted mid-run: spent ${self.ledger.spent_usd:.2f} "
                f"of ${self.ledger.budget.max_usd:.2f}"
            )

    def snapshot_for(self, run_id: str) -> dict:
        entries = [e for e in self.ledger.entries if e.run_id == run_id]
        total = Usage(model=entries[0].usage.model) if entries else None
        for e in entries:
            total = total + e.usage if total else e.usage
        return {
            "run_id": run_id,
            "calls": len(entries),
            "usage": total.model_dump() if total else None,
            "cost_usd": sum(e.cost_usd for e in entries),
            "pricing_version": self.pricing.version,
            "campaign_spent_usd": self.ledger.spent_usd,
            "campaign_remaining_usd": self.ledger.remaining_usd,
        }


def load_pricing(path: Path) -> Pricing:
    return Pricing.model_validate_json(Path(path).read_text())
