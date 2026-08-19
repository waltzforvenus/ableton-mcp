# Merging upstream — the playbook

How to merge [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp)
into this fork without repeating the 2026-08 merge. That merge (`4878234`)
resolved the conflicts correctly *in its commit message* but not in the code:
the fork's device-parameter handlers were "kept" in the dispatch code while
upstream's incompatible versions silently won in the Remote Script's class
body, and `get_device_parameters` / `set_device_parameter` were broken at
runtime for months. Nothing caught it because nothing executed that code
outside Live. The guardrail suite now catches that whole failure class
mechanically — but only if you run it, and only if the merge lands in the
right files. That is what this document is for.

Read CLAUDE.md's hard rules first. They all apply doubly during a merge,
because upstream violates most of them.

## 1. Where upstream's files land here

Upstream is two single files plus packaging. The fork restructured its half;
upstream did not, so every merge crosses this mapping:

| Upstream | Here | How it merges |
|---|---|---|
| `MCP_Server/server.py` (tools, transport, everything) | `src/ableton_mcp/tools.py` — the controllers, kept **in upstream's tool order with byte-identical docstrings** precisely so this diff stays side-by-side readable | Manual port. Diff upstream's `server.py` against its merge base, then apply each tool-level change via the checklist in §3. Never try to textually merge upstream's `server.py` into any file here. |
| `AbletonMCP_Remote_Script/__init__.py` | `remote_script/__init__.py` | Direct merge. The file was `git mv`ed, and git follows the rename during merges — conflicts appear in `remote_script/__init__.py` and you resolve them there. This is why the Remote Script deliberately stays one file. |
| `MCP_Server/bundled_ableton_remote_script/AbletonMCP_init.py` | `src/ableton_mcp/bundled_ableton_remote_script/remote_script_init.py` | **Never merged. Always regenerated.** Take ours (or anything — it is about to be overwritten), finish resolving the canonical Remote Script, then run `uv run ableton-mcp-install-script --sync-bundle`. Upstream's bundled copy once shipped `HOST = "0.0.0.0"` and a script missing every fork tool; `tests/test_bundle_identity.py` fails the build if the regeneration step is forgotten. |
| `MCP_Server/remote_script_install.py` | `src/ableton_mcp/remote_script_install.py` | Direct merge (renamed path; git follows). Check `EXPECTED_REMOTE_SCRIPT_VERSION` afterwards — see §5. |
| telemetry / dataset modules | — | Deleted on sight. See §2. |

