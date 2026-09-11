from __future__ import annotations

import json
from pathlib import Path

import pytest

from quiet.harness.budget import (
    Budget,
    BudgetExceeded,
    BudgetLedger,
    Price,
    Pricing,
    Usage,
    load_pricing,
)

PRICING = Pricing(
    version="test-1",
    models={"m": Price(input=10.0, output=100.0, cache_read=1.0, cache_write=12.5)},
)


def usage(i=0, o=0, cr=0, cw=0, model="m") -> Usage:
    return Usage(
        model=model,
        input_tokens=i,
        output_tokens=o,
        cache_read_tokens=cr,
        cache_write_tokens=cw,
    )


class TestPricing:
    def test_cost_is_per_million(self):
        assert PRICING.cost(usage(i=1_000_000)) == pytest.approx(10.0)
        assert PRICING.cost(usage(o=1_000_000)) == pytest.approx(100.0)

    def test_cache_tokens_are_priced_separately(self):
        assert PRICING.cost(usage(cr=1_000_000)) == pytest.approx(1.0)
        assert PRICING.cost(usage(cw=1_000_000)) == pytest.approx(12.5)

    def test_components_add_up(self):
        u = usage(i=100_000, o=10_000, cr=50_000)
        assert PRICING.cost(u) == pytest.approx(1.0 + 1.0 + 0.05)

    def test_unknown_model_is_an_error_not_a_zero(self):
        """Silently pricing an unknown model at zero would defeat the cap."""
        with pytest.raises(KeyError, match="no price for model"):
            PRICING.cost(usage(i=1000, model="mystery"))

    def test_shipped_pricing_file_parses(self):
        p = load_pricing(Path(__file__).resolve().parents[1] / "quiet/harness/pricing.json")
        assert p.version
        assert "claude-opus-5" in p.models
        # Sanity: output costs more than input on every model.
        for name, price in p.models.items():
            assert price.output > price.input, name


class TestUsageArithmetic:
    def test_adds_fieldwise(self):
        total = usage(i=1, o=2, cr=3, cw=4) + usage(i=10, o=20, cr=30, cw=40)
        assert (total.input_tokens, total.output_tokens) == (11, 22)
        assert (total.cache_read_tokens, total.cache_write_tokens) == (33, 44)

    def test_refuses_to_mix_models(self):
        """Summing across models would produce a number priced at one rate."""
        with pytest.raises(ValueError, match="across models"):
            usage(i=1, model="a") + usage(i=1, model="b")


class TestLedger:
    def make(self, tmp_path, max_usd=1.0, reserve=0.0) -> BudgetLedger:
        return BudgetLedger(
            tmp_path / "ledger.json",
            Budget(max_usd=max_usd, reserve_per_run_usd=reserve),
            PRICING,
        )

    def test_charge_accumulates_and_persists(self, tmp_path):
        led = self.make(tmp_path)
        led.charge("r1", usage(i=100_000))       # $1.00... too big; use smaller
        assert led.ledger.spent_usd == pytest.approx(1.0)
        # A fresh object reads the same history off disk.
        again = self.make(tmp_path)
        assert again.ledger.spent_usd == pytest.approx(1.0)

    def test_survives_a_crash_between_calls(self, tmp_path):
        """The ledger is written after every call, not at the end -- a campaign
        that dies mid-run must not lose its accounting and overspend on resume."""
        led = self.make(tmp_path, max_usd=10.0)
        led.charge("r1", usage(i=10_000))
        del led
        resumed = self.make(tmp_path, max_usd=10.0)
        resumed.charge("r2", usage(i=10_000))
        assert resumed.ledger.spent_usd == pytest.approx(0.2)
        assert len(resumed.ledger.entries) == 2

    def test_budget_can_be_raised_without_rewriting_history(self, tmp_path):
        led = self.make(tmp_path, max_usd=1.0)
        led.charge("r1", usage(i=50_000))
        raised = self.make(tmp_path, max_usd=5.0)
        assert raised.ledger.budget.max_usd == 5.0
        assert raised.ledger.spent_usd == pytest.approx(0.5)

    def test_entries_record_the_pricing_version(self, tmp_path):
        """So a later price change cannot reinterpret an archived cost."""
        led = self.make(tmp_path)
        led.charge("r1", usage(i=1000))
        assert led.ledger.entries[0].pricing_version == "test-1"

    def test_no_temp_files_left_behind(self, tmp_path):
        led = self.make(tmp_path)
        led.charge("r1", usage(i=1000))
        assert not list(tmp_path.glob("*.tmp"))


