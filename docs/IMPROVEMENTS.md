# Improvement backlog

Derived from a four-day production session (2026-08-25 to 08-28) in which this fork drove a
complete shoegaze track: arrangement, drum programming, mixing, and the vocal and bass rigs.
Eight agents mined the session transcript for friction, so every item below is grounded in
something that actually cost time or caused a bug, with measured counts where they exist.
A skeptic pass then checked each proposal against the real Live Object Model and this codebase.

Ranked by impact-to-a-real-workflow divided by effort. Read `CLAUDE.md`'s add-a-command
checklist before starting any of these: each new command touches both halves plus goldens,
the version pair, the bundled script, the README table and `tests/data/tool_names.txt`.

## Build first

### Parameter-name aliases (arrangement_time, parameter_name, file_path)

*Effort: hours*

**Why.** 11 round-trips lost to zero work, and destination_time was guessed wrong 7 times across days — it never stuck. Server-only: no remote-script change, no SCRIPT_VERSION bump, no Live restart. Highest ratio on the list by a wide margin.

**Sketch.** No LOM at all: add a second Optional param and coerce in the controller in src/ableton_mcp/tools.py — do NOT use a pydantic alias, since the tool decorator (tools.py:33-58) hands FastMCP an arg model without populate_by_name and the alias would become the only accepted key.

### set_device_parameters (plural) — dial a whole device in one call

*Effort: half a day*

**Why.** 257 calls hitting 37 distinct targets, 18.3 min of wall clock, longest unbroken run 24 calls. Sell it on round-trips only: a readback at the end of the same main_thread_task is still the same tick, so it will report the same stale pre-write values — just 37 times fewer.

**Sketch.** Loop `DeviceParameter.value = clamped` over a params dict inside one main_thread_task, reusing _set_device_parameter's name-or-index resolve and clamp (remote_script/__init__.py:640-690); return a per-parameter landed/clamped/not-found row.

### duplicate_to_arrangement accepts destination_times (and a full placements list)

*Effort: 1 day*

**Why.** 465 calls, 63.5 min — the #1 tool by count AND time, over a third of all round-trip time, arriving in 80 contiguous runs with no reasoning in between (largest single run: 76 calls). Per-source lists collapse it to 199; the placements form to 80, saving ~50 min a session.

**Sketch.** Loop `Track.duplicate_clip_to_arrangement(clip, t)` over the list inside ONE main_thread_task; return per-placement rows plus what landed (never a bare raise — placement 40 of 76 failing must not erase the 39 that worked), keep the scalar destination_time working, and set min_script_version on the row.

### Refuse arrangement stamps that leave a looping clip's phase reset (refusal half only)

*Effort: 1-2 days*

**Why.** The single worst correctness incident of the session: three hat stamps silently scrambled bars 60-68, 156-164 and 204-208, inventing a phantom crash and a ghost snare, caught only by snapshot diffing. Pure geometry, no new LOM. Ship this in the same release as the batching item above or before it — batching without the guard corrupts 76 positions per call instead of 1.

**Sketch.** Before calling `Track.duplicate_clip_to_arrangement`, scan `Track.arrangement_clips` for any clip with `Clip.looping` true that the stamp would leave a right-hand survivor of (generalise _region_occupied at :1690 to return victims) and refuse, naming the victim and the two safe shapes; always return the overlapped clips' before/after start_time/end_time/loop_start. Give _duplicate_arrangement_clip (:2065) the same guard — identical LOM call at :2089. Widen the trigger past strictly-interior stamps: a head-covering stamp also advances a survivor's content window. Leave allow_mid_clip_stamp's repair path unbuilt until marker re-phasing is verified in real Live.

### Scope get_session_snapshot (tracks, sections, and three default-off bloat flags)

*Effort: 1-2 days*

**Why.** 88% failure — 21 of 24 calls blew the output cap even with both existing flags off, then cost 30 follow-up Bash/Read calls to parse dump files. Every real use was one section of a handful of tracks. Flipping the three defaults alone takes a mature lean snapshot from 107 KB to roughly 45 KB.