Structural note for the server half: a ported upstream tool change usually
also touches `services.py` (the wire call), `presenters.py` (the response
text), and `commands.py` (the command's metadata row). §3 walks through it.

## 2. Standing per-feature resolutions

These are decided. Do not re-litigate them per merge; apply them.

**Device parameters — ours wins, delete theirs entirely, including
class-body definitions.** Upstream's `get_device_parameters` /
`set_device_parameter` address parameters by integer index only and have no
`track_type`; the fork's resolve by name or index, clamp into range, return
display values, and reach return tracks. The wire protocol here speaks the
fork's shape. The lesson of `4878234`: it is not enough to keep our dispatch
entries — if upstream's `_get_device_parameters` / `_set_device_parameter`
method definitions land *anywhere* in the `AbletonMCP` class body, Python
keeps whichever `def` comes later, silently, and dispatch then calls a
signature that no longer exists. Delete upstream's defs, their table rows,
and their server-side tool bodies. `tests/test_remote_script_ast.py`
(`test_no_method_is_defined_twice_in_any_class`) fails the merge if a
duplicate def survives.

**`delete_clip` — upstream's semantics, plus our name echo.** Keep
upstream's no-op-on-empty-slot behavior; keep the fork's `deleted_clip_name`
field in the response. Both halves already implement this combination.

**`HOST` — ours, always.** `HOST = "127.0.0.1"` in
`remote_script/__init__.py`. Upstream binds `0.0.0.0`, exposing an
unauthenticated command-execution socket to the whole local network. Every
merge will conflict or silently adopt theirs here; take ours.
`test_socket_binds_to_loopback` enforces it.

**Telemetry tools — delete on sight, then check what shared a file tail
with them.** Any analytics, usage/crash reporting, install identifiers,
consent flows, `user_prompt`-style parameters, LOM listeners that queue the
user's manual UI actions, or dependencies with a network client: delete,
per CLAUDE.md hard rule 1. Then the lesson of `e57c257`: the deletion hunk
that removed upstream's collection tools also swept away `main()`, which sat
directly after them at end of file, and the `ableton-mcp` console script was
an `ImportError` for months. After any large deletion, look at what the hunk
boundaries touched — and run the suite; `tests/test_entrypoints.py` now
imports every declared console script.

**`load_instrument_or_effect` (wire command) — keep, even though we never
send it.** The tool of that name sends `load_browser_item`. The wire command
stays dispatched and advertised by the Remote Script for third-party clients
speaking the old protocol (plan §9). It has a `commands.py` row for the
cross-half modifying-set equality. Do not "clean it up".

**Orphan rack commands (`inspect_rack`, `map_rack_magnitude`) — dispatchable,
never advertised.** They exist in the Remote Script's `COMMANDS` table with
`advertise=False` and have no server-side tool. The capability-snapshot test
pins this; deciding their fate is scheduled debt (plan §9), not merge
business.

## 3. Porting an upstream tool change (the checklist)

For each tool upstream added or changed, walk the layers in order. Every
step has a guardrail that fails if you skip it — the suite is the checklist's
enforcement, not a substitute for reading this.

1. **Controller — `src/ableton_mcp/tools.py`.** Add or update the `@tool`
   stanza *at the same ordinal position as upstream's file* with a
   byte-copied docstring (docstrings are the model-facing interface and
   they keep future upstream diffs aligned). A controller only coerces
   boundary arguments, calls one service method, and hands the result to a
   presenter. If the ported body does anything else — especially touch the
   wire — `test_mvc_contract.py::test_controllers_never_touch_the_wire`
   objects.
2. **Service — `src/ableton_mcp/services.py`.** One method, usually "build
   the param dict, `self._send(...)`", placed in the same section order as
   `tools.py`. Multi-step logic (decisions between sends) belongs here, not
   in the controller.
3. **Presenter — `src/ableton_mcp/presenters.py`.** The success renderer
   (or an `as_json` alias), named exactly after the tool, plus an
   `ERROR_PHRASES` entry (or an `ERROR_RENDERERS` custom renderer). All
   response text lives here — none in the controller or service.
   `test_every_tool_has_error_wording_and_a_presenter` enforces the
   coverage.
4. **Registry row — `src/ableton_mcp/commands.py`.** `modifying=True` if the
   Remote Script runs it on Live's main thread; an explicit `timeout` if the
   script's queue timeout needs headroom (server ≥ script + 5 s);
   `gated=True` unless the command is in `handshake.LEGACY_CAPABILITIES`
   (the floor) — `test_every_registry_row_is_gated_unless_floor_or_probe`
   rejects ungated non-floor rows. Consider `min_script_version` (§4 below).
5. **Remote Script — `remote_script/__init__.py`.** The `_your_command`
   handler (every parameter defaulted — dispatch calls `**params`) and its
   row in the `COMMANDS` table: `(method, main_thread, queue_timeout,
   advertise)`. State changes must set `main_thread=True`. **Set the
   advertise flag deliberately**: `True` puts it in `SCRIPT_CAPABILITIES`
   (part of the frozen `get_script_info` wire response); the dispatchable
   set is deliberately wider than the advertised one, and
   `test_derived_capabilities_equal_the_pr5_snapshot` pins the advertised
   set against its snapshot.
6. **Capability / version decision.** A genuinely new command on a gated row
   gives old-script users the friendly "re-run the installer" message
   automatically. If an *existing* command's behavior changed such that old
   scripts advertise it but serve it wrongly, add
   `min_script_version="<new version>"` to its row — the name-based gate
   alone cannot tell a repaired script from a broken one that already
   advertises the name (this is exactly how 1.7.0 lied about the
   device-parameter pair).
7. **Regenerate the bundle**: `uv run ableton-mcp-install-script
   --sync-bundle`. Never edit the bundled copy by hand.
8. **If the wire surface changed** (new/changed command, param shape,
   response shape): bump `SCRIPT_VERSION` in `remote_script/__init__.py`
   and `EXPECTED_REMOTE_SCRIPT_VERSION` in
   `src/ableton_mcp/remote_script_install.py` **together, in the same
   commit** — `test_script_version_matches_expected_version` enforces the
   pairing, and reverting the commit then reverts both.
9. **Docs and snapshots**: README tool-reference row († marker if the tool
   is fork-only), `tests/data/tool_names.txt` if the tool surface changed,
   golden cases for the new behavior (`tests/golden_cases.py`, then
   `uv run python tests/record_goldens.py` — the goldens diff goes in the
   commit message), and a CHANGELOG entry.
10. **Run the suite before the manual sweeps**: `uv run pytest`. It
    mechanically catches every historical failure class: duplicate class
    defs and arity breaks (`test_remote_script_ast`), dead console scripts
    (`test_entrypoints`), bundle drift (`test_bundle_identity`), cross-half
    list drift and timeout gaps (`test_cross_half_contract`), ungated
    commands, service sends without registry rows, tools without presenters
    (`test_mvc_contract`), silent tool loss (`test_tool_surface`), README
    drift (`test_readme_sync`), and telemetry vocabulary or novel egress
    imports (`test_no_telemetry`). Then do the manual pass over CLAUDE.md's
    hard rules anyway — tests catch the known classes, not the next one.

## 4. Version-skew risks while users straddle a release

The two halves upgrade at different times: the package upgrades when the
user's MCP client re-resolves it; the Remote Script only upgrades when the
user re-runs `ableton-mcp-install-script` **and restarts Live**. The stated
policy (plan §4):

- **Script older than expected** — gated commands return the installer
  message; ungated floor commands keep working. Commands with a
  `min_script_version` floor return the message even when the old script
  *advertises* them (the 1.7.0 device-parameter case: advertised but broken;
  the version compare is what protects the user from an in-Live TypeError).
- **Script newer than expected** (user ran the installer, then rolled the
  package back) — log a warning, proceed. The newer script's command surface
  is a superset; nothing the older server sends is missing.
- **No handshake at all** (Ableton not running when the server started — the
  common case): the gate answers only from a *successful* handshake. It
  tries one lazily before gating; if Live is still unreachable, the gate
  passes and the send fails with the truthful connection error — never a
  misleading "re-run the installer" about a script nobody has seen.
- **Stale handshake** — the transport invalidates the cached handshake
  whenever it establishes a new socket, so restarting Live with a different
  script re-gates correctly on the next call.
- **Rollback rule** — the `SCRIPT_VERSION` /
  `EXPECTED_REMOTE_SCRIPT_VERSION` pair always changes in one commit, so any
  revert keeps them consistent.

## 5. Release checklist

1. Merge lands on the default branch with `uv run pytest` and `uv build`
   green in CI.
2. Bump `version` in `pyproject.toml`; if the wire surface changed during
   the cycle, the script-version pair was already bumped by the commit that
   changed it (§3 step 8).
3. Write the CHANGELOG entry. If the Remote Script version changed, the
   entry must tell users to re-run `ableton-mcp-install-script` and restart
   Live — see the 1.8.0 entry for the shape.
4. Tag the release (`git tag vX.Y.Z && git push origin vX.Y.Z`). The
   Release workflow (`.github/workflows/release.yml`) re-runs the suite,
   builds, and attaches the wheel and sdist to the GitHub Release
   automatically — that is the publish step.
5. Docker / smithery images build **from the tag, not the branch**.
6. Announce the reinstall requirement where users will see it. The
   handshake's version-mismatch warning is a server-side log line that users
   driving the server through an MCP client never see; the discoverable
   check is the `get_remote_script_info` tool, and the min-version gate's
   message text is what actually reaches the model. Do not rely on the log
   line.