class TestPreflight:
    def make(self, tmp_path, max_usd, reserve=0.0):
        return BudgetLedger(
            tmp_path / "l.json",
            Budget(max_usd=max_usd, reserve_per_run_usd=reserve),
            PRICING,
        )

    def test_passes_when_affordable(self, tmp_path):
        self.make(tmp_path, max_usd=10.0, reserve=1.0).preflight()

    def test_blocks_before_the_run_not_after(self, tmp_path):
        """The whole point: learning the bill afterwards is how a $5.89
        estimate becomes $40.18."""
        led = self.make(tmp_path, max_usd=1.0, reserve=0.5)
        led.charge("r1", usage(i=60_000))        # $0.60 spent, $0.40 left
        with pytest.raises(BudgetExceeded, match="budget exhausted"):
            led.preflight()

    def test_reserve_defaults_to_measured_cost_per_run(self, tmp_path):
        """Once there is history, the estimate comes from what runs actually
        cost rather than from a guess made before the campaign."""
        led = self.make(tmp_path, max_usd=1.0, reserve=0.0)
        led.charge("r1", usage(i=30_000))        # $0.30
        led.charge("r2", usage(i=30_000))        # $0.30 -> mean $0.30
        assert led.ledger.mean_cost_per_run() == pytest.approx(0.30)
        # $0.40 left, a run costs ~$0.30 -> still allowed
        led.preflight()
        led.charge("r3", usage(i=20_000))        # $0.20 -> $0.20 left, mean ~0.267
        with pytest.raises(BudgetExceeded):
            led.preflight()

    def test_explicit_reserve_overrides(self, tmp_path):
        led = self.make(tmp_path, max_usd=1.0)
        led.charge("r1", usage(i=50_000))        # $0.50 left
        led.preflight(reserve_usd=0.4)
        with pytest.raises(BudgetExceeded):
            led.preflight(reserve_usd=0.6)

    def test_error_says_what_to_do(self, tmp_path):
        led = self.make(tmp_path, max_usd=0.1, reserve=1.0)
        with pytest.raises(BudgetExceeded, match="Report the runs completed so far"):
            led.preflight()

    def test_check_guards_mid_run(self, tmp_path):
        led = self.make(tmp_path, max_usd=0.5)
        led.check()
        led.charge("r1", usage(i=60_000))        # $0.60 > $0.50
        with pytest.raises(BudgetExceeded, match="mid-run"):
            led.check()


class TestSnapshot:
    def test_totals_one_run_only(self, tmp_path):
        led = BudgetLedger(
            tmp_path / "l.json", Budget(max_usd=10.0), PRICING
        )
        led.charge("r1", usage(i=1000, o=100))
        led.charge("r1", usage(i=2000, o=200))
        led.charge("r2", usage(i=9999, o=999))

        snap = led.snapshot_for("r1")
        assert snap["calls"] == 2
        assert snap["usage"]["input_tokens"] == 3000
        assert snap["usage"]["output_tokens"] == 300
        assert snap["cost_usd"] == pytest.approx(PRICING.cost(usage(i=3000, o=300)))

    def test_carries_campaign_totals_for_the_run_record(self, tmp_path):
        led = BudgetLedger(tmp_path / "l.json", Budget(max_usd=10.0), PRICING)
        led.charge("r1", usage(i=1_000_000))
        snap = led.snapshot_for("r1")
        assert snap["campaign_spent_usd"] == pytest.approx(10.0)
        assert snap["campaign_remaining_usd"] == pytest.approx(0.0)
        assert snap["pricing_version"] == "test-1"

    def test_unknown_run_is_empty_not_an_error(self, tmp_path):
        led = BudgetLedger(tmp_path / "l.json", Budget(max_usd=1.0), PRICING)
        snap = led.snapshot_for("never-ran")
        assert snap["calls"] == 0 and snap["cost_usd"] == 0.0