**Sketch.** Pure serializer work, no new LOM: default-off `include_warp_markers` (`Clip.warp_markers`, currently emitted in _serialize_clip_common at :2814 gated by NEITHER flag — 73-81% of early snapshots), `include_rack_chains` (`Device.chains`, 9.5 KB per drum rack) and `include_empty_slots`; add tracks/sections filters. Deliberate payload break: rewrite goldens and bump get_script_info's advertised snapshot_schema from v2 to v3.

### MIDI note writes on arrangement clips (arrangement=True on the three note tools)

*Effort: 1-2 days*

**Why.** Today changing one arranged bar means clear + re-add on the Session clip then re-stamp 14-15 positions. The fork already READS arrangement notes through the same view-agnostic helper, so the write path is a resolver swap. Be honest about the win: this does NOT end the re-stamp treadmill (15 placements are 15 independent clips), it makes surgical in-place edits possible and removes the reason to stamp over live material at all.

**Sketch.** Route _add_notes_to_clip (:1109), _clear_notes_from_clip (:1397) and _get_clip_notes (:2935) through _resolve_arrangement_clip (:1640) on arrangement=True — the exact pattern _set_clip_gain already uses (:756); `Clip.set_notes` is the same call on an arrangement clip, which _serialize_arrangement_clip (:2839) already proves responds. Verify first what set_notes does with notes past the clip's end_marker window, and set min_script_version on all three rows or an older script dies on a raw TypeError.


## Worth doing

### Two-tick verify pass in the dispatcher (the keystone)

*Effort: weekend block*

**Why.** Root cause under five separate items: every 'did it land?' check re-reads inside the same callback that wrote, and the file's own comment says the read can stay stale for the whole tick. Costs ~100 ms per verified command against a 4.20 s mean. Large and it touches the riskiest function in the file, so it ranks below the cheap wins on impact-over-effort — but schedule it as a dedicated block, because create_locator's settle, delete_device's retry, add_notes' count and create_audio_clip's warp check all get honest the day it lands.

**Sketch.** Add a separate `VERIFY = {command: method}` dict — do NOT widen the 4-tuple COMMANDS row, that rewrites ~60 rows and the unpack at :351 for a flag most rows never set — and a second `self.schedule_message(1, verify_task)` in _process_command (:377) that re-reads `DeviceParameter.value` / `str_for_value` on tick N+1 and returns a `settled` boolean; raise each affected row's queue_timeout and its socket timeout in lockstep to keep the >=5 s headroom test_cross_half_contract enforces.

### Echo the resolved track name in every modifying result; add optional expect_track_name

*Effort: half a day for the echo, 1 day for the guard*

**Why.** The out-of-range errors were the lucky half — 12 of them. The silent half is an in-range-but-wrong track after the user deletes something mid-session, which is how an Auto Pan meant for the GUITARS bus landed on BASS. The echo half is free, needs no new parameter, and lets a caller who forgot the guard still diff afterwards. Strictly cheaper than making all ~40 tools take names.

**Sketch.** Read `Track.name` in _resolve_track (:577) — one main-thread attribute read, no extra tick — refuse before writing when expect_track_name mismatches, and name what is actually at that index.

### delete_device must verify — and must NOT retry

*Effort: half a day*

**Why.** The handler returns len(track.devices) with no before-count, so a no-op delete prints 'Deleted X; M devices remain' and a highest-index-first sweep then removes the wrong devices. Drop the proposed retry: if the readback was merely stale rather than the delete having failed, a blind second delete removes the NEXT device and destroys a chain that was dialled in.

**Sketch.** Capture len(track.devices) and `Device.name` before `Track.delete_device` (:696-707), compare after, and return `remaining_devices: [{index, name}]` instead of a bare count so a sweep can re-anchor on names; retry only on a settled read once the deferred-verify item lands.

### duplicate_track

*Effort: half a day*

**Why.** Double-tracking is structural to this record and each double was assembled by hand — create at an index, re-route, re-pan, reload devices one at a time. Also makes the index shift predictable (+1 below the source) instead of choosing an insert position and re-reading everything. Best line-count-to-value ratio in the whole set.

