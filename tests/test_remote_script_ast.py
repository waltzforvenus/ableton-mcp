"""
AST guardrails for the Ableton Remote Script.

The Remote Script imports `_Framework` and can only run inside Live, so these
tests never import it — they `ast.parse` the canonical source and check the
structural invariants whose silent violation shipped real regressions
(docs/REFACTOR_PLAN.md §1 and the §5 guardrail table):

- the 2026-08 upstream merge (commit 4878234) left four methods defined twice
  in the `AbletonMCP` class body; Python keeps the later definition, so the
  dispatch code calls the losing fork signatures and the device-parameter
  pair TypeErrors at runtime,
- the same merge left duplicate/unreachable dispatch branches, duplicate
  membership-list entries, and branches that call methods which do not exist,
- upstream binds the command socket to 0.0.0.0; this fork must stay loopback.

Tests that detect a currently-shipped defect are strict xfails naming the
commit that caused it; each fix (plan PR5) flips exactly one marker.

Runs anywhere — no Ableton, no network.
"""

import ast
from functools import lru_cache
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_SCRIPT = REPO_ROOT / "AbletonMCP_Remote_Script" / "__init__.py"
SERVER = REPO_ROOT / "MCP_Server" / "server.py"


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


def _process_command():
    fn = _winning_defs(_ableton_mcp_class()).get("_process_command")
    assert fn is not None, "AbletonMCP._process_command not found"
    return fn


def _module_constant(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"no module-level assignment to {name}")


def _walk_skipping_nested_defs(fndef):
    """Walk a function body without descending into nested function defs."""
    stack = list(fndef.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _self_method_calls(root):
    """Every `self.<name>(...)` Call node in the subtree, nested defs included."""
    calls = []
    for node in ast.walk(root):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        ):
            calls.append(node)
    return calls


def _command_eq_literals(nodes):
    """String literals from `command_type == "..."` comparisons."""
    found = []
    for node in nodes:
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "command_type"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and isinstance(node.comparators[0], ast.Constant)
            and isinstance(node.comparators[0].value, str)
        ):
            found.append(node.comparators[0].value)
    return found


def _command_membership_lists(nodes):
    """The list literals from `command_type in [...]` tests, entries in order."""
    lists = []
    for node in nodes:
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "command_type"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
            and isinstance(node.comparators[0], (ast.List, ast.Tuple))
        ):
            lists.append([ast.literal_eval(elt) for elt in node.comparators[0].elts])
    return lists


def _server_modifying_list():
    """The `is_modifying_command = command_type in [...]` list in server.py,
    entries in order (duplicates preserved)."""
    for node in ast.walk(_parse(SERVER)):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "is_modifying_command"
                for t in node.targets
            )
            and isinstance(node.value, ast.Compare)
            and isinstance(node.value.comparators[0], (ast.List, ast.Tuple))
        ):
            return [ast.literal_eval(e) for e in node.value.comparators[0].elts]
    raise AssertionError("is_modifying_command membership list not found in server.py")


def _accepts_call(fndef, call):
    """Can this def bind this call? Positional/keyword binding check, self
    excluded. Star-args in the call are unknowable statically -> accepted."""
    if any(isinstance(a, ast.Starred) for a in call.args):
        return True
    kw_names = [kw.arg for kw in call.keywords]
    if None in kw_names:  # **kwargs expansion
        return True

    args = fndef.args
    params = [p.arg for p in (args.posonlyargs + args.args)][1:]  # drop self
    n_defaults = len(args.defaults)
    required = params[: len(params) - n_defaults] if n_defaults else params
    kwonly = [p.arg for p in args.kwonlyargs]

    n_pos = len(call.args)
    if args.vararg is None and n_pos > len(params):
        return False  # too many positional args
    if args.kwarg is None and any(k not in params + kwonly for k in kw_names):
        return False  # unknown keyword
    if any(k in params[:n_pos] for k in kw_names):
        return False  # parameter bound twice
    covered = set(params[:n_pos]) | set(kw_names)
    if any(p not in covered for p in required):
        return False  # required parameter left unbound
    kwonly_required = [
        p.arg for p, d in zip(args.kwonlyargs, args.kw_defaults) if d is None
    ]
    if any(p not in kw_names for p in kwonly_required):
        return False
    return True


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

@pytest.mark.xfail(
    strict=True,
    reason="upstream merge 4878234 left _create_audio_track, _delete_clip, "
    "_get_device_parameters and _set_device_parameter each defined twice in "
    "the AbletonMCP class body — the later (upstream) definitions win; see "
    "docs/REFACTOR_PLAN.md §1 item 1 (fix scheduled as PR5)",
)
def test_no_method_is_defined_twice_in_any_class():
    duplicated = {}
    for cls in _class_defs(_parse(REMOTE_SCRIPT)):
        dupes = _duplicates([name for name, _ in _direct_methods(cls)])
        if dupes:
            duplicated[cls.name] = sorted(dupes)
    assert duplicated == {}, f"methods defined twice: {duplicated}"


