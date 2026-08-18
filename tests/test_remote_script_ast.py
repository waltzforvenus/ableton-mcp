"""
AST guardrails for the Ableton Remote Script.

The Remote Script imports `_Framework` and can only run inside Live, so these
tests never import it — they `ast.parse` the canonical source and check the
structural invariants whose silent violation shipped real regressions
(docs/REFACTOR_PLAN.md §1 and the §5 guardrail table):

- the 2026-08 upstream merge (commit 4878234) left four methods defined twice
  in the `AbletonMCP` class body; Python kept the later definition, so the
  dispatch code called the losing fork signatures and the device-parameter
  pair TypeError'd at runtime (repaired by plan PR5),
- the same merge left duplicate/unreachable dispatch branches, duplicate
  membership-list entries, and branches that call methods which do not exist
  (also repaired by PR5),
- upstream binds the command socket to 0.0.0.0; this fork must stay loopback.

Plan PR6 collapsed both `elif` ladders into the module-level `COMMANDS`
literal, dispatched as `getattr(self, method)(**params)`. These parsers were
rewritten against that table (the Remote-Script half of "migration adapter
#3", §5): the ladder-shaped hazards became table-shaped ones — a row naming a
method that does not exist, a handler that cannot bind the wire parameters,
a duplicate key in the dict literal (Python keeps the last silently, exactly
like a duplicate method def), and a derivation of SCRIPT_CAPABILITIES that
drifts from the wire response PR5 froze.

Runs anywhere — no Ableton, no network.
"""

import ast
import json
from functools import lru_cache
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_SCRIPT = REPO_ROOT / "AbletonMCP_Remote_Script" / "__init__.py"
COMMANDS_REGISTRY = REPO_ROOT / "src" / "ableton_mcp" / "commands.py"
GOLDENS_DIR = REPO_ROOT / "tests" / "goldens"


# --------------------------------------------------------------------------
# AST plumbing
# --------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _class_defs(tree):
    return [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]


def _ableton_mcp_class():
    for cls in _class_defs(_parse(REMOTE_SCRIPT)):
        if cls.name == "AbletonMCP":
            return cls
    raise AssertionError("class AbletonMCP not found in the Remote Script")