**Sketch.** `Song.duplicate_track(index)` copies devices, mixer, routing and clips and inserts below the source; expect Live's name suffix (follow with set_track_name), expect the copy to become selected, and catch the edition track-cap raise rather than assuming success.

### Address arrangement clips by start_time, with a plural start_times delete

*Effort: 1-2 days*

**Why.** clip_index is a positional ordinal that renumbers on every delete, which forced 63 get_arrangement_clips calls (27 of them lone singletons) and a hand-maintained highest-index-first ritual across 19 delete runs. Six of eight mining agents logged it independently. The plural delete is the part that removes the hazard rather than documenting it.

**Sketch.** Match `Clip.start_time` over `Track.arrangement_clips` within 1e-3 — the same match-by-value shape _find_trim_stamp already uses (:1682) — and note Live permits no overlaps on a track, so start_time is a genuine unique key; list near-misses in the error the way _jump_to_locator lists cues, since a hand-dragged clip can sit on an arbitrary fraction.

### create_audio_clip: warp=False at import, plus an implied-tempo cross-check

*Effort: 1 day*

**Why.** Six byte-identical-length stems each got a different Auto-Warp guess (117.9, ~240 x4 at double speed, 151 BPM) and the tool reported success with a plausible-looking beat length. The user found it before the assistant did. set_clip_warp exists but only as a separate call after the damage — it cannot reach the clip until the import has already been reported as fine.

**Sketch.** Set `Clip.warping = False` inside the same main_thread_task as `ClipSlot.create_audio_clip`, and read back `Clip.warping` / `Clip.warp_mode` / `Clip.length` against an expected_beats argument; Live analyses imports asynchronously, so put the tempo comparison on a deferred tick, and say plainly that the WAV-header fallback gives no cross-check for AIFF/FLAC/mp3.

### add_notes_to_clip must report what Live accepted, with expect_count

*Effort: 1 day*

**Why.** The controller discards the service result and the presenter prints len(notes) from its own argument, so 'Added 262 notes' prints whether Live took 262 or none. Note the real cause of the 262-sent/172-landed loss: the socket buffers and retries json.loads until it parses, so a truncated command cannot execute — the short list was emitted above the server, where a before/after delta stays silent. expect_count is the load-bearing part, not a nicety.

**Sketch.** Count with `Clip.get_notes_extended` (falling back to `Clip.get_notes`, the fallback _clear_notes_from_clip already has at :1424-1432) before and after `Clip.set_notes`, return {requested, added, clip_note_count}, and make the docstring tell the model to state its intended total when chunking.

### erase_arrangement_region (erase only — drop the split)

*Effort: 2 days*

**Why.** Every bleed cut, guitar entry move and gain-fade slice was hand-driven: generate a WAV, import it, stamp it, delete the erasers highest-index-first, delete the session slot. Five calls and a WAV per cut, and two leftover erasers pointed at a scratchpad file that would rot into broken media. Bringing the eraser under script control fixes that class too.

**Sketch.** Re-aim _trim_arrangement_clip's shipped recipe (:1701) at a span: size an eraser to the region (beats to seconds at the current tempo, then re-measure the stamped footprint the way :1809 does rather than inferring it), stamp with `Track.duplicate_clip_to_arrangement`, delete with `Track.delete_clip`, sweep shards. Inherits trim's hard requirement of one empty Session slot, and must carry the mid-clip stamp guard.

### display_min / display_max on get_device_parameters (plus off-scale refusal)

*Effort: half a day*

**Why.** Scales are mixed within a single device — Limiter's Input Gain is normalised while Lookahead is a raw quantized index — which is why the session accumulated a private conversion table (EQ Eight's 0.2992*log10(f/10), 'Input Gain 0.625 -> +6.0 dB'). The read half is the better half: it makes the scale legible before the write instead of discoverable after it.

**Sketch.** Emit `DeviceParameter.str_for_value(min)` / `str_for_value(max)` as display_min/display_max in _get_device_parameters (:604), plus value_items for quantized params; then refuse when a value overshoots by more than one full range while still clamping small nudges — and update CLAUDE.md's 'prefer clamping over erroring' line in the same commit, since it cites this exact function.

