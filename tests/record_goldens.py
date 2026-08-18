#!/usr/bin/env python3
"""Regenerate the golden characterization fixtures (docs/REFACTOR_PLAN.md
section 5 Level 1, section 6 PR3).

    uv run python tests/record_goldens.py

Executes every case in tests/golden_cases.py against the CURRENT code (via
tests/golden_runner.py — no Ableton, no network) and writes one
tests/goldens/<tool>.json per tool as {"cases": [{name, args, wire, expect}]}.

Output is deterministic: sorted keys, indent 2, trailing newline. Each case
is JSON-round-tripped (with sorted keys) BEFORE it is executed, so the
recorded ``expect`` is produced from exactly the bytes the replaying test
will load — several tools json.dumps the wire response verbatim, and dict
key order would otherwise differ between record and replay.

Goldens should only ever change in a commit that deliberately changes tool
behavior; the diff of the regenerated files IS the behavior change and
belongs in that commit's message.
"""

import json
import logging
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
for path in (TESTS_DIR, REPO_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import golden_cases
from golden_runner import FakeWireClient, call_tool

GOLDENS_DIR = os.path.join(TESTS_DIR, "goldens")


def _normalize(obj):
    """Round-trip through JSON with sorted keys — what the test will see."""
    return json.loads(json.dumps(obj, sort_keys=True))


def record_case(tool, case):
    """Run one case against the current code and return the golden entry."""
    entry = _normalize({
        "name": case["name"],
        "args": case["args"],
        "wire": case["wire"],
    })
    fake = FakeWireClient(entry["wire"])
    expect = call_tool(tool, entry["args"], fake)
    fake.assert_consumed()
    if not isinstance(expect, str):
        raise SystemExit(
            "%s/%s: tool returned %r, expected a string"
            % (tool, case["name"], type(expect))
        )
    entry["expect"] = expect
    return entry


def main():
    # The tools log every canned error at ERROR level; keep the recorder's
    # output to its own one line per file.
    logging.disable(logging.CRITICAL)

    os.makedirs(GOLDENS_DIR, exist_ok=True)
    for tool in golden_cases.TOOLS:
        cases = [c for c in golden_cases.CASES if c["tool"] == tool]
        entries = [record_case(tool, c) for c in cases]
        payload = json.dumps({"cases": entries}, indent=2, sort_keys=True) + "\n"
        path = os.path.join(GOLDENS_DIR, tool + ".json")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        print("wrote %s (%d cases)" % (os.path.relpath(path, REPO_ROOT), len(entries)))

    # Drop goldens for tools that no longer have case definitions, so a
    # renamed tool cannot leave a stale (and silently unreplayed) fixture.
    known = set(golden_cases.TOOLS)
    for filename in sorted(os.listdir(GOLDENS_DIR)):
        stem, ext = os.path.splitext(filename)
        if ext == ".json" and stem not in known:
            os.remove(os.path.join(GOLDENS_DIR, filename))
            print("removed stale goldens/%s" % filename)

    print("recorded %d tools, %d cases"
          % (len(golden_cases.TOOLS), len(golden_cases.CASES)))


if __name__ == "__main__":
    main()