def _direct_methods(cls):
    """(name, node) for every def that is a direct child of the class body —
    the ones that land in the class namespace, later definition winning."""
    return [
        (n.name, n)
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _winning_defs(cls):
    """name -> the def Python actually keeps (the last one in the body)."""
    return dict(_direct_methods(cls))


def _module_constant(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"no module-level assignment to {name}")


def _commands_dict_node():
    """The ast.Dict node of the module-level `COMMANDS = {...}` literal.

    Returned as the raw node (not literal_eval'd) so tests can see duplicate
    keys, which literal_eval silently collapses (last one wins) — the same
    failure mode as a duplicate method definition.
    """
    for node in _parse(REMOTE_SCRIPT).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "COMMANDS":
                    assert isinstance(node.value, ast.Dict), (
                        "COMMANDS must be a dict literal so these guardrails "
                        "can read it without importing _Framework"
                    )
                    return node.value
    raise AssertionError("module-level COMMANDS table not found in the Remote Script")


def _commands_table():
    """The COMMANDS table as a plain dict: name -> (method, main_thread,
    queue_timeout, advertise). Must be a pure literal."""
    table = ast.literal_eval(_commands_dict_node())
    assert table, "COMMANDS table is empty"
    for name, row in table.items():
        assert isinstance(name, str), f"non-string COMMANDS key: {name!r}"
        assert isinstance(row, tuple) and len(row) == 4, (
            f"COMMANDS[{name!r}] must be a 4-tuple "
            f"(method, main_thread, queue_timeout, advertise), got {row!r}"
        )
        method, main_thread, queue_timeout, advertise = row
        assert isinstance(method, str)
        assert isinstance(main_thread, bool)
        assert queue_timeout is None or isinstance(queue_timeout, float)
        assert isinstance(advertise, bool)
    return table


def _golden_wire_calls():
    """Every (command, sorted param names) pair a golden fixture records the
    server actually sending over the wire — the observed wire contract."""
    calls = set()
    for path in sorted(GOLDENS_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for case in data.get("cases", []):
            for exchange in case.get("wire", []):
                params = exchange.get("params") or {}
                calls.add((exchange["command"], tuple(sorted(params))))
    assert calls, "no wire exchanges found in tests/goldens/"
    return calls


def _binds_keywords(fndef, kw_names):
    """Can this def bind a call made purely with these keyword args (as the
    dispatch's `handler(**params)` does)? self excluded."""
    args = fndef.args
    params = [p.arg for p in (args.posonlyargs + args.args)][1:]  # drop self
    n_defaults = len(args.defaults)
    required = params[: len(params) - n_defaults] if n_defaults else params
    kwonly = [p.arg for p in args.kwonlyargs]
    if args.kwarg is None and any(k not in params + kwonly for k in kw_names):
        return False  # unknown keyword
    if any(p not in kw_names for p in required):
        return False  # required parameter left unbound
    kwonly_required = [
        p.arg for p, d in zip(args.kwonlyargs, args.kw_defaults) if d is None
    ]
    if any(p not in kw_names for p in kwonly_required):
        return False
    return True


def _registry_dict_node():
    """The ast.Dict node of the server-side commands.py `COMMANDS` registry.

    Returned as the raw node (not evaluated) for the same reason as
    `_commands_dict_node`: a duplicate key silently keeps the last row, and
    only the raw node still shows both. The assignment is annotated
    (`COMMANDS: dict[str, CommandSpec] = {...}`), so both AnnAssign and plain
    Assign are accepted.
    """
    for node in _parse(COMMANDS_REGISTRY).body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    target = t.id
        if target == "COMMANDS":
            assert isinstance(node.value, ast.Dict), (
                "commands.py COMMANDS must be a dict literal so this "
                "guardrail can see duplicate keys"
            )
            return node.value
    raise AssertionError("COMMANDS registry not found in src/ableton_mcp/commands.py")


def _duplicates(seq):
    seen, dupes = set(), []
    for item in seq:
        if item in seen and item not in dupes:
            dupes.append(item)
        seen.add(item)
    return dupes


# --------------------------------------------------------------------------
# (1) No method defined twice in any class body
# --------------------------------------------------------------------------

def test_no_method_is_defined_twice_in_any_class():
    duplicated = {}
    for cls in _class_defs(_parse(REMOTE_SCRIPT)):
        dupes = _duplicates([name for name, _ in _direct_methods(cls)])
        if dupes:
            duplicated[cls.name] = sorted(dupes)
    assert duplicated == {}, f"methods defined twice: {duplicated}"


# --------------------------------------------------------------------------
# (2) Every COMMANDS row binds the wire parameters the server really sends
# --------------------------------------------------------------------------

def test_command_handlers_bind_observed_wire_params():
    # Dispatch is `getattr(self, method)(**params)`, so a handler must accept
    # every wire parameter name as a keyword. The goldens record the exact
    # params the server sends per command; each observed combination must
    # bind against the winning method definition. This is the arity check
    # whose absence let the 4878234 duplicate definitions ship.
    table = _commands_table()
    winning = _winning_defs(_ableton_mcp_class())
    mismatches = []
    for command, kw_names in sorted(_golden_wire_calls()):
        row = table.get(command)
        if row is None:
            mismatches.append(
                f"golden wire command {command!r} has no COMMANDS row"
            )
            continue
        fndef = winning.get(row[0])
        if fndef is None:
            continue  # nonexistent methods are test (4)'s defect
        if not _binds_keywords(fndef, kw_names):
            mismatches.append(
                f"{command}: {row[0]}() (line {fndef.lineno}) cannot bind the "
                f"golden-observed wire params {sorted(kw_names)}"
            )
    assert mismatches == [], "wire/handler arity drift:\n" + "\n".join(mismatches)


def test_command_handlers_default_every_parameter():
    # The old elif ladders supplied a default for every wire parameter via
    # params.get(key, default); PR6 moved those defaults onto the handler
    # signatures. A required (default-less) parameter would turn "param
    # omitted" from the old default into a TypeError — a wire behavior
    # change — so every parameter of every table handler must keep a default.
    winning = _winning_defs(_ableton_mcp_class())
    missing = []
    for command, row in sorted(_commands_table().items()):
        fndef = winning.get(row[0])
        if fndef is None:
            continue  # test (4)'s defect
        args = fndef.args
        params = [p.arg for p in (args.posonlyargs + args.args)][1:]  # no self
        n_defaults = len(args.defaults)
        required = params[: len(params) - n_defaults] if n_defaults else params
        required += [
            p.arg for p, d in zip(args.kwonlyargs, args.kw_defaults) if d is None
        ]
        if required:
            missing.append(f"{command}: {row[0]}() requires {required}")
    assert missing == [], (
        "table handlers with default-less parameters:\n" + "\n".join(missing)
    )


# --------------------------------------------------------------------------
# (3) No duplicate keys in the COMMANDS table literal
# --------------------------------------------------------------------------

def test_no_duplicate_command_table_keys():
    # A duplicate dict key is the table-shaped version of the duplicate
    # dispatch branch: Python keeps the last row silently, so the earlier
    # command spec vanishes with no error. literal_eval cannot see this —
    # only the raw Dict node can.
    keys = []
    for key in _commands_dict_node().keys:
        assert key is not None, "COMMANDS must not use **-expansion"
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
            "COMMANDS keys must be string literals"
        )
        keys.append(key.value)
    duplicated = _duplicates(keys)
    assert duplicated == [], f"commands dispatched more than once: {sorted(duplicated)}"


# --------------------------------------------------------------------------
# (4) The table only references methods that exist
# --------------------------------------------------------------------------

def test_command_table_references_only_existing_methods():
    winning = _winning_defs(_ableton_mcp_class())
    missing = sorted(
        {
            f"{command} -> {row[0]}"
            for command, row in _commands_table().items()
            if row[0] not in winning
        }
    )
    assert missing == [], f"COMMANDS rows naming nonexistent methods: {missing}"


# --------------------------------------------------------------------------
# (5) No duplicate keys in the server's commands.py registry literal
# --------------------------------------------------------------------------

def test_no_duplicate_keys_in_server_command_registry():
    # The server's modifying-command membership list became the commands.py
    # registry (PR8, the server half of migration adapter #3). The duplicate
    # hazard followed it: a duplicate dict key silently keeps the last
    # CommandSpec — the same failure mode as the RS table's test (3).
    keys = []
    for key in _registry_dict_node().keys:
        assert key is not None, "COMMANDS registry must not use **-expansion"
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
            "COMMANDS registry keys must be string literals"
        )
        keys.append(key.value)
    duplicated = _duplicates(keys)
    assert duplicated == [], f"registry rows defined more than once: {sorted(duplicated)}"