### create_scene / delete_scene / duplicate_session_clip

*Effort: 2 days*

**Why.** Session slots are a fixed scratch space, so a full track meant borrowing an occupied slot and restoring its single note by hand, and the pattern library drifted until clips named 'Verse beat' held verse-2 hats. A free row per tier also removes trim_arrangement_clip's hard failure mode — it refuses outright when a track has no empty slot (:1774-1779).

**Sketch.** `Song.create_scene(index)`, `Song.delete_scene(index)` (handle Live's refusal to delete the last scene, and catch the edition scene cap — it cannot be pre-checked), and `ClipSlot.duplicate_clip_to(target_slot)` hasattr-probed the way the code already probes remove_notes_extended.

### set_clip_region — markers on Session clips

*Effort: 1-2 days*

**Why.** Placing seconds 12-28 of an existing take currently means baking a new WAV with numpy, importing it, and fighting Live's tempo guess (which is why renders had to be power-of-two lengths). On a Session clip the marker window IS the footprint, so this extracts any region with no render and no eraser.

**Sketch.** Write `Clip.start_marker` / `end_marker` / `loop_start` / `loop_end` / `looping`; the unit handling is most of the work and the whole trap — read `Clip.warping` and convert, since a warped clip's markers are in beats and an unwarped clip's are in seconds. Ship without `Clip.crop()` until it is probed: it is documented, never called by this fork, and destructive with no undo for audio.


## Nice to have

- Paste the dead-ends list below into README's Limitations as a Dead Ends section — an afternoon, and it is the thing that stops fade_arrangement_clip being rebuilt a second time. This is the genuinely valuable half of the probe_lom proposal.
- Pagination and filtering on get_browser_items_at_path (limit / offset / name_contains, plus a total and per-folder child counts). Small, filters inside the script so nothing oversized crosses the socket. Record the docstring divergence in docs/UPSTREAM.md — this is an upstream-inherited method. Separately worth a look: get_browser_tree(category_type='instruments') returning an empty tree is a distinct bug, and fixing it would make the cheap-preview problem mostly disappear.
- Positional note tuples [pitch, start, duration, velocity] plus a replace flag on add_notes_to_clip (Clip.remove_notes_extended then Clip.set_notes, reusing the fallback at :1424-1432). Roughly 100K output tokens back, and turns the 13 clear+add rituals into one call. Document that the tuple form cannot carry probability / velocity_deviation / release_velocity, which _notes_from_clip does read back — a read round-tripped through a tuple write drops them.
- create_locator's settle check moved to tick N+1, and _set_current_song_time's settled flag with it — a ~60% first-attempt failure rate on the worst tool in the set. This is a rider on the deferred-verify keystone, not a separate project; it cannot be built before it.
- delete_locator via Song.set_or_delete_cue() at the cue's own position, matching name first then beat within 1e-3, reusing create_locator's settle-verify block and refusing without toggling. Sequence it after the locator settle fix — until then it inherits the same 60% failure rate, and a wrong-position toggle CREATES a stray rather than deleting one.
- set_clip_color / set_track_color (Clip.color_index is a bounded palette int that raises out of range, Clip.color is 0xRRGGBB — accept both, echo what landed). Smallest item and the weakest evidence; ride it on a release already touching the clip handlers. Warn in the presenter that a track colour re-tints clips the caller did not name.
- move_device — probe `Song.move_device(device, target, insert_index)` first, and in the same pass try the cheaper trick that needs no new command: set song.view.selected_track and track.view.selected_device before _load_browser_item's load, since Live's browser inserts relative to the selected device. Note the Airwindows evidence actually belongs to delete_device verification, not to reordering.
- probe_lom as a debugging aid only. Be clear-eyed: it would have caught NEITHER killed feature — Clip.automation_envelope resolves and returns None, Clip.start_marker resolves and writes land uselessly — and edition caps are not probeable at all. Its real content is get_script_info finally reporting Live.Application.get_application().get_version_string(), which is a genuine gap today. Document any output as 'this member resolves', never as 'this feature works'.
- Undo batching — probe before building. begin_undo_step/end_undo_step are documented but never called by this fork, they take NO label argument (the proposal's 'labelled by command name' is not possible), whether they merge LOM writes into one Live undo entry on 12.4.3 must be verified in Live rather than against the fake, and begin_batch/end_batch as separate wire commands leaves an undo step open across ticks — this user both clicked the drums mid-edit and quit Live mid-edit, so it needs a timeout. Medium effort minimum, not small: it modifies _process_command.
- Precision warp-marker writes (Clip.clear_all_warp_markers / add_warp_marker / move_warp_marker / remove_warp_marker) — unverified on this build, and the six-stems incident is already solved by the shipped set_clip_warp turning warping off, with none of the .asd donor map's ~1.8% error. This is a tier-2 alignment feature, not a rescue. If the members do not resolve, add them to the dead-end registry instead.
- place_notes_in_arrangement — largely superseded once arrangement note writes and batched stamps land; the residual win is just the 102 create_clip/delete_clip round-trips. Critically, do NOT implement borrow-and-restore: a borrowed slot's audio clip, warp markers, envelopes, launch settings, colour and name cannot be faithfully restored, so it would quietly lose data. Refuse when no slot is free and point at create_scene.
- track: Union[int, str] across all ~40 tools — same protection as the expect_track_name guard for far more cost (~40 changed JSON schemas the model reads, ~40 goldens, and a numeric-string ambiguity rule). Two Elizabeth vocal tracks existed in this session and Live auto-names duplicates, so 'errors on ambiguity' will fire often. Do the guard first, then add names only to the tools that carry a batch.

## Confirmed dead ends

Verified impossible on Live 12.4.3. Recorded so nobody rebuilds them: `fade_arrangement_clip`
was already written once, passed 424 tests against the fake, and had to be deleted in review.

### Any in-tick retry or same-tick readback verification

_create_locator already does exactly this — write current_song_time, re-read, write again, re-read (:1533-1539) — and the fork's own comment at :1342 says the read can stay stale for the rest of the tick even though the write landed. A second read in the same callback returns the same stale value by construction, which is why the 1.13.0 fix left a retry on essentially every call. There is no yield primitive, and sleeping on Live's main thread blocks audio and UI. Only a later tick can clear. (Also: the 'fold in the playhead move' half of that proposal already exists — create_locator takes a time argument and moves the playhead itself.)

### Clip fades and arrangement automation lanes

Clip.automation_envelope returns None for arrangement clips and create_automation_envelope raises there. fade_arrangement_clip was built, passed 424 tests against the fake, and was deleted in review (CHANGELOG.md:84-88). Fades stay a baked-render job.

### Moving an Arrangement clip's footprint by writing markers

Verified inert on real Live 12.4.3 and shipped inert through scripts 1.9.0-1.11.0: start_marker/end_marker writes land but never move the footprint, so the readback guard refused every trim. The writes do change what plays (Clip.position slides the content window), which is why the Session-clip set_clip_region item above is still worth building — but nothing resizes an arrangement clip except the stamp path.

### Creating group tracks

No LOM function creates one and Track.group_track is read-only. The GUITARS / BASS / ELIZABETH buses built with set_track_routing are the only path, and they are also why routing survived every index shift in the session.

### Render, export, and save_set on 12.4.3

Rendering and export are UI-only with no LOM entry point. save_set returns 'Tried: []' on this build. Both are permanent; do not re-attempt.

### Detecting Live's edition by probing

The LOM exposes no edition or product property, so Intro's 2-return / 16-track / 16-scene caps cannot be read ahead of time — they surface only as a raise from create_return_track or create_scene. Every new tool that can hit a cap must catch and report honestly rather than pre-check.

### A gapless split_arrangement_clip

There is no LOM split and no zero-width stamp, so any eraser-based split leaves a gap of the eraser's own width. A correct split is composable from verbs already shipped — duplicate to empty timeline, trim the copy's head, move it to the cut point — but that is 3+ stamps and a marker-preservation assumption per split, not a thin handler. Scope it separately or not at all; erase_arrangement_region is the part that earns its keep.

