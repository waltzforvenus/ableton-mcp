"""Read-only whole-track pagination measured against the final MCP result.

Cursors bind the entire returned snapshot and requested scope, not native object
identity. Changing any observed field invalidates them; this is conservative,
not a transactional or atomic native snapshot claim.
"""
import base64
import hashlib
import json
from mcp.types import CallToolResult, TextContent


def result_for(payload, mode):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return CallToolResult(content=[TextContent(type='text', text=encoded)] if mode == 'both' else [],
                          structuredContent=payload, isError=False)


def paginate(snapshot, scope, budget, cursor, mode):
    if type(budget) is not int or budget <= 0:
        raise ValueError('max_bytes budget must be a positive integer')
    rows = snapshot.get('tracks')
    if not isinstance(rows, list) or any(not isinstance(t, dict) for t in rows):
        raise ValueError('snapshot tracks are unknown; cannot paginate')
    if snapshot.get('truncated') or snapshot.get('omitted_tracks'):
        raise ValueError('cannot paginate an already truncated snapshot')
    digest = hashlib.sha256(json.dumps([snapshot, scope], sort_keys=True,
        ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    start = 0
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or len(cursor) > 512:
                raise ValueError()
            token = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
            if set(token) != {'digest', 'offset'} or token['digest'] != digest:
                raise ValueError()
            start = token['offset']
            if type(start) is not int or not 0 < start < len(rows):
                raise ValueError()
        except Exception as exc:
            raise ValueError('invalid or stale snapshot cursor; read a fresh first page') from exc

    def build(end):
        out = dict(snapshot)
        out['tracks'] = rows[start:end]
        out['requested_scope'] = scope
        out['omitted_fields'] = [name for flag, name in (
            ('include_notes', 'notes'), ('include_params', 'parameters'),
            ('include_warp_markers', 'warp_markers'), ('include_rack_chains', 'chains'),
            ('include_empty_slots', 'empty_clip_slots')) if not scope[flag]]
        out['omitted_tracks'] = [{'index': t.get('index')} for t in rows[end:]]
        out['next_cursor'] = (base64.urlsafe_b64encode(json.dumps(
            {'digest': digest, 'offset': end}, separators=(',', ':')).encode()).decode()
            if end < len(rows) else None)
        out['page_start'] = start
        out['budget_unit'] = 'utf8_call_tool_result_bytes'
        return result_for(out, mode)

    # Consider every whole-track prefix: cursor/omission metadata makes size
    # non-monotonic near the final page. No arbitrary one-track overflow.
    best = None
    for end in range(start + (1 if rows else 0), len(rows) + 1):
        candidate = build(end)
        if len(candidate.model_dump_json(by_alias=True, exclude_none=True).encode('utf-8')) <= budget:
            best = candidate
    if best is None:
        raise ValueError('snapshot budget cannot fit the envelope and one whole track; increase max_bytes or narrow tracks')
    return best
