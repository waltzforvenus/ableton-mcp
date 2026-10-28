#!/usr/bin/env python3
"""Push MIDI notes from JSON files straight into Ableton Live.

A bulk-note client for the AbletonMCP Remote Script's socket. It speaks the
wire protocol the MCP server speaks — one ``{"type": ..., "params": {...}}``
JSON blob per command to 127.0.0.1:9877, reply read until it parses as JSON —
and asks for the guards the server's tools ask for: ``expect_count`` on every
chunk and ``expect_track_name`` on every write, so whatever the Remote Script
refuses for the server it refuses here too.

Why it exists: a note list sent through the MCP round trip passes through the
model's context twice, read once and re-emitted per tool call. A whole-song
drum part is a few thousand notes, a couple of hundred kilobytes of JSON, and
that is an afternoon of tokens spent on a transfer a generator already wrote
to disk. This moves the file to the clip directly, in seconds, and prints
what Live reported for each chunk — not what was sent.

It is safe beside a running MCP server: the Remote Script accepts any number
of clients (a thread per connection) and serialises every state-changing
command onto Live's main thread, so two clients never interleave a write.

Two input shapes, detected from the file's contents:

  a note file — a JSON list of notes, each
      {"pitch": 36, "start_time": 0.0, "duration": 0.25,
       "velocity": 100, "mute": false}
    pushed to the one clip named on the command line (several files go to
    that clip in order, which is how a generator's ``.0.json``, ``.1.json``
    chunks are laid out):

      python3 scripts/push_notes.py verse1.kt.0.json verse1.kt.1.json \\
          --track 0 --clip 3 --expect-track-name "drums — kick + toms"

  a manifest — a JSON object ``{"targets": [...]}`` (or a bare list of the
    same entries) whose entries each name a note file (relative to the
    manifest) or carry the notes inline, and say where they go:

      {"targets": [
        {"file": "verse1.kt.0.json", "track_index": 0, "clip_index": 3,
         "expect_track_name": "drums — kick + toms",
         "clip_name": "verse1 kt", "clear": true},
        {"file": "verse1.sh.0.json", "track_index": 1, "clip_index": 3,
         "expect_track_name": "drums — hats + snare + cymbals",
         "arrangement": false}
      ]}

      python3 scripts/push_notes.py drums-manifest.json

Every note in every file is validated before the first byte is sent, and the
run stops at the first command Live refuses. Writes are additive, so a run
that stops half-way leaves a half-written clip; it is reported as exactly
that, so the clip can be cleared and pushed again (``--clear`` on the
command line, or ``"clear": true`` on a manifest entry, sends
clear_notes_from_clip first).

Standard library only, on purpose: it runs as ``python3 scripts/push_notes.py``
with nothing installed, and adds nothing to the server's dependency surface.

Exit status: 0 when every chunk landed and Live's counts agree with what was
sent; 1 when Live refused a command, the connection failed, or a count
disagreed; 2 for input this script would not send.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, TextIO

# Notes per add_notes_to_clip call. The Remote Script's receive loop
# accumulates until the blob parses, so the socket would take far more; the
# ceiling is about Live, not the wire. Each write is one task on Live's main
# thread, and a task that runs long is a task the audio and UI threads wait
# behind. 110 kept those tasks short enough to pass unnoticed when 2,513 drum
# notes went in over ~45 calls (Live 12.4.6, Remote Script 1.16.0).
DEFAULT_CHUNK_SIZE = 110

# Seconds to wait for Live's reply to one command. A chunk write normally
# answers well inside a second; the budget is generous because Live answers
# only when its main thread gets round to the task, and a set that is busy
# (loading, saving, a plugin window open) can hold that for a while.
DEFAULT_TIMEOUT = 60.0

NOTE_KEYS = ("pitch", "start_time", "duration", "velocity", "mute")
TARGET_KEYS = frozenset({
    "file", "notes", "track_index", "clip_index", "expect_track_name",
    "clip_name", "arrangement", "clear",
})


class UsageError(Exception):
    """The input could not be used. Nothing was sent; exit status 2."""


class LiveError(Exception):
    """The Remote Script answered a command with ``status: error``."""

    def __init__(self, command_type: str, message: str) -> None:
        super().__init__(message)
        self.command_type = command_type
        self.message = message


class PushFailed(Exception):
    """A command failed mid-run; the message says where and what landed."""


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

class LiveSocket:
    """One connection to the Remote Script socket: send a command, read its
    reply.

    The protocol has no length framing and no request ids: a reply is
    complete when the bytes received so far parse as JSON, and it belongs to
    the last command sent. So exactly one command is in flight per
    connection, always — which is also what makes a second client next to
    the MCP server safe: each has its own connection, and the script
    serialises the writes behind them on Live's main thread.
    """

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None

    def connect(self) -> "LiveSocket":
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=self.timeout)
        except OSError as e:
            reason = e.strerror or str(e)
            raise ConnectionError(
                f"could not connect to {self.host}:{self.port} ({reason}). "
                "Is Ableton Live running with the AbletonMCP control surface "
                "enabled?") from e
        return self

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "LiveSocket":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def send(self, command_type: str,
             params: Optional[Dict[str, Any]] = None) -> Any:
        """Send one command and return its ``result``; raise LiveError on a
        refusal, ConnectionError / TimeoutError on a transport failure."""
        if self._sock is None:
            raise ConnectionError("not connected")
        payload = json.dumps({"type": command_type, "params": params or {}})
        try:
            self._sock.sendall(payload.encode("utf-8"))
            reply = self._read_reply()
        except socket.timeout as e:
            raise TimeoutError(
                f"{command_type}: no reply from Live within "
                f"{self.timeout:g} s") from e
        except ConnectionError as e:
            raise ConnectionError(f"{command_type}: {e}") from e
        except OSError as e:
            raise ConnectionError(
                f"{command_type}: connection to Live lost ({e})") from e
        if reply.get("status") != "success":
            raise LiveError(command_type, str(reply.get("message", reply)))
        return reply.get("result")

    def _read_reply(self) -> Dict[str, Any]:
        assert self._sock is not None
        buffer = b""
        while True:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError(
                    "Live closed the connection "
                    + ("mid-reply" if buffer else "before replying"))
            buffer += chunk
            try:
                reply = json.loads(buffer.decode("utf-8"))
            except UnicodeDecodeError:
                # A multi-byte character split across two recv calls: the
                # rest is on its way. The Remote Script's json.dumps escapes
                # non-ASCII by default, so this is defensive — but a script
                # that ever emits raw UTF-8 must not turn an em dash in a
                # track name into a "connection lost".
                continue
            except ValueError:
                continue  # incomplete JSON — keep reading
            if not isinstance(reply, dict):
                raise ConnectionError(f"unexpected reply from Live: {reply!r}")
            return reply


# --------------------------------------------------------------------------
# Input: notes, targets, manifests
# --------------------------------------------------------------------------

@dataclass
class Target:
    """One clip to write, and everything the write should be guarded by."""

    label: str
    notes: List[Dict[str, Any]]
    track_index: int
    clip_index: int
    expect_track_name: Optional[str] = None
    clip_name: Optional[str] = None
    arrangement: bool = False
    clear: bool = False

    def where(self) -> str:
        view = "arrangement clip" if self.arrangement else "clip"
        return f"track {self.track_index} {view} {self.clip_index}"

    def guard_params(self, with_view: bool = True) -> Dict[str, Any]:
        """The wire parameters every command on this target carries.

        The guard goes on the wire only when the caller gave one: dispatch in
        the Remote Script calls handlers as ``**params``, so a key sent as
        null would still be a keyword an older script has never heard of.
        Same for ``arrangement``, which only scripts ≥ 1.15.0 accept — and
        which the note commands take as a keyword while the name commands
        take it as a different command (``set_arrangement_clip_name``), so
        the name step asks for the params without it.
        """
        params: Dict[str, Any] = {
            "track_index": self.track_index,
            "clip_index": self.clip_index,
        }
        if with_view and self.arrangement:
            params["arrangement"] = True
        if self.expect_track_name is not None:
            params["expect_track_name"] = self.expect_track_name
        return params


def _type_name(value: Any) -> str:
    return {dict: "an object", list: "a list", str: "a string",
            bool: "a boolean", int: "an integer", float: "a number",
            type(None): "null"}.get(type(value), type(value).__name__)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_note(note: Any, where: str) -> None:
    """Refuse anything the Remote Script would silently default or Live
    would reject: the script fills a missing pitch with 60 and a missing
    velocity with 100, which is how a typo becomes a middle-C drum part."""
    if not isinstance(note, dict):
        raise UsageError(f"{where}: expected an object, got {_type_name(note)}")
    missing = [key for key in NOTE_KEYS if key not in note]
    if missing:
        raise UsageError(f"{where}: missing {', '.join(missing)}")
    pitch = note["pitch"]
    if not _is_int(pitch) or not 0 <= pitch <= 127:
        raise UsageError(
            f"{where}: pitch must be an integer 0–127, got {pitch!r}")
    start = note["start_time"]
    if not _is_number(start) or start < 0:
        raise UsageError(
            f"{where}: start_time must be a number ≥ 0 (beats from the "
            f"clip's own start), got {start!r}")
    duration = note["duration"]
    if not _is_number(duration) or duration <= 0:
        raise UsageError(
            f"{where}: duration must be a number > 0, got {duration!r}")
    velocity = note["velocity"]
    if not _is_number(velocity) or not 0 <= velocity <= 127:
        raise UsageError(
            f"{where}: velocity must be a number 0–127, got {velocity!r}")
    if not isinstance(note["mute"], bool):
        raise UsageError(
            f"{where}: mute must be true or false, got {note['mute']!r}")


def validate_notes(notes: Any, where: str) -> List[Dict[str, Any]]:
    if not isinstance(notes, list):
        raise UsageError(
            f"{where}: expected a JSON list of notes, got {_type_name(notes)}")
    for index, note in enumerate(notes):
        validate_note(note, f"{where}: note {index}")
    return notes


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as e:
        raise UsageError(f"{path}: {e.strerror or e}") from e
    except ValueError as e:
        raise UsageError(f"{path}: not valid JSON ({e})") from e


def classify(doc: Any, source: str) -> str:
    """'notes' or 'manifest', from the shape of a parsed file."""
    if isinstance(doc, dict):
        if "targets" in doc:
            return "manifest"
        raise UsageError(
            f"{source}: a JSON object must be a manifest with a "
            f"\"targets\" list; a note file is a JSON list")
    if isinstance(doc, list):
        first = doc[0] if doc else None
        if isinstance(first, dict) and "pitch" not in first and (
                "file" in first or "notes" in first or "track_index" in first):
            return "manifest"
        return "notes"
    raise UsageError(
        f"{source}: expected a list of notes or a manifest, got "
        f"{_type_name(doc)}")


def _require_index(entry: Dict[str, Any], key: str, where: str) -> int:
    if key not in entry:
        raise UsageError(f"{where}: missing {key}")
    value = entry[key]
    if not _is_int(value) or value < 0:
        raise UsageError(f"{where}: {key} must be an integer ≥ 0, got {value!r}")
    return value


def _optional_str(entry: Dict[str, Any], key: str, where: str) -> Optional[str]:
    value = entry.get(key)
    if value is not None and not isinstance(value, str):
        raise UsageError(f"{where}: {key} must be a string, got {value!r}")
    return value


def _optional_bool(entry: Dict[str, Any], key: str, where: str) -> bool:
    value = entry.get(key, False)
    if not isinstance(value, bool):
        raise UsageError(f"{where}: {key} must be true or false, got {value!r}")
    return value


def targets_from_manifest(doc: Any, base_dir: Path, source: str) -> List[Target]:
    if isinstance(doc, dict):
        extra = sorted(set(doc) - {"targets"})
        if extra:
            raise UsageError(f"{source}: unknown top-level keys {extra}")
        entries = doc["targets"]
    else:
        entries = doc
    if not isinstance(entries, list):
        raise UsageError(f"{source}: \"targets\" must be a list")

    targets: List[Target] = []
    for index, entry in enumerate(entries):
        where = f"{source}: target {index}"
        if not isinstance(entry, dict):
            raise UsageError(
                f"{where}: expected an object, got {_type_name(entry)}")
        unknown = sorted(set(entry) - TARGET_KEYS)
        if unknown:
            # A misspelt guard is a guard that is not there; refuse rather
            # than push unguarded on a typo.
            raise UsageError(
                f"{where}: unknown keys {unknown} (known: "
                f"{', '.join(sorted(TARGET_KEYS))})")
        has_file, has_notes = "file" in entry, "notes" in entry
        if has_file == has_notes:
            raise UsageError(
                f"{where}: give exactly one of \"file\" or \"notes\"")
        if has_file:
            if not isinstance(entry["file"], str):
                raise UsageError(
                    f"{where}: file must be a path string, got "
                    f"{entry['file']!r}")
            path = base_dir / entry["file"]
            notes = validate_notes(load_json(path), str(path))
            label = entry["file"]
        else:
            notes = validate_notes(entry["notes"], f"{where}: notes")
            label = f"target {index} (inline)"
        targets.append(Target(
            label=label,
            notes=notes,
            track_index=_require_index(entry, "track_index", where),
            clip_index=_require_index(entry, "clip_index", where),
            expect_track_name=_optional_str(entry, "expect_track_name", where),
            clip_name=_optional_str(entry, "clip_name", where),
            arrangement=_optional_bool(entry, "arrangement", where),
            clear=_optional_bool(entry, "clear", where),
        ))
    return targets


def build_targets(args: argparse.Namespace) -> List[Target]:
    paths = [Path(p) for p in args.files]
    docs = [(path, load_json(path)) for path in paths]
    kinds = [classify(doc, str(path)) for path, doc in docs]

    if all(kind == "notes" for kind in kinds):
        if args.track is None or args.clip is None:
            raise UsageError(
                "a note file needs --track and --clip to say where it goes "
                "(or push a manifest, which names its own targets)")
        notes: List[Dict[str, Any]] = []
        for path, doc in docs:
            notes.extend(validate_notes(doc, str(path)))
        return [Target(
            label=", ".join(path.name for path in paths),
            notes=notes,
            track_index=args.track,
            clip_index=args.clip,
            expect_track_name=args.expect_track_name,
            clip_name=args.clip_name,
            arrangement=args.arrangement,
            clear=args.clear,
        )]

    if len(docs) != 1:
        raise UsageError(
            "push either one manifest or note files, not a mix — a manifest "
            "names its own targets")
    per_clip_flags = (args.track is not None or args.clip is not None
                      or args.expect_track_name is not None
                      or args.clip_name is not None
                      or args.arrangement or args.clear)
    if per_clip_flags:
        raise UsageError(
            "a manifest names its own targets; --track, --clip, "
            "--expect-track-name, --clip-name, --arrangement and --clear "
            "belong on its entries instead")
    path, doc = docs[0]
    return targets_from_manifest(doc, path.parent, str(path))


# --------------------------------------------------------------------------
# Pushing and reporting
# --------------------------------------------------------------------------

def chunked(notes: Sequence[Dict[str, Any]], size: int) -> Iterator[List[Dict[str, Any]]]:
    for start in range(0, len(notes), size):
        yield list(notes[start:start + size])


def chunk_count(total: int, size: int) -> int:
    return (total + size - 1) // size


def describe(target: Target, chunk_size: int) -> str:
    guard = (f"guard '{target.expect_track_name}'"
             if target.expect_track_name is not None else "UNGUARDED")
    parts = [f"{target.label} -> {target.where()} ({guard}): "
             f"{len(target.notes)} notes in "
             f"{chunk_count(len(target.notes), chunk_size)} chunk(s)"]
    if target.clear:
        parts.append("clear first")
    if target.clip_name is not None:
        parts.append(f"then name it '{target.clip_name}'")
    return "; ".join(parts)


@dataclass
class Progress:
    """What has landed so far — printed after a failure, so the log says
    exactly which clips are complete and which one is half-written."""

    calls: int = 0
    notes_sent: int = 0
    clips_done: int = 0
    mismatches: List[str] = field(default_factory=list)
    unverified: List[str] = field(default_factory=list)

    def landed(self) -> str:
        return (f"Landed before that: {self.notes_sent} notes in {self.calls} "
                f"add call(s); {self.clips_done} clip(s) complete.")


def render_chunk_reply(result: Any, sent: int, label: str,
                       progress: Progress) -> str:
    """Say what Live reported for one chunk, and record a disagreement.

    Scripts ≥ 1.15.0 count the clip before and after and report ``added``
    (None when the clip could not be counted); older scripts echo the
    caller's own count back as ``note_count``, which verifies nothing.
    """
    if not isinstance(result, dict):
        progress.unverified.append(label)
        return f"sent {sent}, Live replied {result!r}"
    names = ""
    if "track_name" in result or "clip_name" in result:
        names = (f" — clip '{result.get('clip_name', '?')}' on "
                 f"'{result.get('track_name', '?')}'")
    if "added" in result:
        added = result["added"]
        total = result.get("clip_note_count")
        if added is None:
            progress.unverified.append(label)
            return (f"sent {sent}, Live could not count the clip "
                    f"(added unknown){names}")
        line = f"sent {sent}, Live added {added}, clip now holds {total}{names}"
        if added != sent:
            progress.mismatches.append(f"{label}: sent {sent}, added {added}")
            line += "   ** COUNT MISMATCH **"
        return line
    if "note_count" in result:
        progress.unverified.append(label)
        return (f"sent {sent}, Live echoed {result['note_count']} (a Remote "
                f"Script older than 1.15.0 only echoes the count; re-run "
                f"ableton-mcp-install-script to get a real one){names}")
    progress.unverified.append(label)
    return f"sent {sent}, Live replied {json.dumps(result)}"


def hello(live: LiveSocket, out: TextIO) -> None:
    """Report which Remote Script answered, so a stale install shows up in
    the log next to the counts it produced."""
    try:
        info = live.send("get_script_info")
    except LiveError as e:
        print(f"Connected to {live.host}:{live.port}; get_script_info was "
              f"refused ({e.message}) — a very old Remote Script?", file=out)
        return
    version = info.get("script_version", "?") if isinstance(info, dict) else "?"
    print(f"Connected to {live.host}:{live.port} — Remote Script {version}",
          file=out)


def push_target(live: LiveSocket, target: Target, chunk_size: int,
                progress: Progress, out: TextIO) -> None:
    print(describe(target, chunk_size), file=out)
    step = "before sending"
    try:
        if target.clear:
            step = "clear_notes_from_clip"
            result = live.send("clear_notes_from_clip", target.guard_params())
            cleared = (result.get("cleared_count", "?")
                       if isinstance(result, dict) else "?")
            print(f"  cleared {cleared} note(s)", file=out)

        total = chunk_count(len(target.notes), chunk_size)
        if total == 0:
            print("  no notes to send", file=out)
        for index, chunk in enumerate(chunked(target.notes, chunk_size), start=1):
            step = f"chunk {index}/{total} (add_notes_to_clip)"
            params = target.guard_params()
            params["notes"] = chunk
            params["expect_count"] = len(chunk)
            result = live.send("add_notes_to_clip", params)
            progress.calls += 1
            progress.notes_sent += len(chunk)
            line = render_chunk_reply(
                result, len(chunk), f"{target.label} chunk {index}/{total}",
                progress)
            print(f"  chunk {index}/{total}: {line}", file=out)
            out.flush()

        if target.clip_name is not None:
            command = ("set_arrangement_clip_name" if target.arrangement
                       else "set_clip_name")
            step = command
            params = target.guard_params(with_view=False)
            params["name"] = target.clip_name
            result = live.send(command, params)
            previous = (result.get("previous_name") if isinstance(result, dict)
                        else None)
            print(f"  named clip '{target.clip_name}'"
                  + (f" (was '{previous}')" if previous else ""), file=out)
    except (LiveError, ConnectionError, TimeoutError) as e:
        raise PushFailed(
            f"{target.label} -> {target.where()}, {step}: {e}") from e
    progress.clips_done += 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _index(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def build_parser(env: Mapping[str, str] = os.environ) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="push_notes.py",
        description=(
            "Push MIDI notes from JSON files straight to the AbletonMCP "
            "Remote Script socket, in guarded chunks, and print what Live "
            "reported for each."),
        epilog=(
            "FILE is either a note file (a JSON list of {pitch, start_time, "
            "duration, velocity, mute}), which needs --track and --clip, or "
            "a manifest ({\"targets\": [...]}) that names its own targets. "
            "Exit status 0: everything landed and the counts agree; 1: Live "
            "refused a command, the connection failed, or a count disagreed; "
            "2: bad input."),
    )
    parser.add_argument("files", metavar="FILE", nargs="+",
                        help="note file(s) for one clip, or one manifest")
    where = parser.add_argument_group("where note files go (manifests carry their own)")
    where.add_argument("--track", type=_index, metavar="N",
                       help="track index")
    where.add_argument("--clip", type=_index, metavar="N",
                       help="clip slot index, or the arrangement clip index "
                            "with --arrangement")
    where.add_argument("--expect-track-name", metavar="NAME",
                       help="refuse every write unless track N is really "
                            "named this (indices renumber when tracks are "
                            "added or deleted)")
    where.add_argument("--clip-name", metavar="NAME",
                       help="name the clip after the notes are in")
    where.add_argument("--arrangement", action="store_true",
                       help="the clip is on the Arrangement timeline, not in "
                            "a Session slot")
    where.add_argument("--clear", action="store_true",
                       help="clear the clip's notes first (writes are "
                            "additive)")
    how = parser.add_argument_group("how")
    how.add_argument("--chunk-size", type=_positive_int, metavar="N",
                     default=DEFAULT_CHUNK_SIZE,
                     help=f"notes per add_notes_to_clip call (default "
                          f"{DEFAULT_CHUNK_SIZE})")
    how.add_argument("--host", default=env.get("ABLETON_HOST", "127.0.0.1"),
                     help="Remote Script host (default: $ABLETON_HOST or "
                          "127.0.0.1)")
    how.add_argument("--port", type=int,
                     default=int(env.get("ABLETON_PORT", "9877")),
                     help="Remote Script port (default: $ABLETON_PORT or 9877)")
    how.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                     metavar="SECONDS",
                     help=f"how long to wait for each reply (default "
                          f"{DEFAULT_TIMEOUT:g})")
    how.add_argument("--dry-run", action="store_true",
                     help="validate and print the plan; connect to nothing")
    return parser


def main(argv: Optional[Sequence[str]] = None, out: TextIO = sys.stdout,
         err: TextIO = sys.stderr,
         env: Mapping[str, str] = os.environ) -> int:
    args = build_parser(env).parse_args(argv)

    try:
        targets = build_targets(args)
    except UsageError as e:
        print(f"push_notes: {e}", file=err)
        return 2

    total_notes = sum(len(target.notes) for target in targets)
    print(f"Plan: {total_notes} notes to {len(targets)} clip(s), in chunks of "
          f"at most {args.chunk_size}", file=out)
    for target in targets:
        print(f"  {describe(target, args.chunk_size)}", file=out)
    if args.dry_run:
        print("Dry run: nothing sent.", file=out)
        return 0

    progress = Progress()
    try:
        with LiveSocket(args.host, args.port, args.timeout).connect() as live:
            hello(live, out)
            for target in targets:
                push_target(live, target, args.chunk_size, progress, out)
    except (PushFailed, ConnectionError, TimeoutError) as e:
        out.flush()  # keep the log in order when both streams share a terminal
        print(f"FAILED: {e}", file=err)
        print(progress.landed(), file=err)
        if progress.calls and progress.clips_done < len(targets):
            print("The clip that failed may be half-written; clear it and "
                  "push it again.", file=err)
        return 1
    except KeyboardInterrupt:
        out.flush()
        print("Interrupted.", file=err)
        print(progress.landed(), file=err)
        return 130

    print(f"Done: {progress.notes_sent} notes to {progress.clips_done} "
          f"clip(s) in {progress.calls} add call(s).", file=out)
    if progress.unverified:
        print("Not verified by Live's own count: "
              + "; ".join(progress.unverified), file=out)
    if progress.mismatches:
        out.flush()
        print("Live's counts disagree with what was sent: "
              + "; ".join(progress.mismatches), file=err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
