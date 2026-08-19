"""
Guardrail: the Remote Script installer behaves against a real directory.

`ableton-mcp-install-script` is the one command every user must run; a
packaging move that silently broke it would strand every install
(docs/REFACTOR_PLAN.md §5 guardrail table — new protection). These tests run
`install_remote_script` against a pytest tmp_path standing in for Live's
"User Remote Scripts" directory and pin the three behaviors users depend on:
fresh install, no-op re-run, and backup-then-update of a modified script.

The ABLETON_MCP_SKIP_SCRIPT_INSTALL escape hatch is cleared per-test so the
suite exercises the default path regardless of the developer's environment.

Runs anywhere — no Ableton, no network.
"""

import pytest

import ableton_mcp.remote_script_install as installer


@pytest.fixture(autouse=True)
def _no_skip_env(monkeypatch):
    monkeypatch.delenv("ABLETON_MCP_SKIP_SCRIPT_INSTALL", raising=False)


def _installed_script(target_root):
    return target_root / installer.REMOTE_SCRIPT_FOLDER_NAME / "__init__.py"


def _install(target_root):
    results = installer.install_remote_script(target_root)
    assert len(results) == 1, f"expected one result for one target: {results}"
    return results[0]


def test_fresh_install_copies_the_bundled_script(tmp_path):
    result = _install(tmp_path)
    assert result["status"] == "installed"

    dest = _installed_script(tmp_path)
    assert result["path"] == str(dest)
    assert dest.read_bytes() == installer.bundled_remote_script_init().read_bytes()
    assert result["backup"] is None


def test_rerun_on_identical_install_is_unchanged(tmp_path):
    _install(tmp_path)
    result = _install(tmp_path)
    assert result["status"] == "unchanged"
    assert result["backup"] is None


def test_modified_install_is_backed_up_then_updated(tmp_path):
    _install(tmp_path)
    dest = _installed_script(tmp_path)

    # A user (or an older package) edited the installed script in place.
    modified = dest.read_bytes() + b"\n# local edit\n"
    dest.write_bytes(modified)

    result = _install(tmp_path)
    assert result["status"] == "updated"

    # The previous contents survive as a .py.bak sibling...
    backup = dest.with_suffix(".py.bak")
    assert result["backup"] == str(backup)
    assert backup.read_bytes() == modified
    # ...and the installed script is the bundled source again.
    assert dest.read_bytes() == installer.bundled_remote_script_init().read_bytes()
