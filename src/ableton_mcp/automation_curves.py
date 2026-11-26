"""Lexical curve qualification, not a playback evaluator.

The four serialized controls are observed by dawtool's Ableton parser. Neither
handle normalization/segment ownership nor negative sentinel playback semantics
are qualified here. Preserve them, never guess an interpolation function.
"""
import math

CONTROLS = {'CurveControl1X', 'CurveControl1Y', 'CurveControl2X', 'CurveControl2Y'}


def finite(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not a numeric sample')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('finite numeric sample required')
    return value


def inspect_curve_xml(root, pointee_id, sample_times):
    if not isinstance(sample_times, list) or len(sample_times) > 1024:
        raise ValueError('sample_times must be a list of at most1024')
    times = [finite(t) for t in sample_times]
    matches = []
    for envelope in root.iter('AutomationEnvelope'):
        targets = envelope.findall('EnvelopeTarget/PointeeId')
        if any(t.get('Value') == str(pointee_id) for t in targets):
            if len(targets) != 1:
                raise ValueError('ambiguous target mapping')
            matches.append(envelope)
    if len(matches) != 1:
        raise ValueError('exactly one envelope required')
    containers = matches[0].findall('Automation/Events')
    if len(containers) != 1 or len(containers[0]) > 10000:
        raise ValueError('unknown or unbounded event layout')
    events = []
    for e in containers[0]:
        attrs = set(e.attrib)
        if (e.tag != 'FloatEvent' or len(e) or not {'Time','Value'} <= attrs
                or attrs - ({'Time','Value','Id'} | CONTROLS)
                or (attrs & CONTROLS and attrs & CONTROLS != CONTROLS)):
            raise ValueError('unknown/partial curve schema')
        row = dict(time=finite(e.get('Time')), value=finite(e.get('Value')),
                   id=e.get('Id'), curve_controls={k: finite(e.get(k)) for k in sorted(attrs & CONTROLS)})
        if events and row['time'] <= events[-1]['time']:
            raise ValueError('duplicate or unordered event times')
        events.append(row)
    by_time = {e['time']: e['value'] for e in events}
    nonnegative = [e['time'] for e in events if e['time'] >= 0]
    samples = []
    for t in times:
        if t < 0:
            status = 'negative_time_semantics_unqualified'
        elif t in by_time:
            status = 'stored_event_only'
        elif nonnegative and nonnegative[0] < t < nonnegative[-1]:
            status = 'interpolation_unsupported'
        else:
            status = 'extrapolation_unsupported'
        samples.append(dict(time=t, status=status, stored_value=by_time[t] if status=='stored_event_only' else None,
                            evaluated_value=None))
    return dict(source='saved_copy', schema_qualified=True, native_qualified=False,
                evaluation_supported=False, coverage='serialized_events_only',
                version_metadata={k: root.get(k) for k in ('MajorVersion','MinorVersion','Creator','Revision','SchemaChangeCount')},
                events=events, samples=samples, overridden=None,
                missing_evidence=['version-qualified playback fixtures', 'handle normalization and segment ownership',
                                  'interpolation and sentinel semantics'],
                warning='Stored events/controls are not evaluated playback or current Live overrides')
