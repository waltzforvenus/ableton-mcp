"""Saved-envelope plans and explicit output-only copies. Never native CRUD/reload."""
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
    def __init__(self, read_xml=_read_xml, hash_file=media_hash, writer=None):
        self._writer = writer
        self._read_xml = read_xml
        self._hash_file = hash_file
        self._plans = {}
        self._mutex = threading.Lock()

    def read_saved(self, target, range=None, *, _verified_bytes=None):
        if set(target) != {'saved_path', 'pointee_id'}:
            raise ValueError('saved target requires only saved_path and unique pointee_id')
        path = target['saved_path']
        if _verified_bytes is None:
            before = self._hash_file(path)
            root = self._read_xml(path, max_bytes=8 * 1024 * 1024)
            if self._hash_file(path) != before:
                raise ValueError('saved file changed during read')
        else:
            # Internal descriptor-read bytes, never a caller assertion of state.
            before = hashlib.sha256(_verified_bytes).hexdigest()
            root = ET.fromstring(_verified_bytes)
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
        writing = bool(set(target) & {'output_path', 'backup_path', 'schema'})
        if writing and set(target) != {'saved_path', 'pointee_id', 'output_path', 'backup_path', 'schema'}:
            raise ValueError('writer target requires source, pointee_id, new output, backup and schema')
        read_target = {k: target[k] for k in ('saved_path', 'pointee_id')} if writing else target
        snapshot = self.read_saved(read_target)
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
        if writing:
            if self._writer is None:
                raise ValueError('saved writer not configured')
            binding, _ = self._writer.prepare(target, old, preview, operation)
            if binding['source_sha256'] != snapshot['file_sha256']:
                raise ValueError('source changed during writer plan')
            manifest.update(supported=True, apply_mode='saved_copy_only', writer_binding=binding,
                            reason='value-only new XML copy; no live edit or reload')
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
            if plan.get('apply_mode') == 'saved_copy_only':
                binding, output_data = self._writer.prepare(plan['target'], plan['before_points'],
                                                          plan['preview_points'], plan['operation'])
                if binding != plan['writer_binding']:
                    raise ValueError('stale source or backup writer binding')
                del self._plans[manifest_id]  # consume before possible publication
            else:
                output_data = None
        if output_data is not None:
            target = plan['target']
            try:
                verified_bytes = self._writer.publish(target['output_path'], output_data, binding['destination'])
                observed = self.read_saved({'saved_path': target['output_path'], 'pointee_id': target['pointee_id']},
                                           _verified_bytes=verified_bytes)
                if (observed['points'] != plan['preview_points']
                        or observed['unrelated_envelope_hashes'] != plan['unrelated_envelope_hashes']
                        or observed['file_sha256'] != binding['output_sha256']
                        or self._hash_file(target['saved_path']) != binding['source_sha256']
                        or self._hash_file(target['backup_path']) != binding['backup_sha256']):
                    raise ValueError('saved-copy verification mismatch')
                return dict(supported=True, applied=True, live_applied=False, output_path=target['output_path'],
                            output_sha256=binding['output_sha256'], backup_path=target['backup_path'],
                            reload_risk='may_reenable_overrides', retry_safe=False)
            except Exception as exc:
                return dict(applied=None, live_applied=False, reconciliation_required=True,
                            output_path=target['output_path'], destination_binding=binding['destination'],
                            reason=str(exc), retry_safe=False)
        return dict(supported=False, applied=False, reason=plan['reason'],
                    backup_required=True, reload_risk='may_reenable_overrides')