# --------------------------------------------------------------------------
# (6) The command socket binds to loopback
# --------------------------------------------------------------------------

def test_socket_binds_to_loopback():
    # Upstream binds to "0.0.0.0", exposing an unauthenticated command socket
    # to the whole LAN. CLAUDE.md hard rule 2; check it survives every merge.
    assert _module_constant(_parse(REMOTE_SCRIPT), "HOST") == "127.0.0.1"


# --------------------------------------------------------------------------
# (7) The version pair bumps together
# --------------------------------------------------------------------------

def test_script_version_matches_expected_version():
    from ableton_mcp.remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION

    script_version = _module_constant(_parse(REMOTE_SCRIPT), "SCRIPT_VERSION")
    assert script_version == EXPECTED_REMOTE_SCRIPT_VERSION, (
        "SCRIPT_VERSION (Remote Script) and EXPECTED_REMOTE_SCRIPT_VERSION "
        "(src/ableton_mcp/remote_script_install.py) are compared at runtime and "
        "must be bumped in the same commit"
    )


# --------------------------------------------------------------------------
# (8) The derived capability list equals the frozen PR5 wire response
# --------------------------------------------------------------------------

# The explicit SCRIPT_CAPABILITIES list as PR5 shipped it (script 1.8.0),
# reproduced verbatim so the advertise-flag derivation can never silently
# widen or shrink the get_script_info wire response (docs/REFACTOR_PLAN.md
# §3.6: the dispatchable set is deliberately wider than the advertised one).
# Changing the advertised surface is a deliberate act: edit BOTH the COMMANDS
# advertise flag and this snapshot, and bump SCRIPT_VERSION.
PR5_SCRIPT_CAPABILITIES = [
    "get_session_info",
    "get_track_info",
    "get_script_info",
    "get_clip_notes",
    "get_device_parameters",
    "get_session_snapshot",
    "set_device_parameter",
    "create_midi_track",
    "create_audio_track",
    "create_clip",
    "create_audio_clip",
    "add_notes_to_clip",
    "load_instrument_or_effect",
    "load_browser_item",
    "get_arrangement_clips",
    "duplicate_session_clip_to_arrangement",
    "create_locator",
    "delete_clip",
    "clear_notes_from_clip",
    "get_track_routing",
    "delete_track",
    "delete_device",
    "set_track_volume",
    "set_track_pan",
    "set_track_mute",
    "create_return_track",
    "set_track_arm",
    "set_track_monitoring",
    "save_set",
    "set_track_send",
    "set_count_in",
    "back_to_arrangement",
    "set_track_routing",
    "set_clip_gain",
    "set_arrangement_clip_name",
    "switch_to_arrangement_view",
    "set_current_song_time",
    "get_browser_tree",
    "get_browser_items_at_path",
]


def test_derived_capabilities_equal_the_pr5_snapshot():
    table = _commands_table()
    derived = sorted(name for name, row in table.items() if row[3])
    assert len(PR5_SCRIPT_CAPABILITIES) == 39  # the PR5 count, pinned
    assert derived == sorted(PR5_SCRIPT_CAPABILITIES), (
        f"advertised-command drift — "
        f"gained: {sorted(set(derived) - set(PR5_SCRIPT_CAPABILITIES))}, "
        f"lost: {sorted(set(PR5_SCRIPT_CAPABILITIES) - set(derived))}"
    )
