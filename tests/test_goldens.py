"""Golden characterization suite — replay (docs/REFACTOR_PLAN.md section 5
Level 1, section 6 PR3).

Replays every recorded case in tests/goldens/*.json through the real
ableton_mcp.tools tool functions (Ableton socket faked, no network) and
asserts:

- the tool's response string is byte-identical to the recorded ``expect``;
- the tool performed exactly the recorded wire exchange — command names,
  params, and order — and consumed all of it.

These fixtures were recorded against the pre-refactor monolith, so every
structural PR in the plan must keep them passing byte-for-byte. To
regenerate after a *deliberate* behavior change:

    uv run python tests/record_goldens.py
"""

import difflib
import json
import os

import pytest

import golden_cases
from golden_runner import FakeWireClient, call_tool

GOLDENS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "goldens")


def _load_recorded_cases():
    out = []
    if not os.path.isdir(GOLDENS_DIR):
        return out
    for filename in sorted(os.listdir(GOLDENS_DIR)):
        stem, ext = os.path.splitext(filename)
        if ext != ".json":
            continue
        with open(os.path.join(GOLDENS_DIR, filename), encoding="utf-8") as fh:
            data = json.load(fh)
        for case in data["cases"]:
            out.append((stem, case))
    return out


RECORDED = _load_recorded_cases()


@pytest.mark.parametrize(
    "tool,case",
    RECORDED,
    ids=["%s-%s" % (tool, case["name"]) for tool, case in RECORDED],
)
def test_golden_replay(tool, case):
    fake = FakeWireClient(case["wire"])
    got = call_tool(tool, case["args"], fake)

    expect = case["expect"]
    assert isinstance(got, str)
    if got != expect:
        diff = "\n".join(difflib.unified_diff(
            expect.splitlines(),
            got.splitlines(),
            fromfile="goldens/%s.json :: %s (expected)" % (tool, case["name"]),
            tofile="current code (actual)",
            lineterm="",
        ))
        pytest.fail(
            "golden mismatch for %s/%s — if this change is deliberate, "
            "regenerate with `uv run python tests/record_goldens.py` and "
            "commit the diff:\n%s" % (tool, case["name"], diff)
        )

    # The whole wire script must have been consumed, in order, with matching
    # params (FakeWireClient asserts order/params on each send).
    fake.assert_consumed()


def test_goldens_exist():
    """An empty goldens dir must fail loudly, not skip 100+ tests quietly."""
    assert RECORDED, (
        "tests/goldens/ has no recorded cases — run "
        "`uv run python tests/record_goldens.py`"
    )


def test_goldens_match_case_definitions():
    """Every defined case is recorded and vice versa — a stale or missing
    golden file is a failure, not a silent gap."""
    recorded = {(tool, case["name"]) for tool, case in RECORDED}
    defined = {(case["tool"], case["name"]) for case in golden_cases.CASES}
    missing = defined - recorded
    stale = recorded - defined
    assert not missing and not stale, (
        "goldens out of step with golden_cases.py "
        "(run `uv run python tests/record_goldens.py`):\n"
        "  missing from goldens/: %r\n  stale in goldens/: %r"
        % (sorted(missing), sorted(stale))
    )


def test_goldens_cover_all_46_tools():
    tools = {tool for tool, _ in RECORDED}
    assert len(tools) == 46, (
        "expected goldens for all 46 tools, found %d: %r"
        % (len(tools), sorted(tools))
    )


def test_every_tool_has_an_error_case():
    by_tool = {}
    for tool, case in RECORDED:
        by_tool.setdefault(tool, set()).add(case["name"])
    without = sorted(t for t, names in by_tool.items() if "error" not in names)
    assert not without, "tools missing an error golden: %r" % without