# --------------------------------------------------------------------------
# (2) Every dispatch call binds against the WINNING definition
# --------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason="upstream merge 4878234: dispatch passes the fork signatures "
    "(3-arg _get_device_parameters, 5-arg _set_device_parameter) but the "
    "winning class-body definitions are upstream's narrower ones, so both "
    "commands TypeError inside Live; see docs/REFACTOR_PLAN.md §1 item 1 "
    "(fix scheduled as PR5)",
)
def test_dispatch_calls_match_winning_method_signatures():
    winning = _winning_defs(_ableton_mcp_class())
    mismatches = []
    for call in _self_method_calls(_process_command()):
        name = call.func.attr
        fndef = winning.get(name)
        if fndef is None:
            continue  # nonexistent methods are test (4)'s defect
        if not _accepts_call(fndef, call):
            mismatches.append(
                f"line {call.lineno}: self.{name}() called with "
                f"{len(call.args)} positional arg(s), but the winning "
                f"definition (line {fndef.lineno}) cannot bind them"
            )
    assert mismatches == [], "dispatch/definition arity drift:\n" + "\n".join(mismatches)


# --------------------------------------------------------------------------
# (3) No duplicate command literals across the dispatch ladders
# --------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason="upstream merge 4878234 left dead dispatch branches: "
    "get_device_parameters tested twice, set_device_parameter both in the "
    "main-thread membership list and as its own (unreachable) branch, "
    "create_audio_track handled twice inside main_thread_task; see "
    "docs/REFACTOR_PLAN.md §1 item 3 (fix scheduled as PR5)",
)
def test_no_duplicate_dispatch_literals():
    fn = _process_command()
    top_level = list(_walk_skipping_nested_defs(fn))

    # The top-level ladder: `== "x"` tests plus the contents of the
    # `command_type in [...]` membership lists. Membership lists are deduped
    # here — duplicates *within* one list are test (5)'s defect, not this one.
    ladder = _command_eq_literals(top_level)
    for entries in _command_membership_lists(top_level):
        ladder.extend(dict.fromkeys(entries))
    duplicated = set(_duplicates(ladder))

    # Each nested main_thread_task ladder must be duplicate-free on its own.
    for node in ast.walk(fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not fn:
            duplicated |= set(_duplicates(_command_eq_literals(ast.walk(node))))

    assert duplicated == set(), f"commands dispatched more than once: {sorted(duplicated)}"


# --------------------------------------------------------------------------
# (4) Dispatch only references methods that exist
# --------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason="upstream merge 4878234 kept dispatch branches for "
    "get_browser_categories / get_browser_items, whose handler methods "
    "(_get_browser_categories, _get_browser_items) do not exist anywhere in "
    "the file; see docs/REFACTOR_PLAN.md §1 item 3 (fix scheduled as PR5)",
)
def test_dispatch_references_only_existing_methods():
    winning = _winning_defs(_ableton_mcp_class())
    missing = sorted(
        {
            call.func.attr
            for call in _self_method_calls(_process_command())
            # Handlers are the underscore-prefixed methods; bare self.* calls
            # (log_message, schedule_message, ...) are inherited ControlSurface
            # API this AST check cannot see.
            if call.func.attr.startswith("_") and call.func.attr not in winning
        }
    )
    assert missing == [], f"dispatch calls nonexistent methods: {missing}"


# --------------------------------------------------------------------------
# (5) No duplicate entries inside the modifying-command lists (both halves)
# --------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason="upstream merge 4878234 left duplicate entries in the "
    "modifying-command lists: create_audio_track and delete_clip twice in the "
    "Remote Script's main-thread membership list, set_device_parameter twice "
    "in server.py's is_modifying_command list; see docs/REFACTOR_PLAN.md "
    "Appendix A (fix scheduled as PR5)",
)
def test_no_duplicate_entries_in_modifying_command_lists():
    membership_lists = _command_membership_lists(
        _walk_skipping_nested_defs(_process_command())
    )
    assert membership_lists, "Remote Script main-thread membership list not found"
    problems = {}
    for entries in membership_lists:
        dupes = _duplicates(entries)
        if dupes:
            problems["remote_script_main_thread_list"] = sorted(dupes)
    server_dupes = _duplicates(_server_modifying_list())
    if server_dupes:
        problems["server_is_modifying_command_list"] = sorted(server_dupes)
    assert problems == {}, f"duplicate modifying-command entries: {problems}"


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
    from MCP_Server.remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION

    script_version = _module_constant(_parse(REMOTE_SCRIPT), "SCRIPT_VERSION")
    assert script_version == EXPECTED_REMOTE_SCRIPT_VERSION, (
        "SCRIPT_VERSION (Remote Script) and EXPECTED_REMOTE_SCRIPT_VERSION "
        "(MCP_Server/remote_script_install.py) are compared at runtime and "
        "must be bumped in the same commit"
    )
