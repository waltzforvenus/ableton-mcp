"""Model: read a hosted plugin's saved state out of a Live file, offline.

Live's API exposes nothing of a VST3/AU plugin's internal state — no chunk,
no model or impulse-response path — so "did the amp sim really load that
capture?" could only be answered by saving the set and hex-decoding its XML
by hand. This module does that decode: it reads a ``.als`` set or an
``.adv``/``.adg`` preset (gzipped XML), or a VST3 ``.vstpreset``, finds each
hosted plugin's state blob, and returns its size, hash and the readable
strings inside it (where plugins keep file paths, model names, and often a
JSON or XML document of their own).

Pure file reading: it never touches Live, the socket, or the network, and
never writes. Plugins' blob formats are their own; this does not interpret
them beyond pulling out text, so it is a verification aid, not an editor.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import os
import re
import struct
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterator, List, Optional, Tuple

# Device element tags of hosted plugins in Live's XML.
PLUGIN_TAGS = ("PluginDevice", "AuPluginDevice")
# Track element tags (MainTrack is Live 12's name for the master).
TRACK_TAGS = ("AudioTrack", "MidiTrack", "ReturnTrack", "GroupTrack",
              "MasterTrack", "MainTrack", "PreHearTrack")
# Elements whose text is a plugin state blob.
STATE_TAGS = ("ProcessorState", "ControllerState", "Buffer")

# Printable runs worth reporting; paths and JSON keys are usually ASCII.
_PRINTABLE = re.compile(rb"[\x20-\x7e]{6,}")
_UTF16_PRINTABLE = re.compile(rb"(?:[\x20-\x7e]\x00){6,}")
# Absolute paths ending in a file extension. Spaces are allowed (real model
# and IR folders have them); the match ends at the string's end or at a
# quote, comma, semicolon, brace or bracket after the extension.
_PATHLIKE = re.compile(r"(?:[A-Za-z]:)?[/\\][^\"'<>|]*?\.[A-Za-z0-9]{1,8}(?=$|[\"',;}\]\s])")

MAX_STRINGS = 200


class PluginStateError(ValueError):
    """The file cannot be read as a Live set/preset or VST3 preset."""


def _decode_blob(text: str) -> bytes:
    """Live writes state blobs as hex (whitespace-wrapped); fall back to
    base64 for any build or plugin type that does not."""
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return b""
    try:
        return binascii.unhexlify(compact)
    except (binascii.Error, ValueError):
        try:
            return base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError):
            return compact.encode("utf-8", "replace")


def _strings(blob: bytes) -> List[str]:
    found: List[str] = []
    seen = set()
    for m in _PRINTABLE.finditer(blob):
        s = m.group().decode("ascii")
        if s not in seen:
            seen.add(s)
            found.append(s)
    for m in _UTF16_PRINTABLE.finditer(blob):
        s = m.group().decode("utf-16-le", "replace")
        if s not in seen:
            seen.add(s)
            found.append(s)
    return found[:MAX_STRINGS]


def _blob_summary(blob: bytes) -> Dict[str, Any]:
    strings = _strings(blob)
    paths: List[str] = []
    for s in strings:
        for m in _PATHLIKE.finditer(s):
            if m.group() not in paths:
                paths.append(m.group())
    return {
        "bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "paths": paths,
        "strings": strings,
        "strings_truncated": len(strings) >= MAX_STRINGS,
    }


def _value(el: Optional[ET.Element], tag: str) -> Optional[str]:
    if el is None:
        return None
    child = el.find(tag)
    return child.get("Value") if child is not None else None


def _plugin_name(device: ET.Element) -> Optional[str]:
    desc = device.find("PluginDesc")
    if desc is None:
        return None
    for info in desc:
        for tag in ("Name", "PlugName"):
            name = _value(info, tag)
            if name:
                return name
    return None


def _track_name(track: ET.Element) -> Optional[str]:
    name = track.find("Name")
    return (_value(name, "UserName") or _value(name, "EffectiveName")) if name is not None else None


def _device_states(device: ET.Element) -> Dict[str, Any]:
    states: Dict[str, Any] = {}
    preset = device.find("Preset")
    scope = preset if preset is not None else device
    for el in scope.iter():
        if el.tag in STATE_TAGS and (el.text or "").strip():
            key = el.tag if el.tag not in states else f"{el.tag}_{len(states)}"
            states[key] = _blob_summary(_decode_blob(el.text))
    return states


def _walk_devices(scope: ET.Element) -> Iterator[ET.Element]:
    for el in scope.iter():
        if el.tag in PLUGIN_TAGS:
            yield el


def _read_xml(path: str) -> ET.Element:
    with open(path, "rb") as f:
        head = f.read(2)
    try:
        if head == b"\x1f\x8b":
            with gzip.open(path, "rb") as f:
                data = f.read()
        else:
            with open(path, "rb") as f:
                data = f.read()
        return ET.fromstring(data)
    except (OSError, ET.ParseError, EOFError) as e:
        raise PluginStateError(f"{path} is not a readable Live set or preset: {e}")


def _read_vstpreset(path: str) -> Dict[str, Any]:
    """A VST3 .vstpreset: 'VST3' header, version, 32-char class id, chunk
    list offset; the list names 'Comp' (processor) and 'Cont' (controller)
    chunks by offset and size."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 48 or data[:4] != b"VST3":
        raise PluginStateError(f"{path} is not a VST3 preset (no VST3 header)")
    class_id = data[8:40].decode("ascii", "replace")
    (list_offset,) = struct.unpack("<q", data[40:48])
    chunks: Dict[str, Any] = {}
    if 0 < list_offset <= len(data) - 8 and data[list_offset:list_offset + 4] == b"List":
        (count,) = struct.unpack("<i", data[list_offset + 4:list_offset + 8])
        pos = list_offset + 8
        for _ in range(max(0, min(count, 64))):
            if pos + 20 > len(data):
                break
            cid = data[pos:pos + 4].decode("ascii", "replace")
            off, size = struct.unpack("<qq", data[pos + 4:pos + 20])
            pos += 20
            blob = data[off:off + size] if 0 <= off <= len(data) else b""
            name = {"Comp": "ProcessorState", "Cont": "ControllerState"}.get(cid, cid)
            chunks[name] = _blob_summary(blob)
    return {"source": path, "kind": "vstpreset", "plugins": [{
        "track": None, "plugin": None, "class_id": class_id, "ordinal": 0,
        "states": chunks}]}


