"""The docs quote a test count. Make the docs wrong loudly, not quietly.

This project's whole argument is that a number without a check behind it
is not evidence. A hand-maintained "245 tests pass" in six files decays
within a week, and a stale one in a README is exactly the kind of small
unverified claim an interviewer finds first.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Files that quote the suite size, and the patterns they quote it with.
DOCS = ("README.md", "RUNBOOK.md", "docs/README.md", "docs/4-PLAN.md")

_CLAIM = re.compile(
    r"테스트\s+\**(\d+)개"          # "테스트 245개" / "테스트 **245개**"
    r"|(\d+)개\s+테스트"            # "245개 테스트"
    r"|pytest\s+tests\s+-q\s*#\s*(\d+)"  # the shell comments
)


def claims() -> list[tuple[str, int]]:
    out = []
    for name in DOCS:
        path = ROOT / name
        if not path.exists():
            continue
        for m in _CLAIM.finditer(path.read_text()):
            out.append((name, int(next(g for g in m.groups() if g))))
    return out


def test_the_docs_quote_a_number_at_all():
    """A silent doc is its own failure -- the count is load-bearing."""
    assert claims(), f"no test-count claim found in any of {DOCS}"


def test_every_quoted_test_count_matches_the_suite(request):
    actual = request.session.testscollected
    wrong = [(f, n) for f, n in claims() if n != actual]
    assert not wrong, (
        f"docs claim {wrong} but the suite collects {actual}. "
        f"Update them, or the number stops being evidence."
    )
