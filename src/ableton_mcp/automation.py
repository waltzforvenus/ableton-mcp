"""Read-only saved-envelope previews. No live CRUD or implicit reload authority."""
import copy
import hashlib
import json
import math
import threading
import uuid
import xml.etree.ElementTree as ET
from .plugin_state import _read_xml
from .arrangement_edits import media_hash


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def points_checked(points):
    if not isinstance(points, list) or len(points) > 10000:
        raise ValueError('points must be a list of at most10000')
    result = []
    for point in points:
        if not isinstance(point, dict) or set(point) != {'time', 'value'}:
            raise ValueError('only explicit time/value points supported; curve editing unavailable')
        if any(isinstance(point[k], bool) for k in point):
            raise ValueError('point values must be finite numbers')
        t, v = float(point['time']), float(point['value'])
        if not math.isfinite(t) or not math.isfinite(v):
            raise ValueError('point values must be finite numbers')
        result.append({'time': t, 'value': v})
    if any(a['time'] >= b['time'] for a, b in zip(result, result[1:])):
        raise ValueError('points must have strictly increasing unique times')
    return result


class Automation:
    def __init__(self, read_xml=_read_xml, hash_file=media_hash):
        self._read_xml = read_xml
        self._hash_file = hash_file
        self._plans = {}
        self._mutex = threading.Lock()

    def read_saved(self, target, range=None):
        if set(target) != {'saved_path', 'pointee_id'}:
            raise ValueError('saved target requires only saved_path and unique pointee_id')
        path = target['saved_path']
        before = self._hash_file(path)
        root = self._read_xml(path, max_bytes=8 * 1024 * 1024)
        if self._hash_file(path) != before:
            raise ValueError('saved file changed during read')
        envelopes = list(root.iter('AutomationEnvelope'))
        matches = [e for e in envelopes if e.find('EnvelopeTarget/PointeeId') is not None
                   and e.find('EnvelopeTarget/PointeeId').get('Value') == str(target['pointee_id'])]
        if len(matches) != 1:
            raise ValueError('saved target must identify exactly one envelope')
        selected = matches[0]
        events = selected.find('Automation/Events')
        if events is None or any(len(e) or e.tag != 'FloatEvent' or set(e.attrib) - {'Time', 'Value', 'Id'} for e in events):
            raise ValueError('unsupported event/curve schema; no reconstruction')
        points = points_checked([{'time': e.get('Time'), 'value': e.get('Value')} for e in events])
        # Exact raw envelope hashes bind every unrelated envelope, even unknown types.
        others = {str(i): hashlib.sha256(ET.tostring(e)).hexdigest()
                  for i, e in enumerate(envelopes) if e is not selected}
        result = dict(supported=True, source='saved_copy', envelope_present=bool(points),
                      overridden=None, coverage='saved_envelope_only', points=points,
                      file_sha256=before, unrelated_envelope_hashes=others,
                      interpolation='not_inferred', reload_risk='may_reenable_overrides')
        if range is not None:
            if not isinstance(range, dict) or set(range) != {'start', 'end'}:
                raise ValueError('range requires start/end beats')
            lo, hi = float(range['start']), float(range['end'])
            if not all(math.isfinite(v) for v in (lo, hi)) or lo >= hi:
                raise ValueError('invalid range')
            result.update(points=[p for p in points if lo <= p['time'] < hi],
                          coverage='saved_events_in_half_open_range', range={'start': lo, 'end': hi})
        return result

    def plan(self, target, points, operation):
        if operation not in ('insert', 'replace', 'delete'):
            raise ValueError('explicit insert/replace/delete operation required')
        proposed = points_checked(points)
        if (operation == 'delete' and proposed) or (operation != 'delete' and not proposed):
            raise ValueError('delete requires no points; insert/replace require points')
        snapshot = self.read_saved(target)
        old = snapshot['points']
        preview = proposed
        if operation == 'insert':
            if {p['time'] for p in old} & {p['time'] for p in proposed}:
                raise ValueError('insert cannot replace an existing point; explicit replace required')
            preview = sorted(old + proposed, key=lambda p: p['time'])
        times = [p['time'] for p in old + preview]
        manifest = dict(supported=False, preview_supported=True, source='saved_copy',
                        operation=operation, target=copy.deepcopy(target), before_points=old,
                        preview_points=preview, affected_range=[min(times), max(times)] if times else None,
                        file_sha256=snapshot['file_sha256'],
                        unrelated_envelope_hashes=snapshot['unrelated_envelope_hashes'],
                        reload_risk='may_reenable_overrides', interpolation='not_inferred',
                        reason='lossless live envelope CRUD unavailable; saved-copy diff only, no file write/reload',
                        backup_required=True, manifest_id=uuid.uuid4().hex)
        manifest['fingerprint'] = fingerprint(manifest)
        with self._mutex:
            if len(self._plans) >= 64:
                self._plans.pop(next(iter(self._plans)))
            self._plans[manifest['manifest_id']] = copy.deepcopy(manifest)
        return manifest

    def apply(self, manifest_id, fingerprint):
        with self._mutex:
            plan = self._plans.get(manifest_id)
            if plan is None or plan['fingerprint'] != fingerprint:
                raise ValueError('unknown/stale automation manifest')
            if self._hash_file(plan['target']['saved_path']) != plan['file_sha256']:
                raise ValueError('stale saved automation copy')
        return dict(supported=False, applied=False, reason=plan['reason'],
                    backup_required=True, reload_risk='may_reenable_overrides')
