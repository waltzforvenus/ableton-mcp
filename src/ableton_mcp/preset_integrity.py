"""Offline check for Live preset documents Live would refuse to load.

Live rejects a .adv/.adg whose XML breaks its list rules, and how it says
so is the problem: "Non-unique list ids" answers with a MODAL alert ("The
preset cannot be loaded.") that blocks Live's main thread, so the Remote
Script never replies and every later command queues behind it; "Not all
list members have Ids" fails silently, with no alert and no device. Reading
the file first turns both into a refusal before anything reaches Live.

The two rules, both measured against Live 12.4's own Core Library (0 hits
in 3,258 presets):

- siblings with the same tag in one list must not share an Id (different
  tags may: Live's presets reuse Ids across them);
- every device inside a <Device> holder carries an Id.

Generated presets break them by cloning a template child (its Id comes
along) or by lifting a device out of an .adv, whose root device has none.
"""

import gzip
import os
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Any, Dict, List, Optional

CHECKED_EXTENSIONS = (".adv", ".adg")


def list_id_problems(root: ET.Element, limit: int = 5) -> List[Dict[str, Any]]:
    """Up to ``limit`` rule breaks in a parsed preset document."""
    problems: List[Dict[str, Any]] = []
    for holder in root.iter("Device"):
        for child in holder:
            if child.get("Id") is None:
                problems.append({"rule": "missing_id", "list": "Device",
                                 "member": child.tag})
                if len(problems) >= limit:
                    return problems
    for el in root.iter():
        by_tag: Dict[str, List[str]] = defaultdict(list)
        for child in el:
            if child.get("Id") is not None:
                by_tag[child.tag].append(child.get("Id"))
        for tag, ids in by_tag.items():
            seen = set()
            for i in ids:
                if i in seen:
                    problems.append({"rule": "duplicate_id", "list": el.tag,
                                     "member": tag, "id": i})
                    break
                seen.add(i)
            if len(problems) >= limit:
                return problems
    return problems


def check_preset_file(path: str) -> Optional[Dict[str, Any]]:
    """None when ``path`` is fine or not a document this checks (plugin
    presets, missing files - the load itself reports those); otherwise
    {"path", "problems", "summary"} describing why Live would refuse it."""
    if not path.lower().endswith(CHECKED_EXTENSIONS) or not os.path.isfile(path):
        return None
    try:
        with gzip.open(path) as f:
            root = ET.fromstring(f.read())
    except (OSError, EOFError, ET.ParseError) as e:
        return {"path": path, "problems": [{"rule": "unreadable"}],
                "summary": f"not a readable Live document ({e})"}
    problems = list_id_problems(root)
    if not problems:
        return None
    first = problems[0]
    if first["rule"] == "missing_id":
        summary = (f"a {first['member']} inside <Device> has no Id "
                   "(Live: 'Not all list members have Ids')")
    else:
        summary = (f"two {first['member']} items in <{first['list']}> share "
                   f"Id {first['id']} (Live: 'Non-unique list ids')")
    return {"path": path, "problems": problems, "summary": summary}