def read_plugin_states(path: str, track: Optional[str] = None,
                       plugin: Optional[str] = None) -> Dict[str, Any]:
    """Every hosted plugin's state in ``path``, optionally filtered by track
    name and plugin name (case-insensitive substrings).

    ``ordinal`` counts plugins per track in document order (nested racks
    included), which is chain order for a flat chain."""
    path = os.path.expanduser(path or "")
    if not os.path.isfile(path):
        raise PluginStateError(f"no such file: {path!r}")
    if path.lower().endswith(".vstpreset"):
        return _read_vstpreset(path)
    root = _read_xml(path)
    plugins: List[Dict[str, Any]] = []
    tracks = [t for t in root.iter() if t.tag in TRACK_TAGS]
    scopes: List[Tuple[Optional[str], ET.Element]] = (
        [(_track_name(t) or t.tag, t) for t in tracks] if tracks else [(None, root)])
    want_track = (track or "").strip().lower()
    want_plugin = (plugin or "").strip().lower()
    for track_name, scope in scopes:
        if want_track and want_track not in (track_name or "").lower():
            continue
        for ordinal, device in enumerate(_walk_devices(scope)):
            name = _plugin_name(device)
            if want_plugin and want_plugin not in (name or "").lower():
                continue
            plugins.append({"track": track_name, "plugin": name,
                            "device_tag": device.tag, "ordinal": ordinal,
                            "states": _device_states(device)})
    return {"source": path, "kind": os.path.splitext(path)[1].lstrip(".").lower(),
            "plugin_count": len(plugins), "plugins": plugins}
