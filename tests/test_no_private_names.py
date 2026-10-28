"""
Guardrail: no private project details in the public repository.

This repository is public, and it is developed against real production
sessions. Song titles, collaborator names, track names copied out of a real
set, or personal file paths have leaked into docs and goldens before. The
names themselves must never be committed, not even into this test, so the
denylist lives in an untracked local file:

- ``$ABLETON_MCP_PRIVATE_DENYLIST`` if set, else
- ``tests/.private-names.txt`` (gitignored).

One entry per line, matched case-insensitively as a plain substring; blank
lines and ``#`` comments are ignored. With no denylist present the scan is
skipped (CI and other contributors have none), so it guards the machines
that know what to look for.

Runs anywhere — no Ableton, no network.
"""

import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DENYLIST = REPO_ROOT / "tests" / ".private-names.txt"


def _denylist_path():
    env = os.environ.get("ABLETON_MCP_PRIVATE_DENYLIST")
    return Path(env).expanduser() if env else DEFAULT_DENYLIST


def _load_denylist(path):
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(line.lower())
    return entries


def _tracked_files():
    """Files git tracks (what would be published), or every file when this
    is not a git checkout (an sdist) — minus the usual generated noise."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=REPO_ROOT,
            capture_output=True, check=True, timeout=30,
        ).stdout.decode("utf-8", "replace")
        return [REPO_ROOT / p for p in out.split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        skip = {".git", ".venv", "__pycache__", ".pytest_cache", "dist", "build"}
        return [
            p for p in REPO_ROOT.rglob("*")
            if p.is_file() and not (set(p.relative_to(REPO_ROOT).parts) & skip)
        ]


def scan(files, denylist, root=REPO_ROOT):
    """[(relative path, line number, entry)] for every denylisted hit."""
    hits = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binaries (.icns, images) cannot carry a name in text form
        for lineno, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            for entry in denylist:
                if entry in low:
                    hits.append((str(path.relative_to(root)), lineno, entry))
    return hits


def test_denylist_file_is_never_tracked():
    """The default denylist location must stay gitignored: committing it
    would publish exactly the names it exists to keep out."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(DEFAULT_DENYLIST)],
            cwd=REPO_ROOT, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git unavailable")
    if result.returncode == 128:
        pytest.skip("not a git checkout")
    assert result.returncode == 0, (
        "tests/.private-names.txt is not gitignored; add it to .gitignore"
    )


def test_no_private_names_in_tracked_files():
    path = _denylist_path()
    if not path.is_file():
        pytest.skip(f"no private denylist at {path} (expected on CI)")
    denylist = _load_denylist(path)
    if not denylist:
        pytest.skip("private denylist is empty")
    denylist_resolved = path.resolve()
    files = [f for f in _tracked_files() if f.resolve() != denylist_resolved]
    hits = scan(files, denylist)
    # Report the location only: echoing the entry would print the private
    # name into CI logs, which are public too.
    assert not hits, "private names found at: " + ", ".join(
        f"{p}:{n}" for p, n, _ in hits
    )


def test_scan_matches_case_insensitively(tmp_path):
    """The scanner itself, on a synthetic file, so it is tested even where
    no real denylist exists."""
    f = tmp_path / "doc.md"
    f.write_text("intro\nThe BUS is called Placeholder Name here\n", encoding="utf-8")
    assert scan([f], ["placeholder name"], root=tmp_path) == [("doc.md", 2, "placeholder name")]
    assert scan([f], ["absent"], root=tmp_path) == []
